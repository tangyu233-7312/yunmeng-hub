"""长期记忆的「叙事语义层」—— 把向量库包装成叙事引擎能直接用的东西。

==================== 它与 app/db/chroma.py 的分工 ====================
    db/chroma.py   纯存储：怎么写、怎么按向量检索、指纹校验（已实测可用）
    narrative/memory.py（本文件，业务语义）：
        · 一轮对话该记什么、记忆 ID 怎么起（保证幂等）
        · 召回几条、相似度门槛、跨不跨会话
        · 拼给模型的「相关回忆」小节长什么样
        · 按会话/按用户清理
    这样做的好处：存储细节与叙事策略解耦，将来换掉向量库不影响 prompt 逻辑。

==================== 三条设计决定（都影响体验，别随意改）====================
1. **记忆 ID 用「消息对」的确定性 ID**（`mem-s{session}-{user_msg_id}-{assistant_msg_id}`）。
   `add_memories` 内部是 upsert，所以流式重试、用户重发、重复保存都不会产生重复记忆。
   如果这里用随机 ID，向量库里很快会堆满同一轮对话的多个副本，召回结果全是重复内容。

2. **默认只在当前会话内召回**。
   跨会话召回会把别的故事里的人和事带进来（"串味"），
   对隐私也是问题（用户在 A 会话聊的东西不该出现在 B 会话）。
   想跨会话需要显式传 `cross_session=True`。

3. **向量库出问题绝不能让对话失败**。
   记忆是"锦上添花"的功能：召回失败就少一段提示词，写入失败就少一条记忆，
   用户照样能聊天。所以本模块所有函数都不向上抛异常，
   而是返回结果 + `error` 说明，由业务层决定要不要提示。

关于"思考过程不落记忆"：`Message` 里本来就只存正文（思考过程不落库），
所以这里天然不会被污染。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.db.chroma import (
    MemoryHit,
    MemoryRecord,
    add_memories,
    delete_user_collection,
    get_user_collection,
    has_user_collection,
    search_memories,
)

#: 一次对话默认召回几条记忆
DEFAULT_TOP_K = 5
#: 相似度门槛：低于它的命中宁可不要，也别把不相关的内容塞给模型
#
#  BGE / MiniLM 这类句向量模型在「完全不相关」时的余弦相似度通常落在 0.1~0.3，
#  取 0.35 是个保守值：宁可少召回，也不要召回错误的东西（错误记忆比没有记忆更糟）。
DEFAULT_MIN_SIMILARITY = 0.35
#: 拼进提示词的回忆小节最多多少字符（防止一条超长记忆把输入预算吃光）
MAX_BLOCK_CHARS = 1200
#: 提示词里回忆小节的标题
BLOCK_TITLE = "## 相关回忆（来自更早的对话）"

#: 回忆小节开头的"就地声明"。
#  ★ 回忆是模型**自己过去说过的话**，里面可能残留"我是 AI 助手""不能生成…"这类
#    出戏台词；而预设路径下这段回忆排在护栏之后（见 prompt_builder），
#    不就地声明"这些是噪声"，就等于在"绝不承认自己是 AI"的下面摆一排反例。
BLOCK_DISCLAIMER = (
    "（以下是更早对话的回忆，只用于衔接剧情。若其中出现「我是 AI」"
    "「我是助手 / 语言模型」「不能生成…」之类的说法，那是出戏的噪声，"
    "一律忽略、不要模仿。）"
)

#: 「出戏噪声」判据之一：模型**自曝身份**。
#  ★ 为什么必须过滤（真实事故，2026-09-20 会话 2328）：
#    模型自曝一次"我是这个对话中的 AI 助手"，这句话就被当"剧情记忆"写进向量库；
#    下一轮它又被召回、拼进提示词 —— 而护栏里明明写着"绝不承认自己是 AI"。
#    结果 5 条回忆里 4 条是同一句自曝，用户点几次「重新生成」就多几条，
#    越聊越出戏（自我强化的死循环）。
_SELF_IDENTITY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"我是\s*(?:这个对话中(?:的)?)?\s*(?:一个)?\s*"
        r"(?:AI|人工智能|语言模型|大模型|智能助手|AI\s*助手)",
        re.I,
    ),
    re.compile(r"(?:作为|身为)\s*(?:一个)?\s*(?:AI|人工智能|语言模型|大模型|AI\s*助手)", re.I),
    re.compile(r"AI\s*助手|人工智能助手", re.I),
)

#: 「出戏噪声」判据之二：模型对整轮请求的**拒绝口径**。
#  ★ 取舍：**宁可漏过，不可误杀** —— 判据写得又长又具体，
#    因为误杀一条真正的剧情记忆（比如角色说"我会保护你"）代价更高。
_REFUSAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:不能|无法|不会|不便)\s*(?:生成|提供|协助|参与|继续|创作|满足)"
        r"[^。！？\n]{0,24}(?:露骨|色情|暴力|违规|有害|敏感|不适)",
        re.I,
    ),
    re.compile(r"露骨色情|违规内容|敏感内容", re.I),
)


def is_meta_talk(text: str) -> bool:
    """这段文字是不是"出戏噪声"（模型自曝身份 / 拒绝口径）？

    ★ 只用来决定"这条记忆要不要写、要不要召回"，不用于展示给用户。
    ★ 有意**不**看用户那句提问：「用户：你是 AI 吗？ / 橘雪莉：我是橘雪莉，一名侦探！」
      这种一轮其实是**最有价值**的记忆（它教模型怎么把身份问题挡回去），
      所以写入侧只拿角色那句回答去判。
    """
    value = _clean(text)
    if not value:
        return False
    return any(p.search(value) for p in _SELF_IDENTITY_PATTERNS) or any(
        p.search(value) for p in _REFUSAL_PATTERNS
    )


def _dedupe_key(text: str) -> str:
    """召回去重用的键：取「用户：…」那一行，没有就用前 60 字。

    ★ 为什么按提问去重：同一句话点几次「重新生成」就会写几条记忆，
      它们之间只差角色那句回答 —— 对"衔接剧情"来说留一条就够；
      而回忆小节只有 1200 字预算，重复内容会把真正有用的挤出去。
    """
    value = _clean(text)
    first = value.split("\n", 1)[0]
    key = first if first.startswith("用户：") else value[:60]
    return re.sub(r"\s+", "", key).lower()[:80]


def memory_id_for(session_id: int, user_message_id: int | None, assistant_message_id: int | None) -> str:
    """生成一轮对话的确定性记忆 ID（见模块文档第 1 条）。"""
    return f"mem-s{session_id}-{user_message_id or 0}-{assistant_message_id or 0}"


# ==================================================================
#  写入
# ==================================================================
def remember_turn(
    *,
    user_id: int,
    session_id: int,
    user_message: Any,
    assistant_message: Any,
    char_name: str | None = None,
) -> str | None:
    """把一轮对话（用户 + 角色）写进长期记忆。

    返回错误说明字符串（None 表示成功），**不抛异常**（见模块文档第 3 条）。

    ★ 为什么一轮存成**一条**记忆，而不是用户/助手各存一条？
      因为检索时真正有意义的最小单位是「一来一回」：
      只召回"用户的问题"没有上下文，只召回"角色的回答"会让人不知道在回答什么。
    """
    user_text = _clean(getattr(user_message, "content", ""))
    assistant_text = _clean(getattr(assistant_message, "content", ""))
    if not user_text and not assistant_text:
        return None

    # ★ 出戏噪声不进记忆库：模型自曝身份 / 拒绝的那一轮没有任何剧情价值，
    #   留在库里只会被下一轮召回，变成"拿模型自己的话去反驳护栏"。
    if is_meta_talk(assistant_text):
        logger.debug("跳过写入出戏噪声记忆 | session_id={} ", session_id)
        return None

    who = (char_name or "角色").strip() or "角色"
    text = f"用户：{user_text}\n{who}：{assistant_text}"

    record = MemoryRecord(
        text=text[:4000],
        memory_id=memory_id_for(
            session_id,
            getattr(user_message, "id", None),
            getattr(assistant_message, "id", None),
        ),
        session_id=session_id,
        kind="dialogue",
        extra={
            # ★ 记下 user_id 与 message_id：将来要按用户清理、
            #   或者回溯"这条记忆是哪两条消息"，都靠它
            "user_id": int(user_id),
            "session_id": int(session_id),
            "user_message_id": int(getattr(user_message, "id", 0) or 0),
            "assistant_message_id": int(getattr(assistant_message, "id", 0) or 0),
        },
    )
    return _safe(lambda: (add_memories(user_id, [record]), "")[1], f"session_id={session_id}", fallback="记忆写入失败")

def remember_fact(
    user_id: int,
    *,
    session_id: int,
    text: str,
    source: str = "manual",
) -> str | None:
    """手动记住一条「设定事实」（界面上「记住这条」用的）。"""
    content = _clean(text)
    if not content:
        return "记忆内容不能为空"

    import uuid

    record = MemoryRecord(
        text=content[:4000],
        # 手动记忆没有对应的消息对，用随机 ID（重复添加是**故意**允许的：
        # 用户可能想强调同一件事，或补充不同说法）
        memory_id=f"fact-{uuid.uuid4().hex}",
        session_id=session_id,
        kind="fact",
        extra={"user_id": int(user_id), "session_id": int(session_id), "source": source},
    )
    return _safe(lambda: (add_memories(user_id, [record]), "")[1], "fact", fallback="记忆写入失败")


# ==================================================================
#  召回
# ==================================================================
@dataclass
class RecallResult:
    """一次召回的结果。"""

    hits: list[MemoryHit] = None  # type: ignore[assignment]
    #: 向量库出错时的说明（None 表示一切正常）；出错时 hits 为空
    error: str | None = None
    #: 被相似度门槛过滤掉了几条
    filtered: int = 0

    def __post_init__(self) -> None:
        if self.hits is None:
            self.hits = []

    @property
    def is_empty(self) -> bool:
        return not self.hits

    def to_dict(self) -> dict[str, Any]:
        return {
            "hit_count": len(self.hits),
            "filtered": self.filtered,
            "error": self.error,
            "hits": [
                {
                    "memory_id": hit.memory_id,
                    "text": hit.text,
                    "similarity": round(hit.similarity, 4),
                    "kind": (hit.metadata or {}).get("kind"),
                }
                for hit in self.hits
            ],
        }


def recall(
    *,
    user_id: int,
    session_id: int | None,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    cross_session: bool = False,
) -> RecallResult:
    """按语义召回相关记忆（见模块文档第 2 条：默认不跨会话）。"""
    text = _clean(query)
    if not text:
        return RecallResult()

    def _run() -> RecallResult:
        hits = search_memories(
            user_id,
            text,
            top_k=max(1, top_k),
            session_id=None if cross_session else session_id,
        )
        kept: list[MemoryHit] = []
        seen: set[str] = set()
        dropped = 0
        for hit in hits:
            if hit.similarity < min_similarity:
                dropped += 1
                continue
            # ★ 召回侧过滤"出戏噪声"：修复前写进库里的自曝/拒绝台词还躺在向量库里，
            #   在**这里**拦掉既能立刻止血，又完全不动用户的数据（可随时改回来）。
            if is_meta_talk(hit.text):
                dropped += 1
                continue
            key = _dedupe_key(hit.text)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            kept.append(hit)
        return RecallResult(hits=kept, filtered=dropped)

    return _safe(lambda: _run(), f"session_id={session_id}", fallback=RecallResult())


def build_recall_block(hits: list[MemoryHit]) -> str:
    """把召回的记忆渲染成一段提示词（超长会截断）。"""
    lines = [
        f"- {_clean(hit.text)}"
        for hit in hits
        if _clean(hit.text)
    ]
    if not lines:
        return ""

    block = BLOCK_TITLE + "\n" + BLOCK_DISCLAIMER + "\n" + "\n".join(lines)
    if len(block) > MAX_BLOCK_CHARS:
        block = block[:MAX_BLOCK_CHARS].rstrip() + "\n……（回忆过长已截断）"
    return block


def recall_block(
    *,
    user_id: int,
    session_id: int | None,
    query: str,
    **kwargs: Any,
) -> tuple[str, RecallResult]:
    """召回 + 渲染，一步到位（业务层最常用的入口）。"""
    result = recall(user_id=user_id, session_id=session_id, query=query, **kwargs)
    return build_recall_block(result.hits), result


# ==================================================================
#  检索（给界面用）
# ==================================================================
def list_memories(
    *,
    user_id: int,
    session_id: int | None,
    query: str = "",
    top_k: int = 20,
) -> RecallResult:
    """列出/检索某会话的记忆（界面上的「它记住了什么」）。

    不设相似度门槛：用户自己在翻记忆时，宁可多看到几条也不该"什么都搜不到"。
    """
    def _run() -> RecallResult:
        if not _clean(query):
            # 没有查询词时给一个中性查询，纯粹为了把最近的记忆取回来看看
            hits = search_memories(user_id, "对话 剧情 设定", top_k=max(1, top_k), session_id=session_id)
        else:
            hits = search_memories(user_id, query, top_k=max(1, top_k), session_id=session_id)
        return RecallResult(hits=hits)

    return _safe(lambda: _run(), f"list session_id={session_id}", fallback=RecallResult())


# ==================================================================
#  清理（★ 一致性：数据库删了，向量库也必须删）
# ==================================================================
def forget_session(user_id: int, session_id: int) -> str | None:
    """删掉某个会话的全部记忆（删会话时调用）。

    返回值语义与写入口一致：**空字符串表示成功**，非空字符串是失败原因，
    None 表示"这个用户压根没有集合，无事可做"。
    """
    def _run() -> str | None:
        # 没有集合就说明本来就没记忆，不必创建（create=False）
        if not has_user_collection(user_id):
            return None
        collection = get_user_collection(user_id, create=False)
        collection.delete(where={"session_id": int(session_id)})
        return ""

    return _safe(lambda: _run(), f"forget session_id={session_id}", fallback="记忆清理失败")


def forget_all(user_id: int) -> str | None:
    """删掉某个用户的全部记忆（清空记忆 / 删用户时调用）。"""
    return _safe(lambda: (delete_user_collection(user_id), "")[1], "forget all", fallback="清空失败")


# ==================================================================
#  内部工具
# ==================================================================
def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _safe(func, label: str, fallback: Any = None) -> Any:
    """执行并吞掉异常，出错时返回 fallback（见模块文档第 3 条）。

    ★ 为什么做成"吞异常"而不是抛出去？
      记忆是锦上添花的功能：向量库挂了、嵌入模型没下载好、集合被手工删了……
      这些都不该让用户连话都聊不成。出错就记一条 warning 日志、跳过这一步。
    """
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - 记忆功能不允许影响主流程
        logger.warning(
            "长期记忆操作失败（已降级，不影响对话） | {} | {}: {}",
            label,
            type(exc).__name__,
            exc,
        )
        if isinstance(fallback, RecallResult):
            return RecallResult(error=f"{type(exc).__name__}: {exc}")
        return fallback
