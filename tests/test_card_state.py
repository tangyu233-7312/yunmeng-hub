"""卡自带 JSON 状态块（第二状态存储）：识别、剥块、落库、优先级、漂移检查。

==================== 这组测试守着什么 ====================
用户 2026-10-20 拍板做 P2：
  ① **尽量都认**（`<state>` / JSON 围栏 / 酒馆 MVU 指令块 / 卡内嵌 JSON 脚本 / 自定义 JSON 标签）；
  ② 显示按**作者优先**（卡状态在上，系统状态块收起为细条），但**两套数据都落库**；
  ③ 用**独立表**存卡状态（`card_states`），按形状分桶；
  ④ 卡状态**不注入提示词**；
  ⑤ 漂移检查只做能确证的「字段消失 / 类型变化」，不做数值偏移。

所以这里的断言分成两类：
  · **纯函数**（识别 / 剥块 / diff）：不碰库，跑得快，把规则钉死；
  · **端到端**：真跑一轮假模型回复 → 看库里有没有那份卡状态、正文里有没有残留 JSON、
    提示词里有没有被偷偷塞进去。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import CardState, User
from app.db.mysql import session_scope
from app.main import app
from app.narrative import card_state as cs
from app.narrative import engine
from app.narrative import state as state_mod

from tests.test_state import FakeAdapter  # noqa: E402 - 复用同一套假适配器（见 fake_llm）

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    """★ 这个文件**必须**自己装假适配器。

    `tests/test_state.py` 里那个 `fake_llm` 是**模块级 autouse** —— 它只对它自己那个
    模块生效。曾经以为"同一个包里就共享"，结果这里真去打 mock.invalid，测试红在超时上。
    """
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
        "username": f"cs_{token}",
        "email": f"cs_{token}@example.com",
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
            db.delete(row)  # 会话/消息/卡状态都靠外键 CASCADE 清掉


def _session_with(client: TestClient, user: dict, first_mes: str) -> int:
    """建一张**没有声明 state_schema** 的卡（= 作者自带状态块的典型卡）并开会话。"""
    imported = client.post(
        f"{CARDS}/import",
        json={
            "card": {
                "spec": "chara_card_v2",
                "spec_version": "2.0",
                "data": {
                    "name": f"自带状态卡_{uuid.uuid4().hex[:6]}",
                    "first_mes": first_mes,
                },
            }
        },
        headers=user["headers"],
    )
    assert imported.status_code == 201, imported.text
    card_id = imported.json()["data"]["id"]
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"cs_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-card-state-test",
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


def _reply(client: TestClient, user: dict, session_id: int, text: str) -> dict:
    """发一句话、让假模型回 `text`，返回会话详情（**真正跑完落库**之后读）。"""
    FakeAdapter.script["content"] = text
    FakeAdapter.script["stream"] = [text]
    with client.stream(
        "GET",
        f"{BASE}/{session_id}/stream",
        headers=user["headers"],
        params={"content": "继续"},
    ) as resp:
        assert resp.status_code == 200, resp.status_code
        for _chunk in resp.iter_text():
            pass
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"])
    assert detail.status_code == 200, detail.text
    return detail.json()["data"]


# ==================================================================
#  二、端到端：真跑一轮回复
# ==================================================================
def test_card_state_block_is_stored_separately_and_stripped(
    client: TestClient, user: dict
) -> None:
    """★ 核心链路：模型回复末尾的卡状态块 → 正文干净、单独落库、会话详情能读到。"""
    session_id = _session_with(client, user, "风把门吹响了。")
    detail = _reply(
        client, user, session_id, '她抬头。\n```json\n{"hp": 66, "mood": "警觉"}\n```'
    )
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
    assert "```" not in assistant["content"], "围栏不能被留在正文里"
    assert '"hp"' not in assistant["content"], "原始 JSON 绝不能留在正文里"
    assert "她抬头" in assistant["content"], "正文必须留下"

    panel = detail["card_state"]
    assert panel["has_value"] is True
    assert panel["value"] == {"hp": 66, "mood": "警觉"}
    assert panel["source"] == cs.SRC_FENCE
    assert panel["label"] == "JSON 围栏"
    assert panel["updates"] == 1

    # ★ 两套存储互不覆盖：本系统那份照常是空（这张卡没声明 schema）
    assert detail["state"] is None
    assert detail["state_schema"]["fields"] == []

    # 真的落进独立表（而不是只在详情接口里现算）
    with session_scope() as db:
        rows = list(
            db.scalars(
                select(CardState).where(CardState.session_id == session_id)
            ).all()
        )
    assert len(rows) == 1
    assert rows[0].source == cs.SRC_FENCE
    assert json.loads(rows[0].data_json) == {"hp": 66, "mood": "警觉"}


def test_card_state_updates_in_place_and_drift_is_recorded(
    client: TestClient, user: dict
) -> None:
    """★ 同一形状更新同一行（不每轮新开一行），字段消失会被记下来。

    ★ 键集合保持不变 —— 这里的重点是"同一形状 → 同一行"，
      而"少了 mp"其实会**改变形状**（见 `test_different_shape_gets_its_own_row`：
      形状按**键集合**分桶，少一个键就是另一种形状，会新开一桶）。
      这两个语义同时存在是有意的：字段消失既要**被报告**，也**不该覆盖**旧数据。
    """
    session_id = _session_with(client, user, "风把门吹响了。")
    _reply(client, user, session_id, '<state>{"hp": 90, "mp": 30}</state>')
    detail = _reply(client, user, session_id, '<state>{"hp": 80, "mp": 20}</state>')

    panel = detail["card_state"]
    assert panel["value"] == {"hp": 80, "mp": 20}
    assert panel["updates"] == 2, "同形状应更新同一行"
    # 数值变了不算漂移（没有 schema 就没有"该往哪边算"的定义）
    assert panel["drift"]["missing"] == []
    assert panel["drift"]["retyped"] == []

    with session_scope() as db:
        rows = list(
            db.scalars(
                select(CardState).where(CardState.session_id == session_id)
            ).all()
        )
    assert len(rows) == 1, "同一形状不该新增行"


def test_different_shape_gets_its_own_row(client: TestClient, user: dict) -> None:
    """★ 换了一套字段（卡改版）→ 新开一桶，**旧数据不会被覆盖**。

    这是"独立表 + 形状分桶"的意义：用户切回旧版卡时还看得到当时那份状态。
    """
    session_id = _session_with(client, user, "风把门吹响了。")
    _reply(client, user, session_id, '<state>{"hp": 90}</state>')
    detail = _reply(client, user, session_id, '<state>{"energy": 5, "day": 2}</state>')
    assert detail["card_state"]["value"] == {"energy": 5, "day": 2}

    with session_scope() as db:
        rows = list(
            db.scalars(
                select(CardState).where(CardState.session_id == session_id)
            ).all()
        )
    assert len(rows) == 2, "新旧两种形状必须各留一份"
    shapes = {json.dumps(json.loads(r.data_json), sort_keys=True) for r in rows}
    assert '{"hp": 90}' in shapes


def test_card_state_is_never_injected_into_prompt(
    client: TestClient, user: dict
) -> None:
    """★ 卡状态**不注入提示词**：那是作者的东西，我们只保管。

    守着一个很容易"顺手做掉"的越权：既然我们有了卡状态，看起来"下一轮喂回去"
    很自然 —— 但那等于本系统开始替卡作者维护状态，与本系统自己的 state 冲突。
    """
    session_id = _session_with(client, user, "风把门吹响了。")
    _reply(client, user, session_id, '<state>{"独一无二的字段名_xyz": 7}</state>')
    prompt = client.get(
        f"{BASE}/{session_id}", headers=user["headers"], params={"with_prompt": True}
    )
    assert prompt.status_code == 200, prompt.text
    # ★ 只查**提示词那一节**（`prompt`）：整份响应里当然会有 card_state 面板
    #   （界面要显示它）—— 拿整份 JSON 去断言"没出现"是自欺欺人的写法，会永远红。
    preview = prompt.json()["data"]["prompt"]
    assert preview is not None
    assert "独一无二的字段名_xyz" not in json.dumps(preview, ensure_ascii=False), (
        "卡状态被注入提示词了：它只该存在第二存储里"
    )


def test_system_state_and_card_state_coexist(client: TestClient, user: dict) -> None:
    """★ 两套状态并存：卡声明了 schema 时 `<state>` 归系统，卡自己的围栏归第二存储。

    这是"两套都解析、都落库，只让显示按优先级二选一"的最小证明。
    """
    imported = client.post(
        f"{CARDS}/import",
        json={
            "card": {
                "spec": "chara_card_v2",
                "spec_version": "2.0",
                "data": {
                    "name": f"两套状态卡_{uuid.uuid4().hex[:6]}",
                    "first_mes": "她站在门口。",
                    "extensions": {
                        "hne": {
                            "state_schema": [
                                {
                                    "name": "hp",
                                    "type": "meter",
                                    "label": "HP",
                                    "max": 100,
                                }
                            ]
                        }
                    },
                },
            }
        },
        headers=user["headers"],
    )
    assert imported.status_code == 201, imported.text
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"cs_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-card-state-test",
            "model_name": "mock-model",
            "context_window": 8192,
            "generation": {"temperature": 0.8, "max_tokens": 1024},
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    created = client.post(
        BASE,
        json={
            "character_card_id": imported.json()["data"]["id"],
            "llm_provider_id": provider.json()["data"]["id"],
        },
        headers=user["headers"],
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["data"]["id"]

    detail = _reply(
        client,
        user,
        session_id,
        '<state>{"hp": {"current": 55}}</state>\n```json\n{"作者字段": 1}\n```',
    )
    assert detail["state"]["hp"] == 55, "声明了 schema：<state> 归系统"
    assert detail["card_state"]["has_value"] is True
    assert detail["card_state"]["value"] == {"作者字段": 1}, "围栏归卡"
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
    assert "<state>" not in assistant["content"] and "```" not in assistant["content"]


def test_greeting_card_state_is_stored_and_stripped(
    client: TestClient, user: dict
) -> None:
    """开场白里的卡状态块：同样要剥、同样要落库（建会话那条路不能漏）。"""
    session_id = _session_with(
        client, user, '她抬眼看你。\n```json\n{"开场状态": true}\n```'
    )
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["card_state"]["has_value"] is True
    assert detail["card_state"]["value"] == {"开场状态": True}
    texts = [m["content"] for m in detail["messages"]]
    assert all("```" not in t for t in texts), texts
    assert any("她抬眼看你" in t for t in texts)


def test_pure_chat_never_collects_card_state(client: TestClient, user: dict) -> None:
    """纯聊天：没有卡，就别去收"卡的状态"（面板如实报 enabled=false）。"""
    created = client.post(
        BASE, json={"pure_chat": True, "title": "纯聊天卡状态"}, headers=user["headers"]
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["data"]["id"]
    # ★ 纯聊天会话**没有模型配置**（建会话接口不会替你挑一个），不配的话这条流不会产出
    #   任何回复 —— 断言会以 IndexError 的形式失败，看起来像"状态没收"，其实是没回复。
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"cs_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-pure-chat-test",
            "model_name": "mock-model",
            "context_window": 8192,
            "generation": {"temperature": 0.8, "max_tokens": 1024},
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    patched = client.patch(
        f"{BASE}/{session_id}",
        json={"llm_provider_id": provider.json()["data"]["id"]},
        headers=user["headers"],
    )
    assert patched.status_code == 200, patched.text
    detail = _reply(client, user, session_id, '好。\n<state>{"不该被收": 1}</state>')
    assert detail["card_state"]["enabled"] is False
    # ★ 但 `<state>` 仍然要剥掉：绝不能让用户看到原始 JSON（与以前一致）
    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
    assert "<state>" not in assistant["content"]


def test_card_state_panel_reports_unseen_hint(client: TestClient, user: dict) -> None:
    """还没收到过卡状态时，面板要**说清为什么是空的**，而不是一个空白块。"""
    session_id = _session_with(client, user, "风把门吹响了。")
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    panel = detail["card_state"]
    assert panel["enabled"] is True
    assert panel["has_value"] is False
    assert "还没有输出过" in panel["hint"]


def test_state_module_still_strips_without_schema(
    client: TestClient, user: dict
) -> None:
    """回归：卡没声明 schema 时，`<state>` 仍会被剥、仍不塞进本系统的 state。

    ★ 这条是 P2 最容易打破的旧契约：现在 `<state>` 会被**收进第二存储**，
      但"不当本系统状态"这条必须原样保持（否则每张卡都能凭空长出 hp）。
    """
    cleaned, notes, meta = state_mod.apply_reply_with_meta(
        type("S", (), {"id": 0, "state_json": None, "state_schema_json": None})(),
        '正文\n<state>{"hp": 1}</state>',
    )
    assert "<state>" not in cleaned
    assert meta["required"] is False
    assert notes == []


pytestmark = pytest.mark.integration
