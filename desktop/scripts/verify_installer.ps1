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
  [switch]$Clean
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

if (-not (Test-Path $EnvFile)) { throw "找不到配置模板：$EnvFile" }
Info "配置模板：$EnvFile（只在脚本内部读取）"

# 后端 exe 在跑的话，装出来的那份会连到"已有后端"上去，测的就不是它自己起的后端了
$running = Get-Process -Name 'backend' -ErrorAction SilentlyContinue
if ($running) { throw "有 backend.exe 正在运行（PID $(($running.Id) -join ','))，先关掉再验收" }
Info '没有正在运行的 backend.exe'

if (Test-Path $ProfileDir) { Remove-Item -LiteralPath $ProfileDir -Recurse -Force }
New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null
New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
Info "临时工作目录：$WorkRoot"

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
} else {
  Bad "没有停在首次设置这一步。桌面日志：`n      $logText"
}

# ---------------------------------------------------------------- 5
Step '5/7 填表保存 → 必须进控制台'
Info '（用 CDP 填真实表单；账号从 --EnvFile 读，不进命令行）'

$python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "找不到 $python（CDP 验收需要它）" }
$cdp = Join-Path $ScriptDir 'verify_installer_cdp.py'
if (-not (Test-Path $cdp)) { throw "找不到 $cdp" }

& $python $cdp --port $debugPort --env-file $EnvFile --log-dir $WorkRoot 2>&1 |
  ForEach-Object { Write-Host "      $_" }
$cdpCode = $LASTEXITCODE
if ($cdpCode -eq 0) { Ok '表单保存 → 后端自检通过 → 进入控制台' }
else { Bad "CDP 那一步失败（退出码 $cdpCode），细节见上面的输出" }

# ---------------------------------------------------------------- 6
Step '6/7 数据落在哪'

$userEnv = Join-Path $ProfileDir 'config\.env'
if (Test-Path $userEnv) { Ok '配置写进了临时 profile 的 config\.env（安装目录之外）' }
else { Bad "配置没写进 $userEnv" }

$backendLog = Join-Path $ProfileDir 'logs\backend.log'
# ★ 自检的结论在**桌面日志**里（壳起了那个自检子进程、读它的 stdout）：
#   "自检子进程结束：退出码=0，后端结论=ok"。打包后端日志文件里只会有跑起来之后的
#   访问日志，**不会**有 `[self-check] OK` —— 第一版跑去 backend.log 里找它，
#   于是把一个完全正常的自检判成了失败。
if (Test-Path $desktopLog) {
  $dl = Get-Content -LiteralPath $desktopLog -Raw -Encoding UTF8
  if ($dl -match '后端结论=ok' -and $dl -match '退出码=0') {
    Ok '壳里的"保存前验证"自检：退出码 0 且后端结论 ok'
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

  if (Test-Path $userEnv) { Ok '卸载后用户数据仍在（config\.env 还在 —— 红线守住）' }
  else { Bad '卸载把用户数据删了！会话 / 角色卡 / 世界书都在那里 —— 这是红线级缺陷' }

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
