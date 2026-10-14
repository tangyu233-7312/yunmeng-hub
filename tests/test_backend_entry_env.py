"""`desktop/backend_entry.py` 的 `configure_environment` 契约测试。

==================== 为什么值得单测 ====================
它是**打包版后端的启动入口**：壳传进来的 `--data-dir` / `--log-dir` 在这里被翻译成
`HNE_DATA_DIR` / `HNE_CHROMA_PERSIST_DIR` / `HNE_LOG_DIR`。出错的形态全是**静默**的：

  · 用户明明在向导里把数据目录改到了 D 盘，实际却还用旧位置 ——
    因为这里曾经用 `set_env()` **无条件覆盖**（本轮实测确认过）；
  · 数据根改了、派生的向量库/日志路径却仍按旧的 userData 拼 ——
    于是"库在 D 盘、长期记忆在 C 盘"，用户搬家只搬走一半，且不报任何错。

所以这里把三条规则钉死：用户显式设过的不碰；没设过才给默认；
给了默认时要跟着**生效的数据根**走。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "desktop" / "backend_entry.py"

#: 这些变量会被 configure_environment 读写，每条用例前后都要清干净
_ENV_KEYS = (
    "HNE_DATA_DIR",
    "HNE_CHROMA_PERSIST_DIR",
    "HNE_LOG_DIR",
    "HNE_HOST",
    "HNE_PORT",
    "HNE_DESKTOP_FROZEN",
)


@pytest.fixture()
def entry(monkeypatch):
    """以模块方式载入 desktop/backend_entry.py（它不是包的一部分）。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    spec = importlib.util.spec_from_file_location("hne_backend_entry", ENTRY)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hne_backend_entry"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def env_file(tmp_path) -> Path:
    """一份**不存在**的 `.env` 路径。

    ★ 必须显式传给 `configure_environment`：否则它会回落去读**仓库根**那份
    真实 `.env`，把开发者的实际配置读进 `os.environ`，让用例的隔离失效
    （本轮实测：仓库 `.env` 里的 `HNE_CHROMA_PERSIST_DIR` 就这样渗进来了）。
    """
    return tmp_path / "nonexistent" / ".env"


def _args(module, env_file, **kwargs):
    """造一份 argparse.Namespace。

    ★ **必须显式给 `env_file`**：`configure_environment` 在没有 `--env-file`、
    也没有 `HNE_ENV_FILE` 时会回落去读 `<仓库根>/.env` ——
    那是开发者**真实的配置**（本轮它里面就有 `HNE_CHROMA_PERSIST_DIR`），
    会把仓库的真实值读进 `os.environ`，让用例的断言看似"产品写错了"。
    这正是测试隔离问题，所以这里统一指向一个**不存在**的路径。
    """
    base = {
        "data_dir": None,
        "log_dir": None,
        "host": None,
        "port": None,
    }
    base.update(kwargs)
    base["env_file"] = str(env_file)
    return module.argparse.Namespace(**base)


# ------------------------------------------------------------------
#  常规路径：用户什么都没设 → 壳的 --data-dir 说了算（行为与改动前一致）
# ------------------------------------------------------------------
def test_默认路径下_壳的_data_dir_决定数据根与向量库(entry, tmp_path):
    user_data_dir = tmp_path / "userData" / "data"
    user_data_dir.mkdir(parents=True)

    entry.configure_environment(_args(entry, env_file, data_dir=str(user_data_dir)))

    # 数据根 = <userData>\data 的父级（这就是"少一层 data"那条约定）
    assert os.environ["HNE_DATA_DIR"] == str(tmp_path / "userData")
    assert os.environ["HNE_CHROMA_PERSIST_DIR"] == str(user_data_dir / "chroma")


# ------------------------------------------------------------------
#  ★★ 本轮修的那条：用户显式设过就不许被 --data-dir 盖掉
# ------------------------------------------------------------------
def test_用户显式设过数据根时_不许被_data_dir_覆盖(entry, tmp_path, monkeypatch):
    """这是本轮修的**静默失效**：向导里改了数据目录，壳却把它盖回去。

    覆盖的表现：界面上保存成功、日志也正常，数据却仍然落在旧位置 ——
    用户不会看到任何错误，只会觉得"我明明搬过"。
    """
    user_data_dir = tmp_path / "userData" / "data"
    user_data_dir.mkdir(parents=True)
    monkeypatch.setenv("HNE_DATA_DIR", "D:\\我自己的数据")

    entry.configure_environment(_args(entry, env_file, data_dir=str(user_data_dir)))

    assert os.environ["HNE_DATA_DIR"] == "D:\\我自己的数据", (
        "用户在 .env/.env 里显式设的数据根必须赢过壳传的 --data-dir"
    )


def test_用户改过数据根时_不许再写死旧的向量库路径(entry, tmp_path, monkeypatch):
    """★ 派生路径必须跟着生效的数据根走，否则"搬家只搬一半"。

    曾经的做法是无条件 `HNE_CHROMA_PERSIST_DIR = <userData>\\data\\chroma`：
    用户把数据根改到 D 盘后，库文件去了 D 盘、长期记忆却还留在 C 盘 ——
    功能上能跑、**不报任何错**，但用户备份/搬家时只会带走一半。
    """
    user_data_dir = tmp_path / "userData" / "data"
    user_data_dir.mkdir(parents=True)
    monkeypatch.setenv("HNE_DATA_DIR", "D:\\我自己的数据")

    entry.configure_environment(_args(entry, env_file, data_dir=str(user_data_dir)))

    assert "HNE_CHROMA_PERSIST_DIR" not in os.environ, (
        "用户自定义了数据根、却没单独指定向量库时，应交给后端按新数据根推导，"
        "不能在这里写死旧 userData 下的路径"
    )


def test_用户单独指定过向量库_原样保留(entry, tmp_path, monkeypatch):
    """逐键尊重：改了数据根**又**单独写了向量库的，以他写的为准。"""
    user_data_dir = tmp_path / "userData" / "data"
    user_data_dir.mkdir(parents=True)
    monkeypatch.setenv("HNE_DATA_DIR", "D:\\data")
    monkeypatch.setenv("HNE_CHROMA_PERSIST_DIR", "E:\\我指定的向量库")

    entry.configure_environment(_args(entry, env_file, data_dir=str(user_data_dir)))

    assert os.environ["HNE_CHROMA_PERSIST_DIR"] == "E:\\我指定的向量库"


def test_日志目录同理_用户显式设过就不覆盖(entry, tmp_path, monkeypatch):
    monkeypatch.setenv("HNE_LOG_DIR", "D:\\我的日志")
    entry.configure_environment(_args(entry, env_file, log_dir=str(tmp_path / "logs")))
    assert os.environ["HNE_LOG_DIR"] == "D:\\我的日志"


def test_用户改过数据根时_日志不写死旧位置(entry, tmp_path, monkeypatch):
    """与向量库同一条理由：日志也该跟着数据根走，而不是留在旧 userData。"""
    monkeypatch.setenv("HNE_DATA_DIR", "D:\\data")
    entry.configure_environment(_args(entry, env_file, log_dir=str(tmp_path / "userData" / "logs")))
    assert "HNE_LOG_DIR" not in os.environ, "改了数据根就别再把日志写死在旧目录"


def test_没改数据根时_日志仍按壳给的目录(entry, tmp_path):
    """反面：默认路径下行为要一字不变（显式给出，不依赖后端拼相对路径）。"""
    log_dir = tmp_path / "userData" / "logs"
    entry.configure_environment(_args(entry, env_file, log_dir=str(log_dir)))
    assert os.environ["HNE_LOG_DIR"] == str(log_dir)


# ------------------------------------------------------------------
#  组合：数据根 + 日志 + 端口一起给（真实启动就是这样）
# ------------------------------------------------------------------
def test_默认组合下三个变量都按壳的意图落地(entry, tmp_path):
    user_data_dir = tmp_path / "userData" / "data"
    user_data_dir.mkdir(parents=True)
    log_dir = tmp_path / "userData" / "logs"

    entry.configure_environment(_args(
        entry, env_file,
        data_dir=str(user_data_dir), log_dir=str(log_dir), host="127.0.0.1", port=8123,
    ))

    assert os.environ["HNE_DATA_DIR"] == str(tmp_path / "userData")
    assert os.environ["HNE_CHROMA_PERSIST_DIR"] == str(user_data_dir / "chroma")
    assert os.environ["HNE_LOG_DIR"] == str(log_dir)
    assert os.environ["HNE_HOST"] == "127.0.0.1"
    assert os.environ["HNE_PORT"] == "8123"
    assert os.environ["HNE_DESKTOP_FROZEN"] == "1"
