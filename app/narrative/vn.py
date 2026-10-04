"""角色卡 VN 模式：立绘 + 背景 + **表情跟着状态栏变**。

==================== 它解决什么问题 ====================
角色卡作者想做"视觉小说"式的表现（背景 + 立绘 + 对话框），
但绝大多数卡只有文字人设。本项目不引入图片生成，也不放开 JS，
所以走**声明式**：作者在卡的 `extensions.hne.vn` 里声明

    {
      "enabled": true,
      "background": "https://…/classroom.png",
      "sprites": {"平静": "https://…/calm.png", "生气": "https://…/angry.png"},
      "expression_field": "mood",
      "default": "平静",
      "position": "center"
    }

==================== 为什么表情必须挂在**状态栏**上 ====================
关键设计：表情**不是**新协议、也不是第二套标签，而是**状态栏里的一个字段**
（默认叫 `mood`）。理由有三条：

1. 模型已经每轮输出 `<state>` 了（第六轮起字段由卡定义），
   多一个 `mood` 字段**不需要任何新协议**，也不会多一次模型调用；
2. 立绘切换与文字剧情天然同步 —— 状态变了，图就变了，不会出现
   "嘴上说生气、脸上还在笑"；
3. 与第六轮那条"状态栏字段由卡/世界书说话"的规矩一致：
   作者声明了哪些表情，我们就把它写进状态栏字段的**可选值列表**里，
   模型于是知道"可以填平静 / 生气 / 开心"，而不是瞎写一个没有立绘的词。

若作者没在 schema 里定义这个字段，建会话时会**自动补一个文本字段**（见
`ensure_expression_field`）—— 否则立绘永远不会变，作者会以为功能坏了。

==================== 安全与体积 ====================
图片地址只接受 `http(s)://` 与 `data:image/(png|jpeg|webp|gif);base64,`：
- `javascript:` / `file:` / `blob:` 一律拒绝（界面里会进 `<img src>`）；
- `data:image/svg+xml` 也拒绝：SVG 能带脚本，虽然 `<img>` 里多数浏览器不执行，
  但"能不能执行"不该取决于浏览器版本 —— 本项目对这条零容忍。
- 单条地址 ≤ 300KB 字符（内嵌 base64 图用），最多 24 张立绘。

**坏图不废卡**：某一对立绘的地址不合法 → 只丢那一条并记一条 warning，
其余照用（与"一条世界书条目写坏不该让整张卡不能用"同一取舍）。
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

SPEC = "hne_vn_v1"
#: 卡的私有命名空间：`extensions.hne.vn`
EXTENSION_PARENT = "hne"
EXTENSION_KEY = "vn"

MAX_SPRITES = 24
#: 单条图片地址的长度上限（内嵌 base64 用；立绘通常几十~几百 KB）
MAX_URL_CHARS = 300_000
MAX_NAME_CHARS = 40
MAX_FIELD_CHARS = 24
POSITIONS = ("left", "center", "right")
DEFAULT_POSITION = "center"
DEFAULT_FIELD = "mood"

#: 允许的内嵌图类型（★ 刻意不含 svg+xml：它可能带脚本）
_DATA_PREFIXES = (
    "data:image/png;base64,",
    "data:image/jpeg;base64,",
    "data:image/jpg;base64,",
    "data:image/webp;base64,",
    "data:image/gif;base64,",
)
_NAME_RE = re.compile(r"^[\w\u3400-\u9fff\u3040-\u30ff-]{1,24}$")


def extension_of(card: Any) -> dict[str, Any]:
    """取卡的 `extensions.hne.vn`（没有就空 dict）。"""
    extra = getattr(card, "extra_data", None)
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            extra = None
    if not isinstance(extra, dict):
        return {}
    extensions = extra.get("extensions")
    if not isinstance(extensions, dict):
        return {}
    hne = extensions.get(EXTENSION_PARENT)
    if not isinstance(hne, dict):
        return {}
    raw = hne.get(EXTENSION_KEY)
    return raw if isinstance(raw, dict) else {}


def check_url(value: Any) -> tuple[bool, str]:
    """图片地址是否可用。返回 `(可用, 不能用时的中文原因)`。"""
    text = str(value or "").strip()
    if not text:
        return False, "地址是空的"
    if len(text) > MAX_URL_CHARS:
        return False, f"地址太长（上限 {MAX_URL_CHARS} 字符）"
    lowered = text.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return True, ""
    if lowered.startswith("data:"):
        if lowered.startswith(_DATA_PREFIXES):
            return True, ""
        if lowered.startswith("data:image/svg"):
            return False, "不支持 SVG（可能带脚本）；请用 png / jpeg / webp / gif"
        return False, "只支持 data:image/(png|jpeg|webp|gif);base64 形式的内嵌图"
    return False, "只支持 http(s) 地址或 data:image 内嵌图"


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """把 `extensions.hne.vn` 校验成可信配置。返回 `(配置, warnings)`。

    ★ 与插件的 `validate_config` 同一个哲学：**坏数据降级 + 如实说明**，
      而不是整块拒绝（一张卡的立绘写坏一条，不该让这张卡不能用）。
    """
    notes: list[str] = []
    data = raw if isinstance(raw, dict) else {}

    # ---------------- 立绘：对象或数组两种写法都收 ----------------
    raw_sprites = data.get("sprites") or data.get("expressions") or {}
    pairs: list[tuple[Any, Any]] = []
    if isinstance(raw_sprites, dict):
        pairs = list(raw_sprites.items())
    elif isinstance(raw_sprites, list):
        for item in raw_sprites:
            if isinstance(item, dict):
                pairs.append((item.get("name") or item.get("expression"), item.get("url")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
    elif raw_sprites:
        notes.append("立绘（sprites）要写成「表情名 → 图片地址」的对象或 [{name,url}] 数组，已忽略")

    sprites: dict[str, str] = {}
    for name, url in pairs:
        key = _text(name, MAX_NAME_CHARS)
        if not key:
            continue
        if not str(url or "").strip():
            continue
        if key in sprites:
            continue
        if len(sprites) >= MAX_SPRITES:
            notes.append(f"立绘最多 {MAX_SPRITES} 张，多余的已忽略")
            break
        ok, reason = check_url(url)
        if not ok:
            notes.append(f"立绘「{key}」没生效：{reason}")
            continue
        sprites[key] = str(url).strip()

    # ---------------- 背景 ----------------
    background = ""
    raw_background = data.get("background")
    if str(raw_background or "").strip():
        ok, reason = check_url(raw_background)
        if ok:
            background = str(raw_background).strip()
        else:
            notes.append(f"背景图没生效：{reason}")

    # ---------------- 其它 ----------------
    enabled = data.get("enabled")
    enabled = bool(sprites or background) if enabled is None else bool(enabled)

    field = _text(data.get("expression_field") or data.get("field") or DEFAULT_FIELD, MAX_FIELD_CHARS)
    if not _NAME_RE.match(field or ""):
        notes.append(f"表情字段名「{field}」不合法，已改回 {DEFAULT_FIELD}")
        field = DEFAULT_FIELD

    keys = list(sprites)
    default_expression = _text(data.get("default"), MAX_NAME_CHARS)
    if default_expression not in sprites:
        if default_expression:
            notes.append(
                f"默认表情「{default_expression}」没有对应立绘，已按第一张处理"
            )
        default_expression = "平静" if "平静" in sprites else (keys[0] if keys else "")

    position = _text(data.get("position") or DEFAULT_POSITION, 12).lower()
    if position not in POSITIONS:
        if position:
            notes.append(f"立绘位置「{position}」不认识，已用居中")
        position = DEFAULT_POSITION

    config = {
        "spec": SPEC,
        "enabled": enabled,
        "background": background,
        "sprites": sprites,
        "expression_field": field,
        "default_expression": default_expression,
        "position": position,
        "show_name": bool(data.get("show_name", True)),
        "name": _text(data.get("name"), MAX_NAME_CHARS) or "",
        "sprite_scale": _clamp(data.get("sprite_scale"), 0.3, 2.0, 1.0),
        "background_dim": _clamp(data.get("background_dim"), 0, 80, 20),
    }
    if enabled and not sprites and not background:
        notes.append("开了 VN 模式，但既没有背景也没有立绘（舞台上会是空的）")
    return config, notes


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, round(number, 2)))


def is_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("enabled")) and bool(
        config.get("sprites") or config.get("background")
    )


# ==================================================================
#  当前该显示哪张立绘
# ==================================================================
def expression_of(config: dict[str, Any], state: dict[str, Any] | None) -> str:
    """从状态栏里读当前表情（读不到就用默认表情）。"""
    field = str(config.get("expression_field") or DEFAULT_FIELD)
    raw = (state or {}).get(field)
    if isinstance(raw, (list, dict)):
        raw = ""
    text = _text(raw, MAX_NAME_CHARS)
    return text or str(config.get("default_expression") or "")


def sprite_url(config: dict[str, Any], expression: str) -> str | None:
    """按表情取立绘地址（大小写/空格不敏感；取不到就退回默认表情）。"""
    sprites = config.get("sprites") or {}
    if not isinstance(sprites, dict) or not sprites:
        return None
    key = _text(expression, MAX_NAME_CHARS)
    if key in sprites:
        return str(sprites[key])
    folded = {str(name).strip().lower(): url for name, url in sprites.items()}
    if key.lower() in folded:
        return str(folded[key.lower()])
    fallback = str(config.get("default_expression") or "")
    if fallback in sprites:
        return str(sprites[fallback])
    if fallback.lower() in folded:
        return str(folded[fallback.lower()])
    return str(next(iter(sprites.values())))


def stage(card: Any, state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """给接口用的舞台数据（没开 VN 模式就返回 None）。

    ★ 由**后端**算好"现在该显示哪张图"，前端只负责画：
      这样"表情 → 立绘"的规则只有一份，测试与探针都能直接断言，
      也不会出现"前端各写一套映射、两边对不上"。
    """
    config, notes = normalize(extension_of(card))
    if not is_enabled(config):
        return None
    expression = expression_of(config, state)
    url = sprite_url(config, expression)
    sprites = config.get("sprites") or {}
    if expression and sprites and url is not None:
        direct = expression in sprites or expression.lower() in {
            str(k).strip().lower() for k in sprites
        }
        if not direct:
            notes.append(
                f"状态里的表情「{expression}」没有对应立绘，已退回"
                f"「{config.get('default_expression') or '第一张'}」"
            )
    return {
        "spec": SPEC,
        "enabled": True,
        "background": config.get("background") or None,
        "sprite_url": url,
        "expression": expression,
        "default_expression": config.get("default_expression") or "",
        "expressions": list(sprites),
        "expression_field": config.get("expression_field") or DEFAULT_FIELD,
        "position": config.get("position") or DEFAULT_POSITION,
        "show_name": bool(config.get("show_name", True)),
        "name": config.get("name") or str(getattr(card, "name", "") or ""),
        "sprite_scale": config.get("sprite_scale", 1.0),
        "background_dim": config.get("background_dim", 20),
        "warnings": notes,
    }


# ==================================================================
#  建会话时：把表情字段补进状态栏
# ==================================================================
def ensure_expression_field(schema: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """确保状态栏里有"表情"字段（没有就补一个，并写明可选值）。

    返回要记进日志的说明（不是错误）。**就地修改** `schema`。
    """
    if not is_enabled(config):
        return []
    sprites = list((config.get("sprites") or {}).keys())
    if not sprites:
        return []
    field = str(config.get("expression_field") or DEFAULT_FIELD)
    fields = schema.get("fields")
    if not isinstance(fields, list):
        fields = []
        schema["fields"] = fields
    for item in fields:
        if isinstance(item, dict) and str(item.get("name")) == field:
            return []  # 作者自己定义过了，尊重他的定义（标签/描述都按他的来）
    fields.append(
        {
            "name": field,
            "label": "表情",
            "type": "text",
            "description": (
                "当前表情；可选值：" + " / ".join(sprites) + "（决定显示哪张立绘）"
            ),
        }
    )
    return [
        f"角色卡开了 VN 模式，已往状态栏补一个「{field}」字段（可选值："
        + " / ".join(sprites)
        + "），模型每轮会一起输出它，立绘跟着它换。"
    ]


def describe(config: dict[str, Any]) -> str:
    """一句话摘要（插件页那种"卡片上写清楚"的口径）。"""
    sprites = config.get("sprites") or {}
    if not is_enabled(config):
        return "未启用（没配背景与立绘）"
    bits = []
    if config.get("background"):
        bits.append("有背景")
    if sprites:
        bits.append(f"{len(sprites)} 张立绘：{' / '.join(list(sprites)[:6])}")
    bits.append(f"表情字段 {config.get('expression_field') or DEFAULT_FIELD}")
    return " · ".join(bits)


def merge_into_extensions(extensions: Any, raw: dict[str, Any]) -> dict[str, Any]:
    """把校验后的配置写回 `extensions.hne.vn`（保留其它命名空间原样）。

    ★ 为什么要在**写入时**归一化：这份配置会进 `<img src>`。
      脏数据（`javascript:`、超长 base64）不该等到渲染时才被前端过滤 ——
      存进去的就应该是干净的（与角色卡富文本、插件 CSS 同一套规矩）。
    """
    result = copy.deepcopy(extensions) if isinstance(extensions, dict) else {}
    hne = result.get(EXTENSION_PARENT)
    hne = copy.deepcopy(hne) if isinstance(hne, dict) else {}
    stored = {key: value for key, value in raw.items() if key != "spec"}
    stored["spec"] = SPEC
    hne[EXTENSION_KEY] = stored
    result[EXTENSION_PARENT] = hne
    return result
