"""结构升级：把 ORM 模型与**已有数据库**的差异算出来并补齐（只做加法）。

==================== 它解决什么问题 ====================
`Base.metadata.create_all()` 只会「建缺失的表」，**不会给已有表加列**。
在引入 SQLite 之前，加列这件事由 `scripts/migrate_db.py` 里那张手写的
MySQL DDL 表负责。但那份表有几个问题：

  · 它是**手写的**（列名、类型、注释都要人再抄一遍）—— 抄错一个类型，
    只有当某个用户的旧库真的缺这一列时才会暴露；
  · 它是**MySQL 专用**的（`MEDIUMTEXT` / `COMMENT` / `TINYINT(1)`），
    而 SQLite 一条都不认；
  · 于是"默认用 SQLite"这一版之后，SQLite 用户的旧库**没有任何升级手段**。
    这一条我在上一轮如实记成了欠账，本模块就是来还它的。

现在改成**从 ORM 模型自动推导**：模型是唯一事实来源，缺什么补什么，
两种后端用同一套逻辑（DDL 用各自方言编译）。

==================== ★ 三条硬约束（这是本模块存在的意义）====================

1. **只做加法，绝不破坏。**
   本模块只会生成 `CREATE TABLE` / `CREATE INDEX` / `ALTER TABLE … ADD COLUMN`。
   **永远不会**生成 `DROP` / 改类型 / 改可空性 / 删索引。
   有代码级断言守着这条（`_assert_only_additive`），因为"迁移脚本删了用户数据"
   是这类工具最不可原谅的失败方式 —— 库里有用户的真实故事。

2. **不安全的新列宁可不动，也不能猜。**
   SQLite 的 `ALTER TABLE ADD COLUMN` 不接受「NOT NULL 且无默认值」的列
   （已有行没法填）。这种情况本模块**跳过并说明原因**，而不是
   擅自给它编一个默认值 —— 编错了就是往用户数据里写错东西。

3. **删除类差异只报告，不执行。**
   模型里删掉的列/表在库里仍然存在。这是**故意**的：
   多一列不占多少空间，也不影响 ORM 读写；而自动删列是不可逆的。
   所以只在结果里列出来，让人自己决定。

==================== 与 Alembic 的关系（为什么不用它）====================
本项目刻意保持依赖最小（见 README 的取舍）。这个模块覆盖的是
"加列 / 加索引 / 加表"这三种**真实发生过**的变更形状，
而且只有 200 多行、能一眼读完、有测试。真要处理"改类型 / 拆表"这类
复杂迁移时再引入 Alembic 也不迟 —— 那时数据量级也完全不同了。

==================== 已知的、**不影响功能**的差异（如实记下）====================
用 `ADD COLUMN` 加出来的列会排在表**最后**，而新建库里它们按模型定义顺序排列。
SQLAlchemy 读写时用的是**显式列名**（`SELECT id, title, …`），不依赖列顺序，
所以新旧两种库在功能上完全一致 —— 只有 `PRAGMA table_info` / `DESCRIBE`
的输出顺序不同。本项目没有"按列位置取值"的代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.schema import CreateColumn, CreateIndex

from app.db.base import Base

#: 模型里存在、但库里没有的表，本模块**不主动建** —— 那是 `create_all` 的活。
#: （建表没有"改已有数据"的风险，交给 create_all 更简单，也不会出现两套建表逻辑。）
_MISSING_TABLE_OWNER = "Base.metadata.create_all(bind=engine)"


@dataclass
class SchemaPlan:
    """一次升级要做什么（**可打印、可预演、可测试**）。"""

    statements: list[str] = field(default_factory=list)
    #: 模型里有、库里没的表 —— 交给 create_all
    missing_tables: list[str] = field(default_factory=list)
    #: 想加但**不能安全地加**的列：(表, 列, 原因)
    skipped_columns: list[tuple[str, str, str]] = field(default_factory=list)
    #: 模型里已删、但库里还留着的列（只报告，不动）
    obsolete_columns: list[str] = field(default_factory=list)
    #: 模型里已删、但库里还留着的表（只报告，不动）
    obsolete_tables: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.statements and not self.missing_tables


def _is_constant_default(default_sql: str) -> bool:
    r"""这个 DEFAULT 片段是不是 SQLite 能用于 `ADD COLUMN` 的**常量**？

    ★ 这是实测撞出来的（`test_real_model_old_database_gets_upgraded_in_place` 抓到的）：
      `created_at` 这类列的 `server_default=func.now()` 编译出来是
      `DEFAULT CURRENT_TIMESTAMP`，而 SQLite 对 `ALTER TABLE ADD COLUMN` 的要求是：

          Cannot add a column with non-constant default

      也就是说**只有常量字面量**（数字 / 字符串 / 布尔 / NULL）能作为追加列的默认值。
      MySQL 侧没有这个限制，所以这个坑同样只在 SQLite 上暴露。

    ★ 注意**不能**用"SQLite 版本够不够新"来绕过：
      即便某些版本放开了常量表达式，`CURRENT_TIMESTAMP` 的语义是"这一列
      在**本语句执行时**取一个固定时间" —— 给几十万行旧数据填同一个时间戳，
      与"每行各自的创建时间"完全是两回事。那属于**猜数据**，本模块不做。
    """
    if not default_sql:
        return False
    upper = default_sql.upper()
    # 非法的信号：关键字 / 函数调用
    for word in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME", "NOW(", "UUID("):
        if word in upper:
            return False
    value = default_sql.strip().lstrip("DEFAULT").strip()
    if not value:
        return False
    # 常量：数字、带引号的字符串、布尔、NULL
    if value.upper() in ("NULL", "TRUE", "FALSE"):
        return True
    if value[0] in "'\"":
        return True
    try:
        float(value)
        return True
    except ValueError:
        return False


def _compile_default(column, dialect) -> str:
    """把列的默认值编译成可以内联进 DDL 的 SQL 片段（取不到/非常量就返回空串）。

    ★ 为什么要**内联字面量**而不是用绑定参数：
      `ALTER TABLE` 的 DDL 在 SQLite 上不接受绑定参数（会报 syntax error），
      而这里的内联值全部来自**模型定义**（不是用户输入），
      不存在注入面 —— 这一点必须在注释里说清楚，免得后人以为是漏洞。
    """
    server_default = getattr(column, "server_default", None)
    if server_default is None:
        return ""
    arg: Any = getattr(server_default, "arg", server_default)
    if arg is None:
        return ""
    compiled = ""
    try:
        import sqlalchemy as sa

        if isinstance(arg, str):
            compiled = str(
                sa.literal(arg).compile(dialect=dialect, compile_kwargs={"literal_binds": True})
            )
        else:
            compiled = str(arg.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    except Exception:  # noqa: BLE001 - 编译不了就当作"没有默认值"，交由安全性判断处理
        return ""
    if not compiled:
        return ""
    if not _is_constant_default(compiled):
        # ★ 非常量默认值**不能**写进 ADD COLUMN（SQLite 会拒），也不能假装没有它 ——
        #   返回空串会让"NOT NULL"的列被 `_can_add_safely` 判为不安全而跳过，
        #   这才是诚实的处理。见 `_is_constant_default` 的说明。
        return ""
    return f" DEFAULT {compiled}"


#: `CreateColumn` 会自己把 `server_default` 编译成 ` DEFAULT …`。
#: ★ 这一点是实测发现的（`created_at` 那一条）：
#:   `CreateColumn(Column('created_at', DateTime, server_default=func.now(), nullable=False))`
#:   编译出来是 `created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL` ——
#:   默认值**已经含在里面**了。所以：
#:     · 我们自己再拼一次 → 变成 `DEFAULT … DEFAULT …`（语法错误）；
#:     · 更麻烦的是，"默认值是否安全"这个判断会被它**绕过去**
#:       （我们以为没有默认值，于是放行了一条 SQLite 会拒绝的语句）。
#:   所以这里把它**摘掉**，由我们自己按 `_compile_default` 的规则重新拼 ——
#:   规则集中在一处，判断和执行用的是同一份事实。
_DEFAULT_MARKER = " DEFAULT "


def _column_ddl(column, dialect) -> str:  # noqa: ANN001 - Column, Dialect
    """生成 `ALTER TABLE … ADD COLUMN` 后面的那一段（类型 + **受控的**默认值）。

    ★ 为什么借 `CreateColumn` 而不是自己拼类型名：
      `VARCHAR(64)` / `INTEGER` / `TEXT` 这些类型名在不同方言下完全不同，
      自己拼就等于把 SQLAlchemy 的类型系统重写一遍。而 `CreateColumn`
      用的就是 **init_db.py 建表时那一套**编译路径 —— 所以升级出来的列
      与新建库里的列**逐字一致**（有测试盯着）。
    """
    ddl = str(CreateColumn(column).compile(dialect=dialect)).strip()
    # 摘掉 CreateColumn 自己带的默认值（理由见 _DEFAULT_MARKER 的注释）
    if _DEFAULT_MARKER in ddl:
        ddl = ddl.split(_DEFAULT_MARKER, 1)[0].strip()
    default = _compile_default(column, dialect)
    return f"{ddl}{default}"


def _can_add_safely(column, ddl: str) -> str | None:
    """能安全加吗？不能就返回**人话原因**。"""
    if column.primary_key:
        return "是主键：已有表无法追加主键列（需要重建整张表）"
    if column.computed is not None:
        return "是计算列：SQLite 不支持给已有表追加计算列"
    if not column.nullable and " DEFAULT " not in ddl:
        # ★ 这里同时覆盖两种情形，原因里都要说清，否则用户不知道该改什么：
        #   ① 模型本来就没给默认值；
        #   ② 给了，但那是**非常量**默认值（如 CURRENT_TIMESTAMP），
        #      SQLite 的 ADD COLUMN 不接受（"Cannot add a column with non-constant default"）。
        server_default = getattr(column, "server_default", None)
        if server_default is not None:
            return (
                "是 NOT NULL，且默认值不是常量（如 CURRENT_TIMESTAMP / NOW()）："
                "SQLite 不允许给已有表追加这种列 —— 而且真填进去也等于给所有旧行"
                "编同一个时间戳，属于猜数据。正确做法是先把它改成 nullable=True，"
                "或改用一个常量 server_default"
            )
        return ("是 NOT NULL 且没有默认值：已有行没法填，SQLite 会直接拒绝。"
                "正确做法是先把模型改成 nullable=True，或给它加 server_default")
    return None


def _assert_only_additive(statements: list[str]) -> None:
    """★ 硬门禁：任何一条语句里出现破坏性关键字就直接抛。

    这不是"应该不会发生"，而是"绝不允许发生" —— 库里有用户的真实数据，
    而迁移脚本是唯一能批量改结构的东西。宁可让升级失败，也不能静默删东西。
    """
    forbidden = ("DROP ", "TRUNCATE", "DELETE FROM", "ALTER COLUMN", "RENAME TO")
    for sql in statements:
        upper = sql.upper()
        for word in forbidden:
            if word in upper:
                raise AssertionError(
                    f"迁移计划里出现了破坏性语句（{word.strip()}）：{sql}\n"
                    "本模块只允许 CREATE TABLE / CREATE INDEX / ADD COLUMN。"
                )


def plan_upgrade(engine) -> SchemaPlan:  # noqa: ANN001 - Engine
    """算出"要做什么"，但**不执行任何语句**。"""
    dialect = engine.dialect
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    plan = SchemaPlan()

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            plan.missing_tables.append(table.name)
            continue

        existing_columns = inspector.get_columns(table.name)
        existing_names = {col["name"] for col in existing_columns}

        for column in table.columns:
            if column.name in existing_names:
                continue
            ddl = _column_ddl(column, dialect)
            reason = _can_add_safely(column, ddl)
            if reason:
                plan.skipped_columns.append((table.name, column.name, reason))
                continue
            plan.statements.append(
                f"ALTER TABLE {table.name} ADD COLUMN {ddl}"
            )

        # ---- 索引：模型里有、库里没有的补上（索引不影响数据，安全）----
        try:
            existing_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
        except Exception:  # noqa: BLE001 - 某些方言/视图可能不支持，如实降级
            existing_indexes = set()
        for index in table.indexes:
            if index.name and index.name not in existing_indexes:
                plan.statements.append(
                    str(CreateIndex(index).compile(dialect=dialect)).strip()
                )

        # ---- 库里多出来的列：只报告 ----
        model_names = {col.name for col in table.columns}
        for name in sorted(existing_names - model_names):
            plan.obsolete_columns.append(f"{table.name}.{name}")

    # ---- 库里多出来的表：只报告（模型里已删）----
    # ★ 为什么不自动删：删表是不可逆的，而多一张空表几乎不占空间。
    #   本项目最不能出的错就是"工具把用户数据删了"，所以这里只报告。
    model_tables = set(Base.metadata.tables)
    for name in sorted(existing_tables - model_tables):
        # SQLite 的内部表（sqlite_sequence 之类）不算"多余的"
        if name.startswith("sqlite_"):
            continue
        plan.obsolete_tables.append(name)

    _assert_only_additive(plan.statements)
    return plan


def apply_upgrade(engine, plan: SchemaPlan) -> int:  # noqa: ANN001 - Engine
    """执行计划，返回真正执行了的语句条数。

    ★ 先建缺失的表（`create_all`），再加列/索引：
      顺序反过来的话，给"引用了新表的外键列"加列会失败。

    ==================== ★ 为什么每条语句都要"先查再执行" ====================
    `scripts/migrate_db.py` 在 MySQL 下有**两条**语句来源：历史手写 DDL 表
    与模型推导。如果手写那条已经建了某个索引，模型推导再发一条同样的
    `CREATE INDEX` 就会报 `Duplicate key name`（实测推演出来了）——
    而这时前面几条已经执行成功，用户看到的是"升级失败"，
    重跑一次却又能过（因为第一条已经把索引建好了）。这种"重跑就好"的故障
    最容易被当成偶发，实际是**计划没有按当前状态判断**。

    所以这里对每条语句做一次存在性判断：
      · `CREATE INDEX`     → 索引名已存在就跳过
      · `ADD COLUMN`       → 列已存在就跳过
    其余（`CREATE TABLE` 走 create_all）不在计划里。
    """
    if plan.missing_tables:
        Base.metadata.create_all(bind=engine)
        logger.info("已创建缺失的表 {} 张：{}", len(plan.missing_tables), "、".join(plan.missing_tables))

    if not plan.statements:
        return 0

    inspector = inspect(engine)
    executed = 0
    skipped = 0
    with engine.begin() as conn:
        for sql in plan.statements:
            kind = _already_exists(inspector, sql)
            if kind:
                skipped += 1
                logger.debug("跳过已存在的对象（{}）：{}", kind, sql)
                continue
            conn.execute(text(sql))
            executed += 1
    if skipped:
        logger.info("有 {} 条语句因为对象已存在而跳过（不是失败）", skipped)
    return executed


def _already_exists(inspector, sql: str) -> str | None:  # noqa: ANN001 - Inspector
    """这条 DDL 要建的东西是不是已经存在了？返回对象种类（给日志用）。"""
    upper = sql.upper()

    if upper.startswith("CREATE INDEX"):
        # CREATE INDEX ix_name ON table (...) —— 取索引名（可能是 `带反引号` 或 "带引号"）
        try:
            name = sql.split("INDEX", 1)[1].strip().split()[0]
            name = name.strip('`"[]')
        except IndexError:  # pragma: no cover - 语句形状异常时不去猜
            return None
        for table in inspector.get_table_names():
            try:
                if name in {idx["name"] for idx in inspector.get_indexes(table)}:
                    return "索引"
            except Exception:  # noqa: BLE001 - 某些方言/视图不支持
                continue
        return None

    if upper.startswith("ALTER TABLE") and " ADD COLUMN " in upper:
        try:
            after_table = sql.split("TABLE", 1)[1]
            table = after_table.strip().split()[0].strip('`"[]')
            column = sql.split("ADD COLUMN", 1)[1].strip().split()[0].strip('`"[]')
        except IndexError:  # pragma: no cover
            return None
        if table in set(inspector.get_table_names()):
            if column in {col["name"] for col in inspector.get_columns(table)}:
                return "列"
        return None

    return None


def describe(plan: SchemaPlan) -> list[str]:
    """把计划整理成给终端看的一行行文本（脚本与日志共用，避免两处措辞不一致）。"""
    lines: list[str] = []
    if plan.missing_tables:
        lines.append(f"[表] 缺少 {len(plan.missing_tables)} 张表，将由 {_MISSING_TABLE_OWNER} 创建：")
        lines.extend(f"       + {name}" for name in plan.missing_tables)
    if plan.statements:
        lines.append(f"[列/索引] 要执行的语句 {len(plan.statements)} 条：")
        lines.extend(f"       {sql}" for sql in plan.statements)
    for table, column, reason in plan.skipped_columns:
        lines.append(f"[跳过] {table}.{column} —— {reason}")
    if plan.obsolete_columns:
        lines.append(f"[保留] 库里多出 {len(plan.obsolete_columns)} 列（模型里已删；本模块不删列）：")
        lines.extend(f"       · {name}" for name in plan.obsolete_columns)
    if plan.obsolete_tables:
        lines.append(f"[保留] 库里多出 {len(plan.obsolete_tables)} 张表（模型里已删；本模块不删表）：")
        lines.extend(f"       · {name}" for name in plan.obsolete_tables)
    if plan.is_empty and not plan.skipped_columns:
        lines.append("[OK] 结构已经是最新的，不需要改动。")
    return lines
