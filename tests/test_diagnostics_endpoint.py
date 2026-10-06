"""`/api/v1/system/diagnostics`（运行环境快照）的契约测试。

==================== 为什么要给这个接口写测试 ====================
它会把**本机文件系统路径**返回给客户端（数据目录 / 日志目录 / 向量库目录）。
这是刻意的 —— 「关于」页要显示、用户要复制去打开目录、排查时要贴日志。
但正因为"它会吐路径"，必须钉住两条边界：

1. **需要登录**：未登录不该读到本机目录结构；
2. **不含密钥/口令/会话内容**：只允许出现环境事实。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app

DIAG = "/api/v1/system/diagnostics"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def user(client: TestClient) -> dict:
    """注册并登录一个临时用户（每个测试文件自带，与其它文件同一套写法）。"""
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"dg_{token}",
        "email": f"dg_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    created = client.post("/api/v1/auth/register", json=account)
    assert created.status_code == 201, created.text
    login = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert login.status_code == 200, login.text
    # ★ 注意：登录响应里只有令牌，用户 id 在**注册**响应里（与其它测试文件同一套写法）。
    return {
        "id": created.json()["data"]["id"],
        "username": account["username"],
        "headers": {"Authorization": f"Bearer {login.json()['data']['access_token']}"},
    }


#: 允许出现在响应里的键（白名单 —— 加字段必须同步改这里，逼作者想一次）
ALLOWED_KEYS = {
    "app", "version", "env", "backend", "database",
    "data_dir", "log_dir", "chroma_dir", "python", "platform",
}

#: 绝不允许出现的敏感词（键名与值里都不许有）
FORBIDDEN_SUBSTRINGS = (
    "password", "secret", "api_key", "apikey", "authorization", "bearer",
    "jwt", "token", "fernet", "mysql_password",
)


def test_diagnostics_requires_login(client: TestClient) -> None:
    """未登录必须被拒 —— 没有理由让未登录状态读到本机目录结构。"""
    resp = client.get(DIAG)
    assert resp.status_code == 401, resp.text


def test_diagnostics_shape_and_no_secrets(client: TestClient, user: dict) -> None:
    resp = client.get(DIAG, headers=user["headers"])
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    assert set(data) <= ALLOWED_KEYS, f"出现了白名单外的键：{set(data) - ALLOWED_KEYS}"
    for key in ("app", "version", "backend", "data_dir", "log_dir"):
        assert data.get(key), f"{key} 不能为空（关于页与复制诊断都要用）"

    # 路径必须**没有被打码** —— 打码了用户就没法照着打开了
    for path_key in ("data_dir", "log_dir"):
        value = str(data[path_key])
        assert "*" not in value and "…" not in value, f"{path_key} 不该被打码：{value}"

    blob = json.dumps(data, ensure_ascii=False).lower()
    hits = [w for w in FORBIDDEN_SUBSTRINGS if w in blob]
    assert not hits, f"响应里出现了敏感词：{hits}\n{json.dumps(data, ensure_ascii=False, indent=2)}"


def test_diagnostics_reports_actual_backend(client: TestClient, user: dict) -> None:
    """`backend` 要与当前实际使用的后端一致（默认 sqlite），否则「关于」页会撒谎。"""
    from app.core.config import get_settings

    data = client.get(DIAG, headers=user["headers"]).json()["data"]
    assert data["backend"] == get_settings().DB_BACKEND
    assert data["database"] == get_settings().database_label
