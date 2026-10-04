"""日志配置：用 loguru 接管标准库 logging 与 uvicorn 日志。

特性：
- 控制台彩色输出（重定向到文件时自动关闭颜色）+ 按天滚动文件（保留 14 天）
- 每条日志自动带上当前请求的 request_id，便于串联同一次请求
- uvicorn / sqlalchemy 等第三方库日志统一格式，且保留真实调用位置

实现说明：
    常见做法是用「栈回溯 + logger.opt(depth=...)」定位调用者，但该方案在 Windows 上
    会因路径大小写差异导致回溯失效（日志里全部显示成 logging:callHandlers）。
    因此这里改为从 logging.LogRecord 直接读取真实位置，写入 extra[src]，完全确定性。
"""

from __future__ import annotations

import logging
import sys

from loguru import logger

from app.core.config import get_settings
from app.core.context import get_request_id

# 控制台格式：简洁优先
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[request_id]}</cyan> | "
    "<level>{message}</level>"
)

# 文件格式：带模块:函数:行号，便于精确定位
_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[request_id]} | "
    "{extra[src]} | {message}"
)


def _patch_record(record: dict) -> None:
    """为每条日志补齐公共字段（request_id 与调用位置）。"""
    record["extra"].setdefault("request_id", get_request_id())
    record["extra"].setdefault(
        "src", f"{record['name']}:{record['function']}:{record['line']}"
    )


class InterceptHandler(logging.Handler):
    """把标准库 logging 的记录转发给 loguru，并保留原始调用位置。"""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        logger.opt(exception=record.exc_info).bind(
            src=f"{record.name}:{record.funcName}:{record.lineno}"
        ).log(level, record.getMessage())


def setup_logging() -> None:
    """初始化日志系统。应在应用启动时调用一次。"""
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()

    # 控制台：仅在真正的终端上启用颜色，避免 ANSI 转义码污染重定向的日志
    logger.add(
        sys.stderr,
        level=settings.LOG_LEVEL,
        format=_CONSOLE_FORMAT,
        colorize=sys.stderr.isatty(),
        backtrace=settings.DEBUG,
        diagnose=settings.DEBUG,
    )

    # 文件：每天 0 点切分，保留 14 天，异步写入不阻塞请求
    logger.add(
        settings.log_dir / "app_{time:YYYY-MM-DD}.log",
        level=settings.LOG_LEVEL,
        format=_FILE_FORMAT,
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=settings.DEBUG,
        diagnose=settings.DEBUG,
    )

    logger.configure(patcher=_patch_record)

    # 接管标准库 logging（uvicorn、sqlalchemy 等第三方库的日志）
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        target = logging.getLogger(name)
        target.handlers = [InterceptHandler()]
        target.propagate = False
