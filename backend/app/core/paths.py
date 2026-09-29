"""项目路径解析。

所有路径都以项目根目录（rag-qa/）为基准，避免依赖启动时的工作目录，
这样无论从哪个目录启动服务，数据、模型、索引的位置都是确定的。
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

    为什么需要这个开关：测试里有几处会「清空知识库」（``clear_all``、
    ``DELETE /api/documents``），如果它们指向真实的 ``data/``，
    跑一次测试就会把用户上传的文档连索引一起删掉 —— 这是数据丢失，
    不是「测试副作用」。测试改用临时数据目录，真实知识库不受影响。
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
