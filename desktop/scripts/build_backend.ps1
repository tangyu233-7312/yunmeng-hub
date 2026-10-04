# ============================================================================
#  构建"打包后端"（sidecar）：用 PyInstaller 把云梦枢后端打成桌面版可独立运行的后端
# ----------------------------------------------------------------------------
#  ★ 本文件必须带 UTF-8 BOM，否则 PowerShell 5.1 按 ANSI 读，中文注释与输出全乱码。
#
#  为什么要有这个脚本（而不是直接敲 pyinstaller 命令）：
#   1. **用哪个解释器**很关键：必须用"装了本项目依赖的那个 venv"，
#      而不是随手一个 python —— 参数写错会得到"打包成功但一跑就缺模块"。
#   2. **不污染主环境**：脚本默认用 `desktop/.build-venv`（打包专用），
#      这样验收用的 `.venv` 一个包都不动（四件套与探针的可信度不受影响）。
#   3. 建完**自动跑一次自检**（起服务→探活→退出），
#      不通过就直接失败 —— 免得"构建成功"变成一句空话。
#
#  用法：
#     pwsh -File desktop/scripts/build_backend.ps1                    # 常规构建（默认带模型）
#     pwsh -File desktop/scripts/build_backend.ps1 -NoModel           # 不带模型（产物更小）
#     pwsh -File desktop/scripts/build_backend.ps1 -SkipSelfCheck     # 跳过自检（快速迭代）
#     pwsh -File desktop/scripts/build_backend.ps1 -ForceVenv         # 重建打包专用 venv
# ============================================================================

[CmdletBinding()]
param(
    # 不把本地 ONNX 嵌入模型打进去（产物更小；首次用到长期记忆时会联网下载约 90MB）
    [switch]$NoModel,
    # 构建完不跑自检
    [switch]$SkipSelfCheck,
    # 删掉并重建打包专用 venv
    [switch]$ForceVenv,
    # 自检用的端口
    [int]$SelfCheckPort = 51987
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
#  路径
# ---------------------------------------------------------------------------
$ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopDir = Split-Path -Parent $ScriptsDir
$RepoRoot   = Split-Path -Parent $DesktopDir
$BuildVenv  = Join-Path $DesktopDir '.build-venv'
$VenvPython = Join-Path $BuildVenv 'Scripts\python.exe'
$SpecFile   = Join-Path $ScriptsDir 'backend.spec'
$DistDir    = Join-Path $DesktopDir 'dist'
$WorkDir    = Join-Path $DesktopDir 'build\pyinstaller'
$OutDir     = Join-Path $DistDir 'backend'
$ExePath    = Join-Path $OutDir 'backend.exe'

Write-Host '===================================================================='
Write-Host '构建打包后端（sidecar）'
Write-Host '===================================================================='
Write-Host "  仓库根      : $RepoRoot"
Write-Host "  打包专用venv: $BuildVenv"
Write-Host "  产物目录    : $OutDir"
Write-Host "  含模型      : $(-not $NoModel)"
Write-Host ''

# ---------------------------------------------------------------------------
#  1) 打包专用 venv（与验收用的 .venv 隔离）
# ---------------------------------------------------------------------------
if ($ForceVenv -and (Test-Path $BuildVenv)) {
    Write-Host '[1/5] 重建打包专用 venv（-ForceVenv）…'
    Remove-Item -Recurse -Force $BuildVenv
}

if (-not (Test-Path $VenvPython)) {
    Write-Host '[1/5] 创建打包专用 venv…'
    # 用系统 python 创建（不要用 .venv，避免把它的路径写进产物）
    $BasePython = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $BasePython) { throw '找不到 python，无法创建打包专用 venv' }
    & $BasePython.Source -m venv $BuildVenv
    if ($LASTEXITCODE -ne 0) { throw "创建 venv 失败（退出码 $LASTEXITCODE）" }

    Write-Host '[1/5] 安装依赖（requirements.txt + pyinstaller）…'
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install pyinstaller -r (Join-Path $RepoRoot 'requirements.txt') --quiet
    if ($LASTEXITCODE -ne 0) { throw "安装依赖失败（退出码 $LASTEXITCODE）" }
} else {
    Write-Host '[1/5] 复用已存在的打包专用 venv（要重建请加 -ForceVenv）'
}

# 确认 pyinstaller 真的可用（复用旧 venv 时可能没有）
& $VenvPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[1/5] 该 venv 里没有 PyInstaller，补装…'
    & $VenvPython -m pip install pyinstaller --quiet
    if ($LASTEXITCODE -ne 0) { throw '安装 PyInstaller 失败' }
}

# ---------------------------------------------------------------------------
#  2) 把本地 ONNX 模型缓存一起打进去
# ---------------------------------------------------------------------------
#  ★ 为什么**默认就带**（而不是可选）：ChromaDB 的模型缓存路径写死在
#    `~/.cache/chroma/onnx_models/all-MiniLM-L6-v2`，**不读任何环境变量**
#    （我们读过源码确认）。不带的话，用户第一次用到长期记忆时必须联网下载约 90MB ——
#    对"双击即用"的桌面软件不合适。带上后，后端首启会把它安装到那个位置。
#    显式不要（做小体积调试包）：-NoModel
$ModelSource = Join-Path $env:USERPROFILE '.cache\chroma\onnx_models'
if ($NoModel) {
    Write-Host '[2/5] -NoModel：不带模型（产物更小，但首次用到长期记忆时会联网下载约 90MB）'
    $env:HNE_BUNDLE_MODEL_DIR = ''
} else {
    if (-not (Test-Path $ModelSource)) {
        throw @"
找不到本地 ONNX 模型缓存：$ModelSource
       先在开发机上跑一次后端让它自动下载：
           .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
       （或在「模型配置」里随便触发一次检索），下载完成后再重新构建。
       确实要构建一个不带模型的产物，请显式加 -NoModel。
"@
    }
    Write-Host "[2/5] 将模型缓存打进产物：$ModelSource"
    # 暂存到一个稳定的位置，再通过环境变量告诉 spec 追加 datas（保持 spec 单一来源）
    $ModelStage = Join-Path $DesktopDir 'build\model-data\onnx_models'
    if (Test-Path $ModelStage) { Remove-Item -Recurse -Force $ModelStage }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ModelStage) | Out-Null
    Copy-Item -Recurse -Force $ModelSource $ModelStage
    $env:HNE_BUNDLE_MODEL_DIR = (Split-Path -Parent $ModelStage)
    $size = [math]::Round(((Get-ChildItem $ModelStage -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Write-Host "      已暂存模型（$size MB）"
}

# ---------------------------------------------------------------------------
#  3) 清理上一次的产物
# ---------------------------------------------------------------------------
Write-Host '[3/5] 清理上一次的构建产物…'
foreach ($dir in @($OutDir, $WorkDir)) {
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}

# ---------------------------------------------------------------------------
#  4) 构建
# ---------------------------------------------------------------------------
Write-Host '[4/5] PyInstaller 构建中…（第一次会比较久，onnxruntime 要收集不少东西）'
Push-Location $RepoRoot
try {
    & $VenvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $DistDir `
        --workpath $WorkDir `
        $SpecFile
    $buildExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($buildExit -ne 0) { throw "PyInstaller 构建失败（退出码 $buildExit）" }
if (-not (Test-Path $ExePath)) { throw "构建结束但找不到产物：$ExePath" }

$totalMb = [math]::Round(((Get-ChildItem $OutDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
Write-Host "      产物：$ExePath（整目录 $totalMb MB）"

# ---------------------------------------------------------------------------
#  5) 自检：起服务 → 探活 → 退出
# ---------------------------------------------------------------------------
if ($SkipSelfCheck) {
    Write-Host '[5/5] 已跳过自检（-SkipSelfCheck）'
} else {
    Write-Host "[5/5] 自检：用产物起一次后端并**深度**探活（端口 $SelfCheckPort）…"
    # ★ 用临时数据目录，避免自检去动真实的向量库/日志目录
    $probeData = Join-Path $env:TEMP ("hne-sidecar-selfcheck-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $probeData | Out-Null

    # ★ 必须把真实 .env 传给它：自检用的是临时数据目录，若不给 .env，
    #   数据库口令读不到 → 组件必然不是 ok → 自检必然失败（那是自检的问题，不是产物的问题）。
    $envFile = Join-Path $RepoRoot '.env'
    $selfCheckArgs = @(
        '--self-check', '--host', '127.0.0.1', '--port', "$SelfCheckPort",
        '--data-dir', $probeData, '--log-dir', (Join-Path $probeData 'logs'),
        '--self-check-timeout', '300'
    )
    # ★ 让自检把"自带模型"安装到一个构建专用的缓存里，而不是开发者的 ~/.cache
    #   （自检是构建流水线的一部分，不该往用户目录里写东西）。
    $modelCache = Join-Path $DesktopDir 'build\selfcheck-model-cache'
    $selfCheckArgs += @('--model-cache-dir', $modelCache)
    if (Test-Path $envFile) {
        $selfCheckArgs += @('--env-file', $envFile)
    } else {
        Write-Host "      警告：找不到 $envFile —— 自检会因为读不到数据库配置而失败"
    }

    # ★ 关键：后端的日志写在 **stderr** 上，而 PowerShell 会把原生命令的 stderr 当错误记录；
    #   在 $ErrorActionPreference='Stop' 下这会**中断脚本**。
    #   ★★ 更要紧的一条（本轮实测抓到，属于"假通过"）：
    #     **绝不要把原生命令放进管道**。写成 `& $Exe ... | ForEach-Object {...}` 时，
    #     `$LASTEXITCODE` 会被管道最后一环覆盖成 0 —— 于是**自检失败也被判成通过**。
    #     本轮真的发生了：产物自检的退出码是 -1，而构建脚本一路绿灯。
    #     正确做法：重定向到文件、**只按退出码判断**，要看内容再单独读文件。
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $selfCheckLog = Join-Path $probeData 'selfcheck.log'
    try {
        & $ExePath @selfCheckArgs *> $selfCheckLog
        $selfCheckExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    if (Test-Path $selfCheckLog) {
        Get-Content $selfCheckLog -Encoding UTF8 | ForEach-Object { Write-Host "      $_" }
    }

    if ($selfCheckExit -ne 0) {
        throw "产物自检失败（退出码 $selfCheckExit）—— 构建出来的后端跑不起来，不要发出去"
    }
    Write-Host '      自检通过：产物能起来、所有组件均为 ok、并能自己退出'
}

Write-Host ''
Write-Host '===================================================================='
Write-Host '完成'
Write-Host '===================================================================='
Write-Host "  后端产物 : $ExePath"
Write-Host '  桌面壳会自动优先使用它（找不到时回退本机 Python）。'
Write-Host '  直接手动试跑：'
Write-Host "      `"$ExePath`" --host 127.0.0.1 --port 8000"
Write-Host ''

# ---------------------------------------------------------------------------
#  6) 提个醒：确认产物**不会**被误提交
# ---------------------------------------------------------------------------
#  ★ 为什么构建脚本要管这件事：本轮为了"让 icon.ico 能进版本库、而构建产物不进去"
#    在 .gitignore 上返工了三次（裸写的 `build/` 会匹配任意层级；而 git 又不许
#    在被忽略的父目录里用否定模式放行文件）。这类规则一旦被谁"顺手简化"回去，
#    表现是**静默**的：要么 200MB 产物进了库，要么图标悄悄不提交。
#    这里用 git 自己的判断（`git check-ignore`）做一次体检并大声说出来。
$IconPath = Join-Path $DesktopDir 'build\icon.ico'
$gitAvailable = $null -ne (Get-Command git -ErrorAction SilentlyContinue)
if ($gitAvailable -and (Test-Path $IconPath)) {
    $ignored = & git -C $RepoRoot check-ignore -q 'desktop/build/icon.ico' 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host '  [!] 警告：desktop/build/icon.ico 当前被 .gitignore 忽略 ——'
        Write-Host '      它本该随包发布（否则别人 clone 后打出来的安装包没有图标）。'
        Write-Host '      请检查 .gitignore 里 Python 的 build/ 是否漏了前导斜杠（应为 /build/）。'
    } else {
        Write-Host '  [OK] .gitignore 检查：icon.ico 会被纳入版本库'
    }
    $distIgnored = & git -C $RepoRoot check-ignore -q 'desktop/dist' 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host '  [OK] .gitignore 检查：打包产物目录（desktop/dist）不会入库'
    } else {
        Write-Host '  [!] 警告：desktop/dist 没有被 .gitignore 忽略 —— 产物可能被误提交！'
    }
}
Write-Host ''
