"""带重试的 HTTP 客户端封装（各适配器共用）。

==================== 为什么要重试？====================
调用外部大模型服务时，以下情况是**暂时性**的，重试一次往往就好了：

    · 网络抖动导致的连接失败
    · 触发限流（HTTP 429）
    · 厂商服务端偶发 5xx

而下面这些重试再多次也没用，必须立刻失败并告诉用户去修：

    · 401/403 鉴权失败（Key 错了）
    · 404 模型不存在
    · 400 参数错误

所以重试策略的核心是「**只重试值得重试的错误**」，而不是无脑重试。
无脑重试还会放大对方的限流，甚至让用户的余额被白白消耗。

==================== 退避策略 ====================
每次重试的等待时间翻倍（指数退避）：
    第 1 次失败等 1 秒，第 2 次等 2 秒，第 3 次等 4 秒……
避免在对方正忙时以固定频率持续冲击。
"""

from __future__ import annotations

import random
import time
from typing import Any, Final

import httpx
from loguru import logger

# 值得重试的 HTTP 状态码
RETRYABLE_STATUS: Final[frozenset[int]] = frozenset(
    {
        408,  # Request Timeout
        409,  # Conflict（部分厂商用它表示并发冲突）
        425,  # Too Early
        429,  # Too Many Requests（限流）
        500,  # Internal Server Error
        502,  # Bad Gateway
        503,  # Service Unavailable
        504,  # Gateway Timeout
        529,  # Anthropic 特有的 Overloaded
    }
)

# 基础退避时间（秒）
_BASE_BACKOFF: Final[float] = 1.0
# 最大退避时间，避免等待过久
_MAX_BACKOFF: Final[float] = 8.0


def build_client(timeout: int, *, connect_timeout: int = 15) -> httpx.Client:
    """创建一个配置好的同步 HTTP 客户端。

    超时被拆成三段，比笼统的「总超时」更精确：
        connect  建立 TCP/TLS 连接的超时（连不上要快速失败）
        read     两次数据到达之间的最长间隔
                 ★ 对大模型来说这个值要足够大：模型思考 60 秒才吐第一个字是常事
        write    发送请求体的超时
        pool     从连接池取空闲连接的超时
    """
    return httpx.Client(
        timeout=httpx.Timeout(
            timeout,           # 默认值（同时也是 read 超时）
            connect=connect_timeout,
            read=timeout,
            write=30.0,
            pool=10.0,
        ),
        # 跟随重定向（部分厂商的网关会做跳转）
        follow_redirects=True,
        # 限制连接池规模，避免把对方打爆
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


def is_retryable_exception(exc: BaseException) -> bool:
    """判断网络异常是否值得重试。"""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True
    if isinstance(exc, httpx.RemoteProtocolError):
        # 服务端提前断开连接，通常是瞬时问题
        return True
    return False


def backoff_seconds(attempt: int) -> float:
    """第 attempt 次重试前应等待的秒数（attempt 从 1 开始）。

    加入随机抖动（jitter）：如果多个客户端同时失败，固定间隔会让它们
    在同一时刻一起重试，形成「惊群」。随机抖动可以把它们错开。
    """
    delay = min(_BASE_BACKOFF * (2 ** (attempt - 1)), _MAX_BACKOFF)
    # 在 0.5~1.0 倍之间随机浮动
    return delay * (0.5 + random.random() * 0.5)


def post_with_retry(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    max_retries: int,
    provider_label: str,
) -> httpx.Response:
    """发送 POST 请求，遇到可重试的错误自动重试。

    返回最后一次的响应（可能是失败响应，由调用方解析状态码）。

    注意：**只对非流式请求使用本函数**。
    流式请求一旦开始输出内容就不能重试（会导致内容重复），
    因此流式路径用的是另一套「只在建立连接阶段重试」的逻辑。
    """
    last_exception: BaseException | None = None

    # 尝试次数 = 首次 + 重试次数
    for attempt in range(1, max_retries + 2):
        try:
            response = client.post(url, headers=headers, json=payload)

            # 状态码可重试且还有重试机会 → 等待后重试
            if response.status_code in RETRYABLE_STATUS and attempt <= max_retries:
                wait = backoff_seconds(attempt)
                logger.warning(
                    "调用 {} 返回 HTTP {}，{:.1f} 秒后重试（第 {}/{} 次）",
                    provider_label,
                    response.status_code,
                    wait,
                    attempt,
                    max_retries,
                )
                time.sleep(wait)
                continue

            # 成功，或不可重试的失败 → 直接返回由调用方处理
            return response

        except BaseException as exc:  # noqa: BLE001 - 需要分类处理后再抛出
            if not is_retryable_exception(exc) or attempt > max_retries:
                raise

            last_exception = exc
            wait = backoff_seconds(attempt)
            logger.warning(
                "调用 {} 出现网络异常 {}，{:.1f} 秒后重试（第 {}/{} 次）",
                provider_label,
                type(exc).__name__,
                wait,
                attempt,
                max_retries,
            )
            time.sleep(wait)

    # 理论上来不到这里，防御性兜底
    if last_exception is not None:
        raise last_exception
    raise RuntimeError("post_with_retry 意外走到末尾，这是不应发生的状态")
