"""剧情滚动总结（分层合并）。

覆盖三件事：
  · 纯函数：轮数计算、该不该合并、覆盖区间、表头剥离、掐中间留两头
  · 端到端：真的聊到第 10 / 20 轮时，总结被合并、覆盖标记推进、旧总结不残留
  · 降级：总结那次模型调用失败 → 退回本地压缩 + 如实回报（绝不静默、绝不炸对话）
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.llm.params import GenerationParams, ReasoningEffort, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative import summary as summary_mod

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


# ==================================================================
#  一、纯函数
# ==================================================================
class _Msg:
    def __init__(self, mid: int, role: str, content: str = "x") -> None:
        self.id = mid
        self.role = role
        self.content = content


def _history(rounds: int) -> list[_Msg]:
    """造 rounds 轮对话（含开场白）：greeting + (user + assistant) × rounds。"""
    rows = [_Msg(1, "assistant", "开场白")]
    mid = 2
    for index in range(1, rounds + 1):
        rows.append(_Msg(mid, "user", f"第{index}轮用户"))
        rows.append(_Msg(mid + 1, "assistant", f"第{index}轮角色"))
        mid += 2
    return rows


def test_build_summarize_messages_carries_old_summary_and_new_block() -> None:
    """合并提示里必须同时给出「旧总结」和「新对话」—— 这就是"先遍历旧总结"那一步。"""
    messages = summary_mod.build_summarize_messages(
        "（第 1~10 轮）\n旧总结说：她答应点灯。",
        _history(20)[20:],
        from_round=11,
        to_round=20,
    )
    system = messages[0]
    user = messages[1]
    assert "前情提要" in system.content
    assert "旧总结说：她答应点灯。" in user.content, (
        "旧总结必须带进去（否则等于从头再来）"
    )
    assert "第11轮用户" in user.content
    assert "覆盖第 1~20 轮" in user.content
    assert "不要编造" in system.content


# ==================================================================
#  二、端到端（假模型，不花钱）
# ==================================================================
class FakeAdapter:
    """假适配器：按"这次调用是不是总结"返回不同内容（用系统提示词里的标记判断）。

    ★ 为什么这样写而不是用"返回队列"：一轮里可能有**两次**调用
      （先总结、再正文），队列很容易对错位；按请求内容分流才是稳的。
    """

    script: dict = {}

    def __init__(self, row=None, **_):
        self.row = row
        self.default_params = GenerationParams(max_tokens=512)

    @property
    def budget(self):
        window = getattr(self.row, "context_window", None) or 8192
        max_out = getattr(self.row, "max_tokens", None) or 1024
        return compute_context_budget(window, max_out)

    @staticmethod
    def _is_summary(request) -> bool:
        return "维护「前情提要」" in (request.messages[0].content or "")

    def _content(self, request) -> str:
        if self._is_summary(request):
            return FakeAdapter.script.get("summary_content", "（模型写的总结）")
        return FakeAdapter.script.get("content", "（假回复）")

    def chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        return ChatResult(
            content=self._content(request),
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        yield StreamChunk(delta=self._content(request))
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(total_tokens=15))

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    FakeAdapter.script = {}
    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    monkeypatch.setattr(engine.memory_mod, "remember_turn", lambda **_: None)
    yield FakeAdapter
    FakeAdapter.script = {}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def user(client: TestClient):
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"sm_{token}",
        "email": f"sm_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    logged = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged.status_code == 200
    data = {
        "username": account["username"],
        "headers": {"Authorization": f"Bearer {logged.json()['data']['access_token']}"},
    }
    yield data
    from sqlalchemy import select

    from app.db.models import User
    from app.db.mysql import session_scope

    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == data["username"]))
        if row is not None:
            db.delete(row)


def _make_session(
    client: TestClient, user: dict, rounds: int, *, settings: dict | None = None
) -> tuple[int, dict]:
    """建会话（并按需保存记忆总结设置）后真聊 `rounds` 轮。

    返回 `(session_id, 最后一条发送响应)`。
    ★ 注意触发时机：自动总结发生在"下一轮开始之前"，所以第 8 轮结束后要再发一句
      （第 9 轮）才会合并出「第 1~8 轮」；第二次同理。
    """
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"sm_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-summary-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    card = client.post(
        CARDS,
        json={"name": f"总结卡_{uuid.uuid4().hex[:6]}", "greeting": "……你来了。"},
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    session = client.post(
        BASE,
        json={
            "character_card_id": card.json()["data"]["id"],
            "llm_provider_id": provider.json()["data"]["id"],
        },
        headers=user["headers"],
    )
    assert session.status_code == 201, session.text
    session_id = session.json()["data"]["id"]
    if settings is not None:
        saved = client.patch(
            f"{BASE}/{session_id}/memory-summary",
            json=settings,
            headers=user["headers"],
        )
        assert saved.status_code == 200, saved.text
    payload: dict = {}
    for index in range(1, rounds + 1):
        sent = client.post(
            f"{BASE}/{session_id}/messages",
            json={"content": f"第{index}轮用户说话"},
            headers=user["headers"],
        )
        assert sent.status_code == 201, sent.text
        payload = sent.json()["data"]
    return session_id, payload


def test_summary_is_merged_at_the_block_boundary(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 开了自动总结后，第 8 轮攒满、下一轮开始时合并，标记「第 1~8 轮」。"""
    fake_llm.script["summary_content"] = "【总结】薇拉答应今晚点灯；灯油只剩半桶。"
    session_id, last = _make_session(
        client, user, 9, settings={"auto": True, "rounds": 8}
    )
    assert last["prompt"]["summary"]["to_round"] == 8, last["prompt"]["summary"]
    assert last["prompt"]["summary"]["used_model"] is True

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["summary_coverage"] == {"from_round": 1, "to_round": 8}, detail[
        "summary_coverage"
    ]
    assert detail["rolling_summary"].startswith("（第 1~8 轮）")
    assert "薇拉答应今晚点灯" in detail["rolling_summary"]

    # ★ 被覆盖的那一块不再逐条发给模型（否则总结与原文同时占 token）
    sent_join = "\n".join(
        m.content
        for m in fake_llm.script["requests"][-1].messages
        if m.role != "system"
    )
    assert "第1轮用户说话" not in sent_join
    assert "第8轮用户说话" not in sent_join
    assert "第9轮用户说话" in sent_join

    preview = client.get(
        f"{BASE}/{session_id}", headers=user["headers"], params={"with_prompt": "true"}
    ).json()["data"]
    assert any("前情提要" in w for w in preview["prompt"]["warnings"]), preview[
        "prompt"
    ]["warnings"]


def test_summary_merges_old_summary_into_a_new_one_at_the_second_block(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 第二块：把「旧总结 + 第 9~16 轮」合并成**一份**新总结（1~16），旧的不残留。"""
    fake_llm.script["summary_content"] = "【合并后的总结】灯已点亮，她欠用户一次坦白。"
    session_id, last = _make_session(
        client, user, 17, settings={"auto": True, "rounds": 8}
    )
    assert last["prompt"]["summary"]["to_round"] == 16, last["prompt"]["summary"]

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["summary_coverage"] == {"from_round": 1, "to_round": 16}
    assert "合并后的总结" in detail["rolling_summary"]
    assert "（第 1~8 轮）" not in detail["rolling_summary"], "旧表头必须被替换掉"
    assert detail["rolling_summary"].count("（第 ") == 1, "只允许一个覆盖表头"

    merge_requests = [
        r
        for r in fake_llm.script["requests"]
        if "请把上面两部分合并成一份新的前情提要" in r.messages[-1].content
    ]
    assert len(merge_requests) == 2, "8 轮一次、16 轮一次，共两次合并调用"
    second = merge_requests[-1].messages[-1].content
    assert "合并后的总结" in second, "第二次合并要把第一份总结喂进去"
    assert "第9轮用户说话" in second and "第16轮用户说话" in second
    assert "第1轮用户说话" not in second, "已经总结过的旧轮次不该重复扫描"


def test_auto_off_only_reminds_and_never_spends_tokens(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 用户要求"不强制"：没开自动时，到点**只提醒**，一次模型调用都不许多花。"""
    session_id, last = _make_session(
        client, user, 9, settings={"auto": False, "rounds": 8}
    )
    state = last["prompt"]["summary_state"]
    assert state["due"] is True, state
    assert state["auto"] is False
    assert state["from_round"] == 1 and state["to_round"] == 8
    assert state["cost_tokens"] > 0, "要把这次总结的预估消耗如实告诉用户"
    assert not any(
        "请把上面两部分合并成一份新的前情提要" in r.messages[-1].content
        for r in fake_llm.script["requests"]
    ), "自动总结关着的时候绝不能偷偷调模型"

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["rolling_summary"] in (None, ""), "手动模式不该自己写出总结"
    assert detail["summary_coverage"] is None


def test_manual_run_summarizes_whatever_is_pending(
    client: TestClient, user: dict, fake_llm
) -> None:
    """点「立即总结」：有几轮就总结几轮（不要求攒够一整块）。"""
    fake_llm.script["summary_content"] = "【手动总结】第 1~5 轮的事。"
    session_id, _ = _make_session(
        client, user, 5, settings={"auto": False, "rounds": 8}
    )
    response = client.post(
        f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    panel = response.json()["data"]
    assert panel["coverage"] == {"from_round": 1, "to_round": 5}, panel["coverage"]
    assert "手动总结" in panel["content"]
    assert panel["reminder"]["due"] is False, "总结完就不该再提醒了"
    # 落库了才算数
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["summary_coverage"] == {"from_round": 1, "to_round": 5}


def test_summary_call_always_lowers_thinking(
    client: TestClient, user: dict, fake_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 剧情总结是**机械任务**（把对话压成摘要），思考纯烧钱 ⇒ 一律降到最小。

    ==================== 第二十七轮的行为变更 ====================
    旧契约：只在"真机探测确认该模型支持思考强度"时才发这个参数。后果是
    DeepSeek 被那次不可靠的探测误判为"不支持"，于是总结**从来没降过思考** ——
    省钱机制被一个错误结论堵死了几轮。
    现在：**直接请求 OFF**；若服务端不认这个参数，适配器会自动去掉并重试一次
    （见 `openai_compatible._post_with_effort_fallback`），不会让总结失败。
    """
    session_id, _ = _make_session(
        client, user, 5, settings={"auto": False, "rounds": 8}
    )
    response = client.post(
        f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    calls = [
        r for r in fake_llm.script.get("requests", []) if FakeAdapter._is_summary(r)
    ]
    assert calls, "应该发生过一次总结调用"
    assert all(r.reasoning_effort is ReasoningEffort.OFF for r in calls), (
        "总结一律降到最小思考；实际 = " + repr([r.reasoning_effort for r in calls])
    )


def test_summary_does_not_lower_thinking_when_adapter_opts_out(
    client: TestClient, user: dict, fake_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """适配器显式声明"我不参与思考强度适配"时，才不发这个参数。

    ★ 这条替代了旧的"探测结论为 False ⇒ 不发"：判据从**猜测**（比 token 数）
      换成了**声明**（适配器自己说它支不支持翻译这个字段）。
    """

    def _build(row=None, **_):
        adapter = FakeAdapter(row)
        adapter.supports_reasoning_effort = False
        return adapter

    monkeypatch.setattr(engine, "build_adapter", _build)
    session_id, _ = _make_session(
        client, user, 5, settings={"auto": False, "rounds": 8}
    )
    response = client.post(
        f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    calls = [
        r for r in fake_llm.script.get("requests", []) if FakeAdapter._is_summary(r)
    ]
    assert calls, "应该发生过一次总结调用"
    assert all(r.reasoning_effort is None for r in calls), (
        "适配器声明不支持时，不该覆盖成 OFF；实际 = "
        + repr([r.reasoning_effort for r in calls])
    )


def test_copy_mode_costs_nothing(client: TestClient, user: dict, fake_llm) -> None:
    """模式 4「照抄旧记忆生成新记忆」：不调模型（0 token），内容照抄 + 追加本地摘录。"""
    session_id, last = _make_session(
        client, user, 4, settings={"auto": False, "rounds": 2, "mode": "copy"}
    )
    assert last["prompt"]["summary_state"]["cost_tokens"] == 0, (
        "照抄模式的预估消耗必须是 0"
    )
    assert last["prompt"]["summary_state"]["mode"] == "copy"
    second = client.post(
        f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
    )
    assert second.status_code == 200, second.text
    content = second.json()["data"]["content"]
    assert "第 1~4 轮新增" in content, content
    assert "较早的对话已被压缩" in content or "用户：" in content, content
    assert not any(
        "请把上面两部分合并成一份新的前情提要" in r.messages[-1].content
        for r in fake_llm.script["requests"]
    ), "照抄模式绝不许调模型"


def test_summary_content_is_editable_and_restorable(
    client: TestClient, user: dict, fake_llm
) -> None:
    """面板上的「编辑」与「恢复上一次」：改得动、也回得来。"""
    fake_llm.script["summary_content"] = "【原始总结】她答应点灯。"
    session_id, _ = _make_session(
        client, user, 3, settings={"auto": False, "rounds": 2}
    )
    assert (
        client.post(
            f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
        ).status_code
        == 200
    )

    edited = client.patch(
        f"{BASE}/{session_id}/memory-summary",
        json={"content": "【我改过的】她其实不想点灯。"},
        headers=user["headers"],
    )
    assert edited.status_code == 200, edited.text
    panel = edited.json()["data"]
    assert "我改过的" in panel["content"] and "原始总结" not in panel["content"]
    assert panel["content"].startswith("（第 1~"), "编辑后仍要盖上覆盖表头"
    assert panel["content"].count("（第 ") == 1, "用户贴进来的旧表头要被剥掉"
    assert panel["history"], "编辑前的版本要进历史"

    restored = client.post(
        f"{BASE}/{session_id}/memory-summary/restore", headers=user["headers"]
    )
    assert restored.status_code == 200, restored.text
    assert "原始总结" in restored.json()["data"]["content"], "恢复上一次要能回到旧版本"


def test_disabled_summary_refuses_manual_run(client: TestClient, user: dict) -> None:
    """关掉总开关后连手动总结也不做（并且明确告诉用户原因，不是静默失败）。"""
    session_id, _ = _make_session(client, user, 3, settings={"enabled": False})
    response = client.post(
        f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
    )
    assert response.status_code == 400, response.text
    assert "关闭" in response.text


def test_second_run_without_new_content_does_not_summarize_again(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 连点两下（顺序到达）不该总结两遍：第二遍已经没有新内容可总结了。

    真实事故：横幅点了没反馈 → 用户连点几下 → 连着总结了好几遍、白花 token。
    这不只是前端问题，后端也必须能挡住"重复触发"。
    """
    fake_llm.script["summary_content"] = "【总结】第一次就够了。"
    session_id, _ = _make_session(
        client, user, 3, settings={"auto": False, "rounds": 2}
    )
    first = client.post(
        f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
    )
    assert (
        first.status_code == 200 and first.json()["data"]["coverage"]["to_round"] == 3
    )
    second = client.post(
        f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
    )
    assert second.status_code == 200, second.text
    payload = second.json()["data"]
    # 第二次没有覆盖推进（说明没有重复总结）
    assert payload["coverage"] == {"from_round": 1, "to_round": 3}, payload["coverage"]
    summaries = [
        r
        for r in fake_llm.script["requests"]
        if "请把上面两部分合并成一份新的前情提要" in r.messages[-1].content
    ]
    assert len(summaries) == 1, f"只该调用一次总结模型，实际 {len(summaries)} 次"


def test_concurrent_run_is_rejected_with_busy(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 并发重复点击必须被**挡住并说明**（409），而不是再跑一遍模型。

    这里用"手动占住会话锁"来模拟"上一次还在跑"：真并发在单测里靠 sleep 不可靠。
    """
    session_id, _ = _make_session(
        client, user, 3, settings={"auto": False, "rounds": 2}
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]

    class _Fake:
        id = session_id

    lock = summary_mod._lock_for(_Fake())
    assert lock.acquire(blocking=False), "测试前置：锁应当是空闲的"
    try:
        response = client.post(
            f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
        )
        assert response.status_code == 409, response.text
        assert "进行中" in response.text, response.text
        # 被挡住时**不许**调用模型
        assert not any(
            "请把上面两部分合并成一份新的前情提要" in r.messages[-1].content
            for r in fake_llm.script["requests"]
        )
        assert detail["summary_coverage"] is None
    finally:
        lock.release()

    # 锁释放之后照常能总结（不是把功能锁死了）
    ok = client.post(f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["coverage"]["to_round"] == 3


def test_manual_run_refuses_when_no_complete_round(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 一轮完整对话都没有时拒绝总结（别把开场白当成"第 1 轮"白花一次调用）。

    实测踩过：会话被"撤回"清空后点「立即总结」，它把开场白总结成了「第 1~1 轮」。
    """
    session_id, _ = _make_session(client, user, 0)  # 只有开场白
    response = client.post(
        f"{BASE}/{session_id}/memory-summary/run", headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    assert "还没有完成一轮" in response.json()["message"], response.json()
    assert response.json()["data"]["coverage"] is None
    assert not any(
        "请把上面两部分合并成一份新的前情提要" in r.messages[-1].content
        for r in fake_llm.script.get("requests", [])
    ), "没有完整轮次时不该调模型"


def test_summary_falls_back_to_local_compression_when_model_fails(
    client: TestClient, user: dict, fake_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 总结调用失败：对话照常、总结退回本地压缩，并把原因说出来（不静默降级）。"""

    class BoomAdapter(FakeAdapter):
        def chat(self, request):
            if self._is_summary(request):
                raise RuntimeError("总结这步炸了")
            return super().chat(request)

    monkeypatch.setattr(engine, "build_adapter", lambda row: BoomAdapter(row))
    session_id, last = _make_session(
        client, user, 9, settings={"auto": True, "rounds": 8}
    )

    assert last["prompt"]["summary"]["used_model"] is False
    assert any("本地压缩" in w for w in last["prompt"]["warnings"]), last["prompt"][
        "warnings"
    ]

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["summary_coverage"] == {"from_round": 1, "to_round": 8}, (
        "降级也要推进覆盖"
    )
    assert "较早的对话已被压缩" in detail["rolling_summary"], "退回本地压缩"


pytestmark = pytest.mark.integration
