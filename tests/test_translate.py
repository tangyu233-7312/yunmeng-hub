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

from app.llm.params import GenerationParams, compute_context_budget
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
        json={
            "name": f"外语卡_{uuid.uuid4().hex[:6]}",
            "greeting": "Hello there, traveler.",
        },
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    session = client.post(
        BASE,
        json={
            "character_card_id": card.json()["data"]["id"],
            "llm_provider_id": provider_id,
        },
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


def test_settings_round_trip_and_patch_semantics(
    client: TestClient, user: dict
) -> None:
    session_id, _provider = _make_session(client, user)
    panel = client.get(
        f"{BASE}/{session_id}/translate", headers=user["headers"]
    ).json()["data"]
    assert panel["settings"]["enabled"] is False, "默认必须是关的"
    assert panel["cost_tokens"] == 0
    assert panel["providers"], "面板要给出可选模型配置"
    # ★ 第十六轮：源语言自动识别 ⇒ "输入侧语言"这个设置已退休（一个目标语言就够）
    assert "input_lang" not in panel["settings"]
    assert panel["settings"]["target_lang"] == "简体中文", (
        "面向中文用户：默认外语→简体中文"
    )

    saved = _enable(client, user, session_id, target_lang="日文")
    assert saved["settings"]["target_lang"] == "日文"
    assert saved["settings"]["mode"] == "middleware"
    # PATCH 语义：只改了语言，方向与开关不该被顺手改掉
    only_lang = client.patch(
        f"{BASE}/{session_id}/translate",
        json={"target_lang": "英文"},
        headers=user["headers"],
    ).json()["data"]
    assert only_lang["settings"]["target_lang"] == "英文"
    assert only_lang["settings"]["enabled"] is True
    assert only_lang["settings"]["direction"] == "reply"


def test_reply_translation_keeps_both_texts(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 输出侧：content = 模型原文，另一份 = 译文，界面默认显示译文。"""
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id)
    fake_llm.script["contents"] = [EN_REPLY, ZH_REPLY]
    before = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"][
        "total_tokens"
    ]

    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我该往哪走？"},
        headers=user["headers"],
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
        f"{BASE}/{session_id}/messages",
        json={"content": "你好"},
        headers=user["headers"],
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
        f"{BASE}/{session_id}/messages",
        json={"content": "你好"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    assistant = sent.json()["data"]["assistant_message"]
    assert assistant["content"] == JA_REPLY, "content 永远是模型说的话"
    assert assistant["translation"] is not None, (
        "★ 日文必须触发翻译（这正是旧版的 bug）"
    )
    assert assistant["translation"]["used_model"] is True
    assert assistant["translation"]["lang"] == "简体中文"
    assert assistant["translation"]["text"] == ZH_REPLY
    assert len(fake_llm.script["requests"]) == 2, "一次生成 + 一次翻译，正好两次"


def test_prompt_mode_only_touches_the_prompt(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id, mode="prompt")
    fake_llm.script["contents"] = [EN_REPLY]
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我该往哪走？"},
        headers=user["headers"],
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


def test_input_translation_reaches_the_model(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 输入侧：用户中文 → 译成英文再发；content 是模型看到的英文，原文另存。

    ★ 第十六轮起不再有"输入侧语言"设置：把目标语言选成英文即可做这件事 ——
    两个方向共用一个目标语言，源语言自动识别。
    """
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id, direction="input", target_lang="英文")
    fake_llm.script["contents"] = [EN_INPUT, EN_REPLY]

    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": ZH_INPUT},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    user_row = sent.json()["data"]["user_message"]
    assert user_row["content"] == EN_INPUT, "content = 模型看到的文本（译文）"
    assert user_row["translation"]["text"] == ZH_INPUT, "原话没有丢"
    assert user_row["translation"]["display"] == "translation", (
        "界面显示 translation.text（用户原话）"
    )
    assert user_row["translation"]["lang"] == "英文"
    # 模型真的看到了英文（而不是中文原文）
    seen = " ".join(
        str(getattr(m, "content", "")) for m in fake_llm.script["requests"][-1].messages
    )
    assert EN_INPUT in seen and ZH_INPUT not in seen


def test_both_directions_at_once(client: TestClient, user: dict, fake_llm) -> None:
    """★ 双向：一个目标语言同时管两边（英文输入 → 中文；英文回复 → 中文）。"""
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id, direction="both")  # 目标 = 默认简体中文
    fake_llm.script["contents"] = [ZH_INPUT, EN_REPLY, ZH_REPLY]
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": EN_INPUT},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    data = sent.json()["data"]
    assert len(fake_llm.script["requests"]) == 3, "两次翻译 + 一次生成"
    assert data["user_message"]["translation"]["lang"] == "简体中文"
    assert data["assistant_message"]["translation"]["lang"] == "简体中文"
    # ★ 不变量（整条链路都靠它）：content = **模型看到的文本**，translation.text = **给人看的文本**
    user_row, assistant_row = data["user_message"], data["assistant_message"]
    assert user_row["content"] == ZH_INPUT, "输入侧：模型看到的是译文（中文）"
    assert user_row["translation"]["text"] == EN_INPUT, (
        "输入侧：给人看的是用户原话（英文）"
    )
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
        f"{BASE}/{session_id}/messages",
        json={"content": "我该往哪走？"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    body = sent.json()["data"]
    assert body["assistant_message"]["content"] == EN_REPLY, "回复一个字都不能丢"
    assert any("没能把回复译成" in note for note in body["notes"]), body["notes"]


def test_independent_translation_model_is_used(
    client: TestClient, user: dict, fake_llm
) -> None:
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
    client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我该往哪走？"},
        headers=user["headers"],
    )
    rows = fake_llm.script.get("rows") or []
    assert other_id in rows, f"翻译应该用配置 #{other_id}（实际用过 {rows}）"
    assert session_provider in rows, "生成那一次仍然用会话模型"


def test_long_reply_is_skipped_and_said_so(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id, _provider = _make_session(client, user)
    _enable(client, user, session_id)
    fake_llm.script["contents"] = ["a" * (translate_mod.MAX_CHARS + 100)]
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "讲个长故事"},
        headers=user["headers"],
    )
    assistant = sent.json()["data"]["assistant_message"]
    assert assistant["translation"] is None
    assert len(fake_llm.script["requests"]) == 1, "太长就不译（不花这次钱）"


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
        f"{BASE}/{session_id}/stream",
        params={"content": "你好"},
        headers=user["headers"],
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
        f"{BASE}/{session_id}/messages",
        json={"content": "你好"},
        headers=user["headers"],
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
        f"{BASE}/{session_id}/messages",
        json={"content": "再说一句"},
        headers=user["headers"],
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
        f"{BASE}/{session_id}/messages",
        json={"content": "你好"},
        headers=user["headers"],
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


pytestmark = pytest.mark.integration
