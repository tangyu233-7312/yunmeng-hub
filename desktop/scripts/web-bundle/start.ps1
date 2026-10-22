# ============================================================================
#  云梦枢 · Web 版启动器（绿色免安装）
# ----------------------------------------------------------------------------
#  ★ 本文件必须带 UTF-8 BOM，否则 PowerShell 5.1 按 ANSI 读，中文全乱码。
#
#  这个压缩包和安装包是同一个后端程序，区别只是"打开方式"：
#    · 安装包  → 装上、生成开始菜单图标、Electron 开一个窗口；
#    · 本压缩包 → 解压到任意目录，双击「启动云梦枢.cmd」，它起后端并用**你的浏览器**打开控制台。
#  两者用的是同一份 `web/` 前端与同一套接口，功能一致。
#
#  目录结构（启动器假定）：
#    云梦枢 Web x.y.z\
#      ├─ 启动云梦枢.cmd       ← 用户双击这个
#      ├─ start.ps1            ← 本文件
#      ├─ 使用说明.txt
#      └─ backend\
#           ├─ backend.exe
#           └─ _internal\      ← PyInstaller 的依赖
#
#  用法：
#      双击「启动云梦枢.cmd」
#      或：powershell -NoProfile -ExecutionPolicy Bypass -File start.ps1
#
#  可选参数：
#      -Port 8000             指定端口；不传则自动挑一个空闲的
#      -NoBrowser             只起服务，不打开浏览器
#      -DataDir "D:\我的数据"   数据目录（默认 %APPDATA%\云梦枢）
# ============================================================================

[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$DataDir = "",
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $here 'backend'
$exe = Join-Path $backendDir 'backend.exe'

function Fail([string]$message) {
    Write-Host ''
    Write-Host '======================================================================' -ForegroundColor Red
    Write-Host ' 启动失败' -ForegroundColor Red
    Write-Host '======================================================================' -ForegroundColor Red
    Write-Host " $message" -ForegroundColor Red
    Write-Host ''
    Write-Host " 解压目录：$here"
    Write-Host ''
    exit 1
}

# ---------------------------------------------------------------------------
#  端口探测工具
# ---------------------------------------------------------------------------
function Test-PortBusy([int]$p) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect('127.0.0.1', $p, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(300)
        $busy = ($ok -and $c.Connected)
        $c.Close()
        return $busy
    } catch {
        return $false
    }
}

# ===========================================================================
#  0) 启动前环境预检
# ---------------------------------------------------------------------------
#  ★ 为什么要有这一步：绿色版最常见的失败不是"程序坏了"，而是环境问题 ——
#    解压不完整、解压到了没有写权限的目录、系统架构不对、端口全被占。
#    这些错误发生后，用户往往只看到"窗口一闪就没"或"连不上"，
#    完全不知道下一步该做什么。所以**在启动之前**把能查的都查一遍，
#    并且分清两种：**阻断**（继续下去一定失败）与**提醒**（能跑，但要知道）。
#  ★ 这里只做只读检查，不改用户环境的任何东西。
# ===========================================================================
$blockers = New-Object System.Collections.ArrayList
$warnings = New-Object System.Collections.ArrayList

# 先把数据目录定下来（默认 %APPDATA%\云梦枢，与安装版同一位置）
if (-not $DataDir) {
    $DataDir = Join-Path $env:APPDATA '云梦枢'
}

# ---- 0.1 解压是否完整 ----
if (-not (Test-Path $exe)) {
    $null = $blockers.Add('找不到 backend\backend.exe —— 请把压缩包**整个解压**后再运行（不要在压缩软件的预览窗口里直接双击）。')
} elseif (-not (Test-Path (Join-Path $backendDir '_internal'))) {
    $null = $blockers.Add('解压不完整：缺少 backend\_internal 目录。请重新解压整个压缩包。')
} else {
    $pydCount = @(Get-ChildItem (Join-Path $backendDir '_internal') -Filter '*.pyd' -Recurse -ErrorAction SilentlyContinue).Count
    if ($pydCount -eq 0) {
        $null = $blockers.Add('解压不完整：backend\_internal 里没有程序组件（可能被安全软件清掉了）。')
    }
}

# ---- 0.2 系统架构：这个包只出 Windows x64 ----
if ($env:PROCESSOR_ARCHITECTURE -and $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    $null = $blockers.Add("当前系统架构是 $($env:PROCESSOR_ARCHITECTURE)，本包只支持 Windows x64。")
}
if (-not [Environment]::Is64BitOperatingSystem) {
    $null = $blockers.Add('当前是 32 位 Windows，本包只支持 64 位。')
}

# ---- 0.3 PowerShell 版本 ----
if ($PSVersionTable.PSVersion.Major -lt 5) {
    $null = $blockers.Add("PowerShell 版本过低（$($PSVersionTable.PSVersion)），需要 5.1 或更高。")
}

# ---- 0.4 数据目录可写（解压到 Program Files 下时最常见的坑）----
try {
    if (-not (Test-Path $DataDir)) {
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    }
    $probe = Join-Path $DataDir ('.hne-write-probe-' + [guid]::NewGuid().ToString('N').Substring(0, 6))
    Set-Content -Path $probe -Value 'ok' -Encoding ASCII -ErrorAction Stop
    Remove-Item -Force $probe -ErrorAction SilentlyContinue
} catch {
    $null = $blockers.Add("数据目录无法写入：$DataDir`n      请换个位置，例如：start.ps1 -DataDir `"D:\云梦枢数据`"")
}

# ---- 0.5 首次运行 / 已有数据 ----
$dbFile = Join-Path $DataDir 'data\app.sqlite3'
if (-not (Test-Path $dbFile)) {
    $null = $warnings.Add('这是第一次启动：会在数据目录里自动建库、建表并生成密钥，稍慢几秒属正常。')
} else {
    $dbMb = [math]::Round((Get-Item $dbFile).Length / 1MB, 2)
    Write-Host " 已有数据：$dbFile（$dbMb MB）"
}

# ---- 0.6 磁盘剩余空间 ----
try {
    $qualifier = Split-Path -Qualifier $DataDir
    $freeGb = [math]::Round((Get-PSDrive -Name $qualifier.TrimEnd(':') -ErrorAction Stop).Free / 1GB, 1)
    if ($freeGb -lt 1) {
        $null = $warnings.Add("磁盘 $qualifier 只剩 $freeGb GB，可能不够写入数据与日志。")
    }
} catch { }

# ---- 0.7 端口 ----
if ($Port -gt 0) {
    if (Test-PortBusy $Port) {
        $null = $blockers.Add("指定的端口 $Port 已被占用。请换一个：start.ps1 -Port 8010")
    }
} elseif (Test-PortBusy 8000) {
    $null = $warnings.Add('端口 8000 已被别的程序占用 —— 将自动换一个空闲端口，浏览器地址以窗口里打印的为准。')
}

# ---- 0.8 是否已有实例在跑（两个实例写同一份 SQLite 会互相锁）----
foreach ($p in @(8000, $Port) | Where-Object { $_ -gt 0 } | Select-Object -Unique) {
    try {
        $root = Invoke-WebRequest "http://127.0.0.1:$p/" -UseBasicParsing -TimeoutSec 1.5
        if ($root.StatusCode -eq 200 -and $root.Content -match '正在运行') {
            $null = $warnings.Add("端口 $p 上已经有一个云梦枢后端在运行。如果它用的是同一份数据目录，请不要同时开两个（SQLite 同一时刻只允许一个写事务）。")
            break
        }
    } catch { }
}

if ($warnings.Count -gt 0) {
    Write-Host ''
    Write-Host ' 启动前提醒（不影响使用，但请知悉）：' -ForegroundColor Yellow
    foreach ($w in $warnings) { Write-Host "   · $w" -ForegroundColor Yellow }
}

if ($blockers.Count -gt 0) {
    Write-Host ''
    Write-Host '======================================================================' -ForegroundColor Red
    Write-Host ' 环境检查没通过，为避免启动到一个必然失败的状态，先停下：' -ForegroundColor Red
    Write-Host '======================================================================' -ForegroundColor Red
    foreach ($b in $blockers) { Write-Host "   ! $b" -ForegroundColor Red }
    Write-Host ''
    Write-Host " 解压目录：$here"
    Write-Host ''
    exit 1
}

Write-Host ' 环境检查通过。' -ForegroundColor Green

# ---------------------------------------------------------------------------
#  1) 选端口：优先 8000，被占用就换一个空闲的
# ---------------------------------------------------------------------------
if ($Port -le 0) {
    $Port = 8000
    if (Test-PortBusy $Port) {
        Write-Host ' 端口 8000 已被占用，自动换一个空闲端口…' -ForegroundColor Yellow
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $Port = $listener.LocalEndpoint.Port
        $listener.Stop()
    }
}

# ---------------------------------------------------------------------------
#  2) 起后端
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host ' 云梦枢 Web 版' -ForegroundColor Cyan
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host " 地址    : http://127.0.0.1:$Port/console"
Write-Host " 数据目录: $DataDir"
Write-Host ' 关闭这个窗口（或按 Ctrl+C）即可停止服务。'
Write-Host '======================================================================' -ForegroundColor Cyan
Write-Host ''

# ★ 这里**不传** --data-dir：后端的 --data-dir 语义是"<userData>\data"
#   （见 desktop/backend_entry.py 的注释），传它会多出一层 data。
#   绿色版直接用 HNE_DATA_DIR（数据根），路径语义与安装版一致：
#   库在 <根>\data\app.sqlite3，密钥在 <根>\config\.secrets.env。
$env:HNE_DATA_DIR = $DataDir

$exeArgs = @('--host', '127.0.0.1', '--port', "$Port")
$proc = Start-Process -FilePath $exe -ArgumentList $exeArgs -PassThru -NoNewWindow
if (-not $proc) {
    Fail '无法启动 backend.exe'
}
Write-Host " 后端进程已启动（PID $($proc.Id)），正在等待就绪…"

# 用户关窗时把子进程一起收掉，别留一个孤儿后端占着端口
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -SupportEvent -Action {
    if ($proc -and -not $proc.HasExited) { $proc.Kill() }
}

# ---------------------------------------------------------------------------
#  3) 等它就绪（最多 120 秒：首次启动要建表 + 装 ONNX 模型）
# ---------------------------------------------------------------------------
$url = "http://127.0.0.1:$Port/console"
$healthUrl = "http://127.0.0.1:$Port/health"
$deadline = (Get-Date).AddSeconds(120)
$ready = $false
$lastError = ''

while ((Get-Date) -lt $deadline) {
    if ($proc.HasExited) {
        Fail "后端进程已退出（退出码 $($proc.ExitCode)）。常见原因：数据目录没有写权限，或端口被别的程序抢走。"
    }
    try {
        $resp = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
        $lastError = "HTTP $($resp.StatusCode)"
    } catch {
        $lastError = $_.Exception.Message
    }
    Start-Sleep -Milliseconds 800
}

if (-not $ready) {
    Fail "后端在 120 秒内没有就绪（最后一次：$lastError）"
}

# 起来了也要如实说清楚"有没有组件降级" —— /health 返回 200 不等于一切正常
try {
    $health = (Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 5).Content | ConvertFrom-Json
    $degraded = @()
    foreach ($p in $health.components.PSObject.Properties) {
        if ($p.Value.status -ne 'ok') { $degraded += "$($p.Name)=$($p.Value.status)" }
    }
    if ($degraded.Count -gt 0) {
        Write-Host " 注意：以下组件未就绪（$($degraded -join ', ')），部分功能会不可用。" -ForegroundColor Yellow
    }
} catch { }

# ---------------------------------------------------------------------------
#  4) 打开浏览器
# ---------------------------------------------------------------------------
Write-Host ' 就绪。' -ForegroundColor Green
if (-not $NoBrowser) {
    try {
        Start-Process $url
        Write-Host " 已在浏览器中打开 $url"
        Write-Host ' （没自动打开？手动复制上面的地址到浏览器也行。）'
    } catch {
        Write-Host " 浏览器没打开成功，请手动访问：$url" -ForegroundColor Yellow
    }
} else {
    Write-Host " 请用浏览器访问：$url"
}

Write-Host ''
Write-Host ' 服务正在运行，这个窗口不要关。按 Ctrl+C 停止。'
Write-Host ''

try {
    $proc.WaitForExit()
} catch {
    if ($proc -and -not $proc.HasExited) { $proc.Kill() }
}
exit 0
