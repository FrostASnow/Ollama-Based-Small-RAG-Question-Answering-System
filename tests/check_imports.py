"""导入体检：确认所有关键依赖都能按预期路径导入。

    .venv\\Scripts\\python.exe tests\\check_imports.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 保证能 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

OK = "  [OK]  "
FAIL = "  [FAIL]"

results: list[tuple[str, bool, str]] = []


def check(label: str, importer) -> None:  # noqa: ANN001
    try:
        detail = importer()
        results.append((label, True, detail or ""))
    except Exception as exc:  # noqa: BLE001
        results.append((label, False, f"{type(exc).__name__}: {exc}"))


def _mod(name: str, attr: str | None = None):  # noqa: ANN202
    def run() -> str:
        module = __import__(name, fromlist=["__name__"])
        if attr:
            getattr(module, attr)
        version = getattr(module, "__version__", "?")
        return f"{name}{'.' + attr if attr else ''} (v{version})"

    return run


def _splitter_via_loader() -> str:
    """走 loader 的延迟导入路径构造切分器（顶层导入 sentence_transformers 要 9.5 秒）。"""
    from app.services.loader import build_splitter

    splitter = build_splitter()
    chunks = splitter.split_text("第一段内容。" * 200)
    return f"{type(splitter).__name__} -> {len(chunks)} chunks"


def _audit_all_exports() -> str:
    """遍历 app 包，检查每个模块 `__all__` 里列出的名字是否真的存在。"""
    import importlib
    import pkgutil

    import app

    problems: list[str] = []
    checked = 0

    for module_info in pkgutil.walk_packages(app.__path__, prefix="app."):
        name = module_info.name
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name} 导入失败（{type(exc).__name__}: {exc}）")
            continue

        for exported in getattr(module, "__all__", []) or []:
            checked += 1
            if not hasattr(module, exported):
                problems.append(f"{name}.__all__ 里的 '{exported}' 并不存在")

    if problems:
        raise AssertionError("；".join(problems))
    return f"扫描全部子模块，{checked} 个导出名都存在"


def main() -> int:
    print("=" * 70)
    print("依赖导入体检")
    print("=" * 70)
    print(f"Python: {sys.version.split()[0]}  ({sys.executable})")
    print("-" * 70)

    check("fastapi", _mod("fastapi", "FastAPI"))
    check("uvicorn", _mod("uvicorn"))
    check("pydantic", _mod("pydantic", "BaseModel"))
    check("pydantic-settings", _mod("pydantic_settings", "BaseSettings"))
    check("httpx", _mod("httpx"))
    check("python-multipart", _mod("multipart"))
    check("faiss", _mod("faiss"))
    check("numpy", _mod("numpy"))
    check("torch", _mod("torch"))
    check("transformers", _mod("transformers"))
    check("sentence-transformers", _mod("sentence_transformers", "SentenceTransformer"))
    check("huggingface-hub", _mod("huggingface_hub"))
    check("langchain-core", _mod("langchain_core"))
    check("langchain-huggingface", _mod("langchain_huggingface", "HuggingFaceEmbeddings"))
    check("langchain-ollama", _mod("langchain_ollama", "ChatOllama"))
    check("ollama", _mod("ollama"))
    check(
        "langchain-text-splitters",
        _mod("langchain_text_splitters", "RecursiveCharacterTextSplitter"),
    )
    check("langchain-community: FAISS", _mod("langchain_community.vectorstores", "FAISS"))
    check(
        "langchain-community: DistanceStrategy",
        _mod("langchain_community.vectorstores.utils", "DistanceStrategy"),
    )
    check("langchain-community: PyPDFLoader", _mod("langchain_community.document_loaders", "PyPDFLoader"))
    check("langchain-community: Docx2txtLoader", _mod("langchain_community.document_loaders", "Docx2txtLoader"))
    check("pypdf", _mod("pypdf"))
    check("docx2txt", _mod("docx2txt"))
    check("app.services.loader: build_splitter（延迟导入）", _splitter_via_loader)
    check("全仓库 __all__ 导出名都存在", _audit_all_exports)

    print()
    for label, ok, detail in results:
        prefix = OK if ok else FAIL
        print(f"{prefix} {label:<44} {detail}")

    print()
    failed = [r for r in results if not r[1]]
    print("-" * 70)
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    if failed:
        print("\n以下依赖导入失败，需要调整版本或安装：")
        for label, _, detail in failed:
            print(f"  - {label}: {detail}")
        return 1
    print("全部通过，可以启动服务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
