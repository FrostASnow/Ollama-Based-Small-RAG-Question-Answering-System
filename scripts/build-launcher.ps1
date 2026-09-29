# ============================================================================
#  build-launcher.ps1 —— 编译一体化启动器 RAG-QA.exe
#
#  为什么用 csc.exe（.NET Framework 4.x）而不是 .NET 9 / PyInstaller：
#    * Windows 10 1903+ / 11 自带 .NET Framework 4.8，**目标机器零安装**；
#    * 本机 C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe 直接可用，
#      不需要 .NET SDK、不需要 NuGet、不需要联网（本项目要求离线可用）；
#    * 编译出来的 exe 只有几十 KB，启动瞬时。
#  代价：编译器只支持 C# 5 语法，源码里刻意不用字符串插值、?. 等新语法。
#
#  用法：
#    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-launcher.ps1
#    powershell ... -File scripts\build-launcher.ps1 -CheckOnly    # 只检查是否已是最新
# ============================================================================

[CmdletBinding()]
param(
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch { }

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Source      = Join-Path $ProjectRoot 'launcher\RagQaLauncher.cs'
$IconPath    = Join-Path $ProjectRoot 'launcher\app.ico'
$Output      = Join-Path $ProjectRoot 'RAG-QA.exe'

function Write-Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Fix($text)  { Write-Host "  [BUILD] $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "  [FAIL] $text" -ForegroundColor Red }
function Write-Note($text) { Write-Host "  [INFO] $text" -ForegroundColor Gray }

function Find-Csc {
    $candidates = @(
        (Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $env:SystemRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# ---------------------------------------------------------------------------
# 源文件必须是 UTF-8 带 BOM
# ---------------------------------------------------------------------------
# csc.exe 靠 BOM 判断源文件编码；没有 BOM 时按系统 ANSI(936) 解析，
# 源码里的中文字符串会整片变成乱码 —— 界面上的中文全成问号。
# ---------------------------------------------------------------------------
if (-not (Test-Path $Source)) {
    Write-Bad "找不到源文件：$Source"
    exit 1
}

$bytes = [System.IO.File]::ReadAllBytes($Source)
$hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)
if (-not $hasBom) {
    if ($CheckOnly) {
        Write-Bad 'launcher\RagQaLauncher.cs 缺少 UTF-8 BOM（中文字符串会乱码）'
        exit 1
    }
    Write-Fix '源文件缺少 BOM，正在补上'
    $text = [System.IO.File]::ReadAllText($Source, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText($Source, $text, [System.Text.UTF8Encoding]::new($true))
}

# ---------------------------------------------------------------------------
# 新鲜度检查：exe 必须比源码新
# ---------------------------------------------------------------------------
$sourceTime = (Get-Item $Source).LastWriteTime
$exeExists = Test-Path $Output
if ($CheckOnly) {
    if (-not $exeExists) { Write-Bad 'RAG-QA.exe 不存在，请运行 scripts\build-launcher.cmd'; exit 1 }
    if ((Get-Item $Output).LastWriteTime -lt $sourceTime) {
        Write-Bad 'RAG-QA.exe 比源码旧，请重新运行 scripts\build-launcher.cmd'
        exit 1
    }
    Write-Ok 'RAG-QA.exe 已是最新'
    exit 0
}

$csc = Find-Csc
if (-not $csc) {
    Write-Bad '找不到 csc.exe（.NET Framework 4.x）。'
    Write-Note 'Windows 10/11 默认自带；若确实缺失，请安装 .NET Framework 4.8 运行时。'
    Write-Note '不影响主程序使用：仍可用 scripts\start.cmd / stop.cmd 启动和关闭。'
    exit 1
}

Write-Host '=== 生成图标 ===' -ForegroundColor Cyan
# 图标在构建时生成，仓库里就不用放二进制资源。
# ICO 容器里直接放 PNG 数据（Vista 以后支持），因此不需要自己写 BMP 位图。
function New-IconPng([int]$size) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias
    $g.Clear([System.Drawing.Color]::Transparent)

    $pad = [Math]::Max(1, [int]($size * 0.06))
    $side = $size - 2 * $pad
    $radius = [Math]::Max(2, [int]($size * 0.22))
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $path.AddArc($pad, $pad, $radius * 2, $radius * 2, 180, 90)
    $path.AddArc($pad + $side - $radius * 2, $pad, $radius * 2, $radius * 2, 270, 90)
    $path.AddArc($pad + $side - $radius * 2, $pad + $side - $radius * 2, $radius * 2, $radius * 2, 0, 90)
    $path.AddArc($pad, $pad + $side - $radius * 2, $radius * 2, $radius * 2, 90, 90)
    $path.CloseFigure()

    $brush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(47, 107, 255))
    $g.FillPath($brush, $path)

    $text = if ($size -ge 48) { 'RAG' } else { 'R' }
    $fontSize = if ($size -ge 48) { $size * 0.34 } else { $size * 0.62 }
    $font = New-Object System.Drawing.Font('Segoe UI', $fontSize, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
    $format = New-Object System.Drawing.StringFormat
    $format.Alignment = [System.Drawing.StringAlignment]::Center
    $format.LineAlignment = [System.Drawing.StringAlignment]::Center
    $white = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::White)
    $rect = New-Object System.Drawing.RectangleF(0, 0, $size, $size)
    $g.DrawString($text, $font, $white, $rect, $format)

    $stream = New-Object System.IO.MemoryStream
    $bmp.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    # 返回 MemoryStream 而不是 byte[]：PowerShell 会把函数返回的**数组展开**成
    # 多个对象，调用方拿到的是 object[] 而非 byte[]，写进 ICO 就是空数据
    # （第一次构建产出的 ico 只有 74 字节，就是这个坑）。
    $font.Dispose(); $brush.Dispose(); $white.Dispose(); $format.Dispose(); $g.Dispose(); $bmp.Dispose()
    return $stream
}

try {
    Add-Type -AssemblyName System.Drawing
    $sizes = @(16, 32, 48, 256)
    $images = @()
    foreach ($size in $sizes) {
        $stream = New-IconPng -size $size
        $images += ,$stream.ToArray()
        $stream.Dispose()
    }

    $ico = New-Object System.IO.MemoryStream
    $writer = New-Object System.IO.BinaryWriter($ico)
    $writer.Write([uint16]0)                  # reserved
    $writer.Write([uint16]1)                  # type: icon
    $writer.Write([uint16]$sizes.Count)       # image count
    $offset = 6 + 16 * $sizes.Count
    for ($i = 0; $i -lt $sizes.Count; $i++) {
        $size = $sizes[$i]
        $writer.Write([byte]$(if ($size -ge 256) { 0 } else { $size }))   # width（0 = 256）
        $writer.Write([byte]$(if ($size -ge 256) { 0 } else { $size }))   # height
        $writer.Write([byte]0)                # 调色板数
        $writer.Write([byte]0)                # reserved
        $writer.Write([uint16]1)              # color planes
        $writer.Write([uint16]32)             # bits per pixel
        $writer.Write([uint32]$images[$i].Length)
        $writer.Write([uint32]$offset)
        $offset += $images[$i].Length
    }
    foreach ($image in $images) { $writer.Write($image) }
    $writer.Flush()
    [System.IO.File]::WriteAllBytes($IconPath, $ico.ToArray())
    $writer.Dispose(); $ico.Dispose()
    Write-Ok ("已生成 launcher\app.ico（{0} 字节）" -f (Get-Item $IconPath).Length)
} catch {
    Write-Note "图标生成失败（不影响编译）：$($_.Exception.Message)"
    $IconPath = $null
}

Write-Host ''
Write-Host '=== 编译 RAG-QA.exe ===' -ForegroundColor Cyan
$arguments = @(
    '/nologo',
    '/target:winexe',          # 无控制台窗口（--status/--stop 会自己 AttachConsole）
    '/platform:anycpu',
    '/optimize+',
    '/utf8output',
    '/reference:System.dll',
    '/reference:System.Drawing.dll',
    '/reference:System.Windows.Forms.dll',
    '/reference:System.Web.Extensions.dll',
    ('/out:' + $Output)
)
if ($IconPath -and (Test-Path $IconPath)) { $arguments += ('/win32icon:' + $IconPath) }
$arguments += $Source

& $csc $arguments
if ($LASTEXITCODE -ne 0) {
    Write-Bad "编译失败（csc 退出码 $LASTEXITCODE）"
    exit 1
}

$info = Get-Item $Output
Write-Ok ("{0}   {1:N0} 字节   编译于 {2}" -f $info.Name, $info.Length, $info.LastWriteTime)

Write-Host ''
Write-Host '现在可以：' -ForegroundColor Cyan
Write-Host '  * 双击项目根目录的 RAG-QA.exe —— 图形面板，一键启动/停止' -ForegroundColor Gray
Write-Host '  * RAG-QA.exe --start / --stop / --status —— 供脚本调用' -ForegroundColor Gray
