"""世界书关键词触发检索（lorebook keyword scanning）。

==================== 它解决什么问题 ====================
世界书是一堆「关键词 → 设定文本」的条目。如果把它们**全部**塞进系统提示词：

    · 上下文预算会被长期占用（一本写了几百条的世界书能吃掉上万 token）
    · 模型会因为看到大量与当前剧情无关的设定而分心

所以正确做法是：**只把当前剧情真正提到的那些设定注进去**。

    最近若干条对话（scan_depth）→ 逐个条目匹配关键词 → 命中的按预算挑选（token_budget）
        → 交给 prompt_builder 渲染成「世界设定」小节

==================== 几个刻意的决定 ====================
1. **只扫最近 scan_depth 条消息**（默认 8）。
   再往前的剧情早就无所谓了，扫全量只会让"远古关键词"永远命中。

2. **只做字面包含匹配，不做分词**。
   中文没有空格，而且角色卡作者写的关键词往往就是人名/地名/专有名词，
   直接子串匹配最符合直觉、也最容易预测（SillyTavern 也是这么做的）。
   代价是「龙」会命中「龙虾」这类子串误伤 —— 这一点在界面上如实标注即可。

3. **纯本地计算，不调用任何模型**。
   扫描发生在每次发消息之前，如果它要调模型，响应时间会翻倍、成本也翻倍。

4. **预算用完就停，并把"被丢掉几条"回报给上层**。
   静默丢弃设定会让用户以为"我明明写了这条设定，模型却不知道"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.narrative import state_schema

#: 默认扫描最近多少条消息（对应规范里的 scan_depth）
DEFAULT_SCAN_DEPTH = 8
#: 默认允许注入多少 token 的设定正文（对应规范里的 token_budget）
DEFAULT_TOKEN_BUDGET = 1024


@dataclass
class ScanResult:
    """一次关键词扫描的结果。"""

    #: 命中的条目（原始 dict，保持导入时的字段，便于往返不丢东西）
    entries: list[dict[str, Any]] = field(default_factory=list)
    #: 因为 token 预算不够而被丢掉的命中条目数
    dropped: int = 0
    #: 扫描了多少条消息
    scanned_messages: int = 0
    #: 一共命中了多少条（含被丢掉的）
    matched: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "injected": len(self.entries),
            "dropped": self.dropped,
            "scanned_messages": self.scanned_messages,
        }


@dataclass
class EntryMatch:
    """一条**命中了关键词**的世界书条目（带命中明细）。

    ★ 为什么需要它（而不是只要 `entries`）：
      `scan()` 只回答"注入了哪些条目"，而混合检索要拿它跟语义通道**一起打分/排序**，
      所以必须知道"命中了哪几个关键词、作者给的 insertion_order 是多少" ——
      否则关键词通道只能靠"命中即 1 分"，融合时无信息可用。
    """

    entry: dict[str, Any]
    #: 作者给的插入顺序（同时被当作作者优先级：小的更靠前）
    insertion_order: int
    #: 命中的关键词（原文，未小写化）
    matched_keys: tuple[str, ...]
    #: 条目在世界书数组里的原始下标（排序稳定用）
    index: int
    #: 关键词通道的原始相关度分（见 `score_match`）
    score: float

    @property
    def content(self) -> str:
        return str(self.entry.get("content") or "")


def score_match(matched_keys: tuple[str, ...]) -> float:
    """关键词通道的原始分：命中越多、关键词越长，越相关。

    ★ 刻意做得**简单且可解释**（论文里要能一句话说清）：
        分数 = 命中关键词数 + 0.1 × 命中关键词总字数
      "长关键词命中"比"短关键词命中"更有信息量（如"补给船"比"灯"具体），
      所以给它一个小的长度奖励；数量为主，长度为辅，避免长词一家独大。
    """
    if not matched_keys:
        return 0.0
    return len(matched_keys) + 0.1 * sum(len(key) for key in matched_keys)


def scan_candidates(
    book: Any,
    messages: list[Any],
    *,
    scan_depth: int = DEFAULT_SCAN_DEPTH,
) -> list[EntryMatch]:
    """找出关键词命中的条目，并带上命中明细（**不**做 token 预算裁剪）。

    顺序与世界书里的原始顺序一致；排序与预算交给调用方
    （`scan()` 按 insertion_order + 预算裁剪，混合检索则先融合再统一裁剪）。
    """
    if book is None:
        return []

    all_entries = [
        entry
        for entry in (getattr(book, "entries", None) or [])
        if isinstance(entry, dict)
    ]
    if not all_entries:
        return []

    window = messages if scan_depth <= 0 else messages[-scan_depth:]
    # 把所有待扫文本拼成一段：这样"跨消息的关键词"也能命中，
    # 而且只做一次包含判断，比逐条消息 × 逐条关键词快得多
    haystack = "\n".join(
        str(getattr(item, "content", "") or "") for item in window
    ).lower()

    matches: list[EntryMatch] = []
    for index, entry in enumerate(all_entries):
        if entry.get("enabled", True) is False:
            continue

        # ★ 状态栏格式条目（名字以「[状态栏]」开头 / keys 含 state_definition）
        #   是**给引擎看的定义**，不是给模型看的设定：
        #   它已经被 state_schema 解析成字段清单 + 作者原文，
        #   若再当普通设定注入，同一段话会进提示词两遍，而且那段里带着
        #   `<state>` 示例，模型很可能把示例当正文抄出来。
        if state_schema.is_book_schema_entry(entry):
            continue

        keys = [str(k).strip().lower() for k in (entry.get("keys") or [])]
        keys = [k for k in keys if k]
        if not keys:
            continue
        matched = tuple(key for key in keys if key in haystack)
        if not matched:
            continue

        # insertion_order 小的先插入（与规范/SillyTavern 一致）
        try:
            order = int(entry.get("insertion_order") or 0)
        except (TypeError, ValueError):
            order = 0
        matches.append(
            EntryMatch(
                entry=entry,
                insertion_order=order,
                matched_keys=matched,
                index=index,
                score=score_match(matched),
            )
        )
    return matches


def scan(
    book: Any,
    messages: list[Any],
    *,
    scan_depth: int = DEFAULT_SCAN_DEPTH,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> ScanResult:
    """在最近 scan_depth 条消息里找命中关键词的世界书条目。

    参数：
        book        世界书对象（有 entries 属性即可；None 表示没有世界书）
        messages    消息历史（按时间升序，建议传**本轮即将发送的上下文**）
        scan_depth  只扫最近多少条（<=0 表示扫全部）
        token_budget 允许注入的正文 token 上限（<=0 表示不限制）

    ★ 行为与加混合检索之前**逐字节一致**（按 insertion_order 稳定排序 + 预算裁剪，
      且"至少保留第一条"）：这条路径被世界书单测与预设预览依赖着。
      混合检索走 `scan_candidates()`，它不做裁剪（预算要两路共享，见 retrieval.py）。
    """
    if book is None:
        return ScanResult()

    matches = scan_candidates(book, messages, scan_depth=scan_depth)
    if not matches:
        window = messages if scan_depth <= 0 else messages[-scan_depth:]
        return ScanResult(scanned_messages=len(window))

    # 稳定排序：insertion_order 相同时保持条目原本的先后顺序
    matches.sort(key=lambda item: item.insertion_order)

    from app.narrative.context_manager import estimate_tokens

    selected: list[dict[str, Any]] = []
    used = 0
    dropped = 0
    for match in matches:
        entry = match.entry
        cost = estimate_tokens(str(entry.get("content") or ""))
        if token_budget > 0 and selected and used + cost > token_budget:
            # ★ 至少保留第一条：否则"预算配得比单条还小"会导致永远注不进任何设定，
            #   用户完全看不出原因
            dropped += 1
            continue
        selected.append(entry)
        used += cost

    return ScanResult(
        entries=selected,
        dropped=dropped,
        scanned_messages=len(messages if scan_depth <= 0 else messages[-scan_depth:]),
        matched=len(matches),
    )


def resolve_settings(book: Any) -> tuple[int, int]:
    """从世界书的 extra_data 里读出 (scan_depth, token_budget)。

    规范把这两个参数放在 character_book 里，本项目原样存在 extra_data 中
    （导入导出不丢字段）。缺失或非法时回落到默认值 —— 用户的卡里
    什么奇怪的写法都有，不能让一个 "scan_depth": "8" 把整轮对话搞挂。
    """
    extra = getattr(book, "extra_data", None) or {}
    if not isinstance(extra, dict):
        extra = {}

    def _int(key: str, default: int) -> int:
        raw = extra.get(key)
        if raw is None or isinstance(raw, bool):
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        # 负数在规范里没有含义，视为"未设置"
        return value if value >= 0 else default

    return _int("scan_depth", DEFAULT_SCAN_DEPTH), _int(
        "token_budget", DEFAULT_TOKEN_BUDGET
    )
