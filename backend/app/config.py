"""全局配置。

这个模块有一个重要的副作用：在**任何** HuggingFace / transformers 相关模块
被导入之前，就把离线相关的环境变量设置好。因此 `app.config` 必须是最先被导入
的业务模块（`app/__init__.py` 已保证这一点）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from app.core.paths import MODELS_DIR, PROJECT_ROOT, ensure_runtime_dirs

# ---------------------------------------------------------------------------
# 离线引导（必须在导入 transformers / huggingface_hub 之前执行）
# ---------------------------------------------------------------------------
# HF_HUB_OFFLINE=1        —— huggingface_hub 不再发起任何网络请求
# TRANSFORMERS_OFFLINE=1  —— transformers 只从本地缓存/本地目录加载
# HF_HOME                 —— 缓存目录重定向到项目内，保证自包含
# HF_HUB_DISABLE_TELEMETRY —— 关闭遥测
# ---------------------------------------------------------------------------
_HF_HOME = PROJECT_ROOT / ".hf-cache"
os.environ.setdefault("HF_HOME", str(_HF_HOME))
os.environ.setdefault("HF_HUB_CACHE", str(_HF_HOME / "hub"))
os.environ.setdefault("HF_XET_CACHE", str(_HF_HOME / "xet"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# 让 ollama 的 python 客户端（若被间接使用）也指向本地
os.environ.setdefault("OLLAMA_HOST", "127.0.0.1:11434")

# ---------------------------------------------------------------------------
# 临时目录重定向
# ---------------------------------------------------------------------------
# 部分依赖（tokenizers、pdf 解析等）会在处理过程中写临时文件。
# 统一指到项目内的 .tmp，既避免系统临时目录不可写/被清理导致的中断，
# 也让整个程序的行为更可预测、更自包含。
# ---------------------------------------------------------------------------
_TMP_DIR = PROJECT_ROOT / ".tmp"
_TMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ["TMP"] = str(_TMP_DIR)
os.environ["TEMP"] = str(_TMP_DIR)

from pydantic import Field  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402


class Settings(BaseSettings):
    """服务配置，可通过环境变量或 rag-qa/.env 覆盖。

    环境变量前缀为 ``RAG_``，例如 ``RAG_LLM_MODEL=llama3.2``。
    """

    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------- 服务 ----------------
    app_name: str = "离线 RAG 文档问答"
    version: str = "1.0.0"
    host: str = "127.0.0.1"
    port: int = 8000
    # 服务就绪后自动打开这个地址；留空 = 不打开（默认）。
    # start.ps1 会把实际访问地址填进来（即使 -NoBrowser 也会填，方便日志显示真实地址）：
    # 只有真的能返回 200 了，浏览器才会被打开，因此不会再出现「先弹一个拒绝连接的错误页」。
    open_browser_url: str = ""
    # 是否真的调用系统浏览器（-NoBrowser 时 start.ps1 置 0，地址仍用于日志与探测）
    auto_open_browser: bool = True

    # ---------------- LLM (Ollama) ----------------
    ollama_base_url: str = "http://127.0.0.1:11434"
    # deepseek-r1:1.5b 体积小(约1.1GB)、带推理链；若追求速度可换 llama3.2(约2GB)
    llm_model: str = "deepseek-r1:1.5b"
    llm_temperature: float = 0.1
    llm_num_ctx: int = 4096
    llm_num_predict: int = 1024
    llm_timeout_s: float = 300.0
    # 是否把 deepseek-r1 的  thinking 推理内容作为独立事件推给前端
    expose_thinking: bool = True
    # 后端启动后是否在后台把 LLM 预载入显存（Ollama 懒加载，首次推理要等模型加载）
    warmup_llm: bool = True

    # ---------------- Embeddings ----------------
    # 默认：英文为主的小模型，体积小(约90MB)、384 维、速度快
    # 中文知识库建议改为： BAAI/bge-small-zh-v1.5 （512 维）
    # 多语言场景可改为：  sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    # 留空则自动推导为 models/<模型名末段>，例如 models/all-MiniLM-L6-v2
    embedding_local_dir: str = ""
    embedding_device: str = "cpu"  # cpu / cuda
    embedding_batch_size: int = 32
    # 384 = all-MiniLM-L6-v2；换成 bge-small-zh-v1.5 时需同步改为 512
    embedding_dimension: int = 384

    # ---------------- 切分 ----------------
    chunk_size: int = 800
    chunk_overlap: int = 120

    # ---------------- 检索 ----------------
    top_k: int = 4
    # 余弦相似度下限（向量已 L2 归一化，内积即余弦），低于此值不作为参考
    score_threshold: float = 0.20
    # 送入 LLM 的上下文最大字符数，防止 1.5B 小模型超出 num_ctx
    max_context_chars: int = 6000

    # ---------------- 上传 ----------------
    max_upload_mb: int = 50
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            ".txt",
            ".md",
            ".markdown",
            ".pdf",
            ".docx",
            ".csv",
            ".log",
            ".json",
        ]
    )

    # ---------------- 生成参数 ----------------
    max_history_turns: int = 6  # 送入 LLM 的历史轮数（一问一答算两轮）

    # ------------------------------------------------------------------
    @property
    def embedding_dir(self) -> Path:
        """嵌入模型的本地目录。

        显式配置了 ``embedding_local_dir`` 就用它；否则按模型名自动推导，
        这样切换 ``RAG_EMBEDDING_MODEL_NAME`` 时无需再手工指定路径。
        """
        if self.embedding_local_dir:
            return Path(self.embedding_local_dir)
        return MODELS_DIR / self.embedding_model_name.split("/")[-1]

    def embedding_source(self) -> str:
        """返回 Embeddings 的加载源。

        优先使用本地目录，保证 100% 离线；本地目录不存在时才回退到
        HuggingFace 仓库 ID（此时需要联网或在 HF 缓存中已有）。
        """
        local = self.embedding_dir
        if local.is_dir() and any(local.iterdir()):
            return str(local)
        return self.embedding_model_name

    def is_embedding_ready(self) -> bool:
        local = self.embedding_dir
        return local.is_dir() and (local / "config.json").is_file()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    ensure_runtime_dirs()
    return settings


settings = get_settings()
