"""API v1 路由汇总。

各业务模块的路由在这里统一注册，main.py 会为它们加上 /api/v1 前缀。

已启用：
    system           系统诊断（组件状态、向量库自检、参数探测）
    auth             用户注册 / 登录 / 刷新令牌
    providers        用户自配模型配置的增删改查与诊断
    prompt_presets   提示词预设（酒馆式规则/破甲，规范模型行为）
    character_cards  角色卡管理（含公共卡库与 V2 规范导入导出）
    world_books      世界书管理（可被多张角色卡共用的世界观设定集）
    plugins          插件（声明式：正则替换 / 提示词注入 / CSS 主题，仅从 GitHub 安装）
    narrative        叙事会话与对话（含 SSE 流式输出）
    （向量长期记忆的接口也在 narrative 里：/narrative/sessions/{id}/memories）

==================== 关于路由注册顺序 ====================
FastAPI 按注册顺序匹配路由，因此**带具体路径的路由要写在带路径参数的路由之前**，
否则 /providers/test-draft 可能被 /providers/{provider_id} 抢先匹配。
本文件中 providers 内部已经注意了这一点（详见 app/api/v1/providers.py）。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    character_cards,
    narrative,
    plugins,
    prompt_presets,
    providers,
    system,
    world_books,
)

api_router = APIRouter()


@api_router.get("/ping", tags=["系统"], summary="连通性测试")
async def ping() -> dict[str, str]:
    """用于确认 /api/v1 前缀下的路由已正确挂载。"""
    return {"code": "OK", "message": "pong"}


# ---------------- 系统诊断（无需登录）----------------
api_router.include_router(system.router, prefix="/system", tags=["系统诊断"])

# ---------------- 用户认证（无需登录）----------------
api_router.include_router(auth.router, prefix="/auth", tags=["用户认证"])

# ---------------- 模型配置（需登录）----------------
api_router.include_router(providers.router, prefix="/providers", tags=["模型配置"])

# ---------------- 提示词预设（需登录）----------------
# 「预设」规范的是**模型怎么工作**（规则 / 破甲 / 采样参数），
# 与角色卡（角色是谁）、世界书（世界有什么）三者正交。
api_router.include_router(
    prompt_presets.router, prefix="/prompt-presets", tags=["提示词预设"]
)

# ---------------- 角色卡（需登录）----------------
api_router.include_router(
    character_cards.router, prefix="/character-cards", tags=["角色卡"]
)

# ---------------- 世界书（需登录）----------------
api_router.include_router(world_books.router, prefix="/world-books", tags=["世界书"])

# ---------------- 插件（需登录）----------------
# 声明式插件：正则替换 / 提示词注入（作用于发给模型的提示词）+ CSS 主题（作用于控制台）。
# 安装来源只允许 GitHub；清单是纯数据，不执行任何第三方 JS。
api_router.include_router(plugins.router, prefix="/plugins", tags=["插件"])

# ---------------- 叙事会话与对话（需登录）----------------
# 注意：这个模块里有一个 SSE 流式端点（async def），
# 是本项目第一个「异步接口 + 同步数据库」的组合，写法见 narrative.py 顶部说明。
api_router.include_router(narrative.router, prefix="/narrative", tags=["叙事会话"])
