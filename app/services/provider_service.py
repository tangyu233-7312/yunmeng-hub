"""模型配置业务逻辑：增删改查、密钥加密、连通性测试、参数生效性探测。

==================== 三条安全铁律 ====================
1. **每个查询都必须带 user_id 条件**
   否则就是典型的越权漏洞（IDOR）：用户 A 只要猜到用户 B 的配置 ID，
   就能读取甚至删除对方的配置（里面含 API Key）。

2. **任何情况下都不返回密钥明文**
   对外只给脱敏形式（sk-a****z9）与「是否已配置」标记。

3. **密钥用 Fernet 加密后入库**
   数据库被拖库时，攻击者拿到的只是一堆密文。
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import (
    BadRequestError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
)
from app.core.security import get_api_key_cipher, mask_api_key
from app.db.models import LLMProvider
from app.llm import (
    GenerationParams,
    ProviderConfig,
    create_provider as create_llm_provider,
    create_provider_from_config,
)
from app.llm.base import BaseLLMProvider, HealthCheckResult

# 说明：本项目的 MySQL 时间字段使用 server_default=func.now()，
# 取的是**数据库所在机器的本地时间**。为了让手工写入的时间戳与之一致，
# 这里统一使用本地朴素时间（naive local），不混用 UTC，避免出现「同一张表里
# 两种时区」这种极难排查的问题。
def _now() -> datetime:
    return datetime.now()


def _assert_output_fits(row: LLMProvider) -> None:
    """校验「最大输出 < 上下文窗口」。

    为什么服务层还要再查一遍？因为 PATCH 更新是**部分字段**的：
    用户可能只提交 max_tokens，而 context_window 沿用数据库里的旧值，
    此时接口层的跨字段校验根本看不到完整组合。
    所以真正的防线在这里 —— 在写库之前拿最终组合做一次检查。
    """
    if row.max_tokens >= row.context_window:
        raise BadRequestError(
            f"最大输出 token（{row.max_tokens}）不能大于等于上下文窗口"
            f"（{row.context_window}）—— 那样就没有空间放提示词和对话历史了",
            detail={
                "max_tokens": row.max_tokens,
                "context_window": row.context_window,
                "suggestion": "请调大上下文窗口，或调小最大输出 token",
            },
        )


# ==================================================================
#  查询
# ==================================================================
def list_providers(db: Session, user_id: int) -> list[LLMProvider]:
    """列出某用户的全部模型配置（默认模型排在最前）。"""
    statement = (
        select(LLMProvider)
        .where(LLMProvider.user_id == user_id)
        .order_by(LLMProvider.is_default.desc(), LLMProvider.id)
    )
    return list(db.scalars(statement).all())


def get_owned_provider(db: Session, user_id: int, provider_id: int) -> LLMProvider:
    """按 ID 取配置，**并且必须属于该用户**。

    ★ 查不到时返回 404 而不是 403：这样不会泄露「这个 ID 是否存在」，
      避免攻击者通过状态码差异枚举出别人的配置 ID。
    """
    statement = select(LLMProvider).where(
        LLMProvider.id == provider_id, LLMProvider.user_id == user_id
    )
    row = db.scalar(statement)
    if row is None:
        raise NotFoundError(
            "模型配置不存在", detail={"provider_id": provider_id}
        )
    return row


# ==================================================================
#  默认模型
# ==================================================================
def _clear_other_defaults(db: Session, user_id: int, keep_id: int | None) -> None:
    """把该用户其它配置的 is_default 置为 False。

    「每个用户最多一个默认模型」这条规则由业务逻辑保证（数据库无法用唯一索引
    表达「只允许一个 True」），所以每次设置默认时都要清掉其它的。
    """
    statement = select(LLMProvider).where(
        LLMProvider.user_id == user_id, LLMProvider.is_default.is_(True)
    )
    if keep_id is not None:
        statement = statement.where(LLMProvider.id != keep_id)
    for row in db.scalars(statement).all():
        row.is_default = False


# ==================================================================
#  增 / 改 / 删
# ==================================================================
def _resolve_fallback(
    db: Session, user_id: int, fallback_id: int | None, *, self_id: int | None
) -> int | None:
    """校验"备用模型"：必须是**自己的另一个**配置。

    ★ 在这里就报 422，而不是等到主模型挂了、想切的时候才发现指向了一个不存在的配置
      —— 那时候用户正在等回复，报错会让人以为是模型的问题（真实体验很糟）。
    """
    if fallback_id is None:
        return None
    if self_id is not None and fallback_id == self_id:
        raise BadRequestError(
            "备用模型不能选自己", detail={"field": "fallback_provider_id"}
        )
    row = db.get(LLMProvider, fallback_id)
    if row is None or row.user_id != user_id:
        raise BadRequestError(
            "备用模型不存在，或者不属于你", detail={"field": "fallback_provider_id"}
        )
    return fallback_id


def create_provider(db: Session, user_id: int, payload) -> LLMProvider:
    """新增模型配置（payload 为 ProviderCreate）。"""
    cipher = get_api_key_cipher()

    if payload.is_default:
        _clear_other_defaults(db, user_id, keep_id=None)

    row = LLMProvider(
        user_id=user_id,
        name=payload.name,
        provider_type=payload.provider_type,
        base_url=payload.base_url,
        # ★ 加密后入库，绝不存明文
        api_key_encrypted=cipher.encrypt(payload.api_key),
        model_name=payload.model_name,
        context_window=payload.context_window,
        # 核心生成参数落到独立字段（便于校验与查询）
        temperature=payload.generation.temperature,
        top_p=payload.generation.top_p,
        max_tokens=payload.generation.max_tokens,
        reasoning_effort=payload.generation.reasoning_effort.value,
        # 长尾参数放 JSON 透传
        extra_params=dict(payload.generation.extra),
        fallback_provider_id=_resolve_fallback(
            db, user_id, getattr(payload, "fallback_provider_id", None), self_id=None
        ),
        stream_enabled=bool(getattr(payload, "stream_enabled", True)),
        is_default=payload.is_default,
        is_active=payload.is_active,
    )
    # 写库前做整体校验（接口层已经查过一次，这里是最后一道防线）
    _assert_output_fits(row)
    db.add(row)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # 唯一约束是 (user_id, name)
        raise ConflictError(
            "同名配置已存在，请换一个别名",
            detail={"field": "name", "value": payload.name},
        ) from exc

    db.refresh(row)
    logger.info(
        "新增模型配置 | user_id={} id={} name={} model={}",
        user_id,
        row.id,
        row.name,
        row.model_name,
    )
    return row


def update_provider(db: Session, user_id: int, provider_id: int, payload) -> LLMProvider:
    """更新模型配置（payload 为 ProviderUpdate，PATCH 语义）。"""
    row = get_owned_provider(db, user_id, provider_id)
    cipher = get_api_key_cipher()

    # ---------------- 基础字段 ----------------
    if payload.name is not None:
        row.name = payload.name
    if payload.provider_type is not None:
        row.provider_type = payload.provider_type
    if payload.base_url is not None:
        row.base_url = payload.base_url
    if payload.model_name is not None:
        row.model_name = payload.model_name
    if payload.context_window is not None:
        row.context_window = payload.context_window

    # ---------------- 密钥的三态处理 ----------------
    if payload.clear_api_key:
        row.api_key_encrypted = ""
    elif payload.api_key:
        # 非空才更新；空字符串/null 都表示「保持原密钥不变」
        row.api_key_encrypted = cipher.encrypt(payload.api_key)

    # ---------------- 生成参数 ----------------
    if payload.generation is not None:
        generation = payload.generation
        row.temperature = generation.temperature
        row.top_p = generation.top_p
        row.max_tokens = generation.max_tokens
        row.reasoning_effort = generation.reasoning_effort.value
        row.extra_params = dict(generation.extra)

    # ---------------- 状态 ----------------
    if payload.is_active is not None:
        row.is_active = payload.is_active
    # ★ 备用模型用 model_fields_set 判断"到底传没传"：
    #   传 null = 解绑（不自动切换），完全不传 = 不动它 —— 这两种语义必须分开，
    #   否则用户永远没法取消已经设好的备用模型。
    if "fallback_provider_id" in payload.model_fields_set:
        row.fallback_provider_id = _resolve_fallback(
            db, user_id, payload.fallback_provider_id, self_id=row.id
        )
    if payload.stream_enabled is not None:
        row.stream_enabled = payload.stream_enabled
    if payload.is_default is not None:
        if payload.is_default:
            _clear_other_defaults(db, user_id, keep_id=row.id)
        row.is_default = payload.is_default

    # ★ 部分更新后必须拿**最终组合**再校验一次：
    #   用户可能只改了 max_tokens，context_window 沿用旧值，接口层看不到完整组合
    _assert_output_fits(row)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError(
            "同名配置已存在，请换一个别名", detail={"field": "name"}
        ) from exc

    db.refresh(row)
    logger.info("更新模型配置 | user_id={} id={}", user_id, row.id)
    return row


def delete_provider(db: Session, user_id: int, provider_id: int) -> None:
    """删除模型配置。

    注意：narration_sessions.llm_provider_id 的外键是 ON DELETE SET NULL，
    所以删除配置**不会**删掉用户开过的故事，只是那些会话不再关联模型。
    """
    row = get_owned_provider(db, user_id, provider_id)
    db.delete(row)
    db.commit()
    logger.info("删除模型配置 | user_id={} id={}", user_id, provider_id)


# ==================================================================
#  ORM 行 → 领域对象
# ==================================================================
def to_config(row: LLMProvider, *, api_key: str | None = None) -> ProviderConfig:
    """把数据库行还原成 ProviderConfig。

    参数 api_key 传入时会覆盖数据库里的密钥（用于「测试未保存的配置」这类场景）。
    """
    if api_key is None:
        api_key = get_api_key_cipher().decrypt(row.api_key_encrypted)

    return ProviderConfig(
        name=row.name,
        provider_type=row.provider_type,
        base_url=row.base_url,
        api_key=api_key,
        model_name=row.model_name,
        context_window=row.context_window,
        generation=GenerationParams(
            temperature=row.temperature,
            top_p=row.top_p,
            max_tokens=row.max_tokens,
            # 数据库里存的是字符串，这里恢复成枚举（非法值会被 pydantic 拦下）
            reasoning_effort=row.reasoning_effort,
            extra=dict(row.extra_params or {}),
        ),
    )


def build_adapter(row: LLMProvider) -> BaseLLMProvider:
    """由数据库行构造一个可调用的适配器。

    这是「数据库里的配置」到「真正能发请求的对象」之间的一步，
    测试连接、参数探测、后续的叙事对话都走这里。
    """
    return create_provider_from_config(to_config(row))


def serialize_provider(row: LLMProvider) -> dict:
    """把数据库行整理成对外返回的字典。

    ★ 密钥只以脱敏形式出现，明文绝不外泄。
    """
    api_key = ""
    decryptable = True
    try:
        api_key = get_api_key_cipher().decrypt(row.api_key_encrypted)
    except ConfigurationError:
        # 加密密钥被更换过时，旧密文无法解开。这属于可恢复的问题：
        # 提示用户重新填写即可，不应该让整个列表接口 500。
        decryptable = False

    config = to_config(row, api_key=api_key)
    warnings = config.warnings()
    hints = config.hints()

    if not decryptable:
        warnings.insert(
            0,
            "该配置的 API Key 无法解密（通常是 HNE_API_KEY_ENCRYPTION_KEY 被更换过），"
            "请重新填写密钥。",
        )

    return {
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "base_url": row.base_url,
        "model_name": row.model_name,
        "context_window": row.context_window,
        "generation": config.generation.model_dump(mode="json"),
        "api_key_masked": mask_api_key(api_key),
        "has_api_key": bool(row.api_key_encrypted),
        "api_key_decryptable": decryptable,
        "is_default": row.is_default,
        "is_active": row.is_active,
        "fallback_provider_id": row.fallback_provider_id,
        "stream_enabled": bool(row.stream_enabled),
        "last_tested_at": row.last_tested_at,
        "last_test_ok": row.last_test_ok,
        "last_test_message": row.last_test_message,
        "budget": config.budget.to_dict(),
        "warnings": warnings,
        "hints": hints,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# ==================================================================
#  诊断动作
# ==================================================================
def test_connection(db: Session, row: LLMProvider) -> HealthCheckResult:
    """对配置做一次连通性测试，并把结果写回数据库。"""
    provider = build_adapter(row)
    try:
        result = provider.health_check()
    finally:
        provider.close()

    row.last_tested_at = _now()
    row.last_test_ok = result.ok
    # 字段上限 512，超长会写入失败，这里截断
    row.last_test_message = None if result.ok else (result.message or "")[:512]
    db.commit()
    db.refresh(row)
    return result


def fetch_models(row: LLMProvider) -> list[str]:
    """拉取该服务提供商的可用模型列表。"""
    provider = build_adapter(row)
    try:
        return provider.list_models()
    finally:
        provider.close()


def test_raw_config(
    *,
    provider_type: str,
    base_url: str,
    api_key: str,
    model_name: str,
    context_window: int = 65536,
    generation: GenerationParams | None = None,
) -> HealthCheckResult:
    """**不落库**的连通性测试。

    用于界面上「先测一下再保存」的场景：用户填完表单点「测试连接」，
    此时配置还没入库，没法走 test_connection。
    """
    # 注意这里用的是 create_llm_provider（导入时起了别名）：
    # 本模块自己也有一个叫 create_provider 的函数（新增配置），
    # 如果直接用原名会被**遮蔽**掉，调用时报 "unexpected keyword argument"。
    provider = create_llm_provider(
        provider_type=provider_type,
        base_url=base_url,
        api_key=api_key,
        model_name=model_name,
        context_window=context_window,
        extra_params=generation or GenerationParams(),
        max_retries=0,  # 测试连接不需要重试，快速失败更能说明问题
    )
    try:
        return provider.health_check()
    finally:
        provider.close()
