"""系统诊断接口。

这些接口不承载业务逻辑，只负责回答两个问题：
    1. **「各个组件到底通不通？」**
    2. **「厂商到底听不听我们的话？」**

用途：
  · 开发调试时快速定位问题出在哪一环
  · 部署验收时确认环境是否就绪
  · 答辩演示时直观展示向量记忆、异构适配等核心能力确实在工作
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.exceptions import ConfigurationError
from app.db.chroma import check_vector_store, run_selftest
from app.db.mysql import check_connection
from app.llm import PROVIDER_TYPES, create_provider
from app.llm.diagnostics import probe_reasoning_effort
from app.schemas.common import ApiResponse

router = APIRouter()


def _default_provider():
    """用 .env 里配置的 DEFAULT_LLM_* 构造一个适配器，供诊断接口使用。

    注意：这只是本地开发/演示用途。正式业务中的模型配置来自
    llm_providers 表（每个用户各自配置），不会走这条路径。
    """
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("HNE_DEFAULT_LLM_BASE_URL", settings.DEFAULT_LLM_BASE_URL),
            ("HNE_DEFAULT_LLM_API_KEY", settings.DEFAULT_LLM_API_KEY),
            ("HNE_DEFAULT_LLM_MODEL", settings.DEFAULT_LLM_MODEL),
        )
        if not value
    ]
    if missing:
        raise ConfigurationError(
            "未配置默认模型，无法执行该诊断",
            detail={
                "missing": missing,
                "suggestion": "请在 .env 中填写 HNE_DEFAULT_LLM_BASE_URL / API_KEY / MODEL",
            },
        )

    return create_provider(
        provider_type="openai_compatible",
        base_url=settings.DEFAULT_LLM_BASE_URL,
        api_key=settings.DEFAULT_LLM_API_KEY,
        model_name=settings.DEFAULT_LLM_MODEL,
        context_window=settings.DEFAULT_LLM_CONTEXT_WINDOW,
        timeout=settings.LLM_REQUEST_TIMEOUT,
        max_retries=1,
    )


# ==================================================================
#  组件连通状态
# ==================================================================
@router.get("/components", summary="各组件连通状态")
def component_status() -> ApiResponse[dict[str, Any]]:
    """一次性返回 MySQL、向量库、嵌入后端的状态。

    与 /health 的区别：/health 是「服务是否活着」的快速探针，
    这里是给人看的详细诊断，会带上版本号、路径、维度等信息。
    """
    return ApiResponse.ok(
        {
            "database": check_connection(),
            "vector_store": check_vector_store(probe=False),
        },
        message="组件状态获取成功",
    )


@router.get("/provider-types", summary="支持的模型协议类型")
def provider_types() -> ApiResponse[dict[str, str]]:
    """返回支持的协议类型，供前端渲染「协议类型」下拉框。"""
    return ApiResponse.ok(PROVIDER_TYPES, message="协议类型列表")


# ==================================================================
#  向量库自检
# ==================================================================
@router.post("/vector-store/selftest", summary="向量库自检（写入 → 语义检索）")
def vector_store_selftest() -> ApiResponse[dict[str, Any]]:
    """端到端验证向量记忆链路。

    流程：写入 4 条中文剧情记忆 → 用**用词不同但语义相近**的问题检索
    → 返回排序结果 → 清理自检集合。

    如果相关记忆排在第一位，说明「文本 → 向量 → 语义检索」这条链路是通的。
    """
    return ApiResponse.ok(run_selftest(), message="向量库自检完成")


# ==================================================================
#  模型参数生效性探测
# ==================================================================
@router.post(
    "/probe-reasoning-effort",
    summary="检测模型是否真的支持「思考强度」",
)
def probe_reasoning() -> ApiResponse[dict[str, Any]]:
    """对比实验：以「尽量关闭思考」和「深度思考」各调用一次，看思考 token 是否真有差异。

    ⚠️ **本接口会真实调用模型两次**，因此界面上应做成用户主动点击的按钮，
    不要在保存配置时自动触发。

    为什么需要它？实测 deepseek-flash 会**接受但不理会** reasoning_effort：
    无论设成 off 还是 high，思考 token 都稳定占总输出的约 70%。
    这种「静默无操作」不报错、不给提示，只能靠对比实验发现。
    """
    provider = _default_provider()
    try:
        return ApiResponse.ok(
            probe_reasoning_effort(provider),
            message="思考强度支持情况探测完成",
        )
    finally:
        provider.close()
