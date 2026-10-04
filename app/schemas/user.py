"""用户相关的请求 / 响应模型。"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.security import BCRYPT_MAX_BYTES

#: 用户名只允许字母、数字、下划线（避免各类注入与展示问题）
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,50}$")


class UserRegister(BaseModel):
    """注册请求。"""

    username: str = Field(..., description="登录用户名（3~50 位字母/数字/下划线）")
    email: EmailStr = Field(..., description="邮箱")
    password: str = Field(..., description=f"密码（8 位以上，且不超过 {BCRYPT_MAX_BYTES} 字节）")
    nickname: str | None = Field(default=None, max_length=50, description="昵称，可留空")

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        value = value.strip()
        if not _USERNAME_PATTERN.match(value):
            raise ValueError("用户名只能包含字母、数字、下划线，长度 3~50 位")
        return value

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        """密码强度校验。

        ★ 这里必须按**字节数**校验而不是字符数：
          bcrypt 只处理前 72 字节，而一个中文字符占 3 字节。
          如果只在 Pydantic 里限制 max_length=72 个字符，
          用户填 24 个中文字（=72 字节）之后的部分会被静默截断，
          导致「前 24 个字相同就算同一个密码」这种安全问题。
        """
        if len(value) < 8:
            raise ValueError("密码至少 8 位")
        byte_length = len(value.encode("utf-8"))
        if byte_length > BCRYPT_MAX_BYTES:
            raise ValueError(
                f"密码过长：当前 {byte_length} 字节，bcrypt 最多支持 {BCRYPT_MAX_BYTES} 字节"
                f"（中文一个字约 3 字节，即约 24 个汉字或 72 个英文字符）"
            )
        return value


class UserLogin(BaseModel):
    """登录请求。"""

    username: str = Field(..., description="用户名或邮箱")
    password: str = Field(..., description="密码")


class RefreshTokenRequest(BaseModel):
    """刷新令牌请求。"""

    refresh_token: str = Field(..., description="登录时返回的 refresh_token")


class UserOut(BaseModel):
    """用户信息（对外返回，**绝不包含密码哈希**）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    nickname: str | None = None
    is_active: bool
    created_at: datetime


class TokenPair(BaseModel):
    """登录成功后返回的令牌对。"""

    access_token: str = Field(..., description="访问令牌，放进 Authorization: Bearer <token>")
    refresh_token: str = Field(..., description="刷新令牌，仅用于换取新的访问令牌")
    token_type: str = Field(default="bearer", description="令牌类型，固定为 bearer")
    expires_in: int = Field(..., description="访问令牌有效期（秒）")
