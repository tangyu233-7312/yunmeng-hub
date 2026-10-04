"""状态栏的**字段定义**（schema）从哪里来、怎么校验、怎么讲给模型听。

==================== 它解决什么问题 ====================
在加这个模块之前，状态是**全套写死**的：`app/narrative/state.py` 里
`KNOWN_FIELDS = ("hp", "inventory", "location", "quests", "flags")`，
提示词里明说"字段固定为 hp{current,max}…"，前端也硬编码画一条 HP 条。

结果是：**灯塔守夜人**（我写的演示卡）在 `initial_state` 里放了 HP，
状态栏就显示 HP；而**魔法少女 / 魔女裁判**那张卡从头到尾没声明过 HP，
状态栏**照样**显示 `HP 100/100` —— 因为代码用通用那套兜底了。
用户的原话："这是一个错误的设计。状态栏应该在角色卡里读取
（大部分作者是把状态栏格式和要求放在世界书里面）。"

所以本模块把"有哪些字段"从代码里搬到**卡 / 世界书**里，按优先级解析：

    ★ 唯一权威顺序（user 拍板，勿改）：
      ① 卡 `extensions.hne.state_schema`      —— 显式声明，最权威
      ② 卡关联世界书里的**约定式条目**          —— 作者最常用的位置
      ③ 从卡 `extensions.hne.initial_state` 的顶层键推断
         （HP 只在这张卡真的写了 hp 时才出现 —— 这正是修掉"灯塔的 HP
           跑到魔法少女身上"的关键）
      ④ 都没有 ⇒ **空 schema**：不注入状态协议、不解析 <state>、状态栏
         不画 HP，只如实说明"该卡未定义状态栏格式"

==================== 世界书里怎么写（约定）====================
两种都认，任选其一：

    条目名以 `[状态栏]` 开头        —— 如 "[状态栏] 魔女裁判状态"
    或 `keys` 里含 `state_definition` —— 给别的工具导出的世界书用

条目**正文**里放一个示例（约定与最终输出一致，所以直接抄）：

    [状态栏]
    每轮末尾输出 <state>…</state>，字段：魔力（0~100）、变身状态、携带物。
    <state>{"魔力": 80, "变身状态": "已变身", "携带物": ["魔杖"]}</state>

正文里的示例 JSON 决定**字段清单**（顺序也照它），正文本身会原样
进提示词 —— 作者想怎么描述规则就怎么描述，不用迁就我们的措辞。
这类条目**不再当普通设定注入**（否则同一段话会进提示词两遍，而且
那段里带着 `<state>` 示例，可能让模型把示例当正文抄出来）。

==================== schema 的形状 ====================
    {"spec": "hne_state_v1",
     "fields": [{"name": "hp", "label": "HP", "type": "meter",
                 "max_field": "max", "unit": "", "icon": "", "description": ""}]}

字段 name 就是 `<state>` JSON 里的键名（flat 语义：meter 的 current 在
name 上、上限在 max_field 上，所以 `{"hp": 88, "max": 100}`）。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from loguru import logger

#: schema 版本号（写进落库的 JSON，将来改结构时好判断）
SPEC = "hne_state_v1"

#: 支持的字段类型
#:   text   文本      {"location": "钟楼"}
#:   number 数值      {"金币": 12}
#:   meter  条（current/max 扁平） {"hp": 88, "max": 100}
#:   list   字符串数组 {"inventory": ["提灯"]}
#:   tuples 对象数组   {"quests": [{"title": "…", "status": "active"}]}
#:   flags  键值对象   {"flags": {"灯油": "半桶"}}
FIELD_TYPES = ("text", "number", "meter", "list", "tuples", "flags")

#: 世界书里的约定：条目名以它开头，或 keys 里含它
BOOK_ENTRY_PREFIX = "[状态栏]"
BOOK_ENTRY_KEY = "state_definition"

#: 单条字段上限（防止有人塞几千个字段把提示词撑爆）
MAX_FIELDS = 24
MAX_LABEL = 24
MAX_DESCRIPTION = 200

#: 字段名：允许中日韩字（作者爱用"魔力""灯油"），排除空白与标点，
#: 因为这个名字会进 JSON 键、进提示词、也被前端当 data 属性用
_NAME_RE = re.compile(r"^[\w\u3400-\u9fff\u3040-\u30ff-]{1,24}$")

#: 从世界书条目正文里取示例 JSON：与 state.py 一样取**最后**一个块
_EXAMPLE_RE = re.compile(r"<state>\s*(\{.*?\})\s*</state>", re.S | re.I)

#: 旧五字段（**只在兼容旧会话/旧卡时使用**，不再作为新会话的默认）
LEGACY_FIELDS: tuple[str, ...] = ("hp", "inventory", "location", "quests", "flags")

#: 旧五字段的标签与图标（兼容渲染用，与老界面保持一致）
LEGACY_META: dict[str, dict[str, str]] = {
    "hp": {"label": "HP", "icon": "❤", "max_field": "max"},
    "location": {"label": "位置", "icon": "📍"},
    "inventory": {"label": "背包", "icon": "🎒"},
    "quests": {"label": "任务", "icon": "📜"},
    "flags": {"label": "标记", "icon": "🏷"},
}


# ==================================================================
#  基础工具
# ==================================================================
def _text(value: Any, limit: int) -> str:
    return str(value).strip()[:limit]


def _finite(value: Any) -> float | None:
    """只认真正的有限数字（bool 不算，NaN / inf 不算）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    num = float(value)
    return num if math.isfinite(num) else None


def is_empty(schema: dict[str, Any] | None) -> bool:
    """空 schema = 这张卡没定义状态栏（调用方据此不注入协议、不画状态栏）。"""
    return not (isinstance(schema, dict) and schema.get("fields"))


def field_names(schema: dict[str, Any] | None) -> list[str]:
    if is_empty(schema):
        return []
    return [str(f.get("name")) for f in schema["fields"]]


def field_map(schema: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if is_empty(schema):
        return {}
    return {str(f.get("name")): f for f in schema["fields"]}


def get_field(schema: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    return field_map(schema).get(name)


# ==================================================================
#  解析 / 校验
# ==================================================================
def _normalize_field(raw: dict[str, Any]) -> dict[str, Any] | None:
    """把作者写的一个字段条目校验成规范形状；不合法返回 None（调用方记 note）。"""
    if not isinstance(raw, dict):
        return None
    name = _text(raw.get("name") or raw.get("key") or raw.get("字段") or "", 24)
    if not name or not _NAME_RE.match(name):
        return None
    ftype = _text(raw.get("type") or raw.get("类型") or "", 16).lower()
    if ftype not in FIELD_TYPES:
        ftype = "text"
    out: dict[str, Any] = {
        "name": name,
        "label": _text(raw.get("label") or raw.get("标签") or name, MAX_LABEL) or name,
        "type": ftype,
    }
    description = _text(raw.get("description") or raw.get("desc") or raw.get("说明") or "", MAX_DESCRIPTION)
    if description:
        out["description"] = description
    unit = _text(raw.get("unit") or raw.get("单位") or "", 12)
    if unit:
        out["unit"] = unit
    icon = _text(raw.get("icon") or raw.get("图标") or "", 8)
    if icon:
        out["icon"] = icon
    max_field = _text(raw.get("max_field") or raw.get("上限字段") or "", 24)
    if ftype == "meter":
        out["max_field"] = max_field if _NAME_RE.match(max_field) else "max"
    # 数值字段的上下限（可选）。★ 必须原样保留：state.normalize 里 `_clean_number`
    # 就是靠它把"自造的数值字段"夹到边界；不保留的话声明了也不会生效
    # （这个坑是 benchmark 的状态用例抓出来的：标注"应当被夹取"而实际没夹）。
    low = _finite(raw.get("min"))
    high = _finite(raw.get("max"))
    if low is not None and high is not None and low > high:
        low = high = None
    if low is not None:
        out["min"] = int(low) if float(low).is_integer() else round(low, 2)
    if high is not None:
        out["max"] = int(high) if float(high).is_integer() else round(high, 2)
    # 数值/条的初始值（可选，方便界面给骨架）
    for key, kind in (("initial", "number"), ("initial_current", "number"), ("initial_max", "number")):
        num = _finite(raw.get(key))
        if num is not None:
            out[key] = int(num) if float(num).is_integer() else round(num, 2)
    return out


def parse_schema(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """把作者给的字段清单校验成规范 schema。返回 `(schema, notes)`。

    容错优先（与 state.normalize 同一套哲学）：看不懂的字段直接丢弃并记 note，
    **绝不抛异常** —— 作者写歪一格不该让整张卡的状态栏消失。
    """
    notes: list[str] = []
    if raw is None or raw == "" or raw == {} or raw == []:
        return {"spec": SPEC, "fields": []}, notes

    items: Any = raw
    if isinstance(raw, dict):
        # 兼容 {"fields": [...]} / {"状态栏": [...]} / {"hp": {...}, "location": {...}}（按名推断）
        for key in ("fields", "state_schema", "状态栏", "field_list"):
            if isinstance(raw.get(key), list):
                items = raw[key]
                break
        else:
            items = []
            for key, value in raw.items():
                if isinstance(value, dict):
                    merged = {"name": key, **value}
                elif value in FIELD_TYPES or (isinstance(value, str) and value):
                    merged = {"name": key, "type": str(value).lower()}
                else:
                    continue
                items.append(merged)
    if not isinstance(items, list):
        return {"spec": SPEC, "fields": []}, ["状态栏格式不是数组也不是对象，已忽略"]

    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        field = _normalize_field(item) if isinstance(item, dict) else None
        if field is None:
            notes.append(f"状态栏格式里有一条字段定义不合法，已丢弃：{str(item)[:40]}")
            continue
        if field["name"] in seen:
            notes.append(f"状态栏格式里有重复字段「{field['name']}」，只保留第一个")
            continue
        seen.add(field["name"])
        fields.append(field)
        if len(fields) >= MAX_FIELDS:
            notes.append(f"状态栏字段过多，只保留前 {MAX_FIELDS} 个")
            break
    return {"spec": SPEC, "fields": fields}, notes


# ==================================================================
#  推断：从 JSON 示例 / 初始状态猜字段类型
# ==================================================================
def infer_type(value: Any) -> str:
    """按值猜类型（作者只给示例 JSON 时用）。"""
    if isinstance(value, list):
        return "tuples" if any(isinstance(v, dict) for v in value) else "list"
    if isinstance(value, dict):
        return "flags"
    if _finite(value) is not None:
        return "number"
    return "text"


def infer_fields(sample: dict[str, Any], *, max_field: str = "max") -> list[dict[str, Any]]:
    """从一份示例 JSON 的**顶层键**推断字段清单（顺序照原样保留）。

    ★ meter 的识别有两套写法，都要认（作者的卡这两种都常见）：
      扁平 `{"hp": 100, "max": 100}` —— 本项目规范；成对出现的数值合并成 meter，
        且上限字段本身不再单独出现。
      嵌套 `{"hp": {"current": 100, "max": 100}}` —— SillyTavern 老写法；
        直接是一条 meter（这时上限字段名由 `max_field_name` 给出，
        因为它不在顶层，所以默认用一个不会与别处冲突的名字）。
    """
    if not isinstance(sample, dict):
        return []
    out: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for key, value in sample.items():
        name = _text(key, 24)
        if not name or name in consumed or not _NAME_RE.match(name):
            continue
        # ---- 嵌套写法：{"hp": {"current": 70, "max": 90}} ----
        if isinstance(value, dict) and _finite(value.get("current")) is not None:
            inner_max = _finite(value.get("max"))
            inner_name = _text(value.get("max_name") or "", 24)
            out.append(
                {
                    "name": name,
                    "label": name,
                    "type": "meter",
                    "max_field": inner_name if _NAME_RE.match(inner_name) else "max_value",
                }
            )
            if inner_max is not None:
                out[-1]["initial_max"] = (
                    int(inner_max) if float(inner_max).is_integer() else round(inner_max, 2)
                )
            cur = _finite(value.get("current"))
            if cur is not None:
                out[-1]["initial_current"] = int(cur) if float(cur).is_integer() else round(cur, 2)
            continue
        # ---- 扁平写法：{"hp": 100, "max": 100} ----
        twin = f"{name}_{max_field}" if f"{name}_{max_field}" in sample else None
        if twin is None and name == "hp" and "max" in sample:
            twin = "max"
        if twin is not None and _finite(value) is not None and _finite(sample.get(twin)) is not None:
            consumed.add(twin)
            out.append(
                {
                    "name": name,
                    "label": name,
                    "type": "meter",
                    "max_field": twin if twin != "max" else "max",
                }
            )
            continue
        ftype = infer_type(value)
        field: dict[str, Any] = {"name": name, "label": name, "type": ftype}
        if ftype == "list":
            preview = [str(v)[:12] for v in value[:3] if not isinstance(v, (dict, list))]
            if preview:
                field["description"] = "字符串数组，如 " + "、".join(preview)
        elif ftype == "tuples":
            field["description"] = "对象数组"
        elif ftype == "flags":
            field["description"] = "键值对象"
        out.append(field)
        if len(out) >= MAX_FIELDS:
            break
    return out


def from_example(sample: Any) -> tuple[dict[str, Any], list[str]]:
    """从示例 JSON 建 schema（世界书条目正文里的 `<state>{…}</state>` 走这里）。"""
    if not isinstance(sample, dict) or not sample:
        return {"spec": SPEC, "fields": []}, []
    fields = infer_fields(sample)
    if not fields:
        return {"spec": SPEC, "fields": []}, ["示例 JSON 里没有可用的字段名，已忽略"]
    return {"spec": SPEC, "fields": fields}, []


def legacy_schema() -> dict[str, Any]:
    """旧五字段 schema（**兼容用**：老会话/老卡没声明时按旧样子渲染 + 继续注入协议）。"""
    fields: list[dict[str, Any]] = []
    for name in LEGACY_FIELDS:
        meta = LEGACY_META.get(name, {})
        field: dict[str, Any] = {
            "name": name,
            "label": meta.get("label", name),
            "type": {"hp": "meter", "inventory": "list", "location": "text",
                     "quests": "tuples", "flags": "flags"}[name],
            "legacy": True,
        }
        if meta.get("icon"):
            field["icon"] = meta["icon"]
        if name == "hp":
            field["max_field"] = "max"
        fields.append(field)
    return {"spec": SPEC, "fields": fields, "legacy": True}


# ==================================================================
#  世界书：约定式条目
# ==================================================================
def is_book_schema_entry(entry: Any) -> bool:
    """这条世界书条目是不是"状态栏格式定义"（见模块开头的约定）。"""
    if not isinstance(entry, dict):
        return False
    name = _text(entry.get("name") or entry.get("comment") or "", 64)
    if name.startswith(BOOK_ENTRY_PREFIX):
        return True
    keys = entry.get("keys")
    if isinstance(keys, list):
        return any(str(k).strip().lower() == BOOK_ENTRY_KEY for k in keys)
    return False


def find_book_schema_entry(book: Any) -> dict[str, Any] | None:
    """在世界书里找状态栏格式条目（取第一条启用的）。"""
    entries = getattr(book, "entries", None)
    if isinstance(book, dict):
        entries = book.get("entries")
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("enabled", True) is not False and is_book_schema_entry(entry):
            return entry
    return None


def split_book_entry(entry: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
    """把世界书格式条目拆成 `(schema, 作者原文描述, notes)`。"""
    content = str(entry.get("content") or "")
    notes: list[str] = []
    matches = list(_EXAMPLE_RE.finditer(content))
    if matches:
        try:
            sample = json.loads(matches[-1].group(1))
        except ValueError:
            sample = None
            notes.append(
                "世界书里「[状态栏]」条目的示例 JSON 不是合法 JSON，"
                "已忽略示例（字段清单退回按卡的 initial_state 推断）"
            )
        if sample is not None:
            schema, parse_notes = from_example(sample)
            notes.extend(parse_notes)
            return schema, content, notes
    else:
        notes.append(
            "世界书里「[状态栏]」条目没有找到 <state>{…}</state> 示例，"
            "已忽略（字段清单退回按卡的 initial_state 推断）"
        )
    return {"spec": SPEC, "fields": []}, content, notes


# ==================================================================
#  统一入口：解析这张卡的状态栏
# ==================================================================
def hne_extension(card: Any) -> dict[str, Any]:
    """取卡片的 `extensions.hne`（没有就空 dict）。"""
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
    hne = extensions.get("hne")
    return hne if isinstance(hne, dict) else {}


def resolve_schema(card: Any, book: Any = None) -> tuple[dict[str, Any], list[str]]:
    """解析一张卡的状态栏定义。返回 `(schema, notes)`；空 schema = 没定义。

    ★ 优先级（user 拍板）：卡显式声明 > 世界书约定条目 > 从 initial_state 推断 > 空。
    """
    notes: list[str] = []
    hne = hne_extension(card)
    raw = hne.get("state_schema")
    if raw:
        schema, parse_notes = parse_schema(raw)
        notes.extend(parse_notes)
        if not is_empty(schema):
            schema["source"] = "card"
            return schema, notes

    entry = find_book_schema_entry(book)
    if entry is not None:
        schema, content, book_notes = split_book_entry(entry)
        notes.extend(book_notes)
        if not is_empty(schema):
            schema["source"] = "world_book"
            schema["description"] = content.strip()[:2000]
            return schema, notes

    initial = hne.get("initial_state")
    if isinstance(initial, dict) and initial:
        schema, infer_notes = from_example(initial)
        notes.extend(infer_notes)
        if not is_empty(schema):
            schema["source"] = "initial_state"
            return schema, notes

    logger.debug("这张卡没有声明状态栏格式，状态栏将显示「未定义」")
    return {"spec": SPEC, "fields": []}, notes


# ==================================================================
#  渲染：讲给模型听
# ==================================================================
_TYPE_HINT = {
    "text": "字符串",
    "number": "数字",
    "meter": "数字（当前值）",
    "list": "字符串数组",
    "tuples": "对象数组",
    "flags": "键值对象",
}


def example_json(schema: dict[str, Any]) -> dict[str, Any]:
    """按 schema 造一份示例状态（提示词里的 ```` ```json ```` 那段 + 界面骨架）。"""
    out: dict[str, Any] = {}
    for field in schema.get("fields") or []:
        name = field["name"]
        ftype = field.get("type", "text")
        if ftype == "meter":
            out[name] = field.get("initial_current", field.get("initial", 100))
            out[field.get("max_field") or "max"] = field.get("initial_max", 100)
        elif ftype == "number":
            out[name] = field.get("initial", 0)
        elif ftype == "list":
            out[name] = []
        elif ftype == "tuples":
            out[name] = []
        elif ftype == "flags":
            out[name] = {}
        else:
            out[name] = ""
    return out


def describe_fields(schema: dict[str, Any]) -> str:
    """把字段清单写成一句人话（进提示词，也用在后端校验提醒里）。"""
    parts: list[str] = []
    for field in schema.get("fields") or []:
        name = field["name"]
        ftype = field.get("type", "text")
        if ftype == "meter":
            max_field = field.get("max_field") or "max"
            text = f"{name}（数字，0~{max_field}）"
        else:
            text = f"{name}（{_TYPE_HINT.get(ftype, '文本')}）"
        description = _text(field.get("description"), 60)
        if description:
            text += f"：{description}"
        parts.append(text)
    return "、".join(parts)


def render_block(schema: dict[str, Any], state: dict[str, Any] | None) -> str:
    """渲染"当前状态"这一节（客观事实 + 字段说明 + 作者原文格式要求）。"""
    body = json.dumps(state or {}, ensure_ascii=False, indent=2) if state else "{}"
    lines = [
        "## 当前状态（每轮必须同步）",
        "（这是系统记录的**客观事实**，不是用户说的话；请与它保持一致。）",
        f"```json\n{body}\n```",
    ]
    description = _text(schema.get("description"), 2000)
    if description:
        # 世界书/卡里作者自己写的格式要求：原样带上（作者怎么写就怎么生效）
        lines.append("【状态栏格式 · 作者要求】")
        lines.append(description)
    if not schema.get("legacy"):
        lines.append(f"- 字段固定为：{describe_fields(schema)}；")
    else:
        lines.append(
            "- 字段固定为：hp{current,max}、inventory[]（字符串数组）、location（字符串）、"
            "quests[{title,status}]（status 取 active / done / failed）、flags{}；"
        )
    lines.append("- 某个字段没有变化也要照原样写上，不要省略。")
    return "\n".join(lines)


#: 输出契约的标题（放在提示词最末，刻意做得短而硬）
CONTRACT_TITLE = "[输出格式 · 必须遵守]"


def render_contract(schema: dict[str, Any]) -> str:
    """状态协议的**输出契约**：由 prompt_builder 追加在系统提示词最后。

    ★ 为什么必须单独再放一遍、而且必须放最后：
      用户验收时明确要求"状态栏每一轮都要输出，哪怕没有变化"。
      而原来这段要求夹在系统提示词中段，后面还压着身份守卫、剧情守卫、
      长度要求等一大段更"凶"的指令 —— 真实模型（deepseek-chat + 演示卡）
      把那些都执行了，唯独没输出状态块，界面上就永远只有"还没有状态"。
      格式要求属于"照做就行"的机械指令，放在最后一条最有效。
    ★ 已经带作者原文描述时，机械清单重复第三遍只会互相打架，所以那时省掉。
    """
    from app.narrative.state import STATE_CLOSE, STATE_OPEN

    lines = [
        CONTRACT_TITLE,
        f"- 每轮回复的**最后**都必须紧跟一个 {STATE_OPEN}…{STATE_CLOSE} 块，"
        "里面是更新后的**完整**状态 JSON；",
        "- **即使这一轮状态没有任何变化，也必须原样再输出一次** —— 不输出即视为格式错误；",
        "- 块内只写 JSON，不要写解释、不要用代码围栏包住它，也不要放在正文开头。",
    ]
    if not schema.get("description"):
        lines.insert(
            1,
            f"- 状态 JSON 的字段只能是：{describe_fields(schema)}"
            "（没有变化的字段也要原样写上，不要新增别的字段）。",
        )
    return "\n".join(lines)
