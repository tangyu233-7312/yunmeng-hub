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

import platform
from typing import Any

from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.core.config import data_root, get_settings
from app.core.exceptions import ConfigurationError
from app.db.chroma import check_vector_store, run_selftest
from app.db.mysql import check_connection
from app.llm import PROVIDER_TYPES, create_provider
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
#  运行环境快照（「关于」弹窗与「复制诊断信息」用）
# ==================================================================
@router.get("/diagnostics", summary="运行环境快照（供关于页与复制诊断信息）")
def diagnostics(current_user: CurrentUser) -> ApiResponse[dict[str, Any]]:
    """返回一份**不含任何密钥或对话内容**的运行环境快照。

    ==================== 为什么需要它 ====================
    网页版控制台**拿不到真实的文件系统路径**（浏览器没有这个能力），
    而"数据/日志目录在哪"恰恰是排查问题时最需要的东西之一 ——
    用户要能自己打开目录、把日志文件发给别人看。
    所以由后端把自己知道的路径如实报出来。

    ==================== 安全边界（重要）====================
    · 只返回**路径与版本**这类环境事实，**不含** API Key、密钥、口令、会话内容；
    · **需要登录**（`CurrentUser`）—— 虽然这几种信息本身不敏感，
      但没有理由让未登录状态也能读到本机的目录结构；
    · 路径里会包含本机用户名（例如 `C:\\Users\\某人\\AppData\\...`）。
      这是**刻意**的：用户要复制去打开目录，路径不能被打码。
    """
    settings = get_settings()
    return ApiResponse.ok(
        {
            "app": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "env": settings.APP_ENV,
            "backend": settings.DB_BACKEND,
            # 形如 "sqlite:…/app.sqlite3" 或 "mysql:127.0.0.1:3306/narrative_engine"
            "database": settings.database_label,
            # ★ `data_root()` 是**模块级函数**（不是 Settings 的属性），
            #   而 log_dir / chroma_dir 是 @property —— 第一版把三者当同一种用，
            #   于是拿到 `AttributeError: 'Settings' object has no attribute 'data_root'`。
            "data_dir": str(data_root()),
            "log_dir": str(settings.log_dir),
            "chroma_dir": str(settings.chroma_dir),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.release()}",
        },
        message="运行环境快照",
    )


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


