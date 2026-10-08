# ============================================================================
#  start.ps1 —— 离线启动（Ollama + 后端 + 前端，前端由后端一并托管）
#  用法：双击 scripts\start.cmd，或
#    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start.ps1 [-Port 8080] [-NoBrowser] [-KeepOllama]
#  退出时会一并关闭 Ollama（-KeepOllama 可保留）。
# ============================================================================

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = '127.0.0.1',
    [switch]$NoBrowser,
    [switch]$KeepOllama,
    [switch]$Reload
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# 控制台编码：必须最先执行。中文 Windows 控制台默认代码页 936，脚本自身是 UTF-8(带 BOM)，
# 但输出会按控制台代码页编码 → 中文全乱码，看起来就像「程序启动失败」。
# PYTHONIOENCODING 让子进程 Python 也用 UTF-8，日志才不会乱码。
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # 没有真实控制台（输出被重定向）时设置会失败，忽略即可
}
$env:PYTHONIOENCODING = 'utf-8'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendDir  = Join-Path $ProjectRoot 'backend'
$VenvPython  = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$OllamaDir   = Join-Path $ProjectRoot 'tools\ollama'
$OllamaExe   = Join-Path $OllamaDir 'ollama.exe'
$ModelsDir   = Join-Path $ProjectRoot 'models\ollama'
$LogsDir     = Join-Path $ProjectRoot 'data\logs'
$OllamaUrl   = 'http://127.0.0.1:11434'

function Write-Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Write-Ok($t)   { Write-Host "[OK]   $t" -ForegroundColor Green }
function Write-Warn2($t){ Write-Host "[WARN] $t" -ForegroundColor Yellow }
function Write-Err($t)  { Write-Host "[FAIL] $t" -ForegroundColor Red }

function Test-OllamaUp {
    try {
        Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 2 | Out-Null
        return $true
    } catch { return $false }
}

# 便携版完整性：解压中断留下的「半个包」照样能 serve、照样能列出模型，只有提问会失败，
# 所以必须单独看一眼（判定逻辑与 prepare.ps1 共用 lib\ollama-runtime.ps1）。
. (Join-Path $PSScriptRoot 'lib\ollama-runtime.ps1')

if (-not (Test-Path $LogsDir)) { New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null }

Write-Host @"
============================================================================
 离线 RAG 文档问答
============================================================================
 项目目录 : $ProjectRoot
 访问地址 : http://${BindHost}:$Port
============================================================================
"@ -ForegroundColor White

Write-Step '1/3 环境检查'

if (-not (Test-Path $VenvPython)) {
    Write-Err "未找到虚拟环境：$VenvPython"
    Write-Host '  请先执行： 双击 scripts\prepare.cmd（首次需要联网）' -ForegroundColor Yellow
    exit 1
}
Write-Ok "Python：$(& $VenvPython --version 2>&1)"

$embDir = Join-Path $ProjectRoot 'models\all-MiniLM-L6-v2'
if (-not (Test-Path (Join-Path $embDir 'config.json'))) {
    Write-Warn2 "本地嵌入模型不存在：$embDir"
    Write-Host '  请先执行： 双击 scripts\prepare.cmd（首次需要联网）' -ForegroundColor Yellow
} else {
    Write-Ok '嵌入模型已就位'
}

Write-Step '2/3 启动 Ollama'

# 缺推理引擎时服务照样能起、模型照样能列，但提问一定失败；不在这里点出来，
# 用户要到提问时才发现。
if ((Test-Path $OllamaExe) -and -not (Test-OllamaPayload $OllamaDir)) {
    Write-Warn2 '项目内置的 Ollama 不完整（缺少 lib\ollama 下的推理运行时）'
    Write-Host '  服务能启动、/api/tags 也能列出模型，但任何提问都会失败：' -ForegroundColor Gray
    Write-Host '    error starting llama-server: llama-server binary not found' -ForegroundColor DarkGray
    Write-Host '  修复：双击 scripts\prepare.cmd（会自动清理并重新下载解压）' -ForegroundColor Yellow
    Write-Host '        或改装官方安装包 https://ollama.com/download' -ForegroundColor Yellow
    Write-Host '  网页首屏也会给出同样的提示与命令。' -ForegroundColor Gray
}

$ollamaStartedHere = $false
$ollamaPid = 0

if (Test-OllamaUp) {
    Write-Ok 'Ollama 服务已在运行'
} else {
    if (-not (Test-Path $OllamaExe)) {
        $sys = Get-Command ollama -ErrorAction SilentlyContinue
        if ($sys) { $OllamaExe = $sys.Source; $ModelsDir = $env:OLLAMA_MODELS }
    }

    if (-not (Test-Path $OllamaExe)) {
        Write-Warn2 '未找到 Ollama —— 问答功能暂时不可用（上传与检索不受影响）'
        Write-Host '  服务仍会正常启动，网页上会自动弹出安装引导' -ForegroundColor Gray
        Write-Host '  也可以现在双击 scripts\prepare.cmd 一次性装好（需要联网）' -ForegroundColor Gray
    } else {
        if ($ModelsDir) {
            $env:OLLAMA_MODELS = $ModelsDir
            if (-not (Test-Path $ModelsDir)) { New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null }
        }

        Write-Host "  启动：$OllamaExe serve"
        # -PassThru 记下 PID：退出时按进程树精确回收，而不是把所有叫 ollama 的都杀掉
        $ollamaProcess = Start-Process -FilePath $OllamaExe -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
            -RedirectStandardError (Join-Path $LogsDir 'ollama.err.log') `
            -RedirectStandardOutput (Join-Path $LogsDir 'ollama.out.log')
        if ($ollamaProcess) { $ollamaPid = $ollamaProcess.Id }
        $ollamaStartedHere = $true

        $up = $false
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 500
            if (Test-OllamaUp) { $up = $true; break }
        }
        if ($up) { Write-Ok "Ollama 服务已启动（PID $ollamaPid）" }
        else { Write-Warn2 "Ollama 启动超时，详见 $(Join-Path $LogsDir 'ollama.err.log')" }
    }
}

Write-Step '3/3 启动后端服务'

if (-not (Test-Path $BackendDir)) {
    Write-Err "未找到后端目录：$BackendDir"
    exit 1
}

$BaseUrl = "http://${BindHost}:$Port"

# 启动前预检：先做 TCP 探测（端口空闲时瞬间返回），被占用后再调 /api/health 判断是否本项目自己。
# 反过来先调 health 会白等一次 HTTP 往返，超时留不够还会把自己的实例误判成「别人占用」。
$portBusy = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect($BindHost, $Port)
    $client.Close()
    $portBusy = $true
} catch { }

if ($portBusy) {
    $existingName = $null
    try {
        $existing = Invoke-RestMethod -Uri "$BaseUrl/api/health" -TimeoutSec 6 -ErrorAction Stop
        if ($existing.app_name) { $existingName = $existing.app_name }
    } catch { }

    if ($existingName) {
        Write-Warn2 "端口 $Port 上已经有一个「$existingName」实例在运行"
        Write-Host "  直接访问：$BaseUrl" -ForegroundColor Cyan
        Write-Host '  需要重启的话，先运行 scripts\stop.ps1，再重新启动' -ForegroundColor Gray
        if ($ollamaStartedHere) {
            # 本轮拉起的 Ollama 要留给那个实例用，不能顺手关掉（否则会把在跑的服务弄瘫）
            Write-Host "  注意：刚启动的 Ollama（PID $ollamaPid）会保留，供那个实例使用；" -ForegroundColor Yellow
            Write-Host '        全部关掉请运行 scripts\stop.ps1' -ForegroundColor Yellow
        }
        exit 0
    }

    Write-Err "端口 $Port 已被其它程序占用"
    Write-Host "  换一个端口重试，例如：" -ForegroundColor Yellow
    Write-Host "      scripts\start.cmd -Port 8080" -ForegroundColor Yellow
    exit 1
}

$uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', $BindHost, '--port', "$Port")
if ($Reload) { $uvicornArgs += '--reload' }

Write-Host "  运行：$VenvPython $($uvicornArgs -join ' ')" -ForegroundColor Gray
Write-Host "  浏览器访问：$BaseUrl" -ForegroundColor Cyan
Write-Host '  首次启动要花几秒加载本地嵌入模型（在后台进行，不影响端口监听）' -ForegroundColor Gray
Write-Host '  按 Ctrl+C 停止服务' -ForegroundColor Gray
Write-Host ''

# 浏览器由后端在「确认自己能响应 HTTP」之后再打开：把地址交给 RAG_OPEN_BROWSER_URL，
# 而不是猜启动耗时（猜早了用户只会先看到 ERR_CONNECTION_REFUSED）。
# -NoBrowser 只把 RAG_AUTO_OPEN_BROWSER 置 0，地址照样传给后端用于日志显示。
$env:RAG_OPEN_BROWSER_URL = $BaseUrl
if ($NoBrowser) {
    $env:RAG_AUTO_OPEN_BROWSER = '0'
    Write-Host "  已关闭自动打开浏览器（-NoBrowser），请手动访问：$BaseUrl" -ForegroundColor Gray
} else {
    $env:RAG_AUTO_OPEN_BROWSER = '1'
    Write-Host '  服务就绪后会自动打开浏览器，无需手动刷新' -ForegroundColor Gray
}

# 前台运行 uvicorn（日志进本窗口，Ctrl+C 自然传递）；必须切到 backend/ 目录才能解析 app.main:app
$exitCode = 0
Push-Location $BackendDir
try {
    & $VenvPython @uvicornArgs
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location

    # 退出清理：把本项目用到的 Ollama 一起关掉（否则会长期挂着一个占显存的进程）。
    # 三种都要覆盖：本次拉起的（按 PID 杀进程树）、项目内置的、之前就在运行的（靠监听 11434 定位）。
    if ($KeepOllama) {
        Write-Host "`n已保留 Ollama（-KeepOllama）。它会继续占用显存，需要时用：" -ForegroundColor Gray
        Write-Host '    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop.ps1' -ForegroundColor Gray
    } else {
        Write-Host "`n正在停止 Ollama ..." -ForegroundColor Gray
        if ($ollamaStartedHere) {
            Write-Host "  （本次由启动脚本拉起，PID $ollamaPid）" -ForegroundColor Gray
        }
        $stopped = @(Stop-PortableOllamaForProject -Dir $OllamaDir -ProcessId $ollamaPid -Port 11434)
        if ($stopped.Count -gt 0) {
            Write-Host "[OK]   已停止 Ollama（PID $($stopped -join ', ')）" -ForegroundColor Green
        } elseif (Test-OllamaPortListening -Port 11434) {
            Write-Warn2 'Ollama 端口仍在监听，未能停止。可手动执行：'
            Write-Host '    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop.ps1' -ForegroundColor Yellow
        } else {
            Write-Host '  没有需要停止的 Ollama。' -ForegroundColor Gray
        }
    }
    Write-Host '已停止。' -ForegroundColor Green
}

if ($exitCode -ne 0) {
    Write-Host ''
    Write-Err "后端退出，退出码 $exitCode"
    Write-Host "  若提示端口被占用，可换端口： scripts\start.cmd -Port 8080" -ForegroundColor Yellow
    Write-Host "  详细日志： $(Join-Path $LogsDir 'backend.log')" -ForegroundColor Yellow
    exit $exitCode
}
