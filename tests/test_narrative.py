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
from sqlalchemy import select

from app.db.models import Message, NarrativeSession, User
from app.db.mysql import session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatMessage, ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import context_manager, engine, world_book_scanner
from app.narrative.prompt_builder import build_prompt

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
        "generation": {"temperature": 0.8, "max_tokens": 1024, "reasoning_effort": "auto"},
    }
    body.update(overrides)
    response = client.post(PROVIDERS, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_session(client: TestClient, user: dict, card_id: int, provider_id: int, **kw) -> dict:
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
        max_out = getattr(self.row, "max_tokens", None) or self.default_params.max_tokens
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
#  一、提示词构建
# ==================================================================
class FakeCard:
    """鸭子类型的角色卡（不建库，纯单元测试用）。"""

    def __init__(self, **kw: Any) -> None:
        self.name = kw.get("name", "测试角色")
        self.description = kw.get("description")
        self.personality = kw.get("personality")
        self.background = kw.get("background")
        self.speaking_style = kw.get("speaking_style")
        self.scenario = kw.get("scenario")
        self.example_dialogue = kw.get("example_dialogue")
        self.greeting = kw.get("greeting")
        self.system_prompt = kw.get("system_prompt")
        self.post_history_instructions = kw.get("post_history_instructions")


class FakeBook:
    def __init__(self, entries, name="测试世界"):
        self.name = name
        self.entries = entries


def test_prompt_assembles_persona_fields() -> None:
    card = FakeCard(
        name="雅佩",
        personality="冷静",
        background="北方来的旅人",
        speaking_style="简短",
        scenario="深夜的旅店",
        example_dialogue="用户：你好\n雅佩：……嗯。",
    )
    plan = build_prompt(card=card, history=[])

    assert plan.system_prompt_source == "assembled"
    for fragment in ("雅佩", "冷静", "北方来的旅人", "简短", "深夜的旅店", "对话示例"):
        assert fragment in plan.system_prompt


def test_prompt_prefers_card_system_prompt() -> None:
    """角色卡自带 system_prompt 时必须优先用它（作者的写法最权威）。"""
    card = FakeCard(
        name="雅佩",
        personality="这一段不该出现",
        system_prompt="你是一个只会说反话的角色。",
    )
    plan = build_prompt(card=card, history=[])

    assert plan.system_prompt_source == "card"
    assert plan.system_prompt.startswith("你是一个只会说反话的角色。")
    assert "这一段不该出现" not in plan.system_prompt


def test_prompt_injects_world_book_entries() -> None:
    card = FakeCard(name="雅佩")
    book = FakeBook(
        [
            {"keys": ["龙"], "content": "世上最后一条龙已死去三百年", "enabled": True},
            {"keys": ["禁"], "content": "这段被禁用了", "enabled": False},
        ]
    )
    plan = build_prompt(card=card, history=[], world_book=book)

    assert plan.world_book_entries == 1
    assert "世上最后一条龙已死去三百年" in plan.system_prompt
    assert "这段被禁用了" not in plan.system_prompt


def test_prompt_puts_greeting_as_first_assistant_message() -> None:
    card = FakeCard(name="雅佩")

    class Row:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    history = [Row("assistant", "……你来了。"), Row("user", "嗯。")]
    plan = build_prompt(card=card, history=history)

    roles = [m.role for m in plan.messages]
    assert roles[0] == "system"
    assert roles[1] == "assistant", "开场白必须是第一条 assistant 消息"
    assert plan.messages[1].content == "……你来了。"
    # 历史顺序原样保留：assistant（开场白）→ user（用户刚说的话）
    assert roles == ["system", "assistant", "user"]


def test_post_history_instructions_are_appended_last() -> None:
    card = FakeCard(name="雅佩", post_history_instructions="回复请控制在三句话以内。")
    plan = build_prompt(card=card, history=[])

    assert plan.has_post_history_instructions is True
    last = plan.messages[-1]
    assert last.role == "user"
    # ★ 必须标明"这不是用户说的话"，否则模型会把它当成用户的要求
    assert last.content.startswith("[系统指令")
    assert "三句话" in last.content


def test_prompt_without_card_warns_but_still_builds() -> None:
    """角色卡被删除后（外键 SET NULL）会话仍要能继续用。"""
    plan = build_prompt(card=None, history=[])

    assert plan.system_prompt_source == "none"
    assert plan.warnings, "应当提醒用户人设没有注入"
    assert plan.messages == []


# ==================================================================
#  二、上下文裁剪
# ==================================================================
def test_token_estimator_counts_cjk_heavier_than_latin() -> None:
    assert context_manager.estimate_tokens("你好") == 2
    # 4 个英文字符 ≈ 1 token
    assert context_manager.estimate_tokens("abcd") == 1
    assert context_manager.estimate_tokens("") == 0


def test_context_drops_earliest_messages_when_over_budget() -> None:
    messages = [ChatMessage.system("人设")]
    for i in range(40):
        messages.append(ChatMessage.user(f"第{i}轮用户说的话" * 5))
        messages.append(ChatMessage.assistant(f"第{i}轮角色的回复" * 5))

    budget = compute_context_budget(context_window=1200, max_output_tokens=400)
    plan = context_manager.prepare_context(messages, budget=budget)

    assert plan.history_dropped > 0, "超预算时必须丢掉最早的对话"
    assert plan.history_kept > 0, "最近的对话必须保留"
    assert plan.estimated_tokens <= budget.input_budget
    # 丢掉的一定是最早的那些（保留的最后一条应当是最新的那条）
    assert plan.messages[-1].content == messages[-1].content
    # 系统提示词永远保留
    assert plan.messages[0].is_system


def test_context_within_budget_keeps_everything() -> None:
    messages = [ChatMessage.system("人设"), ChatMessage.user("你好"), ChatMessage.assistant("嗯")]
    budget = compute_context_budget(context_window=8192, max_output_tokens=1024)
    plan = context_manager.prepare_context(messages, budget=budget)

    assert plan.history_dropped == 0
    assert plan.was_trimmed is False
    assert len(plan.messages) == 3


def test_context_injects_rolling_summary() -> None:
    messages = [ChatMessage.system("人设"), ChatMessage.user("继续说")]
    budget = compute_context_budget(context_window=8192, max_output_tokens=1024)
    plan = context_manager.prepare_context(
        messages, budget=budget, summary="用户与角色在旅店里聊了天气。"
    )

    summary_messages = [m for m in plan.messages if "前情提要" in m.content]
    assert len(summary_messages) == 1
    assert "旅店里聊了天气" in summary_messages[0].content
    assert plan.summary_used > 0


def test_context_truncates_oversized_system_prompt() -> None:
    """世界书塞了 5 万字时，系统提示词必须被截断并**如实标记**。"""
    messages = [ChatMessage.system("很长的设定" * 4000), ChatMessage.user("你好")]
    budget = compute_context_budget(context_window=2048, max_output_tokens=512)
    plan = context_manager.prepare_context(messages, budget=budget)

    assert plan.system_truncated is True
    assert plan.was_trimmed is True
    assert "设定过长已截断" in plan.messages[0].content


def test_summarize_dropped_keeps_recent_and_caps_length() -> None:
    rows = [
        type("Row", (), {"role": "user", "content": f"第{i}句" * 100, "id": i})()
        for i in range(30)
    ]
    summary = context_manager.summarize_dropped("之前的前情", rows)

    assert "之前的前情" in summary
    assert "第29句" in summary  # 越靠后的内容越重要，不能被截掉
    assert "中段前情已省略" in summary
    assert len(summary) <= context_manager.SUMMARY_MAX_CHARS + 32


# ==================================================================
#  二之二、世界书关键词触发（3.9）
# ==================================================================
def _book(entries, extra=None):
    book = FakeBook(entries)
    book.extra_data = extra or {}
    return book


def _rows(*texts):
    return [
        type("Row", (), {"role": "user", "content": text, "id": i})()
        for i, text in enumerate(texts)
    ]


def test_scanner_only_returns_entries_hit_by_keywords() -> None:
    book = _book(
        [
            {"keys": ["龙"], "content": "世上最后一条龙已死去", "enabled": True},
            {"keys": ["精灵"], "content": "精灵住在森林深处", "enabled": True},
        ]
    )
    result = world_book_scanner.scan(book, _rows("这里有龙吗", "精灵是谁"))

    contents = [e["content"] for e in result.entries]
    assert "世上最后一条龙已死去" in contents
    assert "精灵住在森林深处" in contents
    assert result.matched == 2
    assert result.scanned_messages == 2


def test_scanner_ignores_disabled_and_keyless_entries() -> None:
    book = _book(
        [
            {"keys": ["龙"], "content": "禁用的龙设定", "enabled": False},
            {"keys": [], "content": "没有关键词的条目"},
            {"keys": [""], "content": "空白关键词"},
            {"keys": ["龙"], "content": "启用的龙设定"},
        ]
    )
    result = world_book_scanner.scan(book, _rows("龙"))

    assert [e["content"] for e in result.entries] == ["启用的龙设定"]


def test_scanner_respects_scan_depth() -> None:
    """★ scan_depth 的意义：很久以前提到的关键词不该再触发注入。"""
    book = _book([{"keys": ["龙"], "content": "龙的设定"}])
    rows = _rows("很久以前提到过龙", "第二句", "第三句")

    fresh = world_book_scanner.scan(book, rows, scan_depth=2)
    full = world_book_scanner.scan(book, rows, scan_depth=0)

    assert full.matched == 1, "全部扫描时应当命中"
    assert fresh.matched == 0, "只扫最近 2 条时不该命中（龙在更早那条里）"
    assert fresh.scanned_messages == 2


def test_scanner_respects_token_budget_but_keeps_first_entry() -> None:
    book = _book(
        [
            {"keys": ["龙"], "content": "第一条设定" * 10, "insertion_order": 0},
            {"keys": ["龙"], "content": "第二条设定" * 10, "insertion_order": 1},
            {"keys": ["龙"], "content": "第三条设定" * 10, "insertion_order": 2},
        ]
    )
    result = world_book_scanner.scan(book, _rows("龙"), token_budget=60)

    assert len(result.entries) == 1, "预算只够放第一条"
    assert result.dropped == 2, "被丢掉的条数必须如实统计"
    assert result.matched == 3


def test_scanner_sorts_by_insertion_order_and_case_insensitive() -> None:
    book = _book(
        [
            {"keys": ["dragon"], "content": "后插入的", "insertion_order": 5},
            {"keys": ["Dragon"], "content": "先插入的", "insertion_order": 1},
        ]
    )
    result = world_book_scanner.scan(book, _rows("A DRAGON appears"))

    assert [e["content"] for e in result.entries] == ["先插入的", "后插入的"]


def test_scanner_settings_fall_back_to_defaults() -> None:
    """★ 用户卡里的值什么写法都有，不能因为 "8" 或 -1 就把整轮对话搞挂。"""
    assert world_book_scanner.resolve_settings(_book([], {"scan_depth": 3, "token_budget": 50})) == (3, 50)
    assert world_book_scanner.resolve_settings(_book([], {"scan_depth": "6"})) == (6, world_book_scanner.DEFAULT_TOKEN_BUDGET)
    assert world_book_scanner.resolve_settings(_book([], {"scan_depth": -1})) == (
        world_book_scanner.DEFAULT_SCAN_DEPTH,
        world_book_scanner.DEFAULT_TOKEN_BUDGET,
    )
    assert world_book_scanner.resolve_settings(_book([], {"scan_depth": "abc"})) == (
        world_book_scanner.DEFAULT_SCAN_DEPTH,
        world_book_scanner.DEFAULT_TOKEN_BUDGET,
    )
    assert world_book_scanner.resolve_settings(None) == (
        world_book_scanner.DEFAULT_SCAN_DEPTH,
        world_book_scanner.DEFAULT_TOKEN_BUDGET,
    )


def test_prompt_injects_only_scanned_entries() -> None:
    card = FakeCard(name="雅佩")
    book = FakeBook(
        [
            {"keys": ["龙"], "content": "龙已死去三百年", "enabled": True},
            {"keys": ["精灵"], "content": "精灵住在森林", "enabled": True},
        ]
    )
    hit = world_book_scanner.scan(book, _rows("这条消息提到龙")).entries
    plan = build_prompt(card=card, history=[], world_book=book, world_book_entries=hit)

    assert plan.world_book_entries == 1
    assert "龙已死去三百年" in plan.system_prompt
    assert "精灵住在森林" not in plan.system_prompt


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


def test_create_session_without_greeting_has_no_messages(
    client: TestClient, user: dict
) -> None:
    card = make_card(client, user, greeting=None)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    assert session["message_count"] == 0
    assert session["messages"] == []


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
        json={"character_card_id": card["id"], "llm_provider_id": provider["id"], "greeting_index": 5},
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


def test_list_sessions_can_filter_archived(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])
    client.patch(f"{BASE}/{session['id']}", json={"status": "archived"}, headers=user["headers"])

    active = client.get(BASE, params={"status": "active"}, headers=user["headers"]).json()["data"]
    archived = client.get(BASE, params={"status": "archived"}, headers=user["headers"]).json()["data"]
    assert session["id"] not in [i["id"] for i in active["items"]]
    assert session["id"] in [i["id"] for i in archived["items"]]


def test_patch_session_renames(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.patch(
        f"{BASE}/{session['id']}", json={"title": "改过的标题"}, headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["title"] == "改过的标题"


def test_patch_session_title_null_keeps_original(client: TestClient, user: dict) -> None:
    """title 是必填字段，传 null 表示"不修改"（与角色卡/世界书一致）。"""
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"], title="原标题")

    response = client.patch(
        f"{BASE}/{session['id']}", json={"title": None}, headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["title"] == "原标题"


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

    assert client.get(f"{BASE}/{session['id']}", headers=user["headers"]).status_code == 404


def test_session_detail_can_skip_prompt_preview(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    detail = client.get(
        f"{BASE}/{session['id']}", params={"with_prompt": "false"}, headers=user["headers"]
    ).json()["data"]
    assert detail["prompt"] is None

    with_prompt = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
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
    client: TestClient, user: dict, other_user: dict, method: str, suffix: str, kwargs: dict
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

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
    assert detail["message_count"] == 3  # 开场白 + 用户 + 助手
    assert detail["total_tokens"] == 42
    assert detail["last_active_at"] is not None
    assert [m["role"] for m in detail["messages"]] == ["assistant", "user", "assistant"]


def test_send_message_passes_history_and_world_book_to_model(
    client: TestClient, user: dict
) -> None:
    """★ 关键断言：模型最终收到的提示词里，人设、世界书、历史一个都不能少。"""
    book = client.post(
        "/api/v1/world-books",
        json={
            "name": f"书_{uuid.uuid4().hex[:6]}",
            "entries": [{"keys": ["龙"], "content": "世上最后一条龙已死去", "enabled": True}],
        },
        headers=user["headers"],
    ).json()["data"]
    card = make_card(client, user, world_book_id=book["id"])
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    client.post(
        f"{BASE}/{session['id']}/messages",
        json={"content": "这里有龙吗"},
        headers=user["headers"],
    )

    request = last_request()
    system = [m for m in request.messages if m.is_system]
    assert system, "必须发系统提示词"
    assert "世上最后一条龙已死去" in system[0].content
    assert "冷静、话少" in system[0].content
    assert [m.role for m in request.messages] == ["system", "assistant", "user", "user"]
    assert request.messages[1].content == "……你来了。"
    assert request.messages[2].content == "这里有龙吗"
    # 最后一条是尾注（post_history_instructions），且标明"不是用户说的话"
    assert request.messages[3].content.startswith("[系统指令")
    assert "三句话" in request.messages[3].content


def test_send_message_propagates_adapter_notes(client: TestClient, user: dict) -> None:
    """★ 适配层为满足协议而改过参数时，必须一路透传到前端（不许静默降级）。"""
    FakeAdapter.script["notes"] = ["Anthropic 开启思考时不接受 temperature，已移除该参数"]
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

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
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

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
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
            name = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
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
    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
    assert detail["messages"][-1]["content"] == "你好，旅人"
    assert [m["role"] for m in detail["messages"]] == ["assistant", "user", "assistant"]


def test_stream_reports_notes_from_adapter(client: TestClient, user: dict) -> None:
    FakeAdapter.script["stream_notes"] = ["OpenAI 兼容协议没有真正的关闭思考，已映射为 minimal"]
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
    # 思考过程不落库（只落正文），否则会污染下一轮的上下文
    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
    assert detail["messages"][-1]["content"] == "答案"


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
    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
    assert "前半" in detail["messages"][-1]["content"]
    assert "中断" in detail["messages"][-1]["content"]


def test_stream_empty_reply_yields_explicit_error(client: TestClient, user: dict) -> None:
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
            usage=TokenUsage(completion_tokens=512, reasoning_tokens=512, total_tokens=512),
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


def test_stream_requires_login(client: TestClient) -> None:
    assert client.get(f"{BASE}/1/stream", params={"content": "hi"}).status_code == 401


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

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
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
        params={"force": "true", "delete_sessions": "false", "delete_world_book": "false"},
        headers=user["headers"],
    )
    assert deleted.status_code == 200, deleted.text

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
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

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
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

    reply_text = [m["content"] for m in detail["messages"] if m["role"] == "assistant"][1]

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

    assert client.patch(
        f"{BASE}/{session_id}/messages/{user_message_id}", json={"content": "x"}
    ).status_code == 401
    assert client.post(
        f"{BASE}/{session_id}/messages/{user_message_id}/retract"
    ).status_code == 401
    assert client.get(f"{BASE}/{session_id}/regenerate").status_code == 401
