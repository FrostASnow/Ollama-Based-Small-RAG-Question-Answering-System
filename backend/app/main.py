"""FastAPI 应用入口。

启动：``python -m uvicorn app.main:app --host 127.0.0.1 --port 8000``，或 ``python -m app.main``。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
# 直接执行 python backend/app/main.py 时也能 import app.*
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.core import browser  # noqa: E402
from app.core.logging import get_logger, setup_logging  # noqa: E402
from app.core.paths import FRONTEND_DIR, ensure_runtime_dirs  # noqa: E402
from app.routers import chat, documents, health  # noqa: E402
from app.services import loader, ollama_client  # noqa: E402
from app.services.embeddings import warmup  # noqa: E402
from app.services.index_health import health_problems, summarize  # noqa: E402
from app.services.registry import registry  # noqa: E402
from app.services.vectorstore import vector_store  # noqa: E402

setup_logging()
logger = get_logger("app.main")


class UTF8JSONResponse(JSONResponse):
    """显式声明 ``charset=utf-8`` 的 JSON 响应：Starlette 默认只给 ``text/*`` 追加 charset。

    不带 charset 时部分客户端（如 Windows PowerShell 5.1 的 ``Invoke-RestMethod``）
    会按 ISO-8859-1 解码，中文变成乱码。
    """

    media_type = "application/json; charset=utf-8"


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_runtime_dirs()

    logger.info("=" * 68)
    logger.info("%s v%s", settings.app_name, settings.version)
    logger.info("离线模式：HF_HUB_OFFLINE=%s", __import__("os").environ.get("HF_HUB_OFFLINE"))
    logger.info("LLM 模型：%s @ %s", settings.llm_model, settings.ollama_base_url)
    logger.info("嵌入模型：%s", settings.embedding_source())
    # 启动脚本填入的 open_browser_url（含 -Port 指定的端口）比 settings.host/port 更准，
    # 两者都没有时才回退到配置值。
    logger.info(
        "访问地址：%s",
        settings.open_browser_url or f"http://{settings.host}:{settings.port}",
    )
    logger.info("=" * 68)

    async def background_init() -> None:
        """预热嵌入模型并恢复索引。

        放后台执行：加载 sentence-transformers 会连带导入 torch，冷启动要十几到几十秒，
        同步等待会让端口迟迟不监听，用户看到的是「无法连接」而不是「正在初始化」。
        """
        try:
            warmed = await asyncio.to_thread(warmup)
            if warmed:
                # 此处才导入切分器依赖：它会连带拉起整个 sentence_transformers/torch，
                # 放模块顶层会明显拖慢冷启动。
                await asyncio.to_thread(loader.preload)
            await asyncio.to_thread(vector_store.load)
            stats = registry.stats()
            logger.info(
                "知识库就绪：%d 个文档 / %d 个分块（向量 %d 条）",
                stats["document_count"], stats["chunk_count"], vector_store.size,
            )

            # 索引是离线产物：换过嵌入模型 / 改过切分参数后它会与当前配置分叉，
            # 启动时就说清楚，别等用户提问时才炸在半路。
            report = await asyncio.to_thread(vector_store.compatibility)
            for problem in health_problems(report):
                logger.warning("%s", problem)
            logger.info("索引一致性：%s", summarize(report))
        except Exception as exc:  # noqa: BLE001
            logger.error("后台初始化失败：%s", exc)

    init_task = asyncio.create_task(background_init())

    async def warmup_llm() -> None:
        """后台把 LLM 预载入显存。

        Ollama 懒加载：权重直到第一次推理才载入，不预热的话用户第一条提问会长时间无响应。
        """
        try:
            reachable, names = await ollama_client.probe()
            if not (reachable and ollama_client.model_is_available(settings.llm_model, names)):
                logger.info("跳过 LLM 预热：Ollama 不可达或模型未安装")
                return
            started = time.perf_counter()
            ok = await ollama_client.warmup_model(settings.llm_model)
            logger.info(
                "LLM 预热%s：%s（%.1fs）",
                "" if ok else "未成功",
                settings.llm_model,
                time.perf_counter() - started,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 预热异常（不影响使用）：%s", exc)

    llm_task = asyncio.create_task(warmup_llm()) if settings.warmup_llm else None

    # 浏览器只在「能真的返回 200」之后才打开，避免用户先看到 ERR_CONNECTION_REFUSED
    browser_task = browser.schedule_auto_open(
        settings.open_browser_url, settings.auto_open_browser
    )

    yield

    # 先撤销「等待就绪开浏览器」的任务，再收尾持久化
    for task in (browser_task, llm_task):
        if task is None or task.done():
            continue
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    logger.info("正在停止服务，持久化索引 ...")

    # 退出时把模型从显存里卸掉：启动脚本通常会连 Ollama 进程一起收掉，
    # 但 -KeepOllama 保留进程时，这一步能保证它不继续占着显存。
    if settings.warmup_llm:
        try:
            if await ollama_client.unload_model(settings.llm_model):
                logger.info("已让 Ollama 卸载模型：%s", settings.llm_model)
        except Exception as exc:  # noqa: BLE001
            logger.debug("卸载模型失败（忽略）：%s", exc)

    if not init_task.done():
        init_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await init_task
    try:
        vector_store.persist()
    except Exception as exc:  # noqa: BLE001
        logger.warning("索引持久化失败：%s", exc)
    logger.info("已停止")


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="基于 LangChain + Ollama + FAISS + HuggingFace Embeddings 的离线文档问答服务",
    lifespan=lifespan,
    default_response_class=UTF8JSONResponse,
)

# 本地单机部署：允许本机前端与调试工具跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(documents.router)
app.include_router(chat.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常 %s %s", request.method, request.url.path)
    return UTF8JSONResponse(
        status_code=500,
        content={"detail": f"服务器内部错误：{exc}", "code": "internal_error"},
    )


# FastAPI 内置的异常处理器绕过 default_response_class 直接构造 JSONResponse，
# 而我们的 404/409/503 文案带中文，所以显式覆盖成 UTF-8 版本。
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> UTF8JSONResponse:
    return UTF8JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> UTF8JSONResponse:
    return UTF8JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


# 静态前端必须最后挂载，否则会抢占 /api 路由
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:  # pragma: no cover
    logger.warning("未找到前端目录：%s", FRONTEND_DIR)


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
