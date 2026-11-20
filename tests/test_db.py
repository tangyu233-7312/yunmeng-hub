"""3.2 数据库集成测试。

这些测试会**真实连接选定的数据库并写入数据**（属于集成测试，不是纯单元测试）。
每个用例都使用随机用户名并在结束时清理，可以反复执行。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_db.py -v
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.models import User
from app.db.mysql import session_scope


def _unique(prefix: str) -> str:
    """生成随机用户名，避免多次运行测试时唯一约束冲突。"""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ==================== 表结构 ====================


# ==================== 连接池 ====================


# ==================== 事务 ====================
def test_transaction_rolls_back_on_error() -> None:
    """session_scope 内抛异常时应整体回滚，不留半条脏数据。"""
    username = _unique("rollback")

    try:
        with session_scope() as db:
            db.add(
                User(
                    username=username,
                    email=f"{username}@example.com",
                    password_hash="dummy-hash",
                )
            )
            # 模拟业务中途失败
            raise RuntimeError("模拟业务异常")
    except RuntimeError:
        pass

    # 用新会话查询，确认这条记录没有被写进数据库
    with session_scope() as db:
        assert db.scalar(select(User).where(User.username == username)) is None


# ==================== 增删改查 + 中文编码 ====================


# ==================== JSON 字段与级联删除 ====================


# ==================== 叙事会话与消息 ====================


# ==================== 死锁回归测试 ====================
def test_session_scope_does_not_deadlock_before_engine_created() -> None:
    """★ 回归测试：Engine 尚未创建时，session_scope() 不得死锁。

    ==================== 曾经的 bug（自杀式死锁）====================
        get_session_factory() 先获取了 _init_lock，
        然后调用 get_engine()，而 get_engine() 又想获取**同一把锁**。
        threading.Lock 是**非重入**的 —— 同一个线程再次 acquire 会永久阻塞。

    ==================== 为什么它藏了这么久？====================
        Web 应用的 lifespan 在启动时会调用 check_connection() -> get_engine()，
        等请求进来时 Engine 早已创建，get_engine() 里的 `if _engine is None`
        为假，根本不会去碰锁 —— 问题被完整掩盖。
        测试同理（TestClient 的 lifespan 会预热 Engine）。

        只有「独立脚本里第一个动作就是 session_scope()」才会暴露，
        表现形式是**没有任何报错、进程静默卡死**，极难定位。

    ==================== 测试手法 ====================
        放在后台线程里执行并设置超时。
        如果直接在主线程调用，死锁时整个测试进程会卡住，
        而不是给出清晰的失败 —— 那就失去回归测试的意义了。
    """
    import threading

    from sqlalchemy import text

    import app.db.mysql as mysql_module

    # 确保 Engine 尚未创建，复现「冷启动」场景
    mysql_module.dispose_engine()

    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            with mysql_module.session_scope() as db:
                outcome["value"] = db.execute(text("SELECT 1")).scalar()
        except BaseException as exc:  # noqa: BLE001 - 需要把异常带回主线程断言
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=15)

    assert not thread.is_alive(), (
        "session_scope() 死锁了：get_session_factory() 在持有 _init_lock 的情况下，"
        "又调用了同样要获取该锁的 get_engine()。"
        "请检查 app/db/mysql.py，确保不在持锁状态下调用另一个加锁函数。"
    )
    assert "error" not in outcome, outcome.get("error")
    assert outcome.get("value") == 1


def test_get_db_dependency_does_not_deadlock() -> None:
    """get_db()（FastAPI 依赖）与 session_scope() 走同一条初始化路径，同样不能死锁。"""
    import threading

    import app.db.mysql as mysql_module

    mysql_module.dispose_engine()
    outcome: dict[str, object] = {}

    def worker() -> None:
        generator = mysql_module.get_db()
        try:
            db = next(generator)
            outcome["ok"] = db is not None
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            generator.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=15)

    assert not thread.is_alive(), "get_db() 死锁了"
    assert outcome.get("ok") is True, outcome.get("error")


# ==================== 测试辅助函数 ====================


pytestmark = pytest.mark.integration
