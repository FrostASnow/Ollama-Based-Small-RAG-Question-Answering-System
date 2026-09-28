"""项目路径解析。

所有路径都以项目根目录（rag-qa/）为基准，避免依赖启动时的工作目录，
这样无论从哪个目录启动服务，数据、模型、索引的位置都是确定的。
"""

from __future__ import annotations

from pathlib import Path

# core/paths.py -> core -> app -> backend -> rag-qa
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]

BACKEND_DIR: Path = PROJECT_ROOT / "backend"
FRONTEND_DIR: Path = PROJECT_ROOT / "frontend"
SCRIPTS_DIR: Path = PROJECT_ROOT / "scripts"
DOCS_DIR: Path = PROJECT_ROOT / "docs"
TESTS_DIR: Path = PROJECT_ROOT / "tests"

DATA_DIR: Path = PROJECT_ROOT / "data"
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
