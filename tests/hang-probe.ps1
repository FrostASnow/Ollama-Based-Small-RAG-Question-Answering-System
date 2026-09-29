# ============================================================================
#  hang-probe.ps1 —— 量化 GUI 窗口的「未响应」情况
#
#  原理：Windows 判定「未响应」靠的就是「窗口线程 5 秒内没取消息」。
#  这里用 SendMessageTimeout(WM_NULL) 主动探活：超时即说明 UI 线程被占住。
#  每 250ms 采一次，记录被阻塞的次数与最长阻塞时长。
#
#  用法：
#    powershell -NoProfile -ExecutionPolicy Bypass -File hang-probe.ps1 [-Seconds 30]
# ============================================================================

[CmdletBinding()]
param(
    [int]$Seconds = 30,
    [int]$SampleTimeoutMs = 300,
    [string]$Exe = ''
)

try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
$ErrorActionPreference = 'Continue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Exe) { $Exe = Join-Path $ProjectRoot 'RAG-QA.exe' }

Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class UiProbe {
  [DllImport("user32.dll", SetLastError=true)]
  static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam,
                                          uint flags, uint timeout, out IntPtr result);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  delegate bool EnumProc(IntPtr h, IntPtr p);

  const uint WM_NULL = 0x0000;
  const uint SMTO_ABORTIFHUNG = 0x0002;
  const uint SMTO_BLOCK = 0x0001;

  public static IntPtr FindWindow(uint pid) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, p) => {
      uint owner; GetWindowThreadProcessId(h, out owner);
      if (owner == pid) {
        var t = new StringBuilder(256); GetWindowText(h, t, 256);
        if (t.ToString() == "离线 RAG 文档问答") { found = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }

  // 返回 true = 有响应；false = 超过 timeout 没响应（即被阻塞）
  public static bool Ping(IntPtr hWnd, uint timeoutMs, out uint elapsedMs) {
    IntPtr result;
    var sw = System.Diagnostics.Stopwatch.StartNew();
    IntPtr ok = SendMessageTimeout(hWnd, WM_NULL, IntPtr.Zero, IntPtr.Zero,
                                   SMTO_ABORTIFHUNG | SMTO_BLOCK, timeoutMs, out result);
    sw.Stop();
    elapsedMs = (uint)sw.ElapsedMilliseconds;
    return ok != IntPtr.Zero;
  }
}
'@ -Language CSharp

Write-Host '启动 GUI...'
$gui = Start-Process -FilePath $Exe -PassThru
$hwnd = [IntPtr]::Zero
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 300
    if ($gui.HasExited) { Write-Host '[FAIL] 进程提前退出'; exit 1 }
    $hwnd = [UiProbe]::FindWindow([uint32]$gui.Id)
    if ($hwnd -ne [IntPtr]::Zero) { break }
}
if ($hwnd -eq [IntPtr]::Zero) { Write-Host '[FAIL] 找不到窗口'; Stop-Process -Id $gui.Id -Force; exit 1 }
Write-Host "窗口已就绪，开始采样 $Seconds 秒（判定阈值 ${SampleTimeoutMs}ms）..."
Write-Host ''

$samples = 0
$blocked = 0
$maxMs = 0
$worst = ''
$blocks = @()
$start = Get-Date
$sw = [System.Diagnostics.Stopwatch]::StartNew()

while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    if ($gui.HasExited) { Write-Host '[FAIL] 采样期间进程退出'; break }
    $elapsed = [uint32]0
    $ok = [UiProbe]::Ping($hwnd, $SampleTimeoutMs, [ref]$elapsed)
    $samples++
    if (-not $ok) {
        $blocked++
        $blocks += [pscustomobject]@{ At = [int]$sw.Elapsed.TotalSeconds; Ms = $SampleTimeoutMs }
        Write-Host ("  [{0,3}s] 被阻塞 ≥{1}ms" -f [int]$sw.Elapsed.TotalSeconds, $SampleTimeoutMs) -ForegroundColor Red
    }
    if ($elapsed -gt $maxMs) { $maxMs = $elapsed; $worst = 't=' + [int]$sw.Elapsed.TotalSeconds + 's' }
    Start-Sleep -Milliseconds 250
}

Stop-Process -Id $gui.Id -Force -ErrorAction SilentlyContinue

Write-Host ''
Write-Host ('=' * 60)
Write-Host ("采样 {0} 次 / 阻塞 {1} 次（{2:P1}）/ 单次最长 {3} ms（{4}）" -f `
    $samples, $blocked, $(if ($samples) { $blocked / $samples } else { 0 }), $maxMs, $worst)
if ($blocked -eq 0) {
    Write-Host 'UI 线程在整个采样期间保持响应。' -ForegroundColor Green
} else {
    Write-Host 'UI 线程存在阻塞 —— 每 5 秒级阻塞就会被 Windows 标成「未响应」。' -ForegroundColor Yellow
}
Write-Host ('=' * 60)
