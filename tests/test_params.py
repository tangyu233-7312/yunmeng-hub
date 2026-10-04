"""生成参数与上下文预算测试（3.4 扩展）。

对应两条核心业务规则：

  1. **max_tokens 包含思考 token** —— 这是「酒馆同款」的默认规则，
     也是用户最容易误解的地方，必须有测试守住。
  2. **思考强度必须由各协议自行翻译** —— 统一层只暴露 ReasoningEffort，
     OpenAI 兼容协议翻译成 reasoning_effort 字段。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_params.py -v
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.llm import (
    ChatMessage,
    ChatRequest,
    GenerationParams,
    ProviderConfig,
    ReasoningEffort,
    compute_context_budget,
    create_provider,
    create_provider_from_config,
    describe_token_split,
    probe_reasoning_effort,
)

OK_BODY: dict[str, Any] = {
    "model": "mock-model",
    "choices": [
        {"message": {"role": "assistant", "content": "好的。"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}


def capture_payload(
    *,
    default_params: GenerationParams | dict[str, Any] | None = None,
    context_window: int = 65536,
    **request_kwargs: Any,
) -> dict[str, Any]:
    """构造一个假适配器，执行一次 chat，返回实际发出的请求体。"""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=OK_BODY)

    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://mock.example.com/v1",
        api_key="k",
        model_name="mock-model",
        context_window=context_window,
        extra_params=default_params,
        max_retries=0,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    provider.chat(ChatRequest(messages=[ChatMessage.user("hi")], **request_kwargs))
    return captured["json"]


# ==================================================================
#  一、生成参数的默认值与校验
# ==================================================================
def test_default_params() -> None:
    params = GenerationParams()
    assert params.temperature == 0.8
    assert params.max_tokens == 2048
    assert params.reasoning_effort is ReasoningEffort.AUTO
    assert params.top_p is None


def test_max_tokens_documented_as_including_reasoning() -> None:
    """★ 字段说明里必须写明「包含思考过程」，因为界面会直接展示它。"""
    description = GenerationParams.model_fields["max_tokens"].description or ""
    assert "包含思考" in description


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", -0.1),
        ("temperature", 2.1),
        ("top_p", -0.1),
        ("top_p", 1.1),
        ("max_tokens", 0),
        ("frequency_penalty", -3.0),
        ("presence_penalty", 3.0),
    ],
)
def test_param_range_validation(field: str, value: Any) -> None:
    """越界参数必须在构造阶段就被拦下，而不是发给厂商换回一个 400。"""
    with pytest.raises(ValidationError):
        GenerationParams(**{field: value})


def test_reasoning_effort_accepts_string() -> None:
    assert GenerationParams(reasoning_effort="high").reasoning_effort is ReasoningEffort.HIGH


def test_reasoning_effort_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        GenerationParams(reasoning_effort="super-high")


# ==================================================================
#  二、从字典构造（数据库 extra_params JSON 的入口）
# ==================================================================
def test_from_dict_splits_known_and_unknown() -> None:
    """已知参数走校验，未知参数放进 extra 透传，两边都不丢。"""
    params = GenerationParams.from_dict(
        {"temperature": 0.5, "max_tokens": 3000, "response_format": {"type": "json_object"}}
    )
    assert params.temperature == 0.5
    assert params.max_tokens == 3000
    assert params.extra == {"response_format": {"type": "json_object"}}


def test_from_dict_drops_internal_metadata() -> None:
    """下划线开头的是内部备注，不应进入请求。"""
    params = GenerationParams.from_dict({"temperature": 0.5, "_note": "备注"})
    assert params.temperature == 0.5
    assert "_note" not in params.extra


def test_from_dict_handles_none() -> None:
    assert GenerationParams.from_dict(None).max_tokens == 2048


# ==================================================================
#  三、请求体拼装
# ==================================================================
def test_wire_params_always_send_temperature_and_max_tokens() -> None:
    """温度与最大输出始终显式发送 —— 行为可预期，也与酒馆一致。"""
    payload = GenerationParams(temperature=0.6, max_tokens=1500).to_wire_params()
    assert payload["temperature"] == 0.6
    assert payload["max_tokens"] == 1500


def test_wire_params_omit_unset_optional_fields() -> None:
    payload = GenerationParams().to_wire_params()
    for key in ("top_p", "frequency_penalty", "presence_penalty", "stop", "seed"):
        assert key not in payload


def test_wire_params_include_set_optional_fields() -> None:
    payload = GenerationParams(top_p=0.9, stop=["\n\n"]).to_wire_params()
    assert payload["top_p"] == 0.9
    assert payload["stop"] == ["\n\n"]


def test_wire_params_extra_overrides_known_fields() -> None:
    """长尾参数最后合并，允许覆盖通用字段（高级用户的逃生口）。"""
    params = GenerationParams(temperature=0.5, extra={"temperature": 1.2})
    assert params.to_wire_params()["temperature"] == 1.2


def test_merged_with_overrides_only_given_fields() -> None:
    base = GenerationParams(temperature=0.3, max_tokens=1000, top_p=0.7)
    merged = base.merged_with({"temperature": 1.0})
    assert merged.temperature == 1.0
    assert merged.max_tokens == 1000   # 未覆盖的保持原值
    assert merged.top_p == 0.7
    # 原对象不应被修改
    assert base.temperature == 0.3


# ==================================================================
#  四、上下文预算
# ==================================================================
def test_context_budget_formula() -> None:
    """输入预算 = 上下文窗口 − 输出预留 − 安全余量。"""
    budget = compute_context_budget(context_window=8192, max_output_tokens=2048)
    assert budget.safety_margin == max(128, int(8192 * 0.05))  # = 409
    assert budget.input_budget == 8192 - 2048 - 409


def test_context_budget_safety_margin_has_floor() -> None:
    """窗口很小时，按比例算出的余量太小，应回落到下限 128。"""
    budget = compute_context_budget(context_window=1024, max_output_tokens=256)
    assert budget.safety_margin == 128


def test_context_budget_flags_usable_range() -> None:
    assert compute_context_budget(8192, 1024).is_usable is True
    # 输出吃掉太多，剩下放不下提示词
    assert compute_context_budget(4096, 3900).is_usable is False


def test_context_budget_note_mentions_reasoning() -> None:
    """★ 预算说明里必须点明「输出预留包含思考」，否则界面会误导用户。"""
    data = compute_context_budget(8192, 2048).to_dict()
    assert "包含思考" in data["note"]
    assert data["max_output_tokens"] == 2048
    assert data["output_share"] == pytest.approx(2048 / 8192, abs=1e-4)


# ==================================================================
#  五、ProviderConfig（界面表单对应的完整配置）
# ==================================================================
def test_provider_config_accepts_valid_input() -> None:
    config = ProviderConfig(
        name="我的 DeepSeek",
        base_url="https://api.deepseek.com",
        api_key="sk-x",
        model_name="deepseek-flash",
        context_window=65536,
        generation=GenerationParams(max_tokens=4096, reasoning_effort="high"),
    )
    assert config.budget.input_budget == 65536 - 4096 - max(128, int(65536 * 0.05))


def test_provider_config_rejects_output_larger_than_window() -> None:
    """最大输出不能大于等于上下文窗口，否则没有空间放提示词。"""
    with pytest.raises(ValidationError, match="上下文窗口"):
        ProviderConfig(
            name="错误配置",
            base_url="https://api.example.com",
            model_name="m",
            context_window=4096,
            generation=GenerationParams(max_tokens=4096),
        )


def test_provider_config_warns_when_reasoning_with_small_output() -> None:
    """★ 推理模型 + 输出偏小 → 正文会被思考挤没，必须提示用户。"""
    config = ProviderConfig(
        name="推理模型小输出",
        base_url="https://api.example.com",
        api_key="sk-x",
        model_name="reasoner",
        context_window=65536,
        generation=GenerationParams(max_tokens=512, reasoning_effort="high"),
    )
    warnings = config.warnings()
    assert any("思考" in w and "2048" in w for w in warnings)


def test_provider_config_no_warning_when_reasoning_off() -> None:
    """关掉思考后，小输出就不再是问题。"""
    config = ProviderConfig(
        name="关闭思考",
        base_url="https://api.example.com",
        api_key="sk-x",
        model_name="m",
        context_window=65536,
        generation=GenerationParams(max_tokens=512, reasoning_effort="off"),
    )
    assert not any("思考" in w for w in config.warnings())


def test_provider_config_warns_on_tight_input_budget() -> None:
    """输出吃掉绝大部分窗口时，剩下的输入预算放不下人设与历史，必须提示。"""
    config = ProviderConfig(
        name="预算紧张",
        base_url="https://api.example.com",
        api_key="sk-x",
        model_name="m",
        context_window=4096,
        generation=GenerationParams(max_tokens=3600),
    )
    warnings = config.warnings()
    assert config.budget.input_budget < 512
    assert any("输入预算" in w for w in warnings)


def test_provider_config_warns_on_missing_key_for_cloud() -> None:
    config = ProviderConfig(
        name="没填密钥",
        base_url="https://api.example.com",
        model_name="m",
    )
    assert any("API Key" in w for w in config.warnings())


def test_provider_config_no_key_warning_for_localhost() -> None:
    """本地部署（Ollama / vLLM）不需要密钥，不应误报。"""
    config = ProviderConfig(
        name="本地模型",
        base_url="http://localhost:11434/v1",
        model_name="qwen2.5:7b",
    )
    assert not any("API Key" in w for w in config.warnings())


def test_provider_config_to_dict_never_leaks_key() -> None:
    config = ProviderConfig(
        name="含密钥",
        base_url="https://api.example.com",
        api_key="sk-super-secret-value",
        model_name="m",
    )
    assert "sk-super-secret-value" not in json.dumps(config.to_dict(), ensure_ascii=False)


# ==================================================================
#  六、思考强度的协议翻译（本层最关键的一环）
# ==================================================================
def test_auto_effort_sends_no_extra_field() -> None:
    """auto 时不发送任何思考相关字段 —— 这是最安全的默认。"""
    payload = capture_payload(default_params=GenerationParams(reasoning_effort="auto"))
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("off", "minimal")],
)
def test_effort_translated_to_openai_field(effort: str, expected: str) -> None:
    """统一思考强度应被翻译成 OpenAI 兼容协议的 reasoning_effort 字段。"""
    payload = capture_payload(default_params=GenerationParams(reasoning_effort=effort))
    assert payload["reasoning_effort"] == expected


def test_request_effort_overrides_provider_default() -> None:
    payload = capture_payload(
        default_params=GenerationParams(reasoning_effort="low"),
        reasoning_effort="high",
    )
    assert payload["reasoning_effort"] == "high"


def test_invalid_effort_in_config_is_rejected_early() -> None:
    """★ 配置层的非法取值必须**当场报错**，不能静默降级。

    理由：写错配置（比如把 high 拼成 hight）如果被悄悄改成 auto，
    用户会以为「深度思考已生效」，实际却什么都没设置 —— 这种静默修正
    比直接报错难排查得多。所以配置路径一律严格校验。
    """
    with pytest.raises(ValidationError):
        GenerationParams(reasoning_effort="extreme")


def test_invalid_effort_in_request_falls_back_to_auto() -> None:
    """★ 请求层则相反：允许容错。

    ChatRequest.reasoning_effort 是普通字符串（可能来自 URL 参数或前端旧版本），
    不该因为一个拼写就整条请求失败，因此降级为 auto 并记日志。
    """
    payload = capture_payload(reasoning_effort="extreme")
    assert "reasoning_effort" not in payload


def test_400_error_with_reasoning_effort_gets_actionable_hint() -> None:
    """★ 部分模型不认识 reasoning_effort，会返回 400。

    错误详情里必须直接给出解法（改回 auto），而不是让用户对着英文报错发呆。
    """
    from app.llm.errors import LLMBadRequestError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"message": "Unrecognized request argument: reasoning_effort"}},
        )

    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://mock.example.com/v1",
        api_key="k",
        model_name="mock-model",
        extra_params=GenerationParams(reasoning_effort="high"),
        max_retries=0,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)

    with pytest.raises(LLMBadRequestError) as exc_info:
        provider.chat(ChatRequest(messages=[ChatMessage.user("hi")]))

    assert "hint" in exc_info.value.detail
    assert "auto" in exc_info.value.detail["hint"]


# ==================================================================
#  七、适配器对外暴露的预算信息
# ==================================================================
def test_provider_exposes_context_budget() -> None:
    """适配器自身就能算预算，便于界面在保存前预览。"""
    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://mock.example.com/v1",
        api_key="k",
        model_name="mock-model",
        context_window=32768,
        extra_params=GenerationParams(max_tokens=4096),
    )
    budget = provider.budget
    assert budget.context_window == 32768
    assert budget.max_output_tokens == 4096
    assert budget.input_budget == 32768 - 4096 - max(128, int(32768 * 0.05))
    provider.close()


def test_provider_budget_reflects_configured_max_tokens() -> None:
    """预算里的输出预留必须等于配置的 max_tokens（含思考的整块）。"""
    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://mock.example.com/v1",
        api_key="k",
        model_name="m",
        context_window=8192,
        extra_params={"max_tokens": 2048},
    )
    assert provider.budget.max_output_tokens == 2048
    provider.close()


# ==================================================================
#  八、从完整配置构造适配器
# ==================================================================
def test_create_provider_from_config() -> None:
    config = ProviderConfig(
        name="集成测试",
        base_url="https://mock.example.com/v1",
        api_key="k",
        model_name="mock-model",
        context_window=16384,
        generation=GenerationParams(temperature=0.9, max_tokens=1024, reasoning_effort="low"),
    )
    provider = create_provider_from_config(config)

    assert provider.model_name == "mock-model"
    assert provider.context_window == 16384
    assert provider.default_params.temperature == 0.9
    assert provider.default_params.reasoning_effort is ReasoningEffort.LOW
    assert provider.budget.max_output_tokens == 1024
    provider.close()


# ==================================================================
#  九、参数生效性诊断（检测厂商是否真的「听话」）
# ==================================================================
def make_probe_provider(handler: Any):
    """构造一个用于探测的假适配器。"""
    provider = create_provider(
        provider_type="openai_compatible",
        base_url="https://mock.example.com/v1",
        api_key="k",
        model_name="mock-model",
        context_window=8192,
        extra_params=GenerationParams(max_tokens=2048),
        max_retries=0,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=30)
    return provider


def _probe_body(reasoning_tokens: int) -> dict[str, Any]:
    return {
        "model": "mock-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": "天空呈蓝色是因为瑞利散射。"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": reasoning_tokens + 20,
            "total_tokens": reasoning_tokens + 40,
            "completion_tokens_details": {"reasoning_tokens": reasoning_tokens},
        },
    }


def test_probe_detects_ignored_parameter() -> None:
    """★ 复现 deepseek-flash 的真实行为：接受参数但完全不理它。

    无论传 minimal 还是 high，思考 token 都一模一样。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_probe_body(140))

    provider = make_probe_provider(handler)
    result = probe_reasoning_effort(provider)

    assert result["supported"] is False
    assert "未生效" in result["verdict"]
    # 结论里要给出可行的替代方案
    assert "auto" in result["verdict"]
    assert len(result["samples"]) == 2
    provider.close()


def test_probe_detects_working_parameter() -> None:
    """参数真的生效时，两次采样的思考 token 应有明显差异。"""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        # high 时思考多，minimal（off 映射而来）时思考少
        reasoning = 180 if payload.get("reasoning_effort") == "high" else 20
        return httpx.Response(200, json=_probe_body(reasoning))

    provider = make_probe_provider(handler)
    result = probe_reasoning_effort(provider)

    assert result["supported"] is True
    assert "已生效" in result["verdict"]
    assert result["samples"][0]["reasoning_tokens"] == 20
    assert result["samples"][1]["reasoning_tokens"] == 180
    provider.close()


def test_probe_treats_small_difference_as_noise() -> None:
    """★ 关键用例：真实的 deepseek-flash 数据（off=97 / high=122）。

    差异 20% 看起来「有点效果」，但推理模型本身的波动就有 ±15%。
    如果阈值定得太低，就会把噪声误判成「参数已生效」—— 第一次实现时正是这么错的。
    正确结论应当是「似乎未生效」。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        reasoning = 122 if payload.get("reasoning_effort") == "high" else 97
        return httpx.Response(200, json=_probe_body(reasoning))

    provider = make_probe_provider(handler)
    result = probe_reasoning_effort(provider)

    assert result["supported"] is False
    assert "似乎未生效" in result["verdict"]
    # 结论里要解释「为什么这点差异不算数」
    assert "波动" in result["verdict"]
    provider.close()


def test_probe_reports_failure_without_raising() -> None:
    """探测失败要如实返回结论，而不是把异常抛给上层。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "Unrecognized request argument: reasoning_effort"}}
        )

    provider = make_probe_provider(handler)
    result = probe_reasoning_effort(provider)

    assert result["supported"] is False
    assert "调用失败" in result["verdict"]
    provider.close()


def test_probe_uses_zero_temperature_for_fair_comparison() -> None:
    """两次采样必须用同样的温度，否则随机性会污染结论。"""
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content).get("temperature"))
        return httpx.Response(200, json=_probe_body(100))

    provider = make_probe_provider(handler)
    probe_reasoning_effort(provider)
    assert seen == [0, 0]
    provider.close()


def test_describe_token_split_states_the_rule() -> None:
    """界面预览用的摘要里必须写明「最大输出包含思考」。"""

    provider = make_probe_provider(lambda request: httpx.Response(200, json=OK_BODY))
    info = describe_token_split(provider)

    assert "包含思考" in info["rule"]
    assert info["budget"]["max_output_tokens"] == 2048
    provider.close()


# ==================================================================
#  十、思考强度「生效性」提醒
#
#  背景：把「思考强度」做成下拉框还不够 —— 部分模型会接受这个参数
#  但完全不理会它。界面上必须能给出**基于实测**的提醒，否则控件就是在骗人。
# ==================================================================
def _config_with_effort(effort: str, **overrides: Any) -> ProviderConfig:
    base: dict[str, Any] = {
        "name": "测试配置",
        "base_url": "https://api.example.com",
        "api_key": "sk-x",
        "model_name": "reasoner-model",
        "context_window": 65536,
        "generation": GenerationParams(max_tokens=4096, reasoning_effort=effort),
    }
    base.update(overrides)
    return ProviderConfig(**base)


def test_probe_conclusion_travels_from_config_to_adapter() -> None:
    """★ 第十六轮的真 bug 守门人：**探测结论必须跟着适配器走**。

    背景：`reasoning_effort_supported` 挂在 `ProviderConfig` 上，而适配器只带走
    `default_params`（那是 `GenerationParams`，**没有**这个字段）。翻译中间件与剧情总结
    想读结论时属性根本不存在，上一版又用 `try/except` 把它吞了 ⇒
    "把翻译的思考降到最小"**静默失效了整整一轮**，而单测还绿着 ——
    因为那个测试伪造了一个真实代码里不存在的 API（`default_params.effective_reasoning_support()`）。
    这条断言钉住"结论确实在适配器上"，并且未知/不支持时必须是 None（不许瞎降）。
    """
    model = "probe-model"
    unknown = create_provider_from_config(_config_with_effort("auto", model_name=model))
    assert unknown.reasoning_support is None, "没探测过 ⇒ 不知道 ⇒ 不许发这个参数"

    unsupported = create_provider_from_config(
        _config_with_effort(
            "auto",
            model_name=model,
            reasoning_effort_supported=False,
            reasoning_effort_probed_model=model,
        )
    )
    assert unsupported.reasoning_support is False, "实测不接受 ⇒ 不许发"

    supported = create_provider_from_config(
        _config_with_effort(
            "auto",
            model_name=model,
            reasoning_effort_supported=True,
            reasoning_effort_probed_model=model,
        )
    )
    assert supported.reasoning_support is True, "实测有效 ⇒ 机械任务才可以降思考"

    # 结论过期（模型名换过）也算"不知道"
    stale = create_provider_from_config(
        _config_with_effort(
            "auto",
            model_name="another-model",
            reasoning_effort_supported=True,
            reasoning_effort_probed_model=model,
        )
    )
    assert stale.reasoning_support is None, "换了模型 ⇒ 旧结论作废"


def test_unprobed_effort_produces_hint_not_warning() -> None:
    """★ 设了思考强度但没验证过 —— 给建议，而不是报错误。

    不能一上来就吓唬用户说"你这个设置可能没用"，因为多数模型确实是支持的。
    """
    config = _config_with_effort("high")
    assert config.warnings() == [] or not any("忽略" in w for w in config.warnings())
    hints = config.hints()
    assert any("尚未验证" in h for h in hints)
    # 提示里要说明检测的成本，让用户有心理预期
    assert any("2 次" in h for h in hints)


def test_auto_effort_produces_no_hint() -> None:
    """auto 不干预厂商默认行为，不存在「设了没用」的问题，永远不提醒。"""
    assert _config_with_effort("auto").hints() == []


def test_probed_unsupported_effort_produces_warning() -> None:
    """★ 已知该模型忽略思考强度，用户还设了非 auto —— 必须给黄色警告。"""
    config = _config_with_effort(
        "high",
        reasoning_effort_supported=False,
        reasoning_effort_probed_model="reasoner-model",
    )
    warnings = config.warnings()
    assert any("忽略" in w and "auto" in w for w in warnings)
    # 已经探测过了，就不该再提示"去检测一下"
    assert not any("尚未验证" in h for h in config.hints())


def test_probed_unsupported_is_fine_when_effort_is_auto() -> None:
    """模型虽然忽略该参数，但用户用的是 auto —— 没有任何问题，不该报警。"""
    config = _config_with_effort(
        "auto",
        reasoning_effort_supported=False,
        reasoning_effort_probed_model="reasoner-model",
    )
    assert not any("忽略" in w for w in config.warnings())


def test_probed_supported_produces_no_warning() -> None:
    """实测支持 —— 设置照常生效，不该有任何提醒。"""
    config = _config_with_effort(
        "high",
        reasoning_effort_supported=True,
        reasoning_effort_probed_model="reasoner-model",
    )
    assert not any("忽略" in w for w in config.warnings())
    assert config.hints() == []


def test_probe_result_becomes_stale_after_model_change() -> None:
    """★ 换了模型，旧结论就不再适用 —— 不能拿过期结论误导用户。"""
    config = _config_with_effort(
        "high",
        model_name="another-model",
        reasoning_effort_supported=False,
        reasoning_effort_probed_model="reasoner-model",  # 探测的是另一个模型
    )

    assert config.reasoning_probe_is_stale is True
    assert config.effective_reasoning_support() is None
    # 不应该拿旧模型的结论去警告
    assert not any("忽略" in w for w in config.warnings())
    # 而应该提示重新检测
    assert any("失效" in h and "another-model" in h for h in config.hints())


def test_probe_result_not_stale_when_model_unchanged() -> None:
    config = _config_with_effort(
        "high",
        reasoning_effort_supported=False,
        reasoning_effort_probed_model="reasoner-model",
    )
    assert config.reasoning_probe_is_stale is False
    assert config.effective_reasoning_support() is False


def test_to_dict_exposes_support_state_and_both_message_lists() -> None:
    """接口返回里要能看出「探测状态」，且 warnings / hints 分开给。"""
    config = _config_with_effort(
        "high",
        reasoning_effort_supported=False,
        reasoning_effort_probed_model="reasoner-model",
        reasoning_effort_probed_at=datetime(2026, 9, 19, 12, 0, 0),
    )
    data = config.to_dict()

    support = data["reasoning_effort_support"]
    assert support["probed"] is True
    assert support["supported"] is False
    assert support["probed_model"] == "reasoner-model"
    assert support["probed_at"].startswith("2026-09-19")
    assert support["stale"] is False

    assert isinstance(data["warnings"], list)
    assert isinstance(data["hints"], list)


def test_probe_result_carries_persistable_fields() -> None:
    """★ 探测结果要能直接写库，否则结论只能"看一眼就没了"。"""
    from app.llm.diagnostics import probe_reasoning_effort

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_probe_body(140))

    provider = make_probe_provider(handler)
    result = probe_reasoning_effort(provider)

    persist = result["persist"]
    assert persist["reasoning_effort_supported"] is False
    assert persist["reasoning_effort_probed_model"] == "mock-model"
    assert isinstance(persist["reasoning_effort_probed_at"], datetime)

    # 写回配置后，界面应该能据此给出警告
    config = _config_with_effort("high", model_name="mock-model", **persist)
    assert any("忽略" in w for w in config.warnings())
    provider.close()


def test_persisted_probe_result_is_stale_for_different_model() -> None:
    """写库的结论带着模型名，换成别的模型后会自动失效。"""
    from app.llm.diagnostics import probe_reasoning_effort

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_probe_body(140))

    provider = make_probe_provider(handler)
    persist = probe_reasoning_effort(provider)["persist"]

    # 把结论挂到一个**不同模型**的配置上
    config = _config_with_effort("high", model_name="totally-different", **persist)
    assert config.reasoning_probe_is_stale is True
    assert not any("忽略" in w for w in config.warnings())
    provider.close()
