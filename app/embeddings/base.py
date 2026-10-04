"""嵌入器抽象基类与「嵌入指纹」。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EmbeddingFingerprint:
    """嵌入空间的「指纹」：后端 + 模型 + 维度。

    frozen=True 表示这是一个不可变对象，可以安全地用作字典键或做相等比较。
    两个指纹相等 => 两个嵌入器产生的向量在同一语义空间，可以互相比较。
    """

    backend: str
    model: str
    dimension: int

    # 存进 ChromaDB 集合元数据时使用的键名前缀
    # （加前缀是为了避免与业务元数据（如 session_id）重名冲突）
    KEY_BACKEND = "embed_backend"
    KEY_MODEL = "embed_model"
    KEY_DIMENSION = "embed_dimension"

    def to_metadata(self) -> dict[str, Any]:
        """转成可写入 ChromaDB 集合元数据的键值对。

        ChromaDB 的元数据只接受 str / int / float / bool 这类标量，
        所以这里统一拍平成标量。
        """
        return {
            self.KEY_BACKEND: self.backend,
            self.KEY_MODEL: self.model,
            self.KEY_DIMENSION: int(self.dimension),
        }

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any] | None) -> "EmbeddingFingerprint | None":
        """从集合元数据中还原指纹。信息不全时返回 None（视为「无指纹」）。"""
        if not metadata:
            return None
        backend = metadata.get(cls.KEY_BACKEND)
        model = metadata.get(cls.KEY_MODEL)
        dimension = metadata.get(cls.KEY_DIMENSION)
        if backend is None or model is None or dimension is None:
            return None
        return cls(backend=str(backend), model=str(model), dimension=int(dimension))

    def describe(self) -> str:
        """人类可读的描述，用于日志和错误信息。"""
        return f"{self.backend}:{self.model}({self.dimension}维)"


class BaseEmbedding(ABC):
    """嵌入器抽象基类。

    子类只需要实现三个方法：
        ensure_ready()      让嵌入器进入可用状态
        embed_documents()   批量把文档转成向量（写入向量库时用）
        embed_query()       把查询文本转成向量（检索时用）
    """

    #: 后端标识，会写进集合指纹。子类必须覆盖。
    backend_name: str = "base"

    def __init__(self, model_name: str, dimension: int = 0) -> None:
        self._model_name = model_name
        # 0 表示"维度尚未确定"，子类应在 ensure_ready() 后把真实维度写进来
        self._dimension = int(dimension)

    # -------------------- 只读属性 --------------------
    @property
    def model_name(self) -> str:
        """模型名，例如 all-MiniLM-L6-v2。"""
        return self._model_name

    @property
    def dimension(self) -> int:
        """向量维度。0 表示尚未探测。"""
        return self._dimension

    @property
    def fingerprint(self) -> EmbeddingFingerprint:
        """当前嵌入器的指纹。"""
        return EmbeddingFingerprint(
            backend=self.backend_name, model=self._model_name, dimension=self._dimension
        )

    @property
    def is_ready(self) -> bool:
        """嵌入器是否已就绪。

        子类应覆盖：本地后端以「模型是否已加载」为准，远程后端以「维度是否已知」为准。
        """
        return self._dimension > 0

    # -------------------- 需要子类实现 --------------------
    @abstractmethod
    def ensure_ready(self) -> None:
        """确保嵌入器可用（幂等，可重复调用）。

        为什么要把「就绪」做成显式的一步？
        因为本地后端加载模型、远程后端探测维度都是耗时或有副作用的操作。
        显式调用之后，调用方才能拿到确定的维度，从而正确地创建 / 校验集合指纹。
        """

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """批量把文本转成向量（写入向量库时使用）。"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """把查询文本转成向量（检索时使用）。

        注意：这里**刻意**与 embed_documents 分成两个方法，而不是共用一个。
        因为部分嵌入模型对「文档」和「查询」要求使用不同的前缀或指令，
        例如 BGE 系列中文模型建议查询侧加
        「为这个句子生成表示以用于检索相关文章：」。
        保留两个入口，将来接入这类模型时不需要改动调用方代码。
        """
        raise NotImplementedError

    # -------------------- 通用能力 --------------------
    def health(self) -> dict[str, Any]:
        """返回嵌入器状态，供健康检查接口使用。"""
        return {
            "status": "ok" if self.is_ready else "not_ready",
            "backend": self.backend_name,
            "model": self._model_name,
            "dimension": self._dimension,
        }

    def close(self) -> None:
        """释放嵌入器占用的资源（例如 HTTP 连接池）。

        默认什么都不做；本地后端无需释放，远程后端应覆盖此方法。
        """
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.fingerprint.describe()}>"
