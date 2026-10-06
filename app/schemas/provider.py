"""用户自配模型（Provider）的请求 / 响应模型。

这些模型同时也是**前端「新增模型」表单的契约** ——
字段含义、取值范围、界面文案的完整说明见 docs/generation-params.md。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.llm import PROVIDER_TYPES, GenerationParams


class ProviderCreate(BaseModel):
    """新增模型配置。"""

    name: str = Field(..., min_length=1, max_length=64, description="配置别名，如「我的 DeepSeek」")
    provider_type: str = Field(default="openai_compatible", description="协议类型")
    base_url: str = Field(..., min_length=1, description="API 地址，如 https://api.deepseek.com")
    api_key: str = Field(default="", description="API 密钥（本地部署可留空）")
    model_name: str = Field(..., min_length=1, max_length=128, description="模型名")
    context_window: int = Field(
        default=65536, ge=512, le=10_000_000, description="模型上下文窗口总容量（输入+输出）"
    )
    generation: GenerationParams = Field(
        default_factory=GenerationParams, description="生成参数（温度 / 最大输出 / 思考强度等）"
    )
    fallback_provider_id: int | None = Field(
        default=None,
        description="备用模型配置ID（必须是自己的另一个配置）：主模型失败且尚未输出内容时自动切换",
    )
    stream_enabled: bool = Field(
        default=True,
        description="是否使用流式传输。★ 关掉后后端走非流式调用、整段一次性返回；"
        "开启但上游不支持流式时，效果会与关闭时相同（界面上的提醒写明了这一点）",
    )
    is_default: bool = Field(default=False, description="是否设为默认模型")
    is_active: bool = Field(default=True, description="是否启用")

    @field_validator("provider_type")
    @classmethod
    def _validate_provider_type(cls, value: str) -> str:
        """协议类型必须是已注册的，否则等到真正调用时才报错就太晚了。"""
        value = value.strip().lower()
        if value not in PROVIDER_TYPES:
            raise ValueError(
                f"不支持的协议类型: {value}，可选值：{', '.join(sorted(PROVIDER_TYPES))}"
            )
        return value

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("API 地址必须以 http:// 或 https:// 开头")
        return value.rstrip("/")

    @field_validator("name", "model_name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _check_output_fits_window(self) -> "ProviderCreate":
        """输出预留不能吃掉整个上下文窗口。

        ★ 这个校验必须放在**接口层**，而不是等到组装 ProviderConfig 时才发现 ——
          否则用户会收到一个 500（内部错误），而不是清晰的 422 参数错误。
        """
        if self.generation.max_tokens >= self.context_window:
            raise ValueError(
                f"最大输出 token（{self.generation.max_tokens}）不能大于等于上下文窗口"
                f"（{self.context_window}）—— 那样就没有空间放提示词和对话历史了"
            )
        return self


class ProviderUpdate(BaseModel):
    """更新模型配置。

    所有字段都是可选的：只提交需要修改的字段即可（PATCH 语义）。

    ★ api_key 的三态约定（很容易设计错，这里明确写清楚）：
        · 不传该字段 / 传 null  -> 表示「不修改」，保留原密钥
        · 传空字符串 ""         -> 也表示「不修改」（前端不回显明文，只能这样表达）
        · 传非空字符串          -> 更新为新密钥
        · clear_api_key = true  -> 显式清空密钥（用于本地部署改用无鉴权模式）
    """
    name: str | None = Field(default=None, min_length=1, max_length=64)
    provider_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    clear_api_key: bool = False
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    context_window: int | None = Field(default=None, ge=512, le=10_000_000)
    generation: GenerationParams | None = None
    fallback_provider_id: int | None = Field(
        default=None,
        description="备用模型配置ID。★ 传 null 表示**解绑**（不自动切换）；不传该字段表示不修改",
    )
    stream_enabled: bool | None = None
    is_default: bool | None = None
    is_active: bool | None = None

    @field_validator("provider_type")
    @classmethod
    def _validate_provider_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value not in PROVIDER_TYPES:
            raise ValueError(f"不支持的协议类型: {value}")
        return value

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value.startswith(("http://", "https://")):
            raise ValueError("API 地址必须以 http:// 或 https:// 开头")
        return value.rstrip("/")


class ProviderOut(BaseModel):
    """模型配置（对外返回）。

    ★ 绝不会包含 api_key 明文 —— 只返回脱敏形式与「是否已配置」的标记。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider_type: str
    base_url: str
    model_name: str
    context_window: int
    generation: dict[str, Any]

    api_key_masked: str = Field(..., description="脱敏后的密钥，如 sk-a****z9")
    has_api_key: bool = Field(..., description="是否已配置密钥")
    api_key_decryptable: bool = Field(
        default=True,
        description="密钥能否成功解密。为 false 时通常意味着加密密钥被更换过，需要用户重新填写",
    )

    is_default: bool
    is_active: bool
    fallback_provider_id: int | None = Field(
        default=None, description="备用模型配置ID（主模型失败且尚未输出内容时自动切换）"
    )
    stream_enabled: bool = Field(default=True, description="是否使用流式传输")

    # ---------------- 连通性测试结果 ----------------
    last_tested_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_message: str | None = None

    # ---------------- 思考强度生效性探测结果 ----------------
    # ★ 第二十七轮已删除：该探测不可靠（详见 docs/dev-notes/handoff.md §27 的实验记录），
    #   改为"直接发送、被服务端拒绝就自动退回"。

    # ---------------- 派生信息 ----------------
    budget: dict[str, Any] = Field(default_factory=dict, description="上下文预算拆解")
    warnings: list[str] = Field(default_factory=list, description="会影响效果的问题（黄色警告）")
    hints: list[str] = Field(default_factory=list, description="建议性提示（灰色信息）")

    created_at: datetime
    updated_at: datetime


class ProviderTestResult(BaseModel):
    """连通性测试结果。"""

    ok: bool
    provider_type: str
    model: str
    latency_ms: int
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    tested_at: datetime


class ModelListResult(BaseModel):
    """可用模型列表。"""

    provider_id: int
    models: list[str]
    count: int
