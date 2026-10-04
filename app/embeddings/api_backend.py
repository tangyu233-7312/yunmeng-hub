"""API 嵌入后端 —— 调用用户自配服务的 /embeddings 接口。

==================== 为什么需要它？====================
ChromaDB 内置的 MiniLM 主要面向英文语料，中文语义效果一般。
而国产厂商（通义、智谱、硅基流动、DeepSeek 生态等）都提供了中文效果更好的
嵌入模型，且大多兼容 OpenAI 的 /embeddings 协议。

有了这个后端，用户只要在 .env 里填上自己的地址与密钥，就能把嵌入质量提升一个档次，
而且**不绑定任何特定厂商** —— 这正是本项目「异构」设计的一部分。

支持的典型场景：
    · 官方云服务     https://api.siliconflow.cn/v1        model=BAAI/bge-m3
    · 自建推理服务   http://localhost:8000/v1             model=bge-large-zh
                    （vLLM / Xinference / TEI 等都以 OpenAI 兼容格式暴露 /embeddings）

==================== 协议约定（OpenAI /embeddings）====================
请求：
    POST {base_url}/embeddings
    {"model": "bge-m3", "input": ["文本1", "文本2"]}

响应：
    {"data": [{"index": 0, "embedding": [...]}, {"index": 1, "embedding": [...]}],
     "usage": {"prompt_tokens": 12, "total_tokens": 12}}

★ 注意：协议**不保证** data 数组的顺序与输入顺序一致，所以必须按 `index` 字段
   重新排序。这是一个很容易被忽略、但会导致「向量与文本错位」的坑。
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

import httpx
from loguru import logger

from app.core.exceptions import VectorStoreError
from app.embeddings.base import BaseEmbedding


class ApiEmbedding(BaseEmbedding):
    """调用外部 HTTP 接口做嵌入。"""

    backend_name = "api"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        dimension: int = 0,
        batch_size: int = 64,
        timeout: int = 60,
    ) -> None:
        """
        参数：
            base_url    API 基础地址，需包含版本号，例如 https://api.siliconflow.cn/v1
            api_key     API 密钥
            model_name  嵌入模型名，例如 BAAI/bge-m3
            dimension   向量维度；填 0 表示首次调用时自动探测
            batch_size  单次请求最多嵌入多少条文本
            timeout     单次请求超时秒数
        """
        super().__init__(model_name=model_name, dimension=dimension)
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key.strip()
        self._batch_size = max(1, int(batch_size))
        self._timeout = int(timeout)

        # ==================== ★ 为什么在这里就创建 HTTP 客户端？====================
        # 直觉上「懒加载更省资源」，但这里**必须**提前创建，原因是死锁：
        #
        #   ensure_ready() 会持有 self._lock 去调 _request()，
        #   _request() 内部会调 _get_client()。
        #   如果 _get_client() 采用懒加载 + 加锁，那么它会尝试再次获取
        #   **同一把非重入锁** —— 同一线程二次 acquire 会永久阻塞。
        #   这个死锁只在「首次探测嵌入维度」时触发，进程静默卡死且没有日志。
        #   （与 app/db/mysql.py 里修过的那处是同一类问题）
        #
        # 而构造 httpx.Client **不会发起任何网络请求**（连接是在第一次请求时才建立），
        # 所以提前创建没有任何代价，却彻底消除了这条加锁路径。
        self._client = httpx.Client(
            timeout=self._timeout,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        # 仅为「批量嵌入时避免并发重复创建」等未来可能的用途保留；
        # 注意：任何持锁代码都不要再调用会加同一把锁的函数。
        self._lock = threading.Lock()

    # -------------------- 内部工具 --------------------
    @property
    def endpoint(self) -> str:
        """完整的请求地址。

        容错：如果用户已经把完整路径写进 base_url，就不再重复拼接 /embeddings。
        """
        if self._base_url.endswith("/embeddings"):
            return self._base_url
        return f"{self._base_url}/embeddings"

    def _masked_key(self) -> str:
        """脱敏后的密钥，用于日志与健康检查。

        ★ 安全要求：任何时候都不能把完整 API Key 写进日志或返回给前端。
        """
        if not self._api_key:
            return "(未配置)"
        if len(self._api_key) <= 8:
            return "*" * len(self._api_key)
        return f"{self._api_key[:4]}****{self._api_key[-4:]}"

    def _get_client(self) -> httpx.Client:
        """获取 HTTP 客户端。

        因为客户端在构造时就已创建，这里只做一次非空的防御性检查 ——
        全程不加锁，也就不会再出现「持锁时又去获取同一把锁」的死锁。
        """
        client = self._client
        if client is None:  # pragma: no cover - 仅用于兜底被外部误置空的情况
            raise VectorStoreError(
                "HTTP 客户端不可用（可能已被关闭）",
                detail={"endpoint": self.endpoint},
            )
        return client

    # -------------------- 就绪与释放 --------------------
    @property
    def is_ready(self) -> bool:
        """对远程后端来说，「就绪」= 已经知道向量维度。"""
        return self._dimension > 0

    def ensure_ready(self) -> None:
        """探测向量维度（幂等）。

        为什么要探测？不同厂商的嵌入模型维度不同（bge-m3=1024、
        text-embedding-3-small=1536...），而创建向量集合时必须知道维度。
        这里用一条极短的文本做一次真实请求，把维度探明并缓存下来。
        """
        if self._dimension > 0:
            return

        with self._lock:
            # 抢到锁后再检查一次，避免并发重复探测
            if self._dimension > 0:
                return

            logger.info(
                "正在探测 API 嵌入维度 | endpoint={} model={} key={}",
                self.endpoint,
                self._model_name,
                self._masked_key(),
            )
            vectors = self._request(["dimension-probe"])
            self._dimension = len(vectors[0])
            logger.info("API 嵌入维度探测完成: {} 维", self._dimension)

    def close(self) -> None:
        """关闭 HTTP 连接池。应用退出时调用。"""
        if self._client is not None:
            self._client.close()
            self._client = None

    # -------------------- 真正的请求 --------------------
    def _request(self, texts: Sequence[str]) -> list[list[float]]:
        """向嵌入接口发起一次请求并解析结果。"""
        payload = {"model": self._model_name, "input": list(texts)}

        try:
            response = self._get_client().post(self.endpoint, json=payload)
        except httpx.HTTPError as exc:
            # 网络层面的失败：DNS、连接被拒、超时等
            raise VectorStoreError(
                f"调用嵌入接口失败：{type(exc).__name__}",
                detail={"endpoint": self.endpoint, "model": self._model_name, "reason": str(exc)},
            ) from exc

        if response.status_code != 200:
            # 服务端层面的失败：鉴权、限流、模型不存在等。
            # 截断响应体，避免把超长 HTML 错误页写进日志
            raise VectorStoreError(
                f"嵌入接口返回 HTTP {response.status_code}",
                detail={
                    "endpoint": self.endpoint,
                    "model": self._model_name,
                    "response": response.text[:300],
                },
            )

        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise VectorStoreError(
                "嵌入接口返回的不是合法 JSON", detail={"endpoint": self.endpoint}
            ) from exc

        data = body.get("data")
        if not isinstance(data, list) or not data:
            raise VectorStoreError(
                "嵌入接口返回格式不符合 OpenAI 规范（缺少 data 数组）",
                detail={"endpoint": self.endpoint, "response": str(body)[:300]},
            )

        # ★ 按 index 排序：协议不保证返回顺序与请求顺序一致
        try:
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            vectors = [[float(value) for value in item["embedding"]] for item in ordered]
        except (TypeError, KeyError, ValueError) as exc:
            raise VectorStoreError(
                "嵌入接口返回的向量格式无法解析",
                detail={"endpoint": self.endpoint, "response": str(body)[:300]},
            ) from exc

        # 一致性校验：如果配置里已指定维度，就核对实际返回的维度
        actual_dimension = len(vectors[0])
        if self._dimension > 0 and actual_dimension != self._dimension:
            raise VectorStoreError(
                "嵌入接口返回的维度与配置不一致",
                detail={
                    "configured": self._dimension,
                    "actual": actual_dimension,
                    "suggestion": "请修正 EMBEDDING_API_DIMENSION，或改为 0 让它自动探测",
                },
            )

        return vectors

    # -------------------- 对外接口 --------------------
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入，自动按 batch_size 分批。

        为什么要分批？一次塞几千条文本会让请求体过大，很多服务会直接拒绝（413）。
        """
        if not texts:
            return []

        self.ensure_ready()
        texts = list(texts)
        results: list[list[float]] = []

        for start in range(0, len(texts), self._batch_size):
            chunk = texts[start : start + self._batch_size]
            results.extend(self._request(chunk))

        if len(results) != len(texts):
            raise VectorStoreError(
                "嵌入接口返回的向量数量与输入文本数量不一致",
                detail={"input_count": len(texts), "output_count": len(results)},
            )
        return results

    def embed_query(self, text: str) -> list[float]:
        """单条嵌入。"""
        return self.embed_documents([text])[0]

    def health(self) -> dict[str, Any]:
        """状态信息（密钥已脱敏）。"""
        info = super().health()
        info.update(
            {
                "endpoint": self.endpoint,
                "masked_key": self._masked_key(),
                "batch_size": self._batch_size,
                "dimension_detected": self._dimension > 0,
            }
        )
        return info
