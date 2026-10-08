"""项目路径解析。

所有路径以项目根目录（rag-qa/）为基准，不依赖启动时的工作目录。
"""

from __future__ import annotations

import os
from pathlib import Path

# core/paths.py -> core -> app -> backend -> rag-qa
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]

BACKEND_DIR: Path = PROJECT_ROOT / "backend"
FRONTEND_DIR: Path = PROJECT_ROOT / "frontend"
SCRIPTS_DIR: Path = PROJECT_ROOT / "scripts"
DOCS_DIR: Path = PROJECT_ROOT / "docs"
TESTS_DIR: Path = PROJECT_ROOT / "tests"


def _resolve_data_dir() -> Path:
    """数据根目录，可用环境变量 ``RAG_DATA_DIR`` 覆盖。

    会清空知识库的测试必须指向临时目录，否则用户的文档与索引会被删掉。
    """
    override = os.environ.get("RAG_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return PROJECT_ROOT / "data"


DATA_DIR: Path = _resolve_data_dir()
UPLOADS_DIR: Path = DATA_DIR / "uploads"
INDEX_DIR: Path = DATA_DIR / "index"
LOGS_DIR: Path = DATA_DIR / "logs"

MODELS_DIR: Path = PROJECT_ROOT / "models"
TOOLS_DIR: Path = PROJECT_ROOT / "tools"

REGISTRY_FILE: Path = INDEX_DIR / "registry.json"
FAISS_INDEX_NAME: str = "faiss_index"


def ensure_runtime_dirs() -> None:
    """创建运行期需要的可写目录。"""
    for path in (DATA_DIR, UPLOADS_DIR, INDEX_DIR, LOGS_DIR, MODELS_DIR, TOOLS_DIR):
        path.mkdir(parents=True, exist_ok=True)
