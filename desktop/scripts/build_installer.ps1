#Requires -Version 5.1
<#
.SYNOPSIS
  给云梦枢桌面版打 Windows 安装包（NSIS）。

.DESCRIPTION
  这个脚本只做四件事：**检查前置条件 → 准备 NSIS 工具链 → 调 electron-builder → 核对产物**。
  它刻意不"自动帮你把 sidecar 也编一遍"：PyInstaller 那一趟要十几分钟，
  而"安装包里缺 backend.exe"和"sidecar 没编"是两回事，混在一起会让人分不清
  到底哪一步出了问题。缺 sidecar 就直接停下并告诉你该先跑什么。

  产物（都在仓库根的 release/，已被 .gitignore 忽略）：
    · 云梦枢 Setup 0.1.0.exe   ← 双击可装的安装包
    · win-unpacked/            ← 安装包解出来的样子（用来核对内容，不必真装）

.NOTES
  ★ 关于"没签名"：这个安装包**没有代码签名**（签名要用户自己的证书）。
    后果要说清楚：Windows SmartScreen 会拦一下，提示"未知发布者"。
    这里不做任何绕过提示的事，也不假称已签名 —— 见 docs/handoff 的"诚实边界"。

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\build_installer.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\build_installer.ps1 -SkipBackendCheck
#>
[CmdletBinding()]
param(
  # 产物目录里已有东西时不清理（默认清理，避免拿到上一次的旧包）
  [switch]$KeepOld,
  # 跳过"必须有 sidecar"的前置检查（只用来验证配置本身，打出来的包是残的）
  [switch]$SkipBackendCheck
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ★ Electron 与 electron-builder 都要求"像 Electron"地跑；DSH 环境里
#   ELECTRON_RUN_AS_NODE=1 会让 Electron 退化成纯 Node（实测过：静默跑错）。
$env:ELECTRON_RUN_AS_NODE = $null

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopDir = Split-Path -Parent $ScriptDir
$RepoRoot = Split-Path -Parent $DesktopDir
$BackendDir = Join-Path $DesktopDir 'dist\backend'
$ReleaseDir = Join-Path $RepoRoot 'release'

function Write-Section([string]$Text) {
  Write-Host ''
  Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Fail([string]$Message, [string]$Fix) {
  Write-Host ''
  Write-Host "[×] $Message" -ForegroundColor Red
  if ($Fix) { Write-Host "    → $Fix" -ForegroundColor Yellow }
  exit 1
}

# ==================================================================
#  NSIS 工具链：我们自己下、自己校验、自己解压，再让 electron-builder 复用
# ==================================================================
# ★ 为什么要有这一段（实测踩到的）：electron-builder 第一次打包会去 GitHub releases 下
#   `nsis-3.0.4.1.7z` / `nsis-resources-3.4.1.7z`，再用它自己那套 Node 侧的
#   下载 + 解压 + 锁流程装进 `%LOCALAPPDATA%\electron-builder\Cache`。
#   本机实测：**这一步会静默卡住** —— 无网络活动、无输出、CPU 几乎不动，
#   缓存里只留下一个空的解压目录与 `.lock`（它是 proper-lockfile 的锁目录，
#   上一次被打断后残留下来，下一次就在那儿一直等锁）。
#   同一时刻用 PowerShell 直接下同一个 URL 只要十几秒，SHA256 与官方值完全一致 ——
#   也就是说卡的不是网络，是那套流程本身，而且它不报错、只是不吭声地等着。
#
#   所以这里改成：**按 electron-builder 自己的命名规则把缓存目录准备好**，
#   并写一份它认的 `.state`（状态 complete）。它读到就会直接复用，不下载、不解压、不加锁。
#   顺带做到"离线可重复"：缓存备好之后，以后再打包完全不需要网络。

# 复刻 electron-builder 的 `hashUrlSafe`（djb2 变体 + base36 + 补齐位数）。
# ★ 两个都在实测中踩到、而且都**不报错**的坑：
#   ① .NET 的 Convert.ToString 只支持 2/8/10/16 进制，base36 得自己算
#      （第一版写 ToString($hash,36)，报 "Invalid Base." —— 而且是运行到那一步才炸）；
#   ② **参数不能叫 `$Input`** —— 那是 PowerShell 的自动变量（管道输入枚举器），
#      函数里的 `$Input` 于是不再是传进来的字符串，算出来是一个与输入无关的常数
#      （实测：四种完全不同的输入算出来全是 `0045h`，而且一声不响）；
#   ③ 中间结果 `(hash<<5)+hash` 会超过 32 位，[uint32] 转换会**抛异常**
#      （实测一次刷了几百行），所以全程用 [int64] 算、每轮按 0xFFFFFFFF 取模 ——
#      这正好等价于 JS 里的 `hash >>>= 0`。
function Get-UrlSafeHash([string]$Text, [int]$Length = 5) {
  $mask = [int64]4294967295
  $hash = [int64]5381
  foreach ($ch in $Text.ToCharArray()) {
    $code = [int64][int]$ch
    $hash = ((($hash -shl 5) + $hash) -bxor $code) -band $mask
  }
  $digits = '0123456789abcdefghijklmnopqrstuvwxyz'
  $value = $hash
  if ($value -eq 0) { $out = '0' } else {
    $out = ''
    while ($value -gt 0) {
      $out = $digits[[int]($value % 36)] + $out
      $value = [int64][math]::Floor($value / 36)
    }
  }
  if ($out.Length -ge $Length) { return $out.Substring(0, $Length) }
  return $out.PadLeft($Length, '0')
}

function Initialize-NsisToolset {
  $sevenZip = Join-Path $DesktopDir 'node_modules\electron-winstaller\vendor\7z.exe'
  if (-not (Test-Path $sevenZip)) {
    Write-Host '[!] 没找到 7z.exe（electron-winstaller 没装？），跳过工具链预置' -ForegroundColor Yellow
    Write-Host '    若接下来打包卡在下载 NSIS 上，请先 `cd desktop; npm install`。' -ForegroundColor Yellow
    return
  }

  $baseUrl = 'https://github.com/electron-userland/electron-builder-binaries/releases/download/'
  $builderCache = Join-Path $env:LOCALAPPDATA 'electron-builder\Cache'
  $tools = @(
    @{ release = 'nsis-3.0.4.1'; file = 'nsis-3.0.4.1.7z'
       sha = '9877df902530f96357d13a7a31ae2b9df67f48b11ffc9a1700a7c961574ec5fa'
       probe = 'Bin\makensis.exe' },
    @{ release = 'nsis-resources-3.4.1'; file = 'nsis-resources-3.4.1.7z'
       sha = '593a9a92ef958321293ac6a2ee61e64bf1bd543142a5bd6b3d310709cc924103'
       probe = 'plugins' }
  )

  $ProgressPreference = 'SilentlyContinue'
  foreach ($tool in $tools) {
    $url = "$baseUrl$($tool.release)/$($tool.file)"
    # 目录名必须与 electron-builder 算出来的一模一样：
    #   <文件名去扩展名>-<完整 URL 的 hashUrlSafe(url,5)>
    $stem = $tool.file -replace '\.(tar\.gz|tgz|tar\.xz|txz|zip|7z)$', ''
    $extractDir = Join-Path (Join-Path $builderCache $tool.release) ("$stem-$(Get-UrlSafeHash $url 5)")
    $stateFile = "$extractDir.state"

    if (Test-Path (Join-Path $extractDir $tool.probe)) {
      Write-Host "[√] NSIS 工具链已就绪：$($tool.release)（免下载）"
      continue
    }

    # 上次被打断留下的锁目录会让 electron-builder 一直等 —— 清掉（只清我们自己的缓存）
    $lockDir = "$extractDir.lock"
    if (Test-Path $lockDir) {
      Write-Host "[i] 清掉上次残留的锁目录：$(Split-Path -Leaf $lockDir)"
      Remove-Item -LiteralPath $lockDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    $archive = Join-Path (Join-Path $builderCache $tool.release) $tool.file
    New-Item -ItemType Directory -Path (Split-Path $archive) -Force | Out-Null
    if ((Test-Path $archive) -and
        (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLower() -ne $tool.sha) {
      Write-Host "[i] 已有归档校验和不符，删掉重下：$($tool.file)"
      Remove-Item -LiteralPath $archive -Force
    }
    if (-not (Test-Path $archive)) {
      Write-Host "[i] 下载 $($tool.file)（本机实测可能很慢，请耐心）"
      $sw = [System.Diagnostics.Stopwatch]::StartNew()
      try {
        Invoke-WebRequest -Uri $url -OutFile $archive -TimeoutSec 900 -UseBasicParsing
      } catch {
        Fail "下载 $($tool.file) 失败：$($_.Exception.Message)" `
          "可以手动下载 $url 放到 $(Split-Path $archive) 再重试（脚本会校验 SHA256 并自己解压）"
      }
      Write-Host ("[i] 下载完成：{0:N0} 字节，用时 {1:N0} 秒" -f (Get-Item $archive).Length, $sw.Elapsed.TotalSeconds)
    }

    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $tool.sha) {
      Fail "$($tool.file) 的 SHA256 不符`n      期望 $($tool.sha)`n      实际 $actual" `
        '删掉这个文件重新下载（宁可停下，也不装一个来路不明的工具链）'
    }

    Write-Host "[i] 解压 $($tool.file) → $(Split-Path -Leaf $extractDir)"
    Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $extractDir -Force | Out-Null
    & $sevenZip x $archive "-o$extractDir" -y | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $extractDir $tool.probe))) {
      Fail "解压 $($tool.file) 失败或缺少 $($tool.probe)" "手动解压到 $extractDir"
    }

    # 写 electron-builder 认的缓存状态（version=1 / state=complete）。
    # fileCount 写 0：它的校验逻辑是"expected>0 时才比对文件数"，
    # 写 0 就等于"不比对数量，只看目录非空"，避免我数出来的数与它不一致而误判损坏。
    $state = [ordered]@{ version = 1; state = 'complete'; timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); fileCount = 0; extractedSize = 0 }
    [System.IO.File]::WriteAllText($stateFile, ($state | ConvertTo-Json), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "[√] NSIS 工具链已就绪：$($tool.release)"
  }
}

Write-Section '前置检查'

if (-not (Test-Path (Join-Path $DesktopDir 'node_modules\electron-builder'))) {
  Fail '没有装 electron-builder' "cd desktop; npm install"
}
Write-Host "[√] electron-builder 已安装"

# Electron 的二进制是 npm 的 postinstall 下载的；npm 11 起默认不跑安装脚本，
# 于是 node_modules/electron 里只有个路径文件、没有 dist/electron.exe。
$ElectronExe = Join-Path $DesktopDir 'node_modules\electron\dist\electron.exe'
if (-not (Test-Path $ElectronExe)) {
  Fail 'Electron 二进制缺失（npm 11 默认不跑安装脚本）' `
    'cd desktop; npm rebuild electron --foreground-scripts'
}
Write-Host "[√] Electron 二进制就位（$([math]::Round((Get-Item $ElectronExe).Length / 1MB, 1)) MB）"

# ★ package.json 的 build.electronDist 指向的就是这份已装好的 Electron：
#   electron-builder 默认会再去 GitHub 下一份同样的 zip（约 136MB）。
#   本机实测那个下载只有约 0.1MB/s（4 分钟走 3.67MB），于是打包会卡在下载上。
#   这里核对它确实存在，免得配置写错时又回退成"静默下载"。
$electronDist = Join-Path $DesktopDir 'node_modules\electron\dist'
$electronVersionFile = Join-Path $electronDist 'version'
if (-not (Test-Path $electronVersionFile)) {
  Fail "electronDist 里没有 version 文件：$electronDist" 'cd desktop; npm rebuild electron --foreground-scripts'
}
Write-Host "[√] electronDist 就位（Electron $((Get-Content $electronVersionFile -Raw).Trim())）"

$IconPath = Join-Path $DesktopDir 'build\icon.ico'
if (-not (Test-Path $IconPath)) {
  Fail '缺少安装图标 desktop/build/icon.ico' 'powershell -File desktop/scripts/make-ico.ps1'
}
Write-Host "[√] 图标就位（$((Get-Item $IconPath).Length) 字节）"

# ---- sidecar ----
# ★ 用 .NET 判平台而不是 $IsWindows：那是 PowerShell 7 的自动变量，
#   在 5.1 里配合 Set-StrictMode 会直接报"变量未定义"。
$onWindows = [System.Environment]::OSVersion.Platform -eq [System.PlatformID]::Win32NT
$ExeName = if ($onWindows) { 'backend.exe' } else { 'backend' }
$BackendExe = Join-Path $BackendDir $ExeName
$HasBackend = Test-Path $BackendExe
$InternalDir = Join-Path $BackendDir '_internal'

if ($HasBackend -and -not (Test-Path $InternalDir)) {
  # onedir 形态的依赖全在 _internal/，缺了它装到用户机上必然起不来
  Fail "sidecar 产物的形态不对：有 $ExeName 但没有 _internal\" `
    '重新构建：powershell -File desktop/scripts/build_backend.ps1'
}

if (-not $HasBackend) {
  if (-not $SkipBackendCheck) {
    Fail '还没有打包后端（desktop/dist/backend/backend.exe）' `
      '先跑：powershell -File desktop/scripts/build_backend.ps1'
  }
  Write-Host '[!] 没有 sidecar —— 按 -SkipBackendCheck 继续，打出来的包**不能给用户**' -ForegroundColor Yellow
} else {
  $backendBytes = 0
  Get-ChildItem $BackendDir -Recurse -File | ForEach-Object { $backendBytes += $_.Length }
  Write-Host ("[√] 打包后端就位：{0:N1} MB（{1:N0} 个文件）" -f ($backendBytes / 1MB), (Get-ChildItem $BackendDir -Recurse -File).Count)
}

# ---- 清理旧产物 ----
if (-not $KeepOld -and (Test-Path $ReleaseDir)) {
  # ★ 只删 release/（我们自己的输出目录）。绝不去碰仓库里其它任何目录。
  Write-Host "[i] 清理旧产物：$ReleaseDir"
  Remove-Item -LiteralPath $ReleaseDir -Recurse -Force
}

Write-Section '准备 NSIS 工具链'
Initialize-NsisToolset

Write-Section '调 electron-builder'
Write-Host "[i] 工作目录：$DesktopDir"
Write-Host '[i] 目标：Windows x64 · NSIS 安装包（不签名）'

# ★ 不用管道接输出：管道会把退出码变成 0，从而"构建失败了却报成功"
#   （上一轮在 build_backend.ps1 里踩过这个坑，这里从一开始就避开）。
$logFile = Join-Path $env:TEMP ('yunmeng-builder-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
# ★ 直接跑 node_modules 里的 cli.js，不走 npx：这台机器上 npx 只有 .ps1 壳，
#   而 PowerShell 5.1 的执行策略会把它挡下来（"禁止运行脚本"）。
$builderCli = Join-Path $DesktopDir 'node_modules\electron-builder\out\cli\cli.js'
if (-not (Test-Path $builderCli)) {
  Fail "找不到 electron-builder 的入口：$builderCli" 'cd desktop; npm install'
}
# 没买代码签名证书就别去碰 signtool：关掉"自动找证书"可以少一次无谓的工具下载
$env:CSC_IDENTITY_AUTO_DISCOVERY = 'false'

Push-Location $DesktopDir
try {
  & node $builderCli --win --x64 --publish never *> $logFile
  $code = $LASTEXITCODE
} finally {
  Pop-Location
}

if ($code -ne 0) {
  Write-Host ''
  Write-Host "[×] electron-builder 失败（退出码 $code）" -ForegroundColor Red
  Write-Host "    完整日志：$logFile" -ForegroundColor Yellow
  Write-Host '    最后 40 行：' -ForegroundColor Yellow
  Get-Content -LiteralPath $logFile -Tail 40 -Encoding UTF8 | ForEach-Object { Write-Host "      $_" }
  exit $code
}
Write-Host "[√] electron-builder 成功（日志：$logFile）"

Write-Section '核对产物'

# ---- 免安装目录（安装包解出来的样子）----
$Unpacked = Join-Path $ReleaseDir 'win-unpacked'
if (Test-Path $Unpacked) {
  $appExe = Get-ChildItem $Unpacked -Filter '*.exe' -File |
    Where-Object { $_.Name -notmatch '^(elevate|Uninstall)' } | Select-Object -First 1
  if (-not $appExe) {
    Fail 'win-unpacked 里找不到应用 exe'
  }
  Write-Host "[√] 应用 exe：$($appExe.Name)"

  $unpackedBackend = Join-Path $Unpacked "resources\backend\$ExeName"
  if ($HasBackend -and -not (Test-Path $unpackedBackend)) {
    Fail "安装目录里没有 resources\backend\$ExeName（extraResources 没生效）"
  }
  if (Test-Path $unpackedBackend) {
    Write-Host "[√] 打包后端已进 resources\backend\"
  }

  # ★ 红线核对：安装目录里**不许**有 .env（配置只在 userData / 仓库根）
  $strayEnv = Get-ChildItem $Unpacked -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq '.env' }
  if ($strayEnv) {
    Fail ("安装目录里出现了 .env：`n" + (($strayEnv | ForEach-Object { '      ' + $_.FullName }) -join "`n")) `
      '这会把某个人的数据库口令与签名密钥发给所有用户 —— 必须查清它从哪来'
  }
  Write-Host '[√] 安装目录里没有 .env（红线）'

  # ==================================================================
  #  ★★ 发布前的"个人数据"体检（用户问出来的需求）
  # ==================================================================
  # 用户的疑问很实在：**"我把安装包发给别人，别人会不会看到我的东西？"**
  # 答案是"不会"——但他不该只凭这句话放心。所以每次打包都自动验一遍：
  # 用户真正的东西在 **MySQL** 与 **%APPDATA%\云梦枢**（都不在安装目录里），
  # 安装目录里只该有**程序**。这里把"像个人数据的东西"逐类找一遍，命中就失败。
  #
  # ★ 两类**已知的良性命中**必须排除，否则每次都会误报（我实测过）：
  #   · `chroma` 字样：是 chromadb 这个库自己的文件名（app.py / base_types.py…）；
  #   · `.sql` 文件：是 chromadb 自带的建表脚本模板，不是数据导出。
  #   排除方式是按**路径**判断（只在 chromadb 包目录内才放过），不是按文件名放过。
  # ★★ 这里踩过一个自己的坑（发布闸门当场抓住）：
  #   第一版把"名字痕迹"直接写成了**构建机的真实用户名与账号 ID** ——
  #   于是我把个人标识写进了要公开发布的脚本里，闸门当场报 BLOCKER。
  #   （本节刻意**不复述**那两个值 —— 为了清痕迹的注释本身不该变成新的痕迹。）
  #   而且那个写法**对别人也没用**：别人的用户名不叫这个，等于没查。
  #   现在改成**运行时从环境识别**：拼出本机的用户目录名、%TEMP% 与仓库路径，
  #   谁跑就在谁的环境里找谁的痕迹 —— 脚本里一个真实身份标识都不留。
  $userLeaf = Split-Path -Leaf $env:USERPROFILE          # 本机用户名（只在内存里用）
  $userEscaped = [regex]::Escape($userLeaf)
  $tempEscaped = [regex]::Escape($env:TEMP)
  $repoEscaped = [regex]::Escape($RepoRoot)

  $dataPatterns = @(
    @{ name = '配置文件 .env';   regex = '(^|\\)\.env(\.|$)' }
    @{ name = '日志文件';        regex = '\.log$' }
    @{ name = '数据库/导出文件'; regex = '\.(sqlite3?|db|dump|sql\.gz)$' }
    @{ name = '会话或消息导出';  regex = '(session|export|messages).*\.(json|csv)$' }
    # 这三条是"本机痕迹"：**构建机的用户名 / 临时目录 / 仓库路径**。
    # 打包用的是 `electronDist` 与 `dist/backend`，正常都不该出现在产物里 ——
    # 一旦出现，说明有人把它整个复制进去了（那里面可能就有个人路径）。
    @{ name = '本机用户目录痕迹'; regex = $userEscaped }
    @{ name = '本机临时目录痕迹'; regex = $tempEscaped }
    @{ name = '仓库路径痕迹';     regex = $repoEscaped }
  )
  Write-Host "[i] 本机痕迹模式已按环境生成（用户名/临时目录/仓库路径，不在脚本里写死）"
  $offenders = @()
  foreach ($pattern in $dataPatterns) {
    $hits = @(Get-ChildItem $Unpacked -Recurse -File -Force -ErrorAction SilentlyContinue |
      Where-Object { $_.FullName -match $pattern.regex })
    if ($hits.Count -gt 0) {
      $offenders += ($hits | Select-Object -First 5 | ForEach-Object { "[$($pattern.name)] $($_.FullName)" })
    }
  }
  if ($offenders.Count -gt 0) {
    Fail ("安装目录里出现了疑似个人数据：`n      " + ($offenders -join "`n      ")) `
      '安装包里只该有程序。用户的数据在 MySQL 与 %APPDATA%，不该被打进来'
  }
  # 单独说明：那两类良性命中，避免下一个人以为"漏查了"
  $chromaLike = @(Get-ChildItem $Unpacked -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'chroma|\.sql$' }).Count
  Write-Host "[√] 没有个人数据类文件（.env / .log / 数据库 / 账号名 全无）"
  Write-Host "      （另有 $chromaLike 个文件名里带 chroma 或 .sql 的项目，是 chromadb 库自带的，非用户数据）"

  # asar 里应该只有壳自己的代码（src/** 与 package.json）
  $asar = Join-Path $Unpacked 'resources\app.asar'
  if (Test-Path $asar) {
    $listing = @(& node (Join-Path $DesktopDir 'node_modules\@electron\asar\bin\asar.js') list $asar 2>&1 |
      ForEach-Object { $_.Trim() } | Where-Object { $_ })
    # `asar list` 会同时列出**目录项**与文件项（`\src` 与 `\src\main.js`）——
    # 只拿文件项去判断，否则目录名一定会被判成"多余内容"。
    $files = @($listing | Where-Object { $_ -match '^[\\/].*\.[A-Za-z0-9]+$' })
    $unexpected = @($files | Where-Object { $_ -notmatch '^[\\/](src[\\/][^\\/]+|package\.json)$' })
    if ($listing.Count -eq 0) {
      Fail 'app.asar 列不出内容（asar 工具或归档本身有问题）'
    } elseif ($unexpected.Count -eq 0) {
      Write-Host "[√] app.asar 里只有壳自己的代码（列出 $($listing.Count) 项，其中文件 $($files.Count) 个）"
    } else {
      Fail ("app.asar 里有不该出现的内容：`n      " + (($unexpected | Select-Object -First 20) -join "`n      "))
    }
  }
} else {
  Write-Host '[!] 没有 win-unpacked（可能被你裁掉了），跳过目录核对' -ForegroundColor Yellow
}

# ---- 安装包本体 ----
$installer = Get-ChildItem $ReleaseDir -Filter '*.exe' -File |
  Where-Object { $_.Name -match 'Setup|setup' } | Select-Object -First 1
if (-not $installer) {
  Fail "$ReleaseDir 里没有找到安装包（*.exe）"
}

$hash = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash
Write-Host ''
Write-Host '[√] 安装包' -ForegroundColor Green
Write-Host "    路径：$($installer.FullName)"
Write-Host ("    大小：{0:N1} MB" -f ($installer.Length / 1MB))
Write-Host "    SHA256：$hash"
Write-Host ''
Write-Host '    提醒：这个包**没有代码签名**，用户首次运行会被 SmartScreen 提示"未知发布者"。' -ForegroundColor Yellow
Write-Host '    这不是缺陷，是"没有购买代码签名证书"的直接后果 —— 文档里已如实写明。' -ForegroundColor Yellow
Write-Host ''
Write-Host '    下一步（可选）：自动验收"装出来的那一份" ——' -ForegroundColor DarkGray
Write-Host '      powershell -NoProfile -ExecutionPolicy Bypass -File desktop\scripts\verify_installer.ps1' -ForegroundColor DarkGray
