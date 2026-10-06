"""真正的浏览器端到端探针：用 CDP 驱动无头 Edge **点一遍**可视化控制台。

==================== 它解决什么问题？====================
pytest 用的是进程内 TestClient，`smoke_test.py` 走的是真实 HTTP ——
但两者都**碰不到 CSS 与 JS**。本项目 4 个最隐蔽的 bug（监听器泄漏、
`[hidden]` 被作者样式覆盖、按钮缺 `data-act`、加载态吞掉文案）全都是
"pytest 全绿但界面点不动"，只能靠真实浏览器发现。

所以这个脚本做三件事：
  1. 造一套测试数据（假模型 + 角色卡 + 世界书 + 会话）
  2. 启动**本机假模型服务**（不联网、不花钱、逐字吐字）
  3. 用 CDP 驱动无头 Edge 真实点击，逐项断言并把结果写进报告

==================== 用法 ====================
    # 1) 先把后端跑起来
    .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
    # 2) 再跑探针（脚本自己管假模型服务与测试数据）
    .\\.venv\\Scripts\\python.exe scripts\\ui_probe.py

可选参数：
    --headed      不用无头模式（想亲眼看它点的时候用）
    --keep        跑完不清测试账号（方便自己去数据库里翻）
    --report PATH 报告输出路径（默认 data/ui_probe_report.json）

退出码：0 = 全部通过，2 = 有失败项，1 = 环境问题（连不上服务/浏览器）。

==================== ★ 两条必须记住的经验 ====================
1. **localStorage 是按「源」隔离的**：在 about:blank 上写 token，
   导航到 127.0.0.1 之后**读不到**（表现是顶栏不出现、请求全 401）。
   正确顺序：先导航到目标源 → 再写 localStorage → 再重载。
2. **不能用 --virtual-time-budget 判断"文字是否逐字出现"**：
   它会把等待网络的虚拟时钟跑得比真实时间快，结论一定是错的。
   判断流式必须用 CDP 真实采样（本脚本的「流式逐字」一项就是这么做的）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

#: 测试账号前缀必须是 cleanup_demo_data.py 认得的（ui_ 已在清单里）
PROBE_USER = "ui_probe_browser"
PROBE_PWD = "Passw0rd!123"
#: 第二个账号：专门用来放一张"别人公开的卡"。
#   scope=public 的语义是"别人公开的卡"，没有这个账号，公共卡库永远是空的，
#   "切筛选之后按钮失效"那个 bug 就永远测不到。
PROBE_OTHER = "ui_probe_other"
FAKE_MODEL_PORT = 8123
CDP_PORT = 9333


class Probe:
    """收集断言结果的小工具。"""

    def __init__(self) -> None:
        self.results: list[dict] = []
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []

    def check(self, step: str, name: str, ok: bool, detail=None) -> None:
        self.results.append(
            {"step": step, "check": name, "ok": bool(ok), "detail": detail}
        )

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.results if not r["ok"]]


# ==================================================================
#  测试数据
# ==================================================================
def _ensure_other_user_with_public_card(c, api: str, username: str, card_name: str) -> None:
    """保证存在「另一个账号 + 它公开的一张卡」，并做成幂等的。

    ★ 为什么需要另一个账号：`scope=public` 查的是**别人**公开的卡
      （`user_id != 我`），所以自己公开的卡不会出现在公共卡库里。
      没有这张卡，「公共卡库」永远是空态 —— 而"切过筛选之后卡片按钮全部失效"
      这个真实 bug 恰好只在**列表里有卡片**时才暴露，等于测了个寂寞。

    幂等做法：这个账号只属于探针，先把它整个删掉（连带卡片）再重建。
    """
    pwd = "Test-Passw0rd!"
    c.post(
        f"{api}/auth/register",
        json={"username": username, "email": f"{username}@example.com", "password": pwd},
    )
    login = c.post(f"{api}/auth/login", json={"username": username, "password": pwd})
    login.raise_for_status()
    h = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    # 清掉上一次留下的卡（反复跑也不会越堆越多）
    got = c.get(f"{api}/character-cards", headers=h, params={"limit": 100, "scope": "mine"})
    if got.status_code == 200:
        items = got.json()["data"].get("items", [])
        for it in items:
            c.delete(f"{api}/character-cards/{it['id']}", headers=h, params={"force": "true"})

    card = c.post(
        f"{api}/character-cards",
        headers=h,
        json={
            "name": card_name,
            "description": "用来验证公共卡库筛选之后的交互",
            "greeting": "（她把书合上）你来得正好。",
            "is_public": True,
        },
    )
    card.raise_for_status()


def seed(base: str) -> dict:
    """注册/登录测试账号，并造齐模型配置、角色卡、世界书、会话。

    ★ 反复跑这个脚本要幂等：先清掉上一次留下的会话/卡/书/配置，再重建。
    """
    api = f"{base}/api/v1"
    with httpx.Client(timeout=30.0) as c:
        c.post(
            f"{api}/auth/register",
            json={"username": PROBE_USER, "email": f"{PROBE_USER}@example.com", "password": PROBE_PWD},
        )
        # 注册成功 / 用户名已存在 都不代表"现在一定登得上"，统一再登录一次
        login = c.post(f"{api}/auth/login", json={"username": PROBE_USER, "password": PROBE_PWD})
        login.raise_for_status()
        token = login.json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {token}"}

        me = c.get(f"{api}/auth/me", headers=h).json()["data"]

        # 清场（注意 scope/force 参数，否则私有资源删不掉）
        for path, params in (
            ("narrative/sessions", {"limit": 100}),
            ("character-cards", {"limit": 100, "scope": "mine"}),
            ("world-books", {"limit": 100, "force": "true"}),
            # ★ 插件**不在这里整体清空**：默认插件是"只发一次"的（users.plugin_defaults_seeded
            #   是墓碑位），清空之后不会重建，反而让"默认两个空壳"这条断言永远不成立。
            #   探针自己造的那些在插件那一节末尾按名字单独删。
            # ★ 只删探针自己造的配置（名字带「探针」前缀），
            #   绝不能顺手删用户自己配的真实模型！
            ("providers", {"limit": 100}),
        ):
            got = c.get(f"{api}/{path}", headers=h, params=params or {"limit": 100})
            if got.status_code != 200:
                continue
            data = got.json()["data"]
            items = data.get("items", data) if isinstance(data, dict) else data
            for it in items or []:
                if path == "providers" and "探针" not in str(it.get("name", "")):
                    continue
                extra = {"force": "true"} if path in ("character-cards", "world-books") else None
                c.delete(f"{api}/{path}/{it['id']}", headers=h, params=extra)

        provider = c.post(
            f"{api}/providers",
            headers=h,
            json={
                "name": "探针假模型",
                "provider_type": "openai_compatible",
                "base_url": f"http://127.0.0.1:{FAKE_MODEL_PORT}/v1",
                "api_key": "",
                "model_name": "fake-model",
                "context_window": 8192,
                "is_default": True,
            },
        )
        provider.raise_for_status()
        provider_id = provider.json()["data"]["id"]

        book = c.post(
            f"{api}/world-books",
            headers=h,
            json={
                "name": "探针世界书",
                "description": "浏览器探针用",
                "scan_depth": 8,
                "token_budget": 1024,
                "entries": [
                    {
                        "keys": ["灯塔"],
                        "content": "北岸的灯塔已熄灭一百年，只有守塔人的后裔还记得怎么点亮它。",
                        "enabled": True,
                        "insertion_order": 0,
                    }
                ],
            },
        )
        book.raise_for_status()
        book_id = book.json()["data"]["id"]

        card = c.post(
            f"{api}/character-cards",
            headers=h,
            json={
                "name": "探针角色",
                "description": "浏览器探针用角色",
                "personality": "沉默寡言但可靠",
                "scenario": "海边的旧灯塔",
                "greeting": "（她抬头看了你一眼）……你也听见钟声了吗？",
                "world_book_id": book_id,
                "is_public": False,
                # ★ 状态栏字段是**卡自己声明的**（不再是代码写死的一套）。
                #   探针 3.1 那一节要验"HP 越界被夹住"，所以这张卡必须声明 hp；
                #   没有这一步，那张卡就是"未定义状态栏"，状态栏里根本没有 HP。
                "extensions": {
                    "hne": {
                        "state_schema": [
                            {"name": "hp", "label": "HP", "type": "meter", "max_field": "max"},
                            {"name": "location", "label": "位置", "type": "text"},
                            {"name": "inventory", "label": "背包", "type": "list"},
                        ],
                        "initial_state": {"hp": 100, "max": 100, "location": "灯塔一层"},
                    }
                },
            },
        )
        card.raise_for_status()
        card_id = card.json()["data"]["id"]

        # ★ 还要造一张**别人公开的**卡，公共卡库才有东西可点。
        #   注意 scope=public 的语义是"别人公开的卡"（`user_id != 我`），
        #   所以自己公开的卡不会出现在公共卡库里 —— 必须另开一个账号来放这张卡。
        _ensure_other_user_with_public_card(c, api, PROBE_OTHER, "探针公开卡")

        session = c.post(
            f"{api}/narrative/sessions",
            headers=h,
            json={"character_card_id": card_id, "llm_provider_id": provider_id, "title": "探针会话"},
        )
        session.raise_for_status()

    return {
        "token": token,
        "user": me,
        "provider_id": provider_id,
        "card_id": card_id,
        "book_id": book_id,
        "session_id": session.json()["data"]["id"],
    }


# ==================================================================
#  CDP 小封装
# ==================================================================
class ModuleLoadFailure(RuntimeError):
    """前端模块加载失败时立即中止后续检查（见 run_checks 里的说明）。"""


class Cdp:
    def __init__(self, ws) -> None:
        self.ws = ws
        self._id = 0
        self.probe: Probe | None = None

    def send(self, method: str, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=90))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method} 失败：{msg['error']}")
                return msg.get("result", {})
            self._on_event(msg)

    def _on_event(self, msg: dict) -> None:
        if self.probe is None:
            return
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning"):
            text = " ".join(
                str(a.get("value", a.get("description", a.get("type"))))
                for a in params.get("args", [])
            )
            self.probe.console_errors.append(f"[{params.get('type')}] {text}")
        elif method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails", {})
            self.probe.page_errors.append(
                d.get("exception", {}).get("description") or d.get("text") or str(d)
            )
        elif method == "Log.entryAdded":
            entry = params.get("entry", {})
            if entry.get("level") in ("error", "warning"):
                # 带上 url：否则只看到一句"Failed to load resource"根本定位不到是哪个请求
                where = entry.get("url") or ""
                self.probe.console_errors.append(
                    f"[log:{entry.get('level')}] {entry.get('text')} {where}".strip()
                )

    def eval(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            return {
                "__error__": str(
                    r["exceptionDetails"].get("exception", {}).get("description")
                )
            }
        return r.get("result", {}).get("value")

    def num(self, expr: str, default: float = 0) -> float:
        """安全地取一个数字。

        ★ 为什么需要：`eval` 在页面里抛异常时返回 `{"__error__": …}`（dict），
          于是 `abs(a - b)` 这类算术会抛 TypeError，把**整个探针**打断 ——
          前面已通过的检查连报告都写不出来。
          探针应当"跑完并给出完整结论"，所以凡是要参与计算的取值都走这里：
          出错就退回 default（断言会失败，但不会中断运行）。
        """
        value = self.eval(expr)
        if isinstance(value, dict) or value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def text(self, expr: str, default: str = "") -> str:
        """安全地取一段文本（出错时把错误信息当文本返回，便于定位）。"""
        value = self.eval(expr)
        if isinstance(value, dict):
            return f"<取值失败：{value.get('__error__', value)}>"
        return default if value is None else str(value)

    def wait_for(self, expr: str, seconds: float = 20.0) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.eval(expr) is True:
                return True
            time.sleep(0.25)
        return False


# ==================================================================
#  主流程
# ==================================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="浏览器端到端探针（真实点击）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--headed", action="store_true", help="不用无头模式")
    parser.add_argument("--keep", action="store_true", help="跑完不清理测试账号")
    parser.add_argument("--report", default="data/ui_probe_report.json")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    edge = next((p for p in EDGE_CANDIDATES if Path(p).exists()), None)
    if edge is None:
        print("找不到 Edge / Chrome，无法做浏览器验证")
        return 1

    try:
        httpx.get(f"{base}/health", timeout=10).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"服务没起来（{base}/health 不通）：{exc}")
        print("请先运行：.\\\\.venv\\\\Scripts\\\\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000")
        return 1

    probe = Probe()

    # ---------- 假模型服务（逐字吐字，延迟调小一点让探针跑得快） ----------
    fake = subprocess.Popen(
        [
            str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
            str(PROJECT_ROOT / "scripts" / "fake_openai_server.py"),
            "--port",
            str(FAKE_MODEL_PORT),
            "--delay",
            "0.03",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(PROJECT_ROOT),
    )
    time.sleep(2.0)

    profile = Path.home() / "AppData" / "Local" / "Temp" / "hne_ui_probe"
    if profile.exists():
        shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)

    browser_args = [
        edge,
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        # ★ 固定成桌面尺寸。不指定的话无头窗口默认只有 800px 宽，
        #   会命中 CSS 里 `max-width: 900px` 的移动端断点 ——
        #   于是"对话区两栏 / 可拖动调节大小"这类只在桌面生效的功能根本测不到
        #   （第一次这么写就踩了：手柄在窄屏是隐藏的，断言直接不成立）。
        "--window-size=1360,1000",
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={profile}",
        f"{base}/console/",
    ]
    if not args.headed:
        browser_args.insert(1, "--headless=new")
    browser = subprocess.Popen(
        browser_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    ws_url = None
    for _ in range(60):
        time.sleep(0.5)
        try:
            pages = [
                t
                for t in httpx.get(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=3).json()
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            ]
        except Exception:  # noqa: BLE001
            continue
        if pages:
            ws_url = pages[0]["webSocketDebuggerUrl"]
            break

    if ws_url is None:
        print("连不上无头浏览器的调试端口")
        browser.kill()
        fake.kill()
        return 1

    data = seed(base)
    session_id = data["session_id"]

    try:
        with connect(ws_url, max_size=48 * 1024 * 1024) as ws:
            cdp = Cdp(ws)
            cdp.probe = probe
            cdp.send("Runtime.enable")
            cdp.send("Log.enable")
            cdp.send("Page.enable")
            try:
                run_checks(cdp, base, data)
            except ModuleLoadFailure as exc:
                # 模块加载失败 → 剩下的检查全部无意义，但报告仍要写出来，
                # 让"到底是哪个模块没加载"这一条信息能到达人眼前。
                print(f"!! 前端模块加载失败，已中止后续检查：{exc}")

            # 截图留档，方便人工复核视觉效果（视口固定成桌面尺寸）
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                width=1360,
                height=1000,
                deviceScaleFactor=1,
                mobile=False,
            )
            data_dir = PROJECT_ROOT / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            def shoot(name: str, doc_name: str | None = None) -> None:
                time.sleep(0.6)
                shot = cdp.send("Page.captureScreenshot", format="png")
                raw = base64.b64decode(shot["data"])
                (data_dir / name).write_bytes(raw)
                # ★ README 与论文素材引用的是 `docs/` 下那几张。
                #   以前只写 data/，于是文档里的截图停在几个版本以前 —— "不会过期"
                #   那句话是假的。这里顺手一起刷新，让它变成真的。
                if doc_name:
                    docs_dir = PROJECT_ROOT / "docs"
                    docs_dir.mkdir(parents=True, exist_ok=True)
                    (docs_dir / doc_name).write_bytes(raw)

            shoot("ui_probe_screenshot.png", "console-screenshot.png")

            # 角色卡页也留一张：卡片外观和"切筛选之后按钮还能不能用"都靠它人工复核
            cdp.eval("document.querySelector('#nav [data-route=\"cards\"]').click(); 'ok'")
            cdp.wait_for("!!document.querySelector('#scope-tabs')", 15)
            cdp.eval("document.querySelector('#scope-tabs [data-scope=\"public\"]').click(); 'ok'")
            cdp.wait_for("!!document.querySelector('.cc')", 20)
            shoot("ui_probe_cards.png", "cards-screenshot.png")

            # 角色卡「详情」：开场白是 HTML 卡片时要能看到渲染视图（用户反馈过这一点）
            cdp.eval("document.querySelector('#scope-tabs [data-scope=\"mine\"]').click(); 'ok'")
            cdp.wait_for("!!document.querySelector('.cc [data-act=\"view\"]')", 20)
            cdp.eval("document.querySelector('.cc [data-act=\"view\"]').click(); 'ok'")
            cdp.wait_for("!!document.querySelector('#modal-root .modal')", 20)
            shoot("ui_probe_card_detail.png", "card-detail-screenshot.png")
            cdp.eval(
                "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
                "   .forEach(b => b.click()); return 'ok'; })()"
            )
            time.sleep(0.4)

            # 提示词预设页：块清单 / 注入方式 / 深度这几列是这次新加的核心界面
            cdp.eval("document.querySelector('#nav [data-route=\"presets\"]').click(); 'ok'")
            cdp.wait_for("!!document.querySelector('#view .cc')", 20)
            cdp.eval(
                "(() => { const b = document.querySelector('#view .cc button[data-act=\"open\"]');"
                " if (b) b.click(); return 'ok'; })()"
            )
            cdp.wait_for("!!document.querySelector('.blocks-table tbody tr')", 20)
            shoot("ui_probe_presets.png", "presets-screenshot.png")
    finally:
        browser.kill()
        fake.kill()
        if not args.keep:
            _cleanup(base)

    report = {
        "base_url": base,
        "session_id": session_id,
        "total": len(probe.results),
        "failed": probe.failed,
        "results": probe.results,
        "console_errors": probe.console_errors,
        "page_errors": probe.page_errors,
    }
    report_path = PROJECT_ROOT / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 60)
    print(f"浏览器探针：{len(probe.results)} 项检查，失败 {len(probe.failed)} 项")
    for item in probe.failed:
        print(f"  ✗ [{item['step']}] {item['check']} → {item['detail']}")
    print(f"控制台报错 {len(probe.console_errors)} 条 / 页面异常 {len(probe.page_errors)} 条")
    for text in (probe.console_errors + probe.page_errors)[:8]:
        print("   ", text[:300])
    print(f"报告：{report_path}")
    print(f"截图：{PROJECT_ROOT / 'data' / 'ui_probe_screenshot.png'}")
    print("=" * 60)
    return 0 if not probe.failed else 2


def _cleanup(base: str) -> None:
    """删掉探针账号（前缀 ui_ 在 cleanup 脚本的前缀清单里）。"""
    script = PROJECT_ROOT / "scripts" / "cleanup_demo_data.py"
    subprocess.run(
        [str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"), str(script)],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_checks(cdp: Cdp, base: str, data: dict) -> None:
    """真正点击界面并逐项断言。"""
    probe = cdp.probe
    token, user, session_id = data["token"], data["user"], data["session_id"]
    # 后面「提示词预设」一节要新建会话并绑定预设，需要这两个 id
    card_id, provider_id = data["card_id"], data["provider_id"]

    def _text_of(_cdp: Cdp, what: str) -> str:
        """安全地取一段界面文本。

        ★ 为什么需要它：`cdp.eval` 在页面里抛异常时返回的是
          `{"__error__": "..."}`（一个 dict）。以前直接对它调用 `.strip()`
          会让**整个探针崩掉** —— 前面已通过的检查连报告都写不出来。
          探针的价值是"跑完给完整结论"，所以取文本一律走这个包装：
          出错就把错误本身当文本返回（断言会失败，但不会中断整个运行）。
        """
        expr = {
            "messages-last": (
                "(() => { const ms = document.querySelectorAll('#messages .msg');"
                " const last = ms[ms.length - 1];"
                " return last?.querySelector('.msg-body')?.textContent || ''; })()"
            ),
        }[what]
        value = _cdp.eval(expr)
        if isinstance(value, dict):
            return f"<取文本失败：{value.get('__error__', value)}>"
        return str(value or "")

    # ---------- 1. 登录态注入 + 首屏 ----------
    # ★ 必须先导航到目标源再写 localStorage（见模块文档第 1 条）
    cdp.send("Page.navigate", url=f"{base}/console/")
    time.sleep(2.0)
    cdp.eval(
        f"localStorage.setItem('hne_access_token', {json.dumps(token)});"
        f"localStorage.setItem('hne_user', {json.dumps(json.dumps(user, ensure_ascii=False))});"
        "'ok'"
    )
    cdp.send("Page.navigate", url=f"{base}/console/")
    ready = cdp.wait_for("!!document.querySelector('#view h1')", 25)
    probe.check("1.加载", "顶栏出现（登录态生效）",
                cdp.eval("!!document.querySelector('#topbar') && !document.querySelector('#topbar').hidden") is True)
    probe.check("1.加载", "首屏渲染完成", ready, cdp.eval("document.querySelector('#view h1')?.textContent"))

    # ★ 静态守卫：把所有前端模块 import 一遍，并检查跨模块用到的导出确实存在。
    #
    #   为什么需要它（真实事故：用户打开控制台**一片空白**）：
    #     前端是原生 ES Module，浏览器按 URL 缓存每个 .js。如果只改了其中一部分，
    #     用户浏览器里会出现「新的 views/*.js + 旧的 ui.js」这种版本混用，
    #     而 ES Module 一旦 import 一个目标模块里**不存在的导出**（本轮新增的
    #     freshViewSignal 就踩中了），会**整包加载失败**：
    #     页面全白、控制台只有一行不显眼的红字，用户完全看不出原因。
    #
    #   现在双保险：
    #     · 模块之间改用裸名（hne/ui）由 index.html 的 importmap 带版本号，
    #       版本一变 URL 就变，浏览器没有旧副本可用；
    #     · 下面这条断言再从"代码本身"这一侧兜一道。
    #   这里刻意**用页面里的裸名导入**，顺带验证 importmap 真的能解析这些名字。
    module_check = cdp.eval(
        "Promise.all(["
        "  'hne/api', 'hne/ui', 'hne/app',"
        "  'hne/auth', 'hne/cards', 'hne/books', 'hne/providers', 'hne/chat', 'hne/presets',"
        "  'hne/plugins'"
        "].map(u => import(u).then(m => ({ u, keys: Object.keys(m) }))"
        "  .catch(e => ({ u, error: String((e && e.message) || e) }))))"
        ".then(list => {"
        "  const bad = list.filter(x => x.error).map(x => x.u + ': ' + x.error);"
        "  const ui = list.find(x => x.u === 'hne/ui');"
        "  const need = ['freshViewSignal', 'modal', 'buttonLoading', 'esc'];"
        "  const missing = ui && !ui.error ? need.filter(k => !ui.keys.includes(k)) : [];"
        "  return bad.length === 0 && missing.length === 0"
        "    ? 'OK' : JSON.stringify({ loadFailed: bad, uiMissing: missing });"
        "})"
    )
    module_ok = module_check == "OK"
    probe.check(
        "1.加载",
        "★ 所有前端模块可加载、跨模块导出齐全（防「一片空白」）",
        module_ok,
        module_check,
    )

    # ★★ 模块加载失败就**立即收工**，不要再往下点。
    #
    #   为什么必须这样：某个 .js 解析失败时，页面只是"半活"的 ——
    #   后面的点击会得到一串莫名其妙的结果（按钮找不到、文本取不到），
    #   探针要么给出误导性的失败清单、要么自己崩掉。
    #   真实踩过：ui.js 少了一个右花括号 → 全站模块加载失败，
    #   结果探针一路点下去，报告里全是"按钮没反应"，把真正的原因埋了。
    #   现在把"模块可加载"当成**前置条件**：不满足就只报这一条并退出。
    if not module_ok:
        raise ModuleLoadFailure(str(module_check))

    # ★ 缓存击穿守卫：页面必须带着版本号加载模块，且服务器要能回答「当前版本」。
    #   这套机制是白屏事故的根治手段（见 app/main.py 的 _frontend_version）。
    version_guard = cdp.eval(
        "(async () => {"
        "  const html = await (await fetch('/console/', { cache: 'no-store' })).text();"
        "  const live = document.body.dataset.webVersion || '';"
        "  const mapped = [...html.matchAll(/\\/console\\/js\\/[a-z/]+\\.js\\?v=([0-9a-f]+)/g)]"
        "    .map(m => m[1]);"
        "  const scripts = [...document.querySelectorAll('script[src]')]"
        "    .map(s => s.getAttribute('src'));"
        "  return JSON.stringify({"
        "    hasVersion: /^[0-9a-f]{12}$/.test(live),"
        "    mappedCount: mapped.length,"
        "    oneVersion: new Set(mapped).size === 1,"
        "    scriptsVersioned: scripts.every(s => s.includes('?v=')),"
        "    moduleResolved: (await import('hne/ui')).esc !== undefined"
        "  });"
        "})()"
    )
    try:
        vg = json.loads(version_guard or "{}")
    except (TypeError, ValueError):
        vg = {}
    probe.check(
        "1.加载",
        "★ 前端资源带版本号（缓存击穿生效，不会再出现新旧版本混用）",
        bool(
            vg.get("hasVersion")
            and vg.get("mappedCount", 0) >= 8
            and vg.get("oneVersion")
            and vg.get("scriptsVersioned")
            and vg.get("moduleResolved")
        ),
        version_guard,
    )
    probe.check("1.加载", "默认落在「叙事会话」页",
                "叙事会话" in (cdp.eval("document.querySelector('#view h1')?.textContent || ''") or ""))
    cdp.wait_for("document.querySelectorAll('.session-item').length >= 1", 20)
    probe.check("1.加载", "会话列表出现探针会话",
                (cdp.eval("document.querySelectorAll('.session-item').length") or 0) >= 1,
                cdp.eval("document.querySelectorAll('.session-item').length"))

    # ---------- 2. 打开会话 ----------
    cdp.eval(f"document.querySelector('.session-item[data-id=\"{session_id}\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#btn-send')", 20)
    probe.check("2.打开会话", "开场白已渲染为第一条消息",
                (cdp.eval("document.querySelectorAll('#messages .msg').length") or 0) >= 1)
    probe.check("2.打开会话", "输入框为空时发送按钮禁用",
                cdp.eval("document.querySelector('#btn-send').disabled") is True)

    # ---------- 3. 流式逐字（★ 只能用真实采样判断） ----------
    cdp.eval(
        "(() => { const i = document.querySelector('#composer-input');"
        " i.value = '灯塔还亮着吗'; i.dispatchEvent(new Event('input', { bubbles: true })); return 'ok'; })()"
    )
    probe.check("3.流式", "输入后发送按钮启用",
                cdp.eval("document.querySelector('#btn-send').disabled") is False)
    cdp.eval("document.querySelector('#btn-send').click(); 'ok'")

    sizes: list[int] = []
    started = time.time()
    while time.time() - started < 60:
        time.sleep(0.1)
        length = cdp.eval(
            "(() => { const ms = document.querySelectorAll('#messages .msg');"
            " const last = ms[ms.length-1];"
            " return last ? (last.querySelector('.msg-body').textContent || '').length : -1; })()"
        )
        if isinstance(length, int) and length >= 0:
            sizes.append(length)
        if cdp.eval(
            "(() => { const ms = document.querySelectorAll('#messages .msg');"
            " const last = ms[ms.length-1];"
            " return last ? !last.classList.contains('pending') : false; })()"
        ) is True:
            break
    growths = sum(1 for a, b in zip(sizes, sizes[1:]) if b > a)
    # ★ 再等一步：流刚结束时 silentReload 可能还在路上，
    #   要等界面真正稳定下来再断言，否则会抓到中间态（这个探针踩过两次）。
    cdp.wait_for(
        "document.querySelectorAll('#messages .msg-actions').length"
        " === document.querySelectorAll('#messages .msg').length",
        20,
    )
    probe.check(
        "3.流式",
        "★ 正文逐字增长（多次采样递增）",
        growths >= 5,
        {"增长次数": growths, "最终长度": sizes[-1] if sizes else 0, "耗时s": round(time.time() - started, 2)},
    )
    probe.check("3.流式", "流结束后发送按钮恢复可用（输入框为空所以是禁用态）",
                cdp.eval("document.querySelector('#btn-send').disabled") is True)
    probe.check("3.流式", "头部显示世界书命中统计",
                "世界书命中" in (cdp.eval("document.querySelector('#chat-sub')?.textContent || ''") or ""),
                cdp.eval("document.querySelector('#chat-sub')?.textContent"))
    probe.check("3.流式", "★ Token 统计条显示了本轮用量",
                "本轮 输入" in (cdp.eval("document.querySelector('#token-stats')?.textContent || ''") or ""),
                cdp.eval("document.querySelector('#token-stats')?.textContent"))
    probe.check("3.流式", "每条消息带 token 数",
                "token" in (cdp.eval("document.querySelector('#messages .msg .msg-tokens')?.textContent || ''") or ""))

    # ★ 结构化状态栏（HP/背包/位置/任务）：假模型默认不输出 <state> 块，
    #   所以这里断言的是"容器渲染出来了、没有状态时给出说明"，而不是具体数值。
    probe.check(
        "3.流式",
        "★ 状态栏渲染出来了（无状态时显示说明，不炸面板）",
        cdp.eval(
            "(() => { const bar = document.querySelector('#state-bar');"
            " if (!bar) return '__missing__';"
            " return bar.classList.contains('empty')"
            "   ? (bar.textContent.includes('还没有状态') ? 'empty-hint' : '__bad_hint__')"
            "   : 'with-state'; })()"
        )
        in ("empty-hint", "with-state"),
        cdp.eval("document.querySelector('#state-bar')?.textContent?.slice(0, 60)"),
    )

    # ★ 空状态下也必须有出口：模型不吐状态块时，用户要能自己先建一份。
    #   （以前「纠正」按钮在空状态下是直接 return —— 点了毫无反应，属于静默无操作。）
    cdp.eval("document.querySelector('#state-bar [data-act=\"edit-state\"]')?.click(); 'ok'")
    manual_open = cdp.wait_for("!!document.querySelector('#modal-root .modal')", 10)
    probe.check(
        "3.1.状态栏",
        "★ 还没有状态时也能「手动填写」（不是点了没反应）",
        manual_open,
        cdp.eval("document.querySelector('#modal-root .modal-head h2')?.textContent"),
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.3)

    # ★ 有状态时「纠正」也必须真的开窗、并把改后的值存回去
    #   （原实现里 openDialog 这个函数根本不存在 → 点了直接 ReferenceError，静默无反应）
    cdp.eval("document.querySelector('#state-bar [data-act=\"edit-state\"]')?.click(); 'ok'")
    fix_open = cdp.wait_for("!!document.querySelector('#modal-root [name=state]')", 10)
    if fix_open:
        cdp.eval(
            "(() => { const t = document.querySelector('#modal-root [name=state]');"
            " const v = JSON.parse(t.value); v.hp = {current: 999, max: 100};"
            " t.value = JSON.stringify(v); return 'ok'; })()"
        )
        cdp.eval("document.querySelector('#btn-save-state').click(); 'ok'")
        clamp_ok = cdp.wait_for(
            "(() => { const bar = document.querySelector('#state-bar');"
            " return !!bar && bar.textContent.includes('100 / 100'); })()",
            20,
        )
    else:
        clamp_ok = False
    probe.check(
        "3.1.状态栏",
        "★ 点「纠正」能改状态，且越界的 HP 被后端夹住（999 → 100）",
        fix_open and clamp_ok,
        {
            "弹窗打开": fix_open,
            "状态栏": (cdp.eval("document.querySelector('#state-bar')?.textContent || ''") or "")[:90],
        },
    )

    # ---------- 3.2 ★ 模型真的吐状态块时，状态栏必须亮起来 ----------
    # 用户验收时反馈"状态栏一直是空的"：这条链（模型输出 <state> → 后端剥离+
    # 校验+落库 → 前端渲染数值）以前**一条浏览器断言都没有** —— 假模型从不说
    # 状态块，于是最要命的那种情况在探针里完全隐形。
    # 约定（见 scripts/fake_openai_server.py）：用户消息里带"状态"两个字，
    # 假模型就扮演守规矩的模型，回复末尾附上状态块。
    cdp.eval(
        "(() => { const i = document.querySelector('#composer-input');"
        " i.value = '看一眼状态'; i.dispatchEvent(new Event('input', { bubbles: true })); return 'ok'; })()"
    )
    cdp.eval("document.querySelector('#btn-send').click(); 'ok'")
    state_ok = cdp.wait_for(
        "(() => { const bar = document.querySelector('#state-bar');"
        " return !!bar && !bar.classList.contains('empty')"
        "   && bar.textContent.includes('88 / 100'); })()",
        60,
    )
    probe.check(
        "3.1.状态栏",
        "★ 模型输出 <state> 块后，状态栏显示真实数值（HP 88/100、位置、背包）",
        state_ok,
        {
            "状态栏": (cdp.eval("document.querySelector('#state-bar')?.textContent || ''") or "")[:90],
            "正文里没有残留 JSON": cdp.eval(
                "!document.querySelector('#messages')?.textContent?.includes('<state>')"
            ),
        },
    )
    # ★ 第十六轮：状态条从"顶部一个大框"改成"消息区末尾一行"（用户反馈太占地方）
    probe.check(
        "3.1.状态栏",
        "★ 状态条在**消息区末尾**（= 最新那条回复的下方），不再是顶部独立大框",
        cdp.eval(
            "(() => { const box = document.querySelector('#messages');"
            " const bar = document.querySelector('#state-bar');"
            " if (!box || !bar || box.lastElementChild !== bar) return 'not-last';"
            " const prev = bar.previousElementSibling;"
            " return prev && prev.classList.contains('msg') && prev.classList.contains('assistant')"
            "   ? 'last-after-assistant' : 'prev-not-assistant'; })()"
        )
        == "last-after-assistant",
        cdp.eval(
            "(() => { const box = document.querySelector('#messages');"
            " return box && box.lastElementChild ? (box.lastElementChild.id || box.lastElementChild.className) : ''; })()"
        ),
    )
    # ★ 默认收成一行：详情要藏着，点了「详情」才展开（这是"不占地方"的关键）
    toggle_result = cdp.eval(
        "(() => { const btn = document.querySelector('#state-bar [data-act=\"toggle-state\"]');"
        " if (!btn) return 'no-toggle';"
        " const detail = document.querySelector('#state-bar .state-bar-detail');"
        " if (!detail) return 'no-detail';"
        " const collapsed = detail.classList.contains('hidden');"
        " btn.click();"
        " return collapsed && !detail.classList.contains('hidden') ? 'expands'"
        "   : 'collapsed=' + collapsed; })()"
    )
    probe.check(
        "3.1.状态栏",
        "★ 默认只占一行（详情收起），点「详情」才展开血条/背包",
        toggle_result == "expands",
        toggle_result,
    )
    probe.check(
        "3.1.状态栏",
        "★ 「看作者原格式」能看到模型这一轮输出的 <state> 原文（未被解析的那一份）",
        cdp.eval(
            "(() => { const pre = document.querySelector('#state-bar .state-raw-pre');"
            " if (!pre) return 'missing';"
            " const t = pre.textContent || '';"
            " return t.includes('{') && t.includes('}') ? 'ok' : 'empty'; })()"
        )
        == "ok",
        (cdp.eval("document.querySelector('#state-bar .state-raw-pre')?.textContent || ''") or "")[:60],
    )
    probe.check(
        "3.1.状态栏",
        "★ 状态块已从可见正文里剥掉（原始 JSON 只能从「看作者原格式」看到，正文里没有）",
        # ★ 第十六轮：这一条以前查整个 #messages，但状态条现在也在 #messages 里、
        #   而且「看作者原格式」**故意**展示原始块 ⇒ 改成只查**消息正文**。
        cdp.eval(
            "(() => { const bodies = Array.from(document.querySelectorAll('#messages .msg .msg-body'));"
            " return bodies.every((b) => !(b.textContent || '').includes('<state>')); })()"
        )
        is True,
        cdp.eval(
            "(() => { const b = document.querySelectorAll('#messages .msg .msg-body');"
            " return b.length ? (b[b.length - 1].textContent || '').slice(0, 40) : ''; })()"
        ),
    )

    # ---------- 3.3 ★ 「设置」对话框：剧情总结那一段必须能打开（模板改动不能炸 JS）----------
    # 剧情总结（前情提要）的界面在这里：会话没攒够 10 轮时不显示正文，
    # 但整段模板每次都会渲染 —— 所以这条断言守的是"改了模板没把弹窗搞崩"。
    cdp.eval("document.querySelector('[data-act=\"settings\"]').click(); 'ok'")
    settings_open = cdp.wait_for("!!document.querySelector('#modal-root #btn-save-settings')", 20)
    settings_text = cdp.eval("document.querySelector('#modal-root .modal')?.textContent || ''") or ""
    probe.check(
        "3.3.设置对话框",
        "★「设置」能打开，且写清了剧情总结的规则（攒够若干轮自动合并）",
        settings_open and "内置守卫" in settings_text,
        settings_text[:100],
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # ---------- 4. 消息操作按钮 ----------
    # ★ 第十六轮：不能用 `.msg:last-child` —— 状态条现在也在 #messages 里、且排在最后，
    #   那个选择器会返回 null（点了没反应，探针报红过一次）。改成"最后一条 .msg"。
    LAST_MSG = (
        "(() => { const ms = document.querySelectorAll('#messages .msg');"
        " return ms.length ? ms[ms.length - 1] : null; })()"
    )
    probe.check(
        "4.消息操作",
        "最后一条回复有「重新生成」按钮",
        cdp.eval(f"!!({LAST_MSG})?.querySelector('[data-act=\"regen\"]')") is True,
    )
    probe.check(
        "4.消息操作",
        "用户消息有「编辑」按钮",
        cdp.eval("!!document.querySelector('#messages .msg.user [data-act=\"edit-msg\"]')") is True,
    )
    probe.check(
        "4.消息操作",
        "只有最后一条用户消息有「撤回」",
        (cdp.eval("document.querySelectorAll('#messages [data-act=\"retract-msg\"]').length") or 0)
        == (cdp.eval("document.querySelectorAll('#messages .msg.user').length") or 0),
        {
            "撤回按钮数": cdp.eval("document.querySelectorAll('#messages [data-act=\"retract-msg\"]').length"),
            "用户消息数": cdp.eval("document.querySelectorAll('#messages .msg.user').length"),
        },
    )
    probe.check(
        "4.消息操作",
        "每条消息都有「复制」",
        (cdp.eval("document.querySelectorAll('#messages [data-act=\"copy-msg\"]').length") or 0)
        == (cdp.eval("document.querySelectorAll('#messages .msg').length") or 0),
    )

    # ---------- 5. 重新生成 ----------
    # ★ 用 _text_of 而不是裸 eval：eval 出错时返回的是 {"__error__": …}（dict），
    #   直接 `.strip()` 会让**整个探针崩掉**，前面 16 项已通过的检查连报告都写不出来。
    #   探针的价值在于"跑完给出完整结论"，所以取文本一律走这个安全包装。
    before = _text_of(cdp, "messages-last")
    # ★ 用户消息数用"重生成前后对比"，不写死 1：
    #   写死数字会在"这一节之前多聊了一轮"时误报（本轮新增状态栏那一步就撞上了），
    #   而这条断言真正要守的是"重新生成不该复制出一条用户消息"。
    users_before = cdp.eval("document.querySelectorAll('#messages .msg.user').length")
    cdp.eval(f"({LAST_MSG})?.querySelector('[data-act=\"regen\"]').click(); 'ok'")
    cdp.wait_for(
        "(() => { const ms = document.querySelectorAll('#messages .msg');"
        " const last = ms[ms.length-1];"
        " return last && !last.classList.contains('pending')"
        "   && (last.querySelector('.msg-body')?.textContent || '').length > 0; })()",
        60,
    )
    after = _text_of(cdp, "messages-last")
    probe.check("5.重新生成", "重新生成后正文非空", bool(after.strip()), after[:60])
    probe.check("5.重新生成", "★ 没有多出重复的用户消息",
                (cdp.eval("document.querySelectorAll('#messages .msg.user').length") or 0) == users_before,
                {"重生成前": users_before,
                 "重生成后": cdp.eval("document.querySelectorAll('#messages .msg.user').length")})

    # ---------- 6. 编辑（会删掉其后内容并自动重新生成） ----------
    cdp.eval("document.querySelector('#messages .msg.user [data-act=\"edit-msg\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root #btn-save-edit')", 10)
    probe.check("6.编辑", "编辑弹窗打开（并提示会删除后续内容）",
                "删除这条消息之后的全部内容" in (cdp.eval("document.querySelector('#modal-root .modal')?.textContent || ''") or ""))
    cdp.eval(
        "(() => { const t = document.querySelector('#modal-root [name=content]');"
        " t.value = '钟声响了三次'; t.dispatchEvent(new Event('input', { bubbles: true })); return 'ok'; })()"
    )
    cdp.eval("document.querySelector('#btn-save-edit').click(); 'ok'")
    cdp.wait_for("!document.querySelector('#modal-root .modal')", 15)
    # ★ 这里必须等"编辑后的文字真的出现在历史里"，不能只等"最后一条气泡不在 pending"。
    #   编辑的流程是 PATCH → 重渲染 → 重新生成，中间有一个瞬间：
    #   「最后一条消息」还是编辑前那条旧回复（既不 pending、也有内容），
    #   于是断言可能抢在重渲染之前取样，看到一个**看起来通过、其实还没更新**的状态。
    #   实测这个竞态是间歇性的（同一份代码，三次里挂一次）。
    cdp.wait_for(
        "(document.querySelector('#messages')?.textContent || '').includes('钟声响了三次')",
        30,
    )
    cdp.wait_for(
        "(() => { const ms = document.querySelectorAll('#messages .msg');"
        " const last = ms[ms.length-1];"
        " return last && !last.classList.contains('pending')"
        "   && (last.querySelector('.msg-body')?.textContent || '').length > 0; })()",
        60,
    )
    probe.check(
        "6.编辑",
        "★ 编辑后的内容出现在历史里",
        "钟声响了三次" in (cdp.eval("document.querySelector('#messages')?.textContent || ''") or ""),
    )
    probe.check(
        "6.编辑",
        "★ 编辑会删掉其后内容再重新生成（用户消息仍只有 1 条）",
        (cdp.eval("document.querySelectorAll('#messages .msg.user').length") or 0) == 1,
    )

    # ---------- 7. 撤回 ----------
    cdp.eval("document.querySelector('#messages [data-act=\"retract-msg\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root .modal')", 10)
    cdp.eval(
        "(() => { const btns = Array.from(document.querySelectorAll('#modal-root button'));"
        " const go = btns.find(b => b.textContent.includes('撤回')); go.click(); return 'ok'; })()"
    )
    cdp.wait_for("document.querySelectorAll('#messages .msg.user').length === 0", 20)
    # ★ 撤回是**异步**的：用户消息先消失，助手那条要等接口返回后列表才重画。
    #   这里必须先等"最终状态"稳定（恰好 1 条），再读一次断言 ——
    #   旧写法在 `.msg.user` 归零的瞬间就读，读到过 0 条（探针报"撤回后消息数 0"）。
    cdp.wait_for("document.querySelectorAll('#messages .msg').length === 1", 20)
    left_msgs = cdp.eval("document.querySelectorAll('#messages .msg').length")
    probe.check(
        "7.撤回",
        "★ 撤回后用户消息与回复都没了",
        left_msgs == 1,
        left_msgs,
    )
    # ★ 撤回是**异步**的：点完「撤回」后，原话要等一次接口返回才回填到输入框。
    #   以前这里直接 eval 一次就断言（而且条件与"详情"是两次独立读取），
    #   于是偶发变红：详情里明明打印着 `钟声响了三次`，条件那次读到的却是空串。
    #   改成先 wait_for 回填、再断言 —— 与文件里其它"等加载完再判"的写法一致。
    restored = cdp.wait_for(
        "(document.querySelector('#composer-input')?.value || '') === '钟声响了三次'", 15
    )
    probe.check(
        "7.撤回",
        "★ 被撤回的原话回到输入框（可以改了再发）",
        restored,
        cdp.eval("document.querySelector('#composer-input')?.value"),
    )

    # ---------- 8. 复制 ----------
    cdp.eval("document.querySelector('#messages [data-act=\"copy-msg\"]').click(); 'ok'")
    time.sleep(0.6)
    probe.check(
        "8.复制",
        "点复制后没有报错（剪贴板权限受限时走兼容路径）",
        not any("复制失败" in e for e in probe.console_errors),
    )

    # ---------- 9. 记忆面板（含「记忆总结」设置）----------
    #
    # ★ 先造 3 轮真实对话 + 把设置调成"每 2 轮提醒、不自动"：
    #   ① 总结只认"完整的一轮"（一问一答都齐），前面的编辑/撤回把关卡清空了，
    #      所以这里必须真的聊几轮，否则「立即总结」会被正当拒绝；
    #   ② 后面要验横幅，需要 pending ≥ rounds。
    prepared = cdp.eval(
        "(async () => {"
        "  const token = localStorage.getItem('hne_access_token');"
        "  const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token };"
        f"  const s = await fetch('{base}/api/v1/narrative/sessions/{session_id}/memory-summary',"
        "    { method: 'PATCH', headers: h,"
        "      body: JSON.stringify({ enabled: true, auto: false, remind: true, rounds: 2 }) });"
        "  if (!s.ok) return 'PATCH ' + s.status;"
        "  for (const t of ['第九节第一轮', '第九节第二轮', '第九节第三轮']) {"
        f"    const r = await fetch('{base}/api/v1/narrative/sessions/{session_id}/messages',"
        "      { method: 'POST', headers: h, body: JSON.stringify({ content: t }) });"
        "    if (!r.ok) return 'POST ' + r.status;"
        "  }"
        "  return 'ok';"
        "})()"
    )
    probe.check("9.记忆", "为记忆总结准备好 3 轮真实对话（设置每 2 轮提醒、不自动）", prepared == "ok", prepared)

    cdp.eval("document.querySelector('[data-act=\"memories\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#mem-list')", 15)
    time.sleep(2.0)  # 等检索返回（要过一次嵌入推理）
    panel_count = cdp.eval("document.querySelectorAll('#mem-list .panel').length") or 0
    mem_text = cdp.eval("document.querySelector('#mem-list')?.textContent || ''") or ""
    probe.check("9.记忆", "记忆面板能打开并列出条目", panel_count >= 1,
                {"面板数": panel_count, "面板文案": mem_text[:120]})

    # ★ 记忆总结那一段（用户要求：开关 / 自动或提醒 / 轮数 / 五种模式 / 可编辑正文 / 立即总结）
    summary_ready = cdp.wait_for("!!document.querySelector('#mem-summary [name=ms-mode]')", 20)
    modes = cdp.eval(
        "Array.from(document.querySelectorAll('#mem-summary [name=ms-mode] option'))"
        ".map(o => o.textContent.trim()).join('|')"
    ) or ""
    probe.check(
        "9.1.记忆总结",
        "★ 面板里有记忆总结：开关 / 自动 / 提醒 / 轮数 / 五种模式 / 可编辑正文 / 立即总结",
        summary_ready
        and "折叠-角色优先" in modes
        and "折叠-剧情优先" in modes
        and "表格总结" in modes
        and "照抄旧记忆生成新记忆" in modes
        and "自定义" in modes
        and cdp.eval("!!document.querySelector('#mem-summary [name=ms-content]')") is True
        and cdp.eval("!!document.querySelector('#mem-summary #btn-ms-run')") is True
        and cdp.eval("!!document.querySelector('#mem-summary [name=ms-auto]')") is True,
        {"模式": modes, "轮数默认": cdp.eval("document.querySelector('#mem-summary [name=ms-rounds]')?.value")},
    )
    # 「立即总结」是**唯一**会花 token 的手动入口：点一下必须真的写出总结（假模型会给一段字）
    cdp.eval("document.querySelector('#mem-summary #btn-ms-run').click(); 'ok'")
    summarized = cdp.wait_for(
        "(() => { const t = document.querySelector('#mem-summary [name=ms-content]');"
        " return !!t && t.value.trim().length > 0; })()",
        30,
    )
    summary_content = cdp.eval(
        "document.querySelector('#mem-summary [name=ms-content]')?.value || ''"
    ) or ""
    probe.check(
        "9.1.记忆总结",
        "★ 点「立即总结」真的生成了总结（并且能马上在面板里看到 / 编辑它）",
        summarized,
        summary_content[:80],
    )

    # ★ 记忆锚点：最多 5 条 / 2000 字，固定注入（用户手写的硬设定）
    anchor_ready = cdp.eval("!!document.querySelector('#ms-anchors #btn-ms-anchor-add')") is True
    cdp.eval("document.querySelector('#ms-anchors #btn-ms-anchor-add').click(); 'ok'")
    cdp.eval(
        "(() => { const i = document.querySelector('#ms-anchors [data-anchor=\"0\"]');"
        " if (!i) return 'no-input';"
        " i.value = '探针锚点：主角是女性';"
        " i.dispatchEvent(new Event('input', { bubbles: true })); return 'ok'; })()"
    )
    cdp.eval("document.querySelector('#ms-anchors #btn-ms-anchor-save').click(); 'ok'")
    anchors_saved = cdp.wait_for(
        "(() => { const el = document.querySelector('#ms-anchor-count');"
        " return !!el && el.textContent.includes('1/'); })()",
        20,
    )
    probe.check(
        "9.2.记忆锚点",
        "★ 面板能加锚点并保存（计数变成 1/5），锚点是固定注入的硬设定",
        anchor_ready and anchors_saved,
        cdp.eval("document.querySelector('#ms-anchor-count')?.textContent || ''"),
    )
    # 锚点必须真的进系统提示词（它是"永不折叠"的那一类）
    cdp.eval("Array.from(document.querySelectorAll('#modal-root [data-close]')).pop().click(); 'ok'")
    time.sleep(0.4)
    cdp.eval("document.querySelector('[data-act=\"prompt\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root .prompt-view')", 15)
    time.sleep(0.4)
    prompt_text = cdp.eval("document.querySelector('#modal-root .modal')?.textContent || ''") or ""
    probe.check(
        "9.2.记忆锚点",
        "★ 锚点出现在发给模型的提示词里（预览与实际一致）",
        "探针锚点：主角是女性" in prompt_text,
        prompt_text[-160:],
    )
    cdp.eval("Array.from(document.querySelectorAll('#modal-root [data-close]')).pop().click(); 'ok'")
    time.sleep(0.4)

    # ★★ 横幅：到点必须弹出，而且点「立即总结」**立刻**给出反馈（正在总结 / 按钮禁用）
    #
    # 用户真实踩过的坑：点了横幅上的总结按钮后**没有任何反馈**，他以为没点上，
    # 于是连点几下 —— 结果 AI 连着总结了好几遍、白花了好几份 token。
    # 所以这里的断言是"点击后**同步**变成不可点"（反馈必须摆在第一个 await 之前）。
    setup = cdp.eval(
        "(async () => {"
        "  const token = localStorage.getItem('hne_access_token');"
        "  const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token };"
        "  for (const t of ['横幅用第一轮', '横幅用第二轮']) {"
        f"    const r = await fetch('{base}/api/v1/narrative/sessions/{session_id}/messages',"
        "      { method: 'POST', headers: h, body: JSON.stringify({ content: t }) });"
        "    if (!r.ok) return 'POST ' + r.status + ': ' + (await r.text()).slice(0, 120);"
        "  }"
        f"  const p = await fetch('{base}/api/v1/narrative/sessions/{session_id}/memory-summary',"
        "    { headers: h });"
        "  const d = (await p.json()).data;"
        "  return JSON.stringify({ due: d.reminder?.due, pending: d.reminder?.pending,"
        "    rounds: d.reminder?.rounds, auto: d.reminder?.auto, remind: d.reminder?.remind,"
        "    enabled: d.settings?.enabled, coverage: d.coverage });"
        "})()"
    )
    try:
        setup_state = json.loads(setup or "{}")
    except (TypeError, ValueError):
        setup_state = {"raw": setup}
    cdp.eval("document.querySelector('[data-act=\"refresh\"]').click(); 'ok'")
    banner_ready = cdp.wait_for("!!document.querySelector('#summary-banner')", 20)
    probe.check(
        "9.3.总结横幅",
        "★ 攒够轮数后弹出「该总结了」横幅（不自动花 token，等你点）",
        banner_ready,
        {"接口状态": setup_state,
         "横幅": (cdp.eval("document.querySelector('#summary-banner')?.textContent || ''") or "")[:80]},
    )
    # 点下去**立刻**读按钮状态：此时请求还没回来，但反馈必须已经摆上了
    cdp.eval("document.querySelector('#summary-banner [data-act=\"summary-run\"]').click(); 'ok'")
    immediate = cdp.eval(
        "(() => { const b = document.querySelector('#summary-banner [data-act=\"summary-run\"]');"
        " if (!b) return 'gone(已完成)';"
        " return (b.disabled ? 'disabled' : 'enabled') + '|' + b.textContent.trim(); })()"
    )
    probe.check(
        "9.3.总结横幅",
        "★ 点「立即总结」后**马上**有反馈（按钮变「总结中…」并禁用，不能重复点）",
        immediate == 'gone(已完成)' or str(immediate).startswith('disabled'),
        {"点击后立刻读到的状态": immediate},
    )
    finished = cdp.wait_for(
        "(() => { const el = document.querySelector('#summary-banner');"
        " return !el || !el.textContent.includes('正在'); })()",
        40,
    )
    banner_after = cdp.eval(
        "document.querySelector('#summary-banner')?.textContent || '（横幅已消失＝已总结完）'"
    ) or ""
    probe.check(
        "9.3.总结横幅",
        "★ 总结完成后给结果（横幅消失或提示已总结，而不是一直转圈）",
        finished,
        banner_after[:80],
    )

    # ---------- 10. ★ 会话不存在时不再摊一个红色错误 ----------
    cdp.eval(
        f"fetch('/api/v1/narrative/sessions/{session_id}', {{method:'DELETE',"
        f" headers:{{Authorization:'Bearer ' + localStorage.getItem('hne_access_token')}}}}); 'ok'"
    )
    time.sleep(1.5)
    cdp.eval("document.querySelector('[data-act=\"refresh\"]').click(); 'ok'")
    time.sleep(1.5)
    body = cdp.eval("document.querySelector('#chat-main')?.textContent || ''") or ""
    probe.check("10.会话消失", "★ 不再显示「会话不存在」这类错误码", "会话不存在" not in body, body[:80])
    probe.check("10.会话消失", "★ 换成一句解释 + 下一步怎么做",
                "这个会话已经不在了" in body)

    # ---------- 10.5 ★ 模型配置：Base URL 填错了能不能自己看出来 ----------
    # 这一节对应"用户接不通第三方模型"的真实反馈：
    #   他填的是 https://ark.cn-beijing.volces.com/api/v3/responses，
    #   而本项目会在这个地址后再拼 /chat/completions，
    #   对方返回「模型不存在」—— 界面要能把这件事说清楚，而不是复读上游那句话。
    cdp.eval("document.querySelector('#nav [data-route=\"providers\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#providers-table, table')", 15)
    time.sleep(1.0)

    # 10.5a 预设按钮：点「豆包」应当把正确的 base_url 填进表单
    cdp.eval("document.querySelector('#btn-new').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root #url-presets')", 10)
    preset_ok = cdp.eval("!!document.querySelector('#modal-root #url-presets')") is True
    probe.check("10.5.BaseURL", "新增配置表单里有常见厂商的地址预设", preset_ok)
    if preset_ok:
        cdp.eval(
            "(() => { const bs = document.querySelectorAll('#url-presets button');"
            " const doubao = Array.from(bs).find(b => b.textContent.includes('豆包'));"
            " doubao.click(); return 'ok'; })()"
        )
        time.sleep(0.4)
        filled = cdp.eval("document.querySelector('#modal-root [name=base_url]').value") or ""
        probe.check(
            "10.5.BaseURL",
            "★ 点「豆包」会填入正确的 Base URL（到 /api/v3 为止）",
            filled == "https://ark.cn-beijing.volces.com/api/v3",
            filled,
        )
        probe.check(
            "10.5.BaseURL",
            "同时提示模型名怎么填（方舟要用控制台里的模型/接入点 ID）",
            "接入点" in (cdp.eval("document.querySelector('#preset-detail')?.textContent || ''") or ""),
        )

        # 10.5b 用户把完整接口地址粘进来时，前端要自动削掉并说明
        cdp.eval(
            "(() => { const i = document.querySelector('#modal-root [name=base_url]');"
            " i.value = 'https://ark.cn-beijing.volces.com/api/v3/responses';"
            " i.dispatchEvent(new Event('input', { bubbles: true })); return 'ok'; })()"
        )
        cdp.eval(
            "(() => { const i = document.querySelector('#modal-root [name=name]'); i.value = '探针豆包'; return 'ok'; })()"
        )
        cdp.eval(
            "(() => { const i = document.querySelector('#modal-root [name=model_name]'); i.value = 'doubao-x'; return 'ok'; })()"
        )
        cdp.eval("document.querySelector('#btn-save').click(); 'ok'")
        time.sleep(1.8)
        saved_url = cdp.eval(
            "(() => { const tr = document.querySelector('#view tr[data-id]');"
            " return tr ? (tr.textContent.includes('/responses') ? 'still-bad' : 'ok') : 'no-row'; })()"
        )
        probe.check(
            "10.5.BaseURL",
            "★ 保存时自动去掉多余的 /responses（不会存下一个必然失效的地址）",
            saved_url == "ok",
            saved_url,
        )
        # 清理这条探针配置（免得留在列表里）。
        # ★ 只点**名字匹配**的那一行 —— 列表第一行可能是用户自己的配置，
        #   按位置点会误删真实数据（探针第一版就是这么写的，看图才发现）。
        cdp.eval(
            "(() => { const tr = Array.from(document.querySelectorAll('#view tr[data-id]'))"
            "   .find(r => r.textContent.includes('探针豆包'));"
            " const del = tr && tr.querySelector('button[data-act=\"del\"]');"
            " if (del) del.click(); return 'ok'; })()"
        )
        time.sleep(0.8)
        cdp.eval(
            "(() => { const btns = Array.from(document.querySelectorAll('#modal-root button'));"
            " const go = btns.find(b => b.textContent.includes('删除')); if (go) go.click(); return 'ok'; })()"
        )
        time.sleep(1.0)

    # ★ 不管上面走到哪一步，进下一节之前先把所有弹窗关干净。
    #   否则"上一节留下的表单"会被误判成"这一节多弹出来的窗"
    #   —— 探针第一版就是这么误报的（第 10.6 节抓到 2 个窗，其中一个是上一节没关的）。
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # ---------- 10.6 ★ 弹窗不能叠加（点一次只弹一个） ----------
    # 真实事故：视图重渲染时又往 #view 上挂了一个委托监听器，
    # 于是点一次「模型」会弹出 N 个一模一样的窗（N = 累计渲染次数）。
    # 这条断言就是专门盯这个的：**点一次必须恰好一个弹窗**。
    cdp.eval("document.querySelector('#nav [data-route=\"providers\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view tr[data-id]')", 20)
    time.sleep(0.8)

    def click_row_button(act: str) -> None:
        cdp.eval(
            "(() => { const tr = Array.from(document.querySelectorAll('#view tr[data-id]'))"
            "   .find(r => r.textContent.includes('探针假模型'));"
            f" const b = tr && tr.querySelector('button[data-act=\"{act}\"]');"
            " if (b) b.click(); return 'ok'; })()"
        )

    def modal_count() -> int:
        return cdp.eval("document.querySelectorAll('#modal-root .modal').length") or 0

    click_row_button('models')
    cdp.wait_for("document.querySelectorAll('#modal-root .modal').length >= 1", 20)
    time.sleep(0.6)
    first_count = modal_count()
    modal_diag = cdp.eval(
        "Array.from(document.querySelectorAll('#modal-root .modal')).map(m => "
        "  (m.querySelector('.modal-head, h3, strong')?.textContent || '?').trim().slice(0,20))"
    )
    probe.check(
        "10.6.弹窗叠加",
        "★ 点一次「模型」只弹一个窗",
        first_count == 1,
        {"弹窗数": first_count, "标题": modal_diag},
    )
    # 关掉再点一次：如果监听器有残留，第二次会弹两个
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)
    click_row_button('models')
    cdp.wait_for("document.querySelectorAll('#modal-root .modal').length >= 1", 20)
    time.sleep(0.6)
    second_count = modal_count()
    probe.check(
        "10.6.弹窗叠加",
        "★ 再点一次仍然只有一个窗（监听器没有叠加）",
        second_count == 1,
        second_count,
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # ★ 再复现一次**用户的真实操作路径**：本视图会"自己重渲染自己"
    #   （测试/探测/删除之后刷新列表），每重渲染一次就多挂一个委托监听器，
    #   于是点一次按钮弹出 N 个窗。这里主动跑两次「测试」把渲染次数堆上去。
    for _ in range(2):
        click_row_button('test')
        cdp.wait_for("document.querySelectorAll('#modal-root .modal').length >= 1", 40)
        time.sleep(0.6)
        cdp.eval(
            "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
            "   .forEach(b => b.click()); return 'ok'; })()"
        )
        time.sleep(0.5)

    click_row_button('models')
    cdp.wait_for("document.querySelectorAll('#modal-root .modal').length >= 1", 20)
    time.sleep(0.6)
    after_refresh_count = modal_count()
    probe.check(
        "10.6.弹窗叠加",
        "★ 反复重渲染之后，点一次仍然只弹一个窗（这是用户报的那个 bug）",
        after_refresh_count == 1,
        after_refresh_count,
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # ---------- 10.6b ★ 跨页串台：预设页 → 插件页，点删除只能弹一个窗 ----------
    # ★ 用户实际复现的那个 bug："删插件弹了两个窗口"，第二个还报
    #   404「提示词预设不存在」。
    #   根因：预设页挂在常驻 #view 上的委托**没有被路由信号摘掉**
    #   （它的控制器只 abort 上一批、没接父信号），而插件页与预设页的卡片
    #   都是 `.cc` + `data-act="del"` —— 插件页的删除被预设页留下的动作接走了。
    #
    # ★ 为什么老断言（10.6）一直是绿的：它只在本页反复点击，
    #   而这条 bug 必须**先访问过预设页再切到插件页**才会出现。
    #   所以这里刻意走用户那条路，而不是"就地再点一次"。
    cdp.eval("document.querySelector('#nav [data-route=\"presets\"]').click(); 'ok'")
    # ★ 等到 h1 出现就够了：整页是渲染完才一次性 mount 的，
    #   而委托监听器就是在这次渲染后绑上的（预设页一张卡都没有时也同样会绑）。
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '') === '提示词预设'", 25)
    time.sleep(0.8)
    cdp.eval("document.querySelector('#nav [data-route=\"plugins\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view [data-view=\"plugins\"] button[data-act=\"del\"]')", 25)
    time.sleep(0.6)
    cdp.eval(
        "(() => { const b = document.querySelector("
        "  '#view [data-view=\"plugins\"] button[data-act=\"del\"]');"
        " if (b) b.click(); return 'ok'; })()"
    )
    cdp.wait_for("document.querySelectorAll('#modal-root .modal').length >= 1", 20)
    time.sleep(0.6)
    cross_count = modal_count()
    cross_titles = cdp.eval(
        "Array.from(document.querySelectorAll('#modal-root .modal h2'))"
        ".map(h => (h.textContent || '').trim())"
    )
    titles_text = "、".join(str(t) for t in (cross_titles or []))
    probe.check(
        "10.6.弹窗叠加",
        "★ 先访问预设页、再切到插件页点「删除」：仍然只弹一个窗（用户复现的那条路）",
        cross_count == 1 and "删除插件" in titles_text,
        {"弹窗数": cross_count, "标题": titles_text},
    )
    # 取消掉，别真删（后面的用例还要用这些默认插件）
    cdp.eval(
        "(() => { const b = document.querySelector('#modal-root .modal [data-cancel]');"
        " if (b) b.click(); return 'ok'; })()"
    )
    time.sleep(0.4)

    # ---------- 10.7 ★ 角色卡：切过筛选之后，卡片上的操作还能不能用 ----------
    # 用户真实反馈："点公共卡库 / 全部可见之后，这个页面的按钮就失效了（点击无反应）"。
    # 根因是"只绑一次"的守卫认的是**传进来的** signal，而筛选那条路径根本没传，
    # 于是委托监听器被绑在一个已经 abort 的旧信号上 —— 一诞生就是死的。
    # 这条断言必须在**真实点击**下跑，静态检查看不出来。
    cdp.eval("document.querySelector('#nav [data-route=\"cards\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#scope-tabs')", 20)

    def switch_scope(scope: str) -> None:
        cdp.eval(f"document.querySelector('#scope-tabs [data-scope=\"{scope}\"]').click(); 'ok'")
        cdp.wait_for(
            "(() => { const b = document.querySelector('#scope-tabs [data-scope=\""
            + scope
            + "\"]'); return b && b.classList.contains('active'); })()",
            20,
        )
        # ★ 必须等列表**加载完**（出现卡片）再往下点。
        #   只等标签高亮是不够的：那会儿 root 里还是"⏳ 加载中…"。
        cdp.wait_for("!!document.querySelector('.cc [data-act=\"view\"]')", 25)
        time.sleep(0.5)

    for scope, label in (("public", "公共卡库"), ("all", "全部可见"), ("mine", "我的卡库")):
        switch_scope(scope)
        # ★ 关键：切完之后卡片上的「详情」必须还能点开
        clicked = cdp.eval(
            "(() => { const b = document.querySelector('.cc [data-act=\"view\"]');"
            " if (!b) return 'no-button'; b.click(); return 'clicked'; })()"
        )
        opened = cdp.wait_for("!!document.querySelector('#modal-root .modal')", 15)
        probe.check(
            "10.7.角色卡",
            f"★ 切到「{label}」之后，卡片上的「详情」仍然能点开（监听器没被绑死）",
            clicked == "clicked" and opened,
            {
                "点击": clicked,
                "弹窗": opened,
                "卡片数": cdp.eval("document.querySelectorAll('.cc').length"),
                "列表文案": (cdp.eval("document.querySelector('#view')?.textContent || ''") or "")[:60],
            },
        )
        cdp.eval(
            "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
            "   .forEach(b => b.click()); return 'ok'; })()"
        )
        time.sleep(0.4)

    # ---------- 10.7.1 ★ 切过卡库之后**工具栏**按钮也必须还能用 ----------
    # 用户真实反馈的后半句："点了公共卡库之后，这一页别的按钮全都点不动了"。
    # 上面那段只覆盖了卡片上的委托按钮（它们挂在常驻的 #view 上，一直没坏），
    # 漏掉的恰恰是工具栏：#scope-tabs / #f-q / #f-sort / #btn-new 这些节点
    # 每次重画都会被 mount() 换掉 —— 如果"只绑一次"，新节点就**天生没有监听器**。
    # 这条断言在修复前实测为红（点「公开卡库」后点「新建角色卡」毫无反应）。
    switch_scope("public")
    cdp.eval("document.querySelector('#scope-tabs [data-scope=\"mine\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#btn-new')", 20)
    cdp.eval("document.querySelector('#btn-new').click(); 'ok'")
    toolbar_modal = cdp.wait_for("!!document.querySelector('#modal-root .modal')", 15)
    probe.check(
        "10.7.角色卡",
        "★ 切到「公共卡库」再切回「我的卡库」后，顶部按钮没失效（能弹出新建卡片窗）",
        toolbar_modal,
        {
            "弹窗": toolbar_modal,
            "标题": cdp.eval("document.querySelector('#modal-root .modal-head h2')?.textContent"),
        },
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # ---------- 10.7.2 导入 JSON 必须能选文件 / 拖文件 ----------
    # 用户真实反馈："只能粘贴，可我的卡都在硬盘上。"
    cdp.eval("document.querySelector('#btn-import-json')?.click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#json-drop')", 15)
    probe.check(
        "10.7.角色卡",
        "★ 导入 JSON 弹窗里能选文件 / 拖文件（不只是粘贴）",
        cdp.eval("!!document.querySelector('#json-drop input[type=file]')") is True,
        {"拖拽区": cdp.eval("!!document.querySelector('#json-drop')")},
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # 顺带验证：委托监听器必须**跟着当前批次一起失效**，不能泄漏到别的页面。
    # （离开本页后信号被 abort，此时界面上不应该再冒出新的 API 请求。）
    reqlog_before = cdp.eval("document.querySelectorAll('#reqlog-list .rl').length") or 0
    cdp.eval("document.querySelector('#nav [data-route=\"books\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('世界书')", 15)
    time.sleep(1.2)
    reqlog_after = cdp.eval("document.querySelectorAll('#reqlog-list .rl').length") or 0
    probe.check(
        "10.7.角色卡",
        "离开角色卡页之后没有残留监听器（世界书页上不再冒出角色卡请求）",
        reqlog_after <= reqlog_before + 1,
        {"离开前": reqlog_before, "离开后": reqlog_after},
    )

    # ---------- 10.7.3 ★ 世界书：点开「查看 / 编辑」再直接关闭，不能卡在"加载中" ----------
    # 用户真实反馈：世界书页点「查看 / 编辑」，不保存直接点「关闭」，
    # 整页变成「⏳ 加载中…」再也不回来（既没报错也没有出口）。
    # 这类"整页加载中"的病根是：先画占位、再等接口，中间只要有一次
    # 旧批次的回调醒来把画面盖成"加载中"又因信号失效直接 return，页面就废了。
    cdp.wait_for("!!document.querySelector('#view .cc [data-act=\"view\"]')", 20)
    cdp.eval(
        "(() => { const b = document.querySelector('#view .cc [data-act=\"view\"]');"
        " if (b) b.click(); return 'ok'; })()"
    )
    opened_book = cdp.wait_for("!!document.querySelector('#modal-root .modal')", 15)
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(1.0)
    book_view_text = cdp.eval("document.querySelector('#view')?.innerText || ''") or ""
    probe.check(
        "10.7.3.世界书",
        "★ 世界书「查看 / 编辑」→ 直接关闭后，页面不卡在「加载中」（列表与工具栏都在）",
        (opened_book and "加载中" not in book_view_text
         and cdp.eval("!!document.querySelector('#btn-new')") is True),
        {
            "弹窗打开": opened_book,
            "工具栏": cdp.eval("!!document.querySelector('#btn-new')"),
            "卡片数": cdp.eval("document.querySelectorAll('#view .cc').length"),
            "片段": book_view_text[:60],
        },
    )
    cdp.eval("document.querySelector('#nav [data-route=\"providers\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('模型配置')", 15)
    time.sleep(0.4)

    # ---------- 10.8 ★ 提示词预设：导入 → 块开关 → 绑定 → 真的影响请求 ----------
    # 这一节对应我新做的「提示词预设」功能。检查顺序刻意是**从界面到请求**：
    # 先确认页面能打开、能导入，再确认"绑定之后模型真的收到预设内容"。
    preset_name = f"探针预设_{int(time.time())}"
    cdp.eval("document.querySelector('#nav [data-route=\"presets\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('提示词预设')", 15)
    probe.check("10.8.预设", "「提示词预设」页能打开", True)

    # ---------- 10.8.1 ★ 内置守卫规则：可见、可删、可还原 ----------
    # 用户明确要求"默认预设也能在预设管理里改和删，并配一个还原选项"。
    # 这里只看界面：内置徽标在不在、"还原内置规则"按钮点了有没有反应。
    builtin_badge = cdp.eval(
        "(() => { const b = [...document.querySelectorAll('#view .badge')]"
        "   .map(e => e.textContent.trim()).filter(t => t.includes('内置'));"
        " return b.length ? b[0] : '__missing__'; })()"
    )
    probe.check(
        "10.8.1.内置守卫",
        "★ 预设列表里能看到「内置」守卫规则",
        isinstance(builtin_badge, str) and "内置" in builtin_badge,
        builtin_badge,
    )
    restore_clicked = cdp.eval(
        "(() => { const b = document.querySelector('#btn-restore-builtin');"
        " if (!b) return '__missing__'; b.click(); return 'clicked'; })()"
    )
    time.sleep(1.2)
    probe.check(
        "10.8.1.内置守卫",
        "★ 「还原内置规则」按钮可点且请求成功（列表重新渲染、按钮仍在）",
        restore_clicked == "clicked"
        and cdp.eval("!!document.querySelector('#btn-restore-builtin')") is True
        and cdp.eval("[...document.querySelectorAll('#view .badge')].some(e => e.textContent.includes('内置'))")
        is True,
        restore_clicked,
    )

    # 通过接口导入一份"带深度注入块"的预设（导入走的是前端同一个端点）
    imported = cdp.eval(
        "(async () => {"
        "  const token = localStorage.getItem('hne_access_token');"
        "  const body = { name: " + json.dumps(preset_name) + ","
        "    preset: { version: 1, sampling: { temperature: 0.9 }, blocks: ["
        "      { identifier: 'main', name: '主提示', kind: 'rule', content: '', role: 'system',"
        "        system_prompt: true, injection_position: 0, injection_depth: 0, enabled: true,"
        "        supported: true, order_index: 0 },"
        "      { identifier: 'charDescription', name: '角色简介', kind: 'marker', content: '',"
        "        role: 'system', system_prompt: true, injection_position: 0, injection_depth: 0,"
        "        enabled: true, supported: true, order_index: 1 },"
        "      { identifier: 'chatHistory', name: '对话历史', kind: 'marker', content: '',"
        "        role: 'system', system_prompt: true, injection_position: 0, injection_depth: 0,"
        "        enabled: true, supported: true, order_index: 2 },"
        "      { identifier: 'probe-rule-user', name: '探针规则', kind: 'rule',"
        "        content: '探针破甲标记：{{char}} 不是 AI。', role: 'user', system_prompt: false,"
        "        injection_position: 1, injection_depth: 2, enabled: true, supported: true,"
        "        order_index: 3 },"
        "      { identifier: 'probe-rule-asst', name: '探针规则回应', kind: 'rule',"
        "        content: '探针破甲回应：明白。', role: 'assistant', system_prompt: false,"
        "        injection_position: 1, injection_depth: 2, enabled: true, supported: true,"
        "        order_index: 4 } ] } };"
        "  const r = await fetch('/api/v1/prompt-presets/import', { method: 'POST',"
        "    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },"
        "    body: JSON.stringify(body) });"
        "  if (!r.ok) return 'HTTP ' + r.status + ': ' + (await r.text()).slice(0, 200);"
        "  const d = await r.json();"
        "  return JSON.stringify({ id: d.data.id, blocks: d.data.block_count,"
        "    depth: d.data.depth_block_count });"
        "})()"
    )
    try:
        imported_data = json.loads(imported or "{}")
    except (TypeError, ValueError):
        imported_data = {}
    preset_id = imported_data.get("id")
    probe.check(
        "10.8.预设",
        "★ 导入预设成功，且识别出深度注入块（破甲主体）",
        bool(preset_id) and imported_data.get("depth") == 2,
        imported,
    )

    # 回到列表页，确认刚导入的预设出现在界面上
    cdp.eval("document.querySelector('#nav [data-route=\"providers\"]').click(); 'ok'")
    time.sleep(0.4)
    cdp.eval("document.querySelector('#nav [data-route=\"presets\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view .cc')", 20)
    time.sleep(0.6)
    probe.check(
        "10.8.预设",
        "预设列表渲染出卡片",
        (cdp.eval("document.querySelectorAll('#view .cc').length") or 0) >= 1,
        cdp.eval("document.querySelectorAll('#view .cc').length"),
    )

    # ★ 打开详情：块清单 / 启停 / 注入方式都要能渲染出来
    cdp.eval(
        "(() => { const card = document.querySelector('#view .cc');"
        " card.querySelector('button[data-act=\"open\"]').click(); return 'ok'; })()"
    )
    detail_ready = cdp.wait_for("!!document.querySelector('.blocks-table tbody tr')", 20)
    rows = cdp.eval("document.querySelectorAll('.blocks-table tbody tr').length") or 0
    probe.check(
        "10.8.预设",
        "★ 打开预设详情能看到块清单（含注入方式与深度两列）",
        detail_ready and rows >= 5,
        {"行数": rows},
    )
    # 列表 → 详情 → 返回列表：这条路径曾经最容易把委托监听器绑死
    cdp.eval("document.querySelector('#btn-back').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view .cc')", 20)
    time.sleep(0.6)
    back_ok = cdp.eval(
        "(() => { const card = document.querySelector('#view .cc');"
        " if (!card) return 'no-card';"
        " card.querySelector('button[data-act=\"open\"]').click(); return 'clicked'; })()"
    )
    reopened = cdp.wait_for("!!document.querySelector('.blocks-table tbody tr')", 20)
    probe.check(
        "10.8.预设",
        "★ 从详情返回列表后，卡片按钮仍然能点（监听器没被绑死）",
        back_ok == "clicked" and reopened,
        {"点击": back_ok, "重开": reopened},
    )

    # ★ 绑定到会话 → 断言预设内容真的进了发出去的请求
    #
    #   注意：第 10 步**故意删掉了**探针的原始会话，所以这里要新建一条。
    #   用原始会话来绑定的话，接口会返回 404，而那条断言就成了"永远失败"。
    new_session = cdp.eval(
        "(async () => {"
        "  const token = localStorage.getItem('hne_access_token');"
        "  const r = await fetch('/api/v1/narrative/sessions', { method: 'POST',"
        "    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },"
        "    body: JSON.stringify({ character_card_id: " + str(card_id) + ","
        "      llm_provider_id: " + str(provider_id) + ", title: '预设探针会话' }) });"
        "  if (!r.ok) return 'HTTP ' + r.status + ': ' + (await r.text()).slice(0, 160);"
        "  const d = await r.json();"
        "  return String(d.data.id);"
        "})()"
    )
    preset_session_id = int(new_session) if str(new_session or "").isdigit() else None

    bind = cdp.eval(
        "(async () => {"
        "  const token = localStorage.getItem('hne_access_token');"
        f"  const r = await fetch('/api/v1/narrative/sessions/{preset_session_id}', {{"
        "    method: 'PATCH', headers: { 'Content-Type': 'application/json',"
        "      Authorization: 'Bearer ' + token },"
        f"    body: JSON.stringify({{ prompt_preset_id: {preset_id} }}) }});"
        "  if (!r.ok) return 'HTTP ' + r.status;"
        "  const d = await r.json();"
        "  return JSON.stringify({ bound: d.data.prompt_preset?.id ?? null,"
        "    effective: d.data.effective_preset?.from ?? null });"
        "})()"
    )
    try:
        bind_data = json.loads(bind or "{}")
    except (TypeError, ValueError):
        bind_data = {}
    probe.check(
        "10.8.预设",
        "★ 会话绑定预设后，接口回报 effective_preset 来源为 session",
        preset_session_id is not None
        and bind_data.get("bound") == preset_id
        and bind_data.get("effective") == "session",
        {"session": preset_session_id, "bind": bind},
    )

    # 用「查看提示词」看装配明细（界面必须能说清"生效了哪些块"）
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('.session-item')", 20)
    time.sleep(0.5)
    # ★ 点**具体的**那条会话（第 10 步删过一条，列表顺序不一定是我们要的）
    cdp.eval(
        "(() => { const item = document.querySelector('.session-item[data-id=\"" + str(preset_session_id) + "\"]')"
        " || document.querySelector('.session-item');"
        " if (item) item.click(); return 'ok'; })()"
    )
    cdp.wait_for("!!document.querySelector('#btn-send')", 20)
    time.sleep(0.8)
    cdp.eval("document.querySelector('[data-act=\"prompt\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root .modal')", 15)
    time.sleep(0.5)
    prompt_text = cdp.eval("document.querySelector('#modal-root .modal')?.textContent || ''") or ""
    probe.check(
        "10.8.预设",
        "★「查看提示词」显示预设来源与装配明细",
        "预设装配明细" in prompt_text and preset_name in prompt_text,
        prompt_text[:120],
    )
    # 点"完整装配预览"，断言深度注入的块出现在预览里
    cdp.eval("document.querySelector('#btn-preview-preset')?.click(); 'ok'")
    preview_ok = cdp.wait_for(
        "(document.querySelector('#preset-preview')?.textContent || '').includes('插进历史')", 25
    )
    preview_text = cdp.eval("document.querySelector('#preset-preview')?.textContent || ''") or ""
    probe.check(
        "10.8.预设",
        "★ 装配预览标出深度注入块（插进历史 · 倒数第 N 条之前）",
        preview_ok and "探针破甲标记" in preview_text,
        preview_text[:200],
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # ---------- 10.9 ★★ 编辑 → 保存 → 返回之后，列表还能不能点 ----------
    #
    # 用户第二次反馈："在角色卡界面中点击按钮后按钮就失效了" +
    # "世界书点编辑后再回到世界书界面就一直卡在加载中"。
    #
    # 根因和 10.7 是同一个：委托监听器被绑在**调用方传进来的旧信号**上
    # （books.js 当初漏改了），一诞生就是死的，而"已绑过"的布尔标志又挡住重绑。
    # 表现有两种：按钮全失效 / 列表永远停在"加载中"。
    # ★ 这条断言覆盖的是**真实的用户操作顺序**：
    #   列表 → 打开编辑（弹窗）→ 保存（触发列表重渲染）→ 返回列表 → 再点一次。
    cdp.eval("document.querySelector('#nav [data-route=\"books\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view .cc')", 20)
    time.sleep(0.5)

    opened = cdp.eval(
        "(() => { const b = document.querySelector('#view .cc button[data-act=\"view\"]');"
        " if (!b) return 'no-button'; b.click(); return 'clicked'; })()"
    )
    editor_ready = cdp.wait_for("!!document.querySelector('#modal-root #btn-save')", 20)
    probe.check(
        "10.9.编辑后仍可点",
        "点「查看 / 编辑」能打开世界书条目编辑器",
        opened == "clicked" and editor_ready,
        {"点击": opened, "编辑器": editor_ready},
    )

    cdp.eval("document.querySelector('#modal-root #btn-save').click(); 'ok'")
    cdp.wait_for("!document.querySelector('#modal-root .modal')", 20)
    # ★ 关键：保存之后列表必须**重新渲染出卡片**（卡在"加载中"就是 bug）
    list_back = cdp.wait_for("!!document.querySelector('#view .cc')", 20)
    probe.check(
        "10.9.编辑后仍可点",
        "★ 世界书保存后列表重新渲染出来（不再卡在「加载中」）",
        list_back,
        (cdp.eval("document.querySelector('#view')?.textContent || ''") or "")[:60],
    )

    # ★ 关键：再点一次「查看 / 编辑」必须还有反应（监听器没被绑死）
    reopened = cdp.eval(
        "(() => { const b = document.querySelector('#view .cc button[data-act=\"view\"]');"
        " if (!b) return 'no-button'; b.click(); return 'clicked'; })()"
    )
    editor_again = cdp.wait_for("!!document.querySelector('#modal-root #btn-save')", 20)
    probe.check(
        "10.9.编辑后仍可点",
        "★ 世界书保存并返回之后，列表按钮仍然能点（监听器没被绑死）",
        reopened == "clicked" and editor_again,
        {"点击": reopened, "编辑器": editor_again},
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # 同样的顺序在**角色卡页**再走一遍（编辑 → 保存 → 返回 → 再点）
    cdp.eval("document.querySelector('#nav [data-route=\"cards\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view .cc')", 20)
    time.sleep(0.5)
    cdp.eval(
        "(() => { const b = document.querySelector('#view .cc button[data-act=\"edit\"]');"
        " if (b) b.click(); return 'ok'; })()"
    )
    # ★ 角色卡编辑弹窗与世界书编辑弹窗的保存按钮**同名**（都是 #btn-save），
    #   所以这里必须先确认弹窗真的关掉了，再判断列表。
    cdp.wait_for("!!document.querySelector('#modal-root #btn-save')", 20)
    cdp.eval("document.querySelector('#modal-root #btn-save').click(); 'ok'")
    cdp.wait_for("!document.querySelector('#modal-root .modal')", 20)
    card_list_back = cdp.wait_for("!!document.querySelector('#view .cc')", 20)
    card_click_again = cdp.eval(
        "(() => { const b = document.querySelector('#view .cc button[data-act=\"view\"]');"
        " if (!b) return 'no-button'; b.click(); return 'clicked'; })()"
    )
    card_detail = cdp.wait_for("!!document.querySelector('#modal-root .modal')", 20)
    probe.check(
        "10.9.编辑后仍可点",
        "★ 角色卡页：编辑保存并返回之后，列表与按钮都还正常",
        card_list_back and card_click_again == "clicked" and card_detail,
        {"列表": card_list_back, "点击": card_click_again, "详情": card_detail},
    )

    # ---------- 10.10 ★ 开场白里的 HTML 卡片要"渲染出来"，而不是显示源码 ----------
    #
    # 用户反馈：他导入的角色卡开场白是一整段 HTML（状态栏那种卡片），
    # 界面上只显示一堆源码，等于"美化没做"。
    # 这里在浏览器里真造一张带 HTML 开场白的卡，点开详情看有没有渲染视图。
    made = cdp.eval(
        "(async () => {"
        "  const token = localStorage.getItem('hne_access_token');"
        "  const r = await fetch('/api/v1/character-cards', { method: 'POST',"
        "    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token },"
        "    body: JSON.stringify({ name: '探针HTML卡_" + str(int(time.time())) + "',"
        "      greeting: '<div class=\"stat\"><b>状态栏</b><table><tr><td>HP</td><td>42</td></tr></table></div>',"
        "      alternate_greetings: ['<div class=\"alt1\">备选甲</div>', '<div class=\"alt2\">备选乙</div>'],"
        "      personality: '测试' }) });"
        "  if (!r.ok) return 'HTTP ' + r.status;"
        "  const d = await r.json();"
        "  return String(d.data.id);"
        "})()"
    )
    html_card_id = int(made) if str(made or "").isdigit() else None
    probe.check("10.10.HTML开场白", "能造出一张 HTML 开场白的角色卡", html_card_id is not None, made)

    if html_card_id is not None:
        # ★ 先清场：前面几个用例可能留下过没关的弹窗，而
        #   `document.querySelector('#modal-root [data-rich="greeting"]')` 命中的是
        #   **文档里第一个**匹配项 —— 结果读到的是上一张卡的弹窗（这个坑真的踩了：
        #   探针报"HTML 没渲染"，其实是它看错了弹窗）。
        #   所以下面一律用 `[...].pop()` 取**最后一个**弹窗，并在打开前先关掉旧的。
        cdp.eval(
            "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
            "   .forEach(b => b.click()); return 'ok'; })()"
        )
        time.sleep(0.4)
        # 回到列表，打开这张卡，再看渲染结果
        cdp.eval("document.querySelector('#nav [data-route=\"providers\"]').click(); 'ok'")
        time.sleep(0.4)
        cdp.eval("document.querySelector('#nav [data-route=\"cards\"]').click(); 'ok'")
        cdp.wait_for(f"!!document.querySelector('.cc[data-id=\"{html_card_id}\"]')", 20)
        cdp.eval(
            f"document.querySelector('.cc[data-id=\"{html_card_id}\"] [data-act=\"view\"]').click(); 'ok'"
        )
        cdp.wait_for("!!document.querySelector('#modal-root .modal')", 20)
        time.sleep(0.8)

        # ★ 渲染容器是一个 **iframe 沙箱**（sandbox="allow-same-origin"，没有 allow-scripts）：
        #   这样卡片自带的 <style> 不会污染控制台自身样式，但父页面仍能读高度。
        #   所以这里断言三件事：iframe 存在、真的在沙箱里、里面的 HTML 已经生效。
        frame_ok = cdp.eval(
            "(() => { const host = [...document.querySelectorAll('#modal-root [data-rich=\"greeting\"]')].pop();"
            " const f = host && host.querySelector('iframe.rich-frame');"
            " return f ? (f.getAttribute('sandbox') || '') : '__missing__'; })()"
        )
        probe.check(
            "10.10.HTML开场白",
            "★ 开场白渲染进 iframe 沙箱（有 allow-same-origin、无 allow-scripts）",
            frame_ok == "allow-same-origin",
            frame_ok,
        )
        inner_ok = cdp.eval(
            "(() => { const host = [...document.querySelectorAll('#modal-root [data-rich=\"greeting\"]')].pop();"
            " const f = host && host.querySelector('iframe');"
            " if (!f || !f.contentDocument) return '__unreadable__';"
            " const tds = f.contentDocument.querySelectorAll('table td');"
            " return tds.length > 1 ? tds[1].textContent.trim() : '__no-table__'; })()"
        )
        probe.check(
            "10.10.HTML开场白",
            "★ 卡片里的表格真的渲染出来了（读到 HP=42）",
            inner_ok == "42",
            inner_ok,
        )
        # 源码页签必须能切过去，且渲染视图被隐藏
        cdp.eval(
            "(() => { const host = [...document.querySelectorAll('#modal-root [data-rich=\"greeting\"]')].pop();"
            " const bar = host && host.querySelector('.segmented');"
            " if (bar) bar.querySelectorAll('button')[1].click(); return 'ok'; })()"
        )
        time.sleep(0.4)
        raw_state = cdp.eval(
            "(() => { const host = [...document.querySelectorAll('#modal-root [data-rich=\"greeting\"]')].pop();"
            " if (!host) return '__no-host__';"
            " const pre = host.querySelector('pre');"
            " const panes = host.querySelectorAll('div');"
            " const pane = [...panes].find(d => d.querySelector('iframe'));"
            " return JSON.stringify({"
            "   hasPre: !!pre, preHidden: pre ? pre.classList.contains('hidden') : null,"
            "   panes: panes.length, hasPane: !!pane,"
            "   paneHidden: pane ? pane.classList.contains('hidden') : null }); })()"
        )
        raw_ok = isinstance(raw_state, str) and (
            '"hasPre":true' in raw_state
            and '"preHidden":false' in raw_state
            and '"paneHidden":true' in raw_state
        )
        probe.check("10.10.HTML开场白", "能在「渲染视图 / 源码」之间切换", raw_ok, raw_state)

        # ---------- 10.11 ★ 备选开场白必须"一条一页"，不能挤在一起 ----------
        # 用户反馈：一条开场白本身就有很多行，全展开既没法看也分不清是第几条。
        alt_probe = (
            "(() => { const pos = [...document.querySelectorAll('#modal-root [data-alt-view-pos]')].pop();"
            " const host = [...document.querySelectorAll('#modal-root [data-rich=\"alt\"]')].pop();"
            " const f = host && host.querySelector('iframe');"
            " return JSON.stringify({ pos: pos ? pos.textContent.trim() : '__missing__',"
            "   text: f && f.contentDocument ? (f.contentDocument.body.textContent || '').trim() : '__missing__' }); })()"
        )
        state1 = cdp.eval(alt_probe)
        cdp.eval(
            "(() => { const b = [...document.querySelectorAll('#modal-root [data-alt-view=\"next\"]')].pop();"
            " if (b) b.click(); return 'ok'; })()"
        )
        time.sleep(0.6)
        state2 = cdp.eval(alt_probe)
        probe.check(
            "10.11.备选开场白翻页",
            "★ 翻页后页码与内容一起变化（第 1 条 → 第 2 条）",
            isinstance(state1, str)
            and isinstance(state2, str)
            and '"pos":"1"' in state1
            and '"pos":"2"' in state2
            and state1 != state2,
            f"{state1} -> {state2}",
        )
        cdp.eval(
            "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
            "   .forEach(b => b.click()); return 'ok'; })()"
        )
        time.sleep(0.4)
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)


    #
    # 用**合成 PointerEvent** 模拟一次真实拖动。注意 setPointerCapture 对合成事件
    # 会抛错，所以实现里用 try 包住了它 —— 那条路径必须能跑通，
    # 否则就成了"自动化测试里点不动、用户那里却是好的"，两边说法对不上。
    #
    # ★ 先切到别的页再切回来，确保这一次对话视图是**新渲染**的：
    #   如果此刻已经在对话页，点一下导航仍可能触发一次异步重渲染，
    #   而探针紧接着就 `querySelector('#chat-resize')` 拿到**旧节点**并派发合成事件 ——
    #   旧节点的监听器已随旧 view signal 一起 abort，于是表现成"拖不动 / 双击没反应"
    #   （真实踩到过：两条断言红，宽高一点没变。见 docs/pitfalls.md 第 35 条。）
    cdp.eval("document.querySelector('#nav [data-route=\"books\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('世界书')", 15)
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#chat-resize')", 15)
    time.sleep(0.5)
    width_before = cdp.num(
        "Math.round(document.querySelector('#chat-layout .chat-side')?.getBoundingClientRect().width ?? -1)"
    )
    drag = cdp.eval(
        "(() => {"
        "  const h = document.querySelector('#chat-resize');"
        "  const r = h.getBoundingClientRect();"
        "  const opt = (x, y) => ({ bubbles: true, pointerId: 1, pointerType: 'mouse',"
        "                          clientX: x, clientY: y, isPrimary: true });"
        "  h.dispatchEvent(new PointerEvent('pointerdown', opt(r.left + 10, r.top + 10)));"
        "  h.dispatchEvent(new PointerEvent('pointermove', opt(r.left + 80, r.top + 60)));"
        "  h.dispatchEvent(new PointerEvent('pointerup', opt(r.left + 80, r.top + 60)));"
        "  return 'ok';"
        "})()"
    )
    time.sleep(0.5)
    width_after = cdp.num(
        "Math.round(document.querySelector('#chat-layout .chat-side')?.getBoundingClientRect().width ?? -1)"
    )
    height_after = cdp.num(
        "Math.round(document.querySelector('#chat-layout')?.getBoundingClientRect().height ?? -1)"
    )
    probe.check(
        "11.拖动大小",
        "★ 拖动右下角手柄能改变会话列表宽度与对话区高度",
        drag == "ok" and width_after > width_before + 30 and height_after > 0,
        {"拖前宽": width_before, "拖后宽": width_after, "拖后高": height_after},
    )

    # 尺寸必须被记住：重新进入本页（视图重建）之后仍然是你调好的宽度
    cdp.eval("document.querySelector('#nav [data-route=\"books\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('世界书')", 15)
    time.sleep(0.4)
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#chat-resize')", 15)
    time.sleep(0.6)
    width_restored = cdp.num(
        "Math.round(document.querySelector('#chat-layout .chat-side')?.getBoundingClientRect().width ?? -1)"
    )
    probe.check(
        "11.拖动大小",
        "★ 重进页面后尺寸被记住（localStorage）",
        abs((width_restored or 0) - (width_after or 0)) <= 2,
        {"拖后宽": width_after, "重进后宽": width_restored},
    )

    # 双击手柄恢复默认
    cdp.eval(
        "document.querySelector('#chat-resize').dispatchEvent("
        "new MouseEvent('dblclick', { bubbles: true })); 'ok'"
    )
    time.sleep(0.5)
    width_reset = cdp.num(
        "Math.round(document.querySelector('#chat-layout .chat-side')?.getBoundingClientRect().width ?? -1)"
    )
    probe.check(
        "11.拖动大小",
        "双击手柄恢复默认大小（调歪了能一键回去）",
        abs((width_reset or 0) - 300) <= 3,
        {"恢复后宽": width_reset},
    )

    # ---------- 10.11 ★★ 状态栏字段来自角色卡 / 世界书（不是代码写死的一套）----------
    #
    # 用户反馈的原话："角色卡通常会有对应的状态栏，而不是全部通用一个，
    # 例如测试用的灯塔守夜人里的 HP 不适用于魔法少女魔法裁判，这是一个错误的设计。
    # 状态栏应该在角色卡里读取（大部分作者是把状态栏格式和要求放在世界书里面）。"
    #
    # 老实现把 hp/inventory/location/quests 全套写死在代码里，于是**没声明过 HP 的卡
    # 也长出 HP 血条**。这一节验两条路：
    #   ① 卡没声明 → 状态栏如实说"未定义"，提示词里也没有状态协议（不许硬塞 HP）
    #   ② 卡声明了 → 状态栏按声明的字段渲染（探针卡在 setup 里声明了 hp）
    with httpx.Client(timeout=30) as c:
        h = {"Authorization": f"Bearer {token}"}
        plain_card = c.post(
            f"{base}/api/v1/character-cards",
            headers=h,
            json={
                "name": f"探针无状态栏卡_{int(time.time())}",
                "description": "这张卡故意不声明状态栏格式",
                "greeting": "（她看了你一眼）",
            },
        )
        plain_card.raise_for_status()
        plain_card_id = plain_card.json()["data"]["id"]
        plain_card_name = plain_card.json()["data"]["name"]

    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#btn-new-session')", 15)
    time.sleep(0.6)
    cdp.eval("document.querySelector('#btn-new-session').click(); 'ok'")
    dialog_ready = cdp.wait_for("!!document.querySelector('#modal-root #sel-card')", 20)
    cdp.eval(
        "(() => { const s = document.querySelector('#modal-root #sel-card');"
        f" const opt = Array.from(s.options).find(o => o.textContent.includes('{plain_card_name}'));"
        " if (opt) s.value = opt.value;"
        " s.dispatchEvent(new Event('change', { bubbles: true }));"
        " const t = document.querySelector('#modal-root [name=title]');"
        " if (t) t.value = '无状态栏会话';"
        " return 'ok'; })()"
    )
    time.sleep(0.5)
    cdp.eval("document.querySelector('#modal-root #btn-create').click(); 'ok'")
    undefined_bar = cdp.wait_for(
        "(() => { const bar = document.querySelector('#state-bar');"
        " return !!bar && bar.textContent.includes('没有定义状态栏格式'); })()",
        30,
    )
    probe.check(
        "10.11.状态栏来自卡",
        "★ 卡没声明状态栏 → 状态栏如实说「没有定义状态栏格式」，不再凭空长出 HP",
        dialog_ready and undefined_bar,
        {
            "状态栏": (cdp.eval("document.querySelector('#state-bar')?.textContent || ''") or "")[:80],
            "有 HP 条": cdp.eval("!!document.querySelector('#state-bar .state-hp')") is True,
        },
    )
    # ★ 而且**提示词里也不能有**状态协议（没声明就不该要求模型输出状态块）
    cdp.eval("document.querySelector('[data-act=\"prompt\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root .modal')", 15)
    time.sleep(0.5)
    plain_prompt = cdp.eval("document.querySelector('#modal-root .modal')?.textContent || ''") or ""
    probe.check(
        "10.11.状态栏来自卡",
        "★ 卡没声明状态栏 → 提示词里既没有「当前状态」也没有输出契约（不硬塞 HP）",
        "当前状态（每轮必须同步）" not in plain_prompt
        and "[输出格式 · 必须遵守]" not in plain_prompt,
        plain_prompt[:120],
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)

    # ② 反向对照：声明了状态栏的卡，协议与状态栏都必须在。
    #    ★ 必须**新建**一条会话：探针的原始会话在第 10 步被故意删掉了。
    #    流程走真实的「新建会话」弹窗（列表会随之刷新并把新会话打开），
    #    状态则用与模型输出同一套的 PATCH 接口写进去 —— 这样断言的是
    #    "状态栏按卡的字段渲染"，而不是"模型这一轮恰好吐对了"。
    declared_sid = cdp.eval(
        "(async () => {"
        "  const token = localStorage.getItem('hne_access_token');"
        "  const h = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token };"
        "  const r = await fetch('/api/v1/narrative/sessions', { method: 'POST', headers: h,"
        f"    body: JSON.stringify({{ character_card_id: {data['card_id']},"
        f"      llm_provider_id: {data['provider_id']}, title: '状态栏探针会话' }}) }});"
        "  if (!r.ok) return 'HTTP ' + r.status;"
        "  const sid = (await r.json()).data.id;"
        "  const p = await fetch('/api/v1/narrative/sessions/' + sid + '/state',"
        "    { method: 'PATCH', headers: h,"
        "      body: JSON.stringify({ state: { hp: 88, max: 100, location: '灯室',"
        "        inventory: ['提灯'] } }) });"
        "  if (!p.ok) return 'PATCH HTTP ' + p.status;"
        "  return String(sid);"
        "})()"
    )
    declared_sid_num = int(declared_sid) if str(declared_sid or "").isdigit() else None
    cdp.eval("document.querySelector('#btn-new-session').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root #sel-card')", 20)
    cdp.eval(
        "(() => { const s = document.querySelector('#modal-root #sel-card');"
        f" const opt = Array.from(s.options).find(o => o.textContent.includes('探针角色'));"
        " if (opt) s.value = opt.value;"
        " s.dispatchEvent(new Event('change', { bubbles: true }));"
        " const t = document.querySelector('#modal-root [name=title]');"
        " if (t) t.value = '状态栏探针会话UI';"
        " return 'ok'; })()"
    )
    time.sleep(0.4)
    cdp.eval("document.querySelector('#modal-root #btn-create').click(); 'ok'")
    declared_ready = cdp.wait_for(
        "(() => { const bar = document.querySelector('#state-bar');"
        " return !!bar && bar.textContent.includes('HP'); })()",
        30,
    )
    declared_bar = cdp.eval("document.querySelector('#state-bar')?.textContent || ''") or ""
    probe.check(
        "10.11.状态栏来自卡",
        "★ 声明了状态栏的卡：状态栏显示它自己声明的字段（HP 条、位置、背包）",
        declared_ready and "hp" in declared_bar.lower(),
        {"状态栏": declared_bar[:80], "接口建的会话": declared_sid_num},
    )
    cdp.eval("document.querySelector('[data-act=\"prompt\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root .modal')", 15)
    time.sleep(0.5)
    declared_prompt = cdp.eval("document.querySelector('#modal-root .modal')?.textContent || ''") or ""
    probe.check(
        "10.11.状态栏来自卡",
        "★ 声明了状态栏的卡：提示词里带着它声明的字段清单",
        "当前状态（每轮必须同步）" in declared_prompt and "hp（数字，0~max）" in declared_prompt,
        declared_prompt[:120],
    )
    # ★ 混合检索（关键词 + 语义融合）必须**看得见**：加了融合/去重/重排之后，
    #   排序不再是"命中就进"，用户没法自己判断"为什么这条进来了、那条没有"。
    probe.check(
        "10.11.状态栏来自卡",
        "★「查看提示词」里有混合检索的计数与逐条来源（可解释，不许黑箱）",
        cdp.eval("!!document.querySelector('#retrieval-summary')") is True
        and "混合检索" in (cdp.eval("document.querySelector('#retrieval-summary')?.textContent || ''") or ""),
        (cdp.eval("document.querySelector('#retrieval-summary')?.textContent || ''") or "")[:100],
    )
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.4)
    declared_detail = cdp.eval(
        "(async () => {"
        "  const token = localStorage.getItem('hne_access_token');"
        f"  const r = await fetch('/api/v1/narrative/sessions/{declared_sid_num}', {{"
        "    headers: { Authorization: 'Bearer ' + token } });"
        "  if (!r.ok) return 'HTTP ' + r.status;"
        "  const d = (await r.json()).data;"
        "  return JSON.stringify({ src: d.state_schema?.source,"
        "    fields: (d.state_schema?.fields || []).map(f => f.name),"
        "    hp: d.state?.hp, has_max: 'max' in (d.state || {}) });"
        "})()"
    )
    try:
        declared_data = json.loads(declared_detail or "{}")
    except (TypeError, ValueError):
        declared_data = {}
    probe.check(
        "10.11.状态栏来自卡",
        "★ 会话落库的字段清单就来自这张卡（source=card，字段 hp/location/inventory）",
        declared_data.get("src") == "card"
        and declared_data.get("fields") == ["hp", "location", "inventory"],
        declared_detail,
    )

    # ---------- 12. 插件（声明式：注入 / 正则 / CSS 主题）----------
    # 用户要求"极简插件市场"。这一节验证四件事：
    #   1. 页面能开、默认有两个空壳插件
    #   2. 在界面上新建一个"提示词注入"插件 → 真的进入了发给模型的提示词（看预览）
    #   3. 新建一个"正则替换"插件 → 预览里的文本被替换（只改发给模型的内容）
    #   4. 新建一个"CSS 主题"插件 → 刷新后样式真的生效；停用后立刻失效
    #   5. 安装来源白名单：非 GitHub 地址被明确拒绝
    api = f"{base}/api/v1"
    auth_headers = {"Authorization": f"Bearer {token}"}
    # ★ 第 10 步故意把原会话删了，这里必须新建一个**有历史**的会话：
    #   预览要能看到世界书命中（正则那条断言靠它），所以先真发一句。
    with httpx.Client(timeout=30) as c:
        fresh = c.post(
            f"{api}/narrative/sessions",
            headers=auth_headers,
            json={
                "character_card_id": card_id,
                "llm_provider_id": provider_id,
                "title": "探针插件用会话",
            },
        )
        plugin_sid = fresh.json()["data"]["id"] if fresh.status_code in (200, 201) else session_id
        with c.stream(
            "GET",
            f"{api}/narrative/sessions/{plugin_sid}/stream",
            headers=auth_headers,
            params={"content": "灯塔还亮着吗"},
        ) as resp:
            for _chunk in resp.iter_text():
                pass

    cdp.eval("document.querySelector('#nav [data-route=\"plugins\"]').click(); 'ok'")
    opened = cdp.wait_for("(document.querySelector('#view h1')?.textContent || '') === '插件'", 20)
    cdp.wait_for("!!document.querySelector('#view .cc')", 20)
    probe.check(
        "12.插件",
        "★ 插件页能打开，并列出三个默认插件（2 个空壳 + 默认主题）",
        opened
        # ★ 第二十三轮：默认插件从 2 个变成 3 个（多了新账号默认启用的「星云暗涌」主题）。
        #   这是**意图变更**，不是把断言改松 —— 数量仍然被钉住。
        and (cdp.eval("document.querySelectorAll('#view .cc:not(.catalog-item)').length") or 0) == 3,
        cdp.eval("document.querySelectorAll('#view .cc:not(.catalog-item) .cc-name').length"),
    )
    probe.check(
        "12.插件",
        "插件页写明了安全边界（不执行第三方 JS / 只允许 GitHub）",
        "不执行任何第三方 JS" in (cdp.eval("document.querySelector('#plugin-note')?.textContent || ''") or ""),
        (cdp.eval("document.querySelector('#plugin-note')?.textContent || ''") or "")[:80],
    )

    # ★ 内置示例目录：对照 SillyTavern 的内置扩展挑出来的"声明式版本"，
    #   并且把**做不到**的那些如实列出来（用户明确要求"先放在插件市场里面"）。
    catalog_count = cdp.eval("document.querySelectorAll('#view .catalog-item').length") or 0
    page_text = cdp.eval("document.querySelector('#view')?.innerText || ''") or ""
    probe.check(
        "12.插件",
        "★ 内置示例目录列出了酒馆对照的示例，并如实写明做不到哪些内置扩展",
        catalog_count >= 5 and "Text To Speech" in page_text and "Quick Reply" in page_text,
        {"示例数": catalog_count, "提到 TTS": "Text To Speech" in page_text},
    )
    # 点「添加」→ 变成我自己的插件（目录本身不会自动生效）
    # ★ 按 key 精确点，不点"第一个"：目录会随版本增长，靠顺序的断言一定会被新条目搞坏
    cdp.eval(
        "document.querySelector('#view .catalog-item button[data-catalog-add=\"authors_note\"]')"
        ".click(); 'ok'"
    )
    added_ok = cdp.wait_for(
        "document.querySelectorAll('#view .cc:not(.catalog-item)').length === 3"
        " && !!document.querySelector('#view .catalog-item .badge.ok')",
        25,
    )
    probe.check(
        "12.插件",
        "★ 目录里的示例点「添加」后才进我的插件（并标成「已添加」）",
        added_ok,
        cdp.eval("document.querySelectorAll('#view .cc:not(.catalog-item)').length"),
    )

    # 界面新建一个「提示词注入」插件（走真实表单：选类型 → 填名字 → 填内容 → 保存）
    cdp.eval("document.querySelector('#btn-new-plugin').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#modal-root [name=name]')", 10)
    cdp.eval(
        "(() => {"
        " const s = document.querySelector('#modal-root [name=kind]');"
        " s.value = 'prompt'; s.dispatchEvent(new Event('change', { bubbles: true }));"
        " document.querySelector('#modal-root [name=name]').value = '探针注入';"
        " const t = document.querySelector('#modal-root [name=content]');"
        " t.value = '【探针插件】每轮结尾只写一句话。';"
        " return 'ok'; })()"
    )
    cdp.eval("document.querySelector('#btn-save-plugin').click(); 'ok'")
    created = cdp.wait_for(
        "Array.from(document.querySelectorAll('#view .cc .cc-name'))"
        ".some(n => n.textContent === '探针注入')",
        20,
    )
    probe.check("12.插件", "★ 在界面上能新建插件（表单 → 列表出现）", created)

    # 正则插件 + CSS 插件用接口建（界面上的正则编辑器由 pytest 覆盖，探针只验效果）
    with httpx.Client(timeout=30) as c:
        regex_created = c.post(
            f"{api}/plugins",
            headers=auth_headers,
            json={
                "name": "探针正则",
                "kind": "regex",
                "config": {"rules": [{"pattern": "灯塔", "replacement": "灯楼"}]},
            },
        )
        css_created = c.post(
            f"{api}/plugins",
            headers=auth_headers,
            json={
                "name": "探针主题",
                "kind": "css",
                "config": {"css": ":root{--brand:#7c3aed;}"},
            },
        )
    probe.check(
        "12.插件",
        "REST 接口能建正则/主题插件",
        regex_created.status_code == 201 and css_created.status_code == 201,
        {"regex": regex_created.status_code, "css": css_created.status_code},
    )

    # 预览里必须看到插件的效果（与真正发出去的是同一套装配）
    preview = httpx.get(
        f"{api}/narrative/sessions/{plugin_sid}",
        params={"with_prompt": "true"},
        headers=auth_headers,
        timeout=30,
    ).json()["data"]["prompt"]
    system = preview["system_prompt"]
    probe.check(
        "12.插件",
        "★ 提示词注入真的进了提示词（预览里能看到）",
        "【探针插件】每轮结尾只写一句话。" in system,
        system[-80:],
    )
    probe.check(
        "12.插件",
        "★ 正则替换只改「发给模型的内容」（预览里 灯塔 → 灯楼）",
        "灯楼" in system and "灯塔" not in system,
        {"含灯楼": "灯楼" in system, "含灯塔": "灯塔" in system},
    )

    # 刷新页面 → CSS 主题必须已经注入（app.js 启动时取 theme.css）
    cdp.send("Page.navigate", url=f"{base}/console/")
    cdp.wait_for("!!document.querySelector('#view h1')", 25)
    theme_ok = cdp.wait_for(
        "(() => { const el = document.getElementById('hne-plugin-theme');"
        " return !!el && el.textContent.includes('#7c3aed'); })()",
        20,
    )
    applied = cdp.eval(
        "getComputedStyle(document.documentElement).getPropertyValue('--brand').trim()"
    )
    probe.check(
        "12.插件",
        "★ CSS 主题插件在启动时真的生效（--brand 变成插件里的紫色）",
        theme_ok and applied == "#7c3aed",
        {"style 标签": theme_ok, "--brand": applied},
    )

    # ★ 换肤必须**当场生效**，不能让用户自己刷新。
    #   真实观感问题：用户启停 CSS 插件后界面没变，以为"插件没生效"。
    #   启停走的是 SPA 内的切换（不重新加载文档），所以这里断言：
    #   同一个 document 里，勾掉 → 立刻变回默认蓝；再勾上 → 立刻变回紫色。
    #   （用 `document` 的 identity 证明"真的没有重新加载"，见下面的 no_reload。）
    cdp.eval("document.querySelector('#nav [data-route=\"plugins\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view [data-view=\"plugins\"] input[data-act=\"toggle\"]')", 25)
    time.sleep(0.6)
    cdp.eval("window.__hne_probe_doc = document; 'ok'")
    cdp.eval(
        "(() => { const cc = Array.from(document.querySelectorAll('#view [data-view=\"plugins\"] .cc'))"
        "   .find(x => (x.querySelector('.cc-name')?.textContent || '').includes('探针主题'));"
        " const box = cc && cc.querySelector('input[data-act=\"toggle\"]');"
        " if (box && box.checked) box.click(); return 'ok'; })()"
    )
    time.sleep(1.2)
    off_brand = cdp.eval(
        "getComputedStyle(document.documentElement).getPropertyValue('--brand').trim()"
    )
    cdp.eval(
        "(() => { const cc = Array.from(document.querySelectorAll('#view [data-view=\"plugins\"] .cc'))"
        "   .find(x => (x.querySelector('.cc-name')?.textContent || '').includes('探针主题'));"
        " const box = cc && cc.querySelector('input[data-act=\"toggle\"]');"
        " if (box && !box.checked) box.click(); return 'ok'; })()"
    )
    time.sleep(1.2)
    on_brand = cdp.eval(
        "getComputedStyle(document.documentElement).getPropertyValue('--brand').trim()"
    )
    no_reload = cdp.eval("window.__hne_probe_doc === document")
    probe.check(
        "12.插件",
        "★ 启停 CSS 插件后换肤**当场生效**（不用刷新页面）",
        off_brand != "#7c3aed" and on_brand == "#7c3aed" and no_reload is True,
        {"停用后": off_brand, "启用后": on_brand, "未重新加载": no_reload},
    )


    # ★ 第十六轮：**深色主题必须是"整页深色"**（用户反馈："打开之后界面非常不好看，
    #   你注意那行白色的"）。根因是组件里写死了 `#fff`/`#fafbfc` —— 写死的地方
    #   换主题换不掉。修法是把它们收进 `--surface`/`--surface-2` 变量。
    #   这条断言就是"以后新增组件漏走变量"的守门人：暗色下任何一块浅色都会被抓到。
    #
    # ★ 这套采样改过两次，两个坑都踩过：
    #   ① **透明背景**：只比"backgroundColor 的亮度"会漏掉一切 `background:transparent`
    #      的块（它们的白来自父级或子文档）—— 所以现在把透明的元素**单独列出来报告**，
    #      而不是当它不存在。
    #   ② **品牌色被误判**：蓝色主按钮 `rgb(124,156,255)` 的亮度 158 > 140，
    #      于是"深色主题"这条断言会去骂一个本来就应该亮的设计色。
    #      现在按"和 --brand/--ok/--warn/--danger 相同就跳过"来放行，而不是按亮度放行。
    with httpx.Client(timeout=30) as c:
        added = c.post(f"{api}/plugins/catalog/dark_theme", headers=auth_headers)
        theme_id = (added.json().get("data") or {}).get("id") if added.status_code == 201 else None
        if theme_id:
            c.patch(
                f"{api}/plugins/{theme_id}", headers=auth_headers, json={"enabled": True}
            )
    cdp.send("Page.navigate", url=f"{base}/console/")
    cdp.wait_for("!!document.querySelector('#view h1')", 25)

    _SAMPLE_JS = (
        "(() => {"
        " const cs = getComputedStyle(document.documentElement);"
        " const hex2rgb = (h) => { const m = (h || '').trim().match(/^#([0-9a-f]{6})$/i);"
        "   if (!m) return ''; const n = parseInt(m[1], 16);"
        "   return 'rgb(' + ((n>>16)&255) + ', ' + ((n>>8)&255) + ', ' + (n&255) + ')'; };"
        " const allow = ['--brand','--brand-dark','--ok','--warn','--danger']"
        "   .map(n => hex2rgb(cs.getPropertyValue(n))).filter(Boolean);"
        " const lumOf = (r, g, b) => 0.299 * r + 0.587 * g + 0.114 * b;"
        # ★ 半透明必须**先合成再量**。这是第三个盲点：
        #   星云主题用的是 `rgba(124,156,255,.10)` 这种 10% 的光、按钮底是
        #   `rgba(255,255,255,.05)` —— 直接拿 RGB 算亮度会把它们判成"浅色块"
        #   （实测误报 3 条：body / .session-item / .btn.sec）。
        #   正确的语义是"**看起来**有多亮"：把颜色按 alpha 合成到底色上再算。
        #   底色取 body 的 background-color（主题的 --bg 就落在这一层）。
        " const baseM = (getComputedStyle(document.body).backgroundColor || '')"
        "   .match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);"
        " const BASE = baseM ? [+baseM[1], +baseM[2], +baseM[3]] : [255, 255, 255];"
        " const comp = (r, g, b, a) => lumOf("
        "   r * a + BASE[0] * (1 - a), g * a + BASE[1] * (1 - a), b * a + BASE[2] * (1 - a));"
        " const picks = ['body','.topbar','#view','.cc','.cc-top','.cc-body','.cc-foot',"
        "   '.cc-greeting','.cc-avatar','.btn.sec','.alert','.panel',"
        "   '.empty','.composer','.msg.assistant .msg-body','table.list th','.tag.plain',"
        "   '.badge.neutral','.session-item','.alt-box','.segmented button','.state-bar',"
        "   '.modal','.modal-body','.modal-foot','.modal-head'];"
        " const out = []; const skipped = []; const dots = [];"
        # ★ 把背景拆成一个个渐变**层**：一个元素的 background-image 可能是
        #   "很多层星点 + 两层星云"叠起来的，不能把所有色标混在一起取最大值。
        #   （`rgba(...)` 里也有逗号，所以不能按逗号 split，要按括号配对切。）
        " const layersOf = (s) => { const res = []; let i = 0;"
        "   while (true) { const re = /(?:radial|linear)-gradient\\(/g; re.lastIndex = i;"
        "     const f = re.exec(s); if (!f) break;"
        "     let d = 1, j = re.lastIndex;"
        "     while (j < s.length && d > 0) { const c = s[j];"
        "       if (c === '(') d++; else if (c === ')') d--; j++; }"
        "     res.push(s.slice(f.index, j)); i = j; }"
        "   return res; };"
        " const lumsOf = (layer) => { const ms = layer.match(/rgba?\\([^)]*\\)/g) || [];"
        "   const r = [];"
        "   for (const s of ms) { const mm = s.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);"
        "     if (!mm) continue; const a = mm[4] === undefined ? 1 : +mm[4];"
        "     if (a === 0) continue; r.push(comp(+mm[1], +mm[2], +mm[3], a)); }"
        "   return r; };"
        # ★ 点状高光 ≠ 浅色块：尺寸只有几个像素的径向渐变（星点）不构成"一块白"，
        #   但如果把它算进"最大值"，一整片星空的 80% 白点会把 body 判成亮块（实测 207）。
        #   所以按声明的尺寸分流：≤ 4px 的算点状，单独报告、不参与"亮块"判定。
        " const isDot = (layer) => { const m = layer.match(/gradient\\(\\s*([\\d.]+)px\\s+([\\d.]+)px/);"
        "   return !!m && +m[1] <= 4 && +m[2] <= 4; };"
        " for (const sel of picks) {"
        "   const el = document.querySelector(sel); if (!el) { skipped.push(sel + ':缺'); continue; }"
        "   const st = getComputedStyle(el);"
        "   const bg = st.backgroundColor || ''; const img = st.backgroundImage || '';"
        "   const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?\\)/);"
        "   const solid = !!m && (m[4] === undefined || +m[4] > 0);"
        # ★ 渐变必须单独解析：它画在 background-image 上，backgroundColor 读出来是**透明**的。
        #   `.cc-greeting`（角色卡的开场白预览框）就是这么漏掉的 ——
        #   "只比 backgroundColor 亮度"会把一整块浅色渐变当成透明块跳过。
        "   if (img && img.indexOf('gradient') >= 0) {"
        "     const fill = []; const dotLums = [];"
        "     for (const L of layersOf(img)) {"
        "       const ls = lumsOf(L); if (!ls.length) continue;"
        "       const top = Math.max.apply(null, ls);"
        "       if (isDot(L)) dotLums.push(top); else fill.push(top); }"
        "     if (dotLums.length) dots.push(sel + '=' + Math.round(Math.max.apply(null, dotLums)));"
        "     if (!fill.length) { skipped.push(sel + (dotLums.length ? ':仅点状高光' : ':渐变不可解析')); continue; }"
        "     out.push(sel + '=grad' + Math.round(Math.max.apply(null, fill)));"
        "     continue; }"
        "   if (!solid) { skipped.push(sel + ':透明'); continue; }"
        "   if (allow.includes(bg)) { skipped.push(sel + ':品牌色'); continue; }"
        "   const a = m[4] === undefined ? 1 : +m[4];"
        "   out.push(sel + '=' + Math.round(comp(+m[1], +m[2], +m[3], a))); }"
        " return out.join(',') + '|' + skipped.join(',') + '|' + dots.join(','); })()"
    )
    sample_raw = str(cdp.eval(_SAMPLE_JS) or "")
    painted_raw, _, rest_raw = sample_raw.partition("|")
    skipped_raw, _, dots_raw = rest_raw.partition("|")
    samples = {}
    for item in painted_raw.split(","):
        if "=" in item:
            key, _, value = item.partition("=")
            samples[key.strip()] = int(value.replace("grad", ""))
    bright = {k: v for k, v in samples.items() if v > 140}
    probe.check(
        "12.插件",
        "★ 深色主题下**不许出现浅色块**（组件颜色必须走变量，不能写死 #fff）",
        len(samples) >= 3 and not bright,
        {"采样": samples, "仍偏亮": bright, "跳过": skipped_raw, "点状高光": dots_raw},
    )

    # ★ 角色卡页的 HTML 开场白是塞进 **iframe** 渲染的 —— iframe 是一份独立文档，
    #   父页面的 CSS 变量不会继承进去。这里曾经写死 `background:#fff`，
    #   于是"深色主题下角色卡页永远有一大块白"（用户反复反馈的那块白）。
    #   透明背景的采样在上面只会被标成"透明"，抓不到它，所以这里**单独把 iframe 拆开量**。
    if html_card_id is not None:
        cdp.eval("document.querySelector('#nav [data-route=\"cards\"]').click(); 'ok'")
        cdp.wait_for(f"!!document.querySelector('.cc[data-id=\"{html_card_id}\"]')", 25)
        cdp.eval(
            f"document.querySelector('.cc[data-id=\"{html_card_id}\"] [data-act=\"view\"]').click(); 'ok'"
        )
        cdp.wait_for("!!document.querySelector('#modal-root iframe.rich-frame')", 25)
        time.sleep(0.8)
        frame_raw = str(
            cdp.eval(
                "(() => { const f = [...document.querySelectorAll('#modal-root iframe.rich-frame')].pop();"
                " if (!f || !f.contentDocument || !f.contentDocument.body) return '读不到 iframe 文档';"
                " const st = getComputedStyle(f.contentDocument.body);"
                " return st.backgroundColor + '|' + st.color; })()"
            )
            or ""
        )
        frame_bg, _, frame_fg = frame_raw.partition("|")
        fb = re.search(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", frame_bg)
        ff = re.search(r"rgba?\((\d+),\s*(\d+),\s*(\d+)", frame_fg)
        frame_lum = (
            0.299 * int(fb.group(1)) + 0.587 * int(fb.group(2)) + 0.114 * int(fb.group(3))
            if fb
            else 255.0
        )
        frame_text_lum = (
            0.299 * int(ff.group(1)) + 0.587 * int(ff.group(2)) + 0.114 * int(ff.group(3))
            if ff
            else 0.0
        )
        probe.check(
            "12.插件",
            "★ 深色主题下，角色卡 HTML 开场白的 iframe 底色也要变深（不许写死 #fff）",
            (fb is not None and frame_lum < 110) and frame_text_lum > 110,
            {"iframe 底": frame_bg, "亮度": round(frame_lum), "正文色": frame_fg},
        )
        # ★ 再在**用户报的那一页**上量一遍：角色卡列表 + 已打开的详情弹窗。
        #   上面那次是在对话页量的，`.cc`（卡片）/`.modal`（弹窗）这些选择器在那边
        #   根本不存在 —— 只报"缺"就等于没测到这里，而白块恰恰出在角色卡页。
        cards_raw = str(cdp.eval(_SAMPLE_JS) or "")
        cards_painted, _, cards_skipped = cards_raw.partition("|")
        cards_samples = {}
        for item in cards_painted.split(","):
            if "=" in item:
                key, _, value = item.partition("=")
                cards_samples[key.strip()] = int(value.replace("grad", ""))
        cards_bright = {k: v for k, v in cards_samples.items() if v > 140}
        probe.check(
            "12.插件",
            "★ 深色主题下，角色卡页（含详情弹窗）也不许出现浅色块",
            # `.cc-greeting` 必须**在采样里**：它就是用户报的那块白
            # （开场白预览框的浅色渐变），漏掉它这条断言等于没测。
            {".cc", ".cc-greeting", ".modal"}.issubset(cards_samples) and not cards_bright,
            {"采样": cards_samples, "仍偏亮": cards_bright, "跳过": cards_skipped},
        )
        cdp.eval(
            "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
            "   .forEach(b => b.click()); return 'ok'; })()"
        )
        time.sleep(0.4)

    # 用完立刻收回：后面几节还要在浅色主题下跑（否则截图与断言都变样）
    with httpx.Client(timeout=30) as c:
        if theme_id:
            c.patch(f"{api}/plugins/{theme_id}", headers=auth_headers, json={"enabled": False})
            c.delete(f"{api}/plugins/{theme_id}", headers=auth_headers)
    cdp.send("Page.navigate", url=f"{base}/console/")
    cdp.wait_for("!!document.querySelector('#view h1')", 25)

    # ---------- 12.8 ★「云梦枢 · 星云暗涌」：默认视觉方案的验收 + 留档截图 ----------
    #
    # 这是云梦枢的**主视觉**（深空底 + 星云 + 毛玻璃 + 细线微光），单独验三件事：
    #   1. 目录里确实有这条主题，加进来就能启用（用户一键可开关、完全可逆）；
    #   2. 星空渐变与顶栏毛玻璃**真的生效**（不是只写了个变量）；
    #   3. 整页没有浅色块 —— 这一条对它尤其重要：面板是**半透明**的，
    #      半透明底如果写错（比如 --surface 给了浅色），看起来同样是"一块白"，
    #      而只比"背景色亮度"的采样会把 rgba(255,255,255,.6) 也算进去 ✔
    def _save_shot(name: str, doc_name: str | None = None) -> None:
        shot = cdp.send("Page.captureScreenshot", format="png")
        raw = base64.b64decode(shot["data"])
        (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
        (PROJECT_ROOT / "data" / name).write_bytes(raw)
        if doc_name:
            # ★ README / 论文素材引用的是 docs/ 下那几张
            (PROJECT_ROOT / "docs").mkdir(parents=True, exist_ok=True)
            (PROJECT_ROOT / "docs" / doc_name).write_bytes(raw)

    with httpx.Client(timeout=30) as c:
        # ★★ 第二十三轮起「星云暗涌」是**新账号的默认主题**（`plugin_service.ensure_defaults`
        #   会在账号第一次被初始化时装上并 enabled=True）。所以这里**不能再"从目录添加"** ——
        #   那会返回 400「已经是最新版」，于是 neb_id 为 None，本节后续的视觉断言全部失败
        #   （实测：三条 12.插件 检查报红，其中一条就是这个）。
        #   正确做法：先去插件列表里找**已经存在的**那一个，记下它原本的 enabled 状态，
        #   本节临时打开、末尾恢复原状 —— 探针不该改变被探对象的最终状态。
        listed = c.get(f"{api}/plugins", headers=auth_headers).json()["data"]
        existing = next(
            (item for item in listed["items"] if item["kind"] == "css" and "星云暗涌" in item["name"]),
            None,
        )
        neb_id = existing["id"] if existing else None
        neb_was_enabled = bool(existing and existing.get("enabled"))
        if neb_id:
            c.patch(f"{api}/plugins/{neb_id}", headers=auth_headers, json={"enabled": True})
    probe.check(
        "12.插件",
        "★ 新账号默认自带「云梦枢 · 星云暗涌」（云梦枢默认视觉方案，一键可开关）",
        neb_id is not None,
        {"插件id": neb_id, "原启用状态": neb_was_enabled},
    )

    cdp.send("Page.navigate", url=f"{base}/console/")
    cdp.wait_for("!!document.querySelector('#view h1')", 25)
    # ★ 用**用户的实际窗口尺寸**留档与采样（他反馈"背景看不出变化"时给的就是 1920×1020）：
    #   星点只有 2px 上下，视口/缩放的差别会直接影响"看不看得见"，
    #   所以测量必须在同一个尺寸下做，否则我量的和他看到的不是一回事。
    cdp.send(
        "Emulation.setDeviceMetricsOverride",
        width=1920,
        height=1020,
        deviceScaleFactor=1,
        mobile=False,
    )
    time.sleep(0.6)
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view .session-item')", 25)
    cdp.eval(
        "(() => { const it = document.querySelector('#view .session-item');"
        " if (it) it.click(); return 'ok'; })()"
    )
    cdp.wait_for("!!document.querySelector('#messages .msg')", 25)
    time.sleep(1.2)

    neb_visuals = cdp.eval(
        "(() => {"
        " const body = getComputedStyle(document.body);"
        " const top = getComputedStyle(document.querySelector('.topbar'));"
        " return {"
        "   nebula: (body.backgroundImage || '').includes('radial-gradient'),"
        "   glass: (top.backdropFilter || top.webkitBackdropFilter || 'none') !== 'none',"
        "   logo: !!document.querySelector('.brand-logo')"
        "     && document.querySelector('.brand-logo').naturalWidth > 0,"
        "   name: (document.querySelector('.brand-text')?.textContent || '').trim()"
        " }; })()"
    ) or {}
    probe.check(
        "12.插件",
        "★ 星云渐变的背景 + 顶栏毛玻璃 + 新图标都真的生效（不是只写了个变量）",
        bool(neb_visuals.get("nebula"))
        and bool(neb_visuals.get("glass"))
        and bool(neb_visuals.get("logo"))
        and neb_visuals.get("name") == "云梦枢",
        neb_visuals,
    )

    # ★ 主题的覆盖**到底有没有赢**：靠肉眼看截图判断不了"20% 透明紫"和"实心紫"的区别，
    #   所以直接读计算样式。这条同时守住两类事故：
    #   ① 覆盖语句没生效（选择器优先级/注入顺序变了）→ 选中态又会变回实心大色块；
    #   ② 有人把主题里这些规则删了。
    neb_overrides = cdp.eval(
        "(() => {"
        " const seg = document.querySelector('.segmented button.active');"
        " const it = document.querySelector('.session-item.active');"
        " const av = document.querySelector('.si-avatar');"
        " const ub = document.querySelector('.msg.user .msg-body');"
        " const ab = document.querySelector('.msg.assistant .msg-body');"
        # ★ 状态栏只在**对话页**存在（角色卡页没有）—— 采样点必须跟页面匹配，
        #   否则会得到空字符串，然后你会以为是 CSS 没生效（这次真的这么误判了一轮）。
        " const sb = document.querySelector('.state-bar');"
        " const g = (el, p) => el ? (getComputedStyle(el)[p] || '') : '';"
        " return {"
        "   tabBg: g(seg, 'backgroundColor'),"
        "   itemBg: g(it, 'backgroundColor'),"
        "   itemShadow: g(it, 'boxShadow'),"
        "   avatarRadius: g(av, 'borderRadius'),"
        "   userBorder: g(ub, 'borderTopColor'),"
        "   aiBorder: g(ab, 'borderTopColor'),"
        "   stateGlow: g(sb, 'boxShadow')"
        " }; })()"
    ) or {}
    probe.check(
        "12.插件",
        "★ 主题覆盖真的生效：选中态是**半透明紫**（不是实心大色块）、头像圆形、气泡紫/青分工",
        "167, 139, 250, 0.2" in str(neb_overrides.get("tabBg"))
        and "167, 139, 250, 0.15" in str(neb_overrides.get("itemBg"))
        # ★ 计算样式里 `#a78bfa` 会被浏览器归一化成 `rgb(167, 139, 250)`，
        #   所以判据用 rgb 三通道，而不是十六进制（第一版写成 hex，断言假红了一次）。
        and "167, 139, 250" in str(neb_overrides.get("itemShadow"))
        and str(neb_overrides.get("avatarRadius")).startswith("50%")
        and "167, 139, 250" in str(neb_overrides.get("userBorder"))
        and "0, 229, 255" in str(neb_overrides.get("aiBorder"))
        # 状态栏那条"还没有状态…"的青色外发光（创新点要被一眼看到）
        and "0, 229, 255" in str(neb_overrides.get("stateGlow")),
        neb_overrides,
    )

    neb_raw = str(cdp.eval(_SAMPLE_JS) or "")
    neb_painted, _, neb_rest = neb_raw.partition("|")
    neb_skipped, _, neb_dots = neb_rest.partition("|")
    neb_samples = {}
    for item in neb_painted.split(","):
        if "=" in item:
            key, _, value = item.partition("=")
            neb_samples[key.strip()] = int(value.replace("grad", ""))
    neb_bright = {k: v for k, v in neb_samples.items() if v > 140}
    probe.check(
        "12.插件",
        "★ 星云暗涌下同样不许出现浅色块（半透明面板也要守）",
        len(neb_samples) >= 3 and not neb_bright,
        {"采样": neb_samples, "仍偏亮": neb_bright, "跳过": neb_skipped, "点状高光": neb_dots},
    )
    # ★ 星空要"看得见，但不抢戏"。这里量的是**CSS 声明层面的峰值**（点状高光的颜色
    #   按 alpha 合成到底色后的亮度）。上限取**正文文字的亮度（约 230）**——
    #   判据不是拍脑袋的数字，而是"背景上的任何一点都不得亮过正文"。
    #   下限防"淡到看不见"（第一版就是这样：背景区最亮只有 31，用户直接说"看不出变化"）。
    #   ★ 它与**渲染后的像素峰值**不是一回事：星点只有 2~4px，抗锯齿会把峰值摊开。
    neb_star_lums = [
        int(item.partition("=")[2])
        for item in str(neb_dots).split(",")
        if "=" in item and item.partition("=")[2].strip().isdigit()
    ]
    star_peak = max(neb_star_lums) if neb_star_lums else 0
    probe.check(
        "12.插件",
        "★ 背景星点「看得见但不抢戏」：合成峰值 40~230（下限=不能看不见，上限=不得亮过正文）",
        40 <= star_peak <= 230,
        {"星点层": neb_dots, "合成峰值": star_peak},
    )
    _save_shot("ui_nebula_chat.png", "nebula-screenshot.png")

    # 角色卡页也留一张：卡片网格最能看出"毛玻璃 + 深空底"的整体质感
    cdp.eval("document.querySelector('#nav [data-route=\"cards\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view .cc')", 25)
    time.sleep(1.0)
    _save_shot("ui_nebula_cards.png", "nebula-cards-screenshot.png")

    # ★ 星辉细节 + "魔法卡牌"悬停：全部读**计算样式**，不靠肉眼。
    #   悬停必须用真实鼠标事件（`Input.dispatchMouseEvent`）—— `:hover` 不是 JS 能设的。
    neb_stars = cdp.eval(
        "(() => {"
        " const b = getComputedStyle(document.body);"
        " const nav = document.querySelector('.nav-item.active');"
        " const av = document.querySelector('#view .cc-avatar');"
        " const g = (el, p) => el ? (getComputedStyle(el)[p] || '') : '';"
        " return {"
        "   starLayers: (b.backgroundImage.match(/radial-gradient/g) || []).length,"
        "   starTile: ((b.backgroundSize || '').includes('190px')"
        "     && (b.backgroundSize || '').includes('460px')),"
        "   starTiers: new Set(((b.backgroundSize || '').match(/\\d+px \\d+px/g) || [])).size,"
        "   navLine: g(nav, 'backgroundImage'),"
        "   navLinePos: g(nav, 'backgroundPosition'),"
        "   avatarRadius: g(av, 'borderRadius')"
        " }; })()"
    ) or {}
    card_box = cdp.eval(
        "(() => { const c = document.querySelector('#view .cc'); if (!c) return null;"
        " const r = c.getBoundingClientRect();"
        " return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)}; })()"
    )
    hover_transform = ""
    if isinstance(card_box, dict):
        cdp.send(
            "Input.dispatchMouseEvent",
            type="mouseMoved",
            x=card_box["x"],
            y=card_box["y"],
        )
        time.sleep(0.7)
        hover_transform = str(
            cdp.eval("getComputedStyle(document.querySelector('#view .cc')).transform") or ""
        )
    probe.check(
        "12.插件",
        "★ 星辉细节：背景星点层（三档 tile）/ 导航中间亮起的极光青线 / 圆形卡片头像",
        int(neb_stars.get("starLayers") or 0) >= 30
        and bool(neb_stars.get("starTile"))
        and int(neb_stars.get("starTiers") or 0) >= 3
        and "0, 229, 255" in str(neb_stars.get("navLine"))
        and "50%" in str(neb_stars.get("navLinePos"))
        and str(neb_stars.get("avatarRadius")).startswith("50%"),
        neb_stars,
    )
    probe.check(
        "12.插件",
        "★ 「魔法卡牌」悬停：鼠标移上去卡片真的上浮 3px（用真实鼠标事件验的）",
        "-3" in hover_transform,
        {"transform": hover_transform, "鼠标位置": card_box},
    )

    # 插件页也留一张（看卡片毛玻璃与目录）
    cdp.eval("document.querySelector('#nav [data-route=\"plugins\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '') === '插件'", 25)
    time.sleep(1.0)
    _save_shot("ui_nebula_plugins.png", "nebula-plugins-screenshot.png")

    # 收回：后面几节还在浅色主题下跑
    # ★ 恢复**原状**而不是一律禁用：这个账号的默认主题本来就是启用的，
    #   一律禁用会让"探针跑完之后用户界面变了"（虽然只影响测试账号，但那是坏习惯）。
    with httpx.Client(timeout=30) as c:
        if neb_id:
            c.patch(f"{api}/plugins/{neb_id}", headers=auth_headers,
                    json={"enabled": neb_was_enabled})
            if not neb_was_enabled:
                c.delete(f"{api}/plugins/{neb_id}", headers=auth_headers)
    cdp.send("Page.navigate", url=f"{base}/console/")
    cdp.wait_for("!!document.querySelector('#view h1')", 25)


    with httpx.Client(timeout=30) as c:
        # ★ 这一次的判据从"硬编码总数 == 6"改成**比较拒绝前后的数量**。
        #   为什么：硬编码的数字随"默认插件有几个"变化（第二十三轮加了默认主题，
        #   这里就从 6 变成 7，于是报红）。而这条检查真正要守的是
        #   **"被拒绝的安装不能留下任何残留"** —— 用前后对比表达这个意图，
        #   既不依赖默认数量，也比原来更严格（原来只查最终值）。
        total_before = c.get(f"{api}/plugins", headers=auth_headers).json()["data"]["total"]
        rejected = c.post(
            f"{api}/plugins/install",
            headers=auth_headers,
            json={"url": "https://gitlab.com/someone/plugin/blob/main/p.json"},
        )
        total_after = c.get(f"{api}/plugins", headers=auth_headers).json()["data"]["total"]
    probe.check(
        "12.插件",
        "★ 安装来源只允许 GitHub（其它域名被明确拒绝且不留残留）",
        rejected.status_code == 400
        and "只允许从 GitHub" in rejected.text
        and total_after == total_before,
        {"状态": rejected.status_code, "拒绝前": total_before, "拒绝后": total_after},
    )

    # 停用正则插件 → 预览里立刻恢复原样（"停用即失效"）
    regex_id = regex_created.json()["data"]["id"]
    with httpx.Client(timeout=30) as c:
        c.patch(f"{api}/plugins/{regex_id}", headers=auth_headers, json={"enabled": False})
    system_off = httpx.get(
        f"{api}/narrative/sessions/{plugin_sid}",
        params={"with_prompt": "true"},
        headers=auth_headers,
        timeout=30,
    ).json()["data"]["prompt"]["system_prompt"]
    probe.check(
        "12.插件",
        "★ 停用插件后立刻不再生效（灯塔 回来了）",
        "灯塔" in system_off and "灯楼" not in system_off,
        {"含灯塔": "灯塔" in system_off, "含灯楼": "灯楼" in system_off},
    )

    # 顺手清掉探针自己造的插件与会话，避免反复跑越堆越多。
    # ★ 默认的两个空壳**不动**（它们有"只发一次"的墓碑位，删了不会重建）；
    #   这里只删：探针自己新建的（名字带"探针"）与从目录添加的那一条。
    with httpx.Client(timeout=30) as c:
        for item in c.get(f"{api}/plugins", headers=auth_headers).json()["data"]["items"]:
            if item["name"].startswith("探针") or item["name"].startswith("作者注"):
                c.delete(f"{api}/plugins/{item['id']}", headers=auth_headers)
        if plugin_sid != session_id:
            c.delete(f"{api}/narrative/sessions/{plugin_sid}", headers=auth_headers)

    # ---------- 12.x ★ 会话在"背后"被删掉：重进对话页不许留下红色 404 ----------
    #
    # 真实场景：删角色卡时勾了「连同对话记录一起删除」，或在另一个标签页删了会话，
    # 而本页还记着那个会话 id。以前重进对话页会**无条件**再请求一次已删的会话 →
    # 浏览器控制台留下一条红色 404（探针每轮都报这一条，用户看到会以为前端坏了）。
    # 修法：会话列表本来就已经取回来了，**先查再请求**。
    # 这里刻意用真实路径复现：REST 删掉 → 切走 → 切回对话页。
    _deleted_url = f"sessions/{plugin_sid}"
    _before = len([e for e in probe.console_errors if _deleted_url in e])
    cdp.eval("document.querySelector('#nav [data-route=\"cards\"]').click(); 'ok'")
    time.sleep(0.6)
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('叙事会话')", 25)
    time.sleep(1.6)
    _after = len([e for e in probe.console_errors if _deleted_url in e])
    # ★ 只断言"没有多打一次注定 404 的请求"：
    #   界面那句「这个会话已经不在了」由第 10 节负责（那里是另一条路径：点刷新按钮），
    #   两条断言各守一条路径，别混在一起。
    probe.check(
        "12.插件",
        "★ 会话在背后被删掉后重进对话页：不再白打一次注定 404 的请求",
        _after == _before,
        {"新增 404": _after - _before, "会话": plugin_sid},
    )

    # ---------- 13. 骰子插件（随机数由**后端**掷，模型只负责叙述）----------
    # ★ 为什么要做这个插件：大模型没有真随机 —— 需要成功就写 18、需要失败就写 3。
    #   这一节验证四件事：
    #     1. 内置目录一键添加「跑团骰点」，编辑表单里有它**自己的**设置项
    #     2. 在输入框里真掷一次（`/r 1d100 聆听`）→ 消息底下出现骰子气泡
    #     3. 点数**落库**：刷新重开页面后还是同一个数字（骰点绝不重掷）
    #     4. 点数与「查看提示词」里的《本轮骰点》一致，且系统提示词里带着
    #        「你没有随机数能力 / 用 <roll> 请求」的规则
    dice_sid: int | None = None
    with httpx.Client(timeout=30) as c:
        fresh_dice = c.post(
            f"{api}/narrative/sessions",
            headers=auth_headers,
            json={
                "character_card_id": card_id,
                "llm_provider_id": provider_id,
                "title": "探针骰子用会话",
            },
        )
        if fresh_dice.status_code in (200, 201):
            dice_sid = int(fresh_dice.json()["data"]["id"])
    probe.check(
        "13.骰子",
        "骰子用会话建好了（下面用它真掷一次）",
        dice_sid is not None,
        dice_sid,
    )

    cdp.eval("document.querySelector('#nav [data-route=\"plugins\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '') === '插件'", 20)
    cdp.wait_for("!!document.querySelector('#view .catalog-item')", 20)
    cdp.eval(
        "document.querySelector('#view .catalog-item button[data-catalog-add=\"trpg_dice\"]')"
        ".click(); 'ok'"
    )
    dice_added = cdp.wait_for(
        "Array.from(document.querySelectorAll('#view .cc:not(.catalog-item) .cc-name'))"
        ".some(n => n.textContent.includes('跑团骰点'))",
        25,
    )
    dice_card = (
        cdp.eval(
            "(() => { const cards = Array.from(document.querySelectorAll('#view .cc:not(.catalog-item)'));"
            " const card = cards.find(x => (x.querySelector('.cc-name')?.textContent || '').includes('跑团骰点'));"
            " return card ? card.innerText : ''; })()"
        )
        or ""
    )
    probe.check(
        "13.骰子",
        "★ 内置目录一键添加「跑团骰点」，卡片上如实写出触发词与默认表达式",
        dice_added and "触发词" in dice_card and "1d100" in dice_card,
        dice_card[:70],
    )

    # 编辑表单：骰子有**自己的**设置项（不是把配置塞进一个通用 JSON 输入框）
    cdp.eval(
        "(() => { const cards = Array.from(document.querySelectorAll('#view .cc:not(.catalog-item)'));"
        " const card = cards.find(x => (x.querySelector('.cc-name')?.textContent || '').includes('跑团骰点'));"
        " card.querySelector('button[data-act=edit]').click(); return 'ok'; })()"
    )
    dice_form = cdp.wait_for(
        "['triggers','default_expr','max_dice','max_sides','allow_model_roll','show_detail','explain']"
        ".every(n => !!document.querySelector(`#modal-root [name=${n}]`))",
        15,
    )
    probe.check(
        "13.骰子",
        "★ 骰子插件的编辑表单有它自己的设置项（触发词 / 默认表达式 / 上限 / 三个开关）",
        dice_form,
        cdp.eval("document.querySelector('#modal-root [name=triggers]')?.value"),
    )
    cdp.eval("document.querySelector('#modal-root [data-close]').click(); 'ok'")

    # 界面上真掷一次
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('叙事会话')", 15)
    cdp.wait_for(f"!!document.querySelector('.session-item[data-id=\"{dice_sid}\"]')", 25)
    cdp.eval(f"document.querySelector('.session-item[data-id=\"{dice_sid}\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#btn-send')", 20)
    cdp.eval(
        "(() => { const i = document.querySelector('#composer-input');"
        " i.value = '/r 1d100 聆听';"
        " i.dispatchEvent(new Event('input', { bubbles: true })); return 'ok'; })()"
    )
    cdp.eval("document.querySelector('#btn-send').click(); 'ok'")
    rolled = cdp.wait_for(
        "(() => { const chip = document.querySelector('#messages .roll-chip');"
        " return !!chip && /1d100\\s*=\\s*\\d+/.test(chip.textContent || ''); })()",
        60,
    )
    chip_text = (
        cdp.eval(
            "(() => { const chip = document.querySelector('#messages .roll-chip');"
            " return chip ? chip.textContent.trim() : ''; })()"
        )
        or ""
    )
    probe.check(
        "13.骰子",
        "★ 输入框里写 /r 1d100，消息底下出现**后端掷出**的骰子气泡（🎲）",
        rolled and "🎲" in chip_text and "1d100" in chip_text,
        chip_text,
    )
    digits = re.search(r"=\s*(\d+)", chip_text)
    roll_total = int(digits.group(1)) if digits else None

    # 点数落库：界面上的数字、接口返回的数字、预览里的数字，三者必须一致
    dice_detail = httpx.get(
        f"{api}/narrative/sessions/{dice_sid}",
        params={"with_prompt": "true"},
        headers=auth_headers,
        timeout=30,
    ).json()["data"]
    user_rows = [m for m in dice_detail["messages"] if m["role"] == "user"]
    stored_roll = ((user_rows[-1].get("rolls") or [{}])[0]) if user_rows else {}
    dice_system = ((dice_detail.get("prompt") or {}).get("system_prompt") or "")
    probe.check(
        "13.骰子",
        "★ 点数落库：界面气泡与接口返回的是同一个数字（还带着说明「聆听」）",
        roll_total is not None
        and stored_roll.get("total") == roll_total
        and stored_roll.get("label") == "聆听",
        {"界面": roll_total, "接口": stored_roll.get("total"), "说明": stored_roll.get("label")},
    )
    probe.check(
        "13.骰子",
        "★ 「查看提示词」里的本轮骰点与点数一致（预览不重掷，与真实请求同一份）",
        "## 本轮骰点" in dice_system
        and roll_total is not None
        and f"**{roll_total}**" in dice_system,
        {"含本轮骰点": "## 本轮骰点" in dice_system, "点数": roll_total},
    )
    probe.check(
        "13.骰子",
        "★ 规则进了系统提示词：模型被告知「没有随机数能力」并要用 <roll> 请求",
        "<roll>" in dice_system and "随机数" in dice_system,
        dice_system[-70:],
    )

    # 刷新重开页面 → 点数必须还是那个数字（落库而不是"每次渲染现掷"）
    cdp.send("Page.navigate", url=f"{base}/console/")
    cdp.wait_for("!!document.querySelector('#view h1')", 25)
    cdp.wait_for(f"!!document.querySelector('.session-item[data-id=\"{dice_sid}\"]')", 25)
    cdp.eval(f"document.querySelector('.session-item[data-id=\"{dice_sid}\"]').click(); 'ok'")
    dice_again = cdp.wait_for(
        "(() => { const chip = document.querySelector('#messages .roll-chip');"
        f" return !!chip && (chip.textContent || '').includes('= {roll_total}'); }})()",
        25,
    )
    probe.check(
        "13.骰子",
        "★ 刷新重开页面后点数不变（骰点落库，绝不重掷）",
        dice_again,
        chip_text,
    )
    # ★ 收尾把界面停在**插件页**，而不是对话页。下一节「纯聊天」的写法是
    #   "点左侧导航 → 立刻点 + 纯聊天"，点导航会触发一次**异步**的视图重渲染：
    #   如果此刻已经在对话页，重渲染会和那次点击撞在一起 —— 弹窗拿到的是**旧**的
    #   view signal，等重渲染把它 abort 掉之后，「创建并开始」就会走
    #   `if (signal?.aborted) return;`：会话其实建好了，界面却毫无反应。
    #   （这次真的踩到了：纯聊天会话没被打开，两条断言变红。）
    #   停在别的页面上，下一节的"点导航 + 等标题"才会等出一个**新的**视图。
    cdp.eval("document.querySelector('#nav [data-route=\"plugins\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '') === '插件'", 20)
    # ★ 这个会话与插件**不在这里删**：界面正开着它，删掉会立刻产生一个
    #   GET /sessions/{id} -> 404，把后面"没有意外的失败请求"那条断言搞红。
    #   它们由探针收尾的账号删除一起处理。

    # ---------- 12.纯聊天（无角色 · 顺带当 API 体检台）----------
    # 用户要的"纯聊天"：不掺角色，用来确认 API 是否正常、或就是想直接聊两句。
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('叙事会话')", 15)
    # ★ 先等**面板落定**再点「+ 纯聊天」。
    #   点导航只是换了外壳，`renderChat` 之后还要异步把"上次那个会话"加载出来；
    #   如果在它加载完之前就建新会话，那个**在途的旧渲染**会在稍后把面板覆盖回去 ——
    #   表现就是"提示说会话已创建、列表里也有，但面板标题还是上一个会话"。
    #   （这条偶发变红过两次，根因就是这个时序；同类写法见本文件其它"等加载完再点"。）
    cdp.wait_for("!!document.querySelector('#chat-title')", 20)
    time.sleep(0.8)
    probe.check(
        "12.纯聊天",
        "会话页有「+ 纯聊天」入口",
        cdp.eval("!!document.querySelector('#btn-pure-chat')") is True,
    )
    cdp.eval("document.querySelector('#btn-pure-chat').click(); 'ok'")
    dialog_ok = cdp.wait_for("!!document.querySelector('#modal-root #btn-create-pure')", 10)
    cdp.eval("document.querySelector('#btn-create-pure').click(); 'ok'")
    panel_ok = cdp.wait_for(
        "(() => { const t = document.querySelector('#chat-title');"
        " return !!t && t.textContent.startsWith('纯聊天')"
        "   && !!document.querySelector('#chat-sub .badge'); })()",
        25,
    )
    sub_text = cdp.eval("document.querySelector('#chat-sub')?.textContent || ''") or ""
    probe.check(
        "12.纯聊天",
        "★ 纯聊天会话能建起来，头部标注「纯聊天」并把协议/模型/流式摊开（体检表）",
        dialog_ok and panel_ok and "协议" in sub_text,
        {
            "弹窗出现": dialog_ok,
            "面板就绪": panel_ok,
            "标题": cdp.eval("document.querySelector('#chat-title')?.textContent || ''"),
            "弹窗数": cdp.eval("document.querySelectorAll('#modal-root .modal').length"),
            "面板首行": (cdp.eval("document.querySelector('#chat-main')?.textContent || ''") or "")[:60],
            "会话列表含纯聊天": cdp.eval(
                "Array.from(document.querySelectorAll('.session-item .si-title'))"
                ".some(t => (t.textContent || '').startsWith('纯聊天'))"
            ),
        },
    )
    probe.check(
        "12.纯聊天",
        "★ 纯聊天里没有状态栏（不套 HP / 背包 / 任务那一套）",
        cdp.eval("!document.querySelector('#state-bar')") is True,
    )

    # 发一句 → 必须有回复（用的是假模型，链路与真实调用完全一致）
    cdp.eval(
        "(() => { const i = document.querySelector('#composer-input');"
        " i.value = '你好，请用一句话介绍你自己';"
        " i.dispatchEvent(new Event('input', { bubbles: true })); return 'ok'; })()"
    )
    cdp.eval("document.querySelector('#btn-send').click(); 'ok'")
    replied = cdp.wait_for(
        "(() => { const ms = document.querySelectorAll('#messages .msg');"
        " const last = ms[ms.length - 1];"
        " return !!last && !last.classList.contains('pending')"
        "   && (last.querySelector('.msg-body')?.textContent || '').length > 0; })()",
        60,
    )
    probe.check("12.纯聊天", "★ 纯聊天能正常收到回复（等价于一次真实的 API 连通性验证）", replied)

    # 提示词必须是「通用助手」，不能混进角色扮演那一套（用预览接口核对，与真实请求同一套装配）
    with httpx.Client(timeout=30) as c:
        items = c.get(
            f"{api}/narrative/sessions", headers=auth_headers, params={"limit": 50}
        ).json()["data"]["items"]
        pure = next((x for x in items if str(x["title"]).startswith("纯聊天")), None)
        pure_detail = (
            c.get(
                f"{api}/narrative/sessions/{pure['id']}",
                headers=auth_headers,
                params={"with_prompt": "true"},
            ).json()["data"]
            if pure
            else {}
        )
    pure_system = ((pure_detail.get("prompt") or {}).get("system_prompt") or "")
    probe.check(
        "12.纯聊天",
        "★ 纯聊天的提示词是「通用助手」：没有人设、没有守卫、没有状态协议",
        bool(pure_detail.get("pure_chat"))
        and "通用 AI 助手" in pure_system
        and "身份认知" not in pure_system
        and "<state>" not in pure_system,
        {
            "pure_chat": pure_detail.get("pure_chat"),
            "来源": (pure_detail.get("prompt") or {}).get("system_prompt_source"),
            "含守卫": "身份认知" in pure_system,
        },
    )
    # ★ 这个纯聊天会话**不在这里删**：界面正开着它，删掉会立刻产生一个
    #   GET /sessions/{id} -> 404，把后面"界面上没有意外的失败请求"那条断言搞红。
    #   它由 seed() 的开场清场与探针收尾的账号删除一起处理。

    # ---------- 14. 角色卡 VN 立绘（舞台 / 表情跟着状态栏变）----------
    # ★ 表情不是新协议：它就是状态栏里的一个字段（默认 mood）。
    #   这一节验证四件事：
    #     1. 卡里声明 extensions.hne.vn 之后，会话页出现 🎭 舞台开关
    #     2. 舞台真的画出来：背景图 + 立绘 + 名牌 + 台词框（地址取自卡里那两条）
    #     3. 开关是真的能开关（本地开关：不落库、不发请求）
    #     4. ★ 改了状态里的表情 → 立绘跟着换（证明"表情 → 立绘"是后端算的，不是写死的）
    # ★ 用**内嵌的纯色 PNG** 而不是外网地址：探针必须离线可跑，
    #   而且外网域名解析失败会在控制台刷一堆 ERR_NAME_NOT_RESOLVED，
    #   把真正要看的报错淹掉（顺便也验证了 data:image 这条路径真的能用）。
    #   尺寸刻意做成"看得见"的（背景 320×180、立绘 80×140），
    #   否则 1×1 的图在截图里等于没画 —— 那这张截图就失去了人工复核的意义。
    vn_bg = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAUAAAAC0CAIAAABqhmJGAAABlElEQVR42u3TAQkAAAzDsCm5guu5fynXMQhEQaGZPaBUJAADAwYGDAwGBgwMGBgwMBgYMDBgYDAwYGDAwICBwcCAgQEDAwYGAwMGBgwMBgYMDBgYMDAYGDAwYGDAwGBgwMCAgcHAgIEBAwMGBgMDBgYMDAZWAQwMGBgwMBgYMDBgYMDAYGDAwICBwcCAgQEDAwYGAwMGBgwMGBgMDBgYMDAYGDAwYGDAwGBgwMCAgQEDg4EBAwMGBgMDBgYMDBgYDAwYGDAwGBgwMGBgwMBgYMDAgIEBA4OBAQMDBgYDAwYGDAwYGAwMGBgwMGBgMDBgYMDAYGDAwICBAQODgQEDAwYGDAwGBgwMGBgMDBgYMDBgYDAwYGDAwGBgwMCAgQEDg4EBAwMGBgwMBgYMDBgYDAwYGDAwYGAwMGBgwMCAgcHAgIEBA4OBAQMDBgYMDAYGDAwYGAysAhgYMDBgYDAwYGDAwICBwcCAgQEDg4EBAwMGBgwMBgYMDBgYMDAYGDAwYGAwMGBgwMCAgcHAgIEBAwMGhm4PG8KoNUJ6LJcAAAAASUVORK5CYII="
    )
    vn_calm = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFAAAACMCAIAAABkj7muAAAAoElEQVR42u3PAQ0AAAgDoMc2mMGMYA9lowCpnlciLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCx81QIBOU5l6waZygAAAABJRU5ErkJggg=="
    )
    vn_angry = (
        "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAFAAAACMCAIAAABkj7muAAAAoElEQVR42u3PAQ0AAAgDoIc1hInNYQ9lowCZrlciLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCx81QI9Hev1eOoc6QAAAABJRU5ErkJggg=="
    )
    with httpx.Client(timeout=30) as c:
        vn_card_resp = c.post(
            f"{api}/character-cards/import",
            headers=auth_headers,
            json={
                "card": {
                    "spec": "chara_card_v2",
                    "data": {
                        "name": f"探针Vn卡_{int(time.time())}",
                        "first_mes": "（她站在窗边回过头）……你来了。",
                        "extensions": {
                            "hne": {
                                "vn": {
                                    "sprites": {"平静": vn_calm, "生气": vn_angry},
                                    "background": vn_bg,
                                    "default": "平静",
                                    "expression_field": "mood",
                                },
                                "state_schema": [{"name": "mood", "type": "text"}],
                                "initial_state": {"mood": "平静"},
                            }
                        },
                    },
                }
            },
        )
        vn_card = vn_card_resp.json()["data"] if vn_card_resp.status_code == 201 else None
        vn_sid = None
        if vn_card:
            created_vn = c.post(
                f"{api}/narrative/sessions",
                headers=auth_headers,
                json={
                    "character_card_id": vn_card["id"],
                    "llm_provider_id": provider_id,
                    "title": "探针VN用会话",
                },
            )
            if created_vn.status_code in (200, 201):
                vn_sid = int(created_vn.json()["data"]["id"])
    probe.check(
        "14.VN立绘",
        "VN 用角色卡与会话建好了（卡里声明了背景 + 两张立绘）",
        vn_card is not None and vn_sid is not None,
        vn_sid,
    )

    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('叙事会话')", 15)
    cdp.wait_for(f"!!document.querySelector('.session-item[data-id=\"{vn_sid}\"]')", 25)
    cdp.eval(f"document.querySelector('.session-item[data-id=\"{vn_sid}\"]').click(); 'ok'")
    staged = cdp.wait_for(
        "(() => { const s = document.querySelector('#vn-stage');"
        f" return !!s && !!s.querySelector('.vn-sprite')"
        f"   && (s.querySelector('.vn-sprite').getAttribute('src') || '') === '{vn_calm}'; }})()",
        25,
    )
    stage_bits = cdp.eval(
        "(() => { const s = document.querySelector('#vn-stage'); if (!s) return {};"
        " const bg = s.querySelector('.vn-bg');"
        " return { bg: bg ? bg.getAttribute('src') : '',"
        "          sprite: s.querySelector('.vn-sprite')?.getAttribute('src') || '',"
        "          name: (s.querySelector('.vn-nameplate')?.textContent || '').trim(),"
        "          line: (s.querySelector('.vn-line')?.textContent || '').slice(0, 20),"
        "          pos: s.className }; })()"
    ) or {}
    probe.check(
        "14.VN立绘",
        "★ 舞台画出来了：背景 + 立绘 + 名牌（带当前表情）+ 台词框",
        staged
        and stage_bits.get("bg") == vn_bg
        and stage_bits.get("sprite") == vn_calm
        and "平静" in str(stage_bits.get("name"))
        and bool(stage_bits.get("line")),
        stage_bits,
    )
    probe.check(
        "14.VN立绘",
        "舞台开关出现了（卡开了 VN 才有这个按钮）",
        cdp.eval("!!document.querySelector('#btn-vn-stage')") is True,
        cdp.eval("document.querySelector('#btn-vn-stage')?.textContent || ''"),
    )
    # 开关是真的能开关（本地开关：只改"看的方式"）
    cdp.eval("document.querySelector('#btn-vn-stage').click(); 'ok'")
    hidden = cdp.wait_for("!document.querySelector('#vn-stage')", 10)
    cdp.eval("document.querySelector('#btn-vn-stage').click(); 'ok'")
    shown_again = cdp.wait_for("!!document.querySelector('#vn-stage')", 10)
    probe.check(
        "14.VN立绘",
        "🎭 开关能关能开（只改看的方式，不落库、不发请求）",
        hidden and shown_again,
        {"关掉后消失": hidden, "再点回来": shown_again},
    )
    # ★ 改状态里的表情 → 立绘换图（"表情 → 立绘"由后端算）
    with httpx.Client(timeout=30) as c:
        patched = c.patch(
            f"{api}/narrative/sessions/{vn_sid}/state",
            headers=auth_headers,
            json={"state": {"mood": "生气"}},
        )
    cdp.eval("document.querySelector('#chat-main button[data-act=\"refresh\"]')?.click(); 'ok'")
    switched = cdp.wait_for(
        "(() => { const s = document.querySelector('#vn-stage');"
        f" return !!s && (s.querySelector('.vn-sprite')?.getAttribute('src') || '') === '{vn_angry}'; }})()",
        25,
    )
    mood_text = cdp.eval(
        "document.querySelector('#vn-stage .vn-mood')?.textContent || ''"
    ) or ""
    probe.check(
        "14.VN立绘",
        "★ 状态里的表情改成「生气」→ 立绘换成生气那张（后端算、前端只画）",
        patched.status_code == 200 and switched and "生气" in mood_text,
        {"状态码": patched.status_code, "换图": switched, "表情": mood_text},
    )
    # ★ 给文档留一张"舞台长什么样"的截图（这张只进 data/，不覆盖 README 里的主截图）：
    #   VN 是纯视觉功能，DOM 断言能证明"图换了"，但排版好不好看只能靠人眼复核一次。
    time.sleep(0.6)
    vn_shot = cdp.send("Page.captureScreenshot", format="png")
    (PROJECT_ROOT / "data" / "ui_probe_vn.png").write_bytes(base64.b64decode(vn_shot["data"]))
    # ★ 收尾切到插件页（理由同骰子节：别把界面停在"下一节要点导航过去"的页面上）
    cdp.eval("document.querySelector('#nav [data-route=\"plugins\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '') === '插件'", 20)
    # 这个会话与卡不在这里删：界面正开着它，删掉会立刻产生 404；
    # 由探针收尾的账号删除一起处理。

    # ---------- 15. 翻译中间件（跨语言对话：原文/译文可切换）----------
    # ★ 假模型的约定（见 scripts/fake_openai_server.py）：
    #     用户消息里带「英文回复」→ 假模型说英文；系统提示词里带「翻译中间件」→ 回一段假译文。
    #   这一节验证四件事：
    #     1. 头部「🌐 翻译」能打开面板，并列出三种模式/三个方向/语言与模型选择
    #     2. 在界面上开启中间件 → 头部按钮变成「🌐 翻译开」
    #     3. 发一句让假模型说英文 → 界面上显示的是**译文**（外加可切换的按钮）
    #     4. 点一下能切回英文原文、再点回译文（纯前端，不发请求）
    with httpx.Client(timeout=30) as c:
        tr_card = c.post(
            f"{api}/character-cards",
            headers=auth_headers,
            json={"name": f"探针外语卡_{int(time.time())}", "greeting": "Hello there, traveler."},
        ).json()["data"]
        tr_sid = c.post(
            f"{api}/narrative/sessions",
            headers=auth_headers,
            json={"character_card_id": tr_card["id"], "llm_provider_id": provider_id,
                  "title": "探针翻译用会话"},
        ).json()["data"]["id"]
    probe.check("15.翻译", "翻译用会话建好了", bool(tr_sid), tr_sid)

    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('叙事会话')", 15)
    cdp.wait_for(f"!!document.querySelector('.session-item[data-id=\"{tr_sid}\"]')", 25)
    cdp.eval(f"document.querySelector('.session-item[data-id=\"{tr_sid}\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#btn-translate')", 20)
    probe.check(
        "15.翻译",
        "头部有「🌐 翻译」入口，且默认是关的（不花用户的钱）",
        (cdp.eval("document.querySelector('#btn-translate')?.textContent || ''") or "").strip()
        == "🌐 翻译",
        cdp.eval("document.querySelector('#btn-translate')?.textContent || ''"),
    )
    cdp.eval("document.querySelector('#btn-translate').click(); 'ok'")
    panel_ok = cdp.wait_for(
        "['tr-enabled','tr-mode','tr-direction','tr-target','tr-provider']"
        ".every(id => !!document.getElementById(id))",
        15,
    )
    # ★ 第十六轮：语言只剩一个下拉（"输入侧语言"已退休），且提示要写明源语言自动识别。
    #   用户原话："默认居然是中文翻译成英文…用户还需要自己切换语言" —— 两个语言框并排时
    #   极易读成"英文 → 中文"而真实语义相反，所以这里把"只许有一个语言控件"钉成断言。
    one_lang_ok = cdp.eval(
        "(() => {"
        " const ids = ['tr-target','tr-input-lang'].filter(id => !!document.getElementById(id));"
        " const hints = Array.from(document.querySelectorAll('#tr-body .hint'))"
        ".map(e => e.textContent || '').join(' ');"
        " return ids.length === 1 && ids[0] === 'tr-target' && hints.includes('自动识别');"
        "})()"
    )
    modes_text = cdp.eval(
        "Array.from(document.querySelectorAll('#tr-mode option')).map(o => o.textContent).join(' / ')"
    ) or ""
    probe.check(
        "15.翻译",
        "★ 面板列出三种模式（关闭 / 只写提示词 0 token / 中间件多一次调用）与两个方向",
        panel_ok and "0 token" in modes_text and "多一次调用" in modes_text,
        modes_text[:70],
    )
    probe.check(
        "15.翻译",
        "★ 只有一个语言下拉（翻译成什么语言），提示写明源语言自动识别（输入侧语言已退休）",
        one_lang_ok is True,
        f"one-lang={one_lang_ok}",
    )
    # 在界面上开启中间件（回复方向、译成简体中文）
    cdp.eval(
        "(() => { const on = document.getElementById('tr-enabled'); on.checked = true;"
        " document.getElementById('tr-mode').value = 'middleware';"
        " document.getElementById('tr-direction').value = 'reply';"
        " return 'ok'; })()"
    )
    cdp.eval("document.querySelector('#btn-save-translate').click(); 'ok'")
    saved_ok = cdp.wait_for(
        "(() => { const b = document.querySelector('#btn-translate');"
        " return !!b && b.textContent.includes('开'); })()",
        20,
    )
    probe.check("15.翻译", "★ 在界面上开启后头部按钮变成「🌐 翻译开」", saved_ok)
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )

    # 发一句让假模型说英文 → 中间件把它译成中文，界面显示译文
    cdp.eval(
        "(() => { const i = document.querySelector('#composer-input');"
        " i.value = '英文回复 请讲一段';"
        " i.dispatchEvent(new Event('input', { bubbles: true })); return 'ok'; })()"
    )
    cdp.eval("document.querySelector('#btn-send').click(); 'ok'")
    translated = cdp.wait_for(
        "(() => { const ms = document.querySelectorAll('#messages .msg.assistant');"
        " const last = ms[ms.length - 1]; if (!last) return false;"
        " const body = last.querySelector('.msg-body')?.textContent || '';"
        " const chip = last.querySelector('.msg-trans')?.textContent || '';"
        " return body.includes('本地假译文') && chip.includes('译文'); })()",
        60,
    )
    shown = cdp.eval(
        "(() => { const ms = document.querySelectorAll('#messages .msg.assistant');"
        " const last = ms[ms.length - 1];"
        " return { body: (last.querySelector('.msg-body')?.textContent || '').slice(0, 24),"
        "          chip: (last.querySelector('.msg-trans')?.textContent || '').trim(),"
        "          hasToggle: !!last.querySelector('[data-act=toggle-translation]') }; })()"
    ) or {}
    probe.check(
        "15.翻译",
        "★ 界面显示的是**译文**（英文原文仍在 DOM 里），并带「看看另一份」按钮",
        translated and shown.get("hasToggle") is True,
        shown,
    )
    # 切换是纯前端的：点一下回英文原文，再点回译文
    # ★ 第十六轮：状态条现在也排在 #messages 末尾，`.msg.assistant:last-of-type` 会返回
    #   null（点击变成对 null 调 click() ⇒ 报错、一次都没切）。改成"最后一条 assistant"。
    LAST_ASSISTANT = (
        "(() => { const ms = document.querySelectorAll('#messages .msg.assistant');"
        " return ms.length ? ms[ms.length - 1] : null; })()"
    )
    cdp.eval(
        f"({LAST_ASSISTANT})?.querySelector('[data-act=toggle-translation]').click(); 'ok'"
    )
    back_to_source = cdp.wait_for(
        "(() => { const ms = document.querySelectorAll('#messages .msg.assistant');"
        " const body = ms[ms.length - 1].querySelector('.msg-body')?.textContent || '';"
        " return body.startsWith('I hear you'); })()",
        10,
    )
    cdp.eval(
        f"({LAST_ASSISTANT})?.querySelector('[data-act=toggle-translation]').click(); 'ok'"
    )
    back_to_translation = cdp.wait_for(
        "(() => { const ms = document.querySelectorAll('#messages .msg.assistant');"
        " const body = ms[ms.length - 1].querySelector('.msg-body')?.textContent || '';"
        " return body.includes('本地假译文'); })()",
        10,
    )
    probe.check(
        "15.翻译",
        "★ 一键切回英文原文、再一键切回译文（纯前端，不改数据、不发请求）",
        back_to_source and back_to_translation,
        {"切到原文": back_to_source, "切回译文": back_to_translation},
    )
    probe.check(
        "15.翻译",
        "★ 每条消息都有逐条「🌐 翻译」按钮（不看总开关：忘了开也能单独译一条）",
        cdp.eval(
            f"!!({LAST_ASSISTANT})?.querySelector('[data-act=\"translate-msg\"]')"
        )
        is True,
        cdp.eval(
            f"({LAST_ASSISTANT})?.querySelector('[data-act=\"translate-msg\"]')?.textContent || ''"
        ),
    )
    # 服务端也留着两份（刷新后还在）
    tr_detail = httpx.get(
        f"{api}/narrative/sessions/{tr_sid}", headers=auth_headers, timeout=30
    ).json()["data"]
    last_assistant = [m for m in tr_detail["messages"] if m["role"] == "assistant"][-1]
    probe.check(
        "15.翻译",
        "★ 落库的是两份：content 是模型原文，translation 是译文（刷新后仍可切换）",
        last_assistant["content"].startswith("I hear you")
        and (last_assistant.get("translation") or {}).get("text", "").startswith("【本地假译文】"),
        {
            "content": last_assistant["content"][:20],
            "译文": (last_assistant.get("translation") or {}).get("text", "")[:16],
        },
    )
    # ★ 给文档留一张截图（与 VN 节同样的理由：翻译的"两份文本 + 切换按钮"是视觉细节）
    time.sleep(0.6)
    tr_shot = cdp.send("Page.captureScreenshot", format="png")
    (PROJECT_ROOT / "data" / "ui_probe_translate.png").write_bytes(
        base64.b64decode(tr_shot["data"])
    )
    # ★ 收尾切到插件页（理由同骰子/VN 节：别把界面停在"下一节要点导航过去"的页面上）
    cdp.eval("document.querySelector('#nav [data-route=\"plugins\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '') === '插件'", 20)

    # ---------- 12. 切页与请求日志 ----------
    # 先把当前会话打开（截图与"最后看一眼界面"都希望停在这里）
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("(document.querySelector('#view h1')?.textContent || '').includes('叙事会话')", 15)
    time.sleep(0.5)
    for route, title in (("cards", "角色卡"), ("books", "世界书"), ("providers", "模型配置"), ("chat", "叙事会话")):
        cdp.eval(f"document.querySelector('#nav [data-route=\"{route}\"]').click(); 'ok'")
        cdp.wait_for(f"(document.querySelector('#view h1')?.textContent || '').includes('{title}')", 15)
        probe.check("11.切页", f"切到「{title}」页", title in (cdp.eval("document.querySelector('#view h1')?.textContent || ''") or ""))
        time.sleep(0.5)

    cdp.eval("document.querySelector('#btn-reqlog').click(); 'ok'")
    time.sleep(0.5)
    probe.check("11.切页", "请求日志抽屉有记录",
                (cdp.eval("document.querySelectorAll('#reqlog-list .rl').length") or 0) >= 1,
                cdp.eval("document.querySelectorAll('#reqlog-list .rl').length"))
    bad_requests = cdp.eval(
        "Array.from(document.querySelectorAll('#reqlog-list .rl')).map(r => "
        "  r.querySelector('.rl-method').textContent + ' ' + "
        "  r.querySelector('.rl-path').textContent + ' -> ' + "
        "  r.querySelector('.rl-status').textContent)"
        ".filter(t => !t.endsWith(' 200') && !t.endsWith(' 201'))"
    )
    # 列出所有非 2xx 的请求：探针跑完应当没有"意外失败"的调用。
    # ★ 第 10 步**故意**删掉了当前会话，那一次 GET 详情的 404 是预期行为，必须排除；
    #   其余任何 4xx/5xx 都算问题（这条断言就是靠它抓出了
    #   `…/sessions/null/regenerate` 那种"会话 id 丢了还在发请求"的 bug）。
    expected_404 = f"/sessions/{session_id} -> 404"
    unexpected = [t for t in (bad_requests or []) if expected_404 not in t]
    probe.check(
        "11.切页",
        "★ 界面上没有意外的失败请求（第 10 步故意的 404 除外）",
        not unexpected,
        unexpected,
    )
    cdp.eval("document.querySelector('#reqlog-list .rl').click(); 'ok'")
    time.sleep(0.4)
    log_body = cdp.eval("document.querySelector('#reqlog-list .rl-body')?.textContent || ''") or ""
    probe.check("11.切页", "点开能看到原始请求/响应体", "响应体" in log_body)
    cdp.eval("document.querySelector('#reqlog-close').click(); 'ok'")

    # ---------- 12. 截图留档 ----------
    # 停在「对话页 + 一个有内容的会话」上，这样截图能同时体现
    # 消息操作按钮、Token 统计条与头部统计（人工复核视觉效果时看它）。
    # ★ 先把可能残留的弹窗关掉：第 10.5 步的操作若没走完会留下遮罩，
    #   截图就变成一张弹窗图（第一次跑出来就是这样）。
    cdp.eval(
        "(() => { document.querySelectorAll('#modal-root .modal [data-close]')"
        "   .forEach(b => b.click()); return 'ok'; })()"
    )
    time.sleep(0.5)
    # 第 10 步把会话删掉了，这里补一个"有内容"的会话再截图，
    # 否则截出来是空状态，看不出消息操作按钮与 Token 统计条长什么样。
    with httpx.Client(timeout=30) as c:
        fresh = c.post(
            f"{base}/api/v1/narrative/sessions",
            headers={"Authorization": f"Bearer {token}"},
            json={"character_card_id": data["card_id"], "llm_provider_id": data["provider_id"],
                  "title": "截图用会话"},
        )
        fresh_sid = fresh.json()["data"]["id"] if fresh.status_code in (200, 201) else None
        if fresh_sid:
            # 流式发一句，让会话里既有用户消息也有角色回复。
            # ★ 必须真的把响应体读完：只是打开 stream 上下文就退出的话，
            #   服务端会因为"客户端断开"而停止生成，截图里就只剩用户那一句。
            with c.stream(
                "GET",
                f"{base}/api/v1/narrative/sessions/{fresh_sid}/stream",
                headers={"Authorization": f"Bearer {token}"},
                params={"content": "灯塔还亮着吗"},
            ) as resp:
                for _chunk in resp.iter_text():
                    pass
    cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
    cdp.wait_for("!!document.querySelector('#view .session-item')", 20)
    # ★ 列表可能还是旧的一屏（切页只是重渲染外壳）。点「刷新」拉一次最新会话列表，
    #   否则会点到被第 10 步删掉的那个会话，截出来还是空面板。
    cdp.eval("document.querySelector('[data-act=\"refresh\"]')?.click(); 'ok'")
    time.sleep(1.5)
    cdp.eval(
        "(() => { const it = document.querySelector('#view .session-item');"
        " if (it) it.click(); return 'ok'; })()"
    )
    cdp.wait_for(
        "document.querySelectorAll('#messages .msg-actions').length"
        " === document.querySelectorAll('#messages .msg').length"
        " && document.querySelectorAll('#messages .msg').length >= 2",
        25,
    )
    # 悬停会让操作按钮显形（CSS opacity），截图前把鼠标移到最后一条消息上
    cdp.eval(
        "(() => { const ms = document.querySelectorAll('#messages .msg');"
        " const last = ms[ms.length-1];"
        " if (last) last.dispatchEvent(new MouseEvent('mouseover', {bubbles:true}));"
        " return 'ok'; })()"
    )
    time.sleep(0.5)

    # ==================================================================
    #  13. ★「关于」弹窗 + 复制诊断信息（第二十七轮新增）
    # ==================================================================
    #  判据分两类，第二类更要紧：
    #    ① 弹窗内容确实来自后端（版本 / 存储方式 / 真实路径），不是写死的；
    #    ② ★★ 诊断文本里**绝不能**出现对话内容或密钥 ——
    #       它的用途是"贴给别人排查"，一旦带出对话原文就是隐私事故。
    #       所以这里真的造一次错误、生成诊断文本，再逐项检查。
    cdp.eval("document.getElementById('btn-about').click(); 'ok'")
    about_open = cdp.wait_for(
        "(document.querySelector('.modal-head h2')?.textContent || '').includes('关于')", 15
    )
    # 弹窗先渲染、环境快照后到，所以要等路径行出现
    cdp.wait_for("!!document.querySelector('.about-path')", 20)
    raw_about = cdp.eval(
        """(() => {
          const out = {};
          for (const r of document.querySelectorAll('.about-row')) {
            out[(r.querySelector('.about-k')?.textContent || '').trim()]
              = (r.querySelector('.about-v')?.textContent || '').trim();
          }
          return JSON.stringify({
            rows: out,
            hasPath: document.querySelectorAll('.about-path').length,
            hasRepo: !!document.querySelector('.about-rows a[href*="github.com"]'),
            hasCopyBtn: !!document.getElementById('about-copy'),
          });
        })()"""
    )
    about = json.loads(raw_about) if isinstance(raw_about, str) else {}
    rows = about.get("rows") or {}
    probe.check(
        "13.关于",
        "★「关于」按钮能打开弹窗并显示版本号",
        bool(about_open) and bool(rows.get("版本")),
        {"打开": about_open, "版本": rows.get("版本")},
    )
    probe.check(
        "13.关于",
        "★「当前存储」写明了用的是哪个库（来自后端，不是写死的）",
        any(k in str(rows.get("当前存储", "")) for k in ("SQLite", "MySQL")),
        rows.get("当前存储"),
    )
    probe.check(
        "13.关于",
        "★ 数据/日志目录来自后端且**未被打码**（用户要照着它去打开目录）",
        about.get("hasPath") == 2
        and os.path.sep in str(rows.get("数据目录", ""))
        and "*" not in str(rows.get("数据目录", "")),
        {"路径行数": about.get("hasPath"), "数据目录": rows.get("数据目录")},
    )
    probe.check(
        "13.关于",
        "★ 开源地址可点击 + 有「复制诊断信息」按钮",
        bool(about.get("hasRepo")) and bool(about.get("hasCopyBtn")),
        {"repo": about.get("hasRepo"), "copy": about.get("hasCopyBtn")},
    )

    # ---- 造两种错误，验证收集器的**取舍**：该收的收、不该收的不收 ----
    #
    # ★★ 两个坑（都是第一版踩的，记在这里免得下次再踩）：
    #   ① 必须走**应用自己的 api 层**（`hne/api` 的 request），不能用裸 `fetch` ——
    #      错误收集器挂在 api 层的请求广播上，裸 fetch 会绕过它，
    #      于是表现为"收集器没工作"。这类"验收脚本走了假路径"的错法最能骗人：
    #      失败指向被测功能，真正错的却是脚本。
    #   ② 用来制造"正常业务失败"的那条 404 **不能打会话路径** ——
    #      收尾总检专门防"被删的会话又被请求"，脚本自己打一条会把自己判红。
    #      换一个同样属于"预期内失败"的非会话路径（不存在的角色卡）。
    cdp.eval(
        """(async () => {
          const { request } = await import('hne/api');
          // ① 401（鉴权失败）—— 值得报出去的故障，应被收集
          //    ★ 必须**显式用一个坏令牌**：默认会用当前登录态，那样拿到的是 200，
          //      这条断言就会永远测不到东西（第一版正是如此）。
          try { await request('GET', '/auth/me', { token: 'definitely-not-a-real-token' }); } catch { /* 预期失败 */ }
          // ② 404（资源不存在）—— 正常业务流程，**不该**被收集
          try { await request('GET', '/character-cards/99999999', {}); } catch { /* 预期失败 */ }
          return 'ok';
        })()"""
    )
    time.sleep(1.2)

    diag_text = cdp.eval(
        """(async () => {
          const d = await import('hne/diagnostics');
          const tk = localStorage.getItem('hne_access_token') || '';
          let env = null;
          try {
            const r = await fetch('/api/v1/system/diagnostics',
              { headers: { Authorization: 'Bearer ' + tk } });
            env = (await r.json()).data;
          } catch {}
          return d.buildDiagnosticText(env, null);
        })()"""
    )
    diag_text = str(diag_text or "")
    probe.check(
        "13.关于",
        "★ 诊断文本含环境事实（版本 / 存储 / 数据目录 / 日志目录）",
        all(k in diag_text for k in ("版本", "存储", "数据目录", "日志目录")),
        diag_text[:140].replace("\n", " | "),
    )
    # ★★ 最关键的一条
    forbidden = ("bearer ", "password", "api_key", "hne_secret", "fernet",
                 "sk-", "reasoning_effort", "first_mes")
    leaked = [w for w in forbidden if w in diag_text.lower()]
    probe.check(
        "13.关于",
        "★★ 诊断文本不含密钥/口令/对话正文（可安全贴给别人）",
        not leaked,
        {"命中": leaked, "长度": len(diag_text)},
    )
    probe.check(
        "13.关于",
        "★ 401 这类故障被收集、404 这类正常业务失败被排除",
        "401" in diag_text and "99999999" not in diag_text,
        [ln for ln in diag_text.splitlines() if "最近错误" in ln or "401" in ln][:2],
    )

    # ---- ★★ 真的点一次「复制诊断信息」，再**读剪贴板**核对内容 ----
    #  为什么值得做：前面的断言只证明"文本生成对了"，
    #  而用户真正的动作是"点按钮 → 粘贴到某处"。中间可能坏在：
    #  剪贴板 API 在 http:// 下不可用、或者复制的是别的东西。
    #  （项目里 Electron 走 file://，那条路还要单独验 —— 见桌面版验收脚本。）
    cdp.send("Browser.grantPermissions", permissions=["clipboardReadWrite", "clipboardSanitizedWrite"])
    cdp.eval(
        """(async () => {
          // 先清空剪贴板，避免"读到上次的旧内容"造成假通过
          try { await navigator.clipboard.writeText('__EMPTY__'); } catch {}
          document.getElementById('about-copy').click();
          return 'ok';
        })()"""
    )
    time.sleep(1.5)
    clip = cdp.eval("(async () => { try { return await navigator.clipboard.readText(); }"
                    " catch (e) { return '__ERR__' + e; } })()")
    clip = str(clip or "")
    probe.check(
        "13.关于",
        "★★ 点「复制诊断信息」后，剪贴板里确实是诊断文本（不是旧内容/空）",
        clip.startswith("【云梦枢诊断信息】")
        and "版本" in clip
        and "数据目录" in clip
        and "__EMPTY__" not in clip,
        clip[:120].replace("\n", " | "),
    )
    clip_leak = [w for w in forbidden if w in clip.lower()]
    probe.check(
        "13.关于",
        "★★ 剪贴板里那份文本同样不含密钥/口令/对话正文",
        not clip_leak,
        {"命中": clip_leak, "长度": len(clip)},
    )

    # 关掉弹窗，别影响后面的收尾总检
    cdp.eval(
        "(() => { const b = document.querySelector('.modal-mask [data-close]');"
        " if (b) b.click(); return 'ok'; })()"
    )
    time.sleep(0.3)

    # ---------- ★ 收尾总检：整轮跑下来，控制台不许有"会话 404" ----------
    #
    # 会话被删掉之后（删角色卡勾了"连同对话记录"、另一个标签页删的、探针自己删的），
    # 前端**不该**再白打一次注定 404 的请求 —— 那条红字会让用户以为前端的坏了。
    # 两条路径都已被修（进对话页 / 点刷新）：先拿列表确认，再决定要不要请求。
    # 这里做总检，覆盖"以后有人又加了第三条路径"的情况。
    _session_404 = [
        e for e in probe.console_errors if "/narrative/sessions/" in e and "404" in e
    ]
    probe.check(
        "99.总检",
        "★ 整轮没有一次「会话 404」请求（被删的会话不再被反复请求）",
        not _session_404,
        {"404": _session_404[:3], "控制台报错总数": len(probe.console_errors)},
    )


if __name__ == "__main__":
    raise SystemExit(main())
