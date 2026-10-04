"""统一的异常体系 —— 把各厂商五花八门的错误归一化。

==================== 为什么需要它？====================
同一个「API Key 不对」，各家的表达方式完全不同：

    OpenAI 兼容:  HTTP 401  {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}}
    Anthropic:    HTTP 401  {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
    某些国产厂商:  HTTP 200  但响应体里塞了一个 {"code": 40101, "msg": "鉴权失败"}   ← 最坑的一种

如果让上层业务去判断这些差异，代码会变得不可维护。
本模块把它们全部映射成下面这几个语义化异常，上层只需：

    try:
        result = provider.chat(request)
    except LLMAuthError:
        # 提示用户去检查自己的 API Key
    except LLMQuotaError:
        # 提示用户余额不足或触发限流，稍后重试
    except LLMTimeoutError:
        # 提示网络超时

============================ 与全局异常处理器的关系 ============================
这些异常都继承自 app.core.exceptions.LLMProviderError，
因此会被全局异常处理器统一转换成 HTTP 502 + 统一 JSON 结构，
同时带上各自独立的 code，前端据此可以给出精准的提示文案。
"""

from __future__ import annotations

import re
from typing import Any

from app.core.exceptions import LLMProviderError


# ==================================================================
#  语义化异常
# ==================================================================
class LLMAuthError(LLMProviderError):
    """鉴权失败：API Key 错误、过期、未授权。

    前端应提示用户「请检查你填写的 API Key」。
    """

    code = "LLM_AUTH_ERROR"
    message = "大模型服务鉴权失败，请检查 API Key 是否正确"


class LLMQuotaError(LLMProviderError):
    """额度/限流问题：余额不足、触发速率限制、并发超限。

    这类错误通常是**可重试**的（限流）或需要用户充值（欠费）。
    """

    code = "LLM_QUOTA_ERROR"
    message = "大模型服务额度不足或触发限流"


class LLMModelNotFoundError(LLMProviderError):
    """模型不存在或该账号无权访问。"""

    code = "LLM_MODEL_NOT_FOUND"
    message = "指定的模型不存在，或当前 API Key 无权访问该模型"


class LLMTimeoutError(LLMProviderError):
    """请求超时（连接超时或读取超时）。"""

    code = "LLM_TIMEOUT"
    message = "大模型服务响应超时"


class LLMConnectionError(LLMProviderError):
    """网络层面无法连通：DNS 解析失败、连接被拒绝、TLS 握手失败等。"""

    code = "LLM_CONNECTION_ERROR"
    message = "无法连接到大模型服务，请检查 Base URL 与网络"


class LLMBadRequestError(LLMProviderError):
    """请求参数有误：上下文超长、参数非法、模型不支持某能力。"""

    code = "LLM_BAD_REQUEST"
    message = "请求大模型服务的参数有误"


class LLMUpstreamError(LLMProviderError):
    """厂商服务端自身故障（5xx）。通常可重试。"""

    code = "LLM_UPSTREAM_ERROR"
    message = "大模型服务端异常"


# 把 HTTP 状态码映射到异常类
_STATUS_MAP: dict[int, type[LLMProviderError]] = {
    400: LLMBadRequestError,
    401: LLMAuthError,
    402: LLMQuotaError,       # 部分厂商用 402 表示欠费
    403: LLMAuthError,        # 403 通常是 Key 无权访问该模型
    404: LLMModelNotFoundError,
    408: LLMTimeoutError,
    429: LLMQuotaError,       # 429 = Too Many Requests（限流）
}


# 从错误信息文本里识别语义的关键词表
# 有些厂商返回 200 + 业务错误码，或在 message 里说明原因，只能靠关键词兜底
_MESSAGE_PATTERNS: list[tuple[re.Pattern[str], type[LLMProviderError]]] = [
    (re.compile(r"api[\s_-]?key|authentication|unauthorized|invalid.*token|鉴权|密钥", re.I), LLMAuthError),
    (re.compile(r"quota|rate[\s_-]?limit|too many requests|insufficient|balance|余额|限流|欠费", re.I), LLMQuotaError),
    (re.compile(r"model.*(not.*found|not.*exist|does not exist)|no such model|模型不存在", re.I), LLMModelNotFoundError),
    # Anthropic 的错误分类名「not_found_error」需要单独覆盖（上面那条要求出现 model 字样）
    (re.compile(r"not[\s_-]?found", re.I), LLMModelNotFoundError),
    (re.compile(r"permission", re.I), LLMAuthError),
    (re.compile(r"timeout|timed out|超时", re.I), LLMTimeoutError),
    (re.compile(r"context.*(length|window|token)|too long|maximum context|上下文超长", re.I), LLMBadRequestError),
    # 「过载」属于服务端临时状态，可以重试，不该被归为参数错误
    (re.compile(r"overload", re.I), LLMUpstreamError),
]


def _extract_error_type(body: Any) -> str:
    """取出厂商的**错误分类标识**。

    为什么需要它？有些厂商的 message 是给人看的自然语言，措辞五花八门；
    而错误分类名是机器可读的、取值有限，拿它做关键词匹配可靠得多。
    例如 Anthropic：
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    这里的 "overloaded_error" 比 message 更能说明问题的性质。
    """
    if not isinstance(body, dict):
        return ""

    error = body.get("error")
    if isinstance(error, dict) and error.get("type"):
        return str(error["type"])

    body_type = body.get("type")
    # 顶层的 "type": "error" 只说明这是个错误对象，不含分类信息
    if body_type and body_type != "error":
        return str(body_type)

    return ""


def _guess_by_message(text: str) -> type[LLMProviderError] | None:
    """根据错误文本或错误分类猜测语义。"""
    for pattern, error_cls in _MESSAGE_PATTERNS:
        if pattern.search(text):
            return error_cls
    return None


def _extract_error_message(body: Any) -> str:
    """从各种形态的错误响应体里尽量抽出可读的错误信息。

    兼容三种常见结构：
        {"error": {"message": "..."}}          OpenAI 风格
        {"type": "error", "error": {...}}      Anthropic 风格
        {"code": 401, "msg": "..."}            部分国产厂商风格
    """
    if not isinstance(body, dict):
        return str(body)[:500] if body else ""

    error = body.get("error")
    if isinstance(error, dict):
        for key in ("message", "msg", "detail"):
            if error.get(key):
                return str(error[key])
    if isinstance(error, str) and error:
        return error

    for key in ("message", "msg", "detail", "error_msg"):
        if body.get(key):
            return str(body[key])

    return str(body)[:500]


def normalize_http_error(
    status_code: int,
    body: Any,
    *,
    provider_label: str,
    endpoint: str | None = None,
) -> LLMProviderError:
    """把一次 HTTP 错误响应转换成统一的语义化异常。

    参数：
        status_code      HTTP 状态码
        body            已解析的响应体（dict）或原始文本（str）
        provider_label  形如 "openai_compatible/deepseek-chat"，用于错误定位
        endpoint        请求地址，便于排查

    处理顺序：
        1. 先按 HTTP 状态码判断（最可靠）
        2. 状态码无法判断时，用「错误分类 + 错误文本」里的关键词兜底
        3. 都判断不出来就归为「参数错误」
    """
    message = _extract_error_message(body)
    error_type = _extract_error_type(body)
    # 关键词猜测时把「错误分类」也纳入搜索范围：
    # Anthropic 的 overloaded_error / permission_error 等分类名，比自然语言的 message 更可靠
    search_text = f"{error_type} {message}".strip()

    error_cls = _STATUS_MAP.get(status_code)
    if error_cls is None:
        if status_code >= 500:
            error_cls = LLMUpstreamError
        else:
            # 4xx 里没覆盖到的、以及「HTTP 200 但业务失败」的情况，
            # 先靠文本猜，猜不出来就当作参数错误
            error_cls = _guess_by_message(search_text) or LLMBadRequestError

    # 详细信息带上足够定位问题的上下文，但**不包含任何密钥**
    detail: dict[str, Any] = {
        "provider": provider_label,
        "http_status": status_code,
        "upstream_message": message or "(厂商未提供错误信息)",
        "raw_body_preview": str(body)[:500],
    }
    if error_type:
        detail["upstream_error_type"] = error_type
    if endpoint:
        detail["endpoint"] = endpoint

    return error_cls(
        f"{error_cls.message}（{provider_label}）",
        detail=detail,
    )


def normalize_exception(
    exc: BaseException,
    *,
    provider_label: str,
    endpoint: str | None = None,
) -> LLMProviderError:
    """把 httpx 层面的网络异常转换成统一的语义化异常。

    区分两类：
        · 连不上（ConnectError / ConnectTimeout / 各种 TimeoutException）→ 网络问题
        · 其它未知异常 → 归为上游异常，避免把底层细节泄露到接口层
    """
    import httpx

    detail: dict[str, Any] = {
        "provider": provider_label,
        "exception_type": type(exc).__name__,
        "reason": str(exc)[:500],
    }
    if endpoint:
        detail["endpoint"] = endpoint

    # 超时类（分层的 TimeoutException 是它们的共同父类）
    if isinstance(exc, httpx.TimeoutException):
        # 连接阶段超时算「连不上」，读取阶段超时算「响应超时」，区分开更有指导意义
        if isinstance(exc, httpx.ConnectTimeout):
            return LLMConnectionError(f"连接大模型服务超时（{provider_label}）", detail=detail)
        return LLMTimeoutError(f"{LLMTimeoutError.message}（{provider_label}）", detail=detail)

    if isinstance(exc, httpx.TransportError):
        return LLMConnectionError(f"{LLMConnectionError.message}（{provider_label}）", detail=detail)

    return LLMUpstreamError(
        f"调用大模型服务时发生未预期错误（{provider_label}）", detail=detail
    )
