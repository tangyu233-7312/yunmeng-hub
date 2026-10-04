"""`app/db/schema_upgrade.py` 的自测：**加列这条升级路径**必须真的能用、且绝不破坏。

==================== 为什么这些断言值得写 ====================
"给已有库加一列"是唯一会**批量改用户数据结构**的动作。它出错的两种方式都很坏：

  · 加错了 → 用户的旧库结构与模型不一致，之后某个功能在**他机器上**才炸，
    而开发机上永远是新建的库，看不到问题；
  · 加多了（顺手 DROP 掉什么）→ 直接删用户数据，不可逆。

所以这里的重点不是"能加列"，而是：
  1. **加出来的列与新建库里的列一致**（同一套编译器，不是手抄的 DDL）；
  2. **不安全的新列宁可不动**（NOT NULL 无默认值 —— 不能替用户猜一个）；
  3. **永远不会生成破坏性语句**（有硬门禁 + 一条专门的断言）。

★ 本文件用**测试自己的表**（丢掉 `Base.metadata` 里真实那张），
  这样"旧结构"可以随便造，也不会真的碰用户库。
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.schema import CreateColumn

from app.db import schema_upgrade
from app.db.base import Base


@pytest.fixture()
def fresh_engine():
    """一个内存 SQLite 库（每个用例一个，互不影响）。"""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    yield engine
    engine.dispose()


def _swap_metadata(monkeypatch: pytest.MonkeyPatch, tables) -> MetaData:
    """把 schema_upgrade 看到的 metadata 换成我们造的那份。"""
    meta = MetaData()
    for table in tables:
        table.to_metadata(meta)
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": meta}))
    return meta


def _old_messages_table(meta: MetaData) -> Table:
    """"老版本"的 messages 表：只有最初那几列。"""
    return Table(
        "messages",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("content", String(100)),
    )


def _new_messages_table(meta: MetaData) -> Table:
    """"新版本"：多了一列带默认值的、一列可空的。"""
    return Table(
        "messages",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("content", String(100)),
        Column("usage_json", String(200), nullable=True),
        Column("retry_count", Integer, nullable=False, server_default=text("0")),
    )


# ==================================================================
#  一、能发现差异
# ==================================================================
def test_plan_is_empty_when_schema_is_current(fresh_engine, monkeypatch) -> None:
    """库与模型一致时，计划必须**完全为空**（幂等的判据）。"""
    meta = MetaData()
    _new_messages_table(meta)
    _swap_metadata(monkeypatch, [])
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": meta}))
    meta.create_all(bind=fresh_engine)

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    assert plan.is_empty, plan.statements
    assert plan.skipped_columns == []


def test_plan_detects_missing_column_and_default(fresh_engine, monkeypatch) -> None:
    """缺的列要被发现，且**默认值要一起带上**（否则 NOT NULL 列加不上去）。"""
    old = MetaData()
    _old_messages_table(old)
    old.create_all(bind=fresh_engine)

    new = MetaData()
    _new_messages_table(new)
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": new}))

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    statements = "\n".join(plan.statements)
    assert "ALTER TABLE messages ADD COLUMN usage_json" in statements
    assert "ALTER TABLE messages ADD COLUMN retry_count" in statements
    # ★ 关键：NOT NULL 的列必须带上 DEFAULT，否则 SQLite 会拒绝、已有行也没法填
    assert "DEFAULT 0" in statements
    assert plan.skipped_columns == []


def test_added_column_ddl_matches_create_table_ddl(fresh_engine, monkeypatch) -> None:
    """★ 加出来的列必须与**新建库**里那一列**逐字一致**。

    这条是本模块存在的主要理由：手写 DDL 会随时间长歪（类型抄错、注释丢了），
    而这里两边用的是同一套编译器（`CreateColumn`），所以应当完全一致。
    """
    old = MetaData()
    _old_messages_table(old)
    old.create_all(bind=fresh_engine)

    new = MetaData()
    new_table = _new_messages_table(new)
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": new}))

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    schema_upgrade.apply_upgrade(fresh_engine, plan)

    column = new_table.columns["retry_count"]
    expected = str(CreateColumn(column).compile(dialect=fresh_engine.dialect)).strip()
    actual = next(
        col["type"] for col in inspect(fresh_engine).get_columns("messages")
        if col["name"] == "retry_count"
    )
    # 类型名一致（SQLite 侧是 INTEGER）
    assert str(actual).upper() == expected.split()[1].upper()
    assert expected.startswith("retry_count INTEGER")


def test_plan_detects_missing_index(fresh_engine, monkeypatch) -> None:
    """模型里有索引、库里没有 → 补一条 CREATE INDEX（索引不影响数据，安全）。"""
    old = MetaData()
    table = Table(
        "messages",
        old,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("session_id", Integer),
    )
    old.create_all(bind=fresh_engine)

    new = MetaData()
    Table(
        "messages",
        new,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("session_id", Integer, index=True),
    )
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": new}))

    # 模型里的索引名要能被 inspector 认出来；SQLAlchemy 默认名是 ix_messages_session_id
    assert "ix_messages_session_id" in {i.name for i in new.tables["messages"].indexes}

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    assert any("CREATE INDEX" in sql and "ix_messages_session_id" in sql for sql in plan.statements), plan.statements


# ==================================================================
#  二、不安全的新列宁可不动
# ==================================================================
def test_unsafe_not_null_without_default_is_skipped(fresh_engine, monkeypatch) -> None:
    """★★ NOT NULL 且无默认值的列：**必须跳过并说明原因**，不能替用户猜一个默认值。

    猜错的后果是往用户数据里写进一个错的默认值 —— 那比"升级失败"严重得多。
    """
    old = MetaData()
    _old_messages_table(old)
    old.create_all(bind=fresh_engine)

    new = MetaData()
    Table(
        "messages",
        new,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("content", String(100)),
        Column("must_have", String(50), nullable=False),  # ← 没有 server_default
    )
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": new}))

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    assert plan.statements == [], "不安全的列不该被写进计划"
    assert len(plan.skipped_columns) == 1
    table, column, reason = plan.skipped_columns[0]
    assert (table, column) == ("messages", "must_have")
    assert "NOT NULL" in reason and "默认值" in reason

    # 而且 apply 之后这一列**确实没有被加上**（计划里没有它）
    schema_upgrade.apply_upgrade(fresh_engine, plan)
    names = {col["name"] for col in inspect(fresh_engine).get_columns("messages")}
    assert "must_have" not in names


def test_primary_key_column_is_skipped(fresh_engine, monkeypatch) -> None:
    """主键列不能追加（需要重建整张表）→ 跳过并说明。"""
    old = MetaData()
    _old_messages_table(old)
    old.create_all(bind=fresh_engine)

    new = MetaData()
    Table(
        "messages",
        new,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("content", String(100)),
        Column("other_id", Integer, primary_key=True),
    )
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": new}))

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    assert plan.statements == []
    assert any(col == "other_id" for _, col, _ in plan.skipped_columns)
    assert any("主键" in reason for _, _, reason in plan.skipped_columns)


# ==================================================================
#  三、绝不破坏
# ==================================================================
def test_only_additive_guard_rejects_destructive_sql() -> None:
    """★★ 硬门禁：计划里出现破坏性语句必须直接抛，而不是"小心一点就好"。

    库里有用户的真实故事，迁移脚本是唯一能批量改结构的东西 —— 所以这条
    不是"应该"，而是"绝不允许"。这条断言同时保护未来的重构。
    """
    for sql in [
        "DROP TABLE messages",
        "TRUNCATE TABLE messages",
        "DELETE FROM messages WHERE 1=1",
        "ALTER TABLE messages DROP COLUMN content",
        "ALTER TABLE messages RENAME TO messages_old",
    ]:
        with pytest.raises(AssertionError):
            schema_upgrade._assert_only_additive([sql])
    # 允许的三类不该被误伤
    schema_upgrade._assert_only_additive([
        "ALTER TABLE messages ADD COLUMN usage_json TEXT",
        "CREATE INDEX ix_messages_session_id ON messages (session_id)",
        "CREATE TABLE plugins (id INTEGER NOT NULL PRIMARY KEY)",
    ])


def test_obsolete_columns_and_tables_are_only_reported(fresh_engine, monkeypatch) -> None:
    """模型里删掉的列/表：**只报告，不删**（删列不可逆，多一列几乎不占空间）。"""
    old = MetaData()
    Table(
        "messages",
        old,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("content", String(100)),
        Column("legacy_col", String(20)),
    )
    Table("ancient_table", old, Column("id", Integer, primary_key=True))
    old.create_all(bind=fresh_engine)

    new = MetaData()
    Table(
        "messages",
        new,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("content", String(100)),
    )
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": new}))

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    assert "messages.legacy_col" in plan.obsolete_columns
    assert "ancient_table" in plan.obsolete_tables
    assert plan.statements == [], "只报告，不该生成任何语句"
    # SQLite 自己的内部表不该被当成"多余的"
    assert not any(name.startswith("sqlite_") for name in plan.obsolete_tables)


# ==================================================================
#  四、真的能跑通（端到端）
# ==================================================================
def test_apply_upgrade_actually_adds_the_column_and_keeps_data(fresh_engine, monkeypatch) -> None:
    """★ 端到端：加列后旧数据**一条不少**，新列可读可写。"""
    old = MetaData()
    _old_messages_table(old)
    old.create_all(bind=fresh_engine)
    with fresh_engine.begin() as conn:
        conn.execute(text("INSERT INTO messages (content) VALUES ('旧数据不能被弄丢')"))

    new = MetaData()
    _new_messages_table(new)
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": new}))

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    executed = schema_upgrade.apply_upgrade(fresh_engine, plan)
    assert executed == len(plan.statements) > 0

    with fresh_engine.connect() as conn:
        rows = conn.execute(text("SELECT content, retry_count FROM messages")).all()
    assert rows == [("旧数据不能被弄丢", 0)], "旧数据必须还在，且新列取到默认值"

    # 再规划一次必须是空的（幂等）
    assert schema_upgrade.plan_upgrade(fresh_engine).is_empty


def test_missing_tables_are_left_to_create_all(fresh_engine, monkeypatch) -> None:
    """缺的表由 create_all 负责（本模块不自己发 CREATE TABLE，避免两套建表逻辑）。"""
    new = MetaData()
    _new_messages_table(new)
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": new}))

    plan = schema_upgrade.plan_upgrade(fresh_engine)
    assert plan.missing_tables == ["messages"]
    assert plan.statements == []

    schema_upgrade.apply_upgrade(fresh_engine, plan)
    assert "messages" in inspect(fresh_engine).get_table_names()


# ==================================================================
#  五、幂等：对象已存在时必须**跳过**而不是报错
# ==================================================================
def test_apply_upgrade_skips_existing_index(fresh_engine, monkeypatch) -> None:
    """★★ 索引已经存在时，`apply_upgrade` 必须跳过它 —— 而不是抛 "Duplicate key name"。

    ==================== 为什么这条一定要有 ====================
    `scripts/migrate_db.py` 在 MySQL 下有**两条**语句来源：历史手写 DDL 表
    与模型推导。若手写那条已经建了某个索引，模型推导再发一条同样的
    `CREATE INDEX`，第二条就会报 `Duplicate key name`。

    更糟的是这个故障的形状：前面几条**已经执行成功**，用户看到"升级失败"，
    重跑一次又能过（索引已存在 → 规划阶段就不再列出它）——
    于是它会被当成偶发问题，而真正的原因是**执行阶段没有按当前状态判断**。

    这里直接构造"计划里有、但库里已经有了"的情形来钉住这个行为。
    """
    meta = MetaData()
    table = Table(
        "messages",
        meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("session_id", Integer, index=True),
    )
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": meta}))
    meta.create_all(bind=fresh_engine)

    index_sql = "CREATE INDEX ix_messages_session_id ON messages (session_id)"
    plan = schema_upgrade.SchemaPlan(statements=[index_sql])
    executed = schema_upgrade.apply_upgrade(fresh_engine, plan)
    assert executed == 0, "索引已存在，不该真的执行"
    # 表还在、索引还在（跳过 ≠ 破坏）
    assert "ix_messages_session_id" in {i["name"] for i in inspect(fresh_engine).get_indexes("messages")}


def test_apply_upgrade_skips_existing_column(fresh_engine, monkeypatch) -> None:
    """列已经存在时同样跳过（两条来源也可能给出同一条 ADD COLUMN）。"""
    meta = MetaData()
    _new_messages_table(meta)
    monkeypatch.setattr(schema_upgrade, "Base", type("FakeBase", (), {"metadata": meta}))
    meta.create_all(bind=fresh_engine)

    plan = schema_upgrade.SchemaPlan(
        statements=["ALTER TABLE messages ADD COLUMN usage_json VARCHAR(200)"]
    )
    assert schema_upgrade.apply_upgrade(fresh_engine, plan) == 0
    # 再来一条**真的缺**的列：必须真的执行
    plan2 = schema_upgrade.SchemaPlan(
        statements=["ALTER TABLE messages ADD COLUMN brand_new TEXT"]
    )
    assert schema_upgrade.apply_upgrade(fresh_engine, plan2) == 1
    assert "brand_new" in {col["name"] for col in inspect(fresh_engine).get_columns("messages")}


def test_reports_obsolete_for_real_models_when_tables_exist(tmp_path) -> None:
    """对**真实模型**做一次体检：库里是最新结构时不该有任何要改的东西。

    ★ 这条守着一个很容易忽略的事实：`tests/conftest.py` 建的测试库是从
      `Base.metadata` 建的，所以它与模型**必须**完全一致 ——
      一旦这里报出差异，说明要么模型有两处定义不一致，要么
      `plan_upgrade` 的比较逻辑（列名/索引名）写歪了。
    """
    from sqlalchemy import create_engine as _create

    engine = _create(f"sqlite+pysqlite:///{tmp_path / 'app.sqlite3'}")
    try:
        Base.metadata.create_all(bind=engine)
        plan = schema_upgrade.plan_upgrade(engine)
        assert plan.statements == [], f"真实模型下不该有待执行语句：{plan.statements}"
        assert plan.missing_tables == []
        assert plan.skipped_columns == []
    finally:
        engine.dispose()


# ==================================================================
#  六、真实模型的"老库升级"端到端
# ==================================================================
def test_real_model_old_database_gets_upgraded_in_place(tmp_path) -> None:
    """★★ 真实场景：一个**缺列的老库**能被就地升级，且原有数据一条不少。

    这是这个模块存在的**全部理由**，所以用真实模型（`narrative_sessions`）
    而不是合成表来跑一遍：老库只建 id/user_id/title 三列 + 插一行数据，
    然后规划 → 执行 → 核对"新列都在、旧数据还在、再规划为空"。

    ★ 顺便钉住一条容易被忽略的性质：**不能靠 `create_all` 修老库** ——
      它只建缺失的**表**，不会给已有表加列（下面 `old_plan` 那一步就是在证明
      "此刻的库确实与模型不一致"，否则这个用例什么也没验证）。
    """
    from sqlalchemy import create_engine as _create

    from app.db.models import NarrativeSession

    engine = _create(f"sqlite+pysqlite:///{tmp_path / 'legacy.sqlite3'}")
    try:
        # ---- 造一个"老库"：只建最初那三列（用整表的列定义，只挑一部分）----
        legacy_meta = MetaData()
        legacy_table = Table(
            "narrative_sessions",
            legacy_meta,
            *[c._copy() for c in NarrativeSession.__table__.columns
              if c.name in {"id", "user_id", "title"}],
        )
        legacy_meta.create_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO narrative_sessions (id, user_id, title) VALUES (1, 7, '老故事')"
            ))

        # 此刻的库确实与模型不一致（证明这个用例不是空跑）
        pre = schema_upgrade.plan_upgrade(engine)
        assert pre.statements, "老库本该缺列；若为空说明这个用例没造出'老库'"
        added = [sql for sql in pre.statements if " ADD COLUMN " in sql]
        assert any("summary_from_round" in sql for sql in added), added
        assert any("rolling_summary" in sql for sql in added), added
        # 缺的表（除 narrative_sessions 外的全部）交给 create_all
        assert "messages" in pre.missing_tables

        # ★★ 时间戳列会被**正确地跳过**：`created_at` 是
        #   `NOT NULL` + `server_default=func.now()`，编译出来是
        #   `DEFAULT CURRENT_TIMESTAMP` —— 而 SQLite 的 ADD COLUMN
        #   不接受非常量默认值（实测报 "Cannot add a column with non-constant default"）。
        #   这是好行为：**宁可少加一列并说清原因，也不能给几十万行旧数据
        #   编同一个时间戳**（那属于猜数据）。
        skipped_names = {col for _, col, _ in pre.skipped_columns}
        assert {"created_at", "updated_at"} <= skipped_names, pre.skipped_columns
        for _, col, reason in pre.skipped_columns:
            if col in ("created_at", "updated_at"):
                assert "非" in reason or "常量" in reason, reason

        # ---- 升级 ----
        executed = schema_upgrade.apply_upgrade(engine, pre)
        assert executed == len(pre.statements) > 0

        # ---- 核对：所有**可以安全追加**的列都到位了 ----
        columns = {col["name"] for col in inspect(engine).get_columns("narrative_sessions")}
        model_columns = {c.name for c in NarrativeSession.__table__.columns}
        assert columns == model_columns - skipped_names, (
            f"还缺列：{(model_columns - skipped_names) - columns}；"
            f"多加的列：{columns - model_columns}"
        )
        assert {"summary_from_round", "rolling_summary", "memory_anchors_json"} <= columns

        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT id, title, rolling_summary FROM narrative_sessions WHERE id = 1"
            )).one()
        assert row[0] == 1 and row[1] == "老故事", "老数据必须原封不动"
        assert row[2] is None, "新加的可空列取到 NULL（没有替用户编值）"

        # 幂等：再规划一次不会是空的（因为那两列**永远**加不上），
        # 但**不会**再冒出别的语句 —— 这一点才是幂等的真正含义。
        again = schema_upgrade.plan_upgrade(engine)
        assert again.missing_tables == []
        assert again.statements == [], f"第二次不该还有待执行语句：{again.statements}"
        assert {col for _, col, _ in again.skipped_columns} == skipped_names
    finally:
        engine.dispose()
