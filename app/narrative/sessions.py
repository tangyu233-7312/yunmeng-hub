"""叙事会话的增删改查与序列化。

==================== 越权规则（与本项目其它模块故意不同）====================
    会话：别人的一律 **404**（连"这个 id 存在"都不透露）

会话里装着用户与角色之间的私人对话，没有"公开"这个概念，
所以不存在角色卡那种"能看不能改"的 403 中间态，直接用 404 最干净。

==================== 为什么 message_count 不每次 count(*) ====================
narrative_sessions.message_count 是**冗余字段**，在写消息时一起维护。
会话列表每行都要显示消息数，如果按行去 count(*)，20 个会话就是 20 条 SQL。
代价是"统计必须与消息表保持同步"——本项目所有对 messages 的写操作
都集中在 app/narrative/engine.py 的三处（开场白、用户消息、助手回复），
这比到处 count(*) 更容易保证正确。
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import BadRequestError, NotFoundError
from app.narrative import dice as dice_mod
from app.narrative import plugin_runtime
from app.narrative import state as state_mod
from app.narrative import state_schema as state_schema_mod
from app.narrative import translate as translate_mod
from app.narrative import vn as vn_mod
from app.db.models import (
    CharacterCard,
    LLMProvider,
    Message,
    NarrativeSession,
    PromptPreset,
    User,
    WorldBook,
)
from app.schemas.narrative import (
    MessageOut,
    SessionBrief,
    SessionDetail,
)
from app.services import character_card_service, plugin_service, provider_service

#: 会话列表默认/最大分页
DEFAULT_LIMIT = 12
MAX_LIMIT = 100

#: 详情接口默认返回多少条消息（从**最新**往前取，避免超长会话把响应撑爆）
DEFAULT_MESSAGE_LIMIT = 200
MAX_MESSAGE_LIMIT = 1000

#: 列表里的最后一条消息预览截多长
PREVIEW_CHARS = 60


def now() -> datetime:
    """统一的"现在"。

    ★ 与 provider_service 保持一致：数据库时间字段用 server_default=func.now()
      （数据库所在机器的本地时间），所以这里也用本地朴素时间，
      避免同一张表里混进两种时区。
    """
    return datetime.now()


# ==================================================================
#  查询
# ==================================================================
def get_owned_session(db: Session, user_id: int, session_id: int) -> NarrativeSession:
    """取会话，且必须属于该用户；否则 404（见模块文档的越权规则）。"""
    row = db.scalar(
        select(NarrativeSession).where(
            NarrativeSession.id == session_id, NarrativeSession.user_id == user_id
        )
    )
    if row is None:
        raise NotFoundError("会话不存在", detail={"session_id": session_id})
    return row


def _last_message_map(db: Session, session_ids: list[int]) -> dict[int, Message]:
    """一次性取出每个会话的最后一条消息（避免 N+1 查询）。

    ★ 这里踩过一个 MySQL 的坑：最初用的是相关子查询
      `select(Message.id).where(...).order_by(desc).limit(1).scalar_subquery()`
      再配 `Message.id.in_(...)`，MySQL 直接报
      `1235 This version of MySQL doesn't yet support 'LIMIT & IN/ALL/ANY/SOME subquery'`。
      改成「先按会话分组取 max(id)，再按这批 id 取消息」两步走，
      在 MySQL 5.7/8.0 上都能跑，而且走的是 (session_id, id) 复合索引。
    """
    if not session_ids:
        return {}

    latest_ids = (
        select(func.max(Message.id))
        .where(Message.session_id.in_(session_ids))
        .group_by(Message.session_id)
        .subquery()
    )
    rows = db.scalars(
        select(Message).where(Message.id.in_(select(latest_ids.c[0])))
    ).all()
    return {row.session_id: row for row in rows}


def _preview(message: Message | None) -> str | None:
    if message is None:
        return None
    # ★ 翻译中间件：列表预览要显示**给人看的那一份**（否则气泡里是中文译文、
    #   左边的列表却是一行外语，界面自相矛盾）。
    #   不变量：`translation.text` 就是"给人看的文本"（输出侧=译文，输入侧=用户原话），
    #   `display` 只是把这个规则显式写出来（`display=content` 时才用 content）。
    data = translate_mod.loads(getattr(message, "translation_json", None))
    text = message.content or ""
    if data and data.get("text") and data.get("display") != "content":
        text = str(data["text"])
    # 换行会让列表行高乱跳，压成单行再截断
    flat = " ".join(text.split())
    return flat[:PREVIEW_CHARS] + ("…" if len(flat) > PREVIEW_CHARS else "")


def card_reference(db: Session, card_id: int | None) -> dict | None:
    """角色卡的精简引用（只带 id/name/avatar，不带 4 个 MEDIUMTEXT）。"""
    if not card_id:
        return None
    # 用 db.get 而不是 join：卡片可能已被删除，这里只取存在的那部分
    card = db.get(CharacterCard, card_id)
    if card is None:
        return None
    return {
        "id": card.id,
        "name": card.name,
        "avatar_url": card.avatar_url,
    }


def _session_warnings(session: NarrativeSession) -> list[str]:
    """会话自身的问题（列表上就要能看出来，别等用户点进去才发现）。"""
    warnings: list[str] = []
    # ★ 只有"叙事会话"才提示"人设丢了"；纯聊天本来就没有角色卡（那是用户主动选的）
    if session.character_card_id is None and not is_pure_chat(session):
        warnings.append("关联的角色卡已被删除，对话将不再注入人设设定")
    if session.llm_provider_id is None:
        warnings.append("尚未选择模型配置（或原配置已被删除），无法继续对话")
    return warnings


def list_sessions(
    db: Session,
    user_id: int,
    *,
    status: str | None = "active",
    q: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[SessionBrief], int]:
    """分页列出会话，按最后活跃时间倒序。

    status 传 None 表示"全部"（界面上「已归档」标签页就是这么用）。
    """
    conditions = [NarrativeSession.user_id == user_id]
    if status in ("active", "archived"):
        conditions.append(NarrativeSession.status == status)
    if q:
        conditions.append(NarrativeSession.title.like(f"%{q}%"))

    total = db.scalar(
        select(func.count()).select_from(NarrativeSession).where(*conditions)
    ) or 0

    rows = list(
        db.scalars(
            select(NarrativeSession)
            .where(*conditions)
            # ★ 排序要能兜住 last_active_at 为 NULL 的会话（刚建但还没说过话），
            #   否则它们会被 MySQL 排到最后甚至随机位置
            .order_by(
                NarrativeSession.last_active_at.desc(),
                NarrativeSession.id.desc(),
            )
            .limit(limit)
            .offset(offset)
        ).all()
    )

    lasts = _last_message_map(db, [row.id for row in rows])
    provider_names = _provider_name_map(db, [row.llm_provider_id for row in rows])

    items = [
        SessionBrief(
            id=row.id,
            title=row.title,
            status=row.status,
            character_card=card_reference(db, row.character_card_id),
            provider_name=provider_names.get(row.llm_provider_id or 0),
            model_name=provider_names.get(("model", row.llm_provider_id or 0)),
            message_count=row.message_count,
            total_tokens=row.total_tokens,
            last_message_preview=_preview(lasts.get(row.id)),
            last_message_role=lasts[row.id].role if row.id in lasts else None,
            last_active_at=row.last_active_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            warnings=_session_warnings(row),
        )
        for row in rows
    ]
    return items, total


def _provider_name_map(db: Session, provider_ids: list[int | None]) -> dict:
    """批量取模型配置的别名与模型名。

    键用 (0, id) 取别名、(model, id) 取模型名，避免再多查一遍数据库 ——
    会话列表每行都要显示这两样，一次查完最省事。
    """
    ids = {pid for pid in provider_ids if pid}
    if not ids:
        return {}
    out: dict = {}
    for row in db.scalars(select(LLMProvider).where(LLMProvider.id.in_(ids))).all():
        out[row.id] = row.name
        out[("model", row.id)] = row.model_name
    return out


def get_messages(
    db: Session,
    session_id: int,
    *,
    limit: int = DEFAULT_MESSAGE_LIMIT,
) -> tuple[list[Message], int, bool]:
    """取会话的消息历史。

    ★ 取的是**最近** limit 条（而不是最早 limit 条）：
      用户打开一个几百轮的长会话时，最需要看到的是最新剧情。
      返回顺序仍然是时间升序，前端直接渲染即可。
    """
    total = db.scalar(
        select(func.count()).select_from(Message).where(Message.session_id == session_id)
    ) or 0

    rows = list(
        db.scalars(
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.id.desc())
            .limit(limit)
        ).all()
    )
    rows.reverse()
    return rows, total, total > len(rows)


def serialize_message(row: Message) -> MessageOut:
    return MessageOut(
        id=row.id,
        role=row.role,
        content=row.content,
        token_count=row.token_count or 0,
        model_name=row.model_name,
        latency_ms=row.latency_ms,
        # ★ 骰点直接读落库的那一份（不重掷）：界面刷新后点数必须还是同一个
        rolls=dice_mod.loads(getattr(row, "rolls_json", None)),
        # ★ 翻译中间件：原文/译文 + 该显示哪一份（界面据此渲染切换按钮）
        translation=translate_mod.loads(getattr(row, "translation_json", None)),
        # ★ 模型输出的 <state> 原文（界面「看作者原格式」；解析成字段后原排版就没了）
        state_raw=getattr(row, "state_raw_json", None),
        created_at=row.created_at,
    )


def is_pure_chat(session: Any) -> bool:
    """这个会话是不是**纯聊天**（无角色）。

    ★ 判据是显式的 `kind` 列，而不是"character_card_id 是不是空"：
      角色卡被删除时会话的 character_card_id 会被置空（SET NULL），
      那属于"卡没了的故事会话"，要如实提示用户人设丢了 —— 与纯聊天长得一样，
      只能靠显式字段区分（见 models/narrative.py 里 kind 的说明）。
    """
    return str(getattr(session, "kind", "story") or "story") == "chat"


def summary_coverage(session: Any) -> dict[str, int] | None:
    """这份滚动总结覆盖第几轮到第几轮（没有总结 = None）。

    ★ 单独抽出来是为了让**界面与接口口径一致**：会话详情、提示词预览
      都读这一个函数，不会出现"这边说 1~10、那边说 1~20"。
    """
    to_round = int(getattr(session, "summary_to_round", 0) or 0)
    if to_round <= 0 or not str(getattr(session, "rolling_summary", "") or "").strip():
        return None
    from_round = int(getattr(session, "summary_from_round", 0) or 0) or 1
    return {"from_round": from_round, "to_round": to_round}


def _memory_summary_state(session: Any, messages: list[Any]) -> dict[str, Any]:
    """给会话详情用的记忆总结状态（**不触发任何模型调用**）。

    ★ 为什么要放在会话详情里：横幅要在**打开会话时**就能出现，
      而"发送消息的响应"只在发完消息之后才有 —— 两者都要有。
    """
    from app.narrative import summary as summary_mod

    settings = summary_mod.load_settings(session)
    reminder = summary_mod.reminder_state(session, messages, settings)
    coverage = summary_coverage(session)
    return {
        "enabled": bool(settings.get("enabled")),
        "auto": bool(settings.get("auto")),
        "remind": bool(settings.get("remind")),
        "rounds": int(settings.get("rounds") or 0),
        "mode": settings.get("mode"),
        "mode_label": summary_mod.MODE_LABELS.get(str(settings.get("mode")), ""),
        "coverage": coverage,
        "chars": len(str(getattr(session, "rolling_summary", "") or "")),
        **reminder,
    }


def translate_state(db: Session, session: NarrativeSession, messages: list[Any]) -> dict[str, Any]:
    """给「翻译」面板用的状态（**不触发任何模型调用**）。

    ★ 与记忆面板同样的口径：把"这一轮要花多少 token"如实算出来给用户看，
      点不点由他决定（本项目对"未经同意花 token"零容忍）。
    """
    state = translate_mod.state(session)
    settings = state["settings"]
    translated = [m for m in messages if translate_mod.loads(getattr(m, "translation_json", None))]
    spent = sum(
        int((translate_mod.loads(m.translation_json) or {}).get("tokens") or 0) for m in translated
    )
    latest = None
    for row in reversed(messages):
        data = translate_mod.loads(getattr(row, "translation_json", None))
        if data:
            latest = {
                "message_id": row.id,
                "direction": data.get("direction"),
                "lang": data.get("lang"),
                "used_model": bool(data.get("used_model")),
                "error": data.get("error") or "",
            }
            break
    state.update(
        {
            "cost_tokens": translate_mod.estimate_cost(settings),
            "translated_messages": len(translated),
            "spent_tokens": spent,
            "latest": latest,
            # 可选模型配置（面板里"翻译用哪个模型"的下拉框）
            "providers": [
                {
                    "id": row.id,
                    "name": row.name,
                    "model_name": row.model_name,
                    "is_active": bool(row.is_active),
                }
                for row in db.scalars(
                    select(LLMProvider)
                    .where(LLMProvider.user_id == session.user_id)
                    .order_by(LLMProvider.id.asc())
                )
            ],
        }
    )
    return state


def serialize_detail(
    db: Session,
    session: NarrativeSession,
    *,
    messages: list[Message],
    messages_total: int,
    messages_truncated: bool,
    prompt=None,
) -> SessionDetail:
    provider = None
    if session.llm_provider_id:
        row = db.get(LLMProvider, session.llm_provider_id)
        if row is not None:
            provider = {
                "id": row.id,
                "name": row.name,
                "model_name": row.model_name,
                "provider_type": row.provider_type,
                "context_window": row.context_window,
                "max_tokens": row.max_tokens,
                "reasoning_effort": row.reasoning_effort,
                # ★ 体检台要用：用户关掉流式之后，界面得如实说明"这一轮不是流式的"
                "stream_enabled": bool(getattr(row, "stream_enabled", True)),
                "fallback_provider_id": getattr(row, "fallback_provider_id", None),
            }

    pure_chat = is_pure_chat(session)

    # ---------------- VN 舞台（角色卡立绘/背景）----------------
    # ★ 由**后端**按"状态栏里的表情字段"算好当前该显示哪张立绘：
    #   "表情 → 立绘"的规则只有一份（app/narrative/vn.py），
    #   前端只负责画，测试与探针都能直接断言，不会出现两边各写一套映射。
    #   纯聊天没有角色卡，自然也没有舞台。
    vn_stage = None
    if not pure_chat and session.character_card_id:
        vn_card = db.get(CharacterCard, session.character_card_id)
        if vn_card is not None:
            vn_stage = vn_mod.stage(vn_card, state_mod.load_state(session))
            if vn_stage and vn_stage.get("warnings"):
                for note in vn_stage["warnings"]:
                    logger.info("VN 舞台提醒 | session_id={} note={}", session.id, note)

    # ---------------- 提示词预设 ----------------
    # ★ 这里要区分两个概念，否则用户会困惑"我明明没绑，模型行为怎么变了"：
    #     prompt_preset    —— 这条会话**显式绑定**的预设（可能为空）
    #     effective_preset —— 实际**生效**的预设（会话绑定 > 全局默认）
    bound = None
    if session.prompt_preset_id:
        row = db.get(PromptPreset, session.prompt_preset_id)
        if row is not None:
            bound = {"id": row.id, "name": row.name, "source": row.source_format}
    effective = None
    active = db.scalar(
        select(PromptPreset).where(
            PromptPreset.user_id == session.user_id,
            PromptPreset.is_active.is_(True),
        )
    )
    # ★ 这两条分支以前写重了（两个分支结果一模一样），这里合成一条。
    if bound is not None:
        effective = {**bound, "from": "session"}
    elif active is not None:
        effective = {
            "id": active.id,
            "name": active.name,
            "source": active.source_format,
            "from": "global_default",
        }

    # ---------------- 内置守卫规则（永远是"追加"，所以单独报一份）----------------
    # ★ 为什么不混进 effective_preset：
    #   它会**同时**和用户的预设生效，塞进"生效的预设"这一个字段里，
    #   界面就没法说清"你绑的是 A，另外还叠加了系统规则 B"。
    guard = db.scalar(
        select(PromptPreset).where(
            PromptPreset.user_id == session.user_id,
            PromptPreset.is_builtin.is_(True),
        )
    )
    if guard is not None:
        from app.narrative import presets as presets_mod

        builtin_guard = {
            "id": guard.id,
            "name": guard.name,
            "from": "builtin",
            "min_reply_chars": presets_mod.min_reply_chars_of(
                presets_mod.from_config(guard.config)
            ),
        }
    else:
        # 还没生成可编辑副本 → 用的就是代码里的出厂内容；
        # 被用户删掉（墓碑位）→ 真的不生效，如实报 null。
        from app.narrative import presets as presets_mod

        owner = db.get(User, session.user_id)
        dismissed = bool(getattr(owner, "builtin_preset_dismissed", False)) if owner else False
        builtin_guard = (
            None
            if dismissed
            else {
                "id": None,
                "name": "内置守卫规则（出厂内容）",
                "from": "builtin_factory",
                "min_reply_chars": presets_mod.DEFAULT_MIN_REPLY_CHARS,
            }
        )

    if pure_chat:
        # ★ 纯聊天不套任何预设/守卫（提示词走「通用助手」分支，见 build_prompt）。
        #   这里必须如实报 null：否则界面会显示"生效预设：XX"，
        #   而实际根本没用到 —— 属于"预览/界面骗人"，本项目零容忍。
        bound = effective = builtin_guard = None

    return SessionDetail(
        id=session.id,
        title=session.title,
        status=session.status,
        pure_chat=pure_chat,
        character_card=card_reference(db, session.character_card_id),
        provider=provider,
        prompt_preset=bound,
        effective_preset=effective,
        builtin_guard=builtin_guard,
        rolling_summary=session.rolling_summary,
        # ★ 覆盖区间（第 X~Y 轮）：界面据此说明"这份前情提要管到哪儿"
        summary_coverage=summary_coverage(session),
        # ★ 记忆总结的开关/提醒状态：会话页据此渲染「该总结了」横幅与记忆面板
        memory_summary=_memory_summary_state(session, messages),
        state=state_mod.load_state(session) or None,
        # ★ 状态栏格式：界面按它渲染（字段、类型、图标都由卡/世界书决定）。
        #   空 schema 也照传（字段为空数组），界面据此显示"该卡未定义状态栏格式"。
        state_schema=state_mod.effective_schema(session),
        # ★ VN 舞台：卡没开就传 null（界面据此隐藏「舞台」开关）
        vn=vn_stage,
        # ★ 翻译面板：设置 + 可选项 + 已译条数 / 花掉的 token
        translate=translate_state(db, session, messages),
        message_count=session.message_count,
        total_tokens=session.total_tokens,
        last_active_at=session.last_active_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[serialize_message(m) for m in messages],
        messages_total=messages_total,
        messages_truncated=messages_truncated,
        prompt=prompt,
        warnings=_session_warnings(session),
    )


# ==================================================================
#  新建
# ==================================================================
def resolve_character_card(db: Session, user_id: int, card_id: int) -> CharacterCard:
    """建会话时选卡：自己的卡或**别人公开的卡**都能用。

    能看不等于能改 —— 这个区别由 character_card_service 保证（这里只是复用）。
    """
    return character_card_service.get_readable_card(db, user_id, card_id)


def resolve_provider_id(
    db: Session, user_id: int, provider_id: int | None
) -> int | None:
    """确定会话用哪个模型配置。

    不传时回落到该用户的**默认模型**；一个配置都没有时返回 None
    （会话照样能建起来，用户配好模型再回来聊即可）。
    """
    if provider_id is not None:
        # 越权检查：别人的配置一律 404（与模型配置模块的规则一致）
        row = provider_service.get_owned_provider(db, user_id, provider_id)
        return row.id

    default_row = db.scalar(
        select(LLMProvider)
        .where(LLMProvider.user_id == user_id)
        .order_by(LLMProvider.is_default.desc(), LLMProvider.id)
        .limit(1)
    )
    return default_row.id if default_row is not None else None


def pick_greeting(card: CharacterCard, greeting_index: int | None) -> str:
    """选开场白：默认用 greeting，greeting_index 指定时用备选里的那一条。

    ★ 备选开场白（alternate_greetings）是角色卡 V2 规范里的字段，
      对应界面上的「换一个开头」。索引越界时报 400 而不是静默回落到默认 ——
      静默回落会让用户以为"备选开场白没生效"，白白排查半天。
    """
    if greeting_index is None:
        return (card.greeting or "").strip()

    alternates = [g for g in (card.alternate_greetings or []) if str(g).strip()]
    if greeting_index >= len(alternates):
        raise BadRequestError(
            f"备选开场白只有 {len(alternates)} 条，取不到第 {greeting_index + 1} 条",
            detail={"greeting_index": greeting_index, "available": len(alternates)},
        )
    return str(alternates[greeting_index]).strip()


def default_title(card: CharacterCard | None) -> str:
    """会话默认标题。

    ★ 没有角色卡时用「纯聊天 · 时间」：这类会话是本项目的一个刻意入口
      （不掺角色，用来快速验证 API 是否正常、或就是想直接聊天）。
    """
    if card is None:
        return f"纯聊天 · {now():%Y-%m-%d %H:%M}"
    return f"{card.name} · {now():%Y-%m-%d %H:%M}"


def create_session(db: Session, user_id: int, payload) -> NarrativeSession:
    """建一个会话，并把角色卡的开场白写成第一条 assistant 消息。

    ★ 为什么开场白要真的落库，而不是每次渲染时临时拼出来？
      因为它是**对话的一部分**：模型后续的回复是基于它生成的，
      上下文裁剪时也会把它当历史消息参与计算。临时拼装会导致
      "界面看到的"与"模型看到的"不一致，出问题时极难排查。

    ★ `character_card_id` 允许为空 —— 那就是**纯聊天会话**（无角色）：
      不写开场白、不装人设、不注入状态协议，提示词走"通用助手"那一条装配分支
      （见 app/narrative/prompt_builder.py）。它同时也是异构适配层的验收台：
      换任意 API / 协议 / 参数都能立刻看到真实效果。
    """
    if payload.character_card_id is None:
        if payload.greeting_index is not None:
            raise BadRequestError(
                "纯聊天会话没有开场白，不能指定 greeting_index"
                "（备选开场白是角色卡的字段）"
            )
        card = None
    else:
        card = resolve_character_card(db, user_id, payload.character_card_id)
    # ★ 世界书也要取出来：状态栏格式的**第二来源**就在它里面（约定式条目，
    #   见 state_schema.find_book_schema_entry），没有它这张卡的定义就解析不出来。
    book = (
        db.get(WorldBook, card.world_book_id) if card is not None and card.world_book_id else None
    )
    provider_id = resolve_provider_id(db, user_id, payload.llm_provider_id)

    session = NarrativeSession(
        user_id=user_id,
        character_card_id=card.id if card is not None else None,
        llm_provider_id=provider_id,
        kind="story" if card is not None else "chat",
        title=(payload.title or "").strip() or default_title(card),
        status="active",
        message_count=0,
        total_tokens=0,
        last_active_at=None,
    )
    db.add(session)
    # 先 flush 拿到自增主键，才能把开场白挂在它下面
    db.flush()

    if card is None:
        # 纯聊天：没有开场白、没有初始状态，直接落库收工
        db.commit()
        db.refresh(session)
        logger.info(
            "新建纯聊天会话 | user_id={} session_id={} provider_id={}",
            user_id,
            session.id,
            provider_id,
        )
        return session

    # ---------------- 状态栏格式：从卡 / 世界书解析一次并落库 ----------------
    # ★ 用户指出的设计错误：以前字段是**全套写死**的（hp/inventory/location/quests），
    #   于是没声明过 HP 的卡（魔法少女 / 魔女裁判）也显示 `HP 100/100`。
    #   现在字段由卡说话：卡 extensions.hne.state_schema > 世界书「[状态栏]」条目 >
    #   卡 initial_state 的顶层键 > 空（没定义就不注入协议、状态栏显示"未定义"）。
    # ★ 解析一次并落库（而不是每轮现算）：提示词与界面状态栏必须永远一致，
    #   否则用户改了卡之后，老会话的历史状态会与显示格式对不上。
    schema, schema_notes = state_schema_mod.resolve_schema(card, book)
    # ★ VN 模式：卡声明了"表情 → 立绘"就必须有对应的状态字段，否则立绘永远不会变。
    #   作者自己定义过就尊重他（见 vn.ensure_expression_field），没定义就补一个，
    #   并把可选表情写进字段描述 —— 模型于是知道该填哪些词，而不是瞎写。
    vn_config, vn_notes = vn_mod.normalize(vn_mod.extension_of(card))
    schema_notes = list(schema_notes) + vn_mod.ensure_expression_field(schema, vn_config)
    for note in vn_notes:
        logger.info("VN 配置提醒 | card_id={} note={}", card.id, note)
    state_mod.save_schema(session, schema)
    for note in schema_notes:
        logger.info("状态栏格式解析提醒 | session_id={} note={}", session.id, note)
    logger.info(
        "会话状态栏格式 | session_id={} source={} fields={}",
        session.id,
        schema.get("source") or "none",
        state_schema_mod.field_names(schema),
    )

    # ---------------- 初始状态：让状态栏从第一轮就亮起来 ----------------
    # ★ 用户验收时的原话："状态栏应该每一轮都输出，哪怕情况没有变化。"
    #   而"等模型第一轮吐状态块"是靠不住的：模型完全可能漏掉格式要求，
    #   于是用户从头到尾只看到空状态栏，还以为功能没做。
    #   所以卡片可以**自带**初始状态，两条路都支持：
    #     1. extensions.hne.initial_state —— 本项目的规范位置（推荐，界面里看不到原始 JSON）
    #     2. 开场白正文里自带 <state> 块 —— 老卡/别的工具导出的卡也常见，
    #        解析后**必须从正文里剥掉**，否则用户会看到一串 JSON
    # ★ 空 schema（这张卡没定义状态栏）时两条路都不走：不解析、不落库，
    #   但开场白里的 <state> 块仍然要剥掉（绝不能让用户看到原始 JSON）。
    extra = card.extra_data if isinstance(card.extra_data, dict) else {}
    hne_ext = (extra.get("extensions") or {}).get("hne") if isinstance(extra, dict) else None
    raw_initial = hne_ext.get("initial_state") if isinstance(hne_ext, dict) else None
    if isinstance(raw_initial, dict) and raw_initial:
        initial_state, _init_notes = state_mod.normalize(raw_initial, None, schema)
        if initial_state:
            state_mod.save_state(session, initial_state)

    greeting = pick_greeting(card, payload.greeting_index)
    if greeting:
        # token 数用估算值：开场白没有经过模型调用，拿不到真实用量。
        # 这与用户消息的处理方式一致（详见 engine.save_user_message）。
        from app.narrative.context_manager import estimate_tokens

        # 开场白里若自带状态块：剥掉（别让用户看到 JSON），并作为初始状态落库。
        # notify_missing=False：开场白没有状态块是常态，不该弹提醒。
        greeting, _greeting_notes = state_mod.apply_reply(
            session, greeting, schema=schema, notify_missing=False
        )

        # 开场白里也可以掷骰（卡作者想给一个随机开局时）：与回复走同一套结算，
        # 标签绝不能让用户看到；没有启用骰子插件时只剥标签、不掷。
        dice_spec = plugin_runtime.dice_spec(plugin_service.load_enabled(db, session.user_id))
        greeting, greeting_rolls = dice_mod.resolve_model_rolls(greeting, dice_spec)

        message = Message(
            session_id=session.id,
            role="assistant",
            content=greeting,
            token_count=estimate_tokens(greeting),
            model_name=None,
            rolls_json=dice_mod.dumps(
                greeting_rolls,
                show_detail=bool(dice_spec.get("show_detail", True)) if dice_spec else True,
            ),
        )
        db.add(message)
        session.message_count = 1

    db.commit()
    db.refresh(session)
    logger.info(
        "新建叙事会话 | user_id={} session_id={} card_id={} provider_id={}",
        user_id,
        session.id,
        card.id,
        provider_id,
    )
    return session


# ==================================================================
#  修改 / 删除
# ==================================================================
def update_session(db: Session, user_id: int, session_id: int, payload) -> NarrativeSession:
    """改会话（PATCH 语义）。

    ★ 用 model_fields_set 区分「没提交」与「提交了 null」：
      title 是必填字段，传 null 视为"不修改"（与角色卡/世界书的约定一致）。
    """
    session = get_owned_session(db, user_id, session_id)
    submitted = payload.model_fields_set

    if "title" in submitted and payload.title:
        session.title = payload.title.strip()

    if "status" in submitted and payload.status:
        session.status = payload.status

    if "character_card_id" in submitted and payload.character_card_id is not None:
        # 换卡只影响**之后**的提示词，已经落库的历史消息不动 ——
        # 否则用户换个角色，整段对话的语气就断成两截了
        card = resolve_character_card(db, user_id, payload.character_card_id)
        session.character_card_id = card.id

    if "llm_provider_id" in submitted and payload.llm_provider_id is not None:
        session.llm_provider_id = resolve_provider_id(
            db, user_id, payload.llm_provider_id
        )

    # 提示词预设：能显式清空（回到"用全局默认预设"），所以这里**不能用
    # "is not None" 判断** —— 传 null 的语义就是"解绑"。
    if "prompt_preset_id" in submitted:
        if payload.prompt_preset_id is None:
            session.prompt_preset_id = None
        else:
            from app.services import prompt_preset_service

            preset = prompt_preset_service.get_preset(
                db, user_id, payload.prompt_preset_id
            )
            session.prompt_preset_id = preset.id

    db.commit()
    db.refresh(session)
    logger.info("更新叙事会话 | user_id={} session_id={}", user_id, session_id)
    return session


def delete_session(db: Session, user_id: int, session_id: int) -> int:
    """删除会话（连同消息）。

    返回被一并删除的消息条数，供接口拼提示语。

    ★ 3.9 起还要清掉这个会话在向量库里的长期记忆。
      ChromaDB 不是事务性的：数据库删干净了、向量库里还留着，
      下次检索就会召回"幽灵记忆"（内容在，但对话记录里根本找不到）——
      这是本步骤最容易出事的地方，所以清理写在这里、并把失败情况记进日志。
    """
    session = get_owned_session(db, user_id, session_id)
    message_count = session.message_count or 0
    # messages.session_id 的外键是 ON DELETE CASCADE + relationship 的
    # cascade="all, delete-orphan"，所以能自动删干净（这一点由测试真删一次验证）
    db.delete(session)
    db.commit()
    logger.info(
        "删除叙事会话 | user_id={} session_id={} messages={}",
        user_id,
        session_id,
        message_count,
    )
    # 先落库成功、再清向量库；向量库失败不回滚（对话记录已经删了，不该因此报错）
    from app.narrative import memory as memory_mod

    memory_mod.forget_session(user_id, session_id)
    return message_count


# ==================================================================
#  单条消息的撤回 / 编辑 / 重新生成（支持界面上「后悔了」这类操作）
# ==================================================================
def get_owned_message(db: Session, session: NarrativeSession, message_id: int) -> Message:
    """取会话里的一条消息；不属于这个会话时 404。

    ★ 这里必须校验 session_id，不能只按主键取：
      消息 id 是全局自增的，只按 id 取就等于允许用户操作**别人会话里的消息**。
    """
    row = db.scalar(
        select(Message).where(
            Message.id == message_id, Message.session_id == session.id
        )
    )
    if row is None:
        raise NotFoundError(
            "消息不存在", detail={"message_id": message_id, "session_id": session.id}
        )
    return row


def _recalculate_stats(db: Session, session: NarrativeSession) -> None:
    """重算会话的消息条数与 token 统计（删除消息后必须调用）。

    ★ 诚实说明这里的口径：
      `total_tokens` 原本累加的是**模型返回的真实用量**（含输入 token），
      删消息之后没法精确还原"如果没删会花多少"，所以这里改成
      「剩余消息的 token_count 之和」—— 数字会变小，是**估算值**。
      宁可给一个能对上消息列表的估算值，也不要留一个"算不回来"的旧总数，
      否则界面上会出现"只有 3 条消息却显示累计 12000 token"的迷惑现象。
    """
    rows = db.scalars(select(Message).where(Message.session_id == session.id)).all()
    session.message_count = len(rows)
    session.total_tokens = int(sum(int(m.token_count or 0) for m in rows))
    db.commit()
    db.refresh(session)


def delete_messages_from(
    db: Session, session: NarrativeSession, *, after_message_id: int | None, inclusive: bool = False
) -> int:
    """删除「晚于 after_message_id」的全部消息（after 为 None 则全删）。

    inclusive=True 时把这条消息自己也删掉（撤回用）。
    返回删除条数。消息的长期记忆无法按消息粒度删（记忆是按"一轮对话"存的，
    且不同会话的记忆用 session_id 过滤），所以这里**不动向量库**；
    界面上「重新生成」写出的新记忆会与原记忆并存，属于可接受的取舍。
    """
    if after_message_id is None:
        targets = list(
            db.scalars(select(Message).where(Message.session_id == session.id)).all()
        )
    else:
        condition = (
            Message.id >= int(after_message_id)
            if inclusive
            else Message.id > int(after_message_id)
        )
        targets = list(
            db.scalars(
                select(Message).where(Message.session_id == session.id, condition)
            ).all()
        )
    for message in targets:
        db.delete(message)
    db.commit()
    if targets:
        _recalculate_stats(db, session)
    return len(targets)


def find_last_user_message(db: Session, session_id: int) -> Message | None:
    """取这个会话里最后一条 user 消息（重新生成以它为锚点）。"""
    return db.scalar(
        select(Message)
        .where(Message.session_id == session_id, Message.role == "user")
        .order_by(Message.id.desc())
        .limit(1)
    )


def find_previous_user_message(
    db: Session, session_id: int, before_message_id: int
) -> Message | None:
    """取 `before_message_id` **之前**最近的一条 user 消息。

    ★ 用途：重新生成**某一条角色回复**时，用来提问的不是那条回复本身，
      而是它前面那条用户消息 —— 老代码混用了这两个 id，把已经删掉的
      assistant 回复当成"用户说的话"喂给模型，导致重新生成没有输出。
    """
    return db.scalar(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.role == "user",
            Message.id < int(before_message_id),
        )
        .order_by(Message.id.desc())
        .limit(1)
    )


def retract_from_message(db: Session, user_id: int, session_id: int, message_id: int) -> dict:
    """撤回：删掉指定的那条 user 消息**及其之后的全部消息**。

    ★ 允许撤回任意一条 user 消息，而不只是最后一条。
      理由：中间那句话写错了同样该能改（这是聊天软件的通用预期），
      而"删掉它之后的内容"是必须的 —— 那些回复都是基于原话生成的，
      留着就会出现"角色在回答一个不存在的问题"。
      代价是后面的剧情会丢，所以界面上必须**先确认、并说清删几条**。
    """
    session = get_owned_session(db, user_id, session_id)
    message = get_owned_message(db, session, message_id)
    if message.role != "user":
        raise BadRequestError(
            "只能撤回你自己说的话（角色的回复请用「重新生成」）",
            detail={"message_id": message_id, "role": message.role},
        )

    content = message.content
    deleted = delete_messages_from(
        db, session, after_message_id=message.id, inclusive=True
    )
    return {
        "deleted_messages": deleted,
        "retracted_content": content,
        "message_count": session.message_count,
        "total_tokens": session.total_tokens,
    }


def update_user_message(
    db: Session, user_id: int, session_id: int, message_id: int, content: str
) -> dict:
    """改写一条用户消息，并删掉它之后的全部消息（编辑后重发用）。

    ★ 只允许改 user 消息：assistant 回复是模型产物，
      改它等于伪造历史，而且改了之后模型会以为那是自己说过的话。
    """
    from app.narrative.context_manager import estimate_tokens

    session = get_owned_session(db, user_id, session_id)
    message = get_owned_message(db, session, message_id)
    if message.role != "user":
        raise BadRequestError(
            "只能编辑你自己说的话（角色回复请用「重新生成」）",
            detail={"message_id": message_id, "role": message.role},
        )

    text = (content or "").strip()
    if not text:
        raise BadRequestError("消息内容不能只有空白字符")

    message.content = text
    message.token_count = estimate_tokens(text)
    db.commit()
    db.refresh(message)

    deleted = delete_messages_from(db, session, after_message_id=message.id)
    return {
        "message": serialize_message(message),
        "deleted_messages": deleted,
        "message_count": session.message_count,
        "total_tokens": session.total_tokens,
    }
