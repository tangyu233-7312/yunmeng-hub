"""通过 CDP 验收**装出来的**云梦枢：走首次设置 → 保存 → 必须进控制台。

为什么单独写这个脚本
--------------------
"后端能不能起来"这件事，用日志就能看；但"用户点的那个按钮到底通不通"
只有真的去点才算数。这个脚本连到 Electron 的调试端口，在**真实页面**里
调用首次设置页自己的保存入口（就是「保存并开始」按钮调用的那个），
然后核对它返回的结论与最终的页面 URL。

★ 为什么要"点按钮"而不是只调 `yunmengSetup.save()`：
  点按钮是"发射后不管"，脚本只能靠等；而 save() 返回的正是那句
  「配置已保存并验证通过」/「起不来后端」的结论本身 —— 能拿到结论就能把
  失败原因原样报出来。所以两者都要：**先点一下按钮**（证明用户真能走通），
  再读它给出的结论。

==================== ★ 两种模式（默认零配置）====================
不加 `--env-file` = **零配置模式**（这才是默认形态）：
    存储方式必须是「本机文件（SQLite）」、MySQL 字段必须隐藏，
    **一个字段都不填**直接保存，然后核对：
      · 进得了控制台；
      · `<userData>\\data\\app.sqlite3` 真的被建出来了（数据落在 userData，不是安装目录）。

加了 `--env-file` = **MySQL 模式**：把存储方式切成 MySQL、按该文件填连接信息。

★ 账号口令从 `--env-file` 读，**不写进命令行**（命令行会被别的进程看到）。

用法：
    # 零配置（默认形态）
    python desktop/scripts/verify_installer_cdp.py --port 47000 --userdata "%APPDATA%\\云梦枢"
    # MySQL 可选形态
    python desktop/scripts/verify_installer_cdp.py --port 47000 --env-file .env
退出码 0 = 通过；非 0 = 失败（原因打印在 stdout）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
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
    parser.add_argument("--env-file", default="", help="MySQL 模式的 .env；不传 = 零配置（SQLite）模式")
    parser.add_argument("--userdata", default="", help="userData 目录（用来核对 SQLite 文件真的建在这）")
    parser.add_argument("--log-dir", default="")
    args = parser.parse_args()

    mysql_mode = bool(args.env_file)
    env_values = load_env_file(args.env_file) if mysql_mode else {}
    if mysql_mode and not env_values.get("HNE_MYSQL_PASSWORD"):
        print("× 给定的 .env 里没有 HNE_MYSQL_PASSWORD，没法拿它做 MySQL 模式的验收")
        return 2
    print(f"  验收模式：{'MySQL（可选形态）' if mysql_mode else '零配置（默认形态 · SQLite）'}")

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

        # ------------------------------------------------------------------
        #  ★ 存储方式：默认必须是「本机文件（SQLite）」，且 MySQL 字段整组隐藏
        # ------------------------------------------------------------------
        storage_tag = await cdp.eval("document.querySelector('#f-HNE_DB_BACKEND')?.tagName || ''")
        if storage_tag != "SELECT":
            print(f"× 「存储方式」不是下拉框，实际是 {storage_tag!r} —— 用户得靠猜能填什么")
            return 12
        backend_value = await cdp.eval("document.querySelector('#f-HNE_DB_BACKEND').value")
        total_mysql = int(await cdp.eval("document.querySelectorAll('#mysql-grid [data-db-only]').length") or 0)
        if total_mysql < 5:
            print(f"× MySQL 字段只有 {total_mysql} 个，页面结构不对")
            return 13

        if mysql_mode:
            await cdp.eval("""
              (() => {
                const sel = document.querySelector('#f-HNE_DB_BACKEND');
                sel.value = 'mysql';
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              })()
            """)
            await asyncio.sleep(0.3)
            if str(await cdp.eval("document.querySelector('#f-HNE_DB_BACKEND').value")) != "mysql":
                print("× 切不到 MySQL 模式")
                return 14
            hidden_after = await cdp.eval(
                "[...document.querySelectorAll('#mysql-grid [data-db-only]')]"
                ".filter(b => b.style.display === 'none').length"
            )
            if int(hidden_after or 0) != 0:
                print(f"× 切到 MySQL 后仍有 {hidden_after} 个字段隐藏着")
                return 15
            print(f"  已切到 MySQL：{total_mysql} 个连接字段全部可见")
        else:
            if str(backend_value) != "sqlite":
                print(f"× 全新安装的默认存储方式不是 sqlite，而是 {backend_value!r}（零配置就假了）")
                return 12
            hidden_now = await cdp.eval(
                "[...document.querySelectorAll('#mysql-grid [data-db-only]')]"
                ".filter(b => b.style.display === 'none').length"
            )
            disabled_now = await cdp.eval(
                "[...document.querySelectorAll('#mysql-grid input')].filter(i => i.disabled).length"
            )
            if int(hidden_now or 0) != total_mysql or int(disabled_now or 0) != total_mysql:
                print(f"× 选本机文件时 MySQL 字段没有全部隐藏/禁用："
                      f"隐藏 {hidden_now}/{total_mysql}、禁用 {disabled_now}/{total_mysql}")
                return 13
            print(f"  默认「本机文件（SQLite）」：{total_mysql} 个 MySQL 字段已隐藏并禁用 —— 一格都不用填")

        values = {f["key"]: (f.get("default") or "") for f in field_defs}
        from_env = 0
        for key in keys:
            if env_values.get(key):
                values[key] = env_values[key]
                from_env += 1
        # 生成型字段（密钥）：这里仍然走"生成器"，因为要的是可复现的自动化填值；
        # 但**按钮是否存在**已经由上面那条断言单独守住了（两件事分开测，别互相掩盖）。
        # ★ 零配置模式下**刻意一个密钥都不填** —— 留给后端自己生成，
        #   这正是"安装即用"要验的那条路（`ensure_secrets` 会被真正执行一次）。
        if mysql_mode:
            for f in field_defs:
                if f.get("generatable") and not (values.get(f["key"]) or "").strip():
                    values[f["key"]] = await cdp.eval(
                        f"window.yunmengSetup.generate({json.dumps(f['key'])})", True
                    )

        # ★★ 填值的**方式**很关键（这里返工过一次，值得写清楚）：
        #   必须把值写进**页面上的输入框**，再触发提交让页面自己去 collect()。
        #   第一版是"把 values 直接交给 window.yunmengSetup.save(values)"——
        #   对零配置没影响（反正不填），但在 MySQL 模式下**页面上的输入框始终是空的**，
        #   于是页面的本地体检（gateLocal → precheck）读到空口令，直接拦下：
        #       "先补上这几项再试：MySQL 口令"
        #   而脚本还傻乎乎地"点了按钮"，报出来的现象是"保存没成功"，
        #   看起来像功能坏了 —— 实际是**验收脚本没走用户走的那条路**。
        #   ★ 又一条同类教训：验收要在**真页面上**把该填的填进去，
        #     不能替用户把活干了（上一轮「随机生成」按钮就是这么被漏掉的）。
        filled = await cdp.eval(
            "(() => { const v = %s; let n = 0;"
            " for (const k of Object.keys(v)) {"
            "   const el = document.getElementById('f-' + k);"
            "   if (!el) continue;"
            "   el.value = v[k];"
            "   el.dispatchEvent(new Event('input', { bubbles: true }));"
            "   el.dispatchEvent(new Event('change', { bubbles: true }));"
            "   n++;"
            " } return n; })()" % json.dumps(values)
        )
        print(f"  表单字段 {len(field_defs)} 个（其中 {from_env} 个取自 --env-file），已填进页面 {filled} 个")

        # ★★ 走用户真正走的那条路：**点「保存并开始」按钮**（而不是直接调 save()）。
        #   第一版验收直接调 save()，于是"按钮本身坏没坏"完全看不见 —— 同类事故
        #   本项目已经踩过一次（「随机生成」按钮从来没被创建过，验收却全绿）。
        await cdp.eval("document.getElementById('form').requestSubmit()")
        await asyncio.sleep(1.5)
        btn_state = await cdp.eval("document.getElementById('btn-save').disabled")
        status_text = str(await cdp.eval("document.getElementById('status').textContent") or "")
        if btn_state is None:
            print("× 找不到「保存并开始」按钮 —— 用户就没有可点的入口")
            return 16
        print(f"  已点击「保存并开始」（按钮进入 disabled={btn_state}），页面提示：{status_text.splitlines()[0][:60] if status_text else '(空)'}")

        # 按钮那条路是"发射后不管"，但结论与导航都能从页面上观察到：
        # 保存失败时按钮会被重新启用并显示红字，成功时页面会导航走。
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
            status = await cdp.eval("document.getElementById('status')?.textContent")
            print(f"× 点「保存并开始」后没能进控制台，当前 URL：{url_after}")
            print(f"  页面提示：{str(status)[:300]}")
            return 8
        print(f"  已进入控制台：{url_after}")

        # ------------------------------------------------------------------
        #  ★ 零配置模式特有：数据必须真的落在 userData 下的 SQLite 文件里
        # ------------------------------------------------------------------
        if not mysql_mode:
            if not args.userdata:
                print("× 零配置模式必须传 --userdata，否则无法核对数据落在哪")
                return 17
            db_file = os.path.join(args.userdata, "data", "app.sqlite3")
            if not os.path.isfile(db_file):
                print(f"× 自动建库失败：没有在 {db_file} 建出 SQLite 文件")
                return 18
            head = b""
            with open(db_file, "rb") as fh:
                head = fh.read(15)
            if head != b"SQLite format 3":
                print(f"× {db_file} 不是 SQLite 库（头 15 字节：{head!r}）")
                return 19
            size = os.path.getsize(db_file)
            print(f"  ★ 自动建库成功：{db_file}（{size} 字节，头 15 字节是 SQLite 魔数）")

            secrets_file = os.path.join(args.userdata, "config", ".secrets.env")
            if not os.path.isfile(secrets_file):
                print(f"× 后端没有自动生成密钥：{secrets_file} 不存在")
                return 20
            with open(secrets_file, "r", encoding="utf-8") as fh:
                secrets_text = fh.read()
            for key in ("HNE_SECRET_KEY", "HNE_API_KEY_ENCRYPTION_KEY"):
                if f"{key}=" not in secrets_text:
                    print(f"× 自动生成的密钥文件里缺 {key}")
                    return 21
            print("  ★ 自动生成密钥成功：.secrets.env 里两个键都在（值不打印）")

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
