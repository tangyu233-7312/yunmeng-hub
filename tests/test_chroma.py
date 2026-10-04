"""3.3 向量库与嵌入后端测试。

覆盖：嵌入器工厂、语义检索、用户隔离、会话过滤、幂等写入、嵌入指纹校验、
      以及 HTTP 诊断接口。

注意：本文件会真实使用本地 ONNX 模型做嵌入推理，属于集成测试。
首次运行需要已缓存模型（本项目已完成下载）；测试结束会自动清理临时集合。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_chroma.py -v
"""

from __future__ import annotations

import json
import random
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.exceptions import VectorStoreError
from app.db.chroma import (
    MemoryRecord,
    add_memories,
    collection_name_for_user,
    delete_user_collection,
    get_chroma_client,
    get_embedding,
    get_user_collection,
    has_user_collection,
    run_selftest,
    search_memories,
)
from app.embeddings import create_embedding
from app.embeddings.onnx_backend import OnnxDefaultEmbedding
from app.main import app


# ==================== 测试夹具 ====================
@pytest.fixture(scope="module", autouse=True)
def ensure_vector_store_ready():
    """整个模块开始前，先把嵌入模型加载好，避免把加载耗时算进每条用例。"""
    embedding = get_embedding()
    embedding.ensure_ready()
    yield


@pytest.fixture
def user_id() -> int:
    """生成一个足够大的随机用户 ID，避免与真实数据冲突；用例结束自动清理集合。"""
    uid = random.randint(1_000_000_000, 2_000_000_000)
    yield uid
    delete_user_collection(uid)


# ==================== 嵌入器工厂 ====================
def test_factory_builds_onnx_backend_by_default() -> None:
    """默认配置应产出本地 ONNX 嵌入器。"""
    embedding = create_embedding(get_settings())
    assert isinstance(embedding, OnnxDefaultEmbedding)
    assert embedding.backend_name == "onnx_default"


def test_onnx_dimension_is_384() -> None:
    """内置模型固定输出 384 维。"""
    embedding = create_embedding(get_settings())
    embedding.ensure_ready()
    assert embedding.dimension == 384
    assert embedding.is_ready is True


def test_embedding_returns_correct_shape() -> None:
    """每条文本都应得到等长的向量，且数量与输入一致。"""
    embedding = get_embedding()
    vectors = embedding.embed_documents(["第一条文本", "second text", "第三条"])
    assert len(vectors) == 3
    assert all(len(vector) == 384 for vector in vectors)
    # 不同文本的向量必须不同，否则说明模型没在真正工作
    assert vectors[0] != vectors[1]


def test_embedding_of_empty_input() -> None:
    """空输入应返回空列表，而不是抛异常。"""
    assert get_embedding().embed_documents([]) == []


# ==================== 语义检索 ====================
def test_semantic_search_finds_memory_with_different_wording(user_id: int) -> None:
    """★ 核心能力验证：查询用词与原文完全不同，仍应命中正确的记忆。

    这是「向量语义检索」区别于「关键词匹配」的关键证据。
    """
    add_memories(
        user_id,
        [
            MemoryRecord(text="勇者亚瑟在幽暗森林里遇到了一只会说话的银色狐狸。", session_id=1),
            MemoryRecord(text="王国的北境常年被冰雪覆盖，村民靠狩猎为生。", session_id=1),
            MemoryRecord(text="酒馆老板说山那边的废墟底下埋着一批宝藏。", session_id=1),
        ],
    )

    hits = search_memories(user_id, "森林里那只银色的动物说了什么？", top_k=3)

    assert hits, "检索不应返回空结果"
    # 最相关的那条必须排第一
    assert "银色狐狸" in hits[0].text
    # 相似度应该是正数（余弦距离 < 1）
    assert hits[0].similarity > 0.5


def test_search_on_user_without_memories_returns_empty() -> None:
    """没有记忆的用户检索时应返回空列表，而不是报错或凭空建集合。"""
    uid = random.randint(2_100_000_000, 2_200_000_000)
    assert search_memories(uid, "随便问点什么") == []
    assert has_user_collection(uid) is False


def test_empty_query_returns_empty(user_id: int) -> None:
    """空查询不应触发向量计算。"""
    add_memories(user_id, [MemoryRecord(text="这是一条测试记忆")])
    assert search_memories(user_id, "   ") == []


# ==================== 用户隔离 ====================
def test_collections_are_isolated_between_users() -> None:
    """A 用户的记忆绝不能被 B 用户检索到。"""
    user_a = random.randint(2_200_000_000, 2_300_000_000)
    user_b = random.randint(2_300_000_000, 2_400_000_000)
    try:
        add_memories(user_a, [MemoryRecord(text="A 用户的秘密：他把钥匙藏在了花瓶下面。")])

        # B 用户没有写入任何记忆，应当检索不到 A 的内容
        assert search_memories(user_b, "钥匙藏在哪里？") == []

        # A 用户自己可以检索到
        assert search_memories(user_a, "钥匙藏在哪里？")
    finally:
        delete_user_collection(user_a)
        delete_user_collection(user_b)


def test_session_filter_limits_search_scope(user_id: int) -> None:
    """session_id 过滤应把检索范围限制在指定会话内。"""
    add_memories(
        user_id,
        [
            MemoryRecord(text="梅林患有严重的花粉过敏，春天从不出门。", session_id=1),
            MemoryRecord(text="梅林在第七纪元担任宫廷首席法师。", session_id=2),
        ],
    )

    only_session_2 = search_memories(user_id, "梅林的过往经历", top_k=5, session_id=2)
    assert only_session_2
    assert all(hit.metadata["session_id"] == 2 for hit in only_session_2)


def test_kind_filter(user_id: int) -> None:
    """kind 过滤应只返回指定类型的记忆。"""
    add_memories(
        user_id,
        [
            MemoryRecord(text="亚瑟的佩剑叫黎明之刃。", kind="fact"),
            MemoryRecord(text="亚瑟说：我们明天出发。", kind="dialogue"),
        ],
    )
    facts = search_memories(user_id, "亚瑟的剑", top_k=5, kind="fact")
    assert facts
    assert all(hit.metadata["kind"] == "fact" for hit in facts)


# ==================== 幂等写入 ====================
def test_upsert_same_id_does_not_duplicate(user_id: int) -> None:
    """同一个 memory_id 重复写入应覆盖而不是新增。"""
    record = MemoryRecord(text="这条记忆会被改写。", memory_id="fixed-id-001")
    add_memories(user_id, [record])
    collection = get_user_collection(user_id, create=False)
    count_after_first = collection.count()

    # 换一个 memory_id，再写一条，确认计数确实会增长（排除"根本没写进去"的可能）
    add_memories(user_id, [MemoryRecord(text="这条是新的。")])
    assert collection.count() == count_after_first + 1

    # 用相同 ID 覆盖写入，计数不应增长
    add_memories(user_id, [MemoryRecord(text="这条记忆被改写了。", memory_id="fixed-id-001")])
    assert collection.count() == count_after_first + 1


# ==================== 嵌入指纹校验 ====================
def test_fingerprint_mismatch_is_detected(user_id: int) -> None:
    """★ 集合的嵌入指纹与当前配置不符时，必须报错而不是继续用。

    这防止了「换了嵌入模型后，新旧向量混在一起比较」这种静默错误。
    """
    client = get_chroma_client()
    name = collection_name_for_user(user_id)
    if name in {c.name for c in client.list_collections()}:
        client.delete_collection(name=name)

    # 手工造一个「用 bge-m3 / 1024 维」建立的集合
    client.create_collection(
        name=name,
        embedding_function=None,
        metadata={
            "embed_backend": "api",
            "embed_model": "BAAI/bge-m3",
            "embed_dimension": 1024,
        },
    )

    from app.db.chroma import get_user_collection

    with pytest.raises(VectorStoreError) as exc_info:
        get_user_collection(user_id)
    # 错误信息里要能看出「哪两个指纹冲突了」
    detail = exc_info.value.detail
    assert detail["stored_fingerprint"] == "api:BAAI/bge-m3(1024维)"
    assert detail["current_fingerprint"] == "onnx_default:all-MiniLM-L6-v2(384维)"


def test_collection_records_fingerprint(user_id: int) -> None:
    """正常创建的集合，元数据里应写入当前嵌入指纹。"""
    from app.db.chroma import get_user_collection

    collection = get_user_collection(user_id)
    assert collection.metadata["embed_backend"] == "onnx_default"
    assert collection.metadata["embed_model"] == "all-MiniLM-L6-v2"
    assert collection.metadata["embed_dimension"] == 384


def test_collection_uses_cosine_space(user_id: int) -> None:
    """集合应使用余弦距离（文本检索的通用选择），而不是默认的平方欧氏距离。"""
    from app.db.chroma import get_user_collection

    collection = get_user_collection(user_id)
    assert collection.configuration["hnsw"]["space"] == "cosine"


# ==================== 一键自检 ====================
def test_run_selftest_cleans_up() -> None:
    """自检应返回结果，并且不留下自检集合。"""
    settings = get_settings()
    result = run_selftest()

    assert result["status"] == "ok"
    assert result["written"] == 4
    assert len(result["queries"]) == 2
    # 每个查询都应返回排好序的结果
    for query in result["queries"]:
        assert query["hits"]
        assert query["hits"][0]["rank"] == 1
        # 相似度应从高到低排列
        similarities = [hit["similarity"] for hit in query["hits"]]
        assert similarities == sorted(similarities, reverse=True)

    # 自检集合必须被清理掉
    leftover = f"{settings.CHROMA_COLLECTION_PREFIX}_selftest"
    assert leftover not in {c.name for c in get_chroma_client().list_collections()}


# ==================== HTTP 接口 ====================
def test_system_components_endpoint() -> None:
    """/api/v1/system/components 应同时报告数据库与向量库状态。"""
    with TestClient(app) as client:
        response = client.get("/api/v1/system/components")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "OK"
    assert body["data"]["database"]["status"] == "ok"
    assert body["data"]["vector_store"]["status"] == "ok"
    assert body["data"]["vector_store"]["embedding"]["dimension"] == 384


def test_vector_store_selftest_endpoint() -> None:
    """自检接口应返回可读的语义检索结果，且不泄露 API Key。"""
    with TestClient(app) as client:
        response = client.post("/api/v1/system/vector-store/selftest")

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "OK"
    data = body["data"]
    assert data["status"] == "ok"

    first_query = data["queries"][0]
    assert first_query["hits"][0]["rank"] == 1
    # 回传的嵌入信息里不应出现明文密钥字段
    assert "api_key" not in str(data["embedding"]).lower()


def test_health_reports_vector_store() -> None:
    """/health 的 vector_store 组件应已从 not_initialized 变为真实状态。"""
    with TestClient(app) as client:
        body = client.get("/health").json()

    vector_store = body["components"]["vector_store"]
    assert vector_store["status"] == "ok"
    assert vector_store["detail"]["distance_space"] == "cosine"
    assert vector_store["detail"]["embedding"]["backend"] == "onnx_default"


# ==================================================================
#  九、远程 API 嵌入后端（此前一直没被真正执行过的路径）
# ==================================================================
def test_api_embedding_creates_http_client_eagerly() -> None:
    """★ 回归测试：HTTP 客户端必须在**构造时**就创建，不能懒加载。

    ==================== 为什么这条断言很重要？====================
    曾经这里用的是「懒加载 + 加锁」写法：

        def _get_client(self):
            if self._client is None:
                with self._lock:          # ← 再次获取同一把锁
                    self._client = httpx.Client(...)

    而调用链是：

        ensure_ready()  ->  with self._lock:  ->  _request()  ->  _get_client()

    `threading.Lock` 非重入，同一线程二次 acquire 会**永久阻塞**。
    该死锁只在「首次探测嵌入维度」时触发，进程静默卡死、没有任何日志。

    因为项目一直使用 onnx_default 后端，这条路径从未被执行，问题被完整掩盖。

    构造 httpx.Client 不会发起任何网络请求，所以提前创建零成本，
    却彻底消除了这条加锁路径。这条断言就是这个设计决定的守卫。
    """
    from app.embeddings.api_backend import ApiEmbedding

    embedding = ApiEmbedding(
        base_url="https://api.example.com/v1", api_key="sk-x", model_name="bge-m3"
    )
    try:
        assert embedding._client is not None, (
            "HTTP 客户端必须是构造时就创建好的 —— 改成懒加载会引入嵌套加锁死锁"
        )
    finally:
        embedding.close()


def _install_mock_embedding_client(embedding: Any, handler: Any) -> None:
    """给 ApiEmbedding 换上一个「不会真的联网」的 HTTP 客户端。"""
    embedding._client.close()
    embedding._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=10)


def test_api_embedding_probes_dimension_on_first_use() -> None:
    """首次使用时自动探测向量维度，且后续调用是幂等的。"""
    from app.embeddings.api_backend import ApiEmbedding

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        payload = json.loads(request.content)
        count = len(payload["input"])
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [0.1] * 8} for index in range(count)
                ]
            },
        )

    embedding = ApiEmbedding(
        base_url="https://api.example.com/v1", api_key="sk-x", model_name="bge-m3"
    )
    _install_mock_embedding_client(embedding, handler)
    try:
        embedding.ensure_ready()
        assert embedding.dimension == 8
        assert embedding.is_ready is True

        embedding.ensure_ready()  # 再调一次应当是幂等的，不重复请求
        assert calls["count"] == 1
    finally:
        embedding.close()


def test_api_embedding_ensure_ready_does_not_deadlock() -> None:
    """★ 在带超时的后台线程里跑 ensure_ready，防止将来重新引入嵌套加锁。

    放在线程里执行是刻意的：如果真的死锁，直接在主线程调用会让整个测试进程卡住，
    连失败信息都拿不到 —— 那就失去回归测试的意义了。
    """
    import threading

    from app.embeddings.api_backend import ApiEmbedding

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.1] * 16}]}
        )

    embedding = ApiEmbedding(
        base_url="https://api.example.com/v1", api_key="sk-x", model_name="bge-m3"
    )
    _install_mock_embedding_client(embedding, handler)

    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            embedding.ensure_ready()
            outcome["dimension"] = embedding.dimension
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=10)

    try:
        assert not thread.is_alive(), (
            "ApiEmbedding.ensure_ready() 死锁了："
            "检查是否存在「持锁时又去获取同一把锁」的调用链"
        )
        assert outcome.get("dimension") == 16, outcome.get("error")
    finally:
        embedding.close()
