"""对话分支（平行世界 / 世界线）测试。

==================== 这个文件最要紧的四条 ====================
用户提这个需求时最在意的一句话是：
    「注意两个世界是独立的，不要出现用户回复第二个世界线，
      却出现第一个世界线也出现反应的情况」

所以本文件的核心不是"分叉能不能用"，而是**世界线之间真的不会互相影响**：
    1. 一条世界线 = 一个独立会话（消息、状态、总结全按 session_id 隔离）
    2. 在平行线发消息，主线的消息与状态**一个字节都不动**
    3. 平行线的提示词里**不含**主线的独有内容，反之亦然
    4. 撤回 / 重新生成 / 删线都只作用在自己那条线上

另外覆盖"默认不打扰"这条产品约束：
    没开启分支模式的会话，行为与加这个功能之前完全一致（详情里 enabled=False，
    消息上不出现分叉入口，会话列表不出现"主线/平行线"标记）。

★ 测试**绝不调用真实大模型 API**（红线）：与 test_narrative.py 同一套
  FakeAdapter + monkeypatch 做法。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Message, NarrativeSession, User
from app.db.mysql import session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import branch as branch_mod
from app.narrative import engine

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


# ==================================================================
#  夹具（与 test_narrative.py 保持一致的口径）
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"br_{token}",
        "email": f"br_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    created = client.post("/api/v1/auth/register", json=account)
    assert created.status_code == 201, created.text
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged_in.status_code == 200, logged_in.text
    return {
        "id": created.json()["data"]["id"],
        "username": account["username"],
        "headers": {"Authorization": f"Bearer {logged_in.json()['data']['access_token']}"},
    }


def _cleanup_user(username: str) -> None:
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == username))
        if row is not None:
            db.delete(row)


@pytest.fixture
def user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    _cleanup_user(data["username"])


@pytest.fixture
def other_user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    _cleanup_user(data["username"])


def make_card(client: TestClient, user: dict, **overrides) -> dict:
    body = {
        "name": f"卡_{uuid.uuid4().hex[:6]}",
        "personality": "冷静、话少",
        "background": "来自北方的旅人",
        "greeting": "……你来了。",
    }
    body.update(overrides)
    response = client.post(CARDS, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_provider(client: TestClient, user: dict, **overrides) -> dict:
    body = {
        "name": f"cfg_{uuid.uuid4().hex[:6]}",
        "provider_type": "openai_compatible",
        "base_url": "https://mock.invalid/v1",
        "api_key": "sk-test-not-a-real-key",
        "model_name": "mock-model",
        "context_window": 8192,
        "generation": {"temperature": 0.8, "max_tokens": 1024, "reasoning_effort": "auto"},
    }
    body.update(overrides)
    response = client.post(PROVIDERS, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_session(client: TestClient, user: dict, **kw) -> dict:
    body = {"character_card_id": make_card(client, user)["id"], "llm_provider_id": make_provider(client, user)["id"]}
    body.update(kw)
    response = client.post(BASE, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ==================================================================
#  假适配器（★ 不联网）
# ==================================================================
class FakeAdapter:
    script: dict[str, Any] = {}

    def __init__(self, row=None, **_: Any) -> None:
        self.row = row
        self.default_params = GenerationParams(max_tokens=512)
        self.context_window = 8192

    @property
    def budget(self):
        window = getattr(self.row, "context_window", None) or self.context_window
        max_out = getattr(self.row, "max_tokens", None) or self.default_params.max_tokens
        return compute_context_budget(window, max_out)

    def chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        # ★ 每次调用返回**不同的**回复：分叉测试要能区分"这条线写的是什么"，
        #   若所有回复都一样，就无法证明两条线各自独立生成了自己的内容。
        index = len(FakeAdapter.script["requests"])
        template = FakeAdapter.script.get("content", "假回复{index}")
        return ChatResult(
            content=str(template).format(index=index),
            model="mock-model",
            finish_reason="stop",
            notes=[],
            usage=TokenUsage(prompt_tokens=30, completion_tokens=12, total_tokens=42),
            latency_ms=7,
        )

    def stream_chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        index = len(FakeAdapter.script["requests"])
        for piece in str(FakeAdapter.script.get("content", "假回复{index}")).format(index=index):
            yield StreamChunk(delta=piece)
        yield StreamChunk(
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=40, completion_tokens=9, total_tokens=49),
        )

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """换掉模型调用与长期记忆（口径与 test_narrative.py 完全一致）。"""
    FakeAdapter.script = {}
    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    monkeypatch.setattr(engine.memory_mod, "remember_turn", lambda **_: None)
    monkeypatch.setattr(
        engine.memory_mod,
        "recall_block",
        lambda **_: ("", engine.memory_mod.RecallResult()),
    )
    yield FakeAdapter
    FakeAdapter.script = {}


# ==================================================================
#  小工具
# ==================================================================
def send(client: TestClient, user: dict, session_id: int, text: str) -> dict:
    response = client.post(
        f"{BASE}/{session_id}/messages", json={"content": text}, headers=user["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def detail(client: TestClient, user: dict, session_id: int) -> dict:
    response = client.get(f"{BASE}/{session_id}", headers=user["headers"])
    assert response.status_code == 200, response.text
    return response.json()["data"]


def enable_branch(client: TestClient, user: dict, session_id: int) -> dict:
    response = client.post(
        f"{BASE}/{session_id}/branch", json={"enabled": True}, headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def fork(client: TestClient, user: dict, session_id: int, **body) -> dict:
    response = client.post(
        f"{BASE}/{session_id}/branch/fork", json=body, headers=user["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def last_assistant(detail_data: dict) -> dict:
    rows = [m for m in detail_data["messages"] if m["role"] == "assistant"]
    assert rows, "这个会话里没有角色回复"
    return rows[-1]


def _contents(detail_data: dict) -> list[str]:
    return [m["content"] for m in detail_data["messages"]]


def _db_session(session_id: int) -> NarrativeSession:
    with session_scope() as db:
        row = db.get(NarrativeSession, session_id)
        assert row is not None
        db.expunge(row)
        return row


# ==================================================================
#  一、默认行为：不开分支时一切照旧（"不打扰现有用户"）
# ==================================================================
def test_branch_is_off_by_default(client: TestClient, user: dict) -> None:
    """新建的会话**默认没有开启**分支 —— 详情里 enabled=False，一条线。"""
    session = make_session(client, user)
    data = detail(client, user, session["id"])

    assert data["branch"]["enabled"] is False
    assert data["branch"]["can_branch"] is False
    assert len(data["branch"]["lines"]) == 1
    # 列表里也不该出现世界线标记（用户还没用过这个概念）
    listing = client.get(BASE, headers=user["headers"]).json()["data"]
    row = next(s for s in listing["items"] if s["id"] == session["id"])
    assert row["branch_line"] is None


def test_branch_toggle_round_trip(client: TestClient, user: dict) -> None:
    """开启 → 能分支；关闭 → 入口收起，但**不删除**已经生成的平行线。"""
    session = make_session(client, user)
    send(client, user, session["id"], "你好")
    enabled = enable_branch(client, user, session["id"])
    assert enabled["branch"]["enabled"] is True
    assert enabled["branch"]["can_branch"] is True

    forked = fork(client, user, session["id"], after_message_id=last_assistant(detail(client, user, session["id"]))["id"])
    twin_id = forked["session"]["id"]

    response = client.post(
        f"{BASE}/{session['id']}/branch", json={"enabled": False}, headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["enabled"] is False

    # ★ 关键断言：关闭只收起入口，平行线**必须还在**（那是真实生成的内容）
    still_there = client.get(f"{BASE}/{twin_id}", headers=user["headers"])
    assert still_there.status_code == 200, "关闭分支模式不该删除平行线"


def test_toggle_requires_ownership(client: TestClient, user: dict, other_user: dict) -> None:
    """别人的会话：开启分支一律 404（会话自身的越权规则）。"""
    session = make_session(client, user)
    response = client.post(
        f"{BASE}/{session['id']}/branch", json={"enabled": True}, headers=other_user["headers"]
    )
    assert response.status_code == 404


# ==================================================================
#  二、分叉的语义
# ==================================================================
def test_fork_copies_history_up_to_the_reply(client: TestClient, user: dict) -> None:
    """分叉复制到**那条回复之前**：新线末尾是用户消息，且不含被分叉的回复。

    ★ 这个语义是"能另写一条走向"的前提 —— 若把被分叉的回复也带过去，
      新线里同一轮就已经有回复了，接下来就无从生成"另一种回答"。
    """
    session = make_session(client, user)
    FakeAdapter.script["content"] = "第一版回复"
    send(client, user, session["id"], "我推开门")
    enable_branch(client, user, session["id"])

    main = detail(client, user, session["id"])
    target = last_assistant(main)
    forked = fork(client, user, session["id"], after_message_id=target["id"])
    twin = forked["session"]

    # 新线的消息 = 开场白 + 用户消息（**不含**那条被分叉的回复）
    twin_contents = _contents(twin)
    assert "第一版回复" not in twin_contents
    assert twin_contents[-1] == "我推开门"
    assert twin["messages"][-1]["role"] == "user"
    # 主线完全没被改动
    assert _contents(detail(client, user, session["id"])) == _contents(main)


def test_fork_links_the_group_and_marks_the_lines(client: TestClient, user: dict) -> None:
    """两条线归到同一个世界线组，且分支点被记下来。"""
    session = make_session(client, user)
    send(client, user, session["id"], "往前走吧")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))

    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    assert twin["branch"]["enabled"] is True
    assert len(twin["branch"]["lines"]) == 2
    assert {line["id"] for line in twin["branch"]["lines"]} == {session["id"], twin["id"]}

    # 数据层：两条线的 group_id 必须一致；branch_from_message_id 指向被分叉的那条回复
    main_row = _db_session(session["id"])
    twin_row = _db_session(twin["id"])
    assert main_row.branch_group_id == twin_row.branch_group_id
    assert twin_row.branch_from_message_id == target["id"]


def test_fork_labels_default_to_main_and_parallel(client: TestClient, user: dict) -> None:
    """默认名：先来的叫「主线」，分出来的叫「平行线」。"""
    session = make_session(client, user)
    send(client, user, session["id"], "出发")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    labels = {line["id"]: line["label"] for line in twin["branch"]["lines"]}
    assert labels[session["id"]] == "主线"
    assert labels[twin["id"]] == "平行线"


def test_fork_is_capped_at_two_lines(client: TestClient, user: dict) -> None:
    """只支持两条线（用户拍板）：第二次分叉必须被明确拒绝，而不是静默加一条。"""
    session = make_session(client, user)
    send(client, user, session["id"], "第一句")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    fork(client, user, session["id"], after_message_id=target["id"])

    again = client.post(
        f"{BASE}/{session['id']}/branch/fork",
        json={"after_message_id": target["id"]},
        headers=user["headers"],
    )
    assert again.status_code == 400, again.text
    assert "两条" in again.json()["message"] or "2" in again.json()["message"]


def test_fork_requires_branch_mode(client: TestClient, user: dict) -> None:
    """没开分支模式就想分叉 → 明确报错（而不是悄悄建一条线）。"""
    session = make_session(client, user)
    send(client, user, session["id"], "你好")
    target = last_assistant(detail(client, user, session["id"]))
    response = client.post(
        f"{BASE}/{session['id']}/branch/fork",
        json={"after_message_id": target["id"]},
        headers=user["headers"],
    )
    assert response.status_code == 400
    assert "分支" in response.json()["message"]


def test_fork_rejects_foreign_message_id(client: TestClient, user: dict) -> None:
    """分叉点必须是**这条会话里**的消息（否则 400，不会拿别人的消息当锚点）。"""
    session = make_session(client, user)
    send(client, user, session["id"], "你好")
    enable_branch(client, user, session["id"])
    response = client.post(
        f"{BASE}/{session['id']}/branch/fork",
        json={"after_message_id": 999999999},
        headers=user["headers"],
    )
    assert response.status_code == 400
    assert "分叉点" in response.json()["message"]


def test_fork_requires_ownership(client: TestClient, user: dict, other_user: dict) -> None:
    session = make_session(client, user)
    send(client, user, session["id"], "你好")
    enable_branch(client, user, session["id"])
    response = client.post(
        f"{BASE}/{session['id']}/branch/fork", json={}, headers=other_user["headers"]
    )
    assert response.status_code == 404


def test_fork_copies_session_level_state(client: TestClient, user: dict) -> None:
    """会话级数据要带过去：状态、状态栏格式、总结、锚点、设置。

    ★ 不带过去的话，新线会表现成"分叉完就失忆、状态栏也变空" ——
      而分叉那一刻两条线的处境本该完全一样。
    """
    session = make_session(client, user)
    send(client, user, session["id"], "第一轮")
    enable_branch(client, user, session["id"])

    # 手工给主线写上"会话级数据"（走库，模拟已经玩了一段时间的会话）
    with session_scope() as db:
        row = db.get(NarrativeSession, session["id"])
        row.rolling_summary = "（第 1~1 轮）他在北境遇到了一个旅人。"
        row.summary_from_round = 1
        row.summary_to_round = 1
        row.summarized_until_message_id = 1
        row.state_json = '{"hp": 88}'
        row.state_schema_json = '{"spec": 1, "source": "card", "fields": []}'
        row.memory_anchors_json = '["他怕黑"]'
        db.commit()

    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    twin_row = _db_session(twin["id"])
    assert twin_row.rolling_summary == "（第 1~1 轮）他在北境遇到了一个旅人。"
    assert twin_row.summary_to_round == 1
    assert twin_row.summarized_until_message_id == 1
    assert twin_row.state_json == '{"hp": 88}'
    assert twin_row.state_schema_json == '{"spec": 1, "source": "card", "fields": []}'
    assert twin_row.memory_anchors_json == '["他怕黑"]'
    # 设定也要一致（分支不改变"这是谁的故事"）
    assert twin_row.character_card_id == _db_session(session["id"]).character_card_id


def test_fork_copies_message_metadata_columns(client: TestClient, user: dict) -> None:
    """**分叉点之前**的回复，其 JSON 元数据列必须逐列带过去。

    ★ 这些列最容易在"复制消息"时被漏掉 —— 漏了以后新线的骰点、译文、
      状态原文会全部消失，而且**不会报错**（只是静默变成 null）。
      所以这里逐列断言，逼着以后新增列时同步更新 branch._copy_message。
    """
    session = make_session(client, user)
    # ★ 发两轮：第一轮回复会在**分叉点之前**（第二轮那条才是分叉点），
    #   于是"复制过来的消息带不带元数据"才有得验。
    FakeAdapter.script["content"] = "第一轮回复"
    send(client, user, session["id"], "第一轮")
    FakeAdapter.script["content"] = "第二轮回复"
    send(client, user, session["id"], "第二轮")
    enable_branch(client, user, session["id"])

    with session_scope() as db:
        rows = list(
            db.scalars(
                select(Message)
                .where(Message.session_id == session["id"], Message.role == "assistant")
                .order_by(Message.id)
            ).all()
        )
        assert len(rows) >= 3, [m.role for m in rows]  # 开场白 + 两轮回复
        first_reply = rows[1]
        first_reply.rolls_json = '[{"total": 42}]'
        first_reply.translation_json = '{"text": "hi", "display": "translation"}'
        first_reply.state_raw_json = '{"hp": 88}'
        first_reply.usage_json = '{"total_tokens": 123}'
        first_reply.state_meta_json = '{"required": true}'
        target_id = rows[-1].id
        db.commit()

    twin = fork(client, user, session["id"], after_message_id=target_id)["session"]

    with session_scope() as db:
        copied = list(
            db.scalars(
                select(Message)
                .where(Message.session_id == twin["id"])
                .order_by(Message.id)
            ).all()
        )
        # 新线 = 开场白 + 第一轮 user + 第一轮回复 + 第二轮 user（**不含**被分叉的第二轮回复）
        assert [m.role for m in copied] == ["assistant", "user", "assistant", "user"]
        copied_reply = copied[2]
        assert copied_reply.rolls_json == '[{"total": 42}]'
        assert copied_reply.translation_json == '{"text": "hi", "display": "translation"}'
        assert copied_reply.state_raw_json == '{"hp": 88}'
        assert copied_reply.usage_json == '{"total_tokens": 123}'
        assert copied_reply.state_meta_json == '{"required": true}'


def test_forked_message_gets_a_new_id(client: TestClient, user: dict) -> None:
    """复制出来的消息必须是**新行**（新 id），不能复用主线的 id。

    ★ 复用 id 会变成"改主线那条消息"，是最危险的一类复制 bug。
    """
    session = make_session(client, user)
    send(client, user, session["id"], "第一轮")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    main_ids = {m["id"] for m in detail(client, user, session["id"])["messages"]}
    twin_ids = {m["id"] for m in twin["messages"]}
    assert not (main_ids & twin_ids), "新线的消息不该与主线共用 id"


def test_fork_recounts_stats(client: TestClient, user: dict) -> None:
    """新线的 message_count 要按**复制过来的条数**算，不能继承主线的计数。"""
    session = make_session(client, user)
    send(client, user, session["id"], "第一轮")
    send(client, user, session["id"], "第二轮")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    twin_row = _db_session(twin["id"])
    with session_scope() as db:
        actual = db.scalar(
            select(Message).where(Message.session_id == twin["id"]).order_by(Message.id)
        )
        assert actual is not None
        count = len(
            list(db.scalars(select(Message).where(Message.session_id == twin["id"])).all())
        )
    assert twin_row.message_count == count
    # 新线少一条（不含被分叉的那条回复）
    assert twin_row.message_count == _db_session(session["id"]).message_count - 1


# ==================================================================
#  三、★★★ 两条世界线互不影响（本功能的核心保证）★★★
# ==================================================================
def test_saying_something_in_the_twin_does_not_touch_the_main(
    client: TestClient, user: dict
) -> None:
    """★ 核心用例：在平行线发消息，主线的消息列表**一个字节都不变**。

    这正是用户最担心的场景（"回复第二个世界线，第一个世界线也出现反应"）。
    """
    session = make_session(client, user)
    send(client, user, session["id"], "我推开门")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    main_before = detail(client, user, session["id"])

    # 在平行线里继续推进剧情
    FakeAdapter.script["content"] = "【平行线】屋里没有人。"
    send(client, user, twin["id"], "我走进屋")

    main_after = detail(client, user, session["id"])
    assert _contents(main_after) == _contents(main_before), "主线的消息绝不该被平行线改动"
    assert main_after["message_count"] == main_before["message_count"]
    assert main_after["total_tokens"] == main_before["total_tokens"]

    # 平行线自己确实长了
    twin_after = detail(client, user, twin["id"])
    assert "【平行线】屋里没有人。" in _contents(twin_after)


def test_prompts_of_the_two_lines_do_not_leak_into_each_other(
    client: TestClient, user: dict
) -> None:
    """两条线的提示词互不包含对方的独有内容。

    ★ 这是比"数据库行数没变"更硬的证据：即使数据没串，
      如果提示词装配时把另一条线的历史喂了进去，模型照样会"串台"。
    """
    session = make_session(client, user)
    send(client, user, session["id"], "主线专属的一句话")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    # 两边各说一句只有自己知道的话
    FakeAdapter.script["content"] = "主线专属的回复"
    send(client, user, session["id"], "主线专属的一句话2")
    main_prompt = _prompt_text(client, user, session["id"])

    FakeAdapter.script["content"] = "平行线专属的回复"
    send(client, user, twin["id"], "平行线专属的一句话")
    twin_prompt = _prompt_text(client, user, twin["id"])

    assert "平行线专属" not in main_prompt, "主线的提示词里漏进了平行线的内容"
    assert "主线专属的一句话2" not in twin_prompt, "平行线的提示词里漏进了主线的内容"


def _prompt_text(client: TestClient, user: dict, session_id: int) -> str:
    """取这个会话「查看提示词」预览里的完整文本。"""
    response = client.get(f"{BASE}/{session_id}", headers=user["headers"])
    assert response.status_code == 200, response.text
    prompt = response.json()["data"]["prompt"]
    assert prompt is not None
    parts = [prompt.get("system_prompt", "")]
    for message in prompt.get("messages", []) or []:
        parts.append(str(message.get("content", "")))
    return "\n".join(parts)


def test_state_is_independent_between_the_two_lines(client: TestClient, user: dict) -> None:
    """结构化状态互相独立：在一条线改状态，另一条线的状态不变。

    ★ 用一张**声明了状态栏**的卡（否则状态接口会因为没有 schema 而拒绝），
      这样才真的验证到"两条线的 state 各存各的"。
    """
    card = make_card(
        client,
        user,
        extensions={"hne": {"state_schema": {"fields": [{"name": "hp", "type": "number", "label": "HP"}]}}},
    )
    provider = make_provider(client, user)
    created = client.post(
        BASE,
        json={"character_card_id": card["id"], "llm_provider_id": provider["id"]},
        headers=user["headers"],
    )
    assert created.status_code == 201, created.text
    session = created.json()["data"]
    send(client, user, session["id"], "开始")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    # 平行线改状态
    response = client.patch(
        f"{BASE}/{twin['id']}/state",
        json={"state": {"hp": 33}},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    assert detail(client, user, twin["id"])["state"] == {"hp": 33}
    # ★ 核心断言：主线**没有**跟着变
    assert detail(client, user, session["id"])["state"] != {"hp": 33}


def test_retract_in_twin_leaves_main_untouched(client: TestClient, user: dict) -> None:
    """在平行线撤回，主线不受任何影响（删除范围按 session_id 过滤）。"""
    session = make_session(client, user)
    send(client, user, session["id"], "第一轮")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    send(client, user, twin["id"], "平行线第二轮")
    main_before = _contents(detail(client, user, session["id"]))

    # 撤回平行线里最后一条用户消息
    twin_detail = detail(client, user, twin["id"])
    last_user = [m for m in twin_detail["messages"] if m["role"] == "user"][-1]
    response = client.post(
        f"{BASE}/{twin['id']}/messages/{last_user['id']}/retract",
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text

    assert _contents(detail(client, user, session["id"])) == main_before


def test_regenerate_in_twin_leaves_main_untouched(client: TestClient, user: dict) -> None:
    """在平行线重新生成，主线的回复不会被替换掉。"""
    session = make_session(client, user)
    FakeAdapter.script["content"] = "主线原来的回复"
    send(client, user, session["id"], "同一个问题")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    main_before = _contents(detail(client, user, session["id"]))

    FakeAdapter.script["content"] = "平行线的新走向"
    # 新线末尾是用户消息 → 不传 message_id，后端以最后一条用户消息为锚点
    response = client.get(f"{BASE}/{twin['id']}/regenerate", headers=user["headers"])
    assert response.status_code == 200, response.text
    _drain(response)

    assert _contents(detail(client, user, session["id"])) == main_before, (
        "在平行线重新生成，主线的内容一个字都不该变"
    )
    assert "平行线的新走向" in _contents(detail(client, user, twin["id"]))


def _drain(response) -> None:
    """把 SSE 响应读空（触发生成与落库）。"""
    for _ in response.iter_lines():
        pass


def test_deleting_twin_leaves_main_there(client: TestClient, user: dict) -> None:
    """删掉平行线，主线必须完好（删会话只删自己那条）。"""
    session = make_session(client, user)
    send(client, user, session["id"], "你好")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    main_before = detail(client, user, session["id"])
    deleted = client.delete(f"{BASE}/{twin['id']}", headers=user["headers"])
    assert deleted.status_code == 200, deleted.text

    main_after = detail(client, user, session["id"])
    assert _contents(main_after) == _contents(main_before)

    # 两条线变回一条：can_branch 重新变真（可以再分一次）
    assert main_after["branch"]["can_branch"] is True
    assert len(main_after["branch"]["lines"]) == 1


def test_two_users_branch_groups_are_isolated(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """别人的世界线组与自己无关（组内查询带 user_id 过滤）。"""
    session = make_session(client, user)
    send(client, user, session["id"], "你好")
    enable_branch(client, user, session["id"])
    target = last_assistant(detail(client, user, session["id"]))
    twin = fork(client, user, session["id"], after_message_id=target["id"])["session"]

    # 另一个用户看不到这两条线里的任何一条
    assert client.get(f"{BASE}/{session['id']}", headers=other_user["headers"]).status_code == 404
    assert client.get(f"{BASE}/{twin['id']}", headers=other_user["headers"]).status_code == 404


# ==================================================================
#  四、改名与组信息
# ==================================================================
def test_rename_line(client: TestClient, user: dict) -> None:
    session = make_session(client, user)
    send(client, user, session["id"], "你好")
    enable_branch(client, user, session["id"])
    response = client.patch(
        f"{BASE}/{session['id']}/branch/label",
        json={"label": "灰烬世界"},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["label"] == "灰烬世界"

    # 留空恢复默认名
    again = client.patch(
        f"{BASE}/{session['id']}/branch/label",
        json={"label": ""},
        headers=user["headers"],
    )
    assert again.json()["data"]["label"] == "主线"


def test_branch_group_endpoint(client: TestClient, user: dict) -> None:
    session = make_session(client, user)
    send(client, user, session["id"], "你好")
    enable_branch(client, user, session["id"])
    response = client.get(f"{BASE}/{session['id']}/branch", headers=user["headers"])
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["enabled"] is True
    assert data["can_branch"] is True


def test_branch_rename_requires_ownership(
    client: TestClient, user: dict, other_user: dict
) -> None:
    session = make_session(client, user)
    response = client.patch(
        f"{BASE}/{session['id']}/branch/label",
        json={"label": "x"},
        headers=other_user["headers"],
    )
    assert response.status_code == 404


# ==================================================================
#  五、纯单元：branch 模块自身的判据
# ==================================================================
def test_group_id_falls_back_to_own_id_for_legacy_sessions() -> None:
    """迁移前的老会话（branch_group_id 为 NULL）自成一组。"""

    class Row:
        id = 7
        branch_group_id = None

    assert branch_mod.group_id_of(Row()) == 7
    assert branch_mod.is_branching(Row()) is False
    assert branch_mod.label_of(Row()) == "主线"


def test_label_of_prefers_custom_name() -> None:
    class Row:
        id = 1
        branch_label = "灰烬线"
        branch_index = 1

    assert branch_mod.label_of(Row()) == "灰烬线"


# ==================================================================
#  六、前端契约（分叉入口与开关都在界面上真的存在）
# ==================================================================
def test_frontend_exposes_branch_entry_points() -> None:
    """前端必须有：分叉按钮、世界线切换器、开启/关闭入口。

    ★ 为什么用静态断言守这个：这是"用户能不能发现这个功能"的问题。
      后端全部实现好了、但界面上没有任何入口，等于没做（用户不会知道）。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "web" / "js" / "views" / "chat.js").read_text(encoding="utf-8")

    assert 'data-act="fork-msg"' in src, "回复上要有「🌿 分支」按钮"
    assert 'data-act="branch-switch"' in src, "要有世界线切换标签"
    # ★ 顶栏那个按钮的 data-act 必须是**字面量**（不能是模板表达式拼出来的）：
    #   tests/test_console.py::test_chat_view_buttons_match_their_delegated_actions
    #   会静态检查"判断过的 act 有没有对应按钮" —— 拼出来的名字它看不见，
    #   那个按钮就会漏出守门人的视野。这里同时钉住这条约定。
    assert 'data-act="branch-main"' in src, "顶栏分支入口要用字面量 data-act"
    assert "act === 'branch-main'" in src, "要有顶栏分支按钮的动作分发"
    assert "act === 'branch-off'" in src, "要有关闭对话分支的动作分发"
    assert "act === 'fork-msg'" in src and "act === 'branch-switch'" in src
    assert 'id="btn-branch"' in src, "顶栏要有一个看得见的分支入口按钮"
    assert "branch/fork" in src and "/branch" in src, "要调用分支接口"
    assert "两条线各自独立" in src, "界面上必须写清两条线互不影响（这是用户最在意的一点）"
    # 关闭分支的说明必须点明"不会删除"（否则用户不敢点）
    assert "不会被删除" in src


def test_fork_button_is_hidden_where_forking_is_impossible() -> None:
    """★ 开场白**不能**有分叉按钮（它前面没有用户消息，分叉必然失败）。

    ==================== 这条守着一个真实观察 ====================
    真实浏览器里数过：一段"开场白 + 一轮对话"的会话里，分叉按钮出现了 **2 个** ——
    其中一个是开场白。而后端 `branch.fork` 明确要求"分叉点前面必须有你说过的话"
    （新线要拿那句话当锚点另写走向），所以点开场白上那个按钮**必然报 400**。

    本项目的既定原则是"把不可能的操作藏起来，比点下去报错体验好"，
    所以这里用静态检查钉住这个判据：两个渲染路径
      · renderMessages（整页渲染）→ forkableAt(messages, index)
      · attachActions（流式收尾后就地补按钮）→ hasPrecedingUserMessage(bubble)
    都必须按"前面有没有 user 消息"来决定是否给出分叉按钮。
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "web" / "js" / "views" / "chat.js").read_text(encoding="utf-8")

    assert "function forkableAt(" in src, "整页渲染路径要有 forkable 判据"
    assert "function hasPrecedingUserMessage(" in src, "流式补按钮路径要有 forkable 判据"
    # 两个判据都必须真的作用到 messageActions 上（不能定义了不用）
    assert "messageActions(m, { forkable: forkableAt(" in src, (
        "renderMessages 必须把 forkable 传给 messageActions"
    )
    assert "messageActions({ role, id }, { forkable: hasPrecedingUserMessage(bubble) })" in src, (
        "attachActions 必须把 forkable 传给 messageActions"
    )
    # 分叉按钮的出现条件里必须带上 forkable（少了它开场白又会长出按钮）
    assert "state.detail?.branch?.can_branch && opts.forkable" in src, (
        "分叉按钮必须同时满足 can_branch 与 forkable"
    )


def test_branch_css_uses_theme_variables() -> None:
    """分支条的样式必须走主题变量（写死浅色会被暗色主题守门人测试抓到）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    css = (root / "web" / "css" / "styles.css").read_text(encoding="utf-8")
    assert ".branch-bar" in css
    assert ".branch-tab" in css
    block_start = css.index(".branch-bar")
    block = css[block_start : block_start + 1800]
    assert "var(--surface" in block, "分支条应当用主题变量做背景"
