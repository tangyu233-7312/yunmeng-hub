"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest

from scripts import benchmark as bench


from app.narrative import retrieval_metrics as metrics


def test_recall_at_k_uses_relevant_set_as_denominator() -> None:
    ranked = ["a", "b", "c", "d"]
    assert metrics.recall_at_k(ranked, ["b", "d"], 2) == pytest.approx(0.5)
    assert metrics.recall_at_k(ranked, ["b", "d"], 4) == pytest.approx(1.0)
    assert metrics.recall_at_k(ranked, [], 4) == 0.0


def test_reciprocal_rank_takes_the_first_hit() -> None:
    assert metrics.reciprocal_rank(["x", "y", "z"], ["z"]) == pytest.approx(1 / 3)
    assert metrics.reciprocal_rank(["z"], ["z"]) == pytest.approx(1.0)
    assert metrics.reciprocal_rank(["x"], ["z"]) == 0.0


def test_ndcg_at_k_matches_hand_computation() -> None:
    # 相关集合 {a, b}，k=2：理想排序 a,b → DCG = 1 + 1/log2(3)，命中同样是 a,b
    assert metrics.ndcg_at_k(["a", "b"], ["a", "b"], 2) == pytest.approx(1.0)
    # 命中顺序反了：DCG = 1/log2(2) + 1/log2(3)（b 在第 1 位、a 在第 2 位）
    dcg = 1.0 / 1 + 1.0 / 1.584962500721156
    idcg = 1.0 / 1 + 1.0 / 1.584962500721156
    assert metrics.ndcg_at_k(["a", "b"], ["a", "b"], 2) == pytest.approx(dcg / idcg)
    # 一条都没命中 → 0
    assert metrics.ndcg_at_k(["x", "y"], ["a"], 2) == 0.0


def test_ranking_report_averages_over_cases() -> None:
    cases = [
        metrics.RankedCase(name="q1", ranked=["a", "b"], relevant=["a"]),
        metrics.RankedCase(name="q2", ranked=["b", "a"], relevant=["a"]),
    ]
    report = metrics.ranking_report(cases, ks=[1])
    assert report["recall@1"] == pytest.approx(0.5)
    assert report["mrr"] == pytest.approx((1.0 + 0.5) / 2)


def test_injection_report_summarizes_tokens_and_dedup() -> None:
    rows = [
        {
            "used_tokens": 100,
            "injected": 2,
            "lexical_total": 1,
            "semantic_total": 3,
            "deduped": 1,
            "dropped": 1,
        },
        {
            "used_tokens": 200,
            "injected": 4,
            "lexical_total": 2,
            "semantic_total": 2,
            "deduped": 0,
            "dropped": 0,
        },
    ]
    report = metrics.injection_report(rows)
    assert report["注入token(均)"] == pytest.approx(150.0)
    assert report["注入token(中位)"] == pytest.approx(150.0)
    assert report["去重率"] == pytest.approx((1 / 4 + 0 / 4) / 2)


def test_classify_notes_prefers_rejection_over_correction() -> None:
    """同时出现"丢弃"和"修正"提醒时算丢弃（更严重的那一类）。"""
    assert (
        metrics.classify_notes(["HP 不是数字，已沿用上一轮"])
        == metrics.OUTCOME_REJECTED
    )
    assert (
        metrics.classify_notes(["HP 越界（150），已夹到 0～100 之间"])
        == metrics.OUTCOME_CORRECTED
    )
    assert metrics.classify_notes([]) == metrics.OUTCOME_UNCHANGED


def test_replay_report_is_honest_about_the_missing_labels() -> None:
    """报告里必须写明"真实语料没有标注、只报成本/合规、本模式只读"。"""
    text = bench.print_replay_report(
        {
            "rows_replayed": 2,
            "semantic_mode_used": "none",
            "cost": {
                "注入条数(均)": 1.0,
                "注入token(均)": 12.0,
                "去重率": 0.0,
                "丢弃条数(均)": 0.0,
            },
            "meta": {
                "sessions_scanned": 1,
                "sessions_used": 1,
                "messages_scanned": 4,
                "state_messages": 2,
                "state_missing": 1,
                "state_sessions": 1,
                "state_idempotent": 1,
                "state_not_idempotent": 0,
            },
            "state_block_missing_rate": 0.5,
            "state_idempotent_rate": 1.0,
            "errors": [],
            "samples": [],
        }
    )
    assert "不报 Recall@k" in text
    assert "只读" in text
    assert "50.0%" in text


pytestmark = pytest.mark.unit
