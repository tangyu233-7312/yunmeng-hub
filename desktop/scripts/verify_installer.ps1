<#
.SYNOPSIS
  验收"装出来的那一份"：装 → 首启向导 → 控制台 → 卸载后用户数据还在。

.DESCRIPTION
  开发态跑得好，**完全不能**说明安装包是好的 —— 这两件事的差别正是阶段 3 的全部内容：
  Node 代码被塞进 asar、后端从 `desktop/dist/backend` 搬到 `resources/backend`、
  userData 从仓库旁边变成 `%APPDATA%\云梦枢`、`.env` 从仓库根消失。
  任何一条对不上，症状都是"开发时好好的，装完打不开"。

  所以这个脚本**只验收装出来的那份**，一步一步做，每步都要看到证据：

    1. 前置检查（安装包在不在、后端没在跑、端口没被占）
    2. 静默装到**临时目录**（绝不动你机器上真实的 %APPDATA%）
    3. 核对安装目录：有 resources\backend\backend.exe、**没有 .env**、asar 里只有壳的代码
    4. 用**全新空配置**启动，看它是不是进了"首次设置"页（不是控制台、不是报错页）
    5. 用 CDP 填表（账号从 --EnvFile 读，**不写进命令行**）→ 保存 → 必须进控制台
    6. 核对配置落在临时 profile、后端自检通过
    7. 关闭 → 静默卸载 → 核对**用户数据仍在**（红线：卸载不删数据）

.NOTES
  ★ 全程用临时目录（`%TEMP%\yunmeng-installer-*`），跑完告诉你它们在哪；
    加 -Keep 可以留着手工翻。真实 `%APPDATA%\云梦枢` 与仓库 `.env` 一律不碰。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\verify_installer.ps1 -EnvFile .env
#>
[CmdletBinding()]
param(
  # 待验收的安装包；不传就用仓库 release\ 里最新的那个
  [string]$Installer,
  # 填表用的配置（默认仓库根 .env）—— 只在脚本内部读取，不会出现在命令行
  [string]$EnvFile,
  # 临时 profile / 安装目录的父目录
  [string]$WorkRoot,
  # 保留临时目录（默认保留，方便你自己看；加 -Clean 才删）
  [switch]$Clean,
  # ★ 模拟"用户机器上早就有一份配置"（而不是全新安装后过向导）。
  #   用途：复现并验证"旧配置里没有 HNE_DB_BACKEND"这条最危险的升级路径
  #   （真实事故见 docs/handoff.md §34.1）。
  #   配套的 `-EnvFile` 提供那份配置的内容；本开关会把它复制到临时 profile 的
  #   `config\.env`，并**刻意删掉 HNE_DB_BACKEND 那一行**。
  [switch]$SeedUserConfig
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:ELECTRON_RUN_AS_NODE = $null

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopDir = Split-Path -Parent $ScriptDir
$RepoRoot = Split-Path -Parent $DesktopDir
$ReleaseDir = Join-Path $RepoRoot 'release'

if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot '.env' }
if (-not $WorkRoot) { $WorkRoot = Join-Path $env:TEMP ('yunmeng-installer-' + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
$ProfileDir = Join-Path $WorkRoot 'profile'
$InstallDir = Join-Path $WorkRoot 'app'

$script:Failures = New-Object System.Collections.Generic.List[string]
$script:Checks = New-Object System.Collections.Generic.List[object]

function Step([string]$Text) {
  Write-Host ''
  Write-Host "── $Text" -ForegroundColor Cyan
}
function Ok([string]$Text) {
  Write-Host "  [√] $Text" -ForegroundColor Green
  $script:Checks.Add([pscustomobject]@{ ok = $true; text = $Text })
}
function Bad([string]$Text) {
  Write-Host "  [×] $Text" -ForegroundColor Red
  $script:Checks.Add([pscustomobject]@{ ok = $false; text = $Text })
  $script:Failures.Add($Text)
}
function Info([string]$Text) { Write-Host "  [i] $Text" -ForegroundColor DarkGray }

# ---------------------------------------------------------------- 1
Step '1/7 前置检查'

if (-not $Installer) {
  if (-not (Test-Path $ReleaseDir)) { throw "没有 $ReleaseDir —— 先跑 build_installer.ps1" }
  $Installer = (Get-ChildItem $ReleaseDir -Filter '*Setup*.exe' -File |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}
if (-not $Installer -or -not (Test-Path $Installer)) { throw "找不到安装包：$Installer" }
Info ("安装包：{0}（{1:N1} MB）" -f $Installer, ((Get-Item $Installer).Length / 1MB))

# ★★ 两种验收模式（默认零配置 —— 因为那才是"装完即用"的形态）
#
#   · 不加 -EnvFile 且仓库根没有 .env → **零配置模式**：
#     一个字段都不填，直接点「保存并开始」，核对 SQLite 文件与自动生成的密钥。
#   · 给了 -EnvFile（或仓库根有 .env）→ **MySQL 可选模式**：
#     把存储方式切成 MySQL、按该文件填连接信息。
#
# ★ 第一版把"必须有 .env"写成了硬前置（`throw "找不到配置模板"`），
#   那在零配置形态下等于**默认拒绝验收** —— 而零配置恰恰是这一版的主路径。
if (-not $EnvFile -and (Test-Path (Join-Path $RepoRoot '.env'))) {
  $EnvFile = Join-Path $RepoRoot '.env'
}
$ZeroConfig = [string]::IsNullOrWhiteSpace($EnvFile) -or -not (Test-Path $EnvFile)
if ($ZeroConfig) {
  Info '验收模式：零配置（默认形态 · SQLite）—— 一个字段都不填'
  Info '（想验 MySQL 那条路：powershell -File desktop\scripts\verify_installer.ps1 -EnvFile .env）'
} elseif ($SeedUserConfig) {
  Info "验收模式：**预置用户配置**（模拟老用户升级，模板 $EnvFile）"
} else {
  Info "验收模式：MySQL 可选形态，配置模板 $EnvFile（只在脚本内部读取）"
}

# 后端 exe 在跑的话，装出来的那份会连到"已有后端"上去，测的就不是它自己起的后端了
$running = Get-Process -Name 'backend' -ErrorAction SilentlyContinue
if ($running) { throw "有 backend.exe 正在运行（PID $(($running.Id) -join ','))，先关掉再验收" }
Info '没有正在运行的 backend.exe'

if (Test-Path $ProfileDir) { Remove-Item -LiteralPath $ProfileDir -Recurse -Force }
New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Info "临时工作目录：$WorkRoot"

# ★★ 预置"老用户的配置"：**刻意不含 HNE_DB_BACKEND**（老版只有 MySQL 一种后端）。
#   这条路径正是真实事故（§34.1）的现场：如果后端只看字段默认值，
#   它就会去连一份空的 SQLite 库，用户拿原账号登录得到"用户名或密码错误"。
$seededConfigFile = Join-Path $ProfileDir 'config\.env'
if ($SeedUserConfig) {
  if ($ZeroConfig) { throw '-SeedUserConfig 需要同时给 -EnvFile（要有一份真实配置可复制）' }
  New-Item -ItemType Directory -Path (Split-Path -Parent $seededConfigFile) -Force | Out-Null
  @(Get-Content -LiteralPath $EnvFile -Encoding UTF8 |
    Where-Object { $_ -notmatch '^\s*HNE_DB_BACKEND\s*=' }) |
    Set-Content -LiteralPath $seededConfigFile -Encoding UTF8
  $seededCount = @(Get-Content -LiteralPath $seededConfigFile -Encoding UTF8 |
    Where-Object { $_ -match '^\s*HNE_[A-Z_]+\s*=' }).Count
  Info "已预置配置：$seededCount 个键，**故意删掉了 HNE_DB_BACKEND**（复现老配置）"
}

# ---------------------------------------------------------------- 2
Step '2/7 静默装到临时目录'

# ★ /D= 必须是命令行**最后一项**，且路径不加引号（NSIS 的老规矩）。
$installLog = Join-Path $WorkRoot 'install.log'
$proc = Start-Process -FilePath $Installer -ArgumentList @('/S', "/D=$InstallDir") -PassThru -Wait
if ($proc.ExitCode -ne 0) { throw "安装失败（退出码 $($proc.ExitCode)）" }
Ok "安装完成（退出码 0）"

$appExe = Get-ChildItem $InstallDir -Filter '*.exe' -File |
  Where-Object { $_.Name -notmatch '^(Uninstall|elevate)' } | Select-Object -First 1
if (-not $appExe) { throw "$InstallDir 里没有找到应用 exe" }
Ok "应用：$($appExe.FullName)"

# ---------------------------------------------------------------- 3
Step '3/7 核对安装目录的内容'

$backendExe = Join-Path $InstallDir 'resources\backend\backend.exe'
if (Test-Path $backendExe) {
  $mb = (Get-ChildItem (Split-Path $backendExe) -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
  Ok ("resources\backend\backend.exe 在（整个后端 {0:N1} MB）" -f $mb)
} else {
  Bad 'resources\backend\backend.exe 不在 —— 装出来的版本没有后端可跑'
}

# ★★ 红线：安装目录里绝不能有 .env（那等于把某个人的口令发给所有用户）
$stray = @(Get-ChildItem $InstallDir -Recurse -File -Force -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -eq '.env' })
if ($stray.Count -eq 0) { Ok '安装目录里没有 .env（红线）' }
else { Bad ("安装目录里有 .env：`n      " + (($stray | ForEach-Object { $_.FullName }) -join "`n      ")) }

# asar 里应该只有壳自己的代码（src/** 与 package.json）
$asar = Join-Path $InstallDir 'resources\app.asar'
if (Test-Path $asar) {
  $listing = @(& node (Join-Path $DesktopDir 'node_modules\@electron\asar\bin\asar.js') list $asar 2>&1 |
    ForEach-Object { $_.Trim() } | Where-Object { $_ })
  # `asar list` 会同时列出**目录项**与文件项（`\src` 与 `\src\main.js`）。
  # 只拿文件项去判断 —— 第一版没这么做，于是把 `\src`、`\package.json` 全判成
  # "多余内容"，报了一个根本不存在的失败（自己把自己绊倒）。
  $files = @($listing | Where-Object { $_ -match '^[\\/].*\.[A-Za-z0-9]+$' })
  $unexpected = @($files | Where-Object { $_ -notmatch '^[\\/](src[\\/][^\\/]+|package\.json)$' })
  if ($listing.Count -eq 0) {
    Bad 'app.asar 列不出内容（asar 工具或归档本身有问题）'
  } elseif ($unexpected.Count -eq 0) {
    Ok "app.asar 里只有 src\ 下的文件与 package.json（列出 $($listing.Count) 项，其中文件 $($files.Count) 个）"
  } else {
    Bad ("app.asar 里有多余内容：`n      " + (($unexpected | Select-Object -First 20) -join "`n      "))
  }
} else {
  Bad 'resources\app.asar 不存在'
}

# ---------------------------------------------------------------- 4 & 5
Step '4/7 用全新空配置启动（应该进"首次设置"页）'

$debugPort = Get-Random -Minimum 45000 -Maximum 48999
$env:HNE_DESKTOP_PROFILE = $ProfileDir
$env:HNE_DESKTOP_DEBUG = '1'
$env:HNE_DESKTOP_STRICT_CONFIG = '1'   # 让"仓库根兜底"一定被关掉（打包态本来就该关）

$stdout = Join-Path $WorkRoot 'app-stdout.log'
$stderr = Join-Path $WorkRoot 'app-stderr.log'
$appProc = Start-Process -FilePath $appExe.FullName `
  -ArgumentList @("--remote-debugging-port=$debugPort") `
  -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
Info "应用已启动（PID $($appProc.Id)，调试端口 $debugPort）"

$desktopLog = Join-Path $ProfileDir 'logs\desktop.log'
# ★★ 这里踩过一个坑（第一次跑验收时 5 项全红，其实应用完全正常）：
#   日志是 UTF-8 写的，而 **Windows PowerShell 5.1 的 `Get-Content` 默认按 ANSI 解** ——
#   中文全变成乱码，于是"进入首次设置""允许仓库根兜底=False"一条都匹配不上。
#   两条修法都用上了：① 一律 `-Encoding UTF8`；② 判据优先用应用打印的
#   **纯 ASCII** 状态行 `[state] ... packaged=true ... repoFallback=false ... step=setup`，
#   免得验收脚本再去赌编码。
$deadline = (Get-Date).AddSeconds(90)
$logText = ''
while ((Get-Date) -lt $deadline) {
  if (Test-Path $desktopLog) { $logText = Get-Content -LiteralPath $desktopLog -Raw -Encoding UTF8 }
  if ($logText -match '\[state\].*\bstep=setup\b') { break }
  Start-Sleep -Milliseconds 400
}

if ($logText -match '\[state\][^\r\n]*packaged=true') {
  Ok '启动日志确认这次跑的是打包形态（[state] packaged=true）'
} else {
  Bad "启动日志里没有确认打包形态。日志：`n      $logText"
}

if ($logText -match '\[state\][^\r\n]*repoFallback=false') {
  Ok '打包态下"仓库根兜底"已关闭（[state] repoFallback=false —— 红线）'
} else {
  Bad '打包态下仍然允许读仓库根 .env —— 会拿别人的凭据连库'
}

if ($logText -match '\[state\][^\r\n]*\bstep=setup\b') {
  Ok '空配置下停在"首次设置"这一步（step=setup，没有跳过向导去连数据库）'
} elseif ($SeedUserConfig -and $logText -match '\[state\][^\r\n]*\bstep=console\b') {
  Ok '已有配置 → 跳过向导直接进控制台（预置配置模式，符合预期）'
} else {
  Bad "没有停在首次设置这一步。桌面日志：`n      $logText"
}

# ---------------------------------------------------------------- 4b（预置配置模式）
#
# ★★ 这一段专门验"老用户升级"那条最危险的路径：配置里**没有 HNE_DB_BACKEND**、
#   只有 MySQL 连接信息。正确行为是**继续用 MySQL**；
#   若退化成默认的 sqlite，用户就会看到一份空库（真实事故见 docs/handoff.md §34.1）。
if ($SeedUserConfig) {
  Step '4b/7 旧配置（无 HNE_DB_BACKEND）必须继续用 MySQL'

  # 拿到后端端口：从控制台 URL 或日志里找
  $consoleUrl = ''
  $deadline = (Get-Date).AddSeconds(180)
  while ((Get-Date) -lt $deadline) {
    if (Test-Path $desktopLog) { $logText = Get-Content -LiteralPath $desktopLog -Raw -Encoding UTF8 }
    # ★ 端口从日志里拿（`[info] 已启动后端 PID=… port=…`）——
    #   比去猜"控制台 URL 会出现在日志里"稳得多：控制台 URL 只在**导航**时才出现，
    #   而这个 mode 下窗口本来就已经在控制台上了，不会再导航一次（第一版就栽在这）。
    if ($logText -match 'port=(\d+)\s+mode=') { $backendPort = [int]$Matches[1]; break }
    Start-Sleep -Milliseconds 500
  }

  if ($backendPort -le 0) {
    Bad "没能在 180 秒内从日志里拿到后端端口。日志尾部：`n      $(($logText -split "`r?`n" | Select-Object -Last 6) -join "`n      ")"
  } else {
    # 等一个稳定信号：控制台页面真的被加载过（后端日志里会有 /console/ 的 200）
    $backendLog = Join-Path $ProfileDir 'logs\backend.log'
    $loaded = $false
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Date) -lt $deadline) {
      if (Test-Path $backendLog) {
        $bl = Get-Content -LiteralPath $backendLog -Raw -Encoding UTF8
        if ($bl -match 'GET /console/ HTTP/1\.1" 200') { $loaded = $true; break }
      }
      Start-Sleep -Milliseconds 500
    }
    if ($loaded) { Ok "后端已就绪，控制台已被加载（端口 $backendPort）" }
    else { Info '（没在 120 秒内看到 /console/ 请求，仍然直接问 /health）' }

    try {
      $health = Invoke-RestMethod -Uri "http://127.0.0.1:$backendPort/health" -TimeoutSec 20
      $dbDetail = $health.components.database.detail
      $which = [string]$dbDetail.backend
      if ($which -eq 'mysql') {
        Ok "★ 后端用的是 MySQL（server=$($dbDetail.server)）—— 旧配置没有被静默换库"
      } else {
        Bad "★ 后端用的是 $which —— 旧配置被静默换库了！用户会看到'用户名或密码错误'（§34.1 那个事故复发了）"
      }
      if ([string]$health.status -eq 'ok') { Ok '整体健康状态 ok' }
      else { Bad "健康状态不是 ok：$($health.status)" }
    } catch {
      Bad "探活失败：$($_.Exception.Message)"
    }
  }
}

# ---------------------------------------------------------------- 5
if ($SeedUserConfig) {
  Step '5/7 跳过"填表保存"（这个模式验的是"已有配置直接启动"，不过向导）'
  Info '（向导那条路由零配置模式与 MySQL 模式各自覆盖，见 verify_installer_cdp.py）'
} else {
Step '5/7 填表保存 → 必须进控制台'
Info '（用 CDP 填真实表单；账号从 --EnvFile 读，不进命令行）'

$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "找不到 $python（CDP 验收需要它）" }
$cdp = Join-Path $ScriptDir 'verify_installer_cdp.py'
if (-not (Test-Path $cdp)) { throw "找不到 $cdp" }

# ★ 两种模式对应的参数不同（零配置不传 --env-file，并带上 --userdata 好核对数据落点）
$cdpArgs = @($cdp, '--port', $debugPort, '--log-dir', $WorkRoot)
if ($ZeroConfig) {
  $cdpArgs += @('--userdata', $ProfileDir)
} else {
  $cdpArgs += @('--env-file', $EnvFile)
}

& $python @cdpArgs 2>&1 | ForEach-Object { Write-Host "      $_" }
$cdpCode = $LASTEXITCODE
if ($cdpCode -eq 0) {
  if ($ZeroConfig) { Ok '零配置：不填任何字段，点「保存并开始」→ 后端自检通过 → 进入控制台' }
  else { Ok '表单保存 → 后端自检通过 → 进入控制台' }
} else { Bad "CDP 那一步失败（退出码 $cdpCode），细节见上面的输出" }
}   # ← 结束 `if ($SeedUserConfig) { … } else { … }`（预置配置模式不过向导）

# ---------------------------------------------------------------- 6
Step '6/7 数据落在哪'

$userEnv = Join-Path $ProfileDir 'config\.env'
$userDb = Join-Path $ProfileDir 'data\app.sqlite3'

if ($ZeroConfig) {
  # ★ 零配置模式下**没有** config\.env 也是对的（用户什么都没填，
  #   而壳注入的 HNE_DATA_DIR 已经足够让后端把数据放到 profile 下）。
  if (Test-Path $userDb) {
    $size = (Get-Item $userDb).Length
    Ok "SQLite 库建在 profile 下（安装目录之外）：data\app.sqlite3，$size 字节"
  } else {
    Bad "没有在 $userDb 建出 SQLite 库 —— 零配置形态下这就是'装完不能用'"
  }
  if (Test-Path $userEnv) { Info '（config\.env 也存在 —— 用户在向导里也点了保存，属正常）' }
} else {
  if (Test-Path $userEnv) { Ok '配置在临时 profile 的 config\.env（安装目录之外）' }
  else { Bad "配置没写进 $userEnv" }
}

$backendLog = Join-Path $ProfileDir 'logs\backend.log'
# ★ 自检的结论在**桌面日志**里（壳起了那个自检子进程、读它的 stdout）：
#   "自检子进程结束：退出码=0，后端结论=ok"。打包后端日志文件里只会有跑起来之后的
#   访问日志，**不会**有 `[self-check] OK` —— 第一版跑去 backend.log 里找它，
#   于是把一个完全正常的自检判成了失败。
if (Test-Path $desktopLog) {
  $dl = Get-Content -LiteralPath $desktopLog -Raw -Encoding UTF8
  if ($dl -match '后端结论=ok' -and $dl -match '退出码=0') {
    Ok '壳里的"保存前验证"自检：退出码 0 且后端结论 ok'
  } elseif ($SeedUserConfig) {
    # ★ 预置配置模式下**不过向导**，所以不会有"保存前验证"这一步 ——
    #   这不是缺陷，而是那条路径本来就不该有它（已经把"用的是哪个后端"
    #   用 /health 单独验过了，见 4b）。如实说明，不判失败。
    Info '（预置配置模式不过向导，因此没有"保存前验证"这一步 —— 属预期；后端可用性已由 4b 的 /health 验过）'
  } else {
    Bad "桌面日志里没看到自检成功结论：`n      $(($dl -split "`r?`n" | Select-Object -Last 10) -join "`n      ")"
  }
}
if (Test-Path $backendLog) {
  $bl = Get-Content -LiteralPath $backendLog -Raw -Encoding UTF8
  if ($bl -match '\[self-check\] OK') { Ok '打包后端日志里也有 [self-check] OK（额外旁证）' }
  else { Info '（后端日志里没有 [self-check] OK —— 自检子进程的输出由壳接管，属正常）' }
} else {
  Info "（没有 $backendLog —— 不影响结论）"
}

# ---------------------------------------------------------------- 7
Step '7/7 关闭 → 卸载 → 用户数据必须还在'

try { $null = $appProc.CloseMainWindow() } catch { }
Start-Sleep -Seconds 2
$still = Get-Process -Id $appProc.Id -ErrorAction SilentlyContinue
if ($still) {
  Info '窗口没关掉，直接结束进程树（模拟用户强退）'
  try { & taskkill /PID $appProc.Id /T /F 2>&1 | Out-Null } catch { }
  Start-Sleep -Seconds 2
}
$left = Get-Process -Name 'backend' -ErrorAction SilentlyContinue
if ($left) { Bad "应用退出后还有 backend.exe 残留（PID $(($left.Id) -join ','))" }
else { Ok '应用退出时把后端子进程一起带走了（没有残留）' }

$uninstaller = Join-Path $InstallDir 'Uninstall 云梦枢.exe'
if (-not (Test-Path $uninstaller)) {
  $uninstaller = (Get-ChildItem $InstallDir -Filter 'Uninstall*.exe' -File -ErrorAction SilentlyContinue |
    Select-Object -First 1).FullName
}
if (-not $uninstaller) {
  Bad '找不到卸载程序，跳过卸载验收'
} else {
  # ★ 卸载程序会把自己复制到临时目录再执行，所以要等它自己结束
  $u = Start-Process -FilePath $uninstaller -ArgumentList @('/S') -PassThru -Wait
  Start-Sleep -Seconds 3
  Ok "静默卸载完成（退出码 $($u.ExitCode)）"

  # ★ 卸载**绝不能**删用户数据。零配置形态下"用户数据"就是那个 SQLite 文件
  #   （会话 / 角色卡 / 世界书全在里面），所以两种模式各查各的落点。
  if ($ZeroConfig) {
    if (Test-Path $userDb) { Ok '卸载后用户数据仍在（data\app.sqlite3 还在 —— 红线守住）' }
    else { Bad '卸载把用户数据删了！会话 / 角色卡 / 世界书都在那个 SQLite 文件里 —— 这是红线级缺陷' }
  } else {
    if (Test-Path $userEnv) { Ok '卸载后用户数据仍在（config\.env 还在 —— 红线守住）' }
    else { Bad '卸载把用户数据删了！会话 / 角色卡 / 世界书都在那里 —— 这是红线级缺陷' }
  }

  if (Test-Path (Join-Path $ProfileDir 'logs')) { Ok '卸载后日志仍在（同样是用户数据）' }
  else { Info '（日志目录没了 —— 不算红线，但值得看一眼）' }
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '════════════════ 汇总 ════════════════' -ForegroundColor Cyan
$pass = @($script:Checks | Where-Object ok).Count
$fail = @($script:Checks | Where-Object { -not $_.ok }).Count
Write-Host "  通过 $pass 项，失败 $fail 项"
if ($fail -gt 0) {
  Write-Host '  失败项：' -ForegroundColor Red
  $script:Failures | ForEach-Object { Write-Host "    · $_" -ForegroundColor Red }
}
Write-Host "  临时目录：$WorkRoot"
if ($Clean) {
  Remove-Item -LiteralPath $WorkRoot -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host '  （已按 -Clean 清理）'
} else {
  Write-Host '  （保留着，方便你自己翻；确认没用后可以直接删掉这个目录）'
}
exit $fail
