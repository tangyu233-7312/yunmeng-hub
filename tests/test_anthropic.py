"""3.4c Anthropic Messages 协议适配器测试。

由于本项目暂时没有 Anthropic 的 API Key，这里全部使用 httpx.MockTransport
伪造服务端响应，按官方文档的请求 / 响应 / SSE 事件结构进行验证。

重点覆盖「协议差异」的部分 —— 这正是本适配器存在的意义：
    · 系统提示词是顶层参数，不是一条消息
    · max_tokens 必填
    · 消息必须严格 user/assistant 交替，且以 user 开头
    · 回复是 content 数组，要按 type 过滤
    · 流式使用命名事件，而不是每行一个增量
    · 思考强度用 thinking.budget_tokens 表达，且必须小于 max_tokens
    · 开启思考时不允许修改 temperature / top_p

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_anthropic.py -v
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.exceptions import ConfigurationError
from app.llm import (
    ChatMessage,
    ChatRequest,
    GenerationParams,
    ReasoningEffort,
    create_provider,
)
from app.llm.anthropic import AnthropicProvider
from app.llm.base import BaseLLMProvider
from app.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMModelNotFoundError,
    LLMQuotaError,
    LLMUpstreamError,
)

OK_BODY: dict[str, Any] = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": "你好，我是测试回复。"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 8},
}


# ==================================================================
#  测试辅助
# ==================================================================
def make_anthropic(
    handler: Any,
    *,
    max_retries: int = 0,
    params: GenerationParams | dict[str, Any] | None = None,
    base_url: str = "https://api.anthropic.com/v1",
    api_key: str = "sk-ant-test-key",
) -> BaseLLMProvider:
    provider = create_provider(
        provider_type="anthropic",
        base_url=base_url,
        api_key=api_key,
        model_name="claude-test",
        extra_params=params,
        max_retries=max_retries,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    return provider


def capture(
    *,
    params: GenerationParams | dict[str, Any] | None = None,
    base_url: str = "https://api.anthropic.com/v1",
    api_key: str = "sk-ant-test-key",
    body: dict[str, Any] | None = None,
    **request_kwargs: Any,
) -> dict[str, Any]:
    """执行一次 chat，返回 (请求体, 请求头, 请求地址)。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json=body or OK_BODY)

    provider = make_anthropic(handler, params=params, base_url=base_url, api_key=api_key)
    provider.chat(ChatRequest(messages=[ChatMessage.user("hi")], **request_kwargs))
    return captured


def sse(*events: tuple[str, str], status_code: int = 200) -> httpx.Response:
    """把 (事件名, JSON 体) 序列拼成 Anthropic 风格的 SSE 响应。"""
    lines: list[str] = []
    for name, data in events:
        lines.append(f"event: {name}")
        lines.append(f"data: {data}")
        lines.append("")
    raw = "\n".join(lines) + "\n"
    return httpx.Response(
        status_code, content=raw.encode("utf-8"), headers={"content-type": "text/event-stream"}
    )


# ==================================================================
#  一、地址与请求头（协议差异）
# ==================================================================
def test_url_appends_v1_when_missing() -> None:
    """用户只填 https://api.anthropic.com 时也要能正确拼出 /v1/messages。

    这与 OpenAI 兼容协议的约定不同（那边要求 base_url 自带版本号），
    但协议差异应该在适配器内部消化，不该推给用户去记。
    """
    captured = capture(base_url="https://api.anthropic.com")
    assert captured["url"] == "https://api.anthropic.com/v1/messages"


def test_url_keeps_existing_v1() -> None:
    captured = capture(base_url="https://api.anthropic.com/v1")
    assert captured["url"] == "https://api.anthropic.com/v1/messages"


def test_url_accepts_full_path() -> None:
    """用户直接填了完整路径时不应重复拼接。"""
    captured = capture(base_url="https://api.anthropic.com/v1/messages")
    assert captured["url"] == "https://api.anthropic.com/v1/messages"


def test_headers_use_x_api_key_and_version() -> None:
    """★ 鉴权头与 OpenAI 完全不同：用 x-api-key，且必须带 anthropic-version。"""
    captured = capture()
    headers = {k.lower(): v for k, v in captured["headers"].items()}

    assert headers["x-api-key"] == "sk-ant-test-key"
    assert headers["anthropic-version"] == "2023-06-01"
    # 绝不能同时发 Authorization
    assert "authorization" not in headers


def test_no_api_key_header_when_empty() -> None:
    """本地代理可能不需要密钥。"""
    captured = capture(api_key="")
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert "x-api-key" not in headers


# ==================================================================
#  二、请求体构造（协议差异）
# ==================================================================
def test_system_prompt_is_top_level_not_a_message() -> None:
    """★ Anthropic 的系统提示词是顶层参数，不是 role=system 的消息。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    provider = make_anthropic(handler)
    provider.chat(
        ChatRequest(
            messages=[
                ChatMessage.system("你是亚瑟。"),
                ChatMessage.system("说话简洁。"),
                ChatMessage.user("你好"),
            ]
        )
    )

    body = captured["json"]
    assert body["system"] == "你是亚瑟。\n\n说话简洁。"
    # messages 里不能出现 system 角色
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert body["messages"][0]["content"] == "你好"


def test_max_tokens_is_always_present() -> None:
    """★ Anthropic 的 max_tokens 是必填项，不给会直接 400。"""
    captured = capture(params=GenerationParams(max_tokens=1234))
    assert captured["json"]["max_tokens"] == 1234


def test_stop_is_renamed_to_stop_sequences() -> None:
    """字段改名：stop → stop_sequences。"""
    captured = capture(stop=["\n\n", "END"])
    assert captured["json"]["stop_sequences"] == ["\n\n", "END"]
    assert "stop" not in captured["json"]


def test_consecutive_same_role_messages_are_merged() -> None:
    """★ Anthropic 要求消息严格交替；连续的 user 消息必须合并。

    叙事引擎完全可能产生「用户连发两条」的情况，适配层要负责处理。
    """
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    provider = make_anthropic(handler)
    provider.chat(
        ChatRequest(
            messages=[
                ChatMessage.user("第一句"),
                ChatMessage.user("第二句"),
                ChatMessage.assistant("回应"),
            ]
        )
    )

    messages = captured["json"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "第一句\n\n第二句"


def test_dialogue_must_start_with_user() -> None:
    """以 assistant 开头会被 Anthropic 拒绝，这里提前给出可操作错误。"""
    provider = make_anthropic(lambda request: httpx.Response(200, json=OK_BODY))
    with pytest.raises(LLMBadRequestError, match="必须以 user 消息开头"):
        provider.chat(ChatRequest(messages=[ChatMessage.assistant("我先说")]))


def test_empty_dialogue_is_rejected() -> None:
    provider = make_anthropic(lambda request: httpx.Response(200, json=OK_BODY))
    with pytest.raises(LLMBadRequestError, match="至少有一条 user 消息"):
        provider.chat(ChatRequest(messages=[ChatMessage.system("只有系统提示词")]))


def test_stream_flag_is_sent() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return sse(("message_stop", '{"type":"message_stop"}'))

    provider = make_anthropic(handler)
    list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))
    assert captured["json"]["stream"] is True


# ==================================================================
#  三、思考强度的翻译（本适配器最有价值的部分）
# ==================================================================
def test_auto_sends_no_thinking_field() -> None:
    captured = capture(params=GenerationParams(reasoning_effort="auto"))
    assert "thinking" not in captured["json"]
    # auto 时温度正常保留
    assert "temperature" in captured["json"]


def test_off_truly_disables_thinking() -> None:
    """★ Anthropic 能**真正**关闭思考 —— 这是它相对 OpenAI 兼容协议的优势。"""
    captured = capture(
        params=GenerationParams(reasoning_effort="off", temperature=0.9, max_tokens=4096)
    )
    assert captured["json"]["thinking"] == {"type": "disabled"}
    # 关闭思考时温度不受限制，应正常保留
    assert captured["json"]["temperature"] == 0.9


@pytest.mark.parametrize(
    ("effort", "ratio"),
    [("low", 0.25), ("medium", 0.50), ("high", 0.75)],
)
def test_effort_maps_to_budget_tokens(effort: str, ratio: float) -> None:
    """★ 语义化的强度被换算成具体的 token 预算。"""
    captured = capture(
        params=GenerationParams(reasoning_effort=effort, max_tokens=4096, temperature=0.9)
    )
    thinking = captured["json"]["thinking"]
    assert thinking["type"] == "enabled"
    assert thinking["budget_tokens"] == int(4096 * ratio)


def test_thinking_budget_is_always_below_max_tokens() -> None:
    """★ Anthropic 的硬约束：budget_tokens 必须严格小于 max_tokens。"""
    captured = capture(
        params=GenerationParams(reasoning_effort="high", max_tokens=1100)
    )
    thinking = captured["json"]["thinking"]
    assert thinking["budget_tokens"] < captured["json"]["max_tokens"]
    # 同时不能低于 Anthropic 规定的最小值
    assert thinking["budget_tokens"] >= AnthropicProvider.MIN_THINKING_BUDGET


def test_thinking_enabled_removes_temperature_and_top_p() -> None:
    """★ 开启思考时 Anthropic 不允许改 temperature / top_p，必须自动移除。"""
    captured = capture(
        params=GenerationParams(
            reasoning_effort="high", max_tokens=4096, temperature=0.9, top_p=0.8
        )
    )
    assert "temperature" not in captured["json"]
    assert "top_p" not in captured["json"]


def test_removal_of_temperature_is_reported_to_caller() -> None:
    """★ 替用户做的决定必须如实回报，不能悄悄删掉。

    否则用户会以为「我设的温度生效了」，实际并没有。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=OK_BODY)

    provider = make_anthropic(
        handler,
        params=GenerationParams(reasoning_effort="high", max_tokens=4096, temperature=0.9),
    )
    result = provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert result.notes, "应当回报为适配协议所做的调整"
    joined = " ".join(result.notes)
    assert "temperature" in joined
    assert "已自动从请求中移除" in joined
    # 同时说明思考预算的换算依据
    assert any("budget_tokens" in note for note in result.notes)


def test_small_max_tokens_with_thinking_raises_actionable_error() -> None:
    """★ max_tokens 放不下思考预算时，直接报错而不是偷偷关掉思考。"""
    provider = make_anthropic(
        lambda request: httpx.Response(200, json=OK_BODY),
        params=GenerationParams(reasoning_effort="high", max_tokens=512),
    )
    with pytest.raises(LLMBadRequestError) as exc_info:
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert "2048" in exc_info.value.detail["suggestion"]
    assert "auto" in exc_info.value.detail["suggestion"]


def test_request_effort_overrides_provider_default() -> None:
    captured = capture(
        params=GenerationParams(reasoning_effort="low", max_tokens=4096),
        reasoning_effort="high",
    )
    assert captured["json"]["thinking"]["budget_tokens"] == int(4096 * 0.75)


# ==================================================================
#  四、响应解析（协议差异）
# ==================================================================
def test_parses_text_content_block() -> None:
    provider = make_anthropic(lambda request: httpx.Response(200, json=OK_BODY))
    result = provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert result.content == "你好，我是测试回复。"
    assert result.model == "claude-test"
    # stop_reason 被归一化
    assert result.finish_reason == "stop"
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 8
    assert result.usage.total_tokens == 20


def test_thinking_block_is_separated_from_text() -> None:
    """★ content 是数组，必须按 type 过滤，否则思考与正文会混在一起。"""
    body = dict(OK_BODY)
    body["content"] = [
        {"type": "thinking", "thinking": "用户只是打了个招呼，我该简单回应。"},
        {"type": "text", "text": "你好！"},
    ]
    provider = make_anthropic(lambda request: httpx.Response(200, json=body))
    result = provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert result.content == "你好！"
    assert result.reasoning == "用户只是打了个招呼，我该简单回应。"


def test_multiple_text_blocks_are_concatenated() -> None:
    body = dict(OK_BODY)
    body["content"] = [
        {"type": "text", "text": "前半段"},
        {"type": "text", "text": "后半段"},
    ]
    provider = make_anthropic(lambda request: httpx.Response(200, json=body))
    assert provider.chat(ChatRequest(messages=[ChatMessage.user("hi")])).content == "前半段后半段"


def test_redacted_thinking_is_marked() -> None:
    """被安全策略加密的思考块读不到内容，但要标注出来而不是静默忽略。"""
    body = dict(OK_BODY)
    body["content"] = [
        {"type": "redacted_thinking", "data": "xxxx"},
        {"type": "text", "text": "回答"},
    ]
    provider = make_anthropic(lambda request: httpx.Response(200, json=body))
    result = provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))
    assert "已被加密" in result.reasoning


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("refusal", "content_filter"),
        (None, None),
    ],
)
def test_stop_reason_is_normalized(stop_reason: str | None, expected: str | None) -> None:
    """★ Anthropic 的 stop_reason 取值与 OpenAI 的 finish_reason 完全不同。"""
    body = dict(OK_BODY)
    body["stop_reason"] = stop_reason
    provider = make_anthropic(lambda request: httpx.Response(200, json=body))
    result = provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))
    assert result.finish_reason == expected


def test_empty_content_block_raises() -> None:
    body = dict(OK_BODY)
    body["content"] = []
    provider = make_anthropic(lambda request: httpx.Response(200, json=body))
    with pytest.raises(LLMUpstreamError, match="空回复"):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_truncated_without_text_raises_actionable_error() -> None:
    """思考吃光配额导致正文为空 —— 与 OpenAI 适配器共用同一套拦截逻辑。"""
    body = dict(OK_BODY)
    body["content"] = [{"type": "thinking", "thinking": "想了很久……"}]
    body["stop_reason"] = "max_tokens"
    body["usage"] = {"input_tokens": 10, "output_tokens": 200}
    provider = make_anthropic(lambda request: httpx.Response(200, json=body))

    with pytest.raises(LLMBadRequestError, match="max_tokens"):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_error_body_with_http_200_is_detected() -> None:
    """Anthropic 的错误结构是 {"type":"error","error":{...}}。"""
    body = {
        "type": "error",
        "error": {"type": "authentication_error", "message": "invalid x-api-key"},
    }
    provider = make_anthropic(lambda request: httpx.Response(200, json=body))
    with pytest.raises(LLMAuthError):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


# ==================================================================
#  五、流式（命名事件 —— 与 OpenAI 形式差异最大）
# ==================================================================
def test_stream_collects_text_deltas() -> None:
    """★ 必须能解析 Anthropic 的命名事件流。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            ("message_start", json.dumps({
                "type": "message_start",
                "message": {"usage": {"input_tokens": 9, "output_tokens": 1}},
            })),
            ("content_block_start", '{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}'),
            ("content_block_delta", '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你"}}'),
            ("content_block_delta", '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"好"}}'),
            ("content_block_stop", '{"type":"content_block_stop","index":0}'),
            ("message_delta", '{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":5}}'),
            ("message_stop", '{"type":"message_stop"}'),
        )

    provider = make_anthropic(handler)
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))

    assert "".join(c.delta for c in chunks) == "你好"
    assert chunks[-1].finish_reason == "stop"


def test_stream_separates_thinking_from_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            ("message_start", '{"type":"message_start","message":{"usage":{"input_tokens":5}}}'),
            ("content_block_delta", '{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"让我想想"}}'),
            ("content_block_delta", '{"type":"content_block_delta","delta":{"type":"text_delta","text":"答案是42"}}'),
            ("message_stop", '{"type":"message_stop"}'),
        )

    provider = make_anthropic(handler)
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))

    assert "".join(c.reasoning_delta for c in chunks) == "让我想想"
    assert "".join(c.delta for c in chunks) == "答案是42"


def test_stream_merges_input_and_output_tokens() -> None:
    """★ 输入 token 在 message_start，输出 token 在 message_delta —— 要合并成完整用量。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            ("message_start", '{"type":"message_start","message":{"usage":{"input_tokens":30}}}'),
            ("content_block_delta", '{"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}'),
            ("message_delta", '{"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}'),
            ("message_stop", '{"type":"message_stop"}'),
        )

    provider = make_anthropic(handler)
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))

    final_usage = chunks[-1].usage
    assert final_usage is not None
    assert final_usage.prompt_tokens == 30
    assert final_usage.completion_tokens == 7
    assert final_usage.total_tokens == 37


def test_stream_ignores_ping_and_unknown_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            ("ping", '{"type":"ping"}'),
            ("content_block_delta", '{"type":"content_block_delta","delta":{"type":"text_delta","text":"正常"}}'),
            ("content_block_stop", '{"type":"content_block_stop","index":0}'),
            ("message_stop", '{"type":"message_stop"}'),
        )

    provider = make_anthropic(handler)
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))
    assert "".join(c.delta for c in chunks) == "正常"


def test_stream_raises_on_error_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            ("content_block_delta", '{"type":"content_block_delta","delta":{"type":"text_delta","text":"开始"}}'),
            ("error", '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'),
        )

    provider = make_anthropic(handler)
    with pytest.raises(LLMUpstreamError):
        list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))


def test_stream_emits_notes_as_first_meta_chunk() -> None:
    """★ 为适配协议做的调整要通过流的第一个元数据片段告知调用方。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return sse(
            ("content_block_delta", '{"type":"content_block_delta","delta":{"type":"text_delta","text":"正文"}}'),
            ("message_stop", '{"type":"message_stop"}'),
        )

    provider = make_anthropic(
        handler,
        params=GenerationParams(reasoning_effort="high", max_tokens=4096, temperature=0.9),
    )
    chunks = list(provider.stream_chat(ChatRequest(messages=[ChatMessage.user("hi")])))

    first = chunks[0]
    assert first.delta == ""          # 元数据片段没有正文
    assert first.is_empty is True     # 只关心 delta 的调用方会自动忽略它
    assert first.notes
    assert "temperature" in " ".join(first.notes)


# ==================================================================
#  六、错误归一化
# ==================================================================
@pytest.mark.parametrize(
    ("status_code", "error_type", "expected"),
    [
        (401, "authentication_error", LLMAuthError),
        (403, "permission_error", LLMAuthError),
        # 404 应当映射到更精确的「模型不存在」，而不是笼统的上游异常
        (404, "not_found_error", LLMModelNotFoundError),
        (429, "rate_limit_error", LLMQuotaError),
        (500, "api_error", LLMUpstreamError),
        # 529 是 Anthropic 特有的「过载」，属于服务端临时状态
        (529, "overloaded_error", LLMUpstreamError),
    ],
)
def test_anthropic_errors_are_normalized(
    status_code: int, error_type: str, expected: type[Exception]
) -> None:
    """★ Anthropic 的错误类型与状态码组合要能正确归类。"""
    body = {"type": "error", "error": {"type": error_type, "message": "something failed"}}
    provider = make_anthropic(lambda request: httpx.Response(status_code, json=body))
    with pytest.raises(expected):
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))


def test_400_with_thinking_gets_actionable_hint() -> None:
    """思考预算相关报错要直接给出解法。"""
    body = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "thinking.budget_tokens: error"},
    }
    provider = make_anthropic(
        lambda request: httpx.Response(400, json=body),
        params=GenerationParams(reasoning_effort="high", max_tokens=4096),
    )
    with pytest.raises(LLMBadRequestError) as exc_info:
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert "thinking" in exc_info.value.detail.get("hint", "")
    assert "auto" in exc_info.value.detail["hint"]


def test_api_key_not_leaked_in_error_detail() -> None:
    body = {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
    provider = make_anthropic(lambda request: httpx.Response(401, json=body))
    with pytest.raises(LLMAuthError) as exc_info:
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert "sk-ant-test-key" not in json.dumps(exc_info.value.detail, ensure_ascii=False)


# ==================================================================
#  七、模型列表与工厂注册
# ==================================================================
def test_list_models() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith("/v1/models")
        assert request.headers.get("anthropic-version") == "2023-06-01"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"},
                    {"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"},
                ]
            },
        )

    provider = make_anthropic(handler)
    assert provider.list_models() == ["claude-haiku-4-5", "claude-sonnet-4-5"]


def test_factory_creates_anthropic_provider() -> None:
    provider = create_provider(
        provider_type="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-x",
        model_name="claude-sonnet-4-5",
    )
    assert isinstance(provider, AnthropicProvider)
    assert provider.provider_type == "anthropic"
    provider.close()


def test_provider_type_is_registered_in_catalog() -> None:
    from app.llm import PROVIDER_TYPES

    assert "anthropic" in PROVIDER_TYPES
    assert "OpenAI" in PROVIDER_TYPES["openai_compatible"]


def test_factory_from_config_with_anthropic() -> None:
    from app.llm import ProviderConfig, create_provider_from_config

    config = ProviderConfig(
        name="我的 Claude",
        provider_type="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-x",
        model_name="claude-sonnet-4-5",
        context_window=200000,
        generation=GenerationParams(max_tokens=8192, reasoning_effort="medium"),
    )
    provider = create_provider_from_config(config)
    assert isinstance(provider, AnthropicProvider)
    assert provider.budget.max_output_tokens == 8192
    provider.close()


def test_unknown_provider_type_still_rejected() -> None:
    with pytest.raises(ConfigurationError):
        create_provider(
            provider_type="gemini",
            base_url="https://example.com",
            api_key="k",
            model_name="m",
        )
