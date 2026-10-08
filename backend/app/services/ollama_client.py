"""Ollama 服务探测（异步、带短超时与结果缓存）。

只用于健康检查与模型列表，不参与推理（推理走 LangChain 的 ChatOllama）。Ollama 不可达
时要等到超时才返回，而健康检查是前端启动时第一个接口，故只发一次请求、超时压到 2 秒、
结果带 TTL 缓存，避免界面卡住几秒。"""

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
    """探测 Ollama 是否可达，返回 ``(可达?, 模型名列表)``；命中缓存时不发网络请求。"""
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
    """让「可达性 + 模型列表」缓存立刻失效。"""
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
    """Ollama 标签匹配要宽松：``deepseek-r1:1.5b`` 也可能被记成 ``deepseek-r1:latest``。"""
    if not names:
        return False
    if model in names:
        return True
    base = model.split(":")[0]
    return any(name == base or name.startswith(f"{base}:") for name in names)


# ---------------------------------------------------------------------------
# 模型能力（是否支持原生 thinking 通道）
# ---------------------------------------------------------------------------
# ChatOllama(reasoning=True) 会带上 think=true，Ollama 于是把思维链单独放进
# message.thinking；**不支持思考的模型**（如 llama3.2）收到该字段可能直接报错，
# 所以先查 /api/show 的 capabilities。这里是同步接口（调用点在建立 LLM 时），结果带 TTL 缓存：
# 用户在面板里重新 pull 或把标签指向新版本后能力可能已变，故 TTL 收到 60 秒。
CAPABILITY_TTL_SECONDS = 60.0
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
    """清掉「模型是否支持原生 thinking」的缓存。

    凡是「模型集合可能已经变了」的时刻（pull、切模型版本、一键安装结束）都必须调用，
    否则最长 60 秒内仍按旧结论处理，表现为推理过程一直不显示。
    """
    _capability_cache.clear()


def invalidate_all() -> None:
    """两个缓存一起清。调用点见 routers/health.py、services/installer.py。"""
    invalidate_cache()
    invalidate_capability_cache()


# ---------------------------------------------------------------------------
# 模型预热
# ---------------------------------------------------------------------------
# Ollama 是**懒加载**的：服务起来了、模型也列得出来，但权重直到第一次推理才载入显存，
# 于是「启动服务」和「能提问」之间差着很久。POST /api/generate 不带 prompt 时只加载不生成
# （done_reason="load"），正好用来在后台提前把模型载入。
LLM_WARMUP_TIMEOUT = 600.0


async def warmup_model(
    model: str,
    base_url: str | None = None,
    keep_alive: str = "10m",
    timeout: float = LLM_WARMUP_TIMEOUT,
) -> bool:
    """让 Ollama 预先载入模型。失败一律吞掉：这只是优化，不该影响服务启动。"""
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
    """让 Ollama 立刻卸载模型（``keep_alive=0``），把显存还回去。

    程序退出时调用：Ollama 进程本身可能还要保留给别的用途，但不该继续占着显存。
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
