"""给已有数据库补上新增的列（幂等、可预演、不删数据）。

==================== 为什么需要这个脚本？====================
`Base.metadata.create_all()` 只会**建缺失的表**，不会给已有表加列。
本项目刻意不引入 Alembic（依赖越少越好），所以"加一列"这件事
需要一个明确、可审查、可重复执行的入口 —— 就是这个脚本。

它只做一件事：**发现缺失的列就补上**。
绝不 DROP、绝不改类型、绝不删数据 —— 那是 `init_db.py --drop` 的事，
而那个命令在这台机器上是**红线**（库里有用户的真实故事）。

==================== ★ 两种后端，两条来源 ====================

| 后端 | 差异从哪来 | 说明 |
|---|---|---|
| **SQLite（默认）** | `app/db/schema_upgrade.py` **从 ORM 模型自动推导** | 模型是唯一事实来源；DDL 用 SQLite 方言编译（`init_db.py` 建表走同一套编译路径） |
| **MySQL（可选）** | 下面那张**手写 DDL 表**（`_MYSQL_EXPECTED`） | 历史原因：它比模型推导早，而且能顺手补外键与索引 |

★ 为什么 MySQL 侧还留着那份手写表：它包含**模型推导做不到**的东西 ——
  外键约束（SQLite/MySQL 都无法"给已有表加外键"而不重建表，但 MySQL 侧
  当年是靠 `ALTER TABLE … ADD CONSTRAINT` 补上的）以及给老库补索引。
  **两份都跑**（先手写、再模型推导兜底），后者还能顺带发现"手写表漏掉的列"。
  这不是重复劳动：手写表一旦抄漏，模型推导会把它抓出来并补上。

==================== 用法 ====================
    # 先看会执行什么 SQL（不连库改任何东西）
    .\\.venv\\Scripts\\python.exe scripts\\migrate_db.py --dry-run

    # 真正执行
    .\\.venv\\Scripts\\python.exe scripts\\migrate_db.py

    # 顺便把缺的表也建出来（等价于跑一次 init_db.py，不 --drop）
    .\\.venv\\Scripts\\python.exe scripts\\migrate_db.py --create-tables

    # 想操作 MySQL 实例就显式指定后端（默认是 sqlite）
    $env:HNE_DB_BACKEND='mysql'; .\\.venv\\Scripts\\python.exe scripts\\migrate_db.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.db.models  # noqa: E402,F401  —— 必须导入，否则 Base.metadata 里没有表定义
from app.db.mysql import dispose_engine, get_engine  # noqa: E402
from app.db.schema_upgrade import apply_upgrade, describe, plan_upgrade  # noqa: E402


def _missing_columns(inspector, table: str, expected: dict) -> list[tuple[str, str]]:
    """返回 [(列名, 建列语句片段)]，只包含当前库里**没有**的列。"""
    existing = {col["name"] for col in inspector.get_columns(table)}
    return [(name, ddl) for name, ddl in expected.items() if name not in existing]


def main() -> int:
    parser = argparse.ArgumentParser(description="幂等地补齐数据库新增的列")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要执行的 SQL")
    parser.add_argument("--create-tables", action="store_true", help="顺便建出缺失的表")
    parser.add_argument(
        "--model-driven",
        action="store_true",
        help="只跑「从 ORM 模型推导」的那条路（跳过 MySQL 手写 DDL 表）",
    )
    args = parser.parse_args()

    setup_logging()
    settings = get_settings()
    engine = get_engine()

    print("=" * 66)
    print(f"  数据库结构补齐  |  {settings.database_label}")
    print("=" * 66)

    if args.create_tables:
        # checkfirst=True：已存在的表原样跳过，不会动里面的数据
        Base.metadata.create_all(bind=engine)
        print("[OK] 缺失的表已创建（已存在的表未被改动）")

    statements: list[str] = []
    if not settings.is_sqlite and not args.model_driven:
        statements.extend(_plan_mysql_handwritten(engine))

    # ---- 第二条来源：从 ORM 模型自动推导（两种后端都跑）----
    plan = plan_upgrade(engine)
    print()
    for line in describe(plan):
        print(line)

    all_statements = statements + plan.statements
    if not all_statements and not plan.missing_tables:
        # ★ 这句话由 describe(plan) 输出（它也会说"已经是最新的"）——
        #   两处都打会变成重复两行，用户会以为跑了两遍。
        dispose_engine()
        return 0

    if args.dry_run:
        print("\n[预演] 未执行任何语句。")
        dispose_engine()
        return 0

    # MySQL 手写的那几条先跑（含外键/索引），再跑模型推导的那批
    executed = 0
    if statements:
        with engine.begin() as conn:
            for sql in statements:
                print(f"  [执行] {sql[:88]}…")
                conn.execute(text(sql))
                executed += 1
    executed += apply_upgrade(engine, plan)

    print(f"\n[完成] 共执行 {executed} 条语句，结构已补齐。"
          "建议再跑一次 --dry-run 确认没有剩余项。")
    dispose_engine()
    return 0


def _plan_mysql_handwritten(engine) -> list[str]:  # noqa: ANN001 - Engine
    """MySQL 侧的历史手写 DDL 表：只返回**该执行**的 `ALTER TABLE` 语句。

    ★ 为什么要保留这一份（而不是全交给模型推导）：
      这里包含模型推导**表达不了**的东西 —— `ADD CONSTRAINT … FOREIGN KEY`
      与配套索引。当年是靠它给老库补上 `prompt_preset_id` 的外键的。
    ★ 它**只对 MySQL 生效**：里面的 `MEDIUMTEXT` / `COMMENT` / `TINYINT(1)`
      SQLite 一条都不认（这正是"SQLite 用户没有升级手段"那个欠账的成因）。
    ★ 加新列时**只往这张表里追加**，不要改已有条目：已经跑过的库不会重复执行
      （下面按 inspector 的实际列判断），而改动历史条目等于伪造历史。
    """
    #: 表名 → {列名: DDL}。全部用 NULL 允许（或带默认值）的列：
    #: 加列时不需要回填已有行，所以线上加列是**瞬时且安全**的。
    expected: dict[str, dict[str, str]] = {
        "messages": {
            "state_meta_json": (
                "TEXT NULL COMMENT '状态遥测 JSON（是否输出状态块 / 校验分类计数 / 自报与落库偏离度）'"
            ),
            "rolls_json": (
                "TEXT NULL COMMENT '骰点 JSON 数组（骰子插件：表达式 / 点数 / 骰面）'"
            ),
            "translation_json": (
                "TEXT NULL COMMENT '翻译中间件结果 JSON（原文/译文 + 方向/模型/token）'"
            ),
            "state_raw_json": (
                "TEXT NULL COMMENT '模型输出的 <state> 原始文本（界面「看作者原格式」用）'"
            ),
            "usage_json": (
                "TEXT NULL COMMENT '本轮真实用量 JSON（厂商 prompt/completion/reasoning + 我们的输入估算）'"
            ),
        },
        "narrative_sessions": {
            "prompt_preset_id": (
                "BIGINT NULL COMMENT '本会话使用的提示词预设ID（空 = 用全局默认预设 / 内置装配）'"
            ),
            "state_json": (
                "MEDIUMTEXT NULL COMMENT "
                "'结构化状态 JSON（已校验：hp / inventory / location / quests / flags）'"
            ),
            "kind": (
                "VARCHAR(16) NOT NULL DEFAULT 'story' "
                "COMMENT '会话类型：story（叙事）/ chat（纯聊天，无角色）'"
            ),
            "state_schema_json": (
                "MEDIUMTEXT NULL COMMENT "
                "'状态栏字段定义 JSON（建会话时按 卡>世界书>initial_state 解析一次；"
                "空 schema = 该卡未定义状态栏）'"
            ),
            "summary_from_round": (
                "INT NULL COMMENT '剧情总结从第几轮开始覆盖（轮 = 一问一答）'"
            ),
            "summary_to_round": (
                "INT NULL COMMENT '剧情总结覆盖到第几轮（下次从这里继续）'"
            ),
            "summary_settings_json": (
                "MEDIUMTEXT NULL COMMENT '记忆总结设置 JSON（开关/自动/提醒/轮数/模式/提示词/上限/模型）'"
            ),
            "summary_history_json": (
                "MEDIUMTEXT NULL COMMENT '总结历史版本 JSON 数组（面板上的恢复上一次）'"
            ),
            "memory_anchors_json": (
                "MEDIUMTEXT NULL COMMENT '记忆锚点 JSON 数组（用户手写，固定注入、永不折叠）'"
            ),
            "translate_settings_json": (
                "MEDIUMTEXT NULL COMMENT '翻译中间件设置 JSON（开关/模式/方向/语言/模型）'"
            ),
        },
        "prompt_presets": {
            "is_builtin": (
                "TINYINT(1) NOT NULL DEFAULT 0 "
                "COMMENT '是否为内置守卫预设（可编辑、可删除、可还原）'"
            ),
        },
        "llm_providers": {
            "fallback_provider_id": (
                "BIGINT NULL COMMENT '备用模型配置ID（主模型失败且尚未输出内容时自动切换）'"
            ),
            "stream_enabled": (
                "TINYINT(1) NOT NULL DEFAULT 1 "
                "COMMENT '是否使用流式传输（默认开；关掉后等整段返回）'"
            ),
        },
        "users": {
            "builtin_preset_dismissed": (
                "TINYINT(1) NOT NULL DEFAULT 0 "
                "COMMENT '是否已删除内置守卫预设（删除后不再自动重建，直到点还原）'"
            ),
            "plugin_defaults_seeded": (
                "TINYINT(1) NOT NULL DEFAULT 0 "
                "COMMENT '是否已初始化默认插件（删掉后不再自动重建）'"
            ),
        },
    }

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    statements: list[str] = []
    for table, columns in expected.items():
        if table not in existing_tables:
            print(f"[跳过] 表 {table} 不存在（先跑 --create-tables 或 init_db.py）")
            continue
        for name, ddl in _missing_columns(inspector, table, columns):
            statements.append(f"ALTER TABLE `{table}` ADD COLUMN `{name}` {ddl}")
            # 外键也一并补上（ORM 定义里有，靠 create_all 是补不出来的）
            if (table, name) == ("narrative_sessions", "prompt_preset_id"):
                statements.append(
                    "ALTER TABLE `narrative_sessions` ADD CONSTRAINT "
                    "`fk_narrative_sessions_prompt_preset` FOREIGN KEY (`prompt_preset_id`) "
                    "REFERENCES `prompt_presets` (`id`) ON DELETE SET NULL"
                )
                # ★ 这条索引**刻意不在这里补**：
                #   模型推导那条路（app/db/schema_upgrade.py）按 inspector 的实际
                #   索引判断，发现缺了会自己发一条 CREATE INDEX。
                #   两处都发就会在"老 MySQL 库"上生成**两条一模一样的 CREATE INDEX**，
                #   第二条当场报 "Duplicate key name"（实测推演出来了，见
                #   app/db/schema_upgrade.apply_upgrade 的幂等处理与
                #   tests/test_schema_upgrade.py::test_apply_upgrade_skips_existing_index）。
                #   所以这里只留外键，索引交给那一处。
    return statements


if __name__ == "__main__":
    raise SystemExit(main())
