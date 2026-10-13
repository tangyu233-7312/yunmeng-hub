"""正在进行的流式生成：进程内的登记表（谁在写、要不要停）。

==================== ★ 这个模块要解决的**真实故障** ====================
用户 2026-10-13 的反馈原话：

    "当用户输入后，在 AI 输出的过程中切换到其他角色卡或者世界线会直接打断 ai 的生成，
     再回来就会发现用户输入孤零零的摆在那里。但是我敢肯定，上游一定已经收到这个请求
     并且输出了，但是我的系统却没有返回到用户的对话框里，只有再消耗 token 点
     「让 AI 写这个走向」。"

根因（读代码 + 真实断线复现得出）：

    `_sse_stream_response.event_stream` 里只有**包住 `yield`** 的那一层 try/except 会走
    `_close_generator`（→ 触发生成器 finally 里的收尾、把已生成的部分落库）。
    而切会话 / 切世界线时，请求被取消的位置**多半是 `await run_in_threadpool(...)`
    那一行**（模型正在思考、还没吐下一个字），**那个 await 不在 try 里**。
    于是生成器根本没被关闭：

      · 生成器的 `except BaseException` → `_keep_partial` 不会跑 → 用户那句话后面
        **什么都没有**，这一轮彻底丢失（用户看到的就是"孤零零一句话"）；
      · 上游 HTTP 连接一直悬着（直到被 GC）；
      · 线程池里那个 `next()` 还在往下跑，但它算出来的东西没人接。

==================== ★ 最终语义（用户 2026-10-13 拍板）====================
    "也可以切换别的也中止而不继续生成，但是**一定要告诉用户被中断**，
     而不是像我刚刚那样什么都没有。"

    所以：**断开 = 中止这一轮**（上游一起停，没生成的 token 不用付），
    但**已经写出来的字必须如实存下来**，并标注「（回复中断）」——
    用户切回来看到的永远是"你的话 + 已经写出来的那段 + 中断标注"，
    绝不是空白；如果当时一个字都还没吐出来，界面会给出
    「让 AI 写这条走向」的重试入口（前端 `pendingReplyHTML`）。

    ★ 为什么**不**做成"切走也继续在后台写完"（我试过，退回来了）：
      那需要把同一个 SQLAlchemy Session 与 ORM 对象跨"请求任务 / 线程池 / 后台线程"
      三处共享，还要跟"生成器被 GC 终结"抢时序。真实断线复现里连撞两次：
      先是 `generator already executing`，再是
      `ObjectDereferencedError: ... has been garbage collected` ——
      修一层冒一层。而"中止 + 保住已生成的部分"同样不让用户白花钱
      （没生成的部分本来也不用付），却把这一整类竞态直接消掉。

==================== 为什么用进程内注册表就够 ====================
    这个项目是单进程部署（桌面版把后端当 sidecar 起一个进程）。
    所以"哪个会话正在生成"用一个 `dict` 就够；将来要多进程再换 Redis，
    不影响上面的语义。
"""

from __future__ import annotations

import threading
from typing import Any

from loguru import logger

#: 一个会话同时只允许一轮生成。两轮同时写同一个会话会互相插队，
#: 历史里出现"两条回复针对同一句话"，模型下一轮会以为自己自言自语。
_RUNNING: dict[int, "RunningGeneration"] = {}
_GUARD = threading.Lock()


class RunningGeneration:
    """一次正在进行的流式生成。

    ★ 为什么需要 `lock`：同步生成器**不是线程安全的** —— 同一时刻只能有一个线程
      在它上面 `next()`。前端断开时，线程池里可能还有一个 `next()` 正在跑
      （它算完就没人接了），而收尾要把它关掉；两个线程同时碰它会直接抛
      "generator already executing"（真实断线复现里撞到过）。
    """

    __slots__ = ("session_id", "label", "lock", "finished")

    def __init__(self, session_id: int, label: str) -> None:
        self.session_id = session_id
        self.label = label
        self.lock = threading.Lock()
        self.finished = threading.Event()

    def __repr__(self) -> str:  # pragma: no cover - 只用于日志
        return f"<RunningGeneration session={self.session_id} {self.label}>"


def is_generating(session_id: int) -> bool:
    """这个会话此刻是否正在生成（界面据此显示"AI 正在写…"；也用于并发守卫）。"""
    with _GUARD:
        rs = _RUNNING.get(session_id)
    return rs is not None and not rs.finished.is_set()


def running_count() -> int:
    """当前进程里正在生成的条数（供测试与排查用）。"""
    with _GUARD:
        return sum(1 for rs in _RUNNING.values() if not rs.finished.is_set())


def register(session_id: int, label: str) -> RunningGeneration:
    """登记一轮生成。已有在跑的直接抛 `ConflictError`（由接口层翻成 409）。

    ★ 为什么把它放在"落库用户消息之前"：一旦这条请求注定要被拒绝，
      就**不该**先把用户那句话写进库 —— 否则库里会多一句永远没人回的话。
    """
    from app.core.exceptions import ConflictError

    with _GUARD:
        existing = _RUNNING.get(session_id)
        if existing is not None and not existing.finished.is_set():
            raise ConflictError(
                "这条世界线还在写上一轮的回复，等它写完再发下一条吧",
                detail={
                    "session_id": session_id,
                    "generating": True,
                    "suggestion": "上一轮马上就好；写完刷新就能看到",
                },
            )
        rs = RunningGeneration(session_id, label)
        _RUNNING[session_id] = rs
        return rs


def unregister(rs: RunningGeneration) -> None:
    """把这一轮从登记表里摘掉（幂等）。"""
    with _GUARD:
        if _RUNNING.get(rs.session_id) is rs:
            _RUNNING.pop(rs.session_id, None)


def advance(rs: RunningGeneration, generator: Any) -> tuple[str, Any]:
    """在锁保护下推进生成器一步（异常原样抛出，由接口层翻译成 SSE error）。"""
    with rs.lock:
        return next(generator)


def close_generator(generator: Any) -> None:
    """让同步生成器走 finally（关闭上游 HTTP 连接、并触发"保留已生成部分"落库）。"""
    close = getattr(generator, "close", None)
    if callable(close):
        try:
            close()
        except BaseException as exc:  # noqa: BLE001 - 收尾失败不能盖住原始错误
            logger.warning("关闭流式生成器时出错: {}", exc)


def close_locked(rs: RunningGeneration, generator: Any) -> None:
    """**在锁保护下**关闭生成器：先等在飞的那一步结束，再关。

    ==================== ★ 这条是真实断线复现抓出来的 ====================
    第一版收尾时直接 `generator.close()`，结果"已经写出来的字"没保住。
    差别就在这里：

      前端断开的那一刻，线程池里**很可能还有一步 `next()` 正在跑**
      （模型刚吐了一个字、正在算下一个）。此时从另一个线程 `close()` 它会直接抛
      `ValueError: generator already executing`；而它被 `close_generator` 吞成了
      warning，于是生成器没被关上 → `finally` 不跑 → `_keep_partial` 不跑 →
      **已经写出来的那段字丢了** —— 用户看到的还是空白。

    所以关之前必须拿一次锁：那一步跑完、锁释放，再关就必定成功。
    """
    with rs.lock:
        close_generator(generator)
