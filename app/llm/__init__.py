"""异构大模型适配层（本项目的核心）。

==================== 这一层解决什么问题？====================
市面上的大模型服务五花八门，协议、字段名、错误码各不相同：

    DeepSeek / 通义 / Kimi / 智谱 / 硅基流动 / vLLM / Ollama(兼容层) / LM Studio
        —— 都兼容 OpenAI 的 /chat/completions 协议
    Anthropic (Claude)
        —— 用自己的 Messages 协议，请求 / 响应 / 流式事件格式完全不同

如果业务代码直接调用各家 SDK，那么每接入一个新厂商都要改动叙事引擎；
本层的作用就是把这些差异**全部吸收在适配器内部**，对外只暴露一套统一接口。
上层的叙事引擎永远只需写：

    provider = create_provider(...)
    result = provider.chat(ChatRequest(messages=[...]))

无论背后是 GPT、Claude 还是本地模型，业务代码一行都不用改。

==================== 目录结构 ====================
    schema.py             统一的入参 / 出参数据结构（协议无关的中间表示）
    params.py             ★ 生成参数与上下文预算（温度 / 最大输出 / 思考强度）
    errors.py             统一的异常体系（把各厂商五花八门的错误码归一化）
    http_client.py        带重试的 HTTP 客户端封装（各适配器共用）
    base.py               BaseLLMProvider 抽象基类
    openai_compatible.py  OpenAI 兼容协议适配器（覆盖绝大多数厂商）
    anthropic.py          Anthropic Messages 协议适配器
    factory.py            按配置动态构造适配器
"""

from app.llm.base import BaseLLMProvider, HealthCheckResult
from app.llm.factory import PROVIDER_TYPES, create_provider, create_provider_from_config
from app.llm.params import (
    REASONING_EFFORT_HINTS,
    ContextBudget,
    GenerationParams,
    ProviderConfig,
    ReasoningEffort,
    compute_context_budget,
    # ★ 第二十七轮：从已删除的 `diagnostics.py` 搬过来（它讲的是 token 预算规则，
    #   与"探测厂商是否听话"无关，不该和那个一起消失）。
    describe_token_split,
)
from app.llm.schema import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    Role,
    StreamChunk,
    TokenUsage,
)

__all__ = [
    # 抽象与结果类型
    "BaseLLMProvider",
    "HealthCheckResult",
    # 统一数据结构
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "Role",
    "StreamChunk",
    "TokenUsage",
    # 生成参数与上下文预算
    "GenerationParams",
    "ReasoningEffort",
    "REASONING_EFFORT_HINTS",
    "ContextBudget",
    "compute_context_budget",
    "ProviderConfig",
    # 工厂
    "PROVIDER_TYPES",
    "create_provider",
    "create_provider_from_config",
    # token 预算说明（纯计算，不调用模型）
    "describe_token_split",
]
