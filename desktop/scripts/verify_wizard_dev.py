"""在**开发态 Electron** 里驱动首次设置页，验收向导这一轮的改动。

为什么要有它
------------
安装包形态的验收（`verify_installer.ps1`）要装一遍、两分钟起，适合"打包之后"验；
但向导的**交互逻辑**（必填拦住没有、详情能不能折叠、测试后临时文件删没删）
在开发态就能验 —— 用 `HNE_DESKTOP_PROFILE` 指一个临时 profile 即可，
**不动用户真实的 `%APPDATA%\\云梦枢`**，也不必重新打包。

它验的每一条都对应一个真实缺陷或本次改动：

  1. 全新 profile → 落到首次设置页（step=setup）
  2. **口令留空点保存 → 就地拦住，且主进程没有去起后端**
     （用户实测：原来会起后端报 1045 + Traceback；现在必须只提示"先补上这几项"）
  3. 两个「随机生成」按钮存在（跨 IPC 契约那条 bug 的墓碑）
  4. 点按钮 → 输入框里真的出现长串
  5. 填对全部 → 「测试连接」通过
  6. 后端报错时：结论在正文、**Traceback 折叠在「查看详情」里**
  7. 测试结束后 `config\\.env.probe` **被删掉**（它是"测试连接"的临时文件，
     里面是真实口令，不该留在用户目录）
  7b. 测试通过后 `config\\.env` **仍然不存在**（"测试连接"不许改用户配置）
  8. 保存 → 进控制台

用法：
    python desktop/scripts/verify_wizard_dev.py --repo-root <仓库根> [--userdata <临时profile>]
退出码 0 = 全过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import pathlib

import websockets

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

FAILED: list[str] = []


def ok(text: str) -> None:
    print(f"  [OK] {text}")


def bad(text: str) -> None:
    print(f"  [!!] {text}")
    FAILED.append(text)


def load_env_file(path: pathlib.Path) -> dict:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Cdp:
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

    async def run(self, expression: str, await_promise: bool = False):
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            raise RuntimeError(
                "页面里执行出错："
                + str(detail.get("text"))
                + " "
                + str((detail.get("exception") or {}).get("description", ""))
            )
        return result.get("result", {}).get("value")


def find_debug_port(userdata: pathlib.Path) -> int:
    """应用把调试端口写在 userData/DevToolsActivePort 里（第一行）。"""
    f = userdata / "DevToolsActivePort"
    for _ in range(60):
        if f.exists():
            try:
                return int(f.read_text(encoding="utf-8").splitlines()[0].strip())
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("等不到 DevToolsActivePort（应用可能没起来）")


def find_target(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
            for t in json.load(resp):
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t
    except Exception:
        return None
    return None


async def wait_for_setup_page(cdp: Cdp, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ready = await cdp.run("Boolean(window.yunmengSetup && window.yunmengSetup.getFields)")
            if ready:
                url = str(await cdp.run("location.href"))
                if "setup.html" in url:
                    return True
        except RuntimeError:
            pass
        await asyncio.sleep(0.5)
    return False


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--userdata", default="")
    parser.add_argument("--keep", action="store_true", help="跑完保留临时 profile")
    args = parser.parse_args()

    repo = pathlib.Path(args.repo_root).resolve()
    desktop = repo / "desktop"
    electron = desktop / "node_modules" / "electron" / "dist" / "electron.exe"
    if not electron.exists():
        print(f"× 找不到 Electron：{electron}")
        return 1

    env_file = repo / ".env"
    if not env_file.exists():
        print(f"× 找不到 {env_file}（验收需要真实的 MySQL 连接信息）")
        return 1
    env_values = load_env_file(env_file)

    userdata = pathlib.Path(args.userdata) if args.userdata else pathlib.Path(
        tempfile.mkdtemp(prefix="hne-wizard-dev-")
    )
    userdata.mkdir(parents=True, exist_ok=True)
    print(f"临时 profile：{userdata}")

    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["HNE_DESKTOP_PROFILE"] = str(userdata)
    env["HNE_DESKTOP_STRICT_CONFIG"] = "1"  # 关掉"仓库根 .env 兜底"，确保进向导
    env["HNE_DESKTOP_DEBUG"] = "1"

    proc = subprocess.Popen(
        # ★ `--remote-debugging-port=0` 不能省：应用自己**不会**开调试端口，
        #   而 0 表示"让系统选一个"，选中的端口会被 Electron 写进
        #   `<userData>\DevToolsActivePort`（第一行）—— 我们就是靠这个找过去的。
        #   第一版漏了这个参数，于是永远等不到那个文件。
        [str(electron), ".", "--remote-debugging-port=0"],
        cwd=str(desktop),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Electron 已启动（PID {proc.pid}）")

    try:
        port = find_debug_port(userdata)
        print(f"调试端口：{port}")
        target = None
        deadline = time.time() + 60
        while time.time() < deadline and not target:
            target = find_target(port)
            if not target:
                await asyncio.sleep(0.5)
        if not target:
            print("× 连不上调试端口")
            return 1

        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
            cdp = Cdp(ws)
            await cdp.call("Runtime.enable")

            print("\n[1] 全新 profile 应落到首次设置页")
            if not await wait_for_setup_page(cdp):
                bad(f"没进首次设置页（当前 {await cdp.run('location.href')}）")
                return 1
            ok(f"落在 {await cdp.run('location.href')}")

            print("\n[1b] 「存储方式」选择项：默认本机文件，MySQL 字段随之隐藏/出现")
            storage_tag = str(await cdp.run(
                "document.querySelector('#f-HNE_DB_BACKEND')?.tagName || '(none)'"
            ))
            if storage_tag == "SELECT":
                ok("存储方式渲染成了下拉框（用户不用猜能填什么）")
            else:
                bad(f"存储方式不是下拉框，实际是 {storage_tag}")
            option_values = await cdp.run(
                "JSON.stringify([...document.querySelector('#f-HNE_DB_BACKEND').options].map(o => o.value))"
            )
            if "sqlite" in str(option_values) and "mysql" in str(option_values):
                ok(f"两个选项都在：{option_values}")
            else:
                bad(f"选项不对：{option_values}")
            default_backend = str(await cdp.run("document.querySelector('#f-HNE_DB_BACKEND').value"))
            if default_backend == "sqlite":
                ok("全新 profile 下默认就是「本机文件（SQLite）」= 零配置")
            else:
                bad(f"全新 profile 的默认存储方式不是 sqlite，而是 {default_backend}")

            # ★ 切到"本机文件"：MySQL 那一组必须**整组隐藏且禁用** ——
            #   否则用户会以为"不填就不给用"，零配置就是假的。
            await cdp.run("""
              (() => {
                const sel = document.querySelector('#f-HNE_DB_BACKEND');
                sel.value = 'sqlite';
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              })()
            """)
            await asyncio.sleep(0.3)
            hidden_mysql = await cdp.run("""
              [...document.querySelectorAll('#mysql-grid [data-db-only]')]
                .filter(b => b.style.display === 'none').length
            """)
            total_mysql = await cdp.run("document.querySelectorAll('#mysql-grid [data-db-only]').length")
            disabled_inputs = await cdp.run("""
              [...document.querySelectorAll('#mysql-grid input')].filter(i => i.disabled).length
            """)
            if int(total_mysql) >= 5 and int(hidden_mysql) == int(total_mysql):
                ok(f"选「本机文件」时 {total_mysql} 个 MySQL 字段全部隐藏")
            else:
                bad(f"MySQL 字段没有全部隐藏：{hidden_mysql}/{total_mysql}")
            if int(disabled_inputs) == int(total_mysql):
                ok("隐藏的同时也禁用了（避免残留值被写进配置）")
            else:
                bad(f"隐藏了但没禁用：{disabled_inputs}/{total_mysql}")
            # ★ 零配置的关键结论：**什么都不填**就该能过本地体检。
            #   这里调的是页面里真正在用的那个纯函数（与保存路径同一份代码），
            #   字段定义从主进程现取 —— 不自己造一份，免得"测的不是真东西"。
            zero_ok = str(await cdp.run("""
              (async () => {
                const info = await window.yunmengSetup.getFields();
                const v = {};
                for (const f of info.fields) {
                  const el = document.querySelector('#f-' + f.key);
                  v[f.key] = el ? el.value : '';
                }
                return window.HneSetupValidate.precheck({ fields: info.fields, values: v }).ok;
              })()
            """, True))
            if zero_ok == "True":
                ok("零配置（本机文件）：一格都不填也能过本地体检")
            else:
                bad(f"选本机文件时本地体检仍然拦人：{zero_ok}")

            # 切回 MySQL，让余下用例走原来的那条路
            # ★ 为什么不用环境变量 `HNE_DB_BACKEND=mysql` 一步到位：
            #   向导表单的默认值来自**配置文件**（main.js 的 getSetupFields 只读
            #   `config\.env`），壳的进程环境变量**不参与**表单预填 ——
            #   第一版验收就是这么假设的，于是"环境变量没生效"被报成失败。
            #   而按用户的方式点一下下拉框，本来就是更真实的路径。
            await cdp.run("""
              (() => {
                const sel = document.querySelector('#f-HNE_DB_BACKEND');
                sel.value = 'mysql';
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              })()
            """)
            await asyncio.sleep(0.3)
            shown_mysql = await cdp.run("""
              [...document.querySelectorAll('#mysql-grid [data-db-only]')]
                .filter(b => b.style.display !== 'none').length
            """)
            if int(shown_mysql) == int(total_mysql):
                ok("切回 MySQL 后字段重新出现")
            else:
                bad(f"切回 MySQL 后字段没全部出现：{shown_mysql}/{total_mysql}")

            print("\n[2] 「随机生成」按钮（跨 IPC 契约那条 bug 的墓碑）")
            buttons = await cdp.run(
                "[...document.querySelectorAll('button')].filter(b => b.textContent.trim() === '随机生成').length"
            )
            if buttons == 2:
                ok("两个密钥字段各有一个「随机生成」按钮")
            else:
                bad(f"「随机生成」按钮数量不对：{buttons}（应为 2）")

            print("\n[2b] 「小眼睛」：默认隐藏，点一下显示、再点回隐藏")
            secret_secret = str(await cdp.run("document.querySelector('#f-HNE_SECRET_KEY').type"))
            secret_pw = str(await cdp.run("document.querySelector('#f-HNE_MYSQL_PASSWORD').type"))
            eyes = int(await cdp.run(
                "[...document.querySelectorAll('button.eye')].length"
            ) or 0)
            if secret_secret == "password" and secret_pw == "password":
                ok("所有密码框默认都是隐藏的（不会一开页面就把秘密摊在屏幕上）")
            else:
                bad(f"密码框默认没有隐藏：secret={secret_secret} password={secret_pw}")
            if eyes >= 3:
                ok(f"有 {eyes} 个「👁」按钮（3 个密码字段各一个）")
            else:
                bad(f"「👁」按钮太少：{eyes}（3 个密码字段应各有一个）")

            # 点第一个眼睛：应变成明文，且图标换成 🙈
            await cdp.run("""
              (() => {
                const btn = document.querySelector('button.eye');
                btn.click();
                return true;
              })()
            """)
            await asyncio.sleep(0.4)
            after_type = str(await cdp.run("document.querySelector('button.eye').closest('.row').querySelector('input').type"))
            after_icon = str(await cdp.run("document.querySelector('button.eye').textContent"))
            after_pressed = str(await cdp.run("document.querySelector('button.eye').getAttribute('aria-pressed')"))
            if after_type == "text":
                ok("点一下 → 变成明文（能看见内容了）")
            else:
                bad(f"点了眼睛但 input.type 还是 {after_type}")
            if after_icon == "🙈":
                ok("图标变成 🙈（用户知道再点一下就隐藏）")
            else:
                bad(f"图标没有跟着变，仍是 {after_icon}")
            if after_pressed == "true":
                ok("aria-pressed=true（读屏软件也能知道当前是显示状态）")
            else:
                bad(f"aria-pressed 没更新：{after_pressed}")

            # 再点一次：应回到隐藏
            await cdp.run("document.querySelector('button.eye').click()")
            await asyncio.sleep(0.4)
            back_type = str(await cdp.run("document.querySelector('button.eye').closest('.row').querySelector('input').type"))
            back_icon = str(await cdp.run("document.querySelector('button.eye').textContent"))
            if back_type == "password" and back_icon == "👁":
                ok("再点一下 → 回到隐藏（图标也变回 👁）")
            else:
                bad(f"再点一下没有回到隐藏：type={back_type} icon={back_icon}")

            # 复制按钮：点了不能报错（file:// 下剪贴板可能被拒，脚本会退到 execCommand）
            copies = int(await cdp.run(
                "[...document.querySelectorAll('button')].filter(b => b.textContent.trim() === '复制').length"
            ) or 0)
            if copies >= 2:
                ok(f"有 {copies} 个「复制」按钮（只给要生成/抄走的字段）")
            else:
                bad(f"「复制」按钮数量不对：{copies}（两个密钥字段各应有一个）")
            await cdp.run("""
              (() => {
                const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '复制');
                if (btn) btn.click();
                return true;
              })()
            """)
            await asyncio.sleep(0.6)
            copy_label = str(await cdp.run("""
              (() => {
                const btn = [...document.querySelectorAll('button')].find(b => b.textContent.trim() === '已复制' || b.textContent.trim() === '复制');
                return btn ? btn.textContent.trim() : '(none)';
              })()
            """))
            if copy_label in ("已复制", "复制"):
                ok(f"点「复制」后按钮状态正常（{copy_label}），没有抛错")
            else:
                bad(f"点「复制」后按钮状态异常：{copy_label}")

            print("\n[3] 必填项没填 → 就地拦住，且**不去起后端**")
            # 先把所有字段清空，只留下主机，模拟用户"口令没填"的情形
            await cdp.run("""
              (() => {
                const inputs = [...document.querySelectorAll('input')];
                for (const i of inputs) i.value = '';
                const host = document.querySelector('#f-HNE_MYSQL_HOST');
                if (host) host.value = '127.0.0.1';
                return true;
              })()
            """)
            before = await cdp.run("document.getElementById('status').textContent")
            await cdp.run("document.getElementById('form').requestSubmit()")
            await asyncio.sleep(2.0)
            after = await cdp.run("document.getElementById('status').textContent")
            field_errors = await cdp.run("""
              [...document.querySelectorAll('.err')].map(e => e.textContent).filter(Boolean).length
            """)
            if "先补上这几项" in str(after):
                ok("提示：先补上这几项（用户一眼知道缺什么）")
            else:
                bad("没有给出『先补上这几项』的提示，实际状态：" + str(after)[:120])
            if field_errors and int(field_errors) >= 2:
                ok(f"{field_errors} 个字段就地高亮（用户不用猜是哪一格）")
            else:
                bad(f"字段级错误提示太少：{field_errors}")
            # ★ 关键：主进程**没有**被叫起来 —— 判据是这一段里日志没有"配置自检"字眼
            log = (userdata / "logs" / "desktop.log").read_text(encoding="utf-8", errors="replace")
            if "配置自检（测试连接）" not in log and "配置自检（保存前验证）" not in log:
                ok("主进程没有被叫去起后端（本地就拦住了）")
            else:
                bad("必填没填却仍然起了后端 —— 本地体检没生效")

            print("\n[4] 点「随机生成」→ 输入框真的出现长串")
            await cdp.run("""
              [...document.querySelectorAll('button')]
                .find(b => b.textContent.trim() === '随机生成').click()
            """)
            await asyncio.sleep(1.0)
            generated = await cdp.run("document.querySelector('#f-HNE_SECRET_KEY').value.length")
            if int(generated or 0) >= 32:
                ok(f"签名密钥已生成（长度 {generated}）")
            else:
                bad(f"点「随机生成」后密钥长度不对：{generated}")

            print("\n[5] 填对全部 → 「测试连接」应通过")
            values = {
                "HNE_MYSQL_HOST": env_values.get("HNE_MYSQL_HOST", "127.0.0.1"),
                "HNE_MYSQL_PORT": env_values.get("HNE_MYSQL_PORT", "3306"),
                "HNE_MYSQL_USER": env_values.get("HNE_MYSQL_USER", "narrative_app"),
                "HNE_MYSQL_PASSWORD": env_values.get("HNE_MYSQL_PASSWORD", ""),
                "HNE_MYSQL_DB": env_values.get("HNE_MYSQL_DB", "narrative_engine"),
                "HNE_SECRET_KEY": "wizard-verify-secret-key-0123456789abcdef",
                "HNE_API_KEY_ENCRYPTION_KEY": "d2l6YXJkLXZlcmlmeS1mZXJuZXQta2V5LTMyYnl0ZXM=",
            }
            filled = await cdp.run(
                "(() => { const v = %s; let n = 0;"
                " for (const [k, val] of Object.entries(v)) { const el = document.querySelector('#f-' + k);"
                "   if (el) { el.value = val; el.dispatchEvent(new Event('input', { bubbles: true })); n++; } }"
                " return n; })()" % json.dumps(values)
            )
            ok(f"填了 {filled} 个字段")
            await cdp.run("document.getElementById('btn-test').click()")
            verdict = ""
            for _ in range(300):
                await asyncio.sleep(1.0)
                text = str(await cdp.run("document.getElementById('status').textContent"))
                if "连接正常" in text or "失败" in text or "不可用" in text or "测试失败" in text:
                    verdict = text
                    break
            if "连接正常" in verdict:
                ok(f"测试连接通过：{verdict.splitlines()[0][:60]}")
            else:
                bad(f"测试连接没通过：{verdict[:200]}")

            print("\n[6] 「测试连接」不许改用户配置；临时探测文件要删掉")
            if not (userdata / "config" / ".env").exists():
                ok("config\\.env 未被创建（测试连接只是试，不落盘）")
            else:
                bad("「测试连接」把配置写进 config\\.env 了 —— 越权改了用户配置")
            if not (userdata / "config" / ".env.probe").exists():
                ok("探测文件 .env.probe 已清理")
            else:
                p = userdata / "config" / ".env.probe"
                bad(f"探测文件仍在：{p}（{p.stat().st_size} 字节，里面是真实口令）")

            print("\n[7] 报错细节应折叠在「查看详情」里")
            # 制造一次"口令错"的失败：把口令改成错的再测一次
            await cdp.run("document.querySelector('#f-HNE_MYSQL_PASSWORD').value = 'definitely-wrong'")
            await cdp.run("document.getElementById('btn-test').click()")
            detail_text = ""
            for _ in range(300):
                await asyncio.sleep(1.0)
                has_toggle = await cdp.run("Boolean(document.querySelector('#status .detail-toggle'))")
                if has_toggle:
                    detail_text = str(await cdp.run("document.querySelector('#status').textContent"))
                    break
            if detail_text:
                ok("出现了「查看详情」按钮（细节默认折叠）")
                hidden = await cdp.run("document.querySelector('#status pre')?.hidden === true")
                if hidden:
                    ok("细节默认是折叠的（正文只剩结论）")
                else:
                    bad("细节没有默认折叠，用户还是会先看到一大段日志")
                pre = str(await cdp.run("document.querySelector('#status pre').textContent"))
                if "access denied" in pre.lower() or "1045" in pre:
                    ok("折叠内容里确实有那条真实原因（信息没丢）")
                else:
                    print(f"      折叠详情前 80 字：{pre[:80]}")
                # 展开/收起的交互
                await cdp.run("document.querySelector('#status .detail-toggle').click()")
                await asyncio.sleep(0.3)
                expanded = await cdp.run("document.querySelector('#status pre')?.hidden === false")
                if expanded:
                    ok("点「查看详情」能展开")
                else:
                    bad("点了「查看详情」但内容没展开")
            else:
                bad("口令错的时候没有出现可折叠的详情")

            print("\n[8] 保存 → 应进控制台")
            await cdp.run("document.querySelector('#f-HNE_MYSQL_PASSWORD').value = %s"
                          % json.dumps(values["HNE_MYSQL_PASSWORD"]))
            await cdp.run("document.getElementById('form').requestSubmit()")
            url_after = ""
            for _ in range(300):
                await asyncio.sleep(1.0)
                try:
                    url_after = str(await cdp.run("location.href"))
                except RuntimeError:
                    url_after = ""
                if "/console/" in url_after:
                    break
            if "/console/" in url_after:
                ok(f"进入控制台：{url_after}")
            else:
                status = await cdp.run("document.getElementById('status')?.textContent")
                bad(f"没进控制台，当前 URL={url_after}，页面提示：{str(status)[:200]}")
            if (userdata / "config" / ".env").exists():
                ok("config\\.env 已写入（保存时才落盘）")
            else:
                bad("保存后 config\\.env 仍不存在")

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        # 收掉可能残留的后端
        try:
            subprocess.run(["taskkill", "/IM", "backend.exe", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if args.keep:
            print(f"\n（按 --keep 保留 profile：{userdata}）")
        else:
            shutil.rmtree(userdata, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for item in FAILED:
            print(f"  · {item}")
        return 1
    print("向导开发态验收：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
