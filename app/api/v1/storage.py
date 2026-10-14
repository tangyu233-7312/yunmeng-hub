"""存储设置接口：查看当前存储方式、测试连接、写入 `.env`。

==================== ★ 这是本项目第一个"会写配置文件"的接口 ====================
在此之前，`app/` 里**一处文件写入都没有** —— 配置只能由用户拿编辑器改。现在把它
做成界面操作，就必须额外守住几条，否则修好"门槛高"会换来更坏的东西。

==================== ★★ 为什么**不需要登录**（第二轮改）====================
第一版要求登录。结果它修不了它本该修的那个问题：

    用户的真实困境是"**登录不上**，因为连错了库"。
    而登录不上 = 拿不到令牌 = 进不去这个页面 ⇒ **死锁**。
    （同理，桌面壳的「切换存储方式」菜单一直是登录前就能用的。）

所以现在它**登录前可用**。代价是没有身份凭证了，因此必须靠**别的**闸门，
下面这几道一个都不能少（都写在 `_guard()` 里）：

1. **只允许本机来源**（`request.client.host`）。服务默认只监听 127.0.0.1，
   但用户完全可能改成 `HNE_HOST=0.0.0.0` 或在前面挂反代 —— 那一刻只有这条站着。
2. **校验 `Host` 头** —— 防 **DNS rebinding**：
   攻击者把 `evil.com` 解析到 127.0.0.1，浏览器会当它是**同源**、不受 CORS 约束，
   于是能直接读写这个接口。那种请求的 `Host` 是 `evil.com`，而本机正常访问是
   `127.0.0.1` / `localhost` —— 一比就能拦下。
3. **要求自定义头 `X-HNE-Storage`**。带自定义头的跨源请求**必须先发预检（OPTIONS）**，
   而本项目的 CORS 白名单里没有攻击者的源 ⇒ 预检失败，请求根本发不出去。
   ★ 代价：用 `/docs` 的「Try it out」调这几个接口要手动加这个头，否则 403。

★ 接口**不会**自己重启后端：浏览器管不到 uvicorn 进程。
  写入后返回 `restart_required=True`，由前端把"去重启"这件事讲清楚。

==================== 残余风险（如实写下来）====================
本机上的**任何进程**都能直接改用户的 `.env`（它就是用户机器上的一个普通文件），
所以"本机可达"本身就约等于"本机可信"。这三道闸挡的是**浏览器里的网页**
（尤其是被诱导访问的恶意页面），不是本机的其它程序。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Request

from app.core.exceptions import ForbiddenError
from app.schemas.common import ApiResponse
from app.schemas.storage import (
    ConnectionTestResult,
    SqliteInspect,
    StorageState,
    StorageUpdate,
    StorageWriteResult,
)
from app.services import storage_service

router = APIRouter()

#: 允许的本机来源地址（IPv6 的环回、以及 Windows 上可能出现的 ::ffff:127.0.0.1）
_LOCAL_HOSTS = frozenset(
    {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1", "0:0:0:0:0:0:0:1"}
)

#: 允许的 `Host` 头主机名（防 DNS rebinding，见模块顶部第 2 条）
_DEFAULT_ALLOWED_HOST_NAMES = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _allowed_host_names() -> frozenset[str]:
    """允许的 Host 主机名集合 = 默认本机名 ∪ `HNE_STORAGE_ALLOWED_HOSTS`。

    ★ 为什么留这个逃生口：若用户用**主机名**访问本机（例如 `http://mypc:8000`），
      客户端 IP 仍是 127.0.0.1（过得了第 1 道闸），但 Host 是 `mypc` ——
      只有默认白名单的话会被 403 挡住，而那时用户**没有任何办法自救**
      （页面本身就是用来排障的，把它锁死会很讽刺）。
      所以给一个显式开关：`HNE_STORAGE_ALLOWED_HOSTS=mypc,其他名`。
      ★ 默认**空**，即默认就是最严的那一组。
    """
    raw = os.environ.get("HNE_STORAGE_ALLOWED_HOSTS", "")
    extra = {item.strip().lower() for item in raw.split(",") if item.strip()}
    return _DEFAULT_ALLOWED_HOST_NAMES | extra

#: 必须携带的自定义请求头（见模块顶部第 3 条）。HTTP 头名大小写不敏感，
#: 但 FastAPI/Starlette 的 headers 用起来是小写键。
REQUIRED_HEADER = "x-hne-storage"


def _guard(request: Request) -> None:
    """本接口的准入检查（**无登录**，所以三道闸都要过）。

    ★ 三种拒绝各自给出**不同的 reason 与 hint**：含糊的 403 会让人以为
      "接口坏了"，而这三种情形的处理方式完全不同（换台机器 / 换 Host / 加头）。
    """
    # ---- 1. 来源必须是本机 ----
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    if host not in _LOCAL_HOSTS:
        raise ForbiddenError(
            "存储设置只允许在本机操作",
            detail={
                "reason": "not_local",
                "client": host,
                "hint": "这个接口会改写数据库配置，因此不开放给远程访问。请在本机的浏览器里操作。",
            },
        )

    # ---- 2. Host 头必须是本机名（防 DNS rebinding）----
    raw_host = (request.headers.get("host") or "").strip()
    if raw_host.startswith("["):  # IPv6 形如 [::1]:8000
        hostname = raw_host.split("]")[0] + "]"
    else:
        hostname = raw_host.split(":")[0]
    if hostname and hostname.lower() not in _allowed_host_names():
        raise ForbiddenError(
            "请求的 Host 不是本机地址，已拒绝",
            detail={
                "reason": "bad_host",
                "host": raw_host,
                "hint": "这是防 DNS rebinding 的检查：只接受通过 127.0.0.1 / localhost 访问。",
            },
        )

    # ---- 3. 自定义头（让跨源请求必须先过预检）----
    if not request.headers.get(REQUIRED_HEADER):
        raise ForbiddenError(
            "缺少必要的请求头，已拒绝",
            detail={
                "reason": "missing_header",
                "header": REQUIRED_HEADER,
                "hint": "浏览器端由页面自动带上；用 /docs 调试时请手动加上这个请求头。",
            },
        )


@router.get("/storage", response_model=ApiResponse[StorageState], summary="查看当前存储方式")
def read_storage(request: Request) -> ApiResponse[StorageState]:
    """当前用的是哪种存储、配置写在哪个文件、哪些键被环境变量钉住。

    ★ **登录前可用** —— 登录不上时正是最需要看它的时候。
    """
    _guard(request)
    return ApiResponse.ok(StorageState(**storage_service.read_state()), message="存储状态")


@router.post(
    "/storage/inspect",
    response_model=ApiResponse[SqliteInspect],
    summary="换路径前体检：这个路径里有账号/数据吗",
)
def inspect_storage_path(payload: dict, request: Request) -> ApiResponse[SqliteInspect]:
    """看一眼某个本机文件路径里有什么（**只读，不会建文件**）。

    ★ 为什么单独做一个接口：用户改路径前就该知道"这里是不是空库"，
      而不是保存 + 重启之后才发现"我的账号不见了"
      （真实发生：他以为是"你把我的密码改了"）。
    """
    _guard(request)
    result = storage_service.inspect_sqlite(str(payload.get("path") or ""))
    return ApiResponse.ok(SqliteInspect(**result), message=result.get("summary") or "")


@router.post(
    "/storage/test",
    response_model=ApiResponse[ConnectionTestResult],
    summary="用提交的参数试连一次（不落盘）",
)
def test_storage(payload: StorageUpdate, request: Request) -> ApiResponse[ConnectionTestResult]:
    """保存前的连通性测试：**不写任何文件**，只在内存里连一次。

    这样"填错了"会在写文件之前被拦住，而不是等到重启后起不来。
    """
    _guard(request)
    result = storage_service.test_connection(payload.model_dump(exclude_none=True))
    return ApiResponse.ok(ConnectionTestResult(**result), message=result.get("message", ""))


@router.put(
    "/storage",
    response_model=ApiResponse[StorageWriteResult],
    summary="保存存储方式（写入 .env，需重启后端生效）",
)
def write_storage(payload: StorageUpdate, request: Request) -> ApiResponse[StorageWriteResult]:
    """把存储配置写进 `.env`。

    ★ 只动白名单里的键、写前备份、原子替换、写完读回复核（见 service 层）。
    ★ **不自动重启**：返回 `restart_required=True`，请前端如实告诉用户。
    """
    _guard(request)
    result = storage_service.write_config(payload.model_dump(exclude_none=True))
    return ApiResponse.ok(StorageWriteResult(**result), message=result["message"])
