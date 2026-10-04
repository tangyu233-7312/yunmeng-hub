"""叙事会话模型（一次完整的「故事」）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class NarrativeSession(Base, TimestampMixin):
    """一次叙事会话 = 一个角色卡 + 一个模型配置 + 一串消息。

    可以理解为「一个存档」：用户选择用哪张角色卡、调用哪个模型，然后开始一段故事。
    """

    __tablename__ = "narrative_sessions"
    __table_args__ = {"comment": "叙事会话表"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="会话ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="所属用户ID",
    )

    # ---------------- 关联的角色与模型 ----------------
    character_card_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # ★ 用 SET NULL 而不是 CASCADE —— 这家改动过，值得说明原因：
        #   最初是 CASCADE（删卡就删故事），但那样太粗暴：
        #   用户可能只是想清理角色卡库，却连辛苦玩了几十轮的故事一起没了。
        #   现在改由接口的勾选项决定（DELETE /character-cards/{id}?delete_sessions=...），
        #   默认走「保留故事、只解除关联」，需要一并删除时必须用户明确选择。
        ForeignKey("character_cards.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        comment="使用的角色卡ID（角色卡被删除后置空，会话本身保留）",
    )
    llm_provider_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # ★ 这里用 SET NULL 而不是 CASCADE：
        #   删除一个模型配置，不应该把用户辛苦写的故事一起删掉，只是「忘了用哪个模型跑的」。
        ForeignKey("llm_providers.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        comment="使用的模型配置ID（配置被删除后置空，会话本身保留）",
    )
    prompt_preset_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # ★ 同样是 SET NULL：删掉一套预设不该把故事一起删掉，
        #   只是这个会话回到「用全局默认预设」。
        ForeignKey("prompt_presets.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        comment="本会话使用的提示词预设ID（空 = 用全局默认预设 / 内置装配）",
    )
    #: 会话类型：story = 有角色卡的叙事会话；chat = 纯聊天（无角色）。
    #  ★ 为什么需要单独一列，而不是"看 character_card_id 是不是空"：
    #    角色卡被删除时 character_card_id 会被置空（SET NULL），
    #    那种情况是"卡没了的故事会话"，要提示用户"人设丢了"；
    #    而纯聊天是用户**主动选的**。两者长得一样，必须显式区分，
    #    否则要么把删卡提示丢掉（对用户撒谎），要么对纯聊天喊"你的卡被删了"。
    kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'story'"),
        comment="会话类型：story（叙事）/ chat（纯聊天，无角色）",
    )

    # ---------------- 会话内容 ----------------
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="会话标题（默认取角色卡名+时间，可重命名）"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default="active",
        nullable=False,
        # 取值：active（进行中）/ archived（已归档）
        comment="会话状态：active / archived",
    )

    # ---------------- 长期记忆（第 3.5 步使用）----------------
    # 当对话越来越长、超出模型上下文窗口时，不能无限往提示词里塞历史消息。
    # 常见做法是：把较早的消息压缩成一段「剧情摘要」存下来，只有它 + 最近若干轮消息进提示词。
    # ★ 第八轮起改成**分层合并**（见 app/narrative/summary.py）：
    #   每积累 SUMMARY_BLOCK_ROUNDS 轮 → 把「旧总结 + 这一块对话」合并成**一份新总结**
    #   （标记它覆盖第几轮到第几轮），**旧总结被替换而不是追加** —— 否则摘要越叠越长、
    #   同一段剧情被重复扫描，白占 token。
    rolling_summary: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="剧情滚动总结（前情提要；覆盖区间见 summary_*_round）"
    )
    # 总结覆盖到哪条消息为止，避免重复压缩
    summarized_until_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="总结已覆盖到的最后一条消息ID"
    )
    summary_from_round: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="这份总结从第几轮开始覆盖（轮 = 一问一答）"
    )
    summary_to_round: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="这份总结覆盖到第几轮（也是下次从哪继续的依据）"
    )
    # ★ 第八轮：总结改成**用户可控**（开关 / 自动或提醒 / 每几轮 / 五种模式 / 自定义提示词 /
    #   字数上限 / 单独指定总结模型）。设置存会话级 JSON —— 每个故事的偏好可以不一样。
    summary_settings_json: Mapped[str | None] = mapped_column(
        MEDIUMTEXT,
        nullable=True,
        comment="记忆总结设置 JSON（见 app/narrative/summary.py 的 default_settings）",
    )
    #: 历史版本（供面板上的「恢复上一次」）：最近 HISTORY_LIMIT 个版本
    summary_history_json: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="总结的历史版本 JSON 数组（恢复上一次用）"
    )
    # ★ 记忆锚点：用户手写的"永远要记住"的硬设定（最多 5 条 / 2000 字）。
    #   它跟世界书一样是作者意志，所以**整条固定注入**，既不参与召回、也不会被总结折叠。
    memory_anchors_json: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="记忆锚点 JSON 数组（用户手写，固定注入、永不折叠）"
    )
    # ★ 第十五轮：自动翻译中间件（跨语言对话）。设置同样存会话级 JSON：
    #   同一张英文卡，用户可能只想"看懂回复"，也可能想"连自己的输入一起译过去"。
    translate_settings_json: Mapped[str | None] = mapped_column(
        MEDIUMTEXT,
        nullable=True,
        comment="翻译中间件设置 JSON（见 app/narrative/translate.py 的 DEFAULT_SETTINGS）",
    )

    # ---------------- 结构化状态（字段由角色卡 / 世界书定义）----------------
    # ★ 为什么状态要落库、而不是只让模型写在小作文里：
    #   模型下一轮很可能忘了或写飘（HP 从 42 变 88、背包里的钥匙突然没了），
    #   落库 + 每轮回注 + 解析时校验，才能让状态在长对话里保持自洽。
    #   解析与校验见 app/narrative/state.py。
    state_json: Mapped[str | None] = mapped_column(
        MEDIUMTEXT,
        nullable=True,
        comment="结构化状态 JSON（字段由 state_schema_json 定义，已校验）",
    )

    # ---------------- 状态栏格式（★ 每张卡不一样）----------------
    # ★ 建会话时按「卡 extensions.hne.state_schema > 世界书 [状态栏] 条目 >
    #   卡 initial_state 的顶层键 > 空」解析一次并落库（见 state_schema.resolve_schema）。
    #   为什么要落库：提示词与界面状态栏必须永远一致 ——
    #   若每轮现算，用户改了卡/世界书之后，老会话的历史状态会与显示格式对不上。
    # ★ NULL（迁移前的旧会话）= 没有记录：由 state.effective_schema 走兼容分支，
    #   已有状态的旧会话仍按旧五字段渲染，避免历史状态栏突然变空白。
    state_schema_json: Mapped[str | None] = mapped_column(
        MEDIUMTEXT,
        nullable=True,
        comment="状态栏字段定义 JSON（含空 schema = 该卡未定义状态栏）",
    )

    # ---------------- 统计信息（前端列表页可直接展示）----------------
    message_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False, comment="消息总数"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
        comment="累计消耗 token 数（用于展示用量）",
    )
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最后活跃时间（用于会话列表排序）"
    )

    # ---------------- 关系 ----------------
    user: Mapped["User"] = relationship(back_populates="sessions")  # noqa: F821
    character_card: Mapped["CharacterCard | None"] = relationship(  # noqa: F821
        back_populates="sessions"
    )
    provider: Mapped["LLMProvider | None"] = relationship(  # noqa: F821
        back_populates="sessions"
    )
    prompt_preset: Mapped["PromptPreset | None"] = relationship()  # noqa: F821
    messages: Mapped[list["Message"]] = relationship(  # noqa: F821
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        # 默认按 id 升序排列，等价于按时间顺序（自增主键天然有序），
        # 这样 session.messages 拿到的就是正确的对话顺序
        order_by="Message.id",
    )

    def __repr__(self) -> str:
        return (
            f"<NarrativeSession id={self.id} title={self.title!r} "
            f"status={self.status} messages={self.message_count}>"
        )
