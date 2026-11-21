"""3.8 叙事会话与对话接口测试（集成测试，需要 MySQL）。

==================== 覆盖范围 ====================
  一、提示词构建（纯单元，不碰数据库、不联网）
      · 角色卡自带 system_prompt 时优先采用
      · 否则按人设字段自动拼装
      · 世界书条目注入
      · 开场白作为第一条 assistant 消息
      · post_history_instructions 追加在历史之后
  二、上下文裁剪（纯单元）
      · 超预算时丢最早的对话、保留最近的消息
      · 保留滚动摘要
      · 系统提示词本身就超预算时按比例截断并如实标记
  三、会话 CRUD + 越权
      · 建会话会写入开场白；列表/详情/改名/归档/删除
      · 别人的会话一律 404
  四、发消息（非流式）
      · 落库两条消息、更新 message_count / total_tokens / last_active_at
      · 适配层的 notes（参数调整）必须透传到响应
      · 模型报错时用户消息仍然保留
  五、SSE 流式
      · 事件顺序 meta → delta… → done
      · 正文逐条推送、最终落库
      · 适配层报错时发出 error 事件（而不是静默断开）

★ 测试**绝不调用真实大模型 API**（红线）：
  一律 monkeypatch 掉 app/narrative/engine.py 里的 build_adapter，
  换成不联网的假适配器；协议解析本身由 tests/test_llm.py 用 MockTransport 覆盖。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_narrative.py -q
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from app.db.models import Message, NarrativeSession, User
from app.db.mysql import get_engine, session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine, streaming
from app.services import imagegen_service

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


# ==================================================================
#  夹具
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"nr_{token}",
        "email": f"nr_{token}@example.com",
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
        "username": account["username"],
        "headers": {
            "Authorization": f"Bearer {logged_in.json()['data']['access_token']}"
        },
    }


def _cleanup_user(username: str) -> None:
    """删掉测试账号（会话 / 消息由外键级联删除）。"""
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == username))
        if row is not None:
            db.delete(row)


@pytest.fixture
def user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    _cleanup_user(data["username"])


@pytest.fixture
def other_user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    _cleanup_user(data["username"])


def make_card(client: TestClient, user: dict, **overrides) -> dict:
    body = {
        "name": f"卡_{uuid.uuid4().hex[:6]}",
        "personality": "冷静、话少",
        "background": "来自北方的旅人",
        "speaking_style": "简短，爱用短句",
        "greeting": "……你来了。",
        "post_history_instructions": "回复请控制在三句话以内。",
    }
    body.update(overrides)
    response = client.post(CARDS, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_provider(client: TestClient, user: dict, **overrides) -> dict:
    body = {
        "name": f"cfg_{uuid.uuid4().hex[:6]}",
        "provider_type": "openai_compatible",
        "base_url": "https://mock.invalid/v1",
        "api_key": "sk-test-not-a-real-key",
        "model_name": "mock-model",
        "context_window": 8192,
        "generation": {
            "temperature": 0.8,
            "max_tokens": 1024,
            "reasoning_effort": "auto",
        },
    }
    body.update(overrides)
    response = client.post(PROVIDERS, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_session(
    client: TestClient, user: dict, card_id: int, provider_id: int, **kw
) -> dict:
    body = {"character_card_id": card_id, "llm_provider_id": provider_id}
    body.update(kw)
    response = client.post(BASE, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ==================================================================
#  假适配器（★ 不联网）
# ==================================================================
class FakeAdapter:
    """一个"假装调用了模型"的适配器。

    它记录收到的 ChatRequest（可以断言提示词拼得对不对），
    并按预设脚本返回回复或抛错。
    """

    #: 类级共享，测试里随时改
    script: dict[str, Any] = {}

    def __init__(self, row=None, **_: Any) -> None:
        self.row = row
        self.requests: list[Any] = []
        self.closed = False
        self.default_params = GenerationParams(max_tokens=512)
        self.context_window = 8192

    # 与真实适配器一致的两个属性
    @property
    def budget(self):
        # ★ 必须跟着 provider 行里的上下文窗口走：测试里故意把窗口配得很小
        #   来触发裁剪，如果这里写死一个大数字，裁剪永远不会发生
        window = getattr(self.row, "context_window", None) or self.context_window
        max_out = (
            getattr(self.row, "max_tokens", None) or self.default_params.max_tokens
        )
        return compute_context_budget(window, max_out)

    def chat(self, request):
        self.requests.append(request)
        FakeAdapter.script.setdefault("requests", []).append(request)
        error = FakeAdapter.script.get("error")
        if error is not None:
            raise error
        return ChatResult(
            content=FakeAdapter.script.get("content", "（假回复）"),
            model="mock-model",
            finish_reason="stop",
            notes=list(FakeAdapter.script.get("notes", [])),
            usage=TokenUsage(prompt_tokens=30, completion_tokens=12, total_tokens=42),
            latency_ms=7,
        )

    def stream_chat(self, request):
        self.requests.append(request)
        FakeAdapter.script.setdefault("requests", []).append(request)
        for note in FakeAdapter.script.get("stream_notes", []):
            yield StreamChunk(notes=[note])
        error = FakeAdapter.script.get("stream_error")
        for piece in FakeAdapter.script.get("stream", ["你", "好", "呀"]):
            yield StreamChunk(delta=piece)
        if error is not None:
            # 已经吐过内容之后再报错：这正是"流式中途失败"的真实形态
            raise error
        yield StreamChunk(
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=40, completion_tokens=9, total_tokens=49),
        )

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """把所有模型调用换成假适配器，并把长期记忆换成不落盘的空壳。

    ★ 为什么要连记忆一起挡掉？
      3.9 之后每一轮对话都会往 ChromaDB 写一条记忆（真实调用嵌入模型、真实落盘），
      如果这里不挡，48 个叙事用例会各自留下向量数据、拖慢 3 分钟并污染向量库。
      记忆本身的行为由 tests/test_memory.py 用真实的 ChromaDB 覆盖 ——
      那里才是它该被测的地方。
    """
    FakeAdapter.script = {}
    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    monkeypatch.setattr(engine.memory_mod, "remember_turn", lambda **_: None)
    monkeypatch.setattr(
        engine.memory_mod,
        "recall_block",
        lambda **_: ("", engine.memory_mod.RecallResult()),
    )
    yield FakeAdapter
    FakeAdapter.script = {}


def last_request() -> Any:
    requests = FakeAdapter.script.get("requests") or []
    assert requests, "假适配器没有收到任何请求"
    return requests[-1]


# ==================================================================
#  三、会话 CRUD 与越权
# ==================================================================
def test_create_session_writes_greeting_as_first_message(
    client: TestClient, user: dict
) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    assert session["title"].startswith(card["name"])
    assert session["message_count"] == 1
    assert len(session["messages"]) == 1
    first = session["messages"][0]
    assert first["role"] == "assistant"
    assert first["content"] == "……你来了。"
    assert session["character_card"]["id"] == card["id"]
    assert session["provider"]["id"] == provider["id"]


def test_create_session_uses_alternate_greeting(client: TestClient, user: dict) -> None:
    card = make_card(
        client, user, greeting="默认开场", alternate_greetings=["备选一", "备选二"]
    )
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"], greeting_index=1)

    assert session["messages"][0]["content"] == "备选二"


def test_create_session_with_out_of_range_greeting_index(
    client: TestClient, user: dict
) -> None:
    card = make_card(client, user, alternate_greetings=["只有一条"])
    provider = make_provider(client, user)
    response = client.post(
        BASE,
        json={
            "character_card_id": card["id"],
            "llm_provider_id": provider["id"],
            "greeting_index": 5,
        },
        headers=user["headers"],
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "BAD_REQUEST"


def test_create_session_falls_back_to_default_provider(
    client: TestClient, user: dict
) -> None:
    """不传模型配置时用默认模型。"""
    card = make_card(client, user)
    make_provider(client, user, name=f"d_{uuid.uuid4().hex[:6]}", is_default=True)
    response = client.post(
        BASE, json={"character_card_id": card["id"]}, headers=user["headers"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["provider"] is not None


def test_create_session_without_any_provider(client: TestClient, user: dict) -> None:
    """一个模型配置都没有时，会话照样建得起来，但会给出提醒。"""
    card = make_card(client, user)
    response = client.post(
        BASE, json={"character_card_id": card["id"]}, headers=user["headers"]
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["provider"] is None
    assert any("模型配置" in w for w in data["warnings"])


def test_create_session_with_others_private_card_is_404(
    client: TestClient, user: dict, other_user: dict
) -> None:
    card = make_card(client, other_user)
    provider = make_provider(client, user)
    response = client.post(
        BASE,
        json={"character_card_id": card["id"], "llm_provider_id": provider["id"]},
        headers=user["headers"],
    )
    assert response.status_code == 404


def test_create_session_with_others_public_card_is_allowed(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """★ 公共卡库的卡可以拿来开局（能看不等于能改，但能用来对话）。"""
    card = make_card(client, other_user, is_public=True)
    provider = make_provider(client, user)
    response = client.post(
        BASE,
        json={"character_card_id": card["id"], "llm_provider_id": provider["id"]},
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text


def test_list_sessions_is_paginated_and_sorted_by_activity(
    client: TestClient, user: dict
) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    first = make_session(client, user, card["id"], provider["id"], title="第一个")
    second = make_session(client, user, card["id"], provider["id"], title="第二个")

    # 给第一个会话发一条消息 -> 它应当被顶到最前
    client.post(
        f"{BASE}/{first['id']}/messages",
        json={"content": "我还活着"},
        headers=user["headers"],
    )

    page = client.get(BASE, params={"limit": 5}, headers=user["headers"]).json()["data"]
    assert page["total"] >= 2
    ids = [item["id"] for item in page["items"]]
    assert ids.index(first["id"]) < ids.index(second["id"])
    # 列表是精简结构：不带 messages 字段
    assert "messages" not in page["items"][0]
    assert page["items"][ids.index(first["id"])]["last_message_preview"]


def test_list_sessions_loads_card_references_with_one_query(
    client: TestClient, user: dict
) -> None:
    provider = make_provider(client, user)
    sessions = []
    for _ in range(3):
        card = make_card(client, user)
        sessions.append(make_session(client, user, card["id"], provider["id"]))

    card_queries: list[str] = []
    provider_queries: list[str] = []
    branch_count_queries: list[str] = []

    def record_card_query(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = statement.casefold()
        if "character_cards" in normalized:
            card_queries.append(statement)
        if "llm_providers" in normalized:
            provider_queries.append(statement)
        if "group by" in normalized and "branch_group_id" in normalized:
            branch_count_queries.append(statement)

    engine_db = get_engine()
    event.listen(engine_db, "before_cursor_execute", record_card_query)
    try:
        response = client.get(BASE, params={"limit": 12}, headers=user["headers"])
    finally:
        event.remove(engine_db, "before_cursor_execute", record_card_query)

    assert response.status_code == 200, response.text
    items_by_id = {item["id"]: item for item in response.json()["data"]["items"]}
    assert len(card_queries) == 1
    assert len(provider_queries) == 1
    assert "api_key_encrypted" not in provider_queries[0].casefold()
    assert branch_count_queries == []
    for session in sessions:
        item = items_by_id[session["id"]]
        assert item["character_card"] == session["character_card"]
        assert item["provider_name"] == session["provider"]["name"]
        assert item["model_name"] == session["provider"]["model_name"]


def test_list_sessions_counts_only_branch_groups_on_the_current_page(
    client: TestClient, user: dict
) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    old_main = make_session(client, user, card["id"], provider["id"], title="旧主线")
    old_twin = make_session(
        client, user, card["id"], provider["id"], title="旧平行线"
    )
    main = make_session(client, user, card["id"], provider["id"], title="主线")
    twin = make_session(client, user, card["id"], provider["id"], title="平行线")

    with session_scope() as db:
        for main_session, twin_session in ((old_main, old_twin), (main, twin)):
            main_row = db.get(NarrativeSession, main_session["id"])
            twin_row = db.get(NarrativeSession, twin_session["id"])
            assert main_row is not None and twin_row is not None
            main_row.branch_group_id = main_row.id
            twin_row.branch_group_id = main_row.id
            main_row.branch_index = 0
            twin_row.branch_index = 1

    branch_count_queries: list[tuple[str, object]] = []

    def record_branch_count_query(
        _connection, _cursor, statement, parameters, _context, _many
    ):
        normalized = statement.casefold()
        if "group by" in normalized and "branch_group_id" in normalized:
            branch_count_queries.append((statement, parameters))

    engine_db = get_engine()
    event.listen(engine_db, "before_cursor_execute", record_branch_count_query)
    try:
        response = client.get(
            BASE, params={"limit": 1}, headers=user["headers"]
        )
    finally:
        event.remove(engine_db, "before_cursor_execute", record_branch_count_query)

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) == 1
    main_item = items[0]
    assert main_item["id"] == main["id"]
    assert main_item["branch_line_count"] == 2
    assert main_item["branch_line"] == "主线"
    assert twin["id"] not in {item["id"] for item in items}
    assert len(branch_count_queries) == 1
    statement, parameters = branch_count_queries[0]
    assert "IN" in statement
    assert int(old_main["id"]) not in parameters
    assert int(main["id"]) in parameters


def test_list_sessions_can_filter_archived(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])
    client.patch(
        f"{BASE}/{session['id']}", json={"status": "archived"}, headers=user["headers"]
    )

    active = client.get(
        BASE, params={"status": "active"}, headers=user["headers"]
    ).json()["data"]
    archived = client.get(
        BASE, params={"status": "archived"}, headers=user["headers"]
    ).json()["data"]
    assert session["id"] not in [i["id"] for i in active["items"]]
    assert session["id"] in [i["id"] for i in archived["items"]]


def test_delete_session_cascades_messages(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.delete(f"{BASE}/{session['id']}", headers=user["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["data"]["deleted_messages"] == 1

    # ★ 真删一次验证：消息必须被级联删干净，不留孤儿行
    with session_scope() as db:
        assert db.get(NarrativeSession, session["id"]) is None
        left = db.scalars(
            select(Message).where(Message.session_id == session["id"])
        ).all()
        assert list(left) == []

    assert (
        client.get(f"{BASE}/{session['id']}", headers=user["headers"]).status_code
        == 404
    )


def test_session_detail_can_skip_prompt_preview(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    detail = client.get(
        f"{BASE}/{session['id']}",
        params={"with_prompt": "false"},
        headers=user["headers"],
    ).json()["data"]
    assert detail["prompt"] is None

    with_prompt = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert with_prompt["prompt"]["system_prompt_source"] == "assembled"


def test_message_limit_returns_latest_messages(client: TestClient, user: dict) -> None:
    card = make_card(client, user, greeting="开场")
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    for i in range(5):
        client.post(
            f"{BASE}/{session['id']}/messages",
            json={"content": f"第{i}条"},
            headers=user["headers"],
        )

    detail = client.get(
        f"{BASE}/{session['id']}", params={"message_limit": 3}, headers=user["headers"]
    ).json()["data"]
    assert len(detail["messages"]) == 3
    assert detail["messages_truncated"] is True
    assert detail["messages_total"] == 11
    # 返回的是**最近** 3 条（顺序仍是时间升序）
    assert detail["messages"][-1]["content"] == "（假回复）"


# ==================================================================
#  四、越权：别人的会话一律 404
# ==================================================================
@pytest.mark.parametrize(
    ("method", "suffix", "kwargs"),
    [
        ("get", "", {}),
        ("patch", "", {"json": {"title": "偷改"}}),
        ("delete", "", {}),
        ("post", "/messages", {"json": {"content": "偷聊"}}),
        ("get", "/stream", {"params": {"content": "偷聊"}}),
    ],
)
def test_other_users_session_is_404(
    client: TestClient,
    user: dict,
    other_user: dict,
    method: str,
    suffix: str,
    kwargs: dict,
) -> None:
    card = make_card(client, other_user)
    provider = make_provider(client, other_user)
    session = make_session(client, other_user, card["id"], provider["id"])

    response = getattr(client, method)(
        f"{BASE}/{session['id']}{suffix}", headers=user["headers"], **kwargs
    )
    assert response.status_code == 404, response.text


def test_narrative_requires_login(client: TestClient) -> None:
    assert client.get(BASE).status_code == 401
    assert client.post(BASE, json={}).status_code == 401


# ==================================================================
#  五、发消息（非流式）
# ==================================================================
def test_send_message_persists_and_updates_stats(
    client: TestClient, user: dict
) -> None:
    FakeAdapter.script["content"] = "假回复：我在。"
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.post(
        f"{BASE}/{session['id']}/messages",
        json={"content": "你在吗"},
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]

    assert data["user_message"]["role"] == "user"
    assert data["user_message"]["content"] == "你在吗"
    assert data["assistant_message"]["content"] == "假回复：我在。"
    assert data["assistant_message"]["model_name"] == "mock-model"
    assert data["usage"]["total_tokens"] == 42
    assert data["context"]["history_dropped"] == 0

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert detail["message_count"] == 3  # 开场白 + 用户 + 助手
    assert detail["total_tokens"] == 42
    assert detail["last_active_at"] is not None
    assert [m["role"] for m in detail["messages"]] == ["assistant", "user", "assistant"]


def test_send_message_propagates_adapter_notes(client: TestClient, user: dict) -> None:
    """★ 适配层为满足协议而改过参数时，必须一路透传到前端（不许静默降级）。"""
    FakeAdapter.script["notes"] = [
        "Anthropic 开启思考时不接受 temperature，已移除该参数"
    ]
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.post(
        f"{BASE}/{session['id']}/messages",
        json={"content": "你好"},
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    assert "temperature" in response.json()["data"]["notes"][0]


def test_send_message_keeps_user_message_when_model_fails(
    client: TestClient, user: dict
) -> None:
    """★ 模型调用失败时，用户打出去的话不能跟着消失。"""
    from app.llm.errors import LLMUpstreamError

    FakeAdapter.script["error"] = LLMUpstreamError("上游炸了")
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.post(
        f"{BASE}/{session['id']}/messages",
        json={"content": "这句话不能丢"},
        headers=user["headers"],
    )
    assert response.status_code == 502, response.text

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert [m["content"] for m in detail["messages"]][-1] == "这句话不能丢"
    assert detail["last_active_at"] is not None


def test_send_message_without_provider_is_400(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    session = make_session(client, user, card["id"], None)

    response = client.post(
        f"{BASE}/{session['id']}/messages",
        json={"content": "在吗"},
        headers=user["headers"],
    )
    assert response.status_code == 400, response.text
    assert "模型配置" in response.json()["message"]


def test_send_message_rejects_blank_content(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.post(
        f"{BASE}/{session['id']}/messages",
        json={"content": "   "},
        headers=user["headers"],
    )
    assert response.status_code == 422, response.text


def test_long_conversation_triggers_rolling_summary(
    client: TestClient, user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超长对话必须触发裁剪，并把被裁掉的内容压进 rolling_summary。"""
    FakeAdapter.script["content"] = "回复" * 200
    card = make_card(client, user)
    # 故意把模型上下文窗口配得很小，几轮就超预算
    provider = make_provider(
        client, user, context_window=2048, generation={"max_tokens": 512}
    )
    session = make_session(client, user, card["id"], provider["id"])

    for i in range(8):
        response = client.post(
            f"{BASE}/{session['id']}/messages",
            json={"content": f"第{i}轮：这是一段很长的话" * 20},
            headers=user["headers"],
        )
        assert response.status_code == 201, response.text

    data = response.json()["data"]
    assert data["context"]["history_dropped"] > 0, "超预算时应当丢弃最早的对话"

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert detail["rolling_summary"], "被裁掉的内容必须压进滚动摘要，否则模型会彻底失忆"


# ==================================================================
#  六、SSE 流式
# ==================================================================
def parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 报文解析成 [(event, data), ...]（前端 chat.js 用的是同一套规则）。"""
    import json

    events: list[tuple[str, dict]] = []
    name = "message"
    data_lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                events.append((name, json.loads("\n".join(data_lines))))
            name, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    if data_lines:
        events.append((name, json.loads("\n".join(data_lines))))
    return events


def test_stream_sends_deltas_then_done(client: TestClient, user: dict) -> None:
    FakeAdapter.script["stream"] = ["你", "好", "，", "旅人"]
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.get(
        f"{BASE}/{session['id']}/stream",
        params={"content": "你好"},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(response.text)
    names = [name for name, _ in events]

    assert names[0] == "meta", "第一条必须是元信息"
    assert names[-2:] == ["done", "end"]
    assert names.count("delta") == 4, "每个片段都要单独推一条（打字机效果靠它）"

    # ★ 逐字出现：把所有 delta 按顺序拼起来就是完整回复
    assert "".join(d["delta"] for n, d in events if n == "delta") == "你好，旅人"

    done = dict(events)["done"]
    assert done["usage"]["total_tokens"] == 49
    assert done["session"]["message_count"] == 3
    assert done["reasoning"] == ""

    # 落库：刷新页面后能重新看到这条回复
    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert detail["messages"][-1]["content"] == "你好，旅人"
    assert [m["role"] for m in detail["messages"]] == ["assistant", "user", "assistant"]


def test_stream_image_events_are_sent_and_persisted(
    monkeypatch: pytest.MonkeyPatch, client: TestClient, user: dict
) -> None:
    """生图走 SSE：正文流完之后先 image_start，再 image，最后 done 也带一份。"""
    created = client.post(
        "/api/v1/plugins",
        headers=user["headers"],
        json={
            "name": "流式生图",
            "kind": "imagegen",
            "enabled": True,
            "config": {
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-image-1",
                "protocol": "openai-images",
                "trigger": "auto",
                "interval": 1,
                "size": "1024x1024",
                "style": "watercolor",
                "max_images": 1,
                "timeout_seconds": 120,
                "max_chars": 1200,
                "extra_body": {},
                "api_key": "sk-stream-imagegen",
            },
        },
    )
    assert created.status_code == 201, created.text

    FakeAdapter.script["stream"] = ["雨夜。", "你推开城门。"]
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    def fake_generate(spec, api_key, prompt):
        return {
            "url": "/media/imagegen/stream-test.png",
            "prompt": prompt,
            "model": spec["model"],
        }

    monkeypatch.setattr(imagegen_service, "_generate_one", fake_generate)
    response = client.get(
        f"{BASE}/{session['id']}/stream",
        params={"content": "我推门进去"},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert "image_start" in names, names
    assert "image" in names, names
    assert names[-2:] == ["done", "end"], names

    image_event = dict(events)["image"]
    assert image_event["error"] is None
    assert image_event["images"][0]["url"] == "/media/imagegen/stream-test.png"
    done = dict(events)["done"]
    assert done["images"][0]["url"] == "/media/imagegen/stream-test.png"

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert (
        detail["messages"][-1]["images"][0]["url"] == "/media/imagegen/stream-test.png"
    )


def test_stream_reports_notes_from_adapter(client: TestClient, user: dict) -> None:
    FakeAdapter.script["stream_notes"] = [
        "OpenAI 兼容协议没有真正的关闭思考，已映射为 minimal"
    ]
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.get(
        f"{BASE}/{session['id']}/stream",
        params={"content": "你好"},
        headers=user["headers"],
    )
    events = parse_sse(response.text)
    notes = [d["notes"] for n, d in events if n == "notes"]
    assert notes and "minimal" in notes[0][0]
    # done 事件里也要带上，方便前端一次性读取。
    # ★ 用 any(...) 而不是取 [-1]：done 里的 notes 是**累积**的，
    #   后面还可能追加别的提醒（例如"本轮没有 <state> 状态块"），
    #   这条断言要守的是"适配层的说明确实带到了 done"，不是"它是最后一条"。
    done_notes = dict(events)["done"]["notes"]
    assert any("minimal" in n for n in done_notes), done_notes


def test_stream_reports_reasoning_separately(client: TestClient, user: dict) -> None:
    """推理模型的思考过程单独走 reason 事件，不与正文混在一起。"""
    original = FakeAdapter.stream_chat

    def stream_with_reasoning(self, request):
        from app.llm.schema import StreamChunk as Chunk

        yield Chunk(reasoning_delta="让我想想…")
        yield Chunk(delta="答案")
        yield Chunk(finish_reason="stop", usage=TokenUsage(total_tokens=5))

    FakeAdapter.stream_chat = stream_with_reasoning
    try:
        card = make_card(client, user)
        provider = make_provider(client, user)
        session = make_session(client, user, card["id"], provider["id"])
        response = client.get(
            f"{BASE}/{session['id']}/stream",
            params={"content": "难题"},
            headers=user["headers"],
        )
    finally:
        FakeAdapter.stream_chat = original

    events = parse_sse(response.text)
    assert [d["delta"] for n, d in events if n == "reason"] == ["让我想想…"]
    assert [d["delta"] for n, d in events if n == "delta"] == ["答案"]
    assert dict(events)["done"]["reasoning"] == "让我想想…"

    # ★ 语义在 2026-10-13 变了（用户要求把思考过程放出来，好对比不同预设）：
    #   思考过程**落库**（单独的 reasoning 列），但**绝不混进 content** ——
    #   这一点是硬的：content 是喂回下一轮上下文的东西，思考混进去就会污染模型。
    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    last = detail["messages"][-1]
    assert last["content"] == "答案", "正文里不能混进思考过程（否则会污染下一轮上下文）"
    assert last["reasoning"] == "让我想想…", (
        "思考过程要能回看：刷新之后仍然拿得到（以前只在流式那几秒显示一眼）"
    )


def test_interrupted_turn_keeps_body_and_reasoning_separately(
    client: TestClient, user: dict
) -> None:
    """被中断的一轮：正文进 content、思考进 reasoning，**各带各的标注**。

    ★ 为什么必须分开存：正文是喂回下一轮上下文的东西（只能放模型说过的话），
      思考是给人看的审计材料。混在一起会污染上下文，等价于把模型的内心独白
      塞回它自己的下一轮提示词里。
    """
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    with session_scope() as db:
        row = db.get(NarrativeSession, session["id"])
        engine.save_user_message(db, row, "我说了一句话")

    with session_scope() as db:
        saved = engine.keep_interrupted_reply(
            db,
            session["id"],
            text="【已经写出来的那半段",
            reasoning_text="【想了一半",
            model_name="fake-model",
        )
        assert saved is not None
        assert saved.content.startswith("【已经写出来的那半段")
        assert saved.content.endswith("（回复中断）")
        assert saved.reasoning.startswith("【想了一半")
        assert "中断" in saved.reasoning
        assert "【想了一半" not in saved.content, (
            "思考绝不能混进正文（会污染下一轮上下文）"
        )

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    last = detail["messages"][-1]
    assert "【已经写出来的那半段" in last["content"]
    assert "【想了一半" in (last["reasoning"] or ""), "思考要能回看"


def test_interrupt_with_only_reasoning_still_saves_something(
    client: TestClient, user: dict
) -> None:
    """★ 用户 2026-10-13 报的 bug：**推理模型先想很久、正文一个字还没吐**时切走。

    表现是"思考过程被中断没有提示标注，又变成了孤零零的用户的最后一句话"。
    根因：落库判据写的是"正文为空就不存"（`if not body: return None`），
    而推理模型恰恰经常长时间只吐思考 —— 于是正文空、整条不存，
    用户已经看到的思考也跟着一起没了。
    现在必须留一条：正文用占位、思考带自己的中断标注。
    """
    from app.db.models import NarrativeSession as _S

    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])
    with session_scope() as db:
        engine.save_user_message(db, db.get(_S, session["id"]), "难题")

    with session_scope() as db:
        saved = engine.keep_interrupted_reply(
            db, session["id"], text="", reasoning_text="我想了很久…", model_name="m"
        )
        assert saved is not None, (
            "只想着、没正文，也必须留下东西（否则用户又看到一片空白）"
        )
        assert "只来得及思考" in saved.content, "正文该是一句如实的占位说明"
        assert "（回复中断）" in saved.content
        assert saved.reasoning and "我想了很久…" in saved.reasoning
        assert "中断" in saved.reasoning

    # 正文与思考**都**空时仍然不硬造（界面会给「让 AI 写这条走向」重试入口）
    other = make_session(client, user, card["id"], provider["id"])
    with session_scope() as db:
        engine.save_user_message(db, db.get(_S, other["id"]), "再来")
    with session_scope() as db:
        assert (
            engine.keep_interrupted_reply(db, other["id"], text="", reasoning_text="")
            is None
        )


def test_store_reasoning_switch_controls_persistence(
    client: TestClient, user: dict
) -> None:
    """★ 模型配置里的「思考过程输出」开关（默认开，用户 2026-10-13 要求）。

    开 = 思考随回复落库（界面可折叠回看）；关 = 回到以前的样子
    （**只在流式时显示一眼**、不落库）。★ 关键：关掉只是不存，
    流式那条 `done` 事件里**照样要有**思考 —— 否则"之前的观感"都没了。
    """
    from app.llm.schema import StreamChunk as Chunk

    original = FakeAdapter.stream_chat

    def stream_with_reasoning(self, request):
        yield Chunk(reasoning_delta="我在想…")
        yield Chunk(delta="正文")
        yield Chunk(finish_reason="stop", usage=TokenUsage(total_tokens=7))

    FakeAdapter.stream_chat = stream_with_reasoning
    try:
        # ① 默认（不传这个字段）= 开 → 思考落库
        card = make_card(client, user)
        provider = make_provider(client, user)
        assert provider["store_reasoning"] is True, "默认必须是开"
        session = make_session(client, user, card["id"], provider["id"])
        client.get(
            f"{BASE}/{session['id']}/stream",
            params={"content": "问"},
            headers=user["headers"],
        )
        detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
            "data"
        ]
        assert detail["messages"][-1]["reasoning"] == "我在想…", "开着的时候要落库"

        # ② 关掉 → 不落库，但流式事件里照样有思考
        client.patch(
            f"{PROVIDERS}/{provider['id']}",
            json={"store_reasoning": False},
            headers=user["headers"],
        )
        card2 = make_card(client, user)
        session2 = make_session(client, user, card2["id"], provider["id"])
        response = client.get(
            f"{BASE}/{session2['id']}/stream",
            params={"content": "问"},
            headers=user["headers"],
        )
        events = parse_sse(response.text)
        assert dict(events)["done"]["reasoning"] == "我在想…", (
            "关掉只影响'存不存'，流式期间的思考照样要发给前端（那才是以前的样子）"
        )
        detail2 = client.get(
            f"{BASE}/{session2['id']}", headers=user["headers"]
        ).json()["data"]
        assert detail2["messages"][-1]["reasoning"] is None, "关掉之后不许落库"
    finally:
        FakeAdapter.stream_chat = original


def test_stream_reports_error_event_instead_of_silent_break(
    client: TestClient, user: dict
) -> None:
    """★ 流式过程中出错必须发 error 事件，不能静默断开。"""
    from app.llm.errors import LLMQuotaError

    FakeAdapter.script["stream"] = ["前", "半"]
    FakeAdapter.script["stream_error"] = LLMQuotaError("余额不足，请充值")
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.get(
        f"{BASE}/{session['id']}/stream",
        params={"content": "你好"},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    names = [n for n, _ in events]

    assert "error" in names, "必须发 error 事件"
    error = dict(events)["error"]
    assert error["code"] == "LLM_QUOTA_ERROR"
    assert "余额不足" in error["message"]
    # 已经生成的半截内容要保住（用户已经看到了）
    assert "".join(d["delta"] for n, d in events if n == "delta") == "前半"
    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert "前半" in detail["messages"][-1]["content"]
    assert "中断" in detail["messages"][-1]["content"]


def test_stream_empty_reply_yields_explicit_error(
    client: TestClient, user: dict
) -> None:
    """推理模型把输出配额全用在思考上 -> 一个字的正文都没有。

    这种情况在非流式路径由 _raise_if_no_content 拦下；
    流式路径已经发过 200 与若干片段了，只能发一条 error 事件把原因说清楚。
    """
    original = FakeAdapter.stream_chat

    def stream_only_reasoning(self, request):
        from app.llm.schema import StreamChunk as Chunk

        yield Chunk(reasoning_delta="想了很久…")
        yield Chunk(
            finish_reason="length",
            usage=TokenUsage(
                completion_tokens=512, reasoning_tokens=512, total_tokens=512
            ),
        )

    FakeAdapter.stream_chat = stream_only_reasoning
    try:
        card = make_card(client, user)
        provider = make_provider(client, user)
        session = make_session(client, user, card["id"], provider["id"])
        response = client.get(
            f"{BASE}/{session['id']}/stream",
            params={"content": "难题"},
            headers=user["headers"],
        )
    finally:
        FakeAdapter.stream_chat = original

    events = parse_sse(response.text)
    assert [n for n, _ in events][-2:] == ["error", "end"]
    error = dict(events)["error"]
    assert error["code"] == "LLM_EMPTY_REPLY"
    assert "max_tokens" in error["message"] or "最大输出" in error["message"]


def test_stream_keeps_user_message_when_provider_missing(
    client: TestClient, user: dict
) -> None:
    """流式路径下校验失败时，用户消息已经落库了（在流开始前就写好了）。"""
    card = make_card(client, user)
    session = make_session(client, user, card["id"], None)

    response = client.get(
        f"{BASE}/{session['id']}/stream",
        params={"content": "这句话也要留下"},
        headers=user["headers"],
    )
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert dict(events)["error"]["code"] == "BAD_REQUEST"

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert detail["messages"][-1]["content"] == "这句话也要留下"


# ==================================================================
#  七、会话被删时的兜底
# ==================================================================
def test_messages_of_deleted_card_still_work(client: TestClient, user: dict) -> None:
    """角色卡被删除后（外键 SET NULL），会话仍可继续对话，只是没有人设。"""
    FakeAdapter.script["content"] = "（没有人设也能聊）"
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    # 删卡但保留会话
    deleted = client.delete(
        f"{CARDS}/{card['id']}",
        params={
            "force": "true",
            "delete_sessions": "false",
            "delete_world_book": "false",
        },
        headers=user["headers"],
    )
    assert deleted.status_code == 200, deleted.text

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    assert detail["character_card"] is None
    assert detail["warnings"], "应当提醒用户人设已丢失"

    response = client.post(
        f"{BASE}/{session['id']}/messages",
        json={"content": "还在吗"},
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text


# ==================================================================
#  消息级操作：编辑 / 撤回 / 重新生成
# ==================================================================
def _seed_turns(client: TestClient, user: dict, turns: int = 2) -> dict:
    """造一个"开场白 + N 轮对话"的会话，返回会话详情与每轮消息 id。

    ★ 为什么要造多轮？
      编辑 / 撤回的语义都是"删掉这条之后的内容"，
      只有一轮对话时根本区分不出"删了后面"和"什么都没删"。
    """
    FakeAdapter.script["content"] = "假回复"
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    for index in range(turns):
        sent = client.post(
            f"{BASE}/{session['id']}/messages",
            json={"content": f"第{index + 1}句"},
            headers=user["headers"],
        )
        assert sent.status_code == 201, sent.text

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
        "data"
    ]
    return {"session": session, "detail": detail, "card": card, "provider": provider}


def _ids_by_role(detail: dict, role: str) -> list[int]:
    return [m["id"] for m in detail["messages"] if m["role"] == role]


def test_edit_user_message_deletes_everything_after_it(
    client: TestClient, user: dict
) -> None:
    """★ 编辑会连带删掉后面的全部内容 —— 这是编辑语义的核心，必须断言到。"""
    seeded = _seed_turns(client, user, turns=2)
    session_id = seeded["session"]["id"]
    user_ids = _ids_by_role(seeded["detail"], "user")
    first_user_id = user_ids[0]

    # 第一句后面：第 2 句用户消息 + 两条助手回复（共 3 条）
    response = client.patch(
        f"{BASE}/{session_id}/messages/{first_user_id}",
        json={"content": "改过的第一句"},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    assert data["message"]["content"] == "改过的第一句"
    assert data["deleted_messages"] == 3, data

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert [m["role"] for m in detail["messages"]] == ["assistant", "user"]
    assert detail["message_count"] == 2
    assert detail["messages"][-1]["content"] == "改过的第一句"


def test_edit_recalculates_session_stats(client: TestClient, user: dict) -> None:
    """删消息后统计必须跟着变，否则界面会出现"3 条消息却累计上万 token"的怪象。"""
    seeded = _seed_turns(client, user, turns=2)
    session_id = seeded["session"]["id"]
    before = seeded["detail"]["total_tokens"]
    assert before > 0

    client.patch(
        f"{BASE}/{session_id}/messages/{_ids_by_role(seeded['detail'], 'user')[0]}",
        json={"content": "短"},
        headers=user["headers"],
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]

    assert detail["message_count"] == 2
    assert detail["total_tokens"] < before
    assert detail["total_tokens"] == sum(m["token_count"] for m in detail["messages"])


def test_edit_assistant_message_is_rejected(client: TestClient, user: dict) -> None:
    """角色回复是模型产物，不允许直接改写（否则模型会以为那是自己说过的话）。"""
    seeded = _seed_turns(client, user, turns=1)
    assistant_id = _ids_by_role(seeded["detail"], "assistant")[-1]

    response = client.patch(
        f"{BASE}/{seeded['session']['id']}/messages/{assistant_id}",
        json={"content": "伪造"},
        headers=user["headers"],
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "BAD_REQUEST"


def test_edit_with_blank_content_is_422(client: TestClient, user: dict) -> None:
    seeded = _seed_turns(client, user, turns=1)
    response = client.patch(
        f"{BASE}/{seeded['session']['id']}/messages/{_ids_by_role(seeded['detail'], 'user')[0]}",
        json={"content": "   "},
        headers=user["headers"],
    )
    assert response.status_code == 422, response.text


def test_retract_removes_message_and_everything_after(
    client: TestClient, user: dict
) -> None:
    seeded = _seed_turns(client, user, turns=2)
    session_id = seeded["session"]["id"]
    mid_user_id = _ids_by_role(seeded["detail"], "user")[0]

    response = client.post(
        f"{BASE}/{session_id}/messages/{mid_user_id}/retract", headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["retracted_content"] == "第1句"
    # 自己 + 助手回复 + 第 2 句 + 助手回复
    assert data["deleted_messages"] == 4, data

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert [m["role"] for m in detail["messages"]] == ["assistant"]
    assert detail["message_count"] == 1


def test_retract_last_turn_leaves_only_greeting(client: TestClient, user: dict) -> None:
    seeded = _seed_turns(client, user, turns=1)
    session_id = seeded["session"]["id"]

    response = client.post(
        f"{BASE}/{session_id}/messages/{_ids_by_role(seeded['detail'], 'user')[-1]}/retract",
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert [m["role"] for m in detail["messages"]] == ["assistant"]
    assert detail["total_tokens"] == detail["messages"][0]["token_count"]


def test_retract_assistant_message_is_rejected(client: TestClient, user: dict) -> None:
    seeded = _seed_turns(client, user, turns=1)
    response = client.post(
        f"{BASE}/{seeded['session']['id']}/messages/{_ids_by_role(seeded['detail'], 'assistant')[-1]}/retract",
        headers=user["headers"],
    )
    assert response.status_code == 400, response.text
    assert "只能撤回" in response.json()["message"]


def test_edit_and_retract_reject_message_from_another_session(
    client: TestClient, user: dict
) -> None:
    """★ 消息 id 是全局自增的：只按主键取就等于允许操作别的会话里的消息。"""
    first = _seed_turns(client, user, turns=1)
    second = _seed_turns(client, user, turns=1)
    foreign_message_id = _ids_by_role(first["detail"], "user")[0]

    patched = client.patch(
        f"{BASE}/{second['session']['id']}/messages/{foreign_message_id}",
        json={"content": "越权改"},
        headers=user["headers"],
    )
    assert patched.status_code == 404, patched.text

    retracted = client.post(
        f"{BASE}/{second['session']['id']}/messages/{foreign_message_id}/retract",
        headers=user["headers"],
    )
    assert retracted.status_code == 404, retracted.text


def test_regenerate_replaces_last_reply_without_duplicating_user_message(
    client: TestClient, user: dict
) -> None:
    """★ 重新生成的定义性断言：回复换了，但用户消息**不会**多出一条。"""
    seeded = _seed_turns(client, user, turns=1)
    session_id = seeded["session"]["id"]
    before_user_ids = _ids_by_role(seeded["detail"], "user")

    FakeAdapter.script["stream"] = ["重", "写", "版"]
    with client.stream(
        "GET", f"{BASE}/{session_id}/regenerate", headers=user["headers"]
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: done" in body
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert _ids_by_role(detail, "user") == before_user_ids, "用户消息不该被复制或改动"
    assert detail["messages"][-1]["content"] == "重写版"
    assert [m["role"] for m in detail["messages"]] == ["assistant", "user", "assistant"]


def test_regenerate_middle_reply_drops_later_messages(
    client: TestClient, user: dict
) -> None:
    """对中间那条回复重新生成时，它之后的内容会一起被删掉（否则前后矛盾）。"""
    seeded = _seed_turns(client, user, turns=2)
    session_id = seeded["session"]["id"]
    first_reply_id = _ids_by_role(seeded["detail"], "assistant")[1]  # 第 1 条回复

    FakeAdapter.script["stream"] = ["换", "一", "版"]
    with client.stream(
        "GET",
        f"{BASE}/{session_id}/regenerate",
        headers=user["headers"],
        params={"message_id": first_reply_id},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "event: done" in body

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert [m["role"] for m in detail["messages"]] == ["assistant", "user", "assistant"]
    assert detail["messages"][-1]["content"] == "换一版"


def test_regenerate_prompt_anchors_on_previous_user_message(
    client: TestClient, user: dict
) -> None:
    """★ 回归：重新生成中间那条回复时，**提问锚点**必须是它前面那条用户消息。

    老实现里只有一个 id 变量，同时充当"要删的那条回复"和"本次提问内容"：
    于是模型收到的是**角色自己说过的话**（而且那条刚从库里删掉了），
    真实表现是「点重新生成没反应，红条一闪就没了」。
    这里直接看发出去的提示词最后一条是谁。
    """
    seeded = _seed_turns(client, user, turns=2)
    session_id = seeded["session"]["id"]
    detail = seeded["detail"]
    reply_id = _ids_by_role(detail, "assistant")[1]
    user_text = [m["content"] for m in detail["messages"] if m["role"] == "user"][0]

    reply_text = [m["content"] for m in detail["messages"] if m["role"] == "assistant"][
        1
    ]

    FakeAdapter.script["stream"] = ["换", "一", "版"]
    with client.stream(
        "GET",
        f"{BASE}/{session_id}/regenerate",
        headers=user["headers"],
        params={"message_id": reply_id},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert "event: done" in body
    assert "event: error" not in body, "重新生成不该以错误收场"
    request = last_request()
    # 尾注（post_history_instructions）也是 user 角色，但它带着"不是用户发言"的前缀
    user_turns = [
        m
        for m in request.messages
        if m.role == "user" and not m.content.startswith("[系统指令")
    ]
    assert user_turns[-1].content == user_text, "提问锚点必须是那条用户消息"
    assert all(m.content != reply_text for m in user_turns), (
        "被替换掉的那条角色回复不应该出现在提示词里（更不该冒充用户发言）"
    )


def test_regenerate_rejects_user_message_id(client: TestClient, user: dict) -> None:
    """重新生成只能针对角色回复；指到用户消息上要明确报错，而不是默默乱删。"""
    seeded = _seed_turns(client, user, turns=1)
    response = client.get(
        f"{BASE}/{seeded['session']['id']}/regenerate",
        headers=user["headers"],
        params={"message_id": _ids_by_role(seeded["detail"], "user")[0]},
    )
    assert response.status_code == 400, response.text
    assert "重新生成" in response.json()["message"]


def test_regenerate_without_any_user_message_is_400(
    client: TestClient, user: dict
) -> None:
    """只有开场白时没有可重新生成的内容 —— 要返回 400，不能让前端白等一条流。"""
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.get(f"{BASE}/{session['id']}/regenerate", headers=user["headers"])
    assert response.status_code == 400, response.text


def test_message_actions_require_login(client: TestClient, user: dict) -> None:
    seeded = _seed_turns(client, user, turns=1)
    session_id = seeded["session"]["id"]
    user_message_id = _ids_by_role(seeded["detail"], "user")[0]

    assert (
        client.patch(
            f"{BASE}/{session_id}/messages/{user_message_id}", json={"content": "x"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"{BASE}/{session_id}/messages/{user_message_id}/retract"
        ).status_code
        == 401
    )
    assert client.get(f"{BASE}/{session_id}/regenerate").status_code == 401


# ==================================================================
#  九、★ 用户切走了也不能丢：**断开 ≠ 取消**（2026-10-13 用户反馈的故障）
#
#  故障原话：
#      "当用户输入后，在 AI 输出的过程中切换到其他角色卡或者世界线会直接打断 ai 的
#       生成，再回来就会发现用户输入孤零零的摆在那里……上游一定已经收到这个请求并且
#       输出了，但是我的系统却没有返回到用户的对话框里，只有再消耗 token 点
#       「让 AI 写这个走向」。"
#
#  根因：旧代码里取消**多半落在 `await run_in_threadpool(...)` 那一行**
#       （模型正在思考），而那一行不在包住 yield 的 try 里 —— 生成器压根没被关闭，
#       于是"保留半截内容"的收尾不会跑，这一轮彻底丢失。
# ==================================================================
def test_interrupted_turn_keeps_what_the_user_already_saw(
    client: TestClient, user: dict
) -> None:
    """★ 已经推送给用户的那段字必须落库，并标注「（回复中断）」——且**幂等**。

    这是用户 2026-10-13 反馈的直接落点：
      "……再回来就会发现用户输入孤零零的摆在那里……一定要告诉用户被中断，
        而不是像我刚刚那样什么都没有。"
    接口层用手里的"已经发出去的字"自己存（`keep_interrupted_reply`），
    不再依赖生成器的收尾 —— 那条路要跨"请求任务 / 线程池 / GC"碰同一个 Session，
    真实断线复现里撞出了 ObjectDereferencedError，字照样丢。

    ★ 幂等这一条同样关键：生成器那边的 `_keep_partial` 也可能跑到，
      两条路都写就会变成"同一句话两条回复"，模型下一轮会以为自己自言自语。
    """
    from app.db.models import Message, NarrativeSession as _S

    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    with session_scope() as db:
        row = db.get(_S, session["id"])
        engine.save_user_message(db, row, "我说了一句话")

    with session_scope() as db:
        saved = engine.keep_interrupted_reply(
            db, session["id"], text="【已经写出来的那半段", model_name="fake-model"
        )
        assert saved is not None, "有内容就该存下来"
        assert saved.content.endswith("（回复中断）"), "必须标注它是被中断的"
        assert saved.content.startswith("【已经写出来的那半段"), (
            "存入的必须是用户看到的那段"
        )
        assert saved.role == "assistant"

    # 幂等：再存一次不应该多出第二条
    with session_scope() as db:
        again = engine.keep_interrupted_reply(
            db, session["id"], text="【已经写出来的那半段", model_name="fake-model"
        )
        assert again is None, "同一句话后面已经有回复了，不能再写第二条"
        assert (
            db.query(Message).filter(Message.session_id == session["id"]).count() == 3
        )

    # 一个字都没有时不该硬造空回复（界面会给重试入口）
    other = make_session(client, user, card["id"], provider["id"])
    with session_scope() as db:
        row2 = db.get(_S, other["id"])
        engine.save_user_message(db, row2, "另一句话")
    with session_scope() as db:
        assert engine.keep_interrupted_reply(db, other["id"], text="   ") is None


def test_send_is_rejected_while_previous_turn_is_still_writing(
    client: TestClient, user: dict
) -> None:
    """上一轮还在后台写时，再发消息要被**明确拒绝**（409），且不留下用户消息。

    ★ 为什么必须拒绝：两轮同时写同一个会话会互相插队，历史里会出现
      "两条回复针对同一句话"，模型下一轮会以为自己自言自语。
    ★ 为什么"不留下用户消息"也要断言：注定被拒的请求不该先在库里留一句话，
      否则用户会看到"我说了但它永远没回"。
    """
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    rs = streaming.register(session["id"], "测试")
    try:
        response = client.get(
            f"{BASE}/{session['id']}/stream",
            params={"content": "再说一句"},
            headers=user["headers"],
        )
        assert response.status_code == 409, response.text
        assert "还在写" in response.json()["message"]

        detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()[
            "data"
        ]
        assert [m["role"] for m in detail["messages"]] == ["assistant"], (
            "被拒绝的请求不该把用户那句话写进库"
        )

        # 重新生成同理
        regen = client.get(
            f"{BASE}/{session['id']}/regenerate", headers=user["headers"]
        )
        assert regen.status_code == 409, regen.text
    finally:
        streaming.unregister(rs)


pytestmark = pytest.mark.integration
