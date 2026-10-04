"""提示词构建：把「角色卡 + 世界书 + 对话历史」拼成一次请求的消息列表。

==================== 为什么单独成模块？====================
提示词是这个项目的"业务核心"：模型表现好不好，八成取决于这里怎么拼。
把它从接口层与适配层里独立出来，好处是：

  · 可以**单独测试**（给定角色卡与历史，断言拼出来的消息序列），
    不必启动 HTTP 服务，也不必联网调模型；
  · 换模型、换协议都不影响它（输出的是协议无关的 ChatMessage）；
  · 将来要加"世界书关键词触发""向量记忆召回"，只需要在这一个地方改。

==================== 拼装规则（与业界习惯对齐）====================
    1. 系统提示词
       · 角色卡自带 system_prompt 时**优先用它**（用户/作者写的就是最权威的）
       · 否则按人设字段自动拼装（名称/简介/性格/背景/说话风格/场景/对话示例）
    2. 世界书条目作为「世界设定」注入系统提示词
       （3.8 阶段由调用方把**已选中的条目**传进来；关键词触发检索是 3.9）
    3. 开场白 greeting 作为**第一条 assistant 消息**写入历史
       —— 故事总得有人先开口，否则用户面对一片空白不知道该说什么
    4. 对话历史按时间顺序排列
    5. post_history_instructions（尾注）追加在历史之后，并用括号标明
       "这是系统给你的指令，不是用户说的话"。

★ 第 5 条为什么要加括号声明？
  因为它以 user 消息的形式发送（这是各家实现尾注的通用做法）。
  如果不声明，模型可能把「请用更冷淡的语气说话」当成**用户在要求它改变人设**，
  从而回复一句"好的我会注意"——这属于典型的提示词注入式的自我破坏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.llm.schema import ChatMessage
from app.narrative import state as state_mod

#: 尾注前面加的那句身份声明（见模块文档第 5 条）
_POST_HISTORY_HEADER = "[系统指令 · 并非用户发言]"

#: 人设自动拼装时各字段的中文小标题
_PERSONA_SECTIONS: tuple[tuple[str, str], ...] = (
    ("description", "简介"),
    ("personality", "性格"),
    ("background", "背景"),
    ("speaking_style", "说话风格"),
    ("scenario", "当前场景"),
)

#: 纯聊天会话（没有角色卡）用的**通用助手**系统提示词。
#  ★ 为什么单独写一份、而不是复用内置守卫：
#    内置守卫是"你是虚构故事里的角色，不是 AI 助手"——那是为角色扮演写的；
#    纯聊天里这句话是完全错的。这一条分支的存在，也让"角色卡只是提示词装配的
#    一个可选层"这个架构论点变得可验证（三层全空 = 退化情形）。
#  ★ 同时它不套用任何提示词预设：纯聊天要当**异构适配层的验收台**（换 API/协议/参数
#    立刻看效果），掺进破甲/作者注那类预设会把体检结果搅浑。
PURE_CHAT_SYSTEM_PROMPT = """[通用助手]
你是一个乐于助人的通用 AI 助手，直接用简体中文回答用户的问题。

- 可以写代码、做分析、整理信息；代码一律用 Markdown 代码块并标注语言。
- 不确定就直说不确定，不要编造事实、链接或引用。
- 需要澄清时先问一句，不要自行假设关键前提。
- 不输出与问题无关的客套与免责声明。"""


@dataclass
class PromptPlan:
    """一次提示词构建的结果。

    它除了消息列表本身，还带回"是怎么拼出来的"这份元信息 ——
    界面上要如实告诉用户「本次用了角色卡自定义提示词」还是「引擎自动拼装」，
    以及世界书贡献了多少字，否则用户无法判断效果差异从哪来。
    """

    messages: list[ChatMessage] = field(default_factory=list)
    """拼好的消息列表（系统提示词 + 历史 + 尾注）。"""

    system_prompt: str = ""
    """本次真正使用的系统提示词全文（便于界面展示与排查）。"""

    system_prompt_source: str = "none"
    """系统提示词来源：card / assembled / world_book_only / memory_only / none / preset。

    · card            角色卡自带 system_prompt，直接采用
    · assembled       按人设字段自动拼装
    · world_book_only 没有角色卡，只有世界书设定
    · memory_only     只有长期记忆召回内容
    · none            什么都没有（例如角色卡已被删除且无世界书）
    · preset          ★ 由提示词预设装配（块的顺序与启停由用户决定）
    """

    world_book_entries: int = 0
    """注入了多少条世界书设定。"""

    recalled_memories: int = 0
    """召回了多少条长期记忆（3.9）。"""

    has_post_history_instructions: bool = False
    """是否追加了尾注指令。"""

    preset_id: int | None = None
    """本次使用的提示词预设ID（None = 走内置装配）。"""

    preset_name: str | None = None
    """预设名称（界面上要能说清"这次是谁在决定行为"）。"""

    preset_blocks_used: list[str] = field(default_factory=list)
    """真正参与装配的预设块（按顺序）。"""

    preset_blocks_skipped: list[str] = field(default_factory=list)
    """被跳过的块及原因（不支持 / 没内容 / 被禁用）。"""

    preset_blocks_disabled: list[str] = field(default_factory=list)
    """被显式禁用的块。★ 单独列出来：用户"关掉"了什么是很重要的信息。"""

    preset_unknown_macros: list[str] = field(default_factory=list)
    """预设里用到、本系统不认识的宏（会原样保留在提示词里）。"""

    depth_blocks: list[int] = field(default_factory=list)
    """深度注入的块数，按深度列出（例如 [4, 4, 2]）。"""

    depth_messages: list[tuple[int, Any]] = field(default_factory=list)
    """要插进对话历史的消息：[(深度, ChatMessage), …]。

    ★ 为什么不在拼装阶段就插进去？
      因为上下文裁剪器会从**最早**的消息开始丢，深度块如果不参与裁剪计数，
      就可能挤爆输入预算；如果参与了，它又会被当成"旧消息"丢掉。
      正确顺序是：**先裁剪，再按深度插回**（见 context_manager.prepare_context）。
    """

    warnings: list[str] = field(default_factory=list)
    """构建过程中的提醒（例如角色卡已丢失，人设无法注入）。"""

    retrieval: dict[str, Any] = field(default_factory=dict)
    """★ 混合检索的计数与逐条明细（关键词/语义两路的分、名次、去留原因）。

    由 engine.build_session_prompt 填（检索发生在装配之前）。
    为什么不在这里做检索：这个函数是纯拼装，检索依赖数据库 / 向量库 / 会话 id。
    """

    summary: dict[str, Any] = field(default_factory=dict)
    """★ 本轮剧情总结的结果（`summary.MergeOutcome.to_dict()`；没合并就是空 dict）。

    由 `engine.prepare_turn` 填（总结要调模型、且要在装配之前）。
    带上它是为了让**降级可见**：总结调用失败时会退回本地压缩，
    这时候界面/日志必须说清"这份前情提要是本地压的，不是模型写的"。
    """

    summary_state: dict[str, Any] = field(default_factory=dict)
    """★ 「该总结了」的提醒状态（不花钱）：{due, pending, rounds, from_round, to_round,
    auto, remind, mode, cost_tokens, content}。前端据此弹横幅等用户点「立即总结」。"""

    retrieval_summary: str = ""
    """混合检索的一行"说人话"摘要（进「查看提示词」预览）。"""

    retrieval_items: list[str] = field(default_factory=list)
    """每条候选一行（来源 / 两路名次 / 最终分 / 为什么留下或丢掉）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_prompt": self.system_prompt,
            "system_prompt_source": self.system_prompt_source,
            "world_book_entries": self.world_book_entries,
            "recalled_memories": self.recalled_memories,
            "has_post_history_instructions": self.has_post_history_instructions,
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "preset_blocks_used": list(self.preset_blocks_used),
            "preset_blocks_skipped": list(self.preset_blocks_skipped),
            "preset_blocks_disabled": list(self.preset_blocks_disabled),
            "preset_unknown_macros": list(self.preset_unknown_macros),
            "depth_blocks": list(self.depth_blocks),
            "warnings": list(self.warnings),
            "retrieval": dict(self.retrieval),
            "retrieval_summary": self.retrieval_summary,
            "retrieval_items": list(self.retrieval_items),
            "summary": dict(self.summary),
            "summary_state": dict(self.summary_state),
        }


# ==================================================================
#  人设拼装
# ==================================================================
def _section(title: str, body: str | None) -> str:
    """拼一个小节；内容为空则返回空串（避免出现「性格：（空）」这种噪音）。"""
    text = (body or "").strip()
    if not text:
        return ""
    return f"## {title}\n{text}"


def assemble_persona_prompt(
    card: Any, *, world_book: Any = None, world_book_entries: list | None = None
) -> str:
    """按角色卡的人设字段自动拼装系统提示词。

    参数用的是「鸭子类型」而不是具体的 ORM 类：
    这样单元测试可以直接传一个简单的假对象，不必真的建库。

    ★ world_book_entries 必须一路传下去：3.9 的关键词触发只注入命中的条目，
      如果这里漏传，就会退化成"把整本世界书都塞进去" —— 而且**不会报错**，
      只是上下文悄悄变长、模型被无关设定干扰。（这个 bug 真实出现过一次。）
    """
    name = (getattr(card, "name", None) or "角色").strip()

    parts: list[str] = [
        f"你正在扮演角色「{name}」。请始终保持这个角色的身份、性格与说话方式，"
        f"不要以 AI 助手或旁白者的身份发言，也不要提及自己是模型。"
    ]

    for field_name, title in _PERSONA_SECTIONS:
        block = _section(title, getattr(card, field_name, None))
        if block:
            parts.append(block)

    example = (getattr(card, "example_dialogue", None) or "").strip()
    if example:
        # 对话示例是 few-shot 范例：比单纯描述性格更能稳定输出风格
        parts.append(
            "## 对话示例（仅供模仿语气与格式，不要照抄内容）\n" + example
        )

    world_block = render_world_book(world_book, world_book_entries)
    if world_block:
        parts.append(world_block)

    return "\n\n".join(parts)


def render_world_book(world_book: Any, entries: list | None = None) -> str:
    """把世界书条目渲染成一段「世界设定」文本。

    entries 为 None 时使用世界书自身的全部启用条目。
    ★ 3.9 会把「按关键词筛过的条目」传进来，本函数不用改。
    """
    if world_book is None:
        return ""

    if entries is None:
        raw = list(getattr(world_book, "entries", None) or [])
        entries = [e for e in raw if isinstance(e, dict) and e.get("enabled", True) is not False]

    lines: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"- {content}")

    if not lines:
        return ""

    book_name = (getattr(world_book, "name", None) or "").strip()
    title = f"## 世界设定{f'（{book_name}）' if book_name else ''}"
    return title + "\n" + "\n".join(lines)


def resolve_system_prompt(
    card: Any, *, world_book: Any = None, world_book_entries: list | None = None
) -> tuple[str, str]:
    """决定本次用哪份系统提示词。

    返回 (提示词全文, 来源标记)。来源标记的取值见 PromptPlan.system_prompt_source。
    """
    custom = (getattr(card, "system_prompt", None) or "").strip() if card else ""

    if custom:
        # ★ 角色卡自带提示词时优先用它。但世界书是**世界观事实**，
        #   不属于"人设写法"，作者换了提示词也仍然需要它，所以照样追加。
        world_block = render_world_book(world_book, world_book_entries)
        if world_block:
            return f"{custom}\n\n{world_block}", "card"
        return custom, "card"

    if card is not None:
        return (
            assemble_persona_prompt(
                card, world_book=world_book, world_book_entries=world_book_entries
            ),
            "assembled",
        )

    world_block = render_world_book(world_book, world_book_entries)
    if world_block:
        return world_block, "world_book_only"
    return "", "none"


# ==================================================================
#  消息序列
# ==================================================================
def build_messages(
    *,
    system_prompt: str,
    history: list[Any],
    post_history_instructions: str | None = None,
) -> list[ChatMessage]:
    """把系统提示词 + 对话历史 + 尾注拼成最终的消息列表。

    history 里每一项只要有 role / content 两个属性即可（ORM 行或 ChatMessage 都行）。
    """
    messages: list[ChatMessage] = []

    if system_prompt.strip():
        messages.append(ChatMessage.system(system_prompt.strip()))

    for item in history:
        content = str(getattr(item, "content", "") or "")
        if not content.strip():
            # 空消息会被部分协议直接拒绝（Anthropic 明文规定），这里统一跳过
            continue
        role = str(getattr(item, "role", "user") or "user").lower()
        if role == "system":
            # 历史里的 system 消息不单独成条，合并进系统提示词位置（见模块文档）
            continue
        messages.append(ChatMessage(role=role, content=content))

    instructions = (post_history_instructions or "").strip()
    if instructions:
        messages.append(
            ChatMessage.user(f"{_POST_HISTORY_HEADER}\n{instructions}")
        )

    return messages


def build_prompt(
    *,
    card: Any,
    history: list[Any],
    world_book: Any = None,
    world_book_entries: list | None = None,
    recalled_memories: str | None = None,
    state_block: str | None = None,
    state_schema: Any = None,
    dice_block: str | None = None,
    translate_hint: str | None = None,
    anchors_block: str | None = None,
    preset: Any = None,
    preset_config: Any = None,
    user_name: str = "",
    pure_chat: bool = False,
) -> PromptPlan:
    """一次完整的提示词构建（系统提示词 → 历史 → 尾注）。

    参数：
        card       角色卡（可为 None —— 卡被删掉后会话仍在，不能因此炸掉）
        history    会话的消息历史（按时间升序）
        world_book 卡片关联的世界书（可为 None）
        world_book_entries 只注入这些条目（None = 全部启用的）
        recalled_memories  长期记忆召回的文本（3.9），追在系统提示词之后
        dice_block  ★ 本轮骰点（骰子插件）：**客观事实**，紧跟在"当前状态"之后。
                    它是数据不是指令，所以不放在提示词末尾（末尾留给必须照做的规则）。
        translate_hint ★ 输出语言要求（翻译中间件的 prompt 模式）：加在**最末尾**，
                    与状态输出契约同一个理由 —— 机械指令越靠后越有效。
        preset     预设 ORM 行（None = 走内置装配，行为与本功能上线前完全一致）
        preset_config 预设解析后的配置（缺省时从 preset 现场解析）
        user_name  用户名，供预设里的 `{{user}}` 宏使用
        pure_chat  ★ 纯聊天会话（无角色，用户主动选的）：走「通用助手」装配，
                   不装人设、不套预设与守卫、不注入状态协议。
                   判据由调用方从 `narrative_sessions.kind` 取（见 sessions.is_pure_chat）——
                   不能只看 card 是不是 None，因为"角色卡被删"也会是 None。

    ★ 兼容性硬约定：`preset is None` 且 `pure_chat=False` 时**必须**产出于加预设之前
      **逐字节相同**的消息序列。现有 650+ 条测试与用户既有会话都依赖这一点，
      所以下面"有预设"和"没预设"是两条明确分开的路径，不做隐式混合。
    """
    warnings: list[str] = []
    if card is None and pure_chat:
        # ---------------- 纯聊天：通用助手分支（与角色扮演完全分开）----------------
        # ★ 这一支刻意**只做三件事**：通用助手提示词 + 历史 + 尾注（空）。
        #   不装人设、不套预设与守卫、不注入状态协议、不插世界书/回忆。
        #   理由见 PURE_CHAT_SYSTEM_PROMPT 上方的说明。
        if preset is not None:
            warnings.append(
                "这是纯聊天会话（没有角色卡），本次**没有**使用你绑定的提示词预设与内置守卫 —— "
                "纯聊天走的是通用助手提示词。"
            )
        system_prompt = PURE_CHAT_SYSTEM_PROMPT
        # ★ 纯聊天也认骰子：用户既然启用了骰子插件，`/r 1d100` 的结果就该让模型看到
        #   （唯一一处纯聊天会被追加的内容 —— 它是插件带来的客观事实，不是人设/预设）
        pure_dice = (dice_block or "").strip()
        if pure_dice:
            system_prompt = f"{system_prompt}\n\n{pure_dice}"
        messages = build_messages(
            system_prompt=system_prompt, history=history, post_history_instructions=None
        )
        return PromptPlan(
            messages=messages,
            system_prompt=system_prompt,
            system_prompt_source="pure_chat",
            world_book_entries=0,
            recalled_memories=0,
            has_post_history_instructions=False,
            warnings=warnings,
        )

    if card is None:
        # 叙事会话但角色卡被删了（SET NULL）：照旧走内置装配 + 如实提示
        warnings.append(
            "这个会话关联的角色卡已被删除，本次没有注入人设设定，"
            "模型只会看到对话历史。建议在会话设置里重新选择一张角色卡。"
        )

    builtin_main, source = resolve_system_prompt(
        card, world_book=world_book, world_book_entries=world_book_entries
    )
    preset_meta: dict[str, Any] = {}
    depth_messages: list[Any] = []

    # ★ 判据是"有没有**预设行**"，不是"有没有配置"：
    #   没绑任何预设的用户，preset 为 None 但 preset_config 里装着内置守卫规则
    #   （守卫是"追加"而不是"替代"）。那种情况必须**照旧走内置装配**，
    #   否则角色人设、世界书、记忆全都会被挤掉（改这里时真的踩过：
    #   系统提示词只剩守卫规则，角色卡等于失效了）。
    #   守卫正文怎么追加见下面「内置装配」那一段。
    if preset is not None:
        builtin_main, preset_meta, depth_messages, system_prompt = _apply_preset(
            preset=preset,
            preset_config=preset_config,
            card=card,
            history=history,
            user_name=user_name,
            world_book=world_book,
            world_book_entries=world_book_entries,
            recalled_memories=recalled_memories,
            state_block=state_block,
            state_schema=state_schema,
            anchors_block=anchors_block,
            builtin_main=builtin_main,
        )
        source = "preset"
        return _finish_with_preset(
            system_prompt=system_prompt,
            history=history,
            depth_messages=depth_messages,
            world_book=world_book,
            world_book_entries=world_book_entries,
            recalled_memories=recalled_memories,
            preset_meta=preset_meta,
            warnings=warnings,
        )

    # ---------------- 内置装配（不绑定预设时的老路径）----------------
    # ★ 记忆锚点紧跟世界设定：它和世界书一样是"作者/用户写下的硬设定"（固定注入、
    #   永不折叠），优先级高于"回忆"（模型自己记的、可能召回不到）。
    anchors_text = (anchors_block or "").strip()
    if anchors_text:
        builtin_main = (
            f"{builtin_main}\n\n{anchors_text}" if builtin_main.strip() else anchors_text
        )
        if source == "none":
            source = "memory_only"

    # ★ 回忆放在人设之后、对话历史之前：
    #   它的定位是"更早的历史"，不是"人设的一部分"。
    #   放在最前面会让人设被大段回忆挤到后面，模型更容易跟着回忆跑偏。
    memory_block = (recalled_memories or "").strip()
    if memory_block:
        builtin_main = (
            f"{builtin_main}\n\n{memory_block}" if builtin_main.strip() else memory_block
        )
        if source == "none":
            source = "memory_only"

    # 当前状态也要排在守卫之前（它是最"贴近此刻"的客观事实）
    state_text = (state_block or "").strip()
    if state_text:
        builtin_main = (
            f"{builtin_main}\n\n{state_text}" if builtin_main.strip() else state_text
        )
        if source == "none":
            source = "memory_only"

    # ★ 本轮骰点紧跟在"当前状态"之后：它同样是最贴近此刻的客观事实，
    #   而且**必须**在系统守卫之前 —— 守卫里"不要替玩家决定行动"那类要求
    #   不能盖过"这个点数是既成事实"（骰点由系统掷出，模型只负责叙述）。
    dice_text = (dice_block or "").strip()
    if dice_text:
        builtin_main = (
            f"{builtin_main}\n\n{dice_text}" if builtin_main.strip() else dice_text
        )
        if source == "none":
            source = "memory_only"

    post = (getattr(card, "post_history_instructions", None) or "") if card else ""

    # ---------------- 内置守卫规则（追加在系统提示词的**最后**）----------------
    # ★ 为什么不是"有守卫就走预设路径"：
    #   守卫预设里**没有**角色人设/世界书/记忆那些标记块，
    #   用它替代内置装配会让系统提示词只剩几段规则 —— 角色卡直接失效。
    #   所以两者的关系是**叠加**：人设照旧按内置装配注入，
    #   守卫正文接在最后（越靠后的指令权重越高，这也是"硬性规则"该在的位置）。
    if preset is None and preset_config is not None:
        guard_text = _guard_text_only(
            preset_config,
            card=card,
            history=history,
            user_name=user_name,
            builtin_main=builtin_main,
        )
        if guard_text:
            builtin_main = (
                f"{builtin_main}\n\n{guard_text}" if builtin_main.strip() else guard_text
            )
            if source == "none":
                source = "preset"

    # ---------------- 状态输出契约（追加在**最末尾**）----------------
    # ★ 越靠后的指令权重越高，而"每轮都要输出状态块"是纯机械的格式要求，
    #   最容易被前面那些"最高优先级"的守卫盖过去（真实模型实测漏掉过）。
    #   所以协议正文在守卫之前，**命令式的契约必须在守卫之后**。
    if state_text:
        contract = state_mod.render_contract(state_schema)
        builtin_main = (
            f"{builtin_main}\n\n{contract}" if builtin_main.strip() else contract
        )
        if source == "none":
            source = "preset"

    # ---------------- 输出语言要求（翻译中间件的 prompt 模式）----------------
    # ★ 放在**倒数第一**：它是"照做就行"的机械指令，越靠后越有效
    #   （与状态输出契约同一个理由 —— 夹在中间会被后面那些更"凶"的守卫盖过去）。
    hint = (translate_hint or "").strip()
    if hint:
        builtin_main = (
            f"{builtin_main}\n\n{hint}" if builtin_main.strip() else hint
        )
        if source == "none":
            source = "preset"

    messages = build_messages(
        system_prompt=builtin_main, history=history, post_history_instructions=post
    )

    return PromptPlan(
        messages=messages,
        system_prompt=builtin_main,
        system_prompt_source=source,
        world_book_entries=_count_entries(world_book, world_book_entries),
        recalled_memories=_count_memories(memory_block),
        has_post_history_instructions=bool(post.strip()),
        warnings=warnings,
    )


def _guard_text_only(
    preset_config: Any,
    *,
    card: Any,
    history: list[Any],
    user_name: str,
    builtin_main: str,
) -> str:
    """只把预设里的**规则正文**渲染成一段文本（不给标记块喂内容）。

    ★ 用途：用户没绑任何预设时，`preset_config` 里装的只有内置守卫块。
      这里刻意不传世界书 / 记忆（`world_block=""`）：那些内容由内置装配
      按自己的口径注入，从这里再注入一次就成了"世界书重复注入"。
    """
    from app.narrative import presets as presets_mod

    req = presets_mod.RenderRequest(
        card=card,
        user_name=user_name,
        history=history,
        world_block="",
        world_entries=0,
        memory_block="",
        builtin_main=builtin_main,
        post_history="",
    )
    result = presets_mod.render_blocks(preset_config, req)
    return "\n\n".join(m.content for m in result.head() if m.content.strip())


def _apply_preset(
    *,
    preset: Any,
    preset_config: Any,
    card: Any,
    history: list[Any],
    user_name: str,
    world_book: Any,
    world_book_entries: list | None,
    recalled_memories: str | None,
    builtin_main: str,
    state_block: str | None = None,
    state_schema: Any = None,
    anchors_block: str | None = None,
) -> tuple[str, dict[str, Any], list[Any], str]:
    """按预设装配，返回 (内置主提示, 元信息, 深度消息, 系统提示词)。

    ★ 这里刻意**不做任何猜测**：块顺序、启停、支持与否全部来自预设本身，
      我们只负责把"内部内容"塞进标记块的位置。
    """
    from app.narrative import presets as presets_mod

    config = preset_config or presets_mod.from_config(getattr(preset, "config", None))
    # 世界书正文的渲染口径与内置装配**完全一致**（同一个函数），
    # 否则"换成预设之后世界书好像不太一样"这种问题根本无从排查。
    world_block = render_world_book(world_book, world_book_entries)

    req = presets_mod.RenderRequest(
        card=card,
        user_name=user_name,
        history=history,
        world_block=world_block,
        world_entries=_count_entries(world_book, world_book_entries),
        memory_block=(recalled_memories or "").strip(),
        builtin_main=builtin_main,
        post_history=(getattr(card, "post_history_instructions", None) or "") if card else "",
    )
    result = presets_mod.render_blocks(config, req)

    # ★ 预设**完全不管世界书**时的兜底（真实事故）：当"当前激活预设"就是内置守卫预设
    #   （里面只有 hneGuard* 三块）时，命中的世界书条目会**静默丢掉** ——
    #   界面还显示"世界书命中 1 条"，模型却一个字都没看到。
    #   判据是"预设里压根没有世界书标记"：这种情况我们替他补上，并如实说明。
    #   若标记存在但被用户停用，那是他明确的意图 —— 不补，只提示。
    _world_ids = {presets_mod.BLOCK_WORLD_BEFORE, presets_mod.BLOCK_WORLD_AFTER}
    _has_world_block = any(b.identifier in _world_ids for b in config.blocks)
    _world_disabled = any(
        b.identifier in _world_ids and not b.enabled for b in config.blocks
    )
    auto_world = ""
    if req.world_block.strip() and not _has_world_block:
        auto_world = req.world_block.strip()
        result.warnings.append(
            "这个预设里没有世界书标记（worldInfoBefore / worldInfoAfter），"
            "命中的世界书条目已自动追加进系统提示词，否则模型看不到你写的设定。"
        )
    elif req.world_block.strip() and _world_disabled:
        result.warnings.append(
            "预设里的世界书标记被停用了，本次没有注入世界书（这是你的预设设置）。"
        )

    # ★ 用 result.messages（而不是 result.head()）：head() 只返回 ChatMessage，
    #   而下面要按 depth 区分"系统提示词 / 深度注入"。
    #   （注意：进系统提示词的块在这里已经被合并成**一条**消息了，块 identifier 已丢失，
    #    所以定位守卫只能按正文标记，见下面的 GUARD_IDENTITY_MARKER。）
    texts = [
        str(getattr(item.message, "content", "") or "")
        for item in result.messages
        if item.depth is None
    ]
    texts = [t for t in texts if t.strip()]

    memory_block = req.memory_block
    # 排在「内置守卫块」之前的额外内容：世界书兜底段 → 长期记忆 → 当前状态
    # （越靠后越"贴近此刻"，而守卫永远在最后、权重最高）
    # 记忆锚点排在世界设定之后、回忆之前（与内置装配的顺序**保持一致**）
    extra_parts = [
        part
        for part in (
            auto_world,
            (anchors_block or "").strip(),
            memory_block,
            (state_block or "").strip(),
        )
        if part
    ]
    if extra_parts:
        extra = "\n\n".join(extra_parts)
        # ★ 为什么必须排在守卫之前：守卫是身份认知 / 不跑偏 / 长度这类
        #   "最高优先级"规则，而回忆是模型自己过去说的话 —— 里面可能残留
        #   "我是 AI 助手"这种出戏台词。排在守卫后面等于当场给护栏摆一排反例
        #   （真实事故：会话 2328，护栏写着"绝不承认自己是 AI"，
        #   紧接着的回忆是 4 条"我是这个对话中的 AI 助手"）。
        #   以前这里是"追加在整条系统提示词末尾"，正好踩中这一点。
        marker = presets_mod.GUARD_IDENTITY_MARKER
        pos = next((i for i, t in enumerate(texts) if marker in t), None)
        if pos is None:
            texts.append(extra)
        else:
            before, _, after = texts[pos].partition(marker)
            texts[pos] = (
                f"{before.rstrip()}\n\n{extra}\n\n{marker}{after}"
                if before.strip()
                else f"{extra}\n\n{marker}{after}"
            )
    system_prompt = "\n\n".join(texts)

    # ★ 状态输出契约追加在**整条系统提示词的最后**（理由见 render_contract 的说明）。
    #   预设路径同样要加：预设是用户自己排的块，契约不属于预设，不能被它挤掉。
    if (state_block or "").strip():
        system_prompt = f"{system_prompt}\n\n{state_mod.render_contract(state_schema)}"

    meta = {
        "preset_id": getattr(preset, "id", None),
        "preset_name": getattr(preset, "name", None),
        "preset_blocks_used": list(result.used),
        "preset_blocks_skipped": list(result.skipped),
        "preset_blocks_disabled": [
            b.identifier for b in config.blocks if not b.enabled
        ],
        "preset_unknown_macros": list(result.unknown_macros),
        "depth_blocks": [m.depth for m in result.messages if m.depth is not None],
        "preset_warnings": list(result.warnings),
    }
    depth_messages = [m for m in result.messages if m.depth is not None]
    return builtin_main, meta, depth_messages, system_prompt


def _finish_with_preset(
    *,
    system_prompt: str,
    history: list[Any],
    depth_messages: list[Any],
    world_book: Any,
    world_book_entries: list | None,
    recalled_memories: str | None,
    preset_meta: dict[str, Any],
    warnings: list[str],
) -> PromptPlan:
    """把预设装配的结果包装成 PromptPlan。

    ★ 尾注（post_history_instructions）**不再单独追加**：
      它在预设里对应 `jailbreak` 标记块，位置由用户决定。
      再自动追加一次就成了"用户明明把尾注关掉了，却还是发了出去"。
    """
    post = ""
    messages = build_messages(
        system_prompt=system_prompt, history=history, post_history_instructions=post
    )
    preset_warnings = list(preset_meta.pop("preset_warnings", []) or [])
    # ★ depth_blocks 由 meta 带进来（`**preset_meta`），这里不能再显式传一次 ——
    #   否则就是 "got multiple values for keyword argument"。
    depth_depths = [int(item.depth) for item in depth_messages]
    assert preset_meta.get("depth_blocks") == depth_depths, (
        "depth_blocks 与 depth_messages 必须一致，否则界面上的数字会和实际发出的请求对不上"
    )

    return PromptPlan(
        messages=messages,
        system_prompt=system_prompt,
        system_prompt_source="preset",
        world_book_entries=_count_entries(world_book, world_book_entries),
        recalled_memories=_count_memories((recalled_memories or "").strip()),
        has_post_history_instructions=False,
        depth_messages=[(int(item.depth), item.message) for item in depth_messages],
        warnings=warnings + preset_warnings,
        **preset_meta,
    )


def _count_entries(world_book: Any, entries: list | None) -> int:
    if world_book is None:
        return 0
    if entries is None:
        entries = [
            e
            for e in (getattr(world_book, "entries", None) or [])
            if isinstance(e, dict) and e.get("enabled", True) is not False
        ]
    return sum(1 for e in entries if isinstance(e, dict) and str(e.get("content") or "").strip())


def _count_memories(block: str) -> int:
    """数一数回忆小节里有几条（每条以 "- " 开头）。"""
    return sum(1 for line in block.splitlines() if line.strip().startswith("- "))
