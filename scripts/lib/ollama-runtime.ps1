# ============================================================================
#  ollama-runtime.ps1 —— 便携版 Ollama 的「安装完整性」工具箱
#
#  被 start.ps1 / prepare.ps1 点源（dot-source）使用，也被 tests\run_all.ps1
#  的预检直接调用做真实回归测试，所以这里只放**纯函数**，不做任何输出与退出。
#
#  为什么需要它
#  -----------
#  便携版 zip 里除了 ollama.exe（约 25MB），还有 lib\ollama\ 下一整套推理运行时
#  （llama-server.exe、ggml*.dll 等，合计约 1.3GB）。解压中断、磁盘空间不足、
#  杀毒软件拦截 dll 写入，都会留下「只有 ollama.exe」的半成品，而它的表现
#  极具欺骗性：
#
#      ollama serve 能启动 ✅   /api/tags 能列出模型 ✅   真正提问 ❌
#      error starting llama-server: llama-server binary not found
#
#  也就是说，所有「进程在不在、模型列不列得出来」这类常规检查都会说正常。
#
#  另一个真实的坑：Windows 不允许删除正在运行的程序。如果用户是先启动服务
#  再点「一键安装」，Remove-Item 会**静默失败**（配合 -ErrorAction
#  SilentlyContinue），于是脚本以为「目录已清空」，接着又看到 ollama.exe 还在，
#  就当成「已经装好了」，一路报 [OK] 并以退出码 0 结束 —— 用户看到「安装成功」，
#  但坏文件一个都没换掉。所以删除必须：先停掉占用该目录的进程 → 删 → 再确认。
# ============================================================================

#: 推理运行时所在的子目录（相对 ollama.exe 所在目录）
$script:OllamaRuntimeSubdir = 'lib\ollama'
#: 推理引擎可能的文件名
$script:OllamaRunnerNames = @('llama-server.exe', 'ollama-llama-server.exe')

function Test-OllamaPayload {
    <#
    .SYNOPSIS
        判断某个 Ollama 目录里是否包含完整的推理运行时。
    .DESCRIPTION
        优先认推理引擎本体；万一日后改名，只要 lib\ollama 下确实有一批文件
        （而不是空目录）就不误报。
    #>
    param([Parameter(Mandatory = $true)][string]$Dir)

    $runtime = Join-Path $Dir $script:OllamaRuntimeSubdir
    if (-not (Test-Path $runtime)) { return $false }

    $files = @(Get-ChildItem -Path $runtime -File -ErrorAction SilentlyContinue)
    if ($files.Count -eq 0) { return $false }

    $runners = @($files | Where-Object { $_.Name -in $script:OllamaRunnerNames })
    return ($runners.Count -gt 0 -or $files.Count -ge 3)
}

function Test-ZipReadable {
    <#
    .SYNOPSIS
        检查 zip 是否是**完整可读**的压缩包。
    .DESCRIPTION
        下载被中断的 zip 打不开中央目录，会抛异常 —— 这正是「解压到一半留下
        半成品」的源头。必须在解压之前先验一次。
    #>
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path $Path)) { return $false }
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
        $zip = [System.IO.Compression.ZipFile]::OpenRead($Path)
        try {
            return ($zip.Entries.Count -gt 0)
        } finally {
            $zip.Dispose()
        }
    } catch {
        return $false
    }
}

function Stop-PortableOllama {
    <#
    .SYNOPSIS
        停止**路径位于指定目录内**的 Ollama 进程（只动我们自己的，不碰系统安装）。
    .OUTPUTS
        被停止的进程数量。
    #>
    param([Parameter(Mandatory = $true)][string]$Dir)

    $stopped = 0
    $targets = @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like 'ollama*' })

    foreach ($proc in $targets) {
        $path = $null
        try { $path = $proc.Path } catch { }
        if (-not $path) { continue }   # 读不到路径（权限不足）时不动它
        if (-not $path.StartsWith($Dir, [System.StringComparison]::OrdinalIgnoreCase)) { continue }

        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
            $stopped++
        } catch { }
    }

    if ($stopped -gt 0) { Start-Sleep -Milliseconds 900 }  # 等文件句柄释放
    return $stopped
}

function Remove-OllamaDir {
    <#
    .SYNOPSIS
        清空 Ollama 目录，并**确认真的删掉了**。
    .DESCRIPTION
        Windows 不允许删除正在运行的程序，此时 Remove-Item 会失败。这里先停掉
        占用该目录的 Ollama 进程，删除后重试一次（进程退出到句柄释放有延迟），
        最后如实返回结果 —— 调用方必须检查返回值，不能当作已清空。
    .OUTPUTS
        $true 表示目录已不存在（或本来就不存在）；$false 表示仍被占用。
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Dir,
        [string]$Exe
    )

    if (-not $Exe) { $Exe = Join-Path $Dir 'ollama.exe' }
    if (-not (Test-Path $Dir)) { return $true }

    Stop-PortableOllama -Dir $Dir | Out-Null
    Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue

    if (-not (Test-Path $Exe)) { return $true }

    Start-Sleep -Milliseconds 1200
    Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
    return (-not (Test-Path $Exe))
}

# ---------------------------------------------------------------------------
# 退出时收掉 Ollama
# ---------------------------------------------------------------------------
# 用户明确要求：程序退出时，把它用到的 Ollama 一起关掉 ——
# 否则任务管理器里会长期挂着一个几百 MB 到 1GB 显存占用的进程，
# 而用户以为「程序已经关了」。
#
# 要收的可能是三种情况，任何一种都不能漏：
#   1. 本次启动脚本自己拉起来的（知道 PID，按进程树杀）
#   2. 项目内置的那份（路径在 tools\ollama 下）
#   3. **之前就在运行的**（例如上次没退干净、或用一键安装拉起来的）
#      —— 这时只能靠「谁在监听 11434 端口」来定位
# 前两种是「我们的」，第三种严格说可能是用户的；调用方用 -KeepOllama 可以全部保留。
# ---------------------------------------------------------------------------
function Stop-ProcessTree {
    <#
    .SYNOPSIS
        结束一个进程及其子进程（Ollama 会派生 runner 子进程，只杀父进程不够）。
    .DESCRIPTION
        刻意临时放宽 $ErrorActionPreference：PowerShell 5.1 在 'Stop' 下
        只要对原生命令的 stderr 做了重定向就会抛 NativeCommandError，
        而 taskkill 在「进程已经退出」时恰好会往 stderr 写东西。
    #>
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & taskkill.exe /F /T /PID $ProcessId *> $null
        if ($LASTEXITCODE -ne 0) {
            Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Get-OllamaProcesses {
    <#
    .SYNOPSIS
        所有名字以 ollama 开头的进程（含官方的 "ollama app" 托盘程序）。
    #>
    return @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like 'ollama*' })
}

function Get-PortListenerProcessId {
    <#
    .SYNOPSIS
        谁在监听指定 TCP 端口；没有则返回 0。
    .DESCRIPTION
        刻意用 **netstat** 而不是 Get-NetTCPConnection：后者在受限账户/受限环境里
        会直接抛「拒绝访问」（实测），一旦如此，「按端口兜底找残留 Ollama」
        就成了摆设 —— 而退出清理恰恰最需要它。
        netstat 不需要额外权限，状态列（LISTENING）也不随系统语言变化。
    #>
    param([int]$Port = 11434)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        foreach ($line in (& netstat.exe -ano -p TCP 2>$null)) {
            $parts = @(($line -split '\s+') | Where-Object { $_ })
            if ($parts.Count -lt 5) { continue }
            if ($parts[1] -notmatch ":$Port`$") { continue }
            if ($parts[3] -ne 'LISTENING') { continue }
            $ownerId = 0
            if ([int]::TryParse($parts[4], [ref]$ownerId)) { return $ownerId }
        }
    } catch {
    } finally {
        $ErrorActionPreference = $previous
    }
    return 0
}

function Get-OllamaPortOwner {
    <#
    .SYNOPSIS
        谁在监听 Ollama 端口（默认 11434）；不是 ollama 进程则返回 $null。
    #>
    param([int]$Port = 11434)

    $ownerId = Get-PortListenerProcessId -Port $Port
    if ($ownerId -le 0) { return $null }

    $proc = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like 'ollama*') { return $proc }
    return $null
}

function Stop-PortableOllamaForProject {
    <#
    .SYNOPSIS
        退出清理：把本项目用到的 Ollama 全部停掉，并确认端口已释放。
    .PARAMETER Dir
        项目内置的 Ollama 目录（tools\ollama）。
    .PARAMETER ProcessId
        本次启动脚本拉起的 Ollama PID（没有就传 0）。
    .PARAMETER Port
        Ollama 监听端口，用于兜底定位「不是我拉起来的那个」。
    .OUTPUTS
        被停掉的 PID 数组。
    #>
    param(
        [string]$Dir,
        [int]$ProcessId = 0,
        [int]$Port = 11434
    )

    $stopped = New-Object System.Collections.Generic.List[int]

    # 1) 本次启动脚本拉起的：按进程树杀，连带它的 runner 子进程
    if ($ProcessId -gt 0 -and (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        Stop-ProcessTree -ProcessId $ProcessId
        $stopped.Add($ProcessId)
    }

    # 2) 项目内置的那份（可能被别的脚本拉起，PID 对不上）
    if ($Dir) {
        foreach ($proc in Get-OllamaProcesses) {
            $path = $null
            try { $path = $proc.Path } catch { }
            if (-not $path) { continue }
            if (-not $path.StartsWith($Dir, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
            if ($stopped.Contains($proc.Id)) { continue }

            Stop-ProcessTree -ProcessId $proc.Id
            $stopped.Add($proc.Id)
        }
    }

    # 3) 兜底：端口上还在监听就说明还有残留，把监听者收掉。
    #    循环几次是为了处理「旧进程刚退出、新进程还没起来」的时序。
    for ($i = 0; $i -lt 10; $i++) {
        $owner = Get-OllamaPortOwner -Port $Port
        if (-not $owner) { break }
        if (-not $stopped.Contains($owner.Id)) {
            Stop-ProcessTree -ProcessId $owner.Id
            $stopped.Add($owner.Id)
        }
        Start-Sleep -Milliseconds 300
    }

    if ($stopped.Count -gt 0) { Start-Sleep -Milliseconds 600 }  # 等显存/句柄释放
    return $stopped.ToArray()
}

function Test-OllamaPortListening {
    param([int]$Port = 11434)

    return ((Get-PortListenerProcessId -Port $Port) -gt 0)
}
