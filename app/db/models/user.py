"""用户表模型。"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    """系统用户。

    安全约定：密码**只保存 bcrypt 哈希，永不保存明文**。
    哈希在 app/core/security.py 里生成（第 3.5 步实现），数据库中即使被拖库也无法反推出原密码。
    """

    __tablename__ = "users"
    __table_args__ = {"comment": "用户表"}

    # ---------------- 主键 ----------------
    # BigInteger 对应 MySQL 的 BIGINT，容纳量远大于 INT，避免用户量增长后主键溢出
    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="用户ID"
    )

    # ---------------- 账号信息 ----------------
    # unique=True 会在数据库层建立唯一约束（比只在代码里查重更可靠，能防止并发注册产生重复）
    # index=True  会建立索引，让「按用户名/邮箱查询」走索引而不是全表扫描
    username: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False, comment="登录用户名"
    )
    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False, comment="邮箱"
    )
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="bcrypt 密码哈希（不可逆，禁止存明文）"
    )
    nickname: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="昵称（可空，未填时展示用户名）"
    )

    # ---------------- 状态 ----------------
    # server_default=text("1") 让数据库层面也有默认值：即使有人绕过 ORM 直接写 SQL 插入，
    # 不指定该字段时也会得到 1（启用），不会因为 NOT NULL 而插入失败
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("1"),
        nullable=False,
        comment="是否启用（禁用后无法登录）",
    )

    #: 用户是否主动删掉了「内置守卫预设」。
    #  ★ 为什么需要这个"墓碑位"：内置预设是**按需创建**的（第一次用就自动建一份，
    #    这样它才能被编辑）。没有这个标记的话，用户删掉它之后下一次请求又会把它
    #    建回来 —— 那就不是"可删除"了。有了它，「删除」才真的生效，
    #    而「还原」按钮就是把这个标记清掉并重新生成一份。
    builtin_preset_dismissed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False,
        comment="是否已删除内置守卫预设（删除后不再自动重建，直到点「还原」）",
    )

    #: 是否已经给这个账号发过两个"默认插件空壳"。
    #  ★ 同一个道理：默认插件是按需创建的，但用户把它们删光之后
    #    不能下一次访问又冒出来 —— 那就不是"可删除"了。
    plugin_defaults_seeded: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False,
        comment="是否已初始化默认插件（删掉后不再自动重建）",
    )

    # ---------------- 关系（ORM 层面的便捷访问）----------------
    # 这些字段不会在数据库里生成列，只是让代码可以 user.providers 直接拿到关联对象。
    # cascade="all, delete-orphan"：删除用户时，其名下的 LLM 配置 / 角色卡 / 会话一并删除。
    providers: Mapped[list["LLMProvider"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    character_cards: Mapped[list["CharacterCard"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    world_books: Mapped[list["WorldBook"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    sessions: Mapped[list["NarrativeSession"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    prompt_presets: Mapped[list["PromptPreset"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        """打印对象时的可读形式，调试时很方便：<User id=1 username='alice'>"""
        return f"<User id={self.id} username={self.username!r}>"
