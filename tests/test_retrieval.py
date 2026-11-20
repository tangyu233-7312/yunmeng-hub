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

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
BOOKS = "/api/v1/world-books"
PROVIDERS = "/api/v1/providers"


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
        "headers": {
            "Authorization": f"Bearer {logged_in.json()['data']['access_token']}"
        },
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

    assert system.count("北岸的灯塔已熄灭一百年") == 1, (
        "同一条设定不能在两节里各出现一遍"
    )
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


pytestmark = pytest.mark.integration
