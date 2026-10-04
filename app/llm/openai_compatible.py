"""OpenAI 兼容协议适配器 —— 覆盖绝大多数大模型厂商。

==================== 为什么一个适配器能覆盖这么多厂商？====================
因为 OpenAI 的 /chat/completions 事实上已经成了行业通用协议，
下面的服务全都兼容它（差异只在 base_url 和模型名）：

    · DeepSeek        https://api.deepseek.com/v1
    · 阿里通义千问     https://dashscope.aliyuncs.com/compatible-mode/v1
    · 月之暗面 Kimi    https://api.moonshot.cn/v1
    · 智谱 GLM        https://open.bigmodel.cn/api/paas/v4
    · 硅基流动         https://api.siliconflow.cn/v1
    · 火山方舟         https://ark.cn-beijing.volces.com/api/v3
    · 本地 vLLM       http://localhost:8000/v1
    · 本地 Ollama     http://localhost:11434/v1
    · LM Studio       http://localhost:1234/v1

所以只要实现这一个适配器，就能接上绝大部分服务；
真正需要单独适配的，是协议差异较大的 Anthropic（见 anthropic.py）。

==================== 协议要点 ====================
请求：
    POST {base_url}/chat/completions
    Authorization: Bearer <key>
    {
      "model": "deepseek-chat",
      "messages": [{"role": "system|user|assistant", "content": "..."}],
      "temperature": 0.8, "max_tokens": 2048,
      "stream": false
    }

非流式响应：
    {"choices": [{"message": {"role": "assistant", "content": "..."},
                  "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
     "model": "deepseek-chat"}

流式响应（SSE，每行以 data: 开头）：
    data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}
    data: {"choices":[{"delta":{"content":"好"},"finish_reason":null}]}
    data: {"choices":[{"delta":{},"finish_reason":"stop"}]}
    data: [DONE]
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from loguru import logger

from app.core.exceptions import LLMProviderError
from app.llm.base import BaseLLMProvider
from app.llm.errors import (
    LLMUpstreamError,
    normalize_exception,
    normalize_http_error,
)
from app.llm.http_client import (
    backoff_seconds,
    is_retryable_exception,
    post_with_retry,
)
from app.llm.params import ReasoningEffort
from app.llm.schema import ChatRequest, ChatResult, StreamChunk, TokenUsage


class OpenAICompatibleProvider(BaseLLMProvider):
    """适配 OpenAI /chat/completions 协议的所有服务。"""

    provider_type = "openai_compatible"
    supports_model_listing = True

    CHAT_PATH = "chat/completions"
    MODELS_PATH = "models"

    def __init__(self, **kwargs: Any) -> None:
        """构造时就把「base_url 里混进了接口路径」这种配置错误拦下来。

        ★ 真实事故（用户实际反馈）：
            豆包的地址被填成 https://ark.cn-beijing.volces.com/api/v3/responses
            —— 那是火山方舟的**另一套协议**（Responses API），
            而本适配器会在这个地址后面再拼 `/chat/completions`，
            请求于是打到 `/api/v3/responses/chat/completions`。
            对方返回「指定的模型不存在，或当前 API Key 无权访问该模型」，
            把"地址拼错了"伪装成"模型名错了"，用户怎么改模型名都没用。

        ★ 为什么放在这个类而不是基类？
            只有「在 base_url 后直接拼路径」的协议才有这个陷阱。
            Anthropic 适配器自己会判断用户是否已经把 `/v1/messages` 填全，
            所以基类不加这道检查（否则会把合法用法误拦）。
        """
        super().__init__(**kwargs)
        self._reject_endpoint_in_base_url()

    #: 统一思考强度 → OpenAI 兼容协议的 reasoning_effort 取值
    #:
    #: ⚠️ 关于 OFF：OpenAI 兼容协议**没有真正的「关闭思考」取值**，
    #:    最接近的是 "minimal"（最小思考）。这一点必须在界面上如实告知用户，
    #:    不能假装关掉了。真正能关闭思考的是其他协议（如 Anthropic 的
    #:    thinking.type="disabled"），由各自的适配器实现。
    _REASONING_EFFORT_MAP: dict[ReasoningEffort, str] = {
        ReasoningEffort.OFF: "minimal",
        ReasoningEffort.LOW: "low",
        ReasoningEffort.MEDIUM: "medium",
        ReasoningEffort.HIGH: "high",
    }

    # ==================== 请求构造 ====================
    def _headers(self) -> dict[str, str]:
        """构造请求头。

        api_key 为空时不发送 Authorization —— 兼容本地部署（Ollama/vLLM 等）
        不需要鉴权的情况。
        """
        headers = {"Content-Type": "application/json"}
        if self.has_api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _apply_reasoning_effort(
        self,
        payload: dict[str, Any],
        effort: ReasoningEffort,
        *,
        max_tokens: int | None = None,
    ) -> list[str]:
        """把统一的思考强度翻译成 OpenAI 兼容协议的字段。

        auto 时完全不发送该字段 —— 这是最安全的默认：
        reasoning_effort 是较新的参数，不少模型和网关还不认识它，
        贸然发送会直接换来一个 400。

        返回值：为适配本协议所做的调整说明。
        """
        if effort is ReasoningEffort.AUTO:
            return []

        mapped = self._REASONING_EFFORT_MAP.get(effort)
        if not mapped:
            return []

        payload["reasoning_effort"] = mapped

        if effort is ReasoningEffort.OFF:
            # 必须如实告诉用户：这里并没有真正关掉思考
            return [
                "OpenAI 兼容协议没有真正的「关闭思考」取值，"
                "已映射为 minimal（最小思考），思考仍会产生但会更少"
            ]
        return []

    def _build_payload(
        self, request: ChatRequest, *, stream: bool
    ) -> tuple[dict[str, Any], list[str]]:
        """把统一的 ChatRequest 翻译成 OpenAI 协议的请求体。

        返回 (请求体, 为适配协议所做的调整说明)。
        """
        payload: dict[str, Any] = {
            "model": request.resolved_model(self.model_name),
            "messages": [message.to_dict() for message in request.messages],
        }
        # 合并生成参数（默认参数 < 请求参数 < extra 透传）
        payload.update(self._generation_params(request))

        # 显式声明是否流式。协议默认就是 false，写出来是为了让请求体自描述，
        # 排查问题时一眼就能看出这次调用是流式还是非流式。
        payload["stream"] = bool(stream)

        # ★ 思考强度必须由本适配器翻译，不能放进通用参数里 ——
        #   因为不同协议表达它的字段名和结构完全不同。
        notes = self._apply_reasoning_effort(
            payload,
            self._resolve_reasoning_effort(request),
            max_tokens=payload.get("max_tokens"),
        )

        if stream:
            # ★ 是否请求「流式返回 token 用量」：OpenAI 需要 stream_options，
            #   但部分厂商不支持这个字段并会直接报 400，
            #   所以默认不发送，需要时可通过配置的 extra_params 显式打开：
            #       {"stream_options": {"include_usage": true}}
            #   这里不做任何默认注入，保证最大兼容性。
            pass

        return payload, notes

    # ==================== 响应解析 ====================
    @staticmethod
    def _safe_json(response: Any) -> Any:
        """尽力把响应解析成 JSON；解析失败则返回原始文本。

        有些厂商出错时返回的是 HTML 错误页而不是 JSON，
        直接调用 response.json() 会抛出难以理解的异常。
        """
        try:
            return response.json()
        except ValueError:
            return response.text[:1000]

    @staticmethod
    def _loads(text: str) -> Any:
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text

    def _raise_if_business_error(self, body: Any) -> None:
        """识别「HTTP 200 但业务失败」这种最坑的返回。

        部分国产厂商即使鉴权失败或余额不足，也返回 HTTP 200，
        真正的错误藏在响应体的 code / error 字段里。
        """
        if not isinstance(body, dict):
            return
        # 有 choices 说明是正常回复
        if body.get("choices"):
            return

        error = body.get("error")
        code = body.get("code")

        # code 为 0 / "0" / 200 / None 视为正常
        code_is_ok = code in (None, 0, "0", 200, "200")
        if error is None and code_is_ok:
            return

        logger.warning("{} 返回 HTTP 200 但响应体包含业务错误: {}", self.label, str(body)[:300])
        raise normalize_http_error(200, body, provider_label=self.label)

    def _raise_for_status(
        self,
        status_code: int,
        body: Any,
        *,
        url: str,
        payload: dict[str, Any],
    ) -> None:
        """把 HTTP 错误响应转成统一异常，并补充可操作的排查提示。"""
        error = normalize_http_error(
            status_code, body, provider_label=self.label, endpoint=url
        )

        # ★ 特别提示：reasoning_effort 是较新的参数，不少模型与网关还不认识它，
        #   会直接返回 400「不支持的参数」。与其让用户对着一句英文报错发呆，
        #   不如直接给出唯一的解法。
        if status_code == 400 and "reasoning_effort" in payload:
            detail = dict(error.detail or {})
            detail["hint"] = (
                "本次请求带上了 reasoning_effort 参数（来自「思考强度」设置）。"
                "若错误信息提到不支持的参数 / unrecognized parameter，"
                "说明该模型不支持思考强度设置，请把思考强度改回 auto。"
            )
            error.detail = detail

        raise error

    def _extract_content(self, message: dict[str, Any]) -> str:
        """从 message 字段中取出正文。

        绝大多数厂商返回字符串，但少数会返回分段结构：
            [{"type": "text", "text": "..."}]
        这里两种都兼容。
        """
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        return ""

    def _extract_reasoning(self, message: dict[str, Any]) -> str:
        """取出推理模型的「思考过程」。

        不同厂商用的字段名不一致，这里都兼容：
            DeepSeek 推理系列 / 部分国产模型:  reasoning_content
            另一些实现:                        reasoning
        普通模型没有这个字段，返回空字符串。
        """
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    def _parse_chat_response(
        self, body: Any, latency_ms: int, *, notes: list[str] | None = None
    ) -> ChatResult:
        """把非流式响应翻译成统一的 ChatResult。"""
        self._raise_if_business_error(body)

        if not isinstance(body, dict):
            raise LLMUpstreamError(
                "大模型返回的不是合法 JSON",
                detail={"provider": self.label, "raw_body_preview": str(body)[:500]},
            )

        choices = body.get("choices") or []
        if not choices:
            raise LLMUpstreamError(
                "大模型返回内容为空（缺少 choices 字段）",
                detail={"provider": self.label, "raw_body_preview": str(body)[:500]},
            )

        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            message = {}

        content = self._extract_content(message)
        reasoning = self._extract_reasoning(message)
        finish_reason = choice.get("finish_reason")
        usage = TokenUsage.from_openai(body.get("usage"))

        # 拦截空回复，避免静默失败
        self._raise_if_no_content(content, reasoning, finish_reason, usage)

        return ChatResult(
            content=content,
            model=str(body.get("model") or self.model_name),
            finish_reason=finish_reason,
            reasoning=reasoning,
            notes=list(notes or []),
            usage=usage,
            latency_ms=latency_ms,
            raw=body,
        )

    # ==================== 非流式对话 ====================
    def chat(self, request: ChatRequest) -> ChatResult:
        url = self._endpoint(self.CHAT_PATH)
        payload, notes = self._build_payload(request, stream=False)

        started = time.perf_counter()
        try:
            response = post_with_retry(
                self._client,
                url,
                headers=self._headers(),
                payload=payload,
                max_retries=self.max_retries,
                provider_label=self.label,
            )
        except BaseException as exc:  # noqa: BLE001 - 统一转换后再抛
            raise self._normalize_exception(exc, endpoint=url) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code != 200:
            self._raise_for_status(
                response.status_code,
                self._safe_json(response),
                url=url,
                payload=payload,
            )

        return self._parse_chat_response(self._safe_json(response), latency_ms, notes=notes)

    # ==================== 流式对话 ====================
    def _parse_stream_event(self, event: dict[str, Any]) -> StreamChunk | None:
        """解析一个 SSE 数据包。

        不同厂商的包结构略有差异，常见的有三类：
            1. 正常内容包：choices[0].delta.content 有文字
            2. 结束包：    choices[0].finish_reason 有值，delta 为空
            3. 用量包：    只有 usage 字段，没有 choices（需要服务端开启 stream_options）
        """
        usage = TokenUsage.from_openai(event.get("usage")) if event.get("usage") else None
        choices = event.get("choices") or []

        if not choices:
            # 只有用量信息、没有内容，也要向上传递（上层需要统计 token）
            return StreamChunk(usage=usage) if usage is not None else None

        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            delta = {}

        content = delta.get("content")
        if isinstance(content, list):
            # 兼容分段结构
            content = "".join(
                item.get("text", "") for item in content if isinstance(item, dict)
            )
        if not isinstance(content, str):
            content = ""

        # 推理模型（如 DeepSeek-R1）会把思考过程放在这个字段里，与正式回复分开
        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
        if not isinstance(reasoning, str):
            reasoning = ""

        return StreamChunk(
            delta=content,
            reasoning_delta=reasoning,
            finish_reason=choice.get("finish_reason"),
            usage=usage,
        )

    def _iter_stream(self, response: Any) -> Iterator[StreamChunk]:
        """逐行读取 SSE 流并解析。"""
        for raw_line in response.iter_lines():
            if not raw_line:
                continue

            line = raw_line.strip()
            if not line:
                continue
            # 以冒号开头的是 SSE 注释（常被用作心跳保活），直接忽略
            if line.startswith(":"):
                continue
            # 只关心 data 行；event / id / retry 等字段本项目用不到
            if not line.startswith("data:"):
                continue

            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break

            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                # 个别厂商会发送非 JSON 的心跳内容，忽略即可，不应中断整个流
                logger.debug("{} 跳过无法解析的流式片段: {}", self.label, data[:120])
                continue

            if not isinstance(event, dict):
                continue

            # 流式过程中也可能混入业务错误（HTTP 200 但包体是 error）
            if event.get("error") and not event.get("choices"):
                raise normalize_http_error(200, event, provider_label=self.label)

            chunk = self._parse_stream_event(event)
            if chunk is not None:
                yield chunk

    def stream_chat(self, request: ChatRequest) -> Iterator[StreamChunk]:
        """流式对话。

        ==================== 关于重试的重要说明 ====================
        流式请求**只能在「还没吐出任何内容」时重试**。

        如果已经向前端推送了一段文字，此时连接断了再重试，
        前端就会看到「前半段 + 重新开始的完整内容」这种重复错乱的输出。
        所以这里用 emitted 标记：一旦产出过非空内容，就不再重试。
        """
        url = self._endpoint(self.CHAT_PATH)
        payload, notes = self._build_payload(request, stream=True)

        # 如果为适配协议做了调整，先发一个「元数据片段」告知调用方。
        # 它没有正文内容（delta 为空），只消费 delta 的调用方会自动忽略它。
        if notes:
            yield StreamChunk(notes=list(notes))

        emitted = False   # 是否已经产出过内容
        attempt = 0       # 已尝试次数

        while True:
            attempt += 1
            try:
                with self._client.stream(
                    "POST", url, headers=self._headers(), json=payload
                ) as response:
                    # 非 200：读取错误体后抛出（此时还没产出内容，属于可重试范畴）
                    if response.status_code != 200:
                        raw = response.read().decode("utf-8", errors="replace")
                        self._raise_for_status(
                            response.status_code, self._loads(raw), url=url, payload=payload
                        )

                    for chunk in self._iter_stream(response):
                        if not chunk.is_empty:
                            emitted = True
                        yield chunk
                    return

            except LLMProviderError:
                # 已经是语义化异常（鉴权失败 / 限流 / 超时等），交给上层处理
                raise
            except BaseException as exc:  # noqa: BLE001
                can_retry = (
                    not emitted
                    and attempt <= self.max_retries
                    and is_retryable_exception(exc)
                )
                if not can_retry:
                    raise self._normalize_exception(exc, endpoint=url) from exc

                wait = backoff_seconds(attempt)
                logger.warning(
                    "{} 流式连接中断（尚未输出内容），{:.1f} 秒后重试（第 {}/{} 次）",
                    self.label,
                    wait,
                    attempt,
                    self.max_retries,
                )
                time.sleep(wait)

    # ==================== 模型列表 ====================
    def list_models(self) -> list[str]:
        """获取可用模型列表。

        注意：并非所有厂商都实现了 /models 接口，调用失败时抛出语义化异常，
        由调用方决定是否忽略。
        """
        url = self._endpoint(self.MODELS_PATH)
        try:
            response = self._client.get(url, headers=self._headers())
        except BaseException as exc:  # noqa: BLE001
            raise normalize_exception(exc, provider_label=self.label, endpoint=url) from exc

        if response.status_code != 200:
            raise normalize_http_error(
                response.status_code,
                self._safe_json(response),
                provider_label=self.label,
                endpoint=url,
            )

        body = self._safe_json(response)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return []

        models: list[str] = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                models.append(str(item["id"]))
            elif isinstance(item, str):
                models.append(item)
        return sorted(models)
