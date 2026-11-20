"""骰子插件：随机数由**后端**掷（模型只负责叙述），受控表达式求值，点数落库。

这一组的重点：
  1. 求值器是**受控**的：不执行代码、有界、坏输入只报错不抛异常
  2. 点数在**用户消息落库那一刻**掷定并写进 `messages.rolls_json` ——
     所以「查看提示词」预览与真实请求给出的是**同一个数字**（不重掷）
  3. 模型写的 `<roll>` 无论插件是否启用都会被剥掉（绝不让用户看到原始协议文本）
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import dice as dice_mod
from app.narrative import engine

PLUGINS = "/api/v1/plugins"
BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


# ==================================================================
#  二、接口层：点数落库、「预览 == 实际」、开关
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
        "username": f"dc_{token}",
        "email": f"dc_{token}@example.com",
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
            "name": f"dc_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-dice-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    card = client.post(
        CARDS,
        # ★ 卡名里不能带"骰子"两个字：卡名会进系统提示词，会让下面
        #   "没启用插件时提示词里不该出现骰子"的断言永远为假（踩过一次）
        json={"name": f"检定卡_{uuid.uuid4().hex[:6]}", "greeting": "……你来了。"},
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


def _add_dice_plugin(client: TestClient, user: dict, **overrides) -> dict:
    config = {
        "triggers": ["/r", "/roll", "掷骰"],
        "default_expr": "1d100",
        "allow_model_roll": True,
        "show_detail": True,
        "explain": True,
        "max_dice": 100,
        "max_sides": 1000,
    }
    config.update(overrides)
    created = client.post(
        PLUGINS,
        json={
            "name": f"跑团骰点_{uuid.uuid4().hex[:6]}",
            "kind": "dice",
            "description": "测试用",
            "config": config,
            "enabled": True,
        },
        headers=user["headers"],
    )
    assert created.status_code == 201, created.text
    return created.json()["data"]


def test_a_broken_default_expression_is_rejected_at_save_time(
    client: TestClient, user: dict
) -> None:
    """★ 写错的默认表达式不该等到用户掷骰时才发现。"""
    bad = client.post(
        PLUGINS,
        json={"name": "坏的骰子", "kind": "dice", "config": {"default_expr": "1d6+"}},
        headers=user["headers"],
    )
    assert bad.status_code == 400, bad.text
    assert "默认表达式写错了" in bad.text


def test_user_command_rolls_once_and_the_prompt_carries_that_same_number(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 本组最关键的一条：点数落库 → 提示词里的数字与接口返回的数字**必须相同**。

    如果改成"构建提示词时现掷"，这里就会挂 —— 而用户点一次「查看提示词」
    就会看到另一个点数（"预览与实际不一致"）。
    """
    session_id = _make_session(client, user)
    _add_dice_plugin(client, user)
    fake_llm.script["content"] = "（他把骰子推回来）"

    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "/r 1d100 聆听"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    body = sent.json()["data"]
    rolls = body["user_message"]["rolls"]
    assert len(rolls) == 1 and rolls[0]["source"] == "user"
    assert rolls[0]["label"] == "聆听" and rolls[0]["total"] is not None

    system = fake_llm.script["requests"][-1].messages[0].content
    assert dice_mod.TURN_BLOCK_TITLE in system, "本轮点数必须以客观事实的形式进提示词"
    assert f"**{rolls[0]['total']}**" in system, "提示词里的点数与落库的点数必须一致"
    assert "剧情必须按上面的点数走" in system

    # 预览走的是同一条装配路径：数字也必须一样
    detail = client.get(
        f"{BASE}/{session_id}", params={"with_prompt": True}, headers=user["headers"]
    )
    assert detail.status_code == 200, detail.text
    preview = detail.json()["data"]["prompt"]["system_prompt"]
    assert f"**{rolls[0]['total']}**" in preview, "「查看提示词」不能给出另一个点数"


def test_dice_rules_are_only_injected_when_the_plugin_is_enabled(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _make_session(client, user)
    fake_llm.script["content"] = "嗯。"
    client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "/r 1d100"},
        headers=user["headers"],
    )
    system = fake_llm.script["requests"][-1].messages[0].content
    assert dice_mod.TURN_BLOCK_TITLE not in system, "没启用插件时不该注入本轮骰点"
    assert "<roll>" not in system, "没启用插件时不该告诉模型 <roll> 这种写法"

    created = _add_dice_plugin(client, user)
    client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "你好"},
        headers=user["headers"],
    )
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "<roll>" in system and "随机数" in system, "启用后必须告诉模型怎么请求掷骰"

    # 停用之后立刻不再生效（与其它插件同一套语义）
    assert (
        client.patch(
            f"{PLUGINS}/{created['id']}",
            json={"enabled": False},
            headers=user["headers"],
        ).status_code
        == 200
    )
    client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "/r 1d100"},
        headers=user["headers"],
    )
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "<roll>" not in system and dice_mod.TURN_BLOCK_TITLE not in system


def test_model_requested_roll_is_resolved_and_stored(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _make_session(client, user)
    _add_dice_plugin(client, user)
    fake_llm.script["content"] = "他抬手格挡。<roll>1d20+5</roll>（然后等你）"

    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我攻击他"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    assistant = sent.json()["data"]["assistant_message"]
    assert (
        "<roll>" not in assistant["content"] and "</roll>" not in assistant["content"]
    )
    assert "（掷骰 1d20+5 = " in assistant["content"]
    rolls = assistant["rolls"]
    assert len(rolls) == 1 and rolls[0]["source"] == "model"
    assert rolls[0]["expression"] == "1d20+5"
    assert f"= {rolls[0]['total']}" in assistant["content"], (
        "正文里的数字要与落库的一致"
    )

    # 下一轮模型在历史里看到的是**真实点数**（否则它只能自己编）
    client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "继续"},
        headers=user["headers"],
    )
    request = fake_llm.script["requests"][-1]
    history = " ".join(str(getattr(item, "content", "")) for item in request.messages)
    assert f"（掷骰 1d20+5 = {rolls[0]['total']}" in history


def test_rolls_survive_a_reload(client: TestClient, user: dict, fake_llm) -> None:
    """点数读的是落库那一份：重新打开会话（重新拉详情）必须还是同一个数字。"""
    session_id = _make_session(client, user)
    _add_dice_plugin(client, user)
    fake_llm.script["content"] = "（沉默）"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "/r 3d6 属性"},
        headers=user["headers"],
    )
    total = sent.json()["data"]["user_message"]["rolls"][0]["total"]

    for _ in range(2):
        detail = client.get(f"{BASE}/{session_id}", headers=user["headers"])
        messages = detail.json()["data"]["messages"]
        user_message = [m for m in messages if m["role"] == "user"][-1]
        assert user_message["rolls"][0]["total"] == total


def test_a_bad_user_expression_becomes_a_readable_error(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _make_session(client, user)
    _add_dice_plugin(client, user)
    fake_llm.script["content"] = "（不懂）"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "/r 1d6+"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    rolls = sent.json()["data"]["user_message"]["rolls"]
    assert len(rolls) == 1 and rolls[0]["error"]
    assert "×" not in rolls[0]["error"] and rolls[0]["total"] is None
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "不合法" in system and "别替他编一个点数" in system


def test_a_plugin_written_by_hand_can_be_created_from_scratch(
    client: TestClient, user: dict, fake_llm
) -> None:
    """手工新建（不走目录）也要能用 —— 校验只有一套。"""
    session_id = _make_session(client, user)
    created = _add_dice_plugin(client, user, triggers=["#投"], default_expr="1d6")
    assert created["kind"] == "dice"
    assert created["summary"].startswith("触发词")

    fake_llm.script["content"] = "（点头）"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "#投 3d6"},
        headers=user["headers"],
    )
    rolls = sent.json()["data"]["user_message"]["rolls"]
    assert len(rolls) == 1 and rolls[0]["expression"] == "3d6"


pytestmark = pytest.mark.integration
