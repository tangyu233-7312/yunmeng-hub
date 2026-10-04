"""纯聊天会话（无角色）测试。

==================== 覆盖重点 ====================
1. 不带 character_card_id 就能建会话，标题自动是「纯聊天 · 时间」
2. 提示词走**通用助手**分支：没有人设、没有身份/沉浸守卫、没有状态协议
3. 纯聊天里模型就算吐了 <state> 也只剥掉、不落库、不提醒（我们压根没要求它）
4. ★ 纯聊天 ≠ 角色卡被删除的叙事会话：两者 character_card_id 都是空，
   但只有后者要提示"人设丢了" —— 这条靠 narrative_sessions.kind 区分，必须守住
5. 会话详情里的 provider 暴露协议 / 流式开关（纯聊天当异构适配层验收台用）
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import User
from app.db.mysql import session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative.prompt_builder import PURE_CHAT_SYSTEM_PROMPT

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


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


def _make_user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"pc_{token}",
        "email": f"pc_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    created = client.post("/api/v1/auth/register", json=account)
    assert created.status_code == 201, created.text
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged_in.status_code == 200, logged_in.text
    return {
        "id": created.json()["data"]["id"],
        "headers": {"Authorization": f"Bearer {logged_in.json()['data']['access_token']}"},
    }


@pytest.fixture
def user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    with session_scope() as db:
        row = db.query(User).filter(User.id == data["id"]).one_or_none()
        if row is not None:
            db.delete(row)


def _provider(client: TestClient, user: dict, **over) -> int:
    payload = {
        "name": f"pc_{uuid.uuid4().hex[:6]}",
        "provider_type": "openai_compatible",
        "base_url": "https://mock.invalid/v1",
        "api_key": "sk-pure-chat",
        "model_name": "mock-model",
        "context_window": 8192,
        **over,
    }
    created = client.post(PROVIDERS, json=payload, headers=user["headers"])
    assert created.status_code == 201, created.text
    return created.json()["data"]["id"]


# ==================================================================
#  一、建会话与详情
# ==================================================================
def test_create_pure_chat_session_without_card(client: TestClient, user: dict) -> None:
    provider_id = _provider(client, user)
    created = client.post(
        BASE, json={"llm_provider_id": provider_id}, headers=user["headers"]
    )
    assert created.status_code == 201, created.text
    detail = created.json()["data"]
    assert detail["pure_chat"] is True
    assert detail["character_card"] is None
    assert detail["title"].startswith("纯聊天 ·")
    assert detail["messages"] == [], "纯聊天没有开场白"
    assert detail["state"] is None
    # 预设/守卫一律不生效 —— 界面必须如实报 null，否则就是"界面骗人"
    assert detail["prompt_preset"] is None
    assert detail["effective_preset"] is None
    assert detail["builtin_guard"] is None
    # 体检台要用的字段
    assert detail["provider"]["provider_type"] == "openai_compatible"
    assert detail["provider"]["stream_enabled"] is True


def test_pure_chat_rejects_greeting_index(client: TestClient, user: dict) -> None:
    response = client.post(
        BASE, json={"greeting_index": 0}, headers=user["headers"]
    )
    assert response.status_code == 400
    assert "greeting_index" in response.text


def test_provider_line_reflects_stream_switch(client: TestClient, user: dict) -> None:
    provider_id = _provider(client, user, stream_enabled=False)
    session = client.post(
        BASE, json={"llm_provider_id": provider_id}, headers=user["headers"]
    ).json()["data"]
    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
    assert detail["provider"]["stream_enabled"] is False


# ==================================================================
#  二、提示词走「通用助手」分支
# ==================================================================
def test_pure_chat_prompt_is_generic_assistant(client: TestClient, user: dict, fake_llm) -> None:
    provider_id = _provider(client, user)
    session = client.post(
        BASE, json={"llm_provider_id": provider_id}, headers=user["headers"]
    ).json()["data"]

    fake_llm.script["content"] = "好的，2 加 2 等于 4。"
    sent = client.post(
        f"{BASE}/{session['id']}/messages",
        json={"content": "2+2 等于几？"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text

    system = fake_llm.script["requests"][-1].messages[0].content
    assert system == PURE_CHAT_SYSTEM_PROMPT
    assert "通用 AI 助手" in system
    # 角色扮演那一套**一句都不能出现**
    assert "身份认知" not in system
    assert "虚构故事" not in system
    assert "剧情不跑偏" not in system
    assert "当前状态（每轮必须同步）" not in system
    assert "<state>" not in system
    # 世界书/回忆也不该被注入
    assert "世界设定" not in system


def test_pure_chat_reply_state_block_is_stripped_but_not_saved(
    client: TestClient, user: dict, fake_llm
) -> None:
    """我们没要求纯聊天输出状态块，所以：剥掉（别让用户看到 JSON）、但不落库、也不提醒。"""
    provider_id = _provider(client, user)
    session = client.post(
        BASE, json={"llm_provider_id": provider_id}, headers=user["headers"]
    ).json()["data"]
    fake_llm.script["content"] = '他说完了。\n<state>{"hp": {"current": 10, "max": 100}}</state>'
    sent = client.post(
        f"{BASE}/{session['id']}/messages", json={"content": "继续"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    stored = sent.json()["data"]["assistant_message"]["content"]
    assert "<state>" not in stored and stored.startswith("他说完了")

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
    assert detail["state"] is None, "纯聊天不落状态"
    notes = sent.json()["data"].get("notes") or []
    assert not any("状态块" in str(n) for n in notes), "没要求过的事不能反过来怪模型"


# ==================================================================
#  三、★ 与"角色卡被删除的故事会话"必须区分开
# ==================================================================
def test_deleted_card_story_session_still_warns_and_is_not_pure_chat(
    client: TestClient, user: dict
) -> None:
    provider_id = _provider(client, user)
    card = client.post(
        CARDS,
        json={"name": f"会被删掉的卡_{uuid.uuid4().hex[:6]}", "greeting": "……"},
        headers=user["headers"],
    ).json()["data"]
    session = client.post(
        BASE,
        json={"character_card_id": card["id"], "llm_provider_id": provider_id},
        headers=user["headers"],
    ).json()["data"]
    assert session["pure_chat"] is False

    # 删卡但保留会话（默认行为）：character_card_id 被置空，kind 仍然是 story
    deleted = client.delete(
        f"{CARDS}/{card['id']}",
        params={"force": "true", "delete_sessions": "false"},
        headers=user["headers"],
    )
    assert deleted.status_code == 200, deleted.text

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
    assert detail["character_card"] is None
    assert detail["pure_chat"] is False, "卡没了的故事会话 ≠ 纯聊天"
    assert any("角色卡已被删除" in w for w in detail["warnings"]), detail["warnings"]


def test_story_session_still_uses_persona_assembly(client: TestClient, user: dict, fake_llm) -> None:
    """回归守卫：加了纯聊天分支之后，普通叙事会话的装配一个字都不能变。

    ★ 状态协议只在**卡片声明了状态栏格式**时才注入（字段清单是卡的，不是代码的），
      所以这张卡显式声明一份 —— 否则「当前状态」那一节不该出现。
    """
    provider_id = _provider(client, user)
    card = client.post(
        f"{CARDS}/import",
        json={
            "card": {
                "spec": "chara_card_v2",
                "data": {
                    "name": "装配守卫",
                    "description": "一个爱数星星的人",
                    "first_mes": "……",
                    "extensions": {
                        "hne": {
                            "state_schema": [
                                {"name": "hp", "label": "HP", "type": "meter"},
                                {"name": "location", "label": "位置", "type": "text"},
                            ]
                        }
                    },
                },
            }
        },
        headers=user["headers"],
    ).json()["data"]
    session = client.post(
        BASE,
        json={"character_card_id": card["id"], "llm_provider_id": provider_id},
        headers=user["headers"],
    ).json()["data"]
    fake_llm.script["content"] = "（她抬头看天）"
    assert (
        client.post(
            f"{BASE}/{session['id']}/messages", json={"content": "你在看什么"}, headers=user["headers"]
        ).status_code
        == 201
    )
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "装配守卫" in system
    assert "身份认知" in system, "叙事会话必须仍然带内置守卫"
    assert "当前状态（每轮必须同步）" in system
    assert "hp（数字，0~max）" in system, "协议里的字段清单必须来自这张卡声明的 schema"
    assert "inventory" not in system, "卡没声明的字段不该出现在协议里"


def test_story_session_without_declared_schema_gets_no_state_protocol(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 回归守卫：卡没声明状态栏格式时，提示词里**不该**出现 HP 那一套。

    修的就是这个问题：写死的字段清单让每张卡都长出 HP 条，
    连从没声明过 HP 的卡（魔法少女 / 魔女裁判）也显示 `HP 100/100`。
    """
    provider_id = _provider(client, user)
    card = client.post(
        CARDS,
        json={"name": "没声明状态栏", "description": "一个爱数星星的人", "greeting": "……"},
        headers=user["headers"],
    ).json()["data"]
    session_row = client.post(
        BASE,
        json={"character_card_id": card["id"], "llm_provider_id": provider_id},
        headers=user["headers"],
    ).json()["data"]
    assert session_row["state_schema"]["fields"] == [], "没声明就该是空 schema"
    fake_llm.script["content"] = "（她抬头看天）"
    assert (
        client.post(
            f"{BASE}/{session_row['id']}/messages",
            json={"content": "你在看什么"},
            headers=user["headers"],
        ).status_code
        == 201
    )
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "身份认知" in system, "叙事会话必须仍然带内置守卫"
    assert "当前状态（每轮必须同步）" not in system
    assert "[输出格式 · 必须遵守]" not in system, "没定义状态栏就不该要求模型输出状态块"
    assert "hp" not in system.lower(), "不该硬塞 HP 那一套字段"


# ==================================================================
#  四、会话列表：纯聊天不该被提示"人设丢了"
# ==================================================================
def test_session_list_does_not_warn_pure_chat(client: TestClient, user: dict) -> None:
    provider_id = _provider(client, user)
    client.post(BASE, json={"llm_provider_id": provider_id}, headers=user["headers"])
    listed = client.get(BASE, headers=user["headers"]).json()["data"]["items"]
    chat_item = next(item for item in listed if item["title"].startswith("纯聊天"))
    assert chat_item["character_card"] is None
    assert not any("角色卡已被删除" in w for w in (chat_item.get("warnings") or []))
