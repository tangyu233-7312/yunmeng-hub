"""叙事会话的请求 / 响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class MessageOut(BaseModel):
    """一条消息（对话界面直接渲染它）。"""

    id: int
    role: Literal["system", "user", "assistant"] = Field(..., description="发言者")
    content: str
    token_count: int = Field(default=0, description="本条消息占用的 token（估算或来自模型用量）")
    model_name: str | None = Field(default=None, description="生成它的模型名（用户消息为 null）")
    latency_ms: int | None = Field(default=None, description="模型响应耗时（毫秒）")
    rolls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="这条消息里掷出的骰点（骰子插件）："
        "[{source, expression, label, total, faces, terms, compare, target, success, error, text}]；"
        "没有启用骰子插件时一律为空数组",
    )
    translation: dict[str, Any] | None = Field(
        default=None,
        description="★ 翻译中间件的「另一份文本」（没译过就是 null）："
        "{text, direction: reply|input, lang, display: content|translation, used_model, "
        "skipped, tokens, model, error}。不变量：`content` 是**模型看到的文本**，"
        "`translation.text` 是**给人看的文本**（输出侧=译文，输入侧=用户原话），"
        "`display` 说明界面该显示哪一份。",
    )
    state_raw: str | None = Field(
        default=None,
        description="★ 这条回复里模型输出的 `<state>` **原始文本**（没输出就是 null）："
        "状态按 schema 解析成字段后，卡作者写的复杂/美化排版就看不到了，"
        "界面用它提供「看作者原格式」。",
    )
    created_at: datetime


class SessionBrief(BaseModel):
    """会话列表项（★ 精简结构：不带消息正文，只有一条预览）。"""

    id: int
    title: str
    status: Literal["active", "archived"]
    character_card: dict[str, Any] | None = Field(
        default=None, description="关联角色卡的精简引用 {id, name, avatar_url}"
    )
    provider_name: str | None = Field(default=None, description="使用的模型配置别名")
    model_name: str | None = Field(default=None, description="实际调用的模型名")
    prompt_preset_name: str | None = Field(
        default=None, description="生效的提示词预设名（空 = 内置装配）"
    )
    message_count: int
    total_tokens: int
    last_message_preview: str | None = Field(default=None, description="最后一条消息的前几十字")
    last_message_role: str | None = None
    last_active_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    warnings: list[str] = Field(
        default_factory=list,
        description="会话本身的问题（例如角色卡已被删除），列表上就要提醒用户",
    )


class SessionContextInfo(BaseModel):
    """上下文与提示词的使用情况（界面上"这一轮到底发了什么"的依据）。"""

    estimated_input_tokens: int = 0
    input_budget: int = 0
    history_kept: int = 0
    history_dropped: int = 0
    summary_tokens: int = 0
    recalled_memories: int = Field(default=0, description="本次召回的长期记忆条数")
    memory_error: str | None = Field(
        default=None, description="记忆检索失败的原因（向量库不可用时会降级继续对话）"
    )
    world_book_entries: int = Field(default=0, description="本次注入的世界书条目数")
    world_book_matched: int = Field(default=0, description="本次命中的世界书条目数")
    system_truncated: bool = False
    trimmed: bool = False
    note: str = ""


class PromptInfo(BaseModel):
    """本次提示词的构建结果（用于界面上的"查看提示词"）。"""

    system_prompt: str = ""
    system_prompt_source: Literal[
        "card",
        "assembled",
        "world_book_only",
        "memory_only",
        "none",
        "preset",
        # 纯聊天会话（无角色）：走「通用助手」装配，不套预设与守卫
        "pure_chat",
    ] = "none"
    world_book_entries: int = Field(
        default=0, description="本次注入了多少条世界书设定（关键词触发的结果）"
    )
    recalled_memories: int = Field(default=0, description="本次召回了多少条长期记忆")
    has_post_history_instructions: bool = False
    warnings: list[str] = Field(default_factory=list)

    # ---------------- 混合检索（关键词 + 语义融合）----------------
    # ★ 为什么要把这些摊给界面：加了融合/去重/重排之后，排序不再是"命中就进"，
    #   用户没法自己判断"为什么这条进来了、那条没有" —— 界面上必须说得清。
    retrieval: dict[str, Any] = Field(
        default_factory=dict,
        description="混合检索明细：{mode, budget_tokens, used_tokens, lexical_total, "
        "semantic_total, injected, book_injected, memory_injected, dropped, deduped, "
        "errors, items:[{key,source,channels,lexical_rank,semantic_rank,score,tokens,"
        "reason,preview,matched_keys}]}",
    )
    retrieval_summary: str = Field(
        default="", description="混合检索的一行摘要（两路候选数 / 去重 / 注入 / 预算占用）"
    )
    retrieval_items: list[str] = Field(
        default_factory=list, description="每条候选一行：来源 / 两路名次 / 最终分 / 去留原因"
    )

    # ---------------- 剧情总结（分层合并）----------------
    summary: dict[str, Any] = Field(
        default_factory=dict,
        description="★ 本轮「剧情总结」的结果：{merged, from_round, to_round, label, "
        "used_model, warning, chars, tokens}。used_model=false 表示模型调用失败、"
        "已退回本地压缩（界面必须如实说明，不许假装是模型写的）",
    )
    summary_state: dict[str, Any] = Field(
        default_factory=dict,
        description="★ 「该总结了」的提醒状态（不花钱）：{due, pending, rounds, from_round, "
        "to_round, auto, remind, mode, cost_tokens}。前端据此弹横幅等用户点「立即总结」",
    )

    # ---------------- 提示词预设（prompt preset）----------------
    # 这几个字段存在的意义：破甲这类东西**看不见就等于不知道有没有生效**。
    preset_id: int | None = Field(default=None, description="本次使用的预设ID（空 = 内置装配）")
    preset_name: str | None = None
    preset_blocks_used: list[str] = Field(
        default_factory=list, description="真正参与装配的块（按顺序）"
    )
    preset_blocks_skipped: list[str] = Field(
        default_factory=list, description="被跳过的块及原因（没内容 / 本系统不支持）"
    )
    preset_blocks_disabled: list[str] = Field(
        default_factory=list, description="被用户显式禁用的块"
    )
    preset_unknown_macros: list[str] = Field(
        default_factory=list, description="本系统不认识的宏（会原样保留在提示词里）"
    )
    depth_blocks: list[int] = Field(
        default_factory=list, description="深度注入的块，各自插在倒数第几条之前"
    )


# ==================================================================
#  长期记忆（3.9）
# ==================================================================
class MemoryHitOut(BaseModel):
    """一条召回/检索到的记忆。"""

    memory_id: str
    text: str
    similarity: float = Field(..., description="相似度（1 最相似，0 无关）")
    kind: str | None = Field(default=None, description="dialogue / summary / fact")


class MemoryListOut(BaseModel):
    """某个会话的记忆检索结果。"""

    query: str = ""
    hits: list[MemoryHitOut] = Field(default_factory=list)
    total: int = 0


class MemoryFactCreate(BaseModel):
    """手动记住一条设定事实。"""

    text: str = Field(
        ..., min_length=1, max_length=4000, description="要记住的内容（建议写成完整的一句话）"
    )

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("记忆内容不能只有空白字符")
        return text


class SessionDetail(BaseModel):
    """会话详情（含消息历史）。"""

    id: int
    title: str
    status: Literal["active", "archived"]
    pure_chat: bool = Field(
        default=False,
        description="★ 是否为**纯聊天会话**（没有角色卡）。"
        "这类会话不注入人设 / 预设 / 状态协议，提示词是「通用助手」那一条分支；"
        "界面据此隐藏状态栏、显示「纯聊天」标记",
    )
    character_card: dict[str, Any] | None = None
    provider: dict[str, Any] | None = Field(
        default=None,
        description="使用的模型配置 {id, name, model_name, provider_type, context_window, "
        "max_tokens, reasoning_effort, stream_enabled} —— 纯聊天会话拿它当"
        "「这套 API 到底是怎么被调用的」的体检表",
    )
    prompt_preset: dict[str, Any] | None = Field(
        default=None,
        description="当前会话绑定的提示词预设 {id, name, source}；"
        "null 表示未绑定（会用全局默认预设或内置装配）",
    )
    effective_preset: dict[str, Any] | None = Field(
        default=None,
        description="★ 实际生效的预设（会话绑定 > 全局默认）。"
        "与 prompt_preset 区分开：用户需要知道「我没绑，但系统替我用了哪一套」",
    )
    builtin_guard: dict[str, Any] | None = Field(
        default=None,
        description="★ 内置守卫规则（身份认知 / 剧情不跑偏 / 输出长度）。"
        "它**永远追加在用户预设之后**而不是替代它，所以单独报一份："
        "{id, name, from: builtin|builtin_factory, min_reply_chars}。"
        "null 表示用户把它删掉了（删除是删，不会自动重建）。",
    )
    rolling_summary: str | None = None
    summary_coverage: dict[str, int] | None = Field(
        default=None,
        description="★ 滚动总结覆盖的轮次区间 {from_round, to_round}；"
        "null = 还没有总结。轮 = 一问一答，开场白算第 1 轮的开头",
    )
    memory_summary: dict[str, Any] = Field(
        default_factory=dict,
        description="★「记忆管理面板」要的状态（会话页据此渲染横幅与面板）："
        "{enabled, auto, remind, rounds, mode, coverage, due, pending, from_round, "
        "to_round, cost_tokens, chars}",
    )
    state: dict[str, Any] | None = Field(
        default=None,
        description="★ 结构化状态，已校验并落库在会话上；"
        "由模型每轮输出的 <state> 块解析而来（字段清单见 state_schema）。"
        "null = 还没有状态。",
    )
    state_schema: dict[str, Any] | None = Field(
        default=None,
        description="★ 这个会话状态栏的**字段定义**（建会话时从角色卡 / 世界书解析一次并落库）。"
        "{spec, source: card|world_book|initial_state, description?, fields:[{name,label,type,...}]}；"
        "fields 为空 = 该卡没有定义状态栏（界面显示「未定义」，也不向模型注入状态协议）。",
    )
    vn: dict[str, Any] | None = Field(
        default=None,
        description="★ 角色卡 VN 模式（立绘/背景）的**舞台数据**，由后端按当前状态算好："
        "{spec, background, sprite_url, expression, expressions, expression_field, "
        "position, show_name, name, sprite_scale, background_dim, warnings}；"
        "null = 这张卡没开 VN 模式（界面里不显示舞台开关）。",
    )
    translate: dict[str, Any] = Field(
        default_factory=dict,
        description="★ 自动翻译中间件面板要的状态："
        "{settings, modes, directions, lang_choices, max_chars, summary, "
        "cost_tokens, translated_messages, spent_tokens}",
    )
    message_count: int
    total_tokens: int
    last_active_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut] = Field(default_factory=list)
    messages_total: int = Field(default=0, description="消息总数（可能多于本次返回的条数）")
    messages_truncated: bool = Field(
        default=False, description="是否因为 limit 而只返回了最近的一部分消息"
    )
    prompt: PromptInfo | None = Field(
        default=None, description="按当前设定构建出的提示词预览（不调用模型）"
    )
    warnings: list[str] = Field(default_factory=list)


class SessionCreate(BaseModel):
    """建会话。"""

    character_card_id: int | None = Field(
        default=None,
        description="用哪张角色卡（可以是别人公开的卡）。**不传 = 纯聊天会话**："
        "不掺角色、不注入人设与状态协议，提示词走「通用助手」装配，"
        "适合快速验证 API 是否正常，或就是想直接聊两句",
    )
    llm_provider_id: int | None = Field(
        default=None,
        description="用哪个模型配置。不传则用该用户的默认模型；一个都没有时会话仍会创建，但要先配好模型才能对话",
    )
    title: str | None = Field(default=None, max_length=200, description="会话标题（默认取角色卡名）")
    greeting_index: int | None = Field(
        default=None,
        ge=0,
        description="用备选开场白里的第几条（不传则用默认开场白 greeting）",
    )


class SummarySettingsUpdate(BaseModel):
    """「记忆管理面板」的保存请求（PATCH 语义：只提交要改的字段）。

    ★ 每一项都能单独改 —— 用户可能只想把"每 8 轮"改成"每 12 轮"，
      不想顺手把开关也一起提交（PATCH 语义由 `model_fields_set` 保证）。
    """

    enabled: bool | None = Field(default=None, description="记忆总结总开关")
    auto: bool | None = Field(
        default=None, description="到点自动总结（会花一次模型调用）；关掉就只弹横幅提醒"
    )
    remind: bool | None = Field(default=None, description="到点弹横幅提醒用户总结（不花钱）")
    rounds: int | None = Field(default=None, description="每几轮触发一次（2~50）")
    mode: Literal["character", "plot", "table", "copy", "custom"] | None = Field(
        default=None, description="总结模式：折叠-角色优先 / 折叠-剧情优先 / 表格总结 / 照抄旧记忆 / 自定义"
    )
    prompt: str | None = Field(default=None, description="自定义总结提示词（mode=custom 时用）")
    use_preset_prompt: bool | None = Field(
        default=None, description="从守卫预设里读「记忆总结」块当提示词"
    )
    max_chars: int | None = Field(default=None, description="单份总结的字数上限")
    provider_id: int | None = Field(
        default=None, description="总结用哪个模型配置；null = 跟随会话模型"
    )
    content: str | None = Field(
        default=None, description="★ 非空时顺便把总结正文改成它（面板上的「编辑」）"
    )


class TranslateSettingsUpdate(BaseModel):
    """「翻译」面板的保存请求（PATCH 语义：只提交要改的字段）。

    ★ 与记忆面板同一套语义：不传的字段保持原值 ——
      用户改一下目标语言，不该顺手把总开关也一起改写。
    """

    enabled: bool | None = Field(default=None, description="翻译中间件总开关（默认关）")
    mode: Literal["off", "prompt", "middleware"] | None = Field(
        default=None,
        description="off=关闭 / prompt=只写提示词（0 token）/ middleware=真的调一次模型翻译",
    )
    direction: Literal["reply", "input", "both"] | None = Field(
        default=None,
        description="reply=只译回复 / input=只译我的输入 / both=双向",
    )
    target_lang: str | None = Field(
        default=None, description="翻译成哪种语言（默认简体中文；源语言自动识别）"
    )
    provider_id: int | None = Field(
        default=None, description="翻译用哪个模型配置；null = 跟随会话模型"
    )
    keep_original: bool | None = Field(default=None, description="保留原文（界面可切换）")


class MemoryAnchorsUpdate(BaseModel):
    """保存记忆锚点（整份替换）。"""

    anchors: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="锚点清单（最多 5 条 / 合计 2000 字；超限会被 400 拒绝，不会静默截断）",
    )


class SessionStateUpdate(BaseModel):
    """手动纠正会话状态（HP / 背包 / 位置 / 任务）。

    ★ 为什么需要它：模型一定会写错（HP 记错、把已经丢掉的钥匙又写回背包）。
      没有手动纠正入口时，用户只能忍着让错误一路滚下去 —— 或者重开会话。
      这里接受**完整状态**（前端给的是当前状态编辑后的 JSON），
      校验规则与模型自己输出时**完全一致**（都走 app/narrative/state.py）。
    """

    state: dict[str, Any] = Field(..., description="完整状态 JSON（字段见 app/narrative/state.py）")


class SessionUpdate(BaseModel):
    """改会话（PATCH 语义）。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "archived"] | None = Field(
        default=None, description="active 进行中 / archived 已归档"
    )
    character_card_id: int | None = Field(
        default=None, description="换一张角色卡（会立即影响之后的提示词）"
    )
    llm_provider_id: int | None = Field(default=None, description="换一个模型配置")
    prompt_preset_id: int | None = Field(
        default=None,
        description="换一个提示词预设。★ 传 null 表示**解绑**（回到全局默认预设 / 内置装配）",
    )


class MessageCreate(BaseModel):
    """发一条用户消息。"""

    content: str = Field(..., min_length=1, max_length=20000, description="用户说的话")

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        """先去掉首尾空白再校验长度。

        ★ 为什么必须这样？
          只写 min_length=1 的话，`"   "`（三个空格）能通过校验，
          于是错误会推迟到服务层才发现 —— 用户收到的是 400 而不是 422。
          参数格式问题就该在参数校验阶段（422）拦下，
          服务层的 400 留给"格式对但业务上不允许"的情况。
        """
        text = value.strip()
        if not text:
            raise ValueError("消息内容不能只有空白字符")
        return text


class MessageUpdate(BaseModel):
    """改写一条用户消息（编辑后重发）。"""

    content: str = Field(..., min_length=1, max_length=20000, description="改写后的内容")

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("消息内容不能只有空白字符")
        return text


class MessageEditResult(BaseModel):
    """编辑一条消息的结果。

    ★ 必须回报删了几条：编辑会连带删掉这条消息**之后**的全部内容，
      不告诉用户的话，他以为"我只是改了个错别字"，然后发现后面的剧情全没了。
    """

    message: MessageOut
    deleted_messages: int = Field(default=0, description="被一并删除的后继消息条数")
    message_count: int = 0
    total_tokens: int = 0


class RetractResult(BaseModel):
    """撤回最后一轮的结果。"""

    deleted_messages: int = Field(default=0, description="删除的消息条数")
    retracted_content: str = Field(default="", description="被撤回的那句话（前端可放回输入框）")
    message_count: int = 0
    total_tokens: int = 0


class SendResult(BaseModel):
    """非流式发送的返回。"""

    user_message: MessageOut
    assistant_message: MessageOut | None = Field(
        default=None, description="助手回复（角色卡没有开场白且模型未回复时为 null）"
    )
    usage: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(
        default_factory=list,
        description="适配层为满足协议要求所做的调整说明（例如 Anthropic 移除了 temperature）",
    )
    prompt: PromptInfo | None = None
    context: SessionContextInfo | None = None
    latency_ms: int = 0


