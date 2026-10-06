"""适配器工厂：按「用户的模型配置」动态构造适配器。

==================== 它在整个链路里的位置 ====================
用户在界面上填写「Base URL + API Key + 模型名 + 协议类型 + 生成参数」，
这些信息存进 llm_providers 表（Key 加密存储）。
每次需要调用模型时，由本工厂根据 provider_type 造出对应的适配器实例。

    ProviderConfig（界面上的一张表单 / 数据库的一行）
              ↓  create_provider_from_config(...)
        BaseLLMProvider 实例（OpenAI 兼容 / Anthropic / ...）
              ↓  provider.chat(...)
            统一的 ChatResult

新增一个协议只需要在 _BUILDERS 里注册一行，上层无需改动。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.llm.base import BaseLLMProvider
from app.llm.params import GenerationParams, ProviderConfig

# 已支持的协议类型 -> 人类可读说明
# 这份表会通过接口暴露给前端，作为「协议类型」下拉框的选项
PROVIDER_TYPES: dict[str, str] = {
    "openai_compatible": (
        "OpenAI 兼容协议 —— 覆盖 DeepSeek / 通义千问 / Kimi / 智谱 GLM / "
        "硅基流动 / 火山方舟 / 本地 vLLM / Ollama / LM Studio 等绝大多数服务"
    ),
    "anthropic": (
        "Anthropic Messages 协议 —— Claude 系列（api.anthropic.com），"
        "以及兼容该协议的代理服务。与 OpenAI 协议的差异较大："
        "系统提示词是顶层参数、max_tokens 必填、消息必须严格交替、"
        "流式使用命名事件、思考强度用 budget_tokens 表达"
    ),
}


def _build_openai_compatible(**kwargs: Any) -> BaseLLMProvider:
    """构造 OpenAI 兼容适配器。延迟导入，避免未使用时也加载相关模块。"""
    from app.llm.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider(**kwargs)


def _build_anthropic(**kwargs: Any) -> BaseLLMProvider:
    """构造 Anthropic Messages 适配器。"""
    from app.llm.anthropic import AnthropicProvider

    return AnthropicProvider(**kwargs)


#: 协议类型 -> 构造函数。新增协议时在这里注册即可。
_BUILDERS: dict[str, Callable[..., BaseLLMProvider]] = {
    "openai_compatible": _build_openai_compatible,
    "anthropic": _build_anthropic,
}


def _clean_params(
    extra_params: dict[str, Any] | GenerationParams | None,
) -> GenerationParams | dict[str, Any]:
    """整理生成参数。

    约定：以单个下划线开头的键是本项目内部使用的元数据，
    **不会被发送到第三方模型服务**。

    例如 extra_params 里可能同时存在：
        {"temperature": 0.8, "max_tokens": 2048, "_note": "给这个配置写的备注"}
    其中 _note 只用于展示，不应混进 API 请求体。

    返回 GenerationParams 时直接透传（已经过校验）；
    返回字典时交给 BaseLLMProvider 内部转换。
    """
    if extra_params is None:
        return {}
    if isinstance(extra_params, GenerationParams):
        return extra_params
    return {key: value for key, value in extra_params.items() if not str(key).startswith("_")}


def create_provider(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model_name: str,
    context_window: int = 65536,
    extra_params: dict[str, Any] | GenerationParams | None = None,
    timeout: int | None = None,
    max_retries: int | None = None,
    settings: Settings | None = None,
) -> BaseLLMProvider:
    """根据用户配置创建适配器实例。

    参数：
        provider_type   协议类型，取值见 PROVIDER_TYPES
        base_url        API 地址，需包含版本号
        api_key         API 密钥（可为空，本地部署的服务通常不需要）
        model_name      模型名
        context_window  模型上下文窗口总容量（输入 + 输出）
        extra_params    生成参数与厂商特有参数。可传 GenerationParams 或字典
        timeout         读取超时秒数，默认取 .env 的 HNE_LLM_REQUEST_TIMEOUT
        max_retries     最大重试次数，默认取 .env 的 HNE_LLM_MAX_RETRIES

    抛出：
        ConfigurationError —— 配置不合法（缺字段、协议不支持等）
    """
    settings = settings or get_settings()
    provider_type = (provider_type or "").strip().lower()

    # ---------- 校验协议类型 ----------
    if provider_type not in _BUILDERS:
        raise ConfigurationError(
            f"不支持的协议类型: {provider_type or '(空)'}",
            detail={
                "supported": sorted(_BUILDERS.keys()),
                "suggestion": "请把 provider_type 改为上面列出的取值之一",
            },
        )

    # ---------- 校验必填项 ----------
    base_url = (base_url or "").strip()
    model_name = (model_name or "").strip()

    missing: list[str] = []
    if not base_url:
        missing.append("base_url")
    if not model_name:
        missing.append("model_name")
    if missing:
        raise ConfigurationError(
            f"模型配置缺少必填项: {', '.join(missing)}",
            detail={"provider_type": provider_type},
        )

    if not base_url.startswith(("http://", "https://")):
        raise ConfigurationError(
            f"base_url 必须以 http:// 或 https:// 开头，当前为: {base_url}",
            detail={"example": "https://api.deepseek.com"},
        )

    # api_key 允许为空：本地部署的 vLLM / Ollama 通常不校验密钥
    return _BUILDERS[provider_type](
        base_url=base_url,
        api_key=api_key or "",
        model_name=model_name,
        timeout=timeout if timeout is not None else settings.LLM_REQUEST_TIMEOUT,
        max_retries=max_retries if max_retries is not None else settings.LLM_MAX_RETRIES,
        context_window=context_window,
        default_params=_clean_params(extra_params),
    )


def create_provider_from_config(
    config: ProviderConfig,
    *,
    settings: Settings | None = None,
) -> BaseLLMProvider:
    """从完整的 ProviderConfig 创建适配器。

    这是「界面表单 → 可调用的适配器」之间最短的一条路径：
    ProviderConfig 里已经包含了连接信息、上下文窗口与全部生成参数，
    因此界面保存后即可直接用它构造适配器做连通性测试。

    注意：config.api_key 应为**解密后的明文**（数据库中存的是密文）。
    """
    adapter = create_provider(
        provider_type=config.provider_type,
        base_url=config.base_url,
        api_key=config.api_key,
        model_name=config.model_name,
        context_window=config.context_window,
        extra_params=config.generation,
        settings=settings,
    )
    # ★ 第二十七轮：这里原来把"思考强度探测结论"从 ProviderConfig 复制到适配器上。
    #   探测功能已删除（不可靠、且给过错误结论），改为：
    #     · 适配器用 `supports_reasoning_effort` **静态声明**自己能不能翻译这个参数；
    #     · 服务端到底接不接受，由请求时的 400 自动退回兜底。
    #   所以这里不再需要搬运任何结论。
    return adapter
