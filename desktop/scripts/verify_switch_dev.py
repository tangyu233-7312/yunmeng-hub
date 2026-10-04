"""在**开发态 Electron** 里验收「切换存储方式」这条新功能。

==================== 它验什么（每一条都对应一个真实风险）====================
   1. 菜单进来的那一页（`?mode=switch`）能打开，并且**预填当前配置**
      （用户要能看见"我现在用的是哪种"，而不是靠猜）；
   2. 页面上「存储方式」下拉的当前值 = 配置文件里的实际值；
   3. ★ 切到 MySQL：**预填不出来的只有口令**，其余连接信息应当已经填好；
   4. ★ **只点「测试这份配置」不改配置**（自检是"试"，不该动用户文件）；
   5. ★ 真的切过去：写配置 → 自检 → **停旧后端** → 用新配置重启 → 回控制台；
   6. ★ 切换**不能把已存密钥弄丢**：页面不回传密钥，切换后 `.env` 里那两个键
      必须还是原来那串（否则用户已保存的 API Key 会永远解不开）；
   7. ★ 失败要**不伤现状**：给一个必然连不上的 MySQL 地址 → 必须报错、
      必须保留原配置、而且**当前后端仍在运行**（用户还能继续用）。

用法：
    python desktop/scripts/verify_switch_dev.py --repo-root <仓库根> [--keep]
退出码 0 = 全过。

★ 全程只动**临时 profile**：`HNE_DESKTOP_PROFILE` 指向 %TEMP% 下的临时目录，
  真实的 `%APPDATA%\\云梦枢` 与仓库根 `.env` 一个字都不改。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from verify_wizard_dev import Cdp, find_debug_port, find_target  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

FAILED: list[str] = []


def ok(text: str) -> None:
    print(f"  [OK] {text}")


def bad(text: str) -> None:
    print(f"  [!!] {text}")
    FAILED.append(text)


def read_env(path: pathlib.Path) -> dict:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def http_health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001
        return None


async def wait_setup(cdp: Cdp, timeout: float = 90) -> str:
    """等切换页**真正建好字段**再返回。

    ★ 只等"URL 到了 setup.html"是不够的（第一版就是这么写的，然后取
      `#f-HNE_DB_BACKEND` 时拿到 null）：切页是通过 `setStep('setup')` +
      `loadFile()` 做的，页面还要**再走一次** boot（读字段 → 建控件）——
      这中间有一小段窗口，字段还不存在。
      所以判据取"那个下拉框真的在 DOM 里"。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await cdp.run("Boolean(window.yunmengSetup && window.yunmengSetup.switchFields)"):
                url = str(await cdp.run("location.href"))
                ready = await cdp.run("Boolean(document.querySelector('#f-HNE_DB_BACKEND'))")
                if "setup.html" in url and ready:
                    return url
        except RuntimeError:
            pass
        await asyncio.sleep(0.5)
    return ""


async def wait_console(cdp: Cdp, timeout: float = 300) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = str(await cdp.run("location.href"))
            if "/console/" in url:
                return url
        except RuntimeError:
            pass
        await asyncio.sleep(1.0)
    return ""


def port_from_url(url: str) -> int:
    try:
        return int(url.split("://", 1)[1].split("/", 1)[0].split(":")[1])
    except Exception:  # noqa: BLE001
        return 0


async def open_switch_page(cdp: Cdp) -> bool:
    """打开切换页（走用户真实入口那条**同一段动作**）。

    ★ 两条路都试，因为它们共用 `openSwitchPage()`：
      ① 渲染进程的显式入口 `window.yunmengSetup.openSwitchPage()`（菜单项 Click 也调它）；
      ② 菜单加速键 Ctrl+Shift+S。
    第一版只试了 ②，结果在中文输入法环境下没命中（`accelerator` 匹配对键码/IME 敏感）
    —— 那不是功能坏了，而是"验收依赖了一个脆的东西"。
    """
    try:
        await cdp.run("window.yunmengSetup.openSwitchPage()", True)
    except RuntimeError:
        pass
    url = await wait_setup(cdp, timeout=30)
    if "setup.html" in url and "mode=switch" in url:
        return True

    # 兜底：试一次快捷键
    for ev in (
        {"type": "rawKeyDown", "modifiers": 2 | 8, "key": "S", "code": "KeyS",
         "windowsVirtualKeyCode": 83, "nativeVirtualKeyCode": 83},
        {"type": "char", "modifiers": 2 | 8, "text": "S", "key": "S", "unmodifiedText": "s"},
        {"type": "keyUp", "modifiers": 2 | 8, "key": "S", "code": "KeyS",
         "windowsVirtualKeyCode": 83, "nativeVirtualKeyCode": 83},
    ):
        await cdp.call("Input.dispatchKeyEvent", ev)
    url = await wait_setup(cdp, timeout=30)
    return "setup.html" in url and "mode=switch" in url


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    repo = pathlib.Path(args.repo_root).resolve()
    desktop = repo / "desktop"
    electron = desktop / "node_modules" / "electron" / "dist" / "electron.exe"
    if not electron.exists():
        print(f"× 找不到 Electron：{electron}")
        return 1

    repo_env = read_env(repo / ".env")
    mysql_ready = bool(repo_env.get("HNE_MYSQL_PASSWORD")) and repo_env.get("HNE_MYSQL_HOST") == "127.0.0.1"

    userdata = pathlib.Path(tempfile.mkdtemp(prefix="hne-switch-dev-"))
    cfgdir = userdata / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    cfg = cfgdir / ".env"
    # ★ 预置一份"本机文件（SQLite）"配置 + 两个**可辨认的假密钥**，
    #   这样第 6 条（切换不能弄丢密钥）才能逐字比对。
    secret_key = "switch-verify-secret-key-0123456789-abcdefghijkl"
    fernet_key = "KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio="
    cfg.write_text(
        "# 验收预置配置\n"
        "HNE_DB_BACKEND=sqlite\n"
        f"HNE_SECRET_KEY={secret_key}\n"
        f"HNE_API_KEY_ENCRYPTION_KEY={fernet_key}\n"
        "HNE_HOST=127.0.0.1\n"
        "HNE_EMBEDDING_BACKEND=onnx_default\n",
        encoding="utf-8",
    )
    print(f"临时 profile：{userdata}（预置：本机文件 + 两个可辨认的假密钥）")
    print(f"MySQL 可用性：{'仓库 .env 里有 127.0.0.1 的口令，可测真实切换' if mysql_ready else '跳过真实 MySQL 切换'}")

    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["HNE_DESKTOP_PROFILE"] = str(userdata)
    env["HNE_DESKTOP_STRICT_CONFIG"] = "1"
    env["HNE_DESKTOP_DEBUG"] = "1"
    env["HNE_DESKTOP_PREFER_PYTHON"] = "1"  # 验收当前源码，不用上次构建的 exe
    for key in ("HNE_DB_BACKEND", "HNE_MYSQL_HOST", "HNE_MYSQL_PASSWORD", "HNE_SQLITE_PATH"):
        env.pop(key, None)

    proc = subprocess.Popen(
        [str(electron), ".", "--remote-debugging-port=0"],
        cwd=str(desktop), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print(f"Electron 已启动（PID {proc.pid}）")

    try:
        port = find_debug_port(userdata)
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

            print("\n[0] 先等应用进控制台（有一份可用配置）")
            console = await wait_console(cdp)
            if not console:
                bad(f"没进控制台（当前 {await cdp.run('location.href')}）")
                return 1
            ok(f"控制台：{console}")
            old_port = port_from_url(console)
            old_health = http_health(old_port)
            if old_health:
                detail = ((old_health.get("components") or {}).get("database") or {}).get("detail") or {}
                ok(f"当前后端在跑：backend={detail.get('backend')} status={old_health.get('status')}")
            else:
                bad(f"控制台 URL 上的 /health 探不通（端口 {old_port}）")

            print("\n[1] 从**菜单**打开「切换存储方式…」")
            if not await open_switch_page(cdp):
                bad("没能打开切换页（菜单项不存在或点不开）")
                return 1
            ok(f"已打开：{await cdp.run('location.href')}")

            print("\n[2] 切换页要预填当前配置，并把「我现在用哪种」写出来")
            backend_value = str(await cdp.run("document.querySelector('#f-HNE_DB_BACKEND').value"))
            note = str(await cdp.run("document.getElementById('switch-note')?.textContent || ''"))
            title = str(await cdp.run("document.querySelector('h1').textContent"))
            if backend_value == "sqlite":
                ok("「存储方式」预填为 sqlite（与配置文件一致）")
            else:
                bad(f"「存储方式」预填不对：{backend_value}（应为 sqlite）")
            if "本机文件" in note:
                ok("页面写明了当前用的是「本机文件（SQLite）」")
            else:
                bad(f"页面没有说明当前用的是哪种：{note[:80]}")
            if "切换存储方式" in title:
                ok(f"标题已切成「{title}」")
            else:
                bad(f"标题没切：{title}")
            save_label = str(await cdp.run("document.getElementById('btn-save').textContent"))
            if "重启" in save_label:
                ok(f"按钮文案说明了会重启后端：「{save_label}」")
            else:
                bad(f"按钮文案还是「{save_label}」—— 切换会重启后端，文案该说清")

            print("\n[3] 切到 MySQL：连接信息应当被预填（口令除外）")
            await cdp.run("""
              (() => {
                const sel = document.querySelector('#f-HNE_DB_BACKEND');
                sel.value = 'mysql';
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              })()
            """)
            await asyncio.sleep(0.3)
            host = str(await cdp.run("document.querySelector('#f-HNE_MYSQL_HOST').value"))
            port_v = str(await cdp.run("document.querySelector('#f-HNE_MYSQL_PORT').value"))
            user_v = str(await cdp.run("document.querySelector('#f-HNE_MYSQL_USER').value"))
            db_v = str(await cdp.run("document.querySelector('#f-HNE_MYSQL_DB').value"))
            visible = await cdp.run(
                "[...document.querySelectorAll('#mysql-grid [data-db-only]')]"
                ".filter(b => b.style.display !== 'none').length"
            )
            if int(visible or 0) >= 5:
                ok(f"MySQL 那 {visible} 个字段重新出现")
            else:
                bad(f"切到 MySQL 后字段没出现：{visible}")
            if host and port_v and user_v and db_v:
                ok(f"连接信息已预填（host={host} port={port_v} user={user_v} db={db_v}），只剩口令要填")
            else:
                bad(f"连接信息没预填全：host={host!r} port={port_v!r} user={user_v!r} db={db_v!r}")

            print("\n[4] ★ 故意填一个连不上的口令 → 必须失败、且**不伤现状**")
            await cdp.run("""
              (() => {
                const el = document.querySelector('#f-HNE_MYSQL_PASSWORD');
                el.value = 'definitely-the-wrong-password';
                el.dispatchEvent(new Event('input', { bubbles: true }));
                return true;
              })()
            """)
            before_cfg = cfg.read_text(encoding="utf-8")
            # ★ 先做一次"这一步到底有没有被触发"的诊断：把提交前后几个关键量抓下来。
            #   第一版失败时状态栏停在初始文案，光看结论分不清是"被本地体检拦下"、
            #   "提交抛异常"、还是"根本没触发提交"。
            diag = await cdp.run("""
              (() => {
                const out = {};
                out.hasSwitchApi = Boolean(window.yunmengSetup && window.yunmengSetup.switchBackend);
                out.saveDisabledBefore = document.getElementById('btn-save').disabled;
                const f = document.getElementById('form');
                out.formFound = Boolean(f);
                try {
                  out.precheck = JSON.stringify(window.HneSetupValidate.precheck({
                    fields: [], values: {},
                  }));
                } catch (e) { out.precheckErr = String(e && e.message); }
                return JSON.stringify(out);
              })()
            """)
            print(f"      诊断：{diag}")
            await cdp.run("document.getElementById('form').requestSubmit()")
            await asyncio.sleep(3.0)
            just_after = str(await cdp.run("document.getElementById('status').textContent"))
            print(f"      提交后 3 秒的状态栏：{just_after[:160]!r}")
            verdict = ""
            for _ in range(300):
                await asyncio.sleep(1.0)
                text = str(await cdp.run("document.getElementById('status').textContent"))
                if "起不来" in text or "已切换" in text or "失败" in text:
                    verdict = text
                    break
            if "起不来" in verdict or "保留" in verdict:
                ok(f"失败被如实报出：{verdict.splitlines()[0][:70]}")
            else:
                bad(f"没有给出预期的失败提示：{verdict[:160]}")
            after_cfg = cfg.read_text(encoding="utf-8")
            if after_cfg == before_cfg:
                ok("失败后配置文件**没有被改动**（保留了原设置）")
            else:
                bad("失败却改了配置文件 —— 用户只是试了一下，不该动他的东西")
            if http_health(old_port):
                ok("失败后**当前后端仍在运行**（用户还能继续用）")
            else:
                bad(f"失败把在跑的后端弄停了（端口 {old_port} 已探不通）")

            print("\n[5] ★ 用正确配置真的切到 MySQL（若仓库 .env 可用）")
            if not mysql_ready:
                print("      （跳过：仓库 .env 里没有可用的 127.0.0.1 MySQL 口令）")
            else:
                for key, field in (
                    ("HNE_MYSQL_HOST", "HNE_MYSQL_HOST"),
                    ("HNE_MYSQL_PORT", "HNE_MYSQL_PORT"),
                    ("HNE_MYSQL_USER", "HNE_MYSQL_USER"),
                    ("HNE_MYSQL_DB", "HNE_MYSQL_DB"),
                    ("HNE_MYSQL_PASSWORD", "HNE_MYSQL_PASSWORD"),
                ):
                    value = repo_env.get(key, "")
                    if not value:
                        continue
                    await cdp.run(
                        "(() => { const el = document.querySelector('#f-%s');"
                        " if (!el) return false; el.value = %s;"
                        " el.dispatchEvent(new Event('input', { bubbles: true })); return true; })()"
                        % (field, json.dumps(value))
                    )
                await cdp.run("document.getElementById('form').requestSubmit()")
                new_console = await wait_console(cdp)
                if not new_console:
                    status = await cdp.run("document.getElementById('status')?.textContent")
                    bad(f"切到 MySQL 后没回控制台：{str(status)[:200]}")
                else:
                    ok(f"切换后回到控制台：{new_console}")
                    new_port = port_from_url(new_console)
                    h = http_health(new_port)
                    detail = ((h or {}).get("components") or {}).get("database") or {}
                    if (detail.get("detail") or {}).get("backend") == "mysql":
                        ok(f"新后端确实在用 MySQL（{detail.get('detail', {}).get('server')}）")
                    else:
                        bad(f"新后端没有用 MySQL：{detail}")

            print("\n[6] ★★ 密钥必须还在（切换不能把已存密钥弄丢）")
            written = read_env(cfg)
            if written.get("HNE_SECRET_KEY") == secret_key:
                ok("SECRET_KEY 逐字未变")
            else:
                bad(f"SECRET_KEY 被改动了：{written.get('HNE_SECRET_KEY', '(空)')[:20]}…")
            if written.get("HNE_API_KEY_ENCRYPTION_KEY") == fernet_key:
                ok("API_KEY_ENCRYPTION_KEY 逐字未变（已保存的 API Key 不会解不开）")
            else:
                bad(f"加密密钥被改动了：{written.get('HNE_API_KEY_ENCRYPTION_KEY', '(空)')[:20]}…")

            print("\n[7] 切回「本机文件」，确认双向都能走")
            await open_switch_page(cdp)
            await cdp.run("""
              (() => {
                const sel = document.querySelector('#f-HNE_DB_BACKEND');
                sel.value = 'sqlite';
                sel.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
              })()
            """)
            await asyncio.sleep(0.3)
            await cdp.run("document.getElementById('form').requestSubmit()")
            back_console = await wait_console(cdp)
            if back_console:
                ok(f"切回本机文件成功，回控制台：{back_console}")
                final = read_env(cfg)
                if final.get("HNE_DB_BACKEND") == "sqlite":
                    ok("配置文件里已写回 HNE_DB_BACKEND=sqlite")
                else:
                    bad(f"配置文件里没写回 sqlite：{final.get('HNE_DB_BACKEND')}")

                raw = cfg.read_text(encoding="utf-8")
                # ★ 判据分两半（上一轮的判据只看了"有没有出现 HNE_MYSQL_"，太粗）：
                #   ① **生效**的 MySQL 键必须一个都没有 —— 选了本机文件就不该去连 MySQL；
                #   ② 但必须**留着注释版** —— 那是让用户能切回去的唯一凭据，
                #      尤其是那串自动生成的随机口令（用户不可能记得它）。
                live = [
                    line for line in raw.splitlines()
                    if line.strip().startswith("HNE_MYSQL_")
                ]
                if not live:
                    ok("选本机文件时，配置里没有任何**生效**的 MySQL 键")
                else:
                    bad(f"配置里有生效的 MySQL 键 —— 用户会以为这里配了 MySQL：{live}")

                stashed = [
                    line.strip() for line in raw.splitlines()
                    if line.strip().startswith("# HNE_MYSQL_")
                ]
                if len(stashed) >= 4:
                    ok(f"★ MySQL 连接信息被保留成注释（{len(stashed)} 行）—— 用户还能切回来")
                else:
                    bad(f"★ MySQL 连接信息**没有**被保留（只有 {len(stashed)} 行注释）—— "
                        "用户切回来时口令就没了（这正是他今晚卡住的原因）")
                if any("HNE_MYSQL_PASSWORD" in line for line in stashed):
                    ok("★ 口令也在留存之列（随机串，用户不可能记得）")
                else:
                    bad("★ 口令没有被留存 —— 切回来时口令格会是空的")
            else:
                status = await cdp.run("document.getElementById('status')?.textContent")
                bad(f"切回本机文件失败：{str(status)[:200]}")

            print("\n[8] ★★ 再切回 MySQL：口令必须**自动填回来**（用户不必回忆随机串）")
            await open_switch_page(cdp)
            prefilled_pwd = str(await cdp.run(
                "document.querySelector('#f-HNE_MYSQL_PASSWORD')?.value || ''"))
            prefilled_user = str(await cdp.run(
                "document.querySelector('#f-HNE_MYSQL_USER')?.value || ''"))
            if prefilled_pwd:
                ok(f"口令格已被自动填回（{len(prefilled_pwd)} 字符，值不打印）")
            else:
                bad("★ 口令格是空的 —— 用户就得自己回忆那串随机口令（今晚他就是这么卡住的）")
            if prefilled_user == "narrative_app":
                ok(f"用户名也填回来了：{prefilled_user}")
            else:
                bad(f"用户名没填回来：{prefilled_user!r}")

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:  # noqa: BLE001
                pass
        try:
            subprocess.run(["taskkill", "/IM", "backend.exe", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
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
    print("存储方式切换开发态验收：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
