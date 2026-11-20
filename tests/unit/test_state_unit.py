"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations

import json


import pytest


from app.narrative import state as state_mod

from app.narrative import state_schema as schemas

from app.narrative import world_book_scanner


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
    state, _notes = state_mod.normalize(
        {"hp": 10, "max": 100}, {}, {"spec": "x", "fields": []}
    )
    assert state == {}


def test_apply_reply_degrades_on_broken_json() -> None:
    """坏 JSON 不能抛异常：沿用上一轮 + 告诉用户。"""
    session = _Session('{"hp": 42, "max": 100}', schema=LEGACY_SCHEMA)
    cleaned, notes = state_mod.apply_reply(session, "正文\n<state>{不是 JSON}</state>")
    assert cleaned == "正文"
    assert session.state_json == '{"hp": 42, "max": 100}'  # 原样保留
    assert notes and "不是合法 JSON" in notes[0]


def test_render_for_prompt_is_empty_without_a_declared_schema() -> None:
    """★ 这张卡没定义状态栏、也没有任何状态 → 不注入任何协议。

    老实现无论什么卡都塞 HP 那套，于是没声明 HP 的卡（魔法少女 / 魔女裁判）
    也长出 `HP 100/100` —— 那是用户明确指出的设计错误。
    """
    assert state_mod.render_for_prompt(_Session(None)) == ""
    assert (
        state_mod.render_for_prompt(_Session(None, schema={"spec": "x", "fields": []}))
        == ""
    )


def test_legacy_session_without_schema_still_renders_old_fields() -> None:
    """迁移前的旧会话（没有 schema 记录，但已有状态）走兼容分支，状态栏不会变空白。"""
    session = _Session('{"hp": 100, "max": 100}')
    block = state_mod.render_for_prompt(session)
    assert state_mod.BLOCK_TITLE in block, "有状态的旧会话仍要能看到当前状态"
    assert state_mod.effective_schema(session).get("legacy") is True


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


def test_card_declared_schema_wins_over_world_book_and_initial_state() -> None:
    """三源优先级：卡显式声明 > 世界书条目 > initial_state 推断。"""
    card = _card_with(
        {
            "state_schema": [{"name": "魔力", "label": "魔力", "type": "number"}],
            "initial_state": {"hp": 100, "max": 100},
        }
    )
    book = _Book(
        [{"keys": ["state_definition"], "content": '<state>{"别的": 1}</state>'}]
    )
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
    book = _Book(
        [{"keys": ["state_definition"], "content": '<state>{"灯油": 60}</state>'}]
    )
    schema, _ = schemas.resolve_schema(_card_with({}), book)
    assert schemas.field_names(schema) == ["灯油"]


def test_world_book_state_entry_without_example_is_reported() -> None:
    """条目里没有示例 JSON → 如实提醒，并退回按 initial_state 推断。"""
    book = _Book(
        [{"name": "[状态栏]", "keys": [], "content": "魔力值 0~100，变身状态两种。"}]
    )
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


pytestmark = pytest.mark.unit
