"""首次配置体检：检测缺失组件，并生成**可直接复制执行**的安装指引。

指引由后端按当前实际安装位置生成、前端只负责展示：安装命令里的路径（项目位置、
ollama.exe 落在哪、脚本在哪）都是部署相关的，硬编码在前端换个目录就失效，也容易
与配置里的模型名/端口不一致。"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.core.logging import get_logger
from app.core.paths import PROJECT_ROOT
from app.schemas import SetupCommand, SetupIssue, SetupOption, SetupReport

logger = get_logger(__name__)

PORTABLE_DIR = PROJECT_ROOT / "tools" / "ollama"
PORTABLE_EXE = PORTABLE_DIR / "ollama.exe"
PORTABLE_ZIP = PROJECT_ROOT / "tools" / "ollama-windows-amd64.zip"
#: 便携版把推理引擎（llama-server）及其依赖放在这个子目录里：只有 ollama.exe
#: 而没有它是**不能推理**的，见 portable_ollama_status()。
OLLAMA_RUNTIME_SUBDIR = Path("lib") / "ollama"
OLLAMA_RUNNER_NAMES = ("llama-server.exe", "ollama-llama-server.exe")
PREPARE_SCRIPT = PROJECT_ROOT / "scripts" / "prepare.ps1"
START_SCRIPT = PROJECT_ROOT / "scripts" / "start.ps1"
DOWNLOAD_MODELS = PROJECT_ROOT / "scripts" / "download_models.py"
REQUIREMENTS = PROJECT_ROOT / "backend" / "requirements.txt"

OLLAMA_DOWNLOAD_PAGE = "https://ollama.com/download"
RELEASE_ZIP_URL = (
    "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip"
)


# ---------------------------------------------------------------------------
# 命令构造
# ---------------------------------------------------------------------------
def _ps(command: str, label: str = "在 PowerShell 中执行", note: str | None = None) -> SetupCommand:
    return SetupCommand(label=label, shell="powershell", command=command, note=note)


def _run_ps1(script: Path, extra: str = "") -> str:
    """生成调用 .ps1 的命令。刻意用 `powershell` 而不是 `pwsh`：Windows 自带前者。"""
    tail = f" {extra}".rstrip()
    return f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"{tail}'


def _quote(path: Path | str) -> str:
    return f'"{path}"'


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------
def detect_ollama_binary() -> dict[str, Any]:
    """查找可用的 ollama.exe，返回 ``{found, path, source}``。

    查找顺序与 start.ps1 保持一致：项目内置 → PATH → 常见安装位置。
    """
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "")

    candidates: list[tuple[Path | None, str]] = [
        (PORTABLE_EXE, "项目内置便携版"),
        (Path(local_appdata) / "Programs" / "Ollama" / "ollama.exe" if local_appdata else None,
         "用户安装"),
        (Path(program_files) / "Ollama" / "ollama.exe" if program_files else None,
         "系统安装"),
        (Path(program_files_x86) / "Ollama" / "ollama.exe" if program_files_x86 else None,
         "系统安装(32位目录)"),
    ]

    for path, source in candidates:
        if path and path.is_file():
            return {"found": True, "path": str(path), "source": source, "on_path": False}

    which = shutil.which("ollama")
    if which:
        return {"found": True, "path": which, "source": "PATH", "on_path": True}

    return {"found": False, "path": None, "source": None, "on_path": False}


def portable_ollama_status(portable_dir: Path | None = None) -> dict[str, Any]:
    """检查项目内置的便携版 Ollama 是否**完整**。

    解压不完整时（只剩 ollama.exe）服务照样能起、/api/tags 照样能列出模型，唯独一发
    提问就报 "llama-server binary not found"，所以必须查推理引擎文件是否存在。
    ``complete`` 为 ``None`` 表示不是便携版布局（如官方安装包），刻意不下结论以免误报。
    """
    directory = portable_dir if portable_dir is not None else PORTABLE_DIR
    exe = directory / "ollama.exe"
    runtime_dir = directory / OLLAMA_RUNTIME_SUBDIR

    status: dict[str, Any] = {
        "present": exe.is_file(),
        "dir": str(directory),
        "exe": str(exe),
        "runtime_dir": str(runtime_dir),
        "runtime_files": 0,
        "runners": [],
        "complete": None,
    }

    if not status["present"]:
        return status

    files = sorted(p.name for p in runtime_dir.iterdir() if p.is_file()) if runtime_dir.is_dir() else []
    runners = [name for name in files if name.lower() in OLLAMA_RUNNER_NAMES]

    status["runtime_files"] = len(files)
    status["runners"] = runners
    # 判定标准刻意宽松：优先认推理引擎本体；万一日后改名，只要运行时目录里确实
    # 有一批文件（而不是空目录）就不误报。
    status["complete"] = bool(runners) or len(files) >= 3
    return status


def _venv_python() -> Path:
    return PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def _probe_tool(name: str, extra_candidates: list[Path | None]) -> dict[str, Any]:
    found = shutil.which(name)
    if found:
        return {"found": True, "path": found, "source": "PATH"}

    for candidate in extra_candidates:
        if candidate and candidate.is_file():
            return {"found": True, "path": str(candidate), "source": "常见安装位置"}

    return {"found": False, "path": None, "source": None}


def detect_toolchain() -> dict[str, Any]:
    """探测本机可用的辅助工具。

    同一套安装指引在不同机器上的「最省事路径」不一样（有 uv 可全自动，只有 Python 走 pip），
    摆出探测结果，指引才算因地制宜。
    """
    home = Path(os.environ["USERPROFILE"]) if os.environ.get("USERPROFILE") else None
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "")

    uv = _probe_tool("uv", [
        (home / ".local" / "bin" / "uv.exe") if home else None,
        (home / ".cargo" / "bin" / "uv.exe") if home else None,
        (Path(local_appdata) / "Programs" / "uv" / "uv.exe") if local_appdata else None,
        (Path(local_appdata) / "Microsoft" / "WinGet" / "Links" / "uv.exe") if local_appdata else None,
    ])

    node = _probe_tool("node", [
        (Path(program_files) / "nodejs" / "node.exe") if program_files else None,
    ])

    venv_python = _venv_python()

    return {
        "uv": uv,
        "node": node,
        "venv": {
            "found": venv_python.is_file(),
            "path": str(venv_python) if venv_python.is_file() else None,
        },
        "python": {
            "path": sys.executable,
            "version": sys.version.split()[0],
        },
        "powershell": {
            # 系统自带的是 powershell.exe；pwsh 需要额外安装
            "windows_powershell": bool(shutil.which("powershell")),
            "pwsh": bool(shutil.which("pwsh")),
        },
    }


def _python_cmd() -> str:
    """优先用项目 venv 的 python，退回当前解释器。"""
    venv = _venv_python()
    exe = venv if venv.is_file() else Path(sys.executable)
    return _quote(exe)


# ---------------------------------------------------------------------------
# 各类问题的解决方式
# ---------------------------------------------------------------------------
def _embedding_options() -> list[SetupOption]:
    return [
        SetupOption(
            id="prepare",
            title="一键准备（推荐）",
            recommended=True,
            description=(
                "装依赖、下嵌入模型、装 Ollama、拉 LLM 模型，一次全部搞定。"
                "只需在首次部署时联网执行一次。"
            ),
            commands=[_ps(_run_ps1(PREPARE_SCRIPT), "完整准备（需要联网，约 2.6GB 下载）")],
            notes=["国内网络可加 -Mirror 走镜像加速"],
        ),
        SetupOption(
            id="model_only",
            title="只下载嵌入模型",
            description="已有 Python 环境、只想补下嵌入模型时使用。",
            commands=[
                _ps(f"{_python_cmd()} {_quote(DOWNLOAD_MODELS)}", "下载嵌入模型（约 90MB）"),
                _ps(f"{_python_cmd()} {_quote(DOWNLOAD_MODELS)} --mirror", "国内镜像加速"),
            ],
        ),
    ]


def _ollama_missing_options() -> list[SetupOption]:
    return [
        SetupOption(
            id="prepare",
            title="方式一：让本项目自动准备（推荐）",
            recommended=True,
            description=(
                "执行脚本会自动下载便携版 Ollama 解压到 tools\\ollama\\，"
                "并把 LLM 模型拉取到项目内的 models\\ollama\\。"
            ),
            commands=[_ps(_run_ps1(PREPARE_SCRIPT), "下载并解压便携版 Ollama + 拉取 LLM")],
            notes=[
                "免安装、免管理员权限、不写注册表、不动 Program Files",
                "模型随项目走，便于整体拷贝到离线机器",
                "便携包含 CUDA 运行库，体积约 1.4GB",
            ],
        ),
        SetupOption(
            id="installer",
            title="方式二：官方安装包",
            description=(
                "从官网下载 OllamaSetup.exe 安装。安装后 ollama 会进入 PATH 并开机自启，"
                "启动脚本能自动发现它。"
            ),
            commands=[
                _ps(f"ollama pull {settings.llm_model}", "安装完成后拉取模型"),
            ],
            notes=[
                "安装包会自动配置 PATH，无需额外设置",
                "模型默认存放于 %USERPROFILE%\\.ollama\\models（不在项目内）",
                "若之后要拷到离线机器，需要额外拷贝该目录",
            ],
            link=OLLAMA_DOWNLOAD_PAGE,
        ),
        SetupOption(
            id="portable_manual",
            title="方式三：手动解压便携版",
            description="网络环境受限、想自己控制下载时使用。下载 zip 后手动解压并启动。",
            commands=[
                _ps(
                    f"Expand-Archive {_quote(PORTABLE_ZIP)} {_quote(PORTABLE_DIR)} -Force",
                    "1) 解压到项目内的 tools\\ollama",
                ),
                _ps(
                    f'$env:OLLAMA_MODELS = {_quote(PROJECT_ROOT / "models" / "ollama")}\n'
                    f"& {_quote(PORTABLE_EXE)} serve",
                    "2) 启动服务（必须先设 OLLAMA_MODELS）",
                ),
                _ps(
                    f"& {_quote(PORTABLE_EXE)} pull {settings.llm_model}",
                    "3) 另开一个窗口拉取模型",
                ),
            ],
            notes=[
                "注意：ollama pull 只是请求正在运行的 serve 进程，"
                "模型存到哪里由 serve 的 OLLAMA_MODELS 决定",
                "如果系统里已有 Ollama 在运行，pull 会走那个实例的目录",
            ],
            link=RELEASE_ZIP_URL,
        ),
    ]


def _ollama_incomplete_options(binary: dict[str, Any], status: dict[str, Any]) -> list[SetupOption]:
    return [
        SetupOption(
            id="repair",
            title="方式一：重新执行一键准备（推荐）",
            recommended=True,
            description=(
                "准备脚本会先检查内置 Ollama 是否完整，发现缺文件就把 tools\\ollama 清掉重下重解压，"
                "因此直接重跑一次即可修好，不用手动删目录。"
            ),
            commands=[_ps(_run_ps1(PREPARE_SCRIPT), "重新准备（会重新下载便携版，需要联网）")],
            notes=[
                "完整包约 1.4GB（含 CUDA 运行库），请尽量在网络通畅时执行",
                "国内网络可加 -Mirror 走镜像加速",
                f"检测到的运行时目录：{status.get('runtime_dir')}",
            ],
        ),
        SetupOption(
            id="official",
            title="方式二：改装官方安装包",
            description=(
                "官方安装包自带完整运行时，装好后 ollama 会进入 PATH，本项目的启动脚本能自动发现它。"
            ),
            commands=[
                _ps(f"ollama pull {settings.llm_model}", "安装完成后拉取模型（模型已存在则会秒过）"),
            ],
            notes=[
                "装完之后建议删掉不完整的 tools\\ollama，避免启动脚本又优先选中它",
                "模型默认存放在 %USERPROFILE%\\.ollama\\models（不在项目内）",
            ],
            link=OLLAMA_DOWNLOAD_PAGE,
        ),
        SetupOption(
            id="manual_unzip",
            title="方式三：手动重新解压 zip",
            description="已经自己下好了 zip、只是上次解压中断时使用。",
            commands=[
                _ps(
                    f"Remove-Item -Recurse -Force {_quote(PORTABLE_DIR)}",
                    "1) 先清掉不完整的安装",
                ),
                _ps(
                    f"Expand-Archive {_quote(PORTABLE_ZIP)} {_quote(PORTABLE_DIR)} -Force",
                    "2) 重新解压",
                ),
                _ps(
                    f"Test-Path {_quote(PORTABLE_DIR / OLLAMA_RUNTIME_SUBDIR / 'llama-server.exe')}",
                    "3) 校验：应输出 True",
                ),
            ],
            notes=["解压后目录里应当有 lib\\ollama\\llama-server.exe 等一批文件；只有 ollama.exe 说明 zip 本身没下完"],
            link=RELEASE_ZIP_URL,
        ),
    ]


def _ollama_not_running_options(binary: dict[str, Any]) -> list[SetupOption]:
    exe = binary.get("path") or "ollama"
    return [
        SetupOption(
            id="start_script",
            title="用启动脚本一起拉起（推荐）",
            recommended=True,
            description="start.ps1 会按需启动 Ollama 并随后启动后端服务。",
            commands=[_ps(_run_ps1(START_SCRIPT), "启动 Ollama + 后端")],
        ),
        SetupOption(
            id="manual_serve",
            title="手动启动 Ollama 服务",
            description="已经用脚本启动过后端、只想单独把 Ollama 拉起来。",
            commands=[
                _ps(f"& {_quote(exe)} serve", "前台启动（保持窗口不要关）"),
            ],
            notes=["Windows 官方安装包通常已设为开机自启，可先确认托盘里没有 Ollama 图标"],
        ),
    ]


def _model_missing_options(binary: dict[str, Any]) -> list[SetupOption]:
    exe = binary.get("path") or "ollama"
    return [
        SetupOption(
            id="pull",
            title=f"拉取模型 {settings.llm_model}",
            recommended=True,
            description="Ollama 已在运行，但里面没有配置的 LLM 模型。",
            commands=[
                _ps(f"& {_quote(exe)} pull {settings.llm_model}", f"拉取 {settings.llm_model}"),
                _ps(f"& {_quote(exe)} list", "确认是否已安装"),
            ],
            notes=[
                f"deepseek-r1:1.5b 约 1.1GB，首次拉取较慢",
                "也可以改用其他模型，然后修改 .env 里的 RAG_LLM_MODEL",
            ],
        ),
        SetupOption(
            id="switch_model",
            title="换用机器上已有的模型",
            description="如果 Ollama 里已经有别的模型，直接改配置即可，无需再下载。",
            commands=[
                _ps(f"& {_quote(exe)} list", "1) 查看已安装的模型"),
                _ps(
                    f'Add-Content -Path {_quote(PROJECT_ROOT / ".env")} '
                    f'-Value "RAG_LLM_MODEL=<你选中的模型名>"',
                    "2) 写入 .env 后重启服务",
                ),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
async def build_report(fresh: bool = False) -> SetupReport:
    """生成配置体检报告。``fresh=True`` 时绕过 Ollama 探测缓存。"""
    from app.services import ollama_client

    if fresh:
        # 点「重新检测」通常意味着刚装完东西（很可能刚 ollama pull），两个缓存都要清：
        # 可达性与模型列表、以及模型能力（是否支持 thinking）。
        ollama_client.invalidate_all()

    reachable, model_names = await ollama_client.probe()
    model_ok = ollama_client.model_is_available(settings.llm_model, model_names)
    binary = detect_ollama_binary()
    embedding_ready = settings.is_embedding_ready()
    venv_ready = _venv_python().is_file()

    # 「文件在」不等于「能加载」：模型目录可能是坏的（权重下载到一半、依赖版本不兼容），
    # 这里真加载一次，把失败原因摆进报告，而不是等用户第一次提问才炸在半路。
    # 加载 sentence-transformers 是 CPU 密集的阻塞操作，必须放线程里，否则会卡住事件循环。
    embedding_loadable: bool | None = None
    embedding_error: str | None = None
    if embedding_ready:
        from app.services.embeddings import try_get_embeddings

        embeddings = await asyncio.to_thread(try_get_embeddings)
        embedding_loadable = embeddings is not None
        if not embedding_loadable:
            from app.services.embeddings import embedding_status

            embedding_error = embedding_status().get("error") or "未知原因"

    issues: list[SetupIssue] = []

    # ---------------- 嵌入模型 ----------------
    if not embedding_ready:
        issues.append(
            SetupIssue(
                id="embedding_missing",
                severity="blocking",
                title="缺少本地嵌入模型",
                detail=(
                    f"未找到 {settings.embedding_model_name}。"
                    "没有它就无法把文档转成向量，检索和问答都用不了。"
                ),
                impact="无法上传索引文档，也无法提问",
                options=_embedding_options(),
            )
        )
    elif embedding_loadable is False:
        issues.append(
            SetupIssue(
                id="embedding_broken",
                severity="blocking",
                title="本地嵌入模型存在但加载失败",
                detail=(
                    f"在 {settings.embedding_dir} 找到了模型文件，但加载时报错：{embedding_error}。"
                    "常见原因是权重文件下载不完整，或依赖版本不匹配。"
                ),
                impact="上传文档与提问都会失败",
                options=_embedding_options(),
            )
        )

    # 依赖没装好时上面的命令也跑不起来，单独提示
    if not venv_ready:
        issues.append(
            SetupIssue(
                id="venv_missing",
                severity="blocking",
                title="Python 依赖尚未安装",
                detail=(
                    "项目虚拟环境 .venv 不存在。当前能启动后端，说明是用了别处的 Python，"
                    "但本项目的脚本（下载模型、准备环境）依赖 .venv。"
                ),
                impact="项目自带脚本无法运行",
                options=[
                    SetupOption(
                        id="prepare",
                        title="一键准备环境（推荐）",
                        recommended=True,
                        description="创建 .venv 并安装全部依赖，同时会准备模型。",
                        commands=[_ps(_run_ps1(PREPARE_SCRIPT), "完整准备（需要联网）")],
                    ),
                    SetupOption(
                        id="pip_only",
                        title="只安装依赖",
                        description="已有 Python 3.10+，只想装依赖。",
                        commands=[
                            _ps(
                                f"{_quote(sys.executable)} -m venv {_quote(PROJECT_ROOT / '.venv')}",
                                "1) 创建虚拟环境",
                            ),
                            _ps(
                                f"& {_quote(_venv_python())} -m pip install -r {_quote(REQUIREMENTS)}",
                                "2) 安装依赖",
                            ),
                        ],
                    ),
                ],
            )
        )

    # ---------------- Ollama ----------------
    portable = portable_ollama_status()

    if not binary["found"]:
        issues.append(
            SetupIssue(
                id="ollama_not_installed",
                severity="blocking",
                title="未检测到 Ollama",
                detail=(
                    "Ollama 是本地运行大模型的引擎，负责根据检索到的文档生成回答。"
                    "当前系统里没有找到 ollama 可执行文件，也没有在监听 "
                    f"{settings.ollama_base_url}。"
                ),
                impact="可以上传和检索文档，但无法生成回答",
                options=_ollama_missing_options(),
            )
        )
    elif portable["present"] and portable["complete"] is False:
        # 服务能起来、模型也列得出来，但推理引擎缺失 —— 这是最容易被忽略的一种，
        # 必须排在「服务未启动 / 缺模型」之前单独报出来，否则用户会一直以为只是没拉模型。
        issues.append(
            SetupIssue(
                id="ollama_incomplete",
                severity="blocking",
                title="Ollama 安装不完整：缺少推理引擎",
                detail=(
                    f"在 {portable['exe']} 找到了 ollama.exe，但 {portable['runtime_dir']} "
                    "下没有推理运行时文件（llama-server.exe 等）。"
                    "这种状态下 Ollama 服务能正常启动、/api/tags 也能列出模型，"
                    "只有真正生成回答时会失败："
                    "“error starting llama-server: llama-server binary not found”。"
                ),
                impact="上传文档与检索正常，但任何提问都会失败",
                options=_ollama_incomplete_options(binary, portable),
            )
        )
    elif not reachable:
        issues.append(
            SetupIssue(
                id="ollama_not_running",
                severity="blocking",
                title="Ollama 已安装但服务未启动",
                detail=(
                    f"在 {binary['path']} 找到了 Ollama（{binary['source']}），"
                    f"但 {settings.ollama_base_url} 没有响应。"
                ),
                impact="无法生成回答",
                options=_ollama_not_running_options(binary),
            )
        )
    elif not model_ok:
        issues.append(
            SetupIssue(
                id="llm_model_missing",
                severity="blocking",
                title=f"Ollama 中缺少模型 {settings.llm_model}",
                detail=(
                    "服务是通的，但里面没有配置的推理模型。"
                    + (f"当前已安装：{', '.join(sorted(model_names))}" if model_names else "当前一个模型都没有。")
                ),
                impact="无法生成回答",
                options=_model_missing_options(binary),
            )
        )

    blocking = sum(1 for issue in issues if issue.severity == "blocking")
    warnings = sum(1 for issue in issues if issue.severity == "warning")

    if blocking == 0:
        headline = "所有组件均已就绪"
    elif blocking == 1:
        headline = f"还需完成 1 项配置即可开始问答"
    else:
        headline = f"还需完成 {blocking} 项配置即可开始问答"

    return SetupReport(
        ready=blocking == 0,
        blocking_count=blocking,
        warning_count=warnings,
        headline=headline,
        issues=issues,
        environment={
            "project_root": str(PROJECT_ROOT),
            "ollama_binary": binary,
            "ollama_portable": portable,
            "ollama_base_url": settings.ollama_base_url,
            "ollama_reachable": reachable,
            "ollama_models": sorted(model_names),
            "llm_model": settings.llm_model,
            "llm_model_available": model_ok,
            "embedding_model": settings.embedding_model_name,
            "embedding_dir": str(settings.embedding_dir),
            "embedding_ready": embedding_ready,
            "embedding_loadable": embedding_loadable,
            "embedding_error": embedding_error,
            "venv_ready": venv_ready,
            "prepare_script": str(PREPARE_SCRIPT),
            "portable_dir": str(PORTABLE_DIR),
            "models_dir": str(PROJECT_ROOT / "models" / "ollama"),
            "platform": sys.platform,
            "toolchain": detect_toolchain(),
            "can_auto_install": PREPARE_SCRIPT.is_file(),
        },
        checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )
