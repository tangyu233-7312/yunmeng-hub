"""会话状态（字段由角色卡 / 世界书定义）的解析与一致性校验。

==================== 它解决什么问题 ====================
角色扮演里"状态"是最容易崩的东西：模型这一轮写 HP=42，下一轮忘了、写成 88；
背包里的钥匙也会凭空消失。所以状态**不能只活在模型的小作文里**，
必须：解析出来 → 校验 → 落库（会话级）→ 下一轮原样喂回去。

==================== ★ 字段清单不再写死在这里 ====================
**有哪些字段**由 `app/narrative/state_schema.py` 从**角色卡 / 世界书**解析：
卡 `extensions.hne.state_schema` > 世界书里的「[状态栏]」条目 > 卡
`initial_state` 的顶层键 > 空（没定义就不注入协议、不解析、状态栏不画 HP）。
本模块只负责"照着 schema 解析与校验"，不假设字段叫什么 ——
曾经的 `KNOWN_FIELDS = ("hp", ...)` 让每张卡都长出 HP 条（连没声明 HP 的
魔法少女卡都有 `HP 100/100`），那是用户明确指出的错误设计。

==================== 五条设计决定 ====================
1. **协议自包含**（见 `render_for_prompt`）：状态协议就写在"当前状态"这一节里，
   不塞进用户可编辑的内置守卫预设 —— 用户一改预设协议就没了；而且改代码里的
   守卫文本对**已经落库**的那份预设根本不生效（这个坑本项目踩过）。
2. **落库前必须剥掉 `<state>` 块**：聊天里不该出现原始 JSON；而且模型复述历史时
   会看到自己以前的 JSON，越滚越大。
3. **校验失败一律降级，绝不抛异常**：状态是"锦上添花"，解析失败就沿用上一轮，
   并如实告诉用户（与长期记忆同一套哲学）。
4. **合并语义 = 旧状态 ∪ 新解析**：模型少写一个字段时沿用旧值，
   而不是让 HP / 背包"凭空消失"。这是它和"直接覆盖"最大的区别。
5. **schema 里没声明的字段一律忽略**（并记一条 note）：模型偶尔会自己加字段，
   以前那些字段会一路存下来，界面又画不出来，属于静默脏数据。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from loguru import logger

from app.narrative import state_schema as schema_mod

#: 提示词里状态这一节的标题（= state_schema 里那份渲染的标题）
BLOCK_TITLE = "## 当前状态（每轮必须同步）"
#: 模型必须输出的状态块标记
STATE_OPEN = "<state>"
STATE_CLOSE = "</state>"
#: 输出契约的标题
CONTRACT_TITLE = schema_mod.CONTRACT_TITLE

#: 取**最后**一个状态块：模型常在正文里复述上一轮的状态，取第一个会拿到旧值
_STATE_RE = re.compile(
    rf"{re.escape(STATE_OPEN)}\s*(\{{.*?\}})\s*{re.escape(STATE_CLOSE)}",
    re.S | re.I,
)

MAX_ITEMS = 50  # 列表 / 对象数组最多保留多少条
MAX_TEXT = 120  # 单个文本字段的长度上限
MAX_FLAGS = 30  # flags 最多保留多少个键
#: 单轮数值跳变阈值：超过只**提醒**，不否决模型（状态该由剧情决定，不该由代码武断改）
HP_MAX_JUMP_RATIO = 0.5
HP_CURRENT_JUMP_RATIO = 0.6
DEFAULT_HP_MAX = 100.0

#: 状态里的固定字段（**兼容旧会话/旧卡**；新会话的字段来自 schema）
KNOWN_FIELDS = schema_mod.LEGACY_FIELDS
#: 任务允许的状态
_QUEST_STATUS = ("active", "done", "failed")

#: 通用数值字段的兜底上限（只用于 meter 上限兜底，与 HP 那个保持同值）
DEFAULT_MAX = DEFAULT_HP_MAX


# ==================================================================
#  读 / 写
# ==================================================================
def load_state(session: Any) -> dict[str, Any]:
    """读出会话已落库的状态（坏了就按空状态处理，绝不抛异常）。"""
    raw = getattr(session, "state_json", None)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "会话状态不是合法 JSON，已按空状态处理 | session_id={}",
            getattr(session, "id", None),
        )
        return {}
    return value if isinstance(value, dict) else {}


def save_state(session: Any, state: dict[str, Any]) -> None:
    """把状态写回会话对象（**不 commit**：交给调用方的事务一起提交）。"""
    session.state_json = json.dumps(state, ensure_ascii=False)


def load_schema(session: Any) -> dict[str, Any]:
    """读出会话落库的状态栏 schema（建会话时解析一次）。

    ★ 坏数据（不是 JSON / 不是对象）按**空 schema** 处理：没定义就不注入协议，
      比"猜一份出来"安全 —— 后者正是我们要修掉的老毛病。
    """
    raw = getattr(session, "state_schema_json", None)
    if not raw:
        return {"spec": schema_mod.SPEC, "fields": []}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(
            "会话状态栏格式不是合法 JSON，已按「未定义」处理 | session_id={}",
            getattr(session, "id", None),
        )
        return {"spec": schema_mod.SPEC, "fields": []}
    if not isinstance(value, dict):
        return {"spec": schema_mod.SPEC, "fields": []}
    parsed, _notes = schema_mod.parse_schema(value)
    # parse_schema 会丢 description / legacy / source 这些元信息，这里补回来
    if not schema_mod.is_empty(parsed):
        parsed = {**value, "fields": parsed["fields"]}
    return parsed


def save_schema(session: Any, schema: dict[str, Any]) -> None:
    """把 schema 写回会话（**不 commit**）。空 schema 也照写，表示"这张卡没定义"。"""
    session.state_schema_json = json.dumps(schema or {}, ensure_ascii=False)


def effective_schema(session: Any) -> dict[str, Any]:
    """会话实际生效的 schema（含**兼容路径**）。

    ★ 三条分支（顺序不能换）：
      1. 会话落库了 schema → 用它（哪怕是"没定义"的空 schema，也要尊重卡作者的决定）
      2. 没落库（迁移前的旧会话）但已有状态 → 退回旧五字段（解析 + 渲染都用它）。
         这是**兼容路径**：这类会话当年就是按写死的五字段跑的，库里也有真实状态，
         一旦不认它，历史状态栏会变空白、模型下一轮也会突然不再输出状态块。
      3. 都没 → 空 schema（状态栏显示"该卡未定义状态栏格式"，不注入协议）
    """
    schema = load_schema(session)
    if not schema_mod.is_empty(schema):
        return schema
    if load_state(session):
        return schema_mod.legacy_schema()
    return schema


# ==================================================================
#  渲染（协议正文 / 输出契约）
# ==================================================================
def render_for_prompt(session: Any, schema: dict[str, Any] | None = None) -> str:
    """渲染"当前状态"这一段（客观事实 + 字段说明 + 作者格式要求）。

    空 schema 返回 **空串**：这张卡没定义状态栏，就不该往提示词里塞一套
    它没用过的字段（老实现无论什么卡都塞 HP 那套，是用户指出的设计错误）。
    ★ 命令式的"每轮都要输出"那几句在 `render_contract()` —— 它由 prompt_builder
      追加到系统提示词的**最末尾**（越靠后的指令权重越高）。
    """
    active = schema if schema is not None else effective_schema(session)
    if schema_mod.is_empty(active):
        return ""
    return schema_mod.render_block(active, load_state(session))


def render_contract(schema: dict[str, Any] | None = None) -> str:
    """状态协议的**输出契约**（由 prompt_builder 追加在系统提示词最后）。

    ★ 必须单独再放一遍、而且必须放最后：用户验收时明确要求"状态栏每一轮都要输出，
      哪怕没有变化"。而这段要求以前夹在系统提示词中段，后面还压着身份守卫、
      剧情守卫、长度要求等一大段更"凶"的指令 —— 真实模型（deepseek-chat +
      演示卡）把那些都执行了，唯独没输出状态块，界面上就永远只有"还没有状态"。
      格式要求属于"照做就行"的机械指令，放在最后一条最有效。
    ★ `schema` 省略时用旧五字段那份（兼容旧调用方/旧测试）。
    """
    active = schema if schema is not None else schema_mod.legacy_schema()
    return schema_mod.render_contract(active)


# ==================================================================
#  解析 + 一致性校验
# ==================================================================
def extract_state_block(content: str) -> tuple[str, str | None]:
    """把正文里的状态块摘出来。

    返回 `(剥掉状态块后的正文, 最后一块的 JSON 原文或 None)`。
    ★ 只取最后一块、但把所有块都从正文里删掉：模型偶尔会先复述旧状态再给新的。
    """
    text = str(content or "")
    matches = list(_STATE_RE.finditer(text))
    if not matches:
        # ★ 兜底：模型偶尔会把块写坏（少了 `}`、少了 `</state>`）。这种块**绝不能留在正文里**
        #   —— 用户会看到一串半截 JSON。协议规定它一定在回复末尾，所以从第一个
        #   `<state>` 起直接截掉。状态本身按"解析失败"处理（沿用上一轮）。
        loose = re.search(re.escape(STATE_OPEN), text, re.I)
        if loose:
            return text[: loose.start()].strip(), None
        return text, None
    return _STATE_RE.sub("", text).strip(), matches[-1].group(1)


def _number(value: Any) -> float | None:
    """只认真正的有限数字（bool 不算，NaN / inf 不算）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    num = float(value)
    return num if math.isfinite(num) else None


def _text(value: Any) -> str:
    return str(value).strip()[:MAX_TEXT]


def _round(value: float) -> int | float:
    """整数就存整数（42 而不是 42.0），避免前端显示成 42.0。"""
    return int(value) if float(value).is_integer() else round(value, 2)


def _scaffold(field: dict[str, Any]) -> Any:
    """某个字段"还没有值"时的形状（让界面骨架与 schema 一致）。"""
    ftype = field.get("type", "text")
    if ftype == "list":
        return []
    if ftype == "tuples":
        return []
    if ftype == "flags":
        return {}
    return ""


def _clean_list(items: Any, notes: list[str], label: str) -> list[str] | None:
    """字符串数组（背包那种）。非法返回 None 表示"沿用上一轮"。"""
    if not isinstance(items, list):
        notes.append(f"状态块里的 {label} 不是数组，已沿用上一轮")
        return None
    cleaned: list[str] = []
    for item in items:
        # 只认字符串：数字/对象混进来通常是模型写歪了，宁可少一条也别存脏数据
        name = _text(item) if isinstance(item, str) else ""
        if name and name not in cleaned:
            cleaned.append(name)
    if len(cleaned) > MAX_ITEMS:
        notes.append(f"{label} 条目过多（{len(cleaned)} 条），只保留前 {MAX_ITEMS} 条")
    return cleaned[:MAX_ITEMS]


def _clean_tuples(items: Any, notes: list[str], label: str) -> list[dict[str, str]] | None:
    """对象数组（任务那种：{title, status}）。非法返回 None。"""
    if not isinstance(items, list):
        notes.append(f"状态块里的 {label} 不是数组，已沿用上一轮")
        return None
    out: list[dict[str, str]] = []
    for item in items[:MAX_ITEMS]:
        if isinstance(item, dict):
            title = _text(item.get("title") or item.get("name") or item.get("名称") or "")
            if not title:
                continue
            status = str(item.get("status") or item.get("状态") or "active").strip().lower()
            out.append({"title": title, "status": status if status in _QUEST_STATUS else "active"})
        elif isinstance(item, str) and item.strip():
            out.append({"title": _text(item), "status": "active"})
    return out


def _clean_flags(value: Any, notes: list[str], label: str) -> dict[str, Any] | None:
    """键值对象（flags 那种）。非法返回 None。"""
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_FLAGS]:
            name = _text(key)
            if not name:
                continue
            if isinstance(item, str):
                kept[name] = _text(item)
            elif isinstance(item, bool) or _number(item) is not None:
                kept[name] = item
        return kept
    notes.append(f"状态块里的 {label} 不是对象，已沿用上一轮")
    return None


def _clean_meter(
    field: dict[str, Any],
    raw: dict[str, Any],
    previous: dict[str, Any],
    notes: list[str],
) -> None:
    """处理 meter（HP 那类"当前值 / 上限"）字段：夹取 + 跳变提醒。

    ★ 兼容两种写法：扁平 `{"hp": 88, "max": 100}`（本项目规范）
      与嵌套 `{"hp": {"current": 88, "max": 100}}`（老卡/别的工具）。
    """
    name = field["name"]
    max_field = field.get("max_field") or "max"
    is_hp = name == "hp"
    prev_max = _number(previous.get(max_field))
    if prev_max is None or prev_max <= 0:
        prev_max = _number(field.get("initial_max")) or DEFAULT_MAX
    prev_cur = _number(previous.get(name))

    value = raw.get(name)
    nested = value if isinstance(value, dict) else None
    if nested is not None:
        cur = _number(nested.get("current"))
        new_max = _number(nested.get("max"))
        if new_max is None:
            new_max = _number(raw.get(max_field))
    else:
        cur = _number(value)
        new_max = _number(raw.get(max_field))

    # 读不出当前值：沿用上一轮（没有上一轮就按上限起）
    if cur is None and nested is None:
        notes.append(f"状态块里的 {name} 不是数字，已沿用上一轮")
        return
    if cur is None:
        cur = prev_cur if prev_cur is not None else prev_max

    if new_max is None:
        new_max = prev_max
    elif new_max <= 0:
        notes.append(f"{name} 上限不是正数，已改回上一轮的值")
        new_max = prev_max
    elif abs(new_max - prev_max) > HP_MAX_JUMP_RATIO * max(prev_max, 1.0):
        notes.append(
            f"{name} 上限一轮内从 {_round(prev_max)} 变成 {_round(new_max)}（超过 50%），"
            "已保留上一轮的上限"
        )
        new_max = prev_max

    clamped = min(max(cur, 0.0), new_max)
    if clamped != cur:
        notes.append(f"{name} 越界（{_round(cur)}），已夹到 0～{_round(new_max)} 之间")
    if prev_cur is not None and abs(clamped - prev_cur) > HP_CURRENT_JUMP_RATIO * new_max:
        notes.append(
            f"{name} 一轮内从 {_round(prev_cur)} 跳到 {_round(clamped)}"
            "（超过上限的 60%），已照模型给的值记录，请确认剧情是否确实如此"
        )
    previous[name] = _round(clamped)
    previous[max_field] = _round(new_max)


def _clean_number(
    field: dict[str, Any], value: Any, notes: list[str]
) -> tuple[bool, Any]:
    """处理 number 字段。返回 `(是否有值, 值)`。"""
    name = field["name"]
    num = _number(value)
    if num is None:
        notes.append(f"状态块里的 {name} 不是数字，已沿用上一轮")
        return False, None
    low = _number(field.get("min"))
    high = _number(field.get("max"))
    if low is not None and high is not None and low > high:
        low = high = None
    if low is not None and num < low:
        notes.append(f"{name} 低于下限 {_round(low)}，已夹到下限")
        num = low
    if high is not None and num > high:
        notes.append(f"{name} 超过上限 {_round(high)}，已夹到上限")
        num = high
    return True, _round(num)


def normalize(
    raw: dict[str, Any],
    previous: dict[str, Any] | None,
    schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """把模型给的状态块按 **schema** 校验成"可信的状态"，并与上一轮合并。

    ★ 只处理 schema 里声明过的字段：没声明的（模型自己加的）一律忽略并记 note。
    ★ 先继承旧值再逐字段覆盖（含 meter 的上限字段）：模型漏写的字段沿用旧值。
    ★ `previous` 允许传 None（= 还没有上一轮，例如卡片自带的初始状态）：
      以前这里会直接 `dict(None)` 抛 TypeError，属于调用方一传错就炸的坑。
    ★ `schema` 为空（这张卡没定义状态栏）：**什么都不做** —— 连字段都不该有。
    """
    notes: list[str] = []
    prev = previous if isinstance(previous, dict) else {}
    if schema_mod.is_empty(schema):
        return {}, notes

    fields = schema["fields"]
    # ★ meter 的上限字段（hp 的 max）在 schema 里是挂在 meter 上的属性、
    #   不是独立字段，所以它也得算"声明过的"，否则合并时会被当成陌生键丢掉。
    allowed = schema_mod.field_names(schema)
    for field in fields:
        if field.get("type") == "meter":
            allowed.append(field.get("max_field") or "max")
    state: dict[str, Any] = {k: v for k, v in prev.items() if k in allowed}

    for field in fields:
        name = field["name"]
        ftype = field.get("type", "text")
        if ftype == "meter":
            max_field = field.get("max_field") or "max"
            if name in raw or max_field in raw:
                _clean_meter(field, raw, state, notes)
            continue
        if name not in raw:
            continue
        value = raw[name]
        if ftype == "number":
            ok, num = _clean_number(field, value, notes)
            if ok:
                state[name] = num
        elif ftype == "text":
            if isinstance(value, str) and value.strip():
                state[name] = _text(value)
            else:
                notes.append(f"状态块里的 {name} 不是文本，已沿用上一轮")
        elif ftype == "list":
            cleaned = _clean_list(value, notes, name)
            if cleaned is not None:
                state[name] = cleaned
        elif ftype == "tuples":
            tuples = _clean_tuples(value, notes, name)
            if tuples is not None:
                state[name] = tuples
        elif ftype == "flags":
            flags = _clean_flags(value, notes, name)
            if flags is not None:
                state[name] = flags

    dropped = [k for k in raw if isinstance(k, str) and k.strip() and k not in allowed]
    if dropped:
        notes.append(
            "状态块里有这张卡没有定义的字段，已忽略："
            + "、".join(str(k).strip()[:20] for k in dropped[:5])
        )
    return state, notes


def ensure_shape(state: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """补齐 schema 里声明、但状态里还没有的字段（照 schema 的空值形状）。

    这样前端的"状态栏骨架"不必自己猜形状（以前的骨架硬编码了 HP）。
    """
    if schema_mod.is_empty(schema):
        return dict(state or {})
    out = dict(state or {})
    for field in schema["fields"]:
        name = field["name"]
        ftype = field.get("type", "text")
        if ftype == "meter":
            max_field = field.get("max_field") or "max"
            out.setdefault(name, field.get("initial_current", field.get("initial", 0)))
            out.setdefault(max_field, field.get("initial_max", DEFAULT_MAX))
        elif name not in out:
            out[name] = _scaffold(field)
    return out


# ==================================================================
#  状态遥测（漂移曲线的原始数据）
# ==================================================================
#: 校验提醒的分类标记。★ 与 `_clean_*` 里写的文案**一一对应**，
#: 改文案就要改这里（`tests/test_state.py` 有断言盯着分类结果）。
#: 四类对应四种**不同的机制**，曲线里要分开看（合起来会看不出是哪一层在起作用）：
#:   clamped  值越界 → 夹到边界（采纳模型的值，只修正到合法区间）
#:   guarded  单轮跳变/上限非法 → 整块拒绝、沿用上一轮（**防漂移护栏**）
#:   rejected 类型/格式坏值 → 丢弃该字段、沿用上一轮（数据质量）
#:   unknown  模型写了这张卡没声明的字段 → 忽略
_CLAMP_MARKERS = ("夹到", "越界")
_GUARD_MARKERS = ("超过 50%", "上限不是正数")
_REJECT_MARKERS = (
    "不是数字",
    "不是文本",
    "不是数组",
    "不是对象",
    "沿用上一轮",
)
_UNKNOWN_MARKERS = ("没有定义", "已忽略")


def classify_notes(notes: list[str] | None) -> dict[str, int]:
    """把校验提醒分成五类计数：`clamped / guarded / rejected / unknown / other`。

    ★ 这是"状态漂移曲线"的分类依据，所以在**唯一**的地方实现一次：
      `retrieval_metrics.classify_notes` 也复用它（那边把五类映射成三种下场）。
    """
    counts = {"clamped": 0, "guarded": 0, "rejected": 0, "unknown": 0, "other": 0}
    for note in notes or []:
        text = str(note)
        if any(marker in text for marker in _REJECT_MARKERS):
            counts["rejected"] += 1
        elif any(marker in text for marker in _UNKNOWN_MARKERS):
            counts["unknown"] += 1
        elif any(marker in text for marker in _GUARD_MARKERS):
            counts["guarded"] += 1
        elif any(marker in text for marker in _CLAMP_MARKERS):
            counts["clamped"] += 1
        else:
            counts["other"] += 1
    return counts


def _jaccard_distance(left: Any, right: Any) -> float:
    """两个集合的差异度（0 = 完全一样，1 = 完全不相交）。"""
    left_set = {str(x) for x in (left or [])}
    right_set = {str(x) for x in (right or [])}
    if not left_set and not right_set:
        return 0.0
    union = left_set | right_set
    return 1.0 - len(left_set & right_set) / len(union) if union else 0.0


def _reported_value(raw: dict[str, Any], field: dict[str, Any]) -> Any:
    """取模型**自称**的字段值（meter 兼容扁平与嵌套两种写法）。"""
    value = raw.get(field["name"])
    if field.get("type") == "meter" and isinstance(value, dict):
        return value.get("current")
    return value


def deviation(
    raw: dict[str, Any], stored: dict[str, Any], schema: dict[str, Any] | None
) -> float:
    """模型**自称**的状态与**校验后落库**的状态之间的偏离度（0~1，越大越飘）。

    分类型算，再对"模型这一轮真的报了的字段"取平均：
      · meter / number：|自报 − 落库| / max(|自报|, 1)
      · list / tuples / flags：集合差异（Jaccard 距离）
      · text：一样 = 0，不一样 = 1
    ★ 这就是"漂移曲线"里的 y 值：没有校验时它会一路涨（模型越写越离谱），
      有校验时被压回 0 附近（越界被夹、脏值被拒）。
    """
    if schema_mod.is_empty(schema) or not isinstance(raw, dict):
        return 0.0
    scores: list[float] = []
    for field in schema["fields"]:
        name = field["name"]
        ftype = field.get("type", "text")
        reported = _reported_value(raw, field)
        if reported is None:
            continue
        kept = stored.get(name)
        if ftype in ("meter", "number"):
            model_num = _number(reported)
            stored_num = _number(kept)
            if model_num is None or stored_num is None:
                scores.append(1.0 if model_num != stored_num else 0.0)
                continue
            scores.append(abs(model_num - stored_num) / max(abs(model_num), 1.0))
        elif ftype == "list":
            scores.append(_jaccard_distance(reported, kept))
        elif ftype == "tuples":
            titles = lambda items: [  # noqa: E731 - 局部小工具，就地读最清楚
                str(i.get("title") or "") for i in (items or []) if isinstance(i, dict)
            ]
            scores.append(_jaccard_distance(titles(reported), titles(kept)))
        elif ftype == "flags":
            scores.append(_jaccard_distance(list(reported), list(kept or {})))
        else:
            scores.append(0.0 if str(reported).strip() == str(kept or "").strip() else 1.0)
    return sum(scores) / len(scores) if scores else 0.0


#: 没输出状态块时的提醒文案（遥测与提示共用，别改一处漏一处）
MISSING_BLOCK_NOTE = (
    "本轮回复里没有 <state> 状态块：模型没有按格式要求输出，"
    "状态栏沿用上一轮的值（可在卡片尾注或预设里再强调一次）"
)


def apply_reply_with_meta(
    session: Any,
    content: str,
    *,
    schema: dict[str, Any] | None = None,
    notify_missing: bool = True,
) -> tuple[str, list[str], dict[str, Any]]:
    """与 `apply_reply` 同一套逻辑，但**额外返回状态遥测**（给漂移曲线用）。

    遥测形状：
        {"required": bool, "had_block": bool, "malformed": bool,
         "counts": {"clamped":n,"rejected":n,"unknown":n,"other":n},
         "deviation": 0.0~1.0, "fields_reported": n}
    ★ `required` 决定这条记录要不要参与统计：卡没声明状态栏 / 纯聊天时**不该**算它漏输出。
    """
    cleaned, raw_json = extract_state_block(content)
    active = schema if schema is not None else effective_schema(session)

    def _meta(payload: dict[str, Any]) -> dict[str, Any]:
        """遥测里**额外带上原始块文本**（`engine` 会把它单独落到 `messages.state_raw_json`）。

        ★ 为什么不让 engine 自己再调一次 `extract_state_block`：那等于把"怎么剥块"
          这条规则写两遍，哪天改歪一处就会出现"遥测说有块、原文却没有"。
        ★ 这个键由调用方 `pop` 掉，不会进 `state_meta_json`（漂移统计只认 counts/deviation）。
        """
        payload["raw"] = raw_json
        return payload

    if schema_mod.is_empty(active):
        # 没要求过状态块：只剥块、不落库、不提醒，遥测标 required=False
        return cleaned, [], _meta({"required": False, "had_block": raw_json is not None})

    zero = {
        "clamped": 0,
        "guarded": 0,
        "rejected": 0,
        "unknown": 0,
        "other": 0,
    }
    if raw_json is None:
        notes = (
            []
            if (not notify_missing or not str(content or "").strip())
            else [MISSING_BLOCK_NOTE]
        )
        return cleaned, notes, _meta({
            "required": True,
            "had_block": False,
            "counts": dict(zero),
            "deviation": 0.0,
            "fields_reported": 0,
        })

    try:
        raw = json.loads(raw_json)
    except ValueError as exc:
        logger.warning(
            "状态块不是合法 JSON，本轮沿用上一轮 | session_id={} err={}",
            getattr(session, "id", None),
            exc,
        )
        return cleaned, ["模型输出的状态块不是合法 JSON，本轮已沿用上一轮状态"], _meta({
            "required": True,
            "had_block": True,
            "malformed": True,
            "counts": dict(zero),
            "deviation": 0.0,
            "fields_reported": 0,
        })

    if not isinstance(raw, dict):
        return cleaned, ["模型输出的状态块不是对象，本轮已沿用上一轮状态"], _meta({
            "required": True,
            "had_block": True,
            "malformed": True,
            "counts": dict(zero),
            "deviation": 0.0,
            "fields_reported": 0,
        })

    previous = load_state(session)
    state, notes = normalize(raw, previous, active)
    state = ensure_shape(state, active)
    save_state(session, state)
    meta = _meta({
        "required": True,
        "had_block": True,
        "malformed": False,
        "counts": classify_notes(notes),
        "deviation": round(deviation(raw, state, active), 4),
        "fields_reported": len([k for k in raw if isinstance(k, str)]),
    })
    return cleaned, notes, meta


def apply_reply(
    session: Any,
    content: str,
    *,
    schema: dict[str, Any] | None = None,
    notify_missing: bool = True,
) -> tuple[str, list[str]]:
    """收下一轮回复里的状态：剥掉状态块 → 校验合并 → 写回会话。

    返回 `(剥掉状态块后的正文, 需要告诉用户的提醒)`。
    ★ 由 `engine.save_assistant_reply` 调用，是流式 / 非流式 / 中断三条路共用的唯一入口。

    ★ `notify_missing`：没找到状态块时要不要提醒用户。
      默认要（**拒绝静默降级**：模型没按格式输出就得说出来，否则用户只看到
      状态栏一直是空的，完全不知道为什么）。建会话时解析开场白传 False，
      因为那时候"开场白里没有状态块"是常态，不该弹提醒。
    ★ 空 schema（这张卡没定义状态栏）：只剥块、不落库、不提醒 ——
      我们压根没要求模型输出状态块，就不能反过来怪它（与纯聊天同一条哲学）。

    ★ 本函数是 `apply_reply_with_meta` 的**薄包装**（只丢掉了遥测那一份返回值）：
      逻辑只有一份，避免"两条路各写一遍、哪天改歪一条"（踩过不止一次）。
    """
    cleaned, notes, _meta = apply_reply_with_meta(
        session, content, schema=schema, notify_missing=notify_missing
    )
    return cleaned, notes
