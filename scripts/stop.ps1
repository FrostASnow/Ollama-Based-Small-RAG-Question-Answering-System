# ============================================================================
#  stop.ps1 —— 停止本项目相关的进程（后端 + Ollama）
#
#  用法：
#    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop.ps1
#
#  注意：如果 Ollama 是你自己安装并常驻使用的，加 -KeepOllama 只停后端。
# ============================================================================

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$KeepOllama
)

$ErrorActionPreference = 'SilentlyContinue'

# 控制台切到 UTF-8，否则中文提示在默认代码页(936)下是乱码
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch { }

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython  = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

function Write-Err($text) { Write-Host "[FAIL] $text" -ForegroundColor Red }
function Write-Warn2($text) { Write-Host "[WARN] $text" -ForegroundColor Yellow }

Write-Host '=== 停止后端 ===' -ForegroundColor Cyan

# 精确匹配本项目 venv 启动的 uvicorn，避免误杀其他 Python 进程
$killed = 0
$procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue
foreach ($p in $procs) {
    $cmd = $p.CommandLine
    if ($cmd -and $cmd -like "*uvicorn*app.main:app*" -and $cmd -like "*$ProjectRoot*") {
        Write-Host "  结束进程 PID=$($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }
}

# 回退：按端口占用查找
if ($killed -eq 0) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conn) {
        $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -eq 'python') {
            Write-Host "  结束占用 $Port 端口的进程 PID=$($proc.Id)"
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            $killed++
        }
    }
}

if ($killed -eq 0) { Write-Host '  没有发现运行中的后端。' -ForegroundColor Gray }
else { Write-Host "[OK]   已停止 $killed 个后端进程" -ForegroundColor Green }

if ($KeepOllama) {
    Write-Host '=== 保留 Ollama（-KeepOllama）===' -ForegroundColor Cyan
    Write-Host '  注意：模型仍占用显存，需要时手动结束 Ollama 进程。' -ForegroundColor Gray
} else {
    Write-Host '=== 停止 Ollama ===' -ForegroundColor Cyan

    # 与 start.ps1 退出时走同一套逻辑（scripts\lib\ollama-runtime.ps1）：
    #   1. 项目内置的那份（路径在 tools\ollama 下）
    #   2. 端口 11434 上仍在监听的（可能是之前留下或系统安装的）
    # 这样「后端停了、Ollama 还在后台吃显存」的情况不会出现。
    $lib = Join-Path $PSScriptRoot 'lib\ollama-runtime.ps1'
    if (-not (Test-Path $lib)) {
        Write-Err "缺少 $lib，无法安全停止 Ollama"
        exit 1
    }
    . $lib

    $stopped = @(Stop-PortableOllamaForProject -Dir (Join-Path $ProjectRoot 'tools\ollama') -Port 11434)
    if ($stopped.Count -gt 0) {
        Write-Host "[OK]   已停止 Ollama（PID $($stopped -join ', ')）" -ForegroundColor Green
    } elseif (Test-OllamaPortListening -Port 11434) {
        Write-Warn2 '端口 11434 仍在监听，但监听者不是 ollama 进程（可能是别的程序占用）'
    } else {
        Write-Host '  没有发现运行中的 Ollama。' -ForegroundColor Gray
    }
}
