"""FastAPI 依赖注入（Dependency Injection）。

==================== 什么是依赖注入？====================
接口函数不自己去创建数据库连接、也不自己解析登录态，而是把需求写在参数里：

    @router.get("/providers")
    def list_providers(db: DbSession, user: CurrentUser):
        ...

FastAPI 会在调用接口前自动执行 get_db()、get_current_user()，
把结果塞进这两个参数。好处是：资源创建与回收集中管理，
接口函数只关心业务逻辑，也方便单元测试时替换成假实现。

==================== 同步数据库 + 异步接口的注意事项（重要）====================
本项目用的是同步 SQLAlchemy Session，请注意规则：

  * 需要读写数据库的接口 -> 用 def 定义
    FastAPI 会自动把它丢进线程池执行，不会阻塞事件循环。

  * 需要调用大模型流式接口的 -> 用 async def 定义
    但此时不能在 async def 里直接做数据库查询（会卡住整个事件循环），
    要把数据库操作放进线程池：

        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(some_sync_db_function)

  这样既能享受异步流式输出的好处，又不用引入异步 ORM 的复杂度，
  是毕业设计场景下性价比最高的方案。

==================== 认证是怎么工作的？====================
    1. 前端在请求头带上  Authorization: Bearer <access_token>
    2. HTTPBearer 负责把 token 从请求头里取出来
    3. decode_token 校验签名与有效期，解析出用户 ID
    4. 从数据库查出用户对象，注入到接口函数

任何一步失败都会抛出 UnauthorizedError（401），由全局异常处理器
统一转成标准错误结构，接口函数里**一行认证代码都不用写**。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import TOKEN_TYPE_ACCESS, decode_token
from app.db.models import User
from app.db.mysql import get_db
from app.services.user_service import get_user_by_id

# 类型别名：接口里直接写 DbSession 即可
DbSession = Annotated[Session, Depends(get_db)]

# auto_error=False：没有携带凭证时不立刻抛错，而是返回 None，
# 让我们自己决定错误文案（默认实现会返回一个英文的 403，不够友好）
_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="把登录接口返回的 access_token 填到这里（不需要手动加 Bearer 前缀）",
)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: DbSession,
) -> User:
    """解析当前登录用户。未登录或凭证无效时抛出 401。"""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError(
            "请先登录",
            detail={"hint": "请求头需要携带 Authorization: Bearer <access_token>"},
        )

    # expected_type="access" 很关键：防止有人拿 refresh_token 直接调业务接口。
    # refresh_token 有效期长达 7 天，若能被当访问令牌用，等于把长期凭证暴露在每次请求里。
    payload = decode_token(credentials.credentials, expected_type=TOKEN_TYPE_ACCESS)

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnauthorizedError(
            "登录凭证格式不正确", detail={"reason": "invalid_subject"}
        ) from exc

    user = get_user_by_id(db, user_id)
    if user is None:
        # 用户被删除后旧令牌仍在有效期内，这里要拦下
        raise UnauthorizedError("账号不存在或已被删除", detail={"reason": "user_not_found"})

    if not user.is_active:
        raise ForbiddenError("账号已被禁用", detail={"reason": "account_disabled"})

    return user


#: 需要登录的接口直接把这个类型写进参数即可
CurrentUser = Annotated[User, Depends(get_current_user)]
