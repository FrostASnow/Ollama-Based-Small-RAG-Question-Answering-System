# ============================================================================
#  ollama-runtime.ps1 —— 便携版 Ollama 的「安装完整性」工具箱
#  点源方：start.ps1 / prepare.ps1；tests\run_all.ps1 预检也直接调用 → 只放纯函数，无输出与退出。
#  半成品（解压中断，只剩 ollama.exe）能 serve、能列模型，唯独提问报 "llama-server binary
#  not found"；且 Windows 不允许删除运行中的程序，删除必须先停进程、删完再确认。
# ============================================================================

#: 推理运行时所在的子目录（相对 ollama.exe 所在目录）
$script:OllamaRuntimeSubdir = 'lib\ollama'
#: 推理引擎可能的文件名
$script:OllamaRunnerNames = @('llama-server.exe', 'ollama-llama-server.exe')

function Test-OllamaPayload {
    # 判断目录里是否有完整推理运行时：优先认推理引擎本体，日后改名时只要
    # lib\ollama 下确实有一批文件（而不是空目录）就不误报。
    param([Parameter(Mandatory = $true)][string]$Dir)

    $runtime = Join-Path $Dir $script:OllamaRuntimeSubdir
    if (-not (Test-Path $runtime)) { return $false }

    $files = @(Get-ChildItem -Path $runtime -File -ErrorAction SilentlyContinue)
    if ($files.Count -eq 0) { return $false }

    $runners = @($files | Where-Object { $_.Name -in $script:OllamaRunnerNames })
    return ($runners.Count -gt 0 -or $files.Count -ge 3)
}

function Test-ZipReadable {
    # 下载被中断的 zip 打不开中央目录会抛异常，这正是「解压到一半留下半成品」
    # 的源头，必须在解压前先验一次。
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
    # 停止路径位于指定目录内的 Ollama（只动我们自己的，不碰系统安装）。
    # 输出：被停止的进程数量。
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
    # 清空 Ollama 目录并确认真的删掉了：Windows 不允许删除运行中的程序，Remove-Item 会失败，
    # 这里先停掉占用该目录的进程、删除后重试一次（句柄释放有延迟），最后如实返回结果 ——
    # 调用方必须检查返回值（$true = 目录已不存在，$false = 仍被占用），不能当作已清空。
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

# 退出时收掉 Ollama，否则会长期挂着占显存的进程。三种情况都不能漏：
#   1. 本次启动脚本拉起的（知道 PID，按进程树杀）  2. 项目内置的那份（tools\ollama 下）
#   3. 之前就在运行的（只能靠「谁在监听 11434」定位）；调用方用 -KeepOllama 可全部保留。
function Stop-ProcessTree {
    # 结束一个进程及其子进程（Ollama 会派生 runner 子进程）。刻意临时放宽
    # $ErrorActionPreference：PS 5.1 在 'Stop' 下重定向原生命令 stderr 会抛 NativeCommandError。
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
    # 所有名字以 ollama 开头的进程（含官方的 "ollama app" 托盘程序）。
    return @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like 'ollama*' })
}

function Get-PortListenerProcessId {
    # 谁在监听指定 TCP 端口；没有则返回 0。刻意用 netstat 而不是 Get-NetTCPConnection：
    # 后者在受限账户下直接抛「拒绝访问」，而退出清理恰恰最需要这条兜底路径。
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
    # 谁在监听 Ollama 端口（默认 11434）；不是 ollama 进程则返回 $null。
    param([int]$Port = 11434)

    $ownerId = Get-PortListenerProcessId -Port $Port
    if ($ownerId -le 0) { return $null }

    $proc = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
    if ($proc -and $proc.ProcessName -like 'ollama*') { return $proc }
    return $null
}

function Stop-PortableOllamaForProject {
    # 退出清理：停掉本项目用到的 Ollama 并确认端口已释放；返回被停掉的 PID 数组。
    # -ProcessId 是本次拉起的 PID（没有传 0），-Port 用于兜底定位不是我拉起的那个。
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

    # 3) 兜底：端口上还在监听就还有残留；循环几次以处理进程换代的时序。
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
