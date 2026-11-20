"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations


import pytest


from app.llm.params import ReasoningEffort

from app.llm.schema import ChatResult, TokenUsage


from app.narrative import translate as translate_mod

EN_REPLY = "Hello there, traveler. The road ahead is long."

ZH_REPLY = "你好，旅行者。前面的路还很长。"

ZH_INPUT = "你好呀，我该往哪走？"

EN_INPUT = "Hi there, which way should I go?"

JA_REPLY = "こんにちは、旅人さん。道はまだ長いです。"

KO_REPLY = "안녕하세요, 여행자님. 길이 아직 멉니다."

ZH_TRAD_REPLY = "你好，旅行者。前面的路還很長。"


def test_defaults_are_off_and_never_spend_tokens() -> None:
    """★ 默认必须关闭：本项目对"未经同意花用户 token"零容忍（与记忆总结同一条规矩）。"""
    settings = translate_mod.default_settings()
    assert settings["enabled"] is False
    assert translate_mod.uses_model(settings, translate_mod.DIRECTION_REPLY) is False
    assert translate_mod.prompt_hint(settings) == ""
    assert translate_mod.estimate_cost(settings, EN_REPLY) == 0


def test_disabling_forces_the_mode_to_off() -> None:
    """关掉总开关后模式必须归零：否则界面会显示"中间件翻译"却什么都没发生。"""
    settings = translate_mod.normalize_settings(
        {"enabled": False, "mode": "middleware", "direction": "both"}
    )
    assert settings["mode"] == translate_mod.MODE_OFF
    assert translate_mod.describe(settings) == "未启用"


def test_invalid_mode_and_direction_fall_back() -> None:
    settings = translate_mod.normalize_settings(
        {
            "enabled": True,
            "mode": "魔法",
            "direction": "两边",
            "provider_id": "7",
            "target_lang": "  ",
        }
    )
    assert settings["mode"] == translate_mod.MODE_MIDDLEWARE
    assert settings["direction"] == translate_mod.DIRECTION_REPLY
    assert settings["provider_id"] == 7, "数字字符串要能收"
    assert settings["target_lang"] == translate_mod.DEFAULT_TARGET_LANG


def test_direction_controls_each_side_independently() -> None:
    reply = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "direction": "reply"}
    )
    assert translate_mod.wants(reply, translate_mod.DIRECTION_REPLY) is True
    assert translate_mod.wants(reply, translate_mod.DIRECTION_INPUT) is False
    both = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "direction": "both"}
    )
    assert translate_mod.wants(both, translate_mod.DIRECTION_REPLY) is True
    assert translate_mod.wants(both, translate_mod.DIRECTION_INPUT) is True


def test_prompt_mode_costs_nothing_but_still_tells_the_model() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "prompt", "target_lang": "简体中文"}
    )
    hint = translate_mod.prompt_hint(settings)
    assert "简体中文" in hint and "输出语言" in hint
    assert translate_mod.uses_model(settings, translate_mod.DIRECTION_REPLY) is False
    assert translate_mod.estimate_cost(settings, EN_REPLY) == 0
    outcome = translate_mod.run(
        EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=None
    )
    assert outcome.used_model is False and "提示词模式" in outcome.skipped


def test_local_language_check_spares_the_call() -> None:
    """★ 已经是目标语言就不调模型 —— 这是真实省下来的钱，不是假装译了。"""
    assert translate_mod.looks_like(ZH_REPLY, "简体中文") is True
    assert translate_mod.looks_like(EN_REPLY, "简体中文") is False
    assert translate_mod.looks_like(EN_REPLY, "英文") is True
    assert translate_mod.looks_like(ZH_REPLY, "英文") is False
    assert translate_mod.looks_like("", "简体中文") is True
    assert translate_mod.looks_like("Привет", "简体中文") is False
    # ★ 第十六轮：源语言自动识别 —— 旧版把假名/谚文一起算进"CJK 占比"，
    #   于是日文、韩文、繁体回复都被判成"已经是简体中文"而**静默不译**。
    assert translate_mod.looks_like(JA_REPLY, "简体中文") is False, (
        "日文必须译，不能当成中文"
    )
    assert translate_mod.looks_like(KO_REPLY, "简体中文") is False, (
        "韩文必须译，不能当成中文"
    )
    assert translate_mod.looks_like(ZH_TRAD_REPLY, "简体中文") is False, (
        "繁体要转成简体"
    )
    assert translate_mod.looks_like(ZH_TRAD_REPLY, "繁體中文") is True, (
        "繁体是繁体目标本身"
    )
    assert translate_mod.looks_like(ZH_REPLY, "繁體中文") is False, "简体要转成繁体"
    assert translate_mod.looks_like(JA_REPLY, "日文") is True
    assert translate_mod.looks_like(ZH_REPLY, "日文") is False, "中文在日文目标下要译"
    assert translate_mod.looks_like(KO_REPLY, "韩文") is True
    assert translate_mod.looks_like(ZH_REPLY, "韩文") is False, "中文在韩文目标下要译"
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    outcome = translate_mod.run(
        ZH_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=None
    )
    assert outcome.used_model is False
    assert "看起来已经是" in outcome.skipped
    assert translate_mod.estimate_cost(settings, ZH_REPLY) == 0


def test_too_long_text_is_skipped_with_a_reason() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    outcome = translate_mod.run(
        "a" * (translate_mod.MAX_CHARS + 10),
        settings,
        direction=translate_mod.DIRECTION_REPLY,
        adapter=_Stub("译文"),
    )
    assert outcome.used_model is False and "太长" in outcome.skipped


class _Stub:
    """最简适配器替身（只实现 chat）。"""

    def __init__(self, content: str = "译文", *, boom: bool = False) -> None:
        self.content = content
        self.boom = boom
        self.calls = 0

    def chat(self, request):  # noqa: ANN001
        self.calls += 1
        if self.boom:
            raise RuntimeError("翻译服务挂了")
        result = ChatResult(
            content=self.content, model="stub-model", finish_reason="stop"
        )
        result.usage = TokenUsage(
            prompt_tokens=30, completion_tokens=20, total_tokens=50
        )
        return result


def test_success_records_text_tokens_and_which_side_to_display() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    stub = _Stub("你好，旅行者。")
    outcome = translate_mod.run(
        EN_REPLY,
        settings,
        direction=translate_mod.DIRECTION_REPLY,
        adapter=stub,
        provider_id=3,
    )
    assert stub.calls == 1
    assert outcome.ok and outcome.text == "你好，旅行者。"
    assert outcome.lang == "简体中文" and outcome.tokens == 50
    assert outcome.display == "translation", "输出侧：界面默认显示译文"
    assert outcome.provider_id == 3 and outcome.model == "stub-model"
    # 输入侧用**同一个**目标语言：用户用英文写，就译成简体中文再发给模型
    # （这一段的设置必须是"方向含输入"，否则输入侧根本不会处理）
    both = translate_mod.normalize_settings(
        {
            "enabled": True,
            "mode": "middleware",
            "direction": "both",
            "target_lang": "简体中文",
        }
    )
    outcome_in = translate_mod.run(
        EN_INPUT, both, direction=translate_mod.DIRECTION_INPUT, adapter=stub
    )
    assert outcome_in.lang == "简体中文", "两个方向共用一个目标语言（没有输入侧语言了）"
    assert outcome_in.used_model is True
    assert outcome_in.display == "translation", "translation.text 永远放给人看的那一份"
    # ★ 而中文用户写中文时：它就是目标语言 → 0 token 直发（这是"面向中文用户"的默认收益）
    already = translate_mod.run(
        ZH_INPUT, both, direction=translate_mod.DIRECTION_INPUT, adapter=stub
    )
    assert already.used_model is False, "用户写的就是目标语言 → 不花这次钱"


def test_model_failure_is_reported_not_raised() -> None:
    """★ 翻译失败绝不能影响对话（与剧情总结降级同一套哲学）。"""
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    outcome = translate_mod.run(
        EN_REPLY,
        settings,
        direction=translate_mod.DIRECTION_REPLY,
        adapter=_Stub(boom=True),
    )
    assert outcome.ok is False
    assert "翻译服务挂了" in outcome.error
    assert outcome.text == ""


def test_empty_model_output_is_treated_as_failure() -> None:
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    outcome = translate_mod.run(
        EN_REPLY,
        settings,
        direction=translate_mod.DIRECTION_REPLY,
        adapter=_Stub("   "),
    )
    assert outcome.ok is False and "空译文" in outcome.error


def test_will_call_model_matches_the_real_skip_rules() -> None:
    """★ 界面靠它决定"要不要显示『正在翻译…』"：判据必须与 `run()` 一致 ——
    **提示了却没译**比不提示更让人困惑（用户会盯着一句永远不消失的提示）。"""
    on = translate_mod.normalize_settings(
        {
            "enabled": True,
            "mode": "middleware",
            "direction": "reply",
            "target_lang": "简体中文",
        }
    )
    assert (
        translate_mod.will_call_model(on, translate_mod.DIRECTION_REPLY, EN_REPLY)
        is True
    )
    # 已经是目标语言 / 太长 / 空文本 ⇒ 不会调模型 ⇒ 不该提示
    assert (
        translate_mod.will_call_model(on, translate_mod.DIRECTION_REPLY, ZH_REPLY)
        is False
    )
    assert translate_mod.will_call_model(on, translate_mod.DIRECTION_REPLY, "") is False
    assert (
        translate_mod.will_call_model(
            on, translate_mod.DIRECTION_REPLY, "a" * (translate_mod.MAX_CHARS + 1)
        )
        is False
    )
    # 方向不含输入 ⇒ 输入侧不提示
    assert (
        translate_mod.will_call_model(on, translate_mod.DIRECTION_INPUT, EN_REPLY)
        is False
    )
    # 关掉总开关 / 提示词模式 都不调模型
    off = translate_mod.normalize_settings({"enabled": False, "mode": "middleware"})
    assert (
        translate_mod.will_call_model(off, translate_mod.DIRECTION_REPLY, EN_REPLY)
        is False
    )
    prompt = translate_mod.normalize_settings({"enabled": True, "mode": "prompt"})
    assert (
        translate_mod.will_call_model(prompt, translate_mod.DIRECTION_REPLY, EN_REPLY)
        is False
    )


class _EffortStub:
    """记录"这次调用带了多大思考强度"的适配器替身。

    ★ 第二十七轮改了它反映的契约：判据从"探测结论"（`reasoning_support`，
      靠比思考 token 数猜出来的、不可靠）换成适配器的**静态声明**
      `supports_reasoning_effort`（默认 True）。真实适配器上就是这个属性
      （`BaseLLMAdapter.__init__` 里声明，failover / whole_reply 会透传）。
    """

    def __init__(self, supports: bool = True) -> None:
        self.seen: list[object] = []
        self.supports_reasoning_effort = supports

    def chat(self, request):  # noqa: ANN001
        self.seen.append(request.reasoning_effort)
        return ChatResult(
            content="译文",
            model="stub-model",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )


def test_thinking_is_lowered_for_translation() -> None:
    """★ 翻译是机械任务，思考纯烧钱（实测：1618 字符的英文译一次烧 5291 token）。

    ==================== 第二十七轮的行为变更 ====================
    旧契约：只在"探测确认该模型接受思考强度"时才发这个参数。
    后果：DeepSeek 被那次不可靠的探测误判为"不支持" ⇒ 翻译**从来没降过思考**。
    新契约：**直接请求 OFF**；模型不认这个参数时由适配器自动去掉并重试一次
    （见 `openai_compatible._post_with_effort_fallback`），翻译不会因此失败。
    """
    settings = translate_mod.normalize_settings(
        {"enabled": True, "mode": "middleware", "target_lang": "简体中文"}
    )
    # ① 默认（适配器参与思考强度适配）⇒ 必须请求降到最小
    normal = _EffortStub()
    translate_mod.run(
        EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=normal
    )
    assert normal.seen == [ReasoningEffort.OFF], "翻译要一律降到最小思考"

    # ② 适配器显式声明"我不参与"⇒ 不覆盖，交给 provider 自己的设置
    opted_out = _EffortStub(supports=False)
    translate_mod.run(
        EN_REPLY, settings, direction=translate_mod.DIRECTION_REPLY, adapter=opted_out
    )
    assert opted_out.seen == [None], "适配器声明不支持时不该发这个参数"


pytestmark = pytest.mark.unit
