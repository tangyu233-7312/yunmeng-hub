"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.narrative import anchors as anchors_mod


class _Session:
    def __init__(self, raw=None) -> None:
        self.id = 1
        self.memory_anchors_json = raw


def test_max_items_is_enforced_by_rejection_not_truncation() -> None:
    """★ 超限必须**拒绝**：悄悄截断会让用户以为存住了（下次打开少一条更迷惑）。"""
    too_many = [f"第 {i} 条锚点" for i in range(anchors_mod.MAX_ITEMS + 1)]
    ok, message, cleaned = anchors_mod.validate(too_many)
    assert ok is False
    assert f"最多只能加 {anchors_mod.MAX_ITEMS} 条" in message
    assert len(cleaned) == anchors_mod.MAX_ITEMS + 1, (
        "拒绝时也把清洗结果带回来，便于前端显示"
    )


def test_total_chars_limit_is_enforced() -> None:
    # ★ 每条都要**不一样**，否则会被去重成一条，就测不到"合计超限"了
    heavy = [f"{i}" + "字" * (anchors_mod.MAX_ONE_CHARS - 1) for i in range(5)]
    assert sum(len(i) for i in heavy) > anchors_mod.MAX_CHARS
    ok, message, _ = anchors_mod.validate(heavy)
    assert ok is False and "合计最多" in message


def test_single_item_is_capped() -> None:
    """单条过长会被截到 MAX_ONE_CHARS（否则一条就把整额吃光）。"""
    ok, _message, cleaned = anchors_mod.validate(
        ["长" * (anchors_mod.MAX_ONE_CHARS + 100)]
    )
    assert ok is True
    assert len(cleaned[0]) == anchors_mod.MAX_ONE_CHARS


def test_blank_and_duplicate_items_are_dropped_with_a_note() -> None:
    ok, note, cleaned = anchors_mod.validate(
        ["  主角是女性  ", "", "   ", "主角是女性", 42, "世界没有魔法"]
    )
    assert ok is True
    assert cleaned == ["主角是女性", "世界没有魔法"], cleaned
    assert "重复" in note, note


def test_broken_json_degrades_to_empty() -> None:
    assert anchors_mod.load(_Session("{不是 JSON")) == []


pytestmark = pytest.mark.unit
