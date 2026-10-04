"""FastAPI 应用入口。

开发环境启动命令（在项目根目录下执行）：

    .\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

启动后：
    可视化控制台  http://127.0.0.1:8000/console
    接口文档      http://127.0.0.1:8000/docs
    健康检查      http://127.0.0.1:8000/health
"""

from __future__ import annotations

import hashlib
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.context import new_request_id, set_request_id
from app.core.exceptions import ConfigurationError, register_exception_handlers
from app.core.logging import setup_logging
from app.db.chroma import check_vector_store, dispose_chroma
from app.db.mysql import check_connection, dispose_engine
from app.schemas.common import ComponentStatus, HealthResponse

# 入口页里等待被替换成真实版本号的占位符（见 _console_index）
WEB_VERSION_PLACEHOLDER = "__WEB_VERSION__"
# web 目录 → (采样时刻, 版本号)，避免每次请求都扫一遍前端目录
_VERSION_CACHE: dict[str, tuple[float, str]] = {}


def _frontend_version(web_dir: Path) -> str:
    """算出前端资源的版本指纹，用于**缓存击穿**。

    ★ 为什么要这个指纹（真实事故：用户打开 /console 一片空白）：

      前端是原生 ES Module，浏览器按 URL 缓存每个 .js。只改其中一部分文件时，
      用户浏览器里会出现「新的 views/chat.js + 旧的 ui.js」这种版本混用；
      而 ES Module 一旦 import 一个目标模块里不存在的导出，会**整包加载失败**
      —— 页面全白，且用户按 Ctrl+F5 也未必救得回来（实测日志里全是 304）。

      解决办法：让 index.html 里的 importmap 给每个模块 URL 带上版本号。
      版本一变 → 所有模块 URL 一起变 → 浏览器没有旧副本可用，必须重新下载。

      指纹取「所有前端文件的 mtime + size」，故意**不用内容哈希**：
        · mtime 只在文件真的被改过时才变，语义刚好够用；
        · 不用把每个文件读一遍，一次 os.scandir 就够了（结果还带缓存）。
    """
    parts: list[str] = []
    for path in sorted(web_dir.rglob("*")):
        if path.is_file():
            stat = path.stat()
            parts.append(f"{path.relative_to(web_dir).as_posix()}:{int(stat.st_mtime)}:{stat.st_size}")
    # 文件列表为空时也要给出一个稳定值，避免返回 None 让模板渲染出 __WEB_VERSION__
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12] if parts else "0"


def _web_version_cached(web_dir: Path) -> str:
    """给 _frontend_version 加一层 2 秒缓存。

    /console/ 每次加载都会问一次版本，而 os.scandir 整棵前端目录虽然便宜，
    但没必要每个请求都做。2 秒足够让"改完文件→刷新页面"立刻看到新版本，
    又不会在高频刷新时反复扫盘。
    """
    key = str(web_dir)
    hit = _VERSION_CACHE.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < 2.0:
        return hit[1]
    version = _frontend_version(web_dir)
    _VERSION_CACHE[key] = (now, version)
    return version


class NoCacheStaticFiles(StaticFiles):
    """托管前端资源：**强制浏览器每次校验**，并把入口页里的版本号渲染出来。

    ★ 为什么 /console 的响应改成 no-store（不再依赖 ETag 协商）：

      只用 `no-cache` 的话，浏览器仍然可能一直复用旧副本 —— 用户实际遇到的就是
      这种情况：日志里全是 304，页面上却始终是旧的 ui.js，Ctrl+F5 也没能救回来。
      前端文件总共只有十几个、几十 KB，本地/单机部署下每次都重新下载的代价
      可以完全忽略。所以这里对 HTML 一律 `no-store`；
      模块 .js 则通过 importmap 的版本号来击穿缓存（见 app/main.py 顶部说明）。

    ★ 为什么不用"文件名加哈希"（webpack 那套）：
      那需要构建步骤，与本项目"零构建、改完刷新即生效"的取舍冲突。
      这里用"URL 查询串带版本号"，效果一样，但不需要任何打包器。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        # 入口页要特殊处理：它里面带着 importmap 的版本号必须每次都是最新的
        if path in (".", "index.html"):
            return self._console_index(scope)
        response = await super().get_response(path, scope)
        # 语义是"可以缓存，但每次必须回源校验"，不是"不缓存"
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        # 明确告诉中间代理：这段内容依赖 ETag 协商，别自作主张缓存
        response.headers["Vary"] = "Accept-Encoding"
        # 前端版本号也顺便暴露给响应头，boot.js 用它做缓存自愈
        response.headers["X-HNE-Web-Version"] = _web_version_cached(Path(self.directory))
        return response

    def _console_index(self, scope) -> Response:
        """渲染入口页：把 __WEB_VERSION__ 替换成真实版本号。

        返回 no-store，保证用户每次打开都拿到当前版本，
        importmap 里的模块 URL 也就永远指向最新代码。
        """
        web_dir = Path(self.directory)
        index = web_dir / "index.html"
        version = _web_version_cached(web_dir)
        try:
            html = index.read_text(encoding="utf-8").replace(WEB_VERSION_PLACEHOLDER, version)
        except OSError:  # pragma: no cover - 文件被删/无权限时给可读的错误
            logger.error("前端入口页读取失败: {}", index)
            return Response("前端入口页读取失败", status_code=500, media_type="text/plain; charset=utf-8")
        headers = {
            "Cache-Control": "no-store, must-revalidate",
            "X-HNE-Web-Version": version,
        }
        return Response(html, media_type="text/html; charset=utf-8", headers=headers)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动时初始化资源，关闭时释放资源。

    lifespan 是 FastAPI 推荐的资源管理方式：
      yield 之前的代码在「服务启动时」执行一次
      yield 之后的代码在「服务关闭时」执行一次
    这样能保证连接池等资源被正确创建和释放，不会泄漏。
    """
    settings = get_settings()

    # ---- 启动阶段 ----
    # 注意顺序：先配好日志，后面的日志才有统一格式；先建目录，日志文件才写得进去
    setup_logging()
    settings.ensure_dirs()

    logger.info("=" * 62)
    logger.info(
        "{} v{} 启动中 | 环境: {}", settings.APP_NAME, settings.APP_VERSION, settings.APP_ENV
    )
    # validate_runtime 会检查密钥、数据库密码等是否填写，返回告警列表
    for problem in settings.validate_runtime():
        logger.warning("配置告警: {}", problem)
    logger.info("接口文档: http://{}:{}/docs", settings.HOST, settings.PORT)

    # ---- 启动自检：MySQL ----
    # 创建 Engine 本身并不会真正连接数据库（SQLAlchemy 是惰性连接），
    # 所以这里主动发一条 SELECT 1 探活，目的是「启动时就把问题暴露出来」，
    # 而不是等用户发第一个请求才报错。
    db_status = check_connection()
    if db_status["status"] == "ok":
        logger.info("MySQL 连接正常 | 版本 {}", db_status.get("mysql_version"))
    else:
        logger.error("MySQL 连接失败 | {}", db_status.get("message"))
        if settings.is_production:
            # 生产环境连不上数据库直接拒绝启动，避免「带病上线」
            raise ConfigurationError("MySQL 连接失败，拒绝启动", detail=db_status)

    # ---- 启动自检：ChromaDB 向量库 + 嵌入后端 ----
    # probe=True 会真实执行一次嵌入推理。这样做的价值是「把问题挡在启动阶段」：
    # 若模型文件损坏、缓存丢失、或 API 嵌入后端配置错误，启动时就会看到明确报错，
    # 而不是等用户辛苦聊了半天、触发记忆检索时才失败。
    vs_status = check_vector_store(probe=True)
    if vs_status["status"] == "ok":
        embedding_info = vs_status.get("embedding", {})
        logger.info(
            "向量库就绪 | 目录 {} | 距离度量 {} | 嵌入后端 {} ({}) | 集合数 {}",
            vs_status.get("persist_dir"),
            vs_status.get("distance_space"),
            embedding_info.get("model"),
            embedding_info.get("backend"),
            vs_status.get("collection_count"),
        )
    else:
        logger.error("向量库初始化失败 | {}", vs_status.get("message"))
        if settings.is_production:
            raise ConfigurationError("向量库初始化失败，拒绝启动", detail=vs_status)

    logger.info("=" * 62)

    yield  # ------------------- 应用运行中 -------------------

    # ---- 关闭阶段：释放连接池与向量库资源 ----
    dispose_engine()
    dispose_chroma()
    logger.info("{} 已停止", settings.APP_NAME)


def create_app() -> FastAPI:
    """构建并返回 FastAPI 应用实例（工厂模式，便于测试时创建隔离实例）。"""
    settings = get_settings()

    app = FastAPI(
        title="异构大模型交互式叙事引擎",
        description=(
            "支持用户自配任意大模型 API（自定义 URL / Key / 模型名）的交互式叙事后端服务。\n\n"
            "**功能模块**：用户认证 · LLM 配置管理 · 角色卡 · 叙事会话 · 向量长期记忆"
        ),
        version=settings.APP_VERSION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ---------------- 跨域：为后续 HTML/JS 与 Electron 前端预留 ----------------
    # 什么是 CORS（跨域）？
    #   浏览器出于安全考虑，默认禁止网页向「不同源」（协议/域名/端口任一不同）的后端
    #   发请求。后续用 HTML 打开的前端页面若要调用本服务，就必须由服务端明确声明
    #   「允许哪些来源访问」，这就是 CORS 中间件的作用。
    # CORS_ORIGINS 白名单在 .env 里配置，不要写成 ["*"]，
    # 因为 allow_credentials=True（允许携带登录 Cookie/凭证）时，浏览器禁止通配符。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,   # 允许访问的前端来源白名单
        allow_credentials=True,                # 允许前端携带认证信息
        allow_methods=["*"],                   # 允许的 HTTP 方法（GET/POST/PUT/DELETE...）
        allow_headers=["*"],                   # 允许前端携带的请求头
        # 允许前端 JS 读取这两个响应头（跨源时默认读不到）：
        #   X-Request-ID        —— 排查问题时用来把前端报错和后端日志对上
        #   X-HNE-Web-Version   —— 前端版本号，boot.js 靠它判断浏览器缓存是不是旧的
        expose_headers=["X-Request-ID", "X-HNE-Web-Version"],
    )

    # ---------------- 全局异常处理 ----------------
    # 注册后，任何未捕获的异常都会变成统一结构的 JSON，而不是一坨 HTML 错误页
    register_exception_handlers(app)

    # ---------------- 请求 ID：串联同一次请求的所有日志 ----------------
    # 每个请求分配一个唯一 ID，写进日志、也放进响应头 X-Request-ID。
    # 用户报错时只要提供这个 ID，就能在日志里精确捞出整条链路。
    @app.middleware("http")
    async def attach_request_id(request: Request, call_next) -> Response:
        # 前端若自己传了 X-Request-ID 就沿用（便于前后端串联），否则新生成一个
        rid = request.headers.get("X-Request-ID") or new_request_id()
        set_request_id(rid)
        # contextualize 让本次请求内的所有日志自动带上 request_id
        with logger.contextualize(request_id=rid):
            response: Response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    # ---------------- 业务路由 ----------------
    # 所有 /api/v1/... 的接口都由 api_router 汇总，便于版本管理：
    # 将来若要发布不兼容的 v2，只需再加一个 router，v1 继续可用。
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    # ---------------- 系统级探针 ----------------
    @app.get("/", tags=["系统"], summary="服务根路径")
    async def root() -> dict[str, str]:
        """确认服务活着，并告诉你怎么进图形界面。

        这个接口**不查数据库、不查向量库**（所以用 `async def` 也不会阻塞事件循环），
        等价于一个"门牌号"：真正的组件健康检查在 `/health`。
        """
        # 简单确认服务活着；这里不查数据库，所以用 async def 也不会有阻塞问题
        return {
            "code": "OK",
            "message": f"{settings.APP_NAME} 正在运行",
            "console": "/console",
            "docs": "/docs",
        }

    @app.get(
        "/health",
        tags=["系统"],
        summary="健康检查",
        response_model=HealthResponse,
    )
    def health() -> HealthResponse:
        """健康检查：实时探测各个外部依赖。

        注意这里用的是 `def` 而不是 `async def`：
        因为 check_connection() 是同步阻塞的数据库调用，若写在 async def 里会卡住
        整个事件循环；用 def 定义时 FastAPI 会自动把它放进线程池执行。
        （详见 app/api/deps.py 顶部的说明）
        """
        db_status = check_connection()
        # 向量库这里用 probe=False（轻量检查）：/health 会被频繁调用，
        # 如果每次都做一次真实的嵌入推理，开销太大。真实的嵌入推理
        # 已经在应用启动时做过一次，也可以通过 POST /api/v1/system/vector-store/selftest 主动触发。
        vs_status = check_vector_store(probe=False)
        components = {
            "database": ComponentStatus(
                status=db_status["status"], detail=db_status
            ),
            "vector_store": ComponentStatus(
                status=vs_status["status"], detail=vs_status
            ),
        }
        # 数据库和向量库任一异常，就认为服务已降级
        overall = (
            "ok"
            if db_status["status"] == "ok" and vs_status["status"] == "ok"
            else "degraded"
        )

        return HealthResponse(
            status=overall,
            app=settings.APP_NAME,
            version=settings.APP_VERSION,
            env=settings.APP_ENV,
            timestamp=datetime.now(timezone.utc),
            components=components,
        )

    # ---------------- 可视化控制台（前端）----------------
    # 把 web/ 目录挂成静态站点，浏览器打开 /console 就能用图形界面操作所有功能。
    #
    # 为什么不用 npm / 构建工具？
    #   前端是纯原生 HTML + CSS + ES Module，没有任何依赖，
    #   让 FastAPI 直接托管静态文件是最省事的做法：
    #   不用装 Node、不用打包、改完刷新页面就生效。
    #   将来要套进 Electron 时，这份代码可以原样复用。
    web_dir = Path(__file__).resolve().parents[1] / "web"
    if web_dir.is_dir():
        # html=True：访问 /console 时自动返回目录下的 index.html
        app.mount(
            "/console",
            NoCacheStaticFiles(directory=str(web_dir), html=True),
            name="console",
        )
        logger.info("可视化控制台已挂载: /console")
    else:  # pragma: no cover - 目录缺失时给出提示，但不影响接口使用
        logger.warning("未找到前端目录 {}，/console 将不可用", web_dir)

    return app


app = create_app()
