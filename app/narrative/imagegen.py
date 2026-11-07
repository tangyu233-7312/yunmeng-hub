"""生图插件的**纯规则层**：配置默认值 / 标签协议 / 提示词拼装 / 接口响应解析。

==================== 为什么单独一个模块 ====================
这与 `app/narrative/dice.py` 的分层理由完全一样：
    · `plugin_runtime` 需要知道「生图协议怎么告诉模型」；
    · `plugin_service` 需要知道「默认配置长什么样」；
    · `imagegen_service` 需要知道「怎么从上游响应里挖出图片」；
    · 测试需要在不碰数据库、不发 HTTP 的前提下验证这些规则。

所以这里**不导入数据库、不读密钥、不发请求**。真正的 HTTP 调用与落盘在
`app/services/imagegen_service.py`。

==================== 两种触发方式 ====================
    auto   —— 每轮（或每 N 轮）由系统根据这一轮正文自动生成配图。
             用户说的「每一轮模型根据生成的语境生成图片」就是这一种。
    model  —— 模型在正文里写 `<hne-image>英文画面描述</hne-image>` 请求配图，
             系统把标签剥掉并替换成图片。适合不想每轮都花钱的人。

★ 标签**无论插件是否启用都会被剥掉**（与 `<roll>` 相同）：
  绝不能让用户看到 `<hne-image>...</hne-image>` 这种原始协议文本。
"""

from __future__ import annotations

import base64
import html
import re
from typing import Any

#: 支持的生图协议（与前端下拉框、plugin_service 的校验口径保持一致）
PROTOCOLS: tuple[str, ...] = ("openai-images", "openai-chat")
#: 触发方式
TRIGGERS: tuple[str, ...] = ("auto", "model")

#: 模型一轮最多能请求几张（也是配置项 max_images 的硬上限）
MAX_IMAGES = 4
#: 标签里单条描述的长度上限
MAX_PROMPT_CHARS = 4000

DEFAULT_STYLE = (
    "cinematic digital illustration, consistent character design, detailed lighting, "
    "rich colors, no text, no captions, no watermark, no UI elements"
)

DEFAULT_PROMPT_TEMPLATE = """Create a cinematic illustration for an interactive fiction scene.
Character: {char}
Player: {user}
Scene:
{scene}

Style: {style}
Important: no text, no captions, no watermark, no UI elements."""

#: 运行时配置的默认值（校验层会给出更严格的上下限；这里是"缺字段也能跑"的兜底）
DEFAULT_CONFIG: dict[str, Any] = {
    "base_url": "",
    "model": "",
    "protocol": "openai-images",
    "trigger": "auto",
    "interval": 1,
    "size": "1024x1024",
    "quality": "",
    "style": DEFAULT_STYLE,
    "prompt_template": "",
    "max_images": 1,
    "timeout_seconds": 120,
    "max_chars": 1200,
    "extra_body": {},
    # ★ 只有密文会进数据库；明文只在"用户刚填完/正在调用"时短暂存在。
    "api_key_enc": "",
}

#: `<hne-image>正文</hne-image>`
_TAG_RE = re.compile(r"<hne-image\b([^>]*)>(.*?)</hne-image\s*>", re.I | re.S)
#: `<hne-image prompt="…" />`
_SELF_TAG_RE = re.compile(r"<hne-image\b([^>]*?)/\s*>", re.I | re.S)
#: `prompt="…"` / `prompt='…'`
_PROMPT_ATTR_RE = re.compile(r"""\bprompt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I | re.S)
#: 残留的未闭合标签（先剥掉，避免原始协议文本漏给用户）
_LEFTOVER_RE = re.compile(r"</?hne-image\b[^>]*>", re.I)


def parse_config(raw: Any) -> dict[str, Any]:
    """把数据库里的配置补成一份可直接使用的字典（**不抛异常**）。

    ★ 这里故意宽容：配置是用户手写的，形状可能不完整。运行时缺什么就用默认值，
      真正的合法性由 `plugin_service.validate_config` 在保存时拦住。
      如果这里也抛异常，一条坏配置会让整轮对话失败 —— 那是不可接受的。
    """
    config = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return config
    for key in DEFAULT_CONFIG:
        if key in raw:
            config[key] = raw[key]

    # 布尔/数字字段做一次宽松归一化；非法值回退默认，不在这里报错。
    config["protocol"] = str(config.get("protocol") or "openai-images")
    if config["protocol"] not in PROTOCOLS:
        config["protocol"] = "openai-images"
    config["trigger"] = str(config.get("trigger") or "auto")
    if config["trigger"] not in TRIGGERS:
        config["trigger"] = "auto"
    config["interval"] = _coerce_int(config.get("interval"), 1, 50, 1)
    config["max_images"] = _coerce_int(config.get("max_images"), 1, MAX_IMAGES, 1)
    config["timeout_seconds"] = _coerce_int(config.get("timeout_seconds"), 10, 600, 120)
    config["max_chars"] = _coerce_int(config.get("max_chars"), 100, 4000, 1200)
    config["base_url"] = str(config.get("base_url") or "").strip().rstrip("/")
    config["model"] = str(config.get("model") or "").strip()
    config["size"] = str(config.get("size") or "").strip()
    config["quality"] = str(config.get("quality") or "").strip()
    config["style"] = str(config.get("style") or "").strip()
    config["prompt_template"] = str(config.get("prompt_template") or "").strip()
    if not isinstance(config.get("extra_body"), dict):
        config["extra_body"] = {}
    return config


def _coerce_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def find_spec(plugins: list[Any] | None) -> tuple[Any, dict[str, Any]] | None:
    """取**启用插件里第一条**生图插件（顺序由 load_enabled 保证）。

    与骰子插件相同：多个同类插件不合并配置 —— "哪一条说了算"必须有明确答案。
    """
    for row in plugins or []:
        if getattr(row, "kind", "") == "imagegen":
            return row, parse_config(getattr(row, "config", None))
    return None


def split_requests(content: str) -> tuple[str, list[str]]:
    """从模型回复里取出 `<hne-image>…</hne-image>` 请求，并返回**去掉标签后的正文**。

    ★ 幂等：已经剥过的正文再调用一次不会变化。
    ★ 无论插件是否启用都要调用（见模块顶部说明）。
    """
    text = str(content or "")
    prompts: list[str] = []

    def take_block(match: re.Match[str]) -> str:
        attrs = match.group(1) or ""
        body = match.group(2) or ""
        prompt = body.strip() or _attr_prompt(attrs)
        _append_prompt(prompts, prompt)
        return ""

    def take_self(match: re.Match[str]) -> str:
        _append_prompt(prompts, _attr_prompt(match.group(1) or ""))
        return ""

    text = _TAG_RE.sub(take_block, text)
    text = _SELF_TAG_RE.sub(take_self, text)
    # 未闭合 / 多余的标签：只剥标签，不把里面的字吃掉（避免误删正文）
    text = _LEFTOVER_RE.sub("", text)
    # 去重且截断（模型偶尔会把同一句描述写两遍）
    unique: list[str] = []
    for prompt in prompts:
        if prompt and prompt not in unique:
            unique.append(prompt)
    return text, unique[:MAX_IMAGES]


def _attr_prompt(attrs: str) -> str:
    match = _PROMPT_ATTR_RE.search(attrs or "")
    if not match:
        return ""
    return html.unescape(match.group(1) if match.group(1) is not None else match.group(2) or "")


def _append_prompt(prompts: list[str], raw: Any) -> None:
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not text:
        return
    prompts.append(text[:MAX_PROMPT_CHARS])


def render_contract(spec: dict[str, Any]) -> str:
    """给模型的「生图协议」说明（只对 trigger=model 生效）。

    放在提示词末尾（权重最高），并且**不强制它每轮都画** ——
    "值不值得画"应该由模型结合语境判断；强制每轮画会制造大量无意义配图。
    """
    if str(spec.get("trigger")) != "model":
        return ""
    max_images = int(spec.get("max_images") or 1)
    return (
        "【生图协议】当这一轮的画面值得画出来时，在正文末尾另起一行写：\n"
        "<hne-image>英文画面描述：人物、动作、环境、光线、构图；"
        "不要写对白、文字、水印、UI</hne-image>\n"
        f"每轮最多 {max_images} 个；系统会把标签替换成图片，标签本身不会显示给用户。"
        "不需要配图时完全不写，不要为了凑数而写。"
    )


def should_generate(spec: dict[str, Any], *, requests: list[str], round_no: int) -> bool:
    """这一轮该不该生成图片。"""
    if str(spec.get("trigger")) == "model":
        return bool(requests)
    interval = int(spec.get("interval") or 1)
    if interval <= 1:
        return True
    return int(round_no or 0) % interval == 0


def build_auto_scene(user_text: str, assistant_text: str, max_chars: int) -> str:
    """把这一轮的上下文压成一段画面描述素材。

    ★ 不把整段历史都塞进去：生图提示词越长越容易失焦，而且很多图像模型对
      提示词长度有硬限制。用户/角色的名字由模板单独提供。
    """
    reply = str(assistant_text or "").strip()
    if not reply:
        return ""
    limit = max(100, int(max_chars or 1200))
    if len(reply) > limit:
        reply = reply[:limit].rstrip() + "…"
    ask = str(user_text or "").strip()
    if ask:
        if len(ask) > 300:
            ask = ask[:300].rstrip() + "…"
        return f"玩家：{ask}\n\n叙述：{reply}"
    return reply


def build_prompts(
    spec: dict[str, Any],
    *,
    requests: list[str],
    scene: str,
    char: str,
    user: str,
) -> list[str]:
    """按配置把「这一轮要画什么」变成最终发给图像模型的提示词列表。"""
    trigger = str(spec.get("trigger") or "auto")
    raws = list(requests) if trigger == "model" else ([scene] if scene else [])
    if not raws:
        return []
    style = str(spec.get("style") or "").strip()
    template = str(spec.get("prompt_template") or "").strip()
    max_images = int(spec.get("max_images") or 1)

    prompts: list[str] = []
    for raw in raws[:max_images]:
        item = str(raw or "").strip()
        if not item:
            continue
        if template:
            text = _fill_template(
                template, {"scene": item, "char": char, "user": user, "style": style}
            )
        elif trigger == "model":
            # 模型自己写的描述通常已经包含构图/光线，这里只补统一画风。
            text = item + (f"\n\nStyle: {style}" if style else "")
        else:
            text = _fill_template(
                DEFAULT_PROMPT_TEMPLATE,
                {"scene": item, "char": char, "user": user, "style": style},
            )
        text = text.strip()[:MAX_PROMPT_CHARS]
        if text:
            prompts.append(text)
    return prompts


def _fill_template(template: str, values: dict[str, str]) -> str:
    """替换已知占位符；不认识的 `{foo}` 原样保留（不报 KeyError）。

    用字符串 replace 逐个替换而不是 format()：用户模板里可能带 JSON 花括号，
    一次 format 就会把它们当成占位符并抛异常。
    """
    text = template
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value or ""))
    return text


def join_endpoint(base_url: str, path: str) -> str:
    """把用户填的 base_url 与接口路径拼起来。

    用户可能填 `https://api.openai.com/v1`、`.../v1/`，也可能直接把完整端点
    粘进来（`.../v1/images/generations`）。三种都要能跑。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/images/generations") or base.endswith("/chat/completions"):
        return base
    return f"{base}/{str(path).lstrip('/')}"


def normalize_b64(value: str) -> str:
    """去掉 base64 里的空白并补齐 padding（部分网关会换行）。"""
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return ""
    remainder = len(text) % 4
    if remainder:
        text += "=" * (4 - remainder)
    return text


def extract_image_ref(value: Any) -> dict[str, Any] | None:
    """从一个值里尽量宽容地挖出图片引用。

    返回：
        {"kind": "b64", "media_type": "image/png"?, "data": "..."}
        {"kind": "url", "url": "https://…"}
    挖不到返回 None。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        # ★ b64_json 是明确约定：值再短也是 base64，不能按"长度不足"丢掉
        raw_b64 = value.get("b64_json")
        if isinstance(raw_b64, str) and raw_b64.strip():
            return {"kind": "b64", "data": normalize_b64(raw_b64)}
        # 常见形态：{url} / {image_url:{url}} / {data} / {image} / {content} / {text}
        for key in ("url", "data", "image", "image_url", "content", "text"):
            if key in value:
                found = extract_image_ref(value[key])
                if found:
                    return found
        return None

    text = str(value or "").strip()
    if not text:
        return None
    # ① data URI（有些网关把 base64 直接嵌在 content 里）
    data_uri = re.search(r"data:(image/[a-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)", text, re.I)
    if data_uri:
        return {
            "kind": "b64",
            "media_type": data_uri.group(1).lower(),
            "data": normalize_b64(data_uri.group(2)),
        }
    # ② markdown 图 ![alt](url)
    markdown = re.search(r"!\[[^\]]*\]\(\s*([^)\s]+)\s*\)", text, re.I)
    if markdown:
        return extract_image_ref(markdown.group(1))
    # ③ 裸 http(s) URL（chat 出图最常见的返回形态）
    url = re.search(r"https?://[^\s\"'<>()]+", text, re.I)
    if url:
        return {"kind": "url", "url": url.group(0)}
    # ④ 纯 base64（没有 data URI 前缀的网关）。
    #   ★ 阈值 64 而不是 256：小尺寸测试图 / 图标也可能只有几十字节；
    #     真正的图片 base64 通常几万字符，64 已经足够避开普通单词。
    if len(text) >= 64 and re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", text):
        return {"kind": "b64", "data": normalize_b64(text)}
    return None


def extract_chat_image(data: Any) -> dict[str, Any] | None:
    """解析 `/chat/completions` 响应里的图片（按宽容优先级逐个字段试）。"""
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}

    # ① message.images[]
    images = message.get("images")
    if isinstance(images, list):
        for item in images:
            found = extract_image_ref(item)
            if found:
                return found
    # ② message.content：字符串（markdown/URL/data URI）或数组（image_url / image 项）
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            found = extract_image_ref(part)
            if found:
                return found
    elif content is not None:
        found = extract_image_ref(content)
        if found:
            return found
    # ③ 有些网关直接回 data[0].{url,b64_json}（与 /images 同形）
    rows = data.get("data")
    if isinstance(rows, list) and rows:
        found = extract_image_ref(rows[0])
        if found:
            return found
    # ④ 兜底扫整个响应
    return extract_image_ref(data)


def extract_images_response(data: Any, protocol: str) -> list[dict[str, Any]]:
    """从上游响应里取出图片引用列表（最多 MAX_IMAGES 张）。"""
    found: list[dict[str, Any]] = []
    if protocol == "openai-images":
        rows = data.get("data") if isinstance(data, dict) else None
        if isinstance(rows, list):
            for item in rows:
                ref = extract_image_ref(item)
                if ref:
                    found.append(ref)
        if not found:
            ref = extract_image_ref(data)
            if ref:
                found.append(ref)
    else:
        ref = extract_chat_image(data)
        if ref:
            found.append(ref)
    return found[:MAX_IMAGES]


# ==================================================================
#  图片字节识别（只认浏览器能安全显示的位图，不认 SVG）
# ==================================================================
def detect_image_type(data: bytes) -> tuple[str, str] | None:
    """按文件头判断图片类型，返回 (扩展名, MIME)；不是支持的位图则 None。

    ★ 故意不支持 SVG：SVG 可以内嵌脚本，而生成结果会直接进 `<img src>`。
      只存 PNG/JPEG/WebP/GIF，浏览器只会当位图解码。
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def decode_b64_image(value: str) -> bytes:
    """解码 base64；失败时抛 ValueError（由服务层转成可读错误）。"""
    text = normalize_b64(value)
    if not text:
        raise ValueError("上游返回的 base64 图片为空")
    try:
        return base64.b64decode(text, validate=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("上游返回的 base64 图片无法解码") from exc
