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
#  一、纯函数：校验、降级、选图
# ==================================================================
def test_image_urls_are_restricted_to_safe_schemes() -> None:
    """★ 这份配置会进 `<img src>`：unsafe 一律拒绝，并说清为什么。"""
    config, notes = vn_mod.normalize(
        {
            "sprites": {
                "平静": CALM,
                "生气": "javascript:alert(1)",
                "坏": "file:///C:/secret.png",
                "svg": "data:image/svg+xml;base64,PHN2Zz4=",
                "内嵌": PNG,
            },
            "background": "data:image/svg+xml;base64,PHN2Zz4=",
        }
    )
    assert set(config["sprites"]) == {"平静", "内嵌"}, config["sprites"]
    assert config["background"] == ""
    joined = " ".join(notes)
    assert "只支持 http(s)" in joined and "不支持 SVG" in joined and "背景图没生效" in joined


def test_url_length_is_capped() -> None:
    config, notes = vn_mod.normalize({"sprites": {"巨图": "data:image/png;base64," + "A" * 400_000}})
    assert config["sprites"] == {}
    assert "地址太长" in " ".join(notes)


def test_sprite_count_is_capped() -> None:
    many = {f"表情{i}": CALM for i in range(vn_mod.MAX_SPRITES + 5)}
    config, notes = vn_mod.normalize({"sprites": many})
    assert len(config["sprites"]) == vn_mod.MAX_SPRITES
    assert f"最多 {vn_mod.MAX_SPRITES} 张" in " ".join(notes)


def test_sprites_accept_both_written_forms() -> None:
    """对象与数组两种写法都收（网上下载的卡两种都见过）。"""
    as_object, _ = vn_mod.normalize({"sprites": {"平静": CALM}})
    as_list, _ = vn_mod.normalize({"sprites": [{"name": "平静", "url": CALM}]})
    as_pairs, _ = vn_mod.normalize({"sprites": [["平静", CALM]]})
    assert as_object["sprites"] == as_list["sprites"] == as_pairs["sprites"] == {"平静": CALM}


def test_default_expression_falls_back_sensibly() -> None:
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM, "生气": ANGRY}})
    assert config["default_expression"] == "平静", "有「平静」就默认平静"
    config, _ = vn_mod.normalize({"sprites": {"开心": CALM, "害羞": ANGRY}})
    assert config["default_expression"] == "开心", "没有平静就用第一张"
    config, notes = vn_mod.normalize({"sprites": {"开心": CALM}, "default": "生气"})
    assert config["default_expression"] == "开心"
    assert "没有对应立绘" in " ".join(notes)


def test_expression_field_name_is_validated() -> None:
    config, notes = vn_mod.normalize({"sprites": {"平静": CALM}, "expression_field": "表情 名"})
    assert config["expression_field"] == vn_mod.DEFAULT_FIELD
    assert "不合法" in " ".join(notes)


def test_enabled_defaults_to_true_when_configured() -> None:
    """作者写了立绘就等于要用它 —— 不该再要求他额外勾一个开关。"""
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM}})
    assert config["enabled"] is True and vn_mod.is_enabled(config) is True
    off, _ = vn_mod.normalize({"sprites": {"平静": CALM}, "enabled": False})
    assert vn_mod.is_enabled(off) is False
    empty, notes = vn_mod.normalize({"enabled": True})
    assert vn_mod.is_enabled(empty) is False and "舞台上会是空的" in " ".join(notes)


def test_sprite_lookup_is_forgiving_about_case_and_spaces() -> None:
    config, _ = vn_mod.normalize({"sprites": {"Calm": CALM, "生气": ANGRY}, "default": "Calm"})
    assert vn_mod.sprite_url(config, "calm") == CALM
    assert vn_mod.sprite_url(config, " 生气 ") == ANGRY
    # 没有对应立绘 → 退回默认，而不是显示空白
    assert vn_mod.sprite_url(config, "害羞") == CALM
    assert vn_mod.sprite_url(vn_mod.normalize({})[0], "平静") is None


def test_expression_is_read_from_the_state_field() -> None:
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM, "生气": ANGRY}, "expression_field": "mood"})
    assert vn_mod.expression_of(config, {"mood": "生气"}) == "生气"
    assert vn_mod.expression_of(config, {"mood": "  "}) == "平静", "空值退回默认"
    assert vn_mod.expression_of(config, None) == "平静"
    assert vn_mod.expression_of(config, {"mood": ["生气"]}) == "平静", "列表不算表情"


def test_stage_explains_when_the_expression_has_no_sprite() -> None:
    card = type("C", (), {"name": "薇拉", "extra_data": {"extensions": {"hne": {"vn": {
        "sprites": {"平静": CALM, "生气": ANGRY},
        "background": BG,
        "default": "平静",
    }}}}})()
    stage = vn_mod.stage(card, {"mood": "害羞"})
    assert stage is not None
    assert stage["sprite_url"] == CALM, "退回默认立绘"
    assert stage["expression"] == "害羞"
    assert any("没有对应立绘" in note for note in stage["warnings"])
    assert stage["background"] == BG and stage["name"] == "薇拉"
    assert stage["expressions"] == ["平静", "生气"]


def test_no_stage_when_the_card_does_not_declare_vn() -> None:
    plain = type("C", (), {"name": "路人", "extra_data": {}})()
    assert vn_mod.stage(plain, {"mood": "生气"}) is None


def test_expression_field_is_added_to_the_schema_with_the_choices() -> None:
    """★ 没这个字段立绘永远不会变 —— 自动补一个，并把可选值告诉模型。"""
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM, "生气": ANGRY}})
    schema = {"spec": "hne_state_v1", "fields": [{"name": "hp", "type": "meter", "max": 10}]}
    notes = vn_mod.ensure_expression_field(schema, config)
    assert [f["name"] for f in schema["fields"]] == ["hp", "mood"]
    mood = schema["fields"][-1]
    assert mood["type"] == "text"
    assert "平静 / 生气" in mood["description"], "可选值必须写进字段描述，模型才知道填什么"
    assert notes and "已往状态栏补一个" in notes[0]
    assert vn_mod.ensure_expression_field(schema, config) == [], "重复调用不再加第二个"


def test_author_defined_expression_field_is_respected() -> None:
    """作者自己定义过就一个字都不动（标签/描述按他的来）。"""
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM}, "expression_field": "心情"})
    schema = {"fields": [{"name": "心情", "label": "她的心情", "type": "text"}]}
    assert vn_mod.ensure_expression_field(schema, config) == []
    assert schema["fields"] == [{"name": "心情", "label": "她的心情", "type": "text"}]


def test_merge_keeps_the_other_namespaces() -> None:
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM}})
    merged = vn_mod.merge_into_extensions(
        {"hne": {"state_schema": [{"name": "hp"}]}, "other": {"x": 1}}, config
    )
    assert merged["other"] == {"x": 1}
    assert merged["hne"]["state_schema"] == [{"name": "hp"}]
    assert merged["hne"]["vn"]["spec"] == vn_mod.SPEC
    assert merged["hne"]["vn"]["sprites"] == {"平静": CALM}


def test_describe_says_what_is_configured() -> None:
    config, _ = vn_mod.normalize({"sprites": {"平静": CALM, "生气": ANGRY}, "background": BG})
    text = vn_mod.describe(config)
    assert "有背景" in text and "2 张立绘" in text and "mood" in text
    assert "未启用" in vn_mod.describe(vn_mod.normalize({})[0])


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
    imported = client.post(f"{CARDS}/import", json={"card": card}, headers=user["headers"])
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
            {"sprites": {"平静": CALM, "生气": ANGRY}, "background": BG, "default": "平静"},
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


def test_stage_follows_the_state_field(client: TestClient, user: dict, fake_llm) -> None:
    """★ 本轮重点：模型改了状态里的表情 → 舞台换图（不是前端各写一套映射）。"""
    card = _import_card(
        client,
        user,
        _card(
            {"sprites": {"平静": CALM, "生气": ANGRY}, "background": BG, "default": "平静"},
            schema=[{"name": "mood", "type": "text"}],
            initial={"mood": "平静"},
        ),
    )
    session_id = _make_session(client, user, card["id"])
    assert (
        client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]["vn"]["sprite_url"]
        == CALM
    )

    fake_llm.script["content"] = "（她皱起眉）别碰那本书。<state>{\"mood\": \"生气\"}</state>"
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "我拿起书"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    assert "<state>" not in sent.json()["data"]["assistant_message"]["content"]

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["state"]["mood"] == "生气"
    assert detail["vn"]["expression"] == "生气"
    assert detail["vn"]["sprite_url"] == ANGRY, "表情变了，立绘必须跟着换"


def test_unknown_expression_falls_back_and_says_so(client: TestClient, user: dict, fake_llm) -> None:
    card = _import_card(
        client,
        user,
        _card(
            {"sprites": {"平静": CALM, "生气": ANGRY}, "default": "平静"},
            schema=[{"name": "mood", "type": "text"}],
        ),
    )
    session_id = _make_session(client, user, card["id"])
    fake_llm.script["content"] = "……<state>{\"mood\": \"害羞\"}</state>"
    client.post(f"{BASE}/{session_id}/messages", json={"content": "你好"}, headers=user["headers"])
    vn = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]["vn"]
    assert vn["sprite_url"] == CALM
    assert any("没有对应立绘" in note for note in vn["warnings"]), vn["warnings"]


def test_pure_chat_and_plain_cards_have_no_stage(client: TestClient, user: dict) -> None:
    """没开 VN 就不该有舞台（界面据此隐藏开关）。"""
    plain = _import_card(client, user, _card({}, schema=[{"name": "hp", "type": "number"}]))
    session_id = _make_session(client, user, plain["id"])
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert detail["vn"] is None

    chat = client.post(
        BASE, json={"character_card_id": None, "title": "纯聊天_vn"}, headers=user["headers"]
    )
    assert chat.status_code == 201, chat.text
    chat_id = chat.json()["data"]["id"]
    assert (
        client.get(f"{BASE}/{chat_id}", headers=user["headers"]).json()["data"]["vn"] is None
    )


def test_vn_config_survives_export(client: TestClient, user: dict) -> None:
    """导出必须把 VN 配置带回去（V2 的 extensions 是往返无损的那一份）。"""
    card = _import_card(
        client, user, _card({"sprites": {"平静": CALM}, "background": BG, "position": "left"})
    )
    exported = client.get(f"{CARDS}/{card['id']}/export", headers=user["headers"]).json()["data"]
    vn = exported["data"]["extensions"]["hne"]["vn"]
    assert vn["sprites"] == {"平静": CALM}
    assert vn["background"] == BG and vn["position"] == "left"
