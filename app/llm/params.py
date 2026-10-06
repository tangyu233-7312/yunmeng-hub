"""统一的生成参数与上下文预算。

============================ 核心规则 ============================
★ max_tokens（最大输出 token）**包含思考过程 token** ★

这是本项目最容易搞错、也最必须在界面上讲清楚的一条规则：

    max_tokens（最大输出）
        ├── 思考过程 token（reasoning_tokens）   ← 推理模型专用
        └── 正文 token（content）

也就是说：**max_tokens 是「思考 + 正文」的总上限，不是正文的长度。**

实测数据（deepseek-flash，一次叙事场景描述）：

    completion_tokens = 433，其中 reasoning_tokens = 380
    → 思考占了 88%，正文只拿到 53 个 token

后果：用户如果把「最大输出」设为 500，而模型是推理模型，
      很可能思考吃掉 450，正文只剩 50 —— 用户会觉得「AI 回复被截断了」。

这条规则与 SillyTavern（酒馆）的默认行为一致。因此：

  · 界面上必须写明「含思考过程」字样
  · 默认值要为推理模型留出余量
  · 上下文预算必须以 max_tokens 为**整块**扣除，而不是只扣正文部分

============================ 上下文预算怎么算 ============================
模型的上下文窗口（context_window）是「输入 + 输出」的总容量：

    ┌──────────────── context_window ────────────────┐
    │   输入（系统提示词 + 历史消息 + 记忆召回）      │  输出（max_tokens）  │
    └────────────────────────────────────────────────┘

    输入预算 = context_window − max_tokens − 安全余量

  · 输出部分必须**整块预留**：模型生成时可能真的一次性用满它
  · 安全余量用于吸收 token 估算误差（不同模型分词方式不同，
    tiktoken 的估算与实际值通常有几个百分点偏差）

============================ 为什么思考强度需要适配层 ============================
各家对「思考强度」的表达方式完全不同：

    OpenAI 系         reasoning_effort = minimal / low / medium / high
    Anthropic         thinking = {"type": "enabled", "budget_tokens": N}
    Qwen / DashScope  enable_thinking = true / false
    vLLM 部署         chat_template_kwargs = {"enable_thinking": false}

所以本项目在**统一层**只暴露一个 `reasoning_effort` 枚举，
由各个适配器翻译成自己协议的字段 —— 这是「异构适配」最典型的用武之地。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ==================================================================
#  思考强度
# ==================================================================
class ReasoningEffort(str, Enum):
    """思考强度（统一枚举，与厂商无关）。"""

    AUTO = "auto"
    """不干预，使用厂商自己的默认行为（最安全的选项）。"""

    OFF = "off"
    """尽量关闭思考。

    ⚠️ 注意：并非所有协议都能真正关闭。OpenAI 兼容协议里最接近的取值是
       `minimal`，它不是「零思考」。若模型不支持，会走 auto 的处理逻辑。
    """

    LOW = "low"
    """轻度思考，适合简单问答、快速响应。"""

    MEDIUM = "medium"
    """中等思考，日常叙事的平衡点。"""

    HIGH = "high"
    """深度思考，适合复杂剧情推演；消耗的输出配额也最多。"""


#: 思考强度与「输出配额消耗」的粗略对应关系，用于界面上的提示文案
REASONING_EFFORT_HINTS: dict[ReasoningEffort, str] = {
    ReasoningEffort.AUTO: "使用模型默认行为，不额外干预",
    ReasoningEffort.OFF: "尽量关闭思考，输出全部留给正文（部分模型不支持）",
    ReasoningEffort.LOW: "轻度思考，响应快，思考约占输出的 30%~50%",
    ReasoningEffort.MEDIUM: "中等思考，思考约占输出的 50%~70%",
    ReasoningEffort.HIGH: "深度思考，思考可能占用 80% 以上的输出配额",
}


# ==================================================================
#  生成参数
# ==================================================================
class GenerationParams(BaseModel):
    """一次对话的生成参数（统一模型，与具体厂商无关）。

    这个模型同时承担三个角色：
      1. 数据库中模型配置的「生成参数」部分
      2. 界面上「API 配置」表单里的参数控件
      3. 组装请求体时参数合并的载体
    """

    model_config = ConfigDict(validate_assignment=True)

    # ---------------- 采样参数 ----------------
    temperature: float = Field(
        default=0.8,
        ge=0.0,
        le=2.0,
        description="采样温度。越低越稳定保守，越高越有创造性。叙事场景建议 0.7~1.0",
    )
    top_p: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="核采样。与温度二选一调整即可，同时调容易相互干扰",
    )

    # ---------------- 输出长度 ----------------
    max_tokens: int = Field(
        default=2048,
        ge=1,
        le=131072,
        description="★ 最大输出 token，**包含思考过程**。不是正文字数上限",
    )

    # ---------------- 思考控制 ----------------
    reasoning_effort: ReasoningEffort = Field(
        default=ReasoningEffort.AUTO,
        description="思考强度。适配器会把它翻译成各厂商特有的字段",
    )

    # ---------------- 其他常用参数 ----------------
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    stop: list[str] | None = Field(default=None, description="停止词，遇到即停止生成")
    seed: int | None = Field(default=None, description="随机种子，用于复现同一结果")

    # ---------------- 长尾参数 ----------------
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="厂商特有参数透传。以 _ 开头的键是内部元数据，不会被发送到上游",
    )

    # -------------------- 构造与转换 --------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "GenerationParams":
        """从字典构造：已知字段走校验，未知字段一律放进 extra 透传。

        这样数据库里的 extra_params JSON 既能存核心参数，
        也能存任意厂商特有参数，两边都不丢。
        """
        if not raw:
            return cls()

        # 以 _ 开头的是内部元数据（例如给配置写的备注），不参与请求
        cleaned = {k: v for k, v in raw.items() if not str(k).startswith("_")}

        known = {k: v for k, v in cleaned.items() if k in cls.model_fields}
        unknown = {k: v for k, v in cleaned.items() if k not in cls.model_fields}
        return cls(**known, extra=unknown)

    def to_wire_params(self) -> dict[str, Any]:
        """转换成可以合并进请求体的字典。

        temperature 与 max_tokens 总是发送（显式声明更可预期，也与酒馆行为一致）；
        其余为空的参数不发送，交给厂商自己的默认值。

        ★ 注意：reasoning_effort **不在这里处理**。
          它必须由各适配器翻译成协议特有字段（OpenAI 是 reasoning_effort，
          Anthropic 是 thinking.budget_tokens），所以不在通用层拼装。
        """
        payload: dict[str, Any] = {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        for key in ("top_p", "frequency_penalty", "presence_penalty", "stop", "seed"):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value

        # 厂商特有参数最后合并，允许它们覆盖上面的通用字段
        payload.update(self.extra)
        return payload

    def merged_with(self, overrides: dict[str, Any]) -> "GenerationParams":
        """返回一个被 overrides 覆盖后的新对象（不修改自身）。"""
        if not overrides:
            return self.model_copy()
        # 注意用 GenerationParams.model_fields（类属性）而不是 self.model_fields：
        # pydantic 2.11 起，通过实例访问 model_fields 已被弃用。
        fields = type(self).model_fields
        base = self.model_dump()
        base_extra = dict(self.extra)
        for key, value in overrides.items():
            if key in fields and key != "extra":
                base[key] = value
            else:
                base_extra[key] = value
        base["extra"] = base_extra
        return GenerationParams(**base)

    def describe(self) -> str:
        """人类可读的摘要，用于日志与界面展示。"""
        return (
            f"temperature={self.temperature} max_tokens={self.max_tokens}"
            f"(含思考) reasoning={self.reasoning_effort.value}"
        )


# ==================================================================
#  上下文预算
# ==================================================================
#: 默认的安全余量比例（用于吸收 token 估算误差）
DEFAULT_SAFETY_RATIO: float = 0.05
#: 安全余量的下限（比例算出来太小时兜底）
MIN_SAFETY_MARGIN: int = 128


@dataclass(frozen=True)
class ContextBudget:
    """上下文预算的拆解结果。

    它是一个**不可变**的数据对象，只用于描述与展示，不参与请求构造。
    真正的上下文裁剪逻辑在 app/narrative/context_manager.py（后续步骤实现）。
    """

    context_window: int
    """模型上下文窗口总容量（输入 + 输出）。"""

    max_output_tokens: int
    """为输出整块预留的 token 数（★包含思考过程）。"""

    safety_margin: int
    """安全余量，用于吸收 token 估算误差。"""

    @property
    def input_budget(self) -> int:
        """可用于「系统提示词 + 历史消息 + 记忆召回」的 token 预算。"""
        return max(0, self.context_window - self.max_output_tokens - self.safety_margin)

    @property
    def output_share(self) -> float:
        """输出预留占整个上下文窗口的比例。"""
        if self.context_window <= 0:
            return 0.0
        return self.max_output_tokens / self.context_window

    @property
    def is_usable(self) -> bool:
        """输入预算是否还够用（低于 512 基本放不下人设与对话历史）。"""
        return self.input_budget >= 512

    def to_dict(self) -> dict[str, Any]:
        return {
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "safety_margin": self.safety_margin,
            "input_budget": self.input_budget,
            "output_share": round(self.output_share, 4),
            "is_usable": self.is_usable,
            "note": "max_output_tokens 包含思考过程 token（推理模型会占用大部分）",
        }

    def describe(self) -> str:
        return (
            f"上下文窗口 {self.context_window} = "
            f"输出预留 {self.max_output_tokens}(含思考) + "
            f"安全余量 {self.safety_margin} + "
            f"输入预算 {self.input_budget}"
        )


def compute_context_budget(
    context_window: int,
    max_output_tokens: int,
    safety_ratio: float = DEFAULT_SAFETY_RATIO,
) -> ContextBudget:
    """根据上下文窗口与最大输出计算预算拆解。

    安全余量取「窗口的 5%」与「128 token」中的较大者。
    """
    margin = max(MIN_SAFETY_MARGIN, int(context_window * safety_ratio))
    return ContextBudget(
        context_window=int(context_window),
        max_output_tokens=int(max_output_tokens),
        safety_margin=margin,
    )


# ==================================================================
#  完整的模型端点配置（对应界面上的「API 配置」表单）
# ==================================================================
class ProviderConfig(BaseModel):
    """一个「用户自配的模型端点」的完整配置。

    对应 llm_providers 表的一行，也对应前端「新增模型」表单的全部字段。
    把它定义在这里，是为了让「界面」与「数据库」共享同一份校验规则 ——
    避免前端能填、后端报错这类割裂体验。
    """

    model_config = ConfigDict(validate_assignment=True)

    # ---------------- 标识与连接 ----------------
    name: str = Field(..., min_length=1, max_length=64, description="配置别名，如「我的 DeepSeek」")
    provider_type: str = Field(default="openai_compatible", description="协议类型")
    base_url: str = Field(..., min_length=1, description="API 地址，需包含版本号，如 https://api.deepseek.com")
    api_key: str = Field(default="", description="API 密钥（本地部署可为空）")
    model_name: str = Field(..., min_length=1, description="模型名，如 deepseek-flash")

    # ---------------- 模型能力 ----------------
    context_window: int = Field(
        default=65536,
        ge=512,
        le=10_000_000,
        description="模型上下文窗口总容量（输入 + 输出）。不同模型不同，需用户按厂商文档填写",
    )

    # ---------------- 生成参数 ----------------
    generation: GenerationParams = Field(default_factory=GenerationParams)

    @model_validator(mode="after")
    def _check_output_fits_window(self) -> "ProviderConfig":
        """输出预留不能吃掉整个上下文窗口，否则一点输入都放不下。"""
        if self.generation.max_tokens >= self.context_window:
            raise ValueError(
                f"最大输出 token（{self.generation.max_tokens}）不能大于等于上下文窗口"
                f"（{self.context_window}）—— 那样就没有空间放提示词和对话历史了"
            )
        return self

    # -------------------- 派生信息 --------------------
    @property
    def budget(self) -> ContextBudget:
        """当前的上下文预算拆解。"""
        return compute_context_budget(self.context_window, self.generation.max_tokens)

    def warnings(self) -> list[str]:
        """返回**会影响效果的问题**（界面用黄色警告展示，不阻断保存）。

        这些提醒的存在意义：很多参数问题不会报错，只会让效果变差，
        用户很难自行察觉，所以需要在界面上主动提示。
        """
        messages: list[str] = []
        generation = self.generation
        budget = self.budget
        effort = generation.reasoning_effort

        # 推理模型 + 输出配额偏小 = 正文被思考挤没
        if (
            effort not in (ReasoningEffort.OFF, ReasoningEffort.AUTO)
            and generation.max_tokens < 2048
        ):
            messages.append(
                f"该模型开启了思考（{effort.value}），而最大输出只有 "
                f"{generation.max_tokens} token。思考过程可能占用大部分配额，"
                f"建议调到 2048 以上，否则正文可能很短甚至为空。"
            )

        if not budget.is_usable:
            messages.append(
                f"输入预算仅剩 {budget.input_budget} token，可能放不下角色设定与对话历史。"
                f"请调大上下文窗口，或调小最大输出。"
            )

        if budget.output_share > 0.5:
            messages.append(
                f"输出预留占了上下文窗口的 {budget.output_share:.0%}，"
                f"留给提示词与历史的空间偏少，建议调低最大输出或调大上下文窗口。"
            )

        if not self.api_key and "localhost" not in self.base_url and "127.0.0.1" not in self.base_url:
            messages.append("未填写 API Key。云端服务通常都需要密钥，请确认。")

        return messages

    def hints(self) -> list[str]:
        """返回**建议性提示**（界面用灰色/蓝色信息条展示，不是错误）。

        与 warnings 的区别：warnings 是「当前配置有问题」，
        hints 是「你可能想多做一步」。
        """
        messages: list[str] = []
        effort = self.generation.reasoning_effort

        if effort is ReasoningEffort.AUTO:
            # auto 永远不需要提醒：它不干预厂商默认行为，不存在「设了没用」的问题
            return messages

        # ★ 第二十七轮：这里原来会提醒"尚未验证该模型是否支持思考强度，建议点检测"。
        #   那个"检测"功能已被**删除** —— 实测证明用思考 token 数根本无法可靠判定
        #   （DeepSeek 各档的差异全在自然波动范围内），它给过错误结论。
        #   现在改为"直接发送、被拒自动退回"（见 openai_compatible._post_with_effort_fallback），
        #   所以这里只需要说明**当前有这个设置**，不再要求用户去验证什么。
        messages.append(
            f"已设置思考强度「{effort.value}」：适配器会把它翻译成厂商字段"
            "（OpenAI 兼容 → reasoning_effort；Anthropic → thinking.budget_tokens）。"
            "若某个模型不认识该参数，服务端会拒绝，届时本应用会**自动去掉它重试一次**"
            "并在回复的「适配说明」里如实告知，不会打断对话。"
        )
        return messages

    def to_dict(self) -> dict[str, Any]:
        """序列化（**不含明文密钥**，用于接口返回）。"""
        return {
            "name": self.name,
            "provider_type": self.provider_type,
            "base_url": self.base_url,
            "model_name": self.model_name,
            "context_window": self.context_window,
            "generation": self.generation.model_dump(mode="json"),
            "budget": self.budget.to_dict(),
            # 两类提示分开返回，前端可以分别用黄色警告条与灰色信息条展示
            "warnings": self.warnings(),
            "hints": self.hints(),
        }


def describe_token_split(provider: Any) -> dict[str, Any]:
    """描述「思考 vs 正文」的 token 分配规则与当前预算。

    纯计算，不调用模型 —— 可用于界面实时预览。

    ★ 它原先住在 `app/llm/diagnostics.py`（那个模块还放着"思考强度生效性探测"）。
      第二十七轮删掉探测功能时差点把它一起删掉 —— 幸好有测试引用它（`test_params.py`）
      把这次误删拦了下来。现在它独立放在这里：它讲的是 **token 预算规则**，
      与"厂商听不听话"无关，本来也不该和探测绑在一起。
    """
    budget = provider.budget
    return {
        "provider": provider.label,
        "model": provider.model_name,
        "reasoning_effort": provider.default_params.reasoning_effort.value,
        "budget": budget.to_dict(),
        "rule": (
            "最大输出 Token（max_tokens）包含思考过程 Token。"
            "推理模型会先把配额用在思考上，剩下的才留给正文。"
        ),
    }


def cheap_reasoning_effort(adapter: Any) -> "ReasoningEffort | None":
    """**机械任务**（翻译中间件 / 剧情总结）该用多大思考强度：能省就省。

    ==================== 第二十七轮的重要修正 ====================
    这个函数原来会先看"该模型是否被**实测**支持思考强度"（`adapter.reasoning_support`，
    来自已删除的探测功能），只在结论为 True 时才降思考。后果是：

      · DeepSeek 被那次**不可靠的探测**误判为"不支持"（见 §27 的实验记录：
        各档思考 token 全在自然波动范围内，根本不足以判定），
      · 于是翻译与剧情总结这两条**本来最该省钱**的链路，
        **从来没能真正降过思考强度** —— 省钱机制被一个错误结论堵死了整整几轮。

    现在改成：**直接请求 OFF，不再预判**。风险由适配器兜住 ——
    若厂商拒绝 `reasoning_effort`，`_post_with_effort_fallback` 会自动去掉它并重试一次，
    对话不会中断，调用方也会在 `notes` 里看到如实说明。
    换句话说：**我们自己试一次，比拿 token 数猜一次更可靠**。

    ★ 返回值 None = "不要覆盖"（沿用 provider 自己的设置）：仅在适配器明确表示
      "不参与思考强度适配"时返回（例如某些协议没有对应字段）。这里用
      `getattr(..., default=False)` 取一个**显式声明**的开关，取不到就按"支持"处理 ——
      因为现在的默认策略是"敢试"，而不是"不敢试"。
    ★ 只用 `getattr(..., default)`，**不包 try/except**：接口变了就该炸出来，
      不许再变成静默的 no-op（上一轮就是这么埋掉整个功能的）。
    """
    if getattr(adapter, "supports_reasoning_effort", True) is False:
        return None
    return ReasoningEffort.OFF
