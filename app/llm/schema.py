"""统一的消息与请求/响应数据结构。

==================== 设计原则 ====================
这些结构是「协议无关」的：它们既不长得像 OpenAI 的格式，也不像 Anthropic 的格式，
而是本项目自己定义的一套中间表示（IR, Intermediate Representation）。

数据流向：

    业务代码 → ChatRequest（统一）
                    ↓  各适配器负责翻译
              厂商专属请求体
                    ↓  HTTP
              厂商专属响应体
                    ↓  各适配器负责翻译回来
   业务代码 ← ChatResult / StreamChunk（统一）

这样上层完全不需要知道背后是谁。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅用于类型标注，避免运行期循环导入
    from app.llm.params import ReasoningEffort


class Role(str, Enum):
    """消息角色。

    继承 str 是为了让它能直接当字符串用（JSON 序列化、字典键都方便）。
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass
class ChatMessage:
    """一条对话消息。"""

    role: str
    content: str
    name: str | None = None
    """可选的发言者名字。少数厂商支持，多数场景用不到。"""

    def __post_init__(self) -> None:
        # 允许传入 Role 枚举或字符串，统一归一化成小写字符串
        self.role = str(self.role).lower()
        if self.role not in {r.value for r in Role}:
            raise ValueError(
                f"不支持的消息角色: {self.role!r}，只允许 system / user / assistant"
            )

    # -------------------- 便捷构造 --------------------
    @classmethod
    def system(cls, content: str) -> "ChatMessage":
        return cls(role=Role.SYSTEM.value, content=content)

    @classmethod
    def user(cls, content: str) -> "ChatMessage":
        return cls(role=Role.USER.value, content=content)

    @classmethod
    def assistant(cls, content: str) -> "ChatMessage":
        return cls(role=Role.ASSISTANT.value, content=content)

    @property
    def is_system(self) -> bool:
        return self.role == Role.SYSTEM.value

    def to_dict(self) -> dict[str, Any]:
        """转成 OpenAI 风格的字典（Anthropic 适配器会另行处理 system）。"""
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        return payload


@dataclass
class TokenUsage:
    """token 用量统计。

    各厂商字段名不同，所以这里提供两个 from_xxx 转换方法：
        OpenAI:     prompt_tokens / completion_tokens / total_tokens
        Anthropic:  input_tokens / output_tokens
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    """推理模型用于「思考」的 token 数。

    ★ 它已经包含在 completion_tokens 里，不是额外开销。
      但很值得单独记录：如果一次调用 completion_tokens=200 而 reasoning_tokens=200，
      说明模型把全部输出配额都用在了思考上，正文一个字都没生成 —— 这正是
      「调用明明成功、却拿到空回复」的典型原因。
    """

    @property
    def is_empty(self) -> bool:
        return self.total_tokens == 0 and self.prompt_tokens == 0

    @classmethod
    def from_openai(cls, raw: dict[str, Any] | None) -> "TokenUsage":
        """解析 OpenAI 风格的 usage 字段。"""
        if not raw:
            return cls()
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        total = int(raw.get("total_tokens") or (prompt + completion))
        # 推理模型的「思考 token 数」藏在这个嵌套字段里
        details = raw.get("completion_tokens_details") or {}
        reasoning = int(details.get("reasoning_tokens") or 0) if isinstance(details, dict) else 0
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            reasoning_tokens=reasoning,
        )

    @classmethod
    def from_anthropic(cls, raw: dict[str, Any] | None) -> "TokenUsage":
        """解析 Anthropic 风格的 usage 字段（字段名不一样）。

        Anthropic 不单独返回思考 token，思考量已计入 output_tokens。
        """
        if not raw:
            return cls()
        prompt = int(raw.get("input_tokens") or 0)
        completion = int(raw.get("output_tokens") or 0)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass
class ChatRequest:
    """统一的聊天请求。

    model 留空时使用「适配器构造时绑定的模型名」，
    这样同一次会话里换模型只需要改 Provider 配置，不必改调用代码。
    """

    messages: list[ChatMessage]

    model: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | None = None

    reasoning_effort: "ReasoningEffort | str | None" = None
    """思考强度。留空则使用 Provider 配置里的默认值。

    它是**协议无关**的统一取值，由各适配器翻译成厂商特有字段：
        OpenAI 兼容 → reasoning_effort
        Anthropic   → thinking.budget_tokens
    """

    extra: dict[str, Any] = field(default_factory=dict)
    """厂商特有参数的透传通道。

    例如某些厂商支持的 response_format、presence_penalty 等。
    有了它，接入新厂商时不需要修改本文件 —— 这是保持「统一接口」稳定的关键。
    """

    # -------------------- 派生视图 --------------------
    @property
    def system_prompt(self) -> str | None:
        """把所有 system 消息合并成一段文本。

        为什么要合并？因为 Anthropic 要求系统提示词放在**顶层独立参数**里，
        而不是作为一条 messages 出现。提供这个视图，适配器就能各取所需。
        """
        parts = [m.content for m in self.messages if m.is_system]
        return "\n\n".join(parts) if parts else None

    @property
    def dialogue(self) -> list[ChatMessage]:
        """去掉 system 之后的对话消息（Anthropic 需要这个形态）。"""
        return [m for m in self.messages if not m.is_system]

    def resolved_model(self, fallback: str) -> str:
        """确定本次请求实际使用的模型名。"""
        return self.model or fallback


@dataclass
class ChatResult:
    """一次非流式调用的结果。"""

    content: str
    model: str
    finish_reason: str | None = None

    reasoning: str = ""
    """推理模型的思考过程（DeepSeek 推理系列会返回 reasoning_content）。

    普通模型为空字符串。
    ★ 这个字段是实测之后补上的：流式接口本来就能拿到思考内容
      （StreamChunk.reasoning_delta），而非流式接口原先把它直接丢掉了，
      两边能力不对称。对推理模型来说，思考过程有时比正文更有价值。
    """

    notes: list[str] = field(default_factory=list)
    """适配层为了满足协议要求而做出的调整说明。

    ★ 为什么要有这个字段？
      不同厂商的协议约束不一样，适配层有时**必须**改动用户填的参数才能发出请求。
      例如：
        · Anthropic 在开启思考时不允许修改 temperature / top_p，只能移除
        · OpenAI 兼容协议没有真正的「关闭思考」取值，只能映射成 minimal
      这些调整如果悄悄做掉，用户就会以为「我设的温度生效了」，
      实际并没有 —— 又是一种静默误导。所以必须把调整过程回报给上层。

    普通会话中该列表为空。
    """

    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    raw: dict[str, Any] | None = None
    """厂商原始响应，仅用于调试排查，正常业务不要依赖它的字段。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "model": self.model,
            "finish_reason": self.finish_reason,
            "reasoning": self.reasoning,
            "notes": self.notes,
            "usage": self.usage.to_dict(),
            "latency_ms": self.latency_ms,
        }


@dataclass
class StreamChunk:
    """流式输出的一个片段。

    与「直接 yield 字符串」相比，带上这些字段的好处是：
      · 上层可以累计 token 用量、记录结束原因
      · 推理模型的「思考过程」可以单独取出来，不与正式回复混在一起
    """

    delta: str = ""
    """本次新增的正文内容（可能为空字符串，例如只带元数据的首/尾包）。"""

    reasoning_delta: str = ""
    """推理模型的思考过程增量（如 DeepSeek-R1 的 reasoning_content）。

    多数模型没有这个字段，取值为空字符串。
    """

    finish_reason: str | None = None
    """结束原因。通常只有最后一个片段才有值：stop / length / tool_calls。"""

    usage: TokenUsage | None = None
    """token 用量。只有部分厂商在流的末尾返回，因此是可选的。"""

    notes: list[str] = field(default_factory=list)
    """适配层为满足协议要求所做的调整说明（通常出现在第一个元数据片段上）。

    含义与 ChatResult.notes 相同，详见那里的说明。
    """

    @property
    def is_empty(self) -> bool:
        """是否既没有正文也没有思考内容。"""
        return not self.delta and not self.reasoning_delta
