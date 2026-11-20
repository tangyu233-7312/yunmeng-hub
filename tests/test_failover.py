"""备用模型自动切换（failover）与「流式传输」开关。

分两层：
  · 适配器包装（FailoverAdapter / WholeReplyAdapter）是纯逻辑，直接单测；
  · 接口层校验（备用模型必须是自己的另一个配置）走真实 API。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app

PROVIDERS = "/api/v1/providers"


# ==================================================================
#  三、接口层：备用模型的归属校验 + 流式开关
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"pv_{token}",
        "email": f"pv_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    created = client.post("/api/v1/auth/register", json=account)
    assert created.status_code == 201, created.text
    logged = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    return {
        "headers": {"Authorization": f"Bearer {logged.json()['data']['access_token']}"}
    }


def _provider(client: TestClient, user: dict, name: str, **extra) -> dict:
    body = {
        "name": name,
        "provider_type": "openai_compatible",
        "base_url": "https://mock.invalid/v1",
        "api_key": "sk-test",
        "model_name": "mock-model",
        "context_window": 8192,
        "generation": {"temperature": 0.8, "max_tokens": 1024},
    }
    body.update(extra)
    return client.post(PROVIDERS, json=body, headers=user["headers"])


def test_fallback_rejects_self_and_foreign(client: TestClient) -> None:
    user = _user(client)
    mine = _provider(client, user, f"主_{uuid.uuid4().hex[:6]}").json()["data"]
    other = _user(client)
    foreign = _provider(client, other, f"别人的_{uuid.uuid4().hex[:6]}").json()["data"]

    # 指向"自己另一个配置"→ 允许（新建时也能直接绑定）
    bound_ok = _provider(
        client, user, f"绑定_{uuid.uuid4().hex[:6]}", fallback_provider_id=mine["id"]
    )
    assert bound_ok.status_code == 201, bound_ok.text
    # 自指 / 指向别人的配置 → 400（在保存时就报，而不是等主模型挂了才发现）
    bad_self = client.patch(
        f"{PROVIDERS}/{mine['id']}",
        json={"fallback_provider_id": mine["id"]},
        headers=user["headers"],
    )
    assert bad_self.status_code == 400, bad_self.text
    bad_foreign = client.patch(
        f"{PROVIDERS}/{mine['id']}",
        json={"fallback_provider_id": foreign["id"]},
        headers=user["headers"],
    )
    assert bad_foreign.status_code == 400, bad_foreign.text


def test_fallback_can_be_cleared_with_null(client: TestClient) -> None:
    user = _user(client)
    main = _provider(client, user, f"主_{uuid.uuid4().hex[:6]}").json()["data"]
    backup = _provider(client, user, f"备_{uuid.uuid4().hex[:6]}").json()["data"]

    bound = client.patch(
        f"{PROVIDERS}/{main['id']}",
        json={"fallback_provider_id": backup["id"]},
        headers=user["headers"],
    )
    assert bound.status_code == 200, bound.text
    assert bound.json()["data"]["fallback_provider_id"] == backup["id"]

    cleared = client.patch(
        f"{PROVIDERS}/{main['id']}",
        json={"fallback_provider_id": None},
        headers=user["headers"],
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["fallback_provider_id"] is None


def test_stream_enabled_defaults_true_and_can_be_turned_off(client: TestClient) -> None:
    user = _user(client)
    row = _provider(client, user, f"流_{uuid.uuid4().hex[:6]}").json()["data"]
    assert row["stream_enabled"] is True, "默认必须开启流式（与用户既有行为一致）"

    off = client.patch(
        f"{PROVIDERS}/{row['id']}",
        json={"stream_enabled": False},
        headers=user["headers"],
    )
    assert off.status_code == 200, off.text
    assert off.json()["data"]["stream_enabled"] is False
    # 不传该字段时不能被重置成默认值
    keep = client.patch(
        f"{PROVIDERS}/{row['id']}", json={"name": "改个名"}, headers=user["headers"]
    )
    assert keep.json()["data"]["stream_enabled"] is False


pytestmark = pytest.mark.integration
