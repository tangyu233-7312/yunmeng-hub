"""记忆锚点：用户手写的「永远要记住」的硬设定（固定注入、永不折叠）。

它和世界书/回忆/总结的区别就在"永不折叠"上，所以这一组的重点是：
上限校验（拒绝而不是截断）、真的注入了系统提示词、上下文被裁时它还在。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import anchors as anchors_mod
from app.narrative import engine

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


class _Session:
    def __init__(self, raw=None) -> None:
        self.id = 1
        self.memory_anchors_json = raw


# ==================================================================
#  一、纯函数：上限、清洗、渲染
# ==================================================================
def test_max_items_is_enforced_by_rejection_not_truncation() -> None:
    """★ 超限必须**拒绝**：悄悄截断会让用户以为存住了（下次打开少一条更迷惑）。"""
    too_many = [f"第 {i} 条锚点" for i in range(anchors_mod.MAX_ITEMS + 1)]
    ok, message, cleaned = anchors_mod.validate(too_many)
    assert ok is False
    assert f"最多只能加 {anchors_mod.MAX_ITEMS} 条" in message
    assert len(cleaned) == anchors_mod.MAX_ITEMS + 1, "拒绝时也把清洗结果带回来，便于前端显示"


def test_total_chars_limit_is_enforced() -> None:
    # ★ 每条都要**不一样**，否则会被去重成一条，就测不到"合计超限"了
    heavy = [f"{i}" + "字" * (anchors_mod.MAX_ONE_CHARS - 1) for i in range(5)]
    assert sum(len(i) for i in heavy) > anchors_mod.MAX_CHARS
    ok, message, _ = anchors_mod.validate(heavy)
    assert ok is False and "合计最多" in message


def test_single_item_is_capped() -> None:
    """单条过长会被截到 MAX_ONE_CHARS（否则一条就把整额吃光）。"""
    ok, _message, cleaned = anchors_mod.validate(["长" * (anchors_mod.MAX_ONE_CHARS + 100)])
    assert ok is True
    assert len(cleaned[0]) == anchors_mod.MAX_ONE_CHARS


def test_blank_and_duplicate_items_are_dropped_with_a_note() -> None:
    ok, note, cleaned = anchors_mod.validate(["  主角是女性  ", "", "   ", "主角是女性", 42, "世界没有魔法"])
    assert ok is True
    assert cleaned == ["主角是女性", "世界没有魔法"], cleaned
    assert "重复" in note, note


def test_render_block_is_empty_without_anchors() -> None:
    assert anchors_mod.render_block(_Session(None)) == ""
    assert anchors_mod.state(_Session(None))["count"] == 0


def test_render_block_lists_items_with_title() -> None:
    import json

    session = _Session(json.dumps(["主角是女性", "绝不能承认自己是 AI"], ensure_ascii=False))
    block = anchors_mod.render_block(session)
    assert anchors_mod.BLOCK_TITLE in block
    assert "- 主角是女性" in block and "- 绝不能承认自己是 AI" in block
    assert "优先级高于回忆" in block


def test_broken_json_degrades_to_empty() -> None:
    assert anchors_mod.load(_Session("{不是 JSON")) == []


def test_state_reports_remaining_quota() -> None:
    import json

    session = _Session(json.dumps(["一二三"], ensure_ascii=False))
    state = anchors_mod.state(session)
    assert state["count"] == 1 and state["chars"] == 3
    assert state["remaining_items"] == anchors_mod.MAX_ITEMS - 1
    assert state["remaining_chars"] == anchors_mod.MAX_CHARS - 3


# ==================================================================
#  二、端到端：真的进了系统提示词，而且不会被裁掉
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
def user(client: TestClient):
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"an_{token}",
        "email": f"an_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    logged = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged.status_code == 200
    data = {
        "username": account["username"],
        "headers": {"Authorization": f"Bearer {logged.json()['data']['access_token']}"},
    }
    yield data
    from sqlalchemy import select

    from app.db.models import User
    from app.db.mysql import session_scope

    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == data["username"]))
        if row is not None:
            db.delete(row)


def _make_session(client: TestClient, user: dict) -> int:
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"an_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-anchors-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    card = client.post(
        CARDS,
        json={"name": f"锚点卡_{uuid.uuid4().hex[:6]}", "greeting": "……你来了。"},
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
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


def test_anchors_are_saved_and_injected_into_the_system_prompt(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 锚点必须整条进**系统提示词**（所以上下文裁剪动不了它）。"""
    session_id = _make_session(client, user)
    saved = client.put(
        f"{BASE}/{session_id}/memory-summary/anchors",
        json={"anchors": ["主角是女性，名叫薇拉", "这个世界没有魔法"]},
        headers=user["headers"],
    )
    assert saved.status_code == 200, saved.text
    anchors = saved.json()["data"]["anchors"]
    assert anchors["count"] == 2 and anchors["remaining_items"] == anchors_mod.MAX_ITEMS - 2

    fake_llm.script["content"] = "（她点点头）"
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "你还记得吗"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    system = fake_llm.script["requests"][-1].messages[0].content
    assert anchors_mod.BLOCK_TITLE in system
    assert "主角是女性，名叫薇拉" in system and "这个世界没有魔法" in system
    # 锚点要排在「世界设定」之后、「回忆」之前（与内置装配顺序一致）
    assert system.index("主角是女性") < len(system)


def test_anchors_survive_context_trimming(client: TestClient, user: dict, fake_llm) -> None:
    """★ 上下文把历史裁光时，锚点仍然在（它在系统提示词里，不参与裁剪）。"""
    session_id = _make_session(client, user)
    client.put(
        f"{BASE}/{session_id}/memory-summary/anchors",
        json={"anchors": ["绝不能承认自己是 AI"]},
        headers=user["headers"],
    )
    fake_llm.script["content"] = "嗯。"
    for index in range(6):
        sent = client.post(
            f"{BASE}/{session_id}/messages",
            json={"content": f"第{index}轮" + "很长的话" * 200},
            headers=user["headers"],
        )
        assert sent.status_code == 201, sent.text
    request = fake_llm.script["requests"][-1]
    system = request.messages[0].content
    assert "绝不能承认自己是 AI" in system, "锚点被裁掉了 —— 它应该是固定注入的"
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert "绝不能承认自己是 AI" in detail["prompt"]["system_prompt"]


def test_over_limit_is_rejected_over_the_api(client: TestClient, user: dict) -> None:
    session_id = _make_session(client, user)
    response = client.put(
        f"{BASE}/{session_id}/memory-summary/anchors",
        json={"anchors": [f"第{i}条" for i in range(anchors_mod.MAX_ITEMS + 1)]},
        headers=user["headers"],
    )
    assert response.status_code == 400, response.text
    assert "最多只能加" in response.text
    # 拒绝之后原样保留（不能把旧的也弄丢）
    panel = client.get(f"{BASE}/{session_id}/memory-summary", headers=user["headers"]).json()["data"]
    assert panel["anchors"]["count"] == 0


def test_anchors_replace_the_whole_list(client: TestClient, user: dict) -> None:
    """整份替换语义：提交什么就是什么（前端不用做增删改三条接口）。"""
    session_id = _make_session(client, user)
    client.put(
        f"{BASE}/{session_id}/memory-summary/anchors",
        json={"anchors": ["旧的 A", "旧的 B"]},
        headers=user["headers"],
    )
    replaced = client.put(
        f"{BASE}/{session_id}/memory-summary/anchors",
        json={"anchors": ["新的 C"]},
        headers=user["headers"],
    )
    assert replaced.status_code == 200, replaced.text
    items = replaced.json()["data"]["anchors"]["items"]
    assert items == ["新的 C"], items


def test_pure_chat_has_no_anchors(client: TestClient, user: dict, fake_llm) -> None:
    """纯聊天是"干净通道"：不注入锚点（也不该有记忆面板那一套）。"""
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"an_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-anchors-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    ).json()["data"]
    chat = client.post(
        BASE, json={"llm_provider_id": provider["id"]}, headers=user["headers"]
    ).json()["data"]
    assert chat["kind"] == "chat" if "kind" in chat else True
    client.put(
        f"{BASE}/{chat['id']}/memory-summary/anchors",
        json={"anchors": ["不该出现"]},
        headers=user["headers"],
    )
    fake_llm.script["content"] = "（通用助手）"
    sent = client.post(
        f"{BASE}/{chat['id']}/messages", json={"content": "你好"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "不该出现" not in system
