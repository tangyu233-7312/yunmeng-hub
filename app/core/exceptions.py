"""统一异常体系与全局异常处理器。

设计目标：
1. 任何错误返回体结构一致：{"code", "message", "detail", "request_id"}
2. 业务代码只需 raise 语义化异常，不必关心 HTTP 状态码细节
3. 未捕获异常在生产环境不泄露堆栈，在开发环境返回完整信息便于定位
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.context import get_request_id


class AppException(Exception):
    """所有业务异常的基类。子类只需覆盖 status_code / code / message。"""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "INTERNAL_ERROR"
    message: str = "服务器内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        detail: Any = None,
    ) -> None:
        if message:
            self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code
        self.detail = detail
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "request_id": get_request_id(),
        }
        if self.detail is not None:
            body["detail"] = self.detail
        return body


# ==================== 通用业务异常 ====================
class BadRequestError(AppException):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "BAD_REQUEST"
    message = "请求参数有误"


class UnauthorizedError(AppException):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "UNAUTHORIZED"
    message = "未认证或登录状态已失效"


class ForbiddenError(AppException):
    status_code = status.HTTP_403_FORBIDDEN
    code = "FORBIDDEN"
    message = "没有权限执行该操作"


class NotFoundError(AppException):
    status_code = status.HTTP_404_NOT_FOUND
    code = "NOT_FOUND"
    message = "请求的资源不存在"


class ConflictError(AppException):
    status_code = status.HTTP_409_CONFLICT
    code = "CONFLICT"
    message = "资源已存在或状态冲突"


# ==================== 本项目领域异常 ====================
class ConfigurationError(AppException):
    """服务自身配置有误，例如缺少必要密钥。"""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "CONFIGURATION_ERROR"
    message = "服务配置有误"


class LLMProviderError(AppException):
    """上游大模型服务调用失败：网络异常 / 鉴权失败 / 限流 / 超时 / 返回格式不符。"""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "LLM_PROVIDER_ERROR"
    message = "大模型服务调用失败"


class VectorStoreError(AppException):
    """向量库（ChromaDB）操作失败。"""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    code = "VECTOR_STORE_ERROR"
    message = "向量库操作失败"


class MemoryStoreError(AppException):
    """长期记忆（向量库）操作失败，且这次操作**本来就是用户的目的**。

    ★ 与降级的关系：
      对话过程中的记忆召回/写入失败只记日志、继续对话（记忆是锦上添花）；
      但用户在「记忆」面板里主动检索、添加、清空时，失败必须如实报错，
      否则用户会以为自己什么都没记住、或者以为已经清空了。
    """

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "MEMORY_STORE_ERROR"
    message = "长期记忆操作失败"


# ==================== 全局处理器 ====================
_HTTP_STATUS_CODE_MAP: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "TOO_MANY_REQUESTS",
    500: "INTERNAL_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
}


def register_exception_handlers(app: FastAPI) -> None:
    """把所有异常处理器注册到应用上。"""

    @app.exception_handler(AppException)
    async def _handle_app_exception(request: Request, exc: AppException) -> JSONResponse:
        logger.warning(
            "业务异常 | {} {} | code={} | {}",
            request.method,
            request.url.path,
            exc.code,
            exc.message,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _HTTP_STATUS_CODE_MAP.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": code,
                "message": str(exc.detail),
                "request_id": get_request_id(),
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 把 Pydantic 的报错压平成前端友好的结构
        errors = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())[1:]) or "body",
                "msg": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        logger.info(
            "参数校验失败 | {} {} | {}", request.method, request.url.path, errors
        )
        return JSONResponse(
            # 直接写数字 422：starlette 新版本把 HTTP_422_UNPROCESSABLE_ENTITY
            # 改名成了 HTTP_422_UNPROCESSABLE_CONTENT，用字面量可同时兼容新旧版本，
            # 也不会产生弃用警告
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "detail": errors,
                "request_id": get_request_id(),
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unhandled_exception(
        request: Request, exc: Exception
    ) -> JSONResponse:
        settings = get_settings()
        logger.exception("未捕获异常 | {} {}", request.method, request.url.path)
        message = (
            f"{type(exc).__name__}: {exc}" if settings.DEBUG else "服务器内部错误"
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "code": "INTERNAL_ERROR",
                "message": message,
                "request_id": get_request_id(),
            },
        )
