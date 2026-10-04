"""大模型适配器抽象基类。

==================== 抽象基类的作用 ====================
它定义了「所有厂商适配器都必须长什么样」：

    chat()         非流式对话   → 返回完整的 ChatResult
    stream_chat()  流式对话     → 逐段返回 StreamChunk
    list_models()  列出可用模型 → 可选能力
    health_check() 连通性测试   → 用于「测试连接」按钮

只要一个适配器实现了这些方法，上层的叙事引擎就能用它，
**完全不需要知道背后是 OpenAI、Claude 还是本地模型**。

==================== 新增一个厂商要做什么？====================
    1. 在 app/llm/ 下新建一个继承 BaseLLMProvider 的类
    2. 实现 chat() 与 stream_chat()（负责协议翻译）
    3. 在 factory.py 的 _BUILDERS 里注册一行
    4. 在 config 的 PROVIDER_TYPES 说明里加上它

叙事引擎、角色卡、会话管理这些业务代码**一行都不用改**。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from app.core.exceptions import ConfigurationError, LLMProviderError
from app.llm.errors import LLMBadRequestError, LLMUpstreamError, normalize_exception
from app.llm.http_client import build_client
from app.llm.params import ContextBudget, GenerationParams, ReasoningEffort, compute_context_budget
from app.llm.schema import ChatMessage, ChatRequest, ChatResult, StreamChunk, TokenUsage


@dataclass
class HealthCheckResult:
    """连通性测试结果。"""

    ok: bool
    provider_type: str
    model: str
    latency_ms: int = 0
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider_type": self.provider_type,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "message": self.message,
            "detail": self.detail,
        }


class BaseLLMProvider(ABC):
    """所有大模型适配器的基类。"""

    #: 协议类型标识，与数据库 llm_providers.provider_type 对应
    provider_type: str = "base"

    #: 该协议是否支持「列出可用模型」能力
    supports_model_listing: bool = False

    #: 该协议是否允许 **system 消息出现在对话中间**。
    #:
    #: ★ 这条能力标记服务于"提示词预设的深度注入"：
    #:   预设里可以有一个 role=system 的块，要求插进最近几条消息之前。
    #:   OpenAI 兼容协议接受这种形态；Anthropic 明确要求 system 只能放在
    #:   顶层 `system` 参数里，出现在 messages 里会被合并/拒绝。
    #:   不支持时我们**降级成 user 消息并加身份声明**，并把降级写进 notes 告知用户。
    supports_mid_conversation_system: bool = True

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout: int = 120,
        max_retries: int = 3,
        context_window: int = 65536,
        default_params: "GenerationParams | dict[str, Any] | None" = None,
    ) -> None:
        """
        参数：
            base_url         API 基础地址，需包含版本号，如 https://api.deepseek.com
            api_key          API 密钥。允许为空（本地部署的模型服务通常不需要鉴权）
            model_name       模型名，如 deepseek-flash
            timeout          读取超时秒数
            max_retries      可重试错误的最大重试次数
            context_window   模型上下文窗口总容量（输入 + 输出），由用户按厂商文档填写
            default_params   默认生成参数（温度、最大输出、思考强度等）。
                             传入 GenerationParams，或直接传字典（会自动校验并转换）。
        """
        self.base_url = base_url.strip().rstrip("/")
        self._api_key = (api_key or "").strip()
        self.model_name = model_name.strip()
        self.timeout = int(timeout)
        self.max_retries = max(0, int(max_retries))
        self.context_window = int(context_window)

        # 统一成 GenerationParams：这样默认参数也会走一遍范围校验，
        # 界面上填了非法值（例如温度 5.0）会在构造阶段就被拦下，
        # 而不是把错误请求发给厂商、拿到一个难懂的 400。
        if isinstance(default_params, GenerationParams):
            self.default_params = default_params
        else:
            self.default_params = GenerationParams.from_dict(default_params)

        # ★ 「思考强度是否真的生效」的探测结论（第十六轮）：由 `create_provider_from_config`
        #   从 ProviderConfig 带过来。为什么它必须跟着适配器走：翻译中间件与剧情总结都要
        #   拿它决定"能不能把思考降到最小"（探测结论为 None/False 时发这个参数会换来 400）。
        #   默认 None = 尚未探测/结论已过期 ⇒ 调用侧一律保守处理。
        self.reasoning_support: bool | None = None

        # httpx.Client 内部维护连接池，复用连接能显著降低延迟。
        # 构造它不会发起任何网络请求，所以在这里创建是安全的。
        self._client = build_client(self.timeout)

    # ==================== 基本信息 ====================
    @property
    def label(self) -> str:
        """用于日志和错误定位的短标签，例如 openai_compatible/deepseek-chat。"""
        return f"{self.provider_type}/{self.model_name}"

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def _reject_endpoint_in_base_url(self) -> None:
        """base_url 里混进了接口路径时，立刻给出可操作的报错。

        ★ 只有「会在 base_url 后面直接拼路径」的适配器才需要调用它
          （OpenAI 兼容协议正是如此）。Anthropic 适配器自己会判断
          "用户是不是已经把 /v1/messages 填全了"，所以不适用。

        判定用「路径段」而不是子串匹配，避免误伤把 /responses 写进域名里的极端情况。
        """
        path = urlparse(self.base_url).path.rstrip("/").lower()
        if not path:
            return

        segments = [seg for seg in path.split("/") if seg]
        #: 出现在 base_url 末尾就说明"用户把完整接口地址粘进来了"
        endpoint_tails = {
            "chat",
            "completions",
            "chat/completions",
            "responses",
            "messages",
            "embeddings",
            "models",
        }
        tail = "/".join(segments[-2:]) if len(segments) >= 2 else segments[-1]
        if segments[-1] not in endpoint_tails and tail not in endpoint_tails:
            return

        stripped = self.base_url
        for suffix in ("/chat/completions", "/completions", "/responses", "/messages", "/chat"):
            if stripped.lower().endswith(suffix):
                stripped = stripped[: -len(suffix)]
                break

        raise ConfigurationError(
            "API 地址填成了完整的接口地址，请在它后面去掉这一段",
            detail={
                "field": "base_url",
                "got": self.base_url,
                "looks_like_endpoint": tail,
                "suggestion": stripped or "https://api.example.com/v1",
                "why": (
                    "本项目会在这个地址后面自己拼接口路径（例如 /chat/completions），"
                    "所以 base_url 只能填到**版本目录**为止。"
                    "填了接口路径的话，请求会打到 .../responses/chat/completions 这种"
                    "不存在的地址，对方往往返回「模型不存在」这类误导性错误。"
                ),
                "examples": [
                    "https://api.deepseek.com/v1",
                    "https://ark.cn-beijing.volces.com/api/v3",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                ],
            },
        )

    @property
    def budget(self) -> ContextBudget:
        """当前上下文预算的拆解（输入能放多少、输出预留多少）。

        ★ 注意：输出预留 = max_tokens，它是**包含思考过程**的整块配额。
          叙事引擎在拼提示词时会用 budget.input_budget 来限制长度，
          详见 app/llm/params.py 的模块说明。
        """
        return compute_context_budget(self.context_window, self.default_params.max_tokens)

    @property
    def masked_api_key(self) -> str:
        """脱敏后的密钥。★ 任何时候都不要输出完整密钥。"""
        if not self._api_key:
            return "(未配置)"
        if len(self._api_key) <= 8:
            return "*" * len(self._api_key)
        return f"{self._api_key[:4]}****{self._api_key[-4:]}"

    def _endpoint(self, path: str) -> str:
        """拼接完整请求地址。"""
        return f"{self.base_url}/{path.lstrip('/')}"

    # ==================== 参数合并 ====================
    def _generation_params(self, request: ChatRequest) -> dict[str, Any]:
        """合并「配置默认参数」与「本次请求参数」，得到最终的请求体参数。

        优先级（后者覆盖前者）：
            default_params（数据库里的模型配置）
              < 请求中显式指定的字段
                < request.extra（厂商特有参数透传）

        这样每个模型配置可以有自己的默认温度等设置，
        而单次请求又能临时覆盖它 —— 叙事引擎正是靠这一点，
        在不同剧情节点切换「更保守」或「更天马行空」的生成风格。
        """
        overrides: dict[str, Any] = {}
        for key in ("temperature", "top_p", "max_tokens", "stop"):
            value = getattr(request, key, None)
            if value is not None:
                overrides[key] = value

        payload = self.default_params.merged_with(overrides).to_wire_params()
        # 请求级的厂商特有参数最后合并，优先级最高
        payload.update(request.extra)
        return payload

    def _resolve_reasoning_effort(self, request: ChatRequest) -> ReasoningEffort:
        """确定本次请求实际使用的思考强度。

        请求里指定了就用请求的，否则回落到模型配置里的默认值。
        """
        raw = (
            request.reasoning_effort
            if request.reasoning_effort is not None
            else self.default_params.reasoning_effort
        )
        if isinstance(raw, ReasoningEffort):
            return raw
        try:
            return ReasoningEffort(str(raw).strip().lower())
        except ValueError:
            logger.warning("无法识别的思考强度 {!r}，已按 auto 处理", raw)
            return ReasoningEffort.AUTO

    def _apply_reasoning_effort(
        self,
        payload: dict[str, Any],
        effort: ReasoningEffort,
        *,
        max_tokens: int | None = None,
    ) -> list[str]:
        """把统一的思考强度翻译成**本协议特有**的字段。

        返回值：为了满足本协议约束而做出的调整说明（会一路回报给用户）。
                默认实现不做任何调整，返回空列表。

        子类覆盖此方法：
            OpenAI 兼容 → payload["reasoning_effort"] = "minimal"/"low"/"medium"/"high"
            Anthropic   → payload["thinking"] = {"type": "enabled", "budget_tokens": N}
                          （且必须移除 temperature / top_p）

        这正是「异构适配层」存在的意义：上层只认 ReasoningEffort，
        各协议自己决定怎么表达它，并如实报告自己做了哪些妥协。
        """
        return []

    # ==================== 空回复拦截（各适配器共用）====================
    def _raise_if_no_content(
        self,
        content: str,
        reasoning: str,
        finish_reason: str | None,
        usage: TokenUsage,
    ) -> None:
        """把「空回复」这种静默失败变成明确报错。

        ★ 这个方法来自一次真实的踩坑：
          推理模型（如 DeepSeek 推理系列）会先把输出配额花在「思考过程」上。
          如果 max_tokens 给得太小，思考还没结束配额就用完了，正文一个字都没生成，
          而接口返回 finish_reason="length"、HTTP 200 —— 看起来是「成功」，
          实际上什么都没拿到，而且不报任何错。

          如果不拦这一层，上层会把这条空回复当成正常结果存进数据库，
          用户看到的只是「AI 没有回复」，排查时完全找不到方向。
        """
        if content.strip():
            return

        detail: dict[str, Any] = {
            "provider": self.label,
            "finish_reason": finish_reason,
            "usage": usage.to_dict(),
            "reasoning_preview": reasoning[:200],
        }

        if finish_reason == "length":
            hint = "（该模型是推理模型，思考过程占用了全部输出配额）" if reasoning else ""
            raise LLMBadRequestError(
                f"模型输出被 max_tokens 截断，且没有产生正文{hint}",
                detail={**detail, "suggestion": "请调大 max_tokens；推理模型建议至少 1024"},
            )

        if finish_reason == "content_filter":
            raise LLMBadRequestError(
                "回复被内容安全策略拦截，未产生正文",
                detail={**detail, "suggestion": "请调整系统提示词或用户输入"},
            )

        if not reasoning:
            raise LLMUpstreamError("模型返回了空回复", detail=detail)

        # 有思考过程但没有正文，且不是被截断 —— 同样属于异常
        raise LLMUpstreamError("模型只返回了思考过程，没有产生正文", detail=detail)

    # ==================== 子类必须实现 ====================
    @abstractmethod
    def chat(self, request: ChatRequest) -> ChatResult:
        """非流式对话：等模型生成完，一次性返回完整结果。"""

    @abstractmethod
    def stream_chat(self, request: ChatRequest) -> Iterator[StreamChunk]:
        """流式对话：边生成边返回，适合实时显示的打字机效果。"""

    # ==================== 可选能力 ====================
    def list_models(self) -> list[str]:
        """列出该服务可用的模型。默认不支持，返回空列表。"""
        return []

    # ==================== 通用实现 ====================
    def health_check(self) -> HealthCheckResult:
        """连通性测试：发一条极短的对话，看能不能正常拿到回复。

        这是「测试连接」按钮背后的实现。它比单纯 ping 域名更有价值，
        因为它同时验证了：网络可达、密钥有效、模型名正确、账号有额度。

        成本：一次 max_tokens=256 的调用；模型答完会自然停止，实际开销极小。
        """
        started = time.perf_counter()
        try:
            result = self.chat(
                ChatRequest(
                    messages=[ChatMessage.user("ping")],
                    # ★ 必须留出足够余量，原因有二：
                    #   1. 推理模型会先花 token 做「思考」，配额给小了会出现
                    #      「思考没结束、正文没开始」的情况
                    #   2. Anthropic 开启思考时要求 budget_tokens(>=1024) < max_tokens，
                    #      配额太小会直接无法开启思考
                    #   模型答完会自然停止，所以给大配额并不会真的多花钱。
                    #   这里取「256」与「配置里的最大输出」中的较大者，兼容两种协议。
                    max_tokens=max(256, self.default_params.max_tokens),
                    temperature=0,
                )
            )
        except LLMProviderError as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            detail = dict(exc.detail or {})

            # ★ 特例：模型「回话了，但没有正文」。
            #
            #   推理模型会先把输出配额花在思考过程上。实测 deepseek-flash 在
            #   max_tokens 不够、或提示极短时，会返回 reasoning_content 却让
            #   content 为空。这种情况其实已经证明了：
            #       · 网络能通   · API Key 有效   · 模型名存在
            #   也就是「连通性验证」这一个目标已经达成。
            #   此时若报「配置错误」，用户会去反复检查明明正确的配置，白折腾。
            responded = bool(detail.get("reasoning_preview")) or detail.get(
                "finish_reason"
            ) == "length"
            if responded:
                logger.info(
                    "连通性测试通过（推理模型未产生正文） | {} | {}", self.label, exc.code
                )
                return HealthCheckResult(
                    ok=True,
                    provider_type=self.provider_type,
                    model=self.model_name,
                    latency_ms=latency_ms,
                    message="连接正常（该模型为推理型，健康检查未产生正文）",
                    detail=detail,
                )

            logger.info("连通性测试失败 | {} | {} | {}", self.label, exc.code, exc.message)
            return HealthCheckResult(
                ok=False,
                provider_type=self.provider_type,
                model=self.model_name,
                latency_ms=latency_ms,
                message=exc.message,
                detail=detail,
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info("连通性测试成功 | {} | {} ms", self.label, latency_ms)
        return HealthCheckResult(
            ok=True,
            provider_type=self.provider_type,
            model=self.model_name,
            latency_ms=latency_ms,
            message="连接正常",
            detail={
                # 正文优先；没有正文时退回思考内容，避免示例是无意义的空串
                "sample_reply": (result.content or result.reasoning)[:50],
                "usage": result.usage.to_dict(),
            },
        )

    def close(self) -> None:
        """关闭 HTTP 连接池。应用退出时调用。"""
        self._client.close()

    # ==================== 内部工具 ====================
    def _normalize_exception(self, exc: BaseException, *, endpoint: str) -> LLMProviderError:
        """把底层网络异常转换成统一的语义化异常（各子类共用）。"""
        return normalize_exception(exc, provider_label=self.label, endpoint=endpoint)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.label} key={self.masked_api_key}>"
