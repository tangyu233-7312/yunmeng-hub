"""结构化状态（HP / 背包 / 位置 / 任务）的解析、校验、落库与回注。

分层与 test_memory.py 一致：
  · 纯函数部分直接调 `app.narrative.state`（不碰库，跑得快）；
  · 端到端部分用假适配器跑一轮真实接口，验证"剥块 → 落库 → 下一轮回注"整条链。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Message, User
from app.db.mysql import session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative import state as state_mod
from app.narrative import state_schema as schemas
from app.narrative import world_book_scanner

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


class _Session:
    """鸭子类型的会话行（纯函数测试用）。"""

    def __init__(
        self,
        state_json: str | None = None,
        sid: int = 1,
        schema: dict | None = None,
    ) -> None:
        self.id = sid
        self.state_json = state_json
        #: 落库的状态栏格式（None = 迁移前的旧会话，走兼容分支）
        self.state_schema_json = (
            json.dumps(schema, ensure_ascii=False) if schema is not None else None
        )


class FakeAdapter:
    """不联网的假适配器（记录收到的请求，供断言提示词用）。"""

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
        "username": f"st_{token}",
        "email": f"st_{token}@example.com",
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
        "headers": {"Authorization": f"Bearer {logged_in.json()['data']['access_token']}"},
    }


@pytest.fixture
def user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == data["username"]))
        if row is not None:
            db.delete(row)


def _session_with(client: TestClient, user: dict, card: dict | None = None) -> int:
    if card is None:
        # ★ 不声明状态栏格式的卡会得到**空 schema**（状态栏显示"未定义"、不注入协议），
        #   所以这条链路测试的卡必须自己声明 —— 字段清单是卡的，不是代码的。
        #   `extensions` 只在 V2 导入格式里有位置，所以走 import 接口建这张卡。
        created = client.post(
            f"{CARDS}/import",
            json={
                "card": {
                    "spec": "chara_card_v2",
                    "data": {
                        "name": f"状态卡_{uuid.uuid4().hex[:6]}",
                        "first_mes": "……你来了。",
                        "extensions": {"hne": {"state_schema": LEGACY_SCHEMA["fields"]}},
                    },
                }
            },
            headers=user["headers"],
        )
        assert created.status_code == 201, created.text
        card_id = created.json()["data"]["id"]
    else:
        # 走**真正的导入接口**：初始状态那两条路（extensions.hne / 开场白里的状态块）
        # 都必须在"导入 → 建会话"这条真实链路上成立，不能只在单元层面成立。
        imported = client.post(
            f"{CARDS}/import", json={"card": card}, headers=user["headers"]
        )
        assert imported.status_code == 201, imported.text
        card_id = imported.json()["data"]["id"]
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"st_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-state-test",
            "model_name": "mock-model",
            "context_window": 8192,
            "generation": {"temperature": 0.8, "max_tokens": 1024},
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    session = client.post(
        BASE,
        json={
            "character_card_id": card_id,
            "llm_provider_id": provider.json()["data"]["id"],
        },
        headers=user["headers"],
    )
    assert session.status_code == 201, session.text
    return session.json()["data"]["id"]


# ==================================================================
#  一、解析（纯函数）
# ==================================================================
def test_extract_takes_last_block_and_strips_all() -> None:
    """模型偶尔先复述旧状态再给新的：必须取**最后**一块，且正文里不留 JSON。"""
    content = (
        "她走进钟楼。\n"
        '<state>{"hp": {"current": 10, "max": 100}}</state>\n'
        "钟声停了。\n"
        '<state>{"hp": {"current": 90, "max": 100}}</state>'
    )
    cleaned, raw = state_mod.extract_state_block(content)
    assert "<state>" not in cleaned and "</state>" not in cleaned
    assert "她走进钟楼。" in cleaned and "钟声停了。" in cleaned
    assert raw == '{"hp": {"current": 90, "max": 100}}'


def test_extract_without_block_is_noop() -> None:
    cleaned, raw = state_mod.extract_state_block("只有正文")
    assert cleaned == "只有正文" and raw is None


# ==================================================================
#  二、校验与合并
# ==================================================================
#: 测试用 schema（="这张卡声明了哪些状态字段"）。
#  ★ 字段清单不再由 state.py 写死，所以这里必须显式给一份 ——
#    这正是本次改动的目的：**没声明的字段不该凭空出现**。
LEGACY_SCHEMA = schemas.legacy_schema()


def test_normalize_clamps_hp_and_keeps_previous_max() -> None:
    state, notes = state_mod.normalize(
        {"hp": {"current": 150, "max": 100}}, {}, LEGACY_SCHEMA
    )
    assert state["hp"] == 100 and state["max"] == 100
    assert any("夹到" in note for note in notes)

    # 上限一轮内翻倍 → 不接受，保留上一轮，并如实提醒
    state2, notes2 = state_mod.normalize(
        {"hp": {"current": 60, "max": 300}}, {"hp": 50, "max": 100}, LEGACY_SCHEMA
    )
    assert state2["max"] == 100
    assert any("上限" in note for note in notes2)


def test_normalize_merges_missing_fields_from_previous() -> None:
    """模型漏写字段时沿用旧值 —— 状态不能被"少写一个键"抹掉。"""
    previous = {
        "hp": 30,
        "max": 100,
        "inventory": ["钥匙"],
        "location": "钟楼",
        "quests": [{"title": "找猫", "status": "active"}],
    }
    state, _ = state_mod.normalize({"location": "地下室"}, previous, LEGACY_SCHEMA)
    assert state["location"] == "地下室"
    assert state["hp"] == 30 and state["max"] == 100
    assert state["inventory"] == ["钥匙"]
    assert state["quests"] == previous["quests"]


def test_normalize_rejects_dirty_values() -> None:
    state, notes = state_mod.normalize(
        {
            "inventory": ["钥匙", 3, "钥匙", ""],
            "quests": [{"title": "找猫", "status": "莫名其妙"}, "顺手修钟"],
            "flags": {"见过神父": True},
            "hp": "一百",
        },
        {},
        LEGACY_SCHEMA,
    )
    assert state["inventory"] == ["钥匙"]  # 数字与空串被丢掉，重复项去重
    assert state["quests"] == [
        {"title": "找猫", "status": "active"},  # 非法 status 归一到 active
        {"title": "顺手修钟", "status": "active"},
    ]
    assert state["flags"] == {"见过神父": True}
    assert any("hp 不是数字" in note for note in notes)


def test_normalize_ignores_fields_the_card_did_not_declare() -> None:
    """★ 没声明的字段一律忽略（并如实提醒）—— 模型自己加的字段不该悄悄落库。"""
    schema = schemas.parse_schema(
        [{"name": "魔力", "label": "魔力", "type": "number"}]
    )[0]
    state, notes = state_mod.normalize(
        {"魔力": 80, "hp": 100, "max": 100, "location": "教室"}, {}, schema
    )
    assert state == {"魔力": 80}, "只有卡里声明过的字段才该被存下来"
    assert any("没有定义" in note for note in notes)


def test_normalize_without_schema_does_nothing() -> None:
    """空 schema（这张卡没定义状态栏）→ 不解析任何字段。"""
    state, _notes = state_mod.normalize({"hp": 10, "max": 100}, {}, {"spec": "x", "fields": []})
    assert state == {}


def test_ensure_shape_follows_the_schema() -> None:
    """骨架由 schema 决定（老实现把 HP 骨架写死在弹窗里）。"""
    schema = schemas.parse_schema(
        [
            {"name": "魔力", "type": "meter"},
            {"name": "携带物", "type": "list"},
            {"name": "变身", "type": "text"},
        ]
    )[0]
    shape = state_mod.ensure_shape({"魔力": 30}, schema)
    assert shape == {"魔力": 30, "max": 100, "携带物": [], "变身": ""}


def test_apply_reply_degrades_on_broken_json() -> None:
    """坏 JSON 不能抛异常：沿用上一轮 + 告诉用户。"""
    session = _Session('{"hp": 42, "max": 100}', schema=LEGACY_SCHEMA)
    cleaned, notes = state_mod.apply_reply(session, "正文\n<state>{不是 JSON}</state>")
    assert cleaned == "正文"
    assert session.state_json == '{"hp": 42, "max": 100}'  # 原样保留
    assert notes and "不是合法 JSON" in notes[0]


def test_render_for_prompt_states_protocol() -> None:
    session = _Session('{"location": "钟楼"}', schema=LEGACY_SCHEMA)
    block = state_mod.render_for_prompt(session)
    assert state_mod.BLOCK_TITLE in block
    assert '"钟楼"' in block
    # ★ 协议拆成两段：事实（render_for_prompt，排在守卫之前）
    #   + 输出契约（render_contract，排在整条提示词最末，见下面的位置断言）。
    #   模型必须能同时看到"当前状态是什么"和"<state> 这个标记怎么写"。
    assert state_mod.STATE_OPEN in state_mod.render_contract(LEGACY_SCHEMA)
    # 没有状态时也要渲染协议，否则模型永远不会开始输出状态块
    assert state_mod.BLOCK_TITLE in state_mod.render_for_prompt(_Session(None, schema=LEGACY_SCHEMA))


def test_render_for_prompt_is_empty_without_a_declared_schema() -> None:
    """★ 这张卡没定义状态栏、也没有任何状态 → 不注入任何协议。

    老实现无论什么卡都塞 HP 那套，于是没声明 HP 的卡（魔法少女 / 魔女裁判）
    也长出 `HP 100/100` —— 那是用户明确指出的设计错误。
    """
    assert state_mod.render_for_prompt(_Session(None)) == ""
    assert state_mod.render_for_prompt(_Session(None, schema={"spec": "x", "fields": []})) == ""


def test_legacy_session_without_schema_still_renders_old_fields() -> None:
    """迁移前的旧会话（没有 schema 记录，但已有状态）走兼容分支，状态栏不会变空白。"""
    session = _Session('{"hp": 100, "max": 100}')
    block = state_mod.render_for_prompt(session)
    assert state_mod.BLOCK_TITLE in block, "有状态的旧会话仍要能看到当前状态"
    assert state_mod.effective_schema(session).get("legacy") is True


# ==================================================================
#  三、端到端：剥块 → 落库 → 下一轮回注
# ==================================================================
def test_reply_state_is_stored_stripped_and_reinjected(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _session_with(client, user)
    fake_llm.script["content"] = (
        "我推开门，风灌了进来。\n"
        '<state>{"hp": {"current": 80, "max": 100}, "inventory": ["铜钥匙"],'
        ' "location": "钟楼", "quests": [{"title": "找猫", "status": "active"}]}</state>'
    )
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我上楼了"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    assistant = sent.json()["data"]["assistant_message"]
    stored = assistant["content"]
    assert "<state>" not in stored, "落库的正文里不能留原始 JSON"
    assert stored.startswith("我推开门")

    # ★ 第十六轮：原始块单独留一份（界面「看作者原格式」用）——
    #   卡作者写的复杂/美化排版在按 schema 解析成字段后就没了，所以必须另存原文。
    assert assistant["state_raw"], "模型这一轮输出了 <state>，原文就该留下来"
    assert '"铜钥匙"' in assistant["state_raw"], "原文要是**未解析**的那一份"
    assert assistant["state_raw"].startswith("{"), "存的是块里的原文，不含 <state> 标签本身"

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    # meter 字段按 schema 扁平存（hp 与它的上限 max 是两个键，不是嵌套对象）
    assert detail["state"]["hp"] == 80 and detail["state"]["max"] == 100
    assert detail["state"]["inventory"] == ["铜钥匙"]
    assert detail["state"]["location"] == "钟楼"
    # 刷新后原文还在（落库了，不是只在响应里）
    stored_rows = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert stored_rows[-1]["state_raw"] == assistant["state_raw"]

    # ★ 遥测 JSON 里**不能**混进原文：漂移统计只认 counts/deviation，
    #   原文单独一列（否则两份数据会各说各话）
    with session_scope() as db:
        row = db.get(Message, stored_rows[-1]["id"])
        meta = json.loads(row.state_meta_json or "{}")
    assert "raw" not in meta, "遥测里不该带原文（它在 state_raw_json 那一列）"

    # 第二轮：提示词里必须带上"当前状态"和上一轮的值（否则模型下一轮必然写飘）
    fake_llm.script["content"] = "钟楼里很暗。"
    again = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我看看四周"},
        headers=user["headers"],
    )
    assert again.status_code == 201, again.text
    system = fake_llm.script["requests"][-1].messages[0].content
    assert state_mod.BLOCK_TITLE in system
    assert '"钟楼"' in system and '"铜钥匙"' in system
    # 状态必须排在最高优先级守卫**之前**（这条是真实事故换来的，别改回去）
    assert system.index(state_mod.BLOCK_TITLE) < system.index("[最高优先级 · 身份认知]")


def test_broken_state_block_does_not_break_the_turn(
    client: TestClient, user: dict, fake_llm
) -> None:
    """模型把状态块写坏时，对话必须照常完成（状态只是锦上添花）。"""
    session_id = _session_with(client, user)
    fake_llm.script["content"] = "……（沉默）\n<state>{坏掉的 JSON</state>"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "你在吗"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["state"] is None, "解析失败时不能写入半截状态"
    assert "<state>" not in detail["messages"][-1]["content"]


# ==================================================================
#  四、输出契约的位置 + 没按格式输出时必须说出来
# ==================================================================
def test_render_contract_demands_every_turn() -> None:
    """用户明确要求："状态栏每一轮都要输出，哪怕情况没有变化。" """
    contract = state_mod.render_contract()
    assert state_mod.CONTRACT_TITLE in contract
    assert state_mod.STATE_OPEN in contract
    assert "没有任何变化" in contract, "契约里必须写明『没变化也要再输出一次』"


def test_apply_reply_warns_when_model_skips_the_state_block() -> None:
    """拒绝静默降级：模型没按格式输出就得说出来，否则用户只看到空状态栏。"""
    # ★ 提醒只在"这张卡**要求过**状态块"时才有意义（第六轮定的口径：
    #   没声明状态栏的卡不注入协议、也就不能反过来怪模型没输出），所以这里给一份 schema。
    session = _Session(None, schema=LEGACY_SCHEMA)
    cleaned, notes = state_mod.apply_reply(session, "只有正文")
    assert cleaned == "只有正文" and session.state_json is None
    assert notes and "没有 <state> 状态块" in notes[0]

    # 解析开场白时要能静默（开场白没有状态块是常态，不该弹提醒）
    _c, quiet = state_mod.apply_reply(session, "开场白", notify_missing=False)
    assert quiet == []


def test_apply_reply_is_quiet_when_the_card_never_asked_for_state() -> None:
    """★ 没定义状态栏的卡：模型不输出状态块**不算错**，不该弹提醒（也不该落库）。"""
    session = _Session(None)
    cleaned, notes = state_mod.apply_reply(session, "只有正文")
    assert cleaned == "只有正文" and notes == []
    assert session.state_json is None


def test_contract_sits_after_the_identity_guard(
    client: TestClient, user: dict, fake_llm
) -> None:
    """契约必须在守卫**之后** —— 夹在中间时真实模型会直接忽略它。"""
    session_id = _session_with(client, user)
    fake_llm.script["content"] = "灯还亮着。"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "看灯"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    system = fake_llm.script["requests"][-1].messages[0].content
    assert state_mod.CONTRACT_TITLE in system
    assert system.index(state_mod.CONTRACT_TITLE) > system.index("[最高优先级 · 身份认知]")


# ==================================================================
#  六、★ 状态栏格式来自卡 / 世界书（本次修复的核心）
# ==================================================================
class _Book:
    """鸭子类型的世界书行（纯函数测试用）。"""

    def __init__(self, entries: list) -> None:
        self.entries = entries


class _Msg:
    """鸭子类型的消息行（世界书扫描用）。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _Card:
    """鸭子类型的角色卡行（只带 extra_data）。"""

    def __init__(self, extra: dict | None) -> None:
        self.extra_data = extra or {}


def _card_with(hne: dict) -> _Card:
    return _Card({"extensions": {"hne": hne}})


def test_card_without_any_declaration_yields_empty_schema() -> None:
    """★ 没声明 = 空 schema（不注入协议、不画状态栏）—— 这是本次要修的错。"""
    schema, notes = schemas.resolve_schema(_card_with({}), None)
    assert schemas.is_empty(schema)
    assert notes == []


def test_card_declared_schema_wins_over_world_book_and_initial_state() -> None:
    """三源优先级：卡显式声明 > 世界书条目 > initial_state 推断。"""
    card = _card_with(
        {
            "state_schema": [{"name": "魔力", "label": "魔力", "type": "number"}],
            "initial_state": {"hp": 100, "max": 100},
        }
    )
    book = _Book([{"keys": ["state_definition"], "content": '<state>{"别的": 1}</state>'}])
    schema, _ = schemas.resolve_schema(card, book)
    assert schema["source"] == "card"
    assert schemas.field_names(schema) == ["魔力"], "卡里的显式声明最权威"


def test_world_book_state_entry_defines_the_fields() -> None:
    """★ 作者把状态栏格式写在世界书里（最常用）—— 正文里的示例 JSON 决定字段。"""
    book = _Book(
        [
            {"keys": ["灯"], "content": "灯塔的设定"},
            {
                "name": "[状态栏] 魔女裁判",
                "keys": [],
                "content": (
                    "每轮末尾输出状态块，字段：魔力、变身、携带物。\n"
                    '<state>{"魔力": 80, "变身": "已变身", "携带物": ["魔杖"]}</state>'
                ),
            },
        ]
    )
    schema, notes = schemas.resolve_schema(_card_with({}), book)
    assert schema["source"] == "world_book"
    assert schemas.field_names(schema) == ["魔力", "变身", "携带物"]
    assert schema["fields"][0]["type"] == "number"
    assert schema["fields"][1]["type"] == "text"
    assert schema["fields"][2]["type"] == "list"
    # ★ 作者原文要原样进提示词（作者怎么写规则就怎么生效）
    assert "每轮末尾输出状态块" in schema["description"]
    assert notes == []


def test_world_book_state_entry_is_found_by_reserved_key() -> None:
    """另一种写法：keys 里含 state_definition（别的工具导出的世界书）。"""
    book = _Book([{"keys": ["state_definition"], "content": '<state>{"灯油": 60}</state>'}])
    schema, _ = schemas.resolve_schema(_card_with({}), book)
    assert schemas.field_names(schema) == ["灯油"]


def test_world_book_state_entry_without_example_is_reported() -> None:
    """条目里没有示例 JSON → 如实提醒，并退回按 initial_state 推断。"""
    book = _Book([{"name": "[状态栏]", "keys": [], "content": "魔力值 0~100，变身状态两种。"}])
    card = _card_with({"initial_state": {"魔力": 80}})
    schema, notes = schemas.resolve_schema(card, book)
    assert schemas.field_names(schema) == ["魔力"]
    assert schema["source"] == "initial_state"
    assert any("示例" in note for note in notes), notes
    # 没有示例时，作者原文也不能丢（它就是"格式要求"本身）
    contract = state_mod.render_contract(schema)
    assert state_mod.CONTRACT_TITLE in contract


def test_schema_from_nested_initial_state_becomes_a_meter() -> None:
    """老写的嵌套 initial_state 也能吃：{"hp":{"current","max"}} → 一条 meter。"""
    card = _card_with({"initial_state": {"hp": {"current": 70, "max": 90}}})
    schema, _ = schemas.resolve_schema(card, None)
    assert schemas.field_names(schema) == ["hp"]
    assert schema["fields"][0]["type"] == "meter"
    assert schema["fields"][0]["initial_current"] == 70
    assert schema["fields"][0]["initial_max"] == 90


def test_schema_parsing_rejects_bad_entries_without_raising() -> None:
    """作者写歪一格不该让整张卡的状态栏消失：坏的丢弃 + 如实提醒。"""
    schema, notes = schemas.parse_schema(
        [
            {"name": "魔力", "type": "number"},
            {"name": "魔力"},  # 重复
            {"name": "有 空格", "type": "text"},  # 非法名
            "整个都不是对象",
        ]
    )
    assert schemas.field_names(schema) == ["魔力"]
    assert len(notes) == 3, notes


def test_render_contract_lists_the_declared_fields() -> None:
    """契约里的字段清单必须与状态协议一致（否则同一份提示词自相矛盾）。"""
    schema, _ = schemas.parse_schema(
        [
            {"name": "魔力", "type": "meter"},
            {"name": "携带物", "type": "list"},
        ]
    )
    contract = schemas.render_contract(schema)
    assert "魔力（数字，0~max）" in contract
    assert "携带物（字符串数组）" in contract
    assert "hp" not in contract


def test_render_contract_omits_machine_list_when_author_wrote_it() -> None:
    """世界书里已经有作者原文时，机械清单不再重复（重复只会互相打架）。"""
    schema = {
        "spec": schemas.SPEC,
        "fields": [{"name": "魔力", "type": "number", "label": "魔力"}],
        "description": "作者自己写的格式要求",
    }
    contract = schemas.render_contract(schema)
    assert state_mod.STATE_OPEN in contract
    assert "字段只能是" not in contract
    assert "作者自己写的格式要求" not in contract, "原文在 render_for_prompt 那边，不重复"


def test_world_book_state_entry_is_not_injected_as_lore() -> None:
    """★ 格式条目**不再当普通设定注入** —— 否则同一段话进提示词两遍，
    而且那段里带着 <state> 示例，模型可能把示例当正文抄出来。
    """
    entry = {"name": "[状态栏] 魔女裁判", "keys": ["状态"], "content": "格式说明"}
    book = _Book([entry])
    result = world_book_scanner.scan(
        book, [_Msg("我想看看状态")], scan_depth=8, token_budget=1024
    )
    assert result.entries == [], "格式条目必须被扫描器跳过"


def test_end_to_end_card_schema_from_world_book(client: TestClient, user: dict) -> None:
    """端到端：世界书里写格式 → 建会话 → 状态栏字段就是世界书里那些。"""
    book = client.post(
        "/api/v1/world-books",
        json={
            "name": f"魔女世界_{uuid.uuid4().hex[:6]}",
            "entries": [
                {
                    "name": "[状态栏] 魔女裁判",
                    "keys": [],
                    "content": (
                        "每轮末尾输出状态块，字段：魔力、变身。\n"
                        '<state>{"魔力": 80, "变身": "已变身"}</state>'
                    ),
                    "enabled": True,
                }
            ],
        },
        headers=user["headers"],
    )
    assert book.status_code == 201, book.text
    book_id = book.json()["data"]["id"]
    card = client.post(
        CARDS,
        json={"name": f"魔法少女_{uuid.uuid4().hex[:6]}", "greeting": "……", "world_book_id": book_id},
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"st_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-state-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    session_id = client.post(
        BASE,
        json={
            "character_card_id": card.json()["data"]["id"],
            "llm_provider_id": provider.json()["data"]["id"],
        },
        headers=user["headers"],
    ).json()["data"]["id"]
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    schema = detail["state_schema"]
    assert schema["source"] == "world_book"
    assert [f["name"] for f in schema["fields"]] == ["魔力", "变身"]
    assert "hp" not in json.dumps(schema, ensure_ascii=False), "★ 这张卡不该有 HP"


# ==================================================================
#  五、初始状态：卡片自带，状态栏从第一轮就亮
# ==================================================================
def test_card_extension_initial_state_fills_the_bar(
    client: TestClient, user: dict
) -> None:
    session_id = _session_with(
        client,
        user,
        card={
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "自带初始状态的卡",
                "first_mes": "风把门吹响了。",
                "extensions": {
                    "hne": {
                        "initial_state": {
                            "hp": {"current": 70, "max": 90},
                            "location": "灯塔一层",
                            "inventory": ["提灯"],
                        }
                    }
                },
            },
        },
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    # ★ 字段清单**从 initial_state 推断**（这张卡没写 state_schema，也没世界书）：
    #   嵌套写法 {"hp":{"current","max"}} 被识别成一条 meter，所以 hp 是**数字**；
    #   HP 只在这张卡真的写了 hp 时才出现（这正是修掉"灯塔的 HP 跑到魔法少女身上"的关键）。
    assert detail["state"]["hp"] == 70
    assert detail["state"]["location"] == "灯塔一层"
    assert detail["state"]["inventory"] == ["提灯"]
    assert detail["state_schema"]["source"] == "initial_state"
    assert [f["name"] for f in detail["state_schema"]["fields"]] == [
        "hp",
        "location",
        "inventory",
    ]


def test_greeting_state_block_is_stripped_even_without_a_schema(
    client: TestClient, user: dict
) -> None:
    """没定义状态栏的卡：开场白里的 <state> 也要剥掉（不能给用户看原始 JSON），但不落库。"""
    session_id = _session_with(
        client,
        user,
        card={
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "没定义状态栏的卡",
                "first_mes": "他抬起头看你。\n<state>{\"location\": \"钟楼\", \"hp\": {\"current\": 40, \"max\": 100}}</state>",
            },
        },
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["state"] is None, "没声明字段的卡不该被凭空塞一份状态"
    assert detail["state_schema"]["fields"] == []
    texts = [m["content"] for m in detail["messages"]]
    assert any("他抬起头看你" in t for t in texts), texts
    assert all("<state>" not in t for t in texts), "开场白里的状态块必须剥掉"


def test_greeting_state_block_becomes_initial_state_and_is_stripped(
    client: TestClient, user: dict
) -> None:
    """开场白里自带 <state> 块的老卡（并声明了字段）：状态要落库，正文里不能留 JSON。"""
    session_id = _session_with(
        client,
        user,
        card={
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "开场白自带状态的卡",
                "first_mes": "他抬起头看你。\n<state>{\"location\": \"钟楼\", \"hp\": {\"current\": 40, \"max\": 100}}</state>",
                "extensions": {"hne": {"state_schema": LEGACY_SCHEMA["fields"]}},
            },
        },
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["state"]["location"] == "钟楼"
    assert detail["state"]["hp"] == 40 and detail["state"]["max"] == 100
    texts = [m["content"] for m in detail["messages"]]
    assert any("他抬起头看你" in t for t in texts), texts
    assert all("<state>" not in t for t in texts), "开场白里的状态块必须剥掉"
