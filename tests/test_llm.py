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

from typing import Any

import pytest

from app.core.config import get_settings
from app.llm import (
    ChatMessage,
    ChatRequest,
    create_provider,
)
from app.llm.base import BaseLLMProvider
from app.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMQuotaError,
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


pytestmark = pytest.mark.live
