# ==================================================================
#  云梦枢 · 应用图标素材生成（可复现）
#
#  为什么需要这一步：
#    设计稿（1024×1024 PNG）是**白底**的，直接放进深色顶栏就是一块白方块，
#    而 Electron 打包也需要一个透明底的图标。
#
#  做法（刻意保守）：
#    1. 从**四角**做洪水填充，只把"与图像边界连通的近白像素"变成透明。
#       ★ 不能用"全图白色→透明"：云梦枢图标里有一本翻开的书，书页本身就是白的，
#         那样会在书页上打出一堆窟窿。洪水填充只吃外围底色，书页的白完整保留。
#    2. 按需缩放出若干尺寸，并且**分两处放**：
#         · web/img/        运行时真正用到的两个（顶栏 + 登录页/favicon）
#         · assets/icon/    打包与归档用（Electron 1024、原始设计稿）
#       ★ 为什么分开：`web/` 是**静态站点根目录**，放进去的每个文件都会被伺服。
#         只有 64/256 是页面真正引用的；1024 与原始稿接近 3MB，留在 web/ 里
#         既是无用负载、也让"这个目录里什么是有用的"变得不好判断。
#
#  用法：
#    powershell -File scripts/make_icon_assets.ps1 -Source "路径\图标.png"
#    默认源文件是 .dsh-drop 里那份设计稿（存在时）。
# ==================================================================
param(
  [string]$Source = "",
  [string]$OutDir = "web/img",
  [string]$PackDir = "assets/icon",
  [int]$Threshold = 40
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $Source) {
  # 默认用仓库里归档的那份设计原稿（assets/icon/logo-source.png）：
  # 以前默认去 .dsh-drop 里找"丢给 AI 的设计稿"，而那个目录是**个人暂存目录**、
  # 已在 2026-10-03 的公开仓库清理中删除（内容备份在仓库外）。
  $cand = Join-Path $root "assets/icon/logo-source.png"
  if (-not (Test-Path $cand)) { throw "找不到设计稿，请用 -Source 指定 PNG 路径" }
  $Source = $cand
}
if (-not (Test-Path $Source)) { throw "源文件不存在：$Source" }

$out = Join-Path $root $OutDir
New-Item -ItemType Directory -Force -Path $out | Out-Null

Add-Type -AssemblyName System.Drawing
# ★ 必须显式引用 System.Drawing：Windows PowerShell 5.1 的 Add-Type 不会自动带上它，
#   否则编译器报"命名空间 System.Drawing 中不存在 Drawing2D"。
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class HneIcon {
    /// 去掉与边界连通的近白底（保留图标内部的白色），再缩放到 size×size
    public static string Prep(string src, string dst, int thresh, int size) {
        using (var raw = new Bitmap(src))
        using (var bmp = new Bitmap(raw.Width, raw.Height, PixelFormat.Format32bppArgb)) {
            using (var g = Graphics.FromImage(bmp)) g.DrawImage(raw, 0, 0, raw.Width, raw.Height);
            int w = bmp.Width, h = bmp.Height;
            var data = bmp.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.ReadWrite, PixelFormat.Format32bppArgb);
            int stride = data.Stride;
            byte[] buf = new byte[stride * h];
            Marshal.Copy(data.Scan0, buf, 0, buf.Length);

            bool[] seen = new bool[w * h];
            var q = new Queue<int>();
            for (int x = 0; x < w; x++) { q.Enqueue(x); q.Enqueue((h - 1) * w + x); }
            for (int y = 0; y < h; y++) { q.Enqueue(y * w); q.Enqueue(y * w + w - 1); }
            int lim = 255 - thresh;
            while (q.Count > 0) {
                int idx = q.Dequeue();
                if (idx < 0 || idx >= w * h || seen[idx]) continue;
                seen[idx] = true;
                int o = (idx / w) * stride + (idx % w) * 4;   // BGRA
                if (buf[o] < lim || buf[o + 1] < lim || buf[o + 2] < lim) continue;
                buf[o + 3] = 0;
                int x = idx % w, y = idx / w;
                if (x > 0) q.Enqueue(idx - 1);
                if (x < w - 1) q.Enqueue(idx + 1);
                if (y > 0) q.Enqueue(idx - w);
                if (y < h - 1) q.Enqueue(idx + w);
            }
            Marshal.Copy(buf, 0, data.Scan0, buf.Length);
            bmp.UnlockBits(data);

            using (var outp = new Bitmap(size, size, PixelFormat.Format32bppArgb))
            using (var g2 = Graphics.FromImage(outp)) {
                g2.InterpolationMode = InterpolationMode.HighQualityBicubic;
                g2.PixelOffsetMode = PixelOffsetMode.HighQuality;
                g2.DrawImage(bmp, new Rectangle(0, 0, size, size));
                outp.Save(dst, ImageFormat.Png);
            }
            return dst;
        }
    }
}
"@

# 归档原稿（白底）：Electron 打包/二次设计时可能要用原始分辨率
$pack = Join-Path $root $PackDir
New-Item -ItemType Directory -Force -Path $pack | Out-Null
Copy-Item $Source (Join-Path $pack "logo-source.png") -Force

# 运行时用到的（页面真的引用它们）→ web/img/
$runtime = @{ "logo-256.png" = 256; "logo-64.png" = 64 }
# 打包/留档用的（页面不引用）→ assets/icon/
$packaging = @{ "logo-1024.png" = 1024 }

foreach ($name in $runtime.Keys) {
  $dst = Join-Path $out $name
  [HneIcon]::Prep($Source, $dst, $Threshold, $runtime[$name]) | Out-Null
  $len = (Get-Item $dst).Length
  Write-Host ("  [运行时] {0,-16} {1,5}x{1,-5} {2,8:N1} KB" -f $name, $runtime[$name], ($len / 1KB))
}
foreach ($name in $packaging.Keys) {
  $dst = Join-Path $pack $name
  [HneIcon]::Prep($Source, $dst, $Threshold, $packaging[$name]) | Out-Null
  $len = (Get-Item $dst).Length
  Write-Host ("  [打包用] {0,-16} {1,5}x{1,-5} {2,8:N1} KB" -f $name, $packaging[$name], ($len / 1KB))
}
Write-Host "完成 → $out"
