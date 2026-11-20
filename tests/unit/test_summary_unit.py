"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.narrative import summary as summary_mod


class _Msg:
    def __init__(self, mid: int, role: str, content: str = "x") -> None:
        self.id = mid
        self.role = role
        self.content = content


class _Session:
    def __init__(
        self, *, summary=None, from_round=None, to_round=None, until=None
    ) -> None:
        self.id = 1
        self.rolling_summary = summary
        self.summary_from_round = from_round
        self.summary_to_round = to_round
        self.summarized_until_message_id = until


def _history(rounds: int) -> list[_Msg]:
    """造 rounds 轮对话（含开场白）：greeting + (user + assistant) × rounds。"""
    rows = [_Msg(1, "assistant", "开场白")]
    mid = 2
    for index in range(1, rounds + 1):
        rows.append(_Msg(mid, "user", f"第{index}轮用户"))
        rows.append(_Msg(mid + 1, "assistant", f"第{index}轮角色"))
        mid += 2
    return rows


def test_round_index_map_counts_user_turns_and_puts_greeting_in_round_one() -> None:
    index = summary_mod.round_index_map(_history(3))
    assert index[1] == 1, "开场白算作第 1 轮的开头"
    assert index[2] == 1 and index[3] == 1
    assert index[4] == 2 and index[6] == 3
    assert summary_mod.count_rounds(_history(3)) == 3


def test_should_merge_only_when_a_full_block_accumulated() -> None:
    """每攒够一整块（设置里的 rounds）才算"到点" —— 不是每轮都总结。"""
    config = {"enabled": True, "rounds": 10}
    session = _Session()
    assert summary_mod.should_merge(session, 9, settings=config) is False
    assert summary_mod.should_merge(session, 10, settings=config) is True
    # 已经覆盖到第 10 轮后，要到第 20 轮才再"到点"
    covered = _Session(to_round=10, summary="（第 1~10 轮）\n旧")
    assert summary_mod.should_merge(covered, 19, settings=config) is False
    assert summary_mod.should_merge(covered, 20, settings=config) is True
    # 关掉总开关就永远不到点
    assert (
        summary_mod.should_merge(covered, 99, settings={"enabled": False, "rounds": 10})
        is False
    )


def test_block_messages_selects_the_right_rounds() -> None:
    history = _history(20)
    chunk = summary_mod.block_messages(history, 11, 20)
    assert len(chunk) == 20, "10 轮 = 20 条消息"
    texts = [m.content for m in chunk]
    assert "第1轮用户" not in texts and "第10轮用户" not in texts
    assert texts[0] == "第11轮用户" and texts[-1] == "第20轮角色"


def test_split_previous_strips_the_round_header() -> None:
    text = "（第 1~10 轮）\n薇拉点亮了灯塔。"
    assert summary_mod.split_previous(text) == "薇拉点亮了灯塔。"
    assert summary_mod.split_previous(None) == ""


def test_clamp_summary_keeps_both_ends() -> None:
    """超长时掐中间留两头：开头是前提、结尾是最新处境，中间最不致命。"""
    text = "开头很重要" + "中" * 500 + "结尾也很重要"
    clamped = summary_mod.clamp_summary(text, limit=100)
    assert clamped.startswith("开头很重要")
    assert clamped.endswith("结尾也很重要")
    assert "中段前情已省略" in clamped


def test_filter_uncovered_drops_summarized_messages() -> None:
    session = _Session(to_round=10, until=21)
    history = _history(20)
    kept = summary_mod.filter_uncovered(session, history)
    assert all(m.id > 21 for m in kept), "被覆盖的消息不该再进提示词"
    assert len(kept) < len(history)


def test_summary_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉总开关 → 一行总结都不做（用户对成本敏感，必须能关）。"""
    outcome = summary_mod.merge_block(
        session=_Session(),
        adapter=None,
        messages=_history(30),
        settings={"enabled": False},
    )
    assert outcome.merged is False
    assert outcome.reason and "关闭" in outcome.reason, (
        "要说明是总开关关着，不是静默不做"
    )


def test_mode_instruction_reaches_the_system_prompt() -> None:
    """选中的那一档必须出现在**系统提示词**里（而不是只存在于常量里）。"""
    messages = summary_mod.build_summarize_messages(
        "旧提要", _history(4), from_round=1, to_round=2, mode=summary_mod.MODE_TABLE
    )
    system = messages[0].content
    assert "| 角色 | 关系 | 关键事件 | 当前状态 | 未了结 |" in system, (
        "列名要写死给模型"
    )


def test_custom_prompt_replaces_the_whole_system_prompt() -> None:
    """ "自定义"是**整段替换**（包括公共约束）—— 否则用户改不掉那些他不想要的规矩。"""
    messages = summary_mod.build_summarize_messages(
        None,
        _history(4),
        from_round=1,
        to_round=2,
        mode="custom",
        custom_prompt="只写三句话。",
    )
    assert messages[0].content == "只写三句话。"


pytestmark = pytest.mark.unit


def test_summary_cost_reflects_custom_prompt_and_output_budget():
    history = _history(20)
    normal = summary_mod.estimate_summary_cost(
        {"mode": "table", "max_chars": 2000}, history, 1, 8
    )
    custom = summary_mod.estimate_summary_cost(
        {"mode": "custom", "prompt": "很短。", "max_chars": 2000}, history, 1, 8
    )
    larger = summary_mod.estimate_summary_cost(
        {"mode": "table", "max_chars": 3000}, history, 1, 8
    )
    assert 0 < custom < normal < larger
