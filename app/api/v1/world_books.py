"""世界书管理接口。

==================== 接口一览 ====================
    GET    /world-books                 列出我的世界书
    POST   /world-books                 新建世界书
    GET    /world-books/{id}            查看详情（含全部条目）
    PATCH  /world-books/{id}            修改（只传要改的字段）
    DELETE /world-books/{id}            删除（被角色卡使用时需加 ?force=true）

==================== 与角色卡的关系 ====================
  · 角色卡通过 world_book_id 引用一本世界书，**一本世界书可被多张卡共用**
    （比如「克苏鲁世界」下面可以有十几张不同角色的卡）。
  · 关联 / 解除关联走角色卡的接口：
        PATCH /character-cards/{id}   {"world_book_id": 12}   关联
        PATCH /character-cards/{id}   {"world_book_id": null} 解除
  · 从角色卡导入时若带了 character_book，会自动建一本世界书并关联。

==================== 世界书为什么没有「公开」选项？====================
  角色卡有 is_public（要建公共卡库），世界书没有。
  因为世界书总是跟着卡走的 —— 别人要用你的世界观，直接复制那张卡即可
  （复制会把世界书一并复制到对方名下，成为对方自己的副本）。
  再单独做一套「公开世界书库」属于另一个功能，当前不需要。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.common import ApiResponse, Page
from app.schemas.world_book import (
    WorldBookBrief,
    WorldBookCreate,
    WorldBookOut,
    WorldBookUpdate,
)
from app.services import world_book_service as book_service

router = APIRouter()


def _to_out(db, row) -> WorldBookOut:
    """把 ORM 行转成详情响应（顺带查出「正在被哪些卡使用」）。"""
    used_by = book_service.cards_using_book(db, row.id)
    return WorldBookOut.model_validate(
        book_service.serialize_detail(row, used_by_cards=used_by)
    )


@router.get("", summary="列出我的世界书", response_model=ApiResponse[Page[WorldBookBrief]])
def list_books(
    db: DbSession,
    current_user: CurrentUser,
    q: str | None = Query(default=None, max_length=100, description="按名称或简介搜索"),
    limit: int = Query(default=book_service.DEFAULT_LIMIT, ge=1, le=book_service.MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[Page[WorldBookBrief]]:
    """分页列出世界书。

    与角色卡列表同理，这里**不返回 entries 全文** ——
    一本世界书可能有几百个条目，列表全带上会很笨重。

    ★ 更关键的是：连**读都不读**那一列。
      曾经这里用 `select(WorldBook)` 取整行，用户导入一本大世界书之后
      列表接口直接 500，报 `(1038, 'Out of sort memory, ...')` ——
      entries 那一列（几百 KB 的 JSON）被塞进排序缓冲，直接把它撑爆。
      现在改成只查标量列 + 在 SQL 里算条目数，见 `list_book_briefs()`。
    """
    rows, total = book_service.list_book_briefs(
        db, current_user.id, q=q, limit=limit, offset=offset
    )
    # 一次性批量查出「每本书被多少张卡使用」，避免 N+1 查询
    counts = book_service.count_cards_by_book(db, [row["id"] for row in rows])

    items = [
        WorldBookBrief.model_validate(
            {
                **row,
                "display_name": book_service.display_name_for(
                    row["name"], row["entry_count"]
                ),
                "card_count": counts.get(row["id"], 0),
            }
        )
        for row in rows
    ]
    return ApiResponse.ok(
        Page.create(items, total=total, limit=limit, offset=offset),
        message=f"共 {total} 本世界书",
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="新建世界书",
    response_model=ApiResponse[WorldBookOut],
)
def create_book(
    payload: WorldBookCreate, db: DbSession, current_user: CurrentUser
) -> ApiResponse[WorldBookOut]:
    """新建一本世界书。

    entries 里每条至少要有一个非空的 content（设定正文）。
    条目里我们不认识的字段会被原样保留，保证导出回 SillyTavern 时不丢设置。
    """
    row = book_service.create_book(db, current_user.id, payload)
    return ApiResponse.ok(_to_out(db, row), message="世界书已创建")


@router.get(
    "/{book_id}", summary="查看世界书", response_model=ApiResponse[WorldBookOut]
)
def get_book(
    book_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[WorldBookOut]:
    """查看详情（含全部条目）。

    返回里的 `used_by_cards` 会列出正在使用这本书的角色卡，
    便于你在删除前确认会影响哪些卡。
    """
    row = book_service.get_owned_book(db, current_user.id, book_id)
    return ApiResponse.ok(_to_out(db, row))


@router.patch(
    "/{book_id}", summary="修改世界书", response_model=ApiResponse[WorldBookOut]
)
def update_book(
    book_id: int,
    payload: WorldBookUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[WorldBookOut]:
    """修改世界书（PATCH 语义：只提交要改的字段）。

    ★ entries 是**整体替换**而不是按条目合并：
      传了就整份换掉，不传则原样不动。
      数组合并的语义很含糊（按什么键合并？顺序怎么算？），
      整体替换对前端反而更好实现 —— 编辑器里本来就持有完整的一份。
    """
    row = book_service.update_book(db, current_user.id, book_id, payload)
    return ApiResponse.ok(_to_out(db, row), message="世界书已更新")


@router.delete(
    "/{book_id}", summary="删除世界书", response_model=ApiResponse[None]
)
def delete_book(
    book_id: int,
    db: DbSession,
    current_user: CurrentUser,
    force: bool = Query(
        default=False,
        description="强制删除。为 false 时，若还有角色卡在用这本书则拒绝删除",
    ),
) -> ApiResponse[None]:
    """删除世界书。

    ★ 默认拒绝删除「还有角色卡在用」的书：删掉之后那些卡会失去世界观设定。
      被拒绝时会返回 409，并在 detail 里列出具体是哪几张卡在用，
      前端可以直接展示给用户看。
      确认要删时加 ?force=true —— 相关卡片的关联会被置空，但卡片本身不受影响。
    """
    book_service.delete_book(db, current_user.id, book_id, force=force)
    return ApiResponse.ok(None, message="世界书已删除")
