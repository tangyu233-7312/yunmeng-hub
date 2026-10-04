"""备用模型自动切换（failover）与「流式传输」开关。

分两层：
  · 适配器包装（FailoverAdapter / WholeReplyAdapter）是纯逻辑，直接单测；
  · 接口层校验（备用模型必须是自己的另一个配置）走真实 API。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.llm.errors import LLMBadRequestError, LLMUpstreamError
from app.llm.failover import FailoverAdapter
from app.llm.schema import ChatRequest, ChatResult, StreamChunk, TokenUsage
from app.llm.whole_reply import WholeReplyAdapter
from app.main import app

PROVIDERS = "/api/v1/providers"


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


# ==================================================================
#  一、FailoverAdapter
# ==================================================================
def test_failover_switches_on_upstream_error() -> None:
    adapter = _wrap(_Adapter(error=LLMUpstreamError("上游 500")), _Adapter(content="备用的回答"))
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
    adapter = _wrap(_Adapter(error=LLMUpstreamError("一上来就挂")), _Adapter(content="备用全文"))
    chunks = list(adapter.stream_chat(_request()))
    assert any(c.notes and "已自动切换到备用模型" in c.notes[0] for c in chunks)
    assert "".join(c.delta or "" for c in chunks) == "备用全文"
    assert adapter.used_fallback is True


# ==================================================================
#  二、WholeReplyAdapter（「流式传输」关掉后的"整段返回"）
# ==================================================================
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


# ==================================================================
#  三、接口层：备用模型的归属校验 + 流式开关
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {"username": f"pv_{token}", "email": f"pv_{token}@example.com", "password": "Test-Passw0rd!"}
    created = client.post("/api/v1/auth/register", json=account)
    assert created.status_code == 201, created.text
    logged = client.post(
        "/api/v1/auth/login", json={"username": account["username"], "password": account["password"]}
    )
    return {"headers": {"Authorization": f"Bearer {logged.json()['data']['access_token']}"}}


def _provider(client: TestClient, user: dict, name: str, **extra) -> dict:
    body = {
        "name": name,
        "provider_type": "openai_compatible",
        "base_url": "https://mock.invalid/v1",
        "api_key": "sk-test",
        "model_name": "mock-model",
        "context_window": 8192,
        "generation": {"temperature": 0.8, "max_tokens": 1024},
    }
    body.update(extra)
    return client.post(PROVIDERS, json=body, headers=user["headers"])


def test_fallback_rejects_self_and_foreign(client: TestClient) -> None:
    user = _user(client)
    mine = _provider(client, user, f"主_{uuid.uuid4().hex[:6]}").json()["data"]
    other = _user(client)
    foreign = _provider(client, other, f"别人的_{uuid.uuid4().hex[:6]}").json()["data"]

    # 指向"自己另一个配置"→ 允许（新建时也能直接绑定）
    bound_ok = _provider(
        client, user, f"绑定_{uuid.uuid4().hex[:6]}", fallback_provider_id=mine["id"]
    )
    assert bound_ok.status_code == 201, bound_ok.text
    # 自指 / 指向别人的配置 → 400（在保存时就报，而不是等主模型挂了才发现）
    bad_self = client.patch(
        f"{PROVIDERS}/{mine['id']}", json={"fallback_provider_id": mine["id"]}, headers=user["headers"]
    )
    assert bad_self.status_code == 400, bad_self.text
    bad_foreign = client.patch(
        f"{PROVIDERS}/{mine['id']}",
        json={"fallback_provider_id": foreign["id"]},
        headers=user["headers"],
    )
    assert bad_foreign.status_code == 400, bad_foreign.text


def test_fallback_can_be_cleared_with_null(client: TestClient) -> None:
    user = _user(client)
    main = _provider(client, user, f"主_{uuid.uuid4().hex[:6]}").json()["data"]
    backup = _provider(client, user, f"备_{uuid.uuid4().hex[:6]}").json()["data"]

    bound = client.patch(
        f"{PROVIDERS}/{main['id']}",
        json={"fallback_provider_id": backup["id"]},
        headers=user["headers"],
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["data"]["fallback_provider_id"] == backup["id"]

    cleared = client.patch(
        f"{PROVIDERS}/{main['id']}", json={"fallback_provider_id": None}, headers=user["headers"]
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["fallback_provider_id"] is None


def test_stream_enabled_defaults_true_and_can_be_turned_off(client: TestClient) -> None:
    user = _user(client)
    row = _provider(client, user, f"流_{uuid.uuid4().hex[:6]}").json()["data"]
    assert row["stream_enabled"] is True, "默认必须开启流式（与用户既有行为一致）"

    off = client.patch(
        f"{PROVIDERS}/{row['id']}", json={"stream_enabled": False}, headers=user["headers"]
    )
    assert off.status_code == 200, off.text
    assert off.json()["data"]["stream_enabled"] is False
    # 不传该字段时不能被重置成默认值
    keep = client.patch(f"{PROVIDERS}/{row['id']}", json={"name": "改个名"}, headers=user["headers"])
    assert keep.json()["data"]["stream_enabled"] is False
