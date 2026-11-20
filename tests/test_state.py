"""结构化状态（HP / 背包 / 位置 / 任务）的解析、校验、落库与回注。

分层与 test_memory.py 一致：
  · 纯函数部分直接调 `app.narrative.state`（不碰库，跑得快）；
  · 端到端部分用假适配器跑一轮真实接口，验证"剥块 → 落库 → 下一轮回注"整条链。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Message, User
from app.db.mysql import session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative import state as state_mod
from app.narrative import state_schema as schemas

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


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
    FakeAdapter.script = {}
    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    monkeypatch.setattr(engine.memory_mod, "remember_turn", lambda **_: None)
    yield FakeAdapter
    FakeAdapter.script = {}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"st_{token}",
        "email": f"st_{token}@example.com",
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


@pytest.fixture
def user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == data["username"]))
        if row is not None:
            db.delete(row)


def _session_with(client: TestClient, user: dict, card: dict | None = None) -> int:
    if card is None:
        # ★ 不声明状态栏格式的卡会得到**空 schema**（状态栏显示"未定义"、不注入协议），
        #   所以这条链路测试的卡必须自己声明 —— 字段清单是卡的，不是代码的。
        #   `extensions` 只在 V2 导入格式里有位置，所以走 import 接口建这张卡。
        created = client.post(
            f"{CARDS}/import",
            json={
                "card": {
                    "spec": "chara_card_v2",
                    "data": {
                        "name": f"状态卡_{uuid.uuid4().hex[:6]}",
                        "first_mes": "……你来了。",
                        "extensions": {
                            "hne": {"state_schema": LEGACY_SCHEMA["fields"]}
                        },
                    },
                }
            },
            headers=user["headers"],
        )
        assert created.status_code == 201, created.text
        card_id = created.json()["data"]["id"]
    else:
        # 走**真正的导入接口**：初始状态那两条路（extensions.hne / 开场白里的状态块）
        # 都必须在"导入 → 建会话"这条真实链路上成立，不能只在单元层面成立。
        imported = client.post(
            f"{CARDS}/import", json={"card": card}, headers=user["headers"]
        )
        assert imported.status_code == 201, imported.text
        card_id = imported.json()["data"]["id"]
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"st_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-state-test",
            "model_name": "mock-model",
            "context_window": 8192,
            "generation": {"temperature": 0.8, "max_tokens": 1024},
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    session = client.post(
        BASE,
        json={
            "character_card_id": card_id,
            "llm_provider_id": provider.json()["data"]["id"],
        },
        headers=user["headers"],
    )
    assert session.status_code == 201, session.text
    return session.json()["data"]["id"]


# ==================================================================
#  二、校验与合并
# ==================================================================
#: 测试用 schema（="这张卡声明了哪些状态字段"）。
#  ★ 字段清单不再由 state.py 写死，所以这里必须显式给一份 ——
#    这正是本次改动的目的：**没声明的字段不该凭空出现**。
LEGACY_SCHEMA = schemas.legacy_schema()


# ==================================================================
#  三、端到端：剥块 → 落库 → 下一轮回注
# ==================================================================
def test_reply_state_is_stored_stripped_and_reinjected(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _session_with(client, user)
    fake_llm.script["content"] = (
        "我推开门，风灌了进来。\n"
        '<state>{"hp": {"current": 80, "max": 100}, "inventory": ["铜钥匙"],'
        ' "location": "钟楼", "quests": [{"title": "找猫", "status": "active"}]}</state>'
    )
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我上楼了"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    assistant = sent.json()["data"]["assistant_message"]
    stored = assistant["content"]
    assert "<state>" not in stored, "落库的正文里不能留原始 JSON"
    assert stored.startswith("我推开门")

    # ★ 第十六轮：原始块单独留一份（界面「看作者原格式」用）——
    #   卡作者写的复杂/美化排版在按 schema 解析成字段后就没了，所以必须另存原文。
    assert assistant["state_raw"], "模型这一轮输出了 <state>，原文就该留下来"
    assert '"铜钥匙"' in assistant["state_raw"], "原文要是**未解析**的那一份"
    assert assistant["state_raw"].startswith("{"), (
        "存的是块里的原文，不含 <state> 标签本身"
    )

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    # meter 字段按 schema 扁平存（hp 与它的上限 max 是两个键，不是嵌套对象）
    assert detail["state"]["hp"] == 80 and detail["state"]["max"] == 100
    assert detail["state"]["inventory"] == ["铜钥匙"]
    assert detail["state"]["location"] == "钟楼"
    # 刷新后原文还在（落库了，不是只在响应里）
    stored_rows = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert stored_rows[-1]["state_raw"] == assistant["state_raw"]

    # ★ 遥测 JSON 里**不能**混进原文：漂移统计只认 counts/deviation，
    #   原文单独一列（否则两份数据会各说各话）
    with session_scope() as db:
        row = db.get(Message, stored_rows[-1]["id"])
        meta = json.loads(row.state_meta_json or "{}")
    assert "raw" not in meta, "遥测里不该带原文（它在 state_raw_json 那一列）"

    # 第二轮：提示词里必须带上"当前状态"和上一轮的值（否则模型下一轮必然写飘）
    fake_llm.script["content"] = "钟楼里很暗。"
    again = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我看看四周"},
        headers=user["headers"],
    )
    assert again.status_code == 201, again.text
    system = fake_llm.script["requests"][-1].messages[0].content
    assert state_mod.BLOCK_TITLE in system
    assert '"钟楼"' in system and '"铜钥匙"' in system
    # 状态必须排在最高优先级守卫**之前**（这条是真实事故换来的，别改回去）
    assert system.index(state_mod.BLOCK_TITLE) < system.index("[最高优先级 · 身份认知]")


def test_broken_state_block_does_not_break_the_turn(
    client: TestClient, user: dict, fake_llm
) -> None:
    """模型把状态块写坏时，对话必须照常完成（状态只是锦上添花）。"""
    session_id = _session_with(client, user)
    fake_llm.script["content"] = "……（沉默）\n<state>{坏掉的 JSON</state>"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "你在吗"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["state"] is None, "解析失败时不能写入半截状态"
    assert "<state>" not in detail["messages"][-1]["content"]


def test_contract_sits_after_the_identity_guard(
    client: TestClient, user: dict, fake_llm
) -> None:
    """契约必须在守卫**之后** —— 夹在中间时真实模型会直接忽略它。"""
    session_id = _session_with(client, user)
    fake_llm.script["content"] = "灯还亮着。"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "看灯"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    system = fake_llm.script["requests"][-1].messages[0].content
    assert state_mod.CONTRACT_TITLE in system
    assert system.index(state_mod.CONTRACT_TITLE) > system.index(
        "[最高优先级 · 身份认知]"
    )


def test_end_to_end_card_schema_from_world_book(client: TestClient, user: dict) -> None:
    """端到端：世界书里写格式 → 建会话 → 状态栏字段就是世界书里那些。"""
    book = client.post(
        "/api/v1/world-books",
        json={
            "name": f"魔女世界_{uuid.uuid4().hex[:6]}",
            "entries": [
                {
                    "name": "[状态栏] 魔女裁判",
                    "keys": [],
                    "content": (
                        "每轮末尾输出状态块，字段：魔力、变身。\n"
                        '<state>{"魔力": 80, "变身": "已变身"}</state>'
                    ),
                    "enabled": True,
                }
            ],
        },
        headers=user["headers"],
    )
    assert book.status_code == 201, book.text
    book_id = book.json()["data"]["id"]
    card = client.post(
        CARDS,
        json={
            "name": f"魔法少女_{uuid.uuid4().hex[:6]}",
            "greeting": "……",
            "world_book_id": book_id,
        },
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"st_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-state-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    session_id = client.post(
        BASE,
        json={
            "character_card_id": card.json()["data"]["id"],
            "llm_provider_id": provider.json()["data"]["id"],
        },
        headers=user["headers"],
    ).json()["data"]["id"]
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    schema = detail["state_schema"]
    assert schema["source"] == "world_book"
    assert [f["name"] for f in schema["fields"]] == ["魔力", "变身"]
    assert "hp" not in json.dumps(schema, ensure_ascii=False), "★ 这张卡不该有 HP"


# ==================================================================
#  五、初始状态：卡片自带，状态栏从第一轮就亮
# ==================================================================
def test_card_extension_initial_state_fills_the_bar(
    client: TestClient, user: dict
) -> None:
    session_id = _session_with(
        client,
        user,
        card={
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "自带初始状态的卡",
                "first_mes": "风把门吹响了。",
                "extensions": {
                    "hne": {
                        "initial_state": {
                            "hp": {"current": 70, "max": 90},
                            "location": "灯塔一层",
                            "inventory": ["提灯"],
                        }
                    }
                },
            },
        },
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    # ★ 字段清单**从 initial_state 推断**（这张卡没写 state_schema，也没世界书）：
    #   嵌套写法 {"hp":{"current","max"}} 被识别成一条 meter，所以 hp 是**数字**；
    #   HP 只在这张卡真的写了 hp 时才出现（这正是修掉"灯塔的 HP 跑到魔法少女身上"的关键）。
    assert detail["state"]["hp"] == 70
    assert detail["state"]["location"] == "灯塔一层"
    assert detail["state"]["inventory"] == ["提灯"]
    assert detail["state_schema"]["source"] == "initial_state"
    assert [f["name"] for f in detail["state_schema"]["fields"]] == [
        "hp",
        "location",
        "inventory",
    ]


def test_greeting_state_block_is_stripped_even_without_a_schema(
    client: TestClient, user: dict
) -> None:
    """没定义状态栏的卡：开场白里的 <state> 也要剥掉（不能给用户看原始 JSON），但不落库。"""
    session_id = _session_with(
        client,
        user,
        card={
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "没定义状态栏的卡",
                "first_mes": '他抬起头看你。\n<state>{"location": "钟楼", "hp": {"current": 40, "max": 100}}</state>',
            },
        },
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["state"] is None, "没声明字段的卡不该被凭空塞一份状态"
    assert detail["state_schema"]["fields"] == []
    texts = [m["content"] for m in detail["messages"]]
    assert any("他抬起头看你" in t for t in texts), texts
    assert all("<state>" not in t for t in texts), "开场白里的状态块必须剥掉"


def test_greeting_state_block_becomes_initial_state_and_is_stripped(
    client: TestClient, user: dict
) -> None:
    """开场白里自带 <state> 块的老卡（并声明了字段）：状态要落库，正文里不能留 JSON。"""
    session_id = _session_with(
        client,
        user,
        card={
            "spec": "chara_card_v2",
            "spec_version": "2.0",
            "data": {
                "name": "开场白自带状态的卡",
                "first_mes": '他抬起头看你。\n<state>{"location": "钟楼", "hp": {"current": 40, "max": 100}}</state>',
                "extensions": {"hne": {"state_schema": LEGACY_SCHEMA["fields"]}},
            },
        },
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["state"]["location"] == "钟楼"
    assert detail["state"]["hp"] == 40 and detail["state"]["max"] == 100
    texts = [m["content"] for m in detail["messages"]]
    assert any("他抬起头看你" in t for t in texts), texts
    assert all("<state>" not in t for t in texts), "开场白里的状态块必须剥掉"


pytestmark = pytest.mark.integration
