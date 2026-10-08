"""离线 RAG 文档问答 —— 后端应用包。

显式导入 config，是为了让离线环境变量早于所有 HuggingFace 模块完成设置。
"""

from app.config import settings  # noqa: F401

__all__ = ["settings"]
__version__ = settings.version
