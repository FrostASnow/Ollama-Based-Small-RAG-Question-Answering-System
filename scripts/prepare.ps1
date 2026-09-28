# ============================================================================
#  prepare.ps1 —— 一次性联网准备（之后即可完全离线运行）
#
#  做四件事：
#    1. 准备 Python 3.12 运行时（优先用 uv，其次用系统已装的 Python）
#    2. 创建 .venv 并安装全部 Python 依赖
#    3. 下载 all-MiniLM-L6-v2 嵌入模型到 models/
#    4. 下载便携版 Ollama 到 tools/ollama 并拉取 LLM 模型
#
#  推荐用法：直接双击 scripts\prepare.cmd
#
#  也可以命令行运行：
#    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare.ps1
#    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare.ps1 -Mirror
#    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare.ps1 -SkipOllama
#    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\prepare.ps1 -LlmModel llama3.2
#
#  说明：脚本默认从项目根目录定位路径，无论从哪个目录执行都可以。
# ============================================================================

[CmdletBinding()]
param(
    [string]$LlmModel = 'deepseek-r1:1.5b',
    [string]$EmbeddingModel = 'sentence-transformers/all-MiniLM-L6-v2',
    [switch]$Mirror,
    [switch]$SkipOllama,
    [switch]$SkipPython
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# 控制台编码：必须最先执行，否则下面的中文提示全是乱码
# （原理见 start.ps1 里的同名代码块）
# ---------------------------------------------------------------------------
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch { }
$env:PYTHONIOENCODING = 'utf-8'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvDir     = Join-Path $ProjectRoot '.venv'
$VenvPython  = Join-Path $VenvDir 'Scripts\python.exe'
$OllamaDir   = Join-Path $ProjectRoot 'tools\ollama'
$OllamaExe   = Join-Path $OllamaDir 'ollama.exe'
$ModelsDir   = Join-Path $ProjectRoot 'models\ollama'
$ReqFile     = Join-Path $ProjectRoot 'backend\requirements.txt'

# 便携版 Ollama 的完整性工具（Test-OllamaPayload / Test-ZipReadable /
# Stop-PortableOllama / Remove-OllamaDir）。同一份也被 start.ps1 点源，
# 并被 tests\run_all.ps1 的预检直接调用做真实回归测试。
. (Join-Path $PSScriptRoot 'lib\ollama-runtime.ps1')

function Write-Step($text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "[OK]   $text" -ForegroundColor Green }
function Write-Warn2($text){ Write-Host "[WARN] $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "[FAIL] $text" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 通用下载
# ---------------------------------------------------------------------------
# 顺序：node + fetch.mjs  →  curl  →  Invoke-WebRequest
#
# 为什么把 node 放第一位：
#   * fetch.mjs 的进度是干净的单行（用 \r 原地刷新），网页日志里能正常显示
#   * 它支持断点续传 —— 1.4GB 的文件中断一次不用从头再来
# curl 放到其后，并且必须加 -sS：
#   默认的进度表用 \r 高频重绘，落到日志文件里就是成百上千行 "0 0 0 0 0"，
#   既是噪音又拖慢前端渲染。-s 关掉进度，-S 保留错误信息。
#
# Validator：给一个「文件是否已经可用」的判定脚本块。
#   存在且通过校验 → 跳过；存在但没通过 → **保留文件继续下载**（fetch.mjs 会从
#   .part 断点续传），而不是删掉重来。1.4GB 的下载重来一次代价太大。
#
# GithubAsset*：走 fetch.mjs 的 --github-asset 模式（用 api.github.com 解析出
#   带签名的真实地址）。**这条路径很重要**：有些网络里 github.com 本身被屏蔽，
#   但 api.github.com 与 release-assets.githubusercontent.com 可用。
# ---------------------------------------------------------------------------
function Get-RemoteFile {
    param(
        [string]$Url,
        [string]$OutFile,
        [string]$Label = $Url,
        [scriptblock]$Validator,
        [string]$GithubAssetRepo,
        [string]$GithubAssetTag,
        [string]$GithubAssetName
    )

    $dir = Split-Path -Parent $OutFile
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

    if (Test-Path $OutFile) {
        if (-not $Validator -or (& $Validator $OutFile)) {
            Write-Host "  已存在，跳过：$OutFile"
            return $true
        }
        Write-Warn2 ' 已存在的文件校验未通过（可能上次没下完），尝试断点续传 ...'
    }

    Write-Host "  下载 $Label"
    Write-Host '  （大文件可能耗时较久，中断后重跑可续传）' -ForegroundColor Gray

    $node = Get-Command node -ErrorAction SilentlyContinue
    if ($node) {
        if ($GithubAssetRepo) {
            & node (Join-Path $PSScriptRoot 'fetch.mjs') --github-asset `
                $GithubAssetRepo $GithubAssetTag $GithubAssetName $OutFile
        } else {
            & node (Join-Path $PSScriptRoot 'fetch.mjs') $Url $OutFile
        }
        if ($LASTEXITCODE -eq 0 -and (Test-Path $OutFile)) { return $true }
        Write-Warn2 'node 下载失败，改试 curl'
    }

    if (-not $Url) { return $false }

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & curl.exe -sS -L --fail --retry 3 --retry-delay 2 -o $OutFile $Url
        if ($LASTEXITCODE -eq 0 -and (Test-Path $OutFile)) { return $true }
        Write-Warn2 'curl 下载失败，改试 Invoke-WebRequest'
    }

    try {
        Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
        if (Test-Path $OutFile) { return $true }
    } catch {
        Write-Warn2 "Invoke-WebRequest 失败：$($_.Exception.Message)"
    }

    # 清理可能的半成品，避免下次误判为「已存在」
    if (Test-Path $OutFile) {
        try {
            $size = (Get-Item $OutFile).Length
            if ($size -eq 0) { Remove-Item $OutFile -Force -ErrorAction SilentlyContinue }
        } catch { }
    }

    Write-Err "无法下载 $Label"
    return $false
}

# ---------------------------------------------------------------------------
# 定位 uv
# ---------------------------------------------------------------------------
# 注意：Get-Command 返回的是 ApplicationInfo，路径在 .Source 上，
# **没有 .FullName 属性** —— 误用会静默拿到空字符串，导致后面整段逻辑
# 悄悄走到「没有 uv」的分支（曾因此回退到 python -m pip，
# 而 uv 创建的 venv 默认不含 pip，直接报 "No module named pip"）。
# 这里统一解析成字符串路径，并额外覆盖几个常见安装位置。
# ---------------------------------------------------------------------------
function Resolve-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) {
        foreach ($prop in @('Source', 'Path', 'Definition')) {
            $value = $cmd.$prop
            if ($value -and (Test-Path $value)) { return $value }
        }
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    return $null
}

# ---------------------------------------------------------------------------
# 安全地运行「预期可能失败」的原生命令
# ---------------------------------------------------------------------------
# PowerShell 5.1 的坑：当 $ErrorActionPreference = 'Stop' 时，
# 只要把原生命令的 stderr 做了重定向（`*> $null` 或 `2>&1`），
# PowerShell 就会抛出 NativeCommandError 并把整个脚本打断。
# 而「探测 pip 在不在」这类操作恰恰既需要重定向、又预期会失败。
# 这个函数临时放宽偏好设置，只把退出码告诉我们。
# ---------------------------------------------------------------------------
function Test-NativeSuccess {
    param([string]$Exe, [string[]]$Arguments)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previous
    }
}

# ---------------------------------------------------------------------------
# Ollama 安装完整性：判定函数在 scripts\lib\ollama-runtime.ps1（已点源）
# ---------------------------------------------------------------------------
# 便携版 zip 里除了 ollama.exe，还有 lib\ollama\ 下一整套推理运行时。
# 解压中断时可能**只留下一个 ollama.exe**，而它的故障表现极具欺骗性：
#     ollama serve 正常启动、/api/tags 也能列出模型 ——
#     唯独真正提问时报 "error starting llama-server: llama-server binary not found"。
# 所以这里显式校验，发现不完整就清掉重来，而不是看到 ollama.exe 就说「已就绪」。
# ---------------------------------------------------------------------------

Write-Host @"
============================================================================
 离线 RAG 文档问答 —— 环境准备
============================================================================
 项目目录 : $ProjectRoot
 LLM      : $LlmModel
 嵌入模型 : $EmbeddingModel
============================================================================
"@ -ForegroundColor White

# ---------------------------------------------------------------------------
# 1. Python 运行时
# ---------------------------------------------------------------------------
if (-not $SkipPython) {
    Write-Step '1/4 准备 Python 运行时'

    $uvPath = Resolve-Uv

    if (-not $uvPath) {
        Write-Warn2 ' 未找到 uv'
        Write-Host '   uv 可以免管理员、免安装地准备 Python，强烈建议先装：'
        Write-Host '       powershell -c "irm https://astral.sh/uv/install.ps1 | iex"' -ForegroundColor Gray
        Write-Host '   或直接使用系统 Python 3.10+ 继续（若已安装）。'
    } else {
        Write-Ok "找到 uv：$uvPath"

        # uv 默认把缓存放在 %LOCALAPPDATA%\uv\cache。
        # 在部分受限机器上该目录不可写，uv 会直接以
        # "Failed to initialize cache ... 拒绝访问" 失败，而后续的 pip 回退
        # 又因为 venv 里没有 pip 而一起失败 —— 用户看到的是一串莫名其妙的报错。
        # 这里先探测一次，不可写就改用项目内的目录。
        if (-not $env:UV_CACHE_DIR) {
            $defaultCache = Join-Path $env:LOCALAPPDATA 'uv\cache'
            $cacheUsable = $true
            try {
                New-Item -ItemType Directory -Force -Path $defaultCache -ErrorAction Stop | Out-Null
                $probeFile = Join-Path $defaultCache '.rag-qa-write-test'
                Set-Content -Path $probeFile -Value 'ok' -ErrorAction Stop
                Remove-Item $probeFile -Force -ErrorAction SilentlyContinue
            } catch {
                $cacheUsable = $false
            }

            if (-not $cacheUsable) {
                $env:UV_CACHE_DIR = Join-Path $ProjectRoot '.uv-cache'
                New-Item -ItemType Directory -Force -Path $env:UV_CACHE_DIR | Out-Null
                Write-Warn2 " uv 默认缓存目录不可写，已改用：$($env:UV_CACHE_DIR)"
            }
        }
    }

    if (-not (Test-Path $VenvPython)) {
        if ($uvPath) {
            Write-Host '  使用 uv 安装 Python 3.12 ...'
            & $uvPath python install 3.12
            Write-Host '  创建虚拟环境 .venv ...'
            # --seed 让 venv 自带 pip/setuptools：
            # 没有 pip 的 venv 会让「回退到 python -m pip」彻底走不通
            & $uvPath venv --seed --python 3.12 $VenvDir
        } else {
            $sysPy = $null
            foreach ($name in @('py', 'python', 'python3')) {
                $cmd = Get-Command $name -ErrorAction SilentlyContinue
                if ($cmd) { $sysPy = $cmd; break }
            }
            if (-not $sysPy) {
                Write-Err ' 找不到 Python。请先安装 Python 3.10+ 或安装 uv 后重试。'
                exit 1
            }
            Write-Host "  使用系统 Python 创建虚拟环境：$($sysPy.Source)"
            & $sysPy.Source -m venv $VenvDir
        }
    } else {
        Write-Ok '  .venv 已存在'
    }

    if (-not (Test-Path $VenvPython)) {
        Write-Err " 虚拟环境创建失败，未找到 $VenvPython"
        exit 1
    }
    Write-Ok "Python：$(& $VenvPython --version 2>&1)"
} else {
    Write-Step '1/4 跳过 Python 准备'
}

# ---------------------------------------------------------------------------
# 2. 安装依赖
# ---------------------------------------------------------------------------
Write-Step '2/4 安装 Python 依赖'

$uvCmd = Resolve-Uv
$depsOk = $false

if ($uvCmd) {
    Write-Host "  使用 uv 安装（$uvCmd）" -ForegroundColor Gray
    & $uvCmd pip install --python $VenvPython -r $ReqFile
    $depsOk = ($LASTEXITCODE -eq 0)
    if (-not $depsOk) {
        Write-Warn2 ' uv 安装失败，回退到 pip 再试一次'
    }
}

if (-not $depsOk) {
    # 关键兜底：uv 创建的虚拟环境默认不带 pip。
    # 如果这时再直接 `python -m pip`，只会得到 "No module named pip" 这种
    # 让人摸不着头脑的报错，所以先用 ensurepip 把 pip 引导出来。
    if (-not (Test-NativeSuccess $VenvPython @('-m', 'pip', '--version'))) {
        Write-Host '  虚拟环境中没有 pip，正在引导 ...' -ForegroundColor Gray
        & $VenvPython -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) {
            Write-Err ' 无法引导 pip。'
            Write-Host '  请安装 uv 后重试（推荐）：' -ForegroundColor Yellow
            Write-Host '      powershell -c "irm https://astral.sh/uv/install.ps1 | iex"' -ForegroundColor Yellow
            exit 1
        }
    }

    Write-Host '  使用 pip 安装' -ForegroundColor Gray
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r $ReqFile
    $depsOk = ($LASTEXITCODE -eq 0)
}

if (-not $depsOk) {
    Write-Err '依赖安装失败'
    Write-Host "  可手动重试： & `"$VenvPython`" -m pip install -r `"$ReqFile`"" -ForegroundColor Yellow
    exit 1
}
Write-Ok '依赖安装完成'

# ---------------------------------------------------------------------------
# 3. 嵌入模型
# ---------------------------------------------------------------------------
Write-Step '3/4 下载嵌入模型'

$dlArgs = @((Join-Path $PSScriptRoot 'download_models.py'), '--model', $EmbeddingModel)
if ($Mirror) { $dlArgs += '--mirror' }
& $VenvPython @dlArgs
if ($LASTEXITCODE -ne 0) {
    Write-Err '嵌入模型下载失败'
    Write-Host '  可加 -Mirror 参数使用国内镜像重试：'
    Write-Host "      重新双击 scripts\prepare.cmd 并加 -Mirror 参数" -ForegroundColor Gray
    exit 1
}

# ---------------------------------------------------------------------------
# 4. Ollama
# ---------------------------------------------------------------------------
if ($SkipOllama) {
    Write-Step '4/4 跳过 Ollama'
    Write-Warn2 ' 未准备 LLM。请自行安装 Ollama 并执行： ollama pull ' + $LlmModel
} else {
    Write-Step '4/4 准备 Ollama 与 LLM 模型'

    # 先体检：只认「有推理引擎」的安装，避免把半个包当成已就绪。
    #
    # 这里踩过一次真实的坑：ollama.exe 正在运行的话，Windows **不允许删除**它，
    # 原来的 `Remove-Item ... -ErrorAction SilentlyContinue` 会静默失败，
    # 紧接着 `Test-Path $OllamaExe` 仍然为真，于是脚本认定「已经装好了」，
    # 一路 [OK] 并以退出码 0 结束 —— 用户看到「一键安装成功」，
    # 但坏文件一个都没换掉（本项目的真实故障就是这么来的）。
    #
    # 现在的顺序是「先下载、后替换」：
    #   1. 只**标记**需要重装，先不动现有文件 —— 万一 1.4GB 下载失败，
    #      用户手上那个（虽然不完整的）Ollama 原封不动，不会更糟；
    #   2. 下载并校验 zip 成功之后，才停进程 → 删除 → 解压 → 再校验。
    $portableIncomplete = (Test-Path $OllamaExe) -and -not (Test-OllamaPayload $OllamaDir)
    $portableWasRunning = $false

    if ($portableIncomplete) {
        Write-Warn2 ' 检测到项目内置的 Ollama 不完整（缺少 lib\ollama 下的推理运行时）'
        Write-Host '   这种状态下服务能启动、模型也列得出来，但一提问就会报' -ForegroundColor Gray
        Write-Host '   “llama-server binary not found”。稍后会重新下载并替换它。' -ForegroundColor Gray
    }

    $needPortableInstall = $portableIncomplete -or (-not (Test-Path $OllamaExe))
    $useSystemOllama = $false

    if ($needPortableInstall -and -not $portableIncomplete) {
        # 完全没有 ollama.exe 时，系统里已有现成的就用现成的，不必下载 1.4GB
        $sysOllama = Get-Command ollama -ErrorAction SilentlyContinue
        if ($sysOllama) {
            Write-Ok " 使用系统已安装的 Ollama：$($sysOllama.Source)"
            $OllamaExe = $sysOllama.Source
            $OllamaDir = Split-Path -Parent $OllamaExe
            $needPortableInstall = $false
            $useSystemOllama = $true
        }
    }

    if (-not $needPortableInstall) {
        if (-not $useSystemOllama) { Write-Ok " 使用项目内置 Ollama：$OllamaExe" }
    } else {
        if ($portableIncomplete) {
            Write-Host '  正在下载完整的便携版（约 1.4GB），完成后才会替换现有文件 ...' -ForegroundColor Gray
        } else {
            Write-Host '  未找到 Ollama，下载便携版（免安装、免管理员）...'
        }

        # 多个下载源，按「实测最快/最可用」排序依次尝试。
        #
        # 本机实测（同一个网络环境下）：
        #   github.com 直连                → 连接被重置，不可用
        #   api.github.com 解析出的签名地址 → 3.0 MB/s（1.36GB 约 8 分钟）
        #   ghproxy.net 镜像               → 291 KB/s（约 82 分钟）
        # 所以默认先走 API 解析这条路；-Mirror 则把镜像提到最前面。
        $directUrl = 'https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip'
        $apiSource = @{
            Label = 'GitHub API 解析（api.github.com → release-assets）'
            Repo  = 'ollama/ollama'
            Tag   = 'latest'
            Asset = 'ollama-windows-amd64.zip'
        }
        $mirrorSource = @{
            Label = '国内镜像 ghproxy.net'
            Url   = 'https://ghproxy.net/' + $directUrl
        }
        $directSource = @{
            Label = 'GitHub 直连'
            Url   = $directUrl
        }

        $sources = if ($Mirror) {
            @($mirrorSource, $apiSource, $directSource)
        } else {
            @($apiSource, $mirrorSource, $directSource)
        }

        $zipPath = Join-Path $ProjectRoot 'tools\ollama-windows-amd64.zip'
        $partPath = "$zipPath.part"
        # Validator：只有「能打开中央目录」的 zip 才算下载完成。
        # 没下完的 zip 留着让 fetch.mjs 走 .part 续传，而不是删掉重来。
        $zipOk = { param($path) Test-ZipReadable -Path $path }

        $downloaded = $false
        $lastLabel = ''
        foreach ($source in $sources) {
            $lastLabel = $source.Label
            if ($source.ContainsKey('Repo')) {
                $ok = Get-RemoteFile -OutFile $zipPath -Label $source.Label -Validator $zipOk `
                    -GithubAssetRepo $source.Repo -GithubAssetTag $source.Tag -GithubAssetName $source.Asset
            } else {
                $ok = Get-RemoteFile -Url $source.Url -OutFile $zipPath -Label $source.Label -Validator $zipOk
            }

            if ($ok -and (Test-ZipReadable $zipPath)) {
                $downloaded = $true
                Write-Ok " 已从「$($source.Label)」下载完成"
                break
            }

            # 换源时必须丢掉上一个源的半成品：不同源的字节流未必要能拼在一起
            if (Test-Path $partPath) {
                Remove-Item $partPath -Force -ErrorAction SilentlyContinue
            }
            Write-Warn2 " 该来源未成功：$($source.Label)"
        }

        if (-not $downloaded) {
            Write-Err "Ollama 便携版下载失败（最后一个来源：$lastLabel）"
            Write-Host '  现有文件保持原样，没有做任何破坏性改动。' -ForegroundColor Gray
            Write-Host '  可手动下载后放到 tools\ollama-windows-amd64.zip，再重跑本脚本：' -ForegroundColor Yellow
            Write-Host '      https://github.com/ollama/ollama/releases' -ForegroundColor Gray
            Write-Host '  国内网络可加 -Mirror 优先走镜像。' -ForegroundColor Gray
            exit 1
        }

        # ---- 到这里才动现有安装 ----
        if ($portableIncomplete) {
            # 先看它是不是正在运行：是的话装完要保持它在跑，别让用户装完发现服务没了
            $portableWasRunning = @(Stop-PortableOllama -Dir $OllamaDir).Count -gt 0
            if ($portableWasRunning) {
                Write-Host '   已停止正在运行的项目内置 Ollama（Windows 不允许删除运行中的程序）' -ForegroundColor Gray
            }

            if (-not (Remove-OllamaDir -Dir $OllamaDir -Exe $OllamaExe)) {
                Write-Err ' 无法删除不完整的 tools\ollama：ollama.exe 仍被占用'
                Write-Host '   Windows 不允许删除正在运行的程序。请先关掉所有 Ollama 窗口与托盘图标，' -ForegroundColor Yellow
                Write-Host '   或手动执行下面这条命令后重新运行一键准备：' -ForegroundColor Yellow
                Write-Host '       Get-Process ollama -ErrorAction SilentlyContinue | Stop-Process -Force' -ForegroundColor Yellow
                exit 1
            }
        } elseif (-not (Remove-OllamaDir -Dir $OllamaDir -Exe $OllamaExe)) {
            Write-Err " 无法清空 $OllamaDir：其中的 ollama.exe 仍被占用"
            Write-Host '   请关掉正在运行的 Ollama 后重试。' -ForegroundColor Yellow
            exit 1
        }

        Write-Host '  解压到 tools\ollama ...'
        New-Item -ItemType Directory -Force -Path $OllamaDir | Out-Null
        try {
            Expand-Archive -Path $zipPath -DestinationPath $OllamaDir -Force -ErrorAction Stop
        } catch {
            Write-Err " 解压失败：$($_.Exception.Message)"
            Write-Host "   zip 可能不完整，删除后重跑本脚本：$zipPath" -ForegroundColor Yellow
            Remove-Item -Recurse -Force $OllamaDir -ErrorAction SilentlyContinue
            exit 1
        }

        # 解压完必须再校验一次：磁盘空间不足、杀软拦截都可能造成半成品
        if (-not (Test-OllamaPayload $OllamaDir)) {
            Write-Err ' 解压后的 Ollama 仍不完整（缺少推理引擎）'
            Write-Host "   期望存在：$(Join-Path $OllamaDir 'lib\ollama\llama-server.exe')" -ForegroundColor Yellow
            Write-Host '   常见原因：zip 没下完、磁盘空间不足、杀毒软件拦截了 dll 写入。' -ForegroundColor Yellow
            Write-Host '   处理办法：删掉 tools\ollama 与 zip 后重跑本脚本，或改用官方安装包：' -ForegroundColor Yellow
            Write-Host '       https://ollama.com/download' -ForegroundColor Gray
            exit 1
        }
        Write-Ok "Ollama 已就绪：$OllamaExe"
    }

    # 模型统一放在项目内，便于整体拷贝到离线机器
    $env:OLLAMA_MODELS = $ModelsDir
    New-Item -ItemType Directory -Force -Path $ModelsDir | Out-Null

    # 日志目录必须先建好，否则下面 Start-Process 的重定向会直接失败
    $LogsDir = Join-Path $ProjectRoot 'data\logs'
    New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

    # 确保服务在跑（pull 需要通过 HTTP API）
    $startedHere = $false
    try {
        Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null
        Write-Ok ' Ollama 服务已在运行'
        Write-Warn2 ' 注意：模型会拉取到「正在运行的那个服务」的模型目录，未必是项目内的 models\ollama'
    } catch {
        Write-Host '  启动 Ollama 服务 ...'
        Write-Host "  模型目录 OLLAMA_MODELS=$ModelsDir" -ForegroundColor Gray
        Start-Process -FilePath $OllamaExe -ArgumentList 'serve' -WindowStyle Hidden `
            -RedirectStandardError (Join-Path $LogsDir 'ollama.err.log') `
            -RedirectStandardOutput (Join-Path $LogsDir 'ollama.out.log')
        $startedHere = $true

        $up = $false
        for ($i = 0; $i -lt 40; $i++) {
            Start-Sleep -Milliseconds 500
            try {
                Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 2 | Out-Null
                Write-Ok ' Ollama 服务已启动'
                $up = $true
                break
            } catch { }
        }
        if (-not $up) {
            Write-Err ' Ollama 服务启动超时'
            Write-Host "  请查看日志：$(Join-Path $LogsDir 'ollama.err.log')" -ForegroundColor Yellow
            exit 1
        }
    }

    Write-Host "  拉取模型 $LlmModel （约 1.1GB，首次较慢）..."
    Write-Host "  模型存放目录：$ModelsDir" -ForegroundColor Gray
    & $OllamaExe pull $LlmModel

    if ($LASTEXITCODE -ne 0) {
        Write-Err "模型 $LlmModel 拉取失败"
        Write-Host "  可手动重试：`"$OllamaExe`" pull $LlmModel" -ForegroundColor Yellow
        if ($startedHere) { Write-Host '  Ollama 服务仍在后台运行。' }
        exit 1
    }
    Write-Ok "模型 $LlmModel 已就绪"

    # 校验模型确实落在预期位置
    $manifestDir = Join-Path $ModelsDir 'manifests'
    if (Test-Path $manifestDir) {
        Write-Ok " 模型文件已存放于：$ModelsDir"
    } elseif (-not $startedHere) {
        Write-Warn2 " 模型可能被拉取到了别处（当前有外部 Ollama 服务在运行）"
        Write-Host '  这不影响使用，但拷贝到离线机器前请确认模型目录。' -ForegroundColor Yellow
    }

    # 必须把本脚本启动的 Ollama 收掉，不能留着它跑。
    #
    # 原因是一个很隐蔽的 Windows 行为（实测）：子进程只要还活着，并且与
    # 本进程共享同一个控制台，**powershell.exe 就不会退出** ——
    # 脚本明明已经跑完最后一行，宿主进程却一直挂着。对「一键安装」来说这是
    # 致命的：「安装」日志俱全、退出码却永远不返回，网页上就一直停在「安装中…」。
    # （实测：把子进程杀掉，挂着的宿主立刻退出。）
    #
    # 所以这里照旧停掉它，并明确告诉用户怎么再拉起来 —— start.ps1 会在启动
    # 后端之前按需拉起 Ollama，所以再运行一次 start.cmd 就够了。
    if ($startedHere) {
        Write-Host '  停止临时启动的 Ollama 服务'
        Get-Process -Name 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
        if ($portableWasRunning) {
            Write-Host '  它安装前本来在运行：双击 scripts\start.cmd 会重新拉起它；' -ForegroundColor Gray
            Write-Host '  网页上点「重新检测」也会给出启动方式。' -ForegroundColor Gray
        }
    }
}

# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
# 依赖导入 + 便携版 Ollama 完整性。后者必须放在这里：
# 半成品会在「准备完成、退出码 0」之后才在提问时暴露，用户完全无从判断，
# 而这个脚本是唯一能在安装阶段就把它抓住的地方。
# ---------------------------------------------------------------------------
Write-Step '自检'
& $VenvPython (Join-Path $ProjectRoot 'tests\check_imports.py')
$importOk = ($LASTEXITCODE -eq 0)

$ollamaOk = $true
if (-not $SkipOllama -and (Test-Path $OllamaExe) -and -not (Test-OllamaPayload $OllamaDir)) {
    $ollamaOk = $false
    Write-Host ''
    Write-Err '内置 Ollama 仍不完整：缺少 lib\ollama 下的推理引擎'
    Write-Host "  期望存在：$(Join-Path $OllamaDir 'lib\ollama\llama-server.exe')" -ForegroundColor Yellow
    Write-Host '  也就是说：服务能启动、模型能列出，但**提问一定失败**。' -ForegroundColor Yellow
    Write-Host '  处理办法（任选其一）：' -ForegroundColor Yellow
    Write-Host '    1. 确保 Ollama 没在运行（含托盘图标），然后重跑本脚本' -ForegroundColor Gray
    Write-Host '    2. 改装官方安装包： https://ollama.com/download' -ForegroundColor Gray
} elseif (-not $SkipOllama -and (Test-Path $OllamaExe)) {
    Write-Host '[OK]   内置 Ollama 运行时完整' -ForegroundColor Green
}

Write-Host @"

============================================================================
 准备完成
============================================================================
 下一步：
     启动服务   双击 scripts\start.cmd
============================================================================
"@ -ForegroundColor Green

if (-not $importOk -or -not $ollamaOk) { exit 1 }
