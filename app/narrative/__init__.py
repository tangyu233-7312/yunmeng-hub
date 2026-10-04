"""叙事引擎核心：提示词构建、上下文管理、对话编排。

这个包是「把所有零件串起来」的地方：

    角色卡（人设 + 开场白）  世界书（世界观设定）
                 \\              /
                  ↓            ↓
              prompt_builder  拼装提示词
                  ↓
              context_manager 按 ContextBudget 裁剪历史（超预算就滚动摘要）
                  ↓
              app/llm/ 适配层 调用模型（流式 / 非流式）
                  ↓
              engine 落库并统计 token

★ 它与 app/llm/ 的分工必须清楚：
    llm/       只负责「协议差异」（OpenAI 兼容 / Anthropic 怎么表达同一个意图）
    narrative/ 只负责「业务语义」（谁在说话、记得哪些设定、上下文放不下怎么办）

  所以这个包里**不出现任何厂商字段名**，只使用统一的数据结构。
"""

from app.narrative.context_manager import (
    ContextPlan,
    estimate_message_tokens,
    estimate_tokens,
    prepare_context,
    summarize_dropped,
)
from app.narrative.prompt_builder import PromptPlan, build_messages, build_prompt

__all__ = [
    "ContextPlan",
    "PromptPlan",
    "build_messages",
    "build_prompt",
    "estimate_message_tokens",
    "estimate_tokens",
    "prepare_context",
    "summarize_dropped",
]
