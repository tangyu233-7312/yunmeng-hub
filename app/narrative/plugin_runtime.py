"""把插件作用到**已经装配好的提示词**上（正则替换 / 提示词注入 / 骰子规则）。

==================== 为什么要单独一个模块？====================
插件的各类提示词能力都必须在"提示词拼装完成之后、发出去之前"这一瞬间生效，
而且**只影响发给模型的内容**：
    · 不改数据库（会话历史里还是原话，用户重新打开界面看到的也是原话）
    · 不改界面显示
    · 「查看提示词」预览走的是同一个入口（engine.build_session_prompt），
      所以预览里能看到插件的效果 —— 本项目对"预览与实际不一致"零容忍。

骰子插件在这里贡献两样东西，分得很清楚：
    · **规则块**（"你没有随机数能力，要掷就写 <roll>"）—— 插件的指令，加在提示词末尾
    · **本轮点数**（`## 本轮骰点`）—— 不是指令而是**客观事实**，由 engine 在装配时
      放进正文（见 app/narrative/dice.py 的 render_turn_block）。
      点数在这里**绝不重掷**：它已经在用户消息落库那一刻写进 messages.rolls_json，
      否则点一次「查看提示词」就会掷出另一个数字。

CSS 插件不在这里（它由前端取 `/plugins/theme.css` 注入）。
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from app.llm.schema import ChatMessage
from app.narrative import dice as dice_mod
from app.narrative.presets import GUARD_IDENTITY_MARKER

#: flags 字符串 → re 标志
_FLAG_MAP = {"i": re.I, "m": re.M, "s": re.S, "x": re.X}


def find_plugin(plugins: list[Any] | None, kind: str) -> Any | None:
    """取启用插件里**同类插件的第一条**（按 priority 排序由 load_enabled 保证）。

    ★ 只用第一条而不是把多个同类插件的配置合并：两台骰子插件同时生效时，
      "哪个触发词说了算"没有自然答案。合并会得到"触发词并集 + 上限取谁"这类
      无人能预期的行为，不如明确"顺序在前的生效"，界面上也看得懂。
    """
    for row in plugins or []:
        if getattr(row, "kind", "") == kind:
            return row
    return None


def dice_spec(plugins: list[Any] | None) -> dict[str, Any] | None:
    """取启用的骰子插件配置（没有就返回 None）。"""
    row = find_plugin(plugins, "dice")
    if row is None:
        return None
    return dice_mod.parse_config(getattr(row, "config", None))


def _compile(rule: dict[str, Any]) -> re.Pattern[str] | None:
    flags = 0
    for ch in str(rule.get("flags") or ""):
        flags |= _FLAG_MAP.get(ch, 0)
    try:
        return re.compile(str(rule.get("pattern") or ""), flags)
    except re.error as exc:  # 校验层已经挡过一次，这里是兜底
        logger.warning("插件正则无法编译，已跳过 | pattern={} err={}", rule.get("pattern"), exc)
        return None


def _regex_replace(text: str, rules: list[dict[str, Any]]) -> tuple[str, int]:
    """按顺序做替换，返回 (新文本, 命中次数)。"""
    result = text
    hits = 0
    for rule in rules:
        pattern = _compile(rule)
        if pattern is None:
            continue
        result, count = pattern.subn(str(rule.get("replacement") or ""), result)
        hits += count
    return result, hits


def _inject(system_prompt: str, position: str, content: str, notes: list[str]) -> str:
    text = content.strip()
    if not text:
        return system_prompt
    if position == "start":
        return f"{text}\n\n{system_prompt}" if system_prompt.strip() else text
    if position == "before_guard":
        if GUARD_IDENTITY_MARKER not in system_prompt:
            notes.append(
                "有插件想插在「守卫之前」，但当前装配里没有内置守卫块，已改为插在提示词末尾"
            )
            return f"{system_prompt}\n\n{text}" if system_prompt.strip() else text
        before, _, after = system_prompt.partition(GUARD_IDENTITY_MARKER)
        if before.strip():
            return f"{before.rstrip()}\n\n{text}\n\n{GUARD_IDENTITY_MARKER}{after}"
        return f"{text}\n\n{GUARD_IDENTITY_MARKER}{after}"
    # position == "end"：越靠后的指令权重越高，文风类约束放这里最有效
    return f"{system_prompt}\n\n{text}" if system_prompt.strip() else text


def apply_to_plan(plan: Any, plugins: list[Any] | None) -> dict[str, Any]:
    """就地修改 `plan`（PromptPlan），返回一份"做了什么"的统计。

    统计只用于日志与排查，不塞进 warnings —— 提示词里插了什么，
    在「查看提示词」里一眼就能看到，没必要每轮都弹一次提醒。
    """
    stats: dict[str, Any] = {
        "prompt_blocks": 0,
        "regex_plugins": 0,
        "replacements": 0,
        "dice_rules": 0,
    }
    if not plugins:
        return stats

    notes: list[str] = []
    system_prompt = str(getattr(plan, "system_prompt", "") or "")

    # ---------------- 0. 骰子规则（"你没有随机数能力"）----------------
    # ★ 放在最前面处理，但最终会落在提示词**末尾**（越靠后权重越高）；
    #   规则块只讲"怎么请求掷骰"，具体点数由 engine 作为客观事实放进正文。
    spec = dice_spec(plugins)
    if spec is not None:
        contract = dice_mod.render_contract(spec)
        if contract.strip():
            system_prompt = _inject(system_prompt, "end", contract, notes)
            stats["dice_rules"] = 1

    # ---------------- 1. 提示词注入 ----------------
    for row in plugins:
        if getattr(row, "kind", "") != "prompt":
            continue
        config = row.config if isinstance(row.config, dict) else {}
        content = str(config.get("content") or "")
        if not content.strip():
            continue
        system_prompt = _inject(
            system_prompt, str(config.get("position") or "end"), content, notes
        )
        stats["prompt_blocks"] += 1

    # ---------------- 2. 正则替换 ----------------
    rules: list[dict[str, Any]] = []
    for row in plugins:
        if getattr(row, "kind", "") != "regex":
            continue
        config = row.config if isinstance(row.config, dict) else {}
        rows = config.get("rules") or []
        if isinstance(rows, list) and rows:
            rules.extend([r for r in rows if isinstance(r, dict)])
            stats["regex_plugins"] += 1
    if rules:
        system_prompt, hits = _regex_replace(system_prompt, rules)
        stats["replacements"] += hits

    # ---------------- 3. 写回计划 ----------------
    # 系统提示词在 messages 里是**第一条**（build_messages 保证），两处必须同步，
    # 否则预览（读 system_prompt）与实际请求（读 messages）会不一致。
    plan.system_prompt = system_prompt
    messages = list(getattr(plan, "messages", []) or [])
    if messages and str(getattr(messages[0], "role", "")) == "system":
        messages[0] = ChatMessage.system(system_prompt)
    if rules:
        for index in range(1, len(messages)):
            message = messages[index]
            content = str(getattr(message, "content", "") or "")
            if not content:
                continue
            new_content, hits = _regex_replace(content, rules)
            if hits:
                stats["replacements"] += hits
                messages[index] = ChatMessage(role=message.role, content=new_content)
    plan.messages = messages

    if notes:
        warnings = getattr(plan, "warnings", None)
        if isinstance(warnings, list):
            warnings.extend(notes)
    return stats
