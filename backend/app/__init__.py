"""离线 RAG 文档问答 —— 后端应用包。

注意：这里显式导入 config，是为了让“离线环境变量”在所有 HuggingFace
相关模块被加载之前就完成设置。
"""

from app.config import settings  # noqa: F401

__all__ = ["settings"]
__version__ = settings.version
