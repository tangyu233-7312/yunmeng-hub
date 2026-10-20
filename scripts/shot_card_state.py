"""单独截一张「作者状态优先」的界面图（只为肉眼复核，不参与任何断言）。

★ 为什么单独写一个脚本而不是塞进探针：探针负责"断言"，截图属于"看一眼"。
  两者混在一起，将来改样式时要么为了看一眼跑十分钟全量探针，
  要么把断言注释掉 —— 都不好。

用法（服务要在 8000 跑着）：
    .\\.venv\\Scripts\\python.exe scripts/shot_card_state.py
产物：data/shot_card_state.png
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from websockets.sync.client import connect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from ui_probe import CDP_PORT, Cdp, EDGE_CANDIDATES  # noqa: E402

BASE = "http://127.0.0.1:8000"
OUT = PROJECT_ROOT / "data" / "shot_card_state.png"


def _seed() -> tuple[str, dict]:
    token = uuid.uuid4().hex[:8]
    account = {
        "username": f"shot_{token}",
        "email": f"shot_{token}@example.com",
        "password": "Test-Passw0rd!",
    }
    with httpx.Client(timeout=30) as c:
        c.post(f"{BASE}/api/v1/auth/register", json=account).raise_for_status()
        login = c.post(
            f"{BASE}/api/v1/auth/login",
            json={"username": account["username"], "password": account["password"]},
        )
        login.raise_for_status()
        access = login.json()["data"]["access_token"]
        h = {"Authorization": f"Bearer {access}"}
        provider = c.post(
            f"{BASE}/api/v1/providers",
            headers=h,
            json={
                "name": f"shot_{token}",
                "provider_type": "openai_compatible",
                "base_url": "https://mock.invalid/v1",
                "api_key": "sk-shot",
                "model_name": "mock-model",
                "context_window": 8192,
                "generation": {"temperature": 0.8, "max_tokens": 1024},
            },
        )
        provider.raise_for_status()
        card = c.post(
            f"{BASE}/api/v1/character-cards",
            headers=h,
            json={
                "name": "灯塔守夜人（作者状态）",
                "description": "开场白自带作者状态块",
                "greeting": "她把提灯挂在钩子上，回头看你。\n```json\n"
                '{"体力": 72, "心情": "警觉", "背包": ["提灯", "油壶"],'
                ' "当前位置": "灯塔三层"}\n```',
            },
        )
        card.raise_for_status()
        session = c.post(
            f"{BASE}/api/v1/narrative/sessions",
            headers=h,
            json={
                "character_card_id": card.json()["data"]["id"],
                "llm_provider_id": provider.json()["data"]["id"],
                "title": "作者状态截图会话",
            },
        )
        session.raise_for_status()
    return access, {"username": account["username"], "sid": session.json()["data"]["id"]}


def main() -> int:
    access, info = _seed()
    edge = next((p for p in EDGE_CANDIDATES if Path(p).exists()), None)
    if edge is None:
        print("找不到 Edge / Chrome")
        return 1
    profile = Path.home() / "AppData" / "Local" / "Temp" / "hne_shot"
    shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True, exist_ok=True)
    browser = subprocess.Popen(
        [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--window-size=1360,900",
            f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={profile}",
            f"{BASE}/console/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
        browser.kill()
        print("连不上调试端口")
        return 1
    try:
        with connect(ws_url, max_size=48 * 1024 * 1024) as ws:
            cdp = Cdp(ws)
            cdp.send("Runtime.enable")
            cdp.send("Page.enable")
            cdp.send(
                "Emulation.setDeviceMetricsOverride",
                width=1360, height=900, deviceScaleFactor=1, mobile=False,
            )
            cdp.eval(
                f"localStorage.setItem('hne_access_token', {json.dumps(access)});"
                f"localStorage.setItem('hne_user', {json.dumps(json.dumps({'username': info['username']}))});'ok'"
            )
            cdp.send("Page.navigate", url=f"{BASE}/console/")
            cdp.wait_for("!!document.querySelector('#view h1')", 25)
            cdp.eval("document.querySelector('#nav [data-route=\"chat\"]').click(); 'ok'")
            cdp.wait_for("!!document.querySelector('.session-item')", 20)
            time.sleep(1.2)
            cdp.eval(
                "(() => { const el = Array.from(document.querySelectorAll('.session-item'))"
                "  .find(x => (x.textContent || '').includes('作者状态截图会话'));"
                " if (!el) return 'no'; el.click(); return 'ok'; })()"
            )
            cdp.wait_for("!!document.querySelector('#card-state')", 20)
            time.sleep(1.2)
            shot = cdp.send(
                "Page.captureScreenshot", format="png", captureBeyondViewport=False
            )
            OUT.write_bytes(base64.b64decode(shot["data"]))
            print("已保存：", OUT)
    finally:
        browser.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
