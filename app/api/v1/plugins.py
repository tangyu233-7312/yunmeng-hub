"""插件接口（极简插件市场）。

==================== 接口一览 ====================
    GET    /plugins                    列出我的插件 + 内置示例目录（首次访问会补两个默认空壳）
    POST   /plugins                    手工新建一个插件
    POST   /plugins/catalog/{key}      添加 / **更新**一条内置示例（作者注 / 云梦枢主题 …）
    POST   /plugins/install            从 GitHub 安装（只允许 GitHub）
    GET    /plugins/theme.css          取启用中的 CSS 插件拼成的样式（前端注入）
    PATCH  /plugins/{id}               改（启停 / 顺序 / 配置 / 名字）
    DELETE /plugins/{id}               删除

==================== 顺序很重要 ====================
`/install` 与 `/theme.css` 必须注册在 `/{plugin_id}` **之前**：
FastAPI 按注册顺序匹配，先匹配到 `/{plugin_id}` 就会把 "install" 当成 id 去校验，
用户看到的是一个莫名其妙的 422（本项目在 providers 上踩过同类问题）。

==================== 安全边界 ====================
详见 `app/services/plugin_service.py` 顶部说明：只允许 GitHub、只下载数据不执行代码、
有体积与条数上限、失败隔离、CSS 过白名单清洗。
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from fastapi.responses import PlainTextResponse

from app.api.deps import CurrentUser, DbSession
from app.schemas.common import ApiResponse
from app.schemas.plugin import (
    PluginCatalogItem,
    PluginCreate,
    PluginInstall,
    PluginInstallResult,
    PluginListOut,
    PluginOut,
    PluginUpdate,
    UnsupportedExtension,
)
from app.services import plugin_service as plugin_service

router = APIRouter()


def _to_out(row) -> PluginOut:
    return PluginOut.model_validate(plugin_service.serialize(row))


@router.get("", summary="列出我的插件", response_model=ApiResponse[PluginListOut])
def list_plugins(db: DbSession, current_user: CurrentUser) -> ApiResponse[PluginListOut]:
    """列出插件（按应用顺序）。

    首次访问时会给账号补上两个**空的**默认插件（正则替换 / 提示词注入各一个）。
    它们是空壳、不改变任何行为，可以停用也可以直接删掉（删了不会自动重建）。
    """
    rows = plugin_service.list_plugins(db, current_user.id)
    payload = PluginListOut(
        items=[_to_out(row) for row in rows],
        total=len(rows),
        catalog=[
            PluginCatalogItem(**item)
            for item in plugin_service.catalog_for(db, current_user.id)
        ],
        unsupported=[
            UnsupportedExtension(**item)
            for item in plugin_service.unsupported_extensions()
        ],
        allowed_hosts=list(plugin_service.ALLOWED_HOSTS),
        security_note=plugin_service.security_note(),
    )
    return ApiResponse.ok(payload)


@router.post(
    "/install",
    summary="从 GitHub 安装插件",
    response_model=ApiResponse[PluginInstallResult],
    status_code=status.HTTP_201_CREATED,
)
def install_plugin(
    payload: PluginInstall, db: DbSession, current_user: CurrentUser
) -> ApiResponse[PluginInstallResult]:
    """按 URL 安装插件（只允许 GitHub）。

    ★ 支持两种地址：GitHub 网页地址（`…/blob/…`）与 raw 地址，前者会自动转换。
    ★ 同一个来源重复安装是**更新**，不会堆出一堆同名副本。
    ★ 下载失败 / 清单非法 / 超限都会明确报错，且不会留下半装状态。
    """
    row, raw_url, size, keys = plugin_service.install_from_url(
        db, current_user.id, payload.url
    )
    result = PluginInstallResult(
        plugin=_to_out(row),
        source_url=raw_url,
        fetched_bytes=size,
        manifest_keys=keys,
    )
    return ApiResponse.ok(result, message=f"插件「{row.name}」已安装")


@router.get(
    "/theme.css",
    summary="启用中的 CSS 插件拼成的样式",
    response_class=PlainTextResponse,
)
def theme_css(db: DbSession, current_user: CurrentUser) -> PlainTextResponse:
    """返回一段 CSS 文本（控制台启动时取一次并注入 <style>）。

    用 text/css 而不是 JSON：前端可以直接塞进样式表，少一层包装。
    内容已经过白名单清洗（挡 `</style>` 逃逸 / `@import` / `expression` 等）。
    """
    return PlainTextResponse(
        plugin_service.theme_css(db, current_user.id),
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/catalog/{key}",
    summary="添加 / 更新一条内置示例插件",
    response_model=ApiResponse[PluginOut],
    status_code=status.HTTP_201_CREATED,
)
def add_from_catalog(
    key: str, response: Response, db: DbSession, current_user: CurrentUser
) -> ApiResponse[PluginOut]:
    """把「内置示例」里的一条加进我的插件；**已经添加过**的就更新到最新内容。

    目录里的条目（作者注 / 目标 / 中文输出 / 清理 Markdown 强调符号 / 云梦枢主题 …）
    **不会自动生效**：必须点这一下才会变成你自己的插件，避免"内置示例偷偷改了提示词"。

    ★ 为什么还要能"更新"：内置条目会随版本升级（尤其主题），而插件内容在**添加时就被拷进
      数据库**了 —— 不更新的话，用户看到的永远是他当初添加的那一版（我们改了半天他看不出变化）。
      三种情况分别给 201（新建）/ 200（更新）/ 400（已是最新，不假装成功）。
    """
    row, updated = plugin_service.add_from_catalog(db, current_user.id, key)
    if updated:
        response.status_code = status.HTTP_200_OK
        return ApiResponse.ok(_to_out(row), message=f"「{row.name}」已更新到最新")
    return ApiResponse.ok(_to_out(row), message=f"已添加「{row.name}」")


@router.post(
    "",
    summary="新建插件",
    response_model=ApiResponse[PluginOut],
    status_code=status.HTTP_201_CREATED,
)
def create_plugin(
    payload: PluginCreate, db: DbSession, current_user: CurrentUser
) -> ApiResponse[PluginOut]:
    """手工新建一个插件（不想去 GitHub 建仓库时的本地写法）。

    校验与"从 URL 安装"共用同一套（`plugin_service.validate_config`）。
    """
    row = plugin_service.create_plugin(db, current_user.id, payload)
    return ApiResponse.ok(_to_out(row), message=f"插件「{row.name}」已创建")


@router.patch(
    "/{plugin_id}", summary="修改插件", response_model=ApiResponse[PluginOut]
)
def update_plugin(
    plugin_id: int,
    payload: PluginUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> ApiResponse[PluginOut]:
    """修改插件（PATCH 语义：只提交要改的字段）。

    启停、顺序、配置都走这里；`config` 传了就整体替换（理由同世界书 entries）。
    """
    row = plugin_service.update_plugin(db, current_user.id, plugin_id, payload)
    return ApiResponse.ok(_to_out(row), message="插件已更新")


@router.delete(
    "/{plugin_id}", summary="删除插件", response_model=ApiResponse[None]
)
def delete_plugin(
    plugin_id: int, db: DbSession, current_user: CurrentUser
) -> ApiResponse[None]:
    """删除插件（没有"被谁引用"的问题，可以直接删）。"""
    plugin_service.delete_plugin(db, current_user.id, plugin_id)
    return ApiResponse.ok(None, message="插件已删除")
