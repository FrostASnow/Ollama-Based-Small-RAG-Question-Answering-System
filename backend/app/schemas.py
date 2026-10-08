"""Pydantic 数据模型（请求 / 响应）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------
class DocumentInfo(BaseModel):
    doc_id: str
    filename: str
    stored_name: str
    extension: str
    size_bytes: int
    sha256: str
    chunk_count: int
    char_count: int
    created_at: str
    status: Literal["indexed", "failed"] = "indexed"
    error: str | None = None
    #: 需要用户知道的解析警告（例如「疑似扫描件 PDF，正文没抽出来」）。
    #: 只写日志不行：界面上只会显示「索引完成」，用户不知道为什么问不出东西。
    warnings: list[str] = Field(default_factory=list)


class DocumentListResponse(BaseModel):
    documents: list[DocumentInfo]
    total: int
    total_chunks: int


class UploadResponse(BaseModel):
    document: DocumentInfo
    message: str


class DeleteResponse(BaseModel):
    doc_id: str
    deleted_chunks: int
    message: str


# ---------------------------------------------------------------------------
# 检索 / 问答
# ---------------------------------------------------------------------------
class SourceChunk(BaseModel):
    index: int  # 引用序号，从 1 开始，与回答中的 [n] 对应
    doc_id: str
    filename: str
    page: int | None = None
    chunk_index: int | None = None
    score: float
    content: str


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)


class SearchResponse(BaseModel):
    query: str
    results: list[SourceChunk]
    elapsed_ms: int
    # 检索诊断：mode / effective_threshold / relaxed / best_score / candidates …
    info: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    question: str = Field(min_length=1)
    history: list[ChatMessage] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)
    doc_ids: list[str] | None = None  # 限定只在指定文档内检索
    stream: bool = True


class ChatResponse(BaseModel):
    answer: str
    thinking: str | None = None
    sources: list[SourceChunk]
    model: str
    elapsed_ms: int
    # 检索策略：qa = 按相关度取 top-k；overview = 总结类问题，按全篇均匀取样
    mode: Literal["qa", "overview"] = "qa"
    # 是否因阈值内一条都没命中而自动放宽了阈值
    relaxed: bool = False
    best_score: float = 0.0


# ---------------------------------------------------------------------------
# 系统状态
# ---------------------------------------------------------------------------
class ModelStatus(BaseModel):
    name: str
    size_bytes: int | None = None
    family: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    app_name: str
    offline: bool
    ollama_reachable: bool
    llm_model: str
    llm_model_available: bool
    ollama_models: list[str]
    embedding_model: str
    embedding_ready: bool
    embedding_device: str
    index_ready: bool
    document_count: int
    chunk_count: int
    detail: dict[str, Any] = Field(default_factory=dict)


class ConfigValues(BaseModel):
    """`GET /api/config` 与 `PUT /api/config` 共用的字段表。

    字段表只有一个来源，避免两边漂移（PUT 收到未知键会被 ``extra="ignore"`` 静默丢弃）。
    """

    # ---------------- LLM ----------------
    llm_model: str = Field(min_length=1, description="Ollama 模型名")
    llm_temperature: float = Field(ge=0.0, le=2.0)
    llm_num_ctx: int = Field(ge=512, le=131072, description="上下文窗口（token）")
    llm_num_predict: int = Field(ge=64, le=32768, description="单次生成上限")
    llm_repeat_penalty: float = Field(ge=0.5, le=3.0)
    llm_repeat_last_n: int = Field(ge=0, le=8192)
    expose_thinking: bool = Field(description="是否把推理链以独立事件推给前端")

    # ---------------- 嵌入 ----------------
    embedding_model_name: str = Field(min_length=1)
    embedding_device: str = Field(min_length=1, description="cpu / cuda")
    embedding_dimension: int = Field(ge=1, le=8192, description="声明维度，须与模型一致")

    # ---------------- 切分与解析 ----------------
    chunk_size: int = Field(ge=100, le=8000)
    chunk_overlap: int = Field(ge=0, le=4000)
    strip_boilerplate: bool = Field(description="删除多页重复的页眉/页脚")
    restore_pdf_paragraphs: bool = Field(description="按版面补回 PDF 段落边界")
    strip_references: bool = Field(description="截掉文末参考文献列表")
    pdf_min_chars_per_page: int = Field(ge=0, le=5000, description="扫描件判定的字符/页下限")

    # ---------------- 检索 ----------------
    top_k: int = Field(ge=1, le=20)
    score_threshold: float = Field(ge=-1.0, le=1.0)
    score_window: float = Field(ge=0.0, le=1.0)
    score_floor: float = Field(ge=-1.0, le=1.0)
    score_relax_limit: float = Field(ge=0.0, le=1.0)
    dedupe_ratio: float = Field(ge=0.0, le=1.0)
    max_context_chars: int = Field(ge=500, le=100000)
    summary_max_chunks: int = Field(ge=1, le=30)

    # ---------------- 上传与生成 ----------------
    max_upload_mb: int = Field(ge=1, le=2048)
    allowed_extensions: list[str] = Field(min_length=1)
    max_history_turns: int = Field(ge=0, le=50)

    # ---------------- 索引维护 ----------------
    backup_before_reindex: bool
    index_backup_keep: int = Field(ge=0, le=50)


class ConfigResponse(ConfigValues):
    """`GET /api/config` 响应：运行期可读写的全部配置。"""


class ConfigUpdate(BaseModel):
    """`PUT /api/config` 请求：字段与 :class:`ConfigResponse` 一一对应，全部可选。

    只传要改的项；``None`` 表示「这一项不动」。
    """

    llm_model: str | None = Field(default=None, min_length=1)
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    llm_num_ctx: int | None = Field(default=None, ge=512, le=131072)
    llm_num_predict: int | None = Field(default=None, ge=64, le=32768)
    llm_repeat_penalty: float | None = Field(default=None, ge=0.5, le=3.0)
    llm_repeat_last_n: int | None = Field(default=None, ge=0, le=8192)
    expose_thinking: bool | None = None

    embedding_model_name: str | None = Field(default=None, min_length=1)
    embedding_device: str | None = Field(default=None, min_length=1)
    embedding_dimension: int | None = Field(default=None, ge=1, le=8192)

    chunk_size: int | None = Field(default=None, ge=100, le=8000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=4000)
    strip_boilerplate: bool | None = None
    restore_pdf_paragraphs: bool | None = None
    strip_references: bool | None = None
    pdf_min_chars_per_page: int | None = Field(default=None, ge=0, le=5000)

    top_k: int | None = Field(default=None, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)
    score_window: float | None = Field(default=None, ge=0.0, le=1.0)
    score_floor: float | None = Field(default=None, ge=-1.0, le=1.0)
    score_relax_limit: float | None = Field(default=None, ge=0.0, le=1.0)
    dedupe_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    max_context_chars: int | None = Field(default=None, ge=500, le=100000)
    summary_max_chunks: int | None = Field(default=None, ge=1, le=30)

    max_upload_mb: int | None = Field(default=None, ge=1, le=2048)
    allowed_extensions: list[str] | None = Field(default=None, min_length=1)
    max_history_turns: int | None = Field(default=None, ge=0, le=50)

    backup_before_reindex: bool | None = None
    index_backup_keep: int | None = Field(default=None, ge=0, le=50)


class ErrorResponse(BaseModel):
    detail: str
    code: str | None = None


# ---------------------------------------------------------------------------
# 首次配置引导
# ---------------------------------------------------------------------------
class SetupCommand(BaseModel):
    """一条可直接复制执行的命令。"""

    label: str
    shell: str = "powershell"  # powershell | cmd
    command: str
    note: str | None = None


class SetupOption(BaseModel):
    """针对某个问题的一种解决方式。"""

    id: str
    title: str
    recommended: bool = False
    description: str = ""
    commands: list[SetupCommand] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    link: str | None = None


class SetupIssue(BaseModel):
    """一个待解决的配置问题。"""

    id: str
    severity: Literal["blocking", "warning"]
    title: str
    detail: str
    impact: str = ""
    options: list[SetupOption] = Field(default_factory=list)


class SetupReport(BaseModel):
    """首次配置体检报告；命令中的路径由后端按实际安装位置生成。"""

    ready: bool
    blocking_count: int
    warning_count: int
    headline: str
    issues: list[SetupIssue] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
    checked_at: str


class InstallRequest(BaseModel):
    """一键安装请求。"""

    mirror: bool = False
    skip_ollama: bool = False
