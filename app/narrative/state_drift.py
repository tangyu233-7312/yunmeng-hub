"""状态漂移曲线：把"模型自称的状态"与"校验后落库的状态"随轮次画出来。

==================== 它回答什么问题 ====================
`state.py` 里的校验（越界夹取 / 上限跳变护栏 / 脏值丢弃 / 未知字段忽略）到底值不值？
回答这个问题不能只给一个总数（"本轮修正了 3 处"），而要给出**随轮次变化的曲线**：

    · **反事实（不校验）**：把模型每轮的原始值直接当状态存下来，它会漂到哪去
      （HP 越滚越高、脏值写进背包、状态栏和剧情互相矛盾）
    · **实际（有校验）**：同样的输入经过校验后，越界程度恒为 0，代价是每轮要修正几处

所以我们同时跑两条线（同一批输入、同一套 schema）：

    naive_out_of_range : 不校验时状态**超出合法区间**的程度（0 = 合法；1 = 超出一倍）
    deviation          : 校验时"模型自报 vs 最终落库"的偏离度（0~1，衡量修正量）

外加每一档校验机制**各自的触发率**（漏输出 / 夹取 / 护栏 / 脏值 / 未知字段），
这样论文里既能说"校验把漂移压回 0"，也能说清"是哪一层在起作用、代价多大"。

==================== 三条硬约定 ====================
1. **确定性**：输入是脚本化的对话序列（`scripts/benchmark_data/drift_script.json`），
   没有任何随机数，跑两次结果一模一样（论文表格要能复现）。
2. **不调模型**：模拟里"模型输出"就是脚本给的原始状态块（红线：评测不花钱）。
3. **真实数据也能画**：落库的 `messages.state_meta_json` 里带着每轮的遥测
   （是否输出状态块、四类修正计数、偏离度），`--from-db` 直接按轮次窗口聚合即可 ——
   前提是那些轮次是在加了遥测之后产生的（历史数据没有，脚本会如实说明）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.narrative import state as state_mod
from app.narrative import state_schema as schema_mod

#: 默认每多少轮汇总一档（曲线上的一个点）
DEFAULT_BUCKET = 5

#: 遥测里的四类修正（顺序即表格列序）
KINDS = ("clamped", "guarded", "rejected", "unknown")


@dataclass
class RoundRecord:
    """一轮的状态遥测（离线模拟与真实数据共用同一个形状）。"""

    round_no: int
    required: bool = True
    had_block: bool = True
    malformed: bool = False
    counts: dict[str, int] = field(
        default_factory=lambda: {kind: 0 for kind in KINDS} | {"other": 0}
    )
    #: 自报 vs 校验后落库的偏离度（0~1）
    deviation: float = 0.0
    #: **反事实**：不校验时这一轮状态的越界程度（0 = 合法；None = 已经烂到算不出来，比如 hp 被写成文字）
    naive_out_of_range: float | None = None
    #: **反事实**：不校验时这一轮有没有把字段类型写坏（脏值被直接存进去）
    naive_garbage: bool = False
    fields_reported: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "required": self.required,
            "had_block": self.had_block,
            "malformed": self.malformed,
            "counts": dict(self.counts),
            "deviation": round(self.deviation, 4),
            "naive_out_of_range": (
                round(self.naive_out_of_range, 4)
                if self.naive_out_of_range is not None
                else None
            ),
            "naive_garbage": self.naive_garbage,
            "fields_reported": self.fields_reported,
        }


# ==================================================================
#  一、越界程度（反事实曲线的 y 值）
# ==================================================================
def meter_pair(
    state: dict[str, Any], name: str, max_field: str
) -> tuple[float | None, float | None]:
    """读出一个 meter 字段的 `(当前值, 上限)`，兼容嵌套写法，读不出就给 None。

    ★ 反事实侧必须容忍"字段被写成对象/文字"这种烂数据（那正是没有校验的后果）：
      早先直接 `_number(state.get(name))`，一遇到嵌套或脏值就整段跳过，
      曲线掉回 0，看起来像"不校验也没事"——完全误导。
    """
    value = state.get(name)
    if isinstance(value, dict):
        return state_mod._number(value.get("current")), state_mod._number(
            value.get("max")
        ) or state_mod._number(state.get(max_field))
    return state_mod._number(value), state_mod._number(state.get(max_field))


def _garbage(state: dict[str, Any], schema: dict[str, Any] | None) -> bool:
    """反事实状态里有没有"类型对不上声明"的字段（不校验就会这样存进去）。"""
    if schema_mod.is_empty(schema):
        return False
    for field in schema["fields"]:
        name = field["name"]
        if name not in state:
            continue
        value = state[name]
        ftype = field.get("type", "text")
        if ftype == "meter" and isinstance(value, dict):
            continue  # 嵌套写法本身是合法的
        if ftype in ("meter", "number"):
            if state_mod._number(value) is None and not isinstance(value, dict):
                return True
        elif ftype == "list":
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                return True
        elif ftype == "tuples":
            if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
                return True
        elif ftype == "flags":
            if not isinstance(value, dict):
                return True
        elif ftype == "text":
            if not isinstance(value, str):
                return True
    return False
def bounds_from(schema: dict[str, Any] | None, initial: dict[str, Any]) -> dict[str, float]:
    """从**初始状态**里定出各字段的合法上限（反事实曲线要拿它当标尺）。

    ★ 为什么不用状态自己的 max：模型要是把 `max` 也写飘了（比如 100 → 400），
      那不校验的一侧就"自己给自己放宽了区间"，漂移看起来反而消失了 ——
      实测踩过：`naive_out_of_range` 在第 15 轮之后掉回 0，图完全不可信。
      所以标尺必须固定在**故事开始时的声明值**上。
    """
    if schema_mod.is_empty(schema):
        return {}
    out: dict[str, float] = {}
    for field in schema["fields"]:
        name = field["name"]
        if field.get("type") == "meter":
            top = state_mod._number(initial.get(field.get("max_field") or "max"))
            if top and top > 0:
                out[name] = float(top)
        elif field.get("type") == "number":
            high = state_mod._number(field.get("max"))
            if high is not None:
                out[name] = float(high)
    return out


def out_of_range(
    state: dict[str, Any],
    schema: dict[str, Any] | None,
    *,
    bounds: dict[str, float] | None = None,
) -> float | None:
    """状态**超出合法区间**的程度：0 = 全部合法；1 = 平均超出上限一倍。

    只对"有明确合法区间"的字段算：
      · meter  → [0, 声明上限]（`bounds` 给的是故事开始时的上限）
      · number → [min, max]（schema 里声明了才管）
    列表/文本/映射没有"区间"概念，不参与（它们的漂移由 deviation 与脏值率体现）。

    ★ 返回 `None` = **一个可算的都没有**（比如 hp 已被写成文字）——
      这与"越界 0（其实很合法）"是两回事，混起来会让曲线骗人（分母里塞 0 会把均值拉低）。
    """
    if schema_mod.is_empty(schema):
        return None
    limits = bounds or {}
    scores: list[float] = []
    for field in schema["fields"]:
        name = field["name"]
        ftype = field.get("type", "text")
        if ftype == "meter":
            value, own_max = meter_pair(state, name, field.get("max_field") or "max")
            top = limits.get(name) or own_max or 0.0
            if value is None or top <= 0:
                continue
            if value > top:
                scores.append((value - top) / top)
            elif value < 0:
                scores.append(abs(value) / top)
            else:
                scores.append(0.0)
        elif ftype == "number":
            value = state_mod._number(state.get(name))
            low = state_mod._number(field.get("min"))
            high = limits.get(name, state_mod._number(field.get("max")) or 0.0)
            if value is None or (low is None and not high):
                continue
            if high and value > high:
                scores.append(abs(value - high) / max(abs(high), 1.0))
            elif low is not None and value < low:
                scores.append(abs(low - value) / max(abs(low), 1.0))
            else:
                scores.append(0.0)
    return sum(scores) / len(scores) if scores else None


# ==================================================================
#  二、离线模拟：同一批输入，跑"不校验"与"有校验"两条线
# ==================================================================
def _blind_apply(
    naive: dict[str, Any], raw: dict[str, Any], schema: dict[str, Any]
) -> dict[str, Any]:
    """**反事实**：不校验时状态会变成什么样 —— 模型写什么就存什么。"""
    out = dict(naive)
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if key == "__no_block__":
            continue
        out[key.strip()[:40]] = value
    return out


def _raw_for_round(
    round_no: int, spec: dict[str, Any], valid: dict[str, Any]
) -> dict[str, Any] | None:
    """按脚本生成"模型这一轮输出的状态块"（None = 这一轮模型没输出块）。

    ★ 基准是**校验后的状态**（`valid`），不是反事实状态 —— 因为真实系统每轮回注给模型的
      就是校验后的值，模型看到的是它、也倾向于照抄它。
      早先版本基于反事实状态生成，结果"模型的错误"被一轮轮复述，
      脏值率虚高到每轮 1.6 条（完全失真）。
    """
    drift = spec.get("drift") or {}
    every = int(drift.get("every") or 0)
    field_name = str(drift.get("field") or "")
    raw: dict[str, Any] = {k: v for k, v in valid.items()}
    if every > 0 and field_name and round_no % every == 0:
        current, _top = meter_pair(valid, field_name, str(drift.get("max_field") or "max"))
        if current is not None:
            raw[field_name] = current + float(drift.get("delta") or 0)
    event = (spec.get("events") or {}).get(str(round_no))
    if isinstance(event, dict):
        if event.get("__no_block__"):
            return None
        raw.update({k: v for k, v in event.items() if not str(k).startswith("__")})
    return raw


def simulate(spec: dict[str, Any]) -> list[RoundRecord]:
    """按脚本跑一遍，返回逐轮遥测（**确定性**，无随机数）。

    ★ **两条线各自闭环**（这一点必须说清楚，否则数字没意义）：
        · 实际（有校验）：模型每轮看到的是**校验后**的状态 → 它照抄它、偶尔写飘
        · 反事实（不校验）：模型看到的是**它自己上一轮写下的**状态 → 漂移会一轮轮累积
      没有护栏的世界里，模型会把 `hp: 125` 当成事实继续往上加；有护栏的世界里，
      它拿回的是被夹住的 `hp: 100`。所以两条线的**输入流**不同 ——
      这不是"同一批输入的两种后处理"，而是"两种系统各自演化"，曲线才有意义。
      （早先版本让两条线共用同一批输入，结果反事实只漂一步就被拉回来，图不可信。）
    """
    schema, _notes = schema_mod.parse_schema(spec.get("schema") or [])
    initial = dict(spec.get("initial") or {})
    bounds = bounds_from(schema, initial)
    valid = dict(initial)  # 有校验：真实落库的状态
    naive = dict(initial)  # 反事实：不校验、由模型自己滚出来的状态
    records: list[RoundRecord] = []
    rounds = int(spec.get("rounds") or 0)
    for round_no in range(1, rounds + 1):
        raw_actual = _raw_for_round(round_no, spec, valid)
        if raw_actual is None:
            records.append(RoundRecord(round_no=round_no, had_block=False))
            continue
        raw_naive = _raw_for_round(round_no, spec, naive) or raw_actual
        naive_next = _blind_apply(naive, raw_naive, schema)
        valid_next, notes = state_mod.normalize(raw_actual, valid, schema)
        records.append(
            RoundRecord(
                round_no=round_no,
                had_block=True,
                counts=state_mod.classify_notes(notes),
                deviation=state_mod.deviation(raw_actual, valid_next, schema),
                naive_out_of_range=out_of_range(naive_next, schema, bounds=bounds),
                naive_garbage=_garbage(naive_next, schema),
                fields_reported=len([k for k in raw_actual if not str(k).startswith("__")]),
            )
        )
        valid, naive = valid_next, naive_next
    return records


# ==================================================================
#  三、聚合：按轮次窗口汇总成曲线上的点
# ==================================================================
def bucket(records: list[RoundRecord], size: int = DEFAULT_BUCKET) -> list[dict[str, Any]]:
    """按"每 size 轮"汇总：每个窗口给一组率与均值（曲线的数据点）。"""
    if size <= 0:
        size = DEFAULT_BUCKET
    out: list[dict[str, Any]] = []
    for start in range(0, len(records), size):
        window = [r for r in records[start : start + size] if r.required]
        if not window:
            continue
        total = len(window)
        counts = {kind: sum(r.counts.get(kind, 0) for r in window) for kind in KINDS}
        # ★ 越界程度只在"算得出来"的轮次上取平均（算不出来的那些由脏值率体现），
        #   否则分母里塞 0 会把均值拉低，看起来像"不校验也没那么糟"。
        oor = [r.naive_out_of_range for r in window if r.naive_out_of_range is not None]
        out.append(
            {
                "bucket": f"{window[0].round_no}-{window[-1].round_no}",
                "rounds": total,
                "missing_rate": sum(1 for r in window if not r.had_block) / total,
                "deviation_mean": sum(r.deviation for r in window) / total,
                "naive_out_of_range_mean": (sum(oor) / len(oor)) if oor else 0.0,
                "naive_out_of_range_rounds": len(oor),
                "naive_garbage_rate": sum(1 for r in window if r.naive_garbage) / total,
                "deviation_max": max(r.deviation for r in window),
                "naive_out_of_range_max": max(oor) if oor else 0.0,
                **{f"{kind}_rate": counts[kind] / total for kind in KINDS},
                **{f"{kind}_total": counts[kind] for kind in KINDS},
            }
        )
    return out


def summary(records: list[RoundRecord]) -> dict[str, Any]:
    """整段对话的总计（论文摘要里那几个数字）。"""
    required = [r for r in records if r.required]
    total = len(required) or 1
    counts = {kind: sum(r.counts.get(kind, 0) for r in required) for kind in KINDS}
    oor = [r.naive_out_of_range for r in required if r.naive_out_of_range is not None]
    return {
        "rounds": len(records),
        "rounds_required": len(required),
        "missing_blocks": sum(1 for r in required if not r.had_block),
        "missing_rate": sum(1 for r in required if not r.had_block) / total,
        "corrected_round_rate": sum(
            1 for r in required if any(r.counts.get(kind, 0) for kind in KINDS)
        )
        / total,
        "deviation_mean": sum(r.deviation for r in required) / total,
        "naive_out_of_range_mean": (sum(oor) / len(oor)) if oor else 0.0,
        "naive_out_of_range_rounds": len(oor),
        "naive_out_of_range_max": max(oor) if oor else 0.0,
        "naive_garbage_rounds": sum(1 for r in required if r.naive_garbage),
        "naive_garbage_rate": sum(1 for r in required if r.naive_garbage) / total,
        **{f"{kind}_total": counts[kind] for kind in KINDS},
    }


# ==================================================================
#  四、出图：零依赖内联 SVG
# ==================================================================
#: 曲线颜色（与项目主题色一致：一条是"没护栏"，一条是"有护栏"）
SERIES_COLORS = ("#d94a5a", "#2f9e6e", "#3b6fd4", "#c98a2b")


def to_svg(
    series: dict[str, list[tuple[float, float]]],
    *,
    width: int = 720,
    height: int = 320,
    title: str = "",
    x_label: str = "轮次",
    y_label: str = "",
    y_max: float | None = None,
) -> str:
    """把若干条曲线画成一张 SVG（**不依赖任何绘图库**，控制台/文档都能直接看）。

    参数 `series`：`{曲线名: [(x, y), ...]}`。x 轴是轮次，y 轴按最大 y 自动定标
    （`y_max` 显式给了就用它，便于多条曲线共用同一标尺）。
    """
    pad_left, pad_right, pad_top, pad_bottom = 56, 130, 34, 40
    plot_w = max(width - pad_left - pad_right, 10)
    plot_h = max(height - pad_top - pad_bottom, 10)
    all_points = [point for points in series.values() for point in points]
    max_x = max((x for x, _ in all_points), default=1.0) or 1.0
    top = y_max if y_max is not None else max((y for _, y in all_points), default=1.0)
    top = top or 1.0

    def sx(x: float) -> float:
        return pad_left + (x / max_x) * plot_w

    def sy(y: float) -> float:
        return pad_top + plot_h - (min(y, top) / top) * plot_h

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{pad_left}" y="20" font-size="14" font-weight="600" fill="#222">{_esc(title)}</text>',
        # 坐标轴
        f'<line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{pad_top + plot_h}" stroke="#888"/>',
        f'<line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{pad_left + plot_w}" '
        f'y2="{pad_top + plot_h}" stroke="#888"/>',
    ]
    # y 轴刻度（0 / 25% / 50% / 75% / 100%）
    for ratio in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = pad_top + plot_h - ratio * plot_h
        parts.append(
            f'<line x1="{pad_left}" y1="{y:.1f}" x2="{pad_left + plot_w}" y2="{y:.1f}" '
            f'stroke="#eee"/>'
        )
        parts.append(
            f'<text x="{pad_left - 8}" y="{y + 4:.1f}" font-size="11" fill="#666" '
            f'text-anchor="end">{ratio * top:.2f}</text>'
        )
    parts.append(
        f'<text x="{pad_left + plot_w / 2:.0f}" y="{height - 10}" font-size="11" '
        f'fill="#666" text-anchor="middle">{_esc(x_label)}</text>'
    )
    if y_label:
        parts.append(
            f'<text x="14" y="{pad_top + plot_h / 2:.0f}" font-size="11" fill="#666" '
            f'transform="rotate(-90 14 {pad_top + plot_h / 2:.0f})" text-anchor="middle">'
            f"{_esc(y_label)}</text>"
        )
    # 曲线 + 图例
    for index, (name, points) in enumerate(series.items()):
        color = SERIES_COLORS[index % len(SERIES_COLORS)]
        if not points:
            continue
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{sx(x):.1f},{sy(y):.1f}" for i, (x, y) in enumerate(points)
        )
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/>')
        for x, y in points:
            parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.5" fill="{color}"/>')
        legend_y = pad_top + 6 + index * 20
        parts.append(
            f'<line x1="{width - pad_right + 10}" y1="{legend_y}" '
            f'x2="{width - pad_right + 34}" y2="{legend_y}" stroke="{color}" stroke-width="2"/>'
        )
        parts.append(
            f'<text x="{width - pad_right + 40}" y="{legend_y + 4}" font-size="11" '
            f'fill="#333">{_esc(name)}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ==================================================================
#  五、真实数据：从落库遥测聚合
# ==================================================================
def records_from_db(*, usernames: list[str] | None = None, limit: int = 5000) -> tuple[list[RoundRecord], dict[str, Any]]:
    """从 `messages.state_meta_json` 读出逐轮遥测（**只 SELECT，绝不写**）。

    返回 `(records, meta)`；`meta` 里说明"有多少轮是加了遥测之后产生的" ——
    历史消息没有这个字段（加遥测之前落的），脚本必须如实说明而不是假装有数据。
    """
    from sqlalchemy import select

    from app.db.models import Message, NarrativeSession, User
    from app.db.mysql import session_scope

    records: list[RoundRecord] = []
    meta: dict[str, Any] = {"scanned": 0, "with_telemetry": 0, "required": 0, "skipped_legacy": 0}
    with session_scope() as db:
        query = (
            select(Message)
            .join(NarrativeSession, NarrativeSession.id == Message.session_id)
            .where(Message.role == "assistant")
            .order_by(Message.id)
            .limit(max(limit, 1))
        )
        if usernames:
            user_ids = list(db.scalars(select(User.id).where(User.username.in_(usernames))))
            query = query.where(NarrativeSession.user_id.in_(user_ids))
        for message in db.scalars(query):
            meta["scanned"] += 1
            raw = getattr(message, "state_meta_json", None)
            if not raw:
                meta["skipped_legacy"] += 1
                continue
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                meta["skipped_legacy"] += 1
                continue
            meta["with_telemetry"] += 1
            if not payload.get("required", True):
                continue
            meta["required"] += 1
            records.append(
                RoundRecord(
                    round_no=len(records) + 1,
                    required=True,
                    had_block=bool(payload.get("had_block", True)),
                    malformed=bool(payload.get("malformed", False)),
                    counts={
                        kind: int((payload.get("counts") or {}).get(kind, 0)) for kind in KINDS
                    }
                    | {"other": int((payload.get("counts") or {}).get("other", 0))},
                    deviation=float(payload.get("deviation") or 0.0),
                    # 真实数据没有"反事实"那一侧（历史只有一个分支）→ 0，图上如实标注
                    naive_out_of_range=0.0,
                    fields_reported=int(payload.get("fields_reported") or 0),
                )
            )
    return records, meta
