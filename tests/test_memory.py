"""3.9 长期记忆与向量召回的测试（集成测试，需要 MySQL 与 ChromaDB）。

==================== 覆盖范围 ====================
  一、写入与召回
      · 一轮对话写进向量库；用意思相近但字面不同的问法能召回（语义检索生效）
      · 相似度门槛把不相关的内容挡在外面
      · 幂等：同一轮重复保存不会产生重复记忆
      · 会话隔离：默认只召回本会话，跨会话要显式开启
      · ★ 向量库坏了不能抛异常（记忆是锦上添花，不该让对话失败）
  二、接入叙事流程
      · 召回的回忆出现在**系统提示词**里（这是"接进去了"的唯一证据）
      · 也出现在接口返回的 context 统计里（界面能看到）
      · 向量库故障时**降级**：对话依然成功，但 memory_error 如实回报
  三、一致性（★ 最容易出事的地方）
      · 删会话 -> 该会话的记忆一起消失（不留"幽灵记忆"）
      · 清空记忆 -> 只清记忆，不碰对话记录
  四、记忆接口
      · GET/POST /sessions/{id}/memories、DELETE /memories
      · 越权：别人的会话一律 404；空内容 422

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_memory.py -q
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.chroma import MemoryRecord
from app.db.chroma import add_memories as chroma_add_memories
from app.db.chroma import search_memories
from app.db.models import User
from app.db.mysql import session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine, memory

BASE = "/api/v1/narrative/sessions"
MEMORIES = "/api/v1/narrative/memories"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


# ==================================================================
#  夹具
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"mm_{token}",
        "email": f"mm_{token}@example.com",
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
        "headers": {
            "Authorization": f"Bearer {logged_in.json()['data']['access_token']}"
        },
    }


def _cleanup_user(username: str, user_id: int) -> None:
    """删账号 + 清向量库（★ 数据库与向量库两边都得清）。"""
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == username))
        if row is not None:
            db.delete(row)
    memory.forget_all(user_id)


@pytest.fixture
def user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    _cleanup_user(data["username"], data["id"])


@pytest.fixture
def other_user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    _cleanup_user(data["username"], data["id"])


class _Row:
    """鸭子类型的消息行（单元测试用，避免为了存两条消息去建库）。"""

    def __init__(self, mid: int, content: str, role: str = "user") -> None:
        self.id = mid
        self.content = content
        self.role = role


class FakeAdapter:
    """不联网的假适配器（记录收到的请求，供断言提示词用）。"""

    script: dict = {}

    def __init__(self, row=None, **_):
        self.row = row
        self.default_params = GenerationParams(max_tokens=512)

    @property
    def budget(self):
        window = getattr(self.row, "context_window", None) or 8192
        max_out = getattr(self.row, "max_tokens", None) or 1024
        return compute_context_budget(window, max_out)

    def chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        return ChatResult(
            content=FakeAdapter.script.get("content", "（假回复）"),
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        yield StreamChunk(delta=FakeAdapter.script.get("content", "（假回复）"))
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(total_tokens=15))

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """只替换模型适配器；**记忆走真实的 ChromaDB**（这一步测的就是它）。"""
    FakeAdapter.script = {}
    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    yield FakeAdapter
    FakeAdapter.script = {}


def make_card(client: TestClient, user: dict, **overrides) -> dict:
    body = {
        "name": f"记忆卡_{uuid.uuid4().hex[:6]}",
        "personality": "记性很好",
        "greeting": "……你来了。",
    }
    body.update(overrides)
    response = client.post(CARDS, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_provider(client: TestClient, user: dict) -> dict:
    response = client.post(
        PROVIDERS,
        json={
            "name": f"mem_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-memory-test",
            "model_name": "mock-model",
            "context_window": 8192,
            "generation": {"temperature": 0.8, "max_tokens": 1024},
        },
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_session(client: TestClient, user: dict, card_id: int, provider_id: int) -> dict:
    response = client.post(
        BASE,
        json={"character_card_id": card_id, "llm_provider_id": provider_id},
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def send(client: TestClient, user: dict, session_id: int, text: str):
    return client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": text},
        headers=user["headers"],
    )


def last_request_messages():
    requests = FakeAdapter.script.get("requests") or []
    assert requests, "假适配器没有收到请求"
    return requests[-1].messages


# ==================================================================
#  一、写入与召回（直接测记忆层）
# ==================================================================
def test_remember_turn_is_idempotent(user: dict) -> None:
    """同一轮对话重复保存不能产生重复记忆（流式重试、用户重发都会走到这里）。"""
    row_user = _Row(101, "我最喜欢的颜色是墨绿色")
    row_assistant = _Row(102, "记住了，墨绿色。", role="assistant")

    for _ in range(3):
        error = memory.remember_turn(
            user_id=user["id"],
            session_id=4242,
            user_message=row_user,
            assistant_message=row_assistant,
            char_name="记忆卡",
        )
        # ★ 写入口的约定：空字符串 = 成功，非空 = 失败原因
        assert error == "", error

    hits = search_memories(user["id"], "喜欢的颜色", top_k=10, session_id=4242)
    assert len(hits) == 1, f"重复保存产生了 {len(hits)} 条记忆"
    assert "墨绿色" in hits[0].text
    assert "记忆卡" in hits[0].text, "记忆里要带上是哪个角色说的"

    memory.forget_session(user["id"], 4242)


def test_recall_uses_semantic_similarity_not_keywords(user: dict) -> None:
    """★ 语义检索的意义：问法里不出现的字，也能召回。

    "我养了一只叫团子的橘猫" 与 "宠物" 字面上不重合，
    靠关键词匹配永远找不到 —— 这正是向量记忆比世界书关键词强的地方。
    """
    chroma_add_memories(
        user["id"],
        [
            MemoryRecord(
                text="用户：我养了一只叫团子的橘猫\n角色：团子这名字真可爱。",
                memory_id="sem-1",
                session_id=5555,
                kind="dialogue",
            )
        ],
    )

    result = memory.recall(user_id=user["id"], session_id=5555, query="我的宠物叫什么名字")
    assert not result.error, result.error
    assert result.hits, "语义相近的问题应当能召回到这条记忆"
    assert "团子" in result.hits[0].text

    memory.forget_session(user["id"], 5555)


def test_recall_filters_low_similarity(user: dict) -> None:
    """相似度门槛必须生效：不相关的内容宁可不要。"""
    chroma_add_memories(
        user["id"],
        [
            MemoryRecord(
                text="用户：帮我算一下 3 的平方根\n角色：约等于 1.732。",
                memory_id="low-1",
                session_id=6666,
                kind="dialogue",
            )
        ],
    )

    # 门槛设成 0.99，任何真实文本都过不了 —— 用来证明门槛确实在起作用
    strict = memory.recall(
        user_id=user["id"],
        session_id=6666,
        query="今天晚饭吃什么",
        min_similarity=0.99,
    )
    assert not strict.hits
    assert strict.filtered >= 1, "被过滤掉的条数要如实统计（界面要展示）"

    memory.forget_session(user["id"], 6666)


def test_recall_is_session_scoped_by_default(user: dict) -> None:
    """★ 默认不跨会话：别的故事里的内容不能串进来。"""
    chroma_add_memories(
        user["id"],
        [
            MemoryRecord(
                text="用户：我们约在旅店后门见面\n角色：好，我等你。",
                memory_id="sc-1",
                session_id=7777,
                kind="dialogue",
            )
        ],
    )

    same = memory.recall(user_id=user["id"], session_id=7777, query="我们约在哪里见面")
    other = memory.recall(user_id=user["id"], session_id=8888, query="我们约在哪里见面")
    cross = memory.recall(
        user_id=user["id"], session_id=8888, query="我们约在哪里见面", cross_session=True
    )

    assert same.hits, "本会话内应当能召回"
    assert not other.hits, "别的会话不该看到这条记忆"
    assert cross.hits, "显式开启跨会话后才允许召回"

    memory.forget_session(user["id"], 7777)


def test_memory_layer_never_raises(monkeypatch: pytest.MonkeyPatch, user: dict) -> None:
    """★ 向量库坏了也不能抛异常：记忆是锦上添花，不该让对话失败。

    注意这里替换的是 **memory 模块里引用到的那两个函数**（模块在做
    `from app.db.chroma import add_memories` 时就把引用固定下来了，
    所以只 patch `app.db.chroma` 里的同名函数是无效的 —— 这个坑真实踩过）。
    """

    def boom(*_, **__):
        raise RuntimeError("向量库炸了")

    monkeypatch.setattr(memory, "search_memories", boom)
    monkeypatch.setattr(memory, "add_memories", boom)

    result = memory.recall(user_id=user["id"], session_id=1, query="随便问问")
    assert result.error and "向量库炸了" in result.error
    assert result.hits == []

    # 写入口同样不能抛：失败时返回**错误说明字符串**（空字符串才表示成功）
    # 这里只断言"有错误"，不断言具体异常文本 —— 对外给的是稳定的用户可读原因，
    # 异常细节只进服务端日志（避免把内部实现泄露到界面）
    assert memory.remember_fact(user["id"], session_id=1, text="一句话")
    assert memory.remember_turn(
        user_id=user["id"],
        session_id=1,
        user_message=_Row(1, "你好"),
        assistant_message=_Row(2, "你也好", role="assistant"),
    )

    # 清理也不能抛：删会话时会调用它，不能把"删会话"本身搞失败
    # （该用户此刻还没有集合，所以这里走的是"没有集合就直接返回"的路径）
    assert memory.forget_session(user["id"], 1) is None


# ==================================================================
#  二、接入叙事流程
# ==================================================================
def test_recalled_memory_reaches_system_prompt(client: TestClient, user: dict) -> None:
    """★ 关键断言：召回的内容真的出现在**发给模型的系统提示词**里。"""
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    chroma_add_memories(
        user["id"],
        [
            MemoryRecord(
                text="用户：我有一把祖传的青铜钥匙\n角色：那把钥匙我一直替你收着。",
                memory_id="inj-1",
                session_id=session["id"],
                kind="dialogue",
            )
        ],
    )

    response = send(client, user, session["id"], "我的那把钥匙还在吗")
    assert response.status_code == 201, response.text

    system = [m for m in last_request_messages() if m.is_system]
    assert system, "必须有系统提示词"
    assert "青铜钥匙" in system[0].content, "召回的回忆必须拼进系统提示词"
    assert "相关回忆" in system[0].content

    data = response.json()["data"]
    assert data["context"]["recalled_memories"] >= 1
    assert data["prompt"]["recalled_memories"] >= 1
    assert not data["context"]["memory_error"]


def test_no_recall_when_nothing_relevant(client: TestClient, user: dict) -> None:
    """★ 反过来的那条：没有相关记忆时，提示词里不能出现回忆小节。

    只测正面很容易骗过自己（比如"回忆小节永远存在、只是内容为空"）。
    """
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = send(client, user, session["id"], "今天天气不错")
    assert response.status_code == 201, response.text

    system = [m for m in last_request_messages() if m.is_system]
    assert "相关回忆" not in system[0].content
    assert response.json()["data"]["context"]["recalled_memories"] == 0


def test_turn_is_written_into_long_term_memory(client: TestClient, user: dict) -> None:
    """一轮对话结束后，它必须能被检索回来。"""
    FakeAdapter.script["content"] = "记住了：你在北境有个妹妹。"
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    send(client, user, session["id"], "我在北境有一个妹妹")

    result = memory.recall(
        user_id=user["id"], session_id=session["id"], query="北境 妹妹"
    )
    assert not result.error, result.error
    assert result.hits, "刚聊过的内容应当已经写进长期记忆"
    assert any("妹妹" in hit.text for hit in result.hits)


def test_memory_failure_degrades_without_breaking_chat(
    client: TestClient, user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 向量库不可用时：对话照样成功，但必须**如实**把失败原因回报给界面。"""
    FakeAdapter.script["content"] = "（照常回复）"
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    def boom(*_, **__):
        raise RuntimeError("嵌入模型未下载")

    monkeypatch.setattr(memory, "search_memories", boom)

    response = send(client, user, session["id"], "你好")
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["assistant_message"]["content"] == "（照常回复）"
    assert data["context"]["memory_error"], "必须把降级原因告诉用户"
    assert "嵌入模型未下载" in data["context"]["memory_error"]


def test_prompt_preview_matches_the_real_request(
    client: TestClient, user: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 预览与真实请求必须走同一条构建路径。

    曾经出现的 bug：会话详情里的「查看提示词」用的是"注入全部世界书条目"的
    简化路径，而真正发请求时用的是"只注入命中的条目" ——
    于是预览显示 2 条、实际只发 1 条，用户拿着预览去排查一个不存在的问题。
    """
    card = make_card(client, user)
    provider = make_provider(client, user)
    book = client.post(
        "/api/v1/world-books",
        json={
            "name": f"预览书_{uuid.uuid4().hex[:6]}",
            "entries": [
                {"keys": ["灯塔"], "content": "灯塔在风暴夜会熄灭", "enabled": True},
                {"keys": ["巨龙"], "content": "巨龙已死去", "enabled": True},
            ],
        },
        headers=user["headers"],
    ).json()["data"]
    client.patch(
        f"{CARDS}/{card['id']}",
        json={"world_book_id": book["id"]},
        headers=user["headers"],
    )

    send(client, user, session_id := make_session(client, user, card["id"], provider["id"])["id"], "灯塔还亮着吗")

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["prompt"]["world_book_entries"] == 1, (
        "预览里只该有命中的那 1 条，而不是世界书全部 2 条"
    )
    assert "巨龙已死去" not in detail["prompt"]["system_prompt"]


# ==================================================================
#  三、一致性：删数据时向量库也要清
# ==================================================================
def test_deleting_session_also_forgets_its_memories(
    client: TestClient, user: dict
) -> None:
    """★ 删会话必须同时清掉它的长期记忆，否则会留下"幽灵记忆"。"""
    FakeAdapter.script["content"] = "好的，我记住了那盏灯。"
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    send(client, user, session["id"], "码头尽头有一盏绿灯")
    before = search_memories(user["id"], "绿灯", top_k=5, session_id=session["id"])
    assert before, "前置条件：这一轮应该已经写进记忆了"

    response = client.delete(f"{BASE}/{session['id']}", headers=user["headers"])
    assert response.status_code == 200, response.text

    after = search_memories(user["id"], "绿灯", top_k=5, session_id=session["id"])
    assert after == [], "会话删了，记忆也必须一起消失"


def test_clearing_all_memories_keeps_conversations(
    client: TestClient, user: dict
) -> None:
    """清空记忆只影响"记忆"，绝不能碰对话记录。"""
    FakeAdapter.script["content"] = "记下了。"
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])
    send(client, user, session["id"], "我讨厌薄荷糖")

    response = client.delete(MEMORIES, headers=user["headers"])
    assert response.status_code == 200, response.text

    assert search_memories(user["id"], "薄荷糖", top_k=5) == [], "记忆应当被清空"

    detail = client.get(f"{BASE}/{session['id']}", headers=user["headers"]).json()["data"]
    assert any("薄荷糖" in m["content"] for m in detail["messages"]), (
        "对话记录不能被一起清掉"
    )


# ==================================================================
#  四、记忆接口
# ==================================================================
def test_memory_endpoints_crud(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    created = client.post(
        f"{BASE}/{session['id']}/memories",
        json={"text": "约定：每逢满月在小镇钟楼碰面"},
        headers=user["headers"],
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["total"] >= 1

    listed = client.get(
        f"{BASE}/{session['id']}/memories",
        params={"q": "满月"},
        headers=user["headers"],
    )
    assert listed.status_code == 200, listed.text
    hits = listed.json()["data"]["hits"]
    assert hits and hits[0]["kind"] == "fact"
    assert "钟楼" in hits[0]["text"]


def test_memory_endpoints_require_ownership(
    client: TestClient, user: dict, other_user: dict
) -> None:
    card = make_card(client, other_user)
    provider = make_provider(client, other_user)
    session = make_session(client, other_user, card["id"], provider["id"])

    assert (
        client.get(f"{BASE}/{session['id']}/memories", headers=user["headers"]).status_code
        == 404
    )
    assert (
        client.post(
            f"{BASE}/{session['id']}/memories",
            json={"text": "偷看"},
            headers=user["headers"],
        ).status_code
        == 404
    )


def test_memory_endpoints_reject_blank_text(client: TestClient, user: dict) -> None:
    card = make_card(client, user)
    provider = make_provider(client, user)
    session = make_session(client, user, card["id"], provider["id"])

    response = client.post(
        f"{BASE}/{session['id']}/memories",
        json={"text": "   "},
        headers=user["headers"],
    )
    assert response.status_code == 422, response.text


def test_memory_endpoints_require_login(client: TestClient) -> None:
    assert client.get(f"{BASE}/1/memories").status_code == 401
    assert client.delete(MEMORIES).status_code == 401
