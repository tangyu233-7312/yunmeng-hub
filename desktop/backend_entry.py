#!/usr/bin/env python
"""桌面版的**后端入口**：给 PyInstaller 打的独立可执行文件当入口。

==================== 为什么需要它（而不是直接打 app.main:app） ====================
uvicorn 可以直接 `--factory` 之类地吃 `app.main:app`，但打包成 exe 之后有两件事
必须由**入口脚本**来做，命令行参数做不到：

1. **先把环境变量设好，再 import app**。
   本项目的配置是 `pydantic-settings`，在 **import 期**就把 `.env` / 环境变量读进
   `Settings` 对象了。如果先 import `app.main` 再设 `HNE_CHROMA_PERSIST_DIR`，
   那设置**不会生效**（而且不报错，只是数据被写到别处）——
   这就是本项目反复强调的"静默不生效"，必须从结构上避免。

2. **指定本地向量模型的缓存目录**。
   ChromaDB 的 ONNX 嵌入模型默认缓存在用户目录（`~/.cache/chroma`），
   打包版要把它指到 userData 下，否则"卸载重装不丢数据"对它不成立。

==================== 与后端的契约 ====================
**完全不改 `app/`**：这里只是"启动方式"的另一个入口，接口、SSE、探针断言都不受影响。
命令行参数与 uvicorn 同名同义（`--host` / `--port`），另外加了两个桌面版专用参数。

用法：
    backend.exe --host 127.0.0.1 --port 8000
    backend.exe --host 127.0.0.1 --port 51234 --data-dir C:\\...\\云梦枢 --log-dir C:\\...\\logs
    backend.exe --self-check --port 51999     # 起服务→探活→自我退出（给打包/CI 用）
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path


def _app_root() -> Path:
    """仓库根（含 `app/` 与 `web/` 的那一层）。

    · 打包后：PyInstaller 把数据解开到 `sys._MEIPASS`，`web/` 就在它下面；
      而 `app/` 是作为 Python 包收进去的，`app.main` 用 `Path(__file__).parents[1] / "web"`
      定位前端目录 —— 在冻结环境里 `__file__` 也会落在 `_MEIPASS` 下，所以两者一致。
    · 开发态：本文件在 `desktop/`，所以仓库根是它的上一级。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def _ensure_repo_on_path(root: Path) -> None:
    """开发态直接 `python desktop/backend_entry.py` 时，保证 `import app` 能找到。"""
    text = str(root)
    if text not in sys.path:
        sys.path.insert(0, text)


def _load_env_file(path: Path) -> int:
    """把 .env 读进进程环境（**已存在的环境变量优先**，与 pydantic-settings 一致）。

    ★ 为什么打包版必须自己读 `.env`：
      开发态用 `python -m uvicorn` 时，pydantic-settings 会从**当前工作目录**读 `.env`；
      而打包后的 backend.exe 不会站在仓库根上（壳给它的 cwd 是资源目录），
      于是"配置没被读到"—— 表现是 MySQL 连不上，而 .env 明明就在那儿。
      所以这里显式读一次，用 `setdefault` 保证不覆盖真正来自环境变量的值。

    ★ 这个解析器刻意做得**很笨**（只认 KEY=VALUE、跳过注释、去掉成对引号）：
      复杂语法一旦理解错就会静默把值读歪，而"读歪了"比"没读到"更难查。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return 0

    loaded = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def configure_environment(args: argparse.Namespace) -> dict[str, str]:
    """**在 import app 之前**把环境变量定下来，并回显"实际生效的值"。

    ★ 回显是刻意做的：这条路径上最容易出的事故就是"我设了，但它没生效"。
      把最终值打出来，日志里就能直接看到真相，而不用去猜。
    """
    applied: dict[str, str] = {}

    def set_env(key: str, value: str) -> None:
        os.environ[key] = value
        applied[key] = value

    # 先读 .env（真正来自环境变量的值优先）
    env_files: list[Path] = []
    if args.env_file:
        env_files.append(Path(args.env_file))
    else:
        env_files.append(_app_root() / ".env")
    for env_file in env_files:
        loaded = _load_env_file(env_file)
        if loaded:
            print(f"[backend] 已从 {env_file} 读入 {loaded} 项配置（不覆盖已有环境变量）", flush=True)

    if args.data_dir:
        data_dir = Path(args.data_dir).resolve()
        chroma_dir = data_dir / "chroma"
        # 向量库实体（长期记忆）—— 必须在 import app 之前设好
        set_env("HNE_CHROMA_PERSIST_DIR", str(chroma_dir))

    if args.log_dir:
        set_env("HNE_LOG_DIR", str(Path(args.log_dir).resolve()))

    if args.host:
        set_env("HNE_HOST", args.host)
    if args.port:
        set_env("HNE_PORT", str(args.port))

    # 打包版的 sys.executable 是 exe 自己，不能被当成 Python 解释器去用
    set_env("HNE_DESKTOP_FROZEN", "1")

    return applied


#: ChromaDB 的 `ONNXMiniLM_L6_V2.DOWNLOAD_PATH` —— **写死在源码里的**：
#:     Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
#: ★ 它**不读任何环境变量**（我们实测确认过：模块里既没有 os.environ 也没有 os.getenv）。
#:   所以"把模型缓存指到 userData"这件事**做不到**，只能把模型放到它认的这个位置。
#:   这决定了打包策略：模型随包带，首启时"安装"到该位置（见 ensure_bundled_model）。
_DEFAULT_MODEL_DIR = Path.home() / ".cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
_CHROMA_MODEL_DIR = _DEFAULT_MODEL_DIR
_CHROMA_MODEL_SENTINEL = ("onnx", "model.onnx")


def use_model_cache_dir(path: str | None) -> str | None:
    """把模型缓存位置临时改到别处（**只给自检用**）。

    ★ 为什么自检需要一个不同于默认的目录：自检是"构建流水线的一部分"，
      它不该往开发者的 `~/.cache` 里写东西（尤其当那次构建是要验证
      "把自带模型安装到目标位置"这条逻辑时，写进真缓存就污染了环境）。
      默认路径在正常启动时**一定要用**（因为 ChromaDB 只认它），
      所以这是一个显式开关，而不是默认行为。
    """
    global _CHROMA_MODEL_DIR
    if not path:
        return None
    _CHROMA_MODEL_DIR = Path(path).resolve()
    return str(_CHROMA_MODEL_DIR)


def ensure_bundled_model() -> str | None:
    """如果包里带了 ONNX 模型，就把它"安装"到 ChromaDB 认的那个固定位置。

    返回一句人话说明（做了什么 / 为什么不需要做），由调用方打印。

    ★ 为什么需要这一步：ChromaDB 首次使用会自己联网下载模型（约 90MB）。
      那对"双击即用"的桌面软件是不合适的 —— 用户可能在没网的环境里第一次打开。
      所以打包版**默认带上模型**，启动时放到它认的位置；已经存在就什么都不做。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None

    bundled = Path(meipass) / "model-cache" / "chroma" / "onnx_models" / "all-MiniLM-L6-v2"
    if not bundled.is_dir():
        return "包里没有自带 ONNX 模型：首次使用某张角色的长期记忆时，ChromaDB 会联网下载（约 90MB）"

    already = _CHROMA_MODEL_DIR.joinpath(*_CHROMA_MODEL_SENTINEL)
    if already.is_file():
        return f"ONNX 模型已在 {_CHROMA_MODEL_DIR}，无需安装"

    try:
        _CHROMA_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(bundled, _CHROMA_MODEL_DIR, dirs_exist_ok=True)
    except OSError as exc:
        # 装不上只降级（还有"自己下载"这条路），但必须**说清楚**，不能装作没事
        return f"自带模型安装失败（{exc}）：将回退为联网下载，首次使用可能需要等待"

    return f"已把自带 ONNX 模型安装到 {_CHROMA_MODEL_DIR}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backend",
        description="云梦枢后端（桌面版入口）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，只本机）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--data-dir", default=None,
                        help="数据目录（向量库与模型缓存都会放在它下面）；不传则用 .env / 默认值")
    parser.add_argument("--log-dir", default=None, help="日志目录；不传则用 .env / 默认值")
    parser.add_argument("--env-file", default=None,
                        help="要读的 .env 路径；不传则用资源根目录下的 .env（已存在的环境变量优先）")
    parser.add_argument("--log-level", default=None, help="uvicorn 日志级别（默认 info）")
    parser.add_argument("--self-check", action="store_true",
                        help="启动后自己探活一次，成功/失败都以退出码体现（打包与 CI 用）")
    parser.add_argument("--self-check-timeout", type=float, default=120.0,
                        help="--self-check 的最长等待秒数")
    parser.add_argument("--model-cache-dir", default=None,
                        help="把 ONNX 模型缓存位置临时改到这里（只给自检用，避免污染开发者的 ~/.cache）")
    return parser.parse_args(argv)


def run_server(args: argparse.Namespace) -> int:
    """起 uvicorn（正常模式：阻塞直到被要求退出）。"""
    import uvicorn

    from app.main import app

    level = args.log_level or os.environ.get("HNE_LOG_LEVEL", "info").lower()
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=level if level in {"critical", "error", "warning", "info", "debug", "trace"} else "info",
    )
    return 0


def run_self_check(args: argparse.Namespace) -> int:
    """起服务 → **深度**探活 → 汇报 → 自己退出（不留给调用方一个跑着的进程）。

    ★ 为什么不能只看 HTTP 200（本轮真踩了）：
      本项目的 `/health` 在**组件降级时依然返回 200**（这是有意的设计 ——
      服务活着，但里面的数据库/向量库可能不可用）。
      第一版自检只判状态码，于是"MySQL 连不上 + 向量库初始化失败"的后端
      也被判成"自检通过"。那正是本项目最反对的**静默降级**：
      打包出来的东西看着是好的，用户一用才发现是坏的。
      所以这里逐项检查 `components.*.status`，任何一项不是 ok 就**失败**并把原因打出来。
    """
    import threading

    import httpx
    import uvicorn

    from app.main import app

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    # ★ daemon 线程：即使 uvicorn 的退出流程卡住，也不挡着我们 os._exit
    thread = threading.Thread(target=server.run, name="self-check-server", daemon=True)
    thread.start()

    url = f"http://{args.host}:{args.port}/health"
    deadline = time.time() + args.self_check_timeout
    last = "（还没探到）"
    payload: dict | None = None

    while time.time() < deadline:
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url)
            if resp.status_code == 200:
                payload = resp.json()
                break
            last = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - 探活失败的种类很多，如实打印即可
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1.0)

    if payload is None:
        print(f"[self-check] FAILED  {url} 在 {args.self_check_timeout:.0f}s 内未就绪；最后一次：{last}",
              flush=True)
        _shutdown(server, thread)
        return 1

    # ---- 深度检查：组件状态必须都是 ok ----
    components = payload.get("components") or {}
    bad = {
        name: (info or {}).get("status")
        for name, info in components.items()
        if (info or {}).get("status") != "ok"
    }

    print(f"[self-check] {url} -> 200，顶层状态={payload.get('status')}", flush=True)
    for name, info in components.items():
        status = (info or {}).get("status")
        detail = (info or {}).get("detail")
        print(f"[self-check]   组件 {name}: {status}", flush=True)
        if status != "ok" and detail:
            print(f"[self-check]     原因：{detail}", flush=True)

    if bad or payload.get("status") != "ok":
        print(f"[self-check] FAILED  服务起来了但组件不可用：{bad or payload.get('status')}", flush=True)
        print("[self-check] 提示：自检时若用了临时 --data-dir，请用 --env-file 指向真实 .env，"
              "否则数据库口令读不到，这里必然失败。", flush=True)
        _shutdown(server, thread)
        return 1

    print("[self-check] OK  所有组件均为 ok", flush=True)
    _shutdown(server, thread)
    return 0


def _shutdown(server, thread) -> None:
    """请求 uvicorn 退出并等它真的结束。

    ★ 这里踩过一个很难查的坑（实测抓到）：原版在 `should_exit` 之后只 join 15 秒，
      超时就 `os._exit(0)`。而 **PyInstaller 的 exe 是"bootloader 父进程 + 真身子进程"**：
      子进程被硬杀时，bootloader 会把退出码报成 **-1**（而不是我们想要的 0）。
      于是 `--self-check` 明明所有组件都 ok，调用方看到的却是失败 ——
      而且它只在"打包产物 + 有人看退出码"时出现，在开发态完全正常。
      修法：有序关闭（先关 lifespan/socket，再 join），并把 join 预算放宽到 30 秒；
      只有真的关不掉才强杀（那时至少要留下日志）。
    """
    server.should_exit = True
    if getattr(server, "force_exit", None) is not None:
        server.force_exit = True
    thread.join(timeout=30)
    if thread.is_alive():
        print("[self-check] 警告：uvicorn 未在 30s 内退出，强制结束进程", flush=True)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，日志里有中文与 ★ 之类的字符 → 直接崩。
    # 必须在任何输出之前改掉。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):  # pragma: no cover - 老环境
            pass

    args = parse_args(argv)
    root = _app_root()
    _ensure_repo_on_path(root)

    applied = configure_environment(args)

    print(f"[backend] 入口={Path(sys.executable).name} 冻结={bool(getattr(sys, '_MEIPASS', None))}", flush=True)
    print(f"[backend] 资源根目录={root}", flush=True)
    print(f"[backend] 监听 {args.host}:{args.port}", flush=True)
    for key, value in applied.items():
        print(f"[backend] 环境变量 {key}={value}", flush=True)
    if not applied:
        print("[backend] 未注入任何环境变量（沿用 .env / 默认值）", flush=True)

    # ★ 顺序：先把缓存位置改好（如果有），再安装自带模型 —— 否则会先往默认位置装一遍
    use_model_cache_dir(args.model_cache_dir)
    model_note = ensure_bundled_model()
    if model_note:
        print(f"[backend] 本地嵌入模型：{model_note}", flush=True)

    if args.self_check:
        return run_self_check(args)
    return run_server(args)


if __name__ == "__main__":
    # ★ 用 os._exit 而不是 sys.exit：PyInstaller 的 exe 里，
    #   `sys.exit` 会走 Python 的解释器关闭流程，而我们的自检刚跑过 uvicorn
    #   （线程/事件循环的残留）—— 那一段可能让**退出码变得不可预期**（实测出现过 -1）。
    #   退出码是 `--self-check` 的唯一契约，必须绝对可靠。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(main())
