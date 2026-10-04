"""自动翻译中间件：跨语言对话（原文 / 译文可切换，成本如实回报）。

==================== 它解决什么问题 ====================
大量角色卡是英文 / 日文写的（酒馆社区尤其如此）。中文用户直接聊会有两个后果：
一是模型被"卡的语言"带着走，回你一串英文；二是用户自己写的句子模型可能理解偏。
本项目不做"翻译 API 集成"，而是复用**统一 LLM 调用层**里的**任意一个模型配置**来做这一步
—— 于是"用哪个模型翻译"本身也成了可替换、可对比的实验变量。

==================== 三种模式（成本必须由用户选）====================
| 模式 | 额外调用 | 说明 |
|---|---|---|
| `off` | 0 | 关闭（默认） |
| `prompt` | **0** | 只在系统提示词末尾加一句"请一律用 X 回复"。不保证（模型可能不听话），但不花钱 |
| `middleware` | 每轮 1 次 | 生成之后真的调一次模型把回复译成 X；原文与译文都留着，界面可切换 |

★ 为什么不默认 `middleware`：它每轮都多花一次调用（长回复时输入 token 不便宜）。
  本项目对"未经同意花用户 token"零容忍（与记忆总结同一条规矩），所以**默认关闭**。

==================== 两种方向：都在同一个字段里 ====================
**不变量（记住这两条，界面与提示词就都不会错）**：

    `messages.content`              = **模型看到的文本**
    `messages.translation_json.text` = **给人看的文本**

| 方向 | `content`（模型看到） | `translation.text`（人看到） |
|---|---|---|
| 输出侧 `reply` | 模型说的原文（外语） | 译文 |
| 输入侧 `input` | **译文** | 用户原话 |

`translation.display` 说明界面该显示哪一份（当前两种方向都是 `translation`，
因为"给人看的那一份"永远存在 `translation.text` 里；字段保留是为了
"以后若把给人看的文本存回 content"时前端不用改）。

★ 为什么输入侧把译文写进 `content`：`content` 是"**模型看到的文本**"这条不变量。
  提示词装配直接读它，不需要再给 prompt_builder 加一层"替换历史"的机制 ——
  少一次改动就少一处不一致。

==================== 语言：一个目标 + 自动识别源语言 ====================
用户**只选「翻译成什么语言」**（默认 `简体中文`），源语言一律本地自动识别。
本项目面向中文用户，主场景是"外语角色卡 → 中文"，所以**开箱默认就是外语译成中文**。
两个方向共用这**同一个**目标语言：输出侧 = 把模型说的话译成目标语言给我看；
输入侧 = 把我写的话译成目标语言再发给模型。

★ 旧版本有第二个设置 `input_lang`（默认"英文"），两个语言框并排时极容易被读成
  "英文 → 中文"，而它的真实语义恰好相反（是"把我的输入译成英文"）—— 真的被用户读错过。
  现在 `input_lang` **已退休**：老会话 JSON 里残留的这个键在 `normalize_settings`
  里被忽略，用户下次保存设置即消失（不需要数据库迁移）。

==================== 两条省钱的判断 ====================
1. **已经是目标语言就不译**：本地判字符构成，命中就 `used_model=False`、0 token
   （真实省下来的钱，而不是"假装译了"）。判据见 `looks_like()`：
   - 目标中文：**出现假名（日文）或谚文（韩文）就不是中文**；再分简繁判专用字；
   - 目标英文：ASCII 字母占比 ≥ 70%；
   - 目标日文：出现假名才算日文；目标韩文：出现谚文才算韩文；
   - 其它语言（俄 / 法 / 德 / 西）：本地判不了，一律老实译。
2. **太长就不译**：超过 `MAX_CHARS`（默认 6000）时跳过并如实说明
   （一轮几千字的翻译很贵，而且多半是整段剧情复述）。

失败绝不影响对话：翻译失败就保留原文 + 记 `error` + 往 notes 里写一条，
回复本身一个字都不会丢（与剧情总结降级同一套哲学）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

# ==================================================================
#  模式与方向
# ==================================================================
MODE_OFF = "off"
MODE_PROMPT = "prompt"
MODE_MIDDLEWARE = "middleware"
MODES = (MODE_OFF, MODE_PROMPT, MODE_MIDDLEWARE)
MODE_LABELS = {
    MODE_OFF: "关闭",
    MODE_PROMPT: "只写提示词（0 token）",
    MODE_MIDDLEWARE: "中间件翻译（多一次调用）",
}
MODE_HINTS = {
    MODE_OFF: "不做任何翻译处理",
    MODE_PROMPT: "在系统提示词末尾加一句「请用 X 回复」：不花额外的钱，但模型可能不听话",
    MODE_MIDDLEWARE: "生成之后真的调一次模型翻译：原文与译文都留着，可切换（每轮多一次调用）",
}

DIRECTION_REPLY = "reply"
DIRECTION_INPUT = "input"
DIRECTION_BOTH = "both"
DIRECTIONS = (DIRECTION_REPLY, DIRECTION_INPUT, DIRECTION_BOTH)
DIRECTION_LABELS = {
    DIRECTION_REPLY: "只译回复（模型说外语 → 译成目标语言给我看）",
    DIRECTION_INPUT: "只译我的输入（我写的话 → 先译成目标语言再发给模型）",
    DIRECTION_BOTH: "双向都译",
}

DEFAULT_TARGET_LANG = "简体中文"
# ★ 没有"输入侧语言"这个设置了：一个目标语言，源语言自动识别。
#   老会话里残留的 `input_lang` 键会被 normalize_settings 忽略（见模块文档）。

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "mode": MODE_MIDDLEWARE,
    "direction": DIRECTION_REPLY,
    # 默认「外语 → 简体中文」：本项目面向中文用户，绝大多数人只需要这一档
    "target_lang": DEFAULT_TARGET_LANG,
    "provider_id": None,
    "keep_original": True,
}

MAX_LANG_CHARS = 16
#: 超过这个长度就不译（一轮几千字的翻译很贵，而且多半是整段剧情复述）
MAX_CHARS = 6000
#: 目标语言的候选（界面下拉框 + 校验白名单）
LANG_CHOICES = (
    "简体中文",
    "繁體中文",
    "英文",
    "日文",
    "韩文",
    "俄文",
    "法文",
    "德文",
    "西班牙文",
)

#: 翻译中间件的系统提示词（本项目自拟：只输出译文，不要解释、不要加引号）
TRANSLATE_SYSTEM_PROMPT = (
    "你是一个翻译中间件，服务于一场跨语言的角色扮演对话。\n"
    "把用户给你的文本翻译成【{lang}】。\n"
    "规则：\n"
    "1. 只输出译文本身。不要解释、不要加引号、不要写「译文：」这类前缀、不要保留原文。\n"
    "2. 保持原文的换行、段落、Markdown 记号与 <state> 之类的标签原样。\n"
    "3. 人名、地名、专有名词照译或音译，全程保持一致。\n"
    "4. 如果原文已经是【{lang}】，原样返回，一个字都不要改。\n"
    "5. 不要添加原文没有的内容，也不要删减。"
)

#: 汉字区（★ 刻意**不含**假名与谚文：把它们算进"中文"会让日文/韩文被误判成"已经是中文"）
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
#: 平假名 / 片假名（含假名扩展与半角片假名）—— 出现即"这是日文"
_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")
#: 谚文（字母区 + 音节区）—— 出现即"这是韩文"
_HANGUL_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7af]")
_LATIN_RE = re.compile(r"[A-Za-z]")

#: 简繁判据：只收**高频、且只在一边出现**的字。
#: ★ 判错的代价是不对称的：把简体判成繁体 → 多花一次调用（只是钱）；
#:   把繁体判成简体 → 该转的没转（用户看到繁体）。所以宁缺毋滥，只收有把握的字。
#:   而且这类本地判据永远只是"省钱的短路"：判不出来就老实译（见 looks_like 末尾）。
_TRAD_ONLY = set(
    "們這說國會學對時間實沒麼樣種覺讓開關頭長點兒幾聽買賣話語讀寫車東馬鳥魚龍風雲電腦"
    "認識記憶應該願從個過還進遠邊連選適錯錢鐘銀鐵錄鍵隊際隨險難雖離顯飛飯館驗體髮"
    "為與於並內兩產業樂愛門問聞書畫習課師醫藥貴費資質賽較轉輕辦農達運遲遺鄉鄰釋陽"
    "陰陸階隱雙雜雞靜頁頂順預領題顏類齊黃麗鬥擊線組織圖裡裏見單萬無數觀歡歲親義藝議"
)
#: 简体专用字（上面那一串的对应简体形）
_SIMP_ONLY = set(
    "们这说国会学对时间实没么样种觉让开关头长点儿几听买卖话语读写车东马鸟鱼龙风电脑"
    "认记忆应该愿从个过还进远边连选适错钱钟银铁录键队际随险难虽离显飞饭馆验体发"
    "为与于并内两产业乐爱门问闻书画习课师医药贵费资质赛较转轻办农达运迟遗乡邻释阳"
    "阴陆阶隐双杂鸡静页顶顺预领题颜类齐黄丽击线组织图见单万无数观欢岁亲义艺议"
)


# ==================================================================
#  设置：默认值 / 校验 / 读写
# ==================================================================
def default_settings() -> dict[str, Any]:
    return dict(DEFAULT_SETTINGS)


def normalize_settings(raw: Any) -> dict[str, Any]:
    """把界面传来的设置校验成可信形态（缺项补默认、越界夹回、非法值拒绝）。"""
    data = raw if isinstance(raw, dict) else {}
    settings = default_settings()

    settings["enabled"] = bool(data.get("enabled", settings["enabled"]))
    mode = str(data.get("mode") or settings["mode"])
    settings["mode"] = mode if mode in MODES else MODE_MIDDLEWARE
    direction = str(data.get("direction") or settings["direction"])
    settings["direction"] = direction if direction in DIRECTIONS else DIRECTION_REPLY
    settings["target_lang"] = _lang(data.get("target_lang"), DEFAULT_TARGET_LANG)
    settings["keep_original"] = bool(data.get("keep_original", True))
    provider_id = data.get("provider_id")
    try:
        settings["provider_id"] = int(provider_id) if provider_id not in (None, "", 0) else None
    except (TypeError, ValueError):
        settings["provider_id"] = None
    # 关掉总开关时把模式也归零：避免界面显示"中间件翻译"却什么都没发生
    if not settings["enabled"]:
        settings["mode"] = MODE_OFF
    return settings


def _lang(value: Any, fallback: str) -> str:
    text = str(value or "").strip()[:MAX_LANG_CHARS]
    return text or fallback


def load_settings(session: Any) -> dict[str, Any]:
    raw = getattr(session, "translate_settings_json", None)
    if not raw:
        return default_settings()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("翻译设置 JSON 解析失败，已用默认值 | session_id={}", getattr(session, "id", None))
        return default_settings()
    return normalize_settings(data)


def save_settings(session: Any, settings: dict[str, Any]) -> None:
    session.translate_settings_json = json.dumps(settings, ensure_ascii=False)


def wants(settings: dict[str, Any], direction: str) -> bool:
    """这个方向要不要处理（总开关 + 方向 + 模式三者都算）。"""
    if not settings.get("enabled"):
        return False
    if settings.get("mode") == MODE_OFF:
        return False
    picked = str(settings.get("direction") or DIRECTION_REPLY)
    return picked in (direction, DIRECTION_BOTH)


def uses_model(settings: dict[str, Any], direction: str) -> bool:
    return wants(settings, direction) and settings.get("mode") == MODE_MIDDLEWARE


def will_call_model(settings: dict[str, Any], direction: str, text: str) -> bool:
    """这一次翻译**会不会真的调模型**（纯本地判断，与 `run()` 的跳过条件保持一致）。

    ★ 界面拿它决定"要不要显示『正在翻译…』"：**提示了却没译**比不提示更让人困惑
      （用户会盯着一个永远不消失的提示）。所以条件必须与 `run()` 逐条对齐：
      总开关 + 方向 + 模式 + 非空 + 不超长 + 不是已经目标语言。
    ★ 唯一判不了的是"有没有可用的模型配置"（那要建适配器）：那种情况 `run()` 会记
      `error`，前端在 `done` 时收回提示，不会一直挂着。
    """
    if settings.get("mode") != MODE_MIDDLEWARE or not wants(settings, direction):
        return False
    body = str(text or "")
    if not body.strip() or len(body) > MAX_CHARS:
        return False
    lang = str(settings.get("target_lang") or DEFAULT_TARGET_LANG)
    return not looks_like(body, lang)


def prompt_hint(settings: dict[str, Any]) -> str:
    """`prompt` 模式写进系统提示词的那一句（`middleware` 也会带上 —— 双保险）。"""
    if not settings.get("enabled") or settings.get("mode") == MODE_OFF:
        return ""
    lines: list[str] = []
    if wants(settings, DIRECTION_REPLY):
        lines.append(
            f"【输出语言】正文一律用{settings.get('target_lang')}书写"
            "（人名与专有名词可以保留原文）；不要因为角色卡或历史里出现别的语言就换语言。"
        )
    if wants(settings, DIRECTION_INPUT):
        lines.append(
            f"【输入语言】用户可能用其它语言输入，"
            f"按{settings.get('target_lang')}理解他的意思即可，回复仍按上面的输出语言要求。"
        )
    return "\n".join(lines)


# ==================================================================
#  语言判定（省钱的本地判断，不调模型）
# ==================================================================
def looks_like(text: str, lang: str) -> bool:
    """粗略判断这段文本是不是**已经是目标语言**（用于"不用译"的短路）。

    ★ 判据全部在本地做，且**保守优先**："宁可多译一次，也不漏译"：
      - 目标**中文**：出现**假名**（日文）或**谚文**（韩文）⇒ 那不是中文 ⇒ 老实译；
        再分简繁 —— 目标简体时含**繁体专用字**要译，目标繁体时含**简体专用字**要译。
        （★ 旧版把假名和谚文一起算进"CJK 占比"，于是日文、韩文回复会被判成
         "已经是简体中文"而**静默不译** —— 那正是"日文自动识别"失效的真正原因。）
      - 目标**日文**：出现假名才算日文；目标**韩文**：出现谚文才算韩文。
      - 目标**英文**：ASCII 字母占比 ≥ 70%（判据与旧版一致）。
      - 其它语言（俄 / 法 / 德 / 西）：本地判不了，一律老实译。
    """
    sample = re.sub(r"\s+", "", str(text or ""))[:400]
    if not sample:
        return True  # 空的没什么可译
    target = str(lang or "")
    has_kana = bool(_KANA_RE.search(sample))
    has_hangul = bool(_HANGUL_RE.search(sample))
    han = len(_CJK_RE.findall(sample)) / len(sample)

    if "中" in target:
        if has_kana or has_hangul:
            return False  # 日文 / 韩文：不是中文，老老实实译
        if han < 0.4:
            return False  # 汉字太少：多半是英文或其它语言
        if "繁" in target:
            return not (_SIMP_ONLY & set(sample))  # 含简体专用字 ⇒ 还得转成繁体
        return not (_TRAD_ONLY & set(sample))  # 含繁体专用字 ⇒ 还得转成简体
    if "日" in target:
        return has_kana
    if "韩" in target:
        return has_hangul
    if "英" in target or "english" in target.lower():
        letters = len(_LATIN_RE.findall(sample))
        others = len(re.findall(r"[^\W\d_]", sample, re.UNICODE)) or 1
        return letters / others >= 0.7
    return False  # 其它语言一律老实译（本地判断不了就别省这次钱）


# ==================================================================
#  结果
# ==================================================================
@dataclass
class TranslationOutcome:
    """一次翻译的结果（`error` 非空 = 没译成，调用方保留原文）。"""

    text: str = ""  # 另一份文本（输入侧 = 用户原文；输出侧 = 译文）
    direction: str = DIRECTION_REPLY
    lang: str = ""
    display: str = "translation"  # 界面默认显示哪一份：content / translation
    used_model: bool = False
    skipped: str = ""  # 跳过原因（已经是目标语言 / 太长 / 未启用）
    tokens: int = 0
    model: str = ""
    provider_id: int | None = None
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "direction": self.direction,
            "lang": self.lang,
            "display": self.display,
            "used_model": self.used_model,
            "skipped": self.skipped,
            "tokens": self.tokens,
            "model": self.model,
            "provider_id": self.provider_id,
            "error": self.error,
        }


def to_dict(outcome: TranslationOutcome) -> dict[str, Any]:
    return outcome.to_dict()


def dumps(outcome: TranslationOutcome | None) -> str | None:
    if outcome is None or not (outcome.text or outcome.error):
        return None
    return json.dumps(outcome.to_dict(), ensure_ascii=False)


def loads(raw: Any) -> dict[str, Any] | None:
    """读 `messages.translation_json`（坏数据当"没有译过"，绝不影响渲染）。"""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("text") else None


def build_messages(text: str, settings: dict[str, Any], *, lang: str) -> list[Any]:
    """拼出"让模型做翻译"的两条消息（与剧情总结同一套：system + user）。"""
    from app.llm.schema import ChatMessage

    system = TRANSLATE_SYSTEM_PROMPT.format(lang=lang)
    return [ChatMessage.system(system), ChatMessage.user(str(text or ""))]


def _effort_for_translation(adapter: Any) -> Any:
    """翻译这次调用用什么思考强度 —— 统一走 `app.llm.params.cheap_reasoning_effort`。

    翻译是**机械任务**，思考纯烧钱（实测：1618 字符的英文回复译一次烧了 5291 token）；
    但"能不能发这个参数"必须看真机探测结论，理由见那个函数自己的说明（总结链路共用它）。
    """
    from app.llm.params import cheap_reasoning_effort

    return cheap_reasoning_effort(adapter)


def run(
    text: str,
    settings: dict[str, Any],
    *,
    direction: str,
    adapter: Any = None,
    provider_id: int | None = None,
) -> TranslationOutcome:
    """执行一次翻译。**任何失败都落在 `error` 里**，绝不抛给调用方。

    `adapter` 为 None 时（没配模型 / 纯函数测试）走"跳过"，把原因写进 `skipped`。
    """
    # ★ 两个方向共用同一个目标语言：没有"输入侧语言"了（源语言自动识别）
    lang = str(settings.get("target_lang") or DEFAULT_TARGET_LANG)
    # ★ `translation.text` 永远放"给人看的那一份"，所以两种方向的 display 都是 translation
    #   （输入侧调用方会把 text 换成用户原话，见 engine._translate_input）
    display = "translation"
    outcome = TranslationOutcome(direction=direction, lang=lang, display=display, provider_id=provider_id)
    body = str(text or "")

    if not wants(settings, direction):
        outcome.skipped = "未启用"
        return outcome
    if not body.strip():
        outcome.skipped = "空文本"
        return outcome
    if len(body) > MAX_CHARS:
        outcome.skipped = f"文本太长（{len(body)} 字 > {MAX_CHARS}）"
        return outcome
    if settings.get("mode") == MODE_PROMPT:
        # 提示词模式：不调模型（0 token），只在提示词里加要求
        outcome.skipped = "提示词模式不调用模型"
        return outcome
    if looks_like(body, lang):
        outcome.skipped = f"看起来已经是{lang}"
        return outcome
    if adapter is None:
        outcome.skipped = "没有可用的模型配置"
        return outcome

    try:
        from app.llm.schema import ChatRequest

        result = adapter.chat(
            ChatRequest(
                messages=build_messages(body, settings, lang=lang),
                # ★ 翻译是**机械任务**，思考纯烧钱（实测：1618 字符的英文回复译一次烧了 5291 token）
                reasoning_effort=_effort_for_translation(adapter),
            )
        )
        translated = str(getattr(result, "content", "") or "").strip()
        if not translated:
            raise ValueError("模型返回了空译文")
        usage = getattr(result, "usage", None)
        outcome.text = translated
        outcome.used_model = True
        outcome.tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
        outcome.model = str(getattr(result, "model", "") or "")
        # ★ 适配层对参数的调整必须如实留痕（统一层的规矩：拒绝静默降级）。
        #   翻译这条链路不往每轮提醒里塞（否则每轮都重复一句），记日志 + 存进 extra，
        #   界面在 🌐 面板里提示"翻译已尽量降低思考"。
        adapter_notes = list(getattr(result, "notes", None) or [])
        if adapter_notes:
            outcome.extra["notes"] = adapter_notes
            logger.info(
                "翻译请求参数被适配层调整 | direction={} notes={}", direction, adapter_notes
            )
    except Exception as exc:  # noqa: BLE001 - 翻译失败绝不能影响对话
        logger.warning(
            "翻译中间件调用失败，已保留原文 | direction={} lang={} err={}", direction, lang, exc
        )
        outcome.error = str(exc) or exc.__class__.__name__
    return outcome


def estimate_cost(settings: dict[str, Any], text: str = "") -> int:
    """预估这次翻译要花多少 token（界面如实显示）。0 = 不调模型。"""
    if settings.get("mode") != MODE_MIDDLEWARE or not settings.get("enabled"):
        return 0
    if text and looks_like(
        text, str(settings.get("target_lang") or DEFAULT_TARGET_LANG)
    ):
        return 0
    system_chars = len(TRANSLATE_SYSTEM_PROMPT.format(lang="简体中文"))
    body_chars = len(text or "x" * 400)
    # 输入 ≈ 系统提示 + 原文；输出 ≈ 原文长度（译文通常与原文同量级）
    return max(1, (system_chars + body_chars) // 2 + body_chars // 2)


def describe(settings: dict[str, Any]) -> str:
    """一句话摘要（界面上的说明，与"预览不能骗人"同一条规矩）。"""
    if not settings.get("enabled") or settings.get("mode") == MODE_OFF:
        return "未启用"
    mode = MODE_LABELS.get(str(settings.get("mode")), "")
    direction = DIRECTION_LABELS.get(str(settings.get("direction")), "")
    return (
        f"{mode} · {direction} · 翻译成 {settings.get('target_lang')}"
        "（源语言自动识别）"
    )


def state(session: Any) -> dict[str, Any]:
    """给「翻译」面板用的状态（与记忆面板同一形状：设置 + 可选项 + 摘要）。"""
    settings = load_settings(session)
    return {
        "settings": settings,
        "modes": [{"value": m, "label": MODE_LABELS[m], "hint": MODE_HINTS[m]} for m in MODES],
        "directions": [{"value": d, "label": DIRECTION_LABELS[d]} for d in DIRECTIONS],
        "lang_choices": list(LANG_CHOICES),
        "max_chars": MAX_CHARS,
        "summary": describe(settings),
    }
