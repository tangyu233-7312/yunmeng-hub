"""ChromaDB 向量库客户端与集合管理。

==================== ChromaDB 是做什么的？====================
它是一个「向量数据库」：专门存储高维向量，并支持「相似度检索」。

本项目用它实现叙事引擎的**长期记忆**：
把过去的剧情片段转成向量存起来；当用户聊到相关话题时，
按**语义**（而不是关键词）把最相关的记忆片段捞出来，再喂给大模型。
例如用户问「那只银色的动物说了什么？」，即使用词与原文完全不同，
也能召回「……遇到了一只会说话的银色狐狸」这条记忆。

==================== 为什么用 PersistentClient？====================
ChromaDB 提供三种客户端：
    · EphemeralClient   纯内存模式，进程退出数据即丢失（只适合临时测试）
    · PersistentClient  本地文件持久化，数据落在磁盘目录（★ 本项目选用）
    · HttpClient        连接独立的 Chroma 服务端（部署更重，当前用不上）

==================== ★★ 一个实测发现的坑（非常重要）====================
ChromaDB 允许给集合绑定一个 embedding_function，由它自动把文本转成向量。
但我们的嵌入后端是「可插拔」的（本地 ONNX / 远程 API），交给 ChromaDB 托管会带来两个问题：

  1. ChromaDB 会把嵌入函数的配置写进集合元数据。若函数里含 API Key，
     等于把密钥明文存进了向量库。
  2. 实测发现：用 embedding_function=None 创建的集合，**重新打开时 ChromaDB 会
     悄悄塞回一个默认的 ONNX 嵌入函数**。此时若代码某处忘记显式传 embeddings，
     它就会用错误的模型生成向量，与库里已有向量不在同一语义空间
     —— 不报错，但检索结果全错。这是最危险的一类「静默错误」。

因此本项目采取三条铁律：
  · 集合一律以 embedding_function=None 创建
  · 所有 add / query 都由本模块**显式传入向量**，绝不依赖 ChromaDB 自动嵌入
  · 集合元数据里记录「嵌入指纹」，每次打开时校验；嵌入模型被换掉后立刻报错，
    而不是让错误数据继续污染检索结果

==================== 集合（Collection）如何划分？====================
按用户隔离：每个用户一个集合，命名 {前缀}_user_{用户ID}。
  · 优点：天然隔离，删除某个用户的数据只需删一个集合，不会误伤他人
  · 同一用户的不同会话之间，用元数据字段 session_id 过滤来区分
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import chromadb
from loguru import logger

from app.core.config import get_settings
from app.core.exceptions import VectorStoreError
from app.db.base import Base  # noqa: F401  (保持数据层导入一致性，无实际用途)
from app.embeddings import BaseEmbedding, EmbeddingFingerprint, create_embedding

# 集合使用的距离度量。文本嵌入一律推荐余弦距离：
# 它只关心向量方向（语义），不受向量长度影响，是文本检索的事实标准。
# 余弦距离 = 1 - 余弦相似度，取值范围 [0, 2]，越小越相似。
_HNSW_SPACE = "cosine"

# 模块级单例
_client: Any = None
_embedding: BaseEmbedding | None = None

_init_lock = threading.Lock()


# ==================================================================
#  单例管理
# ==================================================================
def get_chroma_client() -> Any:
    """获取全局唯一的 ChromaDB 持久化客户端。"""
    global _client
    if _client is None:
        with _init_lock:
            if _client is None:
                settings = get_settings()
                # 确保落地目录存在，否则 ChromaDB 会在首次写入时报错
                settings.chroma_dir.mkdir(parents=True, exist_ok=True)
                logger.info("初始化 ChromaDB 持久化客户端 | 目录: {}", settings.chroma_dir)
                _client = chromadb.PersistentClient(path=str(settings.chroma_dir))
    return _client


def get_embedding() -> BaseEmbedding:
    """获取全局唯一的嵌入器（由 .env 的 EMBEDDING_BACKEND 决定具体实现）。"""
    global _embedding
    if _embedding is None:
        with _init_lock:
            if _embedding is None:
                _embedding = create_embedding(get_settings())
                logger.info("嵌入后端已选定: {}", _embedding.fingerprint.describe())
    return _embedding


def dispose_chroma() -> None:
    """释放向量库相关资源。应用关闭时调用。"""
    global _client, _embedding
    if _embedding is not None:
        # 远程后端会在这里关闭 HTTP 连接池
        _embedding.close()
    _client = None
    _embedding = None
    logger.debug("ChromaDB 客户端与嵌入器已释放")


# ==================================================================
#  集合管理
# ==================================================================
def collection_name_for_user(user_id: int) -> str:
    """用户集合的命名规则。

    ChromaDB 对集合名有约束（长度 3~63、只能包含字母数字和 _-.），
    因此这里不拼入用户名等可能含中文/特殊字符的内容，只用纯数字 ID。
    """
    prefix = get_settings().CHROMA_COLLECTION_PREFIX
    return f"{prefix}_user_{user_id}"


def _collection_names() -> set[str]:
    """列出当前所有集合名。

    兼容不同版本：老版本 list_collections() 返回字符串列表，
    1.x 返回 Collection 对象列表。
    """
    result = get_chroma_client().list_collections()
    names: set[str] = set()
    for item in result:
        names.add(item if isinstance(item, str) else item.name)
    return names


def has_user_collection(user_id: int) -> bool:
    """判断某用户是否已经有记忆集合（用于检索前判断，避免无谓的建集合）。"""
    return collection_name_for_user(user_id) in _collection_names()


def _verify_fingerprint(collection: Any, expected: EmbeddingFingerprint) -> None:
    """校验集合的嵌入指纹与当前配置是否一致。

    为什么要校验？不同模型的向量不可比较。如果用户中途把 EMBEDDING_BACKEND
    从 onnx_default 换成了 api，旧集合里的向量是 384 维 MiniLM，
    新查询向量是 1024 维 bge-m3，算出来的距离毫无意义。
    与其返回莫名其妙的结果，不如直接报错并说清楚怎么修。
    """
    stored = EmbeddingFingerprint.from_metadata(collection.metadata)

    if stored is None:
        # 集合是通过其他途径（如早期版本、手工脚本）建立的，没有指纹信息。
        # 这里补写当前指纹，并留下日志，便于回溯。
        metadata = dict(collection.metadata or {})
        metadata.update(expected.to_metadata())
        collection.modify(metadata=metadata)
        logger.info("集合 {} 缺少嵌入指纹，已补写为 {}", collection.name, expected.describe())
        return

    if stored != expected:
        raise VectorStoreError(
            "集合的嵌入空间与当前配置不一致，继续使用会导致检索结果错乱",
            detail={
                "collection": collection.name,
                "stored_fingerprint": stored.describe(),
                "current_fingerprint": expected.describe(),
                "how_to_fix": (
                    "方案一：把 .env 的 EMBEDDING_BACKEND / EMBEDDING_MODEL_NAME 改回原模型；"
                    "方案二：删除该集合后重建（旧向量需要重新嵌入）"
                ),
            },
        )


def get_user_collection(user_id: int, create: bool = True) -> Any:
    """打开（必要时创建）某个用户的记忆集合。

    参数 create=False 时，若集合不存在则抛出 NotFoundError 风格的错误，
    调用方可用 has_user_collection() 先做判断。
    """
    settings = get_settings()
    client = get_chroma_client()
    embedding = get_embedding()

    # ★ 必须先让嵌入器就绪，才能拿到确定的维度，进而写出正确的指纹
    embedding.ensure_ready()
    fingerprint = embedding.fingerprint

    name = collection_name_for_user(user_id)
    existing = _collection_names()

    if name in existing:
        # 注意：打开已存在的集合时也传 embedding_function=None，
        # 避免 ChromaDB 给它绑定默认嵌入函数（见模块顶部的坑）
        collection = client.get_collection(name=name, embedding_function=None)
    elif create:
        collection = client.create_collection(
            name=name,
            embedding_function=None,
            metadata={
                "description": f"用户 {user_id} 的叙事长期记忆",
                **fingerprint.to_metadata(),
            },
            # 指定余弦距离，而不是默认的平方欧氏距离
            configuration={"hnsw": {"space": _HNSW_SPACE}},
        )
        logger.info("已创建用户记忆集合 {} | 指纹 {}", name, fingerprint.describe())
    else:
        raise VectorStoreError(
            "用户记忆集合不存在", detail={"user_id": user_id, "collection": name}
        )

    # 无论新建还是打开，都校验一次指纹
    _verify_fingerprint(collection, fingerprint)
    return collection


def delete_user_collection(user_id: int) -> bool:
    """删除某用户的记忆集合。返回是否真的删除了。"""
    name = collection_name_for_user(user_id)
    if name not in _collection_names():
        return False
    get_chroma_client().delete_collection(name=name)
    logger.info("已删除用户记忆集合 {}", name)
    return True


# ==================================================================
#  记忆的写入与检索
# ==================================================================
@dataclass
class MemoryRecord:
    """待写入向量库的一条记忆。"""

    text: str
    """记忆正文。"""

    memory_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    """唯一标识。默认随机生成；若传入已存在的 ID 则执行覆盖更新。"""

    session_id: int | None = None
    """所属叙事会话 ID。检索时可以只在本会话内搜索。None 表示全局记忆。"""

    kind: str = "dialogue"
    """记忆类型：dialogue（对话片段）/ summary（剧情摘要）/ fact（设定事实）。"""

    extra: dict[str, Any] = field(default_factory=dict)
    """附加元数据。只支持 str/int/float/bool，复杂结构需自行序列化成字符串。"""


@dataclass
class MemoryHit:
    """检索命中的一条记忆。"""

    memory_id: str
    text: str
    distance: float
    """余弦距离，范围 [0, 2]，越小越相似。"""

    metadata: dict[str, Any]

    @property
    def similarity(self) -> float:
        """把距离换算成更直观的相似度（1 表示完全一致，0 表示无关）。

        余弦距离 d = 1 - cos，所以相似度 = 1 - d。
        """
        return 1.0 - self.distance


def add_memories(user_id: int, records: Sequence[MemoryRecord]) -> int:
    """批量写入（或更新）记忆。返回写入条数。

    使用 upsert 而不是 add：
      · add     遇到已存在的 ID 会报错
      · upsert  存在则覆盖、不存在则新增，天然幂等，重复写入不会产生脏数据
    """
    if not records:
        return 0

    collection = get_user_collection(user_id)
    embedding = get_embedding()

    texts = [record.text for record in records]
    # 这一步就是"把文本变成向量"的地方，也是整个检索质量的决定性环节
    vectors = embedding.embed_documents(texts)

    metadatas: list[dict[str, Any]] = []
    for record in records:
        metadata: dict[str, Any] = {
            # ChromaDB 元数据不接受 None，用 -1 表示"不属于任何会话"
            "session_id": record.session_id if record.session_id is not None else -1,
            "kind": record.kind,
        }
        # 附加元数据必须拍平成标量，否则 ChromaDB 会拒绝写入
        for key, value in record.extra.items():
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
        metadatas.append(metadata)

    collection.upsert(
        ids=[record.memory_id for record in records],
        documents=texts,
        embeddings=vectors,
        metadatas=metadatas,
    )
    logger.debug("用户 {} 写入 {} 条记忆", user_id, len(records))
    return len(records)


def search_memories(
    user_id: int,
    query: str,
    top_k: int = 5,
    session_id: int | None = None,
    kind: str | None = None,
) -> list[MemoryHit]:
    """按语义检索记忆。

    参数：
        query       检索问题
        top_k       最多返回多少条
        session_id  只在该会话内检索（None 表示跨会话检索全部记忆）
        kind        只看某一类记忆（如只检索 summary）
    """
    if not query.strip():
        return []

    # 集合不存在说明该用户还没有任何记忆，直接返回空列表，
    # 不要顺手创建一个空集合（否则会产生大量无意义的空集合）
    if not has_user_collection(user_id):
        return []

    collection = get_user_collection(user_id, create=False)
    embedding = get_embedding()

    # 查询向量与文档向量必须来自同一个嵌入模型，否则比较无意义
    query_vector = embedding.embed_query(query)

    # 构造元数据过滤条件。ChromaDB 用 {"$and": [...]} 组合多个条件。
    conditions: list[dict[str, Any]] = []
    if session_id is not None:
        conditions.append({"session_id": session_id})
    if kind is not None:
        conditions.append({"kind": kind})

    where: dict[str, Any] | None = None
    if len(conditions) == 1:
        where = conditions[0]
    elif len(conditions) > 1:
        where = {"$and": conditions}

    result = collection.query(
        query_embeddings=[query_vector],
        n_results=max(1, top_k),
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    # ChromaDB 的返回是「按查询分组」的，我们只发了一个查询，所以取下标 0
    ids = (result.get("ids") or [[]])[0]
    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]

    hits: list[MemoryHit] = []
    for index, memory_id in enumerate(ids):
        hits.append(
            MemoryHit(
                memory_id=memory_id,
                text=documents[index] if index < len(documents) else "",
                distance=float(distances[index]) if index < len(distances) else 1.0,
                metadata=dict(metadatas[index]) if index < len(metadatas) else {},
            )
        )
    return hits


# ==================================================================
#  健康检查
# ==================================================================
def check_vector_store(probe: bool = False) -> dict[str, Any]:
    """向量库健康检查。

    probe=False  轻量检查：只确认客户端可用、计数集合（适合每次 /health 请求）
    probe=True   真实探测：额外执行一次嵌入推理，验证嵌入后端确实可用
                 （适合应用启动时做一次自检）
    """
    settings = get_settings()
    result: dict[str, Any] = {
        "status": "ok",
        "persist_dir": str(settings.chroma_dir),
        "distance_space": _HNSW_SPACE,
    }

    try:
        client = get_chroma_client()
        result["collection_count"] = len(client.list_collections())
    except Exception as exc:  # noqa: BLE001 - 健康检查必须兜住所有异常
        result["status"] = "error"
        result["message"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        embedding = get_embedding()
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["message"] = f"嵌入后端初始化失败: {type(exc).__name__}: {exc}"
        return result

    result["embedding"] = embedding.health()

    if probe:
        try:
            vector = embedding.embed_query("向量库健康检查探针")
            # ★ 探测成功后要**重新采集一次状态**：
            #   上面的 health() 是在推理发生之前调用的，那时本地模型还没加载，
            #   直接复用会导致 model_loaded 永远显示 false，是典型的「快照过期」错误。
            result["embedding"] = embedding.health()
            result["embedding"]["probe_dimension"] = len(vector)
            result["embedding"]["status"] = "ok"
        except Exception as exc:  # noqa: BLE001
            result["status"] = "error"
            result["message"] = f"嵌入后端不可用: {type(exc).__name__}: {exc}"
            result["embedding"]["status"] = "error"

    return result


# ==================================================================
#  一键自检
# ==================================================================
#: 自检用的中文语料。刻意让「查询词」与「原文用词」不重合，
#: 这样才能证明检索是「按语义」而不是「按关键词」命中的。
_SELFTEST_MEMORIES: list[str] = [
    "勇者亚瑟在幽暗森林深处遇到了一只会说话的银色狐狸。",
    "王国的北境常年被冰雪覆盖，村民靠狩猎和采集为生。",
    "法师梅林警告说，封印着远古恶魔的结界正在逐渐松动。",
    "酒馆老板压低声音告诉我，山那边的废墟底下埋着一批宝藏。",
]

_SELFTEST_QUERIES: list[str] = [
    "森林里那只银色的动物对我说了什么？",  # 期望命中第 1 条
    "哪里可以找到被埋起来的财宝？",          # 期望命中第 4 条
]


def run_selftest() -> dict[str, Any]:
    """端到端自检：写入中文记忆 → 语义检索 → 清理现场。

    这个函数的价值在于：用一次真实调用把「嵌入 → 写入 → 检索」整条链路跑通，
    避免等到业务代码写完才发现某一环有问题。
    """
    settings = get_settings()
    name = f"{settings.CHROMA_COLLECTION_PREFIX}_selftest"
    client = get_chroma_client()
    embedding = get_embedding()
    embedding.ensure_ready()

    # 清理上次自检可能残留的集合，保证每次运行从干净状态开始
    if name in _collection_names():
        client.delete_collection(name=name)

    collection = client.create_collection(
        name=name,
        embedding_function=None,
        metadata={"description": "自检专用集合，可随时删除", **embedding.fingerprint.to_metadata()},
        configuration={"hnsw": {"space": _HNSW_SPACE}},
    )

    try:
        documents = list(_SELFTEST_MEMORIES)
        vectors = embedding.embed_documents(documents)
        collection.upsert(
            ids=[f"selftest-{index}" for index in range(len(documents))],
            documents=documents,
            embeddings=vectors,
            metadatas=[
                {"session_id": 0, "kind": "dialogue"} for _ in documents
            ],
        )

        query_results: list[dict[str, Any]] = []
        for query_text in _SELFTEST_QUERIES:
            query_vector = embedding.embed_query(query_text)
            raw = collection.query(
                query_embeddings=[query_vector],
                n_results=3,
                include=["documents", "distances"],
            )
            hits = [
                {
                    "rank": index + 1,
                    "text": raw["documents"][0][index],
                    "distance": round(float(raw["distances"][0][index]), 4),
                    "similarity": round(1.0 - float(raw["distances"][0][index]), 4),
                }
                for index in range(len(raw["documents"][0]))
            ]
            query_results.append({"query": query_text, "hits": hits})

        return {
            "status": "ok",
            "collection": name,
            "distance_space": _HNSW_SPACE,
            "embedding": embedding.health(),
            "written": len(documents),
            "queries": query_results,
        }
    finally:
        # 无论成功失败，都清理掉自检集合，不污染真实数据
        if name in _collection_names():
            client.delete_collection(name=name)
