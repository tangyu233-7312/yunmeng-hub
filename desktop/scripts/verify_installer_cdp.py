"""通过 CDP 验收**装出来的**云梦枢：填首次设置表单 → 保存 → 必须进控制台。

为什么单独写这个脚本
--------------------
"后端能不能起来"这件事，用日志就能看；但"用户点的那个按钮到底通不通"
只有真的去点才算数。这个脚本连到 Electron 的调试端口，在**真实页面**里
调用首次设置页自己的保存入口（就是「保存并开始」按钮调用的那个），
然后核对它返回的结论与最终的页面 URL。

★ 为什么不"点按钮"而是调 `yunmengSetup.save()`：
  点按钮是"发射后不管"，脚本只能靠等；而 save() 返回的正是那句
  「配置已保存并验证通过」/「起不来后端」的结论本身。
  能拿到结论，就能把失败原因原样报出来，而不是"超时了，不知道为啥"。

★ 账号口令从 `--env-file` 读，**不写进命令行**（命令行会被别的进程看到）。

用法：
    python desktop/scripts/verify_installer_cdp.py --port 47000 --env-file .env --log-dir %TEMP%\\xxx
退出码 0 = 通过；非 0 = 失败（原因打印在 stdout）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.request

import websockets

# ★ Windows 控制台默认是 GBK（cp936），打印一个 GBK 编不出来的字符（比如 "✓"）
#   就会直接 UnicodeEncodeError 抛出去 —— 而这个脚本跑在验收中间，
#   于是"断言已经通过"也会被记成"这一步失败"（实测踩到：按钮断言刚过就崩）。
#   项目里 scripts/smoke_test.py 早就用了同一招，这里补齐。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def load_env_file(path: str) -> dict:
    """极简 .env 解析（只认 KEY=VALUE，去掉引号；与后端读法保持一致）。"""
    values: dict[str, str] = {}
    with open(path, "r", encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                values[key] = value
    return values


def find_page(port: int) -> dict | None:
    """在调试端口上找那个"有 yunmengSetup 的"页面。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
            targets = json.load(resp)
    except Exception:
        return None
    for target in targets:
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
            return target
    return None


class Cdp:
    """够用就好的 CDP 客户端：只要 evaluate 与几个页面查询。"""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._id = 0

    async def call(self, method: str, params: dict | None = None):
        self._id += 1
        msg_id = self._id
        await self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=300)
            message = json.loads(raw)
            if message.get("id") == msg_id:
                if "error" in message:
                    raise RuntimeError(f"{method} 失败：{message['error']}")
                return message.get("result")

    async def eval(self, expression: str, await_promise: bool = False):
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise RuntimeError(f"页面里执行出错：{detail.get('text')} {detail.get('exception', {}).get('description', '')}")
        return result.get("result", {}).get("value")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--log-dir", default="")
    args = parser.parse_args()

    env_values = load_env_file(args.env_file)
    if not env_values.get("HNE_MYSQL_PASSWORD"):
        print("× 给定的 .env 里没有 HNE_MYSQL_PASSWORD，没法拿它做验收")
        return 2

    deadline = asyncio.get_event_loop().time() + 60
    target = None
    while asyncio.get_event_loop().time() < deadline:
        target = find_page(args.port)
        if target:
            break
        await asyncio.sleep(0.5)
    if not target:
        print("× 连不上 Electron 的调试端口（应用可能没起来）")
        return 3

    async with websockets.connect(target["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
        cdp = Cdp(ws)
        await cdp.call("Runtime.enable")

        # 等页面把 yunmengSetup 暴露出来（preload 之后才会有）
        ready = False
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            try:
                ready = bool(await cdp.eval("Boolean(window.yunmengSetup && window.yunmengSetup.getFields)"))
            except RuntimeError:
                ready = False
            if ready:
                break
            await asyncio.sleep(0.5)
        if not ready:
            print(f"× 页面里没有 yunmengSetup（当前 URL：{await cdp.eval('location.href')}）")
            return 4

        url_before = await cdp.eval("location.href")
        print(f"  页面：{url_before}")
        if "setup.html" not in str(url_before):
            print(f"× 全新配置下没有进首次设置页，而是：{url_before}")
            return 5

        fields = await cdp.eval("window.yunmengSetup.getFields().then(r => JSON.stringify(r.fields))", True)
        field_defs = json.loads(fields)
        keys = [f["key"] for f in field_defs]

        # ★★ 必须在**真页面里**确认「随机生成」按钮真的画出来了（本轮真 bug 的墓碑）。
        #   之前这里只有下面那句"直接调 generate()"的填值 —— 它**绕过了按钮**，
        #   于是"按钮从来没被创建"这件事验收完全看不见（用户打开一看：按钮不在）。
        #   教训：验收要**走用户看到的那条路**，不能替用户把活干了。
        generatable = [f["key"] for f in field_defs if f.get("generatable")]
        buttons = await cdp.eval(
            "[...document.querySelectorAll('button')]"
            ".filter(b => b.textContent.trim() === '随机生成').length"
        )
        if not generatable:
            print("× 字段里没有任何「可生成」的项（generatable 全为假）—— 按钮就没理由存在")
            return 10
        if buttons != len(generatable):
            print(f"× 「随机生成」按钮数量不对：页面里 {buttons} 个，可生成字段 {len(generatable)} 个"
                  f"（页面若一个都没有，用户就找不到向导提示里说的那个按钮）")
            return 11
        print(f"  「随机生成」按钮 {buttons} 个，与可生成字段 {len(generatable)} 个一一对应 —— 不再有「有文案没按钮」")

        values = {f["key"]: (f.get("default") or "") for f in field_defs}
        from_env = 0
        for key in keys:
            if env_values.get(key):
                values[key] = env_values[key]
                from_env += 1
        # 生成型字段（密钥）：这里仍然走"生成器"，因为要的是可复现的自动化填值；
        # 但**按钮是否存在**已经由上面那条断言单独守住了（两件事分开测，别互相掩盖）。
        for f in field_defs:
            if f.get("generatable") and not (values.get(f["key"]) or "").strip():
                values[f["key"]] = await cdp.eval(
                    f"window.yunmengSetup.generate({json.dumps(f['key'])})", True
                )
        print(f"  表单字段 {len(field_defs)} 个（其中 {from_env} 个取自 --env-file）")

        # ★ 就是「保存并开始」按钮调用的那个入口
        result = await cdp.eval(
            "window.yunmengSetup.save(%s)" % json.dumps(values), True
        )
        if not isinstance(result, dict):
            print(f"× save() 没有返回结论：{result!r}")
            return 6
        if not result.get("ok"):
            print(f"× 保存/验证失败：{result.get('message')}")
            if result.get("detail"):
                print("  细节：")
                for line in str(result["detail"]).splitlines():
                    print(f"    {line}")
            return 7
        print(f"  save() 结论：{result.get('message')}")

        # 保存成功后主进程会把窗口导航到控制台
        deadline = asyncio.get_event_loop().time() + 240
        url_after = ""
        while asyncio.get_event_loop().time() < deadline:
            try:
                url_after = str(await cdp.eval("location.href"))
            except RuntimeError:
                url_after = ""
            if "/console/" in url_after:
                break
            await asyncio.sleep(1)
        if "/console/" not in url_after:
            print(f"× 保存成功但没能进控制台，当前 URL：{url_after}")
            return 8
        print(f"  已进入控制台：{url_after}")

        # 控制台得真的渲染出来（拿标题当证据，别只信 URL）
        title = await cdp.eval("document.title")
        has_app = await cdp.eval("Boolean(document.querySelector('main, #app, .app-shell'))")
        if not has_app:
            print(f"× 控制台页面没有渲染出内容（title={title!r}）")
            return 9
        print(f"  控制台已渲染（title={title!r}）")

    print("  [√] CDP 验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
