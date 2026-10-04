"""数据库引擎与 Session 管理（**支持 SQLite / MySQL 两种后端**）。

==================== ★ 为什么文件名还叫 mysql.py ====================
这个模块原来只服务 MySQL，引入 SQLite 之后名字就"名不副实"了 ——
但**改名会波及 50 多处 import**（`app.api.*`、`scripts/*.py`、`tests/*.py`），
而改名本身对功能没有任何贡献，风险却全落在"漏改一处 → 运行时才炸"上。
所以这里保留文件名，在前面把话说清楚：**本模块是"数据库装配层"，
不是"MySQL 专用层"**，后端由 `HNE_DB_BACKEND` 决定（默认 sqlite）。

==================== 两种后端怎么选 ====================
    HNE_DB_BACKEND=sqlite（默认）  数据存成一个文件，无需安装任何服务，装完即用
    HNE_DB_BACKEND=mysql           连一个 MySQL 8 实例（给想用 MySQL 的用户）

★ 本项目的定位是**单机桌面应用**，所以默认 sqlite；但 MySQL 这条路径
  **必须一直可用**（多人共用的场景只能靠它），因此两侧都有测试守着：
  `tests/test_db.py` 跑当前后端、`tests/test_db_sqlite.py` 专门跑 sqlite 分支。

==================== 为什么需要连接池？ ====================
建立一条 MySQL 连接要经历 TCP 三次握手 + 身份认证，开销远大于一次普通查询。
连接池的做法是：预先保持若干条「长连接」，请求来了直接取一条用，用完还回去，
避免每个请求都重新建连。这是后端服务的标准做法。

==================== 关键参数说明（对应 .env 里的 DB_* 配置）====================
pool_size      常驻连接数，默认 10。即使空闲也保持这么多条，随取随用。
max_overflow   高峰期允许「临时额外」创建的连接数，默认 20。
               所以本项目的并发上限 = pool_size + max_overflow = 30 条。
pool_timeout   连接池被占满时，新请求最多等待多少秒，超时抛 TimeoutError。
pool_recycle   一条连接最长存活多少秒。MySQL 默认 wait_timeout=28800（8 小时）
               会主动掐断空闲连接，如果应用手里还攥着这条连接，就会报
               "MySQL server has gone away"。这里设为 3600（1 小时），提前换新连接。
pool_pre_ping  取用连接前先发一个轻量探测包，发现连接已失效就自动重建。
               ★ 这是避免「隔夜后第一个请求必然失败」的关键开关。

★ 这些参数**只对 MySQL 生效**：SQLite 不是网络服务，没有"连接超时/被服务端掐断"
  这类问题，它的关键参数是 PRAGMA（见 `_configure_sqlite_connection`）。

==================== 同步 or 异步？====================
本项目刻意选择「同步 SQLAlchemy + QueuePool」：
  * 代码直观、调试方便、事务边界清晰，答辩时容易讲清楚；
  * LLM 流式输出用 async def + httpx 实现，性能瓶颈不在数据库；
  * 需要读写数据库的接口请用 def（FastAPI 会自动放进线程池，不阻塞事件循环）。
详见 app/api/deps.py 顶部的说明。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import get_settings

# 模块级全局变量：整个进程只保留一个 Engine 和一个 Session 工厂
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None

# 加锁是为了线程安全：FastAPI 的多个线程可能同时第一次访问，避免重复创建
_init_lock = threading.Lock()


def _configure_sqlite_connection(dbapi_connection, _record) -> None:
    """每建立一条 SQLite 连接就设置一次 PRAGMA。

    ==================== ★ 为什么必须显式设置（三条都不是可有可无）====================

    1. **foreign_keys = ON** —— SQLite 默认**不启用**外键约束！
       而本项目的"删除用户 → 级联删除角色卡/会话/消息"完全依赖数据库级联
       （见 `tests/test_db.py::test_cascade_delete_removes_owned_rows`）。
       不打开它，删用户会**静默留下孤儿数据**：不报错、只是数据越积越脏。
       ★ 这是 SQLite 上最危险的一个默认值，必须在每条连接上打开。

    2. **journal_mode = WAL** —— 默认的 `delete` 模式下，读和写互相阻塞。
       WAL（预写日志）让"读不挡写、写不挡读"，正是桌面应用"一边流式输出
       一边有人翻会话列表"会遇到的并发形状。

    3. **busy_timeout** —— SQLite 是**单写者**：同一时刻只允许一个写事务。
       没有这个等待时间，第二个写者会**立刻**抛 `database is locked`，
       对用户表现为"随机某个操作报错"，极难复现。设成 10 秒后，
       正常场景下第二个写者等一会就成功了。

    另外把 `synchronous` 设为 NORMAL：WAL 模式下这是官方推荐值
    （FULL 太慢，OFF 会在断电时丢最近的事务）。
    """
    settings = get_settings()
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute(f"PRAGMA busy_timeout = {int(settings.SQLITE_BUSY_TIMEOUT_MS)}")
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


def _build_sqlite_engine() -> Engine:
    """SQLite 后端：一个文件就是一个数据库。"""
    settings = get_settings()
    db_path = settings.sqlite_file
    # 目录不存在时 sqlite3 会报 "unable to open database file"（而不是帮你建目录），
    # 所以这里先把父目录建出来 —— 首次启动、用户删过 data 目录都会走到这。
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        settings.database_url,
        echo=settings.DB_ECHO,
        # ★ check_same_thread=False：FastAPI 的同步接口跑在**线程池**里，
        #   同一条连接可能被不同线程使用。SQLAlchemy 的连接池本身保证了
        #   "一条连接同一时刻只被一个线程借用"，所以这里是安全的；
        #   不设它反而会在跨线程复用时直接抛 ProgrammingError。
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def _build_mysql_engine() -> Engine:
    """MySQL 后端：网络连接池版本。"""
    settings = get_settings()
    return create_engine(
        # 形如 mysql+pymysql://用户:密码@主机:端口/库名?charset=utf8mb4
        settings.database_url,
        # 是否把执行的 SQL 打印到日志，排查问题时可在 .env 里把 DB_ECHO 设为 true
        echo=settings.DB_ECHO,
        # 显式指定使用队列式连接池（SQLAlchemy 对 MySQL 的默认选择，这里写出来便于理解）
        poolclass=QueuePool,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        # 取连接前先探活，自动剔除已被 MySQL 断开的连接
        pool_pre_ping=True,
        connect_args={
            # 建立 TCP 连接的超时时间（秒），避免数据库不可达时长时间卡住
            "connect_timeout": 10,
        },
    )


def _build_engine() -> Engine:
    """按配置创建 Engine（连接池的真正载体）。"""
    settings = get_settings()
    if settings.is_sqlite:
        return _build_sqlite_engine()
    return _build_mysql_engine()


def get_engine() -> Engine:
    """获取全局唯一的 Engine（第一次调用时创建，之后直接复用）。"""
    global _engine
    if _engine is None:
        with _init_lock:
            # 双重检查：可能在等锁期间已被别的线程创建好了
            if _engine is None:
                _engine = _build_engine()
                settings = get_settings()
                if settings.is_sqlite:
                    logger.debug(
                        "SQLite Engine 已创建 | 文件={} | busy_timeout={}ms",
                        settings.sqlite_file,
                        settings.SQLITE_BUSY_TIMEOUT_MS,
                    )
                else:
                    logger.debug(
                        "MySQL Engine 已创建 | pool_size={} max_overflow={} recycle={}s",
                        settings.DB_POOL_SIZE,
                        settings.DB_MAX_OVERFLOW,
                        settings.DB_POOL_RECYCLE,
                    )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """获取全局唯一的 Session 工厂（Session 相当于「一次数据库会话」）。

    ==================== ★ 一个曾经踩过的坑：自杀式死锁 ====================
    下面这行 `engine = get_engine()` **必须写在 `with _init_lock` 之前**。

    原因：`get_engine()` 内部自己也会 `with _init_lock`，而
    `threading.Lock` 是**非重入**的 —— 同一个线程第二次 acquire 会永久阻塞。
    如果写成：

        with _init_lock:
            ... = sessionmaker(bind=get_engine(), ...)   # ❌ 死锁

    那么当 `session_scope()` / `get_db()` 是**第一个**访问数据库的动作时，
    就会卡死在这里：没有任何报错、没有任何日志，进程静默挂起。

    ==================== 为什么这个 bug 极难发现？====================
    Web 应用的 lifespan 启动时会先调用 `check_connection()`，那一步已经创建好
    Engine；等请求进来时 `get_engine()` 里的 `if _engine is None` 为假，
    根本不会去碰锁 —— 于是问题被**完整掩盖**。

    只有「独立脚本里第一个动作就是 `session_scope()`」才会触发，
    而独立脚本通常不是主流程，很容易被忽略。

    现在有回归测试守着这条不变量：
        tests/test_db.py::test_session_scope_does_not_deadlock_before_engine_created

    ★ 通用原则：**永远不要在持有锁的时候，去调用另一个会加同一把锁的函数。**
      需要用到那个函数的结果时，先把它取出来，再进锁。
    """
    global _session_factory
    if _session_factory is None:
        # 先在锁外拿到 Engine（这一步是幂等且线程安全的）
        engine = get_engine()

        with _init_lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=engine,
                    class_=Session,
                    # autoflush=False：查询前不自动把内存中的改动刷进数据库，
                    #                让「什么时候写库」完全由我们显式 commit 控制，行为可预期
                    autoflush=False,
                    # autocommit=False：关闭自动提交，必须显式 commit，这是事务安全的前提
                    autocommit=False,
                    # expire_on_commit=False：commit 之后对象属性仍然可读，
                    #                        否则 commit 会让对象过期，再访问属性会触发额外查询
                    expire_on_commit=False,
                )
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用的「每请求一个 Session」。

    使用方式（接口函数里）：

        from app.api.deps import DbSession

        @router.get("/xxx")
        def read_something(db: DbSession):
            ...

    生命周期：
      请求进来 -> 创建 Session -> 交给接口函数 -> 接口结束 -> finally 里关闭
    出现异常时自动回滚，避免把「写了一半的事务」留在连接池里。
    """
    db = get_session_factory()()
    try:
        yield db
    except Exception:
        # 接口抛异常：回滚本次事务，保证数据一致性
        db.rollback()
        raise
    finally:
        # 无论成功失败都要归还连接到池中，否则连接池很快会被耗尽
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """给「脚本 / 定时任务 / 非请求场景」使用的会话上下文。

    与 get_db 的区别：这里会自动 commit，用起来像 with 语句一样省心。

        with session_scope() as db:
            db.add(User(username="alice"))
        # 离开 with 块时自动提交
    """
    db = get_session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def server_version(conn) -> str:  # noqa: ANN001 - 只在这里用一次，不值得引入 DBAPIConnection 类型
    """取"服务端版本"这类信息，用于日志与 /health 展示。

    ★ 两种后端的取法完全不同，所以统一在这里分派：
        · SQLite 是嵌入式的，没有"服务端"。展示 `sqlite 3.x.y` 更有用
          （用户排查问题时能立刻知道底层库版本）。
        · MySQL 用 `SELECT VERSION()`。
    """
    if get_settings().is_sqlite:
        # exec_driver_sql：SQLite 的 sqlite_version() 是驱动内置函数，
        # 用原生 SQL 取最直接（不用碰 SQLAlchemy 的 DBAPI 连接包装层）
        return f"sqlite {conn.exec_driver_sql('select sqlite_version()').scalar_one()}"
    return str(conn.execute(text("SELECT VERSION()")).scalar_one())


def check_connection() -> dict[str, str]:
    """探测数据库是否可用，供启动自检与 /health 健康检查使用。

    返回示例：
        成功 {"status": "ok", "backend": "sqlite", "server": "sqlite 3.45.1"}
        成功 {"status": "ok", "backend": "mysql",  "server": "8.0.28"}
        失败 {"status": "error", "message": "OperationalError: ..."}

    ★ 这里**刻意不再返回 `mysql_version` 这个键**：它把"后端是 MySQL"写死进了
      接口形状，换成 SQLite 之后那个键名本身就是错的（健康检查是对外契约，
      键名不许说谎）。改成中性的 `backend` + `server`，/health 的调用方
      （前端、桌面壳验收脚本、tests/test_main.py）都按新形状断言。
    """
    backend = "sqlite" if get_settings().is_sqlite else "mysql"
    try:
        with get_engine().connect() as conn:
            version = server_version(conn)
        return {"status": "ok", "backend": backend, "server": version}
    except Exception as exc:  # noqa: BLE001 - 健康检查需要吞掉所有异常并如实返回
        return {"status": "error", "backend": backend, "message": f"{type(exc).__name__}: {exc}"}


def dispose_engine() -> None:
    """关闭连接池中的所有连接。应用退出时调用，属于「优雅关闭」的一部分。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
        logger.debug("数据库连接池已释放")
    _engine = None
    _session_factory = None
