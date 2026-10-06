"""用户自配的大模型 API 配置表 —— 本项目「异构」能力的落点。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.types import PkInt
from app.db.base import Base, TimestampMixin


class LLMProvider(Base, TimestampMixin):
    """一条「用户自配的模型端点」。

    一次配置 = 一个可调用的模型，例如：

        DeepSeek 官方    provider_type=openai_compatible
                        base_url=https://api.deepseek.com/v1
                        model_name=deepseek-chat

        阿里通义千问      provider_type=openai_compatible
                        base_url=https://dashscope.aliyuncs.com/compatible-mode/v1
                        model_name=qwen-plus

        本地 Ollama      provider_type=ollama
                        base_url=http://localhost:11434
                        model_name=qwen2.5:7b

        官方 OpenAI      provider_type=openai_compatible
                        base_url=https://api.openai.com/v1
                        model_name=gpt-4o-mini

    ★ 设计要点：因为绝大多数厂商都兼容 OpenAI 的 /chat/completions 协议，
      所以 provider_type 默认为 openai_compatible，用同一套适配器就能覆盖它们；
      只有协议差异较大的（如 Anthropic Messages、Ollama 原生接口）才需要单独适配器。
      适配器的实现位于 app/llm/（第 3.4 步）。
    """

    __tablename__ = "llm_providers"
    __table_args__ = (
        # 同一个用户下，配置别名不能重复（不同用户之间可以重名）
        UniqueConstraint("user_id", "name", name="uq_llm_provider_user_name"),
        {"comment": "用户自配的大模型 API 配置表"},
    )

    id: Mapped[int] = mapped_column(
        PkInt, primary_key=True, autoincrement=True, comment="配置ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        # ondelete="CASCADE"：数据库层面级联删除，删除用户时这些配置自动消失
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
        comment="所属用户ID",
    )

    # ---------------- 配置内容 ----------------
    name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="用户起的别名，如「我的DeepSeek」"
    )
    provider_type: Mapped[str] = mapped_column(
        String(32),
        default="openai_compatible",
        server_default="openai_compatible",
        nullable=False,
        # 取值：openai_compatible / anthropic / ollama / custom
        comment="协议类型，决定使用哪个适配器",
    )
    base_url: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="API 基础地址，如 https://api.deepseek.com/v1"
    )
    api_key_encrypted: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
        # ★ 安全要点：这里存的是 Fernet 对称加密后的密文，不是明文。
        #   加密/解密在 app/core/security.py 实现，密钥来自 .env 的 API_KEY_ENCRYPTION_KEY。
        #   这样即使数据库被拖库，攻击者也拿不到用户的 API Key。
        comment="API Key 密文（Fernet 加密，禁止存明文）",
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="模型名，如 deepseek-flash"
    )

    # ---------------- 主模型失败时的备用模型 ----------------
    # ★ 只存 id、**刻意不建数据库外键**：migrate_db.py 的幂等加列不支持补外键，
    #   而且外键的级联语义会带来"删一个配置顺手改了别人的备用指向"的意外。
    #   归属与合法性改由服务层校验（必须是同一个用户自己的另一个配置）。
    fallback_provider_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="备用模型配置ID（主模型失败且尚未输出内容时自动切换）",
    )
    # ---------------- 流式传输开关 ----------------
    # ★ 有些模型/中转不支持 SSE，或者会把流式悄悄转成非流式；用户要能自己关掉。
    #   关闭时后端走非流式调用、一次性把整段回复发给前端（前端无需改逻辑）。
    stream_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("1"),
        nullable=False,
        comment="是否使用流式传输（默认开；关掉后等整段返回）",
    )

    # ---------------- 模型能力（上下文窗口）----------------
    # 不同模型的上下文窗口差别很大（8K ~ 1M+），必须由用户按厂商文档填写。
    # 它决定了「能塞多少提示词与历史消息」，是上下文预算计算的基础。
    context_window: Mapped[int] = mapped_column(
        Integer,
        default=65536,
        server_default=text("65536"),
        nullable=False,
        comment="模型上下文窗口总容量（输入 + 输出）",
    )

    # ---------------- 核心生成参数 ----------------
    # ★ 这几个参数是界面上的一等公民控件，所以做成独立字段而不是塞进 JSON。
    #   好处：范围校验有保障、便于查询统计、论文里的数据模型也更清晰。
    temperature: Mapped[float] = mapped_column(
        Float,
        default=0.8,
        server_default=text("0.8"),
        nullable=False,
        comment="采样温度 0~2，叙事场景建议 0.7~1.0",
    )
    top_p: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="核采样 top_p，留空表示不发送该参数"
    )
    max_tokens: Mapped[int] = mapped_column(
        Integer,
        default=2048,
        server_default=text("2048"),
        nullable=False,
        # ★★ 这条注释是给未来的自己（和答辩老师）看的，务必保留
        comment="最大输出 token —— 包含思考过程 token，不是正文字数上限",
    )
    reasoning_effort: Mapped[str] = mapped_column(
        String(16),
        default="auto",
        server_default="auto",
        nullable=False,
        comment="思考强度：auto / off / low / medium / high",
    )


    # ---------------- 生成参数 ----------------
    # MutableDict.as_mutable(JSON) 的作用：
    #   普通 JSON 列如果原地修改（provider.extra_params["temperature"] = 0.8），
    #   SQLAlchemy 检测不到变化，不会生成 UPDATE 语句 —— 这是个很隐蔽的坑。
    #   套上 MutableDict 后，原地修改也能被正确追踪并写库。
    extra_params: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
        comment=(
            "长尾参数：厂商特有字段（如 response_format、presence_penalty）透传；"
            "以 _ 开头的键为本项目内部元数据，不会发送到上游"
        ),
    )

    # ---------------- 状态 ----------------
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        nullable=False,
        comment="是否为该用户的默认模型（每个用户最多一个，由业务逻辑保证）",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("1"),
        nullable=False,
        comment="是否启用",
    )

    # ---------------- 连通性测试结果 ----------------
    # 第 3.4 步会提供「测试连接」接口，把探测结果记下来，前端可以展示「上次检测成功/失败」
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="最后一次连通性测试时间"
    )
    last_test_ok: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="最后一次测试是否成功（NULL 表示从未测试）"
    )
    last_test_message: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="最后一次测试的失败原因摘要"
    )

    # ---------------- 关系 ----------------
    user: Mapped["User"] = relationship(back_populates="providers")  # noqa: F821
    # ★ 这里**故意**不加 passive_deletes，与 CharacterCard.sessions 的写法相反，
    #   因为两者的外键语义不同，别照抄：
    #     · 本表：llm_provider_id 可为空 + ON DELETE SET NULL
    #       -> SQLAlchemy 默认的「删除父对象时把子对象外键置 NULL」正是我们想要的：
    #          删掉一个模型配置，用户辛苦写的故事要保留，只是「不记得当初用的哪个模型了」。
    #     · CharacterCard.sessions：character_card_id 非空 + ON DELETE CASCADE
    #       -> 那边必须加 passive_deletes=True，否则置 NULL 会违反非空约束而报错。
    sessions: Mapped[list["NarrativeSession"]] = relationship(  # noqa: F821
        back_populates="provider"
    )

    def __repr__(self) -> str:
        # 注意：这里**绝对不能**打印 api_key_encrypted，避免密钥意外进入日志
        return (
            f"<LLMProvider id={self.id} name={self.name!r} "
            f"type={self.provider_type} model={self.model_name!r}>"
        )
