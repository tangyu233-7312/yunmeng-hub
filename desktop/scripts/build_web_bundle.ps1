# ============================================================================
#  构建「Web 版」绿色压缩包（免安装、双击启动、用浏览器打开）
# ----------------------------------------------------------------------------
#  ★ 本文件必须带 UTF-8 BOM，否则 PowerShell 5.1 按 ANSI 读，中文全乱码。
#
#  产物：release\云梦枢 Web <版本>.zip
#        = backend\（复用桌面版的打包后端：同一个 backend.exe，同一份 web/ 前端）
#        + 启动云梦枢.cmd + start.ps1 + 使用说明.txt
#
#  为什么复用 dist\backend 而不是另打一份：
#    · 安装版和 Web 版**本来就是同一个后端**，再打一份只会让两边慢慢漂移
#      （"为什么安装版有这功能、Web 版没有"）；
#    · 这一层只换"启动方式"：Electron 开窗口 vs 启动器开浏览器。
#      与 desktop/README.md 里"壳只是壳，后端契约一行未改"是同一条原则。
#
#  用法：
#      pwsh -File desktop/scripts/build_web_bundle.ps1              # 有产物就直接打包
#      pwsh -File desktop/scripts/build_web_bundle.ps1 -Rebuild     # 先重建后端再打包
#      pwsh -File desktop/scripts/build_web_bundle.ps1 -Verify      # 打完自动解压实跑一遍
# ============================================================================

[CmdletBinding()]
param(
    # 先跑 build_backend.ps1 重建后端（较慢，几分钟）
    [switch]$Rebuild,
    # 打包后解压到临时目录、真起一次服务、探活 /health 与 /console，通过才算成功
    [switch]$Verify,
    # 测试用端口（-Verify 时使用）
    [int]$VerifyPort = 52731
)

$ErrorActionPreference = 'Stop'

$ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopDir = Split-Path -Parent $ScriptsDir
$RepoRoot   = Split-Path -Parent $DesktopDir
$DistBackend = Join-Path $DesktopDir 'dist\backend'
$SourceDir  = Join-Path $ScriptsDir 'web-bundle'
$ReleaseDir = Join-Path $RepoRoot 'release'
$StageRoot  = Join-Path $ReleaseDir 'web-bundle'

# ---------------------------------------------------------------------------
#  1) 版本号：从后端读取（与桌面壳、安装包同一套，禁止各写各的）
# ---------------------------------------------------------------------------
$configPy = Join-Path $RepoRoot 'app\core\config.py'
$version = (Select-String -Path $configPy -Pattern 'APP_VERSION:\s*str\s*=\s*"([^"]+)"' |
            Select-Object -First 1).Matches[0].Groups[1].Value
if (-not $version) { throw "没能从 $configPy 读到 APP_VERSION" }
Write-Host "[1/5] 版本：$version"

$slug = "云梦枢 Web $version"
$stageDir = Join-Path $StageRoot $slug
$zipPath = Join-Path $ReleaseDir "$slug.zip"

# ---------------------------------------------------------------------------
#  2) 后端产物：没有（或要求重建）就先构建
# ---------------------------------------------------------------------------
$exe = Join-Path $DistBackend 'backend.exe'
if ($Rebuild -or -not (Test-Path $exe)) {
    Write-Host '[2/5] 构建打包后端（build_backend.ps1）…'
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptsDir 'build_backend.ps1')
    if ($LASTEXITCODE -ne 0) { throw "build_backend.ps1 失败（退出码 $LASTEXITCODE）" }
} else {
    $mb = [math]::Round(((Get-ChildItem $DistBackend -Recurse -File |
          Measure-Object -Property Length -Sum).Sum / 1MB), 1)
    Write-Host "[2/5] 复用已有后端产物（$mb MB）：$DistBackend"
    Write-Host '      （要重建请加 -Rebuild）'
}
if (-not (Test-Path $exe)) { throw "找不到 $exe" }

# ---------------------------------------------------------------------------
#  3) 组装暂存目录
# ---------------------------------------------------------------------------
Write-Host "[3/5] 组装暂存目录：$stageDir"
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Force -Path $stageDir | Out-Null

# 3a) 后端整目录拷过去（backend.exe + _internal）
Copy-Item -Recurse -Force $DistBackend (Join-Path $stageDir 'backend')

# 3b) 启动器 + 说明放在**根**（用户一眼能看到，不用进子目录找）
foreach ($name in @('启动云梦枢.cmd', 'start.ps1', '使用说明.txt')) {
    $src = Join-Path $SourceDir $name
    if (-not (Test-Path $src)) { throw "缺少启动器文件：$src" }
    Copy-Item -Force $src (Join-Path $stageDir $name)
}

# 3c) 自检：启动器引用的 backend.exe 位置必须真的存在
#     ★ 这条检查存在的理由：`backend\backend.exe` 是启动器里写死的相对路径，
#       一旦有人调整了目录结构而忘了改启动器，用户看到的是"一闪就没"。
$resolved = Join-Path $stageDir 'backend\backend.exe'
if (-not (Test-Path $resolved)) { throw "启动器约定的位置没有 backend.exe：$resolved" }

# ---------------------------------------------------------------------------
#  4) 打包（用 .NET 的 ZipFile：Compress-Archive 处理 300MB 会慢到不可用）
# ---------------------------------------------------------------------------
Write-Host '[4/5] 压缩中…（约 300MB → 100MB 上下，需要一两分钟）'
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $stageDir,
    $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $true   # includeBaseDirectory：解压后是"云梦枢 Web x.y.z"一层，不会散落一地
)
$zipMb = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
$hash = (Get-FileHash -Algorithm SHA256 $zipPath).Hash
Write-Host "      产物：$zipPath（$zipMb MB）"
Write-Host "      SHA256：$hash"

# ---------------------------------------------------------------------------
#  5) 可选：真解压 + 真跑（不做这一步，"打包成功"只是一句话）
# ---------------------------------------------------------------------------
if ($Verify) {
    Write-Host "[5/5] 验证：解压到临时目录并真起一次服务（端口 $VerifyPort）…"
    $tmp = Join-Path $env:TEMP ("hne-webbundle-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        Expand-Archive -Path $zipPath -DestinationPath $tmp -Force
        $root = Join-Path $tmp $slug
        if (-not (Test-Path $root)) { throw "解压后没有预期的顶层目录：$root" }

        # ★ 用临时数据目录，别去动真实的 %APPDATA%\云梦枢
        $dataDir = Join-Path $tmp 'verify-data'
        $env:HNE_DATA_DIR = $dataDir
        $verifyExe = Join-Path $root 'backend\backend.exe'
        $proc = Start-Process -FilePath $verifyExe `
            -ArgumentList @('--host', '127.0.0.1', '--port', "$VerifyPort") `
            -PassThru -NoNewWindow -RedirectStandardOutput (Join-Path $tmp 'out.log') `
            -RedirectStandardError (Join-Path $tmp 'err.log')

        $deadline = (Get-Date).AddSeconds(180)
        $ok = $false
        while ((Get-Date) -lt $deadline) {
            if ($proc.HasExited) { break }
            try {
                $r = Invoke-WebRequest "http://127.0.0.1:$VerifyPort/health" -UseBasicParsing -TimeoutSec 3
                if ($r.StatusCode -eq 200) { $ok = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 800
        }

        if (-not $ok) {
            Write-Host '      --- stderr ---' -ForegroundColor Yellow
            if (Test-Path (Join-Path $tmp 'err.log')) { Get-Content (Join-Path $tmp 'err.log') -Tail 30 }
            throw '验证失败：解压出来的产物起不来或没就绪'
        }

        $health = (Invoke-WebRequest "http://127.0.0.1:$VerifyPort/health" -UseBasicParsing).Content | ConvertFrom-Json
        $console = Invoke-WebRequest "http://127.0.0.1:$VerifyPort/console/" -UseBasicParsing
        Write-Host "      /health  -> $($health.status)"
        Write-Host "      /console -> HTTP $($console.StatusCode)，$([math]::Round($console.RawContentLength / 1KB, 1)) KB"

        $bad = @()
        foreach ($p in $health.components.PSObject.Properties) {
            if ($p.Value.status -ne 'ok') { $bad += "$($p.Name)=$($p.Value.status)" }
        }
        if ($bad.Count -gt 0) { throw "验证失败：组件未就绪（$($bad -join ', ')）" }

        if ($console.StatusCode -ne 200) { throw "验证失败：/console 返回 $($console.StatusCode)" }
        Write-Host '      验证通过：解压即用、组件全 ok、控制台可访问' -ForegroundColor Green
    } finally {
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
        Remove-Item Env:\HNE_DATA_DIR -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 300
        if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
    }
} else {
    Write-Host '[5/5] 已跳过验证（要真解压实跑请加 -Verify）'
}

# ---------------------------------------------------------------------------
#  6) 别让 300MB 的产物溜进版本库
# ---------------------------------------------------------------------------
$ignored = & git -C $RepoRoot check-ignore -q 'release'
if ($LASTEXITCODE -eq 0) {
    Write-Host '  [OK] release\ 已被 .gitignore 忽略（产物不会入库）'
} else {
    Write-Host '  [!] 警告：release\ 没有被忽略，产物可能被误提交！' -ForegroundColor Yellow
}

Write-Host ''
Write-Host '======================================================================'
Write-Host " 完成：$zipPath"
Write-Host " SHA256：$hash"
Write-Host '======================================================================'
Write-Host ' 上传到 GitHub Release 时，文件名会被清洗成 ASCII（中文与空格 → -）。'
Write-Host ''
