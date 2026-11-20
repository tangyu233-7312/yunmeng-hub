"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations

import json
import uuid

from typing import Any

import httpx

import pytest


from app.llm import (
    ChatMessage,
    ChatRequest,
    GenerationParams,
    ProviderConfig,
    compute_context_budget,
    create_provider,
)

OK_BODY: dict[str, Any] = {
    "model": "mock-model",
    "choices": [
        {"message": {"role": "assistant", "content": "好的。"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def capture_payload(
    *,
    default_params: GenerationParams | dict[str, Any] | None = None,
    context_window: int = 65536,
    **request_kwargs: Any,
) -> dict[str, Any]:
    """构造一个假适配器，执行一次 chat，返回实际发出的请求体。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://mock.example.com/v1",
        api_key="k",
        model_name="mock-model",
        context_window=context_window,
        extra_params=default_params,
        max_retries=0,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    provider.chat(ChatRequest(messages=[ChatMessage.user("hi")], **request_kwargs))
    return captured["json"]


def test_provider_config_warns_when_reasoning_with_small_output() -> None:
    """★ 推理模型 + 输出偏小 → 正文会被思考挤没，必须提示用户。"""
    config = ProviderConfig(
        name="推理模型小输出",
        base_url="https://api.example.com",
        api_key="sk-x",
        model_name="reasoner",
        context_window=65536,
        generation=GenerationParams(max_tokens=512, reasoning_effort="high"),
    )
    warnings = config.warnings()
    assert any("思考" in w and "2048" in w for w in warnings)


def test_provider_config_no_warning_when_reasoning_off() -> None:
    """关掉思考后，小输出就不再是问题。"""
    config = ProviderConfig(
        name="关闭思考",
        base_url="https://api.example.com",
        api_key="sk-x",
        model_name="m",
        context_window=65536,
        generation=GenerationParams(max_tokens=512, reasoning_effort="off"),
    )
    assert not any("思考" in w for w in config.warnings())


def test_provider_config_warns_on_tight_input_budget() -> None:
    """输出吃掉绝大部分窗口时，剩下的输入预算放不下人设与历史，必须提示。"""
    config = ProviderConfig(
        name="预算紧张",
        base_url="https://api.example.com",
        api_key="sk-x",
        model_name="m",
        context_window=4096,
        generation=GenerationParams(max_tokens=3600),
    )
    warnings = config.warnings()
    assert config.budget.input_budget < 512
    assert any("输入预算" in w for w in warnings)


def test_provider_config_warns_on_missing_key_for_cloud() -> None:
    config = ProviderConfig(
        name="没填密钥",
        base_url="https://api.example.com",
        model_name="m",
    )
    assert any("API Key" in w for w in config.warnings())


def test_provider_config_no_key_warning_for_localhost() -> None:
    """本地部署（Ollama / vLLM）不需要密钥，不应误报。"""
    config = ProviderConfig(
        name="本地模型",
        base_url="http://localhost:11434/v1",
        model_name="qwen2.5:7b",
    )
    assert not any("API Key" in w for w in config.warnings())


def test_provider_config_to_dict_never_leaks_key() -> None:
    config = ProviderConfig(
        name="含密钥",
        base_url="https://api.example.com",
        api_key="sk-super-secret-value",
        model_name="m",
    )
    assert "sk-super-secret-value" not in json.dumps(
        config.to_dict(), ensure_ascii=False
    )


def test_auto_effort_sends_no_extra_field() -> None:
    """auto 时不发送任何思考相关字段 —— 这是最安全的默认。"""
    payload = capture_payload(default_params=GenerationParams(reasoning_effort="auto"))
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("off", "minimal")],
)
def test_effort_translated_to_openai_field(effort: str, expected: str) -> None:
    """统一思考强度应被翻译成 OpenAI 兼容协议的 reasoning_effort 字段。"""
    payload = capture_payload(default_params=GenerationParams(reasoning_effort=effort))
    assert payload["reasoning_effort"] == expected


def test_request_effort_overrides_provider_default() -> None:
    payload = capture_payload(
        default_params=GenerationParams(reasoning_effort="low"),
        reasoning_effort="high",
    )
    assert payload["reasoning_effort"] == "high"


def test_invalid_effort_in_request_falls_back_to_auto() -> None:
    """★ 请求层则相反：允许容错。

    ChatRequest.reasoning_effort 是普通字符串（可能来自 URL 参数或前端旧版本），
    不该因为一个拼写就整条请求失败，因此降级为 auto 并记日志。
    """
    payload = capture_payload(reasoning_effort="extreme")
    assert "reasoning_effort" not in payload


def test_400_error_with_reasoning_effort_gets_actionable_hint() -> None:
    """★ 部分模型不认识 reasoning_effort，会返回 400。

    错误详情里必须直接给出解法（改回 auto），而不是让用户对着英文报错发呆。
    """
    from app.llm.errors import LLMBadRequestError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {"message": "Unrecognized request argument: reasoning_effort"}
            },
        )

    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://mock.example.com/v1",
        api_key="k",
        model_name="mock-model",
        extra_params=GenerationParams(reasoning_effort="high"),
        max_retries=0,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)

    with pytest.raises(LLMBadRequestError) as exc_info:
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert "hint" in exc_info.value.detail
    assert "auto" in exc_info.value.detail["hint"]


def _reject_effort_transport(monkeypatch, calls: list[dict]) -> None:
    """让适配器第一次收到 400「不认识的参数」，去掉该参数后返回 200。"""
    import httpx

    def fake_post_with_retry(
        client, url, *, headers, payload, max_retries, provider_label
    ):
        calls.append(dict(payload))
        request = httpx.Request("POST", url)
        if "reasoning_effort" in payload:
            return httpx.Response(
                400,
                json={"error": {"message": "Unrecognized parameter: reasoning_effort"}},
                request=request,
            )
        return httpx.Response(200, json=OK_BODY, request=request)

    monkeypatch.setattr(
        "app.llm.openai_compatible.post_with_retry", fake_post_with_retry
    )


def test_rejected_effort_is_dropped_and_retried(monkeypatch) -> None:
    """★ 服务端拒绝 reasoning_effort 时：**去掉它重试一次**，并且不打断对话。

    这是删掉「探测」之后替代方案的核心保证 —— 上一版靠"我们自己去比思考 token 数"
    猜模型支不支持（不可靠，且给过错误结论）；现在只认**服务端明确回 400**这个事实。
    """
    calls: list[dict] = []
    _reject_effort_transport(monkeypatch, calls)
    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://api.example.com",
        api_key="sk-x",
        model_name="rejecting-" + uuid.uuid4().hex,
        context_window=8192,
        extra_params={"max_tokens": 1024, "reasoning_effort": "high"},
    )

    result = provider.chat(ChatRequest(messages=[ChatMessage.user("你好")]))

    assert result.content, "去掉参数重试后应当拿到正常回复"
    assert len(calls) == 2, (
        f"应当只重试一次（实际 {len(calls)} 次）：{[list(c) for c in calls]}"
    )
    assert "reasoning_effort" in calls[0], "第一次必须带上该参数（否则测不到退回逻辑）"
    assert "reasoning_effort" not in calls[1], "第二次必须去掉该参数"
    assert any("不支持" in n and "思考强度" in n for n in result.notes), (
        f"必须如实告知这次做过退回，实际 notes={result.notes}"
    )


def test_unsupported_effort_is_remembered_across_calls(monkeypatch) -> None:
    """★ 同一个模型被拒过一次之后，后续请求**不再白试**（否则每次多一次 400 往返）。"""
    calls: list[dict] = []
    _reject_effort_transport(monkeypatch, calls)
    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://api.example.com",
        api_key="sk-x",
        model_name="rejecting-" + uuid.uuid4().hex,
        context_window=8192,
        extra_params={"max_tokens": 1024, "reasoning_effort": "high"},
    )

    provider.chat(ChatRequest(messages=[ChatMessage.user("第一次")]))
    assert len(calls) == 2, "第一次：带参数被拒 → 去掉重试"

    provider.chat(ChatRequest(messages=[ChatMessage.user("第二次")]))
    assert len(calls) == 3, "第二次：应当直接不带参数（只发一次请求）"
    assert "reasoning_effort" not in calls[-1]


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "window,output,margin,remaining,usable",
    [
        (8192, 2048, 409, 5735, True),
        (1024, 256, 128, 640, True),
        (4096, 3900, 204, 0, False),
    ],
    ids=["normal", "margin-floor", "exhausted"],
)
def test_context_budget(window, output, margin, remaining, usable):
    budget = compute_context_budget(window, output)
    assert budget.safety_margin == margin
    assert budget.input_budget == remaining
    assert budget.is_usable is usable
