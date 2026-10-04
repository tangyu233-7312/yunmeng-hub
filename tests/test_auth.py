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

from app.core.security import decode_token, hash_password, verify_password
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


def login(client: TestClient, account: dict[str, str], *, identifier: str | None = None):
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
#  一、密码哈希（纯单元测试）
# ==================================================================
def test_password_hash_is_not_plaintext() -> None:
    hashed = hash_password("Test-Passw0rd!")
    assert hashed != "Test-Passw0rd!"
    # bcrypt 哈希以 $2b$ 开头
    assert hashed.startswith("$2b$")


def test_password_hash_is_salted() -> None:
    """同一个密码两次哈希结果必须不同（随机盐），否则彩虹表就能用。"""
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_password() -> None:
    hashed = hash_password("Test-Passw0rd!")
    assert verify_password("Test-Passw0rd!", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_verify_password_tolerates_broken_hash() -> None:
    """哈希格式损坏时应返回 False，而不是抛异常导致 500。"""
    assert verify_password("whatever", "not-a-bcrypt-hash") is False


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


def test_register_duplicate_username(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    duplicate = dict(account)
    duplicate["email"] = f"other_{uuid.uuid4().hex[:8]}@example.com"

    response = register(client, duplicate)
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"
    assert "用户名" in response.json()["message"]


def test_register_duplicate_email(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    duplicate = dict(account)
    duplicate["username"] = f"other_{uuid.uuid4().hex[:8]}"

    response = register(client, duplicate)
    assert response.status_code == 409
    assert "邮箱" in response.json()["message"]


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


def test_register_rejects_password_over_bcrypt_limit(
    client: TestClient, account: dict[str, str]
) -> None:
    """★ bcrypt 只处理 72 字节。

    超过会被静默截断，导致「前 72 字节相同即视为同一密码」的安全问题，
    所以必须在接口层就拦下 —— 而且提示要说明是**字节**不是字符。
    """
    payload = dict(account)
    payload["password"] = "中" * 30  # 30 × 3 = 90 字节 > 72
    response = register(client, payload)

    assert response.status_code == 422
    assert "字节" in str(response.json()["detail"])


def test_register_accepts_password_at_byte_limit(
    client: TestClient, account: dict[str, str]
) -> None:
    """刚好 72 字节（24 个汉字）应当被接受。"""
    payload = dict(account)
    payload["password"] = "密" * 24  # 24 × 3 = 72 字节
    response = register(client, payload)
    assert response.status_code == 201


# ==================================================================
#  三、登录
# ==================================================================
def test_login_success(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    response = login(client, account)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0


def test_login_with_email(client: TestClient, account: dict[str, str]) -> None:
    """username 字段允许填邮箱。"""
    register(client, account)
    response = login(client, account, identifier=account["email"])
    assert response.status_code == 200


def test_login_wrong_password(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": "wrong-password"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


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


# ==================================================================
#  四、令牌
# ==================================================================
def test_access_token_payload(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    tokens = login(client, account).json()["data"]

    claims = decode_token(tokens["access_token"], expected_type="access")
    assert claims["sub"].isdigit()
    assert claims["type"] == "access"
    # 令牌里不应该放敏感信息
    assert "password" not in str(claims)


def test_me_returns_current_user(client: TestClient, account: dict[str, str]) -> None:
    register(client, account)
    tokens = login(client, account).json()["data"]

    response = client.get("/api/v1/auth/me", headers=auth_header(tokens["access_token"]))
    assert response.status_code == 200
    assert response.json()["data"]["username"] == account["username"]


def test_me_without_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_me_with_garbage_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me", headers=auth_header("not.a.jwt"))
    assert response.status_code == 401
    assert response.json()["detail"]["reason"] == "invalid_token"


def test_me_with_refresh_token_is_rejected(
    client: TestClient, account: dict[str, str]
) -> None:
    """★ 越权防护：refresh_token 有效期长达 7 天，绝不能当访问令牌用。"""
    register(client, account)
    tokens = login(client, account).json()["data"]

    response = client.get("/api/v1/auth/me", headers=auth_header(tokens["refresh_token"]))
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["expected"] == "access"
    assert detail["actual"] == "refresh"


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


def test_refresh_rejects_access_token(client: TestClient, account: dict[str, str]) -> None:
    """★ 反向防护：不允许用访问令牌换新的令牌对。"""
    register(client, account)
    tokens = login(client, account).json()["data"]

    response = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert response.status_code == 401
    assert response.json()["detail"]["expected"] == "refresh"


def test_oauth2_form_login_for_swagger(client: TestClient, account: dict[str, str]) -> None:
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
