"""检索与状态校验的**评测指标**（纯函数，零依赖，可复现）。

==================== 为什么单独一个模块 ====================
`scripts/benchmark.py` 要产出的表格是要写进论文的，所以指标本身必须：
  · **可测**：指标算错是最难发现的一类错误，所以把它们从脚本里抽出来，
    由 `tests/test_retrieval.py` 用已知答案的小例子钉死；
  · **可复现**：不依赖随机数、不依赖向量库、不调模型（红线：测试/评测不花钱）；
  · **口径明确**：每个指标都写清"分母是什么、k 是几、排序里算不算被预算砍掉的条目"。

==================== 三组指标 ====================
1. 排序质量：Recall@k / MRR / nDCG@k —— 衡量"相关的东西有没有排在前面"。
   ★ 排序用的是**融合后的完整名次**（含被 token 预算砍掉的候选），
     因为要评的是检索/融合/重排的质量；预算属于"提示词装配"那一层，
     它另有指标（注入 token、注入条数）。
2. 装配成本：注入条数、注入 token（均值/中位数）、去重率。
   ★ 这一组才是"共享预算"策略的实际代价与收益。
3. 状态一致率：把 `state.normalize` 当成被测对象，用标注好的用例统计
   「原样接受 / 被校验修正 / 被丢弃」三类比例 —— 不需要模型，完全确定性。
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence


# ==================================================================
#  一、排序质量
# ==================================================================
def recall_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """前 k 条里命中多少比例的相关条目（分母 = 相关条目总数）。"""
    targets = set(relevant)
    if not targets:
        return 0.0
    top = list(ranked)[: max(k, 0)]
    return len(targets & set(top)) / len(targets)


def reciprocal_rank(ranked: Sequence[str], relevant: Iterable[str]) -> float:
    """第一个相关条目的名次倒数（1/rank）；一条都没命中记 0。"""
    targets = set(relevant)
    for index, key in enumerate(ranked, start=1):
        if key in targets:
            return 1.0 / index
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """二值相关性的 nDCG@k（相关=1、不相关=0，理想排序是"相关的全在最前面"）。"""
    targets = set(relevant)
    if not targets:
        return 0.0
    top = list(ranked)[: max(k, 0)]
    dcg = sum(
        1.0 / math.log2(index + 1)
        for index, key in enumerate(top, start=1)
        if key in targets
    )
    ideal_hits = min(len(targets), max(k, 0))
    idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


@dataclass
class RankedCase:
    """一个查询的评测输入。"""

    name: str
    ranked: list[str] = field(default_factory=list)
    relevant: list[str] = field(default_factory=list)


def ranking_report(cases: Sequence[RankedCase], ks: Sequence[int] = (1, 3, 5)) -> dict[str, float]:
    """一组查询的排序指标均值。"""
    report: dict[str, float] = {}
    if not cases:
        return report
    for k in ks:
        report[f"recall@{k}"] = _mean(recall_at_k(c.ranked, c.relevant, k) for c in cases)
    report["mrr"] = _mean(reciprocal_rank(c.ranked, c.relevant) for c in cases)
    report[f"ndcg@{max(ks)}"] = _mean(ndcg_at_k(c.ranked, c.relevant, max(ks)) for c in cases)
    return report


def _mean(values: Iterable[float]) -> float:
    items = [float(v) for v in values]
    return sum(items) / len(items) if items else 0.0


# ==================================================================
#  二、装配成本
# ==================================================================
def injection_report(rows: Sequence[dict[str, Any]]) -> dict[str, float]:
    """注入条数 / token / 去重率的均值（rows 是每次检索的 to_dict()）。"""
    if not rows:
        return {}
    tokens = [int(row.get("used_tokens") or 0) for row in rows]
    injected = [int(row.get("injected") or 0) for row in rows]
    candidates = [
        int(row.get("lexical_total") or 0) + int(row.get("semantic_total") or 0) for row in rows
    ]
    deduped = [int(row.get("deduped") or 0) for row in rows]
    dropped = [int(row.get("dropped") or 0) for row in rows]
    return {
        "注入条数(均)": _mean(injected),
        "注入token(均)": _mean(tokens),
        "注入token(中位)": float(statistics.median(tokens)) if tokens else 0.0,
        "去重率": _mean(d / c for d, c in zip(deduped, candidates) if c) if candidates else 0.0,
        "丢弃条数(均)": _mean(dropped),
    }


# ==================================================================
#  三、状态一致率
# ==================================================================
#: 校验结果的三种下场（顺序 = 判定优先级：先看有没有被丢弃，再看有没有被修正）
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_CORRECTED = "corrected"
OUTCOME_REJECTED = "rejected"


def classify_notes(notes: Sequence[str]) -> str:
    """按校验提醒判断这次状态的下场。

    ★ 分类只在 `app/narrative/state.py::classify_notes` 实现一次，
      这里把它的四类计数映射成"三种下场"，避免两处各写一套标记词。
    """
    from app.narrative import state as state_mod

    counts = state_mod.classify_notes(list(notes))
    if counts["rejected"] or counts["unknown"]:
        return OUTCOME_REJECTED
    if counts["clamped"] or counts["guarded"]:
        return OUTCOME_CORRECTED
    return OUTCOME_UNCHANGED


def state_consistency(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """跑一遍状态校验用例，返回三类占比 + 逐条明细。

    用例形如：
        {"name": "...", "schema": {...}, "previous": {...}, "raw": {...},
         "expect": "unchanged" | "corrected" | "rejected"}

    ★ 为什么要"expect"：它让这份 benchmark 同时是**校验语义的回归测试** ——
      指标之外还能立刻发现"某类状态被改了判据"。
    """
    from app.narrative import state as state_mod

    outcomes: list[dict[str, Any]] = []
    mismatched: list[str] = []
    for case in cases:
        schema, _notes = _schema_of(case)
        state, notes = state_mod.normalize(case.get("raw") or {}, case.get("previous") or {}, schema)
        outcome = classify_notes(notes)
        expected = case.get("expect")
        if expected and expected != outcome:
            mismatched.append(f"{case.get('name')}: 期望 {expected}，实际 {outcome}（{notes}）")
        outcomes.append(
            {
                "name": case.get("name"),
                "outcome": outcome,
                "expected": expected,
                "notes": list(notes),
                "state": state,
            }
        )

    total = len(outcomes) or 1
    counter = Counter(item["outcome"] for item in outcomes)
    return {
        "total": len(outcomes),
        "counts": dict(counter),
        "原样接受率": counter[OUTCOME_UNCHANGED] / total,
        "被修正率": counter[OUTCOME_CORRECTED] / total,
        "被丢弃率": counter[OUTCOME_REJECTED] / total,
        "判据不符": mismatched,
        "details": outcomes,
    }


def _schema_of(case: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    from app.narrative import state_schema as schema_mod

    if case.get("schema") is not None:
        return schema_mod.parse_schema(case["schema"])
    return schema_mod.legacy_schema(), []


# ==================================================================
#  四、输出：控制台 / Markdown 表
# ==================================================================
def markdown_table(ablation: dict[str, dict[str, float]], *, columns: Sequence[str]) -> str:
    """把消融结果渲染成 Markdown 表（可直接贴进论文）。"""
    lines = ["| 策略 | " + " | ".join(columns) + " |", "|---" * (len(columns) + 1) + "|"]
    for mode, row in ablation.items():
        cells = [f"{row.get(col, 0.0):.4f}" for col in columns]
        lines.append(f"| `{mode}` | " + " | ".join(cells) + " |")
    return "\n".join(lines)
