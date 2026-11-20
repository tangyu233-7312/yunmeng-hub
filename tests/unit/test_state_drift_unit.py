"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations

import json


from pathlib import Path

import pytest


from app.narrative import state as state_mod

from app.narrative import state_drift as drift

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_data" / "drift_script.json"


def _spec() -> dict:
    return json.loads(SCRIPT.read_text(encoding="utf-8"))


def test_bounds_come_from_the_initial_state_not_the_current_one() -> None:
    """★ 标尺必须是故事开始时的上限：模型把 max 也写飘了，不能"自己给自己放宽"。"""
    schema, _ = state_mod.schema_mod.parse_schema(_spec()["schema"])
    bounds = drift.bounds_from(schema, _spec()["initial"])
    assert bounds["hp"] == 100
    inflated = {"hp": 350, "max": 400}  # 模型把上限也写成 400
    assert drift.out_of_range(inflated, schema, bounds=bounds) == pytest.approx(2.5)


def test_out_of_range_returns_none_when_nothing_is_computable() -> None:
    """算不出来 ≠ 合法：hp 被写成文字时必须给 None，否则均值会被假的 0 拉低。"""
    schema, _ = state_mod.schema_mod.parse_schema(_spec()["schema"])
    assert (
        drift.out_of_range({"hp": "一百", "max": 100}, schema, bounds={"hp": 100.0})
        is None
    )


def test_missing_block_rounds_are_flagged() -> None:
    """脚本里第 7 / 31 轮"模型没输出状态块"，遥测必须如实标出来（而不是靠猜）。"""
    records = {r.round_no: r for r in drift.simulate(_spec())}
    assert records[7].had_block is False
    assert records[31].had_block is False
    assert records[8].had_block is True
    totals = drift.summary(drift.simulate(_spec()))
    assert totals["missing_blocks"] == 2
    assert totals["missing_rate"] == pytest.approx(0.05)


def test_classify_notes_splits_the_four_mechanisms() -> None:
    """四种机制必须分开计数（合成"修正"一类就看不出是哪一层在起作用）。"""
    counts = state_mod.classify_notes(
        [
            "hp 越界（150），已夹到 0～100 之间",
            "hp 上限一轮内从 100 变成 300（超过 50%），已保留上一轮的上限",
            "状态块里的 hp 不是数字，已沿用上一轮",
            "状态块里有这张卡没有定义的字段，已忽略：金币",
        ]
    )
    assert counts == {
        "clamped": 1,
        "guarded": 1,
        "rejected": 1,
        "unknown": 1,
        "other": 0,
    }


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "field,value,dirty",
    [
        ("hp", "一百", True),
        ("inventory", [1, 2], True),
        ("quests", ["x"], True),
        ("flags", ["a"], True),
        ("location", 123, True),
        ("hp", 80, False),
    ],
    ids=["meter", "list", "tuples", "flags", "text", "valid"],
)
def test_drift_report_identifies_dirty_state(field, value, dirty):
    spec = _spec()
    spec.update(rounds=1, drift={}, events={"1": {field: value}})
    report = drift.summary(drift.simulate(spec))
    assert report["naive_garbage_rate"] == float(dirty)
