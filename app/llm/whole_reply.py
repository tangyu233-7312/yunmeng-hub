"""把"流式适配器"伪装成"整段返回"——给模型配置里的「流式传输」开关用。

==================== 为什么包一层，而不是在 engine 里分叉 ====================
engine 的事件序列（meta / notes / reason / delta / done）与**错误处理**只有一份；
如果在 engine 里为"非流式"再写一遍，就多出一份要同步维护的分支
（本项目已经因为"两条路各写一遍"踩过坑，见 docs/pitfalls.md）。
包一层适配器：`stream_chat` 内部调一次 `chat()`，然后只吐**一个** delta ——
前端因此拿到"没有逐字效果"的回复，而不用改任何逻辑。
"""

from __future__ import annotations

from typing import Any, Iterator

from app.llm.schema import ChatRequest, ChatResult, StreamChunk


class WholeReplyAdapter:
    """`stream_chat` 不再逐字：内部走非流式，一次把整段正文吐出来。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.default_params = getattr(inner, "default_params", None)
        # ★ 是否参与思考强度适配也要透传（包一层之后不能丢）
        self.supports_reasoning_effort = getattr(inner, "supports_reasoning_effort", True)

    @property
    def budget(self):
        return self._inner.budget

    @property
    def effective_model_name(self) -> str:
        # 透传（FailoverAdapter 也有这个属性，链式包装时要能一路问到最里层）
        return str(getattr(self._inner, "effective_model_name", "") or "")

    def chat(self, request: ChatRequest) -> ChatResult:
        return self._inner.chat(request)

    def stream_chat(self, request: ChatRequest) -> Iterator[StreamChunk]:
        result = self._inner.chat(request)
        if result.reasoning:
            # 非流式也能拿到思考内容（ChatResult.reasoning），照旧发给前端
            yield StreamChunk(reasoning_delta=result.reasoning)
        if result.notes:
            yield StreamChunk(notes=list(result.notes))
        if result.content:
            yield StreamChunk(delta=result.content)
        yield StreamChunk(finish_reason=result.finish_reason or "stop", usage=result.usage)

    def close(self) -> None:
        self._inner.close()
