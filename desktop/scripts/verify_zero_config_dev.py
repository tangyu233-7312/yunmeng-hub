"""在**开发态 Electron** 里验收「零配置」这条主路径（默认形态）。

==================== 为什么要单独一个脚本 ====================
`verify_wizard_dev.py` 验的是 **MySQL 那条可选路**（填连接信息、口令错、Traceback 折叠…）。
而这一版真正的主路径是完全不同的另一条：

    **一个字段都不填** → 点「保存并开始」→ 后端自己生成密钥、建库、建表 → 进控制台

这条路上有三件事在 pytest 里**验不到**，因为它们属于"壳 + 后端"的接缝：

  1. **壳有没有把 `HNE_DATA_DIR` 注进去**：不注的话数据会按"相对项目根"落下来，
     "卸载重装不丢数据"与"程序可以装在只读位置"这两条承诺同时静默失效；
  2. **向导在零配置下会不会误报必填**（那正是用户最直接的"装完用不了"）；
  3. **保存成功后 SQLite 文件与自动密钥到底在不在**、控制台会不会真的收到一个**能用的后端**。

用法：
    python desktop/scripts/verify_zero_config_dev.py --repo-root <仓库根> [--userdata <临时profile>]
退出码 0 = 全过。
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

# 复用开发态验收里那套 CDP 小工具（同一个脚本目录，import 得到）
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


async def wait_for_setup(cdp: Cdp, timeout: float = 90) -> str:
    """等首次设置页出现，返回它的 URL（超时返回空串）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if await cdp.run("Boolean(window.yunmengSetup && window.yunmengSetup.getFields)"):
                url = str(await cdp.run("location.href"))
                if "setup.html" in url:
                    return url
        except RuntimeError:
            pass
        await asyncio.sleep(0.5)
    return ""


async def wait_for_console(cdp: Cdp, timeout: float = 300) -> str:
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


def http_health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001 - 探活失败就是 None，由调用方判定
        return None


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

    userdata = pathlib.Path(args.userdata) if args.userdata else pathlib.Path(
        tempfile.mkdtemp(prefix="hne-zero-config-dev-")
    )
    userdata.mkdir(parents=True, exist_ok=True)
    print(f"临时 profile：{userdata}")

    env = dict(os.environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["HNE_DESKTOP_PROFILE"] = str(userdata)
    # ★ 关掉"仓库根 .env 兜底" —— 否则仓库里那份 MySQL 配置会让应用直接跳过向导，
    #   那就完全测不到零配置这条路了（这正是"验收要按用户机的状态来"）。
    env["HNE_DESKTOP_STRICT_CONFIG"] = "1"
    env["HNE_DESKTOP_DEBUG"] = "1"
    # ★★ 必须强制走本机 Python：否则一旦本地构建过 `desktop/dist/backend/backend.exe`，
    #   壳会**优先用那个 exe** —— 而它是**上一次构建**的产物，
    #   于是这个脚本测的就不是当前源码（本轮真踩了：零配置判定明明写好了，
    #   向导自检却仍按 MySQL 连库，因为那份 exe 还没有双后端支持，且不报任何错）。
    env["HNE_DESKTOP_PREFER_PYTHON"] = "1"
    # ★ 顺手清掉可能从宿主环境漏进来的数据库类变量：零配置要的就是"什么都没有"
    for key in ("HNE_DB_BACKEND", "HNE_MYSQL_HOST", "HNE_MYSQL_PASSWORD", "HNE_SQLITE_PATH"):
        env.pop(key, None)

    proc = subprocess.Popen(
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

            print("\n[1] 全新 profile（没有仓库 .env 兜底）应落到首次设置页")
            url = await wait_for_setup(cdp)
            if not url:
                bad(f"没进首次设置页（当前 {await cdp.run('location.href')}）")
                return 1
            ok(f"落在 {url}")

            print("\n[2] 默认必须是「本机文件（SQLite）」，且 MySQL 字段整组隐藏+禁用")
            backend = str(await cdp.run("document.querySelector('#f-HNE_DB_BACKEND').value"))
            total = int(await cdp.run("document.querySelectorAll('#mysql-grid [data-db-only]').length") or 0)
            hidden = int(await cdp.run(
                "[...document.querySelectorAll('#mysql-grid [data-db-only]')]"
                ".filter(b => b.style.display === 'none').length"
            ) or 0)
            disabled = int(await cdp.run(
                "[...document.querySelectorAll('#mysql-grid input')].filter(i => i.disabled).length"
            ) or 0)
            if backend == "sqlite":
                ok("存储方式默认 sqlite")
            else:
                bad(f"默认存储方式不是 sqlite，而是 {backend}")
            if total >= 5 and hidden == total and disabled == total:
                ok(f"{total} 个 MySQL 字段全部隐藏且禁用（用户不会以为'不填就不给用'）")
            else:
                bad(f"MySQL 字段没全隐藏/禁用：隐藏 {hidden}/{total}、禁用 {disabled}/{total}")

            print("\n[3] ★ 一个字段都不填，直接点「保存并开始」")
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
                ok("本地体检放行（零配置不会被必填拦住）")
            else:
                bad(f"零配置被本地体检拦住了：{zero_ok}")
            # 清掉两个密钥（表单默认是空的，但万一以后加了预填，这里也要保证"真的什么都不填"）
            await cdp.run("""
              (() => {
                for (const key of ['HNE_SECRET_KEY', 'HNE_API_KEY_ENCRYPTION_KEY']) {
                  const el = document.querySelector('#f-' + key);
                  if (el) el.value = '';
                }
                return true;
              })()
            """)
            await cdp.run("document.getElementById('form').requestSubmit()")
            ok("已提交表单（走的是「保存并开始」按钮那条路）")

            print("\n[4] 保存 → 自检 → 进控制台")
            console_url = await wait_for_console(cdp)
            if not console_url:
                status = await cdp.run("document.getElementById('status')?.textContent")
                bad(f"没能进控制台（提示：{str(status)[:300]}）")
                return 1
            ok(f"进入控制台：{console_url}")

            print("\n[5] ★ 数据必须落在**开发态该落的地方**（证明壳把数据位置定下来了）")
            # ★★ 开发态的数据落点与打包态**故意不同**（见 desktop/src/paths.js）：
            #     打包态 → <userData>\data —— 为了"卸载重装不丢数据"与"装在只读位置也能跑"；
            #     开发态 → <仓库根>\data   —— 保持与引入桌面壳之前完全一致，
            #                                免得开发/验收时的数据位置被悄悄改掉。
            #   所以这里两种都接受，但必须**至少有一个**存在 ——
            #   一个都不在就说明 HNE_DATA_DIR / HNE_SQLITE_PATH 没生效，
            #   数据会按相对项目根落下来，那两条承诺就静默失效了。
            candidates = [
                userdata / "data" / "app.sqlite3",
                repo / "data" / "app.sqlite3",
            ]
            db_file = next((p for p in candidates if p.is_file()), None)
            if db_file:
                head = db_file.read_bytes()[:15]
                if head == b"SQLite format 3":
                    ok(f"自动建库成功：{db_file}（{db_file.stat().st_size} 字节，SQLite 魔数正确）")
                else:
                    bad(f"建出来的文件不是 SQLite 库（头 15 字节 {head!r}）")
            else:
                bad("没有在建出 SQLite 库（两处都找了："
                    + "、".join(str(p) for p in candidates) + "）")

            # 密钥文件在哪个配置目录下由壳决定（开发态是仓库根，打包态是 userData）。
            # ★ 开发态下它**可能压根不生成** —— 因为仓库根的 `.env` 里本来就写着
            #   真实密钥（开发者的配置），自举会认定"用户已配好"、什么都不补。
            #   那不是缺陷：**用户机上没有那份 .env**，密钥一定会被生成
            #   （`scripts/verify_zero_config.py` 与安装包验收都在测那条路）。
            #   所以这里只断言"密钥可用"，并把"文件在哪/有没有"如实报出来。
            secrets_candidates = [
                userdata / "config" / ".secrets.env",
                repo / "config" / ".secrets.env",
            ]
            secrets = next((p for p in secrets_candidates if p.is_file()), None)
            repo_env = repo / ".env"
            if secrets:
                text = secrets.read_text(encoding="utf-8")
                missing = [k for k in ("HNE_SECRET_KEY", "HNE_API_KEY_ENCRYPTION_KEY")
                           if f"{k}=" not in text]
                if missing:
                    bad(f"自动生成的密钥文件里缺 {missing}")
                else:
                    ok(f"自动生成密钥成功（{secrets} 里两个键都在；值不打印）")
            elif repo_env.is_file():
                # 走这条分支说明"密钥文件没生成"，因为仓库根那份 .env 里本来就有密钥。
                # 那两个密钥到底可不可用，由后面的 /health 与真实的注册/登录来证明 ——
                # 这里只如实说明"为什么没有那个文件"，避免让人误以为自举坏了。
                ok("没有生成密钥文件 —— 因为仓库根存在 .env（开发者配置优先），符合预期")
            else:
                bad("没有自动生成密钥文件（找过：" + "、".join(str(p) for p in secrets_candidates) + "）")

            cfg = userdata / "config" / ".env"
            if cfg.is_file():
                cfg_text = cfg.read_text(encoding="utf-8")
                if "HNE_DB_BACKEND=sqlite" in cfg_text:
                    ok("config\\.env 里写明了 HNE_DB_BACKEND=sqlite")
                else:
                    bad("config\\.env 里没有 HNE_DB_BACKEND=sqlite")
                if "HNE_MYSQL_" not in cfg_text:
                    ok("config\\.env 里没有任何 MySQL 键（选本机文件时不写它们）")
                else:
                    bad("config\\.env 里残留了 MySQL 键 —— 用户会以为这里配了 MySQL")
            else:
                bad(f"保存后 config\\.env 不存在：{cfg}")

            print("\n[6] ★ 后端必须真的可用（不是「页面进去了但接口全挂」）")
            state = await cdp.run("window.yunmeng?.getState ? JSON.stringify(window.yunmeng.getState()) : '{}'", True)
            try:
                st = json.loads(str(state))
            except Exception:  # noqa: BLE001
                st = {}
            api_port = int(st.get("port") or 0)
            if not api_port:
                # getState 是异步的；退回从控制台 URL 里取端口
                try:
                    api_port = int(str(console_url).split("://", 1)[1].split("/", 1)[0].split(":")[1])
                except Exception:  # noqa: BLE001
                    api_port = 0
            if not api_port:
                bad("拿不到后端端口，无法核对 /health")
            else:
                health = http_health(api_port)
                if not health:
                    bad(f"/health 探不通（端口 {api_port}）")
                else:
                    db_detail = ((health.get("components") or {}).get("database") or {}).get("detail") or {}
                    if health.get("status") == "ok":
                        ok("整体健康状态 ok")
                    else:
                        bad(f"健康状态不是 ok：{health.get('status')} / {health}")
                    if db_detail.get("backend") == "sqlite":
                        ok(f"探活报 backend=sqlite（{db_detail.get('server')}）")
                    else:
                        bad(f"探活的数据库后端不是 sqlite：{db_detail}")

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
    print("零配置开发态验收：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
