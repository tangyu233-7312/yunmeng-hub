"""上下文管理与 token 估算。

==================== 这个模块解决的问题 ====================
对话越来越长之后，把全部历史塞进提示词一定会撞上模型的上下文窗口。
撞上之后通常有两种结果，都很糟：

  · 厂商直接返回 400（错误信息还是英文的，用户看不懂）
  · 厂商悄悄截断（用户以为模型"忘了"前面的剧情，却找不到原因）

所以必须由我们自己**主动裁剪**：按 ContextBudget 算出输入预算，
超预算时优先丢掉最早的对话，并把丢掉的内容压缩成「剧情滚动摘要」。

    ┌─────────────── input_budget ───────────────┐
    │ 系统提示词 │ 剧情滚动摘要 │ 最近若干轮对话 │ 尾注 │
    └────────────────────────────────────────────┘
                    ↑ 丢的永远是最早的那些轮次

==================== 为什么不引入 tiktoken ====================
估算是为了**决定丢哪些消息**，不是为了精确计费。tiktoken 只服务于 OpenAI 系
（Claude 的分词器不同，本地模型又各有各的），装了它反而给人"算得很准"的错觉。
这里用「按字符类别加权」的启发式估算，误差通常在 ±10% 以内，
而 ContextBudget 里本来就已经预留了 5% 的安全余量来吸收它。

★ 规则（实测校准，短文本的误差在可接受范围）：
    中日韩文字      1 字 ≈ 1 token
    其它字符        1 字 ≈ 0.25 token（英文约 4 字符 1 token）
    每条消息        再加 4 token 的结构开销（role / 分隔符）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import BadRequestError
from app.llm.params import ContextBudget
from app.llm.schema import ChatMessage

#: 每条消息的结构开销（role 字段、role 与 content 之间的分隔符等）
PER_MESSAGE_OVERHEAD = 4

#: 深度注入的 system 块在降级成 user 消息时加的声明前缀
#: （与 app/narrative/presets.DEPTH_SYSTEM_HEADER 保持一致，这里再写一份是为了
#:  让"上下文裁剪"这个模块不依赖预设模块 —— 它的职责不该被预设绑架）
DEPTH_SYSTEM_HEADER = "[系统指令 · 并非用户发言]"

#: 中文/日文/韩文的 Unicode 区间（这些字符基本是「一字一 token」）
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3000, 0x303F),   # CJK 标点
    (0x3040, 0x30FF),   # 日文假名
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x4E00, 0x9FFF),   # CJK 基本区
    (0xAC00, 0xD7AF),   # 韩文
    (0xF900, 0xFAFF),   # CJK 兼容表意文字
    (0xFF00, 0xFFEF),   # 全角字符
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_tokens(text: str | None) -> int:
    """估算一段文本的 token 数（见模块文档的规则说明）。"""
    if not text:
        return 0

    cjk = 0
    other = 0
    for char in text:
        if _is_cjk(char):
            cjk += 1
        else:
            other += 1

    # 其它字符按 4 字符 1 token 折算；不足 1 的部分向上取整，
    # 避免极短文本（如 "hi"）被算成 0
    return cjk + max(0, -(-other // 4))


def estimate_message_tokens(message: Any) -> int:
    """估算一条消息的 token 数（含结构开销）。

    接受 ChatMessage 或任意有 role / content 属性的对象（ORM 行也行）。
    """
    content = str(getattr(message, "content", "") or "")
    return estimate_tokens(content) + PER_MESSAGE_OVERHEAD


def estimate_messages_tokens(messages: list[Any]) -> int:
    """估算一串消息的总 token 数。"""
    return sum(estimate_message_tokens(m) for m in messages)


# ==================================================================
#  裁剪结果
# ==================================================================
@dataclass
class ContextPlan:
    """上下文裁剪的结果。

    除消息列表本身，还带回「丢了什么」，因为这件事必须让用户看见：
    模型突然不记得前面的剧情，用户如果不被告知"上下文已裁剪"，
    只会觉得"这个模型变笨了"。
    """

    messages: list[ChatMessage] = field(default_factory=list)
    """最终要发送的消息列表。"""

    estimated_tokens: int = 0
    """估算的输入 token 数。"""

    input_budget: int = 0
    """本次允许使用的输入预算（= context_window − max_tokens − 安全余量）。"""

    history_kept: int = 0
    """保留了最近多少条历史消息。"""

    history_dropped: int = 0
    """因超预算被丢掉了多少条历史消息。"""

    dropped_messages: list[Any] = field(default_factory=list)
    """被丢掉的消息本身（用于生成滚动摘要）。"""

    summary_used: int = 0
    """本次注入的滚动摘要占了多少 token（0 表示没有摘要）。"""

    recalled_memories: int = 0
    """本次召回了多少条长期记忆（3.9）。"""

    memory_error: str | None = None
    """记忆检索失败的原因（None = 正常）。

    ★ 它必须一路传到界面上：向量库坏了的时候对话照样能用（降级），
      但如果界面上什么都不说，用户会以为"记忆功能生效了只是没召回"，
      于是反复重写设定，永远查不到真正的原因。
    """

    world_book_entries: int = 0
    """本次注入了多少条世界书设定（关键词触发的结果）。"""

    world_book_matched: int = 0
    """本次命中了几条（含因预算被丢掉的）。"""

    system_truncated: bool = False
    """系统提示词是否被截断（世界书条目太多时会这样，必须告知用户）。"""

    depth_injected: int = 0
    """本次插进对话历史的预设块数量（破甲通常靠它生效）。"""

    notes: list[str] = field(default_factory=list)
    """装配/降级层面的说明（例如"深度 system 块被降级为 user"）。

    ★ 这些说明必须能到界面上：降级是我们主动做的妥协，
      不告诉用户，他就会以为"预设一模一样地生效了"。
    """

    @property
    def was_trimmed(self) -> bool:
        return self.history_dropped > 0 or self.system_truncated

    def to_dict(self) -> dict[str, Any]:
        """给前端的元信息（每个数字都能对上，便于用户自己判断）。"""
        return {
            "estimated_input_tokens": self.estimated_tokens,
            "input_budget": self.input_budget,
            "history_kept": self.history_kept,
            "history_dropped": self.history_dropped,
            "summary_tokens": self.summary_used,
            "recalled_memories": self.recalled_memories,
            "memory_error": self.memory_error,
            "world_book_entries": self.world_book_entries,
            "world_book_matched": self.world_book_matched,
            "system_truncated": self.system_truncated,
            "depth_injected": self.depth_injected,
            "trimmed": self.was_trimmed,
            "note": "输入预算 = 上下文窗口 − 最大输出（含思考 token）− 安全余量",
        }


# ==================================================================
#  异常
# ==================================================================
class ContextOverflowError(BadRequestError):
    """系统提示词本身就超出输入预算，连一条历史都放不下。

    ★ 为什么要报错而不是"硬塞进去"？
      硬塞的结果是厂商返回一个 400（英文的、指向不明的），
      或者模型直接开始胡言乱语。与其静默降级，不如明确告诉用户
      「把最大输出调小或把上下文窗口调大」——这也是本项目的既定原则。
    """

    code = "CONTEXT_OVERFLOW"
    message = "上下文放不下这条请求"


# ==================================================================
#  滚动摘要
# ==================================================================
#: 滚动摘要保留的最大字符数（约等于 1000 token，足够交代前情）
SUMMARY_MAX_CHARS = 2000


def compress_messages(dropped: list[Any]) -> str:
    """把一批被丢掉的消息压成"逐条摘录"（**本地压缩**，不调模型）。

    ★ 单独抽出来是为了给分层总结（`app/narrative/summary.py`）复用：
      那条路径要的是"把这几条并进现有正文"，不想要这里带的表头
      （表头由分层总结自己按覆盖区间重写）。
    """
    lines: list[str] = []
    for item in dropped:
        role = str(getattr(item, "role", "user") or "user").lower()
        who = {"user": "用户", "assistant": "角色", "system": "设定"}.get(role, role)
        content = " ".join(str(getattr(item, "content", "") or "").split())
        if not content:
            continue
        # 每条只留前 120 字：摘要的目的是"记得发生过什么"，不是复述原文
        lines.append(f"- {who}：{content[:120]}")
    return "\n".join(lines)


def summarize_dropped(previous_summary: str | None, dropped: list[Any]) -> str:
    """把被丢掉的消息压成一段「前情提要」。

    ★ 诚实说明本步（3.8）的实现方式：
      这里做的是**本地压缩**（截取每条被丢消息的开头），而不是再调一次模型
      去生成摘要。原因有二：
        1. 裁剪发生在**发请求之前**，此时再调一次模型会让响应时间翻倍；
        2. 用户对成本敏感，不能每轮对话都偷偷多花一次调用。
      ★ 第八轮起：**分层合并**（每 N 轮让模型重写一份）才是主路径，见
        `app/narrative/summary.py`；本函数退居"兜底降级"与"预算裁剪"两个用途，
        签名不变（当年就是按"将来换成模型摘要"设计的）。

    previous_summary 传入上一次的摘要，会拼接在开头（形成"越来越长"的前情）。
    """
    if not dropped:
        return (previous_summary or "").strip()

    body_lines = compress_messages(dropped)
    if not body_lines:
        return (previous_summary or "").strip()

    body = "（较早的对话已被压缩）\n" + body_lines
    merged = f"{previous_summary.strip()}\n{body}" if (previous_summary or "").strip() else body

    if len(merged) > SUMMARY_MAX_CHARS:
        # ★ 掐中间、留两头，而不是简单砍尾巴：
        #   开头是"很久以前发生过什么"（故事的前提，丢了就接不上），
        #   结尾是"刚刚发生了什么"（最影响下一轮回复）。
        #   中间那一段是最先被遗忘也不致命的（离当前剧情最远）。
        keep = SUMMARY_MAX_CHARS // 2
        merged = f"{merged[:keep]}\n……（中段前情已省略）……\n{merged[-keep:]}"
    return merged


# ==================================================================
#  主流程
# ==================================================================
def prepare_context(
    messages: list[ChatMessage],
    *,
    budget: ContextBudget,
    summary: str | None = None,
    reserve_tokens: int = 0,
    stats: dict[str, Any] | None = None,
    depth_messages: list[tuple[int, ChatMessage]] | None = None,
    mid_system_supported: bool = True,
) -> ContextPlan:
    """把消息列表裁进输入预算。

    参数：
        messages        已拼好的消息（system 在最前，最后可能是尾注）
        budget          上下文预算（由 app/llm/params.py 计算）
        summary         已有的剧情滚动摘要（会插在系统提示词之后）
        reserve_tokens  额外预留（例如向量记忆召回要占的位置）
        stats           额外回报给界面的统计（世界书命中数、召回条数、记忆错误…），
                        会被原样带进 ContextPlan —— 免得为了几个数字再传一层参数
        depth_messages  预设的深度注入块 [(深度, ChatMessage), …]。
                        ★ **裁剪之后才插进去**，理由见 inject_depth_messages 的注释。
        mid_system_supported  当前协议是否允许 system 消息出现在对话中间
                        （Anthropic 不允许 → 深度 system 块降级为 user + 声明）

    裁剪顺序（从最不重要的开始丢）：
        1. 最早的对话轮次
        2. 尾注（宁可丢尾注也要保住剧情）
        3. 系统提示词过长时按比例截断
    """
    extra = dict(stats or {})

    if not messages:
        return ContextPlan(input_budget=budget.input_budget, **extra)

    # ---------- 拆出三段结构 ----------
    system_msgs = [m for m in messages if m.is_system]
    dialogue = [m for m in messages if not m.is_system]

    # 末条若是"系统指令"形态的尾注，单独拎出来，方便最后才丢
    tail: ChatMessage | None = None
    if dialogue and dialogue[-1].role == "user" and dialogue[-1].content.startswith(
        "[系统指令"
    ):
        tail = dialogue.pop()

    # ---------- 固定开销 ----------
    head_tokens = estimate_messages_tokens(system_msgs)
    tail_tokens = estimate_message_tokens(tail) if tail is not None else 0
    summary_text = (summary or "").strip()
    summary_tokens = estimate_tokens(summary_text) + (PER_MESSAGE_OVERHEAD if summary_text else 0)

    available = max(0, budget.input_budget - reserve_tokens - head_tokens - tail_tokens - summary_tokens)

    # ---------- 从后往前装历史 ----------
    kept: list[ChatMessage] = []
    used = 0
    for message in reversed(dialogue):
        cost = estimate_message_tokens(message)
        if used + cost > available:
            break
        kept.append(message)
        used += cost
    kept.reverse()

    dropped = dialogue[: len(dialogue) - len(kept)]

    # ---------- 系统提示词本身放不下：截断 ----------
    system_truncated = False
    if head_tokens > budget.input_budget:
        # 先保证"能发出去"，再让界面如实提示用户去调大预算
        head_tokens = budget.input_budget
        system_truncated = True
        system_msgs = [
            ChatMessage.system(_truncate_to_tokens(system_msgs[0].content, max(64, budget.input_budget - 8)))
        ]
        kept = []
        dropped = list(dialogue)
        tail = None
        tail_tokens = 0
        available = 0

    if not budget.is_usable:
        raise ContextOverflowError(
            f"输入预算只剩 {budget.input_budget} token，放不下角色设定与对话历史",
            detail={
                "input_budget": budget.input_budget,
                "context_window": budget.context_window,
                "max_output_tokens": budget.max_output_tokens,
                "suggestion": "请调小「最大输出 Token」，或调大模型配置里的「上下文窗口」",
            },
        )

    # ---------- 重新组装 ----------
    final: list[ChatMessage] = list(system_msgs)

    if summary_text:
        # 摘要放在系统提示词之后、历史之前：它扮演的是"更早的历史"这个角色
        final.append(
            ChatMessage.system(f"## 前情提要（更早的对话已压缩）\n{summary_text}")
        )

    final.extend(kept)
    if tail is not None:
        final.append(tail)

    # ---------- ★ 深度注入：裁剪之后才插回去 ----------
    # 顺序很关键：先裁剪（按预算丢最早的对话），再把预设块插进剩余历史的指定深度。
    # 反过来做的话，深度块会被当成"旧消息"丢掉，或者把预算算错 —— 而且不会报错。
    depth_notes: list[str] = []
    if depth_messages:
        # 只重排 system 之后的部分：system 段必须保持在最前面
        head_count = len(final) - (len(kept) + (1 if tail is not None else 0))
        head_part = final[:head_count]
        body_part = final[head_count:]
        body_part, depth_notes = inject_depth_messages(
            body_part, depth_messages, mid_system_supported=mid_system_supported
        )
        final = head_part + body_part

    if summary_text:
        summary_tokens = estimate_tokens(summary_text) + PER_MESSAGE_OVERHEAD

    return ContextPlan(
        messages=final,
        estimated_tokens=estimate_messages_tokens(final),
        input_budget=budget.input_budget,
        history_kept=len(kept),
        history_dropped=len(dropped),
        dropped_messages=list(dropped),
        summary_used=summary_tokens if summary_text else 0,
        system_truncated=system_truncated,
        depth_injected=len(depth_messages or []),
        notes=list((extra.pop("notes", None) or [])) + depth_notes,
        **extra,
    )


def inject_depth_messages(
    dialogue: list[ChatMessage],
    depth_messages: list[tuple[int, ChatMessage]],
    *,
    mid_system_supported: bool = True,
) -> tuple[list[ChatMessage], list[str]]:
    """把"深度注入"的预设块插进对话历史。

    ★ 这是整个预设功能里最能决定成败的一步。

      破甲（让模型突破自带限制的那套提示词）之所以有效，靠的不是把它写进
      系统提示词"嘱咐"一遍，而是把 **user / assistant 成对**的文本
      插在最近几条消息**之前**，让模型以为"我们双方已经就此达成过一致"。
      插错位置 = 效果完全消失，而且**不会报任何错**。

    规则（与 SillyTavern 对齐）：
        depth 表示"插在**倒数第 depth 条**消息之前"。
        所以 depth=1 → 最后一条之前；depth=0 → 追加到最后；
        实际下标 = 消息总数 - depth（再夹到合法范围内）。

    返回 (插好之后的消息, 降级说明列表)。
    """
    if not depth_messages:
        return list(dialogue), []

    notes: list[str] = []
    total = len(dialogue)
    # (下标, 顺序权重, 消息)；顺序权重保证同一深度的多条保持原相对次序
    inserts: list[tuple[int, int, ChatMessage]] = []
    downgraded = 0

    for order, (depth, message) in enumerate(depth_messages):
        # 目标下标：从末尾往前数 depth 条。夹到 [0, total]，
        # 这样"深度超过历史长度"会插在最前面，而不是越界或丢失。
        index = total - max(0, int(depth))
        index = max(0, min(index, total))
        role = str(getattr(message, "role", "user") or "user").lower()
        content = str(getattr(message, "content", "") or "")
        if role == "system" and not mid_system_supported:
            # 有些协议（Anthropic）不允许 system 消息出现在对话中间，
            # 只能降级成 user。★ 但绝不能让它看起来像用户说的话，
            #   所以加一句身份声明 —— 不加的话模型会把破甲当成用户的要求，
            #   回复一句"好的我会注意"，反而削弱效果。
            content = f"{DEPTH_SYSTEM_HEADER}\n{content}"
            role = "user"
            downgraded += 1
        inserts.append((index, order, ChatMessage(role=role, content=content)))

    # 从后往前插，这样前面的下标不会被后面的插入挤位
    inserts.sort(key=lambda item: (item[0], item[1]), reverse=True)
    merged = list(dialogue)
    for index, _order, message in inserts:
        merged.insert(index, message)

    if downgraded:
        notes.append(
            f"有 {downgraded} 个深度注入的 system 块被降级为 user 消息"
            "（当前协议不允许 system 出现在对话中间），并加了「"
            f"{DEPTH_SYSTEM_HEADER}」声明以免模型误当成用户发言。"
        )
    return merged, notes


def _truncate_to_tokens(text: str, limit: int) -> str:
    """按估算 token 数截断文本（从尾部丢，保留开头的人设部分）。"""
    if estimate_tokens(text) <= limit:
        return text
    # 二分查找：估算函数不具备可逆性，用逼近的方式找切点
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= limit:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + "\n……（设定过长已截断）"
