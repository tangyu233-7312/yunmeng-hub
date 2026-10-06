"""用户自配模型（Provider）的管理接口。

==================== 接口一览 ====================
    GET    /providers                     列出我的全部模型配置
    POST   /providers                     新增配置
    GET    /providers/{id}                查看单个配置
    PATCH  /providers/{id}                修改配置（只传要改的字段）
    DELETE /providers/{id}                删除配置
    POST   /providers/{id}/test           连通性测试（结果写回数据库）
    GET    /providers/{id}/models         拉取该服务商的可用模型列表
    POST   /providers/test-draft          **未保存**的配置试连（界面「先测再存」）

==================== 安全要点 ====================
  · 所有接口都要求登录，且只能操作**自己**的配置（服务层强制带 user_id 条件）
  · 响应里永远不含 API Key 明文，只有脱敏形式（sk-a****z9）
  · 更新密钥遵循三态约定，详见 schemas/provider.py 里 ProviderUpdate 的说明
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.common import ApiResponse
from app.schemas.provider import (
    ModelListResult,
    ProviderCreate,
    ProviderOut,
    ProviderTestResult,
    ProviderUpdate,
)
from app.services import provider_service

router = APIRouter()


def _to_out(row) -> ProviderOut:
    return ProviderOut.model_validate(provider_service.serialize_provider(row))


@router.get("", summary="列出我的模型配置", response_model=ApiResponse[list[ProviderOut]])
def list_providers(db: DbSession, current_user: CurrentUser) -> ApiResponse[list[ProviderOut]]:
    """返回当前用户的全部模型配置，默认模型排在最前。"""
    rows = provider_service.list_providers(db, current_user.id)
    return ApiResponse.ok([_to_out(row) for row in rows])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="新增模型配置",
    response_model=ApiResponse[ProviderOut],
)
def create_provider(
    payload: ProviderCreate, db: DbSession, current_user: CurrentUser
) -> ApiResponse[ProviderOut]:
    """新增一个模型配置。

    密钥会先加密再入库；返回体里只有脱敏形式。
    建议界面流程：先调 /providers/test-draft 试连，通过后再保存 ——
    避免把写错的配置存进数据库。
    """
    row = provider_service.create_provider(db, current_user.id, payload)
    return ApiResponse.ok(_to_out(row), message="配置已创建")


@router.get("/{provider_id}", summary="查看模型配置", response_model=ApiResponse[ProviderOut])
def get_provider(
    provider_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[ProviderOut]:
    """查看单个配置的完整信息（含派生出来的诊断信息）。

    返回体里除了配置本身，还有三类**前端可以直接渲染**的派生字段：

        budget                   上下文预算拆解（输入预算 = 窗口 − 最大输出 − 安全余量）
        warnings / hints         黄色警告与灰色提示，分开返回

    这些不落库、每次按当前配置实时算 —— 用户改了最大输出，预算立刻就变。

    ★ 越权访问返回 404 而不是 403：403 会暴露「这个 ID 确实存在」，
      攻击者可据此枚举别人的配置 ID。
    ★ **绝不会返回 api_key 明文**，只返回脱敏形式与「是否已配置」的标记。
    """
    row = provider_service.get_owned_provider(db, current_user.id, provider_id)
    return ApiResponse.ok(_to_out(row))


@router.patch(
    "/{provider_id}", summary="修改模型配置", response_model=ApiResponse[ProviderOut]
)
def update_provider(
    provider_id: int, payload: ProviderUpdate, db: DbSession, current_user: CurrentUser
) -> ApiResponse[ProviderOut]:
    """修改配置（PATCH 语义：只提交需要改的字段）。

    ★ 关于 API Key：
        · 不传 api_key 或传空字符串 -> 保持原密钥不变
        · 传非空字符串              -> 更新为新密钥
        · clear_api_key = true      -> 清空密钥
      这样做是因为前端不会拿到明文密钥，编辑时只能显示占位符，
      无法把「原样」提交回来。
    """
    row = provider_service.update_provider(db, current_user.id, provider_id, payload)
    return ApiResponse.ok(_to_out(row), message="配置已更新")


@router.delete("/{provider_id}", summary="删除模型配置", response_model=ApiResponse[None])
def delete_provider(
    provider_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[None]:
    """删除配置。

    用它开过的叙事会话**不会被删除**，只是不再关联模型
    （外键是 ON DELETE SET NULL）。
    """
    provider_service.delete_provider(db, current_user.id, provider_id)
    return ApiResponse.ok(None, message="配置已删除")


# ==================================================================
#  诊断动作
# ==================================================================
@router.post(
    "/{provider_id}/test",
    summary="连通性测试",
    response_model=ApiResponse[ProviderTestResult],
)
def test_provider(
    provider_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[ProviderTestResult]:
    """发一条极短的对话，验证网络、密钥、模型名与额度是否都正常。

    结果会写回数据库（last_tested_at / last_test_ok / last_test_message），
    这样界面上可以展示「上次检测成功/失败」，不用每次都重新测。
    """
    row = provider_service.get_owned_provider(db, current_user.id, provider_id)
    result = provider_service.test_connection(db, row)

    return ApiResponse.ok(
        ProviderTestResult(
            ok=result.ok,
            provider_type=result.provider_type,
            model=result.model,
            latency_ms=result.latency_ms,
            message=result.message,
            detail=result.detail,
            tested_at=row.last_tested_at,
        ),
        message="连通性测试完成" if result.ok else "连通性测试未通过",
    )


@router.get(
    "/{provider_id}/models",
    summary="拉取可用模型列表",
    response_model=ApiResponse[ModelListResult],
)
def list_models(
    provider_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[ModelListResult]:
    """调用服务商的 /models 接口，便于用户在界面上选择模型名而不是手打。

    注意：并非所有服务商都实现了该接口，失败时会返回语义化错误。
    """
    row = provider_service.get_owned_provider(db, current_user.id, provider_id)
    models = provider_service.fetch_models(row)
    return ApiResponse.ok(
        ModelListResult(provider_id=row.id, models=models, count=len(models)),
        message=f"获取到 {len(models)} 个模型",
    )


@router.post(
    "/test-draft",
    summary="试连未保存的配置",
    response_model=ApiResponse[ProviderTestResult],
)
def test_draft(
    payload: ProviderCreate, current_user: CurrentUser
) -> ApiResponse[ProviderTestResult]:
    """测试一份**还没入库**的配置。

    界面上「先点测试、通过再保存」的流程靠这个接口实现 ——
    只差一步就能避免把写错的配置存进数据库。

    该接口不写任何数据，只做一次真实调用。
    """
    result = provider_service.test_raw_config(
        provider_type=payload.provider_type,
        base_url=payload.base_url,
        api_key=payload.api_key,
        model_name=payload.model_name,
        context_window=payload.context_window,
        generation=payload.generation,
    )
    # 未入库的测试没有持久化时间，这里用当前时间返回给前端展示
    from datetime import datetime

    return ApiResponse.ok(
        ProviderTestResult(
            ok=result.ok,
            provider_type=result.provider_type,
            model=result.model,
            latency_ms=result.latency_ms,
            message=result.message,
            detail=result.detail,
            tested_at=datetime.now(),
        ),
        message="试连完成" if result.ok else "试连未通过",
    )
