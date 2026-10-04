"""世界书模型（世界观设定集 / lorebook）。

==================== 什么是世界书？====================
角色卡描述「AI 要扮演谁」，世界书描述「这个故事发生在什么样的世界里」。
它是一本条目集，每条包含：

    · keys      触发关键词，如 ["龙", "巨龙"]
    · content   命中后注入提示词的设定文本，如「世上最后一条龙已在三百年前死去」
    · enabled   是否启用
    · ...

对话中提到某个关键词时，就把对应条目的内容塞进提示词 ——
这样模型就能"记住"那些没写在人设里、但属于世界观的事实。

==================== ★ 为什么它是一个独立的表？====================
最初的实现把世界书当作角色卡里的一个字段（存在卡的 extra_data 里）。
这带来两个问题：

1. **删卡就必然删世界书**。用户想「删掉这张卡但保留我写了几十个条目的世界书」
   是完全合理的诉求，但字段和卡在同一个数据库行里，做不到。

2. **没法复用**。同一套世界观往往要给多张卡用
   （「克苏鲁世界」下面可以有十几张不同角色的卡），
   塞在卡里就只能每张卡各存一份，改一处要改十几处。

所以世界书独立成表，角色卡通过 world_book_id 外键引用它，一张世界书可被多张卡共用。

==================== 与 Character Card V2 规范的对应 ====================
规范里世界书叫 character_book，挂在 data 下：

    data.character_book = {
      "name"?: string, "description"?: string,
      "scan_depth"?: number, "token_budget"?: number, "recursive_scanning"?: boolean,
      "extensions": Record<string, any>,     // 规范要求必须有
      "entries": [ {keys, content, enabled, insertion_order, ...}, ... ]
    }

对应到本表：

    entries        <- character_book.entries        （核心内容，独立成列便于查询）
    name           <- character_book.name
    description    <- character_book.description
    extra_data     <- 其余全部键（scan_depth / token_budget / extensions / 未知字段）

★ extra_data 保证「导入导出不丢字段」：规范里那些扫描深度、token 预算之类的
  参数我们暂不实现（属于后续的关键词触发检索功能），但必须原样存下来，
  否则用户把卡导出回 SillyTavern 时这些设置就没了。
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, ForeignKey, String, Text
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.types import PkInt
from app.db.base import Base, TimestampMixin


class WorldBook(Base, TimestampMixin):
    """世界书：一组「关键词 → 设定文本」条目，可被多张角色卡共用。

    ★ 关于 entries 的修改方式（容易踩坑）：
      entries 是 JSON 列，里面装的是一个个 dict。
      SQLAlchemy 的 MutableList 只能感知**列表本身**的变化（append / remove / 整体赋值），
      **感知不到列表里某个 dict 内部被改了**：

          book.entries[0]["content"] = "新内容"     # ← 不会被写进数据库！
          book.entries = new_list                   # ← 这样才会

      所以服务层一律采用「整体重新赋值」的写法，绝不原地改嵌套 dict。
    """

    __tablename__ = "world_books"
    __table_args__ = {"comment": "世界书表"}

    id: Mapped[int] = mapped_column(
        PkInt, primary_key=True, autoincrement=True, comment="世界书ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="创建者用户ID",
    )

    # ---------------- 基本信息 ----------------
    # ★ name 允许为空，这是刻意的：
    #   规范里 character_book.name 是**可选**字段，大量真实角色卡不写世界书名字。
    #   如果导入时自作主张补一个（如「爱丽丝 的世界书」），
    #   导出结果就和原始数据不一致了 —— 会破坏「导入导出往返完全一致」这个性质。
    #   所以这里忠实存 NULL，界面显示用的名字由 display_name 兜底（见服务层）。
    name: Mapped[str | None] = mapped_column(
        String(200), index=True, nullable=True, comment="世界书名称（可为空）"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="简介"
    )

    # ---------------- 核心内容 ----------------
    # 条目数组。每条形如：
    #   {"keys": ["龙"], "content": "……", "enabled": true, "insertion_order": 0, "extensions": {}}
    entries: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        comment="条目数组（关键词触发的设定文本）",
    )

    # ---------------- 未映射字段的原样保留区 ----------------
    extra_data: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
        comment="导入时未能映射到本表字段的原始数据（保证导出时不丢字段）",
    )

    # ---------------- 关系 ----------------
    user: Mapped["User"] = relationship(back_populates="world_books")  # noqa: F821
    # ★ 这里不加 passive_deletes，与 CharacterCard.sessions 的写法相反，原因见下方注释：
    #   character_cards.world_book_id 可为空 + ON DELETE SET NULL，
    #   而 SQLAlchemy 删除父对象时的默认行为正是「把子对象外键置 NULL」——
    #   恰好就是我们要的语义：删掉一本世界书，卡片还在，只是不再关联世界书。
    #   （实测验证见 tests/test_character_cards.py 的删除测试。）
    character_cards: Mapped[list["CharacterCard"]] = relationship(  # noqa: F821
        back_populates="world_book"
    )

    def __repr__(self) -> str:
        return (
            f"<WorldBook id={self.id} name={self.name!r} "
            f"entries={len(self.entries or [])}>"
        )
