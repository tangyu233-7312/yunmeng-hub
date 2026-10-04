"""pytest 的全局夹具与**环境隔离**。

==================== ★ 为什么这个文件很关键 ====================
在这个文件出现之前，测试直接读仓库根的 `.env`（里面是真实的 MySQL 连接信息）。
引入 SQLite 之后，默认后端变成了 sqlite，于是有两件事必须由测试入口强制定下来，
否则会出现"跑一次测试就往仓库里塞一个数据库文件 / 一份自动生成的密钥"：

1. **数据根目录**（`HNE_DATA_DIR`）指到 `.pytest-data/`。
   SQLite 文件、向量库、日志、以及自举生成的 `config/.secrets.env` 全都落在这里，
   仓库根保持干净（该目录已在 .gitignore 里）。
   ★ 不指走的话，`ensure_secrets()` 会往仓库根的 `config/` 里写密钥文件 ——
     而这是个**公开仓库**，多出一个密钥文件是绝对不能接受的事故。

2. **数据库后端**默认 `sqlite`。
   效果是"clone 下来 `pytest` 就能跑"，不需要先装一个 MySQL 服务
   （这正是"安装即用"在开发侧的对应物）。
   想验证 MySQL 那条路径的人，显式指定后端即可：
       $env:HNE_DB_BACKEND='mysql'; .\\.venv\\Scripts\\python.exe -m pytest -q

★ 关于 `.env` 的优先级：pydantic-settings 的规则是「环境变量 > .env」，
  所以这里用 `os.environ.setdefault` 设置的值会**盖住** .env 里的同名项 ——
  但也只盖这两项，其余配置（LLM、嵌入后端等）仍照常从 .env 读，
  与引入本文件之前的行为保持一致。
"""

from __future__ import annotations

import os
from pathlib import Path

# 仓库根：tests/conftest.py -> tests -> 仓库根
REPO_ROOT = Path(__file__).resolve().parents[1]

#: 测试数据根目录（SQLite 文件 / 向量库 / 日志 / 自举密钥都在它下面）
TEST_DATA_DIR = REPO_ROOT / ".pytest-data"

# ★ 这几行必须在**任何 app 模块被导入之前**执行：
#   app.core.config 里的路径解析以 HNE_DATA_DIR 为基准点 —— 一旦某个模块
#   先建好了 Engine、或读了日志目录，之后再改就太晚了。
os.environ.setdefault("HNE_DATA_DIR", str(TEST_DATA_DIR))
os.environ.setdefault("HNE_DB_BACKEND", "sqlite")
# 显式给出库文件路径：这样"测试用的库"与"开发者平时用的库"物理上就是两个文件
os.environ.setdefault("HNE_SQLITE_PATH", str(TEST_DATA_DIR / "data" / "pytest.sqlite3"))

# 日志降噪：测试期间不写 DEBUG 噪音（pytest.ini 也设了 log_level，这里管的是 loguru）
os.environ.setdefault("HNE_LOG_LEVEL", "WARNING")

# ★ 测试专用密钥：**绝不使用仓库 .env 里的真实值**。
#   为什么必须固定成"一眼就是测试用的"：
#     · 否则测试会拿开发者真实的 JWT 密钥签发令牌、用真实的 Fernet 密钥加密测试数据 ——
#       即便只是内存里的，也没有任何理由让真实密钥参与测试。
#     · 固定值还让"同一份断言可重复"，不会因为密钥每次随机而出现偶发差异。
#   下面这把 Fernet 密钥是把 32 字节的 0x2a 全部 base64 后的结果（合法且明显是假的）：
_TEST_SECRET_KEY = "test-only-secret-key-0123456789-abcdefghijklmnopqrstuvwxyz"
_TEST_FERNET_KEY = "KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio="

os.environ["HNE_SECRET_KEY"] = _TEST_SECRET_KEY
os.environ["HNE_API_KEY_ENCRYPTION_KEY"] = _TEST_FERNET_KEY


def pytest_report_header(config) -> str:  # noqa: ANN001 - pytest 钩子签名
    """在 pytest 头部打印"这次跑的是哪个后端、数据落在哪"。

    ★ 刻意打印：本项目的教训是"配置没生效却不报错"最难查 ——
      比如你以为在测 MySQL，实际测的是 SQLite（或反过来）。
      把生效值摆在最前面，一眼就能看穿。
    """
    backend = os.environ.get("HNE_DB_BACKEND", "sqlite")
    return f"HNE 测试环境: DB_BACKEND={backend} | DATA_DIR={os.environ.get('HNE_DATA_DIR')}"


def _verify_isolation() -> None:
    """★ 硬门禁：确认测试真的跑在隔离环境里，而不是连上了真实数据。

    这是"防呆"而不是"流程约定"：如果有人以后把上面的 setdefault 挪走、
    或者在别处提前 import 了 app 导致配置被定死，测试就会**悄悄**
    连上开发者的真实数据库和真实密钥目录 ——
    那可能删掉真实数据（因为测试里到处都有"建用户再删用户"）。
    这种事故必须在第一时间以异常形式炸出来，而不是靠人记得检查。

    注意本函数在模块导入时执行，此时 Settings 还没被实例化，
    所以直接清缓存后读，读到的就是我们刚设好的环境变量。
    """
    from app.core.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    if settings.DB_BACKEND != "sqlite":
        return  # 显式要求测 MySQL 的情形：数据在后端里，不算"污染仓库"
    resolved = settings.sqlite_file.resolve()
    expected_root = TEST_DATA_DIR.resolve()
    assert expected_root in resolved.parents, (
        f"测试的 SQLite 库文件不在隔离目录内：{resolved}（期望位于 {expected_root} 之下）。"
        "请检查 tests/conftest.py 顶部的 HNE_DATA_DIR / HNE_SQLITE_PATH 是否失效。"
    )


_verify_isolation()
