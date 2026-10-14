"""守 `ensure_schema()` 的一个**静默失效**：规划前必须自己保证模型已注册。

实测踩到的（2026-10-13）：直接调 `ensure_schema()`（调用方没导入过模型包）时，
`Base.metadata` 里没有那些表 → `plan_upgrade` 规划出**空计划**、一条列都不补，
而且**不报错、不告警** —— 用户升级后会在"点开某个功能"时才看到 `no such column`。
真实启动路径（app.main → router → models）顺带导入过模型，所以从没暴露。

这里用**子进程**真跑一遍：不导入 app 的情况下对一个"缺列的 SQLite 库"调 ensure_schema，
断言列真的补上了。只看源码字符串是证明不了这件事的。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_ensure_schema_works_without_importing_the_app(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite3"
    # ① 先用模型建一个"完整"的库，再把新列删掉，模拟老库
    env = {
        **os.environ,
        "HNE_DB_BACKEND": "sqlite",
        "HNE_SQLITE_PATH": str(db),
        "HNE_DATA_DIR": str(tmp_path),
    }
    subprocess.run(
        [sys.executable, "-c", "import app.main; from app.db.mysql import get_engine; "
         "from app.db.base import Base; Base.metadata.create_all(get_engine())"],
        cwd=REPO, env=env, check=True, capture_output=True,
        # ★ 同样显式指定 UTF-8：理由见下面第 ② 步的注释（GBK 解不了中文日志）。
        encoding="utf-8", errors="replace", timeout=180,
    )
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE llm_providers DROP COLUMN store_reasoning")
    con.commit()
    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_providers)")}
    con.close()
    assert "store_reasoning" not in cols, "测试前提：老库里应当没有这一列"

    # ② 关键：子进程里**只**调 ensure_schema，不导入 app.main
    #
    # ★★ `encoding="utf-8"` + `errors="replace"` 是必须的，别再写 `text=True`：
    #   `text=True` 会用**系统默认编码**解码子进程输出，而 Windows 上是 **GBK** ——
    #   子进程的日志里有中文（UTF-8 字节），GBK 解不了 ⇒ 读取线程
    #   `_readerthread` 抛 UnicodeDecodeError，`proc.stderr` 直接变成 **None**。
    #   后果比"多一条告警"严重：下面第 49 行的 `proc.stderr[-2000:]` 会在
    #   **测试真的失败时**抛 `TypeError: 'NoneType' object is not subscriptable`，
    #   把真正的错误信息**吃掉**，排查方向被彻底带偏。
    #   （实测复现：`pytest -W error::PytestUnhandledThreadExceptionWarning` 必红。）
    proc = subprocess.run(
        [sys.executable, "-c", "from app.db.bootstrap import ensure_schema; ensure_schema()"],
        cwd=REPO, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert proc.returncode == 0, (proc.stderr or "")[-2000:]

    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(llm_providers)")}
    con.close()
    assert "store_reasoning" in cols, (
        "ensure_schema 必须能在没有导入 app 的情况下补齐新列 —— "
        "否则它会静默什么都不做（这正是它自己 import 模型包要防的事）"
    )
