"""卡自带状态（第二状态存储）的模型。

==================== 为什么单独一张表 ====================
本系统的结构化状态存在 `narrative_sessions.state_json`（字段由卡/世界书解析出的
`state_schema` 定义，每轮按 schema 校验、合并、并注入提示词）。

**卡自带的状态是另一种东西**：字段名、类型、由谁更新全由卡作者决定，
本系统既不校验也不注入。两者混进同一列会带来两个无法挽回的问题：

  1. 分不清"这个 hp 是谁写的" —— 用户改了本系统的状态，卡状态会被一起覆盖；
  2. 卡换了一版（字段全变了）时，旧数据既不能留着也不能删，只能互相污染。

所以独立成表：`(session_id, schema_hash)` 唯一，一个会话里"每种形状各存一份"，
形状变了就是新的一行 —— 旧形状的数据**不会被静默覆盖**，回滚卡版本时还能找回来。

==================== 与消息表的关系 ====================
`message_id` / `raw` 记的是"最近一次是谁带来的、原文长什么样"，纯粹为了
「看作者原格式」与排错；它**不**是外键级联的来源（删消息不该让状态消失，
卡状态是会话级的）。所以这里用普通列 + 索引，不加外键。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import LongText, PkInt


class CardState(Base):
    """一个会话里，某个角色卡状态块形状的最新值。"""

    __tablename__ = "card_states"
    __table_args__ = (
        # ★ 唯一键 = 会话 + 形状：同形状只更新、不同形状各留一份（见模块说明）。
        Index("ux_card_states_session_shape", "session_id", "schema_hash", unique=True),
        {"comment": "卡自带状态表（第二状态存储，不注入提示词）"},
    )

    id: Mapped[int] = mapped_column(
        PkInt, primary_key=True, autoincrement=True, comment="主键"
    )
    session_id: Mapped[int] = mapped_column(
        BigInteger,
        # ★ 会话被删 → 卡状态一起删（它离开会话没有任何意义）
        ForeignKey("narrative_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="所属会话ID",
    )
    #: 状态形状指纹（`card_state.schema_hash`：顶层键集合的哈希）
    schema_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="状态形状指纹（顶层键集合的哈希）"
    )
    #: 状态 JSON（原样，未做 schema 校验 —— 卡状态没有 schema 可依）
    data_json: Mapped[str] = mapped_column(
        LongText, nullable=False, comment="卡状态 JSON（原样，未校验）"
    )
    #: 来源：state_tag / json_fence / mvu / script_json / json_tag
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="识别来源（见 app/narrative/card_state.py）"
    )
    #: 最近一次原始块文本（界面「看作者原格式」）
    raw_json: Mapped[str | None] = mapped_column(
        LongText, nullable=True, comment="最近一次的卡状态块原文"
    )
    #: 最近一次来自哪条消息
    message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="最近一次写入来自哪条消息（排错用，不是外键）"
    )
    #: 累计更新次数（用户能看出"这套状态一直在更新"还是"卡住了"）
    updates: Mapped[int] = mapped_column(
        default=0, nullable=False, comment="累计写入次数"
    )
    #: 上一轮的形状指纹（字段消失/类型变化检测的参照；NULL = 第一轮）
    prev_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="上一轮的形状指纹（NULL = 首轮）"
    )
    #: 最近一次漂移检查结果（只含 missing/retyped/new，见 card_state.diff_fields）
    drift_json: Mapped[str | None] = mapped_column(
        LongText, nullable=True, comment="最近一次字段消失/类型变化检查结果 JSON"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False, comment="首次出现时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="最近更新时间",
    )

    def __repr__(self) -> str:
        return (
            f"<CardState session={self.session_id} shape={self.schema_hash[:8]} "
            f"source={self.source} updates={self.updates}>"
        )
