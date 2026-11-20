"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


from typing import Any

import pytest


from app.llm.params import compute_context_budget

from app.llm.schema import ChatMessage


from app.narrative import context_manager, world_book_scanner

from app.narrative.prompt_builder import build_prompt


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
    messages = [
        ChatMessage.system("人设"),
        ChatMessage.user("你好"),
        ChatMessage.assistant("嗯"),
    ]
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


pytestmark = pytest.mark.unit
