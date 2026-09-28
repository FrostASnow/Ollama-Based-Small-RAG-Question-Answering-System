"""结构化日志。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from app.core.paths import LOGS_DIR

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"


def _ensure_utf8_streams() -> None:
    """让中文日志在「控制台 / 管道 / 重定向」下都能正确输出。

    * 直接连到 Windows 控制台时，``sys.stdout.encoding`` 已经是 ``utf-8``
      （Python 自己负责转成 UTF-16 写控制台），此时不能改，改了反而会乱码。
    * 输出被管道或重定向时，编码会退回系统 ANSI 代码页（中文 Windows 上是 cp936），
      下游若按 UTF-8 读取就是一片乱码。这里统一改成 UTF-8。

    这正是「启动脚本一跑满屏乱码」的根因所在。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding in ("utf8", "utf8mb4", "cp65001"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # 某些被包装过的流不支持 reconfigure，忽略即可，不影响主流程
            pass


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    _ensure_utf8_streams()

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOGS_DIR / "backend.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 降低第三方库噪音
    for noisy in ("httpx", "urllib3", "sentence_transformers", "transformers", "faiss"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
