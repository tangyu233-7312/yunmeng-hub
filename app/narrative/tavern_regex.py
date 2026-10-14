"""酒馆**正则脚本**（regex scripts）解释器：让预设里那几十条正则真的生效。

==================== 它是干什么的 ====================
酒馆预设除了"提示词块"，还有一整套**正则脚本**（`extensions.regex_scripts`）。
用户那份「梦鲸思客」预设里就有 27 条（启用 24 条，约 112 KB）。它们做两类事：

1. **把协议标签从发给模型的内容里剥掉**（`promptOnly`）
   模型按预设要求输出 `<dream_body>` / `<dream_option>` / `<UpdateVariable>`
   这类 XML 标签。如果每一轮都把这些标签原样回灌进历史，模型会越来越倾向
   "模仿标签格式"，剧情会退化成格式复读。所以预设用正则把它们**只从发给模型的
   那一份里**删掉（数据库里仍是模型原话）。
2. **把标签渲染成界面卡片**（`markdownOnly`）
   状态栏、选项框、平行事件、思考折叠 —— 这些"好看的样子"全由正则产出 HTML。

在此之前本系统**完全跑不了**它们，而且是**一声不响地丢弃**。
用户 2026-10-14 问"导入时什么被忽略了"，这就是最大的一块。

==================== 语义是照酒馆来的（别自己发明）====================
依据（全部读自酒馆本体 1.19.0，并跑真实文件逐条对过）：

  · 脚本字段与执行     public/scripts/extensions/regex/engine.js::getRegexedString
  · 三个 flag 的判据   engine.js:348-355（markdownOnly / promptOnly / 都不开）
  · 深度条件           engine.js:362-372（minDepth / maxDepth）
  · 正则字面量解析     public/scripts/utils.js:1387 regexFromString
  · 替换串语义         engine.js:419-445（`{{match}}`、`$N`、`$<name>`、trimStrings）
  · 深度怎么算         public/script.js:4504（coreChat 里"后面还剩几条"）

==================== 两条刻意的取舍（都写在这里，不藏着）====================
**取舍 1：`markdownOnly`/`promptOnly` 都不开的脚本，我们改在"装配时"跑。**
  酒馆里这一类跑在 `cleanUpMessage`（生成后、**落库前**），改的是**存下来的正文**；
  于是之后每轮装配拿到的都是清理过的历史。
  本系统有一条硬约定：**提示词层的东西不改数据库**（会话历史里始终是模型原话，
  用户随时能回看）。所以我们把这一类也在**装配提示词时**应用 —— 对"模型看到什么"
  的效果与酒馆一致，同时保住了"数据库是原话"。
  ⇒ 代价：本系统里 `messages.content` 永远是原话；界面要看清干净的样子走显示层。

**取舍 2：`substituteRegex` 只支持 0（原样）。**
  1 / 2 需要先对**正则本身**做宏替换（酒馆的 `substituteParamsExtended`），
  本系统没有对应能力。遇到非 0 会如实报告，而不是猜。

==================== 安全：ReDoS 必须挡 ====================
酒馆正则是**从网上拿来的**（预设可以随便分享），一条恶意回溯就能把后端 CPU
打满 —— 实测标准库 `re` 跑 `([\\s\\S]*)*$` 配 26 个字符就要 **10.8 秒**。
所以这里用 `regex` 模块（唯一能做"带超时的正则执行"的选择），
每条替换都有 `timeout`；超时按"这条脚本本次不生效"处理并记账，**绝不让它拖死请求**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from app.llm.schema import ChatMessage

try:  # pragma: no cover - 取决于环境是否装了 regex
    import regex as _regex

    HAS_TIMEOUT_ENGINE = True
except ImportError:  # pragma: no cover
    _regex = None  # type: ignore[assignment]
    HAS_TIMEOUT_ENGINE = False

# ==================================================================
#  常量
# ==================================================================
#: 酒馆的 placement 枚举（engine.js:281-292）
PLACEMENT_MD_DISPLAY = 0
PLACEMENT_USER_INPUT = 1
PLACEMENT_AI_OUTPUT = 2
PLACEMENT_SLASH_COMMAND = 3
PLACEMENT_WORLD_INFO = 5
PLACEMENT_REASONING = 6

PLACEMENT_LABELS: dict[int, str] = {
    PLACEMENT_MD_DISPLAY: "Markdown 显示（已废弃）",
    PLACEMENT_USER_INPUT: "用户输入",
    PLACEMENT_AI_OUTPUT: "模型输出",
    PLACEMENT_SLASH_COMMAND: "斜杠命令",
    PLACEMENT_WORLD_INFO: "世界书",
    PLACEMENT_REASONING: "思考过程",
}

#: 单条正则的**兜底**保护（防一条正则跑太久把请求拖住）
#: ★ 1 秒是"远大于正常需要、又远小于用户能忍受的卡顿"的量级：
#:   真实那 24 条正则逐条跑完都在毫秒级；这里只兜住 ReDoS 那种指数级回溯。
DEFAULT_TIMEOUT_SECONDS = 1.0

#: 防御性上限（不是校验层，校验层在导入时做；这里是"万一库里有脏数据"）
MAX_SCRIPTS = 500
MAX_PATTERN_CHARS = 20_000

#: JS 的 flag 字母 → `regex` 模块的标志位
#: · g（全局）不在这里 —— 它决定"替换一处还是全部"，单独处理
#: · u（unicode）在 Python 3 里是默认行为，忽略
#: · X 是 JS 的 extended（忽略模式里的空白），对应 Python 的 VERBOSE
_JS_FLAG_MAP: dict[str, str] = {
    "i": "I",
    "m": "M",
    "s": "S",
    "x": "X",
    "X": "X",
    "U": "U",  # ungreedy：把贪婪量词变懒惰
    "A": "A",  # ASCII
    "J": "J",  # 允许重名捕获组
}

#: JS 的字面量形式 `/pattern/flags`。**照抄** JS 的解析正则
#: （utils.js:1390 的 `/(\/?)(.+)\1([a-z]*)/i`）：
#: 注意 `(.+)` 是**贪婪**的、而且 JS 的 `.` 不跨行 —— 这里也**不加 DOTALL**，
#: 否则遇到含换行的 pattern 会得到与酒馆不同的结果。
#: 还要注意 group(2) 可能吃掉整串（无斜杠形式），这正是酒馆的行为。
_JS_LITERAL_RE = re.compile(r"(/?)(.+)\1([a-z]*)", re.IGNORECASE)

#: JS 的"非法 flag"判据（utils.js:1393）：只允许这些字母且不能重复
_JS_VALID_FLAGS_RE = re.compile(r"^(?!.*?(.).*?\1)[gmixXsuUAJ]+$")

#: 命名捕获组：JS 写 `(?<name>`，Python 写 `(?P<name>`。
#: ★ 必须排除 `(?<=` / `(?<!`（那两种是回顾断言，Python 语法相同，不能动）。
_JS_NAMED_GROUP_RE = re.compile(r"\(\?<([A-Za-z_$][\w$]*)>")

#: 替换串里的 `$N` / `$<name>`（engine.js:422）
_REPLACEMENT_GROUP_RE = re.compile(r"\$(\d+)|\$<([^>]+)>")


@dataclass
class RegexScript:
    """一条酒馆正则脚本（字段名与酒馆一致，便于和原文件对照）。"""

    id: str = ""
    name: str = ""
    find_regex: str = ""
    replace_string: str = ""
    placement: list[int] = field(default_factory=list)
    disabled: bool = False
    markdown_only: bool = False
    prompt_only: bool = False
    min_depth: int | None = None
    max_depth: int | None = None
    trim_strings: list[str] = field(default_factory=list)
    run_on_edit: bool = True
    substitute_regex: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, item: Any) -> "RegexScript | None":
        """把酒馆的一条脚本解析成结构；不是字典就返回 None（不让脏数据炸掉整份预设）。"""
        if not isinstance(item, dict):
            return None

        def _as_int(value: Any) -> int | None:
            if value is None or value == "":
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        placement_raw = item.get("placement")
        placement: list[int] = []
        if isinstance(placement_raw, list):
            for p in placement_raw:
                parsed = _as_int(p)
                if parsed is not None:
                    placement.append(parsed)
        else:
            parsed = _as_int(placement_raw)
            if parsed is not None:
                placement.append(parsed)

        trims = item.get("trimStrings")
        return cls(
            id=str(item.get("id") or ""),
            name=str(item.get("scriptName") or ""),
            find_regex=str(item.get("findRegex") or ""),
            replace_string=str(item.get("replaceString") or ""),
            placement=placement,
            disabled=bool(item.get("disabled")),
            markdown_only=bool(item.get("markdownOnly")),
            prompt_only=bool(item.get("promptOnly")),
            min_depth=_as_int(item.get("minDepth")),
            max_depth=_as_int(item.get("maxDepth")),
            trim_strings=[str(t) for t in trims] if isinstance(trims, list) else [],
            run_on_edit=bool(item.get("runOnEdit", True)),
            substitute_regex=_as_int(item.get("substituteRegex")) or 0,
            raw=item,
        )


def load_scripts(extensions: Any) -> list[RegexScript]:
    """从酒馆预设的 `extensions` 里取出正则脚本清单。

    ★ 只认 `extensions.regex_scripts`。取不到就返回空列表 ——
      调用方据此走"没有脚本"的老路径，行为与本功能上线前完全一致。
    """
    if not isinstance(extensions, dict):
        return []
    raw = extensions.get("regex_scripts")
    if not isinstance(raw, list):
        return []
    scripts: list[RegexScript] = []
    for item in raw[:MAX_SCRIPTS]:
        parsed = RegexScript.from_dict(item)
        if parsed is not None:
            scripts.append(parsed)
    return scripts


# ==================================================================
#  解析与编译
# ==================================================================
def _split_literal(text: str) -> tuple[str, str]:
    """把 `/pattern/flags` 拆开；不是字面量形式就整串当 pattern（酒馆同此）。

    对应酒馆的 `regexFromString`：它的做法是拿 `/(\\/?)(.+)\\1([a-z]*)/i` 去 match，
    失败或 flag 非法时回落成 `RegExp(整个输入)`。
    """
    t = (text or "").strip()
    if not t:
        return "", ""
    m = _JS_LITERAL_RE.match(t)
    if not m:
        return t, ""
    flags = m.group(3) or ""
    # flag 非法（有重复字符或不在允许集里）→ 整串当 pattern，与酒馆一致
    if flags and not _JS_VALID_FLAGS_RE.match(flags):
        return t, ""
    pattern = m.group(2) or ""
    return pattern, flags


def _to_python_pattern(pattern: str) -> str:
    """把 JS 正则里 Python 不认的写法转过来（目前只需要转命名组）。"""

    def _rename(match: re.Match[str]) -> str:
        name = match.group(1).replace("$", "_")
        return f"(?P<{name}>"

    return _JS_NAMED_GROUP_RE.sub(_rename, pattern)


def _build_flags(flags: str) -> Any:
    value = 0
    for ch in flags:
        attr = _JS_FLAG_MAP.get(ch)
        if not attr:
            continue  # g / u / y 等：要么单独处理，要么无对应
        value |= getattr(_regex, attr, 0)
    return value


@dataclass
class CompiledScript:
    """编译好的一条脚本（编译失败时 `pattern` 为 None）。"""

    script: RegexScript
    pattern: Any = None
    global_replace: bool = True
    error: str = ""

    @property
    def usable(self) -> bool:
        return self.pattern is not None


def compile_scripts(scripts: list[RegexScript]) -> list[CompiledScript]:
    """预编译；编译不过的记下原因（**不静默跳过**，由调用方汇总报告）。"""
    out: list[CompiledScript] = []
    for s in scripts:
        if s.disabled:
            continue
        pattern_text, flags = _split_literal(s.find_regex)
        if not pattern_text:
            out.append(CompiledScript(s, None, error="正则为空"))
            continue
        if len(pattern_text) > MAX_PATTERN_CHARS:
            out.append(CompiledScript(s, None, error=f"正则太长（>{MAX_PATTERN_CHARS} 字符）"))
            continue
        if s.substitute_regex:
            out.append(
                CompiledScript(
                    s,
                    None,
                    error="substituteRegex≠0（需要先对正则做宏替换，本系统不支持）",
                )
            )
            continue
        try:
            compiled = _regex.compile(_to_python_pattern(pattern_text), _build_flags(flags))
        except Exception as exc:  # regex.error 等的共同基类是 Exception
            out.append(CompiledScript(s, None, error=f"编译失败：{exc}"))
            continue
        out.append(CompiledScript(s, compiled, global_replace="g" in flags))
    return out


# ==================================================================
#  执行
# ==================================================================
def _filter_trim(value: str, trim_strings: list[str]) -> str:
    """按 trimStrings 剔除命中内容里的子串（engine.js:457-465）。"""
    result = value
    for trim in trim_strings:
        if trim:
            result = result.replace(trim, "")
    return result


def _expand_replacement(template: str, match: Any, trim_strings: list[str]) -> str:
    """把替换串里的 `$N` / `$<name>` 展开成对应捕获组的内容。

    ★ 语义照酒馆 engine.js:419-445：
      · 先 `{{match}}`（不分大小写）→ `$0`
      · 组不存在或内容为空 → 替换成**空串**（不是原样留着 `$1`）
      · 取到的组内容要过一遍 trimStrings
    """
    prepared = re.sub(r"\{\{match\}\}", "$0", template, flags=re.IGNORECASE)

    def _sub(match_group: re.Match[str]) -> str:
        num, name = match_group.group(1), match_group.group(2)
        value: str | None = None
        try:
            if num:
                value = match.group(int(num))
            elif name:
                value = match.group(name)
        except (IndexError, KeyError, Exception):  # noqa: B014 - 组名/组号不存在
            value = None
        if not value:
            return ""
        return _filter_trim(value, trim_strings)

    return _REPLACEMENT_GROUP_RE.sub(_sub, prepared)


def apply_one(
    text: str,
    compiled: CompiledScript,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, int, str]:
    """让**一条**脚本作用到文本上，返回 `(新文本, 替换次数, 错误说明)`。

    超时/异常一律当"这条本次不生效"：原样返回输入 + 给出原因。
    ★ 为什么不让异常冒出去：正则来自外部预设，一条跑不了不该让整轮对话失败。
    """
    script = compiled.script
    if not compiled.usable or not text:
        return text, 0, ""

    hits = 0

    def _repl(match: Any) -> str:
        nonlocal hits
        hits += 1
        return _expand_replacement(script.replace_string, match, script.trim_strings)

    try:
        if _regex is None:  # pragma: no cover - 只在没装 regex 时走到
            return text, 0, "缺少 regex 模块，无法安全执行（已跳过）"
        new_text = compiled.pattern.sub(
            _repl,
            text,
            count=0 if compiled.global_replace else 1,
            timeout=timeout,
        )
        return new_text, hits, ""
    except TimeoutError:
        return text, 0, f"执行超时（>{timeout}s，可能是灾难性回溯），本次已跳过"
    except Exception as exc:  # pragma: no cover - 兜底
        return text, 0, f"执行出错：{exc}"


def _flag_matches(script: RegexScript, *, is_markdown: bool, is_prompt: bool) -> bool:
    """三个 flag 的判据（照抄 engine.js:348-355）。

    ★ "两个都不开"不是"不生效"：它跑在**清理正文**那一遍
      （`!isMarkdown && !isPrompt`）。我一开始把这一类读成了"不生效（？）"，
      是读错了 —— 它恰恰是影响最深的一类。
    """
    return bool(
        (script.markdown_only and is_markdown)
        or (script.prompt_only and is_prompt)
        or (
            not script.markdown_only
            and not script.prompt_only
            and not is_markdown
            and not is_prompt
        )
    )


def _depth_allows(script: RegexScript, depth: int | None) -> bool:
    """深度条件（照抄 engine.js:362-372）。

    ★ `depth is None` 时酒馆**整段跳过**深浅判断（`typeof depth === 'number'` 才算），
      所以这里也照做：不传深度 = 不做深度过滤，而不是"都不通过"。
    """
    if depth is None:
        return True
    if script.min_depth is not None and script.min_depth >= -1 and depth < script.min_depth:
        return False
    if script.max_depth is not None and script.max_depth >= 0 and depth > script.max_depth:
        return False
    return True


@dataclass
class ApplyStats:
    """一次应用做了什么（用于日志与界面如实说明）。"""

    scripts_run: int = 0
    scripts_skipped_placement: int = 0
    scripts_skipped_flag: int = 0
    scripts_skipped_depth: int = 0
    replacements: int = 0
    errors: list[str] = field(default_factory=list)
    names_run: list[str] = field(default_factory=list)

    def merge(self, other: "ApplyStats") -> None:
        self.scripts_run += other.scripts_run
        self.scripts_skipped_placement += other.scripts_skipped_placement
        self.scripts_skipped_flag += other.scripts_skipped_flag
        self.scripts_skipped_depth += other.scripts_skipped_depth
        self.replacements += other.replacements
        self.errors.extend(other.errors)
        self.names_run.extend(other.names_run)


def apply_scripts(
    text: str,
    compiled: list[CompiledScript],
    *,
    placement: int,
    is_markdown: bool = False,
    is_prompt: bool = False,
    depth: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, ApplyStats]:
    """把符合条件的脚本**按清单顺序**依次作用到文本上。

    顺序很重要（酒馆就是按数组顺序逐条 `replace`，前一条的输出是后一条的输入）：
    例如"把 `</think>` 删掉"必须排在"把思考段折成 `<details>`"之后，否则后者匹配不到。
    """
    stats = ApplyStats()
    result = text
    for item in compiled:
        script = item.script
        if not _flag_matches(script, is_markdown=is_markdown, is_prompt=is_prompt):
            stats.scripts_skipped_flag += 1
            continue
        if placement not in script.placement:
            stats.scripts_skipped_placement += 1
            continue
        if not _depth_allows(script, depth):
            stats.scripts_skipped_depth += 1
            continue
        if not item.usable:
            # 编译失败的那些：必须留下痕迹，绝不能安静地"看起来没效果"
            if item.error and item.error not in stats.errors:
                stats.errors.append(f"{script.name or script.id}：{item.error}")
            continue
        if not result:
            break
        new_text, hits, error = apply_one(result, item, timeout=timeout)
        if error:
            label = f"{script.name or script.id}：{error}"
            if label not in stats.errors:
                stats.errors.append(label)
            continue
        result = new_text
        stats.scripts_run += 1
        stats.replacements += hits
        stats.names_run.append(script.name or script.id)
    return result, stats


# ==================================================================
#  报告（"哪些跑不了"必须说得出来）
# ==================================================================
def describe_unsupported(scripts: list[RegexScript]) -> list[str]:
    """列出这份预设里**我们支持不了**的部分（导入时如实告知用）。

    返回空列表 = 全部支持。有内容就逐条说明是哪条、为什么。
    """
    notes: list[str] = []
    for s in scripts:
        if s.disabled:
            continue
        reasons: list[str] = []
        if s.substitute_regex:
            reasons.append("substituteRegex≠0")
        label = s.name or s.id or "(无名)"
        if reasons:
            notes.append(f"{label}（{'、'.join(reasons)}）")
    return notes


def supported_placements() -> set[int]:
    """本系统真正接了钩子的 placement。

    · 用户输入 / 模型输出：两条路径都接了（装配提示词 + 界面显示）
    · 思考过程：显示层接了（`messages.reasoning`）
    · 世界书 / 斜杠命令 / Markdown 显示：**没接**，遇到要如实报告
    """
    return {
        PLACEMENT_USER_INPUT,
        PLACEMENT_AI_OUTPUT,
        PLACEMENT_REASONING,
    }


def describe_unwired_placements(scripts: list[RegexScript]) -> list[str]:
    """哪些脚本作用的 placement 我们**没有钩子**（会完全不生效）。

    ★ 这一条是"如实说明"的关键：`WORLD_INFO` 的脚本我们没接，
      如果不说，用户会以为"正则适配了"而其实那一类一直没跑。
    """
    wired = supported_placements()
    out: list[str] = []
    for s in scripts:
        if s.disabled:
            continue
        missing = [p for p in s.placement if p not in wired]
        if missing:
            labels = "、".join(PLACEMENT_LABELS.get(p, str(p)) for p in missing)
            out.append(f"{s.name or s.id or '(无名)'}（作用于：{labels}）")
    return out



# ==================================================================
#  接进装配：把脚本作用到"即将发给模型"的那些消息上
# ==================================================================
def _placement_for_role(role: str) -> int:
    """消息角色 → 酒馆的 placement（script.js:1833-1848）。"""
    return PLACEMENT_USER_INPUT if role == "user" else PLACEMENT_AI_OUTPUT


def apply_to_plan(
    plan: Any,
    extensions: Any,
    *,
    macro_ctx: Any = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    on_error: Callable[[str], None] | None = None,
) -> ApplyStats:
    """把酒馆正则脚本作用到 `plan.messages` 上（**只改这一份发给模型的内容**）。

    ==================== 为什么挂在这里 ====================
    与插件（`plugin_runtime.apply_to_plan`）同一个位置：**装配完成之后、发出去之前**。
    好处是"对话"与「查看提示词」预览走的是同一条路，因此
    **预览里看到的就是实际发出去的**（本项目对这两者不一致零容忍）。
    ★ 代价/取舍：**不改数据库**。会话历史里永远保留模型原话
      （与酒馆不同：酒馆在落库前就改掉了正文）。见模块顶部「取舍 1」。

    ==================== 两遍执行，顺序照酒馆 ====================
    酒馆里这些脚本其实分两个时机跑：
      · 「两个 flag 都不开」的 → 生成后**立刻**跑（cleanUpMessage），改的是存下来的正文；
      · `promptOnly` / 两个都开 → 组装提示词时跑。
    两者有**时间先后**（前者在消息写入时、后者在下一轮装配时）。
    我们把它折进同一次装配：**先跑"都不开"的那批，再跑"提示词"那批**，
    这样相对顺序与酒馆一致（若反过来，后者产出的标签会被前者再清洗一遍）。

    两遍彼此不重叠，这一点是算出来的（`_flag_matches` 的判据保证）：
      · 第一遍 is_markdown=False / is_prompt=False → 只有"都不开"的命中；
        "两个都开"的因为两个条件都为假、第三个分支也要求两者皆为假 → 不命中。
      · 第二遍 is_prompt=True → 只有 `promptOnly` 与"两个都开"的命中；
        "都不开"的要求 is_prompt 为假 → 不命中。

    深度（`minDepth`/`maxDepth`）按酒馆的算法：**从最后一条倒着数**，
    最后一条 depth=0（script.js:4504 的 `coreChat.length - index - 1`）。
    """
    stats = ApplyStats()
    scripts = load_scripts(extensions)
    if not scripts:
        return stats

    compiled = compile_scripts(scripts)
    if not compiled:
        return stats

    messages = list(getattr(plan, "messages", []) or [])
    # 只对**非 system** 消息做（酒馆的 coreChat 就不含 system）：
    # 系统提示词里的世界书走 WORLD_INFO 那条钩子，本系统没接（已在导入时如实报告）。
    positions = [
        i for i, m in enumerate(messages) if str(getattr(m, "role", "")) != "system"
    ]
    total = len(positions)

    for order, index in enumerate(positions):
        message = messages[index]
        role = str(getattr(message, "role", "user") or "user").lower()
        content = str(getattr(message, "content", "") or "")
        if not content:
            continue
        placement = _placement_for_role(role)
        depth = total - order - 1  # 最后一条 = 0

        # 第一遍：「两个 flag 都不开」的（酒馆在生成后就跑的那批）
        content, s1 = apply_scripts(
            content,
            compiled,
            placement=placement,
            is_markdown=False,
            is_prompt=False,
            depth=depth,
            timeout=timeout,
        )
        # 第二遍：promptOnly / 两个都开（酒馆在装配提示词时跑的那批）
        content, s2 = apply_scripts(
            content,
            compiled,
            placement=placement,
            is_markdown=False,
            is_prompt=True,
            depth=depth,
            timeout=timeout,
        )
        stats.merge(s1)
        stats.merge(s2)

        new_text = content
        if new_text != str(getattr(message, "content", "") or ""):
            messages[index] = ChatMessage(role=message.role, content=new_text)

    # 写回：系统提示词与 messages 必须同步（同 plugin_runtime 的理由）
    plan.messages = messages

    if stats.errors and on_error is not None:
        for note in stats.errors:
            on_error(f"酒馆正则：{note}")
    if stats.errors:
        logger.warning(
            "酒馆正则执行有问题 | 条数={} 首个={}", len(stats.errors), stats.errors[0]
        )
    return stats


def apply_to_text(
    text: str,
    extensions: Any,
    *,
    placement: int,
    is_markdown: bool = False,
    is_prompt: bool = False,
    depth: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, ApplyStats]:
    """给**单段文本**应用（显示层用：一条消息渲染成 HTML 卡片）。"""
    scripts = load_scripts(extensions)
    if not scripts:
        return text, ApplyStats()
    return apply_scripts(
        text,
        compile_scripts(scripts),
        placement=placement,
        is_markdown=is_markdown,
        is_prompt=is_prompt,
        depth=depth,
        timeout=timeout,
    )
