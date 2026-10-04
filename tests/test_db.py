"""3.2 数据库集成测试。

这些测试会**真实连接 MySQL 并写入数据**（属于集成测试，不是纯单元测试）。
每个用例都使用随机用户名并在结束时清理，可以反复执行。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_db.py -v
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.db.base import Base
from app.db.models import CharacterCard, LLMProvider, Message, NarrativeSession, User
from app.db.mysql import get_engine, session_scope


def _unique(prefix: str) -> str:
    """生成随机用户名，避免多次运行测试时唯一约束冲突。"""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ==================== 表结构 ====================
def test_all_tables_registered() -> None:
    """确认 5 张表都已注册到 metadata（漏 import 模型是常见坑）。"""
    expected = {"users", "llm_providers", "character_cards", "narrative_sessions", "messages"}
    assert expected <= set(Base.metadata.tables)


# ==================== 连接池 ====================
def test_pool_parameters_match_settings() -> None:
    """确认连接池参数确实来自 .env 配置，而不是被悄悄忽略。

    ★ 只在 MySQL 后端下断言：这些参数（pool_size / max_overflow）是**网络数据库**
      才有的概念，SQLite 不走连接池的那套超时/回收逻辑（它的等价物是 PRAGMA，
      由 tests/test_db_sqlite.py 覆盖）。默认后端是 sqlite，所以本用例在默认
      配置下会被跳过 —— 这是有意的：断言本身没错，只是不适用于那个后端。
    """
    settings = get_settings()
    if settings.is_sqlite:
        pytest.skip("连接池参数只适用于 MySQL 后端（默认后端为 sqlite）")
    pool = get_engine().pool
    assert pool.size() == settings.DB_POOL_SIZE
    assert pool._max_overflow == settings.DB_MAX_OVERFLOW


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
def test_user_crud_and_chinese_roundtrip() -> None:
    """写入并读回中文，验证 utf8mb4 配置正确（不会出现乱码或 ??? ）。"""
    username = _unique("zh")
    nickname = "测试用户·中文昵称"

    with session_scope() as db:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="dummy-hash",
            nickname=nickname,
        )
        db.add(user)

    # 换一个新会话读取，确保数据真的落库了（而不是停留在内存里）
    with session_scope() as db:
        found = db.scalar(select(User).where(User.username == username))
        assert found is not None
        assert found.nickname == nickname          # 中文往返一致
        assert found.is_active is True             # server_default 生效
        assert found.created_at is not None        # server_default=func.now() 生效
        user_id = found.id

    # 清理
    with session_scope() as db:
        db.delete(db.get(User, user_id))


# ==================== JSON 字段与级联删除 ====================
def test_llm_provider_json_mutation_is_tracked() -> None:
    """验证 MutableDict 生效：原地修改 JSON 字段也能被写回数据库。"""
    username = _unique("provider")

    with session_scope() as db:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="dummy-hash",
        )
        db.add(user)
        db.flush()  # flush 让 user.id 立刻生成，但尚未提交事务

        provider = LLMProvider(
            user_id=user.id,
            name="我的DeepSeek",
            provider_type="openai_compatible",
            base_url="https://api.deepseek.com/v1",
            api_key_encrypted="encrypted-placeholder",
            model_name="deepseek-chat",
        )
        db.add(provider)

    # 原地修改 JSON 字段（不加 MutableDict 的话，这种改法不会生成 UPDATE 语句）
    with session_scope() as db:
        provider = db.scalar(select(LLMProvider).where(LLMProvider.user_id == user_id_of(db, username)))
        assert provider is not None
        provider.extra_params["temperature"] = 0.85
        provider_id = provider.id

    with session_scope() as db:
        provider = db.get(LLMProvider, provider_id)
        assert provider is not None
        assert provider.extra_params.get("temperature") == 0.85

    _delete_user(username)


def test_cascade_delete_removes_owned_rows() -> None:
    """删除用户时，其名下的模型配置与角色卡应被数据库级联删除。"""
    username = _unique("cascade")

    with session_scope() as db:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="dummy-hash",
        )
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

    # 用户没了，从属数据也应一并消失
    with session_scope() as db:
        assert db.scalar(select(LLMProvider).where(LLMProvider.user_id == user_id)) is None
        assert db.scalar(select(CharacterCard).where(CharacterCard.user_id == user_id)) is None


# ==================== 叙事会话与消息 ====================
def test_session_and_message_ordering() -> None:
    """验证消息按 id 升序返回，即正确的对话顺序。"""
    username = _unique("session")

    with session_scope() as db:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash="dummy-hash",
        )
        db.add(user)
        db.flush()

        card = CharacterCard(user_id=user.id, name="林间旅人", tags=["奇幻"])
        db.add(card)
        db.flush()

        session = NarrativeSession(
            user_id=user.id, character_card_id=card.id, title="第一章 · 森林入口"
        )
        db.add(session)
        db.flush()

        for role, content in [
            ("user", "我推开了木门。"),
            ("assistant", "门轴发出悠长的吱呀声……"),
            ("user", "我走了进去。"),
        ]:
            db.add(Message(session_id=session.id, role=role, content=content))

    with session_scope() as db:
        session_id = db.scalar(
            select(NarrativeSession.id).where(NarrativeSession.user_id == user_id_of(db, username))
        )
        assert session_id is not None

        # session.messages 由 relationship 的 order_by="Message.id" 保证顺序
        session = db.get(NarrativeSession, session_id)
        assert session is not None
        contents = [m.content for m in session.messages]
        assert contents == ["我推开了木门。", "门轴发出悠长的吱呀声……", "我走了进去。"]

        # 顺带验证 relationship 的升序配置确实生效
        ordered = db.scalars(
            select(Message).where(Message.session_id == session_id).order_by(Message.id)
        ).all()
        assert [m.role for m in ordered] == ["user", "assistant", "user"]

    _delete_user(username)


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
def user_id_of(db, username: str) -> int:
    """按用户名取用户ID，供后续查询使用。"""
    value = db.scalar(select(User.id).where(User.username == username))
    assert value is not None, f"测试用户 {username} 不存在"
    return value


def _delete_user(username: str) -> None:
    """清理测试数据（直接删除用户，从属数据由数据库级联删除）。"""
    with session_scope() as db:
        user = db.scalar(select(User).where(User.username == username))
        if user is not None:
            db.delete(user)
