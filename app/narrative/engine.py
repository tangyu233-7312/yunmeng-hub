"""叙事对话的编排：拼提示词 → 调模型 → 落库。

==================== 一次对话发生了什么 ====================
    save_user_message      用户说的话先落库（哪怕模型调用失败，话也不能丢）
        ↓
    prepare_turn           角色卡 + 世界书 + 历史 → PromptPlan
        ↓                 再按 ContextBudget 裁剪 → ContextPlan（超预算就滚动摘要）
    stream_reply / reply   调用适配层（流式或非流式）
        ↓
    save_assistant_reply   助手回复落库 + 更新会话统计（message_count /
                           total_tokens / last_active_at）

★ 为什么用户消息要"先落库"？
  模型调用可能失败（限流、超时、余额不足）。如果等回复成功才一起写，
  用户辛苦打的一段话会跟着失败一起消失，只能重打一遍。
  这与聊天软件的直觉一致：我说出去的话就是发出去了。

★ 为什么统计里 role 为 user 的消息 token_count 是**估算值**？
  真实用量要等模型返回 usage 才知道（prompt_tokens 是**整段输入**的总和，
  无法拆到单条消息上）。所以：用户消息用估算，助手回复优先用模型返回的
  completion_tokens。界面上要标明这一点，不能假装都是精确值。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError
from app.db.models import (
    CharacterCard,
    LLMProvider,
    Message,
    NarrativeSession,
    User,
    WorldBook,
)
from app.llm.base import BaseLLMProvider
from app.llm.failover import FailoverAdapter
from app.llm.whole_reply import WholeReplyAdapter
from app.llm.schema import ChatRequest, ChatResult, TokenUsage
from app.narrative.context_manager import (
    ContextPlan,
    estimate_tokens,
    prepare_context,
)
from app.narrative import sessions
from app.narrative import anchors as anchors_mod
from app.narrative import dice as dice_mod
from app.narrative import memory as memory_mod
from app.narrative import plugin_runtime
from app.narrative import retrieval as retrieval_mod
from app.narrative import state as state_mod
from app.narrative import summary as summary_mod
from app.narrative import translate as translate_mod
from app.narrative import world_book_scanner
from app.narrative.prompt_builder import PromptPlan, build_prompt
from app.services import plugin_service
from app.services import provider_service

# ==================================================================
#  适配器
# ==================================================================
def build_adapter(row: LLMProvider) -> BaseLLMProvider:
    """由数据库行构造适配器。

    ★ 刻意做成"薄薄一层转发"，而不是直接调用 provider_service.build_adapter：
      叙事相关的测试要**替换掉网络层**（用 httpx.MockTransport 伪造服务端），
      只要 monkeypatch 本模块的 build_adapter 即可，
      不必真的配一个模型、也不必联网（红线：测试不许打真实 API）。
    """
    return provider_service.build_adapter(row)


def _load_fallback_row(db: Session, row: LLMProvider) -> LLMProvider | None:
    """取"备用模型"那一行；没配、被删、被停用、指向自己 → 一律当作没配。

    ★ 为什么不报错：备用模型是**兜底**，它的配置坏了不该影响正常对话；
      真正需要提醒的是"主模型挂了、备用也没有"，那时候的报错已经足够清楚。
    """
    fallback_id = getattr(row, "fallback_provider_id", None)
    if not fallback_id or fallback_id == row.id:
        return None
    fallback = db.get(LLMProvider, fallback_id)
    if fallback is None or fallback.user_id != row.user_id or not fallback.is_active:
        logger.warning(
            "备用模型不可用，已忽略 | provider_id={} fallback_id={}", row.id, fallback_id
        )
        return None
    return fallback


def _effective_model_name(turn: PreparedTurn) -> str:
    """消息上记哪个模型名：**实际**回答的那个，而不是配置里挂着的那个。

    ★ 自动切到备用模型之后，如果还记主模型的名字，用户会以为"主模型答得很好" ——
      而真相是它挂了。宁可如实写。
    """
    adapter = getattr(turn, "adapter", None)
    name = getattr(adapter, "effective_model_name", None)
    return str(name or turn.provider_row.model_name)


def require_provider(db: Session, session: NarrativeSession) -> LLMProvider:
    """取会话使用的模型配置，没配好就明确报错。"""
    if not session.llm_provider_id:
        raise BadRequestError(
            "这个会话还没有可用的模型配置",
            detail={
                "session_id": session.id,
                "suggestion": "请先到「模型配置」里添加一个模型，然后在会话设置里选中它",
            },
        )

    row = db.get(LLMProvider, session.llm_provider_id)
    if row is None:
        # 外键是 ON DELETE SET NULL，理论上不会走到这里；
        # 但真出现了（比如手工改库）也要给一句人话，而不是 500
        raise BadRequestError(
            "这个会话使用的模型配置已被删除",
            detail={"session_id": session.id, "provider_id": session.llm_provider_id},
        )

    if not row.is_active:
        raise BadRequestError(
            f"模型配置「{row.name}」已被停用", detail={"provider_id": row.id}
        )
    return row


def load_card_and_book(
    db: Session, session: NarrativeSession
) -> tuple[CharacterCard | None, WorldBook | None]:
    """取出会话关联的角色卡与世界书。

    ★ 两者都**允许为 None**：
      角色卡被删除时会话仍然保留（外键 SET NULL，这是刻意的设计），
      此时对话不该直接报错，而是给出"没有注入人设"的提醒继续可用。
    """
    card = db.get(CharacterCard, session.character_card_id) if session.character_card_id else None
    book = None
    if card is not None and card.world_book_id:
        book = db.get(WorldBook, card.world_book_id)
    return card, book


def build_request(
    *,
    adapter: BaseLLMProvider,
    plan: ContextPlan,
) -> ChatRequest:
    """把裁剪好的上下文包装成统一请求。

    生成参数**不在这里设置**：温度 / 最大输出 / 思考强度都来自模型配置本身
    （适配器构造时已经带上 default_params），这样同一段剧情换模型时
    不用改业务代码，也不会把 A 模型的偏好参数带进 B 模型。
    """
    return ChatRequest(messages=plan.messages)


# ==================================================================
#  上下文准备
# ==================================================================
@dataclass
class PreparedTurn:
    """一次对话「准备阶段」的全部产物。"""

    prompt: PromptPlan
    context: ContextPlan
    adapter: BaseLLMProvider
    provider_row: LLMProvider
    card: CharacterCard | None = None
    book: WorldBook | None = None
    summary_updated: bool = False
    summary: Any = None
    """★ 本轮的剧情总结结果（`app.narrative.summary.MergeOutcome`；没合并就是 None）。

    带上它是为了让**降级可见**：总结调用模型失败时会退回本地压缩，
    这种时候必须能在界面/日志里说清楚"这份前情提要是本地压的，不是模型写的"。
    """
    rollback_reason: str = ""
    """数据一致性提示：摘要写进去了但消息没落库时必须回滚，见 persist_summary。"""

    notes: list[str] = field(default_factory=list)

    def to_meta(self) -> dict[str, Any]:
        """给前端的构建说明（提示词来源 + 裁剪情况）。"""
        return {**self.prompt.to_dict(), **self.context.to_dict()}


@dataclass
class SessionPrompt:
    """一次提示词构建的完整产物（含世界书命中与记忆召回的统计）。"""

    prompt: PromptPlan
    scan: world_book_scanner.ScanResult
    recall: Any = None
    memory_block: str = ""


def build_session_prompt(
    *,
    user_id: int,
    session_id: int,
    card: Any,
    book: Any,
    history: list[Message],
    prefer_request: bool = True,
    preset: Any = None,
    preset_config: Any = None,
    user_name: str = "",
    state_block: str = "",
    state_schema: Any = None,
    anchors_block: str = "",
    translate_hint: str = "",
    plugins: list[Any] | None = None,
    pure_chat: bool = False,
) -> SessionPrompt:
    """把「世界书关键词触发 + 长期记忆召回 + 提示词拼装」串成一步。

    ★ 为什么单独抽出来？
      对话时要走这套逻辑，界面上的「查看提示词」预览也要走同一套 ——
      否则**预览和真正发出去的东西会不一致**：预览显示"注入了 2 条设定"，
      实际只注入了命中的 1 条。用户会拿着这份预览去排查一个不存在的问题。
      （这个不一致真实出现过一次，是这个函数存在的直接原因。）

    ★ prefer_request=True 时，检索词优先用**最后一条用户消息**。
      但发消息的流程里，用户消息已经落库了，所以历史最后一条就是它；
      而"查看提示词"时也应当以"用户就要说的这句话"为准。
    """
    scan_depth, token_budget = world_book_scanner.resolve_settings(book)

    # ★ 骰子插件的两份贡献之一：**本轮点数**（数据）。
    #   点数在 `save_user_message` 落库那一刻就掷定了（写进 messages.rolls_json），
    #   这里只是把它读出来渲染 —— 绝不在这里现掷：否则点一次「查看提示词」
    #   就会掷出另一组数字，预览与实际请求立刻不一致。
    dice_spec = plugin_runtime.dice_spec(plugins)
    show_detail = bool(dice_spec.get("show_detail", True)) if dice_spec else True
    dice_block = (
        dice_mod.render_turn_block(dice_mod.turn_rolls(history), show_detail=show_detail)
        if dice_spec
        else ""
    )

    # ★ 纯聊天：不召回长期记忆（跨会话语义召回会把别的故事的细节串进来）、
    #   不扫世界书、不注入状态协议。它就该是一条干净的通道。
    if pure_chat:
        return SessionPrompt(
            prompt=build_prompt(
                card=None,
                history=history,
                world_book=None,
                world_book_entries=[],
                recalled_memories=None,
                state_block=None,
                dice_block=dice_block,
                translate_hint=translate_hint,
                preset=None,
                preset_config=None,
                user_name=user_name,
                pure_chat=True,
            ),
            scan=world_book_scanner.scan(None, []),
            recall=None,
            memory_block="",
        )

    query = _latest_user_text(history) if prefer_request else ""

    # ---------------- 混合检索：关键词 + 语义 → 融合 → 去重 → 重排 → 共享预算 ----------------
    # ★ 为什么合并成一次调用（而不是像以前那样各查各的）：
    #   以前两路互不知道对方存在，于是同一条设定可能进提示词两遍、
    #   短而精准的设定会被长而含糊的回忆挤掉预算，而且**没有任何相关性排序**。
    #   现在由 retrieval.py 统一打分/去重/排序，并共享一个 token 预算；
    #   「查看提示词」预览走的也是这一条路，所以预览与真实请求一致。
    result = retrieval_mod.retrieve(
        book=book,
        history=history,
        query=query,
        user_id=user_id,
        session_id=session_id,
        scan_depth=scan_depth,
        # ★ token_budget 的语义变了：以前是"关键词通道自己的额度"，
        #   现在是**两路共享的总预算**（用户拍板）。世界书没配就用默认值。
        budget=token_budget if token_budget > 0 else retrieval_mod.DEFAULT_BUDGET,
    )
    memory_block = memory_mod.build_recall_block(result.memory_hits)
    # 保持 SessionPrompt.recall 的形状（引擎统计要用 recall.error 报"记忆检索失败"）
    recall = memory_mod.RecallResult(
        hits=result.memory_hits, error=(result.errors[0] if result.errors else None)
    )

    prompt = build_prompt(
        card=card,
        history=history,
        world_book=book,
        # ★ matched 为 0 时显式传空列表：不能让 prompt_builder 回落到
        #   "注入全部条目"（那就等于关键词触发白做了）
        world_book_entries=result.world_entries,
        recalled_memories=memory_block,
        state_block=state_block,
        state_schema=state_schema,
        dice_block=dice_block,
        anchors_block=anchors_block,
        translate_hint=translate_hint,
        preset=preset,
        preset_config=preset_config,
        user_name=user_name,
        pure_chat=pure_chat,
    )

    # 混合检索的计数与逐条明细：进「查看提示词」预览（排序逻辑复杂了就必须能解释）
    prompt.retrieval = result.to_dict()
    prompt.retrieval_summary = retrieval_mod.describe(result)
    prompt.retrieval_items = retrieval_mod.debug_lines(result)

    # ★ 用**直接计数**（book_dropped）而不是"命中 − 注入 − 去重"的减法：
    #   跨通道去重会把两路候选合到一起计数，减法在"记忆内部互相去重"时会算歪
    #   （真实踩过：1 条条目被挤出，却算出 1−0−2=−1，于是警告不出现）。
    dropped_book = result.book_dropped
    if dropped_book > 0:
        prompt.warnings.append(
            f"世界书命中 {result.book_matched} 条设定，其中 {dropped_book} 条没有注入："
            f"世界书共享预算已用满（{result.budget} token）。"
            "**回忆不会挤掉设定**（世界书优先装填），所以这里只能调大世界书的 "
            "token_budget，或精简这几条设定本身。"
        )
    for error in result.errors:
        prompt.warnings.append(error)

    # ★ 插件（正则替换 / 提示词注入）在**提示词装配完成之后、发出去之前**生效：
    #   只改这一份"发给模型的内容"，不动数据库、不动界面显示。
    #   放在这里而不是各条路径里 —— 对话与「查看提示词」预览自动保持一致。
    plugin_runtime.apply_to_plan(prompt, plugins)

    # 旧结构继续给出（stats 与界面读的是它），但数值来自融合后的结果：
    #   entries = 真正入选的世界书条目；matched = 关键词通道的候选总数。
    scan = world_book_scanner.ScanResult(
        entries=result.world_entries,
        dropped=max(result.book_dropped, 0),
        scanned_messages=len(history if scan_depth <= 0 else history[-scan_depth:]),
        matched=result.book_matched,
    )
    return SessionPrompt(prompt=prompt, scan=scan, recall=recall, memory_block=memory_block)


def prepare_turn(db: Session, session: NarrativeSession, history: list[Message]) -> PreparedTurn:
    """构建提示词、按预算裁剪上下文、构造适配器。

    这里会**顺带**把裁剪掉的旧对话压成滚动摘要写回数据库 ——
    因为一旦本轮发送成功，那些旧消息就再也不会进上下文了，
    不记下来的话模型会彻底"失忆"。

    3.9 起提示词的构建多了两步（世界书关键词触发、长期记忆召回），
    两者都在 build_session_prompt 里，且都**不会**让对话本身失败。

    ★ 预设（prompt preset）在此处生效：会话绑定的预设 > 用户全局默认预设。
      两者都没有时 preset=None，走内置装配 —— 与加这个功能之前完全一致。
    """
    provider_row = require_provider(db, session)
    adapter = build_adapter(provider_row)
    # ★ 备用模型：主模型失败且**尚未输出任何内容**时自动切换（见 app/llm/failover.py）。
    #   放在这里而不是 engine 的各条路径里：包一层适配器，流式与非流式同时受益。
    fallback_row = _load_fallback_row(db, provider_row)
    if fallback_row is not None:
        adapter = FailoverAdapter(
            primary=adapter,
            fallback=build_adapter(fallback_row),
            primary_model=provider_row.model_name,
            fallback_model=fallback_row.model_name,
            primary_name=provider_row.name,
            fallback_name=fallback_row.name,
        )
    # ★ 关掉「流式传输」时，包一层"整段返回"的适配器：事件序列与错误处理
    #   仍然只有 stream_reply 那一份，前端也不用改（它只是收不到逐字效果）。
    if not bool(getattr(provider_row, "stream_enabled", True)):
        adapter = WholeReplyAdapter(adapter)
    card, book = load_card_and_book(db, session)

    # ---------------- 记忆总结：由**会话设置**决定做不做（用户要求"不强制"）----------------
    # ★ 规则（见 app/narrative/summary.py 与「记忆管理面板」）：
    #   enabled=false       → 完全不总结、也不提醒
    #   到点 && auto=true   → 自动合并一次（花一次模型调用；这是用户自己在面板里开的）
    #   到点 && auto=false  → **只算出一个"该总结了"的状态**，界面弹横幅等他点按钮
    # ★ 无论哪条路，被覆盖的那一块都不再进提示词（否则总结与原文同时占 token）。
    summary_outcome = None
    summary_state: dict[str, Any] = {}
    if not sessions.is_pure_chat(session):
        summary_settings = summary_mod.load_settings(session)
        # 总结可以用**单独的模型配置**（面板里可选；不选就跟着会话模型）
        summary_adapter = adapter
        summary_provider_id = summary_settings.get("provider_id")
        if summary_provider_id and int(summary_provider_id) != int(provider_row.id):
            summary_adapter = _build_summary_adapter(db, session, int(summary_provider_id), adapter)
        if summary_settings.get("enabled") and summary_settings.get("auto"):
            summary_outcome = summary_mod.merge_block(
                session=session,
                adapter=summary_adapter,
                messages=history,
                db=db,
                settings=summary_settings,
            )
            logger.debug(
                "记忆总结（自动） | session_id={} merged={} 覆盖={}~{} 原因={}",
                session.id,
                summary_outcome.merged,
                summary_outcome.from_round,
                summary_outcome.to_round,
                summary_outcome.reason or "ok",
            )
            if summary_outcome.merged:
                db.commit()
                db.refresh(session)
        # 提醒状态：界面据此弹横幅（auto 开着就不必提醒了，免得打扰）
        summary_state = summary_mod.reminder_state(session, history, summary_settings)
        summary_state["content"] = str(session.rolling_summary or "")
        summary_state["enabled"] = bool(summary_settings.get("enabled"))
        if summary_settings.get("auto"):
            summary_state["due"] = False
        # 被总结覆盖的那一块不再进提示词（它们已经由这份总结代表）
        history = summary_mod.filter_uncovered(session, history)

    preset_row, preset_config = resolve_preset(db, session)
    built = build_session_prompt(
        user_id=session.user_id,
        session_id=session.id,
        card=card,
        book=book,
        history=history,
        preset=preset_row,
        preset_config=preset_config,
        user_name=current_user_name(db, session.user_id),
        # ★ 状态每轮都要回注：模型不知道 HP/背包/位置时，下一轮必然写飘。
        #   纯聊天没有状态协议；卡没定义状态栏（空 schema）时 render_for_prompt
        #   返回空串，prompt_builder 也就不会追加输出契约 —— 不硬塞一套它没用过的字段。
        state_block=(
            "" if sessions.is_pure_chat(session) else state_mod.render_for_prompt(session)
        ),
        # ★ schema 也要一起传：输出契约（追加在提示词最末）里的字段清单必须与
        #   "当前状态"那一节的字段一致，否则同一份提示词里前后自相矛盾。
        state_schema=state_mod.effective_schema(session),
        # ★ 记忆锚点：用户手写的硬设定，固定注入（排在 世界设定 → 锚点 → 回忆 之间）。
        #   纯聊天不注入（它刻意是一条干净通道）。
        anchors_block=(
            "" if sessions.is_pure_chat(session) else anchors_mod.render_block(session)
        ),
        # ★ 翻译中间件（prompt 模式）的"输出语言"要求：纯聊天也要（它就是给通用助手的）
        translate_hint=translate_mod.prompt_hint(translate_mod.load_settings(session)),
        # ★ 插件也要在这里带上：不带的话「查看提示词」看不到正则/注入的效果，
        #   而实际请求里却有 —— 又是一次"预览与实际不一致"。
        plugins=plugin_service.load_enabled(db, session.user_id),
        # ★ 纯聊天走通用助手装配（判据来自 kind 列，见 sessions.is_pure_chat）
        pure_chat=sessions.is_pure_chat(session),
    )
    prompt = built.prompt

    context = prepare_context(
        prompt.messages,
        budget=adapter.budget,
        summary=session.rolling_summary,
        # ★ 给召回内容预留位置：不预留的话，回忆会把最近的对话挤出预算，
        #   出现"模型记得很久以前的事，却不记得刚刚说了什么"的荒谬结果
        reserve_tokens=estimate_tokens(built.memory_block) if built.memory_block else 0,
        # ★ 深度注入的块在这里插回（在裁剪**之后**，见 context_manager 的说明）
        depth_messages=prompt.depth_messages,
        # Anthropic 不接受对话中间的 system 消息 → 让裁剪层做降级并回报
        mid_system_supported=getattr(adapter, "supports_mid_conversation_system", True),
        stats={
            "world_book_entries": prompt.world_book_entries,
            "world_book_matched": built.scan.matched,
            "recalled_memories": prompt.recalled_memories,
            "memory_error": getattr(built.recall, "error", None),
        },
    )

    if context.dropped_messages:
        _persist_summary(db, session, context)

    # 本轮的总结结果写进提示词元信息：界面要能说清"这份前情提要是模型写的还是本地压的"
    if summary_outcome is not None and summary_outcome.merged:
        prompt.summary = summary_outcome.to_dict()
        if summary_outcome.warning:
            prompt.warnings.append(summary_outcome.warning)
    # 提醒状态也带上：前端据此弹"该总结了"横幅（不花钱，只是提醒）
    if summary_state:
        prompt.summary_state = summary_state

    return PreparedTurn(
        prompt=prompt,
        context=context,
        adapter=adapter,
        provider_row=provider_row,
        card=card,
        book=book,
        # 两条"摘要被动过"的路径：① 分层合并（攒够一块就重写一份）
        #                            ② 超预算时的本地压缩（老路径，仍然保留为兜底）
        summary_updated=bool(context.dropped_messages)
        or bool(summary_outcome and summary_outcome.merged),
        summary=summary_outcome,
        # 适配层"改过参数"的回报在流式/非流式的第一个片段里（notes）
    )


def resolve_preset(db: Session, session: NarrativeSession) -> tuple[Any, Any]:
    """决定这条会话用哪套提示词预设，并把它解析成配置。

    返回 (预设 ORM 行 | None, 解析后的配置 | None)。

    ★ 第二个返回值是**用户预设 + 内置守卫预设合并后**的结果：
      守卫块永远接在最后。所以"rows 为 None"并不代表"config 为 None"
      —— 一个没绑任何预设的用户，config 里仍然有内置守卫规则。
      调用方不要用 row 是否为空去推断 config 是否为空（踩过一次）。

    ★ 为什么在这里解析而不是在 prompt_builder 里？
      因为 `prompt_builder` 刻意保持"纯函数"（不碰数据库），
      而预设的解析结果要在**装配、预览、界面提示**三处复用同一份，
      在这里解析一次最省事，也保证三处口径一致。
    """
    # 延迟导入：engine 是热点模块，preset 服务只在真正用到时才加载
    from app.narrative import presets as presets_mod
    from app.services import prompt_preset_service

    row = prompt_preset_service.resolve_for_session(db, session)
    base = presets_mod.from_config(row.config) if row is not None else None

    # ★ 内置守卫规则（身份认知 / 不跑偏 / 输出长度）**永远追加在用户预设之后**。
    #   用户明确要求这条规则在所有角色卡对话里都生效，所以它不是"备选项"。
    #
    #   两种情况：
    #     · 用户已经有一份内置预设（打开过预设页就会自动生成）→ 用他改过的那份；
    #     · 还没有 → 直接用代码里的出厂内容（**不在这里建库**：
    #       这里可能是流式生成的线程池里，中途 commit 会牵扯生成事务）。
    #   用户把内置预设删掉后就真的不生效了（删除是删，不是暂时隐藏）。
    guard_row = prompt_preset_service.get_builtin(db, session.user_id)
    if guard_row is not None:
        guard = presets_mod.from_config(guard_row.config)
    else:
        owner = db.get(User, session.user_id)
        dismissed = bool(getattr(owner, "builtin_preset_dismissed", False)) if owner else False
        guard = None if dismissed else presets_mod.build_guard_config()

    return row, presets_mod.merge_configs(base, guard)


def current_user_name(db: Session, user_id: int) -> str:
    """取用户名，供预设里的 `{{user}}` 宏使用。取不到就返回空串（宏替换成空）。

    ★ 做成公开函数是因为**有三处要用同一个口径**：
      真正发请求、`/sessions/{id}` 的提示词预览、预设装配预览。
      任何一处口径不同，就会出现"预览显示 A、实际发的是 B"。
    """
    row = db.get(User, user_id)
    return str(getattr(row, "username", "") or "") if row is not None else ""


def _latest_user_text(history: list[Message]) -> str:
    """取最后一条用户消息作为检索 query。

    ★ 为什么用用户新说的这句，而不是整个上下文？
      检索的语义单位是"用户现在想聊什么"。把角色的回复也拼进去，
      会让 query 被风格化的叙事文本带偏，召回质量反而下降。
    """
    for item in reversed(history):
        if getattr(item, "role", "") == "user":
            return str(getattr(item, "content", "") or "").strip()
    return ""


def remember_turn(
    *,
    user_id: int,
    session_id: int,
    user_message: Message,
    assistant_message: Message,
    char_name: str | None = None,
) -> str | None:
    """把这一轮对话写进长期记忆（失败只记日志，不影响对话）。"""
    return memory_mod.remember_turn(
        user_id=user_id,
        session_id=session_id,
        user_message=user_message,
        assistant_message=assistant_message,
        char_name=char_name,
    )


def _build_summary_adapter(db: Session, session: NarrativeSession, provider_id: int, fallback: Any) -> Any:
    """总结用**单独的模型配置**时构造一个适配器；配置不可用时退回会话模型。

    ★ 为什么要容错：用户可能删掉了那个配置 / 停用了它 / 没填 key。
      总结是"锦上添花"，绝不该因为它的模型坏了就让对话发不出去。
    """
    try:
        row = db.get(LLMProvider, provider_id)
        if row is None or not row.is_active:
            raise ValueError("配置不存在或已停用")
        adapter = build_adapter(row)
        logger.debug("总结将使用单独配置的模型 | session_id={} provider_id={}", session.id, provider_id)
        return adapter
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "总结模型配置不可用，已退回会话模型 | session_id={} provider_id={} err={}",
            getattr(session, "id", None),
            provider_id,
            exc,
        )
        return fallback


def _persist_summary(db: Session, session: NarrativeSession, context: ContextPlan) -> None:
    """把被裁掉的消息并进滚动总结，并记录覆盖到哪一条。

    ★ 为什么要记 summarized_until_message_id？
      否则下一轮又会把"最早的若干条"重新压一遍，摘要里全是重复内容。
      它同时是"下次从哪开始裁剪"的依据（字段早就为这个设计预留了）。
    ★ 第八轮起走 `summary.append_dropped`：**并进同一份正文**（替换而不是叠加），
      覆盖区间也一起推进 —— 与"每 N 轮分层合并"共用一份总结，不会出现两份前情提要。
    """
    last_dropped = context.dropped_messages[-1] if context.dropped_messages else None
    if last_dropped is None:
        return

    changed = summary_mod.append_dropped(
        session, context.dropped_messages, load_history(db, session.id)
    )
    if not changed:
        return
    db.commit()
    db.refresh(session)
    logger.info(
        "上下文超预算，已并进滚动总结 | session_id={} 丢弃 {} 条 覆盖至第 {} 轮",
        session.id,
        len(context.dropped_messages),
        session.summary_to_round,
    )


# ==================================================================
#  落库
# ==================================================================
def load_history(db: Session, session_id: int) -> list[Message]:
    """取全部消息历史（升序）。

    ★ 这里取**全部**而不是只取最近 N 条，因为裁剪需要知道被丢掉了什么
      （要生成摘要）。真正怕的是"把整个库读进内存"，
      而单条会话的消息量级（几千条、几 MB）远达不到那个程度。
    """
    from sqlalchemy import select

    rows = list(
        db.scalars(
            select(Message).where(Message.session_id == session_id).order_by(Message.id)
        ).all()
    )
    return rows


def _translate_adapter(db: Session, session: NarrativeSession, provider_id: int | None) -> Any | None:
    """翻译中间件用哪个模型：用户单独指了一个就用它，否则跟随**会话模型**。

    ★ 与"总结用单独模型"同一套容错：配置被删/停用时退回会话模型；
      会话模型都建不出来（没配 key 之类）就返回 None —— 翻译直接跳过，
      绝不让"翻译"这件事把对话弄失败。
    """
    row = None
    if provider_id:
        try:
            candidate = db.get(LLMProvider, provider_id)
            if candidate is not None and candidate.is_active:
                row = candidate
            else:
                logger.warning(
                    "翻译模型配置不可用，已退回会话模型 | session_id={} provider_id={}",
                    getattr(session, "id", None),
                    provider_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("翻译模型配置读取失败 | provider_id={} err={}", provider_id, exc)
    try:
        source = row or require_provider(db, session)
        return build_adapter(source)
    except Exception as exc:  # noqa: BLE001
        logger.warning("翻译中间件拿不到可用模型，本次跳过 | session_id={} err={}", getattr(session, "id", None), exc)
        return None


def _translate_input(
    db: Session, session: NarrativeSession, text: str, notes: list[str] | None
) -> tuple[str, translate_mod.TranslationOutcome | None]:
    """输入侧翻译：把用户的话译成"卡的语言"再发给模型。

    返回 `(模型要看到的文本, 结果或 None)`。结果的 `text` 已被换成**用户原话**
    （输入侧的"另一份文本"是原文，不是译文 —— 界面默认显示原文）。
    ★ 只有**启用且模式是 middleware** 时才真的调用模型；`prompt` 模式返回原样。
    """
    settings = translate_mod.load_settings(session)
    if not translate_mod.uses_model(settings, translate_mod.DIRECTION_INPUT):
        return text, None
    adapter = _translate_adapter(db, session, settings.get("provider_id"))
    outcome = translate_mod.run(
        text,
        settings,
        direction=translate_mod.DIRECTION_INPUT,
        adapter=adapter,
        provider_id=settings.get("provider_id"),
    )
    if adapter is not None:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001
            pass
    if not outcome.ok:
        # 跳过（已经是目标语言 / 太长）不打扰用户；真失败才说一句
        if outcome.error and notes is not None:
            notes.append(
                f"翻译中间件没能把你的输入译成{outcome.lang}（{outcome.error}），"
                "这一轮按原文发出。"
            )
        return text, None
    translated = outcome.text
    # 另一份 = 用户原文（不变量：`translation.text` 永远是"给人看的那一份"）
    outcome.text = text
    if notes is not None:
        notes.append(f"你的输入已译成{outcome.lang}再发给模型（{outcome.tokens} token）。")
    return translated, outcome


def save_user_message(db: Session, session: NarrativeSession, content: str) -> Message:
    """先落库用户消息（见模块文档的说明）。

    ★ 骰子指令在这里**掷定**（`/r 3d6`、`掷骰 1d20+5`）：这是用户消息唯一的落库点，
      骰点写进 `messages.rolls_json` 之后就不会再变 —— 刷新界面、重新生成、
      「查看提示词」预览读到的都是同一组数字（骰点不能重掷，重掷等于抽卡）。
      没有启用骰子插件时这里什么都不做（连扫描都跳过）。

    ★ 翻译中间件（输入侧）也在这里生效：`content` 存的是**模型看到的文本**（译文），
      用户原话存进 `translation_json.text`（界面默认显示原文，可切换）。
      于是提示词装配不需要任何"替换历史"的机制 —— 少一处改动就少一处不一致。
    """
    text = content.strip()
    if not text:
        raise BadRequestError("消息内容不能为空")

    rolls: list = []
    rolls_json: str | None = None
    spec = plugin_runtime.dice_spec(plugin_service.load_enabled(db, session.user_id))
    if spec is not None:
        rolls = dice_mod.scan_commands(text, spec)
        rolls_json = dice_mod.dumps(
            rolls, show_detail=bool(spec.get("show_detail", True))
        )

    translate_notes: list[str] = []
    model_text, input_outcome = _translate_input(db, session, text, translate_notes)
    if input_outcome is not None and input_outcome.used_model:
        # 翻译消耗计进会话累计（"累计 token"必须如实反映真实花费）
        session.total_tokens = (session.total_tokens or 0) + int(input_outcome.tokens or 0)
    for note in translate_notes:
        logger.info("翻译中间件（输入侧） | session_id={} note={}", session.id, note)

    message = Message(
        session_id=session.id,
        role="user",
        content=model_text,
        token_count=estimate_tokens(model_text),
        model_name=None,
        rolls_json=rolls_json,
        translation_json=translate_mod.dumps(input_outcome),
    )
    db.add(message)
    session.message_count = (session.message_count or 0) + 1
    session.last_active_at = sessions.now()
    db.commit()
    db.refresh(message)
    db.refresh(session)
    return message


@dataclass
class _DisplayText:
    """只为"写长期记忆"准备的一份文本替身（`remember_turn` 只读 content/id）。"""

    content: str
    id: int | None = None


def _display_message(message: Message) -> Any:
    """写记忆时用**给人看的那一份**文本。

    ★ 为什么不是 `content`：跨语言会话里，用户用中文提问、检索也是中文，
      而 `content` 在输入侧是译文、在输出侧是模型原文。把外语原文写进记忆库，
      下一轮用中文提问就召回不到 —— 记忆要按"用户读到的语言"存才有用。
    """
    data = translate_mod.loads(getattr(message, "translation_json", None))
    if data and data.get("text"):
        return _DisplayText(content=str(data["text"]), id=getattr(message, "id", None))
    return message


def translate_one(
    db: Session, session: NarrativeSession, message: Message
) -> tuple[translate_mod.TranslationOutcome | None, str]:
    """按需翻译**单独一条**消息（用户在消息上点「🌐 翻译」）。

    ★ 与自动翻译（`translate_reply` / `_translate_input`）的**唯一区别**：
      **不看总开关与模式** —— 点这一下就是用户的同意（与"立即总结"同一个哲学：
      需要用户花钱的选择权交给他）。其余全部复用：同一份设置（目标语言 / 指定模型）、
      同一套跳过判据、同一条降思考闸门、同一个 `translation_json` 形状
      （`content` 仍是模型看到的那份，`translation.text` 仍是给人看的那份）。
    ★ 已有译文的消息**不重译**：否则会把"原文/译文"的关系冲掉
      （输入侧消息的 `translation.text` 是用户原话，重译会让它变成译文 —— 那是数据损失）。
    ★ 返回 `(结果或 None, 给用户的一句话)`：跳过/失败都要如实说，不许静默什么都不做。
    """
    existing = translate_mod.loads(getattr(message, "translation_json", None))
    if existing and existing.get("text"):
        return None, "这条已经有译文了，没有重复调用模型。"

    settings = translate_mod.load_settings(session)
    # ★ 点这一下就是同意：**绕过总开关与模式**（`run()` 会检查它们）。
    #   其余判据（已经是目标语言 / 太长 / 没有模型配置）一律照旧，跳过原因如实回报。
    run_settings = dict(settings)
    run_settings["enabled"] = True
    run_settings["mode"] = translate_mod.MODE_MIDDLEWARE
    body = str(message.content or "")
    if not body.strip():
        return None, "这条消息是空的，没什么可译。"
    if len(body) > translate_mod.MAX_CHARS:
        return None, f"这条太长（{len(body)} 字 > {translate_mod.MAX_CHARS}），没有翻译。"
    lang = str(settings.get("target_lang") or translate_mod.DEFAULT_TARGET_LANG)
    if translate_mod.looks_like(body, lang):
        return None, f"这条看起来已经是{lang}，不需要翻译。"

    provider_id = settings.get("provider_id")
    adapter = _translate_adapter(db, session, provider_id)
    if adapter is None:
        return None, "没有可用的模型配置，这次没有翻译。"
    try:
        outcome = translate_mod.run(
            body,
            run_settings,
            direction=translate_mod.DIRECTION_REPLY,
            adapter=adapter,
            provider_id=provider_id,
        )
    finally:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001 - 关不掉不影响结果
            pass

    if not outcome.ok:
        reason = outcome.error or outcome.skipped or "未知原因"
        return None, f"翻译没成功（{reason}），已保留原文。"

    message.translation_json = translate_mod.dumps(outcome)
    if outcome.used_model and outcome.tokens:
        # 与自动翻译同一条口径：花在翻译上的钱必须计进会话累计
        session.total_tokens = (session.total_tokens or 0) + int(outcome.tokens)
    db.commit()
    db.refresh(message)
    db.refresh(session)
    logger.info(
        "按需翻译单条消息 | session_id={} message_id={} lang={} tokens={}",
        session.id,
        message.id,
        outcome.lang,
        outcome.tokens,
    )
    return outcome, f"已译成{outcome.lang}（{outcome.tokens} token）"


def translate_reply(
    db: Session,
    session: NarrativeSession,
    message: Message,
    notes: list[str] | None = None,
) -> str | None:
    """输出侧翻译：回复落库之后，把模型原文译成目标语言并存进消息上。

    返回译文（没译就 None）。**任何失败都只记 notes / 日志，绝不影响这一轮回复**。
    ★ 为什么在落库之后做：回复是流式逐字显示给用户的（那时还没有完整文本），
      翻译必须有完整回复才能做。所以用户会先看到原文，随后界面刷新成译文。
    """
    settings = translate_mod.load_settings(session)
    if not translate_mod.uses_model(settings, translate_mod.DIRECTION_REPLY):
        return None
    adapter = _translate_adapter(db, session, settings.get("provider_id"))
    outcome = translate_mod.run(
        message.content,
        settings,
        direction=translate_mod.DIRECTION_REPLY,
        adapter=adapter,
        provider_id=settings.get("provider_id"),
    )
    if adapter is not None:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001
            pass
    if not outcome.ok:
        if outcome.error and notes is not None:
            notes.append(
                f"翻译中间件没能把回复译成{outcome.lang}（{outcome.error}），已保留原文。"
            )
        return None

    message.translation_json = translate_mod.dumps(outcome)
    if outcome.used_model and outcome.tokens:
        session.total_tokens = (session.total_tokens or 0) + int(outcome.tokens)
    db.commit()
    db.refresh(message)
    db.refresh(session)
    logger.info(
        "回复已翻译 | session_id={} lang={} 用模型={} tokens={}",
        session.id,
        outcome.lang,
        outcome.used_model,
        outcome.tokens,
    )
    if notes is not None:
        notes.append(
            f"本轮回复已由翻译中间件译成{outcome.lang}（{outcome.tokens} token），"
            "消息下方可切换原文 / 译文。"
        )
    return outcome.text


def _dice_notes(rolls: list[Any]) -> list[str]:
    """把骰点里**需要特意告诉用户**的事变成提醒。

    ★ 正常的点数不提醒：它已经替换进正文、界面上还有一颗骰子气泡
      （每轮都弹一次"掷了 1d20 = 13"是噪音）。
      只有"模型写了坏表达式"这种事必须说 —— 否则用户只会看到一句
      "（掷骰失败：……）"埋在正文里，不知道是模型的问题还是插件的问题。
    """
    errors = [item for item in rolls if str(getattr(item, "error", ""))]
    if not errors:
        return []
    detail = "；".join(str(item.error) for item in errors)
    return [
        f"模型这一轮请求掷骰，但有 {len(errors)} 次没有掷成：{detail}。"
        "点数没有被编造，请让它按正确写法重试（例：`<roll>1d20+5</roll>`）。"
    ]


def _usage_json(usage: TokenUsage | None, estimated_input_tokens: int | None) -> str | None:
    """把这一轮的真实用量打包成落库 JSON（含**我们发出去前对输入的估算**）。

    ★ 为什么要带上估算：`messages.token_count` 只存 completion 那一半、
      `sessions.total_tokens` 只存累计总和 ⇒ 事后回答不了"启发式估算 vs 厂商
      `prompt_tokens` 差多少"（论文里一直只能写"未做过对照实测"）。
      两份数字放在同一行，`scripts/token_accuracy.py` 只读回放即可出对照表（不花钱）。
    ★ 厂商没给 usage（中断 / 网关不返回）时返回 None，脚本会如实标注样本数。
    """
    if usage is None or usage.is_empty:
        return None
    payload = dict(usage.to_dict())
    payload["estimated_input_tokens"] = int(estimated_input_tokens or 0)
    return json.dumps(payload, ensure_ascii=False)


def save_assistant_reply(
    db: Session,
    session: NarrativeSession,
    *,
    content: str,
    model_name: str | None,
    token_count: int,
    latency_ms: int | None,
    usage: TokenUsage | None = None,
    estimated_input_tokens: int | None = None,
    notes: list[str] | None = None,
) -> Message:
    """助手回复落库，并更新会话统计。

    ★ 这里是**所有路径唯一的落库点**（流式 / 非流式 / 中断保留），
      所以"收状态块"也放在这里：剥掉 `<state>` 块 → 校验 → 写回会话，
      并把校验提醒追加进调用方的 `notes`（前端能看到"HP 越界已夹住"这类说明）。

    ★ 纯聊天会话例外：它压根没被要求输出状态块，所以
      ① 仍然把可能出现的 `<state>` 剥掉（绝不能让用户看到原始 JSON），
      ② 但**不落库、也不提醒** —— 我们没要求它，就不能反过来怪它。
    ★ 卡没有定义状态栏（空 schema）时同理：apply_reply 只剥块、不落库、不提醒。

    ★ 骰子（`<roll>` 标签）也在这里结算，理由与状态块完全相同：
      这是唯一的落库点，而"这一轮到底掷出了几点"事后无法从正文反推
      （标签已被替换成明文）。标签**无论插件是否启用都会被剥掉** ——
      绝不能让用户看到 `<roll>1d20</roll>` 这种原始协议文本。
    """
    if sessions.is_pure_chat(session):
        content, _ = state_mod.extract_state_block(content)
        state_meta = {"required": False}
        raw_state = None
    else:
        content, state_notes, state_meta = state_mod.apply_reply_with_meta(session, content)
        # ★ 原始 <state> 块单独落一列（界面「看作者原格式」用）：卡作者写的复杂排版
        #   被解析成结构化字段后就没了，留一份原文；遥测里不带它（漂移统计只认 counts）。
        raw_state = state_meta.pop("raw", None)
        if notes is not None:
            notes.extend(state_notes)

    dice_spec = plugin_runtime.dice_spec(plugin_service.load_enabled(db, session.user_id))
    content, dice_rolls = dice_mod.resolve_model_rolls(content, dice_spec)
    if notes is not None and dice_spec is not None:
        notes.extend(_dice_notes(dice_rolls))

    message = Message(
        session_id=session.id,
        role="assistant",
        content=content,
        token_count=int(token_count or 0),
        model_name=model_name,
        latency_ms=latency_ms,
        # ★ 状态遥测：这一轮模型自称的状态与校验后落库的状态差在哪。
        #   必须在这里写（落库的唯一入口），事后无法从正文反推 —— `<state>` 已被剥掉。
        #   `required=False` 表示这一轮压根没要求输出状态块（纯聊天 / 卡没声明状态栏），
        #   统计漂移时**不该**把它算成"漏输出"。
        state_meta_json=json.dumps(state_meta, ensure_ascii=False) if state_meta else None,
        # ★ 原始 <state> 文本：留给界面「看作者原格式」（解析成字段后原排版就没了）
        state_raw_json=raw_state,
        # ★ 骰点：模型请求的点数（或"它写了个坏表达式"这件事）也在这里落库
        rolls_json=dice_mod.dumps(
            dice_rolls,
            show_detail=bool(dice_spec.get("show_detail", True)) if dice_spec else True,
        ),
        # ★ 逐轮真实用量（第十六轮）：厂商的 prompt/completion/reasoning 分项 +
        #   **我们发出去前对输入的估算** —— 有了它才能回答"启发式估算 vs 厂商 prompt_tokens
        #   差多少"（`scripts/token_accuracy.py` 只读回放，不额外花钱）。
        #   厂商没给 usage（中断 / 某些网关）时留 NULL，脚本会如实标注样本数。
        usage_json=_usage_json(usage, estimated_input_tokens),
    )
    db.add(message)
    session.message_count = (session.message_count or 0) + 1
    # ★ total_tokens 统计的是"累计消耗"，所以把输入与输出都算进去；
    #   只算输出会让用户严重低估成本（长会话里输入往往远大于输出）
    spent = usage.total_tokens if usage is not None and usage.total_tokens else token_count
    session.total_tokens = (session.total_tokens or 0) + int(spent or 0)
    session.last_active_at = sessions.now()
    db.commit()
    db.refresh(message)
    db.refresh(session)
    return message


def touch_session(db: Session, session: NarrativeSession) -> None:
    """只更新活跃时间（用于"回复失败但用户确实说过话"的场景）。"""
    session.last_active_at = sessions.now()
    db.commit()


# ==================================================================
#  非流式
# ==================================================================
def reply(db: Session, session: NarrativeSession, user_message: Message) -> tuple[Message, ChatResult, PreparedTurn]:
    """非流式：一次性拿到完整回复。"""
    history = load_history(db, session.id)
    turn = prepare_turn(db, session, history)

    started = time.perf_counter()
    try:
        result = turn.adapter.chat(build_request(adapter=turn.adapter, plan=turn.context))
    except BaseException:
        # 调用失败也要更新活跃时间：用户确实说过话，列表排序应当把它顶上来
        touch_session(db, session)
        raise
    finally:
        turn.adapter.close()

    latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
    message = save_assistant_reply(
        db,
        session,
        content=result.content,
        model_name=result.model or turn.provider_row.model_name,
        token_count=result.usage.completion_tokens or estimate_tokens(result.content),
        latency_ms=latency_ms,
        usage=result.usage,
        estimated_input_tokens=turn.context.estimated_tokens,
        # 状态校验的提醒挂到 ChatResult.notes 上，一路带到界面
        notes=result.notes,
    )
    # ★ 落库成功之后才写长期记忆：顺序反了会出现"记忆里有、对话记录里没有"的幽灵内容
    #   ★ 翻译中间件：先把回复译好（如果开了），再写记忆 ——
    #     写进记忆的是**给人看的那一份**（见 _display_message）。
    translate_reply(db, session, message, result.notes)
    remember_turn(
        user_id=session.user_id,
        session_id=session.id,
        user_message=_display_message(user_message),
        assistant_message=_display_message(message),
        char_name=getattr(turn.card, "name", None),
    )
    return message, result, turn


# ==================================================================
#  流式
# ==================================================================
def stream_reply(
    db: Session, session: NarrativeSession, user_message: Message
) -> Iterator[tuple[str, Any]]:
    """流式：逐段产出 (事件类型, 数据)。

    ★ 事件类型与前端约定（也是 SSE 的 event 名）：
        meta        元信息（模型名、提示词来源、上下文裁剪情况）
        notes       适配层为满足协议所做的调整（可能没有）
        reason      推理模型的思考过程增量
        delta       正文增量
        translating ★ 翻译中间件开始工作（**只在确实要调模型时**发；正文此刻已经流完了）
        done        收尾（用量、消息 id、耗时、译文）
        error       出错（★ 必须发出去，不能让前端看到"连接莫名断了"）

    ★ 关于数据库会话：
      这个函数是**同步生成器**，由接口层的 async 生成器用
      run_in_threadpool 逐步驱动。所以这里的 db 操作都在线程池里执行，
      不会阻塞事件循环 —— 这是本项目「同步 ORM + 异步流式」的落点。
    """
    history = load_history(db, session.id)

    try:
        turn = prepare_turn(db, session, history)
    except BaseException:
        touch_session(db, session)
        raise

    adapter = turn.adapter
    started = time.perf_counter()

    yield "meta", {
        **turn.to_meta(),
        "model_name": turn.provider_row.model_name,
        "provider_name": turn.provider_row.name,
    }

    buffer: list[str] = []
    reasoning: list[str] = []
    usage: TokenUsage | None = None
    finish_reason: str | None = None
    notes: list[str] = []
    emitted_any = False

    try:
        for chunk in adapter.stream_chat(build_request(adapter=adapter, plan=turn.context)):
            if chunk.notes:
                # 适配层改动过用户参数时必须回报（红线：拒绝静默降级）
                notes.extend(chunk.notes)
                yield "notes", {"notes": list(chunk.notes)}
            if chunk.reasoning_delta:
                reasoning.append(chunk.reasoning_delta)
                yield "reason", {"delta": chunk.reasoning_delta}
            if chunk.delta:
                buffer.append(chunk.delta)
                emitted_any = True
                yield "delta", {"delta": chunk.delta}
            if chunk.usage is not None:
                usage = chunk.usage
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason
    except BaseException:
        # 调用中途失败：把已经生成的部分**保留下来**（用户至少看到了那些字），
        # 然后让异常继续往上抛，由接口层发 error 事件
        if buffer:
            _keep_partial(db, session, turn, "".join(buffer), reasoning, usage, started)
        else:
            touch_session(db, session)
        raise
    finally:
        adapter.close()

    content = "".join(buffer)

    # ★ 与适配层的 _raise_if_no_content 保持同一套语义，只是这里不能"抛"
    #   （已经 yield 过内容，抛异常只会让前端看到一个断掉的流）：
    #   改成产出一个明确的 error 事件，把原因说清楚。
    if not content.strip():
        if finish_reason == "length" and not reasoning:
            reason_text = (
                "模型输出被 max_tokens 截断，且没有产生正文。"
                "推理模型会先把输出配额花在思考上，请调大「最大输出 Token」。"
            )
        elif reasoning:
            reason_text = "模型只返回了思考过程，没有产生正文。请调大「最大输出 Token」。"
        else:
            reason_text = "模型返回了空回复。"
        detail = {
            "finish_reason": finish_reason,
            "usage": usage.to_dict() if usage else None,
            "reasoning_preview": "".join(reasoning)[:200],
        }
        if buffer:
            _keep_partial(db, session, turn, content, reasoning, usage, started)
        else:
            touch_session(db, session)
        yield "error", {
            "code": "LLM_EMPTY_REPLY",
            "message": reason_text,
            "status": 502,
            "detail": detail,
        }
        return

    latency_ms = int((time.perf_counter() - started) * 1000)
    message = save_assistant_reply(
        db,
        session,
        content=content,
        model_name=_effective_model_name(turn),
        token_count=(usage.completion_tokens if usage else 0) or estimate_tokens(content),
        latency_ms=latency_ms,
        usage=usage,
        estimated_input_tokens=turn.context.estimated_tokens,
        # 状态校验的提醒随 "done" 事件一起发给前端
        notes=notes,
    )
    # ★ 翻译中间件：先把回复译好，再写记忆（记忆写"给人看的那一份"，见 _display_message）
    #   ★ 译之前先告诉前端一声：这一步是**同步的模型调用**（实测好几秒），
    #     不提示的话用户看完英文原文就干等，会以为界面卡住了。
    #     只在**确实会调模型**时才提示（同一套跳过判据），否则"提示了却没译"更让人困惑。
    tr_settings = translate_mod.load_settings(session)
    if translate_mod.will_call_model(
        tr_settings, translate_mod.DIRECTION_REPLY, message.content
    ):
        yield "translating", {
            "direction": translate_mod.DIRECTION_REPLY,
            "lang": tr_settings.get("target_lang"),
        }
    translate_reply(db, session, message, notes)
    remember_turn(
        user_id=session.user_id,
        session_id=session.id,
        user_message=_display_message(user_message),
        assistant_message=_display_message(message),
        char_name=getattr(turn.card, "name", None),
    )

    truncated = finish_reason == "length"
    if truncated:
        notes.append(
            "模型输出达到 max_tokens 上限被截断（该上限包含思考过程 token），"
            "本轮回复可能不完整。"
        )

    yield "done", {
        "message_id": message.id,
        "model_name": message.model_name,
        "latency_ms": latency_ms,
        "finish_reason": finish_reason,
        "truncated": truncated,
        "usage": (usage or TokenUsage()).to_dict(),
        "token_count": message.token_count,
        "reasoning": "".join(reasoning),
        "notes": notes,
        # ★ 骰点随 "done" 一起返回：前端不必等刷新就把骰子气泡显示出来
        #   （`<roll>` 已经被替换成明文点数，界面上再给一颗可读的骰子）
        "rolls": dice_mod.loads(getattr(message, "rolls_json", None)),
        # ★ 翻译中间件：译好了就随 "done" 一起回，前端立刻切成"给人看的那一份"
        #   （否则用户要先看到外语原文，等下一次刷新才变中文）
        "translation": translate_mod.loads(getattr(message, "translation_json", None)),
        "session": {
            "message_count": session.message_count,
            "total_tokens": session.total_tokens,
            "rolling_summary_updated": turn.summary_updated,
        },
    }


def _keep_partial(
    db: Session,
    session: NarrativeSession,
    turn: PreparedTurn,
    content: str,
    reasoning: list[str],
    usage: TokenUsage | None,
    started: float,
) -> None:
    """中途失败时，把已经生成的部分内容存下来并标记为不完整。

    ★ 为什么不留着不存？
      用户已经看到那半段文字了。如果不落库，刷新页面它就消失了，
      用户会以为"我刚才看到的内容是幻觉"。
      存下来并加上"（回复中断）"的标注，用户能接着往下说。
    """
    text = content + "\n\n……（回复中断）"
    try:
        save_assistant_reply(
            db,
            session,
            content=text,
            model_name=turn.provider_row.model_name,
            token_count=(usage.completion_tokens if usage else 0) or estimate_tokens(text),
            latency_ms=int((time.perf_counter() - started) * 1000),
            usage=usage,
            estimated_input_tokens=turn.context.estimated_tokens,
        )
    except BaseException:  # noqa: BLE001 - 落库失败不能盖住原始错误
        logger.exception("保存中断的部分回复失败 | session_id={}", session.id)
        db.rollback()
