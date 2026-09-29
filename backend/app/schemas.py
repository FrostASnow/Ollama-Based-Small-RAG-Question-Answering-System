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
    # 是否因为阈值内一条都没命中而自动放宽了阈值
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


class ConfigResponse(BaseModel):
    llm_model: str
    llm_temperature: float
    llm_num_ctx: int
    embedding_model_name: str
    embedding_device: str
    chunk_size: int
    chunk_overlap: int
    top_k: int
    score_threshold: float
    score_window: float
    score_floor: float
    dedupe_ratio: float
    summary_max_chunks: int
    strip_boilerplate: bool
    max_upload_mb: int
    allowed_extensions: list[str]


class ConfigUpdate(BaseModel):
    llm_model: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_k: int | None = Field(default=None, ge=1, le=20)
    score_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)
    score_window: float | None = Field(default=None, ge=0.0, le=1.0)
    score_floor: float | None = Field(default=None, ge=-1.0, le=1.0)
    summary_max_chunks: int | None = Field(default=None, ge=1, le=30)
    chunk_size: int | None = Field(default=None, ge=100, le=4000)
    chunk_overlap: int | None = Field(default=None, ge=0, le=1000)
    expose_thinking: bool | None = None


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
    """首次配置体检报告。命令中的路径由后端按实际安装位置生成。"""

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
