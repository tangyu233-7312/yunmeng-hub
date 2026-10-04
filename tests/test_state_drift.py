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
from app.narrative import state as state_mod
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
#  一、纯函数：模拟与指标
# ==================================================================
def _spec() -> dict:
    return json.loads(SCRIPT.read_text(encoding="utf-8"))


def test_simulate_is_deterministic() -> None:
    """评测要能复现：跑两次结果逐字节相同（没有随机数）。"""
    first = [r.to_dict() for r in drift.simulate(_spec())]
    second = [r.to_dict() for r in drift.simulate(_spec())]
    assert first == second
    assert len(first) == 40


def test_counterfactual_drifts_while_validated_stays_in_range() -> None:
    """★ 核心结论：同一批模型输出，不校验会一路漂出合法区间，校验后恒为 0。"""
    records = drift.simulate(_spec())
    buckets = drift.bucket(records, 5)
    naive = [row["naive_out_of_range_mean"] for row in buckets]
    assert naive[0] < naive[-1], f"反事实应当越漂越大：{naive}"
    assert naive[-1] > 1.0, f"最后一档应当明显越界：{naive}"
    # 有校验的那条：状态的越界程度**恒为 0**（被夹住了），这里直接用最终状态验证
    schema, _ = state_mod.schema_mod.parse_schema(_spec()["schema"])
    bounds = drift.bounds_from(schema, _spec()["initial"])
    valid = dict(_spec()["initial"])
    for round_no in range(1, 41):
        raw = drift._raw_for_round(round_no, _spec(), valid)
        if raw is None:
            continue
        valid, _notes = state_mod.normalize(raw, valid, schema)
    assert drift.out_of_range(valid, schema, bounds=bounds) == 0.0


def test_bounds_come_from_the_initial_state_not_the_current_one() -> None:
    """★ 标尺必须是故事开始时的上限：模型把 max 也写飘了，不能"自己给自己放宽"。"""
    schema, _ = state_mod.schema_mod.parse_schema(_spec()["schema"])
    bounds = drift.bounds_from(schema, _spec()["initial"])
    assert bounds["hp"] == 100
    inflated = {"hp": 350, "max": 400}  # 模型把上限也写成 400
    assert drift.out_of_range(inflated, schema, bounds=bounds) == pytest.approx(2.5)


def test_out_of_range_returns_none_when_nothing_is_computable() -> None:
    """算不出来 ≠ 合法：hp 被写成文字时必须给 None，否则均值会被假的 0 拉低。"""
    schema, _ = state_mod.schema_mod.parse_schema(_spec()["schema"])
    assert drift.out_of_range({"hp": "一百", "max": 100}, schema, bounds={"hp": 100.0}) is None


def test_garbage_rate_only_grows_in_the_counterfactual() -> None:
    """脏值一旦写进去就洗不掉：反事实的脏值率只会升不会降（这正是校验的意义）。"""
    buckets = drift.bucket(drift.simulate(_spec()), 5)
    rates = [row["naive_garbage_rate"] for row in buckets]
    assert rates[0] == 0.0
    assert max(rates) == 1.0
    assert rates[-1] >= rates[0]


def test_missing_block_rounds_are_flagged() -> None:
    """脚本里第 7 / 31 轮"模型没输出状态块"，遥测必须如实标出来（而不是靠猜）。"""
    records = {r.round_no: r for r in drift.simulate(_spec())}
    assert records[7].had_block is False
    assert records[31].had_block is False
    assert records[8].had_block is True
    totals = drift.summary(drift.simulate(_spec()))
    assert totals["missing_blocks"] == 2
    assert totals["missing_rate"] == pytest.approx(0.05)


def test_all_four_kinds_show_up_in_the_script() -> None:
    """脚本覆盖四类机制：越界夹取 / 上限跳变护栏 / 脏值丢弃 / 未知字段忽略。"""
    totals = drift.summary(drift.simulate(_spec()))
    for kind in drift.KINDS:
        assert totals[f"{kind}_total"] > 0, f"脚本没有覆盖 {kind}：{totals}"


def test_svg_contains_both_series() -> None:
    """零依赖出图：SVG 里必须有两条曲线与图例（能直接打开看）。"""
    buckets = drift.bucket(drift.simulate(_spec()), 5)
    xs = [int(str(row["bucket"]).split("-")[0]) for row in buckets]
    svg = drift.to_svg(
        {
            "不校验（反事实）": [(x, row["naive_out_of_range_mean"]) for x, row in zip(xs, buckets)],
            "有校验（实际）": [(x, 0.0) for x in xs],
        },
        title="状态漂移曲线",
        y_max=5,
    )
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "不校验（反事实）" in svg and "有校验（实际）" in svg
    assert svg.count("<path") == 2, "两条曲线各一条 path"
    assert "状态漂移曲线" in svg


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
    fake_llm.script["content"] = '（她点头）\n<state>{"hp": {"current": 999, "max": 100}}</state>'
    sent = client.post(
        f"{BASE}/{session_id}/messages", json={"content": "还好吗"}, headers=user["headers"]
    )
    assert sent.status_code == 201, sent.text
    meta = _last_meta(session_id)
    assert meta is not None, "遥测必须落库（否则事后无法判断模型输出了没有）"
    assert meta["required"] is True and meta["had_block"] is True
    assert meta["counts"]["clamped"] == 1
    assert meta["deviation"] > 0, "夹取后与自报值必然有偏离"


def test_telemetry_records_a_missing_block(client: TestClient, user: dict, fake_llm) -> None:
    """模型没输出块：遥测记 had_block=False —— 这才是真实的"漏输出率"依据。"""
    session_id = _make_session(client, user, with_schema=True)
    fake_llm.script["content"] = "（她沉默）"
    client.post(f"{BASE}/{session_id}/messages", json={"content": "怎么不说话"}, headers=user["headers"])
    meta = _last_meta(session_id)
    assert meta is not None and meta["had_block"] is False
    assert meta["required"] is True


def test_telemetry_marks_unrequired_when_card_has_no_schema(
    client: TestClient, user: dict, fake_llm
) -> None:
    """★ 卡没声明状态栏：遥测标 required=False —— 统计漂移时**不该**算它漏输出。"""
    session_id = _make_session(client, user, with_schema=False)
    fake_llm.script["content"] = "（她抬头）"
    client.post(f"{BASE}/{session_id}/messages", json={"content": "你好"}, headers=user["headers"])
    meta = _last_meta(session_id)
    assert meta is not None and meta["required"] is False


def test_records_from_db_reads_only_telemetry(client: TestClient, user: dict, fake_llm) -> None:
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


def test_apply_reply_with_meta_keeps_the_two_tuple_wrapper() -> None:
    """`apply_reply` 的返回值形状不能变（老调用方与老测试都靠它）。"""
    session = type("S", (), {"id": 1, "state_json": None, "state_schema_json": json.dumps(LEGACY_SCHEMA)})()
    cleaned, notes = state_mod.apply_reply(session, "只有正文")
    assert cleaned == "只有正文"
    assert notes and "没有 <state> 状态块" in notes[0]
    _cleaned, _notes, meta = state_mod.apply_reply_with_meta(session, "只有正文")
    assert meta["required"] is True and meta["had_block"] is False


def test_classify_notes_splits_the_four_mechanisms() -> None:
    """四种机制必须分开计数（合成"修正"一类就看不出是哪一层在起作用）。"""
    counts = state_mod.classify_notes(
        [
            "hp 越界（150），已夹到 0～100 之间",
            "hp 上限一轮内从 100 变成 300（超过 50%），已保留上一轮的上限",
            "状态块里的 hp 不是数字，已沿用上一轮",
            "状态块里有这张卡没有定义的字段，已忽略：金币",
        ]
    )
    assert counts == {"clamped": 1, "guarded": 1, "rejected": 1, "unknown": 1, "other": 0}
