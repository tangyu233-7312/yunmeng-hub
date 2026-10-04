"""通用响应模型。

==================== 统一响应格式约定 ====================
所有接口返回统一结构，前端（HTML/JS/Electron）只需写一个通用的请求封装即可。

成功：
    {"code": "OK", "message": "success", "data": {...}}

失败（由 app/core/exceptions.py 的全局处理器统一产生）：
    {"code": "NOT_FOUND", "message": "...", "detail": ..., "request_id": "..."}
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, computed_field

# TypeVar("T") 是「类型变量」，让 ApiResponse 能带一个泛型参数：
#   ApiResponse[UserOut]  表示 data 字段是 UserOut 类型
# 这样 Swagger 文档里能显示出 data 的真实结构，IDE 也有类型提示。
T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一成功响应壳。用法：return ApiResponse.ok(user_out)。"""

    code: str = Field(default="OK", description="业务状态码，成功恒为 OK")
    message: str = Field(default="success", description="人类可读的提示信息")
    data: T | None = Field(default=None, description="业务数据")

    @classmethod
    def ok(cls, data: T | None = None, message: str = "success") -> "ApiResponse[T]":
        """构造一个成功响应，避免每次手写三个字段。"""
        return cls(code="OK", message=message, data=data)


class Page(BaseModel, Generic[T]):
    """分页结果壳。

    ==================== 为什么不直接返回数组？====================
    角色卡可能有很多张（尤其是「公共角色卡库」），一次全塞给前端既慢又费内存。
    分页返回时必须同时给出 total，否则前端没法渲染「第 2/5 页」这类控件 ——
    只知道当前这页有几条是不够的。

    ★ 这里刻意没有把它套用到 /providers 上：
      模型配置是一个人的私人设置，通常不超过十条，分页纯属多余。
      数据量级不同，接口设计就不该硬套同一个模子。
    """

    items: list[T] = Field(default_factory=list, description="当前页的数据")
    total: int = Field(..., description="满足条件的总条数（不受本页 limit 影响）")
    limit: int = Field(..., description="每页条数")
    offset: int = Field(..., description="跳过的条数")

    @classmethod
    def create(
        cls, items: list[T], total: int, limit: int, offset: int
    ) -> "Page[T]":
        return cls(items=items, total=total, limit=limit, offset=offset)

    @computed_field(description="是否还有下一页，前端据此决定要不要显示「加载更多」")  # type: ignore[prop-decorator]
    @property
    def has_more(self) -> bool:
        # 用 offset + 本页实际条数 与 total 比较，而不是 offset + limit，
        # 这样最后一页不满时也能正确判定
        return self.offset + len(self.items) < self.total


class ErrorResponse(BaseModel):
    """统一错误响应。

    注意：这个类只用于在 Swagger 文档里展示错误长什么样，
    实际响应体是 app/core/exceptions.py 里的异常处理器动态生成的。
    """

    code: str = Field(..., examples=["NOT_FOUND"], description="业务错误码")
    message: str = Field(..., examples=["请求的资源不存在"], description="错误说明")
    detail: Any = Field(default=None, description="附加信息，如字段级校验错误列表")
    request_id: str = Field(
        default="-", examples=["3f9a1c2b8d4e5f60"], description="请求ID，用于对照服务端日志"
    )


class ComponentStatus(BaseModel):
    """单个外部依赖（MySQL / 向量库 等）的健康状态。"""

    status: str = Field(
        ..., examples=["ok"], description="ok / error / not_initialized"
    )
    detail: dict[str, Any] | None = Field(
        default=None, description="附加信息，例如版本号或失败原因"
    )


class HealthResponse(BaseModel):
    """健康检查响应。

    components 会逐个列出外部依赖的状态，方便快速定位「到底哪一环挂了」，
    而不是只看到一个笼统的 500。
    """

    status: str = Field(..., examples=["ok"], description="整体状态：ok / degraded")
    app: str = Field(..., description="应用名")
    version: str = Field(..., description="应用版本")
    env: str = Field(..., description="运行环境：development / testing / production")
    timestamp: datetime = Field(..., description="服务器当前时间（UTC）")
    components: dict[str, ComponentStatus] = Field(
        default_factory=dict, description="各外部依赖的健康状态"
    )
