"""存储设置（数据库后端）的请求 / 响应模型。

★ 这里刻意**不定义**"口令回传"的字段：读接口只回 `secret_set`（布尔），
  与桌面端 `preload.js` 的做法一致 —— 少一份"把口令读回渲染进程"的风险。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class StorageUpdate(BaseModel):
    """保存存储配置。**只列白名单里的键**，其余字段一律忽略。"""

    model_config = {"extra": "ignore"}

    HNE_DB_BACKEND: Literal["sqlite", "mysql"] = Field(..., description="存储方式")
    HNE_SQLITE_PATH: str | None = Field(default=None, description="本机文件路径（可留空）")
    HNE_MYSQL_HOST: str | None = Field(default=None)
    HNE_MYSQL_PORT: str | None = Field(default=None)
    HNE_MYSQL_USER: str | None = Field(default=None)
    #: 留空 = 沿用已保存的口令（**绝不覆盖成空**，否则老配置会连不上）
    HNE_MYSQL_PASSWORD: str | None = Field(default=None)
    HNE_MYSQL_DB: str | None = Field(default=None)
    #: ★ 换到"空库/不存在"的本机文件路径时，后端会先拒绝并要求确认
    #:   （用户真实踩过：换完路径登录不上，以为是自己密码被改了）。
    #:   前端在用户看过警告并点"确定"之后，把这一项置 true 重发。
    confirm_empty: bool = Field(
        default=False, description="已确认目标路径里没有数据，仍要换过去"
    )


class SqliteInspect(BaseModel):
    """某个本机文件路径的体检结果（换路径前的预警）。"""

    path: str
    exists: bool
    size: int
    has_tables: bool
    user_count: int
    usernames: list[str]
    sessions: int
    messages: int
    error: str | None = None
    #: ok（有账号）/ warn（空库、不存在、读不出）
    level: str
    warning: str | None = None
    summary: str


class PathHistoryItem(BaseModel):
    """历史用过的本机文件路径（帮助用户找回旧数据在哪）。"""

    path: str
    last_used: str | None = None
    exists: bool
    user_count: int
    usernames: list[str] = []
    summary: str


class StorageState(BaseModel):
    """当前存储状态（给「存储设置」页渲染）。"""

    backend: str
    database: str
    env_file: str
    default_sqlite_path: str
    values: dict[str, str]
    #: 口令之类只报"设没设"
    secret_set: dict[str, bool]
    #: 被真实环境变量钉住的键（写 .env 不会生效 —— 必须让用户看到）
    pinned: list[str]
    #: 每个键当前的值来自哪里：env / file / default
    sources: dict[str, str]
    #: 当前这份本机文件的体检（换路径前用得上）
    current_sqlite: SqliteInspect | None = None
    #: 最近用过的路径（最多 3 条，新的在前）
    sqlite_history: list[PathHistoryItem] = []


class StorageWriteResult(BaseModel):
    env_file: str
    backup: str | None = None
    changed: list[str]
    backend: str
    #: 恒为 true：本进程不会自己重启，得用户动手
    restart_required: bool
    message: str
    #: 保存后的路径历史（让界面立刻能显示"原来那份在哪"）
    sqlite_history: list[PathHistoryItem] = []


class ConnectionTestResult(BaseModel):
    ok: bool
    message: str
    backend: str | None = None
    path: str | None = None
    server: str | None = None
    hint: str | None = None
    errors: dict[str, Any] | None = None
