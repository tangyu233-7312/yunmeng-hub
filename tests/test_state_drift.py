"""状态漂移曲线：遥测落库 + 离线确定性模拟 + 反事实对照。

三组断言：
  · 纯函数：模拟可复现、越界程度只在"算得出来"时给值、脏值率只升不降
  · 真实链路：`<state>` 块经真实接口走一遍后，遥测真的落到 messages 上
    （这是第七轮那个"漏输出率恒为 100%"错误的根治点）
  · 出图：SVG 里真的有两条曲线（零依赖，文件可直接打开看）
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Message, User
from app.db.mysql import session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative import state_drift as drift

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_data" / "drift_script.json"

BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"

LEGACY_SCHEMA = {
    "spec": "hne_state_v1",
    "fields": [
        {"name": "hp", "label": "HP", "type": "meter", "max_field": "max"},
        {"name": "location", "label": "位置", "type": "text"},
    ],
}


# ==================================================================
#  二、真实链路：遥测必须落库（第七轮那个错误指标的根治点）
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
        return ChatResult(
            content=FakeAdapter.script.get("content", "（假回复）"),
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
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
        "username": f"dr_{token}",
        "email": f"dr_{token}@example.com",
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
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == data["username"]))
        if row is not None:
            db.delete(row)


def _make_session(client: TestClient, user: dict, *, with_schema: bool) -> int:
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"dr_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-drift-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
    payload = {"name": f"漂移卡_{uuid.uuid4().hex[:6]}", "greeting": "……你来了。"}
    if with_schema:
        payload["extensions"] = {"hne": {"state_schema": LEGACY_SCHEMA["fields"]}}
    card = client.post(CARDS, json=payload, headers=user["headers"])
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
    return session.json()["data"]["id"]


def _last_meta(session_id: int) -> dict | None:
    with session_scope() as db:
        row = db.scalars(
            select(Message)
            .where(Message.session_id == session_id, Message.role == "assistant")
            .order_by(Message.id.desc())
        ).first()
        if row is None or not row.state_meta_json:
            return None
        return json.loads(row.state_meta_json)


def test_telemetry_records_a_normalised_state(
    client: TestClient, user: dict, fake_llm
) -> None:
    """正常一轮：遥测记下"输出了块 + 修正了 1 处（越界夹取）+ 偏离度 > 0"。"""
    session_id = _make_session(client, user, with_schema=True)
    fake_llm.script["content"] = (
        '（她点头）\n<state>{"hp": {"current": 999, "max": 100}}</state>'
    )
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "还好吗"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    meta = _last_meta(session_id)
    assert meta is not None, "遥测必须落库（否则事后无法判断模型输出了没有）"
    assert meta["required"] is True and meta["had_block"] is True
    assert meta["counts"]["clamped"] == 1
    assert meta["deviation"] > 0, "夹取后与自报值必然有偏离"


def test_telemetry_records_a_missing_block(
    client: TestClient, user: dict, fake_llm
) -> None:
    """模型没输出块：遥测记 had_block=False —— 这才是真实的"漏输出率"依据。"""
    session_id = _make_session(client, user, with_schema=True)
    fake_llm.script["content"] = "（她沉默）"
    client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "怎么不说话"},
        headers=user["headers"],
    )
    meta = _last_meta(session_id)
    assert meta is not None and meta["had_block"] is False
    assert meta["required"] is True


def test_telemetry_marks_unrequired_when_card_has_no_schema(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 卡没声明状态栏：遥测标 required=False —— 统计漂移时**不该**算它漏输出。"""
    session_id = _make_session(client, user, with_schema=False)
    fake_llm.script["content"] = "（她抬头）"
    client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "你好"},
        headers=user["headers"],
    )
    meta = _last_meta(session_id)
    assert meta is not None and meta["required"] is False


def test_records_from_db_reads_only_telemetry(
    client: TestClient, user: dict, fake_llm
) -> None:
    """真实数据聚合：只统计带遥测的轮次，并如实报告有多少条是遥测之前的。"""
    session_id = _make_session(client, user, with_schema=True)
    for index, content in enumerate(
        [
            '正文\n<state>{"hp": {"current": 50, "max": 100}}</state>',
            "没有状态块的正文",
            '正文\n<state>{"hp": {"current": 150, "max": 100}}</state>',
        ]
    ):
        fake_llm.script["content"] = content
        sent = client.post(
            f"{BASE}/{session_id}/messages",
            json={"content": f"第{index}轮"},
            headers=user["headers"],
        )
        assert sent.status_code == 201, sent.text
    records, meta = drift.records_from_db(usernames=[user["username"]])
    assert meta["with_telemetry"] == 3, meta
    assert meta["required"] == 3
    assert sum(1 for r in records if not r.had_block) == 1, "第 2 轮漏输出要能被数出来"
    assert sum(r.counts["clamped"] for r in records) == 1
    rows = drift.bucket(records, 3)
    assert rows and rows[0]["missing_rate"] == pytest.approx(1 / 3)


pytestmark = pytest.mark.integration
