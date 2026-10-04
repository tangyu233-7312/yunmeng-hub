"""剧情滚动总结：**分层合并**（旧总结 + 新一块对话 → 一份新总结）。

==================== 它解决什么问题 ====================
长对话迟早超出上下文窗口，于是必须把"较早的对话"压成一段前情提要。原来的实现
（`context_manager.summarize_dropped`）有两个明显的短板：

1. **只在超预算时才动**：写得再长也没人整理，一超预算就把每条被丢消息截 120 字拼起来，
   拼出来的东西是"逐条摘录"，不是"剧情总结"（人物关系、承诺、伏笔全散落在句子里）。
2. **越叠越长**：每次都是 `旧摘要 + 新一段`，同一段剧情会被反复扫描、重复占 token；
   到 2000 字就掐中间留两头，等于**最该记住的中段最先被丢掉**。

==================== 现在怎么做（用户给的规则）====================
用户的原话：

    1~10 轮对话为一个总结；用户在第 20 轮总结的时候，系统先遍历之前的旧总结，
    然后看 11~20 轮的新聊天记录，然后一起总结为一个新总结，标记为 1~20 轮对话，
    然后将旧总结删除（防止重复扫描占 token）。

所以本模块就干这一件事，四个要点：

    · **按轮分块**：轮 = 一问一答。每积累 `SUMMARY_BLOCK_ROUNDS`（默认 10）轮才合并一次，
      不做"每轮都总结"（那样又慢又贵）。
    · **合并而不是追加**：新的正文 = 模型把「旧总结 + 这一块的对话」重写成一份；
      旧正文**被替换**，所以同一段剧情只占一份 token。
    · **带覆盖标记**：落库 `summary_from_round / summary_to_round`，正文开头也写一行
      `（第 1~20 轮）`，界面与提示词都能一眼看出"这份前情提要管到哪儿"。
    · **绝不因为总结失败而影响对话**：模型调用失败就退回本项目的**本地压缩**
      （`context_manager.summarize_dropped`），并把原因如实记进返回值 ——
      与长期记忆同一套哲学：降级可以，静默降级不行。

==================== 一个刻意的取舍 ====================
合并之后，被覆盖的那一块对话**不再进提示词**（它们已经由这份总结代表了）。
这正是用户要的"防止重复扫描占 token"；代价是那一块的原文细节看不到了 ——
所以界面里仍然能翻到历史消息，只是**发给模型**的是总结。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import get_settings
from app.narrative.context_manager import (
    SUMMARY_MAX_CHARS,
    compress_messages,
    estimate_tokens,
    summarize_dropped,
)

#: 合并总结时给模型的系统提示（要什么、不要什么，写清楚）
SUMMARIZE_SYSTEM_PROMPT = """你在为一个长期进行的角色扮演故事维护「前情提要」。
你的读者是**接下来要继续演这个故事的模型**：它需要靠这段提要接住剧情，不能出戏、不能自相矛盾。

请把用户给你的【旧前情提要】和【新发生的对话】合并成**一份**新的前情提要。

必须保留（这是提要存在的意义）：
- 人物：谁是谁、彼此关系与称呼、身份有无暴露
- 约定与承诺：谁答应了谁什么、欠了谁什么
- 关键事件与后果：发生过什么、造成了什么不可逆的改变
- 伏笔与悬念：还没揭晓的事、被提到但没解释的东西
- 当前处境：地点、目标、手上有什么、正在被什么追着

不要写：
- 不要逐句复述对话，不要写"用户说…角色说…"的流水账
- 不要编造原文里没有的情节，也不要替故事往下推演
- 不要写小标题、不要用 Markdown 列表之外的花哨格式

篇幅：200~500 字，中文，一段或几段短列表都可以。只输出提要正文，不要任何解释。"""

# ---------------- 五种总结模式（用户给的清单）----------------
MODE_CHARACTER = "character"
MODE_PLOT = "plot"
MODE_TABLE = "table"
MODE_COPY = "copy"
MODE_CUSTOM = "custom"
#: 面板里的顺序与名字（前端直接读它渲染下拉框，避免两边各写一份）
MODE_LABELS: dict[str, str] = {
    MODE_CHARACTER: "折叠-角色优先",
    MODE_PLOT: "折叠-剧情优先",
    MODE_TABLE: "表格总结",
    MODE_COPY: "照抄旧记忆生成新记忆",
    MODE_CUSTOM: "自定义",
}
MODE_HINTS: dict[str, str] = {
    MODE_CHARACTER: "优先留住「人」：身份、彼此关系与称呼、对用户的态度、答应过的事；事件只留影响人物的部分",
    MODE_PLOT: "优先留住「事」：事件因果链、不可逆的改变、伏笔、当前处境；人物只留推动剧情所需的特征",
    MODE_TABLE: "输出一张 Markdown 表格：角色 | 关系 | 关键事件 | 当前状态 | 未了结",
    MODE_COPY: "★ 不调用模型、不花 token：照抄旧总结，并把最新的聊天记录压缩后追加成新的记忆",
    MODE_CUSTOM: "用你自己写的提示词（也可以勾选「从守卫预设读取」，把提示词写在提示词预设里）",
}
#: 每种模式的**追加指令**（接在 SUMMARIZE_SYSTEM_PROMPT 之后一起当系统提示）。
#:
#: ★ 这五套提示词是**本项目自己写的**：参考产品只公开了模式名与一句说明
#:   （"折叠-角色优先 / 折叠-剧情优先 / 表格总结 / 照抄旧记忆生成新记忆 / 自定义"），
#:   没有公开过原文提示词，所以这里按那五个名字的语义自己设计了一套。
#:   用户对某一档不满意时**不需要改代码**：面板里选「自定义」写自己的提示词即可
#:   （`custom_prompt` 非空会**整段替换**系统提示）。
#: ★ 五档的差别只体现在**组织方式**上（按人 / 按因果链 / 按表格 / 原样叠加 / 用户自定），
#:   共同约束（不许编、不许推演、不许流水账）留在 SUMMARIZE_SYSTEM_PROMPT 里，
#:   避免同一句话在五个地方各写一遍、改的时候漏掉一处。
_MODE_INSTRUCTIONS: dict[str, str] = {
    MODE_CHARACTER: (
        "【本次要求 · 折叠-角色优先】\n"
        "把这份提要写成一份**人物档案**，而不是事件流水账。\n"
        "每个重要角色（用户也要占一条）依次交代：\n"
        "1. 身份与外貌中**后面剧情用得上**的特征（不要写无关的形容）\n"
        "2. 与用户的关系、彼此怎么称呼（称呼若变过，写明从什么变成什么）\n"
        "3. 当前对用户的态度与原因（信任 / 戒备 / 欠着人情 / 记恨 / 依赖…）\n"
        "4. 答应过、隐瞒过、欠着什么（**没兑现的承诺必须留**）\n"
        "5. 身上带着的、只属于他的东西或秘密\n"
        "事件只保留「改变了人物关系或处境」的那些，其余用一个短句带过"
        "（例：二人合力击退了追兵）。\n"
        "篇幅 200~500 字，用「角色名：……」这样的短列表，不要小标题。"
    ),
    MODE_PLOT: (
        "【本次要求 · 折叠-剧情优先】\n"
        "把这份提要写成一条**因果链**，按时间顺序推进，不要按人物分块。\n"
        "每个关键节点写清三件事：发生了什么 → 因为什么 → 造成了什么**不可逆**的后果。\n"
        "正文之后另起两段（用方括号标注）：\n"
        "【伏笔】还没揭晓的事：被提到却没解释的东西、埋了没用上的线索、说了一半的话\n"
        "【当前处境】此刻在哪、要做什么、手上有什么、正被什么追着或卡在哪一步\n"
        "人物只保留推动剧情必需的几个特征（身份 / 能力 / 立场），不要展开写关系。\n"
        "篇幅 200~500 字，用编号短句，不要小标题。"
    ),
    MODE_TABLE: (
        "【本次要求 · 表格总结】\n"
        "只输出一张 Markdown 表格，列**固定**为下面五列（不要增删列、不要改列名）：\n"
        "| 角色 | 关系 | 关键事件 | 当前状态 | 未了结 |\n"
        "每一行一个角色，**用户也必须占一行**。单元格这样写：\n"
        "· 关系：与用户的关系 + 彼此的称呼\n"
        "· 关键事件：他做过的最影响剧情的一两件事（不要复述对话）\n"
        "· 当前状态：在哪、在做什么、身体 / 情绪 / 立场如何\n"
        "· 未了结：欠下的承诺、没揭晓的秘密、悬着的冲突；确实没有就写「—」\n"
        "表格之外**最多**再写一行「当前处境：……」。不要前言，不要总结，不要解释。"
    ),
    MODE_COPY: (
        "【本次要求 · 照抄旧记忆生成新记忆】\n"
        "这一档**默认不调用模型**（0 token）：系统会把旧总结原样保留，"
        "只在末尾追加一段由本地压缩得到的新内容。\n"
        "万一被显式要求调用模型（例如以后加了别的入口），按下面的规矩做：\n"
        "1. 旧总结正文**逐字照抄**，一个字都不要改写 —— 它是用户已经认可的记忆\n"
        "2. 只把【新发生的对话】压成一小段（不超过 200 字），以「（第 X~Y 轮）」开头\n"
        "3. 追加在旧总结之后，两段之间空一行\n"
        "4. 新内容与旧总结冲突时**不要改旧文**，只在末尾用「（注：……）」写明冲突\n"
        "不要合并、不要重写、不要调整顺序。"
    ),
    MODE_CUSTOM: (
        "【本次要求 · 自定义】\n"
        "用户没有提供自定义提示词，因此这一轮暂时按「折叠-角色优先」处理。"
        "在「记忆」面板里填入自己的提示词之后，"
        "系统提示词会被用户的原文**整段替换**（包括上面那段公共约束）。"
    ),
}

#: 「记忆总结」提示词块的标识（写在守卫预设里时用这个 identifier/名字，本模块会去读它）
PRESET_BLOCK_IDENTIFIER = "memorySummary"
PRESET_BLOCK_NAME = "记忆总结"

# ==================================================================
#  同一会话的总结**串行化**（防重复总结）
# ==================================================================
#: 「正在总结中」的统一说法（接口据此返回 409，界面据此提示，别各写一份文案）
BUSY_REASON = "上一次总结还在进行中（正在调用模型），请稍等它完成再点"

#: 每个会话一把锁：总结要调模型、可能跑好几秒，期间用户完全可能**再点一次**
#: （真实事故：横幅点了没反馈，用户连点几下 → 连着总结了好几遍、白花了好几份 token）。
#: 所以第二次进来**直接拒绝并说明"正在总结中"**，而不是再跑一遍。
_LOCKS: dict[int, "threading.Lock"] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(session: Any) -> "threading.Lock":
    key = int(getattr(session, "id", 0) or 0)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def is_busy(session: Any) -> bool:
    """这个会话现在是否正在总结（界面/接口据此给出"正在总结中"而不是再跑一遍）。"""
    lock = _lock_for(session)
    if lock.acquire(blocking=False):
        lock.release()
        return False
    return True

#: 会话级设置的默认值（与 .env 的默认保持一致；面板里改只影响这条会话）
def default_settings() -> dict[str, Any]:
    settings = get_settings()
    return {
        # 记忆总结总开关（关掉 = 完全不总结，也不提醒）
        "enabled": bool(settings.SUMMARY_ENABLED),
        # 到点**自动**总结（会花一次模型调用）。★ 默认关：用户明确说过"不强制"，
        # 不能在他没点过任何按钮的情况下偷偷花他的 token。
        "auto": bool(settings.SUMMARY_AUTO_ENABLED),
        # 到点**弹横幅提醒**（不花钱，只是提醒）
        "remind": True,
        # 每几轮触发一次
        "rounds": int(settings.SUMMARY_BLOCK_ROUNDS),
        # 总结模式（见 MODE_LABELS）
        "mode": MODE_CHARACTER,
        # 自定义提示词（只在 mode=custom 时用；空 = 退回内置模板）
        "prompt": "",
        # 从守卫预设里读「记忆总结」块当提示词
        "use_preset_prompt": False,
        # 单份总结的字数上限
        "max_chars": int(settings.SUMMARY_MAX_CHARS),
        # 总结用哪个模型配置（None = 跟会话模型）
        "provider_id": None,
    }


#: rounds 的可选范围（面板里给个滑杆/数字框；6~10 是参考产品的区间，我们放宽些）
MIN_ROUNDS = 2
MAX_ROUNDS = 50
#: 字数上限可选项
MAX_CHARS_CHOICES = (1000, 2000, 4000, 8000, 20000)


def normalize_settings(raw: Any) -> dict[str, Any]:
    """把用户提交的设置校验成规范形状（坏的字段一律回落到默认值，绝不抛异常）。"""
    base = default_settings()
    if not isinstance(raw, dict):
        return base
    out = dict(base)
    for key in ("enabled", "auto", "remind", "use_preset_prompt"):
        if key in raw:
            out[key] = bool(raw[key])
    if "rounds" in raw:
        try:
            rounds = int(raw["rounds"])
        except (TypeError, ValueError):
            rounds = base["rounds"]
        out["rounds"] = min(max(rounds, MIN_ROUNDS), MAX_ROUNDS)
    if "mode" in raw and str(raw["mode"]) in MODE_LABELS:
        out["mode"] = str(raw["mode"])
    if "prompt" in raw:
        out["prompt"] = str(raw["prompt"] or "")[:8000]
    if "max_chars" in raw:
        try:
            limit = int(raw["max_chars"])
        except (TypeError, ValueError):
            limit = base["max_chars"]
        out["max_chars"] = min(max(limit, 200), 50000)
    if "provider_id" in raw:
        value = raw["provider_id"]
        out["provider_id"] = int(value) if value not in (None, "", 0, "0") else None
    return out


def load_settings(session: Any) -> dict[str, Any]:
    """读出这条会话的记忆总结设置（坏了就按默认值，绝不抛异常）。"""
    raw = getattr(session, "summary_settings_json", None)
    if not raw:
        return default_settings()
    try:
        return normalize_settings(json.loads(raw))
    except (TypeError, ValueError):
        logger.warning("记忆总结设置不是合法 JSON，已按默认值处理 | session_id={}", getattr(session, "id", None))
        return default_settings()


def save_settings(session: Any, settings: dict[str, Any]) -> dict[str, Any]:
    """写回设置（**不 commit**），返回规范化后的值。"""
    normalized = normalize_settings(settings)
    session.summary_settings_json = json.dumps(normalized, ensure_ascii=False)
    return normalized


# ---------------- 版本历史（支持「恢复上一次」）----------------
#: 保留几个历史版本（够"恢复上一次"就行，不必做版本管理）
HISTORY_LIMIT = 5


def load_history(session: Any) -> list[dict[str, Any]]:
    raw = getattr(session, "summary_history_json", None)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _push_history(session: Any, *, reason: str) -> None:
    """把**当前**的总结压进历史（在覆盖它之前调用）。"""
    text = str(getattr(session, "rolling_summary", "") or "").strip()
    if not text:
        return
    items = load_history(session)
    items.append(
        {
            "at": _now_iso(),
            "reason": reason,
            "text": text,
            "from_round": int(getattr(session, "summary_from_round", 0) or 0),
            "to_round": int(getattr(session, "summary_to_round", 0) or 0),
        }
    )
    session.summary_history_json = json.dumps(items[-HISTORY_LIMIT:], ensure_ascii=False)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")

_ROUND_HEADER_RE = re.compile(r"^（第\s*(\d+)\s*[~～-]\s*(\d+)\s*轮）\s*$", re.M)


# ==================================================================
#  轮数计算
# ==================================================================
def round_of(message: Any) -> int:
    """这条消息属于第几轮（轮 = 一问一答，以用户消息计数）。"""
    return int(getattr(message, "round_no", 0) or 0)


def count_rounds(messages: list[Any]) -> int:
    """**完整**的轮数：一问一答都齐了才算一轮。

    ★ 为什么不是"数用户消息条数"：装配提示词时，本轮用户消息**已经落库**、
      而助手回复还没生成。若按用户条数算，第 10 轮刚问完就会被判成"攒够 10 轮"，
      于是把"一个还没答完的回合"也总结进去 —— 用户的原话是
      "第 20 轮时看 11~20 轮"，那就必须等这一轮答完再合并。
    ★ 开场白是 assistant 且前面没有用户消息，所以它不算一轮。
    """
    rounds = 0
    pending_user = False
    for message in messages:
        role = str(getattr(message, "role", "") or "").lower()
        if role == "user":
            pending_user = True
        elif role == "assistant" and pending_user:
            rounds += 1
            pending_user = False
    return rounds


def count_user_turns(messages: list[Any]) -> int:
    """用户发言的条数（轮次编号用；与 `count_rounds` 的差别见上）。"""
    return sum(1 for m in messages if str(getattr(m, "role", "")) == "user")


def round_index_map(messages: list[Any]) -> dict[int, int]:
    """给每条消息算出"它是第几轮"：`{message_id: round_no}`。

    开场白（第一条 assistant）算第 0 轮之前，归给第 1 轮 —— 它属于故事的开头，
    跟第一轮用户发言一起被总结掉最自然。
    """
    index: dict[int, int] = {}
    current = 0
    for message in messages:
        role = str(getattr(message, "role", "") or "").lower()
        if role == "user":
            current += 1
        index[int(getattr(message, "id", 0) or 0)] = max(current, 1)
    return index


def covered_rounds(session: Any) -> int:
    """这个会话的总结已经覆盖到第几轮（没有总结 = 0）。"""
    return int(getattr(session, "summary_to_round", 0) or 0)


def should_merge(session: Any, total_rounds: int, *, settings: dict[str, Any] | None = None) -> bool:
    """是否**到点**了（距离上次覆盖又积累了整整一块）。

    ★ 注意：这里只回答"到点了没有"，**不回答"要不要花 token"**。
      是否真的调用模型由调用方按设置决定（auto 开才自动做，否则只提醒用户）。
    """
    config = settings or load_settings(session)
    if not config.get("enabled"):
        return False
    block = int(config.get("rounds") or get_settings().SUMMARY_BLOCK_ROUNDS)
    return total_rounds - covered_rounds(session) >= block


def pending_rounds(session: Any, messages: list[Any]) -> int:
    """还有多少轮没被总结覆盖（界面用它显示"已积累 N 轮"）。"""
    return max(count_rounds(messages) - covered_rounds(session), 0)


def reminder_state(session: Any, messages: list[Any], settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """给界面看的"要不要提醒用户总结"状态（**不花钱**）。

    返回 {due, pending, rounds, from_round, to_round, cost_tokens, reason}：
      · due=True 且 auto=False 时，前端弹横幅让用户自己决定要不要总结；
      · cost_tokens 是这次总结的**预估**消耗，如实告诉用户（我们没有点数系统）。
    """
    config = settings or load_settings(session)
    pending = pending_rounds(session, messages)
    block = int(config.get("rounds") or 8)
    covered = covered_rounds(session)
    should = bool(config.get("enabled")) and pending >= block
    return {
        "due": should,
        "pending": pending,
        "rounds": block,
        "from_round": covered + 1,
        "to_round": covered + block,
        "auto": bool(config.get("auto")),
        "remind": bool(config.get("remind")),
        "mode": config.get("mode"),
        "cost_tokens": estimate_summary_cost(config, messages, covered, covered + block),
    }


def estimate_summary_cost(
    settings: dict[str, Any], messages: list[Any], from_round: int, to_round: int
) -> int:
    """预估这次总结要花多少 token（≈ 输入 + 输出），用于界面如实显示。"""
    if settings.get("mode") == MODE_COPY:
        return 0  # 照抄模式不调模型
    chunk = block_messages(messages, from_round, to_round)
    # ★ 系统提示词的**两种来源**都要算进去：自定义提示词，或「公共约束 + 本模式指令」。
    #   以前只算公共约束那一段，五档模式加了各自的指令之后预估会偏小
    #   （界面上的"预计消耗"是要用户据此决定点不点的，不能少报）。
    custom = str(settings.get("prompt") or "").strip()
    if custom:
        system_chars = len(custom)
    else:
        mode = str(settings.get("mode") or MODE_CHARACTER)
        system_chars = len(SUMMARIZE_SYSTEM_PROMPT) + len(
            _MODE_INSTRUCTIONS.get(mode) or _MODE_INSTRUCTIONS[MODE_CHARACTER]
        )
    prompt_chars = system_chars + sum(
        len(str(getattr(m, "content", "") or "")[:400]) for m in chunk
    )
    old_chars = len(str(settings.get("_previous") or ""))
    return estimate_tokens("x" * (prompt_chars + old_chars)) + int(
        settings.get("max_chars", 2000)
    ) // 3


# ==================================================================
#  取块 / 拼提示
# ==================================================================
def block_messages(
    messages: list[Any], start_round: int, end_round: int
) -> list[Any]:
    """取出第 `start_round`~`end_round` 轮之间的消息（含两端）。"""
    index = round_index_map(messages)
    return [
        m
        for m in messages
        if start_round <= index.get(int(getattr(m, "id", 0) or 0), 0) <= end_round
    ]


def split_previous(summary: str | None) -> str:
    """把旧总结里的"（第 X~Y 轮）"表头剥掉 —— 合并时要的是正文，表头由我们重写。"""
    text = (summary or "").strip()
    if not text:
        return ""
    return _ROUND_HEADER_RE.sub("", text).strip()


def within_covered(session: Any, message: Any) -> bool:
    """这条消息是否已经被总结覆盖（覆盖到的就不该再进提示词）。"""
    until = getattr(session, "summarized_until_message_id", None)
    if not until:
        return False
    return int(getattr(message, "id", 0) or 0) <= int(until)


def filter_uncovered(session: Any, messages: list[Any]) -> list[Any]:
    """只留还没被总结覆盖的消息（发给模型的上下文 = 总结 + 这部分原文）。"""
    return [m for m in messages if not within_covered(session, m)]


def build_summarize_messages(
    previous_summary: str | None,
    block: list[Any],
    *,
    from_round: int,
    to_round: int,
    mode: str = MODE_CHARACTER,
    custom_prompt: str = "",
) -> list[Any]:
    """拼出"让模型做合并总结"的对话（[system, user]）。

    `mode` 决定系统提示里的**组织方式**（角色优先 / 剧情优先 / 表格 / 照抄 / 自定义），
    `custom_prompt` 非空时整段替换系统提示（"自定义"模式）。
    """
    from app.llm.schema import ChatMessage

    system = (custom_prompt or "").strip()
    if not system:
        instruction = _MODE_INSTRUCTIONS.get(mode) or _MODE_INSTRUCTIONS[MODE_CHARACTER]
        system = f"{SUMMARIZE_SYSTEM_PROMPT}\n\n{instruction}"

    old = split_previous(previous_summary)
    lines: list[str] = []
    for message in block:
        role = str(getattr(message, "role", "") or "").lower()
        who = {"user": "用户", "assistant": "角色", "system": "系统"}.get(role, role)
        content = " ".join(str(getattr(message, "content", "") or "").split())
        if not content:
            continue
        # 总结阶段可以多留一点原文（这是唯一一次读它们的机会）：每条 400 字
        lines.append(f"{who}：{content[:400]}")
    body = "\n".join(lines) or "（这一块没有可用内容）"

    user_prompt = (
        f"【旧前情提要】（覆盖到第 {from_round - 1} 轮；若为空说明这是第一次总结）\n"
        f"{old or '（无）'}\n\n"
        f"【新发生的对话】（第 {from_round}~{to_round} 轮）\n{body}\n\n"
        f"请把上面两部分合并成一份新的前情提要，覆盖第 1~{to_round} 轮。"
    )
    return [
        ChatMessage.system(system),
        ChatMessage.user(user_prompt),
    ]


def resolve_prompt(db: Any, session: Any, settings: dict[str, Any]) -> str:
    """决定这次总结用哪段提示词（返回空串 = 用内置模板）。

    优先级（用户拍板：提示词也可以写在**守卫预设**里）：
      ① `mode=custom` 且填了自定义提示词 → 用它
      ② 勾了"从守卫预设读取" → 取守卫预设里标识为 `memorySummary` / 名为「记忆总结」的块
      ③ 都没有 → 空串（用内置模板）
    """
    mode = settings.get("mode")
    if mode == MODE_CUSTOM and str(settings.get("prompt") or "").strip():
        return str(settings["prompt"]).strip()
    if mode == MODE_CUSTOM or settings.get("use_preset_prompt"):
        block = _preset_summary_block(db, session)
        if block:
            return block
        if mode == MODE_CUSTOM:
            return ""  # 自定义但没写内容、预设里也没有 → 退回内置模板（并如实说明）
    return ""


def _preset_summary_block(db: Any, session: Any) -> str:
    """从（内置）守卫预设里取「记忆总结」块的内容；没有就返回空串。"""
    if db is None or session is None:
        return ""
    try:
        from app.narrative import presets as presets_mod
        from app.services import prompt_preset_service

        row = prompt_preset_service.get_builtin(db, getattr(session, "user_id", None))
        if row is None:
            return ""
        config = presets_mod.from_config(getattr(row, "config", None))
    except Exception as exc:  # noqa: BLE001 - 读预设失败不该影响总结
        logger.debug("读取守卫预设里的记忆总结块失败，已忽略 | err={}", exc)
        return ""
    for block in getattr(config, "blocks", []) or []:
        identifier = str(getattr(block, "identifier", "") or "")
        name = str(getattr(block, "name", "") or "")
        if getattr(block, "enabled", True) is False:
            continue
        if identifier == PRESET_BLOCK_IDENTIFIER or name == PRESET_BLOCK_NAME:
            content = str(getattr(block, "content", "") or "").strip()
            if content:
                return content
    return ""


def copy_merge(previous_summary: str | None, block: list[Any], *, from_round: int, to_round: int) -> str:
    """模式 4「照抄旧记忆生成新记忆」：**不调模型**，旧记忆原文照抄 + 追加本地摘录。

    ★ 这是唯一 0 API 成本的模式：适合"我就是想省 token，内容粗糙点无所谓"的用户。
      代价是文本会越来越长，所以到字数上限时仍然掐中间留两头。
    """
    body = split_previous(previous_summary)
    added = compress_messages(block)
    marker = f"【第 {from_round}~{to_round} 轮新增】"
    if not body:
        return f"{marker}\n{added}" if added else ""
    if not added:
        return body
    return f"{body}\n\n{marker}\n{added}"


# ==================================================================
#  合并
# ==================================================================
@dataclass
class MergeOutcome:
    """一次合并的结果（给调用方判断要不要提示用户）。"""

    merged: bool = False
    from_round: int = 0
    to_round: int = 0
    summary: str = ""
    used_model: bool = False
    warning: str | None = None
    reason: str = ""
    mode: str = ""
    manual: bool = False

    @property
    def label(self) -> str:
        return format_label(self.from_round, self.to_round)

    def to_dict(self) -> dict[str, Any]:
        return {
            "merged": self.merged,
            "from_round": self.from_round,
            "to_round": self.to_round,
            "used_model": self.used_model,
            "label": self.label if self.merged else "",
            "warning": self.warning,
            "reason": self.reason,
            "mode": self.mode,
            "mode_label": MODE_LABELS.get(self.mode, ""),
            "manual": self.manual,
            "chars": len(self.summary or ""),
            "tokens": estimate_tokens(self.summary or ""),
        }


def format_label(from_round: int, to_round: int) -> str:
    """覆盖区间的标签（写进正文第一行，界面与提示词共用）。"""
    return f"（第 {from_round}~{to_round} 轮）"


def append_dropped(session: Any, dropped: list[Any], messages: list[Any]) -> bool:
    """**超预算兜底路径**：把被裁掉的消息并进现有总结（不调模型、不新增表头）。

    ★ 为什么需要它：分层合并是"每 N 轮一次"，但上下文预算可能在这之前就爆了。
      那时被裁掉的消息也得进前情提要，否则就真的丢了。
      两条路径必须写**同一份**正文（一份 token），所以这里：
        · 保留原有正文，把新裁掉的几条本地压缩后接在后面；
        · 覆盖区间向后延伸到"最后一条被裁消息所在的轮"；
        · 仍然只有一个表头 —— 不会出现两份前情提要叠加。
    返回是否真的改动了总结。
    """
    if not dropped:
        return False
    body = split_previous(session.rolling_summary)
    added = compress_messages(dropped)
    if not added:
        return False
    merged = f"{body}\n{added}" if body else added
    index = round_index_map(messages)
    last = dropped[-1]
    last_round = index.get(int(getattr(last, "id", 0) or 0), 0)
    from_round = int(getattr(session, "summary_from_round", 0) or 0) or 1
    to_round = max(int(getattr(session, "summary_to_round", 0) or 0), last_round)
    session.rolling_summary = f"{format_label(from_round, to_round)}\n{clamp_summary(merged)}"
    session.summary_from_round = from_round
    session.summary_to_round = to_round
    session.summarized_until_message_id = int(getattr(last, "id", 0) or 0) or None
    return True


def clamp_summary(text: str, *, limit: int | None = None) -> str:
    """把总结掐到字符上限：**掐中间留两头**。

    开头是故事的前提（丢了接不上），结尾是最新的处境（最影响下一轮），
    中间那段离当前剧情最远、丢了最不致命。
    """
    max_chars = int(limit or get_settings().SUMMARY_MAX_CHARS or SUMMARY_MAX_CHARS)
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    return f"{text[:keep]}\n……（中段前情已省略）……\n{text[-keep:]}"


def merge_block(
    *,
    session: Any,
    adapter: Any,
    messages: list[Any],
    db: Any = None,
    settings: dict[str, Any] | None = None,
    force: bool = False,
) -> MergeOutcome:
    """做一次合并总结，并写回会话字段（不 commit）。

    参数：
        session   会话行（读 `rolling_summary` / `summary_*_round` / 设置，写回同样字段）
        adapter   LLM 适配器（用当前会话的模型配置，主备 failover 也一并生效）
        messages  该会话的**全部**消息（升序）
        db        可选：用于读取守卫预设里的「记忆总结」提示词块
        force     True = **用户手动点了「立即总结」**：不再要求"攒够一整块"，
                  有几轮就总结几轮（尊重用户的主动意愿）
        settings  省略时从会话读

    ★ 谁来决定花不花 token（用户明确要求"不强制"）：
        auto=True  → 调用方（engine）会在到点时自动调它（`force=False`）
        auto=False → 到点只弹横幅，**用户点了按钮**才由接口带 `force=True` 调它
      本函数自己不做"该不该花钱"的判断，只做"能不能总结"的判断。
    """
    config = settings or load_settings(session)
    if not config.get("enabled"):
        return MergeOutcome(reason="记忆总结已关闭")

    # ★ 串行化：同一会话同一时刻只允许一次总结。
    #   否则"用户连点几下"会连着跑几次模型调用、写几份总结（真实发生过）。
    lock = _lock_for(session)
    if not lock.acquire(blocking=False):
        logger.info("这个会话正在总结中，已忽略这次重复触发 | session_id={}", getattr(session, "id", None))
        return MergeOutcome(reason=BUSY_REASON)
    try:
        return _merge_locked(
            session=session,
            adapter=adapter,
            messages=messages,
            db=db,
            config=config,
            force=force,
        )
    finally:
        lock.release()


def _merge_locked(
    *,
    session: Any,
    adapter: Any,
    messages: list[Any],
    db: Any,
    config: dict[str, Any],
    force: bool,
) -> MergeOutcome:
    """真正做合并的那一段（调用方已经拿到会话锁）。"""
    covered = covered_rounds(session)
    total = count_rounds(messages)
    # ★ 一轮完整对话都没有时**拒绝**（而不是把开场白当成"第 1 轮"总结掉）：
    #   实测踩过：会话被"撤回"清空后点「立即总结」，它把开场白总结成了「第 1~1 轮」，
    #   白花一次模型调用、内容还是空的。
    if total <= 0:
        return MergeOutcome(reason="还没有完成一轮对话（一问一答都齐了才能总结）")
    if force:
        # 手动：把"还没被覆盖的全部轮次"一次总结掉（至少一轮）
        to_round = max(total, covered + 1)
    else:
        if not should_merge(session, count_rounds(messages), settings=config):
            return MergeOutcome(reason="还没攒够一整块对话")
        to_round = covered + int(config.get("rounds") or 8)

    from_round = int(getattr(session, "summary_from_round", 0) or 0) or 1
    chunk = block_messages(messages, covered + 1, to_round)
    if not chunk:
        return MergeOutcome(reason=f"第 {covered + 1}~{to_round} 轮没有可用消息")

    mode = str(config.get("mode") or MODE_CHARACTER)
    custom_prompt = resolve_prompt(db, session, config)
    used_model = False
    warning: str | None = None
    if mode == MODE_COPY:
        # ★ 模式 4：不调模型（0 token）
        text = copy_merge(
            session.rolling_summary, chunk, from_round=covered + 1, to_round=to_round
        )
    else:
        prompt = build_summarize_messages(
            session.rolling_summary,
            chunk,
            from_round=covered + 1,
            to_round=to_round,
            mode=mode,
            custom_prompt=custom_prompt,
        )
        try:
            from app.llm.params import cheap_reasoning_effort
            from app.llm.schema import ChatRequest

            # ★ 剧情总结也是**机械任务**（把一段对话压成摘要），思考纯烧钱 ——
            #   与翻译中间件共用同一条闸门：只在真机探测确认该模型接受思考强度时才降。
            result = adapter.chat(
                ChatRequest(messages=prompt, reasoning_effort=cheap_reasoning_effort(adapter))
            )
            text = str(getattr(result, "content", "") or "").strip()
            if not text:
                raise ValueError("模型返回了空总结")
            used_model = True
        except Exception as exc:  # noqa: BLE001 - 总结失败绝不能影响对话
            logger.warning(
                "剧情总结调用失败，已退回本地压缩 | session_id={} err={}",
                getattr(session, "id", None),
                exc,
            )
            text = summarize_dropped(split_previous(session.rolling_summary), chunk)
            warning = f"剧情总结这一步调用模型失败，已用本地压缩代替（内容会粗糙一些）：{exc}"

    text = clamp_summary(text, limit=config.get("max_chars"))
    if not text:
        return MergeOutcome(reason="总结结果为空，保持原样")

    # ★ 关键：**替换**旧总结（不是追加），并把覆盖区间推进 —— 同一段剧情只占一份 token。
    #   覆盖之前先把旧版本压进历史，界面上的「恢复上一次」就有东西可恢复。
    _push_history(session, reason="merge" if not force else "manual")
    session.rolling_summary = f"{format_label(from_round, to_round)}\n{text}"
    session.summary_from_round = from_round
    session.summary_to_round = to_round
    last = chunk[-1]
    session.summarized_until_message_id = int(getattr(last, "id", 0) or 0) or None
    logger.info(
        "剧情总结已合并 | session_id={} 覆盖第 {}~{} 轮 | 模式={} 用模型={} 字符={} 手动={}",
        getattr(session, "id", None),
        from_round,
        to_round,
        mode,
        used_model,
        len(text),
        force,
    )
    return MergeOutcome(
        merged=True,
        from_round=from_round,
        to_round=to_round,
        summary=text,
        used_model=used_model,
        warning=warning,
        mode=mode,
        manual=force,
    )


def edit_summary(session: Any, text: str, *, settings: dict[str, Any] | None = None) -> tuple[bool, str]:
    """用户手动编辑总结正文（保留覆盖区间，旧版本进历史）。返回 `(是否改动, 说明)`。"""
    content = (text or "").strip()
    if not content:
        return False, "总结内容不能为空"
    if not str(getattr(session, "rolling_summary", "") or "").strip():
        return False, "这条会话还没有总结，先做一次总结再编辑"
    config = settings or load_settings(session)
    _push_history(session, reason="edit")
    from_round = int(getattr(session, "summary_from_round", 0) or 0) or 1
    to_round = int(getattr(session, "summary_to_round", 0) or 0) or from_round
    # 用户可能把表头也一起贴进来了：先剥掉再重新盖一个，避免出现两行表头
    body = split_previous(content)
    session.rolling_summary = f"{format_label(from_round, to_round)}\n{clamp_summary(body, limit=config.get('max_chars'))}"
    return True, "总结已更新"


def restore_previous(session: Any) -> tuple[bool, str]:
    """「恢复上一次」：把最近一个历史版本换回来（当前版本进历史，可再换回去）。"""
    items = load_history(session)
    if not items:
        return False, "没有可恢复的历史版本"
    last = items.pop()
    text = str(last.get("text") or "").strip()
    if not text:
        return False, "上一版内容是空的"
    session.summary_history_json = json.dumps(items, ensure_ascii=False)
    current = str(getattr(session, "rolling_summary", "") or "").strip()
    if current:
        items.append(
            {
                "at": _now_iso(),
                "reason": "restore",
                "text": current,
                "from_round": int(getattr(session, "summary_from_round", 0) or 0),
                "to_round": int(getattr(session, "summary_to_round", 0) or 0),
            }
        )
    session.summary_history_json = json.dumps(items[-HISTORY_LIMIT:], ensure_ascii=False)
    session.rolling_summary = text
    if last.get("from_round"):
        session.summary_from_round = int(last["from_round"])
    if last.get("to_round"):
        session.summary_to_round = int(last["to_round"])
    return True, "已恢复上一次的总结"


def snapshot(session: Any, messages: list[Any] | None = None) -> dict[str, Any]:
    """给「记忆管理面板」用的完整状态（设置 + 正文 + 覆盖 + 提醒 + 历史）。"""
    settings = load_settings(session)
    history = load_history(session)
    reminder = reminder_state(session, messages or [], settings)
    return {
        "settings": settings,
        "modes": [
            {"value": key, "label": MODE_LABELS[key], "hint": MODE_HINTS[key]}
            for key in (MODE_CHARACTER, MODE_PLOT, MODE_TABLE, MODE_COPY, MODE_CUSTOM)
        ],
        "rounds_range": [MIN_ROUNDS, MAX_ROUNDS],
        "max_chars_choices": list(MAX_CHARS_CHOICES),
        "content": str(getattr(session, "rolling_summary", "") or "") or "",
        "coverage": (
            {"from_round": int(session.summary_from_round or 1), "to_round": int(session.summary_to_round)}
            if int(getattr(session, "summary_to_round", 0) or 0) > 0
            else None
        ),
        "history": [
            {
                "at": item.get("at"),
                "reason": item.get("reason"),
                "from_round": item.get("from_round"),
                "to_round": item.get("to_round"),
                "chars": len(str(item.get("text") or "")),
            }
            for item in history
        ],
        "reminder": reminder,
        # ★ 正在总结中：前端据此把按钮置灰（防"点了没反应 → 连点 → 重复总结"）
        "busy": is_busy(session),
    }
