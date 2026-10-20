"""卡自带 JSON 状态块（第二状态存储）的识别、抽取与漂移检查。

==================== 它解决什么问题 ====================
本系统要求模型按 `state_schema` 输出 `<state>{…}</state>`，由 `state.py` 解析落库。
但**很多现成的角色卡带着自己的一套状态格式**——它们根本不是写给本系统的：

  · 卡的正则/开场白里定义了 `` ```json {"hp": 42} ``` `` 之类的状态块；
  · 酒馆 MVU（MagVarUpdate）卡在消息末尾输出 `<UpdateVariable>…</UpdateVariable>`；
  · 卡把自己的 HTML 面板当状态用，数据藏在 `<script type="application/json">` 里；
  · 也有的卡直接沿用 `<state>` 标签，但字段和本系统的 schema 完全无关。

以前的处理是：**只认 `<state>`** —— 于是作者精心写的状态块被当成正文留在气泡里，
用户看到一串原始 JSON；而且就算认出来了，它也只能挤进本系统那一份状态，
字段对不上就只能被忽略（`state.py` 的 design-5：未知字段一律丢弃）。

==================== 五条设计决定（改之前先读完）====================

1. **第二份存储，绝不合流。**
   卡的状态和本系统的结构化状态是**两种东西**（字段、语义、由谁负责更新都不同），
   混进同一列以后就再也分不清"这个 hp 是谁写的"。所以卡的状态单独存一张表
   （`card_states`，见 `app/db/models/card_state.py`），本系统的 `sessions.state_json` 不动。
   ★ 副作用要记住：卡状态**不参与**提示词注入、不参与合并语义、不参与 drift 统计。

2. **两套都解析、都落库，只让"显示"按优先级二选一。**
   用户明确要求"作者的状态块优先"。优先级只作用于**界面画哪一个**：
   数据两份都在，随时能切回来看，也不会出现"切换优先级导致历史数据丢失"。

3. **默认保守：只认"强特征 + 在消息末尾"。**
   把正文里的 JSON 误判成状态块 → 那段正文被藏起来，用户看故事时直接断片；
   反过来漏判 → 就是加这个功能之前的行为，什么都不坏。
   所以识别要求：**块在消息尾部**（后面只允许空白/标点）+ **强特征**（见 `_detect`）。
   宁可少认，不能错认 —— 但每种被认出的格式都**原样记录是哪种**（`source`），
   用户在意时能一眼看出"系统按哪种规则处理的"。

4. **识别规则只有一份，放在后端。**
   前端要"隐藏正文里的状态块"就必须和后端答案一致。若前端自己再写一份正则，
   迟早出现"后端认了、前端没认"（或者反过来），这正是本项目踩过的
   `looksLikeHtml` 判据分叉那类事故。所以由 `extract()` 一次给出
   `(剥掉卡状态块后的正文, 状态本身, 来源)`，前端只读结果。

5. **漂移检查只做"能确证"的两件事：字段消失 / 类型变了。**
   数值偏移需要 schema 才知道"该往哪边算"，而卡状态没有 schema，
   硬算必然误报（模型写 `hp: 42` 下一轮写 `"42"` 到底算不算偏移？）。
   所以这里只报 ①上一轮有、这一轮没了的字段；②同名但类型变了的字段。
   其余一概不猜。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from loguru import logger

#: 卡状态的来源标记（落库 & 界面展示共用；**别改名**，`card_states.source` 里存的就是它）
SRC_STATE_TAG = "state_tag"  #: `<state>{…}</state>`（卡沿用本系统的标签）
SRC_FENCE = "json_fence"  #: 消息末尾的 ```json / ``` 围栏
SRC_MVU = "mvu"  #: 酒馆 MVU 的 <UpdateVariable> / <InitVar> / <JSONPatch>
SRC_SCRIPT = "script_json"  #: <script type="application/json">…</script>
SRC_INLINE_TAG = "json_tag"  #: 自定义标签里包着纯 JSON，如 <status>{…}</status> / <StatusPlaceHolderImpl/>

#: 给界面看的来源说明（前端直接显示，别再自己编一套文案）
SOURCE_LABELS: dict[str, str] = {
    SRC_STATE_TAG: "<state> 标签",
    SRC_FENCE: "JSON 围栏",
    SRC_MVU: "酒馆 MVU 指令块",
    SRC_SCRIPT: "卡内嵌 JSON 脚本",
    SRC_INLINE_TAG: "自定义 JSON 标签",
}

#: `<state>` 标签、以及"标签里只包纯 JSON"的自定义状态标签。
#: ★ 只认"内容确实是 JSON"的：`<status>HP 42</status>` 这种纯文本照旧当正文，
#:   不能被藏起来（那是作者写给读者看的内容）。
_TAG_RE = re.compile(r"<([a-zA-Z][\w:-]{1,40})\b[^>]*>(.*?)</\1\s*>", re.S)
#: 自闭合的空占位标签（酒馆助手常见的 `<StatusPlaceHolderImpl/>`：真正的数据由脚本填）
_PLACEHOLDER_RE = re.compile(r"<\s*StatusPlaceHolderImpl\s*/?\s*>", re.I)
#: JSON 围栏：```json …``` / ```JSON …``` / ``` …```（内容必须能解析成对象）
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.S)
#: MVU 指令块
_MVU_RE = re.compile(r"<(UpdateVariable|InitVar|JSONPatch)\b[^>]*>(.*?)</\1\s*>", re.S | re.I)
#: 卡内嵌 JSON 脚本
_SCRIPT_JSON_RE = re.compile(
    r"<script\b[^>]*type\s*=\s*[\"']?application/(?:ld\+)?json[\"']?[^>]*>(.*?)</script\s*>",
    re.S | re.I,
)
#: 消息尾部允许残留的"标点/空白"（块后面只剩这些才算"在末尾"）
_TAIL_NOISE = " \t\r\n　.,，。!！?？~～…、;；:：-—_*"

#: 卡状态最多保存多少字段 / 多深（防畸形块把库撑大）
MAX_FIELDS = 200
MAX_DEPTH = 6
#: 单个字符串值最长保留多少字（卡状态常见长段描述；这里只防爆炸）
MAX_TEXT = 4000


# ==================================================================
#  JSON 解析（宽松）
# ==================================================================
def _loose_json(text: str) -> Any:
    """尽量把一段文本解析成 JSON 值；解析不出来返回 `_NO`.

    ★ 宽松到什么程度：允许前后有多余文字（块里常带一句说明）、
      允许 JSON5 风格的尾逗号与单引号注释 —— 但**只在严格解析失败后**才试，
      这样正常 JSON 的解析路径和标准库完全一致（不会悄悄改语义）。
    ★ 不管宽松到什么程度，第 5 条原则不变：解析不出对象就当**没认出**，交还正文。
    """
    raw = str(text or "").strip()
    if not raw:
        return _NO
    try:
        return json.loads(raw)
    except ValueError:
        pass
    # 去掉块里常见的说明文字：取第一个 `{`/`[` 到最后一个 `}`/`]`
    start = min([i for i in (raw.find("{"), raw.find("[")) if i >= 0], default=-1)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if 0 <= start < end:
        body = raw[start : end + 1]
        try:
            return json.loads(body)
        except ValueError:
            try:
                return json.loads(_strip_json5(body))
            except ValueError:
                return _NO
    return _NO


def _strip_json5(text: str) -> str:
    """去掉 JSON5 里标准 JSON 不认的几样东西（注释 / 尾逗号 / 单引号串）。

    ★ 刻意**不**做得很聪明：每一项都可能改坏用户数据，所以改动只限
      "标准 JSON 明确不合法"的那些位置。改完仍解析失败 → 上层当没认出。
    """
    out = re.sub(r"//[^\n]*", "", text)
    out = re.sub(r"/\*.*?\*/", "", out, flags=re.S)
    out = re.sub(r",(\s*[}\]])", r"\1", out)
    if "'" in out and '"' in out:
        return out  # 单双引号混用，不硬掰（掰错的风险比收益大）
    return out.replace("'", '"') if "'" in out else out


class _NoMatch:
    """"没有认出/没有解析出来"的哨兵（用对象而不是 None：JSON 里的 null 是合法值）。"""

    def __repr__(self) -> str:  # pragma: no cover - 只为调试好看
        return "<NO>"


_NO = _NoMatch()


# ==================================================================
#  识别
# ==================================================================
def _trimmed_value(value: Any) -> Any:
    """把解析出来的值收敛成"可以用来显示的状态"，并限制体积。

    ★ 只保留 JSON 能表达的东西；超出 MAX_FIELDS / MAX_DEPTH 的部分**丢弃并计数**，
      不静默 —— 调用方把计数写进 notes，用户能看到"这块太大被截了"。
    """
    stats = {"dropped_fields": 0, "truncated_text": 0, "depth_limited": 0}

    def walk(node: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            stats["depth_limited"] += 1
            return None
        if isinstance(node, str):
            if len(node) > MAX_TEXT:
                stats["truncated_text"] += 1
                return node[:MAX_TEXT] + "…"
            return node
        if isinstance(node, (int, float, bool)) or node is None:
            return node
        if isinstance(node, list):
            return [walk(item, depth + 1) for item in node[:MAX_FIELDS]]
        if isinstance(node, dict):
            items = list(node.items())
            if len(items) > MAX_FIELDS:
                stats["dropped_fields"] += len(items) - MAX_FIELDS
                items = items[:MAX_FIELDS]
            out: dict[str, Any] = {}
            for key, value in items:
                out[str(key)] = walk(value, depth + 1)
            return out
        return str(node)

    return walk(value, 0), stats


def _at_tail(text: str, end: int) -> bool:
    """块结束位置之后是否只剩空白/标点（= "在消息末尾"）。

    ★ 这条是"保守"的核心：作者的状态块几乎总在回复最后；
      正文中段的 JSON（比如台词里贴的配置片段）不该被藏。
    """
    rest = str(text or "")[end:]
    return not rest.strip(_TAIL_NOISE)


def _looks_like_state(obj: Any) -> bool:
    """解析出来的东西像不像"状态"（而不是一段随手的 JSON）。

    ★ 判据刻意宽（只要是对象/数组就算）+ 尾部位置约束，
      因为卡作者把任意字段名当状态是完全合法的（"尽量都认"是用户的要求）。
    """
    return isinstance(obj, (dict, list))


def _pick_tail(candidates: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    """从所有"在尾部"的候选里挑一个；同级选最后出现的（与 `<state>` 取最后一块同源）。"""
    tailed = [c for c in candidates if _at_tail(text, c["end"])]
    if not tailed:
        return None
    return max(tailed, key=lambda c: c["end"])


def _detect(text: str) -> dict[str, Any] | None:
    """认出消息末尾的卡状态块；没有就是 None。

    返回 `{source, start, end, raw, value, stats}`：
      · `start/end` 是**原文里的字符区间**（前端/落库都按它剥）
      · `raw` 是块内原文（"看作者原格式"用）
      · `value` 是解析后的 JSON 值（已按体积收敛）
    """
    src = str(text or "")
    if not src.strip():
        return None
    found: list[dict[str, Any]] = []

    def add(source: str, start: int, end: int, raw: str, value: Any) -> None:
        if not _looks_like_state(value):
            return
        value, stats = _trimmed_value(value)
        found.append(
            {
                "source": source,
                "start": start,
                "end": end,
                "raw": raw,
                "value": value,
                "stats": stats,
            }
        )

    # ① <state>{…}</state>（最常见：卡沿用本系统的标签）
    for m in _TAG_RE.finditer(src):
        if m.group(1).lower() != "state":
            continue
        value = _loose_json(m.group(2))
        if value is not _NO:
            add(SRC_STATE_TAG, m.start(), m.end(), m.group(2).strip(), value)

    # ② JSON 围栏（```json … ``` / ``` … ```）
    for m in _FENCE_RE.finditer(src):
        lang = (m.group(1) or "").lower()
        body = m.group(2)
        if lang and lang not in ("json", "json5", "jsonc", "js", "javascript"):
            continue  # python / yaml / html 围栏一律不碰
        value = _loose_json(body)
        if value is not _NO:
            add(SRC_FENCE, m.start(), m.end(), body.strip(), value)

    # ③ 酒馆 MVU 指令块
    for m in _MVU_RE.finditer(src):
        body = m.group(2).strip()
        # MVU 的内容可能是 JSON（InitVar 那种），也可能是一串命令 —— 后者不认
        value = _loose_json(body)
        if value is not _NO:
            add(SRC_MVU, m.start(), m.end(), body, value)

    # ④ 卡内嵌 JSON 脚本（HTML 面板把状态塞在 script 里）
    for m in _SCRIPT_JSON_RE.finditer(src):
        value = _loose_json(m.group(1))
        if value is not _NO:
            add(SRC_SCRIPT, m.start(), m.end(), m.group(1).strip(), value)

    # ⑤ 自定义标签包纯 JSON：<status>{…}</status> / <StatusPlaceHolderImpl/> 一带
    for m in _TAG_RE.finditer(src):
        name = m.group(1).lower()
        if name in ("state", "script", "style", "html", "body", "head"):
            continue
        inner = m.group(2).strip()
        if not inner[:1] in ("{", "["):
            continue  # 只有"一眼就是 JSON"的才认，纯文本标签是给人看的正文
        value = _loose_json(inner)
        if value is not _NO:
            add(SRC_INLINE_TAG, m.start(), m.end(), inner, value)

    best = _pick_tail(found, src)
    if best is None:
        return None
    # ★ 占位标签（`<StatusPlaceHolderImpl/>`）本身没有数据，但它是"这张卡自带状态栏"的
    #   强信号。只记来源、不剥正文（它不在尾部时更不该剥）。
    return best


def placeholder_hint(text: str) -> bool:
    """正文里有没有"状态占位标签"（酒馆助手的 `<StatusPlaceHolderImpl/>`）。

    ★ 只用来说明"这张卡自带状态栏"，**不**改变正文：占位标签是作者故意留的空壳，
      不剥掉它也不影响阅读（它渲染出来是空的），剥错了反而去掉作者的锚点。
    """
    return bool(_PLACEHOLDER_RE.search(str(text or "")))


# ==================================================================
#  对外：抽取
# ==================================================================
def extract(content: str) -> tuple[str, dict[str, Any] | None]:
    """把**卡自带的状态块**从正文里摘出来。

    返回 `(剥掉卡状态块后的正文, 认出的状态或 None)`。
    ★ 与 `state.py::extract_state_block` 的分工：
      那个负责**本系统**的 `<state>`（要按 schema 校验、合并、注入提示词）；
      这里负责**卡自己的**状态（只识别、只落库、只显示，不注入、不校验）。
      两条路都在 `engine.save_assistant_reply` 里跑，顺序是先卡状态、后系统状态 ——
      这样同一个 `<state>` 标签不会被两边都抢走：
      ★ 真正的规则是"**这张卡有没有声明 state_schema**"：
        声明了 → `<state>` 归系统（否则每轮的正常状态都会被当成卡状态吞掉）；
        没声明 → `<state>` 归卡（这正是"卡作者自己写的状态块"的典型形态）。
    """
    hit = _detect(content)
    if hit is None:
        return str(content or ""), None
    src = str(content or "")
    cleaned = (src[: hit["start"]] + src[hit["end"] :]).strip()
    return cleaned, {
        **hit,
        "label": SOURCE_LABELS.get(hit["source"], hit["source"]),
    }


def extract_for_session(content: str, has_system_schema: bool) -> tuple[str, dict[str, Any] | None]:
    """`extract` 的会话感知版：按"卡有没有声明 schema"决定 `<state>` 归谁。

    ★ 为什么需要这一层（这是最容易写错的一处）：
      卡声明了 schema 时，模型每轮都会输出 `<state>` —— 那是**给本系统的**，
      如果这里把它当卡状态剥走、落进第二存储，系统的状态就永远是空的，
      状态栏会显示"模型还没输出过"，而正文里那个块又不见了 —— 用户两头落空。
      卡没声明 schema 时正好相反：那个 `<state>` 是作者自己的，
      系统本来就只把它剥掉丢弃（`state.py` 的"没声明就只剥块"），
      现在改成"剥下来存进第二存储"，用户至少能在界面看到作者写的是什么。
    """
    hit = _detect(content)
    if hit is None:
        return str(content or ""), None
    if has_system_schema and hit["source"] == SRC_STATE_TAG:
        # 归系统：这里不动正文，交给 state.py 处理（它会剥块 + 按 schema 解析）
        return str(content or ""), None
    return extract(content)


# ==================================================================
#  漂移检查（只做能确证的两件事）
# ==================================================================
def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def diff_fields(previous: Any, current: Any) -> dict[str, Any]:
    """比较上一轮与这一轮的卡状态，只报**能确证**的变化。

    返回 `{"missing": [...], "retyped": [{key, was, now}], "new": [...]}`。
    ★ 刻意**不**报数值增减（没有 schema 就没有"该往哪个方向"的定义，
      硬报必然误报 —— 这是与 `state.py::deviation` 最关键的区别）。
    ★ 只比第一层（顶层键）：卡状态的嵌套结构各卡不一，深比会淹没用户。
    """
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return {"missing": [], "retyped": [], "new": []}
    missing = [k for k in previous if k not in current]
    new = [k for k in current if k not in previous]
    retyped = []
    for key, old in previous.items():
        if key not in current:
            continue
        was, now = _type_name(old), _type_name(current[key])
        # null 是"这轮还没填"，不算类型变了（否则每轮第一次填都会响一次）
        if was != now and now != "null" and was != "null":
            retyped.append({"key": key, "was": was, "now": now})
    return {"missing": missing[:50], "retyped": retyped[:50], "new": new[:50]}


def drift_notes(diff: dict[str, Any]) -> list[str]:
    """把 diff 翻成人话（写进消息的 notes，用户能直接看懂）。"""
    notes: list[str] = []
    missing = list(diff.get("missing") or [])
    if missing:
        shown = "、".join(missing[:6])
        more = f" 等 {len(missing)} 个" if len(missing) > 6 else ""
        notes.append(f"卡自带状态块里少了上一轮有的字段：{shown}{more}（原样保留上一轮的值）")
    for item in list(diff.get("retyped") or [])[:6]:
        notes.append(
            f"卡自带状态块的字段 {item['key']} 类型变了：{item['was']} → {item['now']}"
            "（本轮按原文记录，没有替你转换）"
        )
    return notes


def schema_hash(value: Any) -> str:
    """卡状态的"形状指纹"：键集合的稳定哈希（用于按形状分桶）。

    ★ 为什么不哈希整个值：值是每轮都变的，那样每轮都是新形状，分桶就没意义了。
      我们要回答的是"卡换了一套字段没有"，所以只取键。
    """
    if isinstance(value, dict):
        shape = sorted(str(k) for k in value)
    elif isinstance(value, list):
        shape = ["[]"]
    else:
        shape = [type(value).__name__]
    digest = hashlib.sha256(json.dumps(shape, ensure_ascii=False).encode("utf-8")).hexdigest()
    return digest[:32]


def dumps(value: Any) -> str | None:
    """落库前序列化（坏值返回 None，绝不抛）。"""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 收敛过的值理论上都能序列化
        logger.warning("卡状态序列化失败：{}", exc)
        return None


def loads(raw: str | None) -> Any:
    """从库里读回卡状态（坏了返回 None，绝不抛）。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


# ==================================================================
#  第二状态存储：写入 / 读取
# ==================================================================
def _write_row(
    db: Any,
    session_id: int,
    value: Any,
    detected: dict[str, Any],
    message_id: int | None,
    drift: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """真正落库的那一步（调用方必须已经有一个 db）。"""
    from sqlalchemy import select

    from app.db.models.card_state import CardState

    shape = schema_hash(value)
    payload = dumps(value)
    if payload is None:
        return None
    row = db.scalar(
        select(CardState).where(
            CardState.session_id == session_id,
            CardState.schema_hash == shape,
        )
    )
    if row is None:
        row = CardState(
            session_id=session_id,
            schema_hash=shape,
            data_json=payload,
            source=str(detected.get("source") or ""),
            raw_json=str(detected.get("raw") or ""),
            message_id=message_id,
            updates=1,
            # ★ 漂移结果必须写在**这次写的那一行**上：少一个键就已经是另一种形状、
            #   新的一行了 —— 写到旧行上的话，界面读最新行时永远看不到这份报告
            #   （这正是第一版实测踩到的：报告写进了旧桶，用户什么都看不到）。
            drift_json=dumps(drift) if drift else None,
        )
        db.add(row)
    else:
        row.data_json = payload
        if drift is not None:
            row.drift_json = dumps(drift)
        row.source = str(detected.get("source") or row.source)
        row.raw_json = str(detected.get("raw") or "")
        row.message_id = message_id
        row.updates = int(row.updates or 0) + 1
    db.flush()
    return {
        "source": row.source,
        "label": SOURCE_LABELS.get(row.source, row.source),
        "schema_hash": shape,
        "updates": int(row.updates or 0),
        "value": value,
        "raw": row.raw_json,
    }


def save(
    db: Any,
    session: Any,
    detected: dict[str, Any],
    *,
    message_id: int | None = None,
    drift: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """把认出来的卡状态写进 `card_states`（按形状分桶，同形状更新、异形状新增）。

    返回写进库的那份快照（给调用方拼提醒 / 前端直接回显），失败返回 None。

    ★ 为什么按 `schema_hash` 分桶而不是一路覆盖：
      卡换了一版（字段全变了）之后，旧形状的数据**不该被静默抹掉** ——
      用户切回旧卡、或想对照"上一版卡到底存了什么"时还得看得到。
      `(session_id, schema_hash)` 唯一，正好表达"每种形状留最新一份"。
    ★ 先查后写（不用 upsert）：MySQL 是 `ON DUPLICATE KEY`、SQLite 是
      `ON CONFLICT DO UPDATE`，两套语法各写一遍最容易只测到其中一种；
      卡状态一轮最多写一次，多一次 SELECT 换来"两种库行为一致"很划算。
    ★ 用**调用方传进来的 db**（与回复落库同一个事务），不再自己开 Session：
      回复落库是一笔事务，卡状态是它的一部分 —— 自己开 Session 会出现
      "回复存了、卡状态没存"或反过来（进程在这两行之间被中断就会）。
    ★ 这里**只 flush 不 commit**：提交由 `save_assistant_reply` 一起做。
      提交顺序反了会留下"卡状态指向一条还不存在的消息"，刷新页面就对不上。
    """
    if not isinstance(detected, dict):
        return None
    value = detected.get("value")
    sid = getattr(session, "id", None)
    if value is None or not sid:
        return None
    try:
        return _write_row(db, int(sid), value, detected, message_id, drift)
    except Exception as exc:  # noqa: BLE001 - 状态是"锦上添花"，写失败绝不能毁掉这轮回复
        logger.warning(
            "卡状态写库失败（本轮回复照常保存）| session_id={} err={}", sid, exc
        )
        return None


def latest(session_id: int, db: Any = None) -> dict[str, Any] | None:
    """读这个会话**最新一次**写入的卡状态（界面面板与优先级判定都读它）。

    ★ 只返回一份（最近更新的那行）：界面要显示的是"现在的状态"；
      历史形状的数据仍在库里（排错/对照用），但不是面板该同时摊给用户看的东西。
    ★ 传了 `db` 就用它（接口里复用请求级 Session）；没传才自己开一个（脚本/测试）。
    """
    own = db is None
    try:
        from sqlalchemy import select

        from app.db.models.card_state import CardState

        if own:
            from app.db.mysql import get_session_factory

            db = get_session_factory()()
        try:
            row = db.scalar(
                select(CardState)
                .where(CardState.session_id == session_id)
                .order_by(CardState.updated_at.desc(), CardState.id.desc())
                .limit(1)
            )
            if row is None:
                return None
            return {
                "source": row.source,
                "label": SOURCE_LABELS.get(row.source, row.source),
                "schema_hash": row.schema_hash,
                "updates": int(row.updates or 0),
                "value": loads(row.data_json),
                "raw": row.raw_json,
                "drift": loads(row.drift_json) or {"missing": [], "retyped": [], "new": []},
                "message_id": row.message_id,
                "updated_at": row.updated_at,
            }
        finally:
            if own:
                db.close()
    except Exception as exc:  # noqa: BLE001 - 读失败 = "没有卡状态"，界面照常显示系统状态栏
        logger.warning("卡状态读库失败 | session_id={} err={}", session_id, exc)
        return None


def record_drift(session_id: int, diff: dict[str, Any], db: Any = None) -> None:
    """把一次漂移检查结果写回最新那行（面板上要能看到"上一轮少了哪些字段"）。"""
    own = db is None
    try:
        from sqlalchemy import select

        from app.db.models.card_state import CardState

        if own:
            from app.db.mysql import get_session_factory

            db = get_session_factory()()
        try:
            row = db.scalar(
                select(CardState)
                .where(CardState.session_id == session_id)
                .order_by(CardState.updated_at.desc(), CardState.id.desc())
                .limit(1)
            )
            if row is None:
                return
            row.drift_json = dumps(diff)
            db.flush()
            if own:
                db.commit()
        finally:
            if own:
                db.close()
    except Exception as exc:  # noqa: BLE001 - 同上：诊断信息写不进去也不该影响回复
        logger.warning("卡状态漂移结果写库失败 | session_id={} err={}", session_id, exc)
