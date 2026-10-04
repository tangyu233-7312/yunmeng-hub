"""受控骰子表达式求值（跑团骰点）—— 骰子插件的求值内核。

==================== 为什么必须由系统掷，而不是让模型"编" ====================
大模型**没有真随机**：同一个提示词下它倾向于给出"恰到好处"的数字（需要成功就写 18，
需要失败就写 3），而且同一个骰点前后两次回答能给出不同结果。跑团、检定、掉落表
这类玩法的乐趣恰恰建立在"结果不由叙述者决定"上。所以骰点必须由后端的伪随机数掷出，
模型只负责**解释**结果。

==================== 为什么自己写解析器（不用 eval / 不放开 JS）====================
`eval` 等于把提示词里的任意文本当代码跑；跑 JS 等于把用户的 API Key 交出去
（与 `app/db/models/plugin.py` 里"绝不执行第三方 JS"是同一条红线）。
所以这里是一台**只认骰子语法的递归下降求值器**：能算的东西固定、有界、可解释。

支持的记法（与 TRPG 习惯一致）::

    1d100            一个百分骰
    d20              省略个数 = 1
    2d6+3            两枚六面骰再加 3
    4d6kh3           四枚六面骰取最高的三枚（DnD 属性生成法）
    4d6kl1           取最低的一枚
    2d6!             爆炸骰：掷出最大面就再掷一枚（有深度上限）
    (1d6+2)*2        带括号的四则运算
    1d20+5<=15       带成功判定：小于等于 15 即成功
    1d100 # 力量检定   `#` 之后是这次掷骰的说明
    1d100 力量检定    没有 `#` 时，表达式之后的那段话也算说明

==================== 结果落库，而不是落文本 ====================
骰点一旦掷出就不能重掷（重掷等于抽卡），所以结果与骰面在**用户消息落库那一刻**
写进 `messages.rolls_json`：界面刷新、重新生成提示词预览读到的都是同一组数字。
这也是"预览与实际请求一致"这条规矩在骰子上的体现 —— 若改成"构建提示词时现掷"，
点一次「查看提示词」就会得到另一个点数。
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

# ==================================================================
#  上限（一切都要有界：表达式能被模型或用户写出来，就必须能被穷举完）
# ==================================================================
MAX_ROLLS_PER_TURN = 5  # 一条消息里最多认几个骰子指令 / 几个 <roll> 标签


@dataclass(frozen=True)
class Limits:
    """求值上限。默认值即"正常玩"够用、又不可能被拿来当计算炸弹的量级。"""

    max_len: int = 240  # 表达式长度
    max_dice: int = 100  # 一次表达式里所有骰子的**总个数**
    max_sides: int = 1000  # 单颗骰子的面数
    max_terms: int = 40  # token 数（防止 `1+1+1+…` 刷屏）
    max_depth: int = 8  # 括号嵌套深度
    max_explode: int = 10  # 爆炸骰的额外投掷次数
    max_abs: int = 100_000_000  # 中间结果的绝对值上限


DEFAULT_LIMITS = Limits()

#: 插件配置里的默认值（校验层与运行时共用，避免两处各写一份）
DEFAULT_CONFIG: dict[str, Any] = {
    "triggers": ["/r", "/roll", "掷骰", "投掷"],
    "default_expr": "1d100",
    "allow_model_roll": True,
    "show_detail": True,
    "explain": True,
    "max_dice": 100,
    "max_sides": 1000,
}

MAX_TRIGGERS = 8
MAX_TRIGGER_CHARS = 8
MAX_EXPR_CHARS = 60  # 默认表达式（配置项）的长度上限

#: 比较符 → 判定函数（`=` 与 `==` 同义）
_COMPARES: dict[str, Any] = {
    "<": lambda total, target: total < target,
    "<=": lambda total, target: total <= target,
    ">": lambda total, target: total > target,
    ">=": lambda total, target: total >= target,
    "=": lambda total, target: total == target,
    "==": lambda total, target: total == target,
    "!=": lambda total, target: total != target,
}

_ROLL_TAG_RE = re.compile(r"<\s*roll\s*>(.*?)<\s*/\s*roll\s*>", re.I | re.S)
_ROLL_OPEN_RE = re.compile(r"<\s*roll\s*>", re.I)
_OPERATOR_CHARS = set("+-*/%()<>!=")


class DiceError(Exception):
    """表达式不合法（一律带**给用户看**的中文原因，不抛英文栈）。"""


# ==================================================================
#  词法
# ==================================================================
@dataclass
class _Token:
    kind: str  # dice / num / op / cmp
    text: str
    count: int = 0  # dice
    sides: int = 0  # dice
    keep: str = ""  # dice: "" / "kh" / "kl"
    keep_n: int = 0  # dice
    explode: bool = False  # dice


_DICE_RE = re.compile(r"(\d*)[dD](\d+)((?:[kK][hHlL]\d*)?)(!?)")
_NUM_RE = re.compile(r"\d+")


def _tokenize(text: str, limits: Limits) -> list[_Token]:
    if len(text) > limits.max_len:
        raise DiceError(f"表达式太长（上限 {limits.max_len} 个字符）")
    tokens: list[_Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        matched = _DICE_RE.match(text, index)
        if matched:
            raw_count, raw_sides, raw_keep, raw_bang = matched.groups()
            count = int(raw_count) if raw_count else 1
            sides = int(raw_sides)
            if count <= 0:
                raise DiceError("骰子个数必须是正数")
            if count > limits.max_dice:
                raise DiceError(f"一次最多掷 {limits.max_dice} 颗骰子（写了 {count} 颗）")
            if sides < 2:
                raise DiceError("骰子至少要有 2 个面")
            if sides > limits.max_sides:
                raise DiceError(f"骰子最多 {limits.max_sides} 个面（写了 {sides} 面）")
            keep, keep_n = "", 0
            if raw_keep:
                keep = raw_keep[:2].lower()
                keep_n = int(raw_keep[2:]) if raw_keep[2:] else 1
                if keep_n <= 0:
                    raise DiceError("取高/取低的个数必须是正数")
                if keep_n > count:
                    raise DiceError(f"只掷了 {count} 颗骰子，不能取 {keep_n} 颗")
            tokens.append(
                _Token(
                    kind="dice",
                    text=matched.group(0),
                    count=count,
                    sides=sides,
                    keep=keep,
                    keep_n=keep_n,
                    explode=bool(raw_bang),
                )
            )
            index = matched.end()
            continue
        matched = _NUM_RE.match(text, index)
        if matched:
            tokens.append(_Token(kind="num", text=matched.group(0)))
            index = matched.end()
            continue
        pair = text[index : index + 2]
        if pair in _COMPARES:
            tokens.append(_Token(kind="cmp", text=pair))
            index += 2
            continue
        if char in _COMPARES:
            tokens.append(_Token(kind="cmp", text=char))
            index += 1
            continue
        if char in "+-*/%()":
            tokens.append(_Token(kind="op", text=char))
            index += 1
            continue
        raise DiceError(f"看不懂的字符：{char!r}")
    if len(tokens) > limits.max_terms:
        raise DiceError(f"表达式太复杂（token 上限 {limits.max_terms}）")
    if not tokens:
        raise DiceError("没有写表达式")
    return tokens


# ==================================================================
#  语法（递归下降，四则 + 一次比较）
# ==================================================================
class _Parser:
    def __init__(self, tokens: list[_Token], limits: Limits, rng: random.Random) -> None:
        self.tokens = tokens
        self.limits = limits
        self.rng = rng
        self.pos = 0
        self.dice_used = 0
        self.terms: list[dict[str, Any]] = []
        self.faces: list[int] = []

    # ---------- 工具 ----------
    def _peek(self) -> _Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> _Token:
        token = self._peek()
        if token is None:
            raise DiceError("表达式没写完")
        self.pos += 1
        return token

    def _guard(self, value: int) -> int:
        if abs(value) > self.limits.max_abs:
            raise DiceError("中间结果太大（已超过上限）")
        return value

    # ---------- 文法 ----------
    def parse(self) -> tuple[int, _Token | None]:
        total = self.expr(0)
        compare = self._peek()
        if compare is not None and compare.kind == "cmp":
            self._next()
            if self._peek() is None:
                raise DiceError("比较符后面没有写目标值")
            target = self.expr(0)
            if self._peek() is not None:
                raise DiceError("比较之后还有多余内容")
            self.terms.append({"kind": "target", "compare": compare.text, "value": target})
            return total, _Token(kind="cmp", text=f"{compare.text}{target}")
        if self._peek() is not None:
            raise DiceError(f"表达式后面有多余内容：{self._peek().text!r}")
        return total, None

    def expr(self, depth: int) -> int:
        value = self.mul(depth)
        while True:
            token = self._peek()
            if token is None or token.kind != "op" or token.text not in ("+", "-"):
                return value
            self._next()
            right = self.mul(depth)
            value = self._guard(value + right if token.text == "+" else value - right)

    def mul(self, depth: int) -> int:
        value = self.unary(depth)
        while True:
            token = self._peek()
            if token is None or token.kind != "op" or token.text not in ("*", "/", "//", "%"):
                return value
            self._next()
            right = self.unary(depth)
            if token.text in ("/", "//") and right == 0:
                raise DiceError("除数不能是 0")
            if token.text == "*":
                value = self._guard(value * right)
            elif token.text == "%":
                value = self._guard(value % right)
            else:
                # `/` 与 `//` 都按整除算：骰点只认整数，小数会让结果无法核对
                value = self._guard(value // right)
        return value

    def unary(self, depth: int) -> int:
        token = self._peek()
        if token is not None and token.kind == "op" and token.text in ("+", "-"):
            self._next()
            value = self.unary(depth)
            return self._guard(value if token.text == "+" else -value)
        return self.atom(depth)

    def atom(self, depth: int) -> int:
        token = self._next()
        if token.kind == "num":
            value = int(token.text)
            self.terms.append({"kind": "const", "value": value})
            return self._guard(value)
        if token.kind == "op" and token.text == "(":
            if depth + 1 > self.limits.max_depth:
                raise DiceError(f"括号嵌套太深（上限 {self.limits.max_depth} 层）")
            value = self.expr(depth + 1)
            closing = self._peek()
            if closing is None or closing.text != ")":
                raise DiceError("括号没有闭合")
            self._next()
            return value
        if token.kind == "dice":
            return self._roll(token)
        raise DiceError(f"{token.text!r} 出现在不该出现的位置")

    def _roll(self, token: _Token) -> int:
        """掷一组骰子，返回保留下来的点数之和。"""
        self.dice_used += token.count
        if self.dice_used > self.limits.max_dice:
            raise DiceError(f"一次最多掷 {self.limits.max_dice} 颗骰子")
        rolled: list[int] = []
        exploded = 0
        for _ in range(token.count):
            value = self.rng.randint(1, token.sides)
            rolled.append(value)
            # 爆炸骰：掷出最大面就追加一颗，深度有上限（否则 1d2! 可能摇很久）
            while token.explode and value == token.sides and exploded < self.limits.max_explode:
                exploded += 1
                value = self.rng.randint(1, token.sides)
                rolled.append(value)
        self.faces.extend(rolled)
        kept = list(rolled)
        dropped: list[int] = []
        if token.keep:
            ordered = sorted(rolled, reverse=(token.keep == "kh"))
            kept = ordered[: token.keep_n]
            dropped = ordered[token.keep_n :]
        self.terms.append(
            {
                "kind": "dice",
                "notation": token.text,
                "sides": token.sides,
                "keep": token.keep,
                "rolled": rolled,
                "kept": kept,
                "dropped": dropped,
                "subtotal": sum(kept),
                "exploded": exploded,
            }
        )
        return self._guard(sum(kept))


# ==================================================================
#  结果
# ==================================================================
@dataclass
class RollResult:
    """一次掷骰的结果（`error` 非空表示这次没掷成）。"""

    expression: str
    total: int | None = None
    terms: list[dict[str, Any]] = field(default_factory=list)
    faces: list[int] = field(default_factory=list)
    label: str = ""
    compare: str = ""
    target: int | None = None
    success: bool | None = None
    error: str = ""
    source: str = "user"  # user（用户指令）/ model（模型 <roll> 标签）

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self, *, show_detail: bool = True) -> dict[str, Any]:
        return {
            "source": self.source,
            "expression": self.expression,
            "label": self.label,
            "total": self.total,
            # 逐颗骰面与取舍过程一起存：论文里要能核对"这个 18 是怎么来的"，
            # 事后从 `1d20 = 18` 是反推不出骰面的
            "terms": self.terms,
            "faces": self.faces,
            "compare": self.compare,
            "target": self.target,
            "success": self.success,
            "error": self.error,
            "text": self.text(show_detail=show_detail),
        }

    def text(self, *, show_detail: bool = True, with_label: bool = True) -> str:
        """一行人类可读的结果（界面与提示词共用同一份，免得两处口径不一致）。"""
        if self.error:
            return f"{self.expression or '（空）'} → 掷骰失败：{self.error}"
        parts = [f"{self.expression} = {self.total}"]
        if self.compare:
            parts.append(f"{self.compare} → {'成功' if self.success else '失败'}")
        if show_detail:
            detail = _detail_text(self.terms)
            if detail:
                parts.append(f"（{detail}）")
        line = " ".join(parts)
        if with_label and self.label:
            line = f"{self.label}：{line}"
        return line


def _detail_text(terms: list[dict[str, Any]]) -> str:
    """把逐个骰面拼成 `骰子 5,6 取高 6` 这样的明细。"""
    chunks: list[str] = []
    for term in terms:
        if term.get("kind") != "dice":
            continue
        rolled = ",".join(str(v) for v in term.get("rolled") or [])
        text = f"骰子 {rolled}"
        if term.get("dropped"):
            verb = "取高" if term.get("keep") == "kh" else "取低"
            text += f" {verb} {','.join(str(v) for v in term['kept'])}"
        if term.get("exploded"):
            text += f" 爆炸 {term['exploded']} 次"
        chunks.append(text)
    return "；".join(chunks)


# ==================================================================
#  对外入口
# ==================================================================
def evaluate(
    expression: str,
    *,
    limits: Limits | None = None,
    rng: random.Random | None = None,
    source: str = "user",
) -> RollResult:
    """求值一条骰子表达式。**任何输入都不会抛异常**，失败落在 `error` 里。"""
    limits = limits or DEFAULT_LIMITS
    generator = rng or random.Random()
    raw = str(expression or "").strip()
    if not raw:
        return RollResult(expression=raw, error="没有写表达式", source=source)
    if len(raw) > limits.max_len:
        return RollResult(
            expression=raw[:limits.max_len], error=f"表达式太长（上限 {limits.max_len} 字符）"
        )
    try:
        parser = _Parser(_tokenize(raw, limits), limits, generator)
        total, compare = parser.parse()
    except DiceError as exc:
        return RollResult(expression=raw, error=str(exc), source=source)
    except RecursionError:  # 极端嵌套的兜底（正常路径到不了）
        return RollResult(expression=raw, error="表达式嵌套太深", source=source)
    result = RollResult(
        expression=raw, total=total, terms=parser.terms, faces=parser.faces, source=source
    )
    if compare is not None:
        compare_text = compare.text
        for symbol in sorted(_COMPARES, key=len, reverse=True):
            if compare_text.startswith(symbol):
                result.compare = symbol
                result.target = int(compare_text[len(symbol) :])
                result.success = bool(_COMPARES[symbol](total, result.target))
                break
    return result


def split_expression(text: str) -> tuple[str, str]:
    """把 `1d20+5 力量检定` 拆成 (表达式, 说明)。

    ★ 规则：`#` 之后一定是说明；没有 `#` 时，从**第一个"两边都不是运算符"的空格**
      处切开 —— 这样 `2d6 + 3 力量检定` 能正确切成 `2d6 + 3` 与 `力量检定`，
      而 `1d20 + 5` 不会被误切（`+` 两侧的空格要跳过）。
    """
    raw = str(text or "").strip()
    if "#" in raw:
        head, _, tail = raw.partition("#")
        return head.strip(), tail.strip()
    index = 0
    while index < len(raw):
        if raw[index].isspace():
            start = index
            while index < len(raw) and raw[index].isspace():
                index += 1
            left = raw[start - 1] if start > 0 else ""
            right = raw[index] if index < len(raw) else ""
            if left in _OPERATOR_CHARS or right in _OPERATOR_CHARS:
                continue  # 运算符两侧的空格：属于表达式
            return raw[:start].strip(), raw[index:].strip()
        index += 1
    return raw, ""


def parse_config(config: Any) -> dict[str, Any]:
    """把插件配置读成运行时用的规范形态（缺项一律补默认值）。

    ★ 校验层（`plugin_service.validate_config`）已经做过一次同样的事，
      这里再补一次是为了"老配置/手改的配置也不会让运行时崩"。
    """
    data = config if isinstance(config, dict) else {}
    merged = dict(DEFAULT_CONFIG)
    merged.update({key: value for key, value in data.items() if value is not None})
    triggers = [str(item) for item in merged.get("triggers") or [] if str(item).strip()]
    merged["triggers"] = triggers or list(DEFAULT_CONFIG["triggers"])
    limits = Limits(
        max_dice=_clamp_int(merged.get("max_dice"), 1, 1000, DEFAULT_LIMITS.max_dice),
        max_sides=_clamp_int(merged.get("max_sides"), 2, 100000, DEFAULT_LIMITS.max_sides),
    )
    merged["_limits"] = limits
    return merged


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def limits_of(spec: dict[str, Any]) -> Limits:
    limits = spec.get("_limits")
    return limits if isinstance(limits, Limits) else DEFAULT_LIMITS


# ==================================================================
#  用户指令（发消息里的 /r 1d100）
# ==================================================================
def scan_commands(
    text: str,
    spec: dict[str, Any],
    *,
    rng: random.Random | None = None,
    limit: int = MAX_ROLLS_PER_TURN,
) -> list[RollResult]:
    """扫描用户消息里的掷骰指令（**逐行**识别，触发词必须在行首）。

    返回的骰点已经"掷完"：调用方负责把它们写进 `messages.rolls_json`，
    之后提示词预览与真实请求读的都是这一份（不再重掷）。
    """
    triggers = [item for item in spec.get("triggers") or [] if item]
    if not triggers:
        return []
    # ★ 长的触发词优先：`/roll` 必须以 `/roll` 匹配，不能被更短的 `/r` 抢走
    #   （否则剩下的 `oll 1d20` 会被当成表达式，报"看不懂的字符 o"）
    triggers = sorted(set(triggers), key=len, reverse=True)
    limits = limits_of(spec)
    default_expr = str(spec.get("default_expr") or DEFAULT_CONFIG["default_expr"])
    results: list[RollResult] = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        hit = ""
        for trigger in triggers:
            if lowered.startswith(trigger.lower()):
                hit = trigger
                break
        if not hit:
            continue
        if len(results) >= limit:
            results.append(
                RollResult(
                    expression="",
                    error=f"一条消息最多掷 {limit} 次，后面的指令已忽略",
                )
            )
            break
        rest = stripped[len(hit) :].strip()
        if not rest:
            results.append(
                evaluate(default_expr, limits=limits, rng=rng, source="user")
            )
            continue
        expression, label = split_expression(rest)
        result = evaluate(expression or default_expr, limits=limits, rng=rng, source="user")
        result.label = label
        results.append(result)
    return results


# ==================================================================
#  模型自报的掷骰（回复里的 <roll>1d20</roll>）
# ==================================================================
def resolve_model_rolls(
    text: str,
    spec: dict[str, Any] | None,
    *,
    rng: random.Random | None = None,
    limit: int = MAX_ROLLS_PER_TURN,
) -> tuple[str, list[RollResult]]:
    """把回复里的 `<roll>` 标签**替换成真实的掷骰结果**。

    返回 `(正文, 骰点列表)`。三种情况：
      · 插件没启用 / 不许模型掷骰 → 只把标签剥掉（绝不让用户看到原始标签），不掷
      · 标签写完整 → 掷出结果，用 `（掷骰 1d20+5 = 18）` 形式的**明文**替换标签，
        于是下一轮模型在历史里看到的就是真实点数（与 `<state>` 剥块同一套思路）
      · 标签写坏（没有闭合）→ 替换成一句说明，并把后面的内容原样保留
    """
    raw = str(text or "")
    if not _ROLL_TAG_RE.search(raw) and not _ROLL_OPEN_RE.search(raw):
        return raw, []

    allow = bool(spec) and bool(spec.get("allow_model_roll", True))
    limits = limits_of(spec) if spec else DEFAULT_LIMITS
    show_detail = bool(spec.get("show_detail", True)) if spec else True
    results: list[RollResult] = []
    counter = {"n": 0}

    def _replace(matched: re.Match[str]) -> str:
        counter["n"] += 1
        expression, label = split_expression(matched.group(1))
        if not allow:
            return "（骰子插件未启用）"
        if counter["n"] > limit:
            return f"（本轮最多掷 {limit} 次，这次没有掷）"
        result = evaluate(expression, limits=limits, rng=rng, source="model")
        result.label = label
        results.append(result)
        return f"（掷骰 {result.text(show_detail=show_detail, with_label=False)}）"

    cleaned = _ROLL_TAG_RE.sub(_replace, raw)
    # 没闭合的标签：剥掉标签本身，后面的内容原样留着（不能像 <state> 那样整段截掉，
    # 因为掷骰常常写在句子中间，截掉会把模型写的正文一并吞掉）
    if _ROLL_OPEN_RE.search(cleaned):
        cleaned = _ROLL_OPEN_RE.sub("（掷骰写法不完整：没有写 </roll>）", cleaned)
        if allow:
            results.append(RollResult(expression="", error="标签没有闭合", source="model"))
    return cleaned, results


# ==================================================================
#  提示词侧：规则块（插件注入）+ 本轮结果块（数据）
# ==================================================================
def render_contract(spec: dict[str, Any]) -> str:
    """给模型的掷骰规则（由 plugin_runtime 追加在系统提示词末尾）。"""
    if not spec.get("explain", True):
        return ""
    triggers = " / ".join(f"`{item}`" for item in spec.get("triggers") or [])
    default_expr = str(spec.get("default_expr") or DEFAULT_CONFIG["default_expr"])
    lines = [
        "## 骰子（随机数一律由系统掷出）",
        "你**没有**随机数能力：任何需要运气的结果（检定、命中、掉落、概率）都不要自己编数字。",
    ]
    if spec.get("allow_model_roll", True):
        lines.append(
            "需要掷骰时，在回复里写 `<roll>表达式</roll>`（例：`<roll>1d20+5</roll>`），"
            "系统会把标签替换成真实点数；**不要**在标签外另写一个自己编的数字，"
            "也不要在同一轮里替玩家宣布成败 —— 点数出来之后下一轮再叙述结果。"
        )
    if triggers:
        lines.append(
            f"玩家也可以用指令自己掷骰：行首写 {triggers} 加表达式"
            f"（不加表达式时默认 {default_expr}），例如 `{triggers.split(' / ')[0].strip('`')} 2d6+3`。"
        )
    lines.append(
        "系统给出的点数是**既成事实**：即使它让剧情变得糟糕或过于顺利，也必须照它叙述，"
        "不许重掷、不许改数、不许换一个更合适的值。"
    )
    return "\n".join(lines)


TURN_BLOCK_TITLE = "## 本轮骰点（系统掷出，不可更改）"


def render_turn_block(rolls: list[dict[str, Any]], *, show_detail: bool = True) -> str:
    """把本轮的骰点渲染成提示词里的一段**客观事实**。"""
    if not rolls:
        return ""
    lines = [TURN_BLOCK_TITLE]
    for item in rolls:
        expression = str(item.get("expression") or "")
        error = str(item.get("error") or "")
        if error:
            if not expression:
                lines.append(f"- （{error}）")
                continue
            lines.append(
                f"- 玩家想掷 `{expression}`，但这个表达式不合法（{error}）："
                "如实告诉玩家，别替他编一个点数。"
            )
            continue
        lines.append(f"- {_roll_line(item, show_detail=show_detail)}")
    lines.append("剧情必须按上面的点数走。")
    return "\n".join(lines)


def _roll_line(item: dict[str, Any], *, show_detail: bool) -> str:
    expression = str(item.get("expression") or "")
    total = item.get("total")
    label = str(item.get("label") or "")
    compare = str(item.get("compare") or "")
    text = f"`{expression}` = **{total}**"
    if compare:
        text += f"（{compare}{item.get('target')} → {'成功' if item.get('success') else '失败'}）"
    if label:
        text = f"{label}：{text}"
    if show_detail:
        detail = _detail_text(item.get("terms") or [])
        if detail:
            text += f"（{detail}）"
    return text


# ==================================================================
#  落库 / 读取
# ==================================================================
def dumps(rolls: list[RollResult], *, show_detail: bool = True) -> str | None:
    if not rolls:
        return None
    return json.dumps(
        [item.to_dict(show_detail=show_detail) for item in rolls], ensure_ascii=False
    )


def loads(raw: Any) -> list[dict[str, Any]]:
    """读 `messages.rolls_json`（坏数据一律当"没有骰点"，绝不让它打断对话）。"""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def turn_rolls(history: list[Any]) -> list[dict[str, Any]]:
    """取**最后一条用户消息**上的骰点（本轮要注入提示词的那些）。

    ★ 只取最后一条：骰点是"这一轮的既成事实"，不是持续状态。
      历史轮次的骰点已经作为正文留在对话里，不需要每轮重复注入。
    """
    for row in reversed(list(history or [])):
        if str(getattr(row, "role", "")) != "user":
            continue
        return loads(getattr(row, "rolls_json", None))
    return []


def describe(rolls: list[dict[str, Any]]) -> str:
    """一句话摘要（界面上给用户看的那种，不写进提示词）。"""
    if not rolls:
        return ""
    if len(rolls) == 1:
        return str(rolls[0].get("text") or rolls[0].get("expression") or "")
    return f"掷了 {len(rolls)} 次：" + "；".join(
        str(item.get("text") or item.get("expression") or "") for item in rolls
    )
