"""角色卡 VN 模式：立绘 + 背景 + **表情跟着状态栏变**。

这一组的重点：
  1. 图片地址是**受控**的（只收 http(s) 与 data:image 内嵌图，SVG / javascript: 一律拒绝），
     坏数据降级并说明原因，而不是让整张卡不能用
  2. 表情**不是新协议**：它就是状态栏里的一个字段；作者没定义时建会话会自动补一个，
     并把可选表情写进字段描述（模型才知道该填哪些词）
  3. 当前该显示哪张立绘由**后端**算好（`detail.vn`），前端只负责画
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative import vn as vn_mod

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"

PNG = "data:image/png;base64,iVBORw0KGgo="
CALM = "https://cdn.example.com/calm.png"
ANGRY = "https://cdn.example.com/angry.png"
BG = "https://cdn.example.com/classroom.png"


def _card(vn: dict, *, schema: list | None = None, initial: dict | None = None) -> dict:
    """按 V2 导入格式造一张卡（extensions 只在 V2 里才有位置）。"""
    hne: dict = {"vn": vn}
    if schema is not None:
        hne["state_schema"] = schema
    if initial is not None:
        hne["initial_state"] = initial
    return {
        "spec": "chara_card_v2",
        "data": {
            "name": f"VN卡_{uuid.uuid4().hex[:6]}",
            "first_mes": "（她站在窗边回过头）……你来了。",
            "extensions": {"hne": hne},
        },
    }


# ==================================================================
#  二、接口层：存进卡、建会话补字段、舞台跟着状态变
# ==================================================================
class FakeAdapter:
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


@pytest.fixture
def user(client: TestClient):
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"vn_{token}",
        "email": f"vn_{token}@example.com",
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


def _import_card(client: TestClient, user: dict, card: dict) -> dict:
    imported = client.post(
        f"{CARDS}/import", json={"card": card}, headers=user["headers"]
    )
    assert imported.status_code == 201, imported.text
    return imported.json()["data"]


def _make_session(client: TestClient, user: dict, card_id: int) -> int:
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"vn_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-vn-test",
            "model_name": "mock-model",
            "context_window": 8192,
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


def test_bad_sprite_is_dropped_at_write_time(client: TestClient, user: dict) -> None:
    """★ 存进去的就应该是干净的（脏数据不该等到渲染时才被前端过滤）。"""
    card = _import_card(
        client,
        user,
        _card(
            {
                "sprites": {"平静": CALM, "坏": "javascript:alert(1)"},
                "background": BG,
            }
        ),
    )
    stored = card["extensions"]["hne"]["vn"]
    assert stored["sprites"] == {"平静": CALM}
    assert stored["background"] == BG
    assert stored["spec"] == vn_mod.SPEC


def test_session_detail_exposes_the_stage_and_adds_the_expression_field(
    client: TestClient, user: dict
) -> None:
    card = _import_card(
        client,
        user,
        _card(
            {
                "sprites": {"平静": CALM, "生气": ANGRY},
                "background": BG,
                "default": "平静",
            },
            schema=[{"name": "hp", "type": "meter", "max": 10, "value": 10}],
        ),
    )
    session_id = _make_session(client, user, card["id"])
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]

    # ① 自动补的表现字段（可选值写在描述里）
    fields = {f["name"]: f for f in detail["state_schema"]["fields"]}
    assert "mood" in fields, "作者没定义表情字段时必须自动补一个"
    assert "平静 / 生气" in fields["mood"]["description"]

    # ② 舞台数据由后端算好
    vn = detail["vn"]
    assert vn and vn["background"] == BG
    assert vn["expressions"] == ["平静", "生气"]
    assert vn["expression_field"] == "mood"
    assert vn["sprite_url"] == CALM, "状态里还没有表情 → 用默认那张"
    assert vn["name"] == card["name"]


def test_stage_follows_the_state_field(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 本轮重点：模型改了状态里的表情 → 舞台换图（不是前端各写一套映射）。"""
    card = _import_card(
        client,
        user,
        _card(
            {
                "sprites": {"平静": CALM, "生气": ANGRY},
                "background": BG,
                "default": "平静",
            },
            schema=[{"name": "mood", "type": "text"}],
            initial={"mood": "平静"},
        ),
    )
    session_id = _make_session(client, user, card["id"])
    assert (
        client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"][
            "vn"
        ]["sprite_url"]
        == CALM
    )

    fake_llm.script["content"] = (
        '（她皱起眉）别碰那本书。<state>{"mood": "生气"}</state>'
    )
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我拿起书"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    assert "<state>" not in sent.json()["data"]["assistant_message"]["content"]

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["state"]["mood"] == "生气"
    assert detail["vn"]["expression"] == "生气"
    assert detail["vn"]["sprite_url"] == ANGRY, "表情变了，立绘必须跟着换"


def test_unknown_expression_falls_back_and_says_so(
    client: TestClient, user: dict, fake_llm
) -> None:
    card = _import_card(
        client,
        user,
        _card(
            {"sprites": {"平静": CALM, "生气": ANGRY}, "default": "平静"},
            schema=[{"name": "mood", "type": "text"}],
        ),
    )
    session_id = _make_session(client, user, card["id"])
    fake_llm.script["content"] = '……<state>{"mood": "害羞"}</state>'
    client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "你好"},
        headers=user["headers"],
    )
    vn = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"][
        "vn"
    ]
    assert vn["sprite_url"] == CALM
    assert any("没有对应立绘" in note for note in vn["warnings"]), vn["warnings"]


def test_pure_chat_and_plain_cards_have_no_stage(
    client: TestClient, user: dict
) -> None:
    """没开 VN 就不该有舞台（界面据此隐藏开关）。"""
    plain = _import_card(
        client, user, _card({}, schema=[{"name": "hp", "type": "number"}])
    )
    session_id = _make_session(client, user, plain["id"])
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["vn"] is None

    chat = client.post(
        BASE,
        json={"character_card_id": None, "title": "纯聊天_vn"},
        headers=user["headers"],
    )
    assert chat.status_code == 201, chat.text
    chat_id = chat.json()["data"]["id"]
    assert (
        client.get(f"{BASE}/{chat_id}", headers=user["headers"]).json()["data"]["vn"]
        is None
    )


def test_vn_config_survives_export(client: TestClient, user: dict) -> None:
    """导出必须把 VN 配置带回去（V2 的 extensions 是往返无损的那一份）。"""
    card = _import_card(
        client,
        user,
        _card({"sprites": {"平静": CALM}, "background": BG, "position": "left"}),
    )
    exported = client.get(
        f"{CARDS}/{card['id']}/export", headers=user["headers"]
    ).json()["data"]
    vn = exported["data"]["extensions"]["hne"]["vn"]
    assert vn["sprites"] == {"平静": CALM}
    assert vn["background"] == BG and vn["position"] == "left"


pytestmark = pytest.mark.integration
