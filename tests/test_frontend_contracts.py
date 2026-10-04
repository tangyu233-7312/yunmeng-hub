"""Web 前端源码的静态契约测试（纯读文件，不需要浏览器、不需要后端）。

==================== 为什么把这类断言单独放一个文件 ====================
前端的 bug 有两类：
  · 逻辑错（要靠探针/浏览器才能发现）；
  · **接线错**（改了 A 忘了改 B、写了个永远不会生效的分支）——
    这一类用"读源码 + 断言关键片段"就能抓住，而且跑得飞快。

本项目已经吃过好几次"接线只接一半"的亏（`chooseBackend` 的 explicit、
`switchMode` 的作用域、`backend_entry` 忽略 `HNE_ENV_FILE`），
所以这里把 web/ 侧的关键接线也钉住。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AUTH_JS = REPO / "web" / "js" / "views" / "auth.js"
STYLES = REPO / "web" / "css" / "styles.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_login_page_tells_which_storage_is_in_use() -> None:
    """★ 登录页必须显示"当前连的是哪个库"。

    ==================== 这条守着一个真实的误判 ====================
    用户切换存储方式后拿原账号登录，界面只回"用户名或密码错误" ——
    他的第一反应是"我的账号被人改了吗？"
    而真相是：两种存储是**两个互相独立的库**，连账号都不互通，
    切过去就得在新库里重新注册。这件事必须写在**用户看得到的地方**。
    """
    src = _read(AUTH_JS)
    assert "auth-storage-note" in src, "登录页要有那块提示的容器"
    # 必须真的去问后端"现在用的是哪个后端"，而不是写死一句文案
    assert "/health" in src, "要读 /health 才知道当前存储方式"
    assert "components" in src and "database" in src and "backend" in src, (
        "要从 /health 的 components.database.detail.backend 取后端类型"
    )
    # 两种存储都要有可读的名字，并且**明确写出"不互通"**
    assert "MySQL 数据库" in src and "本机文件（SQLite）" in src
    assert "不互通" in src, "必须明确说清两边的数据与账号都不互通"
    assert "切换存储方式" in src, "要告诉用户怎么切回去"


def test_login_page_storage_hint_never_blocks_login() -> None:
    """★ 提示是"锦上添花"，探不到就静默隐藏 —— 绝不能影响登录本身。

    判据：那段逻辑必须有 try/catch，且 catch 里不弹错误。
    ★ 锚点要选**代码**里的那段（`fetch('/health'`），而不是模板里的容器 id ——
      后者在 HTML 里先出现一次，从那里切片会把逻辑段切掉一半（第一版就这么错了）。
    """
    src = _read(AUTH_JS)
    # ★ 从 IIFE 开头切到下一个函数边界（`tabs.forEach`）—— 锚点要包含 `try {`。
    #   第一版从 `fetch('/health'` 开始切，而 `try {` 在它**之前**，于是假失败。
    start = src.index("(async () => {\n    const box = $('#auth-storage-note', root);")
    end = src.index("tabs.forEach", start)
    block = src[start:end]
    assert "try {" in block and "catch" in block, "取 /health 必须包在 try/catch 里"
    # catch 里不许 toastErr（那会在登录页弹一个与登录无关的红条）
    catch_part = block[block.index("catch"):]
    assert "toastErr" not in catch_part, "探活失败不该弹错误提示去打扰用户"


def test_storage_hint_has_styles() -> None:
    """提示块要有样式，否则看起来像一段漏出来的裸文本。"""
    css = _read(STYLES)
    assert ".auth-storage-note" in css, "提示容器要有样式"
    assert ".auth-storage-hint" in css, "提示正文要有样式（小一号、压暗）"


def test_no_leftover_debug_artifacts_in_frontend() -> None:
    """前端源码里不该留下 `console.log` 调试残留（本项目零构建，日志会直接进用户控制台）。"""
    offenders: list[str] = []
    for path in (REPO / "web" / "js").rglob("*.js"):
        text = _read(path)
        for idx, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            # 允许注释里出现这个词（说明性文字），只挑真正的调用
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if "console.log(" in stripped:
                offenders.append(f"{path.relative_to(REPO)}:{idx}")
    assert not offenders, f"前端有 console.log 残留：{offenders}"
