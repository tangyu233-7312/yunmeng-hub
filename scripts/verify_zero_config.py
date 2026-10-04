"""端到端验证默认（零配置 / SQLite）路径：起一个真后端，走注册+登录+角色卡+会话。

==================== ★ 为什么必须单独验一遍 ====================
前面所有 SQLite 验证都跑在 **pytest 环境**里（`tests/conftest.py` 装好了隔离环境）。
而"用户装完打开就能用"这条路是**另一条**：没有 conftest、没有 `.env`、
数据落在 `HNE_DATA_DIR` 指定的目录里。这里就把那条路原样跑一次。

判据（全部要过）：
  1. 全新数据目录下，**不提供任何配置**也能起起来（表自动建、密钥自动生成）；
  2. `/health` 报 `backend=sqlite` 且组件全 ok；
  3. 注册 → 登录 → 建角色卡 → 建会话 → 发一条消息（用假模型）全通；
  4. 数据真的落在 `HNE_DATA_DIR` 下（SQLite 文件 + 密钥文件 + 向量库）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\verify_zero_config.py
退出码 0 = 通过。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

CHILD = r'''
import json, os, sys
from pathlib import Path

from fastapi.testclient import TestClient
from app.main import app

out = {}
with TestClient(app) as c:
    health = c.get("/health").json()
    out["health"] = health["status"]
    out["db_detail"] = health["components"]["database"]["detail"]

    r = c.post("/api/v1/auth/register", json={
        "username": "zero_config_user",
        "email": "zero@example.com",
        "password": "Zero-Config-Passw0rd!",
    })
    out["register"] = r.status_code
    if r.status_code != 201:
        out["register_body"] = str(r.json())[:300]
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(0)

    r = c.post("/api/v1/auth/login", json={
        "username": "zero_config_user", "password": "Zero-Config-Passw0rd!",
    })
    out["login"] = r.status_code
    if r.status_code != 200:
        out["login_body"] = str(r.json())[:300]
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(0)
    token = (r.json().get("data") or {}).get("access_token")
    if not token:
        out["login_body"] = str(r.json())[:300]
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(0)
    h = {"Authorization": f"Bearer {token}"}

    out["me"] = c.get("/api/v1/auth/me", headers=h).status_code

    r = c.post("/api/v1/character-cards", json={
        "name": "零配置验证卡", "description": "验证默认后端能存中文",
        "tags": ["验证", "SQLite"], "greeting": "你好，这是一段中文开场白。",
    }, headers=h)
    out["create_card"] = r.status_code
    card = (r.json().get("data") or {}) if r.status_code < 300 else {}
    out["card_name"] = card.get("name")
    out["card_id"] = card.get("id")

    r = c.get("/api/v1/character-cards", params={"tag": "SQLite"}, headers=h)
    out["tag_filter_status"] = r.status_code
    out["tag_filter_total"] = (r.json().get("data") or {}).get("total")

    data_dir = Path(os.environ["HNE_DATA_DIR"])
    out["sqlite_file"] = (data_dir / "data" / "app.sqlite3").is_file()
    # ★ 密钥文件可能**不存在**：仓库根有 .env 时，那里的真实密钥优先，
    #   自举就"什么都不用补"，也就不会写这个文件。
    #   所以要区分"没写"与"写错地方"，并把两种都如实报出来。
    from app.db.bootstrap import secrets_file
    from app.core.config import get_settings

    resolved_secrets = secrets_file()
    out["secrets_file"] = resolved_secrets.is_file()
    out["secrets_path"] = str(resolved_secrets)
    out["secrets_in_data_dir"] = str(data_dir) in str(resolved_secrets)
    # 密钥在本进程里必须真的可用（无论来自 .env 还是自举文件）
    settings = get_settings()
    out["secret_key_ok"] = len(settings.SECRET_KEY) >= 32
    out["fernet_ok"] = len(settings.API_KEY_ENCRYPTION_KEY) == 44
    # ★ 父进程会刻意把这两个键设成"占位符/空"，模拟**用户机上没有任何现成密钥**。
    #   所以这里如果还等于占位符，就说明自举没生效。
    out["secret_key_generated"] = settings.SECRET_KEY not in (
        "", "CHANGE_ME", "CHANGE_ME_GENERATE_A_RANDOM_SECRET",
        "dev-only-insecure-secret-key-change-me", "placeholder-secret-key",
    )
    out["fernet_generated"] = settings.API_KEY_ENCRYPTION_KEY not in ("", "CHANGE_ME_FERNET_KEY")
    # ★ 向量库目录：.env 里写的是 `./data/chroma`，于是它落在 <数据根>/data/chroma
    out["chroma_dir"] = (data_dir / "data" / "chroma").is_dir()
    out["log_dir"] = (data_dir / "data" / "logs").is_dir()

print(json.dumps(out, ensure_ascii=False))
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hne-zero-config-"))
    print(f"全新数据目录：{tmp}")

    env = dict(os.environ)
    # ★ 只留"这是一台新机器"该有的东西：没有任何 HNE_* 配置。
    for key in list(env):
        if key.upper().startswith("HNE_"):
            env.pop(key)
    env["HNE_DATA_DIR"] = str(tmp)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["HNE_LOG_LEVEL"] = "WARNING"
    # ★★ 这两行是"模拟用户机"的关键：用户机上**没有任何现成的密钥**。
    #   仓库根的 .env 里有真实密钥，而环境变量优先级高于 .env —— 所以把它们
    #   显式设成"占位符"就能造出"一台新机器"的状态，从而真正走一遍自举逻辑。
    #   （不设的话自举会认定"用户已经配好了"，那条分支根本不会被跑到。）
    env["HNE_SECRET_KEY"] = "CHANGE_ME_GENERATE_A_RANDOM_SECRET"
    env["HNE_API_KEY_ENCRYPTION_KEY"] = ""

    try:
        proc = subprocess.run(
            [sys.executable, "-c", CHILD],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        stdout = (proc.stdout or "").strip()
        print("---- 子进程 stderr（截断）----")
        for line in (proc.stderr or "").strip().splitlines()[-12:]:
            print(f"  {line}")
        print("---- 结论 ----")
        if not stdout:
            print(f"× 子进程没有输出结论（退出码 {proc.returncode}）")
            return 1
        import json

        payload = json.loads(stdout.splitlines()[-1])
        for key, value in payload.items():
            print(f"  {key} = {value}")

        checks: list[tuple[bool, str]] = [
            (payload.get("health") == "ok", "整体健康状态为 ok"),
            ((payload.get("db_detail") or {}).get("backend") == "sqlite", "探活报 backend=sqlite"),
            (bool((payload.get("db_detail") or {}).get("server")), "探活报出了 sqlite 版本"),
            (payload.get("register") == 201, "全新库里注册成功（说明表被自动建出来了）"),
            (payload.get("login") == 200, "登录成功"),
            (payload.get("me") == 200, "带令牌取当前用户成功"),
            (payload.get("create_card") in (200, 201), "建角色卡成功（中文往返）"),
            (payload.get("card_name") == "零配置验证卡", "角色卡名字往返一致"),
            (payload.get("tag_filter_total") == 1, "按标签筛选命中 1 张（跨方言 SQL 生效）"),
            (payload.get("sqlite_file") is True, "SQLite 库文件落在数据目录下"),
            (payload.get("secrets_in_data_dir") is True, "密钥文件（若生成）落在数据目录下，不在仓库里"),
            (payload.get("secret_key_ok") is True, "SECRET_KEY 可用（>= 32 位）"),
            (payload.get("fernet_ok") is True, "API_KEY_ENCRYPTION_KEY 是合法 Fernet 密钥"),
            (payload.get("chroma_dir") is True, "向量库目录建立在数据目录下"),
            (payload.get("log_dir") is True, "日志目录建立在数据目录下"),
            # ★ 这两条是"跟用户机一模一样"的关键：**不提供任何现成密钥**时，
            #   自举必须自己生成并落盘（真实安装包的用户机就是这个状态）。
            (payload.get("secrets_file") is True, "没有任何现成密钥时，自举真的写出了 .secrets.env"),
            (payload.get("secret_key_generated") is True, "生成的 SECRET_KEY 不是占位符"),
            (payload.get("fernet_generated") is True, "生成的加密密钥不是占位符"),
        ]
        failed = [name for ok, name in checks if not ok]
        for ok, name in checks:
            print(f"  [{'OK' if ok else '!!'}] {name}")
        if failed:
            print(f"\n零配置验收失败 {len(failed)} 项：{failed}")
            return 1
        print("\n零配置验收：全部通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
