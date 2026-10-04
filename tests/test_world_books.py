"""3.6 世界书接口测试（集成测试，需要 MySQL）。

世界书是从「角色卡里的一个字段」提升出来的独立实体，所以这里要额外覆盖：
  · 条目（entries）的规范化与校验
  · 未知字段的原样保留（导出时不丢设置）
  · 被角色卡使用时的删除保护
  · 与角色卡之间的关联 / 解除关联（在 test_character_cards.py 里覆盖）

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_world_books.py -v
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import User, WorldBook
from app.db.mysql import session_scope
from app.main import app

BASE = "/api/v1/world-books"
CARDS = "/api/v1/character-cards"


# ==================================================================
#  夹具
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"wb_{token}",
        "email": f"wb_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    created = client.post("/api/v1/auth/register", json=account)
    assert created.status_code == 201, created.text

    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged_in.status_code == 200, logged_in.text

    return {
        "id": created.json()["data"]["id"],
        "username": account["username"],
        "headers": {
            "Authorization": f"Bearer {logged_in.json()['data']['access_token']}"
        },
    }


def _cleanup_user(username: str) -> None:
    with session_scope() as db:
        row = db.scalar(select(User).where(User.username == username))
        if row is not None:
            db.delete(row)


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
        "name": f"书_{uuid.uuid4().hex[:8]}",
        "description": "测试世界书",
        "entries": [
            {"keys": ["龙"], "content": "世上最后一条龙"},
            {"keys": ["森林", "树林"], "content": "禁忌森林", "enabled": False},
        ],
    }
    data.update(overrides)
    return data


def create(client: TestClient, user: dict, **overrides) -> dict:
    response = client.post(BASE, json=payload(**overrides), headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def make_card(client: TestClient, user: dict, book_id: int | None = None) -> dict:
    body = {"name": f"卡_{uuid.uuid4().hex[:8]}", "world_book_id": book_id}
    response = client.post(CARDS, json=body, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


# ==================================================================
#  一、认证要求
# ==================================================================
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", BASE, {}),
        ("post", BASE, {"json": {}}),
        ("get", f"{BASE}/1", {}),
        ("patch", f"{BASE}/1", {"json": {}}),
        ("delete", f"{BASE}/1", {}),
    ],
)
def test_endpoints_require_login(
    client: TestClient, method: str, path: str, kwargs: dict
) -> None:
    """★ 所有世界书接口都必须要求登录。"""
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


# ==================================================================
#  二、新建
# ==================================================================
def test_create_book(client: TestClient, user: dict) -> None:
    data = create(client, user)

    assert data["name"]
    assert data["display_name"] == data["name"]
    assert data["entry_count"] == 2
    assert data["enabled_entry_count"] == 1  # 有一条 enabled=False
    assert data["used_by_cards"] == []


def test_create_normalizes_entries(client: TestClient, user: dict) -> None:
    """★ 条目要规范化：补默认值、关键词去重去空白、保留未知字段。"""
    data = create(
        client,
        user,
        entries=[
            {
                "keys": ["  龙  ", "龙", "", "巨龙"],
                "content": "  一条龙  ",
                # 规范里我们不认识的字段，必须原样留下
                "position": "before_char",
                "priority": 7,
            }
        ],
    )

    entry = data["entries"][0]
    assert entry["keys"] == ["龙", "巨龙"]  # 去空白 + 去重
    assert entry["content"] == "一条龙"  # 去首尾空白
    assert entry["enabled"] is True  # 默认值
    assert entry["insertion_order"] == 0  # 默认值
    assert entry["extensions"] == {}  # 规范要求的默认值
    # 未知字段原样保留（否则导出回 SillyTavern 时这些设置就没了）
    assert entry["position"] == "before_char"
    assert entry["priority"] == 7


def test_create_tolerates_string_keys(client: TestClient, user: dict) -> None:
    """有些工具会把 keys 写成单个字符串，宽容地当成只含一个关键词。"""
    data = create(client, user, entries=[{"keys": "龙", "content": "设定"}])
    assert data["entries"][0]["keys"] == ["龙"]


def test_create_rejects_entry_without_content(client: TestClient, user: dict) -> None:
    """★ 缺 content 的条目要**报错**，而不是悄悄丢掉。

    静默丢弃会让用户以为「导入成功了」，实际少了一条设定 —— 极难发现。
    """
    response = client.post(
        BASE,
        json=payload(entries=[{"keys": ["龙"], "content": "  "}]),
        headers=user["headers"],
    )
    assert response.status_code == 422
    assert "content" in response.text


def test_create_rejects_too_many_entries(client: TestClient, user: dict) -> None:
    response = client.post(
        BASE,
        json=payload(
            entries=[{"keys": [f"k{i}"], "content": "x"} for i in range(501)]
        ),
        headers=user["headers"],
    )
    assert response.status_code == 422
    assert "最多" in response.text


def test_create_rejects_blank_name(client: TestClient, user: dict) -> None:
    response = client.post(
        BASE, json=payload(name="   "), headers=user["headers"]
    )
    assert response.status_code == 422


def test_create_empty_book_is_allowed(client: TestClient, user: dict) -> None:
    """允许先建一本空书，之后慢慢加条目。"""
    data = create(client, user, entries=[])
    assert data["entry_count"] == 0
    assert data["entries"] == []


# ==================================================================
#  三、列表与查询
# ==================================================================
def test_list_returns_brief_without_entries(client: TestClient, user: dict) -> None:
    """★ 列表不返回 entries 正文（一本世界书可能几百条，带全了很笨重）。"""
    create(client, user)

    body = client.get(BASE, headers=user["headers"]).json()["data"]

    assert set(body.keys()) >= {"items", "total", "limit", "offset", "has_more"}
    item = body["items"][0]
    assert "entries" not in item
    assert item["entry_count"] == 2


def test_list_works_with_a_large_world_book(client: TestClient, user: dict) -> None:
    """★ 回归：条目很大的世界书**不能**把列表接口打挂。

    真实事故（用户反馈）：导入一本大世界书之后「世界书」页直接 500 ——

        (1038, 'Out of sort memory, consider increasing server sort buffer size')

    根因不是数据量本身，而是当时列表查询用 `select(WorldBook)` 取整行，
    于是 **entries 这个几百 KB 的 JSON 列被塞进 MySQL 的排序缓冲**
    （本项目用的是默认的 256KB sort buffer），一溢出就报错。

    所以这个用例必须造**足够大**的数据才有意义：
    约 20 条 × 15000 字符 ≈ 900KB（实测这个量级才会触发 1038；
    小数据跑一万遍也测不出来 —— 这也是它当初能溜到用户手里的原因）。
    """
    big_text = "设定正文。" * 3000  # ≈ 15000 字符 ≈ 45KB
    entries = [
        {"keys": [f"关键词{i}"], "content": big_text, "enabled": i % 3 != 0}
        for i in range(20)
    ]
    create(client, user, entries=entries)

    listed = client.get(BASE, headers=user["headers"])
    assert listed.status_code == 200, listed.text

    item = listed.json()["data"]["items"][0]
    assert item["entry_count"] == 20
    # i%3!=0 → 1..19 里 13 条启用（i=0,3,6,9,12,15,18 是禁用的）
    assert item["enabled_entry_count"] == 13
    # 列表仍然不能带正文
    assert "entries" not in item

    # 搜索走同一条路径，也要能扛住
    assert client.get(BASE, params={"q": "世界"}, headers=user["headers"]).status_code == 200


def test_detail_still_returns_full_entries_for_a_large_book(
    client: TestClient, user: dict
) -> None:
    """反向断言：修列表的时候**不能**把详情页的全文弄丢。"""
    big_text = "设定正文。" * 3000
    created = create(
        client,
        user,
        entries=[{"keys": ["甲"], "content": big_text, "enabled": True}],
    )

    detail = client.get(f"{BASE}/{created['id']}", headers=user["headers"])
    assert detail.status_code == 200, detail.text
    entries = detail.json()["data"]["entries"]
    assert len(entries) == 1
    assert entries[0]["content"] == big_text, "详情必须原样返回全文"


def test_list_pagination(client: TestClient, user: dict) -> None:
    for _ in range(3):
        create(client, user)

    first = client.get(
        BASE, params={"limit": 2, "offset": 0}, headers=user["headers"]
    ).json()["data"]
    second = client.get(
        BASE, params={"limit": 2, "offset": 2}, headers=user["headers"]
    ).json()["data"]

    assert first["total"] == 3
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    assert len(second["items"]) == 1
    assert second["has_more"] is False


def test_list_search(client: TestClient, user: dict) -> None:
    keyword = uuid.uuid4().hex[:8]
    create(client, user, name=f"命中_{keyword}")
    create(client, user, name=f"不命中_{uuid.uuid4().hex[:8]}")

    body = client.get(
        BASE, params={"q": keyword}, headers=user["headers"]
    ).json()["data"]
    assert body["total"] == 1


def test_list_only_returns_own_books(
    client: TestClient, user: dict, other_user: dict
) -> None:
    mine = create(client, user)
    theirs = create(client, other_user)

    body = client.get(BASE, headers=user["headers"]).json()["data"]
    ids = [item["id"] for item in body["items"]]

    assert mine["id"] in ids
    assert theirs["id"] not in ids


def test_get_book_detail(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.get(f"{BASE}/{created['id']}", headers=user["headers"])

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data["entries"]) == 2
    assert data["used_by_cards"] == []


def test_detail_lists_cards_using_it(client: TestClient, user: dict) -> None:
    """★ 详情要告知「正在被哪些卡使用」，便于删除前确认影响范围。"""
    book = create(client, user)
    card = make_card(client, user, book["id"])

    data = client.get(f"{BASE}/{book['id']}", headers=user["headers"]).json()["data"]

    assert len(data["used_by_cards"]) == 1
    assert data["used_by_cards"][0]["id"] == card["id"]
    assert data["used_by_cards"][0]["name"] == card["name"]


# ==================================================================
#  四、越权防护
# ==================================================================
def test_cannot_read_others_book(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """★ 世界书是私有资产，别人的书一律 404（不存在 403 的分支）。"""
    theirs = create(client, other_user)
    response = client.get(f"{BASE}/{theirs['id']}", headers=user["headers"])
    assert response.status_code == 404


def test_cannot_modify_others_book(
    client: TestClient, user: dict, other_user: dict
) -> None:
    theirs = create(client, other_user)

    patched = client.patch(
        f"{BASE}/{theirs['id']}", json={"name": "被改了"}, headers=user["headers"]
    )
    assert patched.status_code == 404

    deleted = client.delete(f"{BASE}/{theirs['id']}", headers=user["headers"])
    assert deleted.status_code == 404

    # 原书没被动过
    still = client.get(f"{BASE}/{theirs['id']}", headers=other_user["headers"])
    assert still.json()["data"]["name"] == theirs["name"]


def test_get_missing_book(client: TestClient, user: dict) -> None:
    assert client.get(f"{BASE}/999999999", headers=user["headers"]).status_code == 404


# ==================================================================
#  五、更新
# ==================================================================
def test_update_name_only(client: TestClient, user: dict) -> None:
    created = create(client, user)

    data = client.patch(
        f"{BASE}/{created['id']}", json={"name": "新名字"}, headers=user["headers"]
    ).json()["data"]

    assert data["name"] == "新名字"
    # 没提交的字段保持原值
    assert data["entry_count"] == 2
    assert data["description"] == "测试世界书"


def test_update_description_to_null(client: TestClient, user: dict) -> None:
    created = create(client, user)
    data = client.patch(
        f"{BASE}/{created['id']}",
        json={"description": None},
        headers=user["headers"],
    ).json()["data"]
    assert data["description"] is None


def test_update_name_null_is_ignored(client: TestClient, user: dict) -> None:
    """name 是必填字段，传 null 视为「不改」而不是「清空」。"""
    created = create(client, user)
    data = client.patch(
        f"{BASE}/{created['id']}", json={"name": None}, headers=user["headers"]
    ).json()["data"]
    assert data["name"] == created["name"]


def test_update_entries_replaces_whole_array(client: TestClient, user: dict) -> None:
    """★ entries 是整体替换，不是按条目合并。"""
    created = create(client, user)
    assert created["entry_count"] == 2

    data = client.patch(
        f"{BASE}/{created['id']}",
        json={"entries": [{"keys": ["新的"], "content": "只剩这一条"}]},
        headers=user["headers"],
    ).json()["data"]

    assert data["entry_count"] == 1
    assert data["entries"][0]["content"] == "只剩这一条"


def test_update_entries_to_empty(client: TestClient, user: dict) -> None:
    created = create(client, user)
    data = client.patch(
        f"{BASE}/{created['id']}", json={"entries": []}, headers=user["headers"]
    ).json()["data"]
    assert data["entries"] == []
    assert data["entry_count"] == 0


def test_update_without_entries_keeps_them(client: TestClient, user: dict) -> None:
    """空请求体不该动到条目。"""
    created = create(client, user)
    data = client.patch(
        f"{BASE}/{created['id']}", json={}, headers=user["headers"]
    ).json()["data"]
    assert data["entry_count"] == 2


def test_update_entries_rejects_invalid(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.patch(
        f"{BASE}/{created['id']}",
        json={"entries": [{"keys": ["x"], "content": ""}]},
        headers=user["headers"],
    )
    assert response.status_code == 422


# ==================================================================
#  六、删除保护
# ==================================================================
def test_delete_unused_book(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.delete(f"{BASE}/{created['id']}", headers=user["headers"])
    assert response.status_code == 200
    assert client.get(f"{BASE}/{created['id']}", headers=user["headers"]).status_code == 404


def test_delete_book_in_use_is_rejected(client: TestClient, user: dict) -> None:
    """★ 还有角色卡在用这本书时，默认拒绝删除，并列出是哪几张卡。"""
    book = create(client, user)
    card = make_card(client, user, book["id"])

    response = client.delete(f"{BASE}/{book['id']}", headers=user["headers"])
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"

    detail = response.json()["detail"]
    assert detail["card_count"] == 1
    assert detail["cards"][0]["id"] == card["id"]
    assert detail["cards"][0]["name"] == card["name"]

    # 书还在
    assert client.get(f"{BASE}/{book['id']}", headers=user["headers"]).status_code == 200


def test_delete_book_force(client: TestClient, user: dict) -> None:
    """★ 加 force=true 才真删；卡片本身不受影响，只是关联被解除。"""
    book = create(client, user)
    card = make_card(client, user, book["id"])

    response = client.delete(
        f"{BASE}/{book['id']}", params={"force": True}, headers=user["headers"]
    )
    assert response.status_code == 200

    assert client.get(f"{BASE}/{book['id']}", headers=user["headers"]).status_code == 404

    # 卡片还在，世界书关联变成 null
    survivor = client.get(f"{CARDS}/{card['id']}", headers=user["headers"])
    assert survivor.status_code == 200
    assert survivor.json()["data"]["world_book"] is None


def test_delete_missing_book(client: TestClient, user: dict) -> None:
    assert client.delete(f"{BASE}/999999999", headers=user["headers"]).status_code == 404


# ==================================================================
#  七、无名世界书（来自导入）与 display_name
# ==================================================================
def test_unnamed_book_gets_display_name(client: TestClient, user: dict) -> None:
    """★ 规范里世界书的名字是可选的，大量真实的卡不写名字。

    数据库忠实存 null（这样导出时能还原成「没有名字」，
    保证往返一致），但界面需要有个说法，所以给一个 display_name 兜底。
    """
    response = client.post(
        f"{CARDS}/import",
        json={
            "card": {
                "spec": "chara_card_v2",
                "spec_version": "2.0",
                "data": {
                    "name": "无名世界书的卡",
                    # 注意：character_book 里**没有** name 字段
                    "character_book": {
                        "entries": [{"keys": ["龙"], "content": "设定"}]
                    },
                },
            }
        },
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    book_id = response.json()["data"]["world_book"]["id"]

    book = client.get(f"{BASE}/{book_id}", headers=user["headers"]).json()["data"]
    assert book["name"] is None, "存储值必须是 null，不能被兜底文案污染"
    assert book["display_name"] == "未命名世界书（1 条）"

    # 导出时要还原成「没有 name 键」，与原始数据一致
    exported = client.get(
        f"{CARDS}/{response.json()['data']['id']}/export", headers=user["headers"]
    ).json()["data"]["data"]
    assert "name" not in exported["character_book"]

    # 用户也可以后来给它起个名字
    renamed = client.patch(
        f"{BASE}/{book_id}", json={"name": "后来起的名字"}, headers=user["headers"]
    ).json()["data"]
    assert renamed["name"] == "后来起的名字"
    assert renamed["display_name"] == "后来起的名字"


def test_import_preserves_book_level_settings(client: TestClient, user: dict) -> None:
    """★ 规范里世界书可以带扫描深度、token 预算等设置。

    这些参数本项目暂时不实现（属于后续的关键词触发检索功能），
    但必须**原样存下来**，否则用户把卡导回 SillyTavern 时设置就没了。
    """
    book_in = {
        "name": "带设置的世界书",
        "description": "说明",
        "scan_depth": 4,
        "token_budget": 1200,
        "recursive_scanning": True,
        "extensions": {"some_plugin": {"enabled": True}},
        "entries": [{"keys": ["龙"], "content": "设定"}],
    }
    response = client.post(
        f"{CARDS}/import",
        json={"card": {"name": "带世界书的卡", "character_book": book_in}},
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    card = response.json()["data"]

    exported = client.get(
        f"{CARDS}/{card['id']}/export", headers=user["headers"]
    ).json()["data"]["data"]

    book_out = exported["character_book"]

    # 书级设置必须一字不差地保留下来
    assert book_out["name"] == "带设置的世界书"
    assert book_out["description"] == "说明"
    assert book_out["scan_depth"] == 4
    assert book_out["token_budget"] == 1200
    assert book_out["recursive_scanning"] is True
    assert book_out["extensions"] == {"some_plugin": {"enabled": True}}

    # 条目**会**被规范化（补上规范要求的默认字段），这是预期行为；
    # 但原有的 keys / content 不能变
    assert len(book_out["entries"]) == 1
    assert book_out["entries"][0]["keys"] == ["龙"]
    assert book_out["entries"][0]["content"] == "设定"
    assert book_out["entries"][0]["extensions"] == {}


# ==================================================================
#  十一、★ 关键词触发检索的参数（3.9）
# ==================================================================
def test_scan_settings_can_be_set_via_api(client: TestClient, user: dict) -> None:
    """★ 3.9 之前 scan_depth / token_budget 只能从角色卡带进来，界面改不了 ——
    于是关键词触发检索做出来也没法调。这里守的是"接口能管这两个参数"。
    """
    created = client.post(
        BASE,
        json={
            "name": f"可调参数_{uuid.uuid4().hex[:6]}",
            "entries": [{"keys": ["龙"], "content": "设定"}],
            "scan_depth": 3,
            "token_budget": 256,
        },
        headers=user["headers"],
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["scan_depth"] == 3
    assert data["token_budget"] == 256

    # ★ 双通道核对：真的写进了 extra_data（导出回 SillyTavern 时不能丢）
    with session_scope() as db:
        row = db.get(WorldBook, data["id"])
        assert row.extra_data.get("scan_depth") == 3
        assert row.extra_data.get("token_budget") == 256

    # 没提交的字段保持原值，README 里说的 PATCH 语义照旧
    patched = client.patch(
        f"{BASE}/{data['id']}", json={"scan_depth": 9}, headers=user["headers"]
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["scan_depth"] == 9
    assert patched.json()["data"]["token_budget"] == 256


def test_scan_settings_default_when_not_set(client: TestClient, user: dict) -> None:
    """没设置时返回默认值（界面才能显示"当前生效的是 8 / 1024"）。"""
    created = client.post(
        BASE, json={"name": f"默认参数_{uuid.uuid4().hex[:6]}", "entries": []},
        headers=user["headers"],
    ).json()["data"]
    assert created["scan_depth"] == 8
    assert created["token_budget"] == 1024


def test_scan_settings_reject_illegal_values(client: TestClient, user: dict) -> None:
    """scan_depth 为 0 / 负数没有含义，要在参数校验阶段就拦下（422）。"""
    for bad in ({"scan_depth": 0}, {"token_budget": -1}):
        response = client.post(
            BASE,
            json={"name": "非法参数", "entries": [], **bad},
            headers=user["headers"],
        )
        assert response.status_code == 422, f"{bad} 应当被拒绝：{response.text}"
