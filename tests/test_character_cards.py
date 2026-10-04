"""3.6 角色卡接口测试（集成测试，需要 MySQL）。

覆盖：增删改查、PATCH 的「未提交 / 提交 null」语义、标签规范化、
      分页与搜索（含 LIKE 通配符转义）、标签 JSON 筛选、
      公开卡库的可见性规则（能看不能改）、删除的会话占用保护、
      复制、以及 Character Card V2 规范的导入导出往返无损。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_character_cards.py -v
"""

from __future__ import annotations

import base64
import json
import struct
import uuid
import zlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import CharacterCard, NarrativeSession, User, WorldBook
from app.db.mysql import session_scope
from app.main import app

BASE = "/api/v1/character-cards"


# ==================================================================
#  夹具
# ==================================================================
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_user(client: TestClient) -> dict:
    """注册并登录一个随机用户。"""
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"cc_{token}",
        "email": f"cc_{token}@example.com",
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
    """删掉测试用户；其名下角色卡与会话由外键级联清除。"""
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
        "name": f"卡_{uuid.uuid4().hex[:8]}",
        "description": "一个用来测试的角色",
        "personality": "冷静、话少",
        "greeting": "（她抬起头看了你一眼）……你来了。",
        "tags": ["奇幻", "侦探"],
    }
    data.update(overrides)
    return data


def create(client: TestClient, user: dict, **overrides) -> dict:
    response = client.post(BASE, json=payload(**overrides), headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _make_session(user_id: int, card_id: int) -> int:
    """直接往数据库插一个叙事会话（3.8 的接口还没做，这里手动造数据）。

    用于验证「删除角色卡时的会话占用保护」。
    """
    with session_scope() as db:
        session = NarrativeSession(
            user_id=user_id,
            character_card_id=card_id,
            title="测试会话",
            status="active",
        )
        db.add(session)
        db.flush()
        return session.id


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
        ("post", f"{BASE}/1/duplicate", {}),
        ("get", f"{BASE}/1/export", {}),
        ("post", f"{BASE}/import", {"json": {"card": {}}}),
    ],
)
def test_endpoints_require_login(
    client: TestClient, method: str, path: str, kwargs: dict
) -> None:
    """★ 所有角色卡接口都必须要求登录。"""
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


# ==================================================================
#  二、新建
# ==================================================================
def test_create_card(client: TestClient, user: dict) -> None:
    data = create(client, user)

    assert data["name"]
    assert data["is_public"] is False
    assert data["is_owner"] is True
    assert data["session_count"] == 0
    assert data["greeting"]
    assert data["tags"] == ["奇幻", "侦探"]


def test_create_normalizes_tags(client: TestClient, user: dict) -> None:
    """★ 标签要去空白、丢空项、去重，避免脏数据进库。"""
    data = create(
        client,
        user,
        tags=["  奇幻  ", "奇幻", "", "   ", "侦探"],
    )
    assert data["tags"] == ["奇幻", "侦探"]


def test_create_cleans_blank_text_to_null(client: TestClient, user: dict) -> None:
    """只填了空白的文本字段应当收敛成 null，而不是存一个空字符串。"""
    data = create(client, user, description="   ", personality="")
    assert data["description"] is None
    assert data["personality"] is None


def test_create_rejects_blank_name(client: TestClient, user: dict) -> None:
    response = client.post(
        BASE, json=payload(name="   "), headers=user["headers"]
    )
    assert response.status_code == 422


def test_create_rejects_too_many_tags(client: TestClient, user: dict) -> None:
    """★ 超限时报错而不是静默截断 —— 静默截断会让用户以为内容存进去了。"""
    response = client.post(
        BASE,
        json=payload(tags=[f"标签{i}" for i in range(21)]),
        headers=user["headers"],
    )
    assert response.status_code == 422
    assert "最多" in response.text


def test_create_allows_duplicate_names(client: TestClient, user: dict) -> None:
    """★ 角色卡允许重名（与模型配置的别名规则不同）。

    同名不同内容是很常见的（「小明·校园篇」和「小明·侦探篇」），
    强行要求唯一会逼用户改掉本来合适的名字。
    """
    name = f"同名_{uuid.uuid4().hex[:8]}"
    first = create(client, user, name=name)
    second = create(client, user, name=name)
    assert first["id"] != second["id"]
    assert first["name"] == second["name"] == name


# ==================================================================
#  三、列表 / 分页 / 搜索 / 筛选
# ==================================================================
def test_list_returns_brief_shape(client: TestClient, user: dict) -> None:
    """★ 列表必须是精简结构。

    角色卡里有若干超大文本字段，列表全带上会让响应体轻易上兆。
    这里断言「长文本字段不出现在列表里」，把这个性能决定钉死。
    """
    create(client, user, personality="很长的性格描写" * 100)

    response = client.get(BASE, headers=user["headers"])
    assert response.status_code == 200
    body = response.json()["data"]

    assert set(body.keys()) >= {"items", "total", "limit", "offset", "has_more"}
    item = body["items"][0]

    for heavy in ("personality", "background", "example_dialogue", "system_prompt"):
        assert heavy not in item, f"列表项不该包含长文本字段 {heavy}"

    # 但应当给出「有没有开场白」和摘要，供列表展示
    assert item["has_greeting"] is True
    assert item["greeting_preview"]


def test_list_pagination(client: TestClient, user: dict) -> None:
    for i in range(3):
        create(client, user, name=f"分页_{uuid.uuid4().hex[:6]}_{i}")

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
    # ★ 最后一页不满时 has_more 必须为 False
    assert second["has_more"] is False

    # 两页之间不能有重复（排序必须确定，否则分页会错乱）
    ids = [i["id"] for i in first["items"]] + [i["id"] for i in second["items"]]
    assert len(ids) == len(set(ids)) == 3


def test_list_search_by_name(client: TestClient, user: dict) -> None:
    keyword = uuid.uuid4().hex[:8]
    create(client, user, name=f"命中_{keyword}")
    create(client, user, name=f"不命中_{uuid.uuid4().hex[:8]}")

    body = client.get(
        BASE, params={"q": keyword}, headers=user["headers"]
    ).json()["data"]

    assert body["total"] == 1
    assert keyword in body["items"][0]["name"]


def test_list_search_escapes_like_wildcards(client: TestClient, user: dict) -> None:
    """★ LIKE 通配符必须转义。

    用户搜索 "50%" 时，那个百分号是**字面意思**。
    如果不转义，`LIKE '%50%%'` 会退化成 `LIKE '%50%'`，
    把所有含 "50" 的记录都搜出来。
    """
    token = uuid.uuid4().hex[:6]
    create(client, user, name=f"打折{token}50%off")
    create(client, user, name=f"编号{token}5030")

    body = client.get(
        BASE, params={"q": f"{token}50%"}, headers=user["headers"]
    ).json()["data"]

    assert body["total"] == 1
    assert "50%off" in body["items"][0]["name"]


def test_list_filter_by_tag(client: TestClient, user: dict) -> None:
    """★ 标签存在 JSON 列里，筛选走 MySQL 的 JSON_CONTAINS。"""
    tag = f"独特标签{uuid.uuid4().hex[:6]}"
    create(client, user, tags=[tag, "其它"])
    create(client, user, tags=["别的"])

    body = client.get(
        BASE, params={"tag": tag}, headers=user["headers"]
    ).json()["data"]

    assert body["total"] == 1
    assert tag in body["items"][0]["tags"]


def test_list_filter_by_tag_is_exact_not_substring(
    client: TestClient, user: dict
) -> None:
    """标签筛选是精确匹配，不能被前缀误命中。"""
    base_tag = f"标签{uuid.uuid4().hex[:6]}"
    create(client, user, tags=[base_tag])
    create(client, user, tags=[base_tag + "扩展"])

    body = client.get(
        BASE, params={"tag": base_tag}, headers=user["headers"]
    ).json()["data"]
    assert body["total"] == 1


def test_list_sort_by_name(client: TestClient, user: dict) -> None:
    token = uuid.uuid4().hex[:6]
    create(client, user, name=f"zzz_{token}")
    create(client, user, name=f"aaa_{token}")

    body = client.get(
        BASE, params={"sort": "name", "q": token}, headers=user["headers"]
    ).json()["data"]
    names = [item["name"] for item in body["items"]]
    assert names == sorted(names)


def test_list_rejects_invalid_scope(client: TestClient, user: dict) -> None:
    response = client.get(
        BASE, params={"scope": "everything"}, headers=user["headers"]
    )
    assert response.status_code == 422


# ==================================================================
#  四、可见性（公开卡库的核心规则）
# ==================================================================
def test_list_scope_mine_excludes_others(client: TestClient, user: dict, other_user: dict) -> None:
    mine = create(client, user, is_public=True)
    theirs = create(client, other_user, is_public=True)

    body = client.get(
        BASE, params={"scope": "mine"}, headers=user["headers"]
    ).json()["data"]
    ids = [item["id"] for item in body["items"]]

    assert mine["id"] in ids
    assert theirs["id"] not in ids


def test_list_scope_public_shows_only_others_public(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """★ scope=public 是「公共卡库」，只列别人公开的卡，不含自己的。"""
    my_public = create(client, user, is_public=True)
    my_private = create(client, user, is_public=False)
    their_public = create(client, other_user, is_public=True)
    their_private = create(client, other_user, is_public=False)

    body = client.get(
        BASE, params={"scope": "public"}, headers=user["headers"]
    ).json()["data"]
    ids = {item["id"] for item in body["items"]}

    assert their_public["id"] in ids
    assert their_private["id"] not in ids  # 别人的私有卡看不到
    assert my_public["id"] not in ids      # 公共卡库不含自己的
    assert my_private["id"] not in ids

    # 别人的卡要标记 is_owner=False，前端据此隐藏「编辑」按钮
    target = next(i for i in body["items"] if i["id"] == their_public["id"])
    assert target["is_owner"] is False


def test_list_scope_all_is_union(client: TestClient, user: dict, other_user: dict) -> None:
    my_private = create(client, user, is_public=False)
    their_public = create(client, other_user, is_public=True)
    their_private = create(client, other_user, is_public=False)

    body = client.get(
        BASE, params={"scope": "all"}, headers=user["headers"]
    ).json()["data"]
    ids = {item["id"] for item in body["items"]}

    assert my_private["id"] in ids
    assert their_public["id"] in ids
    assert their_private["id"] not in ids


# ==================================================================
#  五、越权防护
# ==================================================================
def test_owner_can_read_own_private_card(client: TestClient, user: dict) -> None:
    created = create(client, user, is_public=False)
    response = client.get(f"{BASE}/{created['id']}", headers=user["headers"])
    assert response.status_code == 200


def test_cannot_read_others_private_card(client: TestClient, user: dict, other_user: dict) -> None:
    """★ 别人的私有卡：404，不泄露它是否存在。"""
    theirs = create(client, other_user, is_public=False)

    response = client.get(f"{BASE}/{theirs['id']}", headers=user["headers"])
    assert response.status_code == 404


def test_can_read_others_public_card(client: TestClient, user: dict, other_user: dict) -> None:
    """★ 别人的公开卡可以看全文，但 is_owner 必须是 False。"""
    theirs = create(client, other_user, is_public=True, greeting="你好呀")

    response = client.get(f"{BASE}/{theirs['id']}", headers=user["headers"])
    assert response.status_code == 200

    data = response.json()["data"]
    assert data["greeting"] == "你好呀"
    assert data["is_owner"] is False


def test_cannot_modify_others_public_card(client: TestClient, user: dict, other_user: dict) -> None:
    """★ 公开卡「能看不等于能改」。

    这里刻意返回 403 而不是 404：调用方刚刚还能读到这张卡，
    回 404 会让人以为卡被删了，反而造成困惑。
    403 也不泄露任何新信息 —— 卡本来就是公开的。
    """
    theirs = create(client, other_user, is_public=True)

    patched = client.patch(
        f"{BASE}/{theirs['id']}", json={"name": "被改了"}, headers=user["headers"]
    )
    assert patched.status_code == 403
    assert patched.json()["code"] == "FORBIDDEN"
    # 提示里要给出可行出路
    assert "复制" in patched.json()["message"] + str(patched.json().get("detail"))

    deleted = client.delete(f"{BASE}/{theirs['id']}", headers=user["headers"])
    assert deleted.status_code == 403

    # 确认原卡没被动过
    still = client.get(f"{BASE}/{theirs['id']}", headers=other_user["headers"])
    assert still.json()["data"]["name"] == theirs["name"]


def test_cannot_modify_others_private_card(client: TestClient, user: dict, other_user: dict) -> None:
    """★ 私有卡连「不能改」都要装作不存在（404 而不是 403）。"""
    theirs = create(client, other_user, is_public=False)

    patched = client.patch(
        f"{BASE}/{theirs['id']}", json={"name": "被改了"}, headers=user["headers"]
    )
    assert patched.status_code == 404

    deleted = client.delete(f"{BASE}/{theirs['id']}", headers=user["headers"])
    assert deleted.status_code == 404


def test_get_missing_card(client: TestClient, user: dict) -> None:
    response = client.get(f"{BASE}/999999999", headers=user["headers"])
    assert response.status_code == 404


# ==================================================================
#  六、PATCH 语义
# ==================================================================
def test_patch_updates_only_submitted_fields(client: TestClient, user: dict) -> None:
    created = create(client, user, personality="原性格", scenario="原场景")

    response = client.patch(
        f"{BASE}/{created['id']}",
        json={"personality": "新性格"},
        headers=user["headers"],
    )
    data = response.json()["data"]

    assert data["personality"] == "新性格"
    # 没提交的字段必须原样保留
    assert data["scenario"] == "原场景"


def test_patch_null_clears_field(client: TestClient, user: dict) -> None:
    """★ PATCH 的关键能力：显式传 null 表示「清空该字段」。

    这正是需要 model_fields_set 的原因 —— 「传了 null」和「没传这个字段」
    在 Python 里长得一样，必须靠「字段是否出现在请求里」来区分。
    """
    created = create(client, user, description="待清空的简介")
    assert created["description"] == "待清空的简介"

    response = client.patch(
        f"{BASE}/{created['id']}",
        json={"description": None},
        headers=user["headers"],
    )
    assert response.status_code == 200
    assert response.json()["data"]["description"] is None


def test_patch_absent_field_keeps_value(client: TestClient, user: dict) -> None:
    """空请求体 {} 不应该改动任何东西。"""
    created = create(client, user, greeting="原始开场白", tags=["甲"])

    response = client.patch(
        f"{BASE}/{created['id']}", json={}, headers=user["headers"]
    )
    data = response.json()["data"]

    assert data["greeting"] == "原始开场白"
    assert data["tags"] == ["甲"]
    assert data["name"] == created["name"]


def test_patch_tags_null_clears_to_empty_list(client: TestClient, user: dict) -> None:
    created = create(client, user, tags=["甲", "乙"])

    response = client.patch(
        f"{BASE}/{created['id']}", json={"tags": None}, headers=user["headers"]
    )
    assert response.json()["data"]["tags"] == []


def test_patch_can_publish_and_unpublish(client: TestClient, user: dict) -> None:
    created = create(client, user, is_public=False)

    published = client.patch(
        f"{BASE}/{created['id']}", json={"is_public": True}, headers=user["headers"]
    )
    assert published.json()["data"]["is_public"] is True

    unpublished = client.patch(
        f"{BASE}/{created['id']}", json={"is_public": False}, headers=user["headers"]
    )
    assert unpublished.json()["data"]["is_public"] is False


def test_patch_alternate_greetings(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.patch(
        f"{BASE}/{created['id']}",
        json={"alternate_greetings": ["开头一", "  开头二  "]},
        headers=user["headers"],
    )
    assert response.json()["data"]["alternate_greetings"] == ["开头一", "开头二"]


# ==================================================================
#  七、删除
# ==================================================================
def test_delete_card(client: TestClient, user: dict) -> None:
    created = create(client, user)

    response = client.delete(f"{BASE}/{created['id']}", headers=user["headers"])
    assert response.status_code == 200

    gone = client.get(f"{BASE}/{created['id']}", headers=user["headers"])
    assert gone.status_code == 404


def test_delete_card_in_use_is_rejected(client: TestClient, user: dict) -> None:
    """★ 还有会话在用这张卡时，默认拒绝删除。

    因为删除角色卡会**级联删掉用它开的故事**，
    这是不可逆的破坏性操作，宁可多问一次。
    """
    created = create(client, user)
    _make_session(user["id"], created["id"])

    response = client.delete(f"{BASE}/{created['id']}", headers=user["headers"])
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"

    detail = response.json()["detail"]
    assert detail["session_count"] == 1

    # 卡还在
    assert client.get(f"{BASE}/{created['id']}", headers=user["headers"]).status_code == 200

    # 详情接口应当把占用数量告诉前端，便于提前弹窗提醒
    info = client.get(f"{BASE}/{created['id']}", headers=user["headers"]).json()["data"]
    assert info["session_count"] == 1


def test_delete_card_force_cascades_sessions(client: TestClient, user: dict) -> None:
    """★ force=true 时连同会话一起删除（数据库外键级联）。"""
    created = create(client, user)
    session_id = _make_session(user["id"], created["id"])

    response = client.delete(
        f"{BASE}/{created['id']}", params={"force": True}, headers=user["headers"]
    )
    assert response.status_code == 200

    with session_scope() as db:
        assert db.get(NarrativeSession, session_id) is None
        assert db.get(CharacterCard, created["id"]) is None


def test_delete_missing_card(client: TestClient, user: dict) -> None:
    response = client.delete(f"{BASE}/999999999", headers=user["headers"])
    assert response.status_code == 404


# ==================================================================
#  八、复制
# ==================================================================
def test_duplicate_own_card(client: TestClient, user: dict) -> None:
    created = create(client, user, personality="原性格", greeting="原开场白")

    response = client.post(
        f"{BASE}/{created['id']}/duplicate", headers=user["headers"]
    )
    assert response.status_code == 201
    copy = response.json()["data"]

    assert copy["id"] != created["id"]
    assert copy["personality"] == "原性格"
    assert copy["greeting"] == "原开场白"
    assert "副本" in copy["name"]
    assert copy["is_owner"] is True


def test_duplicate_public_card_becomes_private(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """★ 复制别人的公开卡后，新卡默认私有。

    否则用户随手复制一下就往公共卡库里又推了一份重复内容。
    """
    theirs = create(client, other_user, is_public=True, personality="别人的性格")

    response = client.post(
        f"{BASE}/{theirs['id']}/duplicate", headers=user["headers"]
    )
    assert response.status_code == 201
    copy = response.json()["data"]

    assert copy["personality"] == "别人的性格"
    assert copy["is_public"] is False
    assert copy["user_id"] == user["id"]

    # 复制来的卡是自己的，可以随便改
    patched = client.patch(
        f"{BASE}/{copy['id']}", json={"name": "我改过的"}, headers=user["headers"]
    )
    assert patched.status_code == 200


def test_duplicate_with_custom_name(client: TestClient, user: dict) -> None:
    created = create(client, user)
    response = client.post(
        f"{BASE}/{created['id']}/duplicate",
        params={"name": "我的新卡"},
        headers=user["headers"],
    )
    assert response.json()["data"]["name"] == "我的新卡"


def test_cannot_duplicate_others_private_card(
    client: TestClient, user: dict, other_user: dict
) -> None:
    theirs = create(client, other_user, is_public=False)
    response = client.post(
        f"{BASE}/{theirs['id']}/duplicate", headers=user["headers"]
    )
    assert response.status_code == 404


# ==================================================================
#  九、导出（Character Card V2）
# ==================================================================
def test_export_v2_structure(client: TestClient, user: dict) -> None:
    created = create(
        client,
        user,
        name="爱丽丝",
        description="一位旅人",
        personality="好奇",
        scenario="酒馆",
        greeting="你好，旅人。",
        example_dialogue="<START>\n{{user}}: 你好\n{{char}}: 你好呀",
        tags=["奇幻"],
        alternate_greetings=["另一个开头"],
    )

    response = client.get(f"{BASE}/{created['id']}/export", headers=user["headers"])
    assert response.status_code == 200

    body = response.json()["data"]
    assert body["spec"] == "chara_card_v2"
    assert body["spec_version"] == "2.0"

    data = body["data"]
    # 规范字段名映射必须正确：greeting -> first_mes，example_dialogue -> mes_example
    assert data["name"] == "爱丽丝"
    assert data["first_mes"] == "你好，旅人。"
    assert data["personality"] == "好奇"
    assert data["scenario"] == "酒馆"
    assert data["tags"] == ["奇幻"]
    assert data["alternate_greetings"] == ["另一个开头"]
    assert "mes_example" in data
    # 规范要求这些字段存在且是字符串（不能是 null）
    for key in ("creator_notes", "system_prompt", "post_history_instructions",
                "creator", "character_version"):
        assert isinstance(data[key], str), key
    assert isinstance(data["extensions"], dict)


def test_export_puts_own_fields_in_extensions_namespace(
    client: TestClient, user: dict
) -> None:
    """★ 本项目自有的 background / speaking_style 规范里没有，
    按规范建议放进 extensions 的命名空间，避免与别人的扩展键冲突。"""
    created = create(client, user, background="出身寒门", speaking_style="简短")

    data = client.get(
        f"{BASE}/{created['id']}/export", headers=user["headers"]
    ).json()["data"]["data"]

    hne = data["extensions"]["hne"]
    assert hne["background"] == "出身寒门"
    assert hne["speaking_style"] == "简短"


def test_export_missing_text_fields_are_empty_strings(
    client: TestClient, user: dict
) -> None:
    """★ 缺省字段导出 "" 而不是 null：规范定义它们是 string，
    给 null 有些严格的导入器会直接报错。"""
    created = create(client, user, personality=None, scenario=None, greeting=None)

    data = client.get(
        f"{BASE}/{created['id']}/export", headers=user["headers"]
    ).json()["data"]["data"]

    assert data["personality"] == ""
    assert data["scenario"] == ""
    assert data["first_mes"] == ""


# ==================================================================
#  十、导入（含往返无损）
# ==================================================================
V2_SAMPLE = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "来自酒馆的卡",
        "description": "描述",
        "personality": "性格",
        "scenario": "场景",
        "first_mes": "第一句话",
        "mes_example": "示例对话",
        "creator_notes": "作者的话",
        "system_prompt": "自定义系统提示词",
        "post_history_instructions": "尾注",
        "alternate_greetings": ["备选一"],
        "tags": ["标签A", "标签B"],
        "creator": "某位作者",
        "character_version": "1.2",
        "extensions": {"some_other_plugin": {"voice": "soft"}},
        # 本项目没有对应列，必须原样保留
        "character_book": {
            "name": "角色世界书",
            "entries": [{"keys": ["龙"], "content": "世上最后一条龙", "enabled": True,
                         "insertion_order": 0, "extensions": {}}],
        },
    },
}


def test_import_v2_card(client: TestClient, user: dict) -> None:
    response = client.post(
        f"{BASE}/import",
        json={"card": V2_SAMPLE},
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]

    assert data["name"] == "来自酒馆的卡"
    assert data["greeting"] == "第一句话"
    assert data["example_dialogue"] == "示例对话"
    assert data["scenario"] == "场景"
    assert data["alternate_greetings"] == ["备选一"]
    assert data["tags"] == ["标签A", "标签B"]
    assert data["system_prompt"] == "自定义系统提示词"
    assert data["post_history_instructions"] == "尾注"
    assert data["is_public"] is False


def test_import_preserves_unknown_fields_on_export(
    client: TestClient, user: dict
) -> None:
    """★ 规范硬性要求：导入导出不得丢弃无法识别的字段。

    character_book（世界书）、creator_notes、creator、
    以及别的插件写在 extensions 里的内容，都必须能完整地存下来、再原样导出。

    ★ 唯一的例外：规范规定 character_book.extensions **必须存在**（缺省为 {}），
      而样例数据里没写这一项，所以导出时会按规范补上空对象。
      补字段不会丢数据，属于规范要求的规范化。
    """
    imported = client.post(
        f"{BASE}/import", json={"card": V2_SAMPLE}, headers=user["headers"]
    ).json()["data"]

    exported = client.get(
        f"{BASE}/{imported['id']}/export", headers=user["headers"]
    ).json()["data"]["data"]

    # 原来的字段 + 规范要求的默认 extensions
    expected_book = {"extensions": {}, **V2_SAMPLE["data"]["character_book"]}
    assert exported["character_book"] == expected_book
    assert exported["creator_notes"] == "作者的话"
    assert exported["creator"] == "某位作者"
    assert exported["character_version"] == "1.2"
    # 别人插件写在 extensions 里的内容也不能丢
    assert exported["extensions"]["some_other_plugin"] == {"voice": "soft"}


def test_export_import_roundtrip_is_lossless(client: TestClient, user: dict) -> None:
    """★ 往返无损：导出 -> 导入 -> 再导出，两次结果必须完全一致。

    这是判断「格式转换有没有丢东西」最有力的一个测试。
    """
    source = create(
        client,
        user,
        name="往返测试卡",
        description="简介",
        personality="性格",
        background="背景",
        speaking_style="风格",
        scenario="场景",
        greeting="开场白",
        example_dialogue="示例",
        alternate_greetings=["备选一", "备选二"],
        system_prompt="系统提示词",
        post_history_instructions="尾注",
        tags=["甲", "乙"],
    )

    first_export = client.get(
        f"{BASE}/{source['id']}/export", headers=user["headers"]
    ).json()["data"]

    reimported = client.post(
        f"{BASE}/import", json={"card": first_export}, headers=user["headers"]
    ).json()["data"]

    second_export = client.get(
        f"{BASE}/{reimported['id']}/export", headers=user["headers"]
    ).json()["data"]

    assert first_export == second_export

    # 顺带确认自有字段真的活过了这一趟
    assert reimported["background"] == "背景"
    assert reimported["speaking_style"] == "风格"
    assert reimported["alternate_greetings"] == ["备选一", "备选二"]


def test_import_v1_flat_format(client: TestClient, user: dict) -> None:
    """V1 是扁平结构，没有 spec / data 包裹。"""
    v1 = {
        "name": "V1 老卡",
        "description": "旧格式",
        "personality": "稳重",
        "scenario": "古代",
        "first_mes": "老夫在此。",
        "mes_example": "",
    }
    response = client.post(
        f"{BASE}/import", json={"card": v1}, headers=user["headers"]
    )
    assert response.status_code == 201, response.text

    data = response.json()["data"]
    assert data["name"] == "V1 老卡"
    assert data["greeting"] == "老夫在此。"


def test_import_with_name_override(client: TestClient, user: dict) -> None:
    response = client.post(
        f"{BASE}/import",
        json={"card": V2_SAMPLE, "name_override": "改个名字"},
        headers=user["headers"],
    )
    assert response.json()["data"]["name"] == "改个名字"


def test_import_can_publish_directly(client: TestClient, user: dict) -> None:
    response = client.post(
        f"{BASE}/import",
        json={"card": V2_SAMPLE, "is_public": True},
        headers=user["headers"],
    )
    assert response.json()["data"]["is_public"] is True


def test_import_missing_name_returns_400(client: TestClient, user: dict) -> None:
    """★ 缺 name 要返回 400 + 可读信息，而不是 500。"""
    response = client.post(
        f"{BASE}/import",
        json={"card": {"data": {"description": "没有名字"}}},
        headers=user["headers"],
    )
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_REQUEST"


def test_import_rejects_non_object_card(client: TestClient, user: dict) -> None:
    response = client.post(
        f"{BASE}/import",
        json={"card": {"name": ["不是字符串"]}},
        headers=user["headers"],
    )
    assert response.status_code == 400


def test_import_tolerates_bad_tag_types(client: TestClient, user: dict) -> None:
    """★ 收集来的卡质量参差不齐，非字符串的标签项应当被丢掉而不是报错。

    宁可少导入一个标签，也不要把 "[object Object]" 这种垃圾存进数据库。
    """
    card = {
        "name": "脏数据卡",
        "tags": ["正常标签", 123, None, {"bad": True}, "另一个正常标签"],
        "alternate_greetings": ["正常", 42],
    }
    response = client.post(
        f"{BASE}/import", json={"card": card}, headers=user["headers"]
    )
    assert response.status_code == 201, response.text

    data = response.json()["data"]
    assert data["tags"] == ["正常标签", "另一个正常标签"]
    assert data["alternate_greetings"] == ["正常"]


def test_import_duplicate_of_public_card_is_allowed(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """导入不校验重名（角色卡允许同名）。"""
    theirs = create(client, other_user, is_public=True, name="热门的卡")

    response = client.post(
        f"{BASE}/import",
        json={"card": {"name": "热门的卡"}},
        headers=user["headers"],
    )
    assert response.status_code == 201
    assert response.json()["data"]["name"] == "热门的卡"


# ==================================================================
#  十一、PNG 导入
# ==================================================================
# 下面这些辅助函数会生成**真实合法的 PNG**（正确的文件头、IHDR、IDAT、CRC），
# 而不是随便拼几个字节。这样测出来的结论才对真实文件有效。
def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    """按 PNG 规范拼一个数据块：长度 + 类型 + 数据 + CRC32。"""
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def make_card_png(
    *,
    text_chunks: list[tuple[str, str]] | None = None,
    itxt_chunks: list[tuple[str, str]] | None = None,
) -> bytes:
    """生成一张 1x1 的合法 PNG，并按需塞入角色卡文本块。

    text_chunks 用 tEXt（未压缩，Latin-1）
    itxt_chunks 用 iTXt（zlib 压缩，UTF-8）—— 少数工具使用这种写法

    ★ 注意 tEXt 块**只能存 Latin-1**，放不下中文。
      这正是角色卡数据要用 base64 编码的原因：base64 的结果全是 ASCII，
      而卡片 JSON 本身是 UTF-8 —— 编码一层才能塞进这种老式文本块。
      （iTXt 是后来加的，原生支持 UTF-8，所以不需要 base64。）
    """
    out = b"\x89PNG\r\n\x1a\n"
    # IHDR：宽 1、高 1、8 位色深、颜色类型 2（RGB 真彩色）
    out += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    # IDAT：一行像素 = 1 个滤波字节 0 + 1 个 RGB 像素
    out += _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))

    for keyword, text in text_chunks or []:
        out += _png_chunk(
            b"tEXt", keyword.encode("latin-1") + b"\x00" + text.encode("latin-1")
        )

    for keyword, text in itxt_chunks or []:
        # iTXt：关键字 \0 压缩标志(=1) 压缩方法(=0) 语言标签\0 翻译后关键字\0 文本
        out += _png_chunk(
            b"iTXt",
            keyword.encode("latin-1")
            + b"\x00"
            + b"\x01\x00"
            + b"\x00"
            + b"\x00"
            + zlib.compress(text.encode("utf-8")),
        )

    out += _png_chunk(b"IEND", b"")
    return out


def encode_card(card: dict) -> str:
    """把角色卡 JSON 编成 base64 —— 这是生态里标准的存法。"""
    payload = json.dumps(card, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


def upload_png(
    client: TestClient,
    user: dict,
    data: bytes,
    *,
    filename: str = "card.png",
    is_public: str | None = None,
    name_override: str | None = None,
):
    form = {}
    if is_public is not None:
        form["is_public"] = is_public
    if name_override is not None:
        form["name_override"] = name_override

    return client.post(
        f"{BASE}/import-png",
        files={"file": (filename, data, "image/png")},
        data=form,
        headers=user["headers"],
    )


def test_import_png_card(client: TestClient, user: dict) -> None:
    """★ 主流分发格式：角色卡数据藏在 PNG 的 tEXt 块里（base64 编码的 JSON）。"""
    png = make_card_png(
        text_chunks=[("chara", encode_card(V2_SAMPLE))]
    )

    response = upload_png(client, user, png)
    assert response.status_code == 201, response.text

    data = response.json()["data"]
    assert data["name"] == "来自酒馆的卡"
    assert data["greeting"] == "第一句话"
    assert data["tags"] == ["标签A", "标签B"]
    assert data["is_public"] is False


def test_import_png_keeps_character_book(client: TestClient, user: dict) -> None:
    """★ 世界书也要能从 PNG 里带进来，并且导出时还在。

    这条测试同时守住了「PNG 导入」与「世界书独立成表后仍能无损还原」两件事。
    """
    png = make_card_png(text_chunks=[("chara", encode_card(V2_SAMPLE))])
    imported = upload_png(client, user, png).json()["data"]

    exported = client.get(
        f"{BASE}/{imported['id']}/export", headers=user["headers"]
    ).json()["data"]["data"]

    expected_book = {"extensions": {}, **V2_SAMPLE["data"]["character_book"]}
    assert exported["character_book"] == expected_book


def test_import_png_prefers_ccv3_over_chara(client: TestClient, user: dict) -> None:
    """★ 同一张图里同时有新旧两个关键字时，优先取新的 ccv3。

    新版工具会同时写入两个块（向后兼容老版本）。
    如果取错了，用户拿到的会是过时的旧数据。
    """
    old_card = {"name": "旧数据"}
    new_card = {"name": "新数据", "first_mes": "新的开场白"}

    png = make_card_png(
        text_chunks=[
            ("chara", encode_card(old_card)),
            ("ccv3", encode_card(new_card)),
        ]
    )

    data = upload_png(client, user, png).json()["data"]
    assert data["name"] == "新数据"
    assert data["greeting"] == "新的开场白"


def test_import_png_accepts_raw_json_text_chunk(client: TestClient, user: dict) -> None:
    """少数工具图省事，直接把裸 JSON 塞进文本块而不做 base64。

    解析器两种都要能吃下来。
    """
    raw_json = json.dumps(
        {"name": "Raw JSON Card", "first_mes": "Hello there, traveller."},
        ensure_ascii=False,
    )
    png = make_card_png(text_chunks=[("chara", raw_json)])

    data = upload_png(client, user, png).json()["data"]
    assert data["name"] == "Raw JSON Card"
    # ★ 文本里的空格必须原样保留
    assert data["greeting"] == "Hello there, traveller."


def test_import_png_preserves_spaces_in_base64_card(
    client: TestClient, user: dict
) -> None:
    """★ 回归测试：base64 路径也不能吞掉文本内部的空格。

    背景：为了让 base64 兼容折行，解析时需要对文本做「去掉所有空白」。
    但这个处理**只能用于 base64 解码**，一旦被误用到 JSON 原文上，
    就会把字符串里有意义的空格一起删掉：

        "Hello there, traveller."  ->  "Hellothere,traveller."

    这种破坏是静默的 —— 导入「成功」，但内容已经不对了。
    """
    card = {
        "name": "Space Card",
        "first_mes": "Well well, look   who is here.",
        "mes_example": "line one\nline two",
    }
    png = make_card_png(text_chunks=[("chara", encode_card(card))])

    data = upload_png(client, user, png).json()["data"]
    assert data["greeting"] == "Well well, look   who is here."
    assert data["example_dialogue"] == "line one\nline two"


def test_import_png_itxt_compressed(client: TestClient, user: dict) -> None:
    """iTXt 块（zlib 压缩 + UTF-8）也要支持。"""
    card = {"name": "压缩卡", "first_mes": "中文开场白，需要 UTF-8"}
    png = make_card_png(
        itxt_chunks=[("chara", json.dumps(card, ensure_ascii=False))]
    )

    data = upload_png(client, user, png).json()["data"]
    assert data["name"] == "压缩卡"
    assert data["greeting"] == "中文开场白，需要 UTF-8"


def test_import_png_with_form_options(client: TestClient, user: dict) -> None:
    png = make_card_png(text_chunks=[("chara", encode_card(V2_SAMPLE))])

    data = upload_png(
        client, user, png, is_public="true", name_override="PNG 改名字"
    ).json()["data"]

    assert data["name"] == "PNG 改名字"
    assert data["is_public"] is True


def test_import_png_rejects_non_png(client: TestClient, user: dict) -> None:
    """★ 传了个 .json 文件时要给出**可操作**的提示，而不是一句「格式错误」。"""
    response = upload_png(
        client, user, '{"name":"这其实是个 JSON"}'.encode("utf-8")
    )

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "BAD_REQUEST"
    assert "PNG" in body["message"]
    # 提示里要告诉用户该改用哪个接口
    assert "JSON 导入接口" in body["message"]


def test_import_png_rejects_without_card_chunk(client: TestClient, user: dict) -> None:
    """是一张合法 PNG，但里面没有角色卡数据（普通图片）。"""
    png = make_card_png()  # 不带任何文本块

    response = upload_png(client, user, png)
    assert response.status_code == 400
    assert "没有找到角色卡数据" in response.json()["message"]


def test_import_png_rejects_corrupt_base64(client: TestClient, user: dict) -> None:
    """文本块里是既非 base64 也非 JSON 的垃圾数据。

    注意这里只能用 ASCII —— tEXt 块存不下中文，这正是卡片要用 base64 的原因。
    """
    png = make_card_png(text_chunks=[("chara", "not-base64-nor-json!!!")])

    response = upload_png(client, user, png)
    assert response.status_code == 400
    assert "无法解析" in response.json()["message"]


def test_import_png_rejects_empty_file(client: TestClient, user: dict) -> None:
    response = upload_png(client, user, b"")
    assert response.status_code == 400
    assert "空" in response.json()["message"]


def test_import_png_rejects_oversized_file(client: TestClient, user: dict) -> None:
    """★ 上传必须限制大小，否则传个大文件就能把服务内存撑爆。

    这里真的构造一个超过上限的文件，而不是把上限改小了测 ——
    要验证的是「上限确实生效」这件事本身。
    """
    from app.services import character_card_service as svc

    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (svc.MAX_PNG_BYTES + 1)

    response = upload_png(client, user, oversized)
    assert response.status_code == 400
    assert "文件太大" in response.json()["message"]


def test_import_png_rejects_missing_name(client: TestClient, user: dict) -> None:
    """PNG 里的卡缺 name —— 应当走和 JSON 导入一样的校验，返回 400。"""
    png = make_card_png(
        text_chunks=[("chara", encode_card({"description": "没有名字"}))]
    )

    response = upload_png(client, user, png)
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_REQUEST"


def test_import_png_requires_login(client: TestClient) -> None:
    png = make_card_png(text_chunks=[("chara", encode_card({"name": "x"}))])
    response = client.post(
        f"{BASE}/import-png", files={"file": ("card.png", png, "image/png")}
    )
    assert response.status_code == 401


# ==================================================================
#  十二、世界书关联
# ==================================================================
BOOKS = "/api/v1/world-books"


def make_book(client: TestClient, user: dict, **overrides) -> dict:
    """通过接口建一本世界书，用于关联测试。"""
    payload = {
        "name": f"书_{uuid.uuid4().hex[:8]}",
        "description": "测试用世界书",
        "entries": [
            {"keys": ["龙"], "content": "世上最后一条龙已在三百年前死去"},
            {"keys": ["森林"], "content": "禁忌森林", "enabled": False},
        ],
    }
    payload.update(overrides)
    response = client.post(BOOKS, json=payload, headers=user["headers"])
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_import_creates_world_book(client: TestClient, user: dict) -> None:
    """★ 卡里带的 character_book 应当被**独立建成一本世界书**并自动关联。

    这正是「删卡时可以选择保留世界书」得以实现的前提。
    """
    imported = client.post(
        f"{BASE}/import", json={"card": V2_SAMPLE}, headers=user["headers"]
    ).json()["data"]

    # 卡上应当有世界书引用
    assert imported["world_book"] is not None
    book_id = imported["world_book"]["id"]
    assert imported["world_book"]["name"] == "角色世界书"
    assert imported["world_book"]["entry_count"] == 1

    # 它确实作为独立的一行存在，且可以通过世界书接口单独访问
    detail = client.get(f"{BOOKS}/{book_id}", headers=user["headers"])
    assert detail.status_code == 200
    book = detail.json()["data"]
    assert book["entries"][0]["keys"] == ["龙"]
    assert book["entries"][0]["content"] == "世上最后一条龙"


def test_card_detail_includes_world_book_ref(client: TestClient, user: dict) -> None:
    book = make_book(client, user)
    created = create(client, user, world_book_id=book["id"])

    data = client.get(f"{BASE}/{created['id']}", headers=user["headers"]).json()["data"]

    assert data["world_book"]["id"] == book["id"]
    assert data["world_book"]["display_name"] == book["display_name"]
    assert data["world_book"]["entry_count"] == 2
    assert data["world_book"]["enabled_entry_count"] == 1


def test_card_without_world_book_has_null_ref(client: TestClient, user: dict) -> None:
    created = create(client, user)
    data = client.get(f"{BASE}/{created['id']}", headers=user["headers"]).json()["data"]
    assert data["world_book"] is None


def test_list_shows_world_book_name(client: TestClient, user: dict) -> None:
    book = make_book(client, user, name="克苏鲁世界")
    created = create(client, user, world_book_id=book["id"])

    body = client.get(BASE, params={"q": created["name"]}, headers=user["headers"]).json()["data"]
    assert body["items"][0]["world_book_name"] == "克苏鲁世界"


def test_attach_and_detach_world_book(client: TestClient, user: dict) -> None:
    """通过 PATCH 关联 / 解除关联世界书。"""
    book = make_book(client, user)
    created = create(client, user)
    assert created["world_book"] is None

    # 关联
    attached = client.patch(
        f"{BASE}/{created['id']}",
        json={"world_book_id": book["id"]},
        headers=user["headers"],
    ).json()["data"]
    assert attached["world_book"]["id"] == book["id"]

    # 解除（显式传 null）
    detached = client.patch(
        f"{BASE}/{created['id']}",
        json={"world_book_id": None},
        headers=user["headers"],
    ).json()["data"]
    assert detached["world_book"] is None

    # 世界书本身不受影响
    assert client.get(f"{BOOKS}/{book['id']}", headers=user["headers"]).status_code == 200


def test_patch_without_world_book_field_keeps_it(client: TestClient, user: dict) -> None:
    """不传 world_book_id 时，原有的关联必须保持不动。"""
    book = make_book(client, user)
    created = create(client, user, world_book_id=book["id"])

    updated = client.patch(
        f"{BASE}/{created['id']}", json={"name": "改个名字"}, headers=user["headers"]
    ).json()["data"]

    assert updated["world_book"]["id"] == book["id"]


def test_cannot_attach_others_world_book(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """★ 越权防护：不能把别人的世界书挂到自己的卡上。

    否则用户就能连卡带书一起导出，等于把别人的设定集偷走。
    """
    theirs = make_book(client, other_user)

    response = client.post(
        BASE,
        json=payload(world_book_id=theirs["id"]),
        headers=user["headers"],
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_cannot_attach_nonexistent_world_book(client: TestClient, user: dict) -> None:
    response = client.post(
        BASE, json=payload(world_book_id=999999999), headers=user["headers"]
    )
    assert response.status_code == 404


# ==================================================================
#  十三、★ 删除角色卡：两个可勾选的处理方式
# ==================================================================
def test_delete_without_force_is_rejected_when_attached(
    client: TestClient, user: dict
) -> None:
    """★ 挂着东西却没确认时，拒绝删除并说明挂了什么。

    这个 409 的 detail 就是前端渲染「勾选弹窗」所需要的数据。
    """
    book = make_book(client, user)
    created = create(client, user, world_book_id=book["id"])
    _make_session(user["id"], created["id"])

    response = client.delete(f"{BASE}/{created['id']}", headers=user["headers"])
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"

    detail = response.json()["detail"]
    assert detail["session_count"] == 1
    assert detail["has_world_book"] is True
    assert detail["world_book_id"] == book["id"]
    assert detail["world_book_shared_by_other_cards"] == 0
    # 要让前端知道有两个可选项
    assert "delete_sessions" in detail["options"]
    assert "delete_world_book" in detail["options"]


def test_delete_plain_card_needs_no_force(client: TestClient, user: dict) -> None:
    """什么都没挂的卡，直接删，不必多此一举地要求确认。"""
    created = create(client, user)
    response = client.delete(f"{BASE}/{created['id']}", headers=user["headers"])
    assert response.status_code == 200
    assert response.json()["data"]["deleted_sessions"] == 0


def test_delete_defaults_delete_both(client: TestClient, user: dict) -> None:
    """★ 两个勾选项默认都是「删」，即默认行为是删干净。"""
    book = make_book(client, user)
    created = create(client, user, world_book_id=book["id"])
    session_id = _make_session(user["id"], created["id"])

    response = client.delete(
        f"{BASE}/{created['id']}", params={"force": True}, headers=user["headers"]
    )
    assert response.status_code == 200

    summary = response.json()["data"]
    assert summary["deleted_sessions"] == 1
    assert summary["deleted_world_book"] is True
    assert summary["world_book_kept"] is False

    # 数据库里确实都没了
    with session_scope() as db:
        assert db.get(NarrativeSession, session_id) is None
        assert db.get(WorldBook, book["id"]) is None
        assert db.get(CharacterCard, created["id"]) is None


def test_delete_can_keep_sessions(client: TestClient, user: dict) -> None:
    """★ 取消勾选「删除对话记录」-> 故事保留，只是不再关联这张卡。"""
    created = create(client, user)
    session_id = _make_session(user["id"], created["id"])

    response = client.delete(
        f"{BASE}/{created['id']}",
        params={"force": True, "delete_sessions": False},
        headers=user["headers"],
    )
    assert response.status_code == 200
    assert response.json()["data"]["deleted_sessions"] == 0

    with session_scope() as db:
        survivor = db.get(NarrativeSession, session_id)
        assert survivor is not None, "勾选保留的故事被误删了！"
        # 外键被置空，表示「原来那张卡已经不在了」
        assert survivor.character_card_id is None


def test_delete_can_keep_world_book(client: TestClient, user: dict) -> None:
    """★ 取消勾选「删除世界书」-> 世界书保留下来，可继续给别的卡用。"""
    book = make_book(client, user)
    created = create(client, user, world_book_id=book["id"])

    response = client.delete(
        f"{BASE}/{created['id']}",
        params={"force": True, "delete_world_book": False},
        headers=user["headers"],
    )
    assert response.status_code == 200

    summary = response.json()["data"]
    assert summary["deleted_world_book"] is False
    assert summary["world_book_kept"] is True
    assert "按你的选择" in summary["world_book_kept_reason"]

    # 书还在，且可以重新挂到新卡上
    assert client.get(f"{BOOKS}/{book['id']}", headers=user["headers"]).status_code == 200
    another = create(client, user, world_book_id=book["id"])
    assert another["world_book"]["id"] == book["id"]


def test_delete_keeps_world_book_shared_by_other_cards(
    client: TestClient, user: dict
) -> None:
    """★ 世界书被别的卡共用时，即使勾了「删除」也不会真删。

    为了删这张卡而顺手把别张卡的世界观也删掉，显然不是用户想要的。
    此时必须保留，并在返回值里说明原因 —— 不能让用户以为已经删干净了。
    """
    book = make_book(client, user)
    first = create(client, user, world_book_id=book["id"])
    second = create(client, user, world_book_id=book["id"])  # 共用同一本书

    # 409 的 detail 里应当告知「还有别的卡在用」
    conflict = client.delete(f"{BASE}/{first['id']}", headers=user["headers"])
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["world_book_shared_by_other_cards"] == 1

    # 确认删除，且勾选了要删世界书
    response = client.delete(
        f"{BASE}/{first['id']}",
        params={"force": True, "delete_world_book": True},
        headers=user["headers"],
    )
    assert response.status_code == 200

    summary = response.json()["data"]
    assert summary["deleted_world_book"] is False, "不该删掉别张卡还在用的世界书"
    assert summary["world_book_kept"] is True
    assert "还有 1 张角色卡在使用它" in summary["world_book_kept_reason"]

    # 书还在，第二张卡的关联也没断
    assert client.get(f"{BOOKS}/{book['id']}", headers=user["headers"]).status_code == 200
    still = client.get(f"{BASE}/{second['id']}", headers=user["headers"]).json()["data"]
    assert still["world_book"]["id"] == book["id"]


def test_delete_last_user_of_world_book_removes_it(
    client: TestClient, user: dict
) -> None:
    """当一本书只被这一张卡使用时，勾了删除就真的删掉。"""
    book = make_book(client, user)
    only = create(client, user, world_book_id=book["id"])

    response = client.delete(
        f"{BASE}/{only['id']}", params={"force": True}, headers=user["headers"]
    )
    assert response.json()["data"]["deleted_world_book"] is True
    assert client.get(f"{BOOKS}/{book['id']}", headers=user["headers"]).status_code == 404


# ==================================================================
#  十四、世界书与复制 / 导出
# ==================================================================
def test_duplicate_card_copies_world_book_independently(
    client: TestClient, user: dict
) -> None:
    """★ 复制卡时世界书要**复制成新的一本**，而不是共享同一个。

    如果共享，用户改副本的世界书会把原卡的世界书一起改掉 —— 非常反直觉。
    """
    book = make_book(client, user, name="原书")
    created = create(client, user, world_book_id=book["id"])

    copy = client.post(
        f"{BASE}/{created['id']}/duplicate", headers=user["headers"]
    ).json()["data"]

    assert copy["world_book"]["id"] != book["id"], "复制出来的卡不该共用同一本世界书"
    assert copy["world_book"]["name"] == "原书"

    # 改副本的书，原书不受影响
    client.patch(
        f"{BOOKS}/{copy['world_book']['id']}",
        json={"entries": [{"keys": ["改"], "content": "只改副本"}]},
        headers=user["headers"],
    )
    original = client.get(f"{BOOKS}/{book['id']}", headers=user["headers"]).json()["data"]
    assert original["entry_count"] == 2
    assert original["entries"][0]["content"] == "世上最后一条龙已在三百年前死去"


def test_duplicate_public_card_copies_world_book_to_me(
    client: TestClient, user: dict, other_user: dict
) -> None:
    """复制别人的公开卡时，世界书要复制到自己名下（不能引用对方的）。"""
    their_book = make_book(client, other_user, name="对方的世界书")
    theirs = create(client, other_user, is_public=True, world_book_id=their_book["id"])

    copy = client.post(
        f"{BASE}/{theirs['id']}/duplicate", headers=user["headers"]
    ).json()["data"]

    assert copy["world_book"]["id"] != their_book["id"]
    assert copy["user_id"] == user["id"]

    # 新书是我的，可以随便改
    mine = client.get(
        f"{BOOKS}/{copy['world_book']['id']}", headers=user["headers"]
    ).json()["data"]
    assert mine["user_id"] == user["id"]

    # 对方的书我碰不到
    assert client.get(
        f"{BOOKS}/{their_book['id']}", headers=user["headers"]
    ).status_code == 404


def test_export_includes_linked_world_book(client: TestClient, user: dict) -> None:
    """★ 导出时必须把关联的世界书还原成规范的 character_book 字段。

    否则这张卡导出给别人用，世界观设定就丢了。
    """
    book = make_book(client, user, name="我的世界")
    created = create(client, user, world_book_id=book["id"])

    data = client.get(
        f"{BASE}/{created['id']}/export", headers=user["headers"]
    ).json()["data"]["data"]

    assert data["character_book"]["name"] == "我的世界"
    entries = data["character_book"]["entries"]
    assert len(entries) == 2
    assert entries[0]["keys"] == ["龙"]
    # 规范要求 extensions 必须存在
    assert isinstance(data["character_book"]["extensions"], dict)


def test_export_omits_character_book_when_absent(
    client: TestClient, user: dict
) -> None:
    """没有世界书时应当**整个键都不出现**，而不是给个空对象。

    规范里 character_book 是可选的，省略才贴近原始数据。
    """
    created = create(client, user)
    data = client.get(
        f"{BASE}/{created['id']}/export", headers=user["headers"]
    ).json()["data"]["data"]

    assert "character_book" not in data
