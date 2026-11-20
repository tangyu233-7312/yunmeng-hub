"""3.5 用户认证测试（集成测试，需要 MySQL）。

覆盖：注册校验、重复检测、登录、令牌签发与校验、刷新、越权防护、
      以及几个容易被忽略的安全细节（密码不回显、错误信息不泄露用户是否存在）。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_auth.py -v
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import verify_password
from app.db.models import User
from app.db.mysql import session_scope
from app.main import app


# ==================================================================
#  夹具
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def account() -> dict[str, str]:
    """生成一个随机测试账号，用例结束自动清理。"""
    token = uuid.uuid4().hex[:10]
    data = {
        "username": f"user_{token}",
        "email": f"{token}@example.com",
        "password": "Test-Passw0rd!",
        "nickname": "测试用户",
    }
    yield data
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == data["username"]))
        if row is not None:
            db.delete(row)


def register(client: TestClient, account: dict[str, str]):
    return client.post("/api/v1/auth/register", json=account)


def login(
    client: TestClient, account: dict[str, str], *, identifier: str | None = None
):
    return client.post(
        "/api/v1/auth/login",
        json={
            "username": identifier or account["username"],
            "password": account["password"],
        },
    )


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ==================================================================
#  二、注册
# ==================================================================
def test_register_success(client: TestClient, account: dict[str, str]) -> None:
    response = register(client, account)
    assert response.status_code == 201

    body = response.json()
    assert body["code"] == "OK"
    data = body["data"]
    assert data["username"] == account["username"]
    assert data["is_active"] is True
    # ★ 响应里绝不能出现密码相关字段
    assert "password" not in response.text
    assert "password_hash" not in response.text


def test_registered_password_is_hashed_in_database(
    client: TestClient, account: dict[str, str]
) -> None:
    """★ 数据库里必须只有哈希，没有明文。"""
    register(client, account)
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == account["username"]))
        assert row is not None
        assert row.password_hash != account["password"]
        assert verify_password(account["password"], row.password_hash)


@pytest.mark.parametrize(
    ("field", "value", "hint"),
    [
        ("password", "short", "至少 8 位"),
        ("username", "ab", "用户名"),
        ("username", "有中文的名字", "用户名"),
        ("email", "not-an-email", "email"),
    ],
)
def test_register_validation(
    client: TestClient, account: dict[str, str], field: str, value: str, hint: str
) -> None:
    payload = dict(account)
    payload[field] = value
    response = register(client, payload)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert hint in str(body["detail"])


def test_login_nonexistent_user_gives_same_message(
    client: TestClient, account: dict[str, str]
) -> None:
    """★ 安全要求：「用户不存在」与「密码错误」必须返回**完全相同**的信息。

    否则攻击者可以靠错误文案枚举出哪些用户名是有效的。
    """
    register(client, account)
    wrong_password = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": "wrong-password"},
    )
    no_such_user = client.post(
        "/api/v1/auth/login",
        json={"username": "definitely_not_exists_xyz", "password": "wrong-password"},
    )

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json()["message"] == no_such_user.json()["message"]


def test_login_disabled_account(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == account["username"]))
        assert row is not None
        row.is_active = False

    response = login(client, account)
    assert response.status_code == 401
    assert "禁用" in response.json()["message"]

    # 恢复，避免影响夹具清理
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == account["username"]))
        if row is not None:
            row.is_active = True


def test_me_returns_current_user(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    tokens = login(client, account).json()["data"]

    response = client.get(
        "/api/v1/auth/me", headers=auth_header(tokens["access_token"])
    )
    assert response.status_code == 200
    assert response.json()["data"]["username"] == account["username"]


def test_refresh_issues_new_tokens(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    tokens = login(client, account).json()["data"]

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200
    new_tokens = response.json()["data"]
    assert new_tokens["access_token"]

    # 新令牌应能正常访问
    me = client.get("/api/v1/auth/me", headers=auth_header(new_tokens["access_token"]))
    assert me.status_code == 200


def test_oauth2_form_login_for_swagger(
    client: TestClient, account: dict[str, str]
) -> None:
    """给 Swagger 的 Authorize 按钮用的表单登录端点。"""
    register(client, account)
    response = client.post(
        "/api/v1/auth/token",
        data={"username": account["username"], "password": account["password"]},
    )
    assert response.status_code == 200
    body = response.json()
    # OAuth2 流程要求顶层直接有 access_token / token_type
    assert "access_token" in body
    assert body["token_type"] == "bearer"


pytestmark = pytest.mark.integration


@pytest.mark.parametrize("field,hint", [("username", "用户名"), ("email", "邮箱")])
def test_register_conflicts(client, account, field, hint):
    assert register(client, account).status_code == 201
    duplicate = dict(account)
    other = "other_" + uuid.uuid4().hex[:8]
    duplicate["email" if field == "username" else "username"] = (
        other + "@example.com" if field == "username" else other
    )
    response = register(client, duplicate)
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"
    assert hint in response.json()["message"]


@pytest.mark.parametrize(
    "password,status",
    [("密" * 24, 201), ("密" * 25, 422)],
    ids=["72-bytes", "75-bytes"],
)
def test_register_password_byte_boundary(client, account, password, status):
    response = register(client, {**account, "password": password})
    assert response.status_code == status
    if status == 422:
        assert "字节" in str(response.json()["detail"])


@pytest.mark.parametrize("identifier", ["username", "email"])
def test_login_identifiers(client, account, identifier):
    assert register(client, account).status_code == 201
    response = login(client, account, identifier=account[identifier])
    assert response.status_code == 200
    tokens = response.json()["data"]
    assert tokens["refresh_token"] and tokens["expires_in"] > 0
    assert tokens["token_type"] == "bearer"
    me = client.get("/api/v1/auth/me", headers=auth_header(tokens["access_token"]))
    assert me.status_code == 200
    assert me.json()["data"]["username"] == account["username"]


@pytest.mark.parametrize(
    "case", ["missing", "malformed", "refresh-as-access", "access-as-refresh"]
)
def test_token_boundaries(client, account, case):
    headers = {}
    if case == "malformed":
        headers = auth_header("not.a.jwt")
    if case in ("refresh-as-access", "access-as-refresh"):
        assert register(client, account).status_code == 201
        tokens = login(client, account).json()["data"]
        if case == "access-as-refresh":
            response = client.post(
                "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
            )
        else:
            response = client.get(
                "/api/v1/auth/me", headers=auth_header(tokens["refresh_token"])
            )
    else:
        response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "UNAUTHORIZED"
    if case == "malformed":
        assert body["detail"]["reason"] == "invalid_token"
    if case in ("refresh-as-access", "access-as-refresh"):
        assert body["detail"]["expected"] == (
            "access" if case == "refresh-as-access" else "refresh"
        )
        assert body["detail"]["actual"] == (
            "refresh" if case == "refresh-as-access" else "access"
        )
