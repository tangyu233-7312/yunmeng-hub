"""用户认证接口：注册 / 登录 / 刷新令牌 / 获取当前用户。

==================== 认证方案 ====================
采用 **JWT Bearer Token**，而不是传统 Session：

    注册  ──▶  POST /api/v1/auth/register
    登录  ──▶  POST /api/v1/auth/login      ──▶ 返回 access_token + refresh_token
    调用  ──▶  请求头带上 Authorization: Bearer <access_token>
    续期  ──▶  POST /api/v1/auth/refresh    ──▶ 用 refresh_token 换新的令牌对

为什么用 JWT 而不是 Session？
  · 前端最终要打包成 Electron 桌面应用，JWT 天然适合「客户端持有凭证」的形态
  · 服务端无需存储会话，横向扩展时不用考虑会话共享
  · 代价是「无法立即吊销」—— 本项目通过「访问令牌短（默认 60 分钟）+
    刷新令牌长（默认 7 天）」来平衡。若将来需要强制下线，
    可以引入 jti 黑名单（令牌 payload 里已经预留了 jti 字段）。

★ 两个令牌的分工：
    access_token   有效期短，每次业务请求都带
    refresh_token  有效期长，**只能**用来换取新令牌
  因此 refresh_token 绝不能当访问令牌用 —— 这一点在依赖注入层已经强制校验。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import CurrentUser, DbSession
from app.core.exceptions import UnauthorizedError
from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_REFRESH,
    create_access_token,
    create_refresh_token,
    decode_token,
    token_expires_in_seconds,
)
from app.schemas.common import ApiResponse
from app.schemas.user import (
    RefreshTokenRequest,
    TokenPair,
    UserLogin,
    UserOut,
    UserRegister,
)
from app.services.user_service import (
    authenticate_user,
    get_user_by_id,
    register_user,
    touch_last_login,
)

router = APIRouter()


def _build_token_pair(user_id: int) -> TokenPair:
    """签发一对令牌。"""
    return TokenPair(
        access_token=create_access_token(user_id),
        refresh_token=create_refresh_token(user_id),
        expires_in=token_expires_in_seconds(TOKEN_TYPE_ACCESS),
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="注册新用户",
    response_model=ApiResponse[UserOut],
)
def register(payload: UserRegister, db: DbSession) -> ApiResponse[UserOut]:
    """注册新用户。

    成功后返回用户信息（**不含密码**）。注册接口不直接发令牌，
    前端应紧接着调用登录接口 —— 这样「注册」与「登录」的流程边界更清晰，
    也便于将来加入邮箱验证之类的环节。
    """
    user = register_user(db, payload)
    return ApiResponse.ok(UserOut.model_validate(user), message="注册成功")


@router.post("/login", summary="登录（JSON）", response_model=ApiResponse[TokenPair])
def login(payload: UserLogin, db: DbSession) -> ApiResponse[TokenPair]:
    """登录。username 字段允许填用户名或邮箱。"""
    user = authenticate_user(db, payload.username, payload.password)
    touch_last_login(db, user)
    return ApiResponse.ok(_build_token_pair(user.id), message="登录成功")


@router.post(
    "/token",
    summary="登录（OAuth2 表单，供 Swagger 的 Authorize 按钮使用）",
    response_model=TokenPair,
)
def login_form(
    db: DbSession,
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> TokenPair:
    """OAuth2 密码流表单登录。

    ★ 这个接口存在的唯一目的是**方便在 /docs 页面点「Authorize」按钮测试** ——
      Swagger 需要标准的 OAuth2 表单端点才能自动填充令牌。
      前端（HTML / JS / Electron）请使用上面的 JSON 版本 /login。

    注意：这里直接返回 TokenPair 而不套统一的 ApiResponse 外壳，
    因为 Swagger 的 OAuth2 流程要求响应顶层必须有 access_token / token_type 字段。
    """
    user = authenticate_user(db, form_data.username, form_data.password)
    touch_last_login(db, user)
    return _build_token_pair(user.id)


@router.post("/refresh", summary="刷新令牌", response_model=ApiResponse[TokenPair])
def refresh(payload: RefreshTokenRequest, db: DbSession) -> ApiResponse[TokenPair]:
    """用 refresh_token 换取新的令牌对。

    校验时强制要求类型为 refresh —— 传入 access_token 会被拒绝，
    避免「短令牌换长令牌」这种越权续期。
    """
    claims = decode_token(payload.refresh_token, expected_type=TOKEN_TYPE_REFRESH)

    user = get_user_by_id(db, int(claims["sub"]))
    if user is None or not user.is_active:
        raise UnauthorizedError("账号不存在或已被禁用，请重新登录")

    return ApiResponse.ok(_build_token_pair(user.id), message="令牌已刷新")


@router.get("/me", summary="获取当前登录用户", response_model=ApiResponse[UserOut])
def read_me(current_user: CurrentUser) -> ApiResponse[UserOut]:
    """返回当前登录用户的信息，可用来验证令牌是否仍然有效。"""
    return ApiResponse.ok(UserOut.model_validate(current_user), message="获取成功")
