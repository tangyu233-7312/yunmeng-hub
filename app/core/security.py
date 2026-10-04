"""安全基础设施：密码哈希、JWT 签发与校验、API Key 加解密。

==================== 三件事，一个模块 ====================
    1. hash_password / verify_password   —— 用户登录密码（bcrypt，不可逆）
    2. create_token / decode_token        —— 登录凭证（JWT）
    3. ApiKeyCipher                       —— 用户自配的 LLM API Key（Fernet，可逆）

==================== 为什么用 bcrypt 而不是 SHA-256？====================
SHA-256 之类的通用哈希**算得太快**：现代显卡每秒能算几十亿次，
用户密码如果只有 8 位，几小时就能暴力穷举出来。
bcrypt 故意设计得很慢（可通过 cost 参数调节），并且内置随机盐，
使得「彩虹表」和「批量爆破」都失去意义。

★ 一个必须知道的限制：bcrypt 只处理前 **72 字节**。
  中文一个字符占 3 字节，即使用户名密码也不能太长。
  超出会被静默截断（老版本）或直接报错（bcrypt 5.x），
  所以本项目在接口层就做字节数校验并给出明确提示。

==================== 为什么 API Key 用可逆加密？====================
用户的 LLM API Key 和我们自己的密码不同 —— 调用大模型时必须拿到**明文**，
所以不能哈希，只能加密。这里用 Fernet（AES-128-CBC + HMAC 签名）：

    · 密钥来自 .env 的 HNE_API_KEY_ENCRYPTION_KEY，不随代码入库
    · 密文带 HMAC 签名，被篡改会在解密时报错，而不是解出一段垃圾
    · 这样即使数据库被拖库，攻击者拿到的也只是一堆密文
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError, UnauthorizedError

# ==================================================================
#  密码哈希
# ==================================================================
#: bcrypt 能处理的最大字节数（注意是字节不是字符）
BCRYPT_MAX_BYTES = 72


def hash_password(plain_password: str) -> str:
    """把明文密码转成 bcrypt 哈希字符串（含随机盐，同一密码每次结果都不同）。"""
    raw = plain_password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        # 明确报错而不是让它被静默截断 —— 那会导致「超长密码后 72 字节随便填都能登录」
        raise ValueError(f"密码超过 bcrypt 上限 {BCRYPT_MAX_BYTES} 字节")
    rounds = get_settings().BCRYPT_ROUNDS
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=rounds)).decode("ascii")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验密码是否匹配。任何异常都视为「不匹配」，不向上抛。"""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"), password_hash.encode("ascii")
        )
    except (ValueError, TypeError):
        # 哈希格式损坏、密码超长等情况，一律按验证失败处理
        return False


#: 用于「用户不存在」时消耗等量时间的假哈希。
#: 不硬编码一个固定字符串，而是首次用到时现算并缓存 ——
#: 硬编码的哈希一旦格式有误，checkpw 会直接抛异常而**不做任何计算**，
#: 那样防护就失效了，而且很难发现。
@lru_cache(maxsize=1)
def get_dummy_password_hash() -> str:
    """获取一个真实的 bcrypt 哈希，用于对齐「用户不存在」时的响应耗时。"""
    return hash_password("timing-equalization-dummy-password")


# ==================================================================
#  JWT
# ==================================================================
TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


def _create_token(subject: str | int, token_type: str, expires_delta: timedelta) -> str:
    """签发一个 JWT。

    subject 用用户 ID（字符串形式）。注意 JWT 的 payload 是**明文可读**的，
    所以绝不能把密码、密钥之类的敏感信息放进去。
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        # jti 是唯一标识，将来若要做「登出即失效」的黑名单，就靠它
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str | int) -> str:
    """签发访问令牌（短期，用于每次接口调用）。"""
    settings = get_settings()
    return _create_token(
        subject,
        TOKEN_TYPE_ACCESS,
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )


def create_refresh_token(subject: str | int) -> str:
    """签发刷新令牌（长期，仅用于换取新的访问令牌）。"""
    settings = get_settings()
    return _create_token(
        subject,
        TOKEN_TYPE_REFRESH,
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    )


def decode_token(token: str, *, expected_type: str | None = None) -> dict[str, Any]:
    """校验并解析 JWT。

    抛出 UnauthorizedError（而不是底层库的异常），这样全局异常处理器
    能把它转成统一的 401 响应结构。
    """
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError(
            "登录状态已过期，请重新登录", detail={"reason": "token_expired"}
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError(
            "登录凭证无效", detail={"reason": "invalid_token"}
        ) from exc

    if expected_type and payload.get("type") != expected_type:
        raise UnauthorizedError(
            "凭证类型不正确",
            detail={"expected": expected_type, "actual": payload.get("type")},
        )
    if not payload.get("sub"):
        raise UnauthorizedError("登录凭证缺少用户标识")

    return payload


def token_expires_in_seconds(token_type: str) -> int:
    """返回该类型令牌的有效期秒数（供接口返回给前端展示倒计时）。"""
    settings = get_settings()
    if token_type == TOKEN_TYPE_REFRESH:
        return settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    return settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


# ==================================================================
#  API Key 加解密
# ==================================================================
class ApiKeyCipher:
    """用户自配的 LLM API Key 的对称加解密器。"""

    def __init__(self, key: str | None = None) -> None:
        raw = (key if key is not None else get_settings().API_KEY_ENCRYPTION_KEY).strip()
        if not raw or raw.startswith("CHANGE_ME"):
            raise ConfigurationError(
                "未配置 API_KEY_ENCRYPTION_KEY，无法安全存储用户的 API Key",
                detail={
                    "suggestion": (
                        "请在 .env 中设置 HNE_API_KEY_ENCRYPTION_KEY。"
                        '生成命令：python -c "from cryptography.fernet import Fernet; '
                        'print(Fernet.generate_key().decode())"'
                    )
                },
            )
        try:
            self._fernet = Fernet(raw.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(
                "API_KEY_ENCRYPTION_KEY 不是合法的 Fernet 密钥",
                detail={
                    "reason": str(exc),
                    "requirement": "必须是 44 位 base64 字符串（32 字节密钥）",
                },
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        """加密。空字符串视为「未配置」，原样返回空串而不是加密出一段密文。"""
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """解密。

        ★ 解密失败通常意味着 HNE_API_KEY_ENCRYPTION_KEY 被换过了 ——
          这时所有已存的 API Key 都无法恢复，必须让用户重新填写。
          这里给出明确的错误说明，而不是抛一段看不懂的底层异常。
        """
        if not ciphertext:
            return ""
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            raise ConfigurationError(
                "API Key 解密失败：密文已损坏，或加密密钥发生了变更",
                detail={
                    "suggestion": (
                        "如果最近更换过 HNE_API_KEY_ENCRYPTION_KEY，"
                        "所有旧的 API Key 都无法再解密，请让用户重新填写"
                    )
                },
            ) from exc


@lru_cache(maxsize=1)
def get_api_key_cipher() -> ApiKeyCipher:
    """获取全局唯一的加解密器（带缓存，避免每次请求都重新解析密钥）。"""
    return ApiKeyCipher()


def mask_api_key(plaintext: str | None) -> str:
    """把 API Key 脱敏成可安全展示的形式。

    ★ 任何要返回给前端或写进日志的地方，都必须先过这个函数。
    """
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "*" * len(plaintext)
    return f"{plaintext[:4]}****{plaintext[-4:]}"
