"""嵌入器工厂：按 .env 配置构造对应的嵌入后端。

调用方（app/db/chroma.py）只依赖 BaseEmbedding 抽象，
不需要知道当前用的是本地 ONNX 还是远程 API —— 这就是「可插拔」的含义。
新增一个后端只需要：
    1. 在 app/embeddings/ 下新建一个继承 BaseEmbedding 的类
    2. 在下面的 _BUILDERS 里注册一行
    3. 在 config.py 的 EMBEDDING_BACKEND 里加上新的字面量取值
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.embeddings.base import BaseEmbedding
from app.embeddings.onnx_backend import OnnxDefaultEmbedding


def _build_onnx_default(settings: Settings) -> BaseEmbedding:
    """本地 ONNX 后端。"""
    return OnnxDefaultEmbedding(
        model_name=settings.EMBEDDING_MODEL_NAME,
        dimension=settings.EMBEDDING_DIMENSION,
    )


def _build_api(settings: Settings) -> BaseEmbedding:
    """远程 API 后端。"""
    # 延迟导入：只有真正用到 API 后端时才加载 httpx 相关模块
    from app.embeddings.api_backend import ApiEmbedding

    if not settings.EMBEDDING_API_BASE_URL:
        raise ConfigurationError(
            "EMBEDDING_BACKEND=api 时必须配置 EMBEDDING_API_BASE_URL",
            detail={
                "suggestion": "请在 .env 中填写嵌入服务的地址，例如 https://api.siliconflow.cn/v1",
                "example": {
                    "EMBEDDING_API_BASE_URL": "https://api.siliconflow.cn/v1",
                    "EMBEDDING_API_KEY": "sk-xxxxxxxx",
                    "EMBEDDING_MODEL_NAME": "BAAI/bge-m3",
                    "EMBEDDING_API_DIMENSION": "0",
                },
            },
        )
    if not settings.EMBEDDING_API_KEY:
        raise ConfigurationError(
            "EMBEDDING_BACKEND=api 时必须配置 EMBEDDING_API_KEY",
            detail={"suggestion": "请在 .env 中填写嵌入服务的 API 密钥"},
        )

    return ApiEmbedding(
        base_url=settings.EMBEDDING_API_BASE_URL,
        api_key=settings.EMBEDDING_API_KEY,
        model_name=settings.EMBEDDING_MODEL_NAME,
        # 0 表示首次调用自动探测维度
        dimension=settings.EMBEDDING_API_DIMENSION,
        batch_size=settings.EMBEDDING_BATCH_SIZE,
        timeout=settings.LLM_REQUEST_TIMEOUT,
    )


# 后端名 -> 构造函数。新增后端时在这里注册即可
_BUILDERS = {
    "onnx_default": _build_onnx_default,
    "api": _build_api,
}


def create_embedding(settings: Settings | None = None) -> BaseEmbedding:
    """根据配置创建嵌入器。

    注意：这里只负责「创建」，不负责「加载模型」。
    加载模型 / 探测维度由调用方在合适的时机调用 ensure_ready() 触发，
    这样可以把耗时操作的时机控制在我们手里。
    """
    settings = settings or get_settings()
    builder = _BUILDERS.get(settings.EMBEDDING_BACKEND)

    if builder is None:
        raise ConfigurationError(
            f"未知的嵌入后端: {settings.EMBEDDING_BACKEND}",
            detail={"supported": sorted(_BUILDERS.keys())},
        )

    return builder(settings)
