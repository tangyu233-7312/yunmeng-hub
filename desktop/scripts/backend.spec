# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包描述：把云梦枢后端打成**单个目录**的独立后端（sidecar）。

==================== 为什么是 onedir 而不是 onefile ====================
`--onefile` 每次启动都要把上百 MB 解压到临时目录，冷启动要多等好几秒 ——
对一个"双击就该起来"的桌面应用来说这是硬伤。onedir 直接就地执行，
而且出问题时能进去看看到底少了哪个文件。代价是"一个文件夹"而不是"一个 exe"，
对 electron-builder 的 extraResources 来说完全不是问题。

==================== 为什么要收集这么多东西 ====================
本项目是**重度动态导入**的应用（FastAPI/uvicorn/chromadb/onnxruntime 都靠运行时拼模块名），
PyInstaller 的静态分析必然漏。这里的做法是：
  · 依赖包自己的 hook（PyInstaller 自带 + hooks-contrib）先兜一遍；
  · 对已知会漏的包显式 `collect_all` / `collect_submodules`；
  · **前端 `web/` 目录必须带进去** —— `app/main.py` 用
    `Path(__file__).parents[1] / "web"` 找它，漏了就会"服务起来了但 /console 是 404"。
  · 本地 ONNX 嵌入模型（约 90MB）默认**不打进去**（见 build_backend.ps1 的 -IncludeModel），
    因为 ChromaDB 首次使用时自己能下载并缓存到我们指定的 userData 目录。

构建命令（不要直接调本文件，走脚本，它会把 venv 与路径都安排好）：
    pwsh -File desktop/scripts/build_backend.ps1
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# ----------------------------------------------------------------------
#  路径
# ----------------------------------------------------------------------
# SPECPATH 由 PyInstaller 注入 = 本文件所在目录（desktop/scripts）
SCRIPTS_DIR = Path(SPECPATH).resolve()
DESKTOP_DIR = SCRIPTS_DIR.parent
REPO_ROOT = DESKTOP_DIR.parent

ENTRY_SCRIPT = DESKTOP_DIR / "backend_entry.py"
WEB_DIR = REPO_ROOT / "web"

# ----------------------------------------------------------------------
#  收集：数据文件 / 二进制 / 隐藏导入
# ----------------------------------------------------------------------
datas = [
    # ★ 前端控制台（零构建产物，直接整目录带进去）
    (str(WEB_DIR), "web"),
]

# ★ 本地 ONNX 嵌入模型（约 90MB）：**默认随包带**。
#   为什么要带：ChromaDB 的模型缓存路径是**写死的** `~/.cache/chroma/...`，
#   改不了（见 desktop/backend_entry.py 的说明）。不带的话，用户第一次用到长期记忆时
#   必须联网下载 90MB —— 对一个"双击即用"的桌面软件不合适。
#   带上是这个形状：`model-cache/chroma/onnx_models/all-MiniLM-L6-v2`，
#   后端首启时把它"安装"到 ChromaDB 认的那个位置（已存在则跳过）。
_bundle_model_dir = os.environ.get("HNE_BUNDLE_MODEL_DIR")  # noqa: F821 - spec 运行在 PyInstaller 的命名空间里
if not _bundle_model_dir:
    raise SystemExit(
        "[spec] 错误：没有设置 HNE_BUNDLE_MODEL_DIR。\n"
        "        请通过 desktop/scripts/build_backend.ps1 构建（它会把模型缓存暂存好并设置该变量）；\n"
        "        要显式构建一个不带模型的产物，请用 -NoModel。"
    )

_model_src = Path(_bundle_model_dir) / "onnx_models"
if not _model_src.is_dir():
    raise SystemExit(f"[spec] 错误：{_model_src} 不存在，无法把模型打进产物")

datas.append((str(_model_src), "model-cache/chroma/onnx_models"))
_model_mb = sum(f.stat().st_size for f in _model_src.rglob("*") if f.is_file()) / (1024 * 1024)
print(f"[spec] 已加入本地 ONNX 模型：{_model_src}（{_model_mb:.1f} MB）")
binaries = []
hiddenimports = []

# 这些包的数据文件/二进制是运行期真正要读的（迁移脚本、模型配置、DLL 等），
# 静态分析看不见它们。★ 用 collect_all 一次拿齐三类，并**累加**到上面三个列表
# （第一版这里每次循环都从 globals() 里重新取，等于把上面收集到的覆盖掉了）。
#
# ★ 还要把"确定用不到、而且故意排除掉"的子树从 hidden 里剔出去：
#   `collect_all(onnxruntime)` 会把 `onnxruntime.transformers.*`（模型转换工具，
#   依赖 `onnx` 包，我们只做推理）也列进来，于是日志里刷几十条
#   `ERROR: Hidden import 'onnxruntime.transformers.xxx' not found`。
#   它不是失败，但会把真正的告警淹掉 —— 日志一旦不可读，告警就等于没有。
_DROP_SUBMODULE_PREFIXES = (
    "onnxruntime.transformers",
    "onnxruntime.quantization",
    "onnxruntime.training",
)


def _keep(module: str) -> bool:
    return not module.startswith(_DROP_SUBMODULE_PREFIXES)


for _package in ("chromadb", "onnxruntime", "tokenizers"):
    try:
        _datas, _binaries, _hidden = collect_all(_package)
        datas += _datas
        binaries += _binaries
        hiddenimports += [m for m in _hidden if _keep(m)]
        print(f"[spec] collect_all({_package})：datas={len(_datas)} binaries={len(_binaries)} hidden={len(_hidden)}")
    except Exception as _exc:  # noqa: BLE001 - 收集失败必须说清是哪个包
        print(f"[spec] 警告：collect_all({_package}) 失败：{_exc}")

# ----------------------------------------------------------------------
#  收集：隐藏导入（运行期动态拼出来、静态分析必然漏的）
# ----------------------------------------------------------------------
hiddenimports += [
    # uvicorn：循环导入协议实现与日志配置，靠名字动态加载
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 我们的应用（配置 / 建表 / 数据模型都在 import 期要做事的路径上）
    "app",
    # 本项目数据库层
    "pymysql",
    "sqlalchemy.dialects.mysql",
    "sqlalchemy.dialects.mysql.pymysql",
    # 加密 / JWT（可选 C 加速，缺失时上层有回退，但带上更稳）
    "bcrypt",
    "jwt",
    "cryptography",
    # chromadb 的嵌入实现与遥测
    "chromadb.telemetry.product.posthog",
    "chromadb.api.rust",
    "chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2",
    # ★ `tqdm`：ChromaDB 的 onnx_mini_lm_l6_v2 在**下载模型**时会用它显示进度条。
    #   它不是 chromadb 的必需依赖，所以静态分析看不到 ——
    #   而没有它时，首次启动下载模型会直接 ValueError 失败（"The tqdm python package is not installed"），
    #   表现是"向量库初始化失败"，跟 tqdm 这个名字看上去毫无关系。
    #   打包版默认不带模型（让用户首启下载），所以这个依赖是**必需**的。
    "tqdm",
    "tqdm.auto",
    # 本项目自己的可选依赖（缺失时上层会降级，但打包版应该是"全功能"的）
    "multipart",
    "python_multipart",
]

# 把 chromadb / onnxruntime 的**子模块**也扫进来（它们内部还有动态导入）
for _package in ("chromadb", "onnxruntime"):
    try:
        hiddenimports += [m for m in collect_submodules(_package) if _keep(m)]
    except Exception as _exc:  # noqa: BLE001
        print(f"[spec] 警告：collect_submodules({_package}) 失败：{_exc}")

# ----------------------------------------------------------------------
#  排除：明确不需要的大件（能显著缩小体积）
# ----------------------------------------------------------------------
excludes = [
    "tkinter", "matplotlib", "IPython", "notebook", "jupyter",
    "pytest", "_pytest", "sphinx", "PyQt5", "PySide2", "PySide6",
    "onnxruntime.training",          # 只做推理，训练相关的一大块用不到
    "onnxruntime.transformers",
]

# ----------------------------------------------------------------------
#  分析
# ----------------------------------------------------------------------
a = Analysis(  # noqa: F821 - PyInstaller 注入
    [str(ENTRY_SCRIPT)],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # ★ 不用 UPX：它常被杀软误报，而这是个桌面应用
    console=True,       # ★ 保留控制台：日志是排障的唯一线索（壳会把它的输出落盘）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="backend",
)
