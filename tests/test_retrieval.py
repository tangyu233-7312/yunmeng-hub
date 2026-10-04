"""混合检索（关键词 + 语义 → 融合 → 去重 → 重排 → 共享预算）。

分两层，与 test_state.py 一致：
  · 纯函数层：用**注入的假通道**直接喂候选，把融合/去重/排序/预算的判据钉死
    （不碰数据库、不碰向量库，所以跑得快也不会因为嵌入模型缺失而飘）；
  · 端到端层：走真实接口 + 假模型，验证"预览与实际一致"与"跨小节不重复注入"。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.chroma import MemoryHit
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative import memory as memory_mod
from app.narrative import retrieval as rtv
from app.narrative import world_book_scanner

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
BOOKS = "/api/v1/world-books"
PROVIDERS = "/api/v1/providers"


def _cand(
    key: str,
    text: str,
    *,
    source: str = "world_book",
    lex: int | None = None,
    sem: int | None = None,
    lex_score: float | None = None,
    sem_score: float | None = None,
    priority: float = 1.0,
) -> rtv.Candidate:
    return rtv.Candidate(
        key=key,
        text=text,
        source=source,
        raw={"content": text} if source == "world_book" else MemoryHit(
            memory_id=key, text=text, distance=0.1, metadata={}
        ),
        tokens=rtv.estimate_tokens(text),
        lexical_rank=lex,
        semantic_rank=sem,
        lexical_score=lex_score if lex_score is not None else (float(lex) if lex else None),
        semantic_score=(
            sem_score if sem_score is not None else ((1.0 - 0.1 * sem) if sem else None)
        ),
        priority=priority,
    )


def _run(lexical: list[rtv.Candidate], semantic: list[rtv.Candidate], **kwargs):
    return rtv.retrieve(
        keyword_provider=lambda: lexical,
        semantic_provider=lambda: (semantic, []),
        query=kwargs.pop("query", ""),
        **kwargs,
    )


# ==================================================================
#  一、融合
# ==================================================================
def test_rrf_prefers_a_candidate_hit_by_both_channels() -> None:
    """两路都召回的候选应当排在只被一路召回的前面（这正是"融合"的意义）。"""
    shared = _cand("book:0", "被两路同时召回的设定", lex=2, sem=2)
    only_lexical = _cand("book:1", "只被关键词召回", lex=1)
    only_semantic = _cand("mem:a", "只被语义召回", source="memory", sem=1)
    result = _run([shared, only_lexical], [only_semantic], mode=rtv.MODE_RRF, budget=0)
    order = [c.key for c in result.kept]
    assert order[0] == "book:0", order
    assert set(result.kept[0].channels) == {"keyword", "semantic"}
    assert result.lexical_total == 2 and result.semantic_total == 1


def test_keyword_mode_ignores_semantic_channel() -> None:
    """消融用：只用关键词通道时，语义候选一条都不该进。"""
    result = _run(
        [_cand("book:0", "关键词命中", lex=1)],
        [_cand("mem:a", "语义命中", source="memory", sem=1)],
        mode=rtv.MODE_KEYWORD,
        budget=0,
    )
    assert [c.key for c in result.kept] == ["book:0"]
    assert result.semantic_total == 0


def test_semantic_mode_ignores_keyword_channel() -> None:
    result = _run(
        [_cand("book:0", "关键词命中", lex=1)],
        [_cand("mem:a", "语义命中", source="memory", sem=1)],
        mode=rtv.MODE_SEMANTIC,
        budget=0,
    )
    assert [c.key for c in result.kept] == ["mem:a"]
    assert result.lexical_total == 0


def test_weighted_mode_normalizes_each_channel() -> None:
    """加权模式必须**分通道**归一化：否则量纲不同的两路没法比（关键词 2 分 vs 相似度 0.9）。"""
    lexical = [
        _cand("book:0", "关键词第二", lex=1, lex_score=1.0),
        _cand("book:1", "关键词第一", lex=2, lex_score=2.0),
    ]
    semantic = [_cand("mem:a", "语义第一", source="memory", sem=1, sem_score=0.9)]
    result = _run(lexical, semantic, mode=rtv.MODE_WEIGHTED, budget=0)
    kept = {c.key: c for c in result.kept}
    # 每个通道各自 min-max 归一化：关键词里 2.0 是最高 → 1.0，1.0 是最低 → 0.0；
    # 语义通道只有一个值 → 给满分（否则整路会被"只有一个候选"压成 0）
    assert kept["book:1"].fusion == pytest.approx(1.0)
    assert kept["book:0"].fusion == pytest.approx(0.0)
    assert kept["mem:a"].fusion == pytest.approx(1.0)


def test_unknown_mode_falls_back_to_default() -> None:
    result = _run([_cand("book:0", "x", lex=1)], [], mode="不存在的策略", budget=0)
    assert result.mode == rtv.DEFAULT_MODE


# ==================================================================
#  二、跨通道去重
# ==================================================================
def test_cross_channel_dedupe_keeps_one_and_merges_votes() -> None:
    """同一段内容被两路同时召回 → 只留一条（留世界书那条），另一条如实记原因。"""
    book = _cand("book:0", "北岸灯塔在风暴夜会熄灭。", lex=1)
    same = _cand("mem:a", "北岸灯塔在风暴夜会熄灭。", source="memory", sem=1)
    result = _run([book], [same], budget=0)
    assert len(result.kept) == 1
    winner = result.kept[0]
    assert winner.source == "world_book", "并列时优先留下作者手写的世界书设定"
    assert set(winner.channels) == {"keyword", "semantic"}, "两路的票都要并到胜者身上"
    assert result.deduped == 1
    assert any("重复" in c.reason for c in result.dropped)


def test_dedupe_ignores_punctuation_and_whitespace() -> None:
    """判重按归一化文本（去空白/标点），不是逐字节比较。"""
    a = _cand("book:0", "灯，灭了。", lex=1)
    b = _cand("mem:a", " 灯灭了 ", source="memory", sem=1)
    result = _run([a], [b], budget=0)
    assert len(result.kept) == 1 and result.deduped == 1


# ==================================================================
#  三、共享预算（★ 世界书优先，绝不被回忆挤掉）
# ==================================================================
def test_world_book_entries_are_never_evicted_by_memories() -> None:
    """★ 用户明确的硬规则：**世界书设定绝对不能因为回忆而被挤掉**。

    原话："世界书一般是作者特意加在角色卡里面的，里面的设定比用户对话重要，
    不然容易出戏，乱写和掉马甲之类的，绝对不能挤掉！"
    所以哪怕回忆的分数明显更高、预算只够装一条，也必须装世界书那条。
    """
    book = _cand("book:0", "作者手写的设定", lex=1, priority=0.5)  # 7 token，分数低
    mem = _cand("mem:a", "一段高分回忆", source="memory", sem=1, sem_score=1.0)  # 6 token，分数高
    result = _run([book], [mem], budget=8)  # 只够装世界书那条
    assert [c.key for c in result.kept] == ["book:0"], "世界书必须优先装填"
    assert any(c.key == "mem:a" and "预算" in c.reason for c in result.dropped)


def test_memories_use_only_the_leftover_budget() -> None:
    """回忆只能用世界书吃剩的预算（不是"谁分高谁先"）。"""
    book = _cand("book:0", "灯" * 30, lex=1)  # 30 token
    mem_small = _cand("mem:a", "记" * 20, source="memory", sem=1)  # 20 token（排名更高）
    mem_big = _cand("mem:b", "忆" * 40, source="memory", sem=2)  # 40 token
    result = _run([book], [mem_small, mem_big], budget=50)
    kept = {c.key for c in result.kept}
    assert kept == {"book:0", "mem:a"}, "30+20 刚好用满；40 token 的大回忆装不下"
    assert result.used_tokens == 50


def test_world_book_too_big_is_dropped_for_its_own_reasons_only() -> None:
    """世界书自己装不下时只丢它自己（原因如实写明"世界书优先…自己也装不下"）。"""
    first = _cand("book:0", "灯" * 20, lex=1)
    second = _cand("book:1", "油" * 60, lex=2)
    mem = _cand("mem:a", "记" * 5, source="memory", sem=1)
    result = _run([first, second], [mem], budget=40)
    dropped = {c.key: c.reason for c in result.dropped}
    assert "book:1" in dropped and "世界书优先" in dropped["book:1"]
    assert "mem:a" in {c.key for c in result.kept}, "剩 20 token 够装小回忆"


def test_tiny_budget_still_injects_the_top_candidate() -> None:
    """预算配得比单条还小时也要给一条，否则用户只会看到"命中 N 条、注入 0 条"。"""
    result = _run([_cand("book:0", "灯" * 100, lex=1)], [], budget=5)
    assert len(result.kept) == 1
    assert result.dropped == []


def test_budget_zero_means_unlimited() -> None:
    result = _run(
        [_cand("book:0", "灯" * 100, lex=1), _cand("book:1", "油" * 100, lex=2)],
        [],
        budget=0,
    )
    assert len(result.kept) == 2


# ==================================================================
#  四、重排（默认只做同分裁决 —— 这是 benchmark 量出来的结论）
# ==================================================================
def test_default_tiebreak_never_overturns_fusion() -> None:
    """★ 回归守卫：默认重排的微调量是 1e-6 级，绝不能翻掉融合得出的次序。

    背景（`scripts/benchmark.py` 的消融表）：把"与查询的字面重叠"当作主要重排信号
    会让 Recall@1 从 0.51 掉到 0.31 —— 因为语义通道的价值正是命中**没有字面重叠**的
    同义改写（"我答应过她什么" vs "你答应过今晚把灯点上"）。
    """
    strong_fusion = _cand("mem:a", "完全不含查询字样的回忆", source="memory", sem=1)
    weak_fusion = _cand("book:0", "查询里的字全都在这句里：灯塔灯亮", lex=3)
    result = _run([weak_fusion], [strong_fusion], query="灯塔灯亮", budget=0)
    assert result.kept[0].key == "mem:a", "名次更好的那路必须赢，不能被字面重叠翻盘"


def test_default_tiebreak_prefers_stronger_channel_evidence() -> None:
    """融合分打平时（两路同名词次），按"每路归一化后的证据强度"裁决先后。

    实测依据见 `retrieval.rerank()` 的注释：按先验分裁决会把 Recall@1 从 0.51 打到 0.26，
    按归一化证据裁决则与"最优"持平，而且规则说得清（不靠 key 字母序碰巧排对）。
    """
    # 关键词通道：第 1 名 3.0 分、第 2 名 2.0 分；语义通道：第 1 名 0.9、第 2 名 0.3
    lexical = [
        _cand("book:0", "关键词最强", lex=1, lex_score=3.0),
        _cand("book:1", "关键词次强", lex=2, lex_score=2.0),
    ]
    semantic = [
        _cand("mem:a", "语义最强", source="memory", sem=1, sem_score=0.9),
        _cand("mem:b", "语义次强", source="memory", sem=2, sem_score=0.3),
    ]
    result = _run(lexical, semantic, budget=0)
    order = [c.key for c in result.kept]
    # 第 1 名之间打平（归一化都是 1.0+1.0）→ 按 key 兜底，book:0 在前；
    # 第 2 名之间打平（都 1/62）→ 证据更强的是 book:1（2/3）而不是 mem:b（0.3/0.9）
    assert order.index("book:1") < order.index("mem:b")
    assert order[0] == "book:0"


def test_blend_rerank_is_available_for_ablation_only() -> None:
    """blend（词面重叠加权）保留下来只为消融对照：它能改变次序，但不是默认。"""
    assert rtv.DEFAULT_RERANK == rtv.RERANK_TIEBREAK
    short_key = _cand("book:0", "灯", lex=1)
    relevant = _cand("mem:a", "补给船今晚要靠灯塔认路", source="memory", sem=1)
    blended = rtv.retrieve(
        keyword_provider=lambda: [short_key],
        semantic_provider=lambda: ([relevant], []),
        query="补给船要靠灯塔认路吗",
        budget=0,
        rerank_mode=rtv.RERANK_BLEND,
    )
    assert blended.kept[0].key == "mem:a", "blend 模式下重叠率高的那条会排前面"


def test_overlap_ratio_is_bounded_and_ignores_punctuation() -> None:
    assert rtv.overlap_ratio("", "任何文本") == 0.0
    assert rtv.overlap_ratio("灯塔", "灯塔") == pytest.approx(1.0)
    assert 0 < rtv.overlap_ratio("灯塔亮了吗", "灯塔在风暴夜会熄灭") < 1


# ==================================================================
#  五、降级与可解释
# ==================================================================
def test_semantic_channel_failure_degrades_with_reason() -> None:
    """语义通道挂了 → 关键词照常工作，并且**如实报告**（拒绝静默降级）。"""
    result = rtv.retrieve(
        keyword_provider=lambda: [_cand("book:0", "关键词命中", lex=1)],
        semantic_provider=lambda: (_ for _ in ()).throw(RuntimeError("向量库炸了")),
        query="灯塔",
        budget=0,
    )
    assert [c.key for c in result.kept] == ["book:0"]
    assert result.errors and "向量库炸了" in result.errors[0]
    assert "向量库炸了" in rtv.describe(result)


def test_describe_and_debug_lines_tell_the_truth() -> None:
    result = _run(
        [_cand("book:0", "灯" * 40, lex=1)],
        [_cand("mem:a", "记" * 40, source="memory", sem=1)],
        budget=30,
    )
    summary = rtv.describe(result)
    assert "关键词候选 1 条" in summary and "语义候选 1 条" in summary
    assert rtv.MODE_RRF in summary
    lines = rtv.debug_lines(result)
    assert lines[0].startswith("✔")
    assert any(line.startswith("✘") and "预算" in line for line in lines)


def test_empty_retrieval_says_so() -> None:
    result = _run([], [])
    assert result.kept == [] and result.dropped == []
    assert "两路都没有候选" in rtv.describe(result)


# ==================================================================
#  六、扫描器：新接口与旧行为
# ==================================================================
class _Book:
    def __init__(self, entries: list) -> None:
        self.entries = entries


class _Msg:
    def __init__(self, content: str) -> None:
        self.content = content


def test_scan_candidates_reports_which_keys_matched() -> None:
    book = _Book(
        [
            {"keys": ["灯塔", "灯"], "content": "灯会熄灭", "insertion_order": 3},
            {"keys": ["补给船"], "content": "补给船二十天一次"},
            {"keys": ["龙"], "content": "龙已死去"},
        ]
    )
    matches = world_book_scanner.scan_candidates(book, [_Msg("灯塔还亮着吗")])
    assert len(matches) == 1
    assert matches[0].matched_keys == ("灯塔", "灯")
    assert matches[0].insertion_order == 3
    assert matches[0].content == "灯会熄灭"
    # 命中越多/越长，分越高（"灯塔"+"灯" = 2 个关键词 + 0.3 长度奖励）
    assert matches[0].score == pytest.approx(2.3)


def test_scan_and_scan_candidates_agree_on_counts() -> None:
    """★ scan() 的行为一个字都不能变（世界书单测与预设预览都依赖它）。"""
    book = _Book(
        [
            {"keys": ["灯塔"], "content": "灯会熄灭"},
            {"keys": ["龙"], "content": "龙已死去"},
        ]
    )
    messages = [_Msg("灯塔还亮着吗")]
    legacy = world_book_scanner.scan(book, messages)
    detailed = world_book_scanner.scan_candidates(book, messages)
    assert legacy.matched == len(detailed) == 1
    assert legacy.entries == [detailed[0].entry]
    assert legacy.dropped == 0


# ==================================================================
#  七、端到端：真的接进提示词了吗（预览与实际必须一致）
# ==================================================================
class FakeAdapter:
    script: dict = {}

    def __init__(self, row=None, **_):
        self.row = row
        self.default_params = GenerationParams(max_tokens=512)

    @property
    def budget(self):
        window = getattr(self.row, "context_window", None) or 8192
        max_out = getattr(self.row, "max_tokens", None) or 1024
        return compute_context_budget(window, max_out)

    def chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        return ChatResult(
            content=FakeAdapter.script.get("content", "（假回复）"),
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        yield StreamChunk(delta=FakeAdapter.script.get("content", "（假回复）"))
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(total_tokens=15))

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    FakeAdapter.script = {}
    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    monkeypatch.setattr(engine.memory_mod, "remember_turn", lambda **_: None)
    yield FakeAdapter
    FakeAdapter.script = {}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"rt_{token}",
        "email": f"rt_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged_in.status_code == 200
    data = {
        "username": account["username"],
        "headers": {"Authorization": f"Bearer {logged_in.json()['data']['access_token']}"},
    }
    yield data
    # ★ 跑完把这个测试账号删掉（它名下的卡/书/会话随外键级联消失）：
    #   否则反复跑 pytest 会在用户库里堆一堆 rt_* 账号（真实发生过）。
    from sqlalchemy import select

    from app.db.models import User
    from app.db.mysql import session_scope

    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == data["username"]))
        if row is not None:
            db.delete(row)


def _session_with_book(
    client: TestClient, user: dict, entries: list, *, token_budget: int | None = None
) -> int:
    payload: dict = {"name": f"检索世界_{uuid.uuid4().hex[:6]}", "entries": entries}
    if token_budget is not None:
        payload["token_budget"] = token_budget
    book = client.post(BOOKS, json=payload, headers=user["headers"])
    assert book.status_code == 201, book.text
    card = client.post(
        CARDS,
        json={
            "name": f"检索卡_{uuid.uuid4().hex[:6]}",
            "greeting": "……",
            "world_book_id": book.json()["data"]["id"],
        },
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"rt_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-retrieval-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    session = client.post(
        BASE,
        json={
            "character_card_id": card.json()["data"]["id"],
            "llm_provider_id": provider.json()["data"]["id"],
        },
        headers=user["headers"],
    )
    assert session.status_code == 201, session.text
    return session.json()["data"]["id"]


def test_end_to_end_fused_prompt_has_no_duplicate_and_reports_counts(
    client: TestClient, user: dict, fake_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 端到端：同一条设定被两路同时召回时，提示词里只能出现一次，且计数如实。"""
    session_id = _session_with_book(
        client,
        user,
        [{"keys": ["灯塔"], "content": "北岸的灯塔已熄灭一百年", "enabled": True}],
    )
    # 让语义通道返回一条**与世界书内容重复**的记忆 + 一条独有的记忆
    monkeypatch.setattr(
        memory_mod,
        "recall",
        lambda **_: memory_mod.RecallResult(
            hits=[
                MemoryHit(
                    memory_id="dup",
                    text="北岸的灯塔已熄灭一百年",
                    distance=0.1,
                    metadata={"session_id": session_id, "kind": "dialogue"},
                ),
                MemoryHit(
                    memory_id="uniq",
                    text="你答应过要给船长留一盏灯",
                    distance=0.2,
                    metadata={"session_id": session_id, "kind": "dialogue"},
                ),
            ]
        ),
    )
    fake_llm.script["content"] = "（海风很大）"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "灯塔还亮着吗"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    system = fake_llm.script["requests"][-1].messages[0].content

    assert system.count("北岸的灯塔已熄灭一百年") == 1, "同一条设定不能在两节里各出现一遍"
    assert "你答应过要给船长留一盏灯" in system, "独有的记忆必须照常注入"

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    prompt = detail["prompt"]
    assert prompt["retrieval"]["deduped"] == 1
    assert prompt["retrieval"]["book_injected"] == 1
    assert prompt["retrieval"]["memory_injected"] == 1
    assert "混合检索" in prompt["retrieval_summary"]
    assert "跨通道去重" in " ".join(prompt["retrieval_items"])
    # 计数与实际注入一致（预览不许骗人）
    assert prompt["world_book_entries"] == 1
    assert prompt["recalled_memories"] == 1


def test_end_to_end_preset_preview_uses_the_same_retrieval(
    client: TestClient, user: dict
) -> None:
    """★ 回归守卫：预设的「装配预览」也必须走混合检索这条唯一的路。

    以前这里单独调 `world_book_scanner.scan`（用世界书自己的预算），
    换成共享预算之后它会显示"注入了 N 条"而真实请求只进 1 条 —— 预览骗人。
    实际踩过一次：改完 retrieval 后这里残留了变量名 `scan`，接口直接 500，
    而**单测没覆盖**（是浏览器探针 10.8 抓出来的），所以这里补上。
    """
    book = client.post(
        BOOKS,
        json={
            "name": f"预览世界_{uuid.uuid4().hex[:6]}",
            "entries": [{"keys": ["灯塔"], "content": "灯塔在风暴夜会熄灭", "enabled": True}],
        },
        headers=user["headers"],
    )
    assert book.status_code == 201, book.text
    card = client.post(
        CARDS,
        json={
            "name": f"预览卡_{uuid.uuid4().hex[:6]}",
            "greeting": "……",
            "world_book_id": book.json()["data"]["id"],
        },
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    session = client.post(
        BASE,
        json={"character_card_id": card.json()["data"]["id"]},
        headers=user["headers"],
    ).json()["data"]
    response = client.get(
        "/api/v1/prompt-presets/preview",
        params={"session_id": session["id"]},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    notes = " ".join(response.json()["data"]["notes"])
    assert "共享 token 预算" in notes, notes


def test_end_to_end_budget_squeeze_is_disclosed(
    client: TestClient, user: dict, fake_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """世界书自己装不下时必须在 warnings 里说清，并且**记忆不许挤掉它**。"""
    # 两条世界书条目都写得很长（各 30+ token），预算只有 40：
    # 第 1 条占满预算后，第 2 条必然因预算不足被丢弃 → 触发如实提醒。
    session_id = _session_with_book(
        client,
        user,
        [
            {
                "keys": ["灯塔", "今晚", "灯"],
                "content": "夜里点灯要先看风向，再看潮水，最后看灯芯是不是干的。",
                "enabled": True,
            },
            {
                "keys": ["灯"],
                "content": "北岸灯塔的规矩：绞盘顺时针转三圈，再从下往上依次点三盏火，中途不能停。",
                "enabled": True,
            },
        ],
        token_budget=40,
    )
    monkeypatch.setattr(
        memory_mod,
        "recall",
        lambda **_: memory_mod.RecallResult(
            hits=[
                MemoryHit(
                    memory_id=f"m{i}",
                    text="你说过今晚要留一盏灯亮着",
                    distance=0.1,
                    metadata={"session_id": session_id, "kind": "dialogue"},
                )
                for i in range(3)
            ]
        ),
    )
    fake_llm.script["content"] = "（沉默）"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "今晚灯亮吗"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    prompt = detail["prompt"]
    retrieval = prompt["retrieval"]
    assert retrieval["budget_tokens"] == 40
    assert retrieval["injected"] >= 1, "预算再小也要注入最高分那条"
    assert retrieval["dropped"] >= 1, "预算不足时确实有候选被丢掉"
    # ★ 关键：被丢的是**回忆**，不是世界书（世界书优先装填）
    assert retrieval["book_injected"] == 1, "世界书那条必须进来"
    assert retrieval["memory_dropped"] >= 1, "预算不够时先牺牲回忆"
    assert any("世界书优先" in w for w in prompt["warnings"]), prompt["warnings"]
    assert "未注入" in prompt["retrieval_summary"], prompt["retrieval_summary"]
    assert any("预算不足" in item for item in prompt["retrieval_items"])
