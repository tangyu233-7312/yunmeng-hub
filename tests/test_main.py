"""3.1 基础能力冒烟测试。

覆盖：健康检查、统一响应结构、统一错误结构、请求 ID 透传。
运行： .\\.venv\\Scripts\\python.exe -m pytest -v
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


@pytest.fixture(scope="module")
def client():
    """使用上下文管理器启动，确保 lifespan（日志、目录初始化）被执行。"""
    with TestClient(app) as test_client:
        yield test_client


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok", body["components"]
    assert body["app"] == "YunMengHub"
    assert body["version"]


@pytest.mark.parametrize("component", ["database", "vector_store"])
def test_health_reports_degraded_dependency(
    client: TestClient, monkeypatch, component: str
) -> None:
    """A failed dependency must be visible in both its component and overall status."""
    from app import main as app_main

    target = "check_connection" if component == "database" else "check_vector_store"
    monkeypatch.setattr(
        app_main, target, lambda **kwargs: {"status": "error", "message": "test outage"}
    )
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"][component]["status"] == "error"


def test_health_reports_database_component(client: TestClient) -> None:
    """健康检查必须如实报告数据库状态，而不是笼统地说 ok。"""
    body = client.get("/health").json()
    assert "database" in body["components"]
    assert body["components"]["database"]["status"] == "ok"
    # 顺便确认探活真的问出了"用的哪个后端 + 服务端版本"。
    # ★ 这里断言的是中性的 `backend` / `server`，而不是早期的 `mysql_version`：
    #   引入 SQLite 之后，"后端一定是 MySQL"这个前提不再成立，
    #   把后端名字写死进键名会让健康检查对外说谎。
    detail = body["components"]["database"]["detail"]
    assert detail["backend"] == get_settings().DB_BACKEND
    assert detail["server"]


def test_root_ok(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["code"] == "OK"


def test_404_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/definitely-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert set(body) == {"code", "message", "request_id"}


def test_405_error_shape(client: TestClient) -> None:
    response = client.post("/api/v1/ping")
    assert response.status_code == 405
    assert response.json()["code"] == "METHOD_NOT_ALLOWED"


def test_request_id_is_generated(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers.get("X-Request-ID")


def test_request_id_is_propagated(client: TestClient) -> None:
    """前端传入的 X-Request-ID 应被原样透传，便于前后端串联排查。"""
    response = client.get("/health", headers={"X-Request-ID": "trace-me-0001"})
    assert response.headers["X-Request-ID"] == "trace-me-0001"


pytestmark = pytest.mark.integration
