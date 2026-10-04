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

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote_plus

from pydantic import Field, field_validator
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
    """把配置里的相对路径解析为基于项目根目录的绝对路径。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


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
    APP_VERSION: str = "0.1.0"
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

    # ==================== MySQL ====================
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = Field(default=3306, ge=1, le=65535)
    MYSQL_USER: str = "narrative_app"
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
    def mysql_url(self) -> str:
        """SQLAlchemy 连接串（含数据库名）。密码做 URL 转义，避免特殊字符破坏连接串。"""
        return (
            f"mysql+pymysql://{quote_plus(self.MYSQL_USER)}:{quote_plus(self.MYSQL_PASSWORD)}"
            f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DB}"
            f"?charset={self.MYSQL_CHARSET}"
        )

    @property
    def mysql_server_url(self) -> str:
        """不含数据库名的连接串，用于建库前探测服务是否可达。"""
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
        for directory in (self.chroma_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def validate_runtime(self) -> list[str]:
        """检查关键配置是否可用，返回告警列表（不抛异常，供启动时打印）。

        生产环境若存在告警，直接抛出 ConfigurationError，避免"带病上线"。
        """
        problems: list[str] = []

        if self.SECRET_KEY in _PLACEHOLDERS or len(self.SECRET_KEY) < 32:
            problems.append("SECRET_KEY 未设置或长度不足 32 位，JWT 签名不安全")

        if self.API_KEY_ENCRYPTION_KEY in _PLACEHOLDERS:
            problems.append("API_KEY_ENCRYPTION_KEY 未设置，用户自配的 LLM API Key 无法加密存储")

        if self.MYSQL_PASSWORD in _PLACEHOLDERS:
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
