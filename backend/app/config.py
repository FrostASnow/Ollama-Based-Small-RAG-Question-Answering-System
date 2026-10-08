"""全局配置。

离线环境变量必须在任何 HuggingFace / transformers 模块之前设置，
因此本模块必须最先被导入（`app/__init__.py` 已保证）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from app.core.paths import MODELS_DIR, PROJECT_ROOT, ensure_runtime_dirs

# ---------------------------------------------------------------------------
# 离线引导（必须在导入 transformers / huggingface_hub 之前执行）
# ---------------------------------------------------------------------------
# 缓存目录重定向到项目内、遥测与联网全部关闭，保证自包含且 100% 离线。
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
# 依赖会写临时文件；指到项目内的 .tmp，避免系统临时目录不可写/被清理导致中断。
# ---------------------------------------------------------------------------
_TMP_DIR = PROJECT_ROOT / ".tmp"
_TMP_DIR.mkdir(parents=True, exist_ok=True)
os.environ["TMP"] = str(_TMP_DIR)
os.environ["TEMP"] = str(_TMP_DIR)

from pydantic import Field  # noqa: E402
from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402


class Settings(BaseSettings):
    """服务配置，可用 ``RAG_*`` 环境变量（如 ``RAG_LLM_MODEL``）或 rag-qa/.env 覆盖。"""

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
    # 服务就绪（真的能返回 200）后才自动打开这个地址；留空 = 不打开（默认）。
    # start.ps1 会填入实际访问地址，即使 -NoBrowser 也会填（便于日志显示真实地址）。
    open_browser_url: str = ""
    # 是否真的调用系统浏览器（-NoBrowser 时 start.ps1 置 0，地址仍用于日志与探测）
    auto_open_browser: bool = True

    # ---------------- LLM (Ollama) ----------------
    ollama_base_url: str = "http://127.0.0.1:11434"
    # deepseek-r1:1.5b 带推理链；追求速度可换 llama3.2
    llm_model: str = "deepseek-r1:1.5b"
    llm_temperature: float = 0.1
    # 上下文窗口（token）。中文约 1 字 ≈ 1 token，窗口不足时 Ollama 会静默截断
    # 提示词，表现为模型丢掉指令甚至返回空回答。
    llm_num_ctx: int = 8192
    # 单次生成上限（含推理链）；与 llm_num_ctx 需协调，否则回答会被截断。
    llm_num_predict: int = 2048
    # 重复惩罚：小模型易陷入重复输出的退化，适当提高是最省事的缓解。
    llm_repeat_penalty: float = 1.2
    llm_repeat_last_n: int = 512
    llm_timeout_s: float = 300.0
    # 是否把 deepseek-r1 的  thinking 推理内容作为独立事件推给前端
    expose_thinking: bool = True
    # 是否在后台预载入 LLM 显存（Ollama 懒加载，首次推理要等模型加载）
    warmup_llm: bool = True

    # ---------------- Embeddings ----------------
    # 英文小模型（384 维）。中文建议 BAAI/bge-small-zh-v1.5（512 维），
    # 多语言可用 sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2。
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    # 留空则自动推导为 models/<模型名末段>
    embedding_local_dir: str = ""
    embedding_device: str = "cpu"  # cpu / cuda
    embedding_batch_size: int = 32
    # 384 = all-MiniLM-L6-v2；换成 bge-small-zh-v1.5 时需同步改为 512
    embedding_dimension: int = 384

    # ---------------- 切分 ----------------
    chunk_size: int = 800
    chunk_overlap: int = 120
    # 解析 PDF 时删除多页重复的短行（页眉/页脚）；它们对任何提问相似度都不低，
    # 会挤掉正文的召回槽位。
    strip_boilerplate: bool = True
    # 按版面补回段落边界：PDF 抽出来是硬换行，不补的话切分器只能按字符数硬切，
    # 中文正文会和英文摘要粘在一起。
    restore_pdf_paragraphs: bool = True
    # 截掉文末「参考文献」：关键词密集、语义贫乏，对任何提问相似度都高，
    # 会挤掉正文槽位。
    strip_references: bool = True
    # 平均每页抽取字符数低于此值即判定「疑似扫描件/图片版」；警告必须一路带到
    # 上传响应与文档列表，只写日志用户看不到。
    pdf_min_chars_per_page: int = 120

    # ---------------- 检索 ----------------
    top_k: int = 4
    # 余弦相似度下限（向量已 L2 归一化，内积即余弦），低于此值不作为参考
    score_threshold: float = 0.20
    # 相对窗口：只保留与最佳片段相差不超过该值的候选；0 = 关闭。
    # 小模型的分数分布扁平，单一绝对阈值分不清「都相关」和「一片噪声」。
    score_window: float = 0.12
    # 兜底下限：绝对阈值一条都没命中时，最多放宽到这个分数（0.0 = 不限）
    score_floor: float = 0.10
    # 只在此阈值以内才允许自动放宽：用户把阈值调得更严格时说明他要高精度，
    # 此时不擅自放宽，直接回答「没找到」。
    score_relax_limit: float = 0.35
    # 近重复片段（同一段页眉被切进多个 chunk）只保留分数最高的一个
    dedupe_ratio: float = 0.80
    # 送入 LLM 的上下文最大字符数，防止 1.5B 小模型超出 num_ctx
    max_context_chars: int = 6000
    # ---------------- 概览（总结类问题）----------------
    # 总结类问题问的是**整篇**，与任何单个片段都不相似，阈值筛必然漏；
    # 此时按全篇均匀取样，给模型覆盖全文的片段。
    summary_max_chunks: int = 10

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

    # ---------------- 索引维护 ----------------
    # 重建是「先删后建」：中途失败会让该文档的向量丢失，备份至少可人工还原。
    backup_before_reindex: bool = True
    # 备份保留份数（data/index/backups/ 下按时间排序，超出删最旧）。
    index_backup_keep: int = 3

    # ------------------------------------------------------------------
    @property
    def embedding_dir(self) -> Path:
        """嵌入模型本地目录：配了 ``embedding_local_dir`` 就用它，否则按模型名推导。"""
        if self.embedding_local_dir:
            return Path(self.embedding_local_dir)
        return MODELS_DIR / self.embedding_model_name.split("/")[-1]

    def embedding_source(self) -> str:
        """返回 Embeddings 加载源：本地目录优先（保证离线），缺失时回退 HF 仓库 ID。"""
        local = self.embedding_dir
        if local.is_dir() and any(local.iterdir()):
            return str(local)
        return self.embedding_model_name

    def is_embedding_ready(self) -> bool:
        local = self.embedding_dir
        return local.is_dir() and (local / "config.json").is_file()

    # ------------------------------------------------------------------
    @property
    def context_char_budget(self) -> int:
        """实际可用的上下文预算（字符），由 ``num_ctx`` 反推。

        ``max_context_chars`` 与 ``num_ctx`` 矛盾时 Ollama 会静默截断提示词；
        中文按 1 字 ≈ 1 token 估算，再扣掉生成预留与系统提示词开销。
        """
        reserve = min(1600, max(768, int(self.llm_num_ctx * 0.35)))
        derived = self.llm_num_ctx - reserve - 400
        return max(800, min(self.max_context_chars, derived))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    ensure_runtime_dirs()
    return settings


settings = get_settings()
