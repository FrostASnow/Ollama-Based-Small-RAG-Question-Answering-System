"""服务就绪后自动打开浏览器。

以「真的请求一次目标地址、拿到 2xx」判断就绪（而不是猜一个等待时间）；探测走本机
回环并显式禁用代理解析（否则 ``HTTP_PROXY`` 会把 127.0.0.1 的请求也交给代理），
实际请求放工作线程，不阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import time
import urllib.request
from collections.abc import Callable

from app.core.logging import get_logger

logger = get_logger(__name__)

#: 最长等待就绪时间（留足余量，覆盖杀毒软件体检等慢启动情况）
DEFAULT_TIMEOUT_S = 120.0
#: 轮询间隔
POLL_INTERVAL_S = 0.3
#: 单次探测超时
PROBE_TIMEOUT_S = 3.0

#: 注入点：测试可以替换成假实现，避免真的弹出浏览器
_open_url: Callable[[str], bool] | None = None


def _probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """请求一次 ``url``；拿到 2xx 才算「服务已就绪」。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "rag-qa-readiness-probe"})
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            return 200 <= status < 300
    except Exception:  # noqa: BLE001 - 连接被拒/超时/代理错误都属于「还没就绪」
        return False


def probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """``_probe`` 的公开别名（供测试与诊断使用）。"""
    return _probe(url, timeout=timeout)


async def wait_until_ready(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_S,
    interval: float = POLL_INTERVAL_S,
    probe_func: Callable[[str], bool] | None = None,
) -> float | None:
    """轮询直到 ``url`` 返回 2xx：返回等待耗时（秒），超时返回 ``None``。"""
    check = probe_func or _probe
    started = time.perf_counter()
    deadline = started + timeout

    while True:
        if await asyncio.to_thread(check, url):
            return time.perf_counter() - started
        if time.perf_counter() >= deadline:
            return None
        await asyncio.sleep(interval)


def _default_opener(url: str) -> bool:
    """系统默认浏览器（Windows 上即 ShellExecute 打开默认程序）。"""
    import webbrowser

    return bool(webbrowser.open(url))


def open_browser(url: str, opener: Callable[[str], bool] | None = None) -> bool:
    """用系统默认浏览器打开 ``url``；``opener`` 仅用于测试注入，避免真的弹窗。"""
    opener = opener or _open_url or _default_opener
    try:
        return bool(opener(url))
    except Exception as exc:  # noqa: BLE001
        logger.warning("自动打开浏览器失败：%s（请手动访问 %s）", exc, url)
        return False


async def _auto_open(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
    elapsed = await wait_until_ready(url, timeout=timeout)
    if elapsed is None:
        logger.warning(
            "等待 %.0f 秒后服务仍未就绪，未自动打开浏览器；请稍后手动访问 %s", timeout, url
        )
        return
    logger.info("服务已就绪（%.1fs），正在打开浏览器：%s", elapsed, url)
    await asyncio.to_thread(open_browser, url)


def schedule_auto_open(url: str, enabled: bool = True) -> asyncio.Task[None] | None:
    """按需调度「就绪后自动打开浏览器」；``url`` 为空或 ``enabled`` 为假时返回 ``None``。

    必须在事件循环内调用 —— lifespan 正好满足。
    """
    if not url or not enabled:
        return None
    logger.info("浏览器会在服务就绪后自动打开：%s（无需手动刷新）", url)
    return asyncio.create_task(_auto_open(url))
