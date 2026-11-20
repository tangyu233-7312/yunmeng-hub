"""评测指标 + benchmark 脚本（评测本身也必须可测、可复现）。

★ 为什么评测要写单测：指标算错是最难发现的一类错误 —— 表格看起来"有数字"，
  但如果分母搞错（例如 Recall 的分母用了 k 而不是相关条目数），结论就全歪了。
  所以这里用**手算得出的小例子**把每个指标钉死，再用真实的合成标注集
  把"融合优于单通道""字面重排有害"这两条写进代码里的结论钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts"
for path in (str(PROJECT_ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import benchmark as bench  # noqa: E402
from app.llm.params import GenerationParams, compute_context_budget  # noqa: E402
from app.llm.schema import ChatResult, StreamChunk, TokenUsage  # noqa: E402

CASES = SCRIPTS / "benchmark_data" / "retrieval_cases.json"
STATE_CASES = SCRIPTS / "benchmark_data" / "state_cases.json"


# ==================================================================
#  四、真实数据只读回放（--from-db）
# ==================================================================
class FakeAdapter:
    """不联网的假适配器（只用于造几条真实历史消息，回放本身不需要模型）。"""

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
            content="（假回复）",
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
        yield StreamChunk(delta="（假回复）")
        yield StreamChunk(finish_reason="stop", usage=TokenUsage(total_tokens=15))

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch):
    from app.narrative import engine

    monkeypatch.setattr(engine, "build_adapter", lambda row: FakeAdapter(row))
    monkeypatch.setattr(engine.memory_mod, "remember_turn", lambda **_: None)
    yield FakeAdapter


@pytest.fixture
def replay_user():
    """造一个**独立账号**（只在它自己名下回放），跑完删掉，不碰任何真实用户。"""
    import uuid

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.db.models import User
    from app.db.mysql import session_scope
    from app.main import app

    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"bk_{token}",
        "email": f"bk_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    with TestClient(app) as client:
        assert client.post("/api/v1/auth/register", json=account).status_code == 201
        logged = client.post(
            "/api/v1/auth/login",
            json={"username": account["username"], "password": account["password"]},
        )
        headers = {"Authorization": f"Bearer {logged.json()['data']['access_token']}"}
        book = client.post(
            "/api/v1/world-books",
            json={
                "name": f"回放世界_{token}",
                "entries": [
                    {
                        "keys": ["灯塔"],
                        "content": "灯塔在风暴夜会熄灭",
                        "enabled": True,
                    },
                    {
                        "keys": ["补给船"],
                        "content": "补给船二十天来一次",
                        "enabled": True,
                    },
                    {"keys": ["龙"], "content": "龙已死去三百年", "enabled": True},
                ],
            },
            headers=headers,
        )
        assert book.status_code == 201, book.text
        card = client.post(
            "/api/v1/character-cards",
            json={
                "name": f"回放卡_{token}",
                "greeting": "……你来了。",
                "world_book_id": book.json()["data"]["id"],
                # ★ 声明了状态栏 → 回放时才会统计"状态块漏输出率 / 状态回放一致率"
                "extensions": {
                    "hne": {
                        "state_schema": [
                            {"name": "hp", "type": "meter", "max_field": "max"},
                            {"name": "location", "type": "text"},
                        ],
                        "initial_state": {"hp": 90, "max": 100, "location": "灯塔一层"},
                    }
                },
            },
            headers=headers,
        )
        assert card.status_code == 201, card.text
        provider = client.post(
            "/api/v1/providers",
            json={
                "name": f"回放模型_{token}",
                "provider_type": "openai_compatible",
                "base_url": "https://mock.invalid/v1",
                "api_key": "sk-replay-test",
                "model_name": "mock-model",
                "context_window": 8192,
            },
            headers=headers,
        )
        assert provider.status_code == 201, provider.text
        session_row = client.post(
            "/api/v1/narrative/sessions",
            json={
                "character_card_id": card.json()["data"]["id"],
                "llm_provider_id": provider.json()["data"]["id"],
                "title": f"回放会话_{token}",
            },
            headers=headers,
        )
        assert session_row.status_code == 201, session_row.text
        session_id = session_row.json()["data"]["id"]
        # 两轮用户消息（假模型不输出 <state> → 正好用来量"漏输出率"）
        for text in ("灯塔还亮着吗", "补给船什么时候来"):
            sent = client.post(
                f"/api/v1/narrative/sessions/{session_id}/messages",
                json={"content": text},
                headers=headers,
            )
            assert sent.status_code == 201, sent.text
    yield {"username": account["username"], "session_id": session_id}
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == account["username"]))
        if row is not None:
            db.delete(row)


def test_replay_from_db_is_readonly_and_reports_cost(replay_user) -> None:
    """★ 真库只读回放：读得出会话与轮次，报得出成本，且**不改任何数据**。"""
    from sqlalchemy import func, select

    from app.db.models import Message
    from app.db.mysql import session_scope

    with session_scope() as db:
        before = db.scalar(select(func.count()).select_from(Message))

    report = bench.replay_from_db(
        usernames=[replay_user["username"]],
        limit_sessions=5,
        rounds=2,
        semantic_mode="none",  # 只读回放默认不碰向量库（测试里更不能依赖嵌入模型）
    )

    with session_scope() as db:
        after = db.scalar(select(func.count()).select_from(Message))
    assert before == after, "回放必须是只读的：消息条数不能变"

    assert report["rows_replayed"] >= 1, report
    meta = report["meta"]
    assert meta["sessions_used"] >= 1
    assert meta["messages_scanned"] >= 2
    assert report["cost"]["注入条数(均)"] >= 1.0, "真实语料里应当至少注入一条"
    assert report["cost"]["注入token(均)"] > 0
    # 假模型不输出状态块 → 漏输出率应当是 100%（这条同时证明统计口径真的在算）
    assert meta["state_messages"] >= 1
    assert report["state_block_missing_rate"] == pytest.approx(1.0)


def test_replay_reports_state_idempotency(replay_user) -> None:
    """落库的状态"再校验一遍"不该被改动（幂等）—— 这是真实数据上的状态一致性度量。"""
    report = bench.replay_from_db(
        usernames=[replay_user["username"]],
        limit_sessions=5,
        rounds=1,
        semantic_mode="none",
    )
    meta = report["meta"]
    assert meta["state_sessions"] >= 1, "这个账号的会话声明了状态栏，应当被统计"
    assert report["state_idempotent_rate"] == pytest.approx(1.0)
    assert meta["state_not_idempotent"] == 0


def test_replay_respects_the_username_filter(replay_user) -> None:
    """只回放指定用户：别的账号（包括真实用户）一条都不该被读进来。"""
    rows, meta = bench.load_replay_rows(
        usernames=["绝对不存在的用户名_zzz"], limit_sessions=5, rounds=1
    )
    assert rows == [] and meta["sessions_used"] == 0


pytestmark = pytest.mark.integration
