"""Anthropic Messages 协议适配器。

============================ 这个文件为什么重要？============================
它存在的意义不只是「多支持一家厂商」，而是**证明适配层抽象的是协议差异，
而不只是把 base_url 做成配置项**。

OpenAI 兼容协议和 Anthropic Messages 协议在下面这些地方**全都不一样**：

┌────────────────┬──────────────────────────────┬──────────────────────────────────┐
│ 差异点          │ OpenAI 兼容                   │ Anthropic Messages                │
├────────────────┼──────────────────────────────┼──────────────────────────────────┤
│ 端点            │ POST /chat/completions        │ POST /v1/messages                 │
│ 鉴权            │ Authorization: Bearer xxx     │ x-api-key: xxx                    │
│                 │                               │ + anthropic-version: 2023-06-01   │
│ 系统提示词      │ messages 里 role=system       │ **顶层独立参数** system            │
│ max_tokens      │ 可选                          │ **必填**                           │
│ 停止词          │ stop                          │ stop_sequences                    │
│ 消息交替        │ 无要求                        │ **必须严格 user/assistant 交替**   │
│                 │                               │ 且必须以 user 开头                 │
│ 回复正文        │ choices[0].message.content    │ content[] 数组，需按 type 过滤     │
│ 结束原因        │ finish_reason                 │ stop_reason（取值也不同）          │
│ 流式格式        │ data: {"choices":[{"delta":…}]}│ **命名事件** content_block_delta  │
│ 结束标记        │ data: [DONE]                  │ message_stop 事件                  │
│ 用量字段        │ prompt/completion_tokens      │ input/output_tokens               │
│ 思考强度        │ reasoning_effort 字段          │ thinking: {budget_tokens: N}      │
│                 │                               │ 且必须 < max_tokens                │
│ 思考与采样冲突  │ 无                            │ **开启思考时禁止改 temperature**   │
└────────────────┴──────────────────────────────┴──────────────────────────────────┘

上层的叙事引擎对这一切**零感知** —— 它照旧只写 `provider.chat(request)`。

============================ 关于「思考预算」的换算 ============================
统一层只给一个语义化的 `reasoning_effort`（low/medium/high），
Anthropic 要的却是一个具体的 token 数 `budget_tokens`，而且约束很硬：

    · budget_tokens >= 1024
    · budget_tokens < max_tokens     ← 必须严格小于

所以这里按比例换算：

    low    → max_tokens 的 25%
    medium → max_tokens 的 50%
    high   → max_tokens 的 75%
    （再夹到 [1024, max_tokens - 1] 区间内）

如果 max_tokens 太小（<= 1024）根本放不下思考预算，会直接报错并给出修改建议 ——
**绝不偷偷关掉思考**，因为用户明确要求了思考，静默降级又是一种误导。
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
    LLMBadRequestError,
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
from app.llm.schema import ChatMessage, ChatRequest, ChatResult, StreamChunk, TokenUsage


class AnthropicProvider(BaseLLMProvider):
    """适配 Anthropic Messages 协议（Claude 系列，以及兼容该协议的代理服务）。"""

    provider_type = "anthropic"
    supports_model_listing = True
    #: ★ Anthropic 只接受顶层 `system` 参数，system 消息出现在对话中间会被
    #: `_normalize_dialogue` 合并进相邻消息（等于悄悄改变了语义）。
    #: 所以这里明确声明"不支持中途 system"，让预设的深度 system 块走降级路径
    #: （转成 user + 加身份声明），并把降级如实报给用户。
    supports_mid_conversation_system = False

    MESSAGES_PATH = "messages"
    MODELS_PATH = "models"

    #: Anthropic 要求的 API 版本头。不发送会直接返回 400。
    API_VERSION = "2023-06-01"

    #: Anthropic 的 stop_reason → 本项目统一的 finish_reason
    _STOP_REASON_MAP: dict[str, str] = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "pause_turn": "stop",
        "refusal": "content_filter",
    }

    #: 思考预算占 max_tokens 的比例
    _THINKING_BUDGET_RATIO: dict[ReasoningEffort, float] = {
        ReasoningEffort.LOW: 0.25,
        ReasoningEffort.MEDIUM: 0.50,
        ReasoningEffort.HIGH: 0.75,
    }

    #: Anthropic 规定 budget_tokens 的最小值
    MIN_THINKING_BUDGET = 1024

    # ==================== 地址与请求头 ====================
    def _messages_url(self, path: str = MESSAGES_PATH) -> str:
        """拼接请求地址。

        Anthropic 的 base_url 有两种常见写法，这里都兼容：
            https://api.anthropic.com        -> https://api.anthropic.com/v1/messages
            https://api.anthropic.com/v1     -> https://api.anthropic.com/v1/messages
            https://api.anthropic.com/v1/messages  -> 原样使用（用户填了完整路径）

        这和 OpenAI 兼容协议的约定（base_url 里带版本号、直接拼 chat/completions）
        不同 —— **协议差异就是要在适配器内部消化掉，不能推给用户去记**。
        """
        base = self.base_url.rstrip("/")
        if base.endswith(f"/{path}"):
            return base
        if base.endswith("/v1"):
            return f"{base}/{path}"
        return f"{base}/v1/{path}"

    def _headers(self) -> dict[str, str]:
        """Anthropic 用 x-api-key 而不是 Authorization，且必须带版本头。"""
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": self.API_VERSION,
        }
        if self.has_api_key:
            headers["x-api-key"] = self._api_key
        return headers

    # ==================== 消息规范化 ====================
    @staticmethod
    def _normalize_dialogue(dialogue: list[ChatMessage]) -> list[dict[str, Any]]:
        """把统一消息列表转换成 Anthropic 要求的形式。

        Anthropic 对 messages 有两条硬性要求，违反会直接返回 400：
            1. 角色必须严格 user / assistant 交替出现
            2. 必须以 user 消息开头

        本项目的叙事引擎可能产生连续两条 user 消息（例如用户连发两句），
        所以这里把连续的同一角色**合并**成一条 —— 这是适配层该做的事。
        """
        if not dialogue:
            raise LLMBadRequestError(
                "Anthropic 协议要求至少有一条 user 消息",
                detail={"suggestion": "请检查消息列表是否为空"},
            )

        merged: list[dict[str, Any]] = []
        for message in dialogue:
            if merged and merged[-1]["role"] == message.role:
                # 同角色连续出现 → 合并为一条（保持语义不变）
                merged[-1]["content"] = f"{merged[-1]['content']}\n\n{message.content}"
            else:
                merged.append({"role": message.role, "content": message.content})

        if merged[0]["role"] != "user":
            raise LLMBadRequestError(
                "Anthropic 协议要求对话必须以 user 消息开头",
                detail={
                    "first_role": merged[0]["role"],
                    "suggestion": "请在消息列表最前面补一条 user 消息",
                },
            )
        return merged

    # ==================== 思考强度翻译 ====================
    def _compute_thinking_budget(self, effort: ReasoningEffort, max_tokens: int | None) -> int:
        """把语义化的思考强度换算成 Anthropic 要求的 token 预算。

        约束：MIN_THINKING_BUDGET <= budget_tokens < max_tokens
        """
        if max_tokens is None or max_tokens <= self.MIN_THINKING_BUDGET:
            raise LLMBadRequestError(
                f"Anthropic 开启思考要求 max_tokens 大于 {self.MIN_THINKING_BUDGET}，"
                f"当前为 {max_tokens}",
                detail={
                    "provider": self.label,
                    "thinking_budget_min": self.MIN_THINKING_BUDGET,
                    "max_tokens": max_tokens,
                    "suggestion": (
                        f"请把「最大输出 Token」调到 {self.MIN_THINKING_BUDGET + 512} 以上"
                        "（建议 2048 或更高），或把「思考强度」改回 auto"
                    ),
                },
            )

        ratio = self._THINKING_BUDGET_RATIO.get(effort, 0.5)
        budget = int(max_tokens * ratio)
        budget = max(self.MIN_THINKING_BUDGET, budget)
        # 必须严格小于 max_tokens，否则 Anthropic 会报参数错误
        budget = min(budget, max_tokens - 1)
        return budget

    def _apply_reasoning_effort(
        self,
        payload: dict[str, Any],
        effort: ReasoningEffort,
        *,
        max_tokens: int | None = None,
    ) -> list[str]:
        """把统一的思考强度翻译成 Anthropic 的 thinking 参数。

        返回值：为适配本协议所做的调整说明（会回报给用户）。
        """
        notes: list[str] = []

        if effort is ReasoningEffort.AUTO:
            # 不干预，用 Anthropic 自己的默认（默认不开启思考）
            return notes

        if effort is ReasoningEffort.OFF:
            # Anthropic 支持真正关闭思考 —— 这是它相对 OpenAI 兼容协议的优势
            payload["thinking"] = {"type": "disabled"}
            return notes

        budget = self._compute_thinking_budget(effort, max_tokens)
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

        # ★ Anthropic 硬性约束：开启思考时不允许修改 temperature / top_p。
        #   我们只能把它们从请求里移除（Anthropic 要求 temperature 保持默认值 1）。
        #   这属于「必须替用户做的决定」，所以一定要如实回报，不能悄悄删掉。
        removed = [key for key in ("temperature", "top_p") if key in payload]
        for key in removed:
            payload.pop(key, None)
        if removed:
            notes.append(
                "Anthropic 在开启思考时不允许修改 "
                + " / ".join(removed)
                + "，已自动从请求中移除这些参数（思考模式下 temperature 固定为 1）"
            )

        notes.append(
            f"思考预算 budget_tokens={budget}（由思考强度 {effort.value} 按 "
            f"{self._THINKING_BUDGET_RATIO.get(effort, 0.5):.0%} 换算，必须小于 max_tokens）"
        )
        return notes

    # ==================== 请求体构造 ====================
    def _build_payload(
        self, request: ChatRequest, *, stream: bool
    ) -> tuple[dict[str, Any], list[str]]:
        """把统一的 ChatRequest 翻译成 Anthropic Messages 协议的请求体。

        返回 (请求体, 为适配协议所做的调整说明)。
        """
        params = dict(self._generation_params(request))

        # max_tokens 在 Anthropic 里是**必填项**（OpenAI 协议可以省略）
        max_tokens = int(params.pop("max_tokens", self.default_params.max_tokens))
        # 停止词要改名：stop → stop_sequences
        stop = params.pop("stop", None)

        payload: dict[str, Any] = {
            "model": request.resolved_model(self.model_name),
            "max_tokens": max_tokens,
            "messages": self._normalize_dialogue(request.dialogue),
        }

        # ★ 系统提示词在 Anthropic 里是**顶层参数**，不是一条 role=system 的消息
        system_prompt = request.system_prompt
        if system_prompt:
            payload["system"] = system_prompt

        if stop:
            payload["stop_sequences"] = stop

        # 其余通用参数（temperature / top_p）与厂商特有参数直接透传
        payload.update(params)

        if stream:
            payload["stream"] = True

        # 思考强度必须由本适配器翻译；它可能会反过来移除 temperature / top_p，
        # 因此要放在最后执行。
        notes = self._apply_reasoning_effort(
            payload, self._resolve_reasoning_effort(request), max_tokens=max_tokens
        )

        return payload, notes

    # ==================== 响应解析 ====================
    @staticmethod
    def _safe_json(response: Any) -> Any:
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

    def _map_stop_reason(self, stop_reason: str | None) -> str | None:
        """把 Anthropic 的 stop_reason 归一化成统一的 finish_reason。"""
        if stop_reason is None:
            return None
        return self._STOP_REASON_MAP.get(stop_reason, stop_reason)

    @staticmethod
    def _split_content_blocks(content: Any) -> tuple[str, str]:
        """拆分 Anthropic 的 content 数组，得到 (正文, 思考内容)。

        Anthropic 的回复是一组内容块，而不是一个字符串：
            [{"type": "thinking", "thinking": "..."},
             {"type": "text", "text": "你好"}]
        必须按 type 过滤，否则会把思考内容和正文混在一起。
        """
        texts: list[str] = []
        thinking: list[str] = []

        for block in content or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                texts.append(block.get("text") or "")
            elif block_type == "thinking":
                thinking.append(block.get("thinking") or "")
            elif block_type == "redacted_thinking":
                # 被安全策略加密的思考块，只能标注无法读取
                thinking.append("[思考内容已被加密，不可读取]")

        return "".join(texts), "\n".join(part for part in thinking if part)

    def _raise_for_status(
        self, status_code: int, body: Any, *, url: str, payload: dict[str, Any]
    ) -> None:
        """把 HTTP 错误转成统一异常，并补充可操作的提示。"""
        error = normalize_http_error(
            status_code, body, provider_label=self.label, endpoint=url
        )
        detail = dict(error.detail or {})

        # 提示一：思考预算与 max_tokens 的冲突
        if status_code == 400 and isinstance(payload.get("thinking"), dict):
            if payload["thinking"].get("type") == "enabled":
                detail["hint"] = (
                    "本请求开启了思考（thinking.budget_tokens="
                    f"{payload['thinking'].get('budget_tokens')}）。Anthropic 要求它必须小于 "
                    f"max_tokens={payload.get('max_tokens')} 且不小于 "
                    f"{self.MIN_THINKING_BUDGET}。若报该参数相关错误，"
                    "请调大「最大输出 Token」或把「思考强度」改回 auto。"
                )

        # 提示二：开启思考时 temperature 被移除，若上游仍报该项，说明是其它原因
        if status_code == 400 and "temperature" in str(detail.get("upstream_message", "")):
            detail.setdefault(
                "hint",
                "Anthropic 开启思考时 temperature 必须保持默认值 1。"
                "本适配器已自动移除该参数；若仍报错，请检查是否在 extra_params 里手工传了它。",
            )

        if detail.get("hint"):
            error.detail = detail
        raise error

    def _parse_chat_response(
        self, body: Any, latency_ms: int, *, notes: list[str] | None = None
    ) -> ChatResult:
        """把非流式响应翻译成统一的 ChatResult。"""
        if not isinstance(body, dict):
            raise LLMUpstreamError(
                "Anthropic 返回的不是合法 JSON",
                detail={"provider": self.label, "raw_body_preview": str(body)[:500]},
            )

        # Anthropic 的错误结构：{"type": "error", "error": {...}}
        if body.get("type") == "error" or (body.get("error") and not body.get("content")):
            raise normalize_http_error(200, body, provider_label=self.label)

        content = body.get("content")
        if content is None:
            raise LLMUpstreamError(
                "Anthropic 返回内容为空（缺少 content 字段）",
                detail={"provider": self.label, "raw_body_preview": str(body)[:500]},
            )

        text, thinking = self._split_content_blocks(content)
        finish_reason = self._map_stop_reason(body.get("stop_reason"))
        usage = TokenUsage.from_anthropic(body.get("usage"))

        # 复用基类的空回复拦截（与 OpenAI 适配器同一套逻辑）
        self._raise_if_no_content(text, thinking, finish_reason, usage)

        return ChatResult(
            content=text,
            model=str(body.get("model") or self.model_name),
            finish_reason=finish_reason,
            reasoning=thinking,
            notes=list(notes or []),
            usage=usage,
            latency_ms=latency_ms,
            raw=body,
        )

    # ==================== 非流式对话 ====================
    def chat(self, request: ChatRequest) -> ChatResult:
        url = self._messages_url()
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
        except BaseException as exc:  # noqa: BLE001
            raise self._normalize_exception(exc, endpoint=url) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code != 200:
            self._raise_for_status(
                response.status_code, self._safe_json(response), url=url, payload=payload
            )

        return self._parse_chat_response(
            self._safe_json(response), latency_ms, notes=notes
        )

    # ==================== 流式对话 ====================
    def _parse_stream_event(self, event_type: str | None, event: dict[str, Any]) -> StreamChunk | None:
        """解析一个 Anthropic 命名事件。

        与 OpenAI 的「每行都是完整增量」不同，Anthropic 的事件是**有类型**的：
            message_start        消息开始，带 input_tokens
            content_block_start  一个内容块开始（text 或 thinking）
            content_block_delta  内容块增量（text_delta / thinking_delta）
            content_block_stop   内容块结束
            message_delta        消息级更新，带 stop_reason 与 output_tokens
            message_stop         整个消息结束
            ping                 心跳
        """
        if event_type == "message_start":
            message = event.get("message") or {}
            usage = TokenUsage.from_anthropic(message.get("usage"))
            return StreamChunk(usage=usage) if not usage.is_empty else None

        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                return StreamChunk(delta=delta.get("text") or "")
            if delta_type == "thinking_delta":
                return StreamChunk(reasoning_delta=delta.get("thinking") or "")
            # input_json_delta / signature_delta 等本项目暂不需要
            return None

        if event_type == "message_delta":
            delta = event.get("delta") or {}
            usage = TokenUsage.from_anthropic(event.get("usage")) if event.get("usage") else None
            return StreamChunk(
                finish_reason=self._map_stop_reason(delta.get("stop_reason")),
                usage=usage,
            )

        if event_type == "error":
            # 流中途出错，Anthropic 会发一个 error 事件
            raise normalize_http_error(200, event, provider_label=self.label)

        return None

    def _iter_stream(self, response: Any) -> Iterator[StreamChunk]:
        """逐行读取 Anthropic 的 SSE 流。

        需要同时跟踪 `event:` 行与 `data:` 行 —— 这是与 OpenAI 协议最大的形式差异。
        不过 JSON 体内部也有 "type" 字段，两者取其一即可，这里优先用 JSON 里的。
        """
        current_event: str | None = None
        # message_start 里只有 input_tokens，output_tokens 在 message_delta 才给出，
        # 所以要把输入 token 暂存起来，最后合并成完整用量。
        prompt_tokens = 0

        for raw_line in response.iter_lines():
            if not raw_line:
                continue
            line = raw_line.strip()
            if not line or line.startswith(":"):
                continue

            if line.startswith("event:"):
                current_event = line[len("event:") :].strip()
                continue

            if not line.startswith("data:"):
                continue

            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue

            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                logger.debug("{} 跳过无法解析的流式片段: {}", self.label, data[:120])
                continue

            if not isinstance(event, dict):
                continue

            event_type = event.get("type") or current_event

            # 记录输入 token（只在 message_start 出现）
            if event_type == "message_start":
                message = event.get("message") or {}
                prompt_tokens = int((message.get("usage") or {}).get("input_tokens") or 0)

            chunk = self._parse_stream_event(event_type, event)
            if chunk is not None:
                # 把输入 token 补进用量里，让上层拿到完整数据
                if (
                    event_type == "message_delta"
                    and chunk.usage is not None
                    and prompt_tokens
                ):
                    chunk.usage.prompt_tokens = prompt_tokens
                    chunk.usage.total_tokens = prompt_tokens + chunk.usage.completion_tokens
                yield chunk

            if event_type == "message_stop":
                break

    def stream_chat(self, request: ChatRequest) -> Iterator[StreamChunk]:
        """流式对话。

        与非流式路径一样：**只在尚未吐出任何内容时才允许重试**，
        避免前端看到重复错乱的文本。
        """
        url = self._messages_url()
        payload, notes = self._build_payload(request, stream=True)

        if notes:
            yield StreamChunk(notes=list(notes))

        emitted = False
        attempt = 0

        while True:
            attempt += 1
            try:
                with self._client.stream(
                    "POST", url, headers=self._headers(), json=payload
                ) as response:
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
        """获取可用模型列表。Anthropic 的 /v1/models 返回结构与 OpenAI 类似。"""
        url = self._messages_url(self.MODELS_PATH)
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
