"""世界书业务逻辑：增删改查，以及与角色卡之间的关联维护。

==================== 三条设计决定 ====================
1. **世界书是用户的私有资产，不做公开共享。**
   角色卡有 is_public（要建公共卡库），世界书没有。
   理由：世界书总是跟着卡走的 —— 别人要用你的世界观，复制那张卡即可
   （复制时会把世界书一并复制到对方名下）。
   再单独做一套「公开世界书库」属于另一个功能，暂不需要。

2. **删除世界书默认拒绝「正在被角色卡使用」的书。**
   删掉之后那些卡会失去世界观设定（外键置空），用户多半不是这个意思。

3. **导入导出时与 Character Card V2 的 character_book 字段互转**，
   未映射的键存进 extra_data，保证往返无损。
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import CharacterCard, WorldBook

#: 列表默认 / 最大每页条数，与角色卡保持一致
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

#: V2 规范里 character_book 内部、由本表独立列承载的键。
#: 其余键（scan_depth / token_budget / extensions / 未知字段）全进 extra_data。
_BOOK_MAPPED_KEYS = frozenset({"name", "description", "entries"})


def _escape_like(term: str) -> str:
    """转义 LIKE 通配符（理由同角色卡服务：用户搜 "50%" 时那个百分号是字面意思）。"""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _entry_stats(entries: list | None) -> tuple[int, int]:
    """统计条目总数与启用数，供列表展示。"""
    items = entries or []
    total = len(items)
    enabled = sum(
        1 for item in items if isinstance(item, dict) and item.get("enabled", True)
    )
    return total, enabled


def _display_name(row: WorldBook, total: int) -> str:
    """界面显示用的名字。

    ★ 为什么要有这个字段？
      规范里世界书的名字是可选的，很多卡不写。数据库忠实存 NULL，
      但界面上总不能显示一片空白，所以给一个兜底文案。
      把「存储值」和「显示值」分开之后：
        · 数据保持忠实（导出时能原样还原成「没有名字」）
        · 界面依然友好
      如果直接把兜底文案写进数据库，导出结果就会和原始数据不一致了。
    """
    if row.name:
        return row.name
    return f"未命名世界书（{total} 条）"


def display_name_for(name: str | None, entry_count: int) -> str:
    """`_display_name` 的纯参数版本。

    列表查询为了不读 entries 大列，只拿到标量字段、没有 ORM 行对象，
    所以这里再提供一个"传字符串"的版本，保证两处兜底文案完全一致。
    """
    if name:
        return name
    return f"未命名世界书（{entry_count} 条）"


# ==================================================================
#  查询
# ==================================================================
def list_books(
    db: Session,
    user_id: int,
    *,
    q: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[WorldBook], int]:
    """分页列出该用户的世界书（完整实体，返回 (当前页, 总条数)）。

    ⚠️ **接口层不要用这个函数**：它会把 `entries` 这个可能很大的 JSON 列
    整个读进来。列表接口请用 `list_book_briefs()` —— 原因见那里的说明。
    这里保留是为了给"确实需要整本世界书"的场景（脚本、批量导出）用。
    """
    conditions = _list_conditions(user_id, q)
    total = db.scalar(
        select(func.count()).select_from(WorldBook).where(*conditions)
    ) or 0
    statement = (
        select(WorldBook)
        .where(*conditions)
        .order_by(WorldBook.updated_at.desc(), WorldBook.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(statement).all()), int(total)


#: 世界书列表要用的标量列（**不含 entries / extra_data**）
_BRIEF_COLUMNS = (
    WorldBook.id,
    WorldBook.user_id,
    WorldBook.name,
    WorldBook.description,
    WorldBook.created_at,
    WorldBook.updated_at,
)


def list_book_briefs(
    db: Session,
    user_id: int,
    *,
    q: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """分页列出世界书的**精简信息**。

    ★ 这个函数是为了修一个真实的 500 而写的：

        用户导入一本条目很多的世界书之后，「世界书」页直接报
            (1038, 'Out of sort memory, consider increasing server sort buffer size')

      根因不是数据量本身，而是**列表查询把 entries 整列读出来参与排序**：
      本项目用的是 MySQL 默认的 256KB sort buffer，而 entries 是几百 KB 的 JSON，
      MySQL 把整行塞进排序缓冲时直接溢出。

    ★ 所以这里分两步走，**排序那一步绝不带 entries**：
        ① 只查标量列 + `ORDER BY`（行很小，排序缓冲再也不会爆），顺带拿到 id 列表；
        ② 按 id 单独取 entries 一列，在 Python 里数条数（不参与排序）。
      第二步仍然要把这一页的 JSON 读进内存，但**一页最多 100 本**，
      而且读进来就立刻扔掉，不会像排序那样在数据库侧累积。
    """
    conditions = _list_conditions(user_id, q)
    total = db.scalar(
        select(func.count()).select_from(WorldBook).where(*conditions)
    ) or 0

    # ① 标量列 + 排序 + 分页（★ 这里一定不能出现 entries）
    statement = (
        select(*_BRIEF_COLUMNS)
        .where(*conditions)
        .order_by(WorldBook.updated_at.desc(), WorldBook.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = [dict(row._mapping) for row in db.execute(statement).all()]
    if not rows:
        return [], int(total)

    # ② 单独取这一页的 entries，数条目（在 Python 里做，语法兼容性最好）
    ids = [row["id"] for row in rows]
    detail = db.execute(
        select(WorldBook.id, WorldBook.entries).where(WorldBook.id.in_(ids))
    ).all()
    stats = {book_id: _entry_stats(entries) for book_id, entries in detail}

    for row in rows:
        entry_count, enabled_count = stats.get(row["id"], (0, 0))
        row["entry_count"] = entry_count
        row["enabled_entry_count"] = enabled_count

    return rows, int(total)


def _list_conditions(user_id: int, q: str | None) -> list:
    """列表查询的公共过滤条件（两种列表函数共用，避免两边条件写歪）。"""
    conditions = [WorldBook.user_id == user_id]
    if q:
        keyword = f"%{_escape_like(q.strip())}%"
        conditions.append(
            or_(WorldBook.name.like(keyword), WorldBook.description.like(keyword))
        )
    return conditions


def get_owned_book(db: Session, user_id: int, book_id: int) -> WorldBook:
    """取一本属于该用户的世界书，取不到返回 404。

    ★ 与角色卡不同，这里**没有「别人的公开书」这种情况** ——
      世界书是私有的，所以别人的书一律 404，不存在 403 的分支。
    """
    row = db.scalar(
        select(WorldBook).where(WorldBook.id == book_id, WorldBook.user_id == user_id)
    )
    if row is None:
        raise NotFoundError("世界书不存在", detail={"world_book_id": book_id})
    return row


def count_cards_by_book(db: Session, book_ids: list[int]) -> dict[int, int]:
    """批量统计每本世界书被多少张角色卡使用（一条 GROUP BY，避免 N+1）。"""
    if not book_ids:
        return {}

    statement = (
        select(CharacterCard.world_book_id, func.count())
        .where(CharacterCard.world_book_id.in_(book_ids))
        .group_by(CharacterCard.world_book_id)
    )
    return {book_id: int(count) for book_id, count in db.execute(statement).all()}


def count_cards_using_book(
    db: Session, book_id: int, *, exclude_card_id: int | None = None
) -> int:
    """统计有多少张角色卡在用这本书。

    exclude_card_id 用于「正要把这张卡删掉」的场景：
    那张卡马上就要消失了，不该算进「还有别的卡在用」里。
    """
    conditions = [CharacterCard.world_book_id == book_id]
    if exclude_card_id is not None:
        conditions.append(CharacterCard.id != exclude_card_id)

    return int(
        db.scalar(select(func.count()).select_from(CharacterCard).where(*conditions))
        or 0
    )


def cards_using_book(db: Session, book_id: int, limit: int = 20) -> list[dict]:
    """列出正在使用这本书的角色卡（只给 id 和名字）。

    删除世界书时要把这些卡名告诉用户，让他知道会影响谁 ——
    比干巴巴一句「还有 3 张卡在用」有用得多。
    """
    statement = (
        select(CharacterCard.id, CharacterCard.name)
        .where(CharacterCard.world_book_id == book_id)
        .order_by(CharacterCard.id)
        .limit(limit)
    )
    return [{"id": row_id, "name": name} for row_id, name in db.execute(statement).all()]


# ==================================================================
#  增 / 改 / 删
# ==================================================================
def create_book(db: Session, user_id: int, payload) -> WorldBook:
    """新建世界书（payload 为 WorldBookCreate）。"""
    # ★ 扫描参数（scan_depth / token_budget）存在 extra_data 里，
    #   与从角色卡导入时走的是同一个位置 —— 两条路径的存取方式必须一致，
    #   否则"导入的书能调、手建的书调不了"这种不一致会一直存在下去。
    extra = dict(getattr(payload, "book_settings", None) or {})

    row = WorldBook(
        user_id=user_id,
        name=payload.name,
        description=payload.description,
        # 复制成新的 list，避免与 payload 共享同一个 Python 对象
        entries=list(payload.entries),
        extra_data=extra,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    logger.info(
        "新建世界书 | user_id={} id={} name={} 条目数={} 扫描参数={}",
        user_id,
        row.id,
        row.name,
        len(row.entries or []),
        extra or "默认",
    )
    return row


def update_book(db: Session, user_id: int, book_id: int, payload) -> WorldBook:
    """更新世界书（payload 为 WorldBookUpdate，PATCH 语义）。

    ★ 用 model_fields_set 区分「没提交」与「提交了 null」，理由同角色卡。
    """
    row = get_owned_book(db, user_id, book_id)
    submitted = payload.model_fields_set

    if "name" in submitted and payload.name is not None:
        # name 是必填字段，不接受 null（传 null 视为「不改」而不是「清空」）
        row.name = payload.name
    if "description" in submitted:
        row.description = payload.description
    if "entries" in submitted:
        # ★ 整体替换，绝不原地改 —— JSON 列里的嵌套 dict 改动
        #   SQLAlchemy 检测不到（详见 models/world_book.py 的说明）
        row.entries = list(payload.entries or [])

    settings = getattr(payload, "book_settings", None) or {}
    if settings:
        # ★ 必须**整体重新赋值**：extra_data 是 JSON 列，
        #   MutableDict 只能感知顶层键的增删改，所以先复制一份再赋值最稳
        merged = dict(row.extra_data or {})
        merged.update(settings)
        row.extra_data = merged

    db.commit()
    db.refresh(row)
    logger.info(
        "更新世界书 | user_id={} id={} fields={} 扫描参数={}",
        user_id,
        row.id,
        sorted(submitted),
        settings or "未改动",
    )
    return row


def delete_book(
    db: Session, user_id: int, book_id: int, *, force: bool = False
) -> None:
    """删除世界书。

    默认拒绝删除「还在被角色卡使用」的书：删掉之后那些卡会失去世界观设定。
    确认要删时加 force=true，外键会把相关卡片的 world_book_id 置空。
    """
    row = get_owned_book(db, user_id, book_id)

    used_by = cards_using_book(db, book_id)
    if used_by and not force:
        names = "、".join(item["name"] for item in used_by[:5])
        suffix = " 等" if len(used_by) > 5 else ""
        raise ConflictError(
            f"这本世界书正在被 {len(used_by)} 张角色卡使用（{names}{suffix}），"
            "删除后它们会失去世界观设定",
            detail={
                "world_book_id": book_id,
                "card_count": len(used_by),
                "cards": used_by,
                "hint": "确认要删除时，请在请求里加上 ?force=true",
            },
        )

    db.delete(row)
    db.commit()
    logger.info("删除世界书 | user_id={} id={} 关联卡数={}", user_id, book_id, len(used_by))


# ==================================================================
#  ORM 行 -> 对外结构
# ==================================================================
def serialize_brief(row: WorldBook, *, card_count: int = 0) -> dict:
    total, enabled = _entry_stats(row.entries)
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "display_name": _display_name(row, total),
        "description": row.description,
        "entry_count": total,
        "enabled_entry_count": enabled,
        "card_count": card_count,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_detail(row: WorldBook, *, used_by_cards: list[dict] | None = None) -> dict:
    total, enabled = _entry_stats(row.entries)
    # 3.9：把「关键词触发检索」的生效参数一并返回，界面要能看懂"为什么这次没注入"
    from app.narrative.world_book_scanner import resolve_settings

    scan_depth, token_budget = resolve_settings(row)
    return {
        "id": row.id,
        "user_id": row.user_id,
        "name": row.name,
        "display_name": _display_name(row, total),
        "description": row.description,
        "entries": list(row.entries or []),
        "entry_count": total,
        "enabled_entry_count": enabled,
        "scan_depth": scan_depth,
        "token_budget": token_budget,
        "used_by_cards": used_by_cards or [],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_ref(row: WorldBook) -> dict:
    """嵌在角色卡详情里的精简引用。"""
    total, enabled = _entry_stats(row.entries)
    return {
        "id": row.id,
        "name": row.name,
        "display_name": _display_name(row, total),
        "entry_count": total,
        "enabled_entry_count": enabled,
    }


def serialize_name_map(rows: list[WorldBook]) -> dict[int, str]:
    """id -> 显示名字的映射，供角色卡列表显示「已关联哪本书」。"""
    return {row.id: _display_name(row, _entry_stats(row.entries)[0]) for row in rows}


# ==================================================================
#  与 Character Card V2 的互转
# ==================================================================
def book_from_v2(raw: dict) -> dict:
    """把 V2 规范的 character_book 转成「本表字段」的字典。

    ★ 刻意**不给没名字的世界书补名字**（哪怕界面显示起来会好看些）：
      一旦补了，导出结果就和原始数据不一致，
      会破坏「导入导出往返完全一致」这个性质。界面上的兜底见 _display_name。

    ★ extra_data 收下除 name/description/entries 之外的一切，
      包括 scan_depth（扫描深度）、token_budget（token 预算）、
      extensions（扩展）以及任何未知字段 —— 这些属于后续的关键词触发检索功能，
      现在不实现，但绝不能丢，否则用户导出回 SillyTavern 时设置就没了。
    """
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        name = None

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        description = None

    entries = raw.get("entries")
    if not isinstance(entries, list):
        entries = []

    extra_data = {k: v for k, v in raw.items() if k not in _BOOK_MAPPED_KEYS}

    return {
        "name": name.strip()[:200] if name else None,
        "description": description.strip() if description else None,
        "entries": entries,
        "extra_data": extra_data,
    }


def book_to_v2(row: WorldBook) -> dict:
    """把世界书转回 V2 规范的 character_book 结构。

    ★ name / description 为空时**省略该键**，而不是补空字符串。
      因为规范把它们定义为可选字段，而省略能让
      「导入 → 导出」回到与原始数据完全一致的结构
      （很多卡确实不写世界书名字）。

    ★ extensions 则相反：规范规定它**必须存在**（缺省为 {}），
      所以这里统一补上。把这个约束放在**导出**这一处，
      保证不管世界书是「导入来的」还是「手工建的」，输出结构都一致 ——
      如果只在导入时补，手工新建的书导出就会缺字段（这个不一致真实发生过）。
    """
    data: dict = {}

    if row.name:
        data["name"] = row.name
    if row.description:
        data["description"] = row.description

    # 先铺开导入时原样保存的字段（scan_depth / token_budget / extensions / 未知键）
    for key, value in dict(row.extra_data or {}).items():
        data[key] = value

    # 规范要求 extensions 必须存在且为对象
    if not isinstance(data.get("extensions"), dict):
        data["extensions"] = {}

    data["entries"] = list(row.entries or [])
    return data
