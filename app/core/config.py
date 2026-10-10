"""应用配置模块。

所有配置项集中在此处声明，取值优先级为：

    环境变量  >  .env 文件  >  代码默认值

敏感信息（数据库密码、JWT 密钥、API Key 加密密钥）只写进 .env，
而 .env 已被 .gitignore 忽略，不会进入版本库。

==================== ★ 为什么所有配置项都带 HNE_ 前缀？====================
HNE = Hetero Narrative Engine。给环境变量加命名空间前缀是生产环境的通用做法。

原因是踩过一个真实的坑：本项目最初把配置项命名为 DEFAULT_LLM_BASE_URL、HOST、
PORT、DEBUG 这类「通用名」。结果发现运行环境里恰好存在同名的环境变量（值为空），
而环境变量的优先级高于 .env，于是 .env 里认真填好的地址被一个空字符串
**静默覆盖**了 —— 不报错，只是配置莫名其妙不生效，排查起来极易跑偏。

这类通用名在真实部署中到处都可能撞车：CI 系统、Docker、IDE、其他 CLI 工具、
云平台注入的变量…… 加前缀之后，只有形如 HNE_XXX 的变量才会被读取，
意外撞车的概率基本归零。

因此约定：**.env 里所有键名、以及部署时手工设置的环境变量，都必须带 HNE_ 前缀。**
例如写 HNE_MYSQL_PASSWORD，而不是 MYSQL_PASSWORD。

==================== 为什么用 pydantic-settings？====================
如果直接用 os.getenv("MYSQL_PORT") 读取，拿到的永远是字符串 "3306"，
而且配置文件里写错一个键名、或者漏填某项，程序要到运行时才炸。
pydantic-settings 帮我们做了三件事：

  1. 自动把字符串转成正确的类型（"3306" -> 3306，"true" -> True）
  2. 启动时就校验配置，缺了必填项立刻报错，而不是等用到才发现
  3. 给 IDE 补全和类型提示，写 settings.MYSQL_ 时会自动列出所有可选项

用法：在需要配置的地方调用 get_settings()，不要自己 new Settings()。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：app/core/config.py -> app/core -> app -> 项目根
BASE_DIR: Path = Path(__file__).resolve().parents[2]

# 明显的占位符：出现即说明用户没有填写真实值
_PLACEHOLDERS: set[str] = {
    "",
    "CHANGE_ME",
    "CHANGE_ME_GENERATE_A_RANDOM_SECRET",
    "CHANGE_ME_FERNET_KEY",
}


def _resolve_path(raw: str) -> Path:
    """把配置里的相对路径解析为基于「数据根目录」的绝对路径。

    数据根目录的优先级（这是「单机桌面应用」的关键设计）：

        1. 环境变量 **HNE_DATA_DIR** —— 桌面壳（Electron）用它把全部可变数据
           （SQLite 文件、向量库、日志）指到 userData 下，与安装目录彻底分开。
           这样「卸载重装不丢数据」才成立，而且程序可以装在只读位置。
        2. **项目根目录** —— 开发态与脚本（pytest / init_db.py）的行为，
           与引入本机制之前**完全一致**，不会悄悄换地方。

    ★ 为什么读 os.environ 而不是把它做成 Settings 字段：
      做成字段的话 `data_dir` 属性会变成 `self.DATA_DIR`，而「哪些路径以它为基准」
      需要在字段校验阶段就知道 —— 会引出「字段求值顺序」这类隐性依赖。
      直接在解析函数里读环境变量，语义最简单：**它只是相对路径的基准点**。
    """
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    root = os.environ.get("HNE_DATA_DIR", "").strip()
    base = Path(root).expanduser().resolve() if root else BASE_DIR
    return (base / path).resolve()


def data_root() -> Path:
    """当前数据根目录（给「SQLite 文件放哪」这类需要显式拼接的地方用）。"""
    root = os.environ.get("HNE_DATA_DIR", "").strip()
    return Path(root).expanduser().resolve() if root else BASE_DIR


class Settings(BaseSettings):
    """全局配置对象。所有字段均可通过同名环境变量覆盖。"""

    # model_config 是 pydantic 的「元配置」，用来告诉它去哪里读配置、怎么读
    model_config = SettingsConfigDict(
        # ★ 命名空间前缀：只读取 HNE_XXX 形式的变量，避免与系统里其它同名变量撞车。
        #   加了前缀后，字段 MYSQL_HOST 对应的环境变量是 HNE_MYSQL_HOST。
        #   详细原因见本文件顶部的「为什么所有配置项都带 HNE_ 前缀」。
        env_prefix="HNE_",
        # .env 文件的绝对路径（BASE_DIR 已由上面的代码算好，不受启动目录影响）
        env_file=BASE_DIR / ".env",
        # .env 采用 UTF-8 编码，否则中文注释/中文密码会乱码
        env_file_encoding="utf-8",
        # 环境变量名不区分大小写：HNE_MYSQL_PORT 与 hne_mysql_port 都能匹配
        case_sensitive=False,
        # .env 里存在但本类没声明的键，直接忽略而不报错。
        # 这样分阶段扩展配置时（比如第 3.3 步新增 CHROMA_* 项），不会导致启动失败。
        extra="ignore",
    )

    # ==================== 应用基础 ====================
    APP_NAME: str = "HeteroNarrativeEngine"
    APP_VERSION: str = "0.2.0"
    APP_ENV: Literal["development", "testing", "production"] = "development"
    DEBUG: bool = True
    HOST: str = "127.0.0.1"
    # Field(...) 用来给字段加约束和说明：
    #   default=8000  默认值
    #   ge=1 / le=65535  取值范围（ge = greater or equal，le = less or equal）
    # 若 .env 里写了 PORT=70000，启动时就会报错，而不是等到监听端口失败才发现
    PORT: int = Field(default=8000, ge=1, le=65535)
    API_V1_PREFIX: str = "/api/v1"
    # 前端来源白名单（后续 HTML/CSS/JS 与 Electron 使用）
    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
        ]
    )

    # ==================== 安全 / JWT ====================
    SECRET_KEY: str = "dev-only-insecure-secret-key-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=60, ge=1)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, ge=1)
    BCRYPT_ROUNDS: int = Field(default=12, ge=4, le=16)
    # 用于加密用户自配的 LLM API Key（Fernet，44 位 base64）
    API_KEY_ENCRYPTION_KEY: str = ""

    # ==================== 数据库后端选择 ====================
    # ★ 默认 sqlite：本项目是**单机桌面应用**，用户不该为了用它先去装一个数据库服务器。
    #   sqlite  : 数据存成**一个文件**（默认 <数据目录>/data/app.sqlite3），
    #             无需安装任何服务、无需填任何连接信息，装完即用。
    #   mysql   : 保留为**可选**后端，给"已经有 MySQL / 想用 MySQL"的用户。
    #             在 .env 里写 HNE_DB_BACKEND=mysql 即可切回去，其余代码无需改动。
    #
    # ★ 两者的取舍（写清楚，避免以后误以为 sqlite 什么都能顶）：
    #   · SQLite 是**单写者**模型：同一时刻只允许一个写事务。单机单用户完全够用，
    #     但如果将来要做"一个服务端多人共用"，必须回到 MySQL。
    #   · 因此 MySQL 路径**必须一直保留可用**，不能被删掉、也不能无人测试
    #     （tests/test_db_mysql.py + 环境变量开关守着它）。
    #
    # ★★ 旧配置必须留在 MySQL 上（见下面 _infer_db_backend 的校验器）：
    #    老版本只有 MySQL 一种后端，所以老向导写出来的 `.env` **没有**
    #    `HNE_DB_BACKEND` 这一行。引入 sqlite 默认值之后，那种配置会
    #    "字段默认值生效" → 被**静默**换到一份空的 SQLite 库上，
    #    用户看到的现象是"我原来的账号密码登不进去了"（实测踩到，见 §34）。
    DB_BACKEND: Literal["sqlite", "mysql"] = "sqlite"
    # SQLite 数据文件路径。留空表示用默认值 <数据根目录>/data/app.sqlite3；
    # 填相对路径时以数据根目录为基准（见 _resolve_path）。
    SQLITE_PATH: str = ""
    # SQLite 写锁等待时间（毫秒）。桌面应用写并发极低，10 秒足够；
    # 设成 0 会让"恰好同时写"直接抛 "database is locked"，对用户表现为随机报错。
    SQLITE_BUSY_TIMEOUT_MS: int = Field(default=10000, ge=0)

    # ==================== MySQL（可选后端） ====================
    # ★ 这两个的默认值刻意留空，而不是 127.0.0.1 / narrative_app：
    #   它们被用来判断"这份配置是不是一份 MySQL 配置"（见 _infer_db_backend）。
    #   留空之后，只有**真的写过 MySQL 键**的配置才会被判成 MySQL 配置。
    MYSQL_HOST: str = ""
    MYSQL_PORT: int = Field(default=3306, ge=1, le=65535)
    MYSQL_USER: str = ""
    MYSQL_PASSWORD: str = ""
    MYSQL_DB: str = "narrative_engine"
    MYSQL_CHARSET: str = "utf8mb4"
    # 连接池（SQLAlchemy QueuePool）
    DB_POOL_SIZE: int = Field(default=10, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=20, ge=0)
    DB_POOL_TIMEOUT: int = Field(default=30, ge=1)
    DB_POOL_RECYCLE: int = Field(default=3600, ge=-1)
    DB_ECHO: bool = False

    # ==================== ChromaDB 向量库 ====================
    CHROMA_PERSIST_DIR: str = "./data/chroma"
    CHROMA_COLLECTION_PREFIX: str = "narrative"

    # ---- 嵌入后端选择 ----
    # onnx_default: 用 ChromaDB 内置的 ONNX MiniLM-L6-v2 模型做本地推理（免费、可离线）
    # api         : 调用用户自配 API 的 /embeddings 接口（中文效果通常更好，但产生费用）
    EMBEDDING_BACKEND: Literal["onnx_default", "api"] = "onnx_default"
    EMBEDDING_MODEL_NAME: str = "all-MiniLM-L6-v2"
    # 向量维度，供 onnx_default 后端使用（该内置模型固定为 384 维）
    EMBEDDING_DIMENSION: int = Field(default=384, ge=0)
    # 向量维度，供 api 后端使用。填 0 表示「首次调用时自动探测」，
    # 因为不同厂商的嵌入模型维度不同（bge-m3 是 1024，text-embedding-3-small 是 1536...）
    EMBEDDING_API_DIMENSION: int = Field(default=0, ge=0)
    # 一次请求最多嵌入多少条文本。API 后端需要分批，避免单次请求体过大被拒
    EMBEDDING_BATCH_SIZE: int = Field(default=64, ge=1, le=2048)

    # ---- API 嵌入后端专用（EMBEDDING_BACKEND=api 时才读取）----
    EMBEDDING_API_BASE_URL: str = ""
    EMBEDDING_API_KEY: str = ""

    # ==================== 默认 LLM（可选兜底） ====================
    DEFAULT_LLM_BASE_URL: str = ""
    DEFAULT_LLM_API_KEY: str = ""
    DEFAULT_LLM_MODEL: str = ""
    # 默认模型的上下文窗口总容量（输入 + 输出），仅在诊断接口里使用
    DEFAULT_LLM_CONTEXT_WINDOW: int = Field(default=65536, ge=512)
    LLM_REQUEST_TIMEOUT: int = Field(default=120, ge=1)
    LLM_MAX_RETRIES: int = Field(default=3, ge=0)

    # ==================== 剧情滚动总结（分层合并） ====================
    #: 开关。开着才能在长对话里"记得住前情"；每一次合并会**多花一次模型调用**，
    #: 所以用户可以关掉（关掉后退回"只在超预算时做本地压缩"的老行为）。
    SUMMARY_ENABLED: bool = True
    #: 到点**自动**总结（会花一次模型调用）。★ 默认 **false**：
    #: 用户明确要求"总结与否由用户自己决定、不强制（要花 token）"，
    #: 所以默认只弹横幅提醒，等他点「立即总结」。想自动的用户在面板里打开。
    SUMMARY_AUTO_ENABLED: bool = False
    #: 每积累多少「轮」做一次合并总结（轮 = 一问一答）。
    #: 例：10 → 第 1~10 轮合成一份；到第 20 轮时把"旧总结 + 11~20 轮"合并成
    #: 一份新的（标记 1~20 轮），**旧总结被替换而不是叠加**，所以不会重复占 token。
    SUMMARY_BLOCK_ROUNDS: int = Field(default=8, ge=2, le=100)
    #: 单份总结的字符上限（≈ token 数；超了就掐中间留两头）
    SUMMARY_MAX_CHARS: int = Field(default=2000, ge=200, le=20000)

    # ==================== 日志 ====================
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./data/logs"

    # -------------------- 校验器 --------------------
    # field_validator 会在「配置值被读入之后」再做一次加工或校验。
    # 注意 @classmethod 是固定写法，不能省略。

    @model_validator(mode="before")
    @classmethod
    def _infer_db_backend(cls, data: Any) -> Any:
        """**旧配置必须继续用 MySQL** —— 不让"新默认值"把老用户静默换库。

        ==================== 为什么需要它（实测的事故）====================
        默认后端从"只有 mysql"改成"默认 sqlite"之后：
          · 老版本向导写的 `.env` 里**没有** `HNE_DB_BACKEND` 这一行
            （那时只有一种选择，没必要写）；
          · 于是 pydantic 的字段默认值 `sqlite` 生效；
          · 结果：老用户升级后**被静默换到一份空的 SQLite 库**，
            打开应用看到的是"用户名或密码错误" —— 他会以为账号被人改了。
        （真实发生：见 docs/handoff.md §34。数据一条没丢，但表现极具误导性。）

        ==================== 为什么必须在 `mode="before"` 做 ====================
        第一版写在 `mode="after"` 里、判据用 `model_fields_set` —— **错的**：
        pydantic 会把「从 `.env` 文件读到的键」也记进 `model_fields_set`，
        而老配置里**每个** MySQL 键都来自 `.env`，于是"用户显式设过"恒为真，
        推断永远不会触发（这个错误是实测抓到的：显式写 `HNE_DB_BACKEND=sqlite`
        的零配置被推断成了 mysql）。

        在 before 阶段拿到的 `data` 就是**原始输入**（环境变量 + `.env` + 调用方
        显式传的），所以"键在不在里面"恰好回答"用户到底有没有表过态"。

        ★★ 注意 `data` 里的键是**裸字段名**（`DB_BACKEND` / `MYSQL_HOST`），
           **不带 `HNE_` 前缀** —— `env_prefix` 是"读环境变量时"用的，
           before 阶段拿到的已经是剥掉前缀之后的设置键名。
           第一版按 `HNE_DB_BACKEND` 去查，于是"键永远不在" → 后面又去取
           `HNE_MYSQL_HOST`（同样取不到）→ **推断静默失效**，一切照旧。
           （两处键名都错，而且不报错，只有实测才发现。）

        ==================== 判据（与壳侧 resolvedBackend 同一套语义）====================
        只在 `DB_BACKEND` **完全没出现**、且输入里**确实有 MySQL 连接信息**
        （Host 或 User 非空）时，才推断成 mysql。
        """
        if isinstance(data, dict) and "DB_BACKEND" not in data:
            host = str(data.get("MYSQL_HOST") or "").strip()
            user = str(data.get("MYSQL_USER") or "").strip()
            if host or user:
                data = dict(data)
                data["DB_BACKEND"] = "mysql"
        return data

    @field_validator("API_V1_PREFIX")
    @classmethod
    def _normalize_api_prefix(cls, value: str) -> str:
        """容错处理：无论用户写 "api/v1" 还是 "/api/v1/"，都规范成 "/api/v1"。

        这样即使 .env 里格式写得随意，也不会出现 "//api/v1//ping" 这种坏路径。
        """
        value = value.strip()
        if not value.startswith("/"):
            value = f"/{value}"
        return value.rstrip("/") or "/api/v1"

    @field_validator("LOG_LEVEL")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        """统一转成大写，避免写 "info" 时 loguru 认不出来。"""
        return value.strip().upper()

    @field_validator("MYSQL_CHARSET")
    @classmethod
    def _normalize_charset(cls, value: str) -> str:
        """字符集留空时兜底为 utf8mb4（中文存储必须用它，不能退回 latin1）。"""
        return value.strip() or "utf8mb4"

    # -------------------- 派生属性 --------------------
    # 这些用 @property 装饰的方法不是配置项，而是「根据配置算出来的值」。
    # 好处是只写一次拼接逻辑，别处直接用 settings.mysql_url 即可，
    # 不会出现「这里拼对了、那里拼错了」的问题。

    @property
    def is_production(self) -> bool:
        """是否运行在生产环境（生产环境会启用更严格的校验）。"""
        return self.APP_ENV == "production"

    @property
    def is_sqlite(self) -> bool:
        """当前是否使用 SQLite 后端（多处分支判断都读它，避免各写各的字符串比较）。"""
        return self.DB_BACKEND == "sqlite"

    @property
    def sqlite_file(self) -> Path:
        """SQLite 数据文件的绝对路径（默认 <数据根目录>/data/app.sqlite3）。"""
        raw = self.SQLITE_PATH.strip() or "./data/app.sqlite3"
        return _resolve_path(raw)

    @property
    def database_url(self) -> str:
        """当前后端的 SQLAlchemy 连接串（engine 只认这一个入口）。

        SQLite 用 `sqlite+pysqlite:///绝对路径`：三个斜杠 + 绝对路径是 SQLAlchemy
        的固定写法（Windows 上写成 `sqlite:///E:\\dir\\app.sqlite3` 也能被正确识别）。
        """
        if self.is_sqlite:
            return f"sqlite+pysqlite:///{self.sqlite_file.as_posix()}"
        return self.mysql_url

    @property
    def database_label(self) -> str:
        """给日志/健康检查看的可读名字（不含任何口令）。"""
        return f"sqlite:{self.sqlite_file}" if self.is_sqlite else f"mysql:{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}"

    @property
    def mysql_url(self) -> str:
        """MySQL 的连接串（含数据库名）。

        ★ 注意：**不要直接用这个属性去建 Engine** —— 用 `database_url`
          （它会按 `DB_BACKEND` 分派）。这里保留它是因为 `init_db.py` 导出
          schema.sql、以及排障时想看一眼拼出来的串。
        """
        return (
            f"mysql+pymysql://{quote_plus(self.MYSQL_USER)}:{quote_plus(self.MYSQL_PASSWORD)}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}"
            f"?charset={self.MYSQL_CHARSET}"
        )

    @property
    def mysql_server_url(self) -> str:
        """不含数据库名的 MySQL 连接串，用于建库前探测服务是否可达。

        ★ 只在 `DB_BACKEND=mysql` 时有意义（sqlite 没有"服务端"这个概念）。
        """
        return (
            f"mysql+pymysql://{quote_plus(self.MYSQL_USER)}:{quote_plus(self.MYSQL_PASSWORD)}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/?charset={self.MYSQL_CHARSET}"
        )

    @property
    def chroma_dir(self) -> Path:
        """ChromaDB 持久化目录（绝对路径）。"""
        return _resolve_path(self.CHROMA_PERSIST_DIR)

    @property
    def log_dir(self) -> Path:
        """日志目录（绝对路径）。"""
        return _resolve_path(self.LOG_DIR)

    # -------------------- 行为方法 --------------------
    def ensure_dirs(self) -> None:
        """确保运行期需要的数据目录存在。"""
        directories = [self.chroma_dir, self.log_dir]
        # SQLite 模式下还要保证数据文件所在目录存在 —— sqlite3 只会报
        # "unable to open database file"，不会替我们把父目录建出来。
        if self.is_sqlite:
            directories.append(self.sqlite_file.parent)
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def validate_runtime(self) -> list[str]:
        """检查关键配置是否可用，返回告警列表（不抛异常，供启动时打印）。

        生产环境若存在告警，直接抛出 ConfigurationError，避免"带病上线"。

        ★ 关于两个密钥：它们在启动流程里会被 `app.db.bootstrap.ensure_secrets()`
          **自动生成并落盘**（在调用本方法之前），所以正常情况下这里不会报警。
          仍然保留这两条检查，是因为本方法也可能被"只想看看配置"的脚本单独调用 ——
          那时若确实没配，如实说出来比默默返回空列表更有用。

        ★ `MYSQL_PASSWORD` 只在 `DB_BACKEND=mysql` 时才是问题：
          默认的 sqlite 后端压根不连 MySQL，报"MySQL 连接池无法建立连接"
          是纯误导（用户会去找一个他根本不需要装的软件）。
        """
        problems: list[str] = []

        if self.SECRET_KEY in _PLACEHOLDERS or len(self.SECRET_KEY) < 32:
            problems.append("SECRET_KEY 未设置或长度不足 32 位，JWT 签名不安全")

        if self.API_KEY_ENCRYPTION_KEY in _PLACEHOLDERS:
            problems.append("API_KEY_ENCRYPTION_KEY 未设置，用户自配的 LLM API Key 无法加密存储")

        if not self.is_sqlite and self.MYSQL_PASSWORD in _PLACEHOLDERS:
            problems.append("MYSQL_PASSWORD 未设置，MySQL 连接池将无法建立连接")

        if self.is_production and problems:
            from app.core.exceptions import ConfigurationError

            raise ConfigurationError(
                "生产环境配置校验失败", detail=problems
            )

        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局唯一配置实例（带缓存，避免重复解析 .env）。"""
    return Settings()
