"""自动翻译中间件：跨语言对话（原文/译文可切换，成本如实回报）。

这一组的重点：
  1. 三种模式与两个方向都要**真的**按设置走（off / prompt 不花 token，middleware 才调模型）
  2. `content` 永远是**模型看到的文本**（输入侧 = 译文，输出侧 = 模型原文），
     `display` 说明界面默认显示哪一份 —— 原文/译文都能拿到，不缺不丢
  3. 翻译失败、跳过（已经是目标语言 / 太长）都**不影响对话**，并且如实说明
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.llm.params import GenerationParams, ReasoningEffort, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative import translate as translate_mod

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"

EN_REPLY = "Hello there, traveler. The road ahead is long."
ZH_REPLY = "你好，旅行者。前面的路还很长。"
ZH_INPUT = "你好呀，我该往哪走？"
EN_INPUT = "Hi there, which way should I go?"
# ★ 第十六轮新增：源语言自动识别的样本（日文 / 韩文 / 繁体）
JA_REPLY = "こんにちは、旅人さん。道はまだ長いです。"
KO_REPLY = "안녕하세요, 여행자님. 길이 아직 멉니다."
ZH_TRAD_REPLY = "你好，旅行者。前面的路還很長。"


# ==================================================================
#  一、纯函数：设置、语言判断、跳过、失败
# ==================================================================
def test_defaults_are_off_and_never_spend_tokens() -> None:
    """★ 默认必须关闭：本项目对"未经同意花用户 token"零容忍（与记忆总结同一条规矩）。"""
    settings = translate_mod.default_settings()
    assert settings["enabled"] is False
    assert translate_mod.uses_model(settings, translate_mod.DIRECTION_REPLY) is False
    assert translate_mod.prompt_hint(settings) == ""
    assert translate_mod.estimate_cost(settings, EN_REPLY) == 0


def test_disabling_forces_the_mode_to_off() -> None:
    """关掉总开关后模式必须归零：否则界面会显示"中间件翻译"却什么都没发生。"""
    settings = translate_mod.normalize_settings(
        {"enabled": False, "mode": "middleware", "direction": "both"}
    )
    assert settings["mode"] == translate_mod.MODE_OFF
    assert translate_mod.describe(settings) == "未启用"


def test_invalid_mode_and_direction_fall_back() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "魔法", "direction": "两边", "provider_id": "7", "target_lang": "  "}
    )
    assert settings["mode"] == translate_mod.MODE_MIDDLEWARE
    assert settings["direction"] == translate_mod.DIRECTION_REPLY
    assert settings["provider_id"] == 7, "数字字符串要能收"
    assert settings["target_lang"] == translate_mod.DEFAULT_TARGET_LANG


def test_direction_controls_each_side_independently() -> None:
    reply = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "direction": "reply"}
    )
    assert translate_mod.wants(reply, translate_mod.DIRECTION_REPLY) is True
    assert translate_mod.wants(reply, translate_mod.DIRECTION_INPUT) is False
    both = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "direction": "both"}
    )
    assert translate_mod.wants(both, translate_mod.DIRECTION_REPLY) is True
    assert translate_mod.wants(both, translate_mod.DIRECTION_INPUT) is True


def test_prompt_mode_costs_nothing_but_still_tells_the_model() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "prompt", "target_lang": "简体中文"}
    )
    hint = translate_mod.prompt_hint(settings)
    assert "简体中文" in hint and "输出语言" in hint
    assert translate_mod.uses_model(settings, translate_mod.DIRECTION_REPLY) is False
    assert translate_mod.estimate_cost(settings, EN_REPLY) == 0
    outcome = translate_mod.run(EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=None)
    assert outcome.used_model is False and "提示词模式" in outcome.skipped


def test_local_language_check_spares_the_call() -> None:
    """★ 已经是目标语言就不调模型 —— 这是真实省下来的钱，不是假装译了。"""
    assert translate_mod.looks_like(ZH_REPLY, "简体中文") is True
    assert translate_mod.looks_like(EN_REPLY, "简体中文") is False
    assert translate_mod.looks_like(EN_REPLY, "英文") is True
    assert translate_mod.looks_like(ZH_REPLY, "英文") is False
    assert translate_mod.looks_like("", "简体中文") is True
    assert translate_mod.looks_like("Привет", "简体中文") is False
    # ★ 第十六轮：源语言自动识别 —— 旧版把假名/谚文一起算进"CJK 占比"，
    #   于是日文、韩文、繁体回复都被判成"已经是简体中文"而**静默不译**。
    assert translate_mod.looks_like(JA_REPLY, "简体中文") is False, "日文必须译，不能当成中文"
    assert translate_mod.looks_like(KO_REPLY, "简体中文") is False, "韩文必须译，不能当成中文"
    assert translate_mod.looks_like(ZH_TRAD_REPLY, "简体中文") is False, "繁体要转成简体"
    assert translate_mod.looks_like(ZH_TRAD_REPLY, "繁體中文") is True, "繁体是繁体目标本身"
    assert translate_mod.looks_like(ZH_REPLY, "繁體中文") is False, "简体要转成繁体"
    assert translate_mod.looks_like(JA_REPLY, "日文") is True
    assert translate_mod.looks_like(ZH_REPLY, "日文") is False, "中文在日文目标下要译"
    assert translate_mod.looks_like(KO_REPLY, "韩文") is True
    assert translate_mod.looks_like(ZH_REPLY, "韩文") is False, "中文在韩文目标下要译"
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    outcome = translate_mod.run(ZH_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=None)
    assert outcome.used_model is False
    assert "看起来已经是" in outcome.skipped
    assert translate_mod.estimate_cost(settings, ZH_REPLY) == 0


def test_too_long_text_is_skipped_with_a_reason() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    outcome = translate_mod.run(
        "a" * (translate_mod.MAX_CHARS + 10),
        settings,
        direction=translate_mod.DIRECTION_REPLY,
        adapter=_Stub("译文"),
    )
    assert outcome.used_model is False and "太长" in outcome.skipped


class _Stub:
    """最简适配器替身（只实现 chat）。"""

    def __init__(self, content: str = "译文", *, boom: bool = False) -> None:
        self.content = content
        self.boom = boom
        self.calls = 0

    def chat(self, request):  # noqa: ANN001
        self.calls += 1
        if self.boom:
            raise RuntimeError("翻译服务挂了")
        result = ChatResult(content=self.content, model="stub-model", finish_reason="stop")
        result.usage = TokenUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50)
        return result


def test_success_records_text_tokens_and_which_side_to_display() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    stub = _Stub("你好，旅行者。")
    outcome = translate_mod.run(
        EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=stub, provider_id=3
    )
    assert stub.calls == 1
    assert outcome.ok and outcome.text == "你好，旅行者。"
    assert outcome.lang == "简体中文" and outcome.tokens == 50
    assert outcome.display == "translation", "输出侧：界面默认显示译文"
    assert outcome.provider_id == 3 and outcome.model == "stub-model"
    # 输入侧用**同一个**目标语言：用户用英文写，就译成简体中文再发给模型
    # （这一段的设置必须是"方向含输入"，否则输入侧根本不会处理）
    both = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "direction": "both", "target_lang": "简体中文"}
    )
    outcome_in = translate_mod.run(
        EN_INPUT, both, direction=translate_mod.DIRECTION_INPUT, adapter=stub
    )
    assert outcome_in.lang == "简体中文", "两个方向共用一个目标语言（没有输入侧语言了）"
    assert outcome_in.used_model is True
    assert outcome_in.display == "translation", "translation.text 永远放给人看的那一份"
    # ★ 而中文用户写中文时：它就是目标语言 → 0 token 直发（这是"面向中文用户"的默认收益）
    already = translate_mod.run(
        ZH_INPUT, both, direction=translate_mod.DIRECTION_INPUT, adapter=stub
    )
    assert already.used_model is False, "用户写的就是目标语言 → 不花这次钱"


def test_model_failure_is_reported_not_raised() -> None:
    """★ 翻译失败绝不能影响对话（与剧情总结降级同一套哲学）。"""
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    outcome = translate_mod.run(
        EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=_Stub(boom=True)
    )
    assert outcome.ok is False
    assert "翻译服务挂了" in outcome.error
    assert outcome.text == ""


def test_empty_model_output_is_treated_as_failure() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    outcome = translate_mod.run(
        EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=_Stub("   ")
    )
    assert outcome.ok is False and "空译文" in outcome.error


def test_translation_prompt_forbids_explanations() -> None:
    messages = translate_mod.build_messages(
        EN_REPLY,
        translate_mod.default_settings(),
        lang="简体中文",
    )
    assert messages[0].role == "system" and messages[1].role == "user"
    assert "简体中文" in messages[0].content
    assert "只输出译文" in messages[0].content
    assert messages[1].content == EN_REPLY


def test_json_helpers_never_break_the_reader() -> None:
    assert translate_mod.loads(None) is None
    assert translate_mod.loads("{坏 JSON") is None
    assert translate_mod.loads('{"used_model": true}') is None, "没有 text 就当没译过"
    outcome = translate_mod.run(
        EN_REPLY,
        translate_mod.normalize_settings(
            {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
        ),
        direction=translate_mod.DIRECTION_REPLY,
        adapter=_Stub("你好。"),
    )
    raw = translate_mod.dumps(outcome)
    loaded = translate_mod.loads(raw)
    assert loaded and loaded["text"] == "你好。" and loaded["display"] == "translation"
    assert translate_mod.dumps(None) is None


def test_panel_state_lists_the_choices() -> None:
    class _Session:
        id = 1
        translate_settings_json = None

    state = translate_mod.state(_Session())
    assert [m["value"] for m in state["modes"]] == ["off", "prompt", "middleware"]
    assert [d["value"] for d in state["directions"]] == ["reply", "input", "both"]
    assert "简体中文" in state["lang_choices"]
    assert state["summary"] == "未启用"


# ==================================================================
#  二、接口层：三种模式、两个方向、失败与跳过
# ==================================================================
class FakeAdapter:
    """按脚本依次返回内容；第 `fail_from` 次之后开始抛错（用来测翻译失败）。"""

    script: dict = {}

    def __init__(self, row=None, **_):
        self.row = row
        self.default_params = GenerationParams(max_tokens=512)
        FakeAdapter.script.setdefault("rows", []).append(getattr(row, "id", None))

    @property
    def budget(self):
        window = getattr(self.row, "context_window", None) or 8192
        max_out = getattr(self.row, "max_tokens", None) or 1024
        return compute_context_budget(window, max_out)

    def _next(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
        index = int(FakeAdapter.script.get("index", 0))
        FakeAdapter.script["index"] = index + 1
        fail_from = FakeAdapter.script.get("fail_from")
        if fail_from is not None and index >= fail_from:
            raise RuntimeError("翻译这步炸了")
        contents = FakeAdapter.script.get("contents") or ["（假回复）"]
        return contents[min(index, len(contents) - 1)]

    def chat(self, request):
        content = self._next(request)
        return ChatResult(
            content=content,
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
        content = self._next(request)
        yield StreamChunk(delta=content)
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
        "username": f"tr_{token}",
        "email": f"tr_{token}@example.com",
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


def _make_session(client: TestClient, user: dict) -> tuple[int, int]:
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"tr_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-translate-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    provider_id = provider.json()["data"]["id"]
    card = client.post(
        CARDS,
        json={"name": f"外语卡_{uuid.uuid4().hex[:6]}", "greeting": "Hello there, traveler."},
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    session = client.post(
        BASE,
        json={"character_card_id": card.json()["data"]["id"], "llm_provider_id": provider_id},
        headers=user["headers"],
    )
    assert session.status_code == 201, session.text
    return session.json()["data"]["id"], provider_id


def _enable(client: TestClient, user: dict, session_id: int, **overrides) -> dict:
    payload = {
        "enabled": True,
        "mode": "middleware",
        "direction": "reply",
        "target_lang": "简体中文",
    }
    payload.update(overrides)
    response = client.patch(
        f"{BASE}/{session_id}/translate", json=payload, headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_settings_round_trip_and_patch_semantics(client: TestClient, user: dict) -> None:
    session_id, _provider = _make_session(client, user)
    panel = client.get(f"{BASE}/{session_id}/translate", headers=user["headers"]).json()["data"]
    assert panel["settings"]["enabled"] is False, "默认必须是关的"
    assert panel["cost_tokens"] == 0
    assert panel["providers"], "面板要给出可选模型配置"
    # ★ 第十六轮：源语言自动识别 ⇒ "输入侧语言"这个设置已退休（一个目标语言就够）
    assert "input_lang" not in panel["settings"]
    assert panel["settings"]["target_lang"] == "简体中文", "面向中文用户：默认外语→简体中文"

    saved = _enable(client, user, session_id, target_lang="日文")
    assert saved["settings"]["target_lang"] == "日文"
    assert saved["settings"]["mode"] == "middleware"
    # PATCH 语义：只改了语言，方向与开关不该被顺手改掉
    only_lang = client.patch(
        f"{BASE}/{session_id}/translate", json={"target_lang": "英文"}, headers=user["headers"]
    ).json()["data"]
    assert only_lang["settings"]["target_lang"] == "英文"
    assert only_lang["settings"]["enabled"] is True
    assert only_lang["settings"]["direction"] == "reply"


def test_reply_translation_keeps_both_texts(client: TestClient, user: dict, fake_llm) -> None:
    """★ 输出侧：content = 模型原文，另一份 = 译文，界面默认显示译文。"""
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id)
    fake_llm.script["contents"] = [EN_REPLY, ZH_REPLY]
    before = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"][
        "total_tokens"
    ]

    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "我该往哪走？"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    body = sent.json()["data"]
    assistant = body["assistant_message"]
    assert assistant["content"] == EN_REPLY, "content 永远是模型说的话"
    assert assistant["translation"]["text"] == ZH_REPLY
    assert assistant["translation"]["display"] == "translation"
    assert assistant["translation"]["used_model"] is True
    assert assistant["translation"]["lang"] == "简体中文"
    assert int(assistant["translation"]["tokens"]) == 15, "翻译的 token 也要记下来"
    assert any("翻译中间件" in note for note in body["notes"]), body["notes"]
    # 翻译消耗要计进会话累计（如实反映成本）
    after = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    assert after["total_tokens"] >= before + 30

    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    rows = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert rows[-1]["translation"]["text"] == ZH_REPLY, "刷新后译文还在（落库了）"
    assert detail["translate"]["translated_messages"] >= 1
    assert detail["translate"]["spent_tokens"] >= 15


def test_translation_is_skipped_when_the_reply_is_already_in_the_target_language(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id)
    fake_llm.script["contents"] = [ZH_REPLY]
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "你好"}, headers=user["headers"]
    )
    assistant = sent.json()["data"]["assistant_message"]
    assert assistant["translation"] is None, "已经是中文就不译（0 token）"
    assert len(fake_llm.script["requests"]) == 1, "★ 不该多调一次模型"


def test_japanese_reply_is_auto_detected_and_translated(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 第十六轮：日文回复必须被自动识别并翻译。

    旧版把假名算进"CJK 占比"，日文回复会被判成"已经是中文"→ 0 token 静默跳过，
    于是"日文自动识别后翻成简体中文"看起来配好了、实际什么都没发生。
    """
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id)  # 目标 = 默认的简体中文
    fake_llm.script["contents"] = [JA_REPLY, ZH_REPLY]

    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "你好"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    assistant = sent.json()["data"]["assistant_message"]
    assert assistant["content"] == JA_REPLY, "content 永远是模型说的话"
    assert assistant["translation"] is not None, "★ 日文必须触发翻译（这正是旧版的 bug）"
    assert assistant["translation"]["used_model"] is True
    assert assistant["translation"]["lang"] == "简体中文"
    assert assistant["translation"]["text"] == ZH_REPLY
    assert len(fake_llm.script["requests"]) == 2, "一次生成 + 一次翻译，正好两次"


def test_prompt_mode_only_touches_the_prompt(client: TestClient, user: dict, fake_llm) -> None:
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id, mode="prompt")
    fake_llm.script["contents"] = [EN_REPLY]
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "我该往哪走？"}, headers=user["headers"]
    )
    assert sent.json()["data"]["assistant_message"]["translation"] is None
    assert len(fake_llm.script["requests"]) == 1, "★ prompt 模式不该多调模型"
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "【输出语言】" in system and "简体中文" in system
    # 预览与实际必须一致：预览里也要有这句
    preview = client.get(
        f"{BASE}/{session_id}", params={"with_prompt": True}, headers=user["headers"]
    ).json()["data"]["prompt"]["system_prompt"]
    assert "【输出语言】" in preview


def test_input_translation_reaches_the_model(client: TestClient, user: dict, fake_llm) -> None:
    """★ 输入侧：用户中文 → 译成英文再发；content 是模型看到的英文，原文另存。

    ★ 第十六轮起不再有"输入侧语言"设置：把目标语言选成英文即可做这件事 ——
    两个方向共用一个目标语言，源语言自动识别。
    """
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id, direction="input", target_lang="英文")
    fake_llm.script["contents"] = [EN_INPUT, EN_REPLY]

    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": ZH_INPUT}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    user_row = sent.json()["data"]["user_message"]
    assert user_row["content"] == EN_INPUT, "content = 模型看到的文本（译文）"
    assert user_row["translation"]["text"] == ZH_INPUT, "原话没有丢"
    assert user_row["translation"]["display"] == "translation", "界面显示 translation.text（用户原话）"
    assert user_row["translation"]["lang"] == "英文"
    # 模型真的看到了英文（而不是中文原文）
    seen = " ".join(str(getattr(m, "content", "")) for m in fake_llm.script["requests"][-1].messages)
    assert EN_INPUT in seen and ZH_INPUT not in seen


def test_both_directions_at_once(client: TestClient, user: dict, fake_llm) -> None:
    """★ 双向：一个目标语言同时管两边（英文输入 → 中文；英文回复 → 中文）。"""
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id, direction="both")  # 目标 = 默认简体中文
    fake_llm.script["contents"] = [ZH_INPUT, EN_REPLY, ZH_REPLY]
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": EN_INPUT}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    data = sent.json()["data"]
    assert len(fake_llm.script["requests"]) == 3, "两次翻译 + 一次生成"
    assert data["user_message"]["translation"]["lang"] == "简体中文"
    assert data["assistant_message"]["translation"]["lang"] == "简体中文"
    # ★ 不变量（整条链路都靠它）：content = **模型看到的文本**，translation.text = **给人看的文本**
    user_row, assistant_row = data["user_message"], data["assistant_message"]
    assert user_row["content"] == ZH_INPUT, "输入侧：模型看到的是译文（中文）"
    assert user_row["translation"]["text"] == EN_INPUT, "输入侧：给人看的是用户原话（英文）"
    assert assistant_row["content"] == EN_REPLY, "输出侧：模型说的是英文原文"
    assert assistant_row["translation"]["text"] == ZH_REPLY, "输出侧：给人看的是译文"


def test_translation_failure_never_breaks_the_reply(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id)
    fake_llm.script["contents"] = [EN_REPLY, ZH_REPLY]
    fake_llm.script["fail_from"] = 1  # 第 2 次调用（翻译那一次）开始抛错
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "我该往哪走？"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    body = sent.json()["data"]
    assert body["assistant_message"]["content"] == EN_REPLY, "回复一个字都不能丢"
    assert any("没能把回复译成" in note for note in body["notes"]), body["notes"]


def test_independent_translation_model_is_used(client: TestClient, user: dict, fake_llm) -> None:
    """可以指定**另一个**模型配置来翻译（论文里"用哪个模型译"是可对比的实验变量）。"""
    session_id, session_provider = _make_session(client, user)
    other = client.post(
        PROVIDERS,
        json={
            "name": f"翻译专用_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-translate-2",
            "model_name": "translate-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert other.status_code == 201, other.text
    other_id = other.json()["data"]["id"]
    panel = _enable(client, user, session_id, provider_id=other_id)
    assert panel["settings"]["provider_id"] == other_id

    fake_llm.script["contents"] = [EN_REPLY, ZH_REPLY]
    client.post(f"{BASE}/{session_id}/messages", json={"content": "我该往哪走？"}, headers=user["headers"])
    rows = fake_llm.script.get("rows") or []
    assert other_id in rows, f"翻译应该用配置 #{other_id}（实际用过 {rows}）"
    assert session_provider in rows, "生成那一次仍然用会话模型"


def test_long_reply_is_skipped_and_said_so(client: TestClient, user: dict, fake_llm) -> None:
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id)
    fake_llm.script["contents"] = ["a" * (translate_mod.MAX_CHARS + 100)]
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "讲个长故事"}, headers=user["headers"]
    )
    assistant = sent.json()["data"]["assistant_message"]
    assert assistant["translation"] is None
    assert len(fake_llm.script["requests"]) == 1, "太长就不译（不花这次钱）"


# ==================================================================
#  第十六轮新增：翻译中的提示 / 降思考 / 花费口径
# ==================================================================
def test_will_call_model_matches_the_real_skip_rules() -> None:
    """★ 界面靠它决定"要不要显示『正在翻译…』"：判据必须与 `run()` 一致 ——
    **提示了却没译**比不提示更让人困惑（用户会盯着一句永远不消失的提示）。"""
    on = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "direction": "reply", "target_lang": "简体中文"}
    )
    assert translate_mod.will_call_model(on, translate_mod.DIRECTION_REPLY, EN_REPLY) is True
    # 已经是目标语言 / 太长 / 空文本 ⇒ 不会调模型 ⇒ 不该提示
    assert translate_mod.will_call_model(on, translate_mod.DIRECTION_REPLY, ZH_REPLY) is False
    assert translate_mod.will_call_model(on, translate_mod.DIRECTION_REPLY, "") is False
    assert (
        translate_mod.will_call_model(
            on, translate_mod.DIRECTION_REPLY, "a" * (translate_mod.MAX_CHARS + 1)
        )
        is False
    )
    # 方向不含输入 ⇒ 输入侧不提示
    assert translate_mod.will_call_model(on, translate_mod.DIRECTION_INPUT, EN_REPLY) is False
    # 关掉总开关 / 提示词模式 都不调模型
    off = translate_mod.normalize_settings({"enabled": False, "mode": "middleware"})
    assert translate_mod.will_call_model(off, translate_mod.DIRECTION_REPLY, EN_REPLY) is False
    prompt = translate_mod.normalize_settings({"enabled": True, "mode": "prompt"})
    assert translate_mod.will_call_model(prompt, translate_mod.DIRECTION_REPLY, EN_REPLY) is False


class _EffortStub:
    """记录"这次调用带了多大思考强度"的适配器替身。

    ★ 只暴露**真实适配器上真的存在**的那个属性：探测结论由 `create_provider_from_config`
      从 ProviderConfig 带到 `adapter.reasoning_support`。
      （上一版这里伪造的是 `default_params.effective_reasoning_support()` —— 那个 API
      在 `GenerationParams` 上**根本不存在**，于是测试绿着、功能静默失效，见 §29.13。）
    """

    def __init__(self, support: bool | None) -> None:
        self.seen: list[object] = []
        self.reasoning_support = support

    def chat(self, request):  # noqa: ANN001
        self.seen.append(request.reasoning_effort)
        return ChatResult(
            content="译文",
            model="stub-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )


def test_thinking_is_lowered_for_translation_only_when_supported() -> None:
    """★ 翻译是机械任务，思考纯烧钱（实测：1618 字符的英文译一次烧 5291 token）。

    但 `reasoning_effort` 是较新的参数，**不少网关不认识它、发了直接 400** ——
    翻译失败等于用户白等一场，所以只在"探测确认该模型接受"时才发（统一层默认 auto = 不发）。
    """
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    supported = _EffortStub(True)
    translate_mod.run(
        EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=supported
    )
    assert supported.seen == [ReasoningEffort.OFF], "支持思考强度的模型：翻译要降到最小"

    for support in (False, None):
        unsure = _EffortStub(support)
        translate_mod.run(
            EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=unsure
        )
        assert unsure.seen == [None], f"结论是 {support!r} 时不能发这个参数（会 400）"


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本切成 [(事件名, 数据)]（与 tests/test_narrative.py 的同一套写法）。"""
    events: list[tuple[str, dict]] = []
    name = "message"
    data_lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                events.append((name, json.loads("\n".join(data_lines))))
            name, data_lines = "message", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    if data_lines:
        events.append((name, json.loads("\n".join(data_lines))))
    return events


def test_stream_announces_translation_before_it_happens(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 第十六轮：正文流完之后还要等**一次模型调用**才出译文（好几秒）。

    这几秒必须发 `translating` 事件 —— 用户原话："原文发出来之后，翻译过程没有任何提示，
    用户会觉得莫名其妙"。位置也很关键：在正文之后、`done` 之前。
    """
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id)
    fake_llm.script["contents"] = [EN_REPLY, ZH_REPLY]  # 模型说英文 → 译成中文
    response = client.get(
        f"{BASE}/{session_id}/stream", params={"content": "你好"}, headers=user["headers"]
    )
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    names = [name for name, _ in events]
    assert "translating" in names, f"必须提示「翻译中」（实际事件：{names}）"
    index = names.index("translating")
    assert names[index - 1] in ("delta", "notes"), "提示要出现在正文之后"
    assert index < names.index("done"), "提示要在收尾之前（收尾时前端会把它收掉）"
    payload = dict(events)["translating"]
    assert payload["direction"] == "reply" and payload["lang"] == "简体中文"

    # 反向：回复本来就是目标语言 ⇒ 不调模型 ⇒ **不许**发这个提示（否则提示永远不消失）
    other, _provider2 = _make_session(client, user)
    _enable(client, user, other)
    fake_llm.script["contents"] = [ZH_REPLY]
    response2 = client.get(
        f"{BASE}/{other}/stream", params={"content": "你好"}, headers=user["headers"]
    )
    names2 = [name for name, _ in _parse_sse(response2.text)]
    assert "translating" not in names2, f"没调模型却提示了：{names2}"


def test_manual_translate_of_one_message_ignores_the_switch(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 第十六轮：逐条「🌐 翻译」**不看总开关**。

    用户可能忘了开翻译中间件、或者只想看其中一条的译文 —— 让他为了这一条去改全局设置
    再关掉太重了。点这一下 = 同意花这一次调用（与「立即总结」同一个哲学）。
    另外两条硬要求：**已有译文不重译**（否则会冲掉输入侧"原文=用户原话"的关系）、
    跳过/失败的原因要如实回一句话（不许点了没反应）。
    """
    session_id, _provider = _make_session(client, user)
    # 翻译设置保持默认（关闭）——手动翻译照样要能用
    fake_llm.script["contents"] = [EN_REPLY, ZH_REPLY]
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "你好"}, headers=user["headers"]
    )
    assistant = sent.json()["data"]["assistant_message"]
    assert assistant["translation"] is None, "默认关闭：自动翻译不该发生"
    message_id = assistant["id"]

    resp = client.post(
        f"{BASE}/{session_id}/messages/{message_id}/translate", headers=user["headers"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["data"]["message"]["translation"]["text"] == ZH_REPLY
    assert "已译成" in body["message"], f"要如实说花了多少：{body['message']}"

    # 落库了（刷新还在）
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    rows = [m for m in detail["messages"] if m["id"] == message_id]
    assert rows and rows[0]["translation"]["text"] == ZH_REPLY

    # 幂等：已有译文就不重译，也不许再调一次模型
    calls_before = len(fake_llm.script["requests"])
    again = client.post(
        f"{BASE}/{session_id}/messages/{message_id}/translate", headers=user["headers"]
    )
    assert again.status_code == 200
    assert "已经有译文" in again.json()["message"], again.json()["message"]
    assert len(fake_llm.script["requests"]) == calls_before, "不许重复花钱"

    # 已经就是目标语言 ⇒ 跳过，并且如实说明（不调模型）
    fake_llm.script["contents"] = [ZH_REPLY]
    zh = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "再说一句"}, headers=user["headers"]
    ).json()["data"]["assistant_message"]
    calls_before = len(fake_llm.script["requests"])
    skipped = client.post(
        f"{BASE}/{session_id}/messages/{zh['id']}/translate", headers=user["headers"]
    )
    assert skipped.status_code == 200
    assert "不需要翻译" in skipped.json()["message"], skipped.json()["message"]
    assert len(fake_llm.script["requests"]) == calls_before, "已经是中文就别花钱"


def test_per_turn_usage_is_persisted_for_the_accuracy_report(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 第十六轮：逐轮**真实用量**（厂商分项 + 我们发出去前的输入估算）必须落库。

    为什么：`token_count` 只存 completion 那一半、`session.total_tokens` 只存累计总和
    ⇒ 事后回答不了"启发式估算 vs 厂商 `prompt_tokens` 差多少"，论文里一直只能写
    "未做过对照实测"。落库之后 `scripts/token_accuracy.py` 只读回放就够了（不花钱）。
    """
    session_id, _provider = _make_session(client, user)
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "你好"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    message_id = sent.json()["data"]["assistant_message"]["id"]

    from app.db.models import Message  # noqa: PLC0415
    from app.db.mysql import session_scope  # noqa: PLC0415

    with session_scope() as db:
        row = db.get(Message, message_id)
        usage = json.loads(row.usage_json or "{}")

    assert usage.get("prompt_tokens") == 10, "厂商的输入 token 要原样落库"
    assert usage.get("completion_tokens") == 5, "输出 token 同理"
    assert int(usage.get("estimated_input_tokens") or 0) > 0, (
        "★ 必须同时留下**我们发出去前的估算**，否则对照实测无从做起"
    )
