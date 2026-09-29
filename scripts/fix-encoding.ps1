# ============================================================================
#  fix-encoding.ps1 —— 修复脚本文件编码
#
#  为什么需要它：
#    * .ps1 用编辑器/工具重写后容易丢掉 UTF-8 BOM。Windows PowerShell 5.1
#      遇到没有 BOM 的文件会按系统 ANSI 代码页（中文机器上是 GBK）解析，
#      中文的字节错位会吃掉字符串的引号，最后报一句
#      "Unexpected token" —— 完全看不出真正原因。
#    * .cmd 则相反，必须保持纯 ASCII：cmd.exe 按 OEM 代码页读取，
#      含中文时注释行会被撕成碎片，碎片反过来被当成命令执行。
#
#  本脚本会把 .ps1 补上 BOM、并检查 .cmd 是否仍为纯 ASCII。
#  用法：
#    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix-encoding.ps1
# ============================================================================

[CmdletBinding()]
param(
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot

$ps1Files = @(
    'scripts\prepare.ps1',
    'scripts\start.ps1',
    'scripts\stop.ps1',
    'scripts\fix-encoding.ps1',
    'scripts\build-launcher.ps1',
    'scripts\lib\ollama-runtime.ps1',
    'tests\run_all.ps1',
    'tests\test_launcher.ps1'
)
# .cs 同样需要 BOM：csc.exe 靠 BOM 判断源文件编码，
# 没有 BOM 时会按系统 ANSI(936) 解析，中文界面文字全变乱码。
$csFiles = @(
    'launcher\RagQaLauncher.cs'
)
$cmdFiles = @(
    'scripts\prepare.cmd',
    'scripts\start.cmd',
    'scripts\build-launcher.cmd'
)

$utf8Bom = [System.Text.UTF8Encoding]::new($true)
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$changed = 0
$problems = 0

Write-Host '=== .ps1 / .cs 需要 UTF-8 BOM ===' -ForegroundColor Cyan
foreach ($rel in ($ps1Files + $csFiles)) {
    $path = Join-Path $ProjectRoot $rel
    if (-not (Test-Path $path)) { Write-Host "  [skip] $rel（不存在）"; continue }

    $bytes = [System.IO.File]::ReadAllBytes($path)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)

    if ($hasBom) {
        Write-Host "  [ok]   $rel" -ForegroundColor Green
    } elseif ($CheckOnly) {
        Write-Host "  [FAIL] $rel 缺少 BOM" -ForegroundColor Red
        $problems++
    } else {
        $text = [System.IO.File]::ReadAllText($path, $utf8NoBom)
        [System.IO.File]::WriteAllText($path, $text, $utf8Bom)
        Write-Host "  [fix]  $rel 已补加 BOM" -ForegroundColor Yellow
        $changed++
    }
}

Write-Host ''
Write-Host '=== .cmd 必须纯 ASCII ===' -ForegroundColor Cyan
foreach ($rel in $cmdFiles) {
    $path = Join-Path $ProjectRoot $rel
    if (-not (Test-Path $path)) { Write-Host "  [skip] $rel（不存在）"; continue }

    $bytes = [System.IO.File]::ReadAllBytes($path)
    $nonAscii = @($bytes | Where-Object { $_ -gt 127 }).Count

    if ($nonAscii -eq 0) {
        Write-Host "  [ok]   $rel" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $rel 含 $nonAscii 个非 ASCII 字节" -ForegroundColor Red
        Write-Host '         cmd.exe 会把中文注释误解析成命令，请改成纯 ASCII' -ForegroundColor Yellow
        $problems++
    }
}

Write-Host ''
if ($problems -gt 0) {
    Write-Host "有 $problems 处需要人工处理。" -ForegroundColor Red
    exit 1
}
if ($changed -gt 0) {
    Write-Host "已修复 $changed 个文件。" -ForegroundColor Green
} else {
    Write-Host '所有脚本编码正确。' -ForegroundColor Green
}
