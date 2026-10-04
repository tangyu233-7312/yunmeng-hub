"""记忆锚点：用户亲手写下的「永远要记住」的条目（固定注入、永不被折叠）。

==================== 它和别的记忆有什么区别 ====================
| 东西 | 谁写的 | 会不会被压缩/丢弃 |
|---|---|---|
| 世界书条目 | 角色卡作者 | 按关键词触发，可能本轮不注入 |
| 长期记忆（向量） | 系统自动记 | 按相似度召回，可能召回不到 |
| **剧情总结** | 模型写（用户可改） | 会被下一次合并**替换**（旧的被折叠掉） |
| **记忆锚点** | **用户手写** | **永远整条注入，永不折叠、永不被挤掉** |

所以锚点是"硬设定"的兜底：比如"主角是女性""绝不能承认自己是 AI""这个世界没有魔法"。
它跟世界书一样是用户/作者意志，优先级高于回忆与总结，所以我们把它排在
「世界设定」之后、「回忆」之前（见 prompt_builder 的装配顺序）。

==================== 三条硬规则 ====================
1. **上限**：最多 `MAX_ITEMS` 条、合计最多 `MAX_CHARS` 字（单条最多 `MAX_ONE_CHARS`）。
   超了直接拒绝并告诉用户超在哪 —— 悄悄截断会让用户以为存住了。
2. **绝不静默丢弃**：坏数据（不是字符串、空白）一律剔除并如实计数，不猜不编。
3. **注入位置固定**：拼进**系统提示词**（不是对话历史），因此上下文裁剪不会动它 ——
   这正是它和"回忆"最本质的区别。
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

#: 最多几条（面板上写的就是这个数：0/5）
MAX_ITEMS = 5
#: 合计字数上限（面板上写 0/2000 字）
MAX_CHARS = 2000
#: 单条上限（避免一条就把额度吃光）
MAX_ONE_CHARS = 500

#: 注入时的小节标题
BLOCK_TITLE = "## 记忆锚点（必须始终遵守）"
BLOCK_DISCLAIMER = "（以下是用户/作者手写的硬设定，优先级高于回忆与剧情总结；不要与之矛盾。）"


def _clean(items: Any) -> list[str]:
    """规范化一份锚点清单：去空白、丢空条目、去重（保序）。"""
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split()).strip()
        if not text or text in out:
            continue
        out.append(text[:MAX_ONE_CHARS])
    return out


def validate(items: Any) -> tuple[bool, str, list[str]]:
    """校验并返回 `(是否合法, 说明, 规范化后的清单)`。

    ★ 超限一律**拒绝**而不是截断：用户写了 6 条就该看到"最多 5 条"，
      而不是存进去 5 条、下次打开发现少了一条。
    """
    cleaned = _clean(items)
    source = [i for i in (items or []) if isinstance(i, str) and i.strip()] if isinstance(items, list) else []
    deduped = len({i for i in source}) if source else 0
    if len(cleaned) > MAX_ITEMS:
        return False, f"最多只能加 {MAX_ITEMS} 条锚点（现在 {len(cleaned)} 条）", cleaned
    total = sum(len(i) for i in cleaned)
    if total > MAX_CHARS:
        return False, f"锚点合计最多 {MAX_CHARS} 字（现在 {total} 字）", cleaned
    note = ""
    if deduped and deduped > len(cleaned):
        note = "已自动去掉重复/空白的条目"
    return True, note, cleaned


def load(session: Any) -> list[str]:
    """读出这条会话的锚点（坏了就当空，绝不抛异常）。"""
    raw = getattr(session, "memory_anchors_json", None)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "记忆锚点不是合法 JSON，已按空处理 | session_id={}", getattr(session, "id", None)
        )
        return []
    return _clean(value)


def save(session: Any, items: Any) -> tuple[bool, str]:
    """校验并写回（**不 commit**）。返回 `(是否成功, 说明)`。"""
    ok, message, cleaned = validate(items)
    if not ok:
        return False, message
    session.memory_anchors_json = json.dumps(cleaned, ensure_ascii=False)
    logger.info(
        "记忆锚点已更新 | session_id={} 条数={} 字数={}",
        getattr(session, "id", None),
        len(cleaned),
        sum(len(i) for i in cleaned),
    )
    return True, message or "锚点已保存"


def render_block(session: Any) -> str:
    """渲染成提示词里的一小节（没有锚点就返回空串，不占一个空标题）。"""
    items = load(session)
    if not items:
        return ""
    lines = [f"- {item}" for item in items]
    return f"{BLOCK_TITLE}\n{BLOCK_DISCLAIMER}\n" + "\n".join(lines)


def state(session: Any) -> dict[str, Any]:
    """给「记忆管理面板」用的状态（含上限，前端据此显示 0/5 · 0/2000 字）。"""
    items = load(session)
    total = sum(len(i) for i in items)
    return {
        "items": items,
        "count": len(items),
        "chars": total,
        "max_items": MAX_ITEMS,
        "max_chars": MAX_CHARS,
        "max_one_chars": MAX_ONE_CHARS,
        "remaining_items": max(MAX_ITEMS - len(items), 0),
        "remaining_chars": max(MAX_CHARS - total, 0),
    }
