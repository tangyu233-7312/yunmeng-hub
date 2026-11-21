"""可视化控制台（/console）相关测试。

==================== 这个测试在守什么？====================
前端（web/ 目录）是纯静态资源，Python 侧测不到它的运行。
但有一类问题是可以、也应该在这里守住的：

  1. 控制台有没有被正确挂载（静态资源能不能访问、MIME 类型对不对）
  2. ★ **前端实际发出去的请求形状，后端收不收得下**

第 2 条最容易被忽略：前端表单拼出来的 JSON 里，字段名写错一个字母、
或者多带/少带一个字段，Python 侧的接口测试是发现不了的
（因为那些测试用的是自己手写的 payload）。这里刻意**照抄前端真实拼装的形状**，
一旦后端改了字段名而没同步前端，这些用例就会红。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_console.py -v
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

WEB_DIR = pathlib.Path(__file__).resolve().parents[1] / "web"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


# ==================================================================
#  一、静态资源能不能访问
# ==================================================================
def test_console_index_is_served(client: TestClient) -> None:
    response = client.get("/console/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # 页面上应当能看到系统名（第十七轮定名「云梦枢」），
    # 说明返回的确实是我们的页面而不是别的东西
    assert "云梦枢" in response.text
    # 应用图标：设计稿 PNG（白底已在 scripts/make_icon_assets.ps1 里用洪水填充去掉）
    assert "/console/img/logo-64.png" in response.text, "favicon 没有指向云梦枢图标"
    assert 'class="brand-logo"' in response.text, (
        "顶栏没有用上图标（还是旧的 HNE 方块？）"
    )
    # 关键容器都要在（app.js 依赖这些 id）
    for dom_id in ("topbar", "view", "reqlog", "modal-root", "toasts"):
        assert f'id="{dom_id}"' in response.text, f"index.html 缺少 #{dom_id}"


def test_console_without_trailing_slash_redirects(client: TestClient) -> None:
    response = client.get("/console", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"].endswith("/console/")


@pytest.mark.parametrize(
    ("path", "expect_type"),
    [
        ("/console/css/styles.css", "text/css"),
        ("/console/js/boot.js", "javascript"),
        ("/console/js/app.js", "javascript"),
        ("/console/js/api.js", "javascript"),
        ("/console/js/stream-buffer.js", "javascript"),
        ("/console/js/ui.js", "javascript"),
        ("/console/js/views/auth.js", "javascript"),
        ("/console/js/views/cards.js", "javascript"),
        ("/console/js/views/providers.js", "javascript"),
        ("/console/js/views/books.js", "javascript"),
        ("/console/js/views/chat.js", "javascript"),
    ],
)
def test_console_assets_are_served(
    client: TestClient, path: str, expect_type: str
) -> None:
    """★ 每个模块都要能被浏览器加载到。

    漏掉一个文件（比如改了名字没同步）会让整个界面白屏，
    而且浏览器控制台的报错很容易被忽略 —— 这里用测试把它钉死。
    """
    response = client.get(path)
    assert response.status_code == 200, f"{path} 访问失败"
    assert expect_type in response.headers["content-type"]
    assert len(response.text) > 0


# ==================================================================
#  一之一、★ 缓存击穿：入口页必须带版本号且不可被缓存
# ==================================================================
def test_console_index_replaces_version_placeholder(client: TestClient) -> None:
    """★ 入口页里的 __WEB_VERSION__ 必须被替换成真实版本号。

    没替换的话 importmap 会指向 `?v=__WEB_VERSION__` 这种字面量 URL，
    虽然当时也能加载，但版本号永远不变 = 缓存击穿彻底失效，
    下次改前端又会退回"旧 ui.js + 新 chat.js → 白屏"的老路。
    """
    response = client.get("/console/")
    assert response.status_code == 200
    assert "__WEB_VERSION__" not in response.text
    version = response.headers["X-HNE-Web-Version"]
    assert re.fullmatch(r"[0-9a-f]{12}", version), version
    # 版本号要同时出现在 importmap、body 属性和各资源 URL 上
    assert response.text.count(version) >= 3


def test_console_index_is_not_cacheable(client: TestClient) -> None:
    """入口页必须 no-store：它揣着 importmap 的版本号，缓存住就全错了。"""
    response = client.get("/console/")
    assert "no-store" in response.headers["cache-control"]


def test_console_module_urls_are_versioned(client: TestClient) -> None:
    """入口页里每个前端模块 URL 都要带上同一个版本号。

    只要有一个模块漏了版本号，那次改动就可能让"带版本号的别人"
    和"不带的它"混在一起 —— 正是白屏事故的成因。
    """
    html = client.get("/console/").text
    # 取出 importmap 的 JSON 部分（本页只有一个 importmap）
    match = re.search(r'<script type="importmap">(.*?)</script>', html, flags=re.S)
    assert match, "入口页缺少 importmap"
    imports = json.loads(match.group(1))["imports"]

    assert set(imports) == {
        "hne/app",
        "hne/api",
        "hne/ui",
        "hne/stream-buffer",
        "hne/auth",
        "hne/cards",
        "hne/books",
        "hne/chat",
        "hne/providers",
        "hne/presets",
        # 插件页（第五步新增）。★ 加前端模块时这里也要跟着加 ——
        # 这条断言是"importmap 与模块集合必须逐一对上"的守卫，不是可选项。
        "hne/plugins",
        # 第二十七轮新增：诊断收集（「关于 → 复制诊断信息」用）与「关于」弹窗。
        # ★ 加进来时这条断言果然先红了 —— 它拦住了"新增模块忘了登记 importmap"
        #   （那会让页面白屏），说明这条守卫生效。
        "hne/diagnostics",
        "hne/about",
        # 第三十一轮新增：「存储设置」页（浏览器端也能换数据库）。
        # ★ 又一次被这条断言拦下 —— 它就是在守"新增模块必须登记 importmap"。
        "hne/storage",
    }
    versions = set()
    for bare, url in imports.items():
        assert bare.startswith("hne/"), bare
        assert url.startswith("/console/js/"), url
        assert "?v=" in url, f"{bare} 的 URL 没有版本号: {url}"
        versions.add(url.split("?v=")[1])
    assert len(versions) == 1, f"各模块版本号不一致，会出现跨版本混用: {versions}"

    # 入口脚本自己也要带版本号（否则页面会是新的、app.js 是旧的）
    assert "js/app.js?v=" in html
    assert "js/boot.js?v=" in html


pytestmark = pytest.mark.integration


def test_file_change_updates_served_version(tmp_path):
    import time
    from fastapi import FastAPI
    from app.main import NoCacheStaticFiles

    (tmp_path / "index.html").write_text("__WEB_VERSION__", encoding="utf-8")
    asset = tmp_path / "ui.js"
    asset.write_text("old", encoding="utf-8")
    server = FastAPI()
    server.mount("/", NoCacheStaticFiles(directory=tmp_path, html=True))
    with TestClient(server) as browser:
        first = browser.get("/")
        assert first.status_code == 200
        assert first.text == first.headers["X-HNE-Web-Version"]
        asset.write_text("new-and-longer", encoding="utf-8")
        time.sleep(2.05)
        second = browser.get("/")
        assert second.headers["X-HNE-Web-Version"] != first.headers["X-HNE-Web-Version"]
        assert second.text == second.headers["X-HNE-Web-Version"]
        assert "no-store" in second.headers["Cache-Control"]
