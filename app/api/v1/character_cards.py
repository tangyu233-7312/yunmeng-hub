"""角色卡管理接口。

==================== 接口一览 ====================
    GET    /character-cards                     列出角色卡（自己的 / 公共卡库 / 全部）
    POST   /character-cards                     新建角色卡
    GET    /character-cards/{id}                查看角色卡详情
    PATCH  /character-cards/{id}                修改（只传要改的字段，传 null 表示清空）
    DELETE /character-cards/{id}                删除（有会话在用时需加 ?force=true）
    POST   /character-cards/{id}/duplicate      复制一份到自己名下
    GET    /character-cards/{id}/export         导出为 Character Card V2 JSON
    POST   /character-cards/import              导入 Character Card V2 / V1 JSON
    POST   /character-cards/import-png          导入角色卡 PNG 图片（生态主流分发格式）

==================== 可见性规则 ====================
  · scope=mine   只看自己的（默认）
  · scope=public 浏览别人公开的卡（公共卡库）
  · scope=all    自己 + 别人公开的

  别人的公开卡：**能看，不能改**（改会返回 403，并提示先复制一份）。
  别人的私有卡：连看都看不到（404，不泄露它是否存在）。

==================== 关于路由书写顺序 ====================
FastAPI 按注册顺序匹配。POST /import 与 GET /{card_id} 方法不同不会冲突，
但为了养成习惯，这里仍然把不带路径参数的 /import 写在前面
（同一个坑在 /providers/test-draft 上真实踩过，详见 docs/pitfalls.md）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Form, Query, UploadFile, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.db.models import CharacterCard
from app.db.models import WorldBook
from app.schemas.character_card import (
    CharacterCardBrief,
    CharacterCardCreate,
    CharacterCardDeleteResult,
    CharacterCardExport,
    CharacterCardImport,
    CharacterCardOut,
    CharacterCardUpdate,
)
from app.schemas.common import ApiResponse, Page
from app.services import character_card_service as card_service
from app.services import world_book_service

router = APIRouter()


def _load_world_book(db, row: CharacterCard) -> WorldBook | None:
    """取出卡片关联的世界书（没有关联则返回 None）。

    这里显式查一次而不是用 row.world_book 懒加载，是为了让查询时机可控，
    也避免在批量场景里意外触发 N+1。
    """
    if not row.world_book_id:
        return None
    return db.get(WorldBook, row.world_book_id)


def _to_out(db, row: CharacterCard, viewer_id: int) -> CharacterCardOut:
    """把卡片行转成详情响应（带上会话数与世界书摘要）。"""
    return CharacterCardOut.model_validate(
        card_service.serialize_detail(
            row,
            viewer_id=viewer_id,
            session_count=card_service.count_sessions(db, row.id),
            world_book=_load_world_book(db, row),
        )
    )


# ==================================================================
#  导入（必须写在 /{card_id} 之前）
# ==================================================================
@router.post(
    "/import",
    status_code=status.HTTP_201_CREATED,
    summary="导入角色卡 JSON",
    response_model=ApiResponse[CharacterCardOut],
)
def import_card(
    payload: CharacterCardImport, db: DbSession, current_user: CurrentUser
) -> ApiResponse[CharacterCardOut]:
    """导入一张角色卡（支持 Character Card V2 规范与 V1 扁平格式）。

    会自动识别格式：有 spec / data 字段按 V2 解析，否则按 V1 扁平结构解析。

    ★ 规范要求「导入导出不得丢弃无法识别的字段」，
      所以本表没有对应列的字段（如 character_book 角色专属世界书）
      会被原样存进 extra_data，导出时再还给你，不会缺斤少两。

    导入的卡默认是**私有**的，想公开请传 is_public=true。
    """
    row = card_service.import_card(db, current_user.id, payload)
    return ApiResponse.ok(
        CharacterCardOut.model_validate(
            card_service.serialize_detail(
                row, viewer_id=current_user.id, world_book=_load_world_book(db, row)
            )
        ),
        message="角色卡已导入",
    )


@router.post(
    "/import-png",
    status_code=status.HTTP_201_CREATED,
    summary="导入角色卡 PNG 图片",
    response_model=ApiResponse[CharacterCardOut],
)
def import_card_png(
    db: DbSession,
    current_user: CurrentUser,
    file: UploadFile = File(
        ..., description="角色卡 PNG 文件（卡片数据藏在图片的文本块里）"
    ),
    is_public: bool = Form(default=False, description="导入后是否直接设为公开"),
    name_override: str | None = Form(
        default=None, description="覆盖卡片自带的名字"
    ),
) -> ApiResponse[CharacterCardOut]:
    """从 PNG 图片导入角色卡。

    ==================== 为什么需要这个接口？====================
    SillyTavern 生态里角色卡**主要以 PNG 形式传播**，而不是 .json 文件：
    卡片数据被塞进图片的文本块（tEXt / iTXt，关键字 chara 或 ccv3），
    图片本身还能正常显示成人物立绘。一张图就是完整的卡，
    分享出去不会丢东西 —— 所以网上能直接下载到的基本都是 PNG。

    ==================== 两个实现细节 ====================
    1. **为什么用 `def` 而不是 `async def`？**
       本项目用的是同步 SQLAlchemy Session。按 deps.py 里定的规则，
       凡是读写数据库的接口都用 `def`，交给 FastAPI 丢进线程池，
       这样不会阻塞事件循环。
       因此这里直接读 `file.file`（底层是个临时文件对象）而不是
       `await file.read()` —— 后者只有异步接口才能用。

    2. **限制文件大小**（默认 10 MB）。
       解析要先把它整个读进内存，不设上限的话传个大文件就能把服务撑爆。
       真实角色卡通常在 1 MB 以内，10 MB 已经很宽松。

    解析失败（不是 PNG、没有卡片数据、数据损坏）会返回 400 并说明原因。
    """
    # 多读 1 个字节：这样「刚好超过上限」也能被检测出来，
    # 而不是先读满上限、误判为合格
    raw = file.file.read(card_service.MAX_PNG_BYTES + 1)

    row = card_service.import_card_from_png(
        db,
        current_user.id,
        raw,
        is_public=is_public,
        name_override=name_override,
    )
    return ApiResponse.ok(
        CharacterCardOut.model_validate(
            card_service.serialize_detail(
                row, viewer_id=current_user.id, world_book=_load_world_book(db, row)
            )
        ),
        message="角色卡已从 PNG 导入",
    )


# ==================================================================
#  列表 / 新建
# ==================================================================
@router.get(
    "", summary="列出角色卡", response_model=ApiResponse[Page[CharacterCardBrief]]
)
def list_cards(
    db: DbSession,
    current_user: CurrentUser,
    scope: Literal["mine", "public", "all"] = Query(
        default="mine", description="查询范围：mine 我的 / public 公共卡库 / all 我能看到的全部"
    ),
    q: str | None = Query(default=None, max_length=100, description="按名字或简介搜索"),
    tag: str | None = Query(default=None, max_length=50, description="按标签筛选"),
    sort: Literal["recent", "created", "name"] = Query(
        default="recent", description="排序：recent 最近更新 / created 最近创建 / name 名称"
    ),
    limit: int = Query(default=card_service.DEFAULT_LIMIT, ge=1, le=card_service.MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ApiResponse[Page[CharacterCardBrief]]:
    """分页列出角色卡。

    ★ 列表返回的是**精简结构**（见 schemas/character_card.py 的 CharacterCardBrief）：
      角色卡里有好几个超大文本字段，列表里全带上会让响应体轻易上兆。
      需要全文时请调详情接口。
    """
    rows, total = card_service.list_cards(
        db,
        current_user.id,
        scope=scope,
        q=q,
        tag=tag,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    # 一次性批量查出所有卡的会话数，避免 N+1 查询
    counts = card_service.count_sessions_by_card(db, [row.id for row in rows])

    # 世界书同理：只查名字用于列表展示，一次查回来
    book_ids = {row.world_book_id for row in rows if row.world_book_id}
    book_names: dict[int, str] = {}
    if book_ids:
        books = db.scalars(
            select(WorldBook).where(WorldBook.id.in_(book_ids))
        ).all()
        book_names = world_book_service.serialize_name_map(list(books))

    items = [
        CharacterCardBrief.model_validate(
            card_service.serialize_brief(
                row,
                viewer_id=current_user.id,
                session_count=counts.get(row.id, 0),
                world_book_name=(
                    book_names.get(row.world_book_id) if row.world_book_id else None
                ),
            )
        )
        for row in rows
    ]
    return ApiResponse.ok(
        Page.create(items, total=total, limit=limit, offset=offset),
        message=f"共 {total} 张角色卡",
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="新建角色卡",
    response_model=ApiResponse[CharacterCardOut],
)
def create_card(
    payload: CharacterCardCreate, db: DbSession, current_user: CurrentUser
) -> ApiResponse[CharacterCardOut]:
    """新建一张角色卡。

    只有 name 是必填的，其余字段可以之后慢慢补 ——
    人设这种东西本来就是写一点调一点。
    """
    row = card_service.create_card(db, current_user.id, payload)
    return ApiResponse.ok(
        CharacterCardOut.model_validate(
            card_service.serialize_detail(
                row, viewer_id=current_user.id, world_book=_load_world_book(db, row)
            )
        ),
        message="角色卡已创建",
    )


# ==================================================================
#  详情 / 修改 / 删除
# ==================================================================
@router.get(
    "/{card_id}", summary="查看角色卡", response_model=ApiResponse[CharacterCardOut]
)
def get_card(
    card_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[CharacterCardOut]:
    """查看详情。自己的卡、或别人公开的卡，都能看到全文。"""
    row = card_service.get_readable_card(db, current_user.id, card_id)
    return ApiResponse.ok(_to_out(db, row, current_user.id))


@router.patch(
    "/{card_id}", summary="修改角色卡", response_model=ApiResponse[CharacterCardOut]
)
def update_card(
    card_id: int,
    payload: CharacterCardUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[CharacterCardOut]:
    """修改角色卡（PATCH 语义：只提交要改的字段）。

    ★ 怎么清空一个字段？把它显式设为 null，例如 {"greeting": null}。
      字段**不出现**在请求体里则保持原值 —— 这也是它和 PUT 的区别。
    """
    row = card_service.update_card(db, current_user.id, card_id, payload)
    return ApiResponse.ok(_to_out(db, row, current_user.id), message="角色卡已更新")


@router.delete(
    "/{card_id}",
    summary="删除角色卡",
    response_model=ApiResponse[CharacterCardDeleteResult],
)
def delete_card(
    card_id: int,
    db: DbSession,
    current_user: CurrentUser,
    delete_sessions: bool = Query(
        default=True,
        description="是否连同用它开的对话记录一起删除。默认 true（界面上默认勾选）",
    ),
    delete_world_book: bool = Query(
        default=True,
        description="是否连同它关联的世界书一起删除。默认 true（界面上默认勾选）",
    ),
    force: bool = Query(
        default=False,
        description="确认执行。为 false 时，若卡上还挂着会话或世界书则拒绝删除并返回 409",
    ),
) -> ApiResponse[CharacterCardDeleteResult]:
    """删除角色卡。

    ==================== 它会牵连两样东西，由你决定怎么处理 ====================
        1. 用它开的**叙事会话**（连同全部对话记录）  -> delete_sessions
        2. 它关联的**世界书**                        -> delete_world_book

    两个参数都默认 true，对应界面上**默认勾选**的复选框；
    取消勾选即可保留。这样「删卡」就不会再是一刀切了。

    ==================== 两步式调用（推荐前端这样实现）====================
      · 第一步：直接发 DELETE（不带 force）。
        如果卡上挂着东西，会返回 **409**，detail 里写清楚了：
            session_count                        有多少个会话
            has_world_book / world_book_name      挂着哪本世界书
            world_book_shared_by_other_cards      这本书还有几张别的卡在用
        前端据此渲染勾选弹窗。

      · 第二步：用户确认后，带上 `?force=true` 和勾选结果重发。

    ★ 为什么要多一道 force？
      默认值是「全删」，破坏性很强。加一道显式确认，
      「手滑敲了个 DELETE」就不会造成不可逆的损失。

    ★ 世界书有可能「勾了删但没删掉」：如果这本世界书还被**别的**角色卡共用，
      为了不影响它们，世界书会被保留。接口会在 world_book_kept_reason 里
      如实说明原因，前端请展示给用户。
    """
    summary = card_service.delete_card(
        db,
        current_user.id,
        card_id,
        delete_sessions=delete_sessions,
        delete_world_book=delete_world_book,
        force=force,
    )

    # 拼一句人话，把「到底删了什么、什么没删、为什么」讲清楚
    parts = ["角色卡已删除"]
    if summary["deleted_sessions"]:
        parts.append(f"连同 {summary['deleted_sessions']} 个会话")
    if summary["deleted_world_book"]:
        parts.append("连同关联的世界书")
    if summary["world_book_kept"]:
        parts.append(f"（{summary['world_book_kept_reason']}）")

    return ApiResponse.ok(
        CharacterCardDeleteResult.model_validate(summary),
        message="，".join(parts),
    )


# ==================================================================
#  复制 / 导出
# ==================================================================
@router.post(
    "/{card_id}/duplicate",
    status_code=status.HTTP_201_CREATED,
    summary="复制角色卡到自己名下",
    response_model=ApiResponse[CharacterCardOut],
)
def duplicate_card(
    card_id: int,
    db: DbSession,
    current_user: CurrentUser,
    name: str | None = Query(default=None, max_length=100, description="新卡的名字"),
) -> ApiResponse[CharacterCardOut]:
    """复制一张卡。

    这是「公共卡库」的关键一环：别人公开的卡你只能看，
    想改就得先复制一份到自己名下（复制出来的默认私有）。
    """
    row = card_service.duplicate_card(db, current_user.id, card_id, name=name)
    return ApiResponse.ok(
        CharacterCardOut.model_validate(
            card_service.serialize_detail(
                row, viewer_id=current_user.id, world_book=_load_world_book(db, row)
            )
        ),
        message="已复制到你的角色卡库",
    )


@router.get(
    "/{card_id}/export",
    summary="导出为 Character Card V2",
    response_model=ApiResponse[CharacterCardExport],
)
def export_card(
    card_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[CharacterCardExport]:
    """导出成业界通用的 Character Card V2 JSON。

    导出的 data 部分可以直接保存为 .json 文件，导入到 SillyTavern 等工具里使用。
    前端做「下载」时，把返回的 data 字段转成 Blob 即可。

    ★ 如果这张卡关联了世界书，它会作为规范的 character_book 字段一并导出，
      所以导入到别的工具后世界观设定不会丢。
    """
    row = card_service.get_readable_card(db, current_user.id, card_id)
    exported = card_service.export_card(row, world_book=_load_world_book(db, row))
    return ApiResponse.ok(
        CharacterCardExport.model_validate(exported), message="导出成功"
    )
