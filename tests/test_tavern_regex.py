"""酒馆正则脚本（`extensions.regex_scripts`）的解释器测试。

==================== 这些用例在守什么？====================
这一层的失败**不会报错**，只会"看起来没效果"：
  · 正则没跑 → 模型看到协议标签，剧情慢慢退化成格式复读；
  · 分层判据读错 → 该剥的没剥 / 该美化的没美化；
  · `merge_configs` 漏字段 → 整个功能**一条都不跑**（真实发生过一次）。

所以这里把语义钉死。**期望值不是我自己想的**：是拿酒馆自己的
`RegExp` 引擎（Node 里跑真实的 27 条脚本）逐条对出来的，24/24 逐字节一致；
另外与酒馆的**完整两遍时序**也对过（4/4 一致）。
详见 docs/pitfalls.md §44。

运行： .\\.venv\\Scripts\\python.exe -m pytest tests/test_tavern_regex.py -v
"""

from __future__ import annotations

import pytest

from app.narrative import presets, tavern_regex as tr

# ==================================================================
#  素材
# ==================================================================
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


# ==================================================================
#  解析：JS 的 `/pattern/flags` 字面量
# ==================================================================
def test_split_literal_parses_js_regex_literal() -> None:
    """`/abc/gi` → pattern=`abc`、flags=`gi`（酒馆的 regexFromString 同此）。"""
    assert tr._split_literal("/abc/gi") == ("abc", "gi")
    assert tr._split_literal("/a\\/b/g") == ("a\\/b", "g")


def test_split_literal_handles_missing_slash_form() -> None:
    """★ 没有斜杠包裹时**整串当 pattern**（真实预设里就有这样一条）。

    这与酒馆一致：它的 `regexFromString` 用 `(\\/?)(.+)\\1([a-z]*)` 去 match，
    匹配失败就回落成 `RegExp(整个输入)`。
    """
    raw = "^(?!<think>)([\\s\\S]*\\S[\\s\\S]*)(?:</think>)"
    pattern, flags = tr._split_literal(raw)
    assert pattern == raw
    assert flags == ""


def test_split_literal_rejects_illegal_flags() -> None:
    """flag 非法（重复字符 / 不在允许集）→ 整串当 pattern，与酒馆一致。"""
    pattern, flags = tr._split_literal("/abc/gg")
    assert pattern == "/abc/gg"
    assert flags == ""


def test_named_group_syntax_is_converted() -> None:
    """JS 的 `(?<name>…)` 要转成 Python 的 `(?P<name>…)`，**但回顾断言不能动**。"""
    assert tr._to_python_pattern("(?<year>\\d{4})") == "(?P<year>\\d{4})"
    # (?<= / (?<! 是回顾断言，Python 语法相同，必须原样保留
    assert tr._to_python_pattern("(?<=a)b") == "(?<=a)b"
    assert tr._to_python_pattern("(?<!a)b") == "(?<!a)b"


# ==================================================================
#  执行：替换语义
# ==================================================================
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


def test_non_global_regex_replaces_only_first() -> None:
    """没有 `g` 的 JS 正则只替换**第一处**（`String.replace` 的语义）。"""
    compiled = _compile(_script(findRegex="/a/", replaceString="X"))
    assert compiled[0].global_replace is False
    out, hits, _err = tr.apply_one("aaa", compiled[0])
    assert out == "Xaa"
    assert hits == 1


def test_global_regex_replaces_all() -> None:
    compiled = _compile(_script(findRegex="/a/g", replaceString="X"))
    assert compiled[0].global_replace is True
    out, hits, _err = tr.apply_one("aaa", compiled[0])
    assert out == "XXX"
    assert hits == 3


# ==================================================================
#  分层判据：三个 flag（这是最容易读错的地方）
# ==================================================================
def test_flag_matrix_matches_sillytavern() -> None:
    """★ 四个组合的命中条件（照抄 engine.js:348-355）。

    ★ 特别注意"两个都不开"那一行：它不是"不生效"，
      而是跑在**清理正文**那一遍（`!isMarkdown && !isPrompt`）——
      影响最深的一类。我第一版把这一类读成了"不生效（？）"，是错的。
    """
    both = tr.RegexScript.from_dict(_script(markdownOnly=True, promptOnly=True))
    md = tr.RegexScript.from_dict(_script(markdownOnly=True, promptOnly=False))
    po = tr.RegexScript.from_dict(_script(markdownOnly=False, promptOnly=True))
    neither = tr.RegexScript.from_dict(_script(markdownOnly=False, promptOnly=False))

    # 显示那一遍（isMarkdown=True）
    assert tr._flag_matches(both, is_markdown=True, is_prompt=False)
    assert tr._flag_matches(md, is_markdown=True, is_prompt=False)
    assert not tr._flag_matches(po, is_markdown=True, is_prompt=False)
    assert not tr._flag_matches(neither, is_markdown=True, is_prompt=False)

    # 提示词那一遍（is_prompt=True）
    assert tr._flag_matches(both, is_markdown=False, is_prompt=True)
    assert not tr._flag_matches(md, is_markdown=False, is_prompt=True)
    assert tr._flag_matches(po, is_markdown=False, is_prompt=True)
    assert not tr._flag_matches(neither, is_markdown=False, is_prompt=True)

    # 清理正文那一遍（两者皆 False）
    assert not tr._flag_matches(both, is_markdown=False, is_prompt=False)
    assert not tr._flag_matches(md, is_markdown=False, is_prompt=False)
    assert not tr._flag_matches(po, is_markdown=False, is_prompt=False)
    assert tr._flag_matches(neither, is_markdown=False, is_prompt=False)


def test_placement_filter() -> None:
    """placement 不含当前场景就不跑。"""
    compiled = _compile(_script(placement=[1]))  # 只作用于用户输入
    out, stats = tr.apply_scripts(
        "<dream_body>", compiled, placement=tr.PLACEMENT_AI_OUTPUT, is_prompt=True
    )
    assert out == "<dream_body>", "placement 不匹配却跑了"
    assert stats.scripts_skipped_placement == 1


# ==================================================================
#  深度条件
# ==================================================================
def test_min_depth_skips_recent_messages() -> None:
    """`minDepth=2` = 只对"倒数第 2 条及更早"生效（engine.js:363）。"""
    compiled = _compile(_script(findRegex="/X/g", minDepth=2))
    # depth=0（最后一条）不该跑
    out, _ = tr.apply_scripts("X", compiled, placement=2, is_prompt=True, depth=0)
    assert out == "X"
    # depth=2 该跑
    out, _ = tr.apply_scripts("X", compiled, placement=2, is_prompt=True, depth=2)
    assert out == ""
    # depth=5 也该跑（minDepth 是下界）
    out, _ = tr.apply_scripts("X", compiled, placement=2, is_prompt=True, depth=5)
    assert out == ""


def test_max_depth_skips_old_messages() -> None:
    """`maxDepth=10` = 只对"倒数第 10 条以内（最近的那批）"生效。"""
    compiled = _compile(_script(findRegex="/X/g", maxDepth=10))
    out, _ = tr.apply_scripts("X", compiled, placement=2, is_prompt=True, depth=3)
    assert out == "", "depth=3 在 maxDepth=10 之内，应当生效"
    out, _ = tr.apply_scripts("X", compiled, placement=2, is_prompt=True, depth=50)
    assert out == "X", "depth=50 超出 maxDepth，不该生效"


def test_depth_none_means_no_depth_filtering() -> None:
    """★ `depth=None` 时酒馆**整段跳过**深浅判断（`typeof depth === 'number'`）。

    所以 None 的语义是"不做深度过滤"，不是"都不通过"。
    """
    compiled = _compile(_script(findRegex="/X/g", minDepth=5, maxDepth=6))
    out, _ = tr.apply_scripts("X", compiled, placement=2, is_prompt=True, depth=None)
    assert out == "", "depth=None 时不该做深度过滤"


# ==================================================================
#  顺序与组合
# ==================================================================
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


def test_combined_names_run_ordered_and_counted() -> None:
    compiled = _compile(
        _script(id="a", findRegex="/1/g", replaceString="2"),
        _script(id="b", findRegex="/2/g", replaceString="3"),
    )
    out, stats = tr.apply_scripts("1", compiled, placement=2, is_prompt=True)
    assert out == "3"
    assert stats.replacements == 2, "替换次数要把两步都算上"


# ==================================================================
#  如实报告：跑不了的要说得出来
# ==================================================================
def test_compile_failure_is_reported_not_silent() -> None:
    """正则编译不过时**必须留下痕迹**（不能安静地看起来"没效果"）。"""
    compiled = _compile(_script(findRegex="/(unclosed/g"))
    assert compiled[0].usable is False
    out, stats = tr.apply_scripts(
        "text", compiled, placement=2, is_prompt=True
    )
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


# ==================================================================
#  ★ 安全：ReDoS 必须有护栏
# ==================================================================
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


# ==================================================================
#  ★ 装配层接线（含"不改数据库"这条约定）
# ==================================================================
def test_apply_to_plan_strips_tags_and_keeps_db_untouched() -> None:
    """装配层：标签被剥掉，且**原始消息对象不被就地改动**。"""
    from app.llm.schema import ChatMessage
    from app.narrative.prompt_builder import PromptPlan

    dirty = "<dream_body>正文</dream_body><dream_option id=\"1\">A</dream_option>"
    history = ChatMessage(role="assistant", content=dirty)
    plan = PromptPlan(
        messages=[ChatMessage.system("sys"), history], system_prompt="sys"
    )
    extensions = {"regex_scripts": [_script(findRegex="/<dream_body>|<\\/dream_body>|<dream_option[^>]*>|<\\/dream_option>/g")]}

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
    plan = PromptPlan(messages=[ChatMessage.system("s"), ChatMessage(role="assistant", content=dirty)])
    stats = tr.apply_to_plan(plan, {})
    assert stats.scripts_run == 0
    assert plan.messages[1].content == dirty


def test_apply_to_plan_reports_errors_to_caller() -> None:
    """执行出错要经 `on_error` 交给调用方（进 warnings），不能吞掉。"""
    from app.llm.schema import ChatMessage
    from app.narrative.prompt_builder import PromptPlan

    plan = PromptPlan(messages=[ChatMessage.system("s"), ChatMessage(role="assistant", content="x")])
    seen: list[str] = []
    tr.apply_to_plan(
        plan,
        {"regex_scripts": [_script(findRegex="/(unclosed/g")]},
        on_error=seen.append,
    )
    assert seen, "编译失败没有传给调用方"


# ==================================================================
#  ★ 回归：merge_configs 必须保住 raw_extensions
# ==================================================================
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
        blocks=[presets.PromptBlock(identifier="hneGuard1", content="G", order_index=0)],
    )
    merged = presets.merge_configs(base, guard)
    assert merged is not None
    assert merged.raw_extensions == {"regex_scripts": [{"scriptName": "x"}]}, (
        "合并后 raw_extensions 丢了 —— 酒馆正则会整个静默失效"
    )
    # 守卫自己的 extensions（理论上没有）不该混进来
    guard_with_ext = presets.PromptPresetConfig(
        blocks=[presets.PromptBlock(identifier="hneGuard2", content="G", order_index=0)],
        raw_extensions={"regex_scripts": [{"scriptName": "guard"}]},
    )
    merged2 = presets.merge_configs(base, guard_with_ext)
    assert merged2.raw_extensions == {"regex_scripts": [{"scriptName": "x"}]}
