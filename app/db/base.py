"""ORM 基类与公共字段 Mixin。

【为什么这样写】
SQLAlchemy 2.0 推荐「类型注解 + mapped_column」的声明式写法：

    nickname: Mapped[str | None]      <- 告诉 Python/SQLAlchemy 这个字段的类型
        = mapped_column(String(50))   <- 描述数据库层面的细节（长度、索引、注释等）

好处是 IDE 能自动补全、mypy 能静态检查类型，比旧版 Column(...) 写法更安全。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    Base.metadata 会汇总整个项目里所有表的结构信息，
    建表脚本（scripts/init_db.py）与数据库迁移（Alembic）都基于它。
    """


class TimestampMixin:
    """混入类：给表统一加上「创建时间 / 更新时间」两个字段。

    用法：class User(Base, TimestampMixin) —— 继承后自动拥有这两个字段，
    不用在每张表里重复写一遍。

    注意两个参数的区别：
      * server_default=func.now()  由 MySQL 负责填值（生成的 DDL 里是 DEFAULT CURRENT_TIMESTAMP）
      * onupdate=func.now()        每次执行 UPDATE 时，SQLAlchemy 自动把该字段设为当前时间
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )
