# ============================================================================
#  run_all.ps1 —— 一键跑完所有测试
#  用法：
#    pwsh -File tests\run_all.ps1 [-SkipHttp]     # -SkipHttp 不跑需要在线服务的 HTTP 验收
#  HTTP 验收需要后端已在运行；若未启动会自动拉起临时实例并在结束后关闭。
# ============================================================================

[CmdletBinding()]
param(
    [switch]$SkipHttp,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython  = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$BackendDir  = Join-Path $ProjectRoot 'backend'

# 控制台切到 UTF-8，否则中文测试结果在默认代码页(936)下是乱码
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch { }
$env:PYTHONIOENCODING = 'utf-8'

$script:Results = @()

function Invoke-Suite {
    param([string]$Name, [string]$File, [string[]]$SuiteArgs = @())

    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkGray
    Write-Host "  $Name" -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkGray

    $path = Join-Path $PSScriptRoot $File
    # 刻意不把输出丢给 Out-Null：否则连子进程 stdout 一并被吞，套件失败时毫无线索。
    # 参数名不能叫 $Args —— 那是 PowerShell 的自动变量。
    & $VenvPython $path @SuiteArgs
    $code = $LASTEXITCODE

    $script:Results += [pscustomobject]@{ Name = $Name; Passed = ($code -eq 0) }
}

if (-not (Test-Path $VenvPython)) {
    Write-Host "[FAIL] 未找到虚拟环境：$VenvPython" -ForegroundColor Red
    Write-Host "       请先执行： 双击 scripts\prepare.cmd（首次需要联网）" -ForegroundColor Yellow
    exit 1
}

Write-Host @"
============================================================================
 离线 RAG 文档问答 —— 测试套件
============================================================================
 Python : $VenvPython
 项目   : $ProjectRoot
============================================================================
"@ -ForegroundColor White

# 预检：脚本编码与语法。两条规则一旦破坏，故障都在「启动阶段」且极隐晦：
# .ps1 丢 BOM → PS 5.1 按 GBK 解析，字节错位吃掉引号，报 "Unexpected token"；
# .cmd 含非 ASCII → cmd.exe 按 OEM 代码页读，注释碎片被当成命令执行。
Write-Host ''
Write-Host ('=' * 74) -ForegroundColor DarkGray
Write-Host '  预检：脚本编码与语法' -ForegroundColor Cyan
Write-Host ('=' * 74) -ForegroundColor DarkGray

$scriptOk = $true

foreach ($rel in @('scripts\prepare.ps1', 'scripts\start.ps1', 'scripts\stop.ps1', 'scripts\fix-encoding.ps1', 'scripts\build-launcher.ps1', 'scripts\lib\ollama-runtime.ps1', 'tests\run_all.ps1', 'tests\test_launcher.ps1', 'tests\hang-probe.ps1', 'launcher\RagQaLauncher.cs')) {
    $path = Join-Path $ProjectRoot $rel
    $bytes = [System.IO.File]::ReadAllBytes($path)

    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)
    if ($hasBom) {
        Write-Host "  [PASS] $rel 含 UTF-8 BOM" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $rel 缺少 UTF-8 BOM（PowerShell 5.1 会按 GBK 解析并报错）" -ForegroundColor Red
        $scriptOk = $false
    }

    $parseErrors = $null
    # 只对 .ps1 做语法检查：.cs 只是借用同一套 BOM 规则（csc 也靠 BOM 识别编码），
    # 拿 PowerShell 解析器读它必然误报。
    if ($rel -like '*.ps1') {
        [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$parseErrors) | Out-Null
        if ($parseErrors.Count -eq 0) {
            Write-Host "  [PASS] $rel 语法正确" -ForegroundColor Green
        } else {
            Write-Host "  [FAIL] $rel 语法错误：$($parseErrors[0].Message)" -ForegroundColor Red
            $scriptOk = $false
        }
    }
}

foreach ($rel in @('scripts\start.cmd', 'scripts\prepare.cmd', 'scripts\build-launcher.cmd')) {
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $ProjectRoot $rel))
    $nonAscii = @($bytes | Where-Object { $_ -gt 127 }).Count
    if ($nonAscii -eq 0) {
        Write-Host "  [PASS] $rel 纯 ASCII" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $rel 含 $nonAscii 个非 ASCII 字节（cmd.exe 会误解析成命令）" -ForegroundColor Red
        $scriptOk = $false
    }
}

# 预检：便携版 Ollama 的完整性 / 清理逻辑（真实跑一遍，不只是看字符串）。
# 要挡住两个坑：解压中断留下「只有 ollama.exe」的假安装（能 serve、能列模型，
# 提问才报 llama-server not found）；以及删除被运行中进程占用的目录会静默失败。
$libPath = Join-Path $ProjectRoot 'scripts\lib\ollama-runtime.ps1'
if (-not (Test-Path $libPath)) {
    Write-Host "  [FAIL] 缺少 $libPath" -ForegroundColor Red
    $scriptOk = $false
} else {
    . $libPath

    $probeRoot = Join-Path $ProjectRoot '.tmp\tests\ollama_lock'
    $probeExe = Join-Path $probeRoot 'ollama.exe'
    $probeRuntime = Join-Path $probeRoot 'lib\ollama'
    $lockProcess = $null

    try {
        if (Test-Path $probeRoot) { Remove-Item -Recurse -Force $probeRoot -ErrorAction SilentlyContinue }
        New-Item -ItemType Directory -Force -Path $probeRuntime | Out-Null
        Set-Content -Path (Join-Path $probeRuntime 'ggml.dll') -Value 'x'

        # (a) 只有 ollama.exe → 判定不完整
        Set-Content -Path $probeExe -Value 'x'
        if (-not (Test-OllamaPayload -Dir $probeRoot)) {
            Write-Host '  [PASS] 半成品（只有 ollama.exe）判为不完整' -ForegroundColor Green
        } else {
            Write-Host '  [FAIL] 半成品被判为完整' -ForegroundColor Red
            $scriptOk = $false
        }

        # (b) 补上推理引擎 → 判定完整
        Set-Content -Path (Join-Path $probeRuntime 'llama-server.exe') -Value 'x'
        if (Test-OllamaPayload -Dir $probeRoot) {
            Write-Host '  [PASS] 含 llama-server.exe 判为完整' -ForegroundColor Green
        } else {
            Write-Host '  [FAIL] 完整安装未被识别' -ForegroundColor Red
            $scriptOk = $false
        }

        # (c) zip 校验：垃圾文件不算 zip，真 zip 才算
        $junkZip = Join-Path $probeRoot 'junk.zip'
        Set-Content -Path $junkZip -Value 'this is not a zip'
        $goodZip = Join-Path $probeRoot 'good.zip'
        Compress-Archive -Path $probeExe -DestinationPath $goodZip -Force
        if ((-not (Test-ZipReadable -Path $junkZip)) -and (Test-ZipReadable -Path $goodZip)) {
            Write-Host '  [PASS] 半成品 zip 被识破，完整 zip 被接受' -ForegroundColor Green
        } else {
            Write-Host '  [FAIL] zip 完整性判定不正确' -ForegroundColor Red
            $scriptOk = $false
        }

        # (d) 文件被运行中的进程占用时必须能自动停掉并删除干净（用 ping.exe 的副本
        #     冒充 ollama.exe：进程名随文件名变成 ollama，路径落在被清理的目录内）。
        Remove-Item -Force $probeExe -ErrorAction SilentlyContinue
        Copy-Item -Path (Join-Path $env:SystemRoot 'System32\ping.exe') -Destination $probeExe -Force
        try {
            $lockProcess = Start-Process -FilePath $probeExe -ArgumentList '-t', '127.0.0.1' `
                -WindowStyle Hidden -PassThru -ErrorAction Stop
            Start-Sleep -Milliseconds 700
        } catch {
            $lockProcess = $null
        }

        if ($lockProcess) {
            $removed = Remove-OllamaDir -Dir $probeRoot -Exe $probeExe
            $gone = -not (Test-Path $probeExe)
            if ($removed -and $gone) {
                Write-Host '  [PASS] 运行中的 ollama.exe 被停止后目录成功删除' -ForegroundColor Green
            } else {
                Write-Host '  [FAIL] 目录被占用时未能清理干净（这正是「安装成功却没换文件」的根因）' -ForegroundColor Red
                $scriptOk = $false
            }
        } else {
            Write-Host '  [SKIP] 无法启动占用测试进程，跳过占用场景' -ForegroundColor Yellow
        }

        # (e) 退出清理：按 PID 结束进程树（start.ps1 / stop.ps1 退出时走的就是它）。
        Remove-Item -Recurse -Force $probeRoot -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Force -Path $probeRoot | Out-Null
        Copy-Item -Path (Join-Path $env:SystemRoot 'System32\ping.exe') -Destination $probeExe -Force
        $treeProcess = $null
        try {
            $treeProcess = Start-Process -FilePath $probeExe -ArgumentList '-t', '127.0.0.1' `
                -WindowStyle Hidden -PassThru -ErrorAction Stop
            Start-Sleep -Milliseconds 700
        } catch {
            $treeProcess = $null
        }

        if ($treeProcess) {
            $killed = @(Stop-PortableOllamaForProject -Dir $probeRoot -ProcessId $treeProcess.Id -Port 1)
            Start-Sleep -Milliseconds 500
            $stillAlive = @(Get-Process -Id $treeProcess.Id -ErrorAction SilentlyContinue).Count -gt 0
            if (($killed -contains $treeProcess.Id) -and (-not $stillAlive)) {
                Write-Host '  [PASS] 退出清理能按 PID 结束 ollama 进程' -ForegroundColor Green
            } else {
                Write-Host '  [FAIL] 退出清理没能结束进程（程序关了 Ollama 还留着）' -ForegroundColor Red
                $scriptOk = $false
            }
            if (Test-OllamaPortListening -Port 1) {
                Write-Host '  [FAIL] 端口探测函数把无关端口判成在监听' -ForegroundColor Red
                $scriptOk = $false
            }
        } else {
            Write-Host '  [SKIP] 无法启动进程，跳过退出清理场景' -ForegroundColor Yellow
        }
    } catch {
        Write-Host "  [FAIL] Ollama 完整性工具测试异常：$($_.Exception.Message)" -ForegroundColor Red
        $scriptOk = $false
    } finally {
        if ($lockProcess -and -not $lockProcess.HasExited) {
            Stop-Process -Id $lockProcess.Id -Force -ErrorAction SilentlyContinue
        }
        if ($treeProcess -and -not $treeProcess.HasExited) {
            Stop-Process -Id $treeProcess.Id -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Milliseconds 300
        Remove-Item -Recurse -Force $probeRoot -ErrorAction SilentlyContinue
    }

    # 两个「会退出」的脚本都必须调用退出清理，且都要能保留 Ollama
    foreach ($script in @('scripts\start.ps1', 'scripts\stop.ps1')) {
        $text = Get-Content (Join-Path $ProjectRoot $script) -Raw -Encoding UTF8
        if ($text -match 'Stop-PortableOllamaForProject') {
            Write-Host "  [PASS] $script 退出时清理 Ollama" -ForegroundColor Green
        } else {
            Write-Host "  [FAIL] $script 未在退出时清理 Ollama" -ForegroundColor Red
            $scriptOk = $false
        }
    }
    $startText = Get-Content (Join-Path $ProjectRoot 'scripts\start.ps1') -Raw -Encoding UTF8
    if ($startText -match '\$KeepOllama') {
        Write-Host '  [PASS] start.ps1 提供 -KeepOllama 保留开关' -ForegroundColor Green
    } else {
        Write-Host '  [FAIL] start.ps1 没有 -KeepOllama 开关（想保留 Ollama 的用户无路可走）' -ForegroundColor Red
        $scriptOk = $false
    }

    # prepare.ps1 必须检查删除结果（不能删不掉还继续往下当成功）
    $prepareText = Get-Content (Join-Path $ProjectRoot 'scripts\prepare.ps1') -Raw -Encoding UTF8
    foreach ($needle in @('Remove-OllamaDir', 'Test-OllamaPayload', 'Test-ZipReadable')) {
        if ($prepareText -match $needle) {
            Write-Host "  [PASS] prepare.ps1 使用 $needle" -ForegroundColor Green
        } else {
            Write-Host "  [FAIL] prepare.ps1 未使用 $needle（半成品会被误判成已就绪）" -ForegroundColor Red
            $scriptOk = $false
        }
    }
    if ($prepareText -match 'if \(-not \(Remove-OllamaDir') {
        Write-Host '  [PASS] prepare.ps1 检查了删除结果（删不掉就报错退出）' -ForegroundColor Green
    } else {
        Write-Host '  [FAIL] prepare.ps1 未检查删除结果：删不掉时会静默当成已装好' -ForegroundColor Red
        $scriptOk = $false
    }

    # --github-asset 的 latest 标签必须走 /releases/latest：/releases/tags/latest 会 404。
    $fetchText = Get-Content (Join-Path $ProjectRoot 'scripts\fetch.mjs') -Raw -Encoding UTF8
    if ($fetchText -match 'releases/latest' -and $fetchText -match "tag === 'latest'") {
        Write-Host '  [PASS] fetch.mjs 正确处理 latest 标签' -ForegroundColor Green
    } else {
        Write-Host '  [FAIL] fetch.mjs 未特殊处理 latest（会请求 /releases/tags/latest 并 404）' -ForegroundColor Red
        $scriptOk = $false
    }

    # 进度必须是 \n 结尾的整行：PS 5.1 按行转发原生命令输出，只有 \r 的进度会被
    # 攒到进程退出才落盘，下载在日志里全程一片空白。
    if ($fetchText -match 'PROGRESS_INTERVAL_MS' -and $fetchText -match 'IS_TTY') {
        Write-Host '  [PASS] fetch.mjs 在非交互场景输出整行进度' -ForegroundColor Green
    } else {
        Write-Host '  [FAIL] fetch.mjs 只用 \r 刷进度（安装日志里会长时间一片空白）' -ForegroundColor Red
        $scriptOk = $false
    }
}

$script:Results += [pscustomobject]@{ Name = '预检：脚本编码与语法'; Passed = $scriptOk }

# ---------------------------------------------------------------------------
Invoke-Suite -Name '1/9 依赖导入体检' -File 'check_imports.py'

# ---------------------------------------------------------------------------
Invoke-Suite -Name '2/9 离线核心链路（嵌入 + FAISS + 切分）' -File 'test_offline.py'

# ---------------------------------------------------------------------------
Invoke-Suite -Name '3/9 RAG 端到端（假 LLM）' -File 'test_e2e.py'

# 索引一致性：换模型 / 改切分参数 / 清空知识库之后，索引与配置还对得上吗。
# 专盯「不报错但结果已经不对」的静默故障，故自带 RAG_DATA_DIR 隔离（见文件头）。
Invoke-Suite -Name '4/9 索引一致性与配置对称性' -File 'test_index_consistency.py'

# ---------------------------------------------------------------------------
Invoke-Suite -Name '5/9 Ollama 集成（协议兼容假服务）' -File 'test_ollama_integration.py'

# ---------------------------------------------------------------------------
if ($SkipHttp) {
    Write-Host ''
    Write-Host '6/9 HTTP 层验收 —— 已跳过 (-SkipHttp)' -ForegroundColor Yellow
    $script:Results += [pscustomobject]@{ Name = '6/9 HTTP 层验收'; Passed = $null }
} else {
    # HTTP 验收会 DELETE /api/documents（清空知识库），所以必须跑在临时数据目录上：
    # 总是新起专用实例（RAG_DATA_DIR 指向 .tmp），绝不复用用户正在使用的服务。
    $serverProcess = $null
    $testDataDir = Join-Path $PSScriptRoot '..\.tmp\http-suite-data'
    $testDataDir = [System.IO.Path]::GetFullPath($testDataDir)
    New-Item -ItemType Directory -Path $testDataDir -Force | Out-Null

    # 找一个空闲端口，避免和用户正在运行的服务撞车
    $testPort = $Port
    for ($candidate = $Port; $candidate -lt ($Port + 30); $candidate++) {
        $busy = $false
        try {
            $probe = [System.Net.Sockets.TcpClient]::new()
            $probe.Connect('127.0.0.1', $candidate)
            $probe.Close()
            $busy = $true
        } catch { }
        if (-not $busy) { $testPort = $candidate; break }
    }

    Write-Host ''
    Write-Host "  启动专用测试后端：端口 $testPort，数据目录 .tmp\http-suite-data" -ForegroundColor Gray
    $env:RAG_DATA_DIR = $testDataDir
    $serverProcess = Start-Process -FilePath $VenvPython `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$testPort", '--log-level', 'warning') `
        -WorkingDirectory $BackendDir -PassThru -WindowStyle Hidden
    Remove-Item Env:\RAG_DATA_DIR -ErrorAction SilentlyContinue

    Start-Sleep -Seconds 2

    try {
        Invoke-Suite -Name '6/9 HTTP 层验收（专用临时实例）' -File 'test_http.py' `
            -SuiteArgs @('--base', "http://127.0.0.1:$testPort")
    } finally {
        if ($serverProcess -and -not $serverProcess.HasExited) {
            Write-Host "  关闭临时后端（PID $($serverProcess.Id)）" -ForegroundColor Gray
            Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# 前端测试：静态一致性（DOM id / 模块导出）+ 真实交互（DOM 垫片里跑 app.js）
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Host ''
    Write-Host '7/9、8/9 前端测试 —— 已跳过（未找到 node）' -ForegroundColor Yellow
    $script:Results += [pscustomobject]@{ Name = '7/9 前端测试'; Passed = $null }
    $script:Results += [pscustomobject]@{ Name = '8/9 前端交互测试'; Passed = $null }
} else {
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkGray
    Write-Host '  7/9 前端测试（渲染 + 静态一致性）' -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkGray

    & node (Join-Path $PSScriptRoot 'test_frontend.mjs')
    $script:Results += [pscustomobject]@{
        Name   = '7/9 前端测试'
        Passed = ($LASTEXITCODE -eq 0)
    }

    # 静态检查证明代码自洽，证明不了行为：这一套把真实的 app.js 装进
    # tests/dom-lite.mjs 的 DOM 垫片里跑，断言点击/输入/流式渲染真正发生了什么。
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkGray
    Write-Host '  8/9 前端交互测试（真实 DOM + 假后端）' -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkGray

    & node (Join-Path $PSScriptRoot 'test_frontend_interaction.mjs')
    $script:Results += [pscustomobject]@{
        Name   = '8/9 前端交互测试'
        Passed = ($LASTEXITCODE -eq 0)
    }
}

# 一体化启动器：用户双击的 RAG-QA.exe。它坏了主程序测试全绿也发现不了，
# 所以单独验收：产物新鲜度、--status 自检、GUI 窗口、--start/--stop 闭环。
Write-Host ''
Write-Host ('=' * 74) -ForegroundColor DarkGray
Write-Host '  9/9 一体化启动器（RAG-QA.exe）' -ForegroundColor Cyan
Write-Host ('=' * 74) -ForegroundColor DarkGray

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'test_launcher.ps1')
$script:Results += [pscustomobject]@{
    Name   = '9/9 一体化启动器'
    Passed = ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------------------
Write-Host ''
Write-Host ('=' * 74) -ForegroundColor DarkGray
Write-Host ' 测试汇总' -ForegroundColor Cyan
Write-Host ('=' * 74) -ForegroundColor DarkGray

$failed = 0
foreach ($result in $script:Results) {
    if ($null -eq $result.Passed) {
        Write-Host ("  [跳过] {0}" -f $result.Name) -ForegroundColor Yellow
    } elseif ($result.Passed) {
        Write-Host ("  [通过] {0}" -f $result.Name) -ForegroundColor Green
    } else {
        Write-Host ("  [失败] {0}" -f $result.Name) -ForegroundColor Red
        $failed++
    }
}

Write-Host ''
if ($failed -eq 0) {
    Write-Host ' 全部通过。' -ForegroundColor Green
    exit 0
}
Write-Host " 有 $failed 个套件失败。" -ForegroundColor Red
exit 1
