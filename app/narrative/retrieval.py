"""混合检索：把「世界书关键词触发」与「长期记忆语义召回」合成**一条**通道。

==================== 它解决什么问题 ====================
在加这个模块之前，两路检索是**互不相干**的：

    · 关键词通道（world_book_scanner）：最近 N 条消息里字面命中 keys 的条目，
      只按作者写的 insertion_order 排序 —— **没有相关性分数**；
      自己吃世界书的 token_budget。
    · 语义通道（memory）：拿最后一条用户消息去查向量库，按余弦相似度取 top-k，
      自己吃 MAX_BLOCK_CHARS 的额度。

于是"混合 RAG"实际上只是**两条通道的结果被拼进同一份提示词**：
没有统一打分、没有跨通道去重、没有重排、两个预算互相不知道对方存在。
典型后果：同一条设定既被关键词命中又被语义召回（提示词里出现两遍）、
一条短而精准的设定被一条长而含糊的回忆挤掉预算。

本模块把两路变成**四步管道**（每一步都可单独关掉，便于论文做消融）：

    ① 召回   关键词通道 + 语义通道各自给出候选（带原始分与通道内名次）
    ② 融合   RRF（默认，rank 级、免调参、对两路分数量纲不敏感）
             或 min-max 归一化加权（weighted）—— 供消融对比
    ③ 去重   跨通道按"归一化文本"去重；同一段内容只留一条，
             并把另一路的名次信息合并进来（这样 RRF 能吃到两票）
    ④ 重排   默认 `tiebreak`：融合分说了算，**完全同分**时才用"通道内证据强度 +
             先验"决定次序。`blend`（词面重叠加权）实测会降指标，只留给消融，
             见 `rerank()` 里那张表 —— 这是 benchmark 量出来的结论，不是拍的
    ⑤ 装填   两路**共享一个 token 预算**，但**分两级**：先装世界书条目，
             再用剩余预算装记忆 —— 回忆再高分也**不能挤掉**作者手写的设定
             （用户的优先级：世界书 ≥ 预设 > 用户对话/回忆；见 `pack()`）

==================== 三条设计决定 ====================
1. **可解释**：每个候选都留下"两路原始分 / 各自名次 / 最终分 / 保留或丢弃的原因"，
   由 `describe()` 交给「查看提示词」预览。本项目对"界面/预览骗人"零容忍 ——
   既然排序变复杂了，就必须能说清"为什么这条进来了、那条没有"。
2. **绝不因为检索而炸掉对话**：两个通道各自 try/except 降级（语义通道本来就依赖
   外部向量库），任一路挂了就当作"这一路没有候选"，并如实记进 `errors`。
3. **渲染顺序不变**：融合决定**哪些条目入选**，但渲染仍按
   「世界设定（关键词条目）→ 回忆（记忆）」分小节 —— 与加本模块之前一致，
   免得用户发现"世界设定里混进了回忆"。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from app.narrative import world_book_scanner
from app.narrative.context_manager import estimate_tokens

# ---------------- 融合策略（benchmark 的消融轴）----------------
#: Reciprocal Rank Fusion：score = Σ w_c / (RRF_K + rank_c)，rank 从 1 开始
MODE_RRF = "rrf"
#: 各通道 min-max 归一化后加权求和（需要两路都出现才比得出来，故只在消融里用）
MODE_WEIGHTED = "weighted"
#: 只用关键词通道（= 加本模块之前的旧行为，用于对照）
MODE_KEYWORD = "keyword"
#: 只用语义通道（同上）
MODE_SEMANTIC = "semantic"
FUSION_MODES = (MODE_RRF, MODE_WEIGHTED, MODE_KEYWORD, MODE_SEMANTIC)
#: 运行时默认策略（论文/消融想换就改这里，或调用方显式传 mode=）
DEFAULT_MODE = MODE_RRF

#: RRF 的平滑常数（原论文取 60；越大越"扁平"，越小越突出头部）
RRF_K = 60
#: 两路权重（RRF 里乘在各自的名次项上）
DEFAULT_WEIGHTS: dict[str, float] = {"keyword": 1.0, "semantic": 1.0}

#: 重排模式：
#:   `off`      完全不重排（融合分排序）—— 消融用
#:   `tiebreak` **默认**：融合分完全相同时才用"通道内证据强度 + 先验"决定次序
#:   `blend`    融合分 + 词面重叠 + 先验的线性混合（**实测会降指标**，见下）
RERANK_OFF = "off"
RERANK_TIEBREAK = "tiebreak"
RERANK_BLEND = "blend"
RERANK_MODES = (RERANK_OFF, RERANK_TIEBREAK, RERANK_BLEND)
DEFAULT_RERANK = RERANK_TIEBREAK

#: blend 模式的默认权重（融合为主、重叠为辅）
W_FUSION = 0.7
W_OVERLAP = 0.2
W_PRIORITY = 0.1

#: tiebreak 模式：微调量必须是 **1e-6 级**（RRF 相邻名次的差距约 1.6e-2，
#: 所以这个量级绝不会翻掉融合得出的次序，只在"完全同分"时才起作用）
TIEBREAK_EPS = 1e-6

#: 防御：单通道候选上限（世界书写了几百条命中时不要把预算算爆）
MAX_CANDIDATES_PER_CHANNEL = 50
#: 没有世界书（或它没配 token_budget）时的默认共享预算（与旧默认一致）
DEFAULT_BUDGET = world_book_scanner.DEFAULT_TOKEN_BUDGET

#: 语义通道默认取几条（与 memory.DEFAULT_TOP_K 一致，改这里要同步想清楚）
DEFAULT_SEMANTIC_TOP_K = 5

_KEEP = "kept"

#: 归一化文本时去掉的字符（空白与常见标点）：只用于"是不是同一段内容"的判断
_PUNCT_RE = re.compile(r"[\s，。、；：！？…—－·「」『』（）()\[\]【】<>《》“”\"'`,.;:!?\-_/\\|]+")


# ==================================================================
#  数据结构
# ==================================================================
@dataclass
class Candidate:
    """一个候选（可能是世界书条目，也可能是一条记忆）。"""

    key: str
    """稳定标识：`book:<下标>` / `mem:<memory_id>`（去重与调试用）"""
    text: str
    source: str
    """`world_book` / `memory`"""
    raw: Any = None
    """原始对象（条目 dict / MemoryHit），入选后要拿它去渲染"""
    tokens: int = 0
    #: 关键词通道的原始分与名次（None = 这一路没有召回它）
    lexical_score: float | None = None
    lexical_rank: int | None = None
    matched_keys: tuple[str, ...] = ()
    #: 语义通道的相似度与名次
    semantic_score: float | None = None
    semantic_rank: int | None = None
    #: 先验分（0~1）：作者优先级 / 会话内记忆优先
    priority: float = 0.0
    #: 与查询的词面重叠率（0~1，重排用）
    overlap: float = 0.0
    #: 融合分、最终分
    fusion: float = 0.0
    score: float = 0.0
    #: 保留或丢弃的原因（人话，直接进预览）
    reason: str = ""

    @property
    def channels(self) -> tuple[str, ...]:
        hit = []
        if self.lexical_rank is not None:
            hit.append("keyword")
        if self.semantic_rank is not None:
            hit.append("semantic")
        return tuple(hit)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "channels": list(self.channels),
            "lexical_score": self.lexical_score,
            "lexical_rank": self.lexical_rank,
            "semantic_score": (
                round(self.semantic_score, 4) if self.semantic_score is not None else None
            ),
            "semantic_rank": self.semantic_rank,
            "priority": round(self.priority, 3),
            "overlap": round(self.overlap, 3),
            "fusion": round(self.fusion, 5),
            "score": round(self.score, 5),
            "tokens": self.tokens,
            "reason": self.reason,
            "preview": self.text[:60],
            "matched_keys": list(self.matched_keys),
        }


@dataclass
class RetrievalResult:
    """一次混合检索的完整产物（既给提示词装配，也给预览与 benchmark）。"""

    mode: str = DEFAULT_MODE
    budget: int = DEFAULT_BUDGET
    used_tokens: int = 0
    #: 入选的候选（最终分降序）
    kept: list[Candidate] = field(default_factory=list)
    #: 没入选的候选（含原因：重复 / 预算不足）
    dropped: list[Candidate] = field(default_factory=list)
    #: 两路召回总量（去重前）
    lexical_total: int = 0
    semantic_total: int = 0
    #: 通道级降级说明（例如向量库不可用）
    errors: list[str] = field(default_factory=list)

    # ---------------- 给装配用的视图 ----------------
    @property
    def world_entries(self) -> list[dict[str, Any]]:
        """入选的世界书条目（供 render_world_book，保持融合后的名次）。"""
        return [c.raw for c in self.kept if c.source == "world_book"]

    @property
    def memory_hits(self) -> list[Any]:
        """入选的记忆（供 memory.build_recall_block，保持融合后的名次）。"""
        return [c.raw for c in self.kept if c.source == "memory"]

    @property
    def book_matched(self) -> int:
        return self.lexical_total

    @property
    def book_injected(self) -> int:
        return sum(1 for c in self.kept if c.source == "world_book")

    @property
    def book_dropped(self) -> int:
        return sum(1 for c in self.dropped if c.source == "world_book")

    @property
    def memory_injected(self) -> int:
        return sum(1 for c in self.kept if c.source == "memory")

    @property
    def deduped(self) -> int:
        return sum(1 for c in self.dropped if "重复" in c.reason)

    def to_dict(self) -> dict[str, Any]:
        """给 PromptInfo / benchmark 的结构化摘要（可 JSON 序列化）。"""
        return {
            "mode": self.mode,
            "budget_tokens": self.budget,
            "used_tokens": self.used_tokens,
            "lexical_total": self.lexical_total,
            "semantic_total": self.semantic_total,
            "injected": len(self.kept),
            "book_injected": self.book_injected,
            "memory_injected": self.memory_injected,
            "book_dropped": self.book_dropped,
            "memory_dropped": sum(1 for c in self.dropped if c.source == "memory"),
            "dropped": len(self.dropped),
            "deduped": self.deduped,
            "errors": list(self.errors),
            "items": [c.to_dict() for c in self.kept] + [c.to_dict() for c in self.dropped],
        }


# ==================================================================
#  ① 召回：两个通道
# ==================================================================
def keyword_candidates(
    book: Any,
    history: list[Any],
    *,
    scan_depth: int = world_book_scanner.DEFAULT_SCAN_DEPTH,
) -> list[Candidate]:
    """关键词通道：世界书条目按命中强度排序（作者 order 只作 tie-break）。"""
    matches = world_book_scanner.scan_candidates(book, history, scan_depth=scan_depth)
    if not matches:
        return []
    # 相关度降序；同分时听作者的 insertion_order（小的先），再不行就按原顺序
    matches.sort(key=lambda m: (-m.score, m.insertion_order, m.index))
    out: list[Candidate] = []
    for rank, match in enumerate(matches[:MAX_CANDIDATES_PER_CHANNEL], start=1):
        out.append(
            Candidate(
                key=f"book:{match.index}",
                text=match.content,
                source="world_book",
                raw=match.entry,
                tokens=estimate_tokens(match.content),
                lexical_score=match.score,
                lexical_rank=rank,
                matched_keys=match.matched_keys,
                # 作者优先级：insertion_order 小的更靠前（0 → 1.0，1 → 0.5，…）
                priority=1.0 / (1.0 + max(match.insertion_order, 0)),
            )
        )
    return out


def semantic_candidates(
    *,
    user_id: int | None,
    session_id: int | None,
    query: str,
    top_k: int = DEFAULT_SEMANTIC_TOP_K,
) -> tuple[list[Candidate], list[str]]:
    """语义通道：长期记忆按余弦相似度排序。返回 `(候选, 降级说明)`。

    ★ 依赖外部向量库 + 嵌入后端，所以整段包了 try/except：
      它挂了顶多"这一轮没有回忆"，绝不能让对话发不出去（与 memory.recall 同一套哲学）。
    """
    if user_id is None or not str(query or "").strip():
        return [], []
    try:
        from app.narrative import memory as memory_mod

        result = memory_mod.recall(
            user_id=user_id, session_id=session_id, query=query, top_k=top_k
        )
    except Exception as exc:  # noqa: BLE001 - 见 docstring：检索失败必须降级
        logger.warning("语义通道召回失败，已降级为「本轮没有回忆」| err={}", exc)
        return [], [f"长期记忆检索失败（已跳过，不影响本次对话）：{exc}"]

    # ★ memory.recall 自己**不抛异常**（它内部已经降级），而是把原因放在 result.error 里。
    #   所以这里必须显式把它翻译成"降级说明"，否则用户只会看到"这轮没有回忆"，
    #   完全不知道向量库挂了 —— 那就成了静默降级。
    if getattr(result, "error", None):
        logger.warning("语义通道降级（向量库/嵌入后端不可用）| err={}", result.error)
        return [], [f"长期记忆检索失败（已跳过，不影响本次对话）：{result.error}"]

    out: list[Candidate] = []
    for rank, hit in enumerate(result.hits, start=1):
        text = str(getattr(hit, "text", "") or "")
        if not text.strip():
            continue
        metadata = getattr(hit, "metadata", None) or {}
        same_session = metadata.get("session_id") == session_id
        kind = str(metadata.get("kind") or "dialogue")
        # 先验分：同会话的记忆更可信（本项目默认不跨会话，跨会话属于例外情况）；
        # summary（剧情摘要）比零散对话更"成体系"，给一点点加成。
        priority = (1.0 if same_session else 0.6) + (0.1 if kind == "summary" else 0.0)
        out.append(
            Candidate(
                key=f"mem:{getattr(hit, 'memory_id', rank)}",
                text=text,
                source="memory",
                raw=hit,
                tokens=estimate_tokens(text),
                semantic_score=float(getattr(hit, "similarity", 0.0)),
                semantic_rank=rank,
                priority=min(priority, 1.0),
            )
        )
        if len(out) >= MAX_CANDIDATES_PER_CHANNEL:
            break
    return out, []


# ==================================================================
#  ③ 去重（跨通道）
# ==================================================================
def normalize_text(text: str) -> str:
    """归一化文本：去空白与标点、统一大小写 —— 只用于判重，不改原文。"""
    return _PUNCT_RE.sub("", str(text or "")).lower()


def _best_rank(candidate: Candidate) -> int:
    """这条候选在它出现过的通道里的最好名次（没出现过给一个很大的数）。"""
    ranks = [r for r in (candidate.lexical_rank, candidate.semantic_rank) if r is not None]
    return min(ranks) if ranks else 10**6


def _better(first: Candidate, second: Candidate) -> Candidate:
    """两条重复的候选里留哪条：

    ① 通道内名次更靠前者胜；
    ② 名次相同则**优先世界书**（作者手写的设定比一段对话记忆更权威，
       而且它在提示词里本来就属于「世界设定」那一节）；
    ③ 还平就留先出现的（保证确定性）。
    """
    first_rank, second_rank = _best_rank(first), _best_rank(second)
    if first_rank != second_rank:
        return first if first_rank < second_rank else second
    if first.source != second.source:
        return first if first.source == "world_book" else second
    return first


def dedupe(candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    """跨通道去重：同一段内容只留一条，并把两路的票合并（RRF 因此能吃到两票）。"""
    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    index: dict[str, Candidate] = {}
    for candidate in candidates:
        key = normalize_text(candidate.text)
        if not key:
            dropped.append(_with_reason(candidate, "空内容"))
            continue
        existing = index.get(key)
        if existing is None:
            index[key] = candidate
            kept.append(candidate)
            continue
        winner = _better(existing, candidate)
        loser = candidate if winner is existing else existing
        # 合并两路信息到胜者身上：另一路的名次/分数也带上（这就是"两票"）
        if winner.lexical_rank is None and loser.lexical_rank is not None:
            winner.lexical_rank = loser.lexical_rank
            winner.lexical_score = loser.lexical_score
            winner.matched_keys = loser.matched_keys
        if winner.semantic_rank is None and loser.semantic_rank is not None:
            winner.semantic_rank = loser.semantic_rank
            winner.semantic_score = loser.semantic_score
        winner.priority = max(winner.priority, loser.priority)
        dropped.append(_with_reason(loser, f"与 {winner.key} 内容重复（跨通道去重）"))
        if loser is existing:
            kept.remove(existing)
            index[key] = winner
    return kept, dropped


def _with_reason(candidate: Candidate, reason: str) -> Candidate:
    candidate.reason = reason
    return candidate


# ==================================================================
#  ④ 融合 + 重排
# ==================================================================
def fuse(candidates: list[Candidate], *, mode: str = DEFAULT_MODE, weights: dict[str, float] | None = None) -> None:
    """给每个候选算融合分 `candidate.fusion`（就地修改）。"""
    weight = {**DEFAULT_WEIGHTS, **(weights or {})}
    if mode == MODE_KEYWORD:
        for c in candidates:
            c.fusion = 1.0 / (RRF_K + c.lexical_rank) if c.lexical_rank else 0.0
        return
    if mode == MODE_SEMANTIC:
        for c in candidates:
            c.fusion = 1.0 / (RRF_K + c.semantic_rank) if c.semantic_rank else 0.0
        return
    if mode == MODE_WEIGHTED:
        _fuse_weighted(candidates, weight)
        return
    # 默认 RRF
    for c in candidates:
        score = 0.0
        if c.lexical_rank is not None:
            score += weight["keyword"] / (RRF_K + c.lexical_rank)
        if c.semantic_rank is not None:
            score += weight["semantic"] / (RRF_K + c.semantic_rank)
        c.fusion = score


def _fuse_weighted(candidates: list[Candidate], weight: dict[str, float]) -> None:
    """min-max 归一化后加权（每个通道单独归一化，否则量纲不可比）。"""
    lexical = [c.lexical_score for c in candidates if c.lexical_score is not None]
    semantic = [c.semantic_score for c in candidates if c.semantic_score is not None]

    def _norm(value: float | None, values: list[float]) -> float:
        if value is None or not values:
            return 0.0
        low, high = min(values), max(values)
        if high - low <= 1e-9:
            return 1.0  # 只有一个值（或全相等）：给满分，免得整路被压成 0
        return (value - low) / (high - low)

    for c in candidates:
        c.fusion = (
            weight["keyword"] * _norm(c.lexical_score, lexical)
            + weight["semantic"] * _norm(c.semantic_score, semantic)
        )


def bigrams(text: str) -> set[str]:
    """中文/通用二元字组（不依赖分词器 —— 本项目零额外依赖）。"""
    clean = _PUNCT_RE.sub("", str(text or "").lower())
    if len(clean) < 2:
        return {clean} if clean else set()
    return {clean[i : i + 2] for i in range(len(clean) - 1)}


def overlap_ratio(query: str, text: str) -> float:
    """查询与文本的词面重叠率（Dice 系数，0~1）。"""
    left, right = bigrams(query), bigrams(text)
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def evidence_score(candidate: Candidate, maxima: dict[str, float]) -> float:
    """候选在自己通道里的证据强度（0~2，通道内归一化后相加）。

    ★ 为什么要它：RRF 是**名次**融合、名次是整数，所以"关键词第 2 名 vs 语义第 2 名"
      这种局面会**完全同分**（实测同分很常见）。同分总得有个有意义的次序：
      拿"这一路当时给了多高的原始分（相对本通道最高分）"来比，
      比按 key 字母序排要站得住脚，实测也更好（见 `rerank()` 里那张表）。
    """
    total = 0.0
    if candidate.lexical_rank is not None:
        top = maxima.get("lexical") or 0.0
        if top > 0 and candidate.lexical_score is not None:
            total += min(max(candidate.lexical_score / top, 0.0), 1.0)
    if candidate.semantic_rank is not None:
        top = maxima.get("semantic") or 0.0
        if top > 0 and candidate.semantic_score is not None:
            total += min(max(float(candidate.semantic_score) / top, 0.0), 1.0)
    return total


def rerank(
    candidates: list[Candidate],
    query: str,
    *,
    mode: str = DEFAULT_RERANK,
    weights: tuple[float, float, float] | None = None,
) -> None:
    """写 `candidate.score`（融合分 + 可选的重排调整）。

    ★ 三种模式的选择依据是 **`scripts/benchmark.py` 的实测**（合成标注集，12 query）：

        | 策略                     | Recall@1 | Recall@3 | MRR    | nDCG@5 |
        | keyword_only             | 0.4861   | 0.5278   | 0.7917 | 0.5768 |
        | semantic_only            | 0.2917   | 0.3889   | 0.7083 | 0.4493 |
        | rrf（不重排）             | 0.5139   | 0.9167   | 0.9167 | 0.8962 |
        | rrf + 词面重叠混合(blend) | 0.3056   | 0.8750   | 0.8194 | 0.7765 |

      结论：**融合本身带来最大增益**（Recall@1 0.29/0.49 → 0.51、Recall@3 0.39/0.53 → 0.92），
      而"用与查询的字面重叠去重排"反而**有害** —— 因为语义通道的价值恰恰在于
      命中与查询**没有字面重叠**的同义改写，重叠项会把它们压下去。
      所以默认用 `tiebreak`：融合分说了算，**完全同分**时才按先验分
      （作者 insertion_order / 同会话记忆）微调，量级 1e-6，绝不会翻掉融合次序；
      `blend` 保留下来只用于消融对比（论文里那张表）。

    ★ 为什么不拿"通道内证据强度"当同分裁决：不用了 —— **实测它最好**（见下表），
      所以默认就用它。比较方式是"每路先按本轮的通道最高分归一化，再相加"（无量纲）。

    ★ 同分裁决的实测对比（同一份合成标注集，RRF 之后只换裁决方式）：

        | 同分裁决方式                     | Recall@1 | MRR    | nDCG@5 |
        | 按 key 字母序（无意义的兜底）      | 0.5139   | 0.9167 | 0.8962 |
        | 按先验分（作者优先级/同会话）      | 0.2639   | 0.7917 | 0.8040 |
        | 只按语义相似度                    | 0.3333   | 0.8750 | 0.8373 |
        | **按每路归一化的证据强度（默认）** | **0.5139** | **0.9167** | **0.8962** |

      ★ 一个必须承认的事实：RRF 是名次融合、名次是整数，所以**同分非常常见**，
        在小数据集上"怎么裁决同分"对 Recall@1 的影响甚至和融合策略本身一样大。
        所以这里不藏私：裁决规则写进代码也写进论文，不靠"字母序恰好排对了"。
    """
    maxima = {
        "lexical": max((c.lexical_score or 0.0 for c in candidates), default=0.0),
        "semantic": max((float(c.semantic_score or 0.0) for c in candidates), default=0.0),
    }
    top = max((c.fusion for c in candidates), default=0.0)
    for candidate in candidates:
        candidate.overlap = overlap_ratio(query, candidate.text)
        fused = (candidate.fusion / top) if top > 0 else 0.0

        if mode == RERANK_OFF:
            candidate.score = candidate.fusion  # 消融要的"纯融合分"
            continue

        if mode == RERANK_BLEND:
            w_fusion, w_overlap, w_priority = weights or (W_FUSION, W_OVERLAP, W_PRIORITY)
            candidate.score = (
                w_fusion * fused + w_overlap * candidate.overlap + w_priority * candidate.priority
            )
            continue

        # tiebreak（默认）：融合分说了算，同分时才看"每路归一化的证据强度"
        candidate.score = fused + TIEBREAK_EPS * evidence_score(candidate, maxima)


# ==================================================================
#  ⑤ 共享预算装填
# ==================================================================
#: 共享预算里**优先装填**的来源：世界书条目永远优先于记忆。
#: ★ 用户的原话："世界书一般是作者特意加在角色卡里面的，里面的设定比用户对话重要，
#:   不然容易出戏，乱写和掉马甲之类的，**绝对不能挤掉**！"
#:   所以装填分两级：先按分数装世界书条目，再用**剩余预算**装记忆 ——
#:   回忆再高分也只能吃世界书吃剩的部分，永远不能把设定挤出去。
PRIORITY_SOURCES: tuple[str, ...] = ("world_book",)


def pack(
    candidates: list[Candidate],
    budget: int,
    *,
    priority_sources: tuple[str, ...] = PRIORITY_SOURCES,
) -> tuple[list[Candidate], list[Candidate]]:
    """按最终分装填预算，返回 `(入选, 落选)`。

    ★ 两级装填（用户明确的优先级：**世界书 ≥ 预设 > 用户对话/回忆**）：
        ① 先装 `priority_sources` 里的候选（世界书条目），按分数，占满就占满；
        ② 再用**剩余预算**装其余候选（长期记忆）。
      于是"高分回忆把世界书条目挤出提示词"这种事在物理上不可能发生 ——
      设定丢一条就可能让角色出戏、掉马甲，而少一条回忆只是少一点上下文。
    ★ 至少保留最高分那条：预算配得比单条还小时，若一条都不给，
      用户只会看到"命中 3 条、注入 0 条"，完全不知道为什么。
      （这条规矩继承自原来的世界书扫描器。）
    ★ budget <= 0 表示不限制（沿用世界书扫描器的语义）。
    """
    ordered = sorted(candidates, key=lambda c: (-c.score, c.key))
    if budget <= 0:
        kept = [_with_reason(c, _KEEP) for c in ordered]
        return kept, []

    priority = [c for c in ordered if c.source in priority_sources]
    rest = [c for c in ordered if c.source not in priority_sources]

    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    used = 0

    # ① 世界书（优先来源）：彼此之间按分数竞争，但**不与记忆竞争**
    for candidate in priority:
        if kept and used + candidate.tokens > budget:
            dropped.append(
                _with_reason(
                    candidate,
                    f"token 预算不足（世界书已用 {used}/{budget}；"
                    "世界书优先，不会被回忆挤掉，但自己也装不下时只能丢弃）",
                )
            )
            continue
        kept.append(_with_reason(candidate, _KEEP))
        used += candidate.tokens

    # ② 记忆：只能用剩下的预算
    for candidate in rest:
        if used + candidate.tokens > budget:
            dropped.append(
                _with_reason(candidate, f"token 预算不足（世界书已优先占用 {used}/{budget}）")
            )
            continue
        kept.append(_with_reason(candidate, _KEEP))
        used += candidate.tokens

    # 保持"入选按分数降序"的对外语义（两级装填会打乱顺序）
    kept.sort(key=lambda c: (-c.score, c.key))
    return kept, dropped


# ==================================================================
#  入口
# ==================================================================
def retrieve(
    *,
    book: Any = None,
    history: list[Any] | None = None,
    query: str = "",
    user_id: int | None = None,
    session_id: int | None = None,
    scan_depth: int = world_book_scanner.DEFAULT_SCAN_DEPTH,
    budget: int = DEFAULT_BUDGET,
    mode: str = DEFAULT_MODE,
    weights: dict[str, float] | None = None,
    rerank_mode: str = DEFAULT_RERANK,
    rerank_weights: tuple[float, float, float] | None = None,
    semantic_top_k: int = DEFAULT_SEMANTIC_TOP_K,
    keyword_provider: Callable[[], list[Candidate]] | None = None,
    semantic_provider: Callable[[], tuple[list[Candidate], list[str]]] | None = None,
) -> RetrievalResult:
    """混合检索主入口。两个 `*_provider` 只给测试/benchmark 注入假通道用。"""
    history = list(history or [])
    if mode not in FUSION_MODES:
        logger.warning("未知的检索融合策略 {}，已回退到 {}", mode, DEFAULT_MODE)
        mode = DEFAULT_MODE

    errors: list[str] = []
    lexical: list[Candidate] = []
    semantic: list[Candidate] = []

    # ① 召回（两路各自降级，互不影响）
    try:
        lexical = (
            keyword_provider()
            if keyword_provider is not None
            else keyword_candidates(book, history, scan_depth=scan_depth)
        )
    except Exception as exc:  # noqa: BLE001 - 检索失败不能影响对话
        logger.warning("关键词通道失败，已降级为空 | err={}", exc)
        errors.append(f"世界书关键词触发失败（已跳过）：{exc}")

    if mode != MODE_KEYWORD:
        try:
            semantic, semantic_errors = (
                semantic_provider()
                if semantic_provider is not None
                else semantic_candidates(
                    user_id=user_id, session_id=session_id, query=query, top_k=semantic_top_k
                )
            )
            errors.extend(semantic_errors)
        except Exception as exc:  # noqa: BLE001
            logger.warning("语义通道失败，已降级为空 | err={}", exc)
            errors.append(f"长期记忆检索失败（已跳过，不影响本次对话）：{exc}")
    if mode == MODE_SEMANTIC:
        lexical = []

    result = RetrievalResult(
        mode=mode,
        budget=int(budget or 0),
        lexical_total=len(lexical),
        semantic_total=len(semantic),
        errors=errors,
    )

    # ③ 去重（跨通道）→ ④ 融合 → 重排 → ⑤ 装填
    merged, duplicates = dedupe(lexical + semantic)
    fuse(merged, mode=mode, weights=weights)
    if rerank_mode not in RERANK_MODES:
        logger.warning("未知的重排模式 {}，已回退到 {}", rerank_mode, DEFAULT_RERANK)
        rerank_mode = DEFAULT_RERANK
    rerank(merged, query, mode=rerank_mode, weights=rerank_weights)
    kept, rejected = pack(merged, result.budget)
    result.kept = kept
    result.dropped = duplicates + rejected
    result.used_tokens = sum(c.tokens for c in kept)
    logger.debug(
        "混合检索 | mode={} 关键词候选={} 语义候选={} 去重={} 入选={} 预算={}/{}",
        mode,
        result.lexical_total,
        result.semantic_total,
        len(duplicates),
        len(kept),
        result.used_tokens,
        result.budget,
    )
    return result


def describe(result: RetrievalResult) -> str:
    """把检索结果写成一行"说人话"的摘要（进「查看提示词」预览）。

    ★ 存在的意义：排序逻辑一旦复杂，用户就没法自己判断"为什么这条进来了"。
      所以预览必须同时给出**计数**与**每条的来源/名次/最终分**。
    """
    if not result.lexical_total and not result.semantic_total:
        return "混合检索：两路都没有候选（世界书未命中、也没有相关记忆）"
    parts = [
        f"混合检索（{result.mode}）：关键词候选 {result.lexical_total} 条、"
        f"语义候选 {result.semantic_total} 条，"
        f"去重 {result.deduped} 条，注入 {len(result.kept)} 条"
        f"（世界书 {result.book_injected} + 回忆 {result.memory_injected}），"
        f"占用 {result.used_tokens}/{result.budget} token"
    ]
    parts.append("（★ 世界书优先装填，回忆只用剩余预算）")
    # ★ 没注入的那些也要说清有多少、为什么（去重 / 预算），否则用户只会看到
    #   "命中 5 条、注入 2 条"却不知道为什么 —— 排序逻辑复杂了就必须能解释。
    if result.dropped:
        reasons = Counter("重复" if "重复" in c.reason else "预算不足" for c in result.dropped)
        detail = "、".join(f"{reason} {count} 条" for reason, count in reasons.items())
        parts.append(f"；未注入 {len(result.dropped)} 条（{detail}）")
    if result.errors:
        parts.append("；" + "；".join(result.errors))
    return "".join(parts)


def debug_lines(result: RetrievalResult, *, limit: int = 12) -> list[str]:
    """每条候选一行（来源 / 名次 / 分数 / 去留），供预览逐条展示。"""
    lines: list[str] = []
    for candidate in result.kept + result.dropped:
        channels = "+".join(candidate.channels) or candidate.source
        lines.append(
            f"{'✔' if candidate.reason == _KEEP else '✘'} [{channels}] "
            f"{candidate.source} key={candidate.key} "
            f"lex={candidate.lexical_rank or '-'} sem={candidate.semantic_rank or '-'} "
            f"score={candidate.score:.4f} tokens={candidate.tokens} :: {candidate.reason}"
        )
        if len(lines) >= limit:
            break
    return lines
