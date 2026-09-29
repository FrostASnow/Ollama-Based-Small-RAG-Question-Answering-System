# ============================================================================
#  test_launcher.ps1 —— 一体化启动器 RAG-QA.exe 的验收
#
#  为什么单独测启动器：它是「用户双击的那个东西」，一旦坏了，
#  用户连界面都进不去，而主程序的测试全绿也发现不了。
#
#  覆盖：
#    * 源码编码（.cs 必须带 BOM，否则 csc 按 GBK 读，中文界面全乱码）
#    * exe 存在且比源码新（改代码忘了重新编译是最容易犯的错）
#    * --status / --status --json 的自检输出
#    * --start / --status / --stop 完整闭环（**隔离数据目录**，不碰真实知识库）
#    * 关掉 GUI 面板不会误启服务；--stop 默认不动 Ollama（本测试加 -KeepOllama）
#
#  用法：
#    powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_launcher.ps1
# ============================================================================

[CmdletBinding()]
param(
    [int]$Port = 0
)

$ErrorActionPreference = 'Continue'

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch { }

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Exe         = Join-Path $ProjectRoot 'RAG-QA.exe'
$Source      = Join-Path $ProjectRoot 'launcher\RagQaLauncher.cs'
$TmpRoot     = Join-Path $ProjectRoot '.tmp\tests'
$TestDataDir = Join-Path $TmpRoot 'launcher-data'
$OutFile     = Join-Path $TmpRoot 'launcher-out.txt'

New-Item -ItemType Directory -Force -Path $TmpRoot | Out-Null

$script:Passed = 0
$script:Failed = 0

function Check([string]$Label, [bool]$Condition, [string]$Detail = '') {
    if ($Condition) {
        $script:Passed++
        Write-Host ("  [PASS] $Label" + $(if ($Detail) { "  ($Detail)" } else { '' })) -ForegroundColor Green
    } else {
        $script:Failed++
        Write-Host ("  [FAIL] $Label" + $(if ($Detail) { "  ($Detail)" } else { '' })) -ForegroundColor Red
    }
}

function Section([string]$Title) {
    Write-Host ''
    Write-Host ('-' * 70)
    Write-Host $Title -ForegroundColor Cyan
    Write-Host ('-' * 70)
}

# 调用 exe 并取回 stdout。
# 两个坑：
#   1. PowerShell 不等待 GUI 子系统的程序：直接 & 调用会立刻返回，
#      $LASTEXITCODE 是空的、输出也拿不到 —— 必须借道 cmd（它是控制台程序）。
#   2. 不能用 Start-Process -RedirectStandardOutput：受限环境里会「拒绝访问」
#      （.NET 的进程重定向在 Windows 上走匿名管道，而沙箱禁掉了命名管道）。
#      改用 cmd 的 `> 文件`：由 cmd 直接把文件句柄交给子进程，不经过管道。
#   另外必须放到后台作业里执行：cmd 会一直等到 exe 结束，而 --start 最长等 120 秒。
function Invoke-Launcher([string[]]$Arguments, [int]$TimeoutSec = 180) {
    $stdout = Join-Path $TmpRoot 'launcher-stdout.txt'
    $stderr = Join-Path $TmpRoot 'launcher-stderr.txt'
    Remove-Item $stdout, $stderr -ErrorAction SilentlyContinue

    $quoted = $Arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    $line = "`"$Exe`" $($quoted -join ' ') > `"$stdout`" 2> `"$stderr`""

    $job = Start-Job -ScriptBlock {
        param($CommandLine)
        & cmd.exe /c $CommandLine | Out-Null
        $LASTEXITCODE
    } -ArgumentList $line

    if (-not (Wait-Job $job -Timeout $TimeoutSec)) {
        Stop-Job $job -ErrorAction SilentlyContinue
        Remove-Job $job -Force -ErrorAction SilentlyContinue
        return [pscustomobject]@{ ExitCode = -1; StdOut = ''; StdErr = 'timeout' }
    }
    $code = @(Receive-Job $job)[0]
    Remove-Job $job -Force -ErrorAction SilentlyContinue

    $text = ''
    if (Test-Path $stdout) { $text = [System.IO.File]::ReadAllText($stdout, [System.Text.UTF8Encoding]::new($false)) }
    [pscustomobject]@{
        ExitCode = $code
        StdOut   = $text
        StdErr   = $(if (Test-Path $stderr) { [System.IO.File]::ReadAllText($stderr, [System.Text.UTF8Encoding]::new($false)) } else { '' })
    }
}

function Test-PortOpen([int]$Target) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $ar = $client.BeginConnect('127.0.0.1', $Target, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne(500)) { $client.Close(); return $false }
        $client.EndConnect($ar)
        $client.Close()
        return $true
    } catch { return $false }
}

function Find-FreePort([int]$Start) {
    for ($candidate = $Start; $candidate -lt ($Start + 40); $candidate++) {
        if (-not (Test-PortOpen $candidate)) { return $candidate }
    }
    return 0
}

Write-Host ('=' * 70)
Write-Host '启动器验收（RAG-QA.exe）'
Write-Host ('=' * 70)

# ---------------------------------------------------------------------------
Section '1. 构建产物'
if (-not (Test-Path $Exe)) {
    Check 'RAG-QA.exe 存在' $false '先运行 scripts\build-launcher.cmd'
    Write-Host ''
    Write-Host ("结果：通过 $($script:Passed) 项，失败 $($script:Failed) 项") -ForegroundColor Red
    exit 1
}
Check 'RAG-QA.exe 存在' $true ("{0:N0} 字节" -f (Get-Item $Exe).Length)

$bytes = [System.IO.File]::ReadAllBytes($Source)
$hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)
Check '启动器源码带 UTF-8 BOM（csc 靠它识别中文）' $hasBom $Source

$fresh = (Get-Item $Exe).LastWriteTime -ge (Get-Item $Source).LastWriteTime
Check 'exe 不比源码旧（改完记得重新编译）' $fresh (
    'exe {0:HH:mm:ss} / src {1:HH:mm:ss}' -f (Get-Item $Exe).LastWriteTime, (Get-Item $Source).LastWriteTime)

# 静态检查：启动器必须「编排」而不是「自己重写一遍」
$src = [System.IO.File]::ReadAllText($Source, [System.Text.UTF8Encoding]::new($true))
Check '停止逻辑委托给 stop.ps1' ($src -match '"stop\.ps1"')
Check '启动逻辑委托给 start.ps1' ($src -match '"start\.ps1"')
Check '--stop 支持 -KeepOllama（不强制关掉用户的 Ollama）' ($src -match '\-KeepOllama')
Check 'HTTP 探测显式禁用代理（否则 127.0.0.1 也会走系统代理拿到 502）' ($src -match 'req\.Proxy = null')
Check '不用 *> 重定向脚本输出（PS 5.1 会把原生 stderr 当成终止错误）' ($src -notmatch '"\s\*>\s"')

# ---------------------------------------------------------------------------
Section '2. --status 自检'
$status = Invoke-Launcher @('--status', '--json')
Check '--status --json 退出码为 0' ($status.ExitCode -eq 0) ("exit=$($status.ExitCode)")

$json = $null
try { $json = $status.StdOut.Trim() | ConvertFrom-Json } catch { }
Check 'JSON 可解析' ($null -ne $json) $status.StdOut.Trim()
if ($json) {
    Check 'root 指向项目根目录' ((Resolve-Path $json.root).Path.TrimEnd('\') -eq $ProjectRoot.TrimEnd('\')) $json.root
    Check '报告环境就绪' ($json.ready -eq $true)
    Check '识别出启动/停止脚本与虚拟环境' (
        ($json.start_script -eq 'ok') -and ($json.stop_script -eq 'ok') -and ($json.python -eq 'ok'))
    Check '带 running 字段' ($null -ne $json.running) ("running=$($json.running)")
}

$statusText = Invoke-Launcher @('--status')
Check '--status 文本模式输出中文正常' ($statusText.StdOut -match '项目根目录') $statusText.StdOut.Split("`n")[0].Trim()

# ---------------------------------------------------------------------------
Section '3. GUI 面板冒烟（只开窗口，不自动启服务）'

# 为什么不用 Process.MainWindowTitle：它只报告**可见**的主窗口。
# 在无人交互的会话（CI、远程、沙箱）里窗口对象能建出来但不会真正显示，
# MainWindowTitle 永远是空 —— 那会把「功能正常」误判成失败。
# 这里直接枚举该进程的顶层窗口并检查标题文本，这才是界面真的建对了的证据。
$probeReady = $false
try {
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class LauncherWindowProbe {
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  delegate bool EnumProc(IntPtr h, IntPtr p);
  public static List<string> Titles(uint target, out int visibleCount) {
    var list = new List<string>();
    int visible = 0;
    EnumWindows((h, p) => {
      uint pid; GetWindowThreadProcessId(h, out pid);
      if (pid == target) {
        var t = new StringBuilder(256); GetWindowText(h, t, 256);
        if (t.Length > 0) list.Add(t.ToString());
        if (IsWindowVisible(h)) visible++;
      }
      return true;
    }, IntPtr.Zero);
    visibleCount = visible;
    return list;
  }
}
'@ -Language CSharp
    $probeReady = $true
} catch {
    Write-Host "  [INFO] Add-Type 不可用，退回 MainWindowTitle 判断：$($_.Exception.Message)" -ForegroundColor Gray
}

$gui = Start-Process -FilePath $Exe -PassThru
$guiTitle = ''
$visibleCount = 0
for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Milliseconds 500
    $gui.Refresh()
    if ($gui.HasExited) { break }
    if ($probeReady) {
        $titles = [LauncherWindowProbe]::Titles([uint32]$gui.Id, [ref]$visibleCount)
        $match = $titles | Where-Object { $_ -eq '离线 RAG 文档问答' } | Select-Object -First 1
        if ($match) { $guiTitle = $match; break }
    } elseif ($gui.MainWindowTitle) {
        $guiTitle = $gui.MainWindowTitle
        break
    }
}
Check '面板进程存活（没有构造异常直接崩）' (-not $gui.HasExited) ("pid=$($gui.Id)")
Check '面板窗口已创建且标题正确' ($guiTitle -eq '离线 RAG 文档问答') "title='$guiTitle'"
if ($probeReady -and $guiTitle -and $visibleCount -eq 0) {
    Write-Host '  [INFO] 当前会话没有可见桌面：窗口对象已建立但不会显示（真实桌面下会正常弹出）' -ForegroundColor Gray
}
Stop-Process -Id $gui.Id -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
Check '关掉面板不会顺手启动后端（不该有监听）' (-not (Test-PortOpen 8000)) '8000 未监听'

# ---------------------------------------------------------------------------
Section '4. --start / --status / --stop 闭环（数据目录隔离）'
Remove-Item -Recurse -Force $TestDataDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $TestDataDir | Out-Null
$env:RAG_DATA_DIR = $TestDataDir

$targetPort = if ($Port -gt 0) { $Port } else { Find-FreePort 8093 }
Check '找到空闲端口' ($targetPort -gt 0) "port=$targetPort"

$ollamaBefore = Test-PortOpen 11434

$start = Invoke-Launcher @('--start', '--port', "$targetPort", '--no-browser', '--keep-ollama')
Check '--start 退出码为 0' ($start.ExitCode -eq 0) ("exit=$($start.ExitCode)  $($start.StdOut.Trim())")
Check '服务已监听目标端口' (Test-PortOpen $targetPort) "port=$targetPort"

$running = Invoke-Launcher @('--status', '--json')
$runningJson = $null
try { $runningJson = ($running.StdOut.Trim() | ConvertFrom-Json) } catch { }
Check '--status 报告运行中' ($runningJson -and $runningJson.running -eq $true) $running.StdOut.Trim()
Check '报告的端口与启动端口一致' ($runningJson -and $runningJson.port -eq $targetPort) ("$($runningJson.port) vs $targetPort")
Check '报告模型与知识库规模' ($runningJson -and $runningJson.model -and ($null -ne $runningJson.documents)) (
    "model=$($runningJson.model) docs=$($runningJson.documents) chunks=$($runningJson.chunks)")

$stop = Invoke-Launcher @('--stop', '--port', "$targetPort", '--keep-ollama')
Check '--stop 退出码为 0' ($stop.ExitCode -eq 0) ("exit=$($stop.ExitCode)")
Start-Sleep -Milliseconds 800
Check '停止后端口已释放' (-not (Test-PortOpen $targetPort)) "port=$targetPort"

$afterStop = Invoke-Launcher @('--status', '--json')
$afterJson = $null
try { $afterJson = ($afterStop.StdOut.Trim() | ConvertFrom-Json) } catch { }
Check '--status 报告已停止' ($afterJson -and $afterJson.running -eq $false) $afterStop.StdOut.Trim()

if ($ollamaBefore) {
    Check '-KeepOllama 保住了 Ollama（没有误杀）' (Test-PortOpen 11434) '11434 仍在监听'
} else {
    Write-Host '  [INFO] 测试前 Ollama 未运行，跳过「保留 Ollama」检查' -ForegroundColor Gray
}

# 状态文件应随停止被清掉，避免下次误判端口
$stateFile = Join-Path $TestDataDir 'logs\launcher-state.json'
Check '停止后清理 launcher-state.json' (-not (Test-Path $stateFile))

Remove-Item Env:\RAG_DATA_DIR -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
Write-Host ''
Write-Host ('=' * 70)
Write-Host ("结果：通过 $($script:Passed) 项，失败 $($script:Failed) 项") -ForegroundColor $(if ($script:Failed) { 'Red' } else { 'Green' })
Write-Host ('=' * 70)
exit $(if ($script:Failed -eq 0) { 0 } else { 1 })
