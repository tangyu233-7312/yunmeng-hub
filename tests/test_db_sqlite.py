"""SQLite 后端的专项测试（默认后端）。

==================== ★ 为什么需要这个文件 ====================
`tests/test_db.py` 跑的是"**当前配置的后端**"，而 `tests/conftest.py` 默认把它设成
sqlite —— 但那个文件里的大部分用例（连接池、级联删除、JSON 往返）是从 MySQL 时代
写下来的，它们验证的是"功能对不对"，而不是"SQLite 这条路本身是否被正确配置"。

SQLite 有几个**默认值与 MySQL 完全相反**的地方，不显式设置就会静默出错：

    · `PRAGMA foreign_keys` 默认 **OFF** → 级联删除**静默失效**（留下孤儿数据）
    · 默认 `journal_mode = delete` → 读写互相阻塞
    · 没有 `busy_timeout` → 并发写直接抛 "database is locked"

这些都不是"功能问题"，而是"配置问题"，所以必须由本文件专门盯着：
**每一个 PRAGMA 都对应一条断言**，谁把 `_configure_sqlite_connection` 改坏了，
这里会立刻红。

★ 本文件**不依赖** pytest 的默认后端设置：它用 fixture 显式把环境切成
  "sqlite + 临时目录"，跑完再切回去 —— 这样即使有人用
  `HNE_DB_BACKEND=mysql pytest` 跑全量，本文件依然测的是 SQLite。
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, text

from app.db.base import Base
from app.db.models import CharacterCard, LLMProvider, Message, NarrativeSession, User

# ==================================================================
#  夹具：把"当前后端"强制切成 sqlite + 临时数据目录
# ==================================================================
@pytest.fixture()
def sqlite_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """切到"sqlite + 临时目录"，并**重新自举**数据库。

    ★ 关键点：`get_settings` 是 `lru_cache` 单例、`app.db.mysql` 里的 Engine 也是
      模块级单例，所以改环境变量之后**必须把两者都清掉**，否则"我改了配置但它没生效"
      —— 这正是本项目反复强调要避免的静默失效。
    """
    import app.db.bootstrap as bootstrap
    import app.db.mysql as db_module
    from app.core.config import get_settings

    db_file = tmp_path / "data" / "app.sqlite3"
    monkeypatch.setenv("HNE_DB_BACKEND", "sqlite")
    monkeypatch.setenv("HNE_SQLITE_PATH", str(db_file))
    monkeypatch.setenv("HNE_DATA_DIR", str(tmp_path))

    get_settings.cache_clear()
    db_module.dispose_engine()
    yield db_file
    db_module.dispose_engine()
    get_settings.cache_clear()
    # 让 bootstrap 模块里对 settings 的引用也回到干净状态
    bootstrap.get_settings.cache_clear()


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ==================================================================
#  一、PRAGMA（SQLite 最容易"静默出错"的地方）
# ==================================================================
def test_foreign_keys_pragma_is_on(sqlite_env: Path) -> None:
    """★ 外键必须显式打开：SQLite 默认 **OFF**，级联删除会静默失效。

    这条断言是整份文件里最重要的一条。关掉它会怎样？
    删用户不会报错，但角色卡/会话/消息会**留在库里变成孤儿** ——
    没有任何异常、没有任何日志，只有数据越来越脏。
    """
    from app.db.mysql import get_engine

    with get_engine().connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_journal_mode_is_wal(sqlite_env: Path) -> None:
    """WAL：读不挡写、写不挡读（桌面应用要边流式输出边翻列表）。"""
    from app.db.mysql import get_engine

    with get_engine().connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"


def test_busy_timeout_matches_settings(sqlite_env: Path) -> None:
    """busy_timeout 必须按配置生效，否则并发写会随机抛 "database is locked"。"""
    from app.core.config import get_settings
    from app.db.mysql import get_engine

    with get_engine().connect() as conn:
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == (
            get_settings().SQLITE_BUSY_TIMEOUT_MS
        )


# ==================================================================
#  二、自动建表（"安装即用"的前提）
# ==================================================================
def test_ensure_schema_creates_every_table(sqlite_env: Path) -> None:
    """空库首启必须自动建出全部 8 张表，且返回的表名与模型一致。"""
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import get_engine

    created = ensure_schema()
    expected = set(Base.metadata.tables)
    assert set(created) == expected
    assert expected <= set(inspect(get_engine()).get_table_names())


def test_ensure_schema_is_idempotent(sqlite_env: Path) -> None:
    """★ 幂等：第二次调用**不新建任何表**，也绝不动已有数据。

    为什么要断言"返回空列表"而不是"没报错"：
      create_all 用 IF NOT EXISTS，"没报错"是必然的；
      真正要证明的是"它没有试图重建/清空"。
    """
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import session_scope

    ensure_schema()
    username = _unique("keep")
    with session_scope() as db:
        db.add(User(username=username, email="k@example.com", password_hash="x"))

    assert ensure_schema() == []

    with session_scope() as db:
        assert db.scalar(select(User).where(User.username == username)) is not None


def test_sqlite_file_is_created_under_configured_path(sqlite_env: Path) -> None:
    """库文件必须落在配置指定的位置（而不是别处悄悄建了一个）。"""
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import dispose_engine

    ensure_schema()
    dispose_engine()
    assert sqlite_env.is_file(), f"没有在 {sqlite_env} 建出库文件"
    # 而且它确实是个 SQLite 库（头 16 字节是固定魔数）
    assert sqlite_env.read_bytes()[:15] == b"SQLite format 3"


# ==================================================================
#  三、数据行为（与 MySQL 侧语义必须一致）
# ==================================================================
def test_autoincrement_primary_key_works(sqlite_env: Path) -> None:
    """★ 回归测试：SQLite 的 BIGINT 主键**不会**自增。

    本项目的主键在 MySQL 上是 `BIGINT AUTO_INCREMENT`；SQLite 只把
    `INTEGER PRIMARY KEY` 当自增别名，写成 BIGINT 就会报
    `NOT NULL constraint failed: users.id`。
    所以模型里用的是 `PkInt`（见 app/db/types.py）—— 这条断言守着它。
    """
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import session_scope

    ensure_schema()
    with session_scope() as db:
        user = User(username=_unique("pk"), email="pk@example.com", password_hash="x")
        db.add(user)
        # 不 flush 也能在 commit 后拿到 id；expire_on_commit=False 保证可读
    assert isinstance(user.id, int) and user.id > 0


def test_cascade_delete_removes_owned_rows(sqlite_env: Path) -> None:
    """删除用户 → 名下的模型配置与角色卡必须被级联删除（依赖 foreign_keys=ON）。"""
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import session_scope

    ensure_schema()
    username = _unique("cascade")
    with session_scope() as db:
        user = User(username=username, email="c@example.com", password_hash="x")
        db.add(user)
        db.flush()
        db.add(
            LLMProvider(
                user_id=user.id,
                name="级联测试模型",
                base_url="http://localhost:11434",
                api_key_encrypted="x",
                model_name="qwen2.5:7b",
            )
        )
        db.add(CharacterCard(user_id=user.id, name="级联测试角色", tags=["测试"]))
        user_id = user.id

    with session_scope() as db:
        db.delete(db.get(User, user_id))

    with session_scope() as db:
        assert db.scalar(select(LLMProvider).where(LLMProvider.user_id == user_id)) is None
        assert db.scalar(select(CharacterCard).where(CharacterCard.user_id == user_id)) is None


def test_json_columns_roundtrip(sqlite_env: Path) -> None:
    """JSON 列在 SQLite 上必须能原样往返（dict / list 两种形态）。

    ★ 这条曾经是失败的：给 JSON 列加 `with_variant` 之后，SQLAlchemy 会把
      MutableDict 的写回包装丢掉，插入 dict 直接抛
      `type 'dict' is not supported`。结论是 JSON 列**必须**用通用类型
      （详见 app/db/types.py 里那段"放弃 JsonText 变体"的记录）。
    """
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import session_scope

    ensure_schema()
    username = _unique("json")
    with session_scope() as db:
        user = User(username=username, email="j@example.com", password_hash="x")
        db.add(user)
        db.flush()
        db.add(
            LLMProvider(
                user_id=user.id,
                name="JSON 往返",
                base_url="http://localhost:1",
                api_key_encrypted="x",
                model_name="m",
                extra_params={"temperature": 0.85, "nested": {"a": [1, 2]}},
            )
        )
        provider_id = None

    with session_scope() as db:
        provider = db.scalar(select(LLMProvider).where(LLMProvider.name == "JSON 往返"))
        assert provider is not None
        assert provider.extra_params["temperature"] == 0.85
        assert provider.extra_params["nested"]["a"] == [1, 2]
        provider_id = provider.id
        # 原地修改必须被 MutableDict 感知并写回
        provider.extra_params["temperature"] = 0.5

    with session_scope() as db:
        again = db.get(LLMProvider, provider_id)
        assert again is not None
        assert again.extra_params["temperature"] == 0.5


def test_long_text_is_preserved(sqlite_env: Path) -> None:
    """长文本（原先的 MEDIUMTEXT 列）不能截断：SQLite 那边编译成 TEXT。"""
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import session_scope

    ensure_schema()
    long_text = "长" * 50_000
    username = _unique("long")
    with session_scope() as db:
        user = User(username=username, email="l@example.com", password_hash="x")
        db.add(user)
        db.flush()
        db.add(CharacterCard(user_id=user.id, name="长文本", greeting=long_text))

    with session_scope() as db:
        card = db.scalar(select(CharacterCard).where(CharacterCard.name == "长文本"))
        assert card is not None
        assert card.greeting == long_text


def test_timestamp_server_default_is_filled(sqlite_env: Path) -> None:
    """created_at 必须由数据库填值（server_default），不能是 NULL。"""
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import session_scope

    ensure_schema()
    with session_scope() as db:
        user = User(username=_unique("ts"), email="t@example.com", password_hash="x")
        db.add(user)
    assert user.created_at is not None
    assert user.updated_at is not None


def test_session_and_message_order(sqlite_env: Path) -> None:
    """会话消息按 id 升序（对话顺序），SQLite 上同样成立。"""
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import session_scope

    ensure_schema()
    with session_scope() as db:
        user = User(username=_unique("sess"), email="s@example.com", password_hash="x")
        db.add(user)
        db.flush()
        card = CharacterCard(user_id=user.id, name="林间旅人", tags=["奇幻"])
        db.add(card)
        db.flush()
        session = NarrativeSession(user_id=user.id, character_card_id=card.id, title="第一章")
        db.add(session)
        db.flush()
        for role, content in (("user", "我推开了木门。"), ("assistant", "门轴发出悠长的吱呀声……")):
            db.add(Message(session_id=session.id, role=role, content=content))
        session_id = session.id

    with session_scope() as db:
        row = db.get(NarrativeSession, session_id)
        assert row is not None
        assert [m.content for m in row.messages] == ["我推开了木门。", "门轴发出悠长的吱呀声……"]


# ==================================================================
#  四、探活接口的形状
# ==================================================================
def test_check_connection_reports_backend_and_server(sqlite_env: Path) -> None:
    """探活返回中性键名 `backend` / `server`（不再写死 mysql_version）。"""
    from app.db.bootstrap import ensure_schema
    from app.db.mysql import check_connection

    ensure_schema()
    status = check_connection()
    assert status["status"] == "ok"
    assert status["backend"] == "sqlite"
    assert status["server"].startswith("sqlite ")


def test_check_connection_reports_error_without_raising(sqlite_env: Path, tmp_path: Path, monkeypatch) -> None:
    """连不上时必须**如实返回错误**而不是抛异常（/health 依赖这个契约）。

    ★ 怎么可靠地制造"连不上"：把库文件路径指向一个**已存在的目录**。
      SQLite 打开时会报 "unable to open database file" ——
      这比"指向只读文件"可靠得多（Windows 上只读文件其实也拦不住 sqlite）。
      而"指向不存在的盘符"又会依赖具体机器，不能这么测。
    """
    from app.core.config import get_settings
    from app.db import mysql as db_module

    monkeypatch.setenv("HNE_SQLITE_PATH", str(tmp_path))
    get_settings.cache_clear()
    db_module.dispose_engine()

    status = db_module.check_connection()
    assert status["status"] == "error"
    assert status["backend"] == "sqlite"
    assert status["message"]


# ==================================================================
#  五、自举：自动生成密钥（用户不该被要求手填）
# ==================================================================
def _fernet_ok(value: str) -> bool:
    import base64

    try:
        return len(base64.urlsafe_b64decode(value.encode("ascii"))) == 32
    except Exception:  # noqa: BLE001 - 测试辅助，失败即 False
        return False


def test_ensure_secrets_generates_both_keys(tmp_path: Path, monkeypatch) -> None:
    """★ 一个密钥都没配时，自举必须自己生成两个可用密钥并落盘。"""
    import app.db.bootstrap as bootstrap
    from app.core.config import get_settings

    monkeypatch.setenv("HNE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HNE_SECRET_KEY", "CHANGE_ME_GENERATE_A_RANDOM_SECRET")
    monkeypatch.setenv("HNE_API_KEY_ENCRYPTION_KEY", "")
    get_settings.cache_clear()

    generated = bootstrap.ensure_secrets()
    assert set(generated) == {"HNE_SECRET_KEY", "HNE_API_KEY_ENCRYPTION_KEY"}

    settings = get_settings()
    assert len(settings.SECRET_KEY) >= 32
    assert _fernet_ok(settings.API_KEY_ENCRYPTION_KEY)

    path = bootstrap.secrets_file()
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    # ★ 口令文件里**绝不能**被写进仓库根（那会把密钥提交到公开仓库）
    assert str(tmp_path) in str(path)
    assert settings.API_KEY_ENCRYPTION_KEY in text

    get_settings.cache_clear()


def test_ensure_secrets_keeps_existing_valid_key(tmp_path: Path, monkeypatch) -> None:
    """★ 已存在且合法的加密密钥**绝不能被重新生成**。

    为什么这条最重要：换了 `API_KEY_ENCRYPTION_KEY` 之后，用户已经存进数据库的
    LLM API Key 密文将**永远解不开**（app/core/security.py 会报"密文无法解密"）。
    所以"只补缺失的"是硬约束，必须有测试守着。
    """
    import app.db.bootstrap as bootstrap
    from app.core.config import get_settings

    import base64

    existing = base64.urlsafe_b64encode(b"\x2a" * 32).decode("ascii")
    monkeypatch.setenv("HNE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HNE_SECRET_KEY", "a" * 40)
    monkeypatch.setenv("HNE_API_KEY_ENCRYPTION_KEY", existing)
    get_settings.cache_clear()

    assert bootstrap.ensure_secrets() == {}
    assert get_settings().API_KEY_ENCRYPTION_KEY == existing
    # 也没必要凭空造一个口令文件出来
    assert not bootstrap.secrets_file().exists()

    get_settings.cache_clear()


def test_ensure_secrets_reuses_persisted_values(tmp_path: Path, monkeypatch) -> None:
    """第二次启动必须**沿用**上次生成的口令（否则重启就等于换了密钥）。

    ★ 这个用例模拟的是"进程重启"：环境里不留上次注入的值，只靠落盘文件恢复。
      它同时守着两条不变量：
        · 已落盘的口令能被读回来（`parse_env_text` 解析正确）；
        · 第二次**不会再生成**（`ensure_secrets()` 返回空 = 什么都没补）。
    """
    import app.db.bootstrap as bootstrap
    from app.core.config import get_settings

    monkeypatch.setenv("HNE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HNE_SECRET_KEY", "")
    monkeypatch.setenv("HNE_API_KEY_ENCRYPTION_KEY", "")
    get_settings.cache_clear()

    bootstrap.ensure_secrets()
    bootstrap.ensure_secrets()

    # 落盘文件必须同时含两个键（"这次只补一个"不能把另一个丢掉）
    text = bootstrap.secrets_file().read_text(encoding="utf-8")
    assert "HNE_SECRET_KEY=" in text
    assert "HNE_API_KEY_ENCRYPTION_KEY=" in text

    # ★ 关键：把进程环境**清空**（模拟重启），只留落盘文件。
    #   注意必须真的删掉环境变量：只把 Settings 缓存清掉是不够的 ——
    #   上一次 `ensure_secrets` 已经把值写进 os.environ 了。
    for key in ("HNE_SECRET_KEY", "HNE_API_KEY_ENCRYPTION_KEY"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()

    second = bootstrap.ensure_secrets()
    assert second == {}, "重启后不应重新生成任何口令"
    assert get_settings().API_KEY_ENCRYPTION_KEY in text
    assert get_settings().SECRET_KEY in text

    get_settings.cache_clear()


def test_secrets_file_is_not_in_repo_root(tmp_path: Path, monkeypatch) -> None:
    """口令文件的位置由数据根目录决定，不会跑到仓库根去。"""
    import app.db.bootstrap as bootstrap
    from app.core.config import BASE_DIR

    monkeypatch.setenv("HNE_DATA_DIR", str(tmp_path))
    assert str(tmp_path) in str(bootstrap.secrets_file())
    assert BASE_DIR not in bootstrap.secrets_file().parents


def test_settings_must_be_refetched_after_bootstrap(tmp_path: Path, monkeypatch) -> None:
    """★★ 自举之后**必须重新取一次 Settings**，否则会打出一条自相矛盾的告警。

    ==================== 这条断言的真实来历（打包产物实测抓到）====================
    打包后的 `backend.exe` 首启日志里出现过这样一对相邻的行：

        已自动生成并保存本机密钥：HNE_API_KEY_ENCRYPTION_KEY、HNE_SECRET_KEY
        配置告警: API_KEY_ENCRYPTION_KEY 未设置，用户自配的 LLM API Key 无法加密存储

    前一行说"生成了"，后一行说"没设置"。两句话都不假 —— 因为
    `ensure_secrets()` 写的是 `os.environ` 并清缓存，而调用方**手里那个
    Settings 实例还是自举之前的**（它的值是空串）。

    危害不是"日志难看"：排障的人会顺着这行 WARNING 去查一个根本不存在的配置问题。
    所以这里把"重新取一次"这件事变成一条**可执行的断言**：
    自举后重新取的 Settings 必须能通过 `validate_runtime()`。
    """
    import app.db.bootstrap as bootstrap
    from app.core.config import get_settings

    monkeypatch.setenv("HNE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HNE_SECRET_KEY", "CHANGE_ME_GENERATE_A_RANDOM_SECRET")
    monkeypatch.setenv("HNE_API_KEY_ENCRYPTION_KEY", "")
    monkeypatch.setenv("HNE_DB_BACKEND", "sqlite")  # 免去 MYSQL_PASSWORD 那条告警
    get_settings.cache_clear()

    stale = get_settings()  # 模拟"自举之前就拿到、之后一直没换"的那个实例
    assert stale.API_KEY_ENCRYPTION_KEY == ""
    assert stale.SECRET_KEY in ("", "CHANGE_ME_GENERATE_A_RANDOM_SECRET")
    # 站在"自举前那份"上看，确实该报警（说明这条断言不是在否定告警机制本身）
    assert any("API_KEY_ENCRYPTION_KEY" in p for p in stale.validate_runtime())

    bootstrap.ensure_secrets()

    fresh = get_settings()  # ★ 自举之后重新取
    assert fresh is not stale, "必须换成一个新的 Settings 实例"
    assert fresh.validate_runtime() == [], (
        "自举之后重新取的 Settings 不该再有任何告警 —— "
        "否则启动日志会出现「已生成密钥」+「密钥未设置」这种自相矛盾的两行"
    )

    get_settings.cache_clear()


# ==================================================================
#  七、★★ 后端推断：旧配置必须留在 MySQL（实测事故的墓碑）
# ==================================================================
#: 子进程里跑的探针：按壳的方式把配置喂进环境，然后报出后端选了什么。
#  ★ 必须用**子进程**：这段逻辑读的是"原始输入里有没有某个键"，
#    而在同一个进程里改环境变量 / 反复实例化 Settings 都不可靠
#    （`get_settings` 是 lru_cache 单例、pydantic 也会缓存解析结果）。
_INFER_PROBE = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.environ["REPO"])
cfg = Path(os.environ["CFG"])
if cfg.is_file():
    for raw in cfg.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # 与桌面壳的行为一致：只补环境里还没有的键
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
from app.core.config import get_settings
print("RESULT" + json.dumps({"backend": get_settings().DB_BACKEND}))
"""


def _resolve_backend_with_config(tmp_path: Path, config_text: str) -> str:
    """在一个**干净的子进程**里，用给定的 .env 内容问后端会选哪个后端。"""
    import os
    import subprocess
    import sys

    cfg = tmp_path / ".env"
    cfg.write_text(config_text, encoding="utf-8")
    # ★ 必须把 HNE_*、以及**裸的 MYSQL_*/DB_*** 都丢掉：
    #   推断逻辑读的是**裸键名**（MYSQL_HOST/MYSQL_USER），
    #   宿主环境里残留的同名变量会把结论污染掉（真踩过，见 handoff §34）。
    env = {
        k: v for k, v in os.environ.items()
        if not k.upper().startswith(("HNE_", "MYSQL_", "DB_"))
    }
    env.update({
        "REPO": str(Path(__file__).resolve().parents[1]),
        "CFG": str(cfg),
        "HNE_ENV_FILE": str(cfg),
        "HNE_DATA_DIR": str(tmp_path / "root"),
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.run(
        [sys.executable, "-c", _INFER_PROBE], env=env, cwd=env["REPO"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    import json as _json

    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT"):
            return _json.loads(line[len("RESULT"):])["backend"]
    raise AssertionError(f"探针没有输出结论：stdout={proc.stdout!r} stderr={(proc.stderr or '')[-500:]!r}")


def test_legacy_mysql_config_without_backend_key_stays_mysql(tmp_path: Path) -> None:
    """★★★ 这条是**实测事故的墓碑**，别删。

    ==================== 事故经过（用户升级后当场撞到）====================
    默认后端从"只有 mysql"改成"默认 sqlite"之后：
      · 老版本向导写出来的 `.env` **没有** `HNE_DB_BACKEND` 这一行
        （那时只有一种后端，没必要写）；
      · 于是 pydantic 的字段默认值 `sqlite` 生效；
      · 用户装上新版一打开 → 被**静默**换到一份**空的 SQLite 库**上 →
        拿原来的账号登录 → 界面只回"用户名或密码错误"
        → 他的第一反应是"我的账号被人改了吗？"
      （数据其实一条没丢，全在 MySQL 里。）

    所以：**只要配置里写着 MySQL 连接信息、又没有显式选过后端，就必须留在 MySQL。**
    """
    legacy = (
        "HNE_MYSQL_HOST=127.0.0.1\n"
        "HNE_MYSQL_PORT=3306\n"
        "HNE_MYSQL_USER=narrative_app\n"
        "HNE_MYSQL_PASSWORD=fake-for-test\n"
        "HNE_MYSQL_DB=narrative_engine\n"
    )
    assert _resolve_backend_with_config(tmp_path, legacy) == "mysql", (
        "老配置被换到了另一个后端 —— 用户会看到'账号密码错误'，"
        "而他的数据其实还在 MySQL 里。请检查 Settings._infer_db_backend。"
    )


def test_explicit_backend_key_always_wins(tmp_path: Path) -> None:
    """显式写过 `HNE_DB_BACKEND` 就一律以它为准（两种方向都要验）。"""
    assert _resolve_backend_with_config(
        tmp_path, "HNE_DB_BACKEND=sqlite\nHNE_MYSQL_HOST=127.0.0.1\nHNE_MYSQL_USER=u\n"
    ) == "sqlite", "显式选 sqlite 时不该被 MySQL 键推断覆盖"
    assert _resolve_backend_with_config(
        tmp_path, "HNE_DB_BACKEND=mysql\n"
    ) == "mysql", "显式选 mysql 时即使没有 MySQL 键也要认"


# ==================================================================
#  八、跨方言 SQL（app/db/dialect.py）
# ==================================================================
def test_dialect_expressions_compile_for_both_backends() -> None:
    """两个后端各自编译出**正确**的 SQL：这是"一套代码两种数据库"的核心。

    ★ 为什么直接断言 SQL 字符串：这是一种"契约"，两个后端的差异只允许出现在
      这里（允许不同的函数名），而不允许出现在业务代码里。
      断言字符串能立刻暴露"有人改了一边忘了另一边"。
    """
    from sqlalchemy.dialects import mysql, sqlite as sqlite_dialect

    from app.db.dialect import json_array_contains, like_contains
    from app.db.models import CharacterCard

    pattern_cond = like_contains(CharacterCard.name, "50%")
    probe_cond = json_array_contains(CharacterCard.tags, "奇幻")

    sqlite_sql = str(pattern_cond.compile(dialect=sqlite_dialect.dialect()))
    mysql_sql = str(pattern_cond.compile(dialect=mysql.dialect()))
    assert "LIKE" in sqlite_sql and "ESCAPE" in sqlite_sql
    assert "LIKE" in mysql_sql and "ESCAPE" in mysql_sql
    # 通配符必须已经转义（否则 % 会被当成通配符，搜 "50%" 会命中所有含 50 的记录）
    assert "50!%" in pattern_cond.compile(
        dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}
    ).string

    assert "instr(" in str(probe_cond.compile(dialect=sqlite_dialect.dialect()))
    assert "JSON_CONTAINS(" in str(probe_cond.compile(dialect=mysql.dialect()))


def test_json_array_probe_uses_ascii_escaped_json() -> None:
    """★ 探针必须是 `\\uXXXX` 形态：列里存的就是这个形态（实测过）。

    第一版用原样中文去匹配，SQLite 上"筛选恒为 0 条"且不报任何错。
    """
    from app.db.dialect import JsonArrayContains

    cond = JsonArrayContains(None, __import__("json").dumps("奇幻"))
    assert cond.probe == '"\\u5947\\u5e7b"'


def test_sqlite_library_supports_json_functions(sqlite_env: Path) -> None:
    """打包/部署用的 sqlite3 必须带 JSON1（本项目依赖 json 列与 instr）。

    这条是"环境体检"：某些精简版 Python 会把 sqlite 编成不带 JSON1，
    那样标签筛选、世界书条目解析都会在运行时才炸。
    """
    con = sqlite3.connect(":memory:")
    try:
        assert con.execute("select json_valid('[]')").fetchone()[0] == 1
        assert con.execute("select instr('abc', 'b')").fetchone()[0] == 2
    finally:
        con.close()


def test_sqlite_file_path_is_not_shared_between_tests(sqlite_env: Path) -> None:
    """确认夹具真的把库放进了临时目录（否则测试会污染开发者的真实数据）。"""
    from app.core.config import get_settings

    assert sqlite_env.parent == Path(os.environ["HNE_DATA_DIR"]) / "data"
    assert get_settings().sqlite_file == sqlite_env
