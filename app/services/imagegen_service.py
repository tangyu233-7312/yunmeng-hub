"""生图插件的**执行层**：读配置、解密钥、调上游、落盘、写回消息。

==================== 分层 ====================
纯规则（标签解析 / 提示词拼装 / 响应挖图）在 `app/narrative/imagegen.py`；
这里只做它做不到的事：数据库、Fernet 密钥、HTTP、文件系统。

==================== 三条纪律（都对应真实的坑）====================
1. **生图失败不能拖垮这一轮对话**：正文已经落库、已经流给用户了。这里所有
   异常都收敛成一句可读的 `error`，由引擎作为提示回传，绝不让整轮 500。
2. **不重试**：失败就是失败。生成一张图要花一次钱，自动重试会在上游抖动时
   连续扣费；用户看到"画失败了"比看到一条 60 秒后才出现的错误更诚实。
3. **只存浏览器能安全显示的位图**：PNG / JPEG / WebP / GIF。SVG 可以内嵌脚本，
   而生成结果会直接进 `<img src>`，所以文件头不认识就拒绝落盘。

==================== 密钥 ====================
生图 Key 与模型 API Key 走同一套 Fernet 加密（`app/core/security.py`），
数据库里只有 `api_key_enc` 密文；对外序列化时由 `plugin_service.serialize`
剥掉密文、只给脱敏形式。这里负责在真正调用前解密。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import data_root
from app.core.exceptions import ConfigurationError
from app.core.security import get_api_key_cipher
from app.db.models import Message, NarrativeSession
from app.narrative import imagegen as rules
from app.services import plugin_service

#: 单张图片上限（防"下载一个几百 MB 的文件把磁盘写满"）
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
#: 落盘目录（相对数据根目录）与对外 URL 前缀
MEDIA_SUBDIR = Path("data") / "media" / "imagegen"
MEDIA_URL_PREFIX = "/media/imagegen/"


class ImageGenError(Exception):
    """生图失败（消息已经是可以直接给用户看的中文）。"""


@dataclass
class ImageGenPlan:
    """这一轮准备画什么（在真正发请求之前就能算出来，给 SSE 发 image_start）。"""

    plugin_name: str
    spec: dict[str, Any]
    prompts: list[str]
    api_key: str = ""
    error: str | None = None


@dataclass
class ImageGenOutcome:
    """执行结果（无论成功失败都返回，不抛异常）。"""

    images: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None


# ==================================================================
#  基础路径 / 读取
# ==================================================================
def media_dir() -> Path:
    """生成图片的落盘目录（数据根目录下，与数据库/日志同级）。"""
    return data_root() / MEDIA_SUBDIR


def load_images(message: Message | None) -> list[dict[str, Any]]:
    """从消息里读出图片列表（刷新界面 / done 事件共用）。"""
    if message is None:
        return []
    raw = getattr(message, "images_json", None)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get("url")]


def _load_plugin(db: Session, user_id: int) -> tuple[Any, dict[str, Any]] | None:
    found = rules.find_spec(plugin_service.load_enabled(db, user_id))
    return found


def _decrypt_key(spec: dict[str, Any]) -> tuple[str, str | None]:
    """解出生图 Key；没有配置就返回空串（允许本地无鉴权服务）。"""
    ciphertext = str(spec.get("api_key_enc") or "")
    if not ciphertext:
        return "", None
    try:
        return get_api_key_cipher().decrypt(ciphertext), None
    except ConfigurationError:
        return "", (
            "生图 Key 解密失败：密文已损坏，或 HNE_API_KEY_ENCRYPTION_KEY 被更换过。"
            "请到「插件」页重新填写生图 Key。"
        )


def _previous_user_text(db: Session, session: NarrativeSession, message: Message) -> str:
    row = db.scalar(
        select(Message)
        .where(
            Message.session_id == session.id,
            Message.role == "user",
            Message.id < message.id,
        )
        .order_by(Message.id.desc())
        .limit(1)
    )
    return str(getattr(row, "content", "") or "")


def _round_no(db: Session, session: NarrativeSession, message: Message) -> int:
    """这是第几轮（按"截至这条回复的 assistant 消息条数"算）。

    ★ 不用 `session.message_count // 2`：开场白、中断保留、重新生成都会让
      这个数与"轮"对不上。数 assistant 消息虽然多一次查询，但语义准确。
    """
    count = db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.session_id == session.id,
            Message.role == "assistant",
            Message.id <= message.id,
        )
    )
    return int(count or 0)


# ==================================================================
#  规划：这一轮要不要画、画什么
# ==================================================================
def plan_turn(
    db: Session,
    session: NarrativeSession,
    message: Message,
    *,
    card_name: str = "",
    user_name: str = "",
    requests: list[str] | None = None,
) -> ImageGenPlan | None:
    """算出这一轮的配图计划；不需要/没配置好则返回 None。

    ★ 没填 base_url 或 model 时**直接跳过**（不每轮弹错误）：
      插件列表里会显示"未配置"，用户能看到原因；每轮都报错只会淹没正文。
      但"配了 Key 却解不开"属于故障，会返回带 error 的计划，让这一轮如实告诉用户。
    """
    found = _load_plugin(db, session.user_id)
    if found is None:
        return None
    row, spec = found
    if not str(spec.get("base_url") or "").strip() or not str(spec.get("model") or "").strip():
        logger.warning(
            "生图插件未配置完整，已跳过 | plugin={} 需要 base_url 与 model", getattr(row, "name", "?")
        )
        return None

    requested = [str(item) for item in (requests or []) if str(item).strip()]
    trigger = str(spec.get("trigger") or "auto")
    if trigger == "model":
        if not requested:
            return None
        scene = ""
        prompt_requests = requested
    else:
        # ★ interval=1 时不查轮次（省掉每轮一次 COUNT）；只有真的要"每 N 轮"才查。
        interval = int(spec.get("interval") or 1)
        if interval > 1 and _round_no(db, session, message) % interval != 0:
            return None
        scene = rules.build_auto_scene(
            _previous_user_text(db, session, message),
            str(message.content or ""),
            int(spec.get("max_chars") or 1200),
        )
        prompt_requests = []

    prompts = rules.build_prompts(
        spec,
        requests=prompt_requests,
        scene=scene,
        char=str(card_name or ""),
        user=str(user_name or ""),
    )
    if not prompts:
        return None

    api_key, error = _decrypt_key(spec)
    return ImageGenPlan(
        plugin_name=str(getattr(row, "name", "") or "生图"),
        spec=spec,
        prompts=prompts,
        api_key=api_key,
        error=error,
    )


# ==================================================================
#  执行：调上游 + 落盘 + 写回消息
# ==================================================================
def execute_plan(
    db: Session,
    session: NarrativeSession,
    message: Message,
    plan: ImageGenPlan,
) -> ImageGenOutcome:
    """执行计划；任何失败都收敛成 outcome.error，不向上抛。"""
    if plan.error:
        return ImageGenOutcome(error=plan.error)

    images: list[dict[str, Any]] = []
    error: str | None = None
    for prompt in plan.prompts:
        try:
            images.append(_generate_one(plan.spec, plan.api_key, prompt))
        except ImageGenError as exc:
            error = str(exc)
            break
        except BaseException as exc:  # noqa: BLE001 - 生图绝不能拖垮对话
            logger.exception("生图出现未预期异常 | plugin={}", plan.plugin_name)
            error = f"生图出现未预期错误：{type(exc).__name__}"
            break

    if images:
        message_id = getattr(message, "id", None)
        try:
            message.images_json = json.dumps(images, ensure_ascii=False)
            db.commit()
            db.refresh(message)
        except BaseException as exc:  # noqa: BLE001 - 落库失败也不能拖垮正文
            db.rollback()
            logger.exception(
                "生图已生成但保存失败 | session_id={} message_id={}",
                session.id,
                message_id,
            )
            return ImageGenOutcome(
                images=images,
                error=f"配图已生成但保存失败：{type(exc).__name__}",
            )
        logger.info(
            "生图完成 | session_id={} message_id={} plugin={} images={}",
            session.id,
            message_id,
            plan.plugin_name,
            len(images),
        )

    if error:
        logger.warning(
            "生图失败 | session_id={} message_id={} plugin={} err={}",
            session.id,
            message.id,
            plan.plugin_name,
            error,
        )
        prefix = f"配图只成功了 {len(images)} 张，后续失败：" if images else "配图失败："
        return ImageGenOutcome(images=images, error=prefix + error)
    return ImageGenOutcome(images=images)


def maybe_generate(
    db: Session,
    session: NarrativeSession,
    message: Message,
    *,
    card_name: str = "",
    user_name: str = "",
    requests: list[str] | None = None,
) -> ImageGenOutcome | None:
    """非流式路径的便捷入口：规划 + 执行。"""
    plan = plan_turn(
        db,
        session,
        message,
        card_name=card_name,
        user_name=user_name,
        requests=requests,
    )
    if plan is None:
        return None
    return execute_plan(db, session, message, plan)


def _generate_one(spec: dict[str, Any], api_key: str, prompt: str) -> dict[str, Any]:
    """生成一张图并落盘，返回消息里要存的图片记录。"""
    protocol = str(spec.get("protocol") or "openai-images")
    base_url = str(spec.get("base_url") or "")
    model = str(spec.get("model") or "")
    timeout = int(spec.get("timeout_seconds") or 120)

    if protocol == "openai-chat":
        endpoint = rules.join_endpoint(base_url, "chat/completions")
        body: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        protocol = "openai-images"
        endpoint = rules.join_endpoint(base_url, "images/generations")
        body = {"model": model, "prompt": prompt, "n": 1}
        if str(spec.get("size") or "").strip():
            body["size"] = str(spec.get("size")).strip()
        if str(spec.get("quality") or "").strip():
            body["quality"] = str(spec.get("quality")).strip()

    # ★ extra_body 最后合并：用户可以覆盖标准字段（例如某些网关要 response_format）。
    extra = spec.get("extra_body")
    if isinstance(extra, dict):
        body.update(extra)
    if not endpoint:
        raise ImageGenError("未配置生图接口地址（base_url）")

    response = _post_json(endpoint, body, api_key, timeout, protocol)
    refs = rules.extract_images_response(response, protocol)
    if not refs:
        raise ImageGenError("生图接口返回成功，但响应里没有找到图片（URL / base64 / data URI 都没有）")
    data = _materialize(refs[0], timeout)
    url = _save_image(data)
    return {
        "url": url,
        "prompt": prompt[:1000],
        "model": model,
        "protocol": protocol,
        "size": str(spec.get("size") or ""),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _post_json(
    endpoint: str,
    body: dict[str, Any],
    api_key: str,
    timeout: int,
    protocol: str,
) -> Any:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with httpx.Client(
            timeout=httpx.Timeout(max(10, timeout), connect=10.0),
            follow_redirects=True,
        ) as client:
            response = client.post(endpoint, headers=headers, json=body)
    except httpx.TimeoutException as exc:
        raise ImageGenError(f"生图请求超时（{timeout} 秒）") from exc
    except httpx.HTTPError as exc:
        raise ImageGenError(f"生图请求失败：{type(exc).__name__}") from exc

    if response.status_code >= 400:
        raise ImageGenError(
            f"生图接口返回 HTTP {response.status_code}：{_error_detail(response)}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ImageGenError(
            f"生图接口返回的不是 JSON（{protocol} 协议可能不适用于这个端点）"
        ) from exc


def _error_detail(response: httpx.Response) -> str:
    """从上游错误响应里挖一句可读原因，别把整页 HTML 甩给用户。"""
    raw = ""
    try:
        raw = response.text
    except Exception:  # noqa: BLE001 - 读不到 body 就用状态码
        return "无错误详情"
    text = raw.strip()
    if not text:
        return "无错误详情"
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            text = str(error["message"])
        elif isinstance(error, str):
            text = error
        elif data.get("message"):
            text = str(data["message"])
    return text.replace("\n", " ").strip()[:300] or "无错误详情"


def _materialize(ref: dict[str, Any], timeout: int) -> bytes:
    """把解析出的图片引用变成字节：base64 直接解码，URL 下载。"""
    if ref.get("kind") == "b64":
        data = rules.decode_b64_image(str(ref.get("data") or ""))
        _check_size(data)
        return data

    url = str(ref.get("url") or "")
    if not url:
        raise ImageGenError("生图响应里没有可用的图片地址")
    try:
        with httpx.Client(
            timeout=httpx.Timeout(max(10, timeout), connect=10.0),
            follow_redirects=True,
        ) as client:
            response = client.get(url)
    except httpx.TimeoutException as exc:
        raise ImageGenError(f"图片下载超时（{timeout} 秒）") from exc
    except httpx.HTTPError as exc:
        raise ImageGenError(f"图片下载失败：{type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise ImageGenError(f"图片下载返回 HTTP {response.status_code}")
    data = response.content
    _check_size(data)
    return data


def _check_size(data: bytes) -> None:
    if not data:
        raise ImageGenError("上游返回的图片是空内容")
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ImageGenError(
            f"图片太大（{len(data) / 1024 / 1024:.1f} MB，上限 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB）"
        )


def _save_image(data: bytes) -> str:
    """按文件头定扩展名并原子落盘，返回可给浏览器用的站内 URL。"""
    detected = rules.detect_image_type(data)
    if detected is None:
        raise ImageGenError(
            "上游返回的内容不是 PNG / JPEG / WebP / GIF 图片（可能是 HTML 错误页或 SVG）"
        )
    ext, _mime = detected
    directory = media_dir()
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{ext}"
    target = directory / name
    temp = directory / (name + ".part")
    try:
        temp.write_bytes(data)
        os.replace(temp, target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return MEDIA_URL_PREFIX + name


# ==================================================================
#  删除（撤回 / 编辑 / 重新生成 / 删会话时清掉文件）
# ==================================================================
def remove_files(images: list[dict[str, Any]] | None) -> int:
    """删除消息记录里的图片文件；返回删掉的张数（失败不抛）。"""
    removed = 0
    directory = media_dir()
    for image in images or []:
        if not isinstance(image, dict):
            continue
        url = str(image.get("url") or "")
        if not url.startswith(MEDIA_URL_PREFIX):
            continue
        name = url[len(MEDIA_URL_PREFIX) :]
        # 只认单层文件名：挡 ../ 之类的路径拼接
        if not name or Path(name).name != name:
            continue
        try:
            (directory / name).unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed


def remove_message_images(message: Message | None) -> int:
    return remove_files(load_images(message))
