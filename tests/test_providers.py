"""3.7 模型配置 CRUD 接口测试（集成测试，需要 MySQL）。

覆盖：增删改查、密钥加密存储、密钥三态更新、默认模型互斥、
      越权防护、参数校验，以及响应中绝不泄露密钥。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_providers.py -v
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.security import get_api_key_cipher
from app.db.models import CharacterCard, LLMProvider, NarrativeSession, User
from app.db.mysql import session_scope
from app.main import app

PLAIN_API_KEY = "sk-super-secret-value-abcdef123456"


# ==================================================================
#  夹具
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_user(client: TestClient) -> dict:
    """注册并登录一个随机用户，返回其 id / 认证头 / 用户名。"""
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"pv_{token}",
        "email": f"pv_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    created = client.post("/api/v1/auth/register", json=account)
    assert created.status_code == 201, created.text

    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged_in.status_code == 200, logged_in.text
    access_token = logged_in.json()["data"]["access_token"]

    return {
        "id": created.json()["data"]["id"],
        "username": account["username"],
        "headers": {"Authorization": f"Bearer {access_token}"},
    }


def _cleanup_user(username: str) -> None:
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == username))
        if row is not None:
            db.delete(row)  # 其名下配置由外键级联删除


@pytest.fixture
def user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    _cleanup_user(data["username"])


@pytest.fixture
def other_user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    _cleanup_user(data["username"])


def payload(**overrides) -> dict:
    data = {
        "name": f"cfg_{uuid.uuid4().hex[:8]}",
        "provider_type": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "api_key": PLAIN_API_KEY,
        "model_name": "deepseek-flash",
        "context_window": 65536,
        "generation": {
            "temperature": 0.9,
            "max_tokens": 4096,
            "reasoning_effort": "high",
        },
    }
    data.update(overrides)
    return data


def create(client: TestClient, user: dict, **overrides) -> dict:
    response = client.post(
        "/api/v1/providers", json=payload(**overrides), headers=user["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ==================================================================
#  一、认证要求
# ==================================================================
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        # 注意：TestClient.get() / delete() 不接受 json 参数，
        # 只有 post / patch / put 才有请求体，所以这里分开传参
        ("get", "/api/v1/providers", {}),
        ("post", "/api/v1/providers", {"json": {}}),
        ("get", "/api/v1/providers/1", {}),
        ("patch", "/api/v1/providers/1", {"json": {}}),
        ("delete", "/api/v1/providers/1", {}),
        ("post", "/api/v1/providers/test-draft", {"json": {}}),
    ],
)
def test_endpoints_require_login(
    client: TestClient, method: str, path: str, kwargs: dict
) -> None:
    """★ 所有模型配置接口都必须要求登录。"""
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


# ==================================================================
#  二、新增
# ==================================================================
def test_create_provider(client: TestClient, user: dict) -> None:
    data = create(client, user)

    assert data["name"]
    assert data["provider_type"] == "openai_compatible"
    assert data["model_name"] == "deepseek-flash"
    assert data["has_api_key"] is True
    assert data["api_key_decryptable"] is True
    # ★ 密钥只以脱敏形式出现
    assert data["api_key_masked"] == "sk-s****3456"
    assert data["generation"]["temperature"] == 0.9
    assert data["generation"]["max_tokens"] == 4096


def test_create_response_never_contains_plaintext_key(
    client: TestClient, user: dict
) -> None:
    """★ 响应里绝不能出现密钥明文。"""
    response = client.post(
        "/api/v1/providers", json=payload(), headers=user["headers"]
    )
    assert PLAIN_API_KEY not in response.text


def test_api_key_is_encrypted_in_database(client: TestClient, user: dict) -> None:
    """★ 数据库里存的必须是密文，且能正确解密回明文。"""
    data = create(client, user)

    with session_scope() as db:
        row = db.get(LLMProvider, data["id"])
        assert row is not None
        assert row.api_key_encrypted != PLAIN_API_KEY
        assert PLAIN_API_KEY not in row.api_key_encrypted
        # 用同一个密钥能解回来
        assert get_api_key_cipher().decrypt(row.api_key_encrypted) == PLAIN_API_KEY


def test_create_without_api_key_for_local_model(client: TestClient, user: dict) -> None:
    """本地部署（Ollama / vLLM）允许不填密钥。"""
    data = create(
        client,
        user,
        base_url="http://localhost:11434/v1",
        model_name="qwen2.5:7b",
        api_key="",
    )
    assert data["has_api_key"] is False
    assert data["api_key_masked"] == ""


def test_create_duplicate_name_is_conflict(client: TestClient, user: dict) -> None:
    name = f"dup_{uuid.uuid4().hex[:8]}"
    create(client, user, name=name)

    response = client.post(
        "/api/v1/providers", json=payload(name=name), headers=user["headers"]
    )
    assert response.status_code == 409
    assert "同名" in response.json()["message"]


def test_same_name_allowed_for_different_users(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """唯一约束是 (user_id, name)，不同用户之间可以重名。"""
    name = f"shared_{uuid.uuid4().hex[:8]}"
    create(client, user, name=name)
    data = create(client, other_user, name=name)
    assert data["name"] == name


def test_create_rejects_unknown_provider_type(client: TestClient, user: dict) -> None:
    response = client.post(
        "/api/v1/providers",
        json=payload(provider_type="gemini"),
        headers=user["headers"],
    )
    assert response.status_code == 422


def test_create_rejects_invalid_base_url(client: TestClient, user: dict) -> None:
    response = client.post(
        "/api/v1/providers",
        json=payload(base_url="api.deepseek.com"),
        headers=user["headers"],
    )
    assert response.status_code == 422


def test_create_rejects_output_larger_than_window(client: TestClient, user: dict) -> None:
    """★ 跨字段校验必须返回 422，而不是等到组装配置时变成 500。"""
    response = client.post(
        "/api/v1/providers",
        json=payload(
            context_window=4096, generation={"max_tokens": 4096, "temperature": 0.8}
        ),
        headers=user["headers"],
    )
    assert response.status_code == 422
    assert "上下文窗口" in response.text


def test_create_rejects_invalid_temperature(client: TestClient, user: dict) -> None:
    response = client.post(
        "/api/v1/providers",
        json=payload(generation={"temperature": 5.0, "max_tokens": 1024}),
        headers=user["headers"],
    )
    assert response.status_code == 422


def test_create_rejects_invalid_reasoning_effort(client: TestClient, user: dict) -> None:
    response = client.post(
        "/api/v1/providers",
        json=payload(generation={"max_tokens": 1024, "reasoning_effort": "extreme"}),
        headers=user["headers"],
    )
    assert response.status_code == 422


# ==================================================================
#  三、查询
# ==================================================================
def test_list_only_returns_own_providers(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """★ 列表接口只能看到自己的配置。"""
    mine = create(client, user)
    theirs = create(client, other_user)

    response = client.get("/api/v1/providers", headers=user["headers"])
    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["data"]]

    assert mine["id"] in ids
    assert theirs["id"] not in ids


def test_get_provider(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.get(f"/api/v1/providers/{created['id']}", headers=user["headers"])
    assert response.status_code == 200
    assert response.json()["data"]["id"] == created["id"]


def test_cannot_read_other_users_provider(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """★ 越权防护：访问别人的配置必须 404（而不是 403）。

    返回 404 而不是 403 是刻意的 —— 403 会暴露「这个 ID 确实存在」，
    攻击者可以据此枚举出别人有哪些配置。
    """
    theirs = create(client, other_user)

    response = client.get(f"/api/v1/providers/{theirs['id']}", headers=user["headers"])
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_cannot_delete_other_users_provider(
    client: TestClient, user: dict, other_user: dict
) -> None:
    theirs = create(client, other_user)
    response = client.delete(f"/api/v1/providers/{theirs['id']}", headers=user["headers"])
    assert response.status_code == 404

    # 确认它还在
    still_there = client.get(
        f"/api/v1/providers/{theirs['id']}", headers=other_user["headers"]
    )
    assert still_there.status_code == 200


def test_get_missing_provider(client: TestClient, user: dict) -> None:
    response = client.get("/api/v1/providers/999999999", headers=user["headers"])
    assert response.status_code == 404


# ==================================================================
#  四、响应里的派生信息
# ==================================================================
def test_response_includes_budget_and_hints(client: TestClient, user: dict) -> None:
    """前端拿到的应当是可以直接渲染的完整信息。

    ★ 第二十七轮：`reasoning_effort_support`（探测结论）字段已随探测功能一起删除；
      hints 的文案也从"尚未验证，建议去检测"改成了"会被翻译发送、不支持则自动退回"。
    """
    data = create(client, user)

    budget = data["budget"]
    assert budget["context_window"] == 65536
    assert budget["max_output_tokens"] == 4096
    assert "包含思考" in budget["note"]

    # 思考强度设为 high -> 给灰色**提示**（说明适配器怎么处理），不是黄色警告
    assert data["hints"]
    assert any("自动去掉" in hint for hint in data["hints"]), data["hints"]
    assert not any("尚未验证" in hint for hint in data["hints"]), (
        "不该再让用户去做那个已被删除的「检测」"
    )

    # 探测结论字段必须已经不存在（删干净了，别留个空壳误导前端）
    assert "reasoning_effort_support" not in data, data.keys()



# ==================================================================
#  五、更新
# ==================================================================
def test_update_name_only_keeps_api_key(client: TestClient, user: dict) -> None:
    """★ 不改密钥时，密钥必须原样保留。

    前端拿不到明文密钥，编辑表单里只能显示占位符，
    因此「不传 api_key」必须解释为「保持不变」而不是「清空」。
    """
    created = create(client, user)
    new_name = f"renamed_{uuid.uuid4().hex[:8]}"

    response = client.patch(
        f"/api/v1/providers/{created['id']}",
        json={"name": new_name},
        headers=user["headers"],
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == new_name
    assert data["has_api_key"] is True
    assert data["api_key_masked"] == created["api_key_masked"]


def test_update_with_empty_api_key_keeps_original(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.patch(
        f"/api/v1/providers/{created['id']}",
        json={"api_key": ""},
        headers=user["headers"],
    )
    assert response.json()["data"]["has_api_key"] is True


def test_update_api_key_replaces_it(client: TestClient, user: dict) -> None:
    created = create(client, user)
    new_key = "sk-brand-new-key-987654321"

    response = client.patch(
        f"/api/v1/providers/{created['id']}",
        json={"api_key": new_key},
        headers=user["headers"],
    )
    assert response.status_code == 200
    assert new_key not in response.text

    with session_scope() as db:
        row = db.get(LLMProvider, created["id"])
        assert row is not None
        assert get_api_key_cipher().decrypt(row.api_key_encrypted) == new_key


def test_clear_api_key(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.patch(
        f"/api/v1/providers/{created['id']}",
        json={"clear_api_key": True},
        headers=user["headers"],
    )
    data = response.json()["data"]
    assert data["has_api_key"] is False
    assert data["api_key_masked"] == ""


def test_update_generation_params(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.patch(
        f"/api/v1/providers/{created['id']}",
        json={"generation": {"temperature": 0.3, "max_tokens": 1024, "reasoning_effort": "off"}},
        headers=user["headers"],
    )
    data = response.json()["data"]
    assert data["generation"]["temperature"] == 0.3
    assert data["generation"]["max_tokens"] == 1024
    assert data["generation"]["reasoning_effort"] == "off"


def test_update_validation_uses_final_combination(client: TestClient, user: dict) -> None:
    """★ 部分更新也要做跨字段校验。

    只提交 max_tokens，context_window 沿用数据库里的旧值 ——
    接口层的模型校验看不到完整组合，必须由服务层兜住。
    """
    created = create(client, user, context_window=4096, generation={"max_tokens": 1024})

    response = client.patch(
        f"/api/v1/providers/{created['id']}",
        json={"generation": {"max_tokens": 4096}},
        headers=user["headers"],
    )
    assert response.status_code == 400
    assert "上下文窗口" in response.json()["message"]


# ==================================================================
#  六、默认模型互斥
# ==================================================================
def test_only_one_default_provider(client: TestClient, user: dict) -> None:
    """★ 每个用户最多一个默认模型 —— 设置新的会自动取消旧的。"""
    first = create(client, user, is_default=True)
    second = create(client, user, is_default=True)

    listed = client.get("/api/v1/providers", headers=user["headers"]).json()["data"]
    defaults = [item for item in listed if item["is_default"]]

    assert len(defaults) == 1
    assert defaults[0]["id"] == second["id"]

    # 第一条应当已被取消
    first_after = client.get(
        f"/api/v1/providers/{first['id']}", headers=user["headers"]
    ).json()["data"]
    assert first_after["is_default"] is False


def test_promote_existing_provider_to_default(client: TestClient, user: dict) -> None:
    first = create(client, user, is_default=True)
    second = create(client, user)

    response = client.patch(
        f"/api/v1/providers/{second['id']}",
        json={"is_default": True},
        headers=user["headers"],
    )
    assert response.json()["data"]["is_default"] is True

    first_after = client.get(
        f"/api/v1/providers/{first['id']}", headers=user["headers"]
    ).json()["data"]
    assert first_after["is_default"] is False


def test_default_provider_listed_first(client: TestClient, user: dict) -> None:
    create(client, user)
    target = create(client, user, is_default=True)

    listed = client.get("/api/v1/providers", headers=user["headers"]).json()["data"]
    assert listed[0]["id"] == target["id"]


# ==================================================================
#  七、删除
# ==================================================================
def test_delete_provider(client: TestClient, user: dict) -> None:
    created = create(client, user)

    response = client.delete(
        f"/api/v1/providers/{created['id']}", headers=user["headers"]
    )
    assert response.status_code == 200

    gone = client.get(f"/api/v1/providers/{created['id']}", headers=user["headers"])
    assert gone.status_code == 404


def test_delete_provider_keeps_narrative_sessions(
    client: TestClient, user: dict
) -> None:
    """★ 删除模型配置**不能**连带删掉用户的故事。

    外键是 ON DELETE SET NULL：会话要保留，只是「不记得当初用的哪个模型了」。
    ★ 这里必须实测而不是靠推理：
      SQLAlchemy 删除父对象时的默认行为是「把子对象外键置 NULL」，
      在本表（外键可空 + SET NULL）上恰好正确；
      但在 character_cards 那张表（外键非空 + CASCADE）上就会直接报错。
      两张表写法相似、行为相反，只有真的删一次才能确认没写反。
    """
    created = create(client, user)

    with session_scope() as db:
        card = CharacterCard(user_id=user["id"], name="删除模型测试卡", extra_data={})
        db.add(card)
        db.flush()
        session = NarrativeSession(
            user_id=user["id"],
            character_card_id=card.id,
            llm_provider_id=created["id"],
            title="要保留的故事",
            status="active",
        )
        db.add(session)
        db.flush()
        session_id = session.id

    response = client.delete(
        f"/api/v1/providers/{created['id']}", headers=user["headers"]
    )
    assert response.status_code == 200

    with session_scope() as db:
        survivor = db.get(NarrativeSession, session_id)
        assert survivor is not None, "故事被误删了！"
        assert survivor.llm_provider_id is None, "外键应当被置为 NULL"


def test_delete_missing_provider(client: TestClient, user: dict) -> None:
    response = client.delete("/api/v1/providers/999999999", headers=user["headers"])
    assert response.status_code == 404


# ==================================================================
#  八、试连未保存的配置
# ==================================================================
def test_test_draft_does_not_persist(client: TestClient, user: dict) -> None:
    """★ 「先测再存」的接口不能往数据库写东西。

    使用一个必然失败的地址（不存在的域名），验证 400/502 语义化错误
    而不是 500，同时确认没有新配置被创建。
    """
    before = client.get("/api/v1/providers", headers=user["headers"]).json()["data"]

    response = client.post(
        "/api/v1/providers/test-draft",
        json=payload(
            name="未保存的配置",
            base_url="https://this-domain-does-not-exist-xyz.invalid/v1",
        ),
        headers=user["headers"],
    )
    # 连通失败会返回 200 + ok=False（测试连接本身成功了，只是结果是不通）
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ok"] is False

    after = client.get("/api/v1/providers", headers=user["headers"]).json()["data"]
    assert len(after) == len(before)
    assert all(item["name"] != "未保存的配置" for item in after)
