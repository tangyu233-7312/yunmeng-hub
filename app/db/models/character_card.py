"""角色卡模型 —— 叙事引擎的「人设」载体。"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, String, Text, text
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class CharacterCard(Base, TimestampMixin):
    """角色卡：描述「AI 要扮演谁」。

    这张表的字段与提示词构建强相关：
    app/narrative/prompt_builder.py 会把这些字段拼装成系统提示词（System Prompt）。

    ==================== 与业界规范的对应关系 ====================
    角色卡不是本项目发明的概念。SillyTavern 生态有一套事实标准
    「Character Card V2」（spec: chara_card_v2 / spec_version: 2.0），
    字段对应关系如下（导入导出代码见 services/character_card_service.py）：

        本表字段                    Character Card V2
        ----------------------      --------------------------------
        name                        data.name
        description                 data.description
        personality                 data.personality
        scenario                    data.scenario
        greeting                    data.first_mes
        example_dialogue            data.mes_example
        alternate_greetings         data.alternate_greetings
        system_prompt               data.system_prompt
        post_history_instructions   data.post_history_instructions
        tags                        data.tags
        background / speaking_style 规范里没有 -> 导出时放进 extensions.hne 命名空间
        extra_data                  其余字段原样保存
        world_book_id               data.character_book（★ 独立成 world_books 表，
                                    详见 app/db/models/world_book.py 里的说明）

    ★ extra_data 存在的理由：规范明确要求「导入导出时**不得丢弃**无法识别的字段」。
      规范里那些我们暂未实现的字段（creator_notes、creator、character_version、
      以及各种插件写在 extensions 里的内容）如果直接扔掉，
      用户的卡导入再导出就残缺了。所以把没映射的原始数据整包存下来，保证往返无损。

    ★ 关于字段长度的选择（很多人会忽略这点）：
        TEXT       最多 64 KB
        MEDIUMTEXT 最多 16 MB
      「开场白」「对话示例」「系统提示词」都可能塞入大段文本，所以用 MEDIUMTEXT；
      其余（一句话简介、性格、背景等）用 TEXT 足够。
    """

    __tablename__ = "character_cards"
    __table_args__ = {"comment": "角色卡表"}

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="角色卡ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="创建者用户ID",
    )

    # ---------------- 基础信息 ----------------
    name: Mapped[str] = mapped_column(
        String(100), index=True, nullable=False, comment="角色名"
    )
    avatar_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="头像地址"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="一句话简介"
    )

    # ---------------- 人设细节（拼提示词的素材）----------------
    personality: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="性格特征"
    )
    background: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="背景故事 / 身世设定"
    )
    speaking_style: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="说话风格与语气"
    )
    scenario: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="初始场景，即故事从哪里开始"
    )
    example_dialogue: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="对话示例（few-shot 范例，用于稳定输出风格）"
    )

    # ---------------- 开场白 ----------------
    # ★ 这两个字段是交互式叙事引擎的关键：故事总要有一个开头。
    #   建会话时把 greeting 作为「角色的第一条消息」写入消息表，
    #   用户看到的第一句话就是它，而不是一片空白等自己先说话。
    #
    #   对应 SillyTavern 角色卡 V2 规范里的 first_mes / alternate_greetings。
    greeting: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="开场白（角色说的第一句话，故事从这里开始）"
    )
    # 备选开场白：对应规范里的 alternate_greetings（界面上的「换一个开头」）
    alternate_greetings: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        comment="备选开场白数组，用户可从中挑选一个开局",
    )

    # ---------------- 提示词覆盖（高级选项）----------------
    # 角色卡可以自带一段系统提示词，优先级**高于**引擎的默认人设提示词。
    # 对应规范的 system_prompt / post_history_instructions。
    system_prompt: Mapped[str | None] = mapped_column(
        MEDIUMTEXT,
        nullable=True,
        comment="自定义系统提示词（留空则用引擎按人设字段自动拼装的那份）",
    )
    post_history_instructions: Mapped[str | None] = mapped_column(
        MEDIUMTEXT,
        nullable=True,
        comment="尾注指令，追加在对话历史之后（用于强化文风或输出格式要求）",
    )

    # ---------------- 标签 ----------------
    # 同样使用 MutableList，保证 tags.append("奇幻") 这类原地修改能被检测到并写库
    tags: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        default=list,
        nullable=False,
        comment="标签数组，如 ['奇幻','侦探']",
    )

    # ---------------- 导入卡片的「原样保留」区 ----------------
    extra_data: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
        comment="导入时未能映射到本表字段的原始数据（保证导出时不丢字段）",
    )

    # ---------------- 可见性 ----------------
    is_public: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False,
        comment="是否公开（公开后其他用户可选用该角色卡）",
    )

    # ---------------- 关联的世界书 ----------------
    # 一张卡可以引用一本世界书，一本世界书可被多张卡共用（如「克苏鲁世界」）。
    # ★ ON DELETE SET NULL：删掉世界书，卡片本身还在，只是不再关联世界观设定。
    #   这比 CASCADE 合理得多 —— 世界书是独立资产，不该反过来决定卡片的存亡。
    world_book_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("world_books.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
        comment="关联的世界书ID（可为空）",
    )

    # ---------------- 关系 ----------------
    user: Mapped["User"] = relationship(back_populates="character_cards")  # noqa: F821
    world_book: Mapped["WorldBook | None"] = relationship(  # noqa: F821
        back_populates="character_cards"
    )

    # ★ 这里的关系配置改过一轮，值得说明「为什么现在是这个写法」：
    #
    #   最初 narrative_sessions.character_card_id 是 NOT NULL + ON DELETE CASCADE，
    #   而 SQLAlchemy 默认会「把子对象外键置 NULL」，直接违反非空约束 ——
    #   删除一张已被使用的角色卡会报 IntegrityError（详见 docs/pitfalls.md 第 11 条）。
    #   当时的修法是加 passive_deletes=True，让数据库的 CASCADE 生效。
    #
    #   后来这个设计被推翻了：删卡就把用户辛苦写的故事一起删掉太粗暴，
    #   改成由接口的勾选项（delete_sessions）来决定。
    #   于是外键变成「可空 + ON DELETE SET NULL」，语义反转成
    #   「默认保留故事，只是不再关联这张卡」。
    #
    #   现在这个组合的含义：
    #     · passive_deletes=True   交给数据库去置 NULL，ORM 不插手
    #     · 不写 delete-orphan     否则 ORM 会反向把会话全删掉，那是旧行为
    #     · 真要删会话时          由 service 层显式删除（用户勾选了才删）
    sessions: Mapped[list["NarrativeSession"]] = relationship(  # noqa: F821
        back_populates="character_card",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<CharacterCard id={self.id} name={self.name!r}>"
