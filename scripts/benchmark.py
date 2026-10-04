"""离线检索评测（benchmark）：混合检索的消融表 + 状态一致率。

==================== 它回答什么问题 ====================
论文标题里的"混合 RAG"必须有可量化的证据。这个脚本产出四组数字：

    ① 排序质量   Recall@1/3/5、MRR、nDCG@5 —— 相关的东西有没有排在前面
    ② 消融对比   只用关键词 / 只用语义 / RRF 融合 / 加权融合 / 融合但不重排
    ③ 装配成本   注入条数、注入 token（均值/中位）、去重率、丢弃条数
    ④ 状态一致率 模型自报的状态里，有多少被原样接受 / 被修正 / 被丢弃

==================== 三条硬约定 ====================
1. **绝不调用真实大模型**（项目红线）：排序指标只用本地数据算；
   语义通道的相似度默认取自标注（`scripts/benchmark_data/retrieval_cases.json`），
   想用真实嵌入模型时加 `--semantic embedding`（走本地 onnx，不联网、不花钱）。
2. **可复现**：同样的输入必须给出同样的数字；不依赖随机数、不需要数据库。
3. **不骗人**：数据集是**合成小集**（12 条 query / 10 条世界书条目 / 10 条记忆），
   脚本自己会把这句话打在报告里 —— 它证明的是"融合与重排的机制成立"，
   不等于"在真实语料上的绝对效果"。真实语料回放走 `--from-db`（见 README/§20）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\benchmark.py
    .\\.venv\\Scripts\\python.exe scripts\\benchmark.py --k 1,3,5 --json data/benchmark_report.json
    .\\.venv\\Scripts\\python.exe scripts\\benchmark.py --semantic embedding   # 需已下载嵌入模型
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.narrative import retrieval as rtv  # noqa: E402
from app.narrative import retrieval_metrics as metrics  # noqa: E402
from app.narrative import state_schema as schema_mod  # noqa: E402

DATA_DIR = PROJECT_ROOT / "scripts" / "benchmark_data"
DEFAULT_CASES = DATA_DIR / "retrieval_cases.json"
DEFAULT_STATE_CASES = DATA_DIR / "state_cases.json"
DEFAULT_DRIFT_SCRIPT = DATA_DIR / "drift_script.json"

#: 消融策略（键 → (融合模式, 重排模式)）
ABLATIONS: dict[str, tuple[str, str]] = {
    "keyword_only": (rtv.MODE_KEYWORD, rtv.RERANK_TIEBREAK),
    "semantic_only": (rtv.MODE_SEMANTIC, rtv.RERANK_TIEBREAK),
    "rrf_no_rerank": (rtv.MODE_RRF, rtv.RERANK_OFF),
    "rrf(+tiebreak)": (rtv.MODE_RRF, rtv.RERANK_TIEBREAK),
    "weighted(+tiebreak)": (rtv.MODE_WEIGHTED, rtv.RERANK_TIEBREAK),
    "rrf(+blend 词面重排)": (rtv.MODE_RRF, rtv.RERANK_BLEND),
}

#: 共享预算：与运行时默认一致（世界书没配 token_budget 时用它）
BUDGET = rtv.DEFAULT_BUDGET
#: 语义通道取多少条（与运行时默认一致）
TOP_K = rtv.DEFAULT_SEMANTIC_TOP_K


# ==================================================================
#  数据加载
# ==================================================================
def load_cases(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class DictBook:
    """把数据集里的 book_entries 伪装成世界书对象（world_book_scanner 只要求有 entries）。"""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries


class DictMsg:
    def __init__(self, content: str) -> None:
        self.content = content


# ==================================================================
#  语义通道：两种模式
# ==================================================================
def semantic_from_labels(query_case: dict[str, Any], memory_keys: list[str]) -> list[rtv.Candidate]:
    """从标注里读相似度（模拟向量库返回的 top-k），按相似度降序 → 名次。"""
    similarity = query_case.get("similarity") or {}
    scored = [
        (key, float(score))
        for key, score in similarity.items()
        if key in memory_keys and float(score) > 0
    ]
    scored.sort(key=lambda item: (-item[1], item[0]))
    out: list[rtv.Candidate] = []
    texts = {m["key"]: m["text"] for m in query_case["_memories"]}
    for rank, (key, score) in enumerate(scored[:TOP_K], start=1):
        text = texts.get(key, "")
        out.append(
            rtv.Candidate(
                key=key,
                text=text,
                source="memory",
                raw=key,
                tokens=rtv.estimate_tokens(text),
                semantic_score=score,
                semantic_rank=rank,
                priority=1.0,
            )
        )
    return out


def build_embedding_scorer() -> Any:
    """真实嵌入模型打分（本地 onnx，不联网）。模型没下载时直接报错退出。"""
    from app.db.chroma import get_embedding

    embedding = get_embedding()

    def _score(query: str, memory_keys: list[str], memories: list[dict[str, Any]]):
        import math

        query_vector = embedding.embed_query(query)
        vectors = embedding.embed_documents([m["text"] for m in memories])

        def _cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(y * y for y in b)) or 1.0
            return dot / (na * nb)

        scored = [
            (memory["key"], _cosine(query_vector, vector))
            for memory, vector in zip(memories, vectors)
        ]
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:TOP_K]

    return _score


# ==================================================================
#  跑一遍消融
# ==================================================================
def run_ablation(
    data: dict[str, Any],
    *,
    ks: list[int],
    semantic_mode: str,
) -> tuple[dict[str, dict[str, float]], dict[str, Any], list[dict[str, Any]]]:
    entries = data["book_entries"]
    memories = data["memories"]
    memory_keys = [m["key"] for m in memories]
    book = DictBook(entries)
    embedding_scorer = build_embedding_scorer() if semantic_mode == "embedding" else None

    # 每个查询先算一次两路候选（消融之间共用，保证只变"融合策略"这一个变量）
    per_query: list[dict[str, Any]] = []
    for case in data["queries"]:
        query = case["query"]
        messages = [DictMsg(query)]
        lexical = rtv.keyword_candidates(book, messages, scan_depth=1)
        if embedding_scorer is not None:
            similarities = embedding_scorer(query, memory_keys, memories)
            texts = {m["key"]: m["text"] for m in memories}
            semantic = [
                rtv.Candidate(
                    key=key,
                    text=texts[key],
                    source="memory",
                    raw=key,
                    tokens=rtv.estimate_tokens(texts[key]),
                    semantic_score=score,
                    semantic_rank=rank,
                    priority=1.0,
                )
                for rank, (key, score) in enumerate(similarities, start=1)
                if score > 0
            ]
        else:
            semantic = semantic_from_labels({**case, "_memories": memories}, memory_keys)
        per_query.append(
            {"case": case, "query": query, "lexical": lexical, "semantic": semantic}
        )

    ablation: dict[str, dict[str, float]] = {}
    costs: dict[str, dict[str, float]] = {}
    samples: list[dict[str, Any]] = []
    for name, (mode, rerank_mode) in ABLATIONS.items():
        ranked_cases: list[metrics.RankedCase] = []
        rows: list[dict[str, Any]] = []
        for item in per_query:
            result = rtv.retrieve(
                query=item["query"],
                mode=mode,
                rerank_mode=rerank_mode,
                budget=BUDGET,
                keyword_provider=lambda item=item: item["lexical"],
                semantic_provider=lambda item=item: (item["semantic"], []),
            )
            # ★ 排序指标用**完整名次**（含被预算砍掉的候选）：这里评的是检索/融合/重排，
            #   预算的影响单独由"装配成本"那一组指标体现。
            ranked = [c.key for c in result.kept] + [
                c.key for c in sorted(result.dropped, key=lambda c: -c.score)
            ]
            ranked_cases.append(
                metrics.RankedCase(
                    name=item["query"], ranked=ranked, relevant=list(item["case"]["relevant"])
                )
            )
            rows.append(result.to_dict())
            if name == "rrf(+tiebreak)":
                samples.append(
                    {
                        "query": item["query"],
                        "relevant": item["case"]["relevant"],
                        "summary": rtv.describe(result),
                        "items": rtv.debug_lines(result, limit=6),
                    }
                )
        ablation[name] = {**metrics.ranking_report(ranked_cases, ks=ks), **metrics.injection_report(rows)}
        costs[name] = metrics.injection_report(rows)
    return ablation, costs, samples


# ==================================================================
#  输出
# ==================================================================
RANK_COLUMNS = ("recall@1", "recall@3", "recall@5", "mrr", "ndcg@5")
COST_COLUMNS = ("注入条数(均)", "注入token(均)", "注入token(中位)", "去重率", "丢弃条数(均)")


def print_report(
    *, ablation: dict[str, dict[str, float]], state: dict[str, Any], semantic_mode: str, ks: list[int]
) -> str:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("混合检索评测（离线合成标注集 · 不调用任何大模型）")
    lines.append(f"语义通道：{'真实嵌入模型（本地 onnx）' if semantic_mode == 'embedding' else '标注相似度（模拟理想向量检索）'}")
    lines.append(f"共享预算：{BUDGET} token/轮   语义 top-k：{TOP_K}   指标：k={ks}")
    lines.append("=" * 78)

    lines.append("\n[1] 排序质量（消融对比，越高越好；完整名次含被预算砍掉的候选）")
    header = "| 策略 | " + " | ".join(RANK_COLUMNS) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(RANK_COLUMNS) + 1))
    for name, row in ablation.items():
        cells = " | ".join(f"{row.get(col, 0.0):.4f}" for col in RANK_COLUMNS)
        lines.append(f"| `{name}` | {cells} |")

    lines.append("\n[2] 装配成本（共享一个 token 预算的代价）")
    lines.append("| 策略 | " + " | ".join(COST_COLUMNS) + " |")
    lines.append("|" + "---|" * (len(COST_COLUMNS) + 1))
    for name, row in ablation.items():
        cells = " | ".join(
            f"{row.get(col, 0.0):.2f}" if col != "去重率" else f"{row.get(col, 0.0):.1%}"
            for col in COST_COLUMNS
        )
        lines.append(f"| `{name}` | {cells} |")

    lines.append("\n[3] 状态一致性校验（模型自报状态的下场）")
    lines.append(
        f"    用例 {state['total']} 条：原样接受 {state['counts'].get(metrics.OUTCOME_UNCHANGED, 0)} 条"
        f"（{state['原样接受率']:.1%}）· 被修正 {state['counts'].get(metrics.OUTCOME_CORRECTED, 0)} 条"
        f"（{state['被修正率']:.1%}）· 被丢弃 {state['counts'].get(metrics.OUTCOME_REJECTED, 0)} 条"
        f"（{state['被丢弃率']:.1%}）"
    )
    if state["判据不符"]:
        lines.append("    ★ 有用例的下场与标注不符（判据可能被改动）：")
        for item in state["判据不符"]:
            lines.append(f"      - {item}")

    lines.append("\n[4] 说明（写进论文时必须带上）")
    lines.append("    · 数据集为**合成小集**（12 query / 10 世界书条目 / 10 记忆），")
    lines.append("      证明的是「融合/去重/重排机制成立」，不代表真实语料上的绝对效果。")
    lines.append("    · 语义通道默认用标注相似度（模拟理想向量检索），故该列是融合上限；")
    lines.append("      --semantic embedding 可用本地嵌入模型替换成真实分数。")
    lines.append("    · 排序指标用完整名次；注入条数/token 才受共享预算影响，两组不要混着读。")
    lines.append("    · `rrf(+blend 词面重排)` 是**负面对照**：它证明「用字面重叠重排会伤语义召回」，")
    lines.append("      所以运行时默认走 `tiebreak`（只在融合分打平时才微调，量级 1e-6）。")
    text = "\n".join(lines)
    print(text)
    return text


# ==================================================================
#  真实数据只读回放（--from-db）
# ==================================================================
#: 回放时每个会话最多回看几轮（默认只回放最后几轮，避免把几个小时的历史全跑一遍）
DEFAULT_REPLAY_ROUNDS = 3
#: 默认最多回放多少个会话
DEFAULT_REPLAY_SESSIONS = 20


class ReplayRow:
    """一次"历史回放"的输入：某会话在某一轮的上下文 + 真实历史。"""

    def __init__(
        self,
        *,
        session_id: int,
        user_id: int,
        username: str,
        title: str,
        query: str,
        history: list[Any],
        book: Any,
        scan_depth: int,
        token_budget: int,
    ) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.username = username
        self.title = title
        self.query = query
        self.history = history
        self.book = book
        self.scan_depth = scan_depth
        self.token_budget = token_budget


def load_replay_rows(
    *,
    usernames: list[str] | None = None,
    limit_sessions: int = DEFAULT_REPLAY_SESSIONS,
    rounds: int = DEFAULT_REPLAY_ROUNDS,
) -> tuple[list[ReplayRow], dict[str, Any]]:
    """从**真实数据库**里读出可回放的会话（**只 SELECT，绝不写**）。

    ★ 为什么只读：本项目红线 —— 库里**真实用户**的数据一律不动。
      这里全程只用 ORM 的查询接口，没有任何 add / commit / delete。

    ★ 回放的是什么：取每个会话**最后 `rounds` 条用户消息**，
      用"那一刻的上下文"重跑一遍检索，看真实语料下注入了多少条、多少 token。
      这不改变任何已经发生的事，只是把同一套管道在真实分布上再算一遍。
    """
    from sqlalchemy import select

    from app.db.models import CharacterCard, Message, NarrativeSession, User, WorldBook
    from app.db.mysql import session_scope
    from app.narrative import state as state_mod

    rows: list[ReplayRow] = []
    meta: dict[str, Any] = {
        "sessions_scanned": 0,
        "sessions_used": 0,
        "messages_scanned": 0,
        "state_messages": 0,
        "state_missing": 0,
        "state_legacy_messages": 0,
        "state_sessions": 0,
        "state_idempotent": 0,
        "state_not_idempotent": 0,
    }

    with session_scope() as db:
        query = select(NarrativeSession).where(NarrativeSession.kind == "story")
        if usernames:
            user_ids = list(
                db.scalars(select(User.id).where(User.username.in_(usernames)))
            )
            query = query.where(NarrativeSession.user_id.in_(user_ids))
        query = query.order_by(NarrativeSession.id.desc()).limit(max(limit_sessions, 1))
        sessions = list(db.scalars(query))

        for session in sessions:
            meta["sessions_scanned"] += 1
            messages = list(
                db.scalars(
                    select(Message)
                    .where(Message.session_id == session.id)
                    .order_by(Message.id.asc())
                )
            )
            meta["messages_scanned"] += len(messages)
            if not messages:
                continue

            card = (
                db.get(CharacterCard, session.character_card_id)
                if session.character_card_id
                else None
            )
            book = (
                db.get(WorldBook, card.world_book_id)
                if card is not None and card.world_book_id
                else None
            )
            user_row = db.get(User, session.user_id)
            username = str(getattr(user_row, "username", "") or "")

            # ---- 状态一致性（真实数据）：有 schema 的会话，其落库状态必须"再校验一遍也不变"
            schema = state_mod.load_schema(session)
            if not schema_mod.is_empty(schema):
                stored = state_mod.load_state(session)
                if stored:
                    meta["state_sessions"] += 1
                    _state, notes = state_mod.normalize(stored, stored, schema)
                    if notes:
                        meta["state_not_idempotent"] += 1
                    else:
                        meta["state_idempotent"] += 1
                # ★ 「漏输出率」必须读**遥测**（messages.state_meta_json），不能看正文：
                #   `<state>` 块在落库前就被剥掉了，所以"正文里没有 <state>"恒成立 ——
                #   第七轮那版指标因此永远是 100%，是错的（第十一轮修正）。
                for index, message in enumerate(messages):
                    if message.role != "assistant" or index == 0:
                        continue  # 第 0 条是开场白，本来就不要求状态块
                    raw_meta = getattr(message, "state_meta_json", None)
                    if not raw_meta:
                        meta["state_legacy_messages"] += 1
                        continue
                    try:
                        payload = json.loads(raw_meta)
                    except (TypeError, ValueError):
                        meta["state_legacy_messages"] += 1
                        continue
                    if not payload.get("required", True):
                        continue
                    meta["state_messages"] += 1
                    if not payload.get("had_block", True):
                        meta["state_missing"] += 1

            from app.narrative import world_book_scanner

            scan_depth, token_budget = world_book_scanner.resolve_settings(book)
            user_indexes = [
                index for index, m in enumerate(messages) if m.role == "user"
            ][-max(rounds, 1) :]
            if not user_indexes:
                continue
            meta["sessions_used"] += 1
            for index in user_indexes:
                rows.append(
                    ReplayRow(
                        session_id=session.id,
                        user_id=session.user_id,
                        username=username,
                        title=str(session.title or ""),
                        query=str(messages[index].content or ""),
                        history=messages[: index + 1],
                        book=book,
                        scan_depth=scan_depth,
                        token_budget=token_budget,
                    )
                )
    return rows, meta


def replay_from_db(
    *,
    usernames: list[str] | None = None,
    limit_sessions: int = DEFAULT_REPLAY_SESSIONS,
    rounds: int = DEFAULT_REPLAY_ROUNDS,
    semantic_mode: str = "auto",
    mode: str = rtv.DEFAULT_MODE,
    rerank_mode: str = rtv.DEFAULT_RERANK,
    samples: int = 3,
) -> dict[str, Any]:
    """在真实语料上做一次只读回放，返回（可 JSON 序列化的）结果。

    `semantic_mode`：
      · `none`      只回放关键词通道（不碰向量库，最快）
      · `embedding` 用真实向量库召回（需要嵌入模型可用）
      · `auto`      先按 embedding 试，失败就如实记进 `errors` 并继续（默认）
    """
    rows, meta = load_replay_rows(
        usernames=usernames, limit_sessions=limit_sessions, rounds=rounds
    )
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    preview: list[dict[str, Any]] = []
    for row in rows:
        use_semantic = semantic_mode in ("embedding", "auto")
        result = rtv.retrieve(
            book=row.book,
            history=row.history,
            query=row.query,
            user_id=None if semantic_mode == "none" else row.user_id,
            session_id=row.session_id,
            scan_depth=row.scan_depth,
            budget=row.token_budget if row.token_budget > 0 else rtv.DEFAULT_BUDGET,
            mode=mode if use_semantic else rtv.MODE_KEYWORD,
            rerank_mode=rerank_mode,
        )
        if semantic_mode == "auto" and result.errors:
            errors.extend(result.errors)
            # ★ 嵌入模型不可用是**很常见**的（模型没下载 / 后端没配）：
            #   第一轮就发现了就整体降级为 keyword-only，而不是每轮白等一次超时。
            semantic_mode = "none"
        results.append(result.to_dict())
        if len(preview) < samples:
            preview.append(
                {
                    "session_id": row.session_id,
                    "query": row.query[:40],
                    "summary": rtv.describe(result),
                    "items": rtv.debug_lines(result, limit=4),
                }
            )

    cost = metrics.injection_report(results)
    total_state = meta["state_messages"] or 1
    state_total = meta["state_sessions"] or 1
    # ★ 真实遥测的逐轮曲线：跨会话按"落库顺序"编号（同一条会话内就是轮次顺序）。
    #   历史消息没有遥测 → 只能统计新聊出来的轮次，这一点在报告里如实写明。
    from app.narrative import state_drift as drift_mod

    drift_records, drift_meta = drift_mod.records_from_db(usernames=usernames)
    return {
        "rows_replayed": len(results),
        "semantic_mode_used": semantic_mode,
        "cost": cost,
        "meta": meta,
        "buckets": drift_mod.bucket(drift_records, drift_mod.DEFAULT_BUCKET),
        "buckets_meta": drift_meta,
        "drift_summary": drift_mod.summary(drift_records) if drift_records else {},
        "state_block_missing_rate": (
            meta["state_missing"] / meta["state_messages"] if meta["state_messages"] else None
        ),
        # ★ 没有遥测时给 None 而不是 0.0：0.0 会被读成"模型从不漏输出"，那是假的
        "state_idempotent_rate": (
            meta["state_idempotent"] / meta["state_sessions"] if meta["state_sessions"] else None
        ),
        "errors": sorted(set(errors)),
        "samples": preview,
    }


def print_replay_report(report: dict[str, Any]) -> str:
    """真实数据回放的报告（口径与合成集**分开**，绝不允许混着读）。"""
    meta = report["meta"]
    cost = report["cost"]
    lines: list[str] = []
    lines.append("\n[5] 真实数据只读回放（--from-db）")
    lines.append(
        f"    会话：扫了 {meta['sessions_scanned']} 个、用了 {meta['sessions_used']} 个"
        f"（共 {meta['messages_scanned']} 条消息）；回放轮次 {report['rows_replayed']}"
    )
    lines.append(f"    语义通道：{report['semantic_mode_used']}")
    if cost:
        lines.append(
            f"    注入条数(均) {cost.get('注入条数(均)', 0):.2f} · "
            f"注入token(均) {cost.get('注入token(均)', 0):.1f} · "
            f"去重率 {cost.get('去重率', 0):.1%} · "
            f"丢弃条数(均) {cost.get('丢弃条数(均)', 0):.2f}"
        )
    if meta["state_messages"]:
        lines.append(
            f"    ★ 状态块漏输出率：{report['state_block_missing_rate']:.1%}"
            f"（{meta['state_missing']}/{meta['state_messages']} 条助手回复没带 <state>，"
            "只统计**声明了状态栏**的会话）"
        )
    if meta["state_sessions"]:
        lines.append(
            f"    ★ 状态回放一致率：{report['state_idempotent_rate']:.1%}"
            f"（{meta['state_idempotent']}/{meta['state_sessions']} 个会话的落库状态"
            "再校验一遍不会被改动）"
        )
    if report["errors"]:
        lines.append("    降级说明：" + "；".join(report["errors"][:3]))
    lines.append(
        "    ★ 口径提醒：真实语料**没有相关性标注**，所以这里只报「成本 / 合规」指标，"
        "不报 Recall@k。要排序质量请看上面合成集的消融表。"
    )
    lines.append("    ★ 本模式**只读**（SELECT）：不动任何真实数据。")
    text = "\n".join(lines)
    print(text)
    return text


# ==================================================================
#  状态漂移曲线（第十一轮）
# ==================================================================
def run_drift(spec_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """离线确定性漂移模拟 → `(逐档数据, 总计)`。不调用任何模型。"""
    from app.narrative import state_drift as drift_mod

    spec = load_cases(spec_path)
    records = drift_mod.simulate(spec)
    buckets = drift_mod.bucket(records, int(spec.get("bucket") or drift_mod.DEFAULT_BUCKET))
    return buckets, drift_mod.summary(records)


def write_drift_svg(buckets: list[dict[str, Any]], path: Path) -> str:
    """把曲线画成 SVG（零依赖）：反事实 vs 实际，两条线共用同一标尺。"""
    from app.narrative import state_drift as drift_mod

    xs = [int(str(row["bucket"]).split("-")[0]) for row in buckets]
    naive = [(x, float(row["naive_out_of_range_mean"])) for x, row in zip(xs, buckets)]
    valid = [(x, 0.0) for x in xs]  # 有校验：越界程度恒为 0（被夹住了）
    top = max([y for _, y in naive] + [0.1]) * 1.1
    svg = drift_mod.to_svg(
        {"不校验（反事实）": naive, "有校验（实际）": valid},
        title="状态漂移曲线（HP 越界程度 · 不校验 vs 有校验）",
        x_label="轮次",
        y_label="越界程度（0=合法）",
        y_max=top,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(svg, encoding="utf-8")
    return str(path)


def print_drift_report(
    buckets: list[dict[str, Any]], totals: dict[str, Any], *, real: dict[str, Any] | None = None
) -> str:
    """控制台表格：每一档（每 N 轮）的各类率与两条曲线的 y 值。"""
    lines: list[str] = []
    lines.append("\n[6] 状态漂移曲线（离线确定性模拟 · 不调用模型）")
    lines.append(
        "    同一批「模型输出」，跑两条**各自闭环**的线："
        "实际回注校验后的状态；反事实回注模型自己写下的状态。"
    )
    header = (
        "    | 轮次档 | 漏输出率 | 夹取率 | 护栏率 | 脏值率 | 未知字段率 "
        "| 修正偏离度 | 不校验越界 | 校验后越界 | 反事实脏值率 |"
    )
    lines.append(header)
    lines.append("    |" + "---|" * 10)
    for row in buckets:
        lines.append(
            "    | {b} | {miss:.0%} | {clamp:.0%} | {guard:.0%} | {rej:.0%} | {unk:.0%} "
            "| {dev:.3f} | {naive:.2f} | 0.00 | {garb:.0%} |".format(
                b=row["bucket"],
                miss=row["missing_rate"],
                clamp=row["clamped_rate"],
                guard=row["guarded_rate"],
                rej=row["rejected_rate"],
                unk=row["unknown_rate"],
                dev=row["deviation_mean"],
                naive=row["naive_out_of_range_mean"],
                garb=row["naive_garbage_rate"],
            )
        )
    lines.append(
        "    总计：{rounds} 轮 · 漏输出 {miss:.1%} · 至少修正一次的轮次 {fix:.1%} · "
        "修正偏离度均值 {dev:.3f} · 不校验越界均值 {naive:.2f}（峰值 {peak:.2f}） · "
        "反事实脏值率 {garb:.1%}".format(
            rounds=totals["rounds"],
            miss=totals["missing_rate"],
            fix=totals["corrected_round_rate"],
            dev=totals["deviation_mean"],
            naive=totals["naive_out_of_range_mean"],
            peak=totals["naive_out_of_range_max"],
            garb=totals["naive_garbage_rate"],
        )
    )
    lines.append(
        "    ★ 口径：越界程度只对**算得出来**的轮次取平均（hp 被写成文字的那些轮由脏值率体现），"
        "所以分母会随档变化。"
    )
    if real is not None:
        meta = real.get("meta", {})
        lines.append("\n[7] 真实数据的状态遥测（--from-db，只读）")
        if meta.get("state_messages"):
            lines.append(
                f"    有遥测的助手轮次 {meta['state_messages']} 条："
                f"漏输出率 {real['state_block_missing_rate']:.1%}"
                f"（{meta['state_missing']}/{meta['state_messages']}）"
            )
            for row in real.get("buckets", []):
                lines.append(
                    "      {b}：漏输出 {m:.0%} · 夹取 {c:.0%} · 护栏 {g:.0%} · 脏值 {r:.0%} "
                    "· 未知字段 {u:.0%} · 偏离度 {d:.3f}".format(
                        b=row["bucket"],
                        m=row["missing_rate"],
                        c=row["clamped_rate"],
                        g=row["guarded_rate"],
                        r=row["rejected_rate"],
                        u=row["unknown_rate"],
                        d=row["deviation_mean"],
                    )
                )
        else:
            lines.append(
                "    还没有可用的遥测："
                f"扫了 {real.get('buckets_meta', {}).get('scanned', 0)} 条助手消息，"
                f"其中 {real.get('buckets_meta', {}).get('skipped_legacy', 0)} 条"
                "是**加遥测之前**落库的（历史数据无法反推模型当时是否输出了状态块 ——"
                "`<state>` 块在保存前就被剥掉了）。新聊的轮次会自动带上遥测。"
            )
    text = "\n".join(lines)
    print(text)
    return text


# ==================================================================
#  [8] 骰子求值器（骰子插件）
# ==================================================================
DICE_ROUNDS_DEFAULT = 6000
#: 体检项：(记号, 含义, 合法下界, 合法上界, 理论均值, 是否要求"两端都摸到")
DICE_PROBES: tuple[tuple[str, str, int, int, float | None, bool], ...] = (
    ("1d100", "百分骰", 1, 100, 50.5, True),
    ("d20", "省略个数 = 1", 1, 20, 10.5, True),
    ("2d6+3", "加常数", 5, 15, 10.0, False),
    ("4d6kh3", "四取三（属性生成）", 3, 18, None, False),
    ("4d6kl1", "四取低一", 1, 6, None, False),
    ("2d6!", "爆炸骰（有深度上限）", 2, 132, None, False),
    ("(1d6+2)*2", "括号与乘法", 6, 16, 11.0, False),
    ("1d20+5<=15", "成功判定", 6, 25, None, False),
)


def run_dice(rounds: int = DICE_ROUNDS_DEFAULT) -> dict[str, Any]:
    """给骰子求值器做体检（离线、固定种子，不调用大模型）。

    ★ 这不是"证明随机性"（伪随机数无法被证明），而是**排掉低级错误**：
      曾经写错面数、漏掉爆炸深度上限、把取高写成取低 —— 这几类都能被抓到。
      种子固定，所以报告在每次运行之间**逐字可复现**（论文里能直接引用）。
    """
    import random as _random

    from app.narrative import dice as dice_mod

    rows: list[dict[str, Any]] = []
    for notation, label, low, high, expected, cover in DICE_PROBES:
        rng = _random.Random(20260926)
        values = [dice_mod.evaluate(notation, rng=rng).total for _ in range(rounds)]
        assert all(isinstance(value, int) for value in values), notation
        observed_min, observed_max = min(values), max(values)
        mean = sum(values) / len(values)
        ok = observed_min >= low and observed_max <= high
        if expected is not None:
            ok = ok and abs(mean - expected) <= 1.5
        if cover:
            # 6000 次里一次都没摸到某一端 ≈ 概率 e^-60，摸不到就说明边界写错了
            ok = ok and observed_min == low and observed_max == high
        rows.append(
            {
                "notation": notation,
                "label": label,
                "mean": mean,
                "expected_mean": expected,
                "min": observed_min,
                "max": observed_max,
                "low": low,
                "high": high,
                "ok": ok,
            }
        )

    # 单颗骰子的分布体检（卡方，df = 面数-1；固定种子 → 结论稳定）
    rng = _random.Random(20260926)
    sides = 6
    counts = [0] * sides
    for _ in range(rounds):
        counts[dice_mod.evaluate(f"1d{sides}", rng=rng).total - 1] += 1
    expected_each = rounds / sides
    chi_square = sum((count - expected_each) ** 2 / expected_each for count in counts)
    # 卡方临界值（df=5）：p=0.05 → 11.07；p=0.001 → 20.52
    chi_ok = chi_square <= 20.52

    # 可复现性：同种子两次必须完全一致；不给种子时不该每次都一样
    first = dice_mod.evaluate("10d100", rng=_random.Random(1234)).faces
    second = dice_mod.evaluate("10d100", rng=_random.Random(1234)).faces
    unseeded = {tuple(dice_mod.evaluate("10d100").faces) for _ in range(5)}
    deterministic = first == second
    varied = len(unseeded) > 1

    out_of_range = sum(
        1
        for row in rows
        if not (row["min"] >= row["low"] and row["max"] <= row["high"])
    )
    return {
        "rounds_per_probe": rounds,
        "samples": rounds * len(DICE_PROBES),
        "rows": rows,
        "histogram": {str(index + 1): counts[index] for index in range(sides)},
        "chi_square": chi_square,
        "chi_square_ok": chi_ok,
        "deterministic_with_seed": deterministic,
        "varied_without_seed": varied,
        "out_of_range_probes": out_of_range,
        "all_ok": all(row["ok"] for row in rows) and chi_ok and deterministic and varied,
    }


def print_dice_report(report: dict[str, Any]) -> str:
    lines = [
        "",
        "=" * 78,
        "[8] 骰子求值器（骰子插件 · 离线 · 不调用大模型）",
        "=" * 78,
        f"    每项样本 {report['rounds_per_probe']} 次（固定种子 20260926，可逐字复现）",
        "",
        "    记号（ASCII 对齐，中文含义放行尾，免得 CJK 宽度把表撕歪）",
    ]
    for row in report["rows"]:
        expected = "—" if row["expected_mean"] is None else f"{row['expected_mean']:.2f}"
        lines.append(
            "    {n:<14} 均值 {m:>8.2f}（理论 {e:>7}）  范围 {lo:>4}~{hi:<5}"
            "（合法 {blo}~{bhi}）  {v}  {l}".format(
                n=row["notation"],
                m=row["mean"],
                e=expected,
                lo=row["min"],
                hi=row["max"],
                blo=row["low"],
                bhi=row["high"],
                v="✓" if row["ok"] else "✗",
                l=row["label"],
            )
        )
    lines.append("")
    lines.append(
        f"    分布体检（1d6 × {report['rounds_per_probe']}）：卡方 = {report['chi_square']:.2f}"
        f"（df=5；p=0.05 → 11.07，p=0.001 → 20.52）→ "
        + ("看不出偏斜 ✓" if report["chi_square_ok"] else "偏斜可疑 ✗")
    )
    lines.append(
        "    可复现性：同种子两次一致 "
        + ("✓" if report["deterministic_with_seed"] else "✗")
        + " · 不给种子时结果不重复 "
        + ("✓" if report["varied_without_seed"] else "✗")
    )
    lines.append(
        f"    ★ 越界记号数：{report['out_of_range_probes']} / {len(report['rows'])} ——"
        "求值器的输出**按构造**不可能越界，模型自报的数字做不到这一点"
        "（对照 [7] 节的反事实脏值率）。"
    )
    text = "\n".join(lines)
    print(text)
    return text


def dice_markdown(report: dict[str, Any]) -> str:
    """骰子体检的 Markdown 表（写进 benchmark_report.md）。"""
    lines = [
        "## 骰子求值器体检（离线 · 固定种子）",
        "",
        f"- 每项样本 {report['rounds_per_probe']} 次，共 {report['samples']} 次掷骰",
        "",
        "| 记号 | 含义 | 实测均值 | 理论均值 | 实测范围 | 合法范围 | 判定 |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["rows"]:
        expected = "—" if row["expected_mean"] is None else f"{row['expected_mean']:.2f}"
        lines.append(
            f"| `{row['notation']}` | {row['label']} | {row['mean']:.2f} | {expected} | "
            f"{row['min']}~{row['max']} | {row['low']}~{row['high']} | "
            f"{'✓' if row['ok'] else '✗'} |"
        )
    lines.append("")
    lines.append(
        f"- 1d6 分布卡方 = {report['chi_square']:.2f}（df=5，p=0.05 临界 11.07）→ "
        + ("看不出偏斜" if report["chi_square_ok"] else "**偏斜可疑**")
    )
    lines.append(
        f"- 同种子可复现：{'是' if report['deterministic_with_seed'] else '否'}；"
        f"不给种子时不重复：{'是' if report['varied_without_seed'] else '否'}"
    )
    lines.append(
        "- 求值器输出**按构造**不会越界（模型自报数字无法保证这一点）"
    )
    return "\n".join(lines)


# ==================================================================
#  CLI
# ==================================================================
def main() -> int:
    # 评测是"批量跑同一条管道"，逐次检索的 DEBUG 日志会淹掉表格 —— 关掉它，
    # 只保留脚本自己要打印的报告（要排查时把下面这行去掉即可）。
    from loguru import logger

    logger.remove()

    parser = argparse.ArgumentParser(description="混合检索离线评测（不调用大模型）")
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="检索标注集 JSON")
    parser.add_argument("--state-cases", default=str(DEFAULT_STATE_CASES), help="状态校验用例 JSON")
    parser.add_argument("--k", default="1,3,5", help="Recall@k 的 k，逗号分隔")
    parser.add_argument(
        "--semantic",
        choices=("labels", "embedding"),
        default="labels",
        help="语义通道来源：标注相似度（默认，离线）或真实嵌入模型",
    )
    parser.add_argument("--json", default="data/benchmark_report.json", help="结果写到哪里")
    parser.add_argument("--md", default="data/benchmark_report.md", help="Markdown 表写到哪里")
    parser.add_argument(
        "--drift-script",
        default=str(DEFAULT_DRIFT_SCRIPT),
        help="状态漂移模拟脚本（离线确定性，默认 scripts/benchmark_data/drift_script.json）",
    )
    parser.add_argument(
        "--drift-svg", default="data/benchmark_drift.svg", help="漂移曲线 SVG 写到哪里"
    )
    # ---------------- 真实数据只读回放 ----------------
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="额外做一次**真实数据只读回放**（只 SELECT，不动任何数据）；"
        "没有相关性标注，所以只报成本/合规指标",
    )
    parser.add_argument(
        "--user",
        default="",
        help="--from-db 时只看这些用户（逗号分隔用户名；留空 = 全部用户）",
    )
    parser.add_argument(
        "--limit-sessions",
        type=int,
        default=DEFAULT_REPLAY_SESSIONS,
        help=f"--from-db 时最多回放多少个会话（默认 {DEFAULT_REPLAY_SESSIONS}）",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=DEFAULT_REPLAY_ROUNDS,
        help=f"--from-db 时每个会话回放最后几轮（默认 {DEFAULT_REPLAY_ROUNDS}）",
    )
    parser.add_argument(
        "--replay-semantic",
        choices=("auto", "embedding", "none"),
        default="auto",
        help="--from-db 的语义通道：auto（先试真实向量库，失败就降级）/ embedding / none",
    )
    parser.add_argument(
        "--dice-rounds",
        type=int,
        default=DICE_ROUNDS_DEFAULT,
        help=f"[8] 骰子体检每项的样本数（默认 {DICE_ROUNDS_DEFAULT}）",
    )
    args = parser.parse_args()

    ks = [int(x) for x in str(args.k).split(",") if x.strip()]
    data = load_cases(Path(args.cases))
    state_cases = load_cases(Path(args.state_cases))["cases"]

    ablation, _costs, samples = run_ablation(data, ks=ks, semantic_mode=args.semantic)
    state = metrics.state_consistency(state_cases)
    drift_buckets, drift_totals = run_drift(Path(args.drift_script))
    dice = run_dice(rounds=max(200, int(args.dice_rounds)))
    text = print_report(
        ablation=ablation, state=state, semantic_mode=args.semantic, ks=ks
    )
    print_drift_report(drift_buckets, drift_totals)
    svg_path = write_drift_svg(drift_buckets, PROJECT_ROOT / args.drift_svg)
    print(f"\n漂移曲线：{svg_path}")

    report = {
        "dataset": args.cases,
        "semantic_mode": args.semantic,
        "budget_tokens": BUDGET,
        "semantic_top_k": TOP_K,
        "ks": ks,
        "ablation": ablation,
        "state_consistency": {
            k: v for k, v in state.items() if k != "details"
        },
        "samples": samples,
        "drift": {"buckets": drift_buckets, "totals": drift_totals, "svg": args.drift_svg},
        "dice": dice,
        "note": "合成小集，仅证明机制成立；排序指标用完整名次，注入成本受共享预算影响。",
    }

    # ---------------- 真实数据只读回放（可选）----------------
    replay: dict[str, Any] | None = None
    if args.from_db:
        usernames = [u.strip() for u in str(args.user).split(",") if u.strip()]
        replay = replay_from_db(
            usernames=usernames or None,
            limit_sessions=args.limit_sessions,
            rounds=args.rounds,
            semantic_mode=args.replay_semantic,
        )
        print_replay_report(replay)
        report["from_db"] = replay
    print_drift_report(drift_buckets, drift_totals, real=replay if args.from_db else None)
    # ★ 骰子放在最后：控制台的节号要单调（[6]/[7] 会因为"带真实数据再打一遍"而出现两次，
    #   把 [8] 夹在中间会让节号看起来是乱序的）
    print_dice_report(dice)
    json_path = PROJECT_ROOT / args.json
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = PROJECT_ROOT / args.md
    md_path.write_text(
        "# 混合检索评测（合成小集 · 离线 · 不调用大模型）\n\n"
        f"- 语义通道：`{args.semantic}`　共享预算：{BUDGET} token/轮　语义 top-k：{TOP_K}\n"
        f"- 指标：k={ks}；排序用完整名次（含被预算砍掉的候选）\n\n"
        "## 排序质量\n\n"
        + metrics.markdown_table(ablation, columns=RANK_COLUMNS)
        + "\n\n## 装配成本\n\n"
        + metrics.markdown_table(ablation, columns=COST_COLUMNS)
        + f"\n\n## 状态一致性校验\n\n用例 {state['total']} 条：原样接受 {state['原样接受率']:.1%}、"
        f"被修正 {state['被修正率']:.1%}、被丢弃 {state['被丢弃率']:.1%}\n"
        + (
            "\n## 真实数据只读回放（--from-db）\n\n"
            f"- 会话：扫描 {replay['meta']['sessions_scanned']} 个、使用 {replay['meta']['sessions_used']} 个，"
            f"共 {replay['meta']['messages_scanned']} 条消息；回放 {replay['rows_replayed']} 轮\n"
            f"- 语义通道：`{replay['semantic_mode_used']}`\n"
            f"- 注入条数(均) {replay['cost'].get('注入条数(均)', 0):.2f} · "
            f"注入 token(均) {replay['cost'].get('注入token(均)', 0):.1f} · "
            f"去重率 {replay['cost'].get('去重率', 0):.1%}\n"
            f"- 状态块漏输出率 "
            + (
                f"{replay['state_block_missing_rate']:.1%}"
                if replay["state_block_missing_rate"] is not None
                else "无可用遥测（历史轮次没有遥测：`<state>` 在保存前就被剥掉了）"
            )
            + " · 状态回放一致率 "
            + (
                f"{replay['state_idempotent_rate']:.1%}\n"
                if replay["state_idempotent_rate"] is not None
                else "无可用数据\n"
            )
            + "- 口径：真实语料无相关性标注，故只报成本/合规；本模式只读（SELECT）。\n"
            if replay
            else ""
        )
        + "\n## 状态漂移曲线（离线确定性模拟）\n\n"
        + "| 轮次档 | 漏输出率 | 夹取率 | 护栏率 | 脏值率 | 未知字段率 | 修正偏离度 | 不校验越界 | 校验后越界 | 反事实脏值率 |\n"
        + "|---|---|---|---|---|---|---|---|---|---|\n"
        + "\n".join(
            "| {b} | {m:.0%} | {c:.0%} | {g:.0%} | {r:.0%} | {u:.0%} | {d:.3f} | {n:.2f} | 0.00 | {gb:.0%} |".format(
                b=row["bucket"],
                m=row["missing_rate"],
                c=row["clamped_rate"],
                g=row["guarded_rate"],
                r=row["rejected_rate"],
                u=row["unknown_rate"],
                d=row["deviation_mean"],
                n=row["naive_out_of_range_mean"],
                gb=row["naive_garbage_rate"],
            )
            for row in drift_buckets
        )
        + f"\n\n总计：{drift_totals['rounds']} 轮 · 漏输出 {drift_totals['missing_rate']:.1%} · "
        f"至少修正一次 {drift_totals['corrected_round_rate']:.1%} · "
        f"不校验越界均值 {drift_totals['naive_out_of_range_mean']:.2f}"
        f"（峰值 {drift_totals['naive_out_of_range_max']:.2f}） vs 校验后 0.00 · "
        f"反事实脏值率 {drift_totals['naive_garbage_rate']:.1%}\n"
        f"\n曲线图（零依赖 SVG）：`{args.drift_svg}`\n"
        "\n> 读法：两条线**各自闭环** —— 实际这条把校验后的状态回注给模型；"
        "反事实这条把模型自己写下的状态回注给它，于是越界程度一轮轮滚大、"
        "脏值一旦写进去就再也洗不掉（脏值率只升不降）。\n\n"
        + dice_markdown(dice)
        + "\n",
        encoding="utf-8",
    )

    print(f"\n报告：{json_path}")
    print(f"表格：{md_path}")
    if state["判据不符"]:
        print("\n[警告] 状态校验用例的判据与标注不一致，见上面 [3] 节。")
        return 2
    if not dice["all_ok"]:
        print("\n[警告] 骰子求值器体检没有全部通过，见上面 [8] 节。")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
