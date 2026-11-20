"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.llm.errors import LLMBadRequestError, LLMUpstreamError

from app.llm.failover import FailoverAdapter

from app.llm.schema import ChatRequest, ChatResult, StreamChunk, TokenUsage

from app.llm.whole_reply import WholeReplyAdapter


def _request() -> ChatRequest:
    return ChatRequest(messages=[], model="m")


class _Adapter:
    """假适配器：按脚本决定"抛错"还是"正常回答"。"""

    def __init__(self, *, content="备用回答", error=None, chunks=None, model="m"):
        self.content = content
        self.error = error
        self.chunks = chunks
        self.model = model
        self.default_params = None
        self.closed = False

    @property
    def budget(self):  # pragma: no cover - 只为满足接口
        return None

    def chat(self, request):
        if self.error:
            raise self.error
        return ChatResult(content=self.content, model=self.model, finish_reason="stop")

    def stream_chat(self, request):
        if self.chunks is None:
            if self.error:
                raise self.error
            yield StreamChunk(delta=self.content)
        else:
            yield from self.chunks

    def close(self):
        self.closed = True


def _wrap(primary, fallback) -> FailoverAdapter:
    return FailoverAdapter(
        primary=primary,
        fallback=fallback,
        primary_model="main-model",
        fallback_model="backup-model",
        primary_name="主",
        fallback_name="备",
    )


def test_failover_switches_on_upstream_error() -> None:
    adapter = _wrap(
        _Adapter(error=LLMUpstreamError("上游 500")), _Adapter(content="备用的回答")
    )
    result = adapter.chat(_request())
    assert result.content == "备用的回答"
    assert adapter.used_fallback is True
    assert adapter.effective_model_name == "backup-model"
    assert any("已自动切换到备用模型" in note for note in result.notes)


def test_failover_does_not_switch_on_bad_request() -> None:
    """参数错误换个模型也是白搭：必须原样抛出，让用户去改配置。"""
    adapter = _wrap(_Adapter(error=LLMBadRequestError("参数不对")), _Adapter())
    with pytest.raises(LLMBadRequestError):
        adapter.chat(_request())
    assert adapter.used_fallback is False


def test_failover_stream_switches_only_before_first_token() -> None:
    adapter = _wrap(
        _Adapter(chunks=[StreamChunk(delta="主模型已经吐了一半")]),
        _Adapter(content="备用"),
    )
    # 主模型只吐一段就正常结束 → 不该切
    chunks = list(adapter.stream_chat(_request()))
    assert [c.delta for c in chunks] == ["主模型已经吐了一半"]
    assert adapter.used_fallback is False


def test_failover_stream_raises_when_already_emitted() -> None:
    """已经输出过内容再失败：**不能**切（否则两段不同模型的文字会拼在一起）。"""

    def exploding():
        yield StreamChunk(delta="前")
        raise LLMUpstreamError("断了")

    adapter = _wrap(_Adapter(chunks=exploding()), _Adapter())
    with pytest.raises(LLMUpstreamError):
        list(adapter.stream_chat(_request()))
    assert adapter.used_fallback is False


def test_failover_stream_switches_before_any_token() -> None:
    adapter = _wrap(
        _Adapter(error=LLMUpstreamError("一上来就挂")), _Adapter(content="备用全文")
    )
    chunks = list(adapter.stream_chat(_request()))
    assert any(c.notes and "已自动切换到备用模型" in c.notes[0] for c in chunks)
    assert "".join(c.delta or "" for c in chunks) == "备用全文"
    assert adapter.used_fallback is True


def test_whole_reply_emits_single_delta() -> None:
    inner = _Adapter(content="一整段回复")
    chunks = list(WholeReplyAdapter(inner).stream_chat(_request()))
    deltas = [c.delta for c in chunks if c.delta]
    assert deltas == ["一整段回复"], "关掉流式后必须只吐一个 delta（前端因此不会逐字）"
    assert chunks[-1].finish_reason == "stop"


def test_whole_reply_keeps_reasoning_and_usage() -> None:
    class _WithReasoning(_Adapter):
        def chat(self, request):
            return ChatResult(
                content="正文",
                model="m",
                reasoning="我先想了想",
                usage=TokenUsage(total_tokens=7),
                finish_reason="stop",
            )

    chunks = list(WholeReplyAdapter(_WithReasoning()).stream_chat(_request()))
    assert any(c.reasoning_delta == "我先想了想" for c in chunks)
    assert any(c.usage and c.usage.total_tokens == 7 for c in chunks)


pytestmark = pytest.mark.unit
