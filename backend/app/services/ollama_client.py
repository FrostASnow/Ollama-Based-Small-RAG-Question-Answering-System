"""Ollama 服务探测（异步、带短超时与结果缓存）。

只用于健康检查与模型列表，不参与推理（推理走 LangChain 的 ChatOllama）。

设计要点
--------
Ollama 不可达时，连接尝试要等到超时才返回。健康检查是前端启动时第一个
调用的接口，如果每次都要串行等两次超时，界面就会「卡住好几秒」。
因此这里：
  1. 只发一次请求（/api/tags 成功即视为可达，不额外探测）；
  2. 超时压到 2 秒；
  3. 用 TTL 缓存结果，避免前端反复刷新时把等待时间叠加起来。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 2.0
CACHE_TTL_SECONDS = 5.0

_cache: dict[str, Any] = {"at": 0.0, "reachable": False, "names": []}


async def probe(base_url: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> tuple[bool, list[str]]:
    """探测 Ollama 是否可达，并返回已安装模型名列表。

    返回 ``(可达?, 模型名列表)``。命中缓存时不会发起网络请求。
    """
    url = (base_url or settings.ollama_base_url).rstrip("/")
    now = time.monotonic()

    if now - _cache["at"] < CACHE_TTL_SECONDS:
        return bool(_cache["reachable"]), list(_cache["names"])

    reachable = False
    names: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{url}/api/tags")
            if response.status_code == 200:
                reachable = True
                payload = response.json()
                for item in payload.get("models", []) or []:
                    name = item.get("name") or item.get("model")
                    if name:
                        names.append(str(name))
            else:
                logger.debug("Ollama /api/tags 返回 HTTP %s", response.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ollama 探测失败：%s", exc)

    _cache.update({"at": now, "reachable": reachable, "names": names})
    return reachable, list(names)


def invalidate_cache() -> None:
    _cache["at"] = 0.0


async def list_models(base_url: str | None = None) -> list[dict[str, Any]]:
    """返回 /api/tags 的原始模型条目（用于展示参数量、量化等级等）。"""
    url = (base_url or settings.ollama_base_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            response = await client.get(f"{url}/api/tags")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ollama 模型列表获取失败：%s", exc)
        return []
    return list(payload.get("models", []) or [])


async def model_names(base_url: str | None = None) -> list[str]:
    _, names = await probe(base_url)
    return names


async def is_reachable(base_url: str | None = None) -> bool:
    reachable, _ = await probe(base_url)
    return reachable


def model_is_available(model: str, names: list[str]) -> bool:
    """Ollama 的标签匹配需要宽松一些：

    ``deepseek-r1:1.5b`` 可能被记录成 ``deepseek-r1:1.5b`` 或 ``deepseek-r1:latest``。
    """
    if not names:
        return False
    if model in names:
        return True
    base = model.split(":")[0]
    return any(name == base or name.startswith(f"{base}:") for name in names)


# ---------------------------------------------------------------------------
# 模型能力（是否支持原生 thinking 通道）
# ---------------------------------------------------------------------------
# ChatOllama(reasoning=True) 会在请求里带上 think=true，Ollama 于是把思维链
# 单独放在 message.thinking 里。**不支持思考的模型**（例如 llama3.2）收到这个
# 字段可能直接报错，所以要先问一下 /api/show 的 capabilities 再决定开不开。
#
# 这里是同步接口：调用点在建立 LLM 时的 _build_llm()。结果带 TTL 缓存，
# 正常只在第一次提问前多花一次本机 HTTP 往返（毫秒级）。
# ---------------------------------------------------------------------------
CAPABILITY_TTL_SECONDS = 300.0
_capability_cache: dict[str, tuple[float, bool]] = {}


def supports_thinking(model: str, base_url: str | None = None) -> bool:
    """该模型是否支持 Ollama 的原生 thinking 通道。"""
    url = (base_url or settings.ollama_base_url).rstrip("/")
    key = f"{url}|{model}"
    now = time.monotonic()

    cached = _capability_cache.get(key)
    if cached and now - cached[0] < CAPABILITY_TTL_SECONDS:
        return cached[1]

    supported = False
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT) as client:
            response = client.post(f"{url}/api/show", json={"model": model})
            if response.status_code == 200:
                capabilities = response.json().get("capabilities") or []
                supported = "thinking" in capabilities
            else:
                logger.debug("/api/show 返回 HTTP %s", response.status_code)
    except Exception as exc:  # noqa: BLE001 - 查不到就按「不支持」处理，功能降级但不出错
        logger.debug("查询模型能力失败：%s", exc)

    _capability_cache[key] = (now, supported)
    return supported


def invalidate_capability_cache() -> None:
    _capability_cache.clear()


# ---------------------------------------------------------------------------
# 模型预热
# ---------------------------------------------------------------------------
# Ollama 是**懒加载**的：服务起来了、模型也列得出来，但权重直到第一次推理
# 才载入显存。本机实测这一下要 100 秒左右（1.1GB 权重 + CUDA 初始化，
# 4GB 显存的笔记本 GPU 上还会部分回落到 CPU），之后同一模型只要 0.4 秒。
# 也就是说「启动服务」和「能提问」之间差着近两分钟。
#
# POST /api/generate 不带 prompt 时 Ollama 只加载不生成（返回 done_reason="load"），
# 正好用来在后台把模型提前载入，等用户真正提问时就已经是热的。
# ---------------------------------------------------------------------------
LLM_WARMUP_TIMEOUT = 600.0


async def warmup_model(
    model: str,
    base_url: str | None = None,
    keep_alive: str = "10m",
    timeout: float = LLM_WARMUP_TIMEOUT,
) -> bool:
    """让 Ollama 预先载入模型。成功返回 True。

    失败一律吞掉：这只是优化，不该影响服务启动。
    """
    url = (base_url or settings.ollama_base_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/api/generate", json={"model": model, "keep_alive": keep_alive}
            )
            if response.status_code == 200:
                return True
            logger.debug("模型预热返回 HTTP %s：%s", response.status_code, response.text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.debug("模型预热失败（不影响使用）：%s", exc)
    return False


async def unload_model(
    model: str,
    base_url: str | None = None,
    timeout: float = 5.0,
) -> bool:
    """让 Ollama 立刻卸载模型（``keep_alive=0``），把显存/内存还回去。

    程序退出时调用：即使 Ollama 进程本身要保留（用户可能还用它跑别的东西），
    也不该继续占着这 1GB 左右的显存。
    """
    url = (base_url or settings.ollama_base_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{url}/api/generate", json={"model": model, "keep_alive": 0}
            )
            if response.status_code == 200:
                return True
            logger.debug("模型卸载返回 HTTP %s", response.status_code)
    except Exception as exc:  # noqa: BLE001 - 退出路径上的失败不该影响收尾
        logger.debug("模型卸载失败（忽略）：%s", exc)
    return False
