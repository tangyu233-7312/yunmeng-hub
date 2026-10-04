"""3.4 异构大模型适配层测试。

分两类：

  1. **协议解析测试（不联网）** —— 用 httpx.MockTransport 伪造服务端响应，
     覆盖请求体构造、响应解析、SSE 流式解析、错误码归一化、重试逻辑。
     这类测试任何时候都能跑，是保证协议翻译正确的主力。

  2. **真实 API 测试（联网）** —— 需要 .env 里配置了 DEFAULT_LLM_* 才会执行，
     否则自动跳过。用于验证与真实厂商的端到端联通。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_llm.py -v
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError
from app.llm import (
    ChatMessage,
    ChatRequest,
    Role,
    TokenUsage,
    create_provider,
)
from app.llm.base import BaseLLMProvider
from app.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMModelNotFoundError,
    LLMQuotaError,
    LLMUpstreamError,
)


# ==================================================================
#  测试辅助
# ==================================================================
def make_provider(
    handler: Any,
    *,
    max_retries: int = 0,
    default_params: dict[str, Any] | None = None,
    api_key: str = "test-key-12345678",
) -> BaseLLMProvider:
    """构造一个「HTTP 层被替换成 MockTransport」的适配器。

    这样测的是真实的请求构造与响应解析逻辑，只是把网络那一层换成了假实现。
    """
    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://mock.example.com/v1",
        api_key=api_key,
        model_name="mock-model",
        extra_params=default_params,
        timeout=30,
        max_retries=max_retries,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    return provider


def json_response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def sse_response(*events: str, status_code: int = 200) -> httpx.Response:
    """把若干 SSE 行拼成一个流式响应。"""
    body = "\n\n".join(events) + "\n\n"
    return httpx.Response(
        status_code,
        content=body.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


OK_CHAT_BODY: dict[str, Any] = {
    "id": "chatcmpl-mock",
    "model": "mock-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "你好，我是测试回复。"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
}


# ==================================================================
#  一、统一数据结构
# ==================================================================
def test_message_convenience_constructors() -> None:
    assert ChatMessage.system("a").role == "system"
    assert ChatMessage.user("b").role == "user"
    assert ChatMessage.assistant("c").role == "assistant"
    assert ChatMessage.user("hi").is_system is False


def test_message_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="不支持的消息角色"):
        ChatMessage(role="robot", content="x")


def test_system_prompt_is_merged() -> None:
    """多条 system 消息应被合并成一段（Anthropic 需要这种形态）。"""
    request = ChatRequest(
        messages=[
            ChatMessage.system("你是亚瑟。"),
            ChatMessage.system("你说话简洁。"),
            ChatMessage.user("你好"),
        ]
    )
    assert request.system_prompt == "你是亚瑟。\n\n你说话简洁。"


def test_system_prompt_is_none_when_absent() -> None:
    request = ChatRequest(messages=[ChatMessage.user("你好")])
    assert request.system_prompt is None


def test_dialogue_excludes_system_messages() -> None:
    request = ChatRequest(
        messages=[
            ChatMessage.system("系统提示"),
            ChatMessage.user("用户问题"),
            ChatMessage.assistant("模型回答"),
        ]
    )
    roles = [m.role for m in request.dialogue]
    assert roles == ["user", "assistant"]


def test_resolved_model_falls_back_to_provider_model() -> None:
    """请求未指定模型时应回落到适配器绑定的模型。"""
    request = ChatRequest(messages=[ChatMessage.user("hi")])
    assert request.resolved_model("fallback-model") == "fallback-model"

    request_with_model = ChatRequest(messages=[ChatMessage.user("hi")], model="explicit-model")
    assert request_with_model.resolved_model("fallback-model") == "explicit-model"


def test_token_usage_from_openai() -> None:
    usage = TokenUsage.from_openai(
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (10, 5, 15)


def test_token_usage_from_openai_computes_total_when_missing() -> None:
    usage = TokenUsage.from_openai({"prompt_tokens": 10, "completion_tokens": 5})
    assert usage.total_tokens == 15


def test_token_usage_from_anthropic_uses_different_field_names() -> None:
    """Anthropic 的字段名与 OpenAI 不同，必须单独解析。"""
    usage = TokenUsage.from_anthropic({"input_tokens": 7, "output_tokens": 3})
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (7, 3, 10)


def test_token_usage_handles_none() -> None:
    assert TokenUsage.from_openai(None).is_empty is True
    assert TokenUsage.from_anthropic(None).is_empty is True


# ==================================================================
#  二、请求构造
# ==================================================================
def test_chat_builds_expected_request_body() -> None:
    """请求体应严格符合 OpenAI 协议。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return json_response(OK_CHAT_BODY)

    provider = make_provider(handler)
    provider.chat(
        ChatRequest(
            messages=[ChatMessage.system("你是亚瑟"), ChatMessage.user("你好")],
            temperature=0.7,
            max_tokens=100,
        )
    )

    assert captured["url"] == "https://mock.example.com/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key-12345678"
    assert captured["json"]["model"] == "mock-model"
    assert captured["json"]["temperature"] == 0.7
    assert captured["json"]["max_tokens"] == 100
    assert captured["json"]["stream"] is False
    # system 消息在 OpenAI 协议里是 messages 的一员
    assert captured["json"]["messages"][0] == {"role": "system", "content": "你是亚瑟"}


def test_api_key_can_be_empty_for_local_services() -> None:
    """本地部署（Ollama / vLLM）通常不需要密钥，此时不应发送 Authorization 头。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return json_response(OK_CHAT_BODY)

    provider = make_provider(handler, api_key="")
    provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))
    assert captured["auth"] is None


def test_request_params_override_default_params() -> None:
    """优先级：默认参数 < 请求参数。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return json_response(OK_CHAT_BODY)

    provider = make_provider(handler, default_params={"temperature": 0.1, "top_p": 0.9})
    provider.chat(ChatRequest(messages=[ChatMessage.user("hi")], temperature=0.8))

    assert captured["json"]["temperature"] == 0.8   # 请求覆盖了默认值
    assert captured["json"]["top_p"] == 0.9         # 未覆盖的沿用默认值


def test_extra_params_are_passed_through() -> None:
    """厂商特有参数应能透传（这是「不修改抽象层就能接新厂商」的关键）。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return json_response(OK_CHAT_BODY)

    provider = make_provider(handler)
    provider.chat(
        ChatRequest(
            messages=[ChatMessage.user("hi")],
            extra={"frequency_penalty": 0.5, "response_format": {"type": "json_object"}},
        )
    )
    assert captured["json"]["frequency_penalty"] == 0.5
    assert captured["json"]["response_format"] == {"type": "json_object"}


# ==================================================================
#  三、非流式响应解析
# ==================================================================
def test_chat_parses_response() -> None:
    provider = make_provider(lambda request: json_response(OK_CHAT_BODY))
    result = provider.chat(ChatRequest(messages=[ChatMessage.user("你好")]))

    assert result.content == "你好，我是测试回复。"
    assert result.finish_reason == "stop"
    assert result.model == "mock-model"
    assert result.usage.total_tokens == 20
    assert result.latency_ms >= 0


def test_chat_parses_segmented_content() -> None:
    """少数厂商把 content 返回成分段数组，也要能正确拼接。"""
    body = {
        "model": "mock-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}],
                },
                "finish_reason": "stop",
            }
        ],
    }
    provider = make_provider(lambda request: json_response(body))
    assert provider.chat(ChatRequest(messages=[ChatMessage.user("hi")])).content == "第一段第二段"


def test_chat_raises_on_empty_choices() -> None:
    provider = make_provider(lambda request: json_response({"model": "m", "choices": []}))
    with pytest.raises(LLMUpstreamError, match="缺少 choices"):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_chat_raises_when_body_is_not_json() -> None:
    provider = make_provider(
        lambda request: httpx.Response(200, content=b"<html>gateway error</html>")
    )
    with pytest.raises(LLMUpstreamError):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


# ==================================================================
#  三之二、推理模型（reasoning model）相关
#
#  背景：DeepSeek 推理系列这类模型会先把输出配额花在「思考过程」上，
#  正文字段可能是空的。以下用例来自一次真实的踩坑。
# ==================================================================
def test_usage_parses_reasoning_tokens() -> None:
    """思考 token 数藏在嵌套字段里，要能解析出来。"""
    usage = TokenUsage.from_openai(
        {
            "prompt_tokens": 36,
            "completion_tokens": 78,
            "total_tokens": 114,
            "completion_tokens_details": {"reasoning_tokens": 75},
        }
    )
    assert usage.reasoning_tokens == 75
    # 思考 token 已包含在 completion_tokens 内，不是额外开销
    assert usage.completion_tokens == 78


def test_usage_reasoning_tokens_defaults_to_zero() -> None:
    """普通模型没有该字段时不应报错。"""
    raw = {"prompt_tokens": 1, "completion_tokens": 2}
    assert TokenUsage.from_openai(raw).reasoning_tokens == 0


def test_chat_extracts_reasoning_content() -> None:
    """非流式响应里的思考过程不能被丢弃（要与流式接口能力对称）。"""
    body = {
        "model": "reasoner",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "你好呀",
                    "reasoning_content": "用户要求说三个字，我应该直接回复。",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 15},
        },
    }
    provider = make_provider(lambda request: json_response(body))
    result = provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert result.content == "你好呀"
    assert result.reasoning == "用户要求说三个字，我应该直接回复。"
    assert result.usage.reasoning_tokens == 15
    # 序列化后也应带上思考过程
    assert result.to_dict()["reasoning"]


def test_chat_raises_when_output_truncated_without_content() -> None:
    """★ 核心用例：配额被思考过程吃光导致正文为空，必须报错而不是「假装成功」。

    这正是让 deepseek-flash 返回空回复的那个场景。
    """
    body = {
        "model": "reasoner",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "让我仔细想想这个问题……",
                },
                "finish_reason": "length",
            }
        ],
        "usage": {
            "prompt_tokens": 61,
            "completion_tokens": 200,
            "total_tokens": 261,
            "completion_tokens_details": {"reasoning_tokens": 200},
        },
    }
    provider = make_provider(lambda request: json_response(body))

    with pytest.raises(LLMBadRequestError) as exc_info:
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    # 错误信息必须可操作：要告诉用户「调大 max_tokens」
    assert "max_tokens" in exc_info.value.message
    detail = exc_info.value.detail
    assert detail["finish_reason"] == "length"
    assert detail["usage"]["reasoning_tokens"] == 200
    assert "suggestion" in detail


def test_chat_raises_on_content_filter() -> None:
    """被内容安全策略拦截时要说清原因，而不是返回空字符串。"""
    body = {
        "model": "mock-model",
        "choices": [
            {"message": {"role": "assistant", "content": ""}, "finish_reason": "content_filter"}
        ],
    }
    provider = make_provider(lambda request: json_response(body))
    with pytest.raises(LLMBadRequestError, match="内容安全策略"):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_chat_raises_on_completely_empty_reply() -> None:
    """既没有正文也没有思考内容，属于上游异常。"""
    body = {
        "model": "mock-model",
        "choices": [{"message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
    }
    provider = make_provider(lambda request: json_response(body))
    with pytest.raises(LLMUpstreamError, match="空回复"):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_chat_raises_when_only_reasoning_without_truncation() -> None:
    """有思考过程但正文为空、且不是被截断 —— 同样要报错。"""
    body = {
        "model": "mock-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": "", "reasoning_content": "思考中"},
                "finish_reason": "stop",
            }
        ],
    }
    provider = make_provider(lambda request: json_response(body))
    with pytest.raises(LLMUpstreamError, match="只返回了思考过程"):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_health_check_ok_when_reasoning_consumes_budget() -> None:
    """★ 健康检查的特殊处理：思考吃光配额时，仍应判定「连接正常」。

    因为这时候「网络通、鉴权过、模型存在」这三件事都已经被证明了，
    把用户吓一跳说「配置错误」是不对的。
    """
    body = {
        "model": "reasoner",
        "choices": [
            {
                "message": {"role": "assistant", "content": "", "reasoning_content": "思考"},
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 256, "total_tokens": 261},
    }
    provider = make_provider(lambda request: json_response(body))
    result = provider.health_check()

    assert result.ok is True
    assert "推理型" in result.message


def test_health_check_uses_larger_token_budget() -> None:
    """健康检查必须给推理模型留出配额，否则必然误判。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return json_response(OK_CHAT_BODY)

    provider = make_provider(handler)
    provider.health_check()
    assert captured["json"]["max_tokens"] >= 256


# ==================================================================
#  四、流式响应解析
# ==================================================================
def test_stream_chat_collects_deltas() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            'data: {"choices":[{"delta":{"content":"你"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"好"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"呀"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        )

    provider = make_provider(handler)
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))

    text = "".join(c.delta for c in chunks)
    assert text == "你好呀"
    # 最后一个包的结束原因应被捕获
    assert chunks[-1].finish_reason == "stop"


def test_stream_chat_ignores_heartbeat_and_broken_json() -> None:
    """心跳注释行与非法 JSON 不应中断整个流。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            ": keep-alive",
            'data: {"choices":[{"delta":{"content":"正常"},"finish_reason":null}]}',
            "data: {这不是合法JSON",
            "",
            'data: {"choices":[{"delta":{"content":"内容"},"finish_reason":null}]}',
            "data: [DONE]",
        )

    provider = make_provider(handler)
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))
    assert "".join(c.delta for c in chunks) == "正常内容"


def test_stream_chat_captures_reasoning_and_usage() -> None:
    """DeepSeek-R1 这类推理模型的思考过程要能单独取出，用量也要能捕获。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            'data: {"choices":[{"delta":{"reasoning_content":"让我想想"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"答案是"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":"42"},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":9,"completion_tokens":4,"total_tokens":13}}',
            "data: [DONE]",
        )

    provider = make_provider(handler)
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))

    assert "".join(c.reasoning_delta for c in chunks) == "让我想想"
    assert "".join(c.delta for c in chunks) == "答案是42"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 13


def test_stream_chat_stops_at_done_marker() -> None:
    """[DONE] 之后的内容不应被继续消费。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response(
            'data: {"choices":[{"delta":{"content":"有效"},"finish_reason":null}]}',
            "data: [DONE]",
            'data: {"choices":[{"delta":{"content":"失效"},"finish_reason":null}]}',
        )

    provider = make_provider(handler)
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))
    assert "".join(c.delta for c in chunks) == "有效"


def test_stream_chat_raises_on_business_error_event() -> None:
    """流式过程中混入的错误包也要被识别出来。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return sse_response('data: {"error":{"message":"rate limit exceeded","type":"rate_limit"}}')

    provider = make_provider(handler)
    with pytest.raises(LLMQuotaError):
        list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))


# ==================================================================
#  五、错误码归一化（本层最重要的价值之一）
# ==================================================================
@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (401, {"error": {"message": "Incorrect API key provided"}}, LLMAuthError),
        (403, {"error": {"message": "permission denied"}}, LLMAuthError),
        (402, {"error": {"message": "insufficient balance"}}, LLMQuotaError),
        (429, {"error": {"message": "rate limit exceeded"}}, LLMQuotaError),
        (404, {"error": {"message": "model not found"}}, LLMModelNotFoundError),
        (400, {"error": {"message": "invalid parameter"}}, LLMBadRequestError),
        (500, {"error": {"message": "internal error"}}, LLMUpstreamError),
        (503, {"error": {"message": "service unavailable"}}, LLMUpstreamError),
    ],
)
def test_http_errors_are_normalized(
    status_code: int, body: dict[str, Any], expected: type[Exception]
) -> None:
    provider = make_provider(lambda request: json_response(body, status_code=status_code))
    with pytest.raises(expected):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_business_error_returned_with_http_200_is_detected() -> None:
    """★ 最坑的一种：鉴权失败却返回 HTTP 200，错误藏在响应体里。"""
    body = {"code": 40101, "msg": "鉴权失败，请检查 API Key", "data": None}
    provider = make_provider(lambda request: json_response(body, status_code=200))
    with pytest.raises(LLMAuthError):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_quota_error_guessed_from_chinese_message() -> None:
    """HTTP 状态码没有区分度时，靠错误文本兜底识别。"""
    body = {"code": 1001, "msg": "账户余额不足，请充值后再试"}
    provider = make_provider(lambda request: json_response(body, status_code=200))
    with pytest.raises(LLMQuotaError):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_connection_error_is_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = make_provider(handler)
    with pytest.raises(LLMConnectionError):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_timeout_is_normalized() -> None:
    from app.llm.errors import LLMTimeoutError

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider = make_provider(handler)
    with pytest.raises(LLMTimeoutError):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_error_detail_contains_context_but_not_api_key() -> None:
    """错误详情要能定位问题，但绝不能泄露密钥。"""
    provider = make_provider(
        lambda request: json_response({"error": {"message": "bad key"}}, status_code=401)
    )
    with pytest.raises(LLMAuthError) as exc_info:
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    detail_text = json.dumps(exc_info.value.detail, ensure_ascii=False)
    assert "test-key-12345678" not in detail_text
    assert exc_info.value.detail["provider"] == "openai_compatible/mock-model"
    assert exc_info.value.detail["http_status"] == 401


def test_repr_masks_api_key() -> None:
    provider = make_provider(lambda request: json_response(OK_CHAT_BODY))
    assert "test-key-12345678" not in repr(provider)
    assert provider.masked_api_key == "test****5678"


# ==================================================================
#  六、重试逻辑
# ==================================================================
def test_retries_on_rate_limit_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 属于可重试错误，重试后成功则不应把异常抛给上层。"""
    # 把退避等待换成空操作，避免测试真的睡好几秒
    monkeypatch.setattr("app.llm.http_client.time.sleep", lambda seconds: None)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return json_response({"error": {"message": "rate limit"}}, status_code=429)
        return json_response(OK_CHAT_BODY)

    provider = make_provider(handler, max_retries=2)
    result = provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert calls["count"] == 2
    assert result.content == "你好，我是测试回复。"


def test_does_not_retry_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 重试再多次也没用，必须立刻失败，避免白白消耗调用次数。"""
    monkeypatch.setattr("app.llm.http_client.time.sleep", lambda seconds: None)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return json_response({"error": {"message": "bad key"}}, status_code=401)

    provider = make_provider(handler, max_retries=3)
    with pytest.raises(LLMAuthError):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert calls["count"] == 1


def test_retries_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.llm.http_client.time.sleep", lambda seconds: None)

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            raise httpx.ConnectError("temporary failure", request=request)
        return json_response(OK_CHAT_BODY)

    provider = make_provider(handler, max_retries=3)
    assert provider.chat(ChatRequest(messages=[ChatMessage.user("hi")])).content


# ==================================================================
#  七、工厂与配置校验
# ==================================================================
def test_factory_creates_openai_compatible() -> None:
    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        api_key="sk-x",
        model_name="deepseek-chat",
    )
    assert provider.provider_type == "openai_compatible"
    assert provider.base_url == "https://api.deepseek.com/v1"
    provider.close()


def test_factory_normalizes_provider_type() -> None:
    """大小写与空格应被容错处理。"""
    provider = create_provider(
        provider_type="  OpenAI_Compatible  ",
        base_url="https://example.com/v1",
        api_key="k",
        model_name="m",
    )
    assert provider.provider_type == "openai_compatible"
    provider.close()


def test_factory_rejects_unknown_provider_type() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        create_provider(
            provider_type="not_a_real_protocol",
            base_url="https://example.com/v1",
            api_key="k",
            model_name="m",
        )
    assert "supported" in exc_info.value.detail


def test_factory_requires_base_url_and_model() -> None:
    with pytest.raises(ConfigurationError, match="缺少必填项"):
        create_provider(
            provider_type="openai_compatible", base_url="", api_key="k", model_name=""
        )


def test_factory_rejects_invalid_url_scheme() -> None:
    with pytest.raises(ConfigurationError, match="http"):
        create_provider(
            provider_type="openai_compatible",
            base_url="api.deepseek.com/v1",
            api_key="k",
            model_name="m",
        )


# ==================================================================
#  base_url 里混进接口路径（真实事故：火山方舟填成 …/api/v3/responses）
# ==================================================================
@pytest.mark.parametrize(
    "bad_url, expected_suggestion",
    [
        # ★ 用户真实填过的地址：Ark 的 /responses 是**另一套协议**，
        #   而本适配器会在后面拼 /chat/completions，于是报"模型不存在"
        (
            "https://ark.cn-beijing.volces.com/api/v3/responses",
            "https://ark.cn-beijing.volces.com/api/v3",
        ),
        (
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            "https://ark.cn-beijing.volces.com/api/v3",
        ),
        ("https://api.deepseek.com/chat/completions", "https://api.deepseek.com"),
        ("https://api.deepseek.com/v1/chat/completions", "https://api.deepseek.com/v1"),
    ],
)
def test_base_url_with_endpoint_path_is_rejected(
    bad_url: str, expected_suggestion: str
) -> None:
    """★ 拒绝静默降级：地址填错时必须当场报错，并给出该填成什么。

    不拦的话请求会打到 `.../responses/chat/completions` 这种不存在的路径，
    对方返回的是「指定的模型不存在，或当前 API Key 无权访问该模型」——
    把"地址拼错了"伪装成"模型名错了"，用户怎么改模型名都没用。
    """
    with pytest.raises(ConfigurationError) as exc_info:
        create_provider(
            provider_type="openai_compatible",
            base_url=bad_url,
            api_key="k",
            model_name="m",
        )

    detail = exc_info.value.detail
    assert detail["suggestion"] == expected_suggestion
    assert detail["field"] == "base_url"
    assert detail["got"] == bad_url


@pytest.mark.parametrize(
    "good_url",
    [
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://ark.cn-beijing.volces.com/api/v3",
        # 阿里云的兼容模式路径里带 compat，属于正常 base_url，不能被误拦
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://open.bigmodel.cn/api/paas/v4",
        "http://127.0.0.1:11434/v1",
        # 本地部署常见：路径段就叫 completions 的网关不存在，但 /v1 结尾必须放行
        "https://my-gateway.internal/llm/v1",
    ],
)
def test_base_url_without_endpoint_path_is_accepted(good_url: str) -> None:
    """反向断言：正常的 base_url 一个都不能被误拦（否则功能直接不可用）。"""
    provider = create_provider(
        provider_type="openai_compatible", base_url=good_url, api_key="k", model_name="m"
    )
    assert provider.base_url == good_url
    provider.close()


def test_anthropic_base_url_may_include_full_path() -> None:
    """★ 反向保护：Anthropic 适配器**允许**用户把 /v1/messages 填全。

    它自己会判断是否重复拼接（见 tests/test_anthropic.py::test_url_accepts_full_path），
    所以「base_url 里不许出现接口路径」这条检查**不能**放进基类，
    否则会把一个合法用法误拦 —— 这是加这道检查时差点踩到的坑。
    """
    provider = create_provider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com/v1/messages",
        api_key="k",
        model_name="claude-3-5-sonnet",
    )
    assert provider.base_url == "https://api.anthropic.com/v1/messages"
    provider.close()

    """下划线开头的内部元数据不能被发送到第三方服务。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return json_response(OK_CHAT_BODY)

    provider = make_provider(
        handler, default_params={"temperature": 0.5, "_note": "内部备注", "_owner": "alice"}
    )
    provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert captured["json"]["temperature"] == 0.5
    assert "_note" not in captured["json"]
    assert "_owner" not in captured["json"]


# ==================================================================
#  八、模型列表与连通性测试
# ==================================================================
def test_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/models")
        return json_response({"data": [{"id": "b-model"}, {"id": "a-model"}]})

    provider = make_provider(handler)
    assert provider.list_models() == ["a-model", "b-model"]


def test_list_models_tolerates_plain_string_list() -> None:
    provider = make_provider(lambda request: json_response({"data": ["m1", "m2"]}))
    assert provider.list_models() == ["m1", "m2"]


def test_health_check_success() -> None:
    provider = make_provider(lambda request: json_response(OK_CHAT_BODY))
    result = provider.health_check()
    assert result.ok is True
    assert result.provider_type == "openai_compatible"
    assert result.detail["usage"]["total_tokens"] == 20


def test_health_check_failure_does_not_raise() -> None:
    """连通性测试要把失败「报告出来」而不是抛异常，供「测试连接」按钮使用。"""
    provider = make_provider(
        lambda request: json_response({"error": {"message": "bad key"}}, status_code=401)
    )
    result = provider.health_check()
    assert result.ok is False
    assert "鉴权" in result.message
    assert result.detail["http_status"] == 401


# ==================================================================
#  九、真实 API 测试（需要 .env 配置 DEFAULT_LLM_*，否则跳过）
# ==================================================================
@pytest.fixture(scope="module")
def live_provider() -> BaseLLMProvider:
    """基于 .env 中的 DEFAULT_LLM_* 构造真实适配器。未配置则跳过。"""
    settings = get_settings()
    if not (
        settings.DEFAULT_LLM_BASE_URL
        and settings.DEFAULT_LLM_API_KEY
        and settings.DEFAULT_LLM_MODEL
    ):
        pytest.skip("未配置 DEFAULT_LLM_*，跳过真实 API 测试")

    provider = create_provider(
        provider_type="openai_compatible",
        base_url=settings.DEFAULT_LLM_BASE_URL,
        api_key=settings.DEFAULT_LLM_API_KEY,
        model_name=settings.DEFAULT_LLM_MODEL,
        timeout=settings.LLM_REQUEST_TIMEOUT,
        max_retries=1,
    )
    yield provider
    provider.close()


@pytest.mark.live
def test_live_health_check(live_provider: BaseLLMProvider) -> None:
    result = live_provider.health_check()
    assert result.ok is True, f"连通性测试失败: {result.message} | {result.detail}"
    assert result.latency_ms > 0


@pytest.mark.live
def test_live_chat(live_provider: BaseLLMProvider) -> None:
    result = live_provider.chat(
        ChatRequest(
            messages=[
                ChatMessage.system("你是一个简洁的助手，只回答被问到的内容。"),
                ChatMessage.user("用四个字以内回答：中国的首都是哪里？"),
            ],
            temperature=0,
            # ★ 推理模型（如 DeepSeek 推理系列）会先消耗配额做「思考」，
            #   配额给小了会得到空正文并抛错，因此真实测试必须留足余量。
            max_tokens=512,
        )
    )
    assert result.content.strip()
    assert result.usage.total_tokens > 0
    assert result.latency_ms > 0


@pytest.mark.live
def test_live_stream_chat(live_provider: BaseLLMProvider) -> None:
    """验证真实厂商的 SSE 流能被正确解析，且能拼出完整回复。"""
    chunks = list(
        live_provider.stream_chat(
            ChatRequest(
                messages=[ChatMessage.user("从 1 数到 5，只输出数字，用逗号分隔。")],
                temperature=0,
                max_tokens=512,
            )
        )
    )
    text = "".join(chunk.delta for chunk in chunks)
    assert text.strip(), "流式输出不应为空"
    assert len(chunks) >= 1


@pytest.mark.live
def test_live_wrong_api_key_raises_auth_error() -> None:
    """故意用错误的密钥，验证错误码归一对真实厂商同样有效。"""
    settings = get_settings()
    if not settings.DEFAULT_LLM_BASE_URL:
        pytest.skip("未配置 DEFAULT_LLM_*，跳过真实 API 测试")

    provider = create_provider(
        provider_type="openai_compatible",
        base_url=settings.DEFAULT_LLM_BASE_URL,
        api_key="sk-definitely-invalid-key-for-testing",
        model_name=settings.DEFAULT_LLM_MODEL or "test",
        timeout=30,
        max_retries=0,
    )
    try:
        with pytest.raises((LLMAuthError, LLMQuotaError, LLMBadRequestError)):
            provider.chat(ChatRequest(messages=[ChatMessage.user("hi")], max_tokens=8))
    finally:
        provider.close()
