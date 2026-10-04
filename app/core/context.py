"""请求上下文。

用 ContextVar 保存当前请求的 request_id，使日志和错误响应能够串联同一次请求，
便于排查"用户报错但不知道是哪条日志"的问题。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    """生成一个短小、便于肉眼比对的请求 ID。"""
    return uuid.uuid4().hex[:16]


def set_request_id(value: str) -> None:
    """写入当前上下文的 request_id。"""
    _request_id_var.set(value)


def get_request_id() -> str:
    """读取当前上下文的 request_id，未设置时返回 "-"。"""
    return _request_id_var.get()
