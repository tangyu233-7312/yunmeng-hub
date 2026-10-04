"""用户业务逻辑：注册与登录。

放在 services 层而不是直接写在路由里，好处是：
  · 路由层只负责收发 HTTP，业务规则集中在一处，便于测试与复用
  · 将来如果需要「命令行创建管理员」之类的功能，也能直接复用这些函数
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from loguru import logger

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import get_dummy_password_hash, hash_password, verify_password
from app.db.models import User
from app.schemas.user import UserRegister


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.get(User, user_id)


def get_user_by_username_or_email(db: Session, identifier: str) -> User | None:
    """按用户名或邮箱查找用户（登录时两者都允许）。"""
    statement = select(User).where(
        or_(User.username == identifier, User.email == identifier)
    )
    return db.scalar(statement)


def register_user(db: Session, payload: UserRegister) -> User:
    """注册新用户。

    唯一性检查做两层：
      1. 先查一次，给出**友好**的错误提示（哪个字段重复了）
      2. 依赖数据库的唯一索引兜底，防止并发注册产生重复

    只做第 1 层是不够的：两个请求同时通过检查时仍会写入重复数据，
    而这种错误只能在数据库层拦下。
    """
    # ---- 第一层：友好检查 ----
    if db.scalar(select(User.id).where(User.username == payload.username)):
        raise ConflictError(
            "该用户名已被注册", detail={"field": "username", "value": payload.username}
        )
    if db.scalar(select(User.id).where(User.email == payload.email)):
        raise ConflictError(
            "该邮箱已被注册", detail={"field": "email", "value": payload.email}
        )

    user = User(
        username=payload.username,
        email=payload.email,
        # ★ 只存 bcrypt 哈希，永不保存明文
        password_hash=hash_password(payload.password),
        nickname=payload.nickname or None,
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError as exc:
        # ---- 第二层：数据库唯一索引兜底（并发场景）----
        db.rollback()
        logger.warning("注册时触发唯一约束冲突: {}", exc.orig)
        raise ConflictError(
            "用户名或邮箱已被占用，请换一个",
            detail={"reason": "unique_constraint_violation"},
        ) from exc

    db.refresh(user)
    logger.info("新用户注册成功 | id={} username={}", user.id, user.username)
    return user


def authenticate_user(db: Session, identifier: str, password: str) -> User:
    """校验账号密码，成功返回用户对象，失败抛出统一的 401。

    ★ 安全细节：无论「用户不存在」还是「密码错误」，都返回**完全相同**的错误信息。
      否则攻击者可以通过错误文案差异枚举出哪些用户名是有效的。

    ★ 另一个细节：用户不存在时也要**做一次等价的密码校验**。
      因为 bcrypt 校验本身要花约 100ms，如果用户不存在时直接返回，
      响应会明显更快 —— 攻击者靠**计时差异**同样能枚举用户名。
      所以这里用一个固定的假哈希跑一遍，把耗时补齐。
    """
    user = get_user_by_username_or_email(db, identifier)

    if user is None:
        # 消耗与真实校验相当的时间，抹平计时差异
        verify_password(password, get_dummy_password_hash())
        raise UnauthorizedError(
            "用户名或密码错误", detail={"reason": "invalid_credentials"}
        )

    if not verify_password(password, user.password_hash):
        raise UnauthorizedError(
            "用户名或密码错误", detail={"reason": "invalid_credentials"}
        )

    if not user.is_active:
        # 账号被禁用的提示要与「密码错误」区分开，否则用户会一直以为是密码问题
        raise UnauthorizedError(
            "账号已被禁用，请联系管理员", detail={"reason": "account_disabled"}
        )

    return user


def touch_last_login(db: Session, user: User) -> None:
    """记录登录（当前仅写日志；将来如需统计登录活跃度可扩展为字段）。"""
    logger.info("用户登录成功 | id={} username={}", user.id, user.username)
