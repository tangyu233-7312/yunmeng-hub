"""参数生效性诊断 —— 检测「厂商是否真的听我们的话」。

============================ 为什么需要这个模块？============================
「让用户能选择思考强度」这件事有一个隐藏前提：**模型真的会听**。

实测发现（deepseek-flash，同一提示、temperature=0）：

    思考强度    输出token   思考token   思考占比
    auto         218        153        70%
    high         235        172        73%
    low          183        133        73%
    off          254        175        69%   ← 设成「关闭」，思考反而更多

结论：参数被**接受了**（没有报错），但**完全没有生效**，模型始终思考约 70%。

这比返回 400 更危险 —— 用户以为「深度思考已开启」，实际什么都没改变，
而且不会有任何提示。这类「静默无操作」（silent no-op）只能靠**对比实验**发现：

    用同一个提示、同样的温度，分别以 off 和 high 调用两次，
    比较两次的思考 token 数是否真的有差异。

本模块就是把这个对比实验自动化，供界面上的「检测参数支持情况」按钮使用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.core.exceptions import LLMProviderError
from app.llm.base import BaseLLMProvider
from app.llm.params import ReasoningEffort
from app.llm.schema import ChatMessage, ChatRequest

#: 判定「参数生效」所需的思考 token 差异率阈值。
#:
#: ★ 为什么定得这么高（40%）？
#:   因为推理模型的思考长度**本身就有明显的自然波动**。实测 deepseek-flash
#:   在同一提示、temperature=0 下连续 4 次调用，思考 token 分别是：
#:       153 / 172 / 133 / 175   →  波动范围约 ±15%
#:
#:   如果阈值定成 15%，那么「参数被完全忽略」的情况也会因为随机波动
#:   而经常被误判成「已生效」。第一次实现时就踩了这个坑。
#:
#:   真正的判别依据是：**支持关闭思考的模型，off 的思考 token 会接近 0**，
#:   与 high 的差异会非常显著（通常 80% 以上）。只有 10%~20% 的差异，
#:   那只是推理模型的正常波动，不能作为「参数生效」的证据。
DIFFERENCE_THRESHOLD: float = 0.40

#: 默认探测提示词。刻意选一个「需要一点推理但不复杂」的问题，
#: 让模型既会思考、又不会因为太长而撑爆配额。
DEFAULT_PROBE_PROMPT = "请用一句话说明：为什么天空是蓝色的？"


def _sample(
    provider: BaseLLMProvider,
    prompt: str,
    effort: ReasoningEffort,
    max_tokens: int,
) -> dict[str, Any]:
    """用指定思考强度调用一次，采集 token 数据。"""
    result = provider.chat(
        ChatRequest(
            messages=[ChatMessage.user(prompt)],
            max_tokens=max_tokens,
            # temperature=0 降低采样随机性，让两次对比更可信
            temperature=0,
            reasoning_effort=effort,
        )
    )
    return {
        "effort": effort.value,
        "completion_tokens": result.usage.completion_tokens,
        "reasoning_tokens": result.usage.reasoning_tokens,
        "content_length": len(result.content or ""),
        "latency_ms": result.latency_ms,
    }


def probe_reasoning_effort(
    provider: BaseLLMProvider,
    *,
    prompt: str = DEFAULT_PROBE_PROMPT,
    max_tokens: int = 2048,
) -> dict[str, Any]:
    """探测该模型是否真的支持「思考强度」参数。

    做法：以 off 和 high 各调用一次，比较思考 token 数。

    返回结构：
        {
          "parameter": "reasoning_effort",
          "supported": true / false / null,
          "verdict": "人类可读的结论",
          "samples": [...],
          "cost_note": "本次探测额外消耗了 2 次调用"
        }

    成本提示：这个探测会**真实调用两次模型**，因此界面上应做成
    「用户主动点击」的按钮，而不是保存配置时自动执行。
    """
    samples: list[dict[str, Any]] = []

    # 依次用「尽量关闭思考」与「深度思考」各调用一次
    for effort in (ReasoningEffort.OFF, ReasoningEffort.HIGH):
        try:
            samples.append(_sample(provider, prompt, effort, max_tokens))
        except LLMProviderError as exc:
            logger.info("参数探测失败 | {} | {} | {}", provider.label, effort.value, exc.message)
            return {
                "parameter": "reasoning_effort",
                "supported": False,
                "verdict": (
                    f"调用失败，无法完成探测：{exc.message}。"
                    "若错误信息提到不支持的参数，说明该模型不接受思考强度设置，"
                    "请把思考强度改回 auto。"
                ),
                "samples": samples,
                "provider": provider.label,
                "cost_note": f"本次探测已消耗 {len(samples) + 1} 次模型调用",
            }

    off_sample, high_sample = samples[0], samples[1]
    off_tokens = off_sample["reasoning_tokens"]
    high_tokens = high_sample["reasoning_tokens"]

    # 以两者中较大的那个为基准算差异率，避免除以 0
    baseline = max(off_tokens, high_tokens, 1)
    difference_ratio = (high_tokens - off_tokens) / baseline

    supported: bool | None
    if difference_ratio >= DIFFERENCE_THRESHOLD:
        # high 的思考明显多于 off，且差异足够大 —— 参数确实在起作用
        supported = True
        verdict = (
            f"思考强度**已生效**：high 的思考 token（{high_tokens}）比 off（{off_tokens}）"
            f"多 {difference_ratio:.0%}，超过判定阈值 {DIFFERENCE_THRESHOLD:.0%}。"
        )
    elif difference_ratio <= 0:
        # off 的思考不降反增，说明参数被忽略了
        supported = False
        verdict = (
            f"思考强度**未生效**：把强度设为「尽量关闭」后，思考 token 反而有 "
            f"{off_tokens}（high 为 {high_tokens}）。"
            "该模型接受了这个参数，但完全没有理会它 —— "
            "设置不会报错，也不会有任何效果。"
            "建议改回 auto，通过调整「最大输出 Token」来控制正文长度。"
        )
    else:
        # 有差异但不够大 —— 大概率只是推理模型的自然波动，不能下结论
        supported = False
        verdict = (
            f"思考强度**似乎未生效**：off 与 high 的思考 token 分别为 "
            f"{off_tokens} 与 {high_tokens}，差异仅 {difference_ratio:.0%}，"
            f"未达到判定阈值 {DIFFERENCE_THRESHOLD:.0%}。"
            "真正支持关闭思考的模型，off 的思考 token 会接近 0，差异会非常显著；"
            "这个量级的差别更可能只是推理模型的正常波动。"
            "建议把思考强度改回 auto，改为通过「最大输出 Token」控制正文长度。"
        )

    logger.info(
        "参数探测完成 | {} | supported={} | off={} high={}",
        provider.label,
        supported,
        off_tokens,
        high_tokens,
    )

    return {
        "parameter": "reasoning_effort",
        "supported": supported,
        "verdict": verdict,
        "difference_ratio": round(difference_ratio, 4),
        "threshold": DIFFERENCE_THRESHOLD,
        "samples": samples,
        "provider": provider.label,
        "model": provider.model_name,
        "note": (
            "推理模型的思考长度存在约 ±15% 的自然波动（实测数据），"
            "因此判定阈值设为 40%：只有差异足够显著，才能认定参数真的生效。"
            "若你对结论存疑，可以多点几次，观察结论是否稳定。"
        ),
        "cost_note": "本次探测消耗了 2 次模型调用",
        # ★ 供调用方直接写库：把结论挂到模型配置上，界面下次就能给出基于实测的提醒。
        #   注意同时记录「探测时用的模型名」，用于判断结论是否已经过期。
        "persist": {
            "reasoning_effort_supported": supported,
            "reasoning_effort_probed_model": provider.model_name,
            "reasoning_effort_probed_at": datetime.now(timezone.utc).replace(tzinfo=None),
        },
    }


def describe_token_split(provider: BaseLLMProvider) -> dict[str, Any]:
    """描述「思考 vs 正文」的 token 分配规则与当前预算。

    纯计算，不调用模型 —— 可用于界面实时预览。
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
