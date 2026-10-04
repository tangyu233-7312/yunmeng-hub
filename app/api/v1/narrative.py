"""叙事会话与对话接口。

==================== 接口一览 ====================
    POST   /narrative/sessions                     建会话（写入角色卡开场白）
    GET    /narrative/sessions                     会话列表（按最后活跃时间倒序）
    GET    /narrative/sessions/{id}                会话详情（含消息历史）
    PATCH  /narrative/sessions/{id}                重命名 / 归档 / 换卡换模型
    DELETE /narrative/sessions/{id}                删除（级联删消息）
    POST   /narrative/sessions/{id}/messages       发一条用户消息（非流式）
    GET    /narrative/sessions/{id}/stream         发一条用户消息（SSE 流式）★
    GET    /narrative/sessions/{id}/regenerate     重新生成最后一条回复（SSE 流式）
    PATCH  /narrative/sessions/{id}/messages/{mid} 编辑你说过的一句话（删掉其后内容）
    POST   /narrative/sessions/{id}/messages/{mid}/retract  撤回最后一轮
    GET    /narrative/sessions/{id}/memories        查看这个会话的长期记忆（3.9）
    POST   /narrative/sessions/{id}/memories        记住一条内容（3.9）
    DELETE /narrative/sessions/{id}/memories        清空这个会话的长期记忆（3.9）
    DELETE /narrative/memories                      清空我的全部长期记忆（3.9）

==================== 为什么流式接口用 GET？====================
浏览器原生的 EventSource 只支持 GET。虽然本项目前端改用 fetch + 流式读取
（因为 EventSource **无法自定义请求头**，只能把 JWT 塞进 URL，
那会把令牌写进服务器日志与浏览器历史，是明确的安全隐患），
但保留 GET 形态让 `curl "…/stream?content=你好"` 就能直接调通，
调试与答辩演示都方便得多。

代价是消息正文要放在查询串里（受 URL 长度限制，本项目限制 2 万字符），
且请求体不会被记录到访问日志里 —— 对"用户自己打给模型的短句"来说可以接受。

==================== 这个模块里唯一一个 async def ====================
SSE 端点必须是 async def，而本项目的数据库是**同步** SQLAlchemy。
红线：async def 里禁止直接查数据库，否则会卡住整个事件循环。
所以这里的做法是：
    · 先在线程池里做认证后的会话校验（失败直接返回 JSON 404/400，前端好处理）
    · 再用 run_in_threadpool 逐步驱动同步生成器（engine.stream_reply）
这样既拿到了流式输出的体验，又不用引入异步 ORM 的复杂度。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse
from loguru import logger
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, DbSession
from app.core.context import get_request_id
from app.core.exceptions import (
    AppException,
    BadRequestError,
    ConflictError,
    MemoryStoreError,
)
from app.db.models import Message, NarrativeSession
from app.db.mysql import session_scope
from app.schemas.common import ApiResponse, Page
from app.schemas.narrative import (
    MemoryFactCreate,
    MemoryHitOut,
    MemoryListOut,
    MessageCreate,
    MessageEditResult,
    MessageUpdate,
    PromptInfo,
    RetractResult,
    SendResult,
    SessionBrief,
    SessionContextInfo,
    SessionCreate,
    SessionDetail,
    MemoryAnchorsUpdate,
    SessionStateUpdate,
    SummarySettingsUpdate,
    SessionUpdate,
    TranslateSettingsUpdate,
)
from app.narrative import engine, memory
from app.narrative import anchors as anchors_mod
from app.narrative import state as state_mod
from app.narrative import summary as summary_mod
from app.narrative import translate as translate_mod
from app.narrative import sessions as session_service
from app.services import plugin_service

router = APIRouter()

# ==================================================================
#  SSE 事件拼装
# ==================================================================
def sse_event(event: str, data) -> str:
    """把一条事件编码成 SSE 报文。

    ★ 必须是「两个换行」结尾 —— SSE 规范里空行才代表一条事件结束。
      少一个换行会让浏览器把下面几条事件粘成一条，前端只能拿到残缺数据。
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def error_event_payload(exc: BaseException) -> dict:
    """把异常转成 error 事件的负载。

    ★ 复用 AppException 自己的 code / message / status_code 与 detail，
      与全局异常处理器（app/core/exceptions.py）保持**同一套语义** ——
      否则同一个错误在非流式下是一个样子、流式下又是另一个样子，
      前端得写两套判断，用户看到的提示也会不一致。
    """
    if isinstance(exc, AppException):
        return {
            "code": exc.code,
            "message": exc.message,
            "status": exc.status_code,
            "detail": exc.detail,
            "request_id": get_request_id(),
        }
    logger.exception("流式对话出现未预期异常")
    return {
        "code": "INTERNAL_ERROR",
        "message": "服务器内部错误",
        "status": 500,
        "detail": None,
        "request_id": get_request_id(),
    }


# ==================================================================
#  会话 CRUD
# ==================================================================
@router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    summary="新建叙事会话",
    response_model=ApiResponse[SessionDetail],
)
def create_session(
    payload: SessionCreate, db: DbSession, current_user: CurrentUser
) -> ApiResponse[SessionDetail]:
    """建会话：选一张角色卡（自己的或别人公开的）+ 一个模型配置。

    ★ 角色卡的开场白会被写成**第一条 assistant 消息**，
      这样用户一进来就看到角色先开口，而不是一片空白等自己先说话。

    不传 llm_provider_id 时自动用你的默认模型；一个模型配置都没有时
    会话照样会建起来（只是暂时没法对话），界面会给出提醒。
    """
    session = session_service.create_session(db, current_user.id, payload)
    messages, total, truncated = session_service.get_messages(db, session.id)
    detail = session_service.serialize_detail(
        db,
        session,
        messages=messages,
        messages_total=total,
        messages_truncated=truncated,
        prompt=_prompt_preview(db, session),
    )
    return ApiResponse.ok(detail, message="会话已创建")


@router.get(
    "/sessions",
    summary="列出叙事会话",
    response_model=ApiResponse[Page[SessionBrief]],
)
def list_sessions(
    db: DbSession,
    current_user: CurrentUser,
    status_filter: Annotated[
        str,
        Query(
            alias="status",
            pattern="^(active|archived|all)$",
            description="active 进行中 / archived 已归档 / all 全部",
        ),
    ] = "active",
    q: str | None = Query(default=None, max_length=100, description="按标题搜索"),
    limit: int = Query(default=session_service.DEFAULT_LIMIT, ge=1, le=session_service.MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[Page[SessionBrief]]:
    """分页列出会话（按最后活跃时间倒序）。

    ★ 列表是**精简结构**：不带消息正文，只带最后一条的预览。
      一个聊了几百轮的会话，正文可能有几 MB，全带上列表就废了。
    """
    items, total = session_service.list_sessions(
        db,
        current_user.id,
        status=None if status_filter == "all" else status_filter,
        q=q,
        limit=limit,
        offset=offset,
    )
    return ApiResponse.ok(
        Page.create(items, total=total, limit=limit, offset=offset),
        message=f"共 {total} 个会话",
    )


def _prompt_preview(db: Session, session) -> PromptInfo:
    """按当前设定构建一次提示词，但**不调用模型**。

    用途是界面上的「查看提示词」：用户能看到自己写的人设与设定
    到底以什么形式发给了模型，这是排查"为什么模型不按设定说话"的唯一办法。

    ★ 3.9 起必须走 **engine.build_session_prompt**，与真正发请求时用的是同一条路径：
      否则预览会显示"注入了全部世界书条目"，而实际只注入命中的那几条 ——
      用户会拿着这份不一致的预览去排查一个根本不存在的问题。
    """
    card, book = engine.load_card_and_book(db, session)
    history = engine.load_history(db, session.id)
    # ★ 被「剧情总结」覆盖的那一块**不会**逐条发给模型（它们由前情提要代表），
    #   所以预览也必须把它们去掉 —— 否则预览里能看到、真实请求里没有，
    #   又是一次"预览与实际不一致"。
    history = summary_mod.filter_uncovered(session, history)
    # ★ 预设也要一起带上：不带的话预览会显示"内置装配"，
    #   而真正发请求时用的是预设 —— 又是一次"预览与实际不一致"。
    preset_row, preset_config = engine.resolve_preset(db, session)
    built = engine.build_session_prompt(
        user_id=session.user_id,
        session_id=session.id,
        card=card,
        book=book,
        history=history,
        preset=preset_row,
        preset_config=preset_config,
        user_name=engine.current_user_name(db, session.user_id),
        # ★ 状态也必须出现在预览里：否则"预览没有状态、实际发了状态"，
        #   又是一次预览与实际不一致（本项目对这条零容忍）。
        #   纯聊天没有状态协议；卡没定义状态栏时 render_for_prompt 返回空串
        #   （预览里也就不该出现一套它没用过的字段）。
        state_block=(
            "" if session_service.is_pure_chat(session) else state_mod.render_for_prompt(session)
        ),
        state_schema=state_mod.effective_schema(session),
        # ★ 锚点也要出现在预览里，否则"预览没有锚点、实际发了锚点"又是一次不一致
        anchors_block=(
            "" if session_service.is_pure_chat(session) else anchors_mod.render_block(session)
        ),
        # ★ 翻译中间件的输出语言要求也要出现在预览里（否则"预览没有、实际发了"）
        translate_hint=translate_mod.prompt_hint(translate_mod.load_settings(session)),
        # ★ 插件同理：正则替换 / 提示词注入只作用于"发给模型的内容"，
        #   预览必须看到同样的结果，否则用户会以为插件没生效（或反过来）。
        plugins=plugin_service.load_enabled(db, session.user_id),
        pure_chat=session_service.is_pure_chat(session),
    )
    info = PromptInfo.model_validate(built.prompt.to_dict())
    # ★ 前情提要必须出现在预览里（它是真实请求的一部分，而且是最容易被忽略的一部分）
    coverage = session_service.summary_coverage(session)
    if coverage and (session.rolling_summary or "").strip():
        info.warnings.append(
            f"本轮已启用「前情提要」（覆盖第 {coverage['from_round']}~"
            f"{coverage['to_round']} 轮）：被覆盖的消息不会逐条发送，"
            "它们的全文在「设置」对话框里可以看到。"
        )
    return info


@router.get(
    "/sessions/{session_id}",
    summary="查看会话详情",
    response_model=ApiResponse[SessionDetail],
)
def get_session(
    session_id: int,
    db: DbSession,
    current_user: CurrentUser,
    message_limit: int = Query(
        default=session_service.DEFAULT_MESSAGE_LIMIT,
        ge=1,
        le=session_service.MAX_MESSAGE_LIMIT,
        description="返回最近多少条消息（长会话不必一次全取）",
    ),
    with_prompt: bool = Query(default=True, description="是否附带提示词预览"),
) -> ApiResponse[SessionDetail]:
    """查看会话详情（含消息历史，默认最近 200 条）。

    别人的会话一律 404（会话是纯私密内容，没有"公开"这一说）。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    messages, total, truncated = session_service.get_messages(
        db, session_id, limit=message_limit
    )
    detail = session_service.serialize_detail(
        db,
        session,
        messages=messages,
        messages_total=total,
        messages_truncated=truncated,
        prompt=_prompt_preview(db, session) if with_prompt else None,
    )
    return ApiResponse.ok(detail)


@router.patch(
    "/sessions/{session_id}",
    summary="修改会话",
    response_model=ApiResponse[SessionDetail],
)
def update_session(
    session_id: int,
    payload: SessionUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[SessionDetail]:
    """重命名 / 归档 / 换角色卡 / 换模型配置（PATCH 语义）。

    ★ 换角色卡只影响**之后**的提示词，已经落库的历史消息不会变 ——
      否则用户换个角色，整段对话的语气会断成两截。
    """
    session = session_service.update_session(db, current_user.id, session_id, payload)
    messages, total, truncated = session_service.get_messages(db, session_id)
    detail = session_service.serialize_detail(
        db,
        session,
        messages=messages,
        messages_total=total,
        messages_truncated=truncated,
        prompt=_prompt_preview(db, session),
    )
    return ApiResponse.ok(detail, message="会话已更新")


@router.delete(
    "/sessions/{session_id}",
    summary="删除会话",
    response_model=ApiResponse[dict],
)
def delete_session(
    session_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[dict]:
    """删除会话，连同它的全部消息一起删（外键 CASCADE）。

    ★ 3.9 起还会一并清掉这个会话在向量库里的**长期记忆**，
      否则会留下检索得到、却在对话记录里找不到的"幽灵记忆"。
    """
    deleted_messages = session_service.delete_session(db, current_user.id, session_id)
    return ApiResponse.ok(
        {"session_id": session_id, "deleted_messages": deleted_messages},
        message=f"会话已删除（连同 {deleted_messages} 条消息及其长期记忆）",
    )


# ==================================================================
#  长期记忆（3.9）
# ==================================================================
def _recall_or_raise(result) -> None:
    """用户在记忆面板里的主动操作，失败必须报错（不能像对话那样悄悄降级）。"""
    if getattr(result, "error", None):
        raise MemoryStoreError(
            "长期记忆检索失败（向量库或嵌入后端不可用）",
            detail={"reason": result.error},
        )


def _memory_list_out(session, result, query: str) -> MemoryListOut:
    hits = [
        MemoryHitOut(
            memory_id=hit.memory_id,
            text=hit.text,
            similarity=round(hit.similarity, 4),
            kind=(hit.metadata or {}).get("kind"),
        )
        for hit in result.hits
    ]
    return MemoryListOut(query=query, hits=hits, total=len(hits))


@router.get(
    "/sessions/{session_id}/memories",
    summary="查看这个会话的长期记忆",
    response_model=ApiResponse[MemoryListOut],
)
def list_memories(
    session_id: int,
    db: DbSession,
    current_user: CurrentUser,
    q: str = Query(default="", max_length=1000, description="检索词；留空则随便看看"),
    limit: int = Query(default=20, ge=1, le=50),
) -> ApiResponse[MemoryListOut]:
    """检索这个会话的长期记忆。

    ★ 不设相似度门槛：用户自己翻记忆时，宁可多看到几条，
      也不该出现"我明明记得聊过，却什么都搜不到"的困惑。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    result = memory.list_memories(
        user_id=current_user.id, session_id=session.id, query=q, top_k=limit
    )
    _recall_or_raise(result)
    return ApiResponse.ok(
        _memory_list_out(session, result, q),
        message=f"找到 {len(result.hits)} 条记忆",
    )


@router.post(
    "/sessions/{session_id}/memories",
    status_code=status.HTTP_201_CREATED,
    summary="记住一条内容",
    response_model=ApiResponse[MemoryListOut],
)
def add_memory(
    session_id: int,
    payload: MemoryFactCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[MemoryListOut]:
    """手动把一段内容存成长期记忆（`kind=fact`）。

    用途：剧情里定下来的事（"我们约定在旅店后门见面"）值得被长期记住，
    但对话本身可能很久以后才再被提到 —— 手动记一条，之后按语义就能召回。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    error = memory.remember_fact(current_user.id, session_id=session.id, text=payload.text)
    if error:
        raise MemoryStoreError("长期记忆写入失败（向量库或嵌入后端不可用）", detail={"reason": error})

    result = memory.list_memories(
        user_id=current_user.id, session_id=session.id, query=payload.text, top_k=20
    )
    _recall_or_raise(result)
    return ApiResponse.ok(
        _memory_list_out(session, result, payload.text), message="已记住这条内容"
    )


@router.delete(
    "/sessions/{session_id}/memories",
    summary="清空这个会话的长期记忆",
    response_model=ApiResponse[dict],
)
def clear_session_memories(
    session_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[dict]:
    """只清掉**这个会话**的长期记忆，别的会话一条都不动。

    ★ 为什么要按会话清（用户真实反馈）：一个人会同时开好几张卡/好几条线，
      "清空全部"会把别的故事的回忆一起抹掉 —— 多开体验直接废掉。
      想一次清干净仍然可以用 `DELETE /memories`（界面上的"全部会话"入口）。
    ★ 和"清空全部"一样，它**不删任何对话记录**，只是让模型忘掉回忆。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    error = memory.forget_session(current_user.id, session.id)
    if error:
        raise MemoryStoreError(
            "清空本会话长期记忆失败（向量库不可用）", detail={"reason": error}
        )
    logger.info(
        "清空本会话长期记忆 | user_id={} session_id={}", current_user.id, session.id
    )
    return ApiResponse.ok(
        {"session_id": session.id}, message="已清空本会话的长期记忆（对话记录不受影响）"
    )


@router.delete(
    "/memories",
    summary="清空我的全部长期记忆",
    response_model=ApiResponse[dict],
)
def clear_memories(
    db: DbSession, current_user: CurrentUser
) -> ApiResponse[dict]:
    """清空当前用户的**全部**长期记忆（所有会话一起清）。

    ★ 这是不可逆操作，前端必须二次确认；接口这里只做"确实要清"的语义校验。
      注意它**不会**删除任何对话记录 —— 只是让模型"忘掉"这些回忆。
    """
    error = memory.forget_all(current_user.id)
    if error:
        raise MemoryStoreError("清空长期记忆失败（向量库不可用）", detail={"reason": error})
    logger.info("清空长期记忆 | user_id={}", current_user.id)
    return ApiResponse.ok(
        {"user_id": current_user.id}, message="已清空全部长期记忆（对话记录不受影响）"
    )


# ==================================================================
#  自动翻译中间件
# ==================================================================
def _translate_panel(db, session) -> dict:
    """「翻译」面板要的全部信息（设置 / 可选项 / 已译条数 / 花掉的 token）。"""
    messages = engine.load_history(db, session.id)
    state = session_service.translate_state(db, session, messages)
    state["session_provider_id"] = session.llm_provider_id
    return state


# ==================================================================
#  记忆总结（「记忆管理面板」）
# ==================================================================
def _summary_panel(db, session) -> dict:
    """面板要的全部信息（设置 / 正文 / 覆盖 / 提醒 / 历史 / 可选项）。"""
    messages = engine.load_history(db, session.id)
    state = summary_mod.snapshot(session, messages)
    # 总结模型的可选项：用户自己的模型配置（+ "跟随会话模型"）
    providers = [
        {"id": row.id, "name": row.name, "model_name": row.model_name}
        for row in db.scalars(
            select_providers(session.user_id)
        )
    ]
    state["providers"] = providers
    state["session_provider_id"] = session.llm_provider_id
    # ★ 记忆锚点（用户手写的硬设定）：面板上显示 0/5 条 · 0/2000 字
    state["anchors"] = anchors_mod.state(session)
    return state


def select_providers(user_id: int):
    """该用户启用中的模型配置（给面板挑"总结用哪个模型"）。"""
    from sqlalchemy import select as _select

    from app.db.models import LLMProvider

    return (
        _select(LLMProvider)
        .where(LLMProvider.user_id == user_id, LLMProvider.is_active.is_(True))
        .order_by(LLMProvider.id)
    )


@router.get(
    "/sessions/{session_id}/memory-summary",
    summary="记忆总结面板（设置 + 正文 + 提醒状态）",
    response_model=ApiResponse[dict],
)
def get_memory_summary(
    session_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[dict]:
    """读「记忆管理面板」的全部状态（纯读，不触发任何模型调用）。"""
    session = session_service.get_owned_session(db, current_user.id, session_id)
    return ApiResponse.ok(_summary_panel(db, session))


@router.patch(
    "/sessions/{session_id}/memory-summary",
    summary="保存记忆总结设置 / 编辑总结正文",
    response_model=ApiResponse[dict],
)
def update_memory_summary(
    session_id: int,
    payload: SummarySettingsUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[dict]:
    """保存面板上的设置；`content` 非空时顺便把总结正文改成它。

    ★ 编辑走的是同一条路：正文会被重新盖上覆盖表头（用户不必自己写"（第 1~8 轮）"）。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    submitted = payload.model_fields_set
    notes: list[str] = []

    if "content" in submitted and payload.content is not None:
        ok, message = summary_mod.edit_summary(session, payload.content)
        if not ok:
            raise BadRequestError(message)
        notes.append(message)

    if submitted - {"content"}:
        settings = summary_mod.load_settings(session)
        patch = payload.model_dump(exclude_unset=True, exclude={"content"})
        settings.update({k: v for k, v in patch.items()})
        settings = summary_mod.save_settings(session, settings)
        notes.append(
            "设置已保存："
            + ("已开启" if settings["enabled"] else "已关闭")
            + f" / {'自动总结' if settings['auto'] else '仅提醒'}"
            + f" / 每 {settings['rounds']} 轮"
            + f" / {summary_mod.MODE_LABELS.get(settings['mode'], settings['mode'])}"
        )

    db.commit()
    db.refresh(session)
    logger.info(
        "记忆总结设置已更新 | user_id={} session_id={} fields={}",
        current_user.id,
        session.id,
        sorted(submitted),
    )
    return ApiResponse.ok(_summary_panel(db, session), message="；".join(notes) or "已保存")


@router.get(
    "/sessions/{session_id}/translate",
    summary="翻译中间件的设置与统计",
    response_model=ApiResponse[dict],
)
def get_translate(session_id: int, db: DbSession, current_user: CurrentUser) -> ApiResponse[dict]:
    """「翻译」面板要的状态（**不触发任何模型调用**）。

    ★ 把"这一轮大概要花多少 token"如实算出来给用户看：默认关闭，
      开 `middleware` 才会真的多一次调用（与记忆总结同一条规矩）。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    return ApiResponse.ok(_translate_panel(db, session))


@router.patch(
    "/sessions/{session_id}/translate",
    summary="保存翻译中间件设置",
    response_model=ApiResponse[dict],
)
def update_translate(
    session_id: int,
    payload: TranslateSettingsUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[dict]:
    """只提交要改的字段（PATCH 语义），与记忆面板一致。"""
    session = session_service.get_owned_session(db, current_user.id, session_id)
    submitted = payload.model_fields_set
    settings = translate_mod.load_settings(session)
    if submitted:
        patch = payload.model_dump(exclude_unset=True)
        settings.update({k: v for k, v in patch.items()})
        # ★ 合并之后再归一化一次：关掉总开关时模式要归零、非法值要退回默认，
        #   否则界面会显示"中间件翻译"却什么都没发生。
        settings = translate_mod.normalize_settings(settings)
        translate_mod.save_settings(session, settings)
    db.commit()
    db.refresh(session)
    logger.info(
        "翻译中间件设置已更新 | user_id={} session_id={} fields={}",
        current_user.id,
        session.id,
        sorted(submitted),
    )
    return ApiResponse.ok(
        _translate_panel(db, session),
        message="设置已保存：" + translate_mod.describe(settings),
    )


@router.post(
    "/sessions/{session_id}/messages/{message_id}/translate",
    summary="按需翻译单条消息（开场白也可以，不看总开关）",
    response_model=ApiResponse[dict],
)
def translate_message(
    session_id: int,
    message_id: int,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[dict]:
    """手动翻译**这一条**消息（含开场白与你自己说的话）。

    ★ 为什么需要一个"不看总开关"的入口：用户可能忘了开翻译中间件、
      或者只想看其中一条的译文 —— 让他为一条消息去改全局设置再关掉，太重了。
      点这一下 = 用户同意花这一次调用（与「立即总结」同一个哲学）。
    ★ 已有译文的消息不会重译（避免冲掉输入侧"原文=用户原话"的关系），
      跳过原因都会写在返回的 message 里，绝不静默无操作。
    """
    from app.narrative import engine as engine_mod
    from app.narrative import sessions as sessions_mod

    session = session_service.get_owned_session(db, current_user.id, session_id)
    message = sessions_mod.get_owned_message(db, session, message_id)
    _outcome, note = engine_mod.translate_one(db, session, message)
    return ApiResponse.ok(
        {"message": sessions_mod.serialize_message(message)},
        message=note,
    )


@router.put(
    "/sessions/{session_id}/memory-summary/anchors",
    summary="保存记忆锚点（整份替换，最多 5 条 / 2000 字）",
    response_model=ApiResponse[dict],
)
def put_memory_anchors(
    session_id: int,
    payload: MemoryAnchorsUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[dict]:
    """整份替换这条会话的锚点。

    ★ 为什么是"整份替换"而不是增删改三条接口：面板本身就是"最多 5 条的清单"，
      整份提交最不容易出现"前端以为删掉了、后端还留着"的不一致。
    ★ 超限**拒绝**而不是截断：用户写了 6 条就该看到"最多 5 条"，
      而不是存进去 5 条、下次打开发现少一条。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    ok, message = anchors_mod.save(session, payload.anchors)
    if not ok:
        raise BadRequestError(message)
    db.commit()
    db.refresh(session)
    return ApiResponse.ok(_summary_panel(db, session), message=message)


@router.post(
    "/sessions/{session_id}/memory-summary/run",
    summary="立即总结（用户手动触发，会消耗一次模型调用）",
    response_model=ApiResponse[dict],
)
def run_memory_summary(
    session_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[dict]:
    """用户点了「立即总结」：有几轮就总结几轮（不要求攒够一整块）。

    ★ 这是**唯一**会花 token 的入口（自动总结除外），而且只由用户点击触发 ——
      用户明确要求"总结与否自己决定、不强制"。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    messages = engine.load_history(db, session.id)
    settings = summary_mod.load_settings(session)
    if not settings.get("enabled"):
        raise BadRequestError("记忆总结已关闭（先在面板里打开）")

    provider_row = engine.require_provider(db, session)
    adapter = engine.build_adapter(provider_row)
    if settings.get("provider_id") and int(settings["provider_id"]) != int(provider_row.id):
        adapter = engine._build_summary_adapter(db, session, int(settings["provider_id"]), adapter)

    try:
        outcome = summary_mod.merge_block(
            session=session,
            adapter=adapter,
            messages=messages,
            db=db,
            settings=settings,
            force=True,
        )
    finally:
        try:
            adapter.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结果
            pass

    if outcome.merged:
        db.commit()
        db.refresh(session)
    elif outcome.reason == summary_mod.BUSY_REASON:
        # ★ 409：同一会话的总结是**串行**的。用户连点几下时，第一次在跑、
        #   后面几次会走到这里 —— 必须明确告诉他"正在总结中"，
        #   而不是静默再跑一遍（那会白花好几份 token，真实发生过）。
        raise ConflictError(summary_mod.BUSY_REASON)
    panel = _summary_panel(db, session)
    return ApiResponse.ok(
        panel,
        message=(
            f"已总结第 {outcome.from_round}~{outcome.to_round} 轮"
            + ("（照抄模式，未调用模型）" if not outcome.used_model and not outcome.warning else "")
            if outcome.merged
            else f"没有可总结的内容：{outcome.reason}"
        ),
    )


@router.post(
    "/sessions/{session_id}/memory-summary/restore",
    summary="恢复上一次的总结",
    response_model=ApiResponse[dict],
)
def restore_memory_summary(
    session_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[dict]:
    """把最近一个历史版本换回来（再把当前版本压回历史，所以可以来回切）。"""
    session = session_service.get_owned_session(db, current_user.id, session_id)
    ok, message = summary_mod.restore_previous(session)
    if not ok:
        raise BadRequestError(message)
    db.commit()
    db.refresh(session)
    return ApiResponse.ok(_summary_panel(db, session), message=message)


# ==================================================================
#  结构化状态（HP / 背包 / 位置 / 任务）：手动纠正
# ==================================================================
@router.patch(
    "/sessions/{session_id}/state",
    summary="手动纠正这个会话的状态",
    response_model=ApiResponse[dict],
)
def update_session_state(
    session_id: int,
    payload: SessionStateUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[dict]:
    """用手写的状态覆盖当前状态（模型写错时的补救入口）。

    ★ 校验规则与模型自己输出时**同一套**（`state_mod.normalize`）：
      也就是说手动改也要受"HP 越界夹住 / 上限跳变拒绝 / 脏字段丢弃"的约束，
      不会因为"是人写的"就绕过校验 —— 否则前端一存脏数据，下一轮提示词就废了。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    previous = state_mod.load_state(session)
    schema = state_mod.effective_schema(session)
    # 手动纠正按"完整替换"处理，但缺字段仍然沿用上一轮（与模型输出同一套合并语义）
    # ★ schema 也要带上：字段清单由卡/世界书定义，手动改同样只能改这张卡声明过的字段。
    state, notes = state_mod.normalize(payload.state, previous, schema)
    state = state_mod.ensure_shape(state, schema)
    state_mod.save_state(session, state)
    db.commit()
    logger.info(
        "手动纠正状态 | user_id={} session_id={} fields={}",
        current_user.id,
        session.id,
        sorted(payload.state.keys()),
    )
    return ApiResponse.ok(
        {"session_id": session.id, "state": state},
        message=("；".join(notes) if notes else "状态已更新"),
    )


# ==================================================================
#  发消息（非流式）
# ==================================================================
@router.post(
    "/sessions/{session_id}/messages",
    status_code=status.HTTP_201_CREATED,
    summary="发一条消息（非流式）",
    response_model=ApiResponse[SendResult],
)
def send_message(
    session_id: int,
    payload: MessageCreate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[SendResult]:
    """发一条用户消息，等模型生成完整回复后一起返回。

    ★ 用户消息在**调模型之前**就已经落库了：模型调用失败（限流/超时/余额不足）
      时用户打的那段话不会跟着消失 —— 这与聊天软件的直觉一致。
      失败时会返回 4xx/502，但消息还在，刷新页面能看到。

    想要"边生成边显示"的打字机效果，请用 `GET …/stream`。
    """
    session = session_service.get_owned_session(db, current_user.id, session_id)
    user_message = engine.save_user_message(db, session, payload.content)
    assistant, result, turn = engine.reply(db, session, user_message)

    return ApiResponse.ok(
        SendResult(
            user_message=session_service.serialize_message(user_message),
            assistant_message=session_service.serialize_message(assistant),
            usage=result.usage.to_dict(),
            notes=list(result.notes),
            prompt=PromptInfo.model_validate(turn.prompt.to_dict()),
            context=SessionContextInfo.model_validate(turn.context.to_dict()),
            latency_ms=result.latency_ms,
        ),
        message="回复已生成",
    )


# ==================================================================
#  发消息（SSE 流式）★ 本模块唯一的 async def
# ==================================================================
#: 流式回复的两种模式：
#:   send        用户刚打了一句话（已在流开始前落库）→ 直接生成回复
#:   regenerate  重新生成 → 生成前要先删掉上一次的回复（含失败留下的半截）
STREAM_SEND = "send"
STREAM_REGENERATE = "regenerate"


def _sse_stream_response(
    *,
    session_id: int,
    user_message_id: int,
    mode: str,
    request_label: str,
    delete_inclusive: bool = False,
    delete_from_id: int | None = None,
) -> StreamingResponse:
    """把「同步生成器 → SSE 报文」这段架子抽出来，供发消息与重新生成共用。

    ★ 为什么要把用户消息 id 当成参数传进来，而不是在这里落库？
      落库必须在**流开始之前**完成（失败要能返回 JSON 404/400，而不是塞进流里），
      所以调用方先在线程池里做完校验与落库，这里只负责推流。

    ★ `user_message_id` 的含义是「拿哪条消息当**本次提问**喂给模型」，
      它必须是 user 消息 —— 这个不变量以前是隐含的，结果踩了坑（见下）。

    ★ `delete_from_id`：从哪条消息开始删。默认与 user_message_id 相同。
      为什么需要它单独存在（真实 bug）：
        「重新生成某条**角色回复**」时，待删除的是那条 **assistant** 回复，
        而用来提问的是它**前面那条 user 消息** —— 两者不是同一条。
        老代码把 assistant 回复的 id 同时当成"删除锚点"和"提问内容"，
        于是把一条**刚从库里删掉的角色回复**当作"用户说的话"喂给了模型，
        生成结果自然是空的 / 报错（前端表现为"点重新生成没反应"）。

    ★ delete_inclusive 只对 regenerate 有意义：
      True  → 连 delete_from_id 这一条也删掉（用户点了某条**中间**的回复）
      False → 只删它之后的内容（用最后一条回复重新生成时，那条回复正是待替换的）

    两者必须区分开：写测试时就是因为没区分，
    导致"重新生成最后一条"把用户那句话一起删了（断言当场抓到）。

    ★ 生成器里为什么必须用 session_scope() 自己开 session？
      响应开始后，请求级依赖 db 的生命周期不再可靠（详见文件顶部说明）。
    """

    async def event_stream() -> AsyncIterator[str]:
        # 告诉浏览器自动重连的等待时间（本项目不依赖自动重连，写上无害）
        yield "retry: 3000\n\n"

        with session_scope() as stream_db:
            session = stream_db.get(NarrativeSession, session_id)
            if session is None:
                # 极端情况：用户在流开始前刚好把会话删了
                yield sse_event(
                    "error",
                    {
                        "code": "NOT_FOUND",
                        "message": "会话不存在",
                        "status": 404,
                        "detail": {"session_id": session_id},
                        "request_id": get_request_id(),
                    },
                )
                return

            user_message = stream_db.get(Message, user_message_id)
            if user_message is None:
                yield sse_event(
                    "error",
                    {
                        "code": "INTERNAL_ERROR",
                        "message": "刚保存的用户消息找不到了",
                        "status": 500,
                        "detail": {"message_id": user_message_id},
                        "request_id": get_request_id(),
                    },
                )
                return

            if mode == STREAM_REGENERATE:
                # ★ 重新生成：先把被替换的那条回复**及其之后的内容**删掉，再生成。
                #
                #   为什么连它自己一起删（inclusive=True）？
                #     不删它的话，新回复会追加在它后面 —— 于是历史里出现
                #     "两条针对同一句话的回复"，模型下次会以为自己在自言自语。
                #     （这个 bug 是写测试时抓到的，不是推理出来的。）
                #
                #   为什么删除放在流里、而不是流开始前？
                #     放前面就会"删了但生成失败"，用户连旧回复都没了；
                #     放这里至少保证删完立刻接着生成，失败也能再点一次。
                removed = session_service.delete_messages_from(
                    stream_db,
                    session,
                    after_message_id=(
                        delete_from_id if delete_from_id is not None else user_message.id
                    ),
                    inclusive=delete_inclusive,
                )
                if removed:
                    logger.info(
                        "重新生成：已清除被替换的 {} 条内容 | session_id={}",
                        removed,
                        session_id,
                    )

            generator = engine.stream_reply(stream_db, session, user_message)

            while True:
                # ★ 同步生成器必须在**线程池**里推进：
                #   直接 next() 会把"等模型吐字"的这段时间用在事件循环上，
                #   整个服务在此期间无法处理任何其它请求。
                step = await run_in_threadpool(_next_or_none, generator)
                if step is None:
                    break
                event, data = step
                try:
                    yield sse_event(event, data)
                except BaseException as exc:  # noqa: BLE001
                    # ★ 前端断开（关页面 / 点停止）：主动通知同步生成器收尾，
                    #   否则它会继续把整段回复生成完、白花钱。
                    logger.info(
                        "SSE 连接中断，停止生成 | {} | session_id={} | {}",
                        request_label,
                        session_id,
                        exc,
                    )
                    _close_generator(generator)
                    return
                if event == "error":
                    # 错误已经如实发出去了，这里结束流（生成器也已被关闭）
                    break

        logger.info("SSE 流结束 | {} | session_id={}", request_label, session_id)
        # 告诉浏览器/客户端这条流正常结束了（EventSource 会识别它）
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx 等反向代理默认会缓冲响应，这一行让它们把 SSE 放行
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/sessions/{session_id}/stream",
    summary="发一条消息（SSE 流式）",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "SSE 事件流。事件类型：\n"
                "- meta  元信息（模型名、提示词来源、上下文裁剪情况）\n"
                "- notes 适配层为满足协议所做的参数调整\n"
                "- reason 推理模型的思考过程增量\n"
                "- delta 正文增量\n"
                "- done  收尾（用量、消息 id、耗时）\n"
                "- error 出错（★ 一定会发，不会静默断开）"
            ),
        }
    },
)
async def stream_message(
    request: Request,
    session_id: int,
    db: DbSession,
    current_user: CurrentUser,
    content: str = Query(..., min_length=1, max_length=20000, description="用户说的话"),
) -> StreamingResponse:
    """流式对话（打字机效果）。

    ==================== 三层结构，各司其职 ====================
      1. **认证 + 会话校验**（同步，线程池里完成）
         失败直接返回 JSON 404/400 —— 此时还没开始流式输出，
         前端能像普通请求一样处理错误，比在流里塞错误好得多。
      2. **用户消息落库**（同步，线程池）
         同样在流开始前完成。
      3. **SSE 生成器**（async）
         用 run_in_threadpool 逐步驱动同步生成器 engine.stream_reply，
         每个片段转成一条 SSE 报文发出去。

    ★ 为什么校验要放在前面而不是生成器里？
      一旦开始 yield，HTTP 响应头就已经发出去了（200），
      之后再想报 404 只能塞进流里，浏览器/前端处理起来麻烦得多。
      能提前失败的一定提前失败。
    """

    # ---------- 第 1、2 步：都在线程池里跑，绝不阻塞事件循环 ----------
    def _prepare() -> int:
        """完成全部校验与落库，只返回用户消息 id。

        ★ 为什么只返回一个 int？
          run_in_threadpool 的每次调用都要跨线程传数据，而 SQLAlchemy 的
          ORM 对象**不能跨线程安全使用**（session 绑定在线程上）。
          所以这里只返回纯数字，生成器里再用自己的 session 重新取数据。
        """
        session = session_service.get_owned_session(db, current_user.id, session_id)
        user_message = engine.save_user_message(db, session, content)
        return user_message.id

    user_message_id = await run_in_threadpool(_prepare)

    return _sse_stream_response(
        session_id=session_id,
        user_message_id=user_message_id,
        mode=STREAM_SEND,
        request_label="发送",
    )


@router.get(
    "/sessions/{session_id}/regenerate",
    summary="重新生成一条回复（SSE 流式）",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "事件类型与 …/stream 完全一致（meta / notes / reason / delta / done / error）",
        },
        400: {"description": "这个会话里还没有你说过的话，没有可重新生成的内容"},
    },
)
async def regenerate_message(
    session_id: int,
    db: DbSession,
    current_user: CurrentUser,
    message_id: int | None = Query(
        default=None,
        description="重新生成哪一条回复。不传则用最后一条（中间那条会连带删掉它之后的全部内容）",
    ),
) -> StreamingResponse:
    """重新生成：删掉选中的那条回复，用**同一句用户消息**再生成一次。

    ★ 与「重新发一遍」的区别：这里不会多出一条用户消息。
      如果用「撤回 + 重发」来实现，历史里会留下两条一模一样的用户发言，
      模型会以为自己被问了两遍。

    ★ 传 message_id 时指的是**assistant 回复**那条；它**自己以及之后的内容**
      都会被删掉再重写（后面的剧情是接着它展开的，留着会前后矛盾）。
      不传时退回到"最后一条回复"。
    """

    def _prepare() -> tuple[int, int | None]:
        """返回 (用来提问的用户消息 id, 删除起点 id | None)。

        ★ 为什么要分成两个数：
          用户点的是**角色回复**，要删的也是那条回复；
          但这次生成该"接着哪句话往下写"，靠的是它前面那条**用户消息**。
          老代码只有一个变量，两种含义混在了一起。
        """
        session = session_service.get_owned_session(db, current_user.id, session_id)
        if message_id is not None:
            target = session_service.get_owned_message(db, session, message_id)
            if target.role != "assistant":
                raise BadRequestError(
                    "「重新生成」只能用在角色的回复上（你说的话请用「撤回」或「编辑」）",
                    detail={"message_id": message_id, "role": target.role},
                )
            # ★ 提问锚点必须是**这条回复之前**的用户消息，不能是这条回复自己
            anchor = session_service.find_previous_user_message(db, session.id, target.id)
            if anchor is None:
                raise BadRequestError(
                    "这条回复之前找不到你说过的话，无法据此重新生成",
                    detail={"message_id": message_id, "suggestion": "请用「撤回」重发这一轮"},
                )
            # 用户点的是这一条回复 → 它自己也要被替换掉（从它开始删）
            return anchor.id, target.id

        anchor = session_service.find_last_user_message(db, session.id)
        if anchor is None:
            raise BadRequestError(
                "这个会话里还没有你说过的话，没有可重新生成的内容",
                detail={"session_id": session_id, "suggestion": "先发一句话再试"},
            )
        # ★ 没指定时锚点是**用户消息**，删的是它之后的回复 —— 用户消息本身必须留着
        return anchor.id, None

    prompt_id, delete_from_id = await run_in_threadpool(_prepare)

    return _sse_stream_response(
        session_id=session_id,
        user_message_id=prompt_id,
        mode=STREAM_REGENERATE,
        request_label="重新生成",
        delete_inclusive=delete_from_id is not None,
        delete_from_id=delete_from_id,
    )


@router.post(
    "/sessions/{session_id}/messages/{message_id}/retract",
    summary="撤回（删掉这条你说的话及之后的内容）",
    response_model=ApiResponse[RetractResult],
)
def retract_message(
    session_id: int,
    message_id: int,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[RetractResult]:
    """撤回一条用户消息。

    ★ 会**连带删除**这条消息之后的全部消息（包括角色的回复）——
      那些回复是基于原来的问题生成的，留着会前后矛盾。
      返回体里的 `deleted_messages` 就是删了几条，界面必须先确认再执行。
    ★ 只能撤回 user 消息；assistant 是模型产物，请用「重新生成」。
    """
    result = session_service.retract_from_message(
        db, current_user.id, session_id, message_id
    )
    return ApiResponse.ok(
        RetractResult(**result),
        message=f"已撤回（连同 {result['deleted_messages']} 条消息）",
    )


@router.patch(
    "/sessions/{session_id}/messages/{message_id}",
    summary="编辑你说过的一句话（会删掉它之后的内容）",
    response_model=ApiResponse[MessageEditResult],
)
def edit_message(
    session_id: int,
    message_id: int,
    payload: MessageUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[MessageEditResult]:
    """改写一条用户消息。

    ★ 会**连带删除**这条消息之后的全部消息（包括角色回复）——
      因为那些回复是基于原来的问题生成的，留着会前后矛盾。
      返回体里的 `deleted_messages` 就是删了几条，界面必须显示出来。
    ★ 只能改 user 消息；assistant 是模型产物，请用「重新生成」。
    """
    result = session_service.update_user_message(
        db, current_user.id, session_id, message_id, payload.content
    )
    deleted = result["deleted_messages"]
    return ApiResponse.ok(
        MessageEditResult(**result),
        message=(
            f"已更新（并删除其后的 {deleted} 条消息）" if deleted else "已更新"
        ),
    )


#: 生成器推进的辅助函数放在模块级，方便 run_in_threadpool 直接引用
def _next_or_none(generator) -> tuple[str, object] | None:
    """推进一步同步生成器；StopIteration 时返回 None。

    ★ 不能直接在 run_in_threadpool 里传 `lambda: next(gen, None)`：
      StopIteration 是一个"控制流异常"，跨线程传递时容易被包装成
      难以理解的错误。用一个普通函数把它翻译成 None 最稳。

    ★ 适配层抛出的语义化异常（限流 / 鉴权失败 / 超时…）在这里被翻译成
      一条 error 事件，而不是让整个响应 500 —— 前端已经收到了 200 与若干片段，
      此时唯一正确的做法就是把错误**如实发下去**（红线：不许静默断开）。
    """
    try:
        return next(generator)
    except StopIteration:
        return None
    except BaseException as exc:  # noqa: BLE001
        payload = error_event_payload(exc)
        # 生成器已经因为异常退出了，这里顺手关掉它（触发 finally 里的 close()）
        _close_generator(generator)
        return "error", payload


def _close_generator(generator) -> None:
    """让同步生成器走 finally（关闭 HTTP 连接池、保存半截内容）。"""
    close = getattr(generator, "close", None)
    if callable(close):
        try:
            close()
        except BaseException as exc:  # noqa: BLE001
            logger.warning("关闭流式生成器时出错: {}", exc)
