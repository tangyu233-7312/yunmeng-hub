"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.narrative import dice as dice_mod


class _Seq:
    """按剧本出点数的假随机源 —— 爆炸骰/取高取低只有这样才能精确断言。"""

    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def randint(self, low: int, high: int) -> int:
        value = self.values.pop(0)
        assert low <= value <= high, f"剧本给的 {value} 不在 {low}~{high} 内"
        return value


def test_notation_is_evaluated_the_trpg_way() -> None:
    rng = _Seq([4, 6])
    result = dice_mod.evaluate("2d6+3", rng=rng)
    assert result.ok and result.total == 13
    # 逐颗骰面必须留下来（论文里要能核对"这个 13 是怎么来的"）
    assert result.faces == [4, 6]
    assert result.terms[0]["subtotal"] == 10 and result.terms[-1]["value"] == 3


def test_keep_highest_and_lowest_drop_the_rest() -> None:
    high = dice_mod.evaluate("4d6kh3", rng=_Seq([1, 1, 5, 1]))
    assert high.total == 7, "4d6 取高 3：5+1+1"
    low = dice_mod.evaluate("4d6kl1", rng=_Seq([3, 5, 1, 5]))
    assert low.total == 1, "4d6 取低 1"


def test_exploding_dice_stop_at_the_budget() -> None:
    """★ 爆炸骰必须有上限：`1d2!` 理论上可以无限炸下去。"""
    result = dice_mod.evaluate("1d2!", rng=_Seq([2, 2, 1]))
    assert result.ok and result.total == 5
    assert result.terms[0]["exploded"] == 2
    # 剧本只给 3 个值：上限一旦失效，第 4 次 randint 会 IndexError
    capped = dice_mod.evaluate(
        "1d2!", rng=_Seq([2] * (dice_mod.DEFAULT_LIMITS.max_explode + 1))
    )
    assert capped.ok
    assert capped.terms[0]["exploded"] == dice_mod.DEFAULT_LIMITS.max_explode


def test_success_check_is_kept_next_to_the_roll() -> None:
    won = dice_mod.evaluate("1d20+5<=15", rng=_Seq([10]))
    assert (
        won.total == 15
        and won.compare == "<="
        and won.target == 15
        and won.success is True
    )
    lost = dice_mod.evaluate("1d20+5<=15", rng=_Seq([19]))
    assert lost.total == 24 and lost.success is False


def test_limits_are_reported_not_silently_bypassed() -> None:
    """★ 超限一律**拒绝并说明原因**，绝不"截断后照算"（截断会静默改变语义）。"""
    cases = {
        "200d6": "最多掷 100 颗",
        "1d2000": "最多 1000 个面",
        "1d0": "至少要有 2 个面",
        "(" * 12 + "1d6" + ")" * 12: "嵌套太深",
        "1d6/0": "除数不能是 0",
        "1d6+": "没写完",
    }
    for expression, keyword in cases.items():
        result = dice_mod.evaluate(expression)
        assert not result.ok, expression
        assert keyword in result.error, f"{expression} → {result.error}"
    assert "太长" in dice_mod.evaluate("1d6" * 200).error


def test_no_code_is_ever_executed() -> None:
    """★ 插件不放开 JS，解析器也不许被当成解释器：一切非骰子语法都只是"看不懂"。"""
    for expression in (
        "__import__('os').system('echo hi')",
        "1d6; import os",
        "1d6 or 1",
    ):
        result = dice_mod.evaluate(expression)
        assert not result.ok
        assert result.total is None
        assert "看不懂的字符" in result.error or "多余内容" in result.error


def test_expression_and_label_are_split_the_same_way_everywhere() -> None:
    assert dice_mod.split_expression("1d20+5 力量检定") == ("1d20+5", "力量检定")
    assert dice_mod.split_expression("2d6 + 3 力量检定") == ("2d6 + 3", "力量检定")
    assert dice_mod.split_expression("1d20 + 5") == ("1d20 + 5", "")
    assert dice_mod.split_expression("1d100 # 聆听") == ("1d100", "聆听")


def test_triggers_only_match_at_the_start_of_a_line() -> None:
    spec = dice_mod.parse_config({})
    assert dice_mod.scan_commands("我该/r 1d6 吗", spec) == [], "行中间的不是指令"
    assert len(dice_mod.scan_commands("/r 1d6", spec)) == 1
    assert len(dice_mod.scan_commands("骰个 1d6", spec)) == 0, "触发词必须紧贴行首"


def test_longer_trigger_wins_over_the_shorter_one() -> None:
    """★ `/roll` 不能被更短的 `/r` 抢走（否则剩下的 `oll 1d20` 会被当成表达式）。"""
    spec = dice_mod.parse_config({})
    rolls = dice_mod.scan_commands("/roll 1d20", spec)
    assert len(rolls) == 1 and rolls[0].ok and rolls[0].expression == "1d20"


def test_bare_trigger_uses_the_default_expression() -> None:
    spec = dice_mod.parse_config({"default_expr": "2d6"})
    rolls = dice_mod.scan_commands("掷骰", spec)
    assert len(rolls) == 1 and rolls[0].expression == "2d6"


def test_too_many_commands_are_capped_with_an_explanation() -> None:
    spec = dice_mod.parse_config({})
    rolls = dice_mod.scan_commands("\n".join(["/r 1d6"] * 8), spec)
    assert len(rolls) == dice_mod.MAX_ROLLS_PER_TURN + 1
    assert rolls[-1].error and "最多掷" in rolls[-1].error


def test_model_roll_tags_are_replaced_by_real_numbers() -> None:
    spec = dice_mod.parse_config({})
    text, rolls = dice_mod.resolve_model_rolls(
        "我举盾格挡。<roll>1d20+5</roll> 门后传来低吼。", spec, rng=_Seq([12])
    )
    assert "<roll>" not in text and "</roll>" not in text
    assert "（掷骰 1d20+5 = 17" in text
    assert len(rolls) == 1 and rolls[0].total == 17 and rolls[0].source == "model"


def test_model_roll_tags_are_stripped_even_without_the_plugin() -> None:
    """★ 插件没启用也要剥：绝不能让用户看到 `<roll>` 这种原始协议文本。"""
    text, rolls = dice_mod.resolve_model_rolls("挥剑。<roll>1d20</roll>", None)
    assert "<roll>" not in text and rolls == []
    assert "骰子插件未启用" in text


def test_a_broken_roll_tag_keeps_the_rest_of_the_reply() -> None:
    """没有闭合标签时只能剥标签本身 —— 掷骰常写在句子中间，整段截掉会吞掉正文。"""
    spec = dice_mod.parse_config({})
    text, rolls = dice_mod.resolve_model_rolls("先掷 <roll>1d20 然后我继续说话", spec)
    assert "然后我继续说话" in text
    assert "<roll>" not in text
    assert len(rolls) == 1 and rolls[0].error


def test_bad_payloads_never_break_the_reader() -> None:
    assert dice_mod.loads(None) == []
    assert dice_mod.loads("{不是 JSON") == []
    assert dice_mod.loads('{"a": 1}') == []
    assert dice_mod.loads('[{"total": 3}, 5]') == [{"total": 3}]
    assert dice_mod.dumps([]) is None


pytestmark = pytest.mark.unit
