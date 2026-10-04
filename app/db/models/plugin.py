"""插件模型（声明式插件：正则替换 / 提示词注入 / CSS 主题 / 跑团骰点）。

==================== 为什么只做"声明式"这四种？====================
用户要一个"极简插件市场"，但他真正需要的能力只有四类：
    1. 正则替换   —— 把发给模型的提示词里某些词换掉（改口癖、屏蔽词、术语统一）
    2. 提示词注入 —— 在世界书/回忆之外再插一段固定文本（风格约束、文风样例）
    3. CSS 主题   —— 换控制台的配色/字号（纯样式，不碰数据）
    4. 跑团骰点   —— 需要随机数时由**后端**掷（解析器在 app/narrative/dice.py），
                     模型与用户都只能"请求"点数，不能自己造

★ **绝不执行第三方 JS**：插件的载体只有"数据"（JSON 清单 + 正则/文本/CSS/骰子参数），
  没有可执行代码，因此也就拿不到 API Key、发不出请求、读不到会话内容。
  这是刻意的取舍：能跑 JS 的插件系统等于把用户的 key 交给陌生人。
  骰子是唯一"带逻辑"的一种，但那份逻辑是我们自己写的、有界的求值器。

==================== 为什么配置存 JSON？====================
理由与 prompt_presets 相同：插件的结构会随能力扩展而变，
拆成表意味着每加一种能力就要改表结构，而本项目刻意不引入迁移框架。
插件只有"整套取出来用"和"整套导入导出"两种用法，没有按字段查询的需求。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, text
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.types import LongText, PkInt
from app.db.base import Base, TimestampMixin

#: 四种插件类型（与前端 tab、schema 的 Literal 保持一致）
PLUGIN_KINDS = ("regex", "prompt", "css", "dice")


class Plugin(Base, TimestampMixin):
    """一个声明式插件（属于某个用户）。"""

    __tablename__ = "plugins"
    __table_args__ = {"comment": "插件表（声明式：正则替换 / 提示词注入 / CSS 主题）"}

    id: Mapped[int] = mapped_column(
        PkInt, primary_key=True, autoincrement=True, comment="插件ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="所属用户ID",
    )

    name: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="插件名（允许重名，理由同角色卡）"
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="插件类型：regex / prompt / css / dice"
    )
    description: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="一句话说明这个插件做什么"
    )
    version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="插件自报的版本号（纯展示）"
    )
    author: Mapped[str | None] = mapped_column(
        String(120), nullable=True, comment="作者（纯展示，来源 URL 里的账号不一定是作者）"
    )

    #: 安装来源。手工新建的插件为 NULL；从 GitHub 安装的记原始 URL（便于审计与重装）
    source_url: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="安装来源 URL（GitHub 页面或 raw 地址）"
    )
    #: 手工新建的默认插件标记（默认插件可以被停用/删除，删了就不会再自动重建）
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0"), comment="是否为随账号初始化的默认插件"
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("1"), comment="是否启用"
    )
    #: 顺序：数值小的先应用（正则按顺序做替换；提示词块按各自位置插入）
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("100"), comment="应用顺序（小的先）"
    )

    #: 配置本体，结构随 kind 而定（见 app/services/plugin_service.py 的校验）
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        comment="插件配置（regex.rules / prompt.{position,content} / css.css / dice.{triggers,…}）",
    )
    #: 原始清单（导入时的原样保留：导出/排查时能看到作者写了什么，不丢字段）
    raw_manifest: Mapped[str | None] = mapped_column(
        LongText, nullable=True, comment="安装时的原始 JSON 清单（原样保存，便于审计）"
    )
