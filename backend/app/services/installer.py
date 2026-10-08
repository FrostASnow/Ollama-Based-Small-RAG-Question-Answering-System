"""一键安装：让用户直接在网页上跑完环境准备。

子进程输出重定向到文件而非管道捕获（受限环境禁止匿名管道，后端重启也会丢管道内容）；
日志靠字节偏移量增量读取，故刷新页面后仍能回放完整历史；单例互斥防并发下载；
取消时按进程树杀（prepare.ps1 下面还有 python/ollama 子进程）。"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.core.paths import LOGS_DIR, PROJECT_ROOT

logger = get_logger(__name__)

INSTALL_LOG = LOGS_DIR / "install.log"
PREPARE_SCRIPT = PROJECT_ROOT / "scripts" / "prepare.ps1"

# 日志回放上限：只把最近这么多行推给新接入的客户端，避免一次吐出几万行
MAX_REPLAY_LINES = 400


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Installer:
    """一次性环境准备任务的管理器。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._status = "idle"  # idle | running | succeeded | failed | cancelled
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._returncode: int | None = None
        self._mode: dict[str, Any] = {}
        self._log_handle: Any = None
        self._lines_read = 0
        # 进度行节流状态：进度条一秒刷新几十次，全推给前端既没用又卡
        self._last_progress_text: str | None = None
        self._last_progress_at = 0.0

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "returncode": self._returncode,
                "running": self._status == "running",
                "mode": dict(self._mode),
                "log_file": str(INSTALL_LOG),
            }

    def is_running(self) -> bool:
        with self._lock:
            return self._status == "running"

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def start(self, mirror: bool = False, skip_ollama: bool = False) -> tuple[bool, str]:
        """启动准备脚本。返回 ``(是否已启动, 说明)``。"""
        with self._lock:
            if self._status == "running":
                return False, "已有安装任务正在运行"

            if not PREPARE_SCRIPT.is_file():
                return False, f"未找到准备脚本：{PREPARE_SCRIPT}"

            args = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", str(PREPARE_SCRIPT),
            ]
            if mirror:
                args.append("-Mirror")
            if skip_ollama:
                args.append("-SkipOllama")

            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            # 每次都从头写，避免上次的失败信息混进这次输出
            try:
                INSTALL_LOG.write_text("", encoding="utf-8")
            except OSError as exc:
                return False, f"无法写入日志文件：{exc}"

            self._lines_read = 0
            self._last_progress_text = None
            self._last_progress_at = 0.0

            # 顺序不能反：先写服务端头部，再用 "ab" 追加模式打开子进程输出句柄。
            # 若先以 "wb" 打开（文件位置在 0）再追加头部，子进程第一笔输出会从偏移 0
            # 覆盖上去，日志里出现被啃掉前半截的行。
            self._append("[rag-qa] 开始准备环境 ...")
            self._append(f"[rag-qa] 命令：{' '.join(args)}")
            self._append(f"[rag-qa] 完整日志：{INSTALL_LOG}")

            try:
                self._log_handle = INSTALL_LOG.open("ab", buffering=0)
                self._process = subprocess.Popen(
                    args,
                    stdout=self._log_handle,
                    stderr=subprocess.STDOUT,
                    cwd=str(PROJECT_ROOT),
                    env={
                        **os.environ,
                        "PYTHONIOENCODING": "utf-8",
                        "PYTHONUNBUFFERED": "1",
                    },
                    # 独立进程组，方便取消时整棵树一起杀
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
            except Exception as exc:  # noqa: BLE001
                self._cleanup_handle()
                return False, f"无法启动准备脚本：{exc}"

            self._status = "running"
            self._started_at = _now_iso()
            self._finished_at = None
            self._returncode = None
            self._mode = {"mirror": mirror, "skip_ollama": skip_ollama}

            threading.Thread(target=self._waiter, args=(self._process,), daemon=True).start()
            logger.info("安装任务已启动：%s", " ".join(args))
            return True, "已开始"

    def _waiter(self, process: subprocess.Popen) -> None:
        """后台线程：只负责等待进程结束并落状态。"""
        try:
            returncode = process.wait()
        except Exception as exc:  # noqa: BLE001
            returncode = -1
            self._append(f"[rag-qa] 等待进程时出错：{exc}")

        with self._lock:
            self._returncode = returncode
            self._finished_at = _now_iso()
            if self._status == "cancelled":
                pass  # 取消时状态已定，不要覆盖
            elif returncode == 0:
                self._status = "succeeded"
            else:
                self._status = "failed"
            final_status = self._status

        self._append(f"[rag-qa] 进程结束，退出码 {returncode}")
        self._append(f"[rag-qa] 任务状态：{final_status}")

        # prepare.ps1 刚拉完 LLM 模型（或刚装好便携版 Ollama），两个探测结果的
        # TTL 缓存不主动失效的话，用户看到 [OK] 后界面仍会报「没有这个模型」。
        if final_status == "succeeded":
            self._invalidate_probe_caches()

        self._cleanup_handle()
        logger.info("安装任务结束：status=%s returncode=%s", final_status, returncode)

    @staticmethod
    def _invalidate_probe_caches() -> None:
        """让 Ollama 探测缓存立刻失效（安装刚结束，模型列表多半已经变了）。"""
        try:
            from app.services.ollama_client import invalidate_all

            invalidate_all()
        except Exception as exc:  # noqa: BLE001 - 缓存刷新失败不该影响安装收尾
            logger.debug("刷新 Ollama 探测缓存失败（忽略）：%s", exc)

    def _cleanup_handle(self) -> None:
        try:
            if self._log_handle:
                self._log_handle.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._log_handle = None

    # ------------------------------------------------------------------
    # 取消
    # ------------------------------------------------------------------
    def cancel(self) -> tuple[bool, str]:
        with self._lock:
            if self._status != "running" or self._process is None:
                return False, "当前没有正在运行的安装任务"

            pid = self._process.pid
            self._status = "cancelled"

        self._append("[rag-qa] 收到取消请求，正在终止进程树 ...")
        try:
            # /T 连子进程一起杀：prepare.ps1 下面还有 python、ollama 等
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=20,
            )
            message = "已取消安装"
        except Exception as exc:  # noqa: BLE001
            try:
                self._process.kill()
                message = "已强制结束（未能清理子进程）"
            except Exception:  # noqa: BLE001
                message = f"取消失败：{exc}"

        logger.info("安装任务已取消：%s", message)
        return True, message

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _append(self, line: str) -> None:
        """把服务端自己的提示也写进同一个日志文件，保证时序一致。"""
        try:
            with INSTALL_LOG.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass

    @staticmethod
    def _parse_segments(data: bytes) -> list[dict[str, Any]]:
        """把原始字节解析成日志行，并区分「普通行」和「进度行」。

        终止符决定行性质：``\\n`` 是普通行（追加），``\\r`` 是进度刷新（curl、tqdm
        都这么做，语义是回到行首重写），前端应**覆盖上一行**而非追加；不区分的话
        一次大文件下载就会刷出成百上千行 ``0 0 0 0 0``，把真正的错误冲出视野。
        """
        text = data.decode("utf-8", errors="replace")
        segments: list[dict[str, Any]] = []
        buffer = ""

        for char in text:
            if char == "\n":
                if buffer:
                    segments.append({"text": buffer, "progress": False})
                buffer = ""
            elif char == "\r":
                if buffer:
                    segments.append({"text": buffer, "progress": True})
                buffer = ""
            else:
                buffer += char

        if buffer:
            segments.append({"text": buffer, "progress": True})

        return segments

    @staticmethod
    def _collapse_progress(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """回放历史时，同一段进度只保留最后一帧。"""
        collapsed: list[dict[str, Any]] = []
        for segment in segments:
            if segment["progress"] and collapsed and collapsed[-1]["progress"]:
                collapsed[-1] = segment
            else:
                collapsed.append(segment)
        return collapsed

    def _throttle_progress(self, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """限制进度行的推送频率：内容没变或变化太频繁（约 300ms 一条）就丢弃，
        但**每一轮的最后一条进度一定发出**，否则界面会停在过时的进度上。
        """
        now = time.monotonic()
        progress_indexes = [i for i, s in enumerate(segments) if s["progress"]]
        last_progress_index = progress_indexes[-1] if progress_indexes else -1

        out: list[dict[str, Any]] = []
        for index, segment in enumerate(segments):
            if not segment["progress"]:
                out.append(segment)
                self._last_progress_text = None  # 普通行之后进度重新计数
                continue

            if segment["text"] == self._last_progress_text:
                continue

            if index != last_progress_index and (now - self._last_progress_at) < 0.3:
                continue

            self._last_progress_text = segment["text"]
            self._last_progress_at = now
            out.append(segment)

        return out

    def read_new_lines(self) -> list[dict[str, Any]]:
        """增量读取日志新增内容。

        偏移量必须以**字节**为单位推进：子进程随时可能写出半行，末尾多字节字符被
        截断后解码得到 U+FFFD，重新编码是 3 字节、与原始残缺序列不等长，偏移量会
        逐渐漂移，日志里就会出现被啃掉前半截的行甚至整段重复。
        """
        if not INSTALL_LOG.is_file():
            return []

        try:
            with INSTALL_LOG.open("rb") as handle:
                handle.seek(self._lines_read)
                chunk = handle.read()
        except OSError:
            return []

        if not chunk:
            return []

        # 只处理到最后一个换行或回车为止，剩下的半行留给下次
        last_break = max(chunk.rfind(b"\n"), chunk.rfind(b"\r"))
        if last_break == -1:
            return []

        complete = chunk[: last_break + 1]
        self._lines_read += len(complete)
        return self._throttle_progress(self._parse_segments(complete))

    def replay(self, max_lines: int = MAX_REPLAY_LINES) -> tuple[list[dict[str, Any]], int]:
        """读取已有日志供回放，返回 ``(最近若干行, 应当设置的读取偏移量)``。

        行和偏移量必须在**同一次读取**里确定：先 tail 再 sync 的话，两次调用之间
        新写入的内容会被永久跳过；偏移量也只推进到最后一个完整换行处。
        """
        if not INSTALL_LOG.is_file():
            return [], 0

        try:
            with INSTALL_LOG.open("rb") as handle:
                data = handle.read()
        except OSError:
            return [], 0

        last_break = max(data.rfind(b"\n"), data.rfind(b"\r"))
        consumed = last_break + 1 if last_break != -1 else 0
        segments = self._collapse_progress(self._parse_segments(data[:consumed]))
        return segments[-max_lines:], consumed

    def set_offset(self, offset: int) -> None:
        self._lines_read = max(0, offset)

    def reset(self) -> None:
        """清空状态，让用户可以重新发起安装。"""
        with self._lock:
            if self._status == "running":
                return
            self._status = "idle"
            self._started_at = None
            self._finished_at = None
            self._returncode = None
            self._mode = {}
            self._lines_read = 0


installer = Installer()


async def stream_install() -> Any:
    """SSE 事件生成器：先回放历史，再持续跟进新输出。"""
    from app.core.sse import format_sse

    snapshot = installer.snapshot()

    # 回放已有日志，让中途刷新页面的用户也能看到完整过程
    if snapshot["status"] != "idle":
        replay_segments, offset = installer.replay()
        installer.set_offset(offset)
        for segment in replay_segments:
            yield format_sse("log", {"line": segment["text"], "progress": segment["progress"]})
        yield format_sse("status", snapshot)

    if not snapshot["running"]:
        yield format_sse("end", snapshot)
        return

    idle_rounds = 0
    while True:
        segments = installer.read_new_lines()
        if segments:
            idle_rounds = 0
            for segment in segments:
                yield format_sse(
                    "log", {"line": segment["text"], "progress": segment["progress"]}
                )
        else:
            idle_rounds += 1
            if idle_rounds % 20 == 0:  # 约每 10 秒一次心跳
                yield ": ping\n\n"

        state = installer.snapshot()
        if not state["running"]:
            # 收尾：把最后一点输出读干净
            for segment in installer.read_new_lines():
                yield format_sse(
                    "log", {"line": segment["text"], "progress": segment["progress"]}
                )
            yield format_sse("status", state)
            yield format_sse("end", state)
            return

        await _sleep(0.5)


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


__all__ = ["installer", "stream_install", "INSTALL_LOG"]
