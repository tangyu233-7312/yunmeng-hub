"""消息模型（会话里的每一条对话）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Message(Base):
    """一条对话消息。

    ★ 注意：这张表**刻意不继承 TimestampMixin**。
      因为消息一旦写下就不会再修改，只留 created_at 即可；
      多一个永远用不到的 updated_at 是浪费存储和心智负担。
    这个细节体现了「按数据特性设计表」而不是机械套模板。
    """

    __tablename__ = "messages"
    __table_args__ = (
        # 复合索引：对话历史查询几乎永远是
        #   SELECT * FROM messages WHERE session_id = ? ORDER BY id
        # 建立 (session_id, id) 复合索引后，MySQL 能直接按顺序取数据，
        # 既快速过滤又免去额外排序，是最贴合本场景的索引设计。
        Index("ix_messages_session_id_id", "session_id", "id"),
        {"comment": "消息表"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="消息ID"
    )
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("narrative_sessions.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话ID",
    )

    # ---------------- 消息内容 ----------------
    # 角色取值（与 OpenAI 协议对齐）：
    #   system    系统提示词，通常不落库为普通消息，但保留该取值以兼容
    #   user      用户输入
    #   assistant 模型回复
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="角色：system / user / assistant"
    )
    content: Mapped[str] = mapped_column(
        # 单条消息可能很长（模型一次输出几千字），TEXT 的 64KB 上限偏紧，故用 MEDIUMTEXT
        MEDIUMTEXT,
        nullable=False,
        comment="消息正文",
    )

    # ---------------- 元数据（便于统计与成本分析）----------------
    token_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="本条消息的 token 数"
    )
    model_name: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="生成该消息所用模型名（用户消息为 NULL）"
    )
    latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="模型响应耗时（毫秒），用于性能分析"
    )
    # ★ 状态遥测（第十一轮）：这一轮模型**自称**的状态与**校验后落库**的状态差在哪。
    #   为什么要落库：`<state>` 块在保存前就被剥掉了，事后无法从正文判断
    #   "模型这一轮到底输出了没有"（第七轮的 --from-db 漏输出率就因此恒为 100%，是错的）。
    #   形状：{"required": true, "had_block": true, "counts": {"clamped":1,...},
    #          "deviation": 0.12, "fields_reported": 4}
    state_meta_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="状态遥测 JSON（漂移曲线的原始数据）"
    )

    # ★ 骰点（第十二轮）：这条消息里掷出的骰子（用户 `/r 1d100` 或模型的 `<roll>`）。
    #   为什么要落库而不是"用的时候现掷"：骰点一旦掷出就不能重掷（重掷等于抽卡）。
    #   落库之后，刷新界面、「查看提示词」预览、重新生成读到的都是同一组数字。
    #   形状：[{"source":"user","expression":"1d100","total":47,"faces":[47],
    #          "terms":[{...}],"text":"1d100 = 47"}, …]
    rolls_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="骰点 JSON 数组（骰子插件：点数与骰面）"
    )

    # ★ 翻译中间件（第十五轮）：这条消息的"另一份文本"。
    #   形状：{"text", "direction", "lang", "display", "used_model", "skipped",
    #          "tokens", "model", "provider_id", "error"}
    #   `content` 永远是**模型看到的文本**（输入侧 = 译文，输出侧 = 模型原文），
    #   `display` 告诉界面"给人看哪一份" —— 于是界面切换原文/译文不需要多余字段。
    translation_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="翻译中间件结果 JSON（原文/译文可切换）"
    )

    # ★ 状态原始块（第十六轮）：模型这一轮输出的 `<state>…</state>` 里的**原始文本**。
    #   为什么要单独留一份：`<state>` 会被按 schema 解析成结构化字段，卡作者写的
    #   复杂/美化排版到这一步就没了（用户要"看作者原格式"就只能看原文）。
    #   形状：`<state>` 里的那一段文本（通常是 JSON，未解析、原样）。
    #   ★ 只有**当轮真的输出了块**时才写；没输出/纯聊天/卡没声明状态栏时是 NULL。
    state_raw_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="模型输出的 <state> 原始文本（界面「看作者原格式」用）"
    )

    # ★ 逐轮真实用量（第十六轮）：厂商返回的 usage 分项 + **我们发出去前对输入的估算**。
    #   为什么要落库：`token_count` 只存了 completion 那一半、`session.total_tokens` 只存了
    #   累计总和 ⇒ 事后无法回答"启发式估算 vs 厂商 prompt_tokens 差多少"（论文里一直
    #   只能写"未做过对照实测"）。
    #   形状：{"prompt_tokens": n, "completion_tokens": n, "reasoning_tokens": n,
    #          "total_tokens": n, "estimated_input_tokens": n}
    #   ★ reasoning_tokens 也在这里 —— 它是"思考烧钱"的直接证据（含在 completion 内）。
    usage_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="本轮真实用量 JSON（厂商分项 + 我们的输入估算）"
    )

    # ---------------- 时间 ----------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, comment="创建时间"
    )

    # ---------------- 关系 ----------------
    session: Mapped["NarrativeSession"] = relationship(  # noqa: F821
        back_populates="messages"
    )

    def __repr__(self) -> str:
        # 正文可能很长，这里只截取前 20 个字符，避免日志被刷屏
        preview = (self.content or "")[:20]
        return f"<Message id={self.id} role={self.role} content={preview!r}...>"
