"""提示词预设（prompt preset）接口。

==================== 接口一览 ====================
    GET    /prompt-presets                      列出我的预设
    POST   /prompt-presets/import               导入（酒馆 completion preset JSON 或本系统格式）
    POST   /prompt-presets                       用"内置装配"为模板新建一份
    GET    /prompt-presets/{id}                  查看详情（含全部块与采样参数）
    PATCH  /prompt-presets/{id}                  改名字 / 描述 / 块顺序启停 / 采样参数 / 设为默认
    DELETE /prompt-presets/{id}                  删除（绑定它的会话自动回落到默认预设）
    PATCH  /prompt-presets/{id}/blocks/{block}   单独改一个块（正文 / 启停 / 角色 / 注入深度）
    POST   /prompt-presets/{id}/blocks           追加一个自定义块（"加破甲块"走这里）
    DELETE /prompt-presets/{id}/blocks/{block}   删掉一个自定义块（内置块不给删，只能禁用）
    GET    /prompt-presets/{id}/export           导出成酒馆能读的 JSON
    GET    /prompt-presets/preview               装配预览（可带 session_id 走真实上下文）
    GET    /prompt-presets/meta                  块清单 / 支持的宏 / 参数生效性说明

==================== 为什么把"元信息"单独开一个接口？====================
界面要能告诉用户「有哪些块可选」「哪些宏会被替换」「哪些参数云端无效」。
这些是**系统的能力清单**，跟具体某份预设无关，单独给一个端点更省事，
也避免前端把这些硬编码进 JS（硬编码的那份迟早会和后端不一致）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, DbSession
from app.narrative import presets
from app.schemas.common import ApiResponse
from app.schemas.prompt_preset import (
    PresetBlockContentIn,
    PresetBlockOut,
    PresetBrief,
    PresetDetail,
    PresetImportIn,
    PresetPreviewOut,
    PresetUpdateIn,
)
from app.services import prompt_preset_service

router = APIRouter()


# ==================================================================
#  ★ 路由注册顺序有讲究（本文件里踩过一次）
#
#  FastAPI 按**注册顺序**匹配。`/{preset_id}` 会把 `/preview` 也吃进去
#  （然后因为 "preview" 不是整数而直接 422/404，**不会**回头去找别的路由）。
#  所以所有固定路径（/meta、/preview）必须写在 /{preset_id} 之前。
# ==================================================================
@router.get("/meta", summary="预设能力清单（块 / 宏 / 参数生效性）")
def preset_meta(current_user: CurrentUser) -> ApiResponse[dict]:
    """返回本系统对预设的支持能力。

    界面上这几张表都由此渲染：
      · blocks   —— 有哪些内置块（可作为装配顺序里的位置）
      · markers  —— 哪些块是"占位符"（自己没正文，代表一段内部内容）
      · macros   —— 支持的宏变量（不支持的会原样保留并提示）
      · params   —— 采样参数的生效性（native 真的发 / passthrough 云端会忽略）
    """
    return ApiResponse.ok(
        {
            "blocks": [
                {
                    "identifier": identifier,
                    "label": presets.BLOCK_LABELS[identifier],
                    "kind": (
                        presets.KIND_MARKER
                        if identifier in presets.MARKER_BLOCKS
                        else presets.KIND_RULE
                    ),
                    "supported": identifier not in presets.UNSUPPORTED_BLOCKS,
                }
                for identifier in presets.BLOCK_LABELS
            ],
            "markers": list(presets.MARKER_BLOCKS),
            "unsupported_blocks": sorted(presets.UNSUPPORTED_BLOCKS),
            "macros": [
                {"token": token, "label": label}
                for token, label in presets.SUPPORTED_MACROS
            ],
            "params": presets.PARAM_SUPPORT,
            "note": (
                "「云端会忽略」的参数是本地推理引擎参数（top_k / min_p / "
                "repetition_penalty 等）：发给 OpenAI 兼容接口不会报错，但也不会生效。"
                "本系统照常保存它们，以便导出回酒馆时保持完整。"
            ),
        }
    )


@router.get("/preview", summary="装配预览：这套预设会把提示词拼成什么样")
def preview_preset(
    db: DbSession,
    current_user: CurrentUser,
    session_id: int | None = Query(
        default=None, description="指定会话则走**真实上下文**（卡片/世界书/历史），否则只做结构预览"
    ),
    preset_id: int | None = Query(default=None, description="指定预设；不传则用会话绑定或全局默认"),
) -> ApiResponse[PresetPreviewOut]:
    """预览装配结果。

    ★ 强烈建议带 session_id：只有走真实上下文，"这条消息由哪个块贡献"
      才是可信的。不带时返回的是结构预览（界面会标注清楚）。
    """
    result = prompt_preset_service.preview(
        db, current_user.id, session_id=session_id, preset_id=preset_id
    )
    return ApiResponse.ok(result)


# ==================================================================
#  列表 / 新建 / 导入
# ==================================================================
@router.get("", summary="列出我的提示词预设", response_model=ApiResponse[list[PresetBrief]])
def list_presets(db: DbSession, current_user: CurrentUser) -> ApiResponse[list[PresetBrief]]:
    rows = prompt_preset_service.list_presets(db, current_user.id)
    return ApiResponse.ok([prompt_preset_service.to_brief(row) for row in rows])


@router.post(
    "/import",
    status_code=status.HTTP_201_CREATED,
    summary="导入预设（酒馆 completion preset / 本系统格式）",
    response_model=ApiResponse[PresetDetail],
)
def import_preset(
    payload: PresetImportIn, db: DbSession, current_user: CurrentUser
) -> ApiResponse[PresetDetail]:
    """导入一份预设。

    导入是**保真 + 告知**：解析不了的块、云端无效的参数都会原样存下来，
    并在 `import_notes` 里明确说明 —— 用户拿这份预设还能导回酒馆继续用。
    """
    row = prompt_preset_service.import_preset(db, current_user.id, payload)
    return ApiResponse.ok(prompt_preset_service.to_detail(row))


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="用系统内置装配为模板新建预设",
    response_model=ApiResponse[PresetDetail],
)
def create_preset(
    db: DbSession,
    current_user: CurrentUser,
    name: str = Query(..., min_length=1, max_length=120, description="预设名称"),
    description: str | None = Query(default=None, max_length=500),
) -> ApiResponse[PresetDetail]:
    """新建一份"等价于当前内置装配"的预设，作为可编辑的起点。"""
    row = prompt_preset_service.create_from_default(
        db, current_user.id, name=name, description=description
    )
    return ApiResponse.ok(prompt_preset_service.to_detail(row))


# ==================================================================
#  单个预设
# ==================================================================
@router.get("/{preset_id}", summary="查看预设详情", response_model=ApiResponse[PresetDetail])
def get_preset(preset_id: int, db: DbSession, current_user: CurrentUser) -> ApiResponse[PresetDetail]:
    row = prompt_preset_service.get_preset(db, current_user.id, preset_id)
    return ApiResponse.ok(prompt_preset_service.to_detail(row))


@router.patch(
    "/{preset_id}", summary="修改预设", response_model=ApiResponse[PresetDetail]
)
def update_preset(
    preset_id: int, payload: PresetUpdateIn, db: DbSession, current_user: CurrentUser
) -> ApiResponse[PresetDetail]:
    row = prompt_preset_service.update_preset(db, current_user.id, preset_id, payload)
    return ApiResponse.ok(prompt_preset_service.to_detail(row))


@router.post(
    "/builtin/restore",
    summary="还原内置守卫规则（身份认知 / 不跑偏 / 输出长度）",
    response_model=ApiResponse[PresetDetail],
)
def restore_builtin(db: DbSession, current_user: CurrentUser) -> ApiResponse[PresetDetail]:
    """把内置守卫预设恢复成出厂内容。

    ★ 为什么需要它：内置规则是**可删除**的（用户明确要求），
      删掉之后不该被系统偷偷重建 —— 那就得给一个明确的"我要恢复"入口，
      否则用户只能靠清数据库才能把规则找回来。
    ★ 路径注册在 `/{preset_id}` **之前**（当前文件里它就是字面量路径，
      而 FastAPI 按注册顺序匹配，放后面会被 `/{preset_id}` 抢走并 422）。
    """
    row = prompt_preset_service.restore_builtin(db, current_user.id)
    return ApiResponse.ok(prompt_preset_service.to_detail(row), message="内置守卫规则已还原")


@router.delete("/{preset_id}", summary="删除预设")
def delete_preset(preset_id: int, db: DbSession, current_user: CurrentUser) -> ApiResponse[dict]:
    """删除预设。

    ★ 绑定过它的会话**不会**被删除：外键是 ON DELETE SET NULL，
      会话自动回落到"全局默认预设 / 内置装配"。故事不会因为删预设而丢。
    ★ 删的是**内置守卫预设**时，删除是**真的删除**：系统不会自动重建
      （靠 users.builtin_preset_dismissed 记住），想再要回来用 `/builtin/restore`。
    """
    prompt_preset_service.delete_preset(db, current_user.id, preset_id)
    return ApiResponse.ok({"deleted": preset_id})


# ==================================================================
#  块级操作
# ==================================================================
@router.post(
    "/{preset_id}/blocks",
    status_code=status.HTTP_201_CREATED,
    summary="追加一个自定义块（加破甲块走这里）",
    response_model=ApiResponse[PresetDetail],
)
def add_block(
    preset_id: int, payload: PresetBlockOut, db: DbSession, current_user: CurrentUser
) -> ApiResponse[PresetDetail]:
    row = prompt_preset_service.add_block(db, current_user.id, preset_id, payload)
    return ApiResponse.ok(prompt_preset_service.to_detail(row))


@router.patch(
    "/{preset_id}/blocks/{identifier}",
    summary="改一个块（正文 / 启停 / 角色 / 注入深度）",
    response_model=ApiResponse[PresetDetail],
)
def update_block(
    preset_id: int,
    identifier: str,
    payload: PresetBlockContentIn,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[PresetDetail]:
    row = prompt_preset_service.update_block(
        db, current_user.id, preset_id, identifier, payload
    )
    return ApiResponse.ok(prompt_preset_service.to_detail(row))


@router.delete(
    "/{preset_id}/blocks/{identifier}",
    summary="删掉一个自定义块（内置块只能禁用，不能删）",
    response_model=ApiResponse[PresetDetail],
)
def delete_block(
    preset_id: int, identifier: str, db: DbSession, current_user: CurrentUser
) -> ApiResponse[PresetDetail]:
    row = prompt_preset_service.delete_block(db, current_user.id, preset_id, identifier)
    return ApiResponse.ok(prompt_preset_service.to_detail(row))


@router.get("/{preset_id}/export", summary="导出成酒馆能读的 JSON")
def export_preset(
    preset_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[dict]:
    """导出成 SillyTavern completion preset 格式，方便拿回酒馆继续改。"""
    return ApiResponse.ok(prompt_preset_service.export_preset(db, current_user.id, preset_id))
