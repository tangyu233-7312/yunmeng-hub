"""Independent business rules; see docs/测试计划.md for risk and scope."""

from __future__ import annotations

import pytest

from app.narrative import presets, tavern_regex as tr


def _script(**over):
    """造一条脚本（默认值贴近真实预设的常见形状）。"""
    base = {
        "id": "s1",
        "scriptName": "测试脚本",
        "findRegex": "/<dream_body>/g",
        "replaceString": "",
        "placement": [2],
        "disabled": False,
        "markdownOnly": False,
        "promptOnly": True,
        "minDepth": None,
        "maxDepth": None,
        "trimStrings": [],
        "runOnEdit": True,
        "substituteRegex": 0,
    }
    base.update(over)
    return base


def _compile(*scripts):
    return tr.compile_scripts([tr.RegexScript.from_dict(s) for s in scripts])


def test_replacement_expands_numbered_groups() -> None:
    """`$1` 要展开成捕获组内容（JS 写法），而不是 Python 的 `\\1`。"""
    compiled = _compile(
        _script(findRegex="/(a)(b)/g", replaceString="[$2$1]"),
    )
    out, hits, err = tr.apply_one("ab", compiled[0])
    assert out == "[ba]"
    assert hits == 1
    assert err == ""


def test_missing_group_becomes_empty_not_literal() -> None:
    """★ 引用了不存在的组 → 替换成**空串**，绝不能把 `$9` 原样留在文本里。

    照抄酒馆 engine.js:432-435（`if (!match) return ''`）。
    """
    compiled = _compile(_script(findRegex="/(a)/g", replaceString="[$9]"))
    out, _hits, _err = tr.apply_one("a", compiled[0])
    assert out == "[]", f"不存在的组没被吃成空串：{out!r}"


def test_match_placeholder_is_supported() -> None:
    """`{{match}}`（不分大小写）等价于 `$0`（engine.js:421）。"""
    compiled = _compile(_script(findRegex="/b/g", replaceString="<{{MATCH}}>"))
    out, _hits, _err = tr.apply_one("abc", compiled[0])
    assert out == "a<b>c"


def test_trim_strings_are_stripped_from_captured_group() -> None:
    """捕获组内容要过一遍 `trimStrings`（engine.js:438 / 457-465）。"""
    compiled = _compile(
        _script(
            findRegex="/x(\\w+)x/g",
            replaceString="$1",
            trimStrings=["drop"],
        )
    )
    out, _hits, _err = tr.apply_one("xadropbx", compiled[0])
    assert out == "ab"


def test_placement_filter() -> None:
    """placement 不含当前场景就不跑。"""
    compiled = _compile(_script(placement=[1]))  # 只作用于用户输入
    out, stats = tr.apply_scripts(
        "<dream_body>", compiled, placement=tr.PLACEMENT_AI_OUTPUT, is_prompt=True
    )
    assert out == "<dream_body>", "placement 不匹配却跑了"
    assert stats.scripts_skipped_placement == 1


def test_scripts_run_in_list_order() -> None:
    """★ 按清单顺序依次作用（前一条的输出是后一条的输入）。

    真实预设依赖这一点：例如"把 `</think>` 删掉"必须排在
    "把思考段折成 `<details>`"之后，否则后者就匹配不到了。
    """
    compiled = _compile(
        _script(id="a", findRegex="/foo/g", replaceString="bar"),
        _script(id="b", findRegex="/bar/g", replaceString="baz"),
    )
    out, stats = tr.apply_scripts("foo", compiled, placement=2, is_prompt=True)
    assert out == "baz", f"顺序不对：{out!r}"
    assert stats.scripts_run == 2


def test_compile_failure_is_reported_not_silent() -> None:
    """正则编译不过时**必须留下痕迹**（不能安静地看起来"没效果"）。"""
    compiled = _compile(_script(findRegex="/(unclosed/g"))
    assert compiled[0].usable is False
    out, stats = tr.apply_scripts("text", compiled, placement=2, is_prompt=True)
    assert out == "text"
    assert stats.errors, "编译失败没有报出来"
    assert "编译失败" in stats.errors[0] or "unclosed" in stats.errors[0]


def test_substitute_regex_nonzero_is_reported_unsupported() -> None:
    """`substituteRegex≠0` 需要先对正则做宏替换，本系统不支持 —— 必须报出来。"""
    scripts = [tr.RegexScript.from_dict(_script(substituteRegex=1))]
    notes = tr.describe_unsupported(scripts)
    assert notes and "substituteRegex" in notes[0]
    # 编译结果也应标为不可用
    assert tr.compile_scripts(scripts)[0].usable is False


def test_disabled_scripts_are_skipped() -> None:
    compiled = _compile(_script(disabled=True))
    assert compiled == [], "停用的脚本不该进编译清单"


def test_unwired_placement_is_reported() -> None:
    """作用于我们没接钩子的位置（世界书等）要说出来，否则用户以为全都跑了。"""
    scripts = [tr.RegexScript.from_dict(_script(placement=[tr.PLACEMENT_WORLD_INFO]))]
    notes = tr.describe_unwired_placements(scripts)
    assert notes and "世界书" in notes[0]


def test_catastrophic_backtracking_is_stopped_by_timeout() -> None:
    """★ 灾难性回溯必须被超时掐掉，而不是把请求挂死。

    这是真实威胁：酒馆正则**来自网上**。实测标准库 `re` 跑
    `([\\s\\S]*)*$` 配 26 个字符要 10.8 秒（见 tavern_regex 模块文档）。
    """
    if not tr.HAS_TIMEOUT_ENGINE:  # pragma: no cover
        pytest.skip("本机没装 regex 模块（退化成不跑正则，不会挂死）")
    compiled = _compile(_script(findRegex="/(?:a|aa)+b/g", replaceString="Q"))
    assert compiled[0].usable
    evil = "a" * 40  # 没有 b 结尾 → 指数级回溯
    out, hits, err = tr.apply_one(evil, compiled[0], timeout=0.3)
    assert out == evil, "超时后应当原样返回"
    assert hits == 0
    assert "超时" in err, f"超时没有被如实报告：{err!r}"


def test_timeout_error_is_surfaced_in_stats() -> None:
    """超时的脚本要在统计里报出来（不能静默跳过）。"""
    if not tr.HAS_TIMEOUT_ENGINE:  # pragma: no cover
        pytest.skip("本机没装 regex 模块")
    compiled = _compile(_script(findRegex="/(?:a|aa)+b/g", replaceString="Q"))
    _out, stats = tr.apply_scripts(
        "a" * 40, compiled, placement=2, is_prompt=True, timeout=0.3
    )
    assert stats.errors, "超时没有进错误清单"


def test_apply_to_plan_strips_tags_and_keeps_db_untouched() -> None:
    """装配层：标签被剥掉，且**原始消息对象不被就地改动**。"""
    from app.llm.schema import ChatMessage
    from app.narrative.prompt_builder import PromptPlan

    dirty = '<dream_body>正文</dream_body><dream_option id="1">A</dream_option>'
    history = ChatMessage(role="assistant", content=dirty)
    plan = PromptPlan(
        messages=[ChatMessage.system("sys"), history], system_prompt="sys"
    )
    extensions = {
        "regex_scripts": [
            _script(
                findRegex="/<dream_body>|<\\/dream_body>|<dream_option[^>]*>|<\\/dream_option>/g"
            )
        ]
    }

    stats = tr.apply_to_plan(plan, extensions)

    assert stats.scripts_run >= 1
    assert "<dream_body>" not in plan.messages[1].content
    assert "正文" in plan.messages[1].content
    # ★ 这条最重要：数据库/调用方拿到的那条消息必须原封不动
    assert history.content == dirty, "提示词层的正则**不该**改到原始消息"
    # system 消息不受影响
    assert plan.messages[0].content == "sys"


def test_apply_to_plan_noop_without_scripts() -> None:
    """没有正则脚本时行为必须与改造前完全一致（空 extensions 不碰任何东西）。"""
    from app.llm.schema import ChatMessage
    from app.narrative.prompt_builder import PromptPlan

    dirty = "<dream_body>x</dream_body>"
    plan = PromptPlan(
        messages=[ChatMessage.system("s"), ChatMessage(role="assistant", content=dirty)]
    )
    stats = tr.apply_to_plan(plan, {})
    assert stats.scripts_run == 0
    assert plan.messages[1].content == dirty


def test_apply_to_plan_reports_errors_to_caller() -> None:
    """执行出错要经 `on_error` 交给调用方（进 warnings），不能吞掉。"""
    from app.llm.schema import ChatMessage
    from app.narrative.prompt_builder import PromptPlan

    plan = PromptPlan(
        messages=[ChatMessage.system("s"), ChatMessage(role="assistant", content="x")]
    )
    seen: list[str] = []
    tr.apply_to_plan(
        plan,
        {"regex_scripts": [_script(findRegex="/(unclosed/g")]},
        on_error=seen.append,
    )
    assert seen, "编译失败没有传给调用方"


def test_merge_configs_keeps_raw_extensions() -> None:
    """★ 这条守的是一个**真实发生过的静默失效**。

    `resolve_preset()` 会把内置守卫预设**合并**进用户预设。第一版
    `merge_configs` 只搬了 blocks/sampling/warnings 三个字段，
    于是 `raw_extensions` 恒为空 —— C 档的正则**一条都不会跑**，
    而且不报任何错，表现成"功能做好了但完全没效果"。
    """
    base = presets.PromptPresetConfig(
        blocks=[presets.PromptBlock(identifier="a", content="A", order_index=0)],
        raw_extensions={"regex_scripts": [{"scriptName": "x"}]},
    )
    guard = presets.PromptPresetConfig(
        blocks=[
            presets.PromptBlock(identifier="hneGuard1", content="G", order_index=0)
        ],
    )
    merged = presets.merge_configs(base, guard)
    assert merged is not None
    assert merged.raw_extensions == {"regex_scripts": [{"scriptName": "x"}]}, (
        "合并后 raw_extensions 丢了 —— 酒馆正则会整个静默失效"
    )
    # 守卫自己的 extensions（理论上没有）不该混进来
    guard_with_ext = presets.PromptPresetConfig(
        blocks=[
            presets.PromptBlock(identifier="hneGuard2", content="G", order_index=0)
        ],
        raw_extensions={"regex_scripts": [{"scriptName": "guard"}]},
    )
    merged2 = presets.merge_configs(base, guard_with_ext)
    assert merged2.raw_extensions == {"regex_scripts": [{"scriptName": "x"}]}


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "pattern,text,replacement,expected,hits",
    [
        ("/a/", "aaa", "X", "Xaa", 1),
        ("/a/g", "aaa", "X", "XXX", 3),
        ("abc", "abc", "X", "X", 1),
        (r"/a\/b/g", "a/b", "X", "X", 1),
        (r"/(?<year>\d{4})/g", "2026", "$1", "2026", 1),
        ("/(?<=a)b/g", "ab", "X", "aX", 1),
        ("/(?<!a)b/g", "cb", "X", "cX", 1),
        ("/abc/gg", "abc", "X", "abc", 0),
    ],
    ids=[
        "first",
        "global",
        "bare",
        "slash",
        "named-group",
        "lookbehind",
        "negative-lookbehind",
        "invalid-flags",
    ],
)
def test_regex_execution(pattern, text, replacement, expected, hits):
    out, stats = tr.apply_scripts(
        text,
        _compile(_script(findRegex=pattern, replaceString=replacement)),
        placement=2,
        is_prompt=True,
    )
    assert out == expected
    assert stats.replacements == hits


@pytest.mark.parametrize(
    "markdown,prompt,phase_md,phase_prompt,applies",
    [
        (True, True, True, False, True),
        (True, False, True, False, True),
        (False, True, True, False, False),
        (False, False, True, False, False),
        (True, True, False, True, True),
        (True, False, False, True, False),
        (False, True, False, True, True),
        (False, False, False, True, False),
        (True, True, False, False, False),
        (True, False, False, False, False),
        (False, True, False, False, False),
        (False, False, False, False, True),
    ],
)
def test_regex_phase_selection(markdown, prompt, phase_md, phase_prompt, applies):
    scripts = _compile(
        _script(findRegex="/X/g", markdownOnly=markdown, promptOnly=prompt)
    )
    out, stats = tr.apply_scripts(
        "X", scripts, placement=2, is_markdown=phase_md, is_prompt=phase_prompt
    )
    assert out == ("" if applies else "X")
    assert stats.replacements == int(applies)


@pytest.mark.parametrize(
    "minimum,maximum,depth,applies",
    [
        (2, None, 0, False),
        (2, None, 2, True),
        (2, None, 5, True),
        (None, 10, 3, True),
        (None, 10, 50, False),
        (5, 6, None, True),
    ],
)
def test_regex_depth_selection(minimum, maximum, depth, applies):
    scripts = _compile(_script(findRegex="/X/g", minDepth=minimum, maxDepth=maximum))
    out, _ = tr.apply_scripts("X", scripts, placement=2, is_prompt=True, depth=depth)
    assert out == ("" if applies else "X")
