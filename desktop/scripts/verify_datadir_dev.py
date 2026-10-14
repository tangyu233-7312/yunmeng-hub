"""在**开发态 Electron** 里验收新增的「数据目录（HNE_DATA_DIR）」这项可选项。

============================ 为什么要真机验 ============================
单测能证明"字段表里有这一项、`.env` 里写得出来"，但证明不了**界面里真的能填到**：
  · 字段可能被分错组 —— 落进 MySQL 那组后，选默认的「本机文件」时整组隐藏，
    用户根本看不见它（`dirOnly` 就是为此加的）；
  · 属性可能过不了 IPC 边界 —— 页面拿到 `undefined`，分组判断失效
    （本项目踩过同类 bug：`generate` 函数过不了 IPC，按钮永远不出现）。

所以在真实 Electron 里走一遍：打开向导 → 填数据目录 → 保存 → 查 `.env` 真的写进去了。

用法：
    python desktop/scripts/verify_datadir_dev.py --repo-root <仓库根>
退出码 0 = 全过。

★ 不动用户真实的 `%APPDATA%\\云梦枢`：用 `HNE_DESKTOP_PROFILE` 指临时 profile。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
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


async def wait_setup(cdp: Cdp, timeout: float = 90) -> str:
    """等向导页把字段真正建出来（只等 URL 不够，见 verify_switch_dev 的说明）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = str(await cdp.run("location.href"))
        except RuntimeError:
            url = ""
        if "setup.html" in url:
            has = await cdp.run("Boolean(document.querySelector('#f-HNE_DATA_DIR'))")
            if has is True:
                return url
        await asyncio.sleep(0.5)
    return ""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", required=True)
    args = parser.parse_args()

    repo = pathlib.Path(args.repo_root).resolve()
    desktop = repo / "desktop"
    electron = desktop / "node_modules" / "electron" / "dist" / "electron.exe"
    if not electron.exists():
        print(f"× 找不到 Electron：{electron}")
        return 1

    userdata = pathlib.Path(tempfile.mkdtemp(prefix="hne-datadir-dev-"))
    cfgdir = userdata / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    # ★ 刻意**不预置** config\.env：全新 profile 才会落到首次设置页（step=setup）。
    #   第一版预置了一份"本机文件"配置，结果应用判定"配置可用"直接进了控制台，
    #   向导根本没出现 —— 探针报的是"字段不存在"，看起来像功能坏了，其实是走错路径。
    print(f"临时 profile：{userdata}（不预置配置 → 走首次设置）")

    env = dict(__import__("os").environ)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    env["HNE_DESKTOP_PROFILE"] = str(userdata)
    env["HNE_DESKTOP_STRICT_CONFIG"] = "1"
    env["HNE_DESKTOP_DEBUG"] = "1"
    env["HNE_DESKTOP_PREFER_PYTHON"] = "1"
    for key in ("HNE_DB_BACKEND", "HNE_MYSQL_HOST", "HNE_MYSQL_PASSWORD",
                "HNE_SQLITE_PATH", "HNE_DATA_DIR", "HNE_CHROMA_PERSIST_DIR"):
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

            print("\n[1] 首次设置页里必须能看见「数据目录」这一项")
            url = await wait_setup(cdp, timeout=90)
            if not url:
                bad(f"向导里没有 #f-HNE_DATA_DIR（当前 {await cdp.run('location.href')}）")
                return 1
            ok(f"已进向导：{url}")

            # ★ 关键 1：它所在的 fieldset 必须**可见**。
            #   若被分进 MySQL 组，选默认「本机文件」时整组 display:none ——
            #   字段存在但用户永远看不到（这正是 dirOnly 要防的）。
            visible = await cdp.run("""
                (() => {
                  const el = document.getElementById('f-HNE_DATA_DIR');
                  if (!el) return 'no-field';
                  const fs = el.closest('fieldset');
                  if (!fs) return 'no-fieldset';
                  const cs = getComputedStyle(fs);
                  return JSON.stringify({
                    legend: (fs.querySelector('legend') || {}).textContent || '',
                    display: cs.display,
                    offsetParent: Boolean(el.offsetParent),
                    inMysqlGrid: Boolean(el.closest('#mysql-grid')),
                    inDataDirGrid: Boolean(el.closest('#datadir-grid'))
                  });
                })()
            """)
            ok(f"字段位置：{visible}")
            try:
                import json as _json
                info = _json.loads(str(visible))
            except Exception:  # noqa: BLE001
                bad(f"读不到字段位置信息：{visible}")
                return 1
            if info.get("inMysqlGrid"):
                bad("字段落进了 mysql-grid —— 选「本机文件」（默认）时会被整组隐藏")
            elif not info.get("inDataDirGrid"):
                bad("字段不在 datadir-grid 里（分组接错）")
            else:
                ok("字段在独立分组 datadir-grid 里（不被存储方式连累）")
            if info.get("display") == "none" or not info.get("offsetParent"):
                bad(f"字段所在分组不可见：{info}")
            else:
                ok(f"分组可见（legend={info.get('legend')!r}）")

            print("\n[2] 填一个数据目录并保存")
            target_dir = userdata / "my-own-data"
            await cdp.run(f"""
                (() => {{
                  const el = document.getElementById('f-HNE_DATA_DIR');
                  el.value = {str(target_dir)!r};
                  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                  return el.value;
                }})()
            """)
            filled = await cdp.run("document.getElementById('f-HNE_DATA_DIR').value")
            ok(f"输入框里的值：{filled}")

            saved = await cdp.run("""
                (async () => {
                  const btn = document.getElementById('btn-save');
                  btn.click();
                  return true;
                })()
            """, True)
            ok(f"已点「保存并开始」：{saved}")

            # 等配置落盘（保存会先自检/起后端，给足时间）
            env_file = cfgdir / ".env"
            written = None
            deadline = time.time() + 120
            while time.time() < deadline:
                vals = read_env(env_file)
                if vals.get("HNE_DATA_DIR"):
                    written = vals["HNE_DATA_DIR"]
                    break
                await asyncio.sleep(1)

            print("\n[3] 配置里必须真的写进了 HNE_DATA_DIR")
            if not written:
                bad(f".env 里没有 HNE_DATA_DIR（当前内容键：{sorted(read_env(env_file))}）")
            else:
                ok(f"已写入：HNE_DATA_DIR={written}")
                if pathlib.Path(written).resolve() == target_dir.resolve():
                    ok("写入的值与用户填的一致")
                else:
                    bad(f"写入的值与用户填的不一致：{written} != {target_dir}")

            print("\n[4] 「不会自动搬家」这句警告要进配置文件（用户以后只会看这个文件）")
            text = env_file.read_text(encoding="utf-8-sig") if env_file.is_file() else ""
            if "不会自动搬家" in text:
                ok("配置里有搬家警告")
            else:
                bad("配置里缺「不会自动搬家」的警告")

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    print("\n=== 结论 ===")
    if FAILED:
        for item in FAILED:
            print(f"  × {item}")
        return 2
    print("  全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
