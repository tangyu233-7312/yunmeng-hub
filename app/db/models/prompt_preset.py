"""提示词预设模型（酒馆式 completion preset）。

==================== 它和"角色卡"是什么关系？====================
    角色卡  = 这个角色是谁（人设、性格、说话方式）
    世界书  = 这个世界有什么（设定、地名、人物关系）
    ★ 预设  = **模型应当怎么工作**（规则、破甲、输出格式、采样参数）

三者是正交的：同一张角色卡配不同预设，模型的表现可以完全不同。
用户明确说过："角色卡设定通常写在世界书里，而预设则主要是规范 AI 的行为"，
所以预设必须是**独立于角色卡**的一等实体，不能塞进角色卡的字段里。

==================== 为什么配置存 JSON 而不是拆成表？====================
预设的结构（块清单 + 顺序 + 采样参数）是**跟着酒馆规范走**的，
规范一变我们就要跟着加字段。拆成 `preset_blocks` 表意味着每次都要改表结构，
而我们又不想引入迁移框架（这个项目刻意只依赖 MySQL + SQLAlchemy）。

存 JSON 的代价是"不能按块查询"，但预设的使用方式本来就只有两种：
「整套取出来装配」和「整套复制/导出」—— 没有按块查询的需求。
所以这里 JSON 是更合适的选择，而且能**原样保真**地导入导出酒馆预设。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, text
from sqlalchemy.dialects.mysql import JSON, MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class PromptPreset(Base, TimestampMixin):
    """一套提示词预设。

    装配语义见 `app/narrative/presets.py`（解析与渲染都在那里，本文件只管存）。
    """

    __tablename__ = "prompt_presets"
    __table_args__ = {"comment": "提示词预设表（规范模型行为的规则/破甲/采样参数）"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="预设ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="所属用户ID",
    )

    name: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="预设名称（允许重名，理由同角色卡）"
    )
    description: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="一句话说明这套预设是干什么的"
    )

    # ---------------- 配置本体 ----------------
    #: 结构见 app/narrative/presets.py::PromptPresetConfig.to_dict()
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="块清单 + 顺序 + 采样参数（JSON）"
    )

    source_filename: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="导入来源文件名（便于追查这份预设从哪来）"
    )
    source_format: Mapped[str] = mapped_column(
        String(32),
        default="hne",
        server_default="hne",
        nullable=False,
        comment="来源格式：hne / sillytavern",
    )

    #: 导入时产生的提醒（"这些参数云端无效""这些块本系统不支持"…）。
    #: ★ 单独存一列而不是塞进 config：它是**导入那一刻的结论**，
    #:   界面上要能随时回看，而不是每次重新推导（推导结果可能随版本变化）。
    import_notes: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="导入时的提醒与降级说明（换行分隔）"
    )

    is_active: Mapped[bool] = mapped_column(
        # 全局默认预设：新建会话时默认用它。同一用户同时只有一个为真。
        default=False,
        server_default=text("0"),
        nullable=False,
        comment="是否为该用户的全局默认预设",
    )

    #: 是否是「内置守卫预设」（身份认知 / 剧情不跑偏 / 输出长度）。
    #  ★ 它和普通预设**同样是可编辑的一行数据**，而不是写死在代码里的规则：
    #    用户明确要求"这些默认预设也可以由用户在预设管理里修改和删除"。
    #    标记的作用是让界面能显示「内置」徽标、并在删除时留下墓碑位。
    is_builtin: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False,
        comment="是否为内置守卫预设（可编辑、可删除、可还原）",
    )

    # ---------------- 关系 ----------------
    user: Mapped["User"] = relationship(back_populates="prompt_presets")  # noqa: F821

    def __repr__(self) -> str:
        blocks = len((self.config or {}).get("blocks") or [])
        return (
            f"<PromptPreset id={self.id} name={self.name!r} "
            f"blocks={blocks} active={self.is_active}>"
        )
