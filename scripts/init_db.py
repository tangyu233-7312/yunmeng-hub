"""数据库初始化脚本。

==================== 它做三件事 ====================
  1. 检查数据库能否连上（连不上会给出明确提示，而不是抛一堆堆栈）
  2. 按 ORM 模型建表（已存在的表会被跳过，可重复执行，是「幂等」的）
  3. 打印建好的表与字段，方便你肉眼核对

==================== 用法 ====================
在项目根目录执行：

    # 建表（最常用）
    .\\.venv\\Scripts\\python.exe scripts\\init_db.py

    # 只导出建表 SQL 到 scripts/schema.sql，方便放进论文附录或人工审查
    .\\.venv\\Scripts\\python.exe scripts\\init_db.py --dump-sql

    # 先删掉所有表再重建（★ 会清空数据，只在开发阶段用）
    .\\.venv\\Scripts\\python.exe scripts\\init_db.py --drop

==================== 为什么表结构以 ORM 为准？====================
SQL 脚本与 ORM 模型两处各写一遍，很容易「改了模型忘了改 SQL」而逐渐不一致。
所以这里让 SQL 由模型自动生成（--dump-sql），ORM 模型是唯一事实来源。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ---- 让脚本能 import 到项目里的 app 包 ----
# 直接运行 scripts/init_db.py 时，Python 的模块搜索路径里只有 scripts/ 目录，
# 找不到上一级的 app 包。这里手动把「项目根目录」加进搜索路径。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import inspect  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.mysql import check_connection, dispose_engine, get_engine  # noqa: E402

# ★ 必须导入模型包，否则 Base.metadata 里是空的，建表会「成功但一张表也没建」
import app.db.models  # noqa: E402, F401


def dump_sql() -> Path:
    """把所有表的建表语句导出到 scripts/schema.sql。"""
    # 使用 MySQL 方言编译，保证生成的 SQL 与线上实际建表语句一致
    from sqlalchemy.dialects import mysql

    dialect = mysql.dialect()
    statements: list[str] = [
        "-- 本文件由 scripts/init_db.py --dump-sql 自动生成，请勿手工修改。",
        "-- 表结构的唯一事实来源是 app/db/models/ 下的 ORM 模型。",
        "",
    ]

    # sorted_tables 已经按外键依赖排好序，因此建表顺序不会违反外键约束
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        statements.append(f"{ddl};")
        statements.append("")

    target = PROJECT_ROOT / "scripts" / "schema.sql"
    target.write_text("\n".join(statements), encoding="utf-8")
    return target


def print_table_summary() -> None:
    """打印数据库中每张表的字段，方便与预期核对。"""
    inspector = inspect(get_engine())
    table_names = inspector.get_table_names()

    if not table_names:
        print("  （数据库里没有任何表）")
        return

    for name in table_names:
        columns = inspector.get_columns(name)
        print(f"\n  [表] {name}  —— 共 {len(columns)} 个字段")
        for col in columns:
            # 标注出主键与自增，肉眼扫一眼就能看懂结构
            marks = []
            if col.get("primary_key"):
                marks.append("PK")
            if col.get("autoincrement"):
                marks.append("自增")
            if not col.get("nullable", True):
                marks.append("非空")
            suffix = f"  ({', '.join(marks)})" if marks else ""
            print(f"      - {col['name']:<32} {col['type']}{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化项目的 MySQL 数据库")
    parser.add_argument(
        "--dump-sql", action="store_true", help="仅导出建表 SQL 到 scripts/schema.sql"
    )
    parser.add_argument(
        "--drop", action="store_true", help="先删除所有表再重建（会清空数据！）"
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="跳过 --drop 的二次确认（用于脚本或自动化，请谨慎使用）",
    )
    args = parser.parse_args()

    settings = get_settings()

    # 让脚本和 Web 服务使用同一套日志格式（默认 loguru 格式会与项目风格不一致）
    setup_logging()

    if args.dump_sql:
        path = dump_sql()
        print(f"[OK] 建表 SQL 已导出：{path}")
        return 0

    print("=" * 64)
    print(f"  数据库初始化  |  {settings.MYSQL_USER}@{settings.MYSQL_HOST}:"
          f"{settings.MYSQL_PORT}/{settings.MYSQL_DB}")
    print("=" * 64)

    # ---- 步骤 1：连通性检查 ----
    status = check_connection()
    if status["status"] != "ok":
        print("\n[失败] 无法连接数据库：")
        print(f"       {status.get('message')}")
        print("\n请检查：")
        print("  1. MySQL 服务是否已启动（服务名 MySQL80）")
        print("  2. .env 里的 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD 是否正确")
        print("  3. 数据库 narrative_engine 是否已创建")
        dispose_engine()
        return 1
    print(f"\n[OK] 数据库连接正常，MySQL 版本 {status.get('mysql_version')}")

    engine = get_engine()

    # ---- 步骤 2：建表 ----
    if args.drop:
        # 二次确认：这一步会清空数据，误操作代价很高。
        # 加 -y 可跳过，便于脚本化调用。
        if not args.yes:
            confirm = input("\n!! --drop 会删除所有表及其数据，确认请输入 YES：")
            if confirm.strip() != "YES":
                print("已取消。")
                dispose_engine()
                return 0
        print("\n[..] 正在删除已有表 ...")
        Base.metadata.drop_all(bind=engine)

    print("\n[..] 正在创建表 ...")
    # create_all 默认 checkfirst=True：已存在的表会被跳过，因此可以反复执行
    Base.metadata.create_all(bind=engine)
    print("[OK] 建表完成")

    # ---- 步骤 3：结构核对 ----
    print("\n[..] 当前数据库结构：")
    print_table_summary()

    expected = {t.name for t in Base.metadata.sorted_tables}
    actual = set(inspect(engine).get_table_names())
    missing = expected - actual
    print()
    if missing:
        print(f"[警告] 以下表未创建成功：{sorted(missing)}")
        dispose_engine()
        return 1

    print(f"[完成] 共 {len(expected)} 张表全部就绪：{sorted(expected)}")
    print("\n提示：执行 `init_db.py --dump-sql` 可导出建表 SQL 以备审查。")

    dispose_engine()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
