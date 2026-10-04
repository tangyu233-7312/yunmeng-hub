"""开发期清场脚本：清理测试与演示产生的遗留数据。

==================== 用法 ====================
在项目根目录执行：

    .\\.venv\\Scripts\\python.exe scripts\\cleanup_demo_data.py

只会删除用户名以测试前缀开头的记录，不会碰真实数据。
用户被删除时，其名下的模型配置 / 角色卡 / 会话 / 消息
由数据库外键的 ON DELETE CASCADE 自动级联清理。

==================== 这个脚本曾经跑不起来 ====================
它一度会**静默卡死**（无报错、无日志、进程挂起），
根因在 app/db/mysql.py：`get_session_factory()` 持有 `_init_lock` 时
又调用了同样要获取该锁的 `get_engine()`，而 `threading.Lock` 不可重入
—— 自杀式死锁。

之所以一直没被发现：Web 服务的 lifespan 启动时会先创建 Engine，
等请求进来时已经不会再去碰锁，把问题完整掩盖了。
本脚本是「第一个动作就是 session_scope()」的少数场景之一，因此必然触发。

该 bug 已修复，并有回归测试守着：
    tests/test_db.py::test_session_scope_does_not_deadlock_before_engine_created

==================== 关于输出 flush ====================
所有输出都带 flush=True。原因：Windows 上 stdout 重定向到管道/文件时是块缓冲，
一旦脚本中途异常退出，缓冲区内容会全部丢失，排查时只能看到"毫无输出"，
会让人误以为是死锁。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select  # noqa: E402

from app.db.models import LLMProvider, User  # noqa: E402
from app.db.mysql import dispose_engine, session_scope  # noqa: E402

#: 测试与演示脚本使用的用户名**前缀**（不含通配符，下同）。
#: ★ 每新增一个测试文件就记得回来补一个前缀 —— 这份清单一旦过期，
#:   测试中断（比如夹具在 yield 之前就抛错）留下的用户就清理不到了。
#:   目前各前缀的来源：
#:     demo_  早期手工演示
#:     pv_    tests/test_providers.py
#:     cc_    tests/test_character_cards.py
#:     wb_    tests/test_world_books.py
#:     ui_    tests/test_console.py、scripts/ui_probe.py
#:     smoke_ scripts/smoke_test.py
#:     msg_   scripts/smoke_test.py「消息级操作」里造的临时账号
#:     mm_    tests/test_memory.py
#:     st_    tests/test_state.py
#:     rt_    tests/test_retrieval.py
#:     bk_    tests/test_benchmark.py（--from-db 回放用的账号）
#:     sm_    tests/test_summary.py
#:     an_    tests/test_anchors.py
#:     dr_    tests/test_state_drift.py
#:     dc_    tests/test_dice.py（骰子插件）
#:     vn_    tests/test_vn.py（角色卡 VN 立绘）
#:     tr_    tests/test_translate.py（翻译中间件）
#:     pp_    tests/test_retrieval.py 里"预设装配预览"那条（历史命名，现已并入 rt_）
#:     dbg_   临时调试脚本（脚本用完就删，但账号会留在库里）
#:     tmp_   临时验证脚本（与 dbg_ 同类；★ 之前漏了它，
#:            导致临时账号清理不掉 —— docs/handoff.md 里写的一直是"含 tmp_"）
#:     pa_ / user_  早期遗留命名
_TEST_PREFIXES = (
    "demo_",
    "pv_",
    "cc_",
    "wb_",
    "ui_",
    "smoke_",
    "msg_",
    "mm_",
    "st_",
    "rt_",
    "bk_",
    "sm_",
    "an_",
    "dr_",
    "dc_",
    "vn_",
    "tr_",
    "pp_",
    "dbg_",
    "tmp_",
    "pa_",
    "user_",
)


def _like_pattern(prefix: str) -> str:
    """把前缀转成安全的 LIKE 模式。

    ★★ 这里必须转义，否则会**误删真实用户**：
      SQL 的 LIKE 里 `_` 是「任意单个字符」的通配符，不是字面下划线。
      所以直接写 'ui_%' 实际匹配的是「ui + 任意一个字符 + 任意内容」，
      像 uin_foo、ui3_xxx 这种**正常用户名**也会被匹配到 ——
      而这个脚本是用来删数据的，误删代价很高。

      转义后 'ui\\_%' 才是「字面量 ui_ 开头」的意思。
      （MySQL 的 LIKE 默认用反斜杠当转义符，不需要额外写 ESCAPE 子句。）
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def log(message: str) -> None:
    print(message, flush=True)


def _counts(db) -> tuple[int, int]:
    users = db.scalar(select(func.count()).select_from(User)) or 0
    providers = db.scalar(select(func.count()).select_from(LLMProvider)) or 0
    return users, providers


def main() -> int:
    try:
        return _run()
    finally:
        # 脚本不像 Web 服务那样有 lifespan 负责收尾，
        # 必须显式释放连接池，否则连接会一直挂到进程退出。
        dispose_engine()
        log("[收尾] 数据库连接池已释放")


def _run() -> int:
    log("[1] 统计清理前的数据量")
    with session_scope() as db:
        before_users, before_providers = _counts(db)
    log(f"    users={before_users}  providers={before_providers}")

    log("[2] 查找测试遗留用户")
    # 用 _like_pattern 转义后再拼条件（直接把 'ui_%' 丢给 LIKE 会误伤真实用户名）
    condition = User.username.like(_like_pattern(_TEST_PREFIXES[0]))
    for prefix in _TEST_PREFIXES[1:]:
        condition = condition | User.username.like(_like_pattern(prefix))

    with session_scope() as db:
        names = list(db.scalars(select(User.username).where(condition)).all())
    log(f"    匹配到 {len(names)} 个: {names or '（无）'}")

    if not names:
        log("    没有需要清理的账号，跳过删除")
    else:
        log("[3] 逐个删除（每个用户一个独立事务，避免长时间持锁）")
        for index, username in enumerate(names, start=1):
            with session_scope() as db:
                row = db.scalar(select(User).where(User.username == username))
                if row is not None:
                    db.delete(row)
            log(f"    [{index}/{len(names)}] 已删除 {username}")

    log("[4] 复核")
    with session_scope() as db:
        after_users, after_providers = _counts(db)
    log(f"    users:     {before_users} -> {after_users}")
    log(f"    providers: {before_providers} -> {after_providers}")

    # [5] 顺带报告向量库里"已无人认领"的集合。
    #
    # ★ 为什么只报告、不自动删？
    #   删用户会连带清掉它名下的向量集合（memory.forget_all），但如果进程在
    #   两者之间被打断，集合就会留下，从此**没有任何代码路径能再访问它**
    #   （检索永远带 user_id 条件）—— 白占磁盘。
    #   但"删数据"这件事本身就是红线，所以这里只报告 + 给命令，让用户自己决定。
    #   判定条件刻意保守：**拥有者 ID 在 users 表里完全不存在**才算孤儿，
    #   绝不按用户名前缀猜（前缀猜错会误删真实用户的记忆）。
    _report_orphan_collections()

    log("[完成]")
    return 0


def _report_orphan_collections() -> None:
    try:
        from app.db.chroma import get_chroma_client
        from app.core.config import get_settings

        with session_scope() as db:
            live_ids = {int(uid) for uid in db.scalars(select(User.id)).all()}

        prefix = get_settings().CHROMA_COLLECTION_PREFIX
        pattern = re.compile(rf"^{re.escape(prefix)}_user_(\d+)$")
        client = get_chroma_client()
        orphans: list[str] = []
        for collection in client.list_collections():
            name = getattr(collection, "name", None) or str(collection)
            match = pattern.match(name)
            if match and int(match.group(1)) not in live_ids:
                orphans.append(name)
    except Exception as exc:  # noqa: BLE001 - 报告失败不该让清理脚本整体失败
        log(f"[5] 跳过向量库孤儿集合检查（{type(exc).__name__}: {exc}）")
        return

    if not orphans:
        log("[5] 向量库：没有无人认领的集合")
        return

    log(f"[5] 向量库：发现 {len(orphans)} 个无人认领的集合（拥有者账号已不存在，检索永远找不到它们）")
    for name in sorted(orphans)[:10]:
        log(f"      {name}")
    if len(orphans) > 10:
        log(f"      ……还有 {len(orphans) - 10} 个")
    log("      需要清掉的话执行：")
    log("      .\\.venv\\Scripts\\python.exe -c \"from app.db.chroma import get_chroma_client; c=get_chroma_client(); [c.delete_collection(n) for n in %r]\"" % (sorted(orphans),))


if __name__ == "__main__":
    raise SystemExit(main())
