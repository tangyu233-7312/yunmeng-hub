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
import os
import pathlib
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import User
from app.db.mysql import session_scope
from app.main import app

WEB_DIR = pathlib.Path(__file__).resolve().parents[1] / "web"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _make_user(client: TestClient) -> dict:
    token = uuid.uuid4().hex[:10]
    account = {
        "username": f"ui_{token}",
        "email": f"ui_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": account["username"], "password": account["password"]},
    )
    assert logged_in.status_code == 200

    # ★ 登录响应里只有令牌，**没有** user 对象（实测确认）。
    #   前端也是靠紧接着调一次 /auth/me 来补齐用户信息的，这里保持同样的做法。
    headers = {
        "Authorization": f"Bearer {logged_in.json()['data']['access_token']}"
    }
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text

    return {"id": me.json()["data"]["id"], "username": account["username"], "headers": headers}


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
    assert 'class="brand-logo"' in response.text, "顶栏没有用上图标（还是旧的 HNE 方块？）"
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


def test_frontend_files_exist_on_disk() -> None:
    assert (WEB_DIR / "index.html").is_file()
    assert (WEB_DIR / "css" / "styles.css").is_file()
    for name in ("app", "api", "ui"):
        assert (WEB_DIR / "js" / f"{name}.js").is_file()
    for view in ("auth", "cards", "providers", "books", "chat"):
        assert (WEB_DIR / "js" / "views" / f"{view}.js").is_file()


def test_root_mentions_console(client: TestClient) -> None:
    assert client.get("/").json()["console"] == "/console"


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
    match = re.search(
        r'<script type="importmap">(.*?)</script>', html, flags=re.S
    )
    assert match, "入口页缺少 importmap"
    imports = json.loads(match.group(1))["imports"]

    assert set(imports) == {
        "hne/app",
        "hne/api",
        "hne/ui",
        "hne/auth",
        "hne/cards",
        "hne/books",
        "hne/chat",
        "hne/providers",
        "hne/presets",
        # 插件页（第五步新增）。★ 加前端模块时这里也要跟着加 ——
        # 这条断言是"importmap 与模块集合必须逐一对上"的守卫，不是可选项。
        "hne/plugins",
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


def test_every_frontend_module_has_an_importmap_entry() -> None:
    """★ 新增前端模块时，忘了在 importmap 里登记就会白屏 —— 这里守住。

    前端模块之间一律用裸名互导（import { modal } from 'hne/ui'），
    裸名只能靠 importmap 解析。漏登记 = 运行时才炸、页面全白。
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<script type="importmap">(.*?)</script>', html, flags=re.S)
    assert match, "入口页缺少 importmap"
    mapped = set(json.loads(match.group(1))["imports"])

    for path in sorted((WEB_DIR / "js").rglob("*.js")):
        # boot.js 是入口守卫，不参与裸名互导（见文件内说明）
        if path.name == "boot.js":
            continue
        assert f"hne/{path.stem}" in mapped, f"{path.name} 没有登记进 importmap"


def test_frontend_modules_do_not_use_relative_imports() -> None:
    """★ 模块之间不许再用 './xxx.js' / '../xxx.js' 这种相对导入。

    白屏事故的根因就是相对导入：相对 URL 不带版本号，
    浏览器会拿缓存里的旧副本，于是"新文件 import 旧模块的导出"直接炸。
    改用裸名 + importmap 带版本号之后，这条禁令要一直守住。
    """
    offenders: list[str] = []
    pattern = re.compile(r"""from\s+['"](\.{1,2}/[^'"]+)['"]""")
    for path in sorted((WEB_DIR / "js").rglob("*.js")):
        for found in pattern.findall(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name} → {found}")
    assert not offenders, f"这些模块仍在用相对导入: {offenders}"


def test_frontend_version_changes_when_a_frontend_file_changes(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ 改前端文件后版本号必须变，且要经过缓存层生效。

    这是整个缓存击穿机制的发动条件：版本不变，URL 就不变，
    浏览器就会继续用旧副本。所以这里直接对 _frontend_version 做白盒验证。
    """
    from app import main as app_main

    (tmp_path / "js").mkdir()
    target = tmp_path / "js" / "ui.js"
    target.write_text("export const a = 1;", encoding="utf-8")

    first = app_main._frontend_version(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{12}", first)

    # 只改内容、并把 mtime 推后一秒：mtime 精度是秒，不推后可能算出同一个值
    target.write_text("export const a = 2;", encoding="utf-8")
    stat = target.stat()
    os.utime(target, (stat.st_atime, stat.st_mtime + 5))

    assert app_main._frontend_version(tmp_path) != first

    # 缓存层：同一个目录 2 秒内直接复用，不会每次都扫盘
    monkeypatch.setattr(app_main, "_VERSION_CACHE", {})
    a = app_main._web_version_cached(tmp_path)
    b = app_main._web_version_cached(tmp_path)
    assert a == b == app_main._frontend_version(tmp_path)


def test_boot_guard_exists_and_reloads_on_version_mismatch() -> None:
    """★ 自动自愈的守卫脚本必须存在，且关键行为不能被删掉。

    boot.js 是"index.html 自己被缓存住"时的最后一道保险：
    它 no-store 问一次真实版本，发现对不上就自动重载。
    这里用文本断言守住这个行为，避免以后被当成"没用的文件"删掉。
    """
    boot = (WEB_DIR / "js" / "boot.js").read_text(encoding="utf-8")
    assert "cache: 'no-store'" in boot
    assert "X-HNE-Web-Version" in boot
    assert "location.replace" in boot
    assert "data-web-version" in boot or "dataset" in boot


# ==================================================================
#  一之二、★ 前端排查记录：hidden 属性曾经完全不生效
# ==================================================================
def test_stylesheet_makes_hidden_attribute_actually_hide() -> None:
    """★ 回归测试：样式表必须保证 `[hidden]` 真的能隐藏元素。

    ==================== 这条测试守的是什么？====================
    HTML 的 `hidden` 属性是靠浏览器内置样式 `[hidden]{display:none}` 生效的，
    但**作者样式表里的 display 会覆盖它**。

    本项目给 `.reqlog` 与 `.topbar` 都写了 `display: flex`，
    于是 `element.hidden = true` 只是加了个属性、元素照样显示：

        · 请求日志抽屉的「关闭」按钮点了完全没反应（面板从打开那一刻就关不掉）
        · 登录页会错误地多出一条顶部导航栏

    这类问题**在 Python 侧根本测不出来** —— 必须由真实浏览器计算样式才知道。
    所以这里的策略是：用真实浏览器验证过一次之后，
    退而求其次在测试里守住那条 `!important` 兜底规则不被误删。
    """
    css = (WEB_DIR / "css" / "styles.css").read_text(encoding="utf-8")
    # 先去掉注释，避免"规则写在注释里"造成假通过
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert re.search(
        r"\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important", css_no_comments
    ), (
        "styles.css 里缺少 [hidden]{display:none!important} —— "
        "删掉它会让所有用 hidden 属性控制显隐的元素全部失效"
    )


def test_console_panels_start_collapsed(client: TestClient) -> None:
    """请求日志抽屉初始必须是收起的（否则一进页面就挡住右侧内容）。"""
    html = client.get("/console/").text
    aside = re.search(r"<aside[^>]*id=\"reqlog\"[^>]*>", html)
    assert aside is not None, "找不到请求日志抽屉"
    assert "hidden" in aside.group(0), "请求日志抽屉应当是初始收起的（带 hidden 属性）"


def test_console_layout_leaves_room_for_drawer() -> None:
    """★ 抽屉打开时应当**挤窄内容区**而不是浮在内容上面。

    最初是纯浮层，结果把「新建角色卡」这类靠右的按钮遮住了，
    屏幕上只露出一个角，没法点。
    """
    css = (WEB_DIR / "css" / "styles.css").read_text(encoding="utf-8")
    css_no_comments = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    assert "body.log-open" in css_no_comments, "缺少抽屉打开时的布局调整规则"
    assert re.search(
        r"padding-right\s*:\s*var\(--reqlog-w\)", css_no_comments
    ), "抽屉打开时应当给内容区让出面板宽度"


# ==================================================================
#  一之三、★ 前端排查记录：事件监听器泄漏到其它页面
# ==================================================================
def test_view_delegated_listeners_are_abortable() -> None:
    """★ 回归测试：挂在常驻 `#view` 上的委托监听器必须带 `{ signal }`。

    ==================== 这个 bug 有多迷惑 ====================
    `#view` 是**常驻元素**，切页只是替换它内部的内容。
    如果把委托监听器挂在 `#view` 上却不带 signal，它就**永远不会被移除** ——
    访问过的每个页面都会在 `#view` 上留下一个监听器，越积越多。

    真实出现过的症状（每一个都很难联想到"监听器泄漏"）：
      · 在世界书页点卡片，会同时触发之前"角色卡页"留下的监听器，
        它拿着世界书的 id 去请求 `/character-cards/{id}`，
        于是屏幕上瞬间弹出一堆「角色卡不存在」
      · 一个按钮被 N 个监听器各开一次弹窗，用户要关 N 次
      · N 个监听器依次把按钮改为"加载中"，后一个会把前一个的 spinner
        当成原始内容存下来，按钮最后变成空白甚至显示 "undefined"

    修法是给每次导航配一个 AbortController，切页时 `abort()` 一次性摘掉。
    这里用静态检查守住：视图里 `root.addEventListener` 的个数
    不能多于 `{ signal }` 的个数。
    """
    views = sorted((WEB_DIR / "js" / "views").glob("*.js"))
    assert views, "没有找到任何视图文件"

    offenders = []
    for path in views:
        src = path.read_text(encoding="utf-8")
        n_listeners = src.count("root.addEventListener(")
        n_signals = src.count("{ signal }")
        if n_listeners > n_signals:
            offenders.append(f"{path.name}(监听器 {n_listeners} 个，signal {n_signals} 个)")

    assert not offenders, (
        "以下视图的 root.addEventListener 缺少 { signal }，"
        "会导致监听器泄漏到其它页面：" + "；".join(offenders)
    )


def test_view_delegated_actions_are_scoped_to_their_own_view() -> None:
    """★ 回归测试：`#view` 上的委托动作必须**认得出按钮属于哪一页**。

    ==================== 这条测试守的是用户实际复现的那个 bug ====================
    预设页与插件页的卡片都是 `.cc`，删除按钮都叫 `data-act="del"`。
    预设页留下的一批监听器没有被路由信号摘掉（原因见下一条测试），
    于是**在插件页点「删除」会弹两个窗**：一个是插件的、一个是预设的；
    第二个还拿着插件的 id 去删预设，报 404「提示词预设不存在」。

    光靠 signal 是"一处忘了接就全废"的防线，所以再加一条与信号无关的保险：
    每个列表容器带上自己的 `data-view="<页面名>"`，处理函数开头必须
    `btn.closest('[data-view="<页面名>"]')` 认领 —— 认不出来就不管。
    这样"在 A 页点按钮、B 页的动作被执行"在结构上就不可能发生。
    """
    expectations = {
        "plugins.js": "plugins",
        "presets.js": "presets",
        "cards.js": "cards",
        "books.js": "books",
        "providers.js": "providers",
    }
    problems = []
    for filename, view in expectations.items():
        src = (WEB_DIR / "js" / "views" / filename).read_text(encoding="utf-8")
        if f'data-view="{view}"' not in src:
            problems.append(f"{filename} 的列表容器缺少 data-view=\"{view}\"")
        if f"""closest('[data-view="{view}"]')""" not in src:
            problems.append(f"{filename} 的委托处理函数没有做 data-view 归属校验")
    assert not problems, (
        "以下视图缺少「按钮归属校验」，别的页面残留的监听器会打进它们的动作里："
        + "；".join(problems)
    )


def test_presets_view_controllers_are_chained_to_the_route_signal() -> None:
    """★ 回归测试：`nextController` 必须把新控制器接到视图信号上。

    ==================== 这个 bug 的隐蔽之处 ====================
    `nextController(current)` 一开始只做两件事：abort 上一批、返回新控制器。
    看起来"每次都换新信号、旧的立刻失效"，但它**丢掉了与路由信号的连接**：
    切页时 `routeAbort.abort()` 只能摘掉"当初接在路由信号上"的那一批，
    而这个新控制器谁都不认识它 —— 于是它永远不会被 abort，
    留在常驻 `#view` 上的委托监听器就一直活着（症状见上一条测试）。

    这类"接了但没接全"的写法静态很难看出来，所以这里钉住调用形态：
    `nextController(` 一律必须带第二个参数（视图信号）。
    """
    src = (WEB_DIR / "js" / "views" / "presets.js").read_text(encoding="utf-8")
    calls = re.findall(r"nextController\(([^)]*)\)", src)
    # 定义本身（function nextController(current, parent)）不算调用
    calls = [c for c in calls if not c.strip().startswith("current,")]
    assert calls, "没有解析到 nextController 的调用，检查代码是不是改写了"
    bad = [c for c in calls if "," not in c]
    assert not bad, (
        f"nextController({bad[0] if bad else ''}) 没有接视图信号 —— "
        "切页后这批监听器不会被摘掉，会在别的页面被触发（弹两个窗 / 404）"
    )


def test_rich_iframe_document_follows_the_theme() -> None:
    """★ 回归测试：角色卡 HTML 渲染用的 iframe **不能把底色写死成白色**。

    ==================== 用户看到的是"深色主题里一大块白" ====================
    HTML 开场白是塞进 `srcdoc` iframe 里渲染的。iframe 是一份**独立文档**，
    父页面的 CSS 变量不会继承进去；以前那里的 `body{background:#fff}` 是写死的，
    所以插件换肤换到哪都换不掉这一块。

    修法是渲染时从父页面读当前主题变量注入（`richDocHead()`）。
    这里做两个静态检查：① 取色逻辑存在；② 该函数真的被用来拼 srcdoc。
    """
    src = (WEB_DIR / "js" / "ui.js").read_text(encoding="utf-8")
    assert "function richDocHead()" in src, "iframe 文档头必须是按主题动态生成的函数"
    assert "richDocHead() + sanitizeHtml(" in src, "srcdoc 必须用 richDocHead() 拼"
    assert "getComputedStyle(document.documentElement)" in src, (
        "richDocHead() 必须从父页面读当前主题变量"
    )
    head = src.split("function richDocHead()", 1)[1].split("</style>", 1)[0]
    assert "background:#fff" not in head and "background: #fff" not in head, (
        "iframe 文档头里不能再出现写死的白色底 —— 深色主题下它就是那块白"
    )


def test_card_greeting_preview_uses_theme_variables() -> None:
    """★ 回归测试：角色卡列表里的「开场白预览框」不许写死浅色。

    ==================== 用户原话："深色主题下角色卡的白还是在" ====================
    那块白不是背景色，而是 `.cc-greeting` 上的**浅色渐变**：

        background: linear-gradient(180deg, #f7f9fc, #f2f5fa);   /* 写死 */
        color: #4a5568;                                          /* 写死 */

    它躲过了两条防线，值得记下来：
      · 探针只比较 `getComputedStyle().backgroundColor` 的亮度，而渐变画在
        `background-image` 上 —— `backgroundColor` 读出来是**透明**，被当透明块跳过；
      · 采样清单里当时也**没有** `.cc-greeting` 这个选择器。
    """
    css = re.sub(
        r"/\*.*?\*/",
        "",
        (WEB_DIR / "css" / "styles.css").read_text(encoding="utf-8"),
        flags=re.S,
    )
    block = re.search(r"\.cc-greeting\s*\{([^}]*)\}", css)
    assert block is not None, "找不到 .cc-greeting 的样式规则"
    body = block.group(1)
    assert "var(--surface" in body, "开场白预览框的底色必须走 --surface* 变量"
    assert "var(--text-dim)" in body or "var(--text" in body, "字色必须走 --text* 变量"
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", body), (
        "开场白预览框里出现了写死的颜色 —— 深色主题下它就是那块白：\n" + body
    )


def test_no_hardcoded_light_backgrounds_in_stylesheet() -> None:
    """★ 回归测试：`background` 里不许出现**写死的浅色**（走变量才换得掉主题）。

    规则（只查 `:root` 之外的 `background*` 声明）：
      · `#fff` / `#fafbfc` 这类**亮于 200** 的十六进制颜色 → 违规；
      · `rgba(255,255,255,.9)` 这种"高不透明度的白" → 违规；
      · 声明里出现 `var(...)` 就放行（主题换得掉）；
      · `rgba(255,255,255,.08)` 这类**低透明度**的提亮叠加放行（深色导航栏的
        hover 高光就是这个用法，它本来就是"在任意底色上提亮一点"）。

    这条测试比"逐个人工 grep"可靠：人类会漏，而漏掉的表现就是"深色主题下一块白"。
    """
    css = re.sub(
        r"/\*.*?\*/",
        "",
        (WEB_DIR / "css" / "styles.css").read_text(encoding="utf-8"),
        flags=re.S,
    )
    root = re.search(r":root\s*\{.*?\}", css, flags=re.S)
    assert root is not None, "找不到 :root 变量块"
    body = css[root.end() :]

    def lum(r: int, g: int, b: int) -> float:
        return 0.299 * r + 0.587 * g + 0.114 * b

    offenders: list[str] = []
    for m in re.finditer(r"background(?:-image|-color)?\s*:\s*([^;}]+)", body):
        decl = m.group(1)
        if "var(" in decl:
            continue
        for hexcode in re.findall(r"#([0-9a-fA-F]{3,8})\b", decl):
            h = hexcode
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            if len(h) not in (6, 8):
                continue
            r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
            if lum(r, g, b) > 200:
                offenders.append(f"#{hexcode}（亮度 {lum(r, g, b):.0f}）← {decl.strip()[:70]}")
        for rgba in re.findall(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)", decl):
            r, g, b, a = int(rgba[0]), int(rgba[1]), int(rgba[2]), float(rgba[3])
            if a >= 0.5 and min(r, g, b) > 235:
                offenders.append(f"rgba({r},{g},{b},{a}) ← {decl.strip()[:70]}")

    assert not offenders, (
        "styles.css 里有写死的浅色背景 —— 深色主题下它们换不掉，会变成一块白。"
        "请改用 --surface / --surface-2 / --surface-3 之类的变量：\n  " + "\n  ".join(offenders)
    )


def test_alert_borders_use_theme_variables() -> None:
    """★ 回归测试：提示框 / 失败气泡的**边框色**不许写死。

    ==================== 深色主题下它们是五条"亮线" ====================
    `.alert.warn/.danger/.info/.ok` 与 `.msg.failed .msg-body` 的 border-color
    原本写死成四个浅色（`#f0d9a0` / `#f3c2bb` / `#c8d3f7` / `#b7e3cd`）。
    背景色早就走了 `--*-soft` 变量、能跟着主题变，**只有边框留在浅色** ——
    于是深色主题下每个提示框都镶了一圈亮边（横幅、失败回复都中招）。

    ★ 为什么之前的守门人没抓到：它们扫的是 `background*`，而边框是 `border-color`；
      探针采样同样只量背景色。**"只检查背景"的守门人天然看不见边框。**
    """
    css = re.sub(
        r"/\*.*?\*/",
        "",
        (WEB_DIR / "css" / "styles.css").read_text(encoding="utf-8"),
        flags=re.S,
    )
    expectations = {
        ".alert.warn": "--warn-border",
        ".alert.danger": "--danger-border",
        ".alert.info": "--info-border",
        ".alert.ok": "--ok-border",
        ".msg.failed .msg-body": "--danger-border",
    }
    problems = []
    for selector, var in expectations.items():
        m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        if m is None:
            problems.append(f"找不到规则 {selector}")
            continue
        body = m.group(1)
        if f"var({var})" not in body:
            problems.append(f"{selector} 的边框必须用 var({var})，现在是：{body.strip()[:60]}")
        if re.search(r"#[0-9a-fA-F]{3,8}\b", body):
            problems.append(f"{selector} 里还有写死的颜色：{body.strip()[:60]}")
    assert not problems, "提示框边框没走主题变量（深色主题下会是一条亮线）：\n  " + "\n  ".join(
        problems
    )
    # 变量本身必须在 :root 里有浅色默认值，否则浅色下这些框就没边框了
    root = re.search(r":root\s*\{.*?\}", css, flags=re.S)
    assert root is not None
    for var in ("--warn-border", "--danger-border", "--info-border", "--ok-border"):
        assert f"{var}:" in root.group(0), f":root 缺少 {var} 的浅色默认值"


def test_no_hardcoded_light_inline_styles_in_js() -> None:
    """★ 回归测试：JS 里**内联**写死的浅色背景同样要判失败。

    ==================== 为什么单独有这一条 ====================
    上一条只扫 `styles.css`，而代码里还有一类写法能躲过它：

        zone.style.background = '#fafbfc';   // ← 拖拽上传区"回到静止态"

    真实后果：拖过一次文件之后，深色主题下那个上传框**卡在浅色**。
    （同一个函数里的 `hot()` 写的是 `var(--brand-soft)`，只有 `idle()` 漏了 ——
    这类"一半走变量、一半写死"的地方，人工复查最容易看过去。）

    规则与上一条一致：`background*` 里出现亮于 200 的十六进制颜色就判失败，
    声明里含 `var(...)` 则放行。
    """

    def lum(r: int, g: int, b: int) -> float:
        return 0.299 * r + 0.587 * g + 0.114 * b

    offenders: list[str] = []
    for path in sorted((WEB_DIR / "js").rglob("*.js")):
        src = path.read_text(encoding="utf-8")
        # 先去掉注释：注释里为了说明这个坑，会**引用**旧写法（`以前写死了 background:#fff`），
        # 不剥掉就会把"记录 bug 的文档"当成 bug 本身。
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = re.sub(r"//[^\n]*", "", src)
        for m in re.finditer(r"background(?:-color|-image)?\s*[:=]\s*([^;`\n]{0,120})", src):
            decl = m.group(1)
            if "var(" in decl:
                continue
            for hexcode in re.findall(r"#([0-9a-fA-F]{3,8})\b", decl):
                h = hexcode
                if len(h) == 3:
                    h = "".join(c * 2 for c in h)
                if len(h) not in (6, 8):
                    continue
                r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
                if lum(r, g, b) > 200:
                    offenders.append(
                        f"{path.relative_to(WEB_DIR)}: #{hexcode}（亮度 {lum(r, g, b):.0f}）"
                    )

    assert not offenders, (
        "以下 JS 内联样式里写死了浅色背景 —— 深色主题下它们换不掉：\n  "
        + "\n  ".join(offenders)
    )


def test_half_variablized_colors_are_rejected() -> None:
    """★ 回归测试：**底色走变量、文字/边框写死深色** 的配色一律判失败。

    ==================== 这个模式已经踩过三次 ====================
    1. `plugins.js` 的目录头像：
       `style="background:var(--brand-soft);color:#6b46c1"` —— 底色跟着主题变，
       字色没变，深色主题下深紫压在深藏青上**看不清**；
    2. `ui.js` 的拖拽区：`hot()` 用 `var(--brand-soft)`、`idle()` 写 `#fafbfc`
       （这条由上一条"写死浅色背景"守门人抓，不在这里）；
    3. 提示框：背景早就是 `--*-soft` 变量、`border-color` 还是浅色 → 一圈亮边
       （由 `test_alert_borders_use_theme_variables` 抓）。

    共同点：**同一处配色里有一半是变量**。人工复查最容易放过这种地方
    （看着"已经变量化了"），而它会随主题一起坏。

    ★ 规则刻意收得很窄，只抓"**底色是变量 + 文字/边框是写死的深色**"：
      · `background:var(--brand); color:#fff` 不抓（品牌底上的白字本来就该写死）；
      · `background:#1f2933; color:#e6edf3`（代码块这种"永远深色"的部件）不抓；
      · 只有"底色会跟着主题变、而压在上面的浓色不会"才判失败 ——
        深色主题下那就是"看不清"。
    """

    def lum(r: int, g: int, b: int) -> float:
        return 0.299 * r + 0.587 * g + 0.114 * b

    def dark_hexes(decl: str) -> list[str]:
        found = []
        for hexcode in re.findall(r"#([0-9a-fA-F]{3,8})\b", decl):
            h = hexcode
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            if len(h) not in (6, 8):
                continue
            r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
            if lum(r, g, b) < 150:
                found.append(f"#{hexcode}（亮度 {lum(r, g, b):.0f}）")
        return found

    offenders: list[str] = []

    def scan(decl: str, where: str) -> None:
        # 底色必须是变量（否则就是"永远深色"的部件，不属于这个模式）
        if not re.search(r"background(?:-color|-image)?\s*:\s*[^;}]*var\(", decl):
            return
        # 文字色：`color:` 且不是 `border-color` / `background-color` 的一部分
        for m in re.finditer(r"(?<![-\w])color\s*:\s*([^;}]+)", decl):
            for hit in dark_hexes(m.group(1)):
                offenders.append(f"{where}: 文字色 {hit}（底色是变量）")
        for m in re.finditer(r"border(?:-[a-z]+)?-color\s*:\s*([^;}]+)", decl):
            for hit in dark_hexes(m.group(1)):
                offenders.append(f"{where}: 边框色 {hit}（底色是变量）")

    # ① CSS：逐条规则体
    css = re.sub(
        r"/\*.*?\*/",
        "",
        (WEB_DIR / "css" / "styles.css").read_text(encoding="utf-8"),
        flags=re.S,
    )
    root = re.search(r":root\s*\{.*?\}", css, flags=re.S)
    body = css[root.end() :] if root else css
    for m in re.finditer(r"\{([^{}]*)\}", body):
        scan(m.group(1), "styles.css")

    # ② JS：内联 style 属性 / .style.xxx = '...' 赋值
    for path in sorted((WEB_DIR / "js").rglob("*.js")):
        src = path.read_text(encoding="utf-8")
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = re.sub(r"//[^\n]*", "", src)
        rel = path.relative_to(WEB_DIR).as_posix()
        for m in re.finditer(r"style\s*=\s*[\"']([^\"']*)[\"']", src):
            scan(m.group(1), rel)
        for m in re.finditer(r"\.style\.[A-Za-z]+\s*=\s*['\"]([^'\"]*)['\"]", src):
            scan(m.group(1), rel)

    assert not offenders, (
        "以下地方是「底色走变量、文字/边框写死深色」的配色 —— 深色主题下会看不清：\n  "
        + "\n  ".join(offenders)
    )


def test_chat_view_buttons_match_their_delegated_actions() -> None:
    """★ 回归测试：视图里判断的每个 data-act，都必须真的存在这样一个按钮。

    ==================== 这条测试守的是真实踩到的 bug ====================
    对话界面的「发送」按钮最初忘了写 `data-act="send"`：

        <button class="btn" id="btn-send" disabled>发送</button>

    而处理点击的委托监听器是这么分发的：

        const btn = e.target.closest('button[data-act]');
        if (act === 'send') return sendMessage(...);

    于是点击「发送」**毫无反应，而且控制台一条报错都没有** ——
    静态看代码很难发现，pytest 也测不到，只有真实浏览器里点一下才暴露。
    （顺带一提：`closest()` 还会因此向上找到工具栏的「查看提示词」按钮，
    把它当成被点的按钮，行为更加莫名其妙。）

    这里用静态检查守住「判断的 act」与「声明的 act」一致，
    避免以后再犯 —— 这类低级错误一旦漏到用户手里，用户是没法继续测试的。
    """
    src = (WEB_DIR / "js" / "views" / "chat.js").read_text(encoding="utf-8")

    handled = {
        m.group(1)
        for m in re.finditer(r"act === '([a-z-]+)'", src)
    } | {
        # switch 形态写成 case 'xxx': 的也一起收进来
        m.group(1)
        for m in re.finditer(r"e\.target\.dataset\.act === '([a-z-]+)'", src)
    }
    declared = set(re.findall(r'data-act="([a-z-]+)"', src))

    assert handled, "没有从 chat.js 里解析出任何 data-act 判断，检查代码是不是改写了"
    missing = sorted(handled - declared)
    assert not missing, (
        f"chat.js 里判断了这些 data-act 却找不到对应的按钮：{missing} —— "
        "按钮少了 data-act 属性时，点击会**毫无反应且不报错**"
    )


def test_button_loading_guards_against_double_restore() -> None:
    """★ 回归测试：`buttonLoading` 被连续调用两次时，恢复不能把内容设成 `undefined`。

    真实踩到过：两个恢复函数先后执行，第二个读到的是**已被删除的** dataset 值，
    于是执行了 `btn.innerHTML = undefined` ——
    浏览器会把它转成字面量字符串，按钮上真的显示出了 `undefined` 七个字母。
    """
    src = (WEB_DIR / "js" / "ui.js").read_text(encoding="utf-8")
    match = re.search(r"export function buttonLoading[\s\S]*?\n}", src)
    assert match, "ui.js 里找不到 buttonLoading"

    body = match.group(0)
    assert "btn.dataset.originalHtml" in body, "原始内容应当记在元素上，才能抵御重复调用"
    assert re.search(
        r"if \(original === undefined\) return;", body
    ), "恢复函数必须先判断原始内容是否已被别的调用消费掉，否则会把 innerHTML 设成 undefined"


# ==================================================================
#  二、★ 前端真实请求形状能否被后端接受
# ==================================================================
def test_provider_payload_from_console_form(client: TestClient, user: dict) -> None:
    """照抄 providers.js 里 collect() 拼出来的形状。

    ★ 前端在既有字段之外还多带了 is_default / is_active / clear_api_key，
      必须确认后端不会因此报 422。
    """
    payload = {
        "name": f"控制台配置_{uuid.uuid4().hex[:6]}",
        "provider_type": "openai_compatible",
        "base_url": "https://api.deepseek.com",
        "model_name": "deepseek-flash",
        "context_window": 65536,
        "generation": {
            "temperature": 0.8,
            "max_tokens": 2048,
            "reasoning_effort": "auto",
        },
        "api_key": "sk-console-shape-test",
        "is_default": True,
        "is_active": True,
    }
    response = client.post("/api/v1/providers", json=payload, headers=user["headers"])
    assert response.status_code == 201, response.text


def test_provider_draft_test_payload(client: TestClient, user: dict) -> None:
    """「先测再存」那条路径：前端把表单内容直接发给 /providers/test-draft。

    前端编辑既有配置时也会带 clear_api_key，这里一并确认不会炸。
    """
    payload = {
        "name": "draft",
        "provider_type": "openai_compatible",
        "base_url": "https://this-host-does-not-exist-xyz.invalid",
        "model_name": "deepseek-flash",
        "context_window": 65536,
        "generation": {"temperature": 0.8, "max_tokens": 2048},
        "api_key": "",
        "is_default": False,
        "is_active": True,
        "clear_api_key": True,
    }
    response = client.post(
        "/api/v1/providers/test-draft", json=payload, headers=user["headers"]
    )
    # 域名不存在 -> 连通失败，但接口本身应当是 200 + ok=false（不是 422/500）
    assert response.status_code == 200, response.text
    assert response.json()["data"]["ok"] is False


def test_card_payload_from_console_form(client: TestClient, user: dict) -> None:
    """照抄 cards.js 里角色卡表单拼出来的形状（大量字段为 null）。"""
    payload = {
        "name": f"控制台角色_{uuid.uuid4().hex[:6]}",
        "description": None,
        "personality": "冷静",
        "background": None,
        "speaking_style": None,
        "scenario": None,
        "example_dialogue": None,
        "greeting": "你好，旅人。",
        "system_prompt": None,
        "post_history_instructions": None,
        "avatar_url": None,
        "tags": ["奇幻"],
        "alternate_greetings": ["另一个开头"],
        "is_public": False,
        "world_book_id": None,
    }
    response = client.post(
        "/api/v1/character-cards", json=payload, headers=user["headers"]
    )
    assert response.status_code == 201, response.text


def test_card_patch_payload_from_console_form(
    client: TestClient, user: dict
) -> None:
    """★ 前端编辑表单是**全量提交**（所有字段都带上，空的给 null）。

    对 PATCH 来说这意味着「把没填的字段清空」，是符合用户直觉的
    （表单里清空了，保存后就该是空的）。这里确认这种全量 PATCH 不会出问题。
    """
    created = client.post(
        "/api/v1/character-cards",
        json={"name": f"待改_{uuid.uuid4().hex[:6]}", "personality": "旧性格"},
        headers=user["headers"],
    ).json()["data"]

    full_patch = {
        "name": created["name"],
        "description": None,
        "personality": "新性格",
        "background": None,
        "speaking_style": None,
        "scenario": None,
        "example_dialogue": None,
        "greeting": None,
        "system_prompt": None,
        "post_history_instructions": None,
        "avatar_url": None,
        "tags": [],
        "alternate_greetings": [],
        "is_public": False,
        "world_book_id": None,
    }
    response = client.patch(
        f"/api/v1/character-cards/{created['id']}",
        json=full_patch,
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["personality"] == "新性格"
    assert data["tags"] == []
    # ★ 注意：详情响应里没有 world_book_id，而是 world_book 这个引用对象
    #   （前端编辑表单就是读 existing.world_book?.id 来回填下拉框的）
    assert data["world_book"] is None


def test_book_payload_from_console_editor(client: TestClient, user: dict) -> None:
    """照抄 books.js 条目编辑器拼出来的形状。

    ★ 编辑器会把条目的原始对象整体回传（含我们不认识的字段），
      这里确认这些"陌生字段"不会导致 422，并且会被保留。
    """
    payload = {
        "name": f"控制台世界书_{uuid.uuid4().hex[:6]}",
        "description": None,
        "entries": [
            {
                "keys": ["龙", "巨龙"],
                "content": "世上最后一条龙",
                "enabled": True,
                "insertion_order": 0,
                "extensions": {},
                # 编辑器原样带回来的陌生字段
                "position": "before_char",
                "priority": 7,
                "selective": False,
            }
        ],
    }
    response = client.post(
        "/api/v1/world-books", json=payload, headers=user["headers"]
    )
    assert response.status_code == 201, response.text

    entry = response.json()["data"]["entries"][0]
    assert entry["position"] == "before_char"
    assert entry["priority"] == 7


def test_book_patch_payload_with_null_name(client: TestClient, user: dict) -> None:
    """★ 编辑器保存时，name 为空会传 null。

    后端把「必填字段传 null」解释为「不修改」而不是「清空」——
    否则用户只是没改名字，一保存名字就没了。
    """
    created = client.post(
        "/api/v1/world-books",
        json={"name": f"原名_{uuid.uuid4().hex[:6]}", "entries": []},
        headers=user["headers"],
    ).json()["data"]

    response = client.patch(
        f"/api/v1/world-books/{created['id']}",
        json={"name": None, "description": None, "entries": []},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["name"] == created["name"]


def test_delete_with_console_query_params(client: TestClient, user: dict) -> None:
    """照抄 cards.js 删除弹窗最终发出的查询参数组合。"""
    created = client.post(
        "/api/v1/character-cards",
        json={"name": f"待删_{uuid.uuid4().hex[:6]}"},
        headers=user["headers"],
    ).json()["data"]

    response = client.delete(
        f"/api/v1/character-cards/{created['id']}",
        params={"force": "true", "delete_sessions": "false", "delete_world_book": "false"},
        headers=user["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["deleted_sessions"] == 0


def test_console_does_not_require_login(client: TestClient) -> None:
    """控制台页面本身不需要登录（登录是页面内部的事）。"""
    assert client.get("/console/").status_code == 200


# ==================================================================
#  三、★ 对话界面（3.8）真实请求形状
# ==================================================================
def test_chat_create_session_payload_from_console(
    client: TestClient, user: dict
) -> None:
    """照抄 chat.js 里 openCreateDialog 拼出来的建会话请求。

    ★ 这里刻意带上 `title: null`：前端的输入框留空时就是传 null，
      后端必须把"没填标题"理解成"用默认标题"，而不是把标题存成字符串 "null"。
    """
    card = client.post(
        "/api/v1/character-cards",
        json={"name": f"对话卡_{uuid.uuid4().hex[:6]}", "greeting": "……你来了。"},
        headers=user["headers"],
    ).json()["data"]
    provider = client.post(
        "/api/v1/providers",
        json={
            "name": f"chat_{uuid.uuid4().hex[:6]}",
            "provider_type": "openai_compatible",
            "base_url": "https://mock.invalid/v1",
            "api_key": "sk-chat-shape",
            "model_name": "mock-model",
            "context_window": 8192,
            "generation": {"temperature": 0.8, "max_tokens": 1024},
        },
        headers=user["headers"],
    ).json()["data"]

    response = client.post(
        "/api/v1/narrative/sessions",
        json={
            "character_card_id": card["id"],
            "llm_provider_id": provider["id"],
            "title": None,
        },
        headers=user["headers"],
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["title"], "标题留空时必须给一个默认标题"
    assert data["messages"][0]["content"] == "……你来了。"


def test_chat_stream_endpoint_shape(client: TestClient, user: dict) -> None:
    """★ 前端用 fetch 读这条流，所以响应头与事件名必须严格对上。

    这里刻意用**一个模型配置齐全的会话**，并把适配器换成假的
    （与 test_narrative.py 同样的做法，绝不联网），
    这样能断言到真正的事件序列：meta → delta… → done → end。
    """
    import app.narrative.engine as narrative_engine
    from app.llm.schema import StreamChunk, TokenUsage

    class FakeAdapter:
        def __init__(self, *_, **__):
            pass

        @property
        def budget(self):
            from app.llm.params import compute_context_budget

            return compute_context_budget(8192, 1024)

        def stream_chat(self, request):
            yield StreamChunk(delta="你")
            yield StreamChunk(delta="好")
            yield StreamChunk(finish_reason="stop", usage=TokenUsage(total_tokens=5))

        def close(self):
            pass

    original = narrative_engine.build_adapter
    narrative_engine.build_adapter = lambda row: FakeAdapter()
    try:
        card = client.post(
            "/api/v1/character-cards",
            json={"name": f"流卡_{uuid.uuid4().hex[:6]}", "greeting": "……"},
            headers=user["headers"],
        ).json()["data"]
        provider = client.post(
            "/api/v1/providers",
            json={
                "name": f"stream_{uuid.uuid4().hex[:6]}",
                "provider_type": "openai_compatible",
                "base_url": "https://mock.invalid/v1",
                "api_key": "sk-stream-shape",
                "model_name": "mock-model",
                "context_window": 8192,
                "generation": {"temperature": 0.8, "max_tokens": 1024},
            },
            headers=user["headers"],
        ).json()["data"]
        session_id = client.post(
            "/api/v1/narrative/sessions",
            json={"character_card_id": card["id"], "llm_provider_id": provider["id"]},
            headers=user["headers"],
        ).json()["data"]["id"]

        response = client.get(
            f"/api/v1/narrative/sessions/{session_id}/stream",
            params={"content": "你好"},
            headers=user["headers"],
        )
    finally:
        narrative_engine.build_adapter = original

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # 前端 chat.js 依赖这两个头（不缓冲 / 允许流式）
    assert "no-cache" in response.headers.get("cache-control", "")
    assert response.headers.get("x-accel-buffering") == "no"
    # ★ 事件名必须与 chat.js 的 consumeStream() 里判断的那几个完全一致
    for name in ("event: meta", "event: delta", "event: done", "event: end"):
        assert name in response.text, f"缺少 {name} 事件"
    assert response.text.rstrip().endswith("event: end\ndata: {}")
