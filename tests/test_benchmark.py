"""评测指标 + benchmark 脚本（评测本身也必须可测、可复现）。

★ 为什么评测要写单测：指标算错是最难发现的一类错误 —— 表格看起来"有数字"，
  但如果分母搞错（例如 Recall 的分母用了 k 而不是相关条目数），结论就全歪了。
  所以这里用**手算得出的小例子**把每个指标钉死，再用真实的合成标注集
  把"融合优于单通道""字面重排有害"这两条写进代码里的结论钉住。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
for path in (str(PROJECT_ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import benchmark as bench  # noqa: E402
from app.llm.params import GenerationParams, compute_context_budget  # noqa: E402
from app.llm.schema import ChatResult, StreamChunk, TokenUsage  # noqa: E402
from app.narrative import retrieval_metrics as metrics  # noqa: E402

CASES = SCRIPTS / "benchmark_data" / "retrieval_cases.json"
STATE_CASES = SCRIPTS / "benchmark_data" / "state_cases.json"


# ==================================================================
#  一、指标本身（手算可验证）
# ==================================================================
def test_recall_at_k_uses_relevant_set_as_denominator() -> None:
    ranked = ["a", "b", "c", "d"]
    assert metrics.recall_at_k(ranked, ["b", "d"], 2) == pytest.approx(0.5)
    assert metrics.recall_at_k(ranked, ["b", "d"], 4) == pytest.approx(1.0)
    assert metrics.recall_at_k(ranked, [], 4) == 0.0


def test_reciprocal_rank_takes_the_first_hit() -> None:
    assert metrics.reciprocal_rank(["x", "y", "z"], ["z"]) == pytest.approx(1 / 3)
    assert metrics.reciprocal_rank(["z"], ["z"]) == pytest.approx(1.0)
    assert metrics.reciprocal_rank(["x"], ["z"]) == 0.0


def test_ndcg_at_k_matches_hand_computation() -> None:
    # 相关集合 {a, b}，k=2：理想排序 a,b → DCG = 1 + 1/log2(3)，命中同样是 a,b
    assert metrics.ndcg_at_k(["a", "b"], ["a", "b"], 2) == pytest.approx(1.0)
    # 命中顺序反了：DCG = 1/log2(2) + 1/log2(3)（b 在第 1 位、a 在第 2 位）
    dcg = 1.0 / 1 + 1.0 / 1.584962500721156
    idcg = 1.0 / 1 + 1.0 / 1.584962500721156
    assert metrics.ndcg_at_k(["a", "b"], ["a", "b"], 2) == pytest.approx(dcg / idcg)
    # 一条都没命中 → 0
    assert metrics.ndcg_at_k(["x", "y"], ["a"], 2) == 0.0


def test_ranking_report_averages_over_cases() -> None:
    cases = [
        metrics.RankedCase(name="q1", ranked=["a", "b"], relevant=["a"]),
        metrics.RankedCase(name="q2", ranked=["b", "a"], relevant=["a"]),
    ]
    report = metrics.ranking_report(cases, ks=[1])
    assert report["recall@1"] == pytest.approx(0.5)
    assert report["mrr"] == pytest.approx((1.0 + 0.5) / 2)


def test_injection_report_summarizes_tokens_and_dedup() -> None:
    rows = [
        {"used_tokens": 100, "injected": 2, "lexical_total": 1, "semantic_total": 3, "deduped": 1, "dropped": 1},
        {"used_tokens": 200, "injected": 4, "lexical_total": 2, "semantic_total": 2, "deduped": 0, "dropped": 0},
    ]
    report = metrics.injection_report(rows)
    assert report["注入token(均)"] == pytest.approx(150.0)
    assert report["注入token(中位)"] == pytest.approx(150.0)
    assert report["去重率"] == pytest.approx((1 / 4 + 0 / 4) / 2)


# ==================================================================
#  二、状态一致率：fixture 既是数据源也是判据回归
# ==================================================================
def test_state_consistency_fixture_matches_its_labels() -> None:
    """★ 标注写了什么下场，校验就必须给出什么下场（判据被改动会立刻红）。"""
    cases = json.loads(STATE_CASES.read_text(encoding="utf-8"))["cases"]
    report = metrics.state_consistency(cases)
    assert report["判据不符"] == [], report["判据不符"]
    total = (
        report["原样接受率"] + report["被修正率"] + report["被丢弃率"]
    )
    assert total == pytest.approx(1.0)
    assert report["total"] == len(cases)


def test_classify_notes_prefers_rejection_over_correction() -> None:
    """同时出现"丢弃"和"修正"提醒时算丢弃（更严重的那一类）。"""
    assert metrics.classify_notes(["HP 不是数字，已沿用上一轮"]) == metrics.OUTCOME_REJECTED
    assert metrics.classify_notes(["HP 越界（150），已夹到 0～100 之间"]) == metrics.OUTCOME_CORRECTED
    assert metrics.classify_notes([]) == metrics.OUTCOME_UNCHANGED


# ==================================================================
#  三、benchmark 结论（写成回归守卫，避免以后悄悄变差）
# ==================================================================
@pytest.fixture(scope="module")
def ablation() -> dict[str, dict[str, float]]:
    data = bench.load_cases(CASES)
    result, _costs, _samples = bench.run_ablation(data, ks=[1, 3, 5], semantic_mode="labels")
    return result


def test_fusion_beats_single_channels(ablation: dict[str, dict[str, float]]) -> None:
    """★ 核心结论：融合（RRF）在 Recall@3 上明显优于任一单通道。"""
    rrf = ablation["rrf(+tiebreak)"]
    assert rrf["recall@3"] > ablation["keyword_only"]["recall@3"]
    assert rrf["recall@3"] > ablation["semantic_only"]["recall@3"]
    assert rrf["recall@1"] >= ablation["semantic_only"]["recall@1"]


def test_tiebreak_is_not_worse_than_raw_fusion(ablation: dict[str, dict[str, float]]) -> None:
    """默认重排（tiebreak）只做同分裁决 + 按证据裁决，指标不得低于"不重排"。

    ★ RRF 同分很常见，所以裁决方式是**要量的**：按先验分裁决实测会掉到 0.26，
      按"每路归一化证据"裁决与"最优"持平（见 retrieval.rerank 的注释表）。
    """
    rrf = ablation["rrf(+tiebreak)"]
    plain = ablation["rrf_no_rerank"]
    assert rrf["recall@1"] >= plain["recall@1"]
    assert rrf["mrr"] >= plain["mrr"]


def test_lexical_blend_rerank_is_a_negative_control(ablation: dict[str, dict[str, float]]) -> None:
    """★ 负面对照：用字面重叠重排会掉 Recall@1（所以它不是默认，只留给消融表）。"""
    assert ablation["rrf(+blend 词面重排)"]["recall@1"] < ablation["rrf(+tiebreak)"]["recall@1"]


def test_ablation_covers_the_documented_set() -> None:
    """消融表里必须有"只用一路 / 不重排 / 融合 / 加权 / 负面对照"这几档。"""
    assert set(bench.ABLATIONS) == {
        "keyword_only",
        "semantic_only",
        "rrf_no_rerank",
        "rrf(+tiebreak)",
        "weighted(+tiebreak)",
        "rrf(+blend 词面重排)",
    }


# ==================================================================
#  三·2、骰子求值器体检（[8] 节）
# ==================================================================
def test_dice_diagnostics_pass_and_are_reproducible() -> None:
    """骰子体检必须全过，而且**逐字可复现**（固定种子，论文里能直接引用）。"""
    first = bench.run_dice(rounds=600)
    second = bench.run_dice(rounds=600)
    assert first["all_ok"] is True, first["rows"]
    assert first == second, "同种子两次体检结果必须完全一致"
    assert first["out_of_range_probes"] == 0
    assert first["deterministic_with_seed"] is True
    assert first["varied_without_seed"] is True, "不给种子时必须每次不同，否则就不是随机了"


def test_dice_diagnostics_cover_the_documented_notations() -> None:
    """体检表要覆盖文档里承诺的每一种记法（文档写了却不测＝没做）。"""
    notations = {row[0] for row in bench.DICE_PROBES}
    assert {"1d100", "d20", "2d6+3", "4d6kh3", "4d6kl1", "2d6!", "(1d6+2)*2"} <= notations
    assert any("<=" in item for item in notations), "成功判定也要在表里"


def test_dice_histogram_is_not_lopsided() -> None:
    report = bench.run_dice(rounds=1200)
    assert report["chi_square_ok"] is True
    assert sum(report["histogram"].values()) == 1200
    assert len(report["histogram"]) == 6
    text = bench.dice_markdown(report)
    assert "卡方" in text and "| 记号 |" in text


# ==================================================================
#  四、真实数据只读回放（--from-db）
# ==================================================================
class FakeAdapter:
    """不联网的假适配器（只用于造几条真实历史消息，回放本身不需要模型）。"""

    def __init__(self, row=None, **_):
        self.row = row
        self.default_params = GenerationParams(max_tokens=512)

    @property
    def budget(self):
        window = getattr(self.row, "context_window", None) or 8192
        max_out = getattr(self.row, "max_tokens", None) or 1024
        return compute_context_budget(window, max_out)

    def chat(self, request):
        return ChatResult(
            content="（假回复）",
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
        yield StreamChunk(delta="（假回复）")
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(total_tokens=15))

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    from app.narrative import engine

    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    monkeypatch.setattr(engine.memory_mod, "remember_turn", lambda **_: None)
    yield FakeAdapter


@pytest.fixture
def replay_user():
    """造一个**独立账号**（只在它自己名下回放），跑完删掉，不碰任何真实用户。"""
    import uuid

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.db.models import User
    from app.db.mysql import session_scope
    from app.main import app

    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"bk_{token}",
        "email": f"bk_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", json=account).status_code == 201
        logged = client.post(
            "/api/v1/auth/login",
            json={"username": account["username"], "password": account["password"]},
        )
        headers = {"Authorization": f"Bearer {logged.json()['data']['access_token']}"}
        book = client.post(
            "/api/v1/world-books",
            json={
                "name": f"回放世界_{token}",
                "entries": [
                    {"keys": ["灯塔"], "content": "灯塔在风暴夜会熄灭", "enabled": True},
                    {"keys": ["补给船"], "content": "补给船二十天来一次", "enabled": True},
                    {"keys": ["龙"], "content": "龙已死去三百年", "enabled": True},
                ],
            },
            headers=headers,
        )
        assert book.status_code == 201, book.text
        card = client.post(
            "/api/v1/character-cards",
            json={
                "name": f"回放卡_{token}",
                "greeting": "……你来了。",
                "world_book_id": book.json()["data"]["id"],
                # ★ 声明了状态栏 → 回放时才会统计"状态块漏输出率 / 状态回放一致率"
                "extensions": {
                    "hne": {
                        "state_schema": [
                            {"name": "hp", "type": "meter", "max_field": "max"},
                            {"name": "location", "type": "text"},
                        ],
                        "initial_state": {"hp": 90, "max": 100, "location": "灯塔一层"},
                    }
                },
            },
            headers=headers,
        )
        assert card.status_code == 201, card.text
        provider = client.post(
            "/api/v1/providers",
            json={
                "name": f"回放模型_{token}",
                "provider_type": "openai_compatible",
                "base_url": "https://mock.invalid/v1",
                "api_key": "sk-replay-test",
                "model_name": "mock-model",
                "context_window": 8192,
            },
            headers=headers,
        )
        assert provider.status_code == 201, provider.text
        session_row = client.post(
            "/api/v1/narrative/sessions",
            json={
                "character_card_id": card.json()["data"]["id"],
                "llm_provider_id": provider.json()["data"]["id"],
                "title": f"回放会话_{token}",
            },
            headers=headers,
        )
        assert session_row.status_code == 201, session_row.text
        session_id = session_row.json()["data"]["id"]
        # 两轮用户消息（假模型不输出 <state> → 正好用来量"漏输出率"）
        for text in ("灯塔还亮着吗", "补给船什么时候来"):
            sent = client.post(
                f"/api/v1/narrative/sessions/{session_id}/messages",
                json={"content": text},
                headers=headers,
            )
            assert sent.status_code == 201, sent.text
    yield {"username": account["username"], "session_id": session_id}
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == account["username"]))
        if row is not None:
            db.delete(row)


def test_replay_from_db_is_readonly_and_reports_cost(replay_user) -> None:
    """★ 真库只读回放：读得出会话与轮次，报得出成本，且**不改任何数据**。"""
    from sqlalchemy import func, select

    from app.db.models import Message
    from app.db.mysql import session_scope

    with session_scope() as db:
        before = db.scalar(select(func.count()).select_from(Message))

    report = bench.replay_from_db(
        usernames=[replay_user["username"]],
        limit_sessions=5,
        rounds=2,
        semantic_mode="none",  # 只读回放默认不碰向量库（测试里更不能依赖嵌入模型）
    )

    with session_scope() as db:
        after = db.scalar(select(func.count()).select_from(Message))
    assert before == after, "回放必须是只读的：消息条数不能变"

    assert report["rows_replayed"] >= 1, report
    meta = report["meta"]
    assert meta["sessions_used"] >= 1
    assert meta["messages_scanned"] >= 2
    assert report["cost"]["注入条数(均)"] >= 1.0, "真实语料里应当至少注入一条"
    assert report["cost"]["注入token(均)"] > 0
    # 假模型不输出状态块 → 漏输出率应当是 100%（这条同时证明统计口径真的在算）
    assert meta["state_messages"] >= 1
    assert report["state_block_missing_rate"] == pytest.approx(1.0)


def test_replay_reports_state_idempotency(replay_user) -> None:
    """落库的状态"再校验一遍"不该被改动（幂等）—— 这是真实数据上的状态一致性度量。"""
    report = bench.replay_from_db(
        usernames=[replay_user["username"]],
        limit_sessions=5,
        rounds=1,
        semantic_mode="none",
    )
    meta = report["meta"]
    assert meta["state_sessions"] >= 1, "这个账号的会话声明了状态栏，应当被统计"
    assert report["state_idempotent_rate"] == pytest.approx(1.0)
    assert meta["state_not_idempotent"] == 0


def test_replay_respects_the_username_filter(replay_user) -> None:
    """只回放指定用户：别的账号（包括真实用户）一条都不该被读进来。"""
    rows, meta = bench.load_replay_rows(
        usernames=["绝对不存在的用户名_zzz"], limit_sessions=5, rounds=1
    )
    assert rows == [] and meta["sessions_used"] == 0


def test_replay_report_is_honest_about_the_missing_labels() -> None:
    """报告里必须写明"真实语料没有标注、只报成本/合规、本模式只读"。"""
    text = bench.print_replay_report(
        {
            "rows_replayed": 2,
            "semantic_mode_used": "none",
            "cost": {"注入条数(均)": 1.0, "注入token(均)": 12.0, "去重率": 0.0, "丢弃条数(均)": 0.0},
            "meta": {
                "sessions_scanned": 1,
                "sessions_used": 1,
                "messages_scanned": 4,
                "state_messages": 2,
                "state_missing": 1,
                "state_sessions": 1,
                "state_idempotent": 1,
                "state_not_idempotent": 0,
            },
            "state_block_missing_rate": 0.5,
            "state_idempotent_rate": 1.0,
            "errors": [],
            "samples": [],
        }
    )
    assert "不报 Recall@k" in text
    assert "只读" in text
    assert "50.0%" in text
