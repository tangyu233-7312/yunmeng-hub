"""角色卡业务逻辑：增删改查、公开卡库、导入导出。

==================== 四条设计决定（答辩时会被问到）====================
1. **查询公开卡与修改公开卡，是两件事**
   别人的公开卡你能看（GET 返回 200），但不能改（PATCH/DELETE 返回 403）。
   别人的私有卡则连看都看不到（一律 404）。
   为什么这里允许 403 而不是像模型配置那样一律 404？
   因为模型配置里 403 会泄露「这个 ID 存在」；而公开卡本来就是给人看的，
   调用方已经能读到它了，此时回 403 反而更清楚（回 404 会让人以为卡被删了）。

2. **删除角色卡会连带删除用它开的故事**（数据库外键是 ON DELETE CASCADE）
   这是个破坏性很强的操作，所以默认**拒绝删除**有会话在用的卡（409），
   必须显式传 force=true 才真删。让用户每一步都知道自己在删什么。

3. **允许同名角色卡**
   与模型配置（别名唯一）不同：同名的两张卡内容很可能不同
   （比如「小明·校园篇」和「小明·侦探篇」都叫「小明」），
   强行唯一会让用户被迫改名。这里不做唯一约束。

4. **导入导出对齐 Character Card V2 规范**，且保证往返无损（详见下方映射表）。
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
)
from app.db.dialect import json_array_contains, like_contains
from app.db.models import CharacterCard, NarrativeSession, WorldBook
from app.schemas.character_card import CharacterCardCreate, CharacterCardImport
from app.schemas.world_book import normalize_entries
from app.services import world_book_service
from app.utils.png_card import PngCardError, extract_card_json

# ==================================================================
#  查询范围（scope）
# ==================================================================
SCOPE_MINE = "mine"
SCOPE_PUBLIC = "public"
SCOPE_ALL = "all"

#: 列表默认 / 最大每页条数。上限存在的意义：防止有人传 limit=100000 把数据库拖垮
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: 开场白摘要的截断长度（列表展示用）
GREETING_PREVIEW_LENGTH = 120

#: 允许上传的 PNG 上限。
#: ★ 上限不是「怕文件大」，而是**防内存耗尽**：上传的文件要整个读进内存才能解析，
#:   没有上限的话，随便传个大文件就能把服务撑爆。
#:   真实角色卡通常在 1 MB 以内（数据块是 base64 文本，图片本身也小），
#:   10 MB 已经非常宽松。
MAX_PNG_BYTES = 10 * 1024 * 1024


def _now() -> datetime:
    """与数据库 server_default=func.now() 保持一致的本地朴素时间。"""
    return datetime.now()


def _resolve_world_book(
    db: Session, user_id: int, book_id: int | None
) -> WorldBook | None:
    """校验要关联的世界书确实属于当前用户。

    ★ 这一步不能省：如果不校验，用户 A 就能把用户 B 的世界书 ID 挂到自己的卡上，
      之后连卡带书一起导出，等于把别人的设定集偷走了（典型的 IDOR 漏洞）。
    """
    if book_id is None:
        return None
    # 直接复用世界书服务的查询，它会强制带 user_id 条件并在查不到时抛 404
    return world_book_service.get_owned_book(db, user_id, book_id)


# ==================================================================
#  查询
# ==================================================================
def _scope_condition(user_id: int, scope: str):
    """把 scope 参数翻译成一个 SQL 条件。

    mine   : 只看自己的（默认，管理自己的卡库）
    public : 只看**别人**公开的（浏览公共卡库；不含自己的，避免和 mine 重复）
    all    : 自己 + 别人公开的（「我能看到的全部」，用于全局搜索）
    """
    if scope == SCOPE_MINE:
        return CharacterCard.user_id == user_id
    if scope == SCOPE_PUBLIC:
        return (CharacterCard.is_public.is_(True)) & (CharacterCard.user_id != user_id)
    # SCOPE_ALL
    return or_(
        CharacterCard.user_id == user_id,
        CharacterCard.is_public.is_(True),
    )


def _build_filters(user_id: int, scope: str, q: str | None, tag: str | None):
    """拼装列表接口的全部筛选条件（列表与计数共用，保证两者结果一致）。"""
    conditions = [_scope_condition(user_id, scope)]

    if q:
        # 名字或简介命中即算匹配。
        # ★ 用 like_contains（app/db/dialect.py）而不是 `column.like(...)`：
        #   后者编译出来没有 ESCAPE 子句，而 SQLite **没有默认转义符** ——
        #   用户搜 "50%" 时会匹配到 0 条（本该命中 1 条），实测就是这么挂的。
        # 用 like 而非 ilike：MySQL 建表用的是 utf8mb4_unicode_ci，本身不区分大小写。
        #   ★ SQLite 侧的已知差异：它的 LIKE 对非 ASCII 是区分大小写的，
        #     而中文没有大小写，所以中文搜索行为一致；英文关键词在 SQLite 上更严格
        #     （属于"更精确"而非"更错"）。这一点在 README 的双后端章节里写明。
        term = q.strip()
        conditions.append(
            or_(
                like_contains(CharacterCard.name, term),
                like_contains(CharacterCard.description, term),
            )
        )

    if tag:
        conditions.append(json_array_contains(CharacterCard.tags, tag.strip()))

    return conditions


def _order_by(sort: str):
    """返回 ORDER BY 子句。

    ★ 所有排序都追加 id 作为第二排序键，原因：
      如果只按 updated_at 排，时间戳相同的记录在两次查询里顺序可能不同，
      分页时就会出现「第 1 页出现过、第 2 页又出现一次」的错乱。
      加一个唯一列兜底，排序才是确定的。
    """
    if sort == "created":
        return (CharacterCard.created_at.desc(), CharacterCard.id.desc())
    if sort == "name":
        return (CharacterCard.name.asc(), CharacterCard.id.asc())
    return (CharacterCard.updated_at.desc(), CharacterCard.id.desc())


def list_cards(
    db: Session,
    user_id: int,
    *,
    scope: str = SCOPE_MINE,
    q: str | None = None,
    tag: str | None = None,
    sort: str = "recent",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[CharacterCard], int]:
    """分页查询角色卡，返回 (当前页数据, 总条数)。"""
    conditions = _build_filters(user_id, scope, q, tag)

    # 先查总数：total 用于前端渲染分页控件，必须是「满足条件的全部条数」，
    # 而不是本页条数
    total = db.scalar(
        select(func.count()).select_from(CharacterCard).where(*conditions)
    ) or 0

    statement = (
        select(CharacterCard)
        .where(*conditions)
        .order_by(*_order_by(sort))
        .limit(limit)
        .offset(offset)
    )
    rows = list(db.scalars(statement).all())
    return rows, int(total)


def count_sessions_by_card(db: Session, card_ids: list[int]) -> dict[int, int]:
    """批量统计每张卡被多少个会话使用。

    ★ 为什么要「批量」？最直白的写法是在循环里对每张卡查一次，也就是 N+1 查询 ——
      列表有 20 张卡就发 21 条 SQL。这里用一条 GROUP BY 拿到全部计数，
      再在 Python 里配对，数据库压力小得多。
    """
    if not card_ids:
        return {}

    statement = (
        select(NarrativeSession.character_card_id, func.count())
        .where(NarrativeSession.character_card_id.in_(card_ids))
        .group_by(NarrativeSession.character_card_id)
    )
    return {card_id: int(count) for card_id, count in db.execute(statement).all()}


def count_sessions(db: Session, card_id: int) -> int:
    """统计单张卡被多少会话使用。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(NarrativeSession)
            .where(NarrativeSession.character_card_id == card_id)
        )
        or 0
    )


def get_readable_card(db: Session, user_id: int, card_id: int) -> CharacterCard:
    """取一张「我能看的」角色卡：自己的，或者别人公开的。

    两种情况之外一律 404（包括「卡根本不存在」和「是别人的私有卡」）。
    ★ 这两者返回同一个错误，是为了不泄露「这个 ID 是否存在」——
      否则攻击者可以靠状态码差异枚举出系统里有哪些卡。
    """
    statement = select(CharacterCard).where(
        CharacterCard.id == card_id,
        or_(CharacterCard.user_id == user_id, CharacterCard.is_public.is_(True)),
    )
    row = db.scalar(statement)
    if row is None:
        raise NotFoundError("角色卡不存在", detail={"card_id": card_id})
    return row


def get_owned_card(db: Session, user_id: int, card_id: int) -> CharacterCard:
    """取一张「我的」角色卡，用于修改 / 删除。

    三种结果：
      · 是我的          -> 正常返回
      · 别人的公开卡    -> 403（并提示可以「复制一份到自己名下」再改）
      · 不存在 / 别人私有 -> 404
    """
    row = db.scalar(select(CharacterCard).where(CharacterCard.id == card_id))

    if row is None:
        raise NotFoundError("角色卡不存在", detail={"card_id": card_id})

    if row.user_id != user_id:
        if row.is_public:
            raise ForbiddenError(
                "这是其他用户的公开角色卡，你只能查看，不能修改或删除",
                detail={
                    "card_id": card_id,
                    "hint": "可以先复制一份到自己名下再改：POST "
                    f"/api/v1/character-cards/{card_id}/duplicate",
                },
            )
        # 私有卡：装作不存在
        raise NotFoundError("角色卡不存在", detail={"card_id": card_id})

    return row


# ==================================================================
#  增 / 改 / 删
# ==================================================================
def _sanitize_extensions(extensions: Any) -> Any:
    """入库前归一化 `extensions.hne.vn`（VN 立绘/背景）。**不改其它命名空间**。

    ★ 为什么在写入口就做：这份配置里的图片地址会进界面的 `<img src>`。
      脏数据（`javascript:`、`data:image/svg+xml`、超长 base64）不该等到渲染时
      才被前端过滤 —— 存进去的就应该是干净的（与富文本、插件 CSS 同一套规矩）。
    ★ 就地导入（与 `plugin_service` 处理骰子配置同一手法）：`app.narrative.vn`
      是纯函数模块（不认识数据库/服务层），这里只在真正要写 VN 配置时才需要它。
    """
    if not isinstance(extensions, dict):
        return extensions
    hne = extensions.get("hne")
    if not isinstance(hne, dict) or "vn" not in hne:
        return extensions
    from app.narrative import vn as vn_mod

    normalized, notes = vn_mod.normalize(hne.get("vn"))
    for note in notes:
        logger.info("角色卡 VN 配置提醒 | note={}", note)
    return vn_mod.merge_into_extensions(extensions, normalized)


def _apply_create_fields(row: CharacterCard, payload: CharacterCardCreate) -> None:
    """把 CharacterCardCreate 的字段灌进 ORM 行（新建与复制共用）。"""
    for field in (
        "name",
        "avatar_url",
        "description",
        "personality",
        "background",
        "speaking_style",
        "scenario",
        "example_dialogue",
        "greeting",
        "system_prompt",
        "post_history_instructions",
        "is_public",
    ):
        setattr(row, field, getattr(payload, field))

    # 列表字段必须复制成新 list/dict，不能直接引用 payload 里的对象：
    # 否则两个 ORM 行会共享同一个 Python 列表，改一个另一个也跟着变。
    row.tags = list(payload.tags)
    row.alternate_greetings = list(payload.alternate_greetings)

    # ★ extensions（含 extensions.hne.state_schema / initial_state）存在 extra_data 里：
    #   V2 规范要求"认不出的字段一个都不能丢"，所以导入的其它键也在这里。
    if payload.extensions is not None:
        extra = dict(row.extra_data or {})
        extra["extensions"] = _sanitize_extensions(copy.deepcopy(payload.extensions))
        row.extra_data = extra


def create_card(db: Session, user_id: int, payload: CharacterCardCreate) -> CharacterCard:
    """新建角色卡。"""
    # 先校验要关联的世界书确实属于当前用户（防止把别人的书挂到自己的卡上）
    book = _resolve_world_book(db, user_id, payload.world_book_id)

    row = CharacterCard(
        user_id=user_id,
        extra_data={},
        world_book_id=book.id if book else None,
    )
    _apply_create_fields(row, payload)

    db.add(row)
    db.commit()
    db.refresh(row)

    logger.info(
        "新建角色卡 | user_id={} id={} name={} public={} world_book_id={}",
        user_id,
        row.id,
        row.name,
        row.is_public,
        row.world_book_id,
    )
    return row


def update_card(
    db: Session, user_id: int, card_id: int, payload
) -> CharacterCard:
    """更新角色卡（payload 为 CharacterCardUpdate，PATCH 语义）。

    ★ 关键：用 payload.model_fields_set 判断「这次请求到底提交了哪些字段」。
      只有出现在请求里的字段才会被赋值，于是：
        · 字段没提交        -> 保持原值
        · 提交了 null       -> 把该字段清空（这是别的写法做不到的）
      详见 schemas/character_card.py 里 CharacterCardUpdate 的说明。
    """
    row = get_owned_card(db, user_id, card_id)
    submitted = payload.model_fields_set

    for field in (
        "name",
        "avatar_url",
        "description",
        "personality",
        "background",
        "speaking_style",
        "scenario",
        "example_dialogue",
        "greeting",
        "system_prompt",
        "post_history_instructions",
        "is_public",
    ):
        if field in submitted:
            setattr(row, field, getattr(payload, field))

    # 列表字段：显式传 null 表示「清空」，等价于空数组
    if "tags" in submitted:
        row.tags = list(payload.tags or [])
    if "alternate_greetings" in submitted:
        row.alternate_greetings = list(payload.alternate_greetings or [])

    # 世界书关联：传整数 = 换成这本；传 null = 解除关联；不传 = 保持原样
    if "world_book_id" in submitted:
        book = _resolve_world_book(db, user_id, payload.world_book_id)
        row.world_book_id = book.id if book else None

    # ★ extensions：传对象 = 覆盖；传 null = 清空（卡就不再声明状态栏格式）；
    #   不传 = 保持原样。合并而不是整体替换 extra_data ——
    #   导入时保留的其它未知字段（creator 等）不能被一次编辑抹掉。
    if "extensions" in submitted:
        extra = dict(row.extra_data or {})
        if payload.extensions is None:
            extra.pop("extensions", None)
        else:
            extra["extensions"] = _sanitize_extensions(copy.deepcopy(payload.extensions))
        row.extra_data = extra

    db.commit()
    db.refresh(row)
    logger.info("更新角色卡 | user_id={} id={} fields={}", user_id, row.id, sorted(submitted))
    return row


def delete_card(
    db: Session,
    user_id: int,
    card_id: int,
    *,
    delete_sessions: bool = True,
    delete_world_book: bool = True,
    force: bool = False,
) -> dict:
    """删除角色卡，并处理它牵连的两样东西。

    ==================== 牵连关系（本接口最需要小心的地方）====================
    一张卡可能挂着两样东西，删卡时都要把选择权交给用户：

        1. 用它开的**叙事会话**（连同全部对话记录）
        2. 它关联的**世界书**

    ★ 两个参数都默认 True（对应界面上默认勾选的复选框），
      符合「删卡就删干净」的直觉；取消勾选则保留。

    ★ 但默认值本身是有破坏性的，所以还留了一道闸：`force`。
      没传 force 而卡上确实挂着东西时，直接返回 409，
      并在 detail 里列清楚「挂着什么、各有多少」，让前端能据此渲染勾选弹窗；
      用户确认后再带 force=true 重发。
      这样「手滑敲了个 DELETE」不会造成不可逆的损失。

    ★ 世界书额外一条规则：**只有没有别的卡在用它时才真的删**。
      一本世界书可以给多张卡共用，为了删这张卡而顺手把别张卡的世界观也删掉，
      显然不是用户的本意。这种情况会保留世界书，并在返回值里说明原因。

    返回一个摘要 dict，让接口能如实告诉用户「到底删掉了什么」。
    """
    row = get_owned_card(db, user_id, card_id)

    session_count = count_sessions(db, card_id)
    # 必须在删除卡片**之前**把世界书对象取出来，否则外键一断就找不到了
    book = db.get(WorldBook, row.world_book_id) if row.world_book_id else None
    # 只统计「别的卡」——本卡反正要删了，不该算进「还有人在用」里
    other_cards_using_book = (
        world_book_service.count_cards_using_book(
            db, book.id, exclude_card_id=card_id
        )
        if book is not None
        else 0
    )

    # ---------------- 安全闸：挂着东西却没确认 ----------------
    if not force and (session_count or book is not None):
        raise ConflictError(
            "删除这张角色卡会牵连到其它数据，请确认要一并处理哪些",
            detail={
                "card_id": card_id,
                "session_count": session_count,
                "has_world_book": book is not None,
                "world_book_id": book.id if book else None,
                "world_book_name": (
                    world_book_service.serialize_ref(book)["display_name"]
                    if book
                    else None
                ),
                "world_book_shared_by_other_cards": other_cards_using_book,
                "options": {
                    "delete_sessions": "是否连同对话记录一起删除（默认 true）",
                    "delete_world_book": "是否连同世界书一起删除（默认 true）",
                },
                "hint": "确认后请在请求里加上 ?force=true，并按需要设置 "
                "delete_sessions / delete_world_book",
            },
        )

    summary: dict = {
        "deleted_sessions": 0,
        "deleted_world_book": False,
        "world_book_kept": False,
        "world_book_kept_reason": None,
    }

    # ---------------- 会话 ----------------
    if delete_sessions and session_count:
        # 用批量 DELETE 而不是逐个 ORM 对象删除：
        # 一条 SQL 搞定，且 messages 由数据库外键级联清掉（见 models/message.py）
        db.execute(
            delete(NarrativeSession).where(
                NarrativeSession.character_card_id == card_id
            )
        )
        db.flush()
        summary["deleted_sessions"] = session_count

    # ---------------- 卡片本身 ----------------
    db.delete(row)
    # 先 flush，让卡片真的从表里消失。
    # 之后剩下没被删的会话才会被外键置空 character_card_id（SET NULL）。
    db.flush()

    # ---------------- 世界书 ----------------
    if book is not None:
        if not delete_world_book:
            summary["world_book_kept"] = True
            summary["world_book_kept_reason"] = "按你的选择保留了世界书"
        elif other_cards_using_book:
            summary["world_book_kept"] = True
            summary["world_book_kept_reason"] = (
                f"还有 {other_cards_using_book} 张角色卡在使用它，"
                "为避免影响它们，世界书已保留"
            )
        else:
            db.delete(book)
            summary["deleted_world_book"] = True

    db.commit()
    logger.info(
        "删除角色卡 | user_id={} id={} 会话={} 世界书={}",
        user_id,
        card_id,
        summary["deleted_sessions"],
        "已删" if summary["deleted_world_book"] else "保留",
    )
    return summary


def duplicate_card(
    db: Session, user_id: int, card_id: int, *, name: str | None = None
) -> CharacterCard:
    """把一张卡复制到自己名下。

    这是「公开卡库」的关键一环：别人公开的卡你只能看，想改就得先复制一份。
    复制来源可以是自己的卡，也可以是别人的公开卡（走 get_readable_card）。
    """
    source = get_readable_card(db, user_id, card_id)

    if name is None:
        # 名字上限 100 字符，加上后缀可能超长，所以先截断再拼
        suffix = "（副本）"
        name = source.name[: 100 - len(suffix)] + suffix

    # ★ 世界书要**复制成新的一本**，而不是让两张卡指向同一个 world_book_id。
    #   如果只是共享引用，用户改副本的世界书会把原卡的世界书一起改掉，
    #   这是非常反直觉的行为。而且别人公开的卡，其世界书是对方名下的，
    #   更不可能让复制者直接共用。
    copied_book_id: int | None = None
    if source.world_book_id:
        source_book = db.get(WorldBook, source.world_book_id)
        if source_book is not None:
            book_copy = WorldBook(
                user_id=user_id,
                name=source_book.name,
                description=source_book.description,
                # deepcopy：entries 是嵌套结构，浅拷贝会让两本书共享内层 dict，
                # 改一本的另一本跟着变（JSON 列不会报错，只会静默地互相污染）
                entries=copy.deepcopy(list(source_book.entries or [])),
                extra_data=copy.deepcopy(dict(source_book.extra_data or {})),
            )
            db.add(book_copy)
            db.flush()  # 拿到自增 id 才能挂到卡片上
            copied_book_id = book_copy.id

    row = CharacterCard(
        user_id=user_id,
        # 逐字段复制人设内容
        name=name,
        avatar_url=source.avatar_url,
        description=source.description,
        personality=source.personality,
        background=source.background,
        speaking_style=source.speaking_style,
        scenario=source.scenario,
        example_dialogue=source.example_dialogue,
        greeting=source.greeting,
        alternate_greetings=list(source.alternate_greetings or []),
        system_prompt=source.system_prompt,
        post_history_instructions=source.post_history_instructions,
        tags=list(source.tags or []),
        # ★ 复制出来的卡默认**私有**：否则用户复制一张公开卡，
        #   一不小心就往公共卡库里又推了一份重复内容。
        is_public=False,
        # 复制过来的世界书（也是自己名下的新副本）
        world_book_id=copied_book_id,
        # 未被映射的原始数据也一并带走，保证复制不丢字段
        extra_data=copy.deepcopy(dict(source.extra_data or {})),
    )

    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "复制角色卡 | user_id={} 源={} 新={} 世界书副本={}",
        user_id,
        card_id,
        row.id,
        copied_book_id,
    )
    return row


# ==================================================================
#  ORM 行 -> 对外结构
# ==================================================================
def _greeting_preview(greeting: str | None) -> str | None:
    """把开场白压成一行摘要，供列表展示。"""
    if not greeting:
        return None
    flat = " ".join(greeting.split())  # 把换行、连续空格压成单个空格
    if len(flat) <= GREETING_PREVIEW_LENGTH:
        return flat
    return flat[:GREETING_PREVIEW_LENGTH] + "…"


def serialize_brief(
    row: CharacterCard,
    *,
    viewer_id: int,
    session_count: int = 0,
    world_book_name: str | None = None,
) -> dict:
    """列表项（精简版）。刻意不含开场白 / 对话示例等长文本。"""
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "avatar_url": row.avatar_url,
        "description": row.description,
        "tags": list(row.tags or []),
        "is_public": row.is_public,
        "is_owner": row.user_id == viewer_id,
        "session_count": session_count,
        "has_greeting": bool(row.greeting),
        "greeting_preview": _greeting_preview(row.greeting),
        # 只给世界书的名字，不给条目正文（列表要轻）
        "world_book_name": world_book_name,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_detail(
    row: CharacterCard,
    *,
    viewer_id: int,
    session_count: int = 0,
    world_book: WorldBook | None = None,
) -> dict:
    """详情（完整版）。

    world_book 由调用方查好后传进来（而不是在这里用 row.world_book 触发懒加载）：
    列表接口需要批量查询以避免 N+1，把查询交给调用方更容易控制。
    """
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "avatar_url": row.avatar_url,
        "description": row.description,
        "personality": row.personality,
        "background": row.background,
        "speaking_style": row.speaking_style,
        "scenario": row.scenario,
        "example_dialogue": row.example_dialogue,
        "greeting": row.greeting,
        "alternate_greetings": list(row.alternate_greetings or []),
        "system_prompt": row.system_prompt,
        "post_history_instructions": row.post_history_instructions,
        "tags": list(row.tags or []),
        "is_public": row.is_public,
        # ★ 扩展命名空间（extensions.hne.state_schema / initial_state 都在里面）：
        #   卡片编辑界面要用它编辑"状态栏格式"，所以详情必须吐出来。
        "extensions": copy.deepcopy((row.extra_data or {}).get("extensions")),
        "is_owner": row.user_id == viewer_id,
        "session_count": session_count,
        # 世界书精简短引用（id / 名字 / 条目数），不含条目正文
        "world_book": (
            world_book_service.serialize_ref(world_book) if world_book else None
        ),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# ==================================================================
#  Character Card V2 导入 / 导出
# ==================================================================
#: 本表字段负责承载的 V2 data 字段。除此之外的键一律进 extra_data，
#: 这样规范里「不得丢弃无法识别的字段」这条要求才能满足。
MAPPED_V2_KEYS = frozenset(
    {
        "name",
        "description",
        "personality",
        "scenario",
        "first_mes",
        "mes_example",
        "alternate_greetings",
        "system_prompt",
        "post_history_instructions",
        "tags",
    }
)

#: 本项目自有的、V2 规范里没有的字段，导出时放进 extensions 的命名空间里
HNE_EXTENSION_KEY = "hne"

SPEC_NAME = "chara_card_v2"
SPEC_VERSION = "2.0"


def _as_text(value: object) -> str | None:
    """宽容地把导入数据里的值转成文本。

    ★ 为什么宽容？角色卡是用户从各处收集来的，来源五花八门，
      有的导出器会把 personality 写成数字或干脆给个对象。
      这里只接受「本来就是字符串」的值，其余一律当没填 ——
      宁可少导入一个字段，也不要把 "[object Object]" 这种垃圾存进数据库。
    """
    if isinstance(value, str):
        return value
    return None


def _as_str_list(value: object) -> list[str]:
    """宽容地把导入数据里的值转成字符串数组，非字符串项直接丢掉。"""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def parse_card_json(raw: dict) -> dict:
    """把导入的 JSON 解析成「本表字段」的字典。

    自动识别三种输入：
      1. V2 规范： {"spec": "chara_card_v2", "spec_version": "2.0", "data": {...}}
      2. V2 但缺 spec： 只有 data 字段（有些导出器会省略 spec）
      3. V1 / 扁平：  {"name": ..., "first_mes": ...} 直接就是那一层
    """
    if not isinstance(raw, dict):
        raise BadRequestError("角色卡 JSON 必须是一个对象")

    data = raw.get("data")
    if isinstance(data, dict) and ("name" in data or "spec" in raw):
        payload = data
    else:
        # V1 扁平格式
        payload = raw

    fields: dict = {
        "name": _as_text(payload.get("name")),
        "description": _as_text(payload.get("description")),
        "personality": _as_text(payload.get("personality")),
        "scenario": _as_text(payload.get("scenario")),
        # 规范的 first_mes 对应本项目的 greeting（开场白）
        "greeting": _as_text(payload.get("first_mes")),
        # 规范的 mes_example 对应本项目的 example_dialogue
        "example_dialogue": _as_text(payload.get("mes_example")),
        "alternate_greetings": _as_str_list(payload.get("alternate_greetings")),
        "system_prompt": _as_text(payload.get("system_prompt")),
        "post_history_instructions": _as_text(
            payload.get("post_history_instructions")
        ),
        "tags": _as_str_list(payload.get("tags")),
    }

    # 本项目自有字段藏在 extensions.hne 命名空间里（规范推荐的做法）
    extensions = payload.get("extensions")
    hne = extensions.get(HNE_EXTENSION_KEY) if isinstance(extensions, dict) else None
    if isinstance(hne, dict):
        fields["background"] = _as_text(hne.get("background"))
        fields["speaking_style"] = _as_text(hne.get("speaking_style"))
        fields["avatar_url"] = _as_text(hne.get("avatar_url"))

    # 世界书单独取出：它不再塞进 extra_data，而是独立建成 world_books 表的一行
    # （原因见 models/world_book.py：要能复用、要能在删卡时选择保留）
    book_raw = payload.get("character_book")
    fields["world_book"] = book_raw if isinstance(book_raw, dict) else None

    # 其余一切原样保存，导出时再吐回去
    extra_data = {
        k: v
        for k, v in payload.items()
        if k not in MAPPED_V2_KEYS and k != "character_book"
    }
    if extensions is not None:
        extra_data["extensions"] = _sanitize_extensions(extensions)
    fields["extra_data"] = extra_data

    return fields


def import_card(db: Session, user_id: int, payload) -> CharacterCard:
    """导入角色卡（payload 为 CharacterCardImport）。

    校验复用 CharacterCardCreate —— 这样「导入」与「手工新建」
    走的是同一套规则（标签上限、文本清洗全一致），不会出现两套标准。
    世界书的条目也复用 schemas/world_book.py 的 normalize_entries，同理。

    ★ 顺序很关键：**先把所有校验做完，再往数据库写任何东西**。
      如果先建了世界书、后面角色卡校验才失败，
      数据库里就会留一本没有卡在用的孤儿世界书。
    """
    fields = parse_card_json(payload.card)

    if payload.name_override:
        fields["name"] = payload.name_override.strip()

    extra_data = fields.pop("extra_data", {})
    book_raw = fields.pop("world_book", None)
    fields["is_public"] = payload.is_public

    # ---- 第一步：校验角色卡本身 ----
    try:
        validated = CharacterCardCreate(**fields)
    except ValidationError as exc:
        # 把 pydantic 的报错翻译成 400 + 可读信息，而不是抛 500
        raise BadRequestError(
            "角色卡数据不符合规范，导入失败",
            detail={
                "errors": [
                    {
                        "field": ".".join(str(part) for part in err.get("loc", ())),
                        "msg": err.get("msg", ""),
                    }
                    for err in exc.errors()
                ]
            },
        ) from exc

    # ---- 第二步：校验世界书（如果卡里带了的话）----
    book_fields: dict | None = None
    if book_raw:
        book_fields = world_book_service.book_from_v2(book_raw)
        try:
            book_fields["entries"] = normalize_entries(book_fields["entries"])
        except ValueError as exc:
            raise BadRequestError(
                f"角色卡里的世界书（character_book）数据不合法：{exc}",
                detail={"reason": "invalid_character_book"},
            ) from exc

    # ---- 第三步：两样都校验通过了，才开始写库 ----
    world_book_id: int | None = None
    if book_fields is not None:
        book = WorldBook(user_id=user_id, **book_fields)
        db.add(book)
        db.flush()  # 先拿到自增 id，才能挂到卡片上
        world_book_id = book.id

    row = CharacterCard(
        user_id=user_id, extra_data=extra_data, world_book_id=world_book_id
    )
    _apply_create_fields(row, validated)

    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "导入角色卡 | user_id={} id={} name={} 世界书={}",
        user_id,
        row.id,
        row.name,
        world_book_id if world_book_id else "无",
    )
    return row


def import_card_from_png(
    db: Session,
    user_id: int,
    raw: bytes,
    *,
    is_public: bool = False,
    name_override: str | None = None,
) -> CharacterCard:
    """从 PNG 图片导入角色卡。

    SillyTavern 生态里角色卡主要以 PNG 形式传播（卡片 JSON 藏在图片的文本块中），
    所以这个功能决定了「能不能直接吃下网上现成的卡」。

    ★ 解析细节全部封装在 app/utils/png_card.py 里（纯函数、可独立测试），
      本函数只负责三件事：限制大小 → 翻译异常 → 复用既有的导入流程。
    """
    if len(raw) > MAX_PNG_BYTES:
        raise BadRequestError(
            f"文件太大了（{len(raw) / 1024 / 1024:.1f} MB），"
            f"上限为 {MAX_PNG_BYTES // 1024 // 1024} MB",
            detail={"max_bytes": MAX_PNG_BYTES, "actual_bytes": len(raw)},
        )

    try:
        card_json = extract_card_json(raw)
    except PngCardError as exc:
        # 把工具层的异常翻译成对用户友好的 400，并保留原始原因便于排查
        raise BadRequestError(
            str(exc),
            detail={"reason": "invalid_png_card"},
        ) from exc

    # 解析出 JSON 之后，走的完全是和 JSON 导入相同的路径 ——
    # 校验规则、字段映射、extra_data 保留逻辑全部复用，不会出现两套标准。
    return import_card(
        db,
        user_id,
        CharacterCardImport(
            card=card_json,
            is_public=is_public,
            name_override=name_override,
        ),
    )


def export_card(
    row: CharacterCard, *, world_book: WorldBook | None = None
) -> dict:
    """把角色卡导出成 Character Card V2 结构。

    ==================== 字段映射 ====================
        data.name                       <- name
        data.description                <- description
        data.personality                <- personality
        data.scenario                   <- scenario
        data.first_mes                  <- greeting
        data.mes_example                <- example_dialogue
        data.alternate_greetings        <- alternate_greetings
        data.system_prompt              <- system_prompt
        data.post_history_instructions  <- post_history_instructions
        data.tags                       <- tags
        data.extensions.hne.*           <- background / speaking_style / avatar_url
        data.character_book             <- 关联的 world_books 那一行（有才输出）
        其余（creator、character_version、未知字段）<- extra_data 原样吐回

    ★ world_book 由调用方查好传进来；为 None 时**整个键都不输出**。
      规范里 character_book 是可选的，省略比给个空对象更贴近原始数据，
      也是「导入 → 导出」能完全一致的前提。

    ★ 所有文本字段缺省时导出为空字符串 ""，而不是 null：
      规范把它们定义为 string，给 null 有些严格的导入器会直接报错。
    """
    data: dict = {
        "name": row.name,
        "description": row.description or "",
        "personality": row.personality or "",
        "scenario": row.scenario or "",
        "first_mes": row.greeting or "",
        "mes_example": row.example_dialogue or "",
        # 下面几个是 V2 新增字段，本项目没有对应列，给规范的默认空值
        "creator_notes": "",
        "system_prompt": row.system_prompt or "",
        "post_history_instructions": row.post_history_instructions or "",
        "alternate_greetings": list(row.alternate_greetings or []),
        "tags": list(row.tags or []),
        "creator": "",
        "character_version": "",
        "extensions": {},
    }

    # 世界书从独立的表还原成规范的 character_book 结构
    if world_book is not None:
        data["character_book"] = world_book_service.book_to_v2(world_book)

    extra_data = dict(row.extra_data or {})

    # 恢复导入时原样保存的字段（creator / character_version / 未知字段等）
    for key, value in extra_data.items():
        # extensions 单独合并（见下）；character_book 已由上面的世界书逻辑输出，
        # 这里跳过是为了防止历史数据里残留的副本把正确内容覆盖掉
        if key in ("extensions", "character_book"):
            continue
        data[key] = value

    # 合并 extensions：保留导入时的原始内容，再叠加本项目的 hne 命名空间
    original_extensions = extra_data.get("extensions")
    extensions = dict(original_extensions) if isinstance(original_extensions, dict) else {}

    hne: dict = {}
    if row.background:
        hne["background"] = row.background
    if row.speaking_style:
        hne["speaking_style"] = row.speaking_style
    if row.avatar_url:
        hne["avatar_url"] = row.avatar_url
    if hne:
        extensions[HNE_EXTENSION_KEY] = hne

    data["extensions"] = extensions

    return {"spec": SPEC_NAME, "spec_version": SPEC_VERSION, "data": data}
