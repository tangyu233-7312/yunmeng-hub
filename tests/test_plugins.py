"""插件系统测试（声明式：正则替换 / 提示词注入 / CSS 主题）。

==================== 覆盖重点 ====================
1. 三份配置的校验与清洗（含"CSS 里的 </style> 逃逸"这种真实攻击面）
2. 只允许 GitHub：域名白名单、重定向后**再查一次**、体积上限、清单非法
3. 插件对提示词的作用：注入位置、正则只改"发给模型的内容"、停用即失效
4. 「查看提示词」预览与真正发出去的内容**必须一致**（本项目对这条零容忍）
5. CSS 主题接口只拼接启用中的 CSS 插件，且经过清洗
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.models import User
from app.db.mysql import session_scope
from app.llm.params import GenerationParams, compute_context_budget
from app.llm.schema import ChatResult, StreamChunk, TokenUsage
from app.main import app
from app.narrative import engine
from app.narrative.presets import GUARD_IDENTITY_MARKER
from app.services import plugin_service
from app.services.plugin_service import PLUGIN_CATALOG

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

PLUGINS = "/api/v1/plugins"
BASE = "/api/v1/narrative/sessions"
CARDS = "/api/v1/character-cards"
PROVIDERS = "/api/v1/providers"

RAW_URL = "https://raw.githubusercontent.com/example/hne-plugins/main/hello.json"
BLOB_URL = "https://github.com/example/hne-plugins/blob/main/hello.json"


# ==================================================================
#  不联网的假模型（提示词类断言靠它拿到"真正发出去的请求"）
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
        FakeAdapter.script.setdefault("requests", []).append(request)
        return ChatResult(
            content=FakeAdapter.script.get("content", "（假回复）"),
            model="mock-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=3,
        )

    def stream_chat(self, request):
        FakeAdapter.script.setdefault("requests", []).append(request)
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


def _make_user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"pl_{token}",
        "email": f"pl_{token}@example.com",
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
        "headers": {"Authorization": f"Bearer {logged_in.json()['data']['access_token']}"},
    }


@pytest.fixture
def user(client: TestClient) -> dict:
    data = _make_user(client)
    yield data
    with session_scope() as db:
        row = db.query(User).filter(User.id == data["id"]).one_or_none()
        if row is not None:
            db.delete(row)


def _make_session(client: TestClient, user: dict) -> int:
    card = client.post(
        CARDS,
        json={"name": f"插件卡_{uuid.uuid4().hex[:6]}", "greeting": "……你来了。"},
        headers=user["headers"],
    )
    assert card.status_code == 201, card.text
    provider = client.post(
        PROVIDERS,
        json={
            "name": f"pl_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-plugin-test",
            "model_name": "mock-model",
            "context_window": 8192,
        },
        headers=user["headers"],
    )
    assert provider.status_code == 201, provider.text
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


def _create(client: TestClient, user: dict, **body) -> dict:
    payload = {"kind": "prompt", "config": {"position": "end", "content": "插件内容"}, **body}
    created = client.post(PLUGINS, json=payload, headers=user["headers"])
    assert created.status_code == 201, created.text
    return created.json()["data"]


def _send(client: TestClient, user: dict, session_id: int, text: str = "我推门进去") -> None:
    response = client.post(
        f"{BASE}/{session_id}/messages", json={"content": text}, headers=user["headers"]
    )
    assert response.status_code == 201, response.text


# ==================================================================
#  一、默认插件与 CRUD
# ==================================================================
def test_first_list_creates_two_empty_defaults_then_never_duplicates(
    client: TestClient, user: dict
) -> None:
    first = client.get(PLUGINS, headers=user["headers"])
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["total"] == 2, data
    kinds = sorted(item["kind"] for item in data["items"])
    assert kinds == ["prompt", "regex"]
    # ★ 默认插件必须是**空的**：预置规则会悄悄改用户的提示词
    for item in data["items"]:
        assert item["is_builtin"] is True
        if item["kind"] == "regex":
            assert item["config"]["rules"] == []
        else:
            assert item["config"]["content"] == ""
    assert data["security_note"] and data["allowed_hosts"] == list(plugin_service.ALLOWED_HOSTS)

    again = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    assert again["total"] == 2, "重复访问不能把默认插件越加越多"

    # 删掉默认插件后不能再自动重建（用户的删除是明确的意图）
    for item in again["items"]:
        assert client.delete(f"{PLUGINS}/{item['id']}", headers=user["headers"]).status_code == 200
    after = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    assert after["total"] == 0, "删掉的默认插件不该被自动重建"


def test_toggle_priority_and_delete(client: TestClient, user: dict) -> None:
    row = _create(client, user, name="开关测试", config={"position": "end", "content": "x"})
    off = client.patch(f"{PLUGINS}/{row['id']}", json={"enabled": False}, headers=user["headers"])
    assert off.status_code == 200 and off.json()["data"]["enabled"] is False
    order = client.patch(f"{PLUGINS}/{row['id']}", json={"priority": 5}, headers=user["headers"])
    assert order.json()["data"]["priority"] == 5
    assert client.delete(f"{PLUGINS}/{row['id']}", headers=user["headers"]).status_code == 200
    assert client.patch(f"{PLUGINS}/{row['id']}", json={"enabled": True}, headers=user["headers"]).status_code == 404


def test_other_users_plugin_is_404(client: TestClient, user: dict) -> None:
    row = _create(client, user, name="我的", config={"position": "end", "content": "x"})
    other = _make_user(client)
    try:
        assert (
            client.patch(f"{PLUGINS}/{row['id']}", json={"enabled": False}, headers=other["headers"]).status_code
            == 404
        )
        assert client.delete(f"{PLUGINS}/{row['id']}", headers=other["headers"]).status_code == 404
    finally:
        with session_scope() as db:
            other_row = db.query(User).filter(User.id == other["id"]).one_or_none()
            if other_row is not None:
                db.delete(other_row)


# ==================================================================
#  二、配置校验与 CSS 清洗
# ==================================================================
@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "regex", "config": {"rules": [{"pattern": "(", "replacement": ""}]}},  # 坏正则
        {"kind": "regex", "config": {"rules": [{"pattern": "a", "flags": "q"}]}},  # 非法 flags
        {"kind": "regex", "config": {"rules": [{"replacement": "x"}]}},  # 缺 pattern
        {"kind": "regex", "config": {"rules": "不是数组"}},
        {"kind": "prompt", "config": {"position": "中间"}},  # 位置非法
        {"kind": "css", "config": {"css": "x" * (plugin_service.MAX_CSS_CHARS + 1)}},
        {"kind": "js", "config": {}},  # 类型不支持
    ],
)
def test_bad_config_is_rejected(client: TestClient, user: dict, payload: dict) -> None:
    body = {"name": "非法插件", **payload}
    response = client.post(PLUGINS, json=body, headers=user["headers"])
    assert response.status_code in (400, 422), response.text
    # 不能留下半装状态
    assert client.get(PLUGINS, headers=user["headers"]).json()["data"]["total"] == 2


def test_css_sanitizer_blocks_escape_and_remote_import() -> None:
    dirty = (
        "</style><script>alert(1)</script>"
        "@import url(https://evil.example/x.css);"
        ".a{width:expression(alert(1));background:url(javascript:alert(2));behavior:url(#x)}"
        ".b{color:red}"
    )
    clean = plugin_service.sanitize_css(dirty)
    assert "</style>" not in clean, "必须挡掉 style 标签逃逸"
    assert "@import" not in clean
    assert "expression(" not in clean
    assert "javascript:" not in clean
    assert "behavior:" not in clean
    assert ".b{color:red}" in clean, "正常样式不能被误伤"


def test_summary_is_human_readable() -> None:
    assert plugin_service.summary_of("regex", {"rules": [{"pattern": "a"}]}) == "1 条替换规则"
    assert "末尾" in plugin_service.summary_of("prompt", {"position": "end", "content": "abc"})
    assert "主题样式" in plugin_service.summary_of("css", {"css": "a{}"})


# ==================================================================
#  三、只允许 GitHub（安装）
# ==================================================================
@pytest.mark.parametrize(
    "url,expected_part",
    [
        ("http://github.com/u/r/blob/main/a.json", "https"),
        ("https://gitlab.com/u/r/blob/main/a.json", "只允许从 GitHub"),
        ("https://raw.githubusercontent.com.evil.com/u/r/main/a.json", "只允许从 GitHub"),
        ("https://github.com/u/r", "blob"),
        ("", "请填写"),
    ],
)
def test_install_url_whitelist(url: str, expected_part: str) -> None:
    with pytest.raises(Exception) as exc:
        plugin_service.normalize_github_url(url)
    assert expected_part in str(exc.value)


def test_blob_url_is_converted_to_raw() -> None:
    assert plugin_service.normalize_github_url(BLOB_URL) == RAW_URL
    # raw 地址原样通过（顺手去掉 query，GitHub 的 ?plain=1 之类不该带过去）
    assert plugin_service.normalize_github_url(RAW_URL + "?plain=1") == RAW_URL


GIST_RAW = "https://gist.githubusercontent.com/someone/abc123/raw"


def test_gist_urls_are_supported() -> None:
    """gist 也是 GitHub：最轻的分享方式，写一个插件不必建仓库。"""
    # 单文件 gist：页面地址自动补 /raw
    assert plugin_service.normalize_github_url("https://gist.github.com/someone/abc123") == GIST_RAW
    # 已经是 gist 的 raw 地址
    assert plugin_service.normalize_github_url(GIST_RAW) == GIST_RAW
    assert (
        plugin_service.normalize_github_url("https://gist.github.com/someone/abc123/raw")
        == GIST_RAW
    )
    # 多文件：?file= 明确指定（原样使用）
    assert (
        plugin_service.normalize_github_url("https://gist.github.com/someone/abc123?file=demo.json")
        == f"{GIST_RAW}/demo.json"
    )
    # 多文件：页面锚点 #file-demo-json 也能还原（GitHub 的锚点规则有损，只在能安全还原时猜）
    assert (
        plugin_service.normalize_github_url(
            "https://gist.github.com/someone/abc123#file-demo-json"
        )
        == f"{GIST_RAW}/demo.json"
    )
    # 还原不了的锚点不猜（gist 的 /raw 默认给第一个文件）
    assert (
        plugin_service.normalize_github_url(
            "https://gist.github.com/someone/abc123#file-某个中文名"
        )
        == GIST_RAW
    )


def test_gist_url_rejects_path_traversal_and_short_path() -> None:
    with pytest.raises(Exception) as exc:
        plugin_service.normalize_github_url("https://gist.github.com/someone/abc123?file=../x.json")
    assert "只能是文件名" in str(exc.value)
    with pytest.raises(Exception) as exc2:
        plugin_service.normalize_github_url("https://gist.github.com/someone")
    assert "gist 地址" in str(exc2.value)


def test_install_from_gist_url(client: TestClient, user: dict) -> None:
    with session_scope() as db:
        row, raw, _, _ = plugin_service.install_from_url(
            db,
            user["id"],
            "https://gist.github.com/someone/abc123?file=demo.json",
            client=_mock_client(_manifest()),
        )
    assert raw == f"{GIST_RAW}/demo.json"
    assert row.source_url == raw and row.kind == "regex"


def _mock_client(payload: bytes, *, status: int = 200, location: str | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if location and request.url.host == "raw.githubusercontent.com":
            return httpx.Response(302, headers={"Location": location}, request=request)
        return httpx.Response(status, content=payload, request=request)

    return httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True, timeout=5
    )


def _manifest(**over) -> bytes:
    body = {
        "spec": "hne_plugin_v1",
        "name": "示例插件",
        "kind": "regex",
        "version": "1.0.0",
        "author": "tester",
        "description": "把「你」换成「阁下」",
        "config": {"rules": [{"pattern": "你", "replacement": "阁下"}]},
    }
    body.update(over)
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def test_install_happy_path_and_reinstall_updates(client: TestClient, user: dict) -> None:
    with session_scope() as db:
        row, raw, size, keys = plugin_service.install_from_url(
            db, user["id"], BLOB_URL, client=_mock_client(_manifest())
        )
        assert raw == RAW_URL and size > 0 and "config" in keys
        first_id = row.id

    listed = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    installed = [i for i in listed["items"] if i["source_url"] == RAW_URL]
    assert len(installed) == 1 and installed[0]["author"] == "tester"

    # 同一来源再次安装 = 更新，而不是堆一份同名副本
    with session_scope() as db:
        row2, _, _, _ = plugin_service.install_from_url(
            db,
            user["id"],
            BLOB_URL,
            client=_mock_client(_manifest(version="2.0.0", config={"rules": []})),
        )
        assert row2.id == first_id and row2.version == "2.0.0"
    listed2 = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    assert len([i for i in listed2["items"] if i["source_url"] == RAW_URL]) == 1


def test_install_rejects_redirect_outside_allowlist(client: TestClient, user: dict) -> None:
    with session_scope() as db, pytest.raises(Exception) as exc:
        plugin_service.install_from_url(
            db,
            user["id"],
            BLOB_URL,
            client=_mock_client(_manifest(), location="https://evil.example/x.json"),
        )
    assert "重定向" in str(exc.value)
    assert client.get(PLUGINS, headers=user["headers"]).json()["data"]["total"] == 2


def test_install_rejects_oversize_and_bad_manifest(client: TestClient, user: dict) -> None:
    with session_scope() as db:
        with pytest.raises(Exception) as exc:
            plugin_service.install_from_url(
                db,
                user["id"],
                BLOB_URL,
                client=_mock_client(b"{" + b" " * plugin_service.MAX_MANIFEST_BYTES),
            )
        assert "太大" in str(exc.value)

        with pytest.raises(Exception) as exc2:
            plugin_service.install_from_url(
                db, user["id"], BLOB_URL, client=_mock_client(b"not json")
            )
        assert "合法 JSON" in str(exc2.value)

        with pytest.raises(Exception) as exc3:
            plugin_service.install_from_url(
                db,
                user["id"],
                BLOB_URL,
                client=_mock_client(_manifest(kind="shell")),
            )
        assert "kind" in str(exc3.value)

        with pytest.raises(Exception) as exc4:
            plugin_service.install_from_url(
                db, user["id"], BLOB_URL, client=_mock_client(_manifest(spec="other_spec"))
            )
        assert "spec" in str(exc4.value)
    # 四次失败都不能留下东西
    assert client.get(PLUGINS, headers=user["headers"]).json()["data"]["total"] == 2


def test_install_surfaces_download_error(client: TestClient, user: dict) -> None:
    with session_scope() as db, pytest.raises(Exception) as exc:
        plugin_service.install_from_url(
            db, user["id"], BLOB_URL, client=_mock_client(b"", status=404)
        )
    assert "404" in str(exc.value)


# ==================================================================
#  四、插件真的作用在"发给模型的提示词"上
# ==================================================================
def test_prompt_plugin_is_injected_at_the_very_end(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _make_session(client, user)
    _create(
        client,
        user,
        name="文风约束",
        kind="prompt",
        config={"position": "end", "content": "【插件】每轮结尾只写一句话。"},
    )
    _send(client, user, session_id)
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "【插件】每轮结尾只写一句话。" in system
    # 末尾位置：插件内容必须排在身份守卫**之后**（越靠后权重越高）
    assert system.index("【插件】每轮结尾只写一句话。") > system.index(GUARD_IDENTITY_MARKER)
    assert system.rstrip().endswith("【插件】每轮结尾只写一句话。") or system.index(
        "【插件】每轮结尾只写一句话。"
    ) > system.index("[最高优先级 · 身份认知]")


def test_prompt_plugin_before_guard_and_start(client: TestClient, user: dict, fake_llm) -> None:
    session_id = _make_session(client, user)
    _create(
        client,
        user,
        name="守卫前",
        kind="prompt",
        config={"position": "before_guard", "content": "【守卫前】先读设定。"},
    )
    _create(
        client,
        user,
        name="开头",
        kind="prompt",
        config={"position": "start", "content": "【开头】你是叙事者。"},
    )
    _send(client, user, session_id)
    system = fake_llm.script["requests"][-1].messages[0].content
    assert system.index("【开头】你是叙事者。") < system.index("【守卫前】先读设定。")
    assert system.index("【守卫前】先读设定。") < system.index(GUARD_IDENTITY_MARKER)


def test_regex_plugin_changes_only_what_is_sent(
    client: TestClient, user: dict, fake_llm
) -> None:
    session_id = _make_session(client, user)
    _create(
        client,
        user,
        name="称呼统一",
        kind="regex",
        config={"rules": [{"pattern": "灯塔", "replacement": "灯楼"}]},
    )
    fake_llm.script["content"] = "我走进灯塔。"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "我去灯塔看看"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text

    request = fake_llm.script["requests"][-1]
    joined = "\n".join(str(m.content) for m in request.messages)
    assert "灯楼" in joined and "灯塔" not in joined, "发给模型的提示词必须被替换"

    # ★ 数据库里的正文与用户消息**不能**被改（插件只影响发给模型的内容）
    stored = sent.json()["data"]
    assert "灯楼" not in stored["assistant_message"]["content"]
    assert "灯塔" in stored["assistant_message"]["content"]
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    user_msgs = [m["content"] for m in detail["messages"] if m["role"] == "user"]
    assert "我去灯塔看看" in user_msgs, "用户原话必须原样保存"


def test_disabled_plugin_has_no_effect(client: TestClient, user: dict, fake_llm) -> None:
    session_id = _make_session(client, user)
    row = _create(
        client,
        user,
        name="会被停用",
        kind="prompt",
        config={"position": "end", "content": "【不该出现】"},
    )
    client.patch(f"{PLUGINS}/{row['id']}", json={"enabled": False}, headers=user["headers"])
    _send(client, user, session_id)
    system = fake_llm.script["requests"][-1].messages[0].content
    assert "【不该出现】" not in system


def test_preview_matches_the_real_request(client: TestClient, user: dict, fake_llm) -> None:
    """预览与实际必须一致 —— 这是本项目对"预览骗人"的零容忍条款。"""
    session_id = _make_session(client, user)
    _create(
        client,
        user,
        name="预览一致性",
        kind="prompt",
        config={"position": "end", "content": "【预览必须看到我】"},
    )
    _create(
        client,
        user,
        name="预览正则",
        kind="regex",
        config={"rules": [{"pattern": "推门", "replacement": "踹门"}]},
    )
    _send(client, user, session_id, text="我推门进去")
    sent_system = fake_llm.script["requests"][-1].messages[0].content
    sent_joined = "\n".join(str(m.content) for m in fake_llm.script["requests"][-1].messages)

    preview = client.get(
        f"{BASE}/{session_id}", params={"with_prompt": "true"}, headers=user["headers"]
    ).json()["data"]["prompt"]
    assert "【预览必须看到我】" in preview["system_prompt"]
    assert preview["system_prompt"] == sent_system, "预览的系统提示词与实际必须逐字一致"
    assert "踹门" in sent_joined and "踹门" in preview["system_prompt"] + sent_joined


# ==================================================================
#  六、内置示例目录（对照 SillyTavern 的内置扩展挑出来的）
# ==================================================================
def test_every_catalog_entry_is_valid_and_declarative() -> None:
    """目录里的每一条都必须是合法配置 —— 否则用户点「添加」才发现装不上。

    ★ 判据用 `PLUGIN_KINDS`（唯一真源）而不是写死的三元组：
      第十三轮加了 `dice` 之后，写死的列表会让"目录里多一条合法插件"变成测试失败，
      而这条测试真正要守的是"**每一种**都必须是声明式能力、且配置校验能过"。
    """
    assert plugin_service.PLUGIN_CATALOG, "目录不能是空的"
    assert set(plugin_service.PLUGIN_KINDS) == {"regex", "prompt", "css", "dice"}
    for spec in plugin_service.PLUGIN_CATALOG:
        assert spec["kind"] in plugin_service.PLUGIN_KINDS
        assert spec["source"], "每条都要写清楚对应酒馆的哪个能力"
        config = plugin_service.validate_config(spec["kind"], spec["config"])
        assert plugin_service.summary_of(spec["kind"], config)
    keys = [spec["key"] for spec in plugin_service.PLUGIN_CATALOG]
    assert len(keys) == len(set(keys)), "key 不能重复"
    # ★ 骰子插件必须在目录里（用户要的"插件化架构 + 骰子"，必须一键能拿到）
    assert "trpg_dice" in keys


def test_unsupported_sillytavern_extensions_are_listed_honestly(
    client: TestClient, user: dict
) -> None:
    """做不到的就如实列出来（TTS / 生图 / 快捷回复…），不能装作支持。"""
    data = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    names = " ".join(item["name"] for item in data["unsupported"])
    assert "Text To Speech" in names and "Image Generation" in names
    assert all(item["reason"] for item in data["unsupported"])
    assert len(data["catalog"]) == len(plugin_service.PLUGIN_CATALOG)


def test_add_from_catalog_then_no_duplicate(client: TestClient, user: dict, fake_llm) -> None:
    listed = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    entry = next(item for item in listed["catalog"] if item["key"] == "authors_note")
    assert entry["installed"] is False

    added = client.post(f"{PLUGINS}/catalog/authors_note", headers=user["headers"])
    assert added.status_code == 201, added.text
    assert added.json()["data"]["kind"] == "prompt"

    again = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    marked = next(item for item in again["catalog"] if item["key"] == "authors_note")
    assert marked["installed"] is True
    assert marked["update_available"] is False, "刚添加的就是最新版，不该提示有更新"
    assert again["total"] == 3, "默认两个空壳 + 刚添加的一个"

    # ★ 第十七轮改了语义：同一个接口现在同时承担"添加"与"**更新**"。
    #   已经是最新时不假装"又成功了一次"，而是如实说"已经是最新版"。
    #   （旧断言是"已经添加过"—— 那是"只能添加不能更新"时代的话术，
    #     而"内容在添加时就被拷贝、内置主题升级后用户看不出变化"正是本轮要修的问题。）
    dup = client.post(f"{PLUGINS}/catalog/authors_note", headers=user["headers"])
    assert dup.status_code == 400 and "已经是最新版" in dup.text
    assert client.get(PLUGINS, headers=user["headers"]).json()["data"]["total"] == 3

    missing = client.post(f"{PLUGINS}/catalog/nonexistent", headers=user["headers"])
    assert missing.status_code == 404


def test_catalog_can_update_a_stale_builtin_copy(client: TestClient, user: dict) -> None:
    """★ 内置插件升级后，用户那份"添加时拷贝"的旧内容**能一键更新**。

    ==================== 这条守的是一个真实困惑 ====================
    内置主题（云梦枢 · 星云暗涌）随版本升级，而插件内容在**添加时就被拷进数据库**了。
    于是出现"我改了主题、用户却说看不出变化" —— 他看到的还是当初那份。
    以前只能让他删掉重加（还得自己发现）。现在目录会如实标 `update_available`，
    接口把这个 key 再 POST 一次就覆盖成最新内容。
    """
    # 1) 从目录添加
    first = client.post(f"{PLUGINS}/catalog/authors_note", headers=user["headers"])
    assert first.status_code == 201
    plugin_id = first.json()["data"]["id"]

    # 2) 模拟"这是老版本"：直接把库里的内容改成别的东西
    with session_scope() as session:
        row = session.get(plugin_service.Plugin, plugin_id)
        row.config = {"position": "end", "content": "【老版本的内容】"}
    listed = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    entry = next(i for i in listed["catalog"] if i["key"] == "authors_note")
    assert entry["installed"] is True and entry["update_available"] is True, (
        "库里那份与目录不同，界面上应当显示「有更新」"
    )

    # 3) 点「更新到最新」= 同一个接口再 POST 一次
    upd = client.post(f"{PLUGINS}/catalog/authors_note", headers=user["headers"])
    assert upd.status_code == 200, upd.text
    assert "已更新到最新" in upd.text
    assert upd.json()["data"]["id"] == plugin_id, "更新是覆盖同一个插件，不是再建一个"
    listed2 = client.get(PLUGINS, headers=user["headers"]).json()["data"]
    entry2 = next(i for i in listed2["catalog"] if i["key"] == "authors_note")
    assert entry2["update_available"] is False
    assert listed2["total"] == 3, "更新不该多出一条插件"


def test_catalog_does_not_clobber_same_name_different_kind(client: TestClient, user: dict) -> None:
    """用户自己建了一个**同名但类型不同**的插件时，内置示例不许把它覆盖掉。"""
    _create(client, user, name="作者注（Author's Note）", kind="regex",
            config={"rules": [{"pattern": "a", "replacement": "b"}]})
    resp = client.post(f"{PLUGINS}/catalog/authors_note", headers=user["headers"])
    assert resp.status_code == 400 and "不会覆盖" in resp.text


def test_catalog_regex_entry_really_rewrites_the_prompt(
    client: TestClient, user: dict, fake_llm
) -> None:
    """目录里的「清理 Markdown 强调符号」必须真的生效，而不只是列表里好看。"""
    session_id = _make_session(client, user)
    assert client.post(f"{PLUGINS}/catalog/strip_markdown", headers=user["headers"]).status_code == 201
    fake_llm.script["content"] = "（他点点头）"
    sent = client.post(
        f"{BASE}/{session_id}/messages",
        json={"content": "他说 **你好**，又补了一句 *小声* 的话"},
        headers=user["headers"],
    )
    assert sent.status_code == 201, sent.text
    joined = "\n".join(str(m.content) for m in fake_llm.script["requests"][-1].messages)
    assert "**" not in joined and "你好" in joined
    assert "*小声*" not in joined and "小声" in joined
    # 数据库里仍是用户原话（插件只改"发给模型的"）
    detail = client.get(f"{BASE}/{session_id}", headers=user["headers"]).json()["data"]
    user_msgs = [m["content"] for m in detail["messages"] if m["role"] == "user"]
    assert any("**你好**" in t for t in user_msgs)


# ==================================================================
#  五、CSS 主题接口
# ==================================================================

def test_theme_css_only_joins_enabled_css_plugins(client: TestClient, user: dict) -> None:
    _create(
        client,
        user,
        name="紫色主题",
        kind="css",
        config={"css": ":root{--brand:#7c3aed;}"},
    )
    dark = _create(
        client,
        user,
        name="深色主题",
        kind="css",
        config={"css": "body{background:#111}</style>@import url(https://evil.example/x.css);"},
    )
    _create(
        client,
        user,
        name="不该出现",
        kind="prompt",
        config={"position": "end", "content": "【提示词插件不该进 CSS】"},
    )
    # 停用其中一个：停用的不能出现在主题里
    client.patch(f"{PLUGINS}/{dark['id']}", json={"enabled": False}, headers=user["headers"])

    response = client.get(f"{PLUGINS}/theme.css", headers=user["headers"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    css = response.text
    assert "--brand:#7c3aed;" in css
    assert "深色主题" not in css and "#111" not in css, "停用的插件不能生效"
    assert "提示词插件不该进 CSS" not in css

    # 重新启用后再取，必须带上，且逃逸/远程导入被清洗
    client.patch(f"{PLUGINS}/{dark['id']}", json={"enabled": True}, headers=user["headers"])
    css2 = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "#111" in css2
    assert "</style>" not in css2 and "@import" not in css2


def test_catalog_yunmeng_starfield_lists_are_consistent(client: TestClient, user: dict) -> None:
    """★ 星点背景的三个列表必须**等长**（`background-image` / `-size` / `-repeat`）。

    ==================== 为什么值得一条测试 ====================
    星点有几十层，每一层都要在这三个逗号列表里各占一个位置。**只要数量对不上**，
    浏览器要么整条声明作废、要么把 tile 尺寸错位到别的层上 —— 表现是"背景没了"或者
    "星云变成一块块方格"，而且**不报任何错**。（本项目踩过的是它的近亲：
    用 `background` 简写把 `background-size` 重置掉。）

    这些列表现在由 `_starfield_css()` 从同一份数据生成，但**手改一次就可能破**，
    所以这里直接解析目录里那份 CSS 来数。
    """
    spec = next(s for s in PLUGIN_CATALOG if s["key"] == "yunmeng_nebula")
    css = str(spec["config"]["css"])

    def count(prop: str) -> int:
        body = css.split(f"{prop}:", 1)[1].split(";", 1)[0]
        if prop == "background-image":
            return body.count("radial-gradient")
        return body.count(",") + 1

    layers = count("background-image")
    sizes = count("background-size")
    repeats = count("background-repeat")
    assert layers == sizes == repeats, (
        f"星点背景三个列表长度不一致：image={layers} size={sizes} repeat={repeats} —— "
        "浏览器会静默丢弃或错位平铺"
    )
    assert layers >= 30, f"星点太少（现在 {layers} 层），用户会反馈'背景看不出变化'"


def test_catalog_yunmeng_theme_defines_every_variable(client: TestClient, user: dict) -> None:
    """★「云梦枢 · 星云暗涌」必须把组件用到的变量**给全**。

    ==================== 这条守的是"深色主题里那块白"的老账 ====================
    一个深色主题只换 `--bg/--panel`，而组件还在读 `--surface`/`--*-border` 时，
    那些组件会**停留在浅色默认值**上 —— 这就是之前反复出现的"深色下冒出一块白/一条亮线"。

    做法：从 `styles.css` 的 `:root` 里把变量名全抓出来，逐个检查主题里有没有定义
    （允许引擎补缺的那几个除外，它们由 `_surface_shim` 兜底）。

    ★ 只要求"**值是颜色的**变量"：`--radius` / `--mono` / `--reqlog-w` 这类
      结构性数值本来就不该由主题重写，把它们算进来只会得到一条没人理的测试。
      判据用"值里出现颜色字面量或 var(...)"，所以以后**再新增一个颜色变量**，
      这条测试会自动开始要求它 —— 这正是我们要的守门方式。
    """
    css = (WEB_DIR / "css" / "styles.css").read_text(encoding="utf-8")
    root = re.search(r":root\s*\{(.*?)\}", css, flags=re.S)
    assert root is not None, "找不到 :root 变量块"
    pairs = re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", root.group(1))
    assert len(pairs) >= 20, f"只解析到 {len(pairs)} 个变量，解析逻辑可能失效"
    colour_vars = {
        name
        for name, value in pairs
        if ("#" in value) or ("rgb" in value) or ("hsl" in value)
    }
    assert len(colour_vars) >= 15, f"只认出 {len(colour_vars)} 个颜色变量，判据可能失效"

    # 引擎会按"只补缺"的规则兜底的几个：主题不写也不会出问题
    shim_backed = {
        "--surface",
        "--panel",
        "--surface-2",
        "--surface-3",
        "--warn-border",
        "--danger-border",
        "--info-border",
        "--ok-border",
        "--catalog-accent",
    }
    spec = next((s for s in PLUGIN_CATALOG if s["key"] == "yunmeng_nebula"), None)
    assert spec is not None, "内置目录里没有「云梦枢 · 星云暗涌」"
    theme_css = str(spec["config"]["css"])
    missing = sorted(v for v in colour_vars - shim_backed if f"{v}:" not in theme_css)
    assert not missing, (
        "星云暗涌主题没有给全这些变量，组件会退回**浅色默认值**（深色下就是一块白）："
        + "、".join(missing)
    )


def test_catalog_yunmeng_theme_composes_with_the_engine(client: TestClient, user: dict) -> None:
    """装上这条主题后：引擎不再补任何东西（说明它自己给全了），且清洗不会削掉毛玻璃。"""
    spec = next(s for s in PLUGIN_CATALOG if s["key"] == "yunmeng_nebula")
    _create(client, user, name="星云暗涌", kind="css", config={"css": spec["config"]["css"]})
    css = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "backdrop-filter" in css, "毛玻璃被清洗掉了（sanitize_css 不该动它）"
    assert "radial-gradient" in css, "星云渐变丢了"
    # 引擎的补缺块不该出现：这条主题把变量都给全了
    assert "引擎补全" not in css, "主题没给全变量，引擎不得不补 —— 请补齐而不是依赖兜底"


def test_theme_css_backfills_catalog_accent(client: TestClient, user: dict) -> None:
    """主题没写 `--catalog-accent` 时，引擎让它跟着该主题的 `--brand`。

    ★ 为什么：目录里那个 ✦ 头像原来是 JS 内联的 `color:#6b46c1`，
      而底是 `var(--brand-soft)` —— 深色主题下深紫压在深藏青上**看不清**。
      收进变量之后，老主题（不会自动升级）由引擎补，用户不必删了重加。
    """
    _create(
        client,
        user,
        name="老主题",
        kind="css",
        config={"css": ":root{--brand:#7c9cff;--panel:#1a2029;}"},
    )
    css = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "--catalog-accent:var(--brand)" in css


def test_theme_css_keeps_themes_own_catalog_accent(client: TestClient, user: dict) -> None:
    """用户自己调过就以他写的为准。"""
    _create(
        client,
        user,
        name="自己调过目录色的主题",
        kind="css",
        config={"css": ":root{--brand:#7c9cff;--catalog-accent:#ffd479;}"},
    )
    css = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "#ffd479" in css
    assert "--catalog-accent:var(--brand)" not in css


def test_theme_css_backfills_alert_border_colors(client: TestClient, user: dict) -> None:
    """老主题缺 `--*-border` 时，引擎按"文字色 × 柔和底"的中间调补上。

    ★ 为什么：提示框边框以前写死成四个浅色，深色主题下每个 `.alert` 都镶一圈**亮边**
      （横幅、失败回复都中招）。用户库里那份主题是"添加时拷贝"的，不会自动升级，
      所以由引擎补 —— 补出来的是中间调，跟着主题一起变深，而不是一条亮线。
    """
    _create(
        client,
        user,
        name="老深色主题",
        kind="css",
        config={
            "css": ":root{--panel:#1a2029;--warn-text:#e6c579;--warn-soft:#332a15;"
            "--danger-text:#f0a396;--danger-soft:#3a1f1c;}"
        },
    )
    css = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "--warn-border:var(--warn-text);" in css
    assert "color-mix(in srgb, var(--warn-text) 40%, var(--warn-soft))" in css
    assert "--danger-border:var(--danger-text);" in css
    # 主题没提到 ok/info 这套色 → 不要去碰它（免得给无关主题注入变量）
    assert "--ok-border" not in css and "--info-border" not in css


def test_theme_css_does_not_override_themes_own_border_colors(
    client: TestClient, user: dict
) -> None:
    """用户自己写了 `--warn-border` 就以他写的为准。"""
    _create(
        client,
        user,
        name="自己调过边框的主题",
        kind="css",
        config={
            "css": ":root{--panel:#101418;--warn-text:#ffd479;--warn-soft:#2b2410;"
            "--warn-border:#8a6d1f;}"
        },
    )
    css = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "#8a6d1f" in css
    assert "--warn-border:var(--warn-text)" not in css, "已经写了的变量不能被引擎改写"


def test_theme_css_backfills_surface_levels_for_old_themes(
    client: TestClient, user: dict
) -> None:
    """老主题只写了 `--surface` 时，引擎要补上二/三级表面变量。

    ★ 这是真实事故：`--surface-2/--surface-3` 是后加进来的语义变量，
      而插件内容在用户点「添加」时就**拷进了数据库**，目录升级不会改用户那份。
      结果 "深色主题下弹窗底栏/备选区又是一块白" —— 这一条保证不用删了重加也能修好。
    """
    _create(
        client,
        user,
        name="老深色主题",
        kind="css",
        config={"css": ":root{--bg:#12161c;--panel:#1b2230;--surface:#1b2230;}"},
    )
    css = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "--surface-2:var(--surface)" in css
    assert "--surface-3:var(--surface-2)" in css


def test_theme_css_maps_legacy_panel_theme_onto_surface(
    client: TestClient, user: dict
) -> None:
    """更老的主题只知道 `--panel`（没有 `--surface`）—— 也要能完整覆盖。

    ★ 这类主题在界面上表现为"大部分变暗了，卡片/弹窗还是白的"，
      因为新组件读的是 `--surface`，而它只写了 `--panel`。
    """
    _create(
        client,
        user,
        name="更老的深色主题",
        kind="css",
        config={"css": ":root{--bg:#12161c;--panel:#1b2230;}"},
    )
    css = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "--surface:var(--panel)" in css
    assert "--surface-2:var(--surface)" in css
    assert "--surface-3:var(--surface-2)" in css


def test_theme_css_keeps_surface_levels_the_theme_already_defines(
    client: TestClient, user: dict
) -> None:
    """用户自己写过的变量一律以他写的为准 —— 只补缺的，不覆盖。"""
    _create(
        client,
        user,
        name="新深色主题",
        kind="css",
        config={"css": ":root{--surface:#1b2230;--surface-3:#101418;}"},
    )
    css = client.get(f"{PLUGINS}/theme.css", headers=user["headers"]).text
    assert "#101418" in css
    assert "--surface-2:var(--surface)" in css, "缺的那一级仍然要补"
    assert "--surface-3:var(--surface-2)" not in css, "已经写了的不能被改掉"
