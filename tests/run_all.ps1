# ============================================================================
#  run_all.ps1 —— 一键跑完所有测试
#
#  用法：
#    pwsh -File tests\run_all.ps1
#    pwsh -File tests\run_all.ps1 -SkipHttp        # 不跑需要在线服务的 HTTP 验收
#
#  说明：
#    HTTP 验收需要后端已在运行（scripts\start.ps1）。
#    若检测到服务未启动，会自动拉起一个临时实例并在结束后关闭。
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
    param([string]$Name, [string]$File, [string[]]$Args = @())

    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkGray
    Write-Host "  $Name" -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkGray

    $path = Join-Path $PSScriptRoot $File
    & $VenvPython $path @Args
    $code = $LASTEXITCODE

    $script:Results += [pscustomobject]@{ Name = $Name; Passed = ($code -eq 0) }
    return $code
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

# ---------------------------------------------------------------------------
# 预检：脚本编码与语法
# ---------------------------------------------------------------------------
# 这两条规则一旦被破坏，故障表现都极其隐晦，而且都在「启动阶段」：
#   * .ps1 丢掉 UTF-8 BOM → PowerShell 5.1 按 GBK 解析中文，
#     字节错位会吃掉字符串的引号，报 "Unexpected token" 让人完全摸不着头脑
#   * .cmd 含非 ASCII   → cmd.exe 按 OEM 代码页读，注释行被撕成碎片，
#     碎片反过来被当成命令执行，脚本一行都跑不到
# 放进测试套件，避免以后编辑文件时再次踩坑。
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host ('=' * 74) -ForegroundColor DarkGray
Write-Host '  预检：脚本编码与语法' -ForegroundColor Cyan
Write-Host ('=' * 74) -ForegroundColor DarkGray

$scriptOk = $true

foreach ($rel in @('scripts\prepare.ps1', 'scripts\start.ps1', 'scripts\stop.ps1', 'scripts\lib\ollama-runtime.ps1', 'tests\run_all.ps1')) {
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
    [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$parseErrors) | Out-Null
    if ($parseErrors.Count -eq 0) {
        Write-Host "  [PASS] $rel 语法正确" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $rel 语法错误：$($parseErrors[0].Message)" -ForegroundColor Red
        $scriptOk = $false
    }
}

foreach ($rel in @('scripts\start.cmd', 'scripts\prepare.cmd')) {
    $bytes = [System.IO.File]::ReadAllBytes((Join-Path $ProjectRoot $rel))
    $nonAscii = @($bytes | Where-Object { $_ -gt 127 }).Count
    if ($nonAscii -eq 0) {
        Write-Host "  [PASS] $rel 纯 ASCII" -ForegroundColor Green
    } else {
        Write-Host "  [FAIL] $rel 含 $nonAscii 个非 ASCII 字节（cmd.exe 会误解析成命令）" -ForegroundColor Red
        $scriptOk = $false
    }
}

# ---------------------------------------------------------------------------
# 预检：便携版 Ollama 的完整性 / 清理逻辑（真实跑一遍，不只是看字符串）
# ---------------------------------------------------------------------------
# 两个真实踩过的坑，都必须在这里挡住：
#   1. 解压中断留下「只有 ollama.exe」的假安装：ollama serve 能启动、
#      /api/tags 也能列出模型，唯独一提问就报 llama-server binary not found。
#   2. Windows 不允许删除正在运行的程序。如果用户先启动服务再点「一键安装」，
#      Remove-Item 会静默失败，脚本随后看到 ollama.exe 还在，就当成「已装好」，
#      一路 [OK] 并以退出码 0 结束 —— 用户看到「安装成功」，坏文件一个没换。
# 所以这里真的起一个占用文件的进程来验证「停止 → 删除 → 确认」这条链路。
# ---------------------------------------------------------------------------
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

        # (d) 文件被运行中的进程占用时必须能自动停掉并删除干净
        #     （用 ping.exe 的副本冒充 ollama.exe：进程名随文件名变成 ollama，
        #       路径又落在被清理的目录内，正好复现真实场景）
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
        #     这条保证「程序关了，Ollama 还挂在后台吃显存」不会再发生。
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

    # --github-asset 的 latest 标签：必须走 /releases/latest。
    # /releases/tags/latest 会 404（并不存在名叫 latest 的标签），
    # 而这正是「默认下载源一开始就失败、被迫退回慢镜像」的原因。
    $fetchText = Get-Content (Join-Path $ProjectRoot 'scripts\fetch.mjs') -Raw -Encoding UTF8
    if ($fetchText -match 'releases/latest' -and $fetchText -match "tag === 'latest'") {
        Write-Host '  [PASS] fetch.mjs 正确处理 latest 标签' -ForegroundColor Green
    } else {
        Write-Host '  [FAIL] fetch.mjs 未特殊处理 latest（会请求 /releases/tags/latest 并 404）' -ForegroundColor Red
        $scriptOk = $false
    }

    # 进度必须是「\n 结尾的整行」：PowerShell 5.1 按行转发原生命令输出，
    # 只有 \r 的进度会被攒到进程退出才落盘 —— 1.4GB 下载在日志里全程一片空白，
    # 用户根本分不清是在下载还是卡死。
    if ($fetchText -match 'PROGRESS_INTERVAL_MS' -and $fetchText -match 'IS_TTY') {
        Write-Host '  [PASS] fetch.mjs 在非交互场景输出整行进度' -ForegroundColor Green
    } else {
        Write-Host '  [FAIL] fetch.mjs 只用 \r 刷进度（安装日志里会长时间一片空白）' -ForegroundColor Red
        $scriptOk = $false
    }
}

$script:Results += [pscustomobject]@{ Name = '预检：脚本编码与语法'; Passed = $scriptOk }

# ---------------------------------------------------------------------------
Invoke-Suite -Name '1/6 依赖导入体检' -File 'check_imports.py' | Out-Null

# ---------------------------------------------------------------------------
Invoke-Suite -Name '2/6 离线核心链路（嵌入 + FAISS + 切分）' -File 'test_offline.py' | Out-Null

# ---------------------------------------------------------------------------
Invoke-Suite -Name '3/6 RAG 端到端（假 LLM）' -File 'test_e2e.py' | Out-Null

# ---------------------------------------------------------------------------
Invoke-Suite -Name '4/6 Ollama 集成（协议兼容假服务）' -File 'test_ollama_integration.py' | Out-Null

# ---------------------------------------------------------------------------
if ($SkipHttp) {
    Write-Host ''
    Write-Host '5/6 HTTP 层验收 —— 已跳过 (-SkipHttp)' -ForegroundColor Yellow
    $script:Results += [pscustomobject]@{ Name = '5/6 HTTP 层验收'; Passed = $null }
} else {
    # 检查服务是否在跑，不在就临时拉起一个
    $serverProcess = $null
    $alreadyRunning = $false
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 | Out-Null
        $alreadyRunning = $true
    } catch { }

    if (-not $alreadyRunning) {
        Write-Host ''
        Write-Host "  后端未运行，正在临时启动（端口 $Port）..." -ForegroundColor Gray
        $serverProcess = Start-Process -FilePath $VenvPython `
            -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port", '--log-level', 'warning') `
            -WorkingDirectory $BackendDir -PassThru -WindowStyle Hidden

        Start-Sleep -Seconds 2
    }

    try {
        Invoke-Suite -Name '5/6 HTTP 层验收（真实服务）' -File 'test_http.py' `
            -Args @('--base', "http://127.0.0.1:$Port") | Out-Null
    } finally {
        if ($serverProcess -and -not $serverProcess.HasExited) {
            Write-Host "  关闭临时后端（PID $($serverProcess.Id)）" -ForegroundColor Gray
            Stop-Process -Id $serverProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------------------
# 前端测试：渲染（XSS 转义、引用角标）+ 静态一致性（DOM id / 模块导出）
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Host ''
    Write-Host '6/6 前端测试 —— 已跳过（未找到 node）' -ForegroundColor Yellow
    $script:Results += [pscustomobject]@{ Name = '6/6 前端测试'; Passed = $null }
} else {
    Write-Host ''
    Write-Host ('=' * 74) -ForegroundColor DarkGray
    Write-Host '  6/6 前端测试（渲染 + 静态一致性）' -ForegroundColor Cyan
    Write-Host ('=' * 74) -ForegroundColor DarkGray

    & node (Join-Path $PSScriptRoot 'test_frontend.mjs')
    $script:Results += [pscustomobject]@{
        Name   = '6/6 前端测试'
        Passed = ($LASTEXITCODE -eq 0)
    }
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
