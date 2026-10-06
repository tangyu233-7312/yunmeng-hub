"""主模型失败时自动切到「备用模型」——一层薄薄的适配器包装。

==================== 为什么做成适配器，而不是改 engine ====================
engine 里"流式 / 非流式 / 中断保留"三条路已经够复杂，包一层能让**两条路同时受益**，
而且完全不用碰热点代码。

==================== 两条硬规则 ====================
1. **只在"还没吐出任何内容"时才切**：已经吐了一半再换模型，用户会看到两段
   来自不同模型的文字拼在一起（重试还会让前半段重复）—— 宁可如实报错。
2. **切换必须说出来**：切了备用模型会在流里发一条 notes（"已自动切换到备用模型 X"）。
   本项目对"静默降级"零容忍 —— 用户得知道自己看到的是谁写的。

哪些错误值得切：网络/超时/限流/上游 5xx（换个模型很可能就好了）；
哪些不值得：参数错误、模型名不存在（换一个模型也可能一样，而且用户该去改配置）。
"""

from __future__ import annotations

from typing import Any, Iterator

from loguru import logger

from app.llm.errors import (
    LLMBadRequestError,
    LLMModelNotFoundError,
    LLMProviderError,
)
from app.llm.schema import ChatRequest, ChatResult, StreamChunk

#: 不值得切换的错误：再换一个模型也是白搭，应该让用户去改配置
_NO_FAILOVER = (LLMBadRequestError, LLMModelNotFoundError)

#: 备用模型这条路要不要试：配额/欠费**值得试**（另一个 key 可能有额度）
_WORTH_FAILOVER = LLMProviderError


def _should_failover(exc: BaseException) -> bool:
    if isinstance(exc, _NO_FAILOVER):
        return False
    return isinstance(exc, _WORTH_FAILOVER)


class FailoverAdapter:
    """把「主 + 备」两个适配器包成一个（接口与普通适配器完全一致）。"""

    def __init__(
        self,
        primary: Any,
        fallback: Any,
        *,
        primary_model: str = "",
        fallback_model: str = "",
        primary_name: str = "",
        fallback_name: str = "",
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_model = primary_model
        self._fallback_model = fallback_model
        self.primary_name = primary_name
        self.fallback_name = fallback_name
        #: 本次是否真的用了备用模型（engine 用它决定消息上记哪个模型名）
        self.used_fallback = False
        self.default_params = getattr(primary, "default_params", None)
        # ★ 是否参与思考强度适配也要透传（否则包一层 failover 之后"能不能降思考"就丢了）
        self.supports_reasoning_effort = getattr(primary, "supports_reasoning_effort", True)

    # ---------------- 与普通适配器一致的对外接口 ----------------
    @property
    def budget(self):
        return self._primary.budget

    @property
    def effective_model_name(self) -> str:
        return self._fallback_model if self.used_fallback else self._primary_model

    def chat(self, request: ChatRequest) -> ChatResult:
        try:
            return self._primary.chat(request)
        except BaseException as exc:  # noqa: BLE001 - 要判断类型后再决定抛不抛
            if not _should_failover(exc):
                raise
            logger.warning(
                "主模型调用失败，切换到备用模型 | primary={} fallback={} err={}",
                self.primary_name or self._primary_model,
                self.fallback_name or self._fallback_model,
                exc,
            )
            self.used_fallback = True
            result = self._fallback.chat(request)
            result.notes = list(result.notes) + [self._switched_note(exc)]
            return result

    def stream_chat(self, request: ChatRequest) -> Iterator[StreamChunk]:
        emitted = False
        try:
            for chunk in self._primary.stream_chat(request):
                if chunk.delta or chunk.reasoning_delta:
                    emitted = True
                yield chunk
        except BaseException as exc:  # noqa: BLE001
            # ★ 已经输出过内容就不再切换（否则用户会看到两个模型的文字拼在一起）
            if emitted or not _should_failover(exc):
                raise
            logger.warning(
                "主模型流式失败且尚未输出内容，切换到备用模型 | primary={} fallback={} err={}",
                self.primary_name or self._primary_model,
                self.fallback_name or self._fallback_model,
                exc,
            )
            self.used_fallback = True
            yield StreamChunk(notes=[self._switched_note(exc)])
            yield from self._fallback.stream_chat(request)

    def close(self) -> None:
        for adapter in (self._primary, self._fallback):
            try:
                adapter.close()
            except BaseException:  # noqa: BLE001 - 关连接失败不该影响对话
                logger.debug("关闭适配器时出错（已忽略）")

    def _switched_note(self, exc: BaseException) -> str:
        target = self.fallback_name or self._fallback_model or "备用模型"
        return f"主模型调用失败（{type(exc).__name__}），已自动切换到备用模型「{target}」"
