# 由 assets/icon/logo-1024.png 生成多尺寸 icon.ico（Windows 任务栏 / 安装包用）
#
# ★ 为什么不用第三方工具：electron-builder 能直接吃 PNG，但 Windows 的**窗口/任务栏图标**
#   与 NSIS 安装包需要 .ico；用 System.Drawing 生成可以避免为了一个图标引入 ImageMagick
#   这类外部依赖，且脚本可重跑（与 scripts/make_icon_assets.ps1 的做法一致）。
#
# ★ 两个已经踩过的 Windows 坑：
#   1) Add-Type 必须显式 -ReferencedAssemblies System.Drawing，否则报"找不到 Drawing2D"；
#   2) 本文件必须存成 **UTF-8 BOM**，否则 PowerShell 5.1 按 ANSI 读，中文注释全乱码。
#
# 用法：pwsh -NoProfile -File desktop/scripts/make-ico.ps1

$ErrorActionPreference = 'Stop'

$desktopDir = Split-Path -Parent $PSScriptRoot
$repoRoot   = Split-Path -Parent $desktopDir
$sourcePng  = Join-Path $repoRoot 'assets\icon\logo-1024.png'
$buildDir   = Join-Path $desktopDir 'build'
$targetIco  = Join-Path $buildDir 'icon.ico'

if (-not (Test-Path $sourcePng)) {
    throw "找不到图标源文件：$sourcePng"
}
if (-not (Test-Path $buildDir)) {
    New-Item -ItemType Directory -Path $buildDir | Out-Null
}

Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;

public static class IcoMaker
{
    // 依次绘制多个尺寸再打包成单个 .ico —— Windows 会按需挑最合适的一档
    public static void Make(string sourcePng, string targetIco, int[] sizes)
    {
        using (var source = new Bitmap(sourcePng))
        using (var buffer = new MemoryStream())
        {
            var blobs = new byte[sizes.Length][];

            for (int i = 0; i < sizes.Length; i++)
            {
                int size = sizes[i];
                using (var canvas = new Bitmap(size, size, PixelFormat.Format32bppArgb))
                using (var graphics = Graphics.FromImage(canvas))
                {
                    graphics.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    graphics.PixelOffsetMode = PixelOffsetMode.HighQuality;
                    graphics.SmoothingMode = SmoothingMode.HighQuality;
                    graphics.CompositingQuality = CompositingQuality.HighQuality;
                    graphics.Clear(Color.Transparent);
                    graphics.DrawImage(source, new Rectangle(0, 0, size, size));

                    using (var single = new MemoryStream())
                    {
                        canvas.Save(single, ImageFormat.Png);
                        blobs[i] = single.ToArray();
                    }
                }
            }

            using (var writer = new BinaryWriter(buffer))
            {
                writer.Write((ushort)0);              // reserved
                writer.Write((ushort)1);              // type = icon
                writer.Write((ushort)sizes.Length);   // image count

                int offset = 6 + 16 * sizes.Length;
                for (int i = 0; i < sizes.Length; i++)
                {
                    writer.Write((byte)(sizes[i] >= 256 ? 0 : sizes[i])); // width（256 记作 0）
                    writer.Write((byte)(sizes[i] >= 256 ? 0 : sizes[i])); // height
                    writer.Write((byte)0);            // 调色板数
                    writer.Write((byte)0);            // reserved
                    writer.Write((ushort)1);          // color planes
                    writer.Write((ushort)32);         // bits per pixel
                    writer.Write((uint)blobs[i].Length);
                    writer.Write((uint)offset);
                    offset += blobs[i].Length;
                }
                for (int i = 0; i < sizes.Length; i++)
                {
                    writer.Write(blobs[i]);
                }
            }

            File.WriteAllBytes(targetIco, buffer.ToArray());
        }
    }
}
'@

[IcoMaker]::Make($sourcePng, $targetIco, @(16, 24, 32, 48, 64, 128, 256))

$info = Get-Item $targetIco
Write-Host ("[OK] 已生成 {0}（{1} 字节，尺寸 16/24/32/48/64/128/256）" -f $info.FullName, $info.Length)
