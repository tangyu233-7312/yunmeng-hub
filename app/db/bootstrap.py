"""首次运行自举：**让用户什么都不用配就能跑起来**。

==================== 这个模块解决什么问题 ====================
在本模块出现之前，用户要启动后端必须先手工准备两样东西：

1. `HNE_SECRET_KEY`（JWT 签名）与 `HNE_API_KEY_ENCRYPTION_KEY`（Fernet，加密用户自配的
   LLM API Key）—— 没填就只有一个"配置告警"，真正用到时（登录、保存 API Key）
   才炸，而且报错离原因很远。
2. **数据库里的表** —— `Base.metadata.create_all()` 原本只出现在
   `scripts/init_db.py` / `scripts/migrate_db.py` 里，运行时**从不建表**。
   于是"用户连上一个空库能通过自检、一注册就报 table doesn't exist"。

这两件事对"双击安装即用"的桌面软件都是不可接受的 ——
用户凭什么要为了用你的软件先去读一篇部署文档？

==================== 设计要点（每一条都是为了不产生新的坑）====================

* **只补缺失的，绝不覆盖已存在的。**
  用户在 `.env` 里认真写过的值永远优先。这里只处理三种"等于没配"的情况：
  空字符串、已知占位符、非法的 Fernet 密钥。

* **口令写进独立文件，不碰用户的 `.env`。**
  落在 `<数据目录>/config/.secrets.env`（打包态即 `%APPDATA%\\云梦枢\\config\\`）。
  为什么不直接改写 `.env`：那个文件是**用户自己编辑的**，我们在里面插入/重排内容
  既有覆盖用户改动的风险，也会让"这一行为什么变了"变得无从追溯。
  独立文件只有一个来源、一个用途，语义干净。
  ★ 桌面壳的首启向导本来就往这个 `config\\` 目录写 `.env`，两个文件同目录、互不干扰。

* **`os.environ` 是本进程内的生效通道。**
  pydantic-settings 的优先级是「环境变量 > .env」，所以把自举出来的值写进
  `os.environ` 就能生效。已存在（且非占位符）的环境变量**不覆盖**。

* **绝不能"顺手"重新生成密钥。**
  `API_KEY_ENCRYPTION_KEY` 一旦改变，用户已经存进数据库的 API Key 密文就**永远解不开**
  （`app/core/security.py` 会报"密文无法解密"）。
  所以生成逻辑只认"空/占位符/非法"三种情况，形态合法的密钥一律原样沿用 ——
  哪怕它是用户手写的、看起来乱七八糟的。
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from loguru import logger

from app.core.config import data_root, get_settings
from app.db.base import Base

#: 已知占位符（与 app/core/config.py 的 _PLACEHOLDERS 同义）。
#: 这里**故意重复一份**而不是 import 那个私有集合：两处的用途不同 ——
#: config 那边是"发出配置告警"，这边是"决定要不要自动生成"。
#: 把两者绑在一起会让"只想改告警文案"变成"改动了自举行为"，风险更大。
_PLACEHOLDER_VALUES = {
    "",
    "CHANGE_ME",
    "CHANGE_ME_GENERATE_A_RANDOM_SECRET",
    "CHANGE_ME_FERNET_KEY",
    "dev-only-insecure-secret-key-change-me",
}

#: 自举生成的口令写在这里（与用户自己编辑的 `.env` 分开存放）
SECRETS_FILENAME = ".secrets.env"

_SECRETS_HEADER = """\
# 云梦枢自动生成的本机密钥 —— **请勿删除、请勿提交到版本库**
#
# 这个文件由后端在首次运行时自动创建（见 app/db/bootstrap.py）。
# 里面是 JWT 签名密钥与 API Key 加密密钥，用户不需要理解、也不需要修改。
#
# ★ 删掉它的后果：
#     · HNE_SECRET_KEY         重新生成 —— 已登录的用户需要重新登录（仅此而已）
#     · HNE_API_KEY_ENCRYPTION_KEY 重新生成 —— **已保存的 LLM API Key 将无法解密**，
#       需要到「模型配置」里把那把 Key 重新填一次。
#   所以除非确实想重置，否则别动它。
"""


def secrets_file() -> Path:
    """自举口令文件的路径：`<数据目录>/config/.secrets.env`。

    数据目录的判定复用 `app.core.config` 的同一套规则（`HNE_DATA_DIR` 优先，
    否则项目根），这样打包态（userData）与开发态（仓库根）各归各的，不会串。
    """
    return data_root() / "config" / SECRETS_FILENAME


def parse_env_text(text: str) -> dict[str, str]:
    """解析极简 KEY=VALUE（与桌面壳 `backend-config.js` 的解析器保持同一套规则）。

    ★ 刻意做得很笨：只认 KEY=VALUE、跳过注释、去掉成对引号。
      复杂的 .env 语法一旦理解错，就会**静默**把值读歪 —— 那比"读不到"更难查。
    """
    out: dict[str, str] = {}
    for raw_line in text.replace("\ufeff", "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def _write_secrets(values: dict[str, str]) -> None:
    """把生成的口令落盘（0600 权限，能设就设）。

    ★ 为什么不用 `os.chmod` 失败就报错：Windows 上 chmod 基本是空操作，
      强行报错只会让"生成口令"这件本该无感的事变成启动失败。失败就算了。
    """
    target = secrets_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [_SECRETS_HEADER]
    for key, value in values.items():
        lines.append(f"{key}={value}\n")
    # 先写临时文件再替换：避免"写到一半断电"留下半截文件（那会让下次启动读不到密钥）
    tmp = target.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(target)
    try:
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - Windows 上通常不生效，属预期
        pass


def _is_blank_or_placeholder(value: str) -> bool:
    """这个值是不是"等于没配"？"""
    return value.strip() in _PLACEHOLDER_VALUES


def _is_valid_fernet_key(value: str) -> bool:
    """是不是一个形态合法的 Fernet 密钥（32 字节的 urlsafe base64）。

    ★ 为什么要判"合法"而不只判"非空"：`API_KEY_ENCRYPTION_KEY` 填错格式时，
      错误要等到用户第一次保存 API Key 才暴露（`Fernet(raw)` 抛异常）。
      自举阶段顺便判一次，能把"配错了"和"没配"归成同一类处理 ——
      都由我们生成一个能用的，而不是留一颗定时炸弹。
      ★ 只校验**长度与 base64 可解性**，不去验证内容；用户合法的自定义密钥
        一律保留（保持"最小干预"）。
    """
    text = value.strip()
    if len(text) != 44:
        return False
    try:
        raw = base64.urlsafe_b64decode(text.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    return len(raw) == 32


def generate_secret_key() -> str:
    """生成 JWT 签名密钥（48 字节 → base64url 字符串，长于校验要求的 32 位）。"""
    return secrets.token_urlsafe(48)


def generate_fernet_key() -> str:
    """生成 Fernet 密钥（32 字节 → urlsafe base64，44 字符）。

    与 `Fernet.generate_key()` 的输出**完全等价**（后者就是 `urlsafe_b64encode(os.urandom(32))`），
    这里手写是为了不引入 cryptography 依赖 —— 本模块在**任何** import app 的场合都会跑，
    包括那些只想读配置的轻量脚本。
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def ensure_secrets() -> dict[str, str]:
    """确保 `SECRET_KEY` / `API_KEY_ENCRYPTION_KEY` 可用，**必须在 `get_settings()` 之前调用**。

    流程：
        1. 读已落盘的自举口令 → 注入 `os.environ`（不覆盖真正的环境变量）
        2. 再看一眼"当前值是不是等于没配" → 是就生成、落盘、注入
        3. 清掉 `get_settings()` 的缓存，让新值**本次启动就生效**

    返回：本次**新生成**的键名（用于日志；值绝不打印）。
    """
    # ---- 1. 载入上次生成的 ----
    path = secrets_file()
    loaded: dict[str, str] = {}
    if path.is_file():
        try:
            loaded = parse_env_text(path.read_text(encoding="utf-8"))
        except OSError as exc:  # pragma: no cover - 权限/损坏等少见情况
            logger.warning("自举口令文件读取失败（将重新生成）：{}", exc)
            loaded = {}
    for key, value in loaded.items():
        # setdefault 语义：真正的环境变量 / .env 里已有的值优先
        os.environ.setdefault(key, value)

    # ---- 2. 补齐仍然缺失的 ----
    generated: dict[str, str] = {}
    settings = get_settings()

    if _is_blank_or_placeholder(settings.SECRET_KEY) or len(settings.SECRET_KEY.strip()) < 32:
        generated["HNE_SECRET_KEY"] = generate_secret_key()

    if _is_blank_or_placeholder(settings.API_KEY_ENCRYPTION_KEY) or not _is_valid_fernet_key(
        settings.API_KEY_ENCRYPTION_KEY
    ):
        generated["HNE_API_KEY_ENCRYPTION_KEY"] = generate_fernet_key()

    # ---- 3. 落盘 + 注入 + 让本次启动就用上 ----
    if generated:
        # 合并写回：保留文件里已有的键，避免"这次只生成一个"把另一个丢掉
        merged = {**loaded, **generated}
        _write_secrets(merged)
        for key, value in generated.items():
            os.environ[key] = value
        # ★ 必须清缓存：Settings 是 lru_cache 单例，在此之前可能已经被别处实例化过
        #   （例如导入链上的某个模块调用了 get_settings()）。
        #   不清的话"我们生成了密钥"与"后端实际用的是旧值"会同时成立 —— 又是一种静默不生效。
        get_settings.cache_clear()
        logger.info(
            "已自动生成并保存本机密钥：{}（文件：{}）",
            "、".join(sorted(generated)),
            path,
        )

    return generated


def ensure_schema() -> list[str]:
    """按 ORM 模型建表（**幂等**：已存在的表跳过），返回本次新建的表名。

    ==================== ★ 为什么运行时必须自己建表 ====================
    原先只有 `scripts/init_db.py` 会建表，运行时从不建。用户拿到的空库能通过
    "SELECT 1" 探活，但注册账号时会报表不存在 —— 这是最典型的
    "自检通过、实际不可用"。桌面应用没有"让用户去跑一个脚本"这条路径，
    所以启动时自建是唯一合理的选择。

    ==================== 为什么不用 Alembic 迁移 ====================
    桌面版**首次创建**数据库文件时 `create_all` 完全够用（此时表一定与模型一致）。
    需要谨慎的是"升级安装"——旧库缺新列。这一点由
    `scripts/migrate_db.py` 负责（它对 MySQL 走的是手写 ALTER），
    SQLite 侧的列级升级目前**尚未实现**：现在还是 0.x/POC 阶段，
    升级做法是"备份旧库文件 → 用新版本的 create_all 建新库 → 手工搬数据"，
    在 README 里如实写明，不装作已经支持。

    ★ 已存在的表**不会被修改**：create_all 只做 `CREATE TABLE IF NOT EXISTS`，
      它不会 ALTER 已有表，所以对旧库是安全的（不会破坏数据）。
    """
    # ★ 局部 import：本模块的"生成密钥"部分希望保持轻量（不拖进 SQLAlchemy），
    #   只有真正要建表时才需要引擎。
    from app.db.mysql import get_engine

    settings = get_settings()
    if settings.is_sqlite:
        Path(settings.sqlite_file).parent.mkdir(parents=True, exist_ok=True)

    engine = get_engine()
    existing = set(_table_names(engine))
    Base.metadata.create_all(bind=engine)
    created = sorted(set(Base.metadata.tables) - existing)
    if created:
        logger.info("已自动创建数据表 {} 张：{}", len(created), "、".join(created))
    return created


def _table_names(engine) -> list[str]:  # noqa: ANN001 - Engine 类型此处只需用到 inspect
    from sqlalchemy import inspect

    return list(inspect(engine).get_table_names())
