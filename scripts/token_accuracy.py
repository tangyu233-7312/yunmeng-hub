"""token 估算精度对照实测（**只读**，不调模型、不花钱）。

==================== 为什么要有这个脚本 ====================
本项目对"发出去多少 token"用的是一套**启发式估算**（`context_manager.estimate_tokens`，
中文按字数、英文按字符数，再加 5% 安全余量）。它决定：
  · 上下文预算怎么裁（裁多了丢剧情、裁少了被厂商 400）
  · 界面上的「估算输入 / 预算」与「本轮预估」
论文里一直只能写"**未做过**估算值 vs 厂商 `prompt_tokens` 的对照实测"。

第十六轮起，每一轮助手回复都把**厂商返回的 usage 分项**与**我们发出去前的估算**
一起落在 `messages.usage_json` 里 ⇒ 这个脚本只读回放就能给出对照表，
不需要再花一分钱。

==================== 怎么用 ====================
    .\\.venv\\Scripts\\python.exe scripts\\token_accuracy.py            # 打印对照表
    .\\.venv\\Scripts\\python.exe scripts\\token_accuracy.py --json     # 另存 data/token_accuracy_report.json

★ 样本只有"本轮起记录过的轮次"（老数据没有这一列），脚本会如实写出 N。
★ 口径：`estimated_input_tokens` 是我们**发出去之前**对整段输入的估算；
        `prompt_tokens` 是厂商**真实**算的输入 token。两者比值的理想值 = 1.0；
        估算偏小（<1）意味着预算可能被突破，偏大（>1）意味着上下文被过早裁掉。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db.models import Message, NarrativeSession  # noqa: E402
from app.db.mysql import session_scope  # noqa: E402

REPORT = ROOT / "data" / "token_accuracy_report.json"


def load_samples() -> list[dict]:
    """只读取出所有带 `usage_json` 的助手消息（按时间升序）。"""
    samples: list[dict] = []
    with session_scope() as db:
        rows = db.execute(
            select(Message, NarrativeSession.title)
            .join(NarrativeSession, Message.session_id == NarrativeSession.id)
            .where(Message.role == "assistant", Message.usage_json.is_not(None))
            .order_by(Message.id)
        ).all()
        for message, title in rows:
            try:
                usage = json.loads(message.usage_json or "{}")
            except ValueError:
                continue
            samples.append(
                {
                    "message_id": message.id,
                    "session_id": message.session_id,
                    "title": title,
                    "model": message.model_name,
                    "estimated": int(usage.get("estimated_input_tokens") or 0),
                    "actual": int(usage.get("prompt_tokens") or 0),
                    "completion": int(usage.get("completion_tokens") or 0),
                    "reasoning": int(usage.get("reasoning_tokens") or 0),
                    "total": int(usage.get("total_tokens") or 0),
                    "content_len": len(message.content or ""),
                }
            )
    return samples


def summarize(samples: list[dict]) -> dict:
    """把样本压成对照结论（纯函数，便于单测）。

    只统计两边都有值的轮次：估算为 0（老数据）或厂商没给 usage 的都不算。
    """
    pairs = [s for s in samples if s["estimated"] > 0 and s["actual"] > 0]
    if not pairs:
        return {"samples": 0, "usable": 0}

    ratios = [s["estimated"] / s["actual"] for s in pairs]
    under = [s for s in pairs if s["estimated"] < s["actual"]]  # 低估：可能突破预算
    completion = sum(s["completion"] for s in pairs)
    reasoning = sum(s["reasoning"] for s in pairs)
    return {
        "samples": len(samples),
        "usable": len(pairs),
        "ratio_mean": round(statistics.fmean(ratios), 3),
        "ratio_median": round(statistics.median(ratios), 3),
        "ratio_min": round(min(ratios), 3),
        "ratio_max": round(max(ratios), 3),
        "ratio_p90": round(sorted(ratios)[max(0, int(len(ratios) * 0.9) - 1)], 3),
        "under_estimated": len(under),
        "under_estimated_share": round(len(under) / len(pairs), 3),
        "worst_under": round(min(ratios), 3),
        "estimated_sum": sum(s["estimated"] for s in pairs),
        "actual_sum": sum(s["actual"] for s in pairs),
        "reasoning_tokens": reasoning,
        "completion_tokens": completion,
        "reasoning_share": round(reasoning / completion, 3) if completion else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="token 估算精度对照实测（只读）")
    parser.add_argument("--json", action="store_true", help=f"另存报告到 {REPORT.name}")
    parser.add_argument("--last", type=int, default=15, help="打印最近多少轮明细")
    args = parser.parse_args()

    samples = load_samples()
    stats = summarize(samples)

    print("=" * 66)
    print("  token 估算精度对照（估算值 vs 厂商 prompt_tokens，只读回放）")
    print("=" * 66)
    if not stats.get("usable"):
        print(f"  可用样本：0（带 usage_json 的消息共 {stats.get('samples', 0)} 条）")
        print("  ★ 第十六轮起才开始记录逐轮用量：先聊几轮，再回来跑这个脚本。")
        return 0

    print(f"  样本：{stats['usable']} 轮（带用量的消息 {stats['samples']} 条）")
    print()
    print(f"  {'消息':>6} {'模型':<18} {'估算输入':>8} {'真实输入':>8} {'比值':>6} {'思考':>6}")
    for s in samples[-args.last :]:
        ratio = s["estimated"] / s["actual"] if s["actual"] else 0
        print(
            f"  {s['message_id']:>6} {(s['model'] or '-'):<18} {s['estimated']:>8} "
            f"{s['actual']:>8} {ratio:>6.2f} {s['reasoning']:>6}"
        )
    print()
    print("  ---------- 结论 ----------")
    print(f"  估算/真实 比值：均值 {stats['ratio_mean']} · 中位 {stats['ratio_median']} "
          f"· 最小 {stats['ratio_min']} · 最大 {stats['ratio_max']} · P90 {stats['ratio_p90']}")
    print(f"  低估（估算 < 真实）的轮次：{stats['under_estimated']} / {stats['usable']} "
          f"= {stats['under_estimated_share'] * 100:.1f}%（这些轮次预算可能被突破）")
    print(f"  累计：估算 {stats['estimated_sum']} vs 真实输入 {stats['actual_sum']}"
          f"（偏差 {(stats['ratio_mean'] - 1) * 100:+.1f}%）")
    print(f"  思考 token 占输出：{stats['reasoning_tokens']} / {stats['completion_tokens']} "
          f"= {stats['reasoning_share'] * 100:.1f}%（这部分的钱是思考，不是正文）")
    print()
    print("  ★ 论文口径：n 轮、比值均值/中位与低估占比都必须照抄上面的数字，")
    print("    不许写成「误差 <10%」这种没有样本支撑的话。")

    if args.json:
        REPORT.write_text(
            json.dumps({"stats": stats, "samples": samples}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"  报告：{REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
