"""从 PNG 图片里取出角色卡数据。

==================== 为什么角色卡会藏在图片里？====================
SillyTavern 生态的分发方式很特别：角色卡不是 .json 文件，而是一张**图片**。
卡片数据被塞进 PNG 的一个文本块里，图片本身还能正常显示（通常是人物的立绘）。
好处是一张图 = 头像 + 人设 + 世界书 + 开场白，分享出去不会丢东西。

==================== 数据到底存在哪？====================
PNG 文件由一连串「数据块（chunk）」组成，每个块的结构固定：

    ┌──────────┬──────────┬─────────────┬──────────┐
    │ 长度 4B  │ 类型 4B  │  数据 N B   │ CRC 4B   │
    └──────────┴──────────┴─────────────┴──────────┘

角色卡放在 **tEXt** 块里，格式是 `关键字 \\0 文本`：

    tEXt  "chara\\0eyJzcGVjIjoiY2hhcmFfY2FyZF92MiIs..."   ← 文本是 base64 的 JSON

关键字有两种（新版用 ccv3 存 V3 卡，老版用 chara 存 V2/V1 卡），
本项目**优先取 ccv3**，没有再看 chara。

除了 tEXt，还有 **iTXt** 块（支持压缩与 UTF-8）。少数工具会用它，所以也一并支持。

★ 顺便解释一个「为什么」：**为什么卡片数据要 base64 编码？**
  tEXt 是 PNG 早期就定的块类型，按规范只能存 **Latin-1** 字符，
  直接放中文/日文会写不进去。而角色卡 JSON 是 UTF-8 的。
  base64 的结果全是 ASCII，套一层就能安全塞进 tEXt —— 这就是那个编码的由来。
  （后来的 iTXt 原生支持 UTF-8，所以走 iTXt 的卡往往不做 base64。）

==================== 本模块的职责边界 ====================
只做一件事：**PNG 字节流 → 角色卡 dict**。
不碰数据库、不碰 HTTP，解析失败抛 PngCardError。
这样它可以被单独测试（构造几个字节就能测），也便于将来复用。
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib

#: PNG 文件头固定这 8 个字节，用来快速判断「这到底是不是 PNG」
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: 角色卡可能使用的文本块关键字，**按优先级排列**：
#: ccv3 是较新的 V3 格式，如果同一张图里两者都有，应当优先用新的。
CARD_KEYWORDS = ("ccv3", "chara")

#: 只关心这两种能装文本的块
_TEXT_CHUNK_TYPES = (b"tEXt", b"iTXt")


class PngCardError(ValueError):
    """PNG 解析失败。

    继承 ValueError 而不是项目的 AppException：
    本模块属于纯工具层，不该知道 HTTP 状态码这类 Web 概念。
    由 service 层负责翻译成对用户友好的业务异常。
    """


def _iter_chunks(data: bytes):
    """遍历 PNG 的所有数据块，逐个产出 (类型, 数据)。

    ★ 这里刻意**不校验 CRC**：CRC 用于检测数据损坏，但我们的目的只是
      把角色卡读出来。图片本身哪怕有轻微损坏，只要卡片那个块是完好的，
      就应该能正常导入 —— 为此拒绝一张卡对用户毫无帮助。

    ★ 解析到文件末尾时若发现块长度越界，直接停止遍历而不是报错：
      有些工具会在 PNG 尾部追加自定义数据，不该因此判定整张图无效。
    """
    offset = len(PNG_SIGNATURE)

    while offset + 12 <= len(data):  # 至少要有 长度4 + 类型4 + CRC4
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length

        if payload_end + 4 > len(data):
            # 块声明的长度超出了文件实际大小 —— 文件被截断了，停止解析
            return

        yield chunk_type, data[payload_start:payload_end]

        # 跳过 数据 + 4 字节 CRC
        offset = payload_end + 4


def _decode_text_chunk(chunk_type: bytes, payload: bytes) -> tuple[str, str] | None:
    """把一个文本块解成 (关键字, 文本)。不是文本块或格式不对时返回 None。"""
    if chunk_type == b"tEXt":
        # tEXt 格式：关键字 \0 文本（都是 Latin-1 编码）
        separator = payload.find(b"\x00")
        if separator < 0:
            return None
        keyword = payload[:separator].decode("latin-1")
        text = payload[separator + 1 :].decode("latin-1")
        return keyword, text

    if chunk_type == b"iTXt":
        # iTXt 格式：关键字 \0 压缩标志 压缩方法 语言标签 \0 翻译后关键字 \0 文本
        separator = payload.find(b"\x00")
        if separator < 0:
            return None
        keyword = payload[:separator].decode("latin-1")

        rest = payload[separator + 1 :]
        if len(rest) < 2:
            return None
        compression_flag = rest[0]
        # rest[1] 是压缩方法，目前规范只定义了 0，可以不看
        rest = rest[2:]

        # 跳过「语言标签」
        language_end = rest.find(b"\x00")
        if language_end < 0:
            return None
        rest = rest[language_end + 1 :]

        # 跳过「翻译后的关键字」
        translated_end = rest.find(b"\x00")
        if translated_end < 0:
            return None
        text_bytes = rest[translated_end + 1 :]

        if compression_flag == 1:
            try:
                text_bytes = zlib.decompress(text_bytes)
            except zlib.error as exc:
                raise PngCardError(
                    "PNG 里角色卡文本块声明为已压缩，但解压失败（文件可能已损坏）"
                ) from exc

        # iTXt 的文本按规范是 UTF-8；用 replace 兜底，
        # 避免个别字节错误导致整张卡报废
        return keyword, text_bytes.decode("utf-8", errors="replace")

    return None


def _decode_card_payload(text: str) -> dict:
    """把文本块里的内容解析成角色卡 dict。

    标准做法是 base64 编码的 JSON，但实际文件里两种写法都遇到过：
      · base64(JSON)  ← SillyTavern 等主流工具
      · 裸 JSON       ← 少数工具图省事直接塞原文

    ★ 策略：两种都试。先试 base64，失败就按原文当 JSON 解析，
      这样不管来源是哪种都能吃下来。
    """
    # ★ base64 文本里可能有换行（有些工具会按 64 列折行），所以要临时去掉空白。
    #   但**绝不能**把这个「去空白」的结果当成 JSON 原文去解析 ——
    #   那会把字符串内部有意义的空格也一起删掉：
    #       "Hello there, traveller."  ->  "Hellothere,traveller."
    #   所以下面 base64 用去空白的版本，裸 JSON 一律用原文。
    stripped = text.strip()
    compact = "".join(text.split())
    if not stripped:
        raise PngCardError("PNG 里的角色卡文本块是空的")

    candidates: list[str] = []
    try:
        # 补足 padding：有些工具输出的 base64 省略了末尾的 '='
        padded = compact + "=" * (-len(compact) % 4)
        candidates.append(base64.b64decode(padded, validate=True).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        # 不是合法的 base64（比如本来就是裸 JSON），跳过
        pass

    # 裸 JSON 放在最后兜底，用的是**原始文本**
    candidates.append(stripped)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise PngCardError(
        "PNG 里的角色卡数据无法解析：既不是 base64 编码的 JSON，也不是合法的 JSON 原文"
    )


def extract_card_json(data: bytes) -> dict:
    """从 PNG 字节流中提取角色卡 JSON。

    失败时抛出 PngCardError（附带可操作的中文提示）。
    """
    if not data:
        raise PngCardError("上传的文件是空的")

    if not data.startswith(PNG_SIGNATURE):
        raise PngCardError(
            "这不是一个 PNG 图片（文件头不匹配）。"
            "如果这是一个 .json 角色卡文件，请改用 JSON 导入接口"
        )

    # 收集所有候选文本块。同一个关键字可能出现多次，取**第一个**：
    # 后出现的通常是编辑工具追加的副本，前面的才是原始数据。
    found: dict[str, str] = {}

    for chunk_type, payload in _iter_chunks(data):
        if chunk_type not in _TEXT_CHUNK_TYPES:
            continue
        decoded = _decode_text_chunk(chunk_type, payload)
        if decoded is None:
            continue

        keyword, text = decoded
        if keyword in CARD_KEYWORDS and keyword not in found:
            found[keyword] = text
            # 关键字已经集齐了，没必要再往下扫
            if len(found) == len(CARD_KEYWORDS):
                break

    for keyword in CARD_KEYWORDS:
        if keyword in found:
            return _decode_card_payload(found[keyword])

    raise PngCardError(
        "这张 PNG 里没有找到角色卡数据。"
        f"（已查找文本块关键字：{'、'.join(CARD_KEYWORDS)}）"
        "请确认这张图确实是角色卡，而不是一张普通图片"
    )
