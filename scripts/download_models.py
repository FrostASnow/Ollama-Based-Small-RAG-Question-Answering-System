"""下载 HuggingFace 嵌入模型到本地 models/ 目录，供完全离线加载。

这是**唯一需要联网**的步骤之一（另一个是 ollama pull）。
下载完成后，把整个 rag-qa 目录拷到离线机器即可直接运行。

用法：
    :: 默认模型 all-MiniLM-L6-v2
    .venv\\Scripts\\python.exe scripts\\download_models.py

    :: 国内镜像加速
    .venv\\Scripts\\python.exe scripts\\download_models.py --mirror

    :: 换用中文模型
    .venv\\Scripts\\python.exe scripts\\download_models.py --model BAAI/bge-small-zh-v1.5

    :: 只校验已有文件，不联网
    .venv\\Scripts\\python.exe scripts\\download_models.py --verify
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# HF 缓存/日志重定向到项目内：默认位置 %USERPROFILE%\.cache\huggingface 在受限环境
# 可能不可写，放在项目里也让整个程序便于整体拷贝到离线机器。
_HF_HOME = PROJECT_ROOT / ".hf-cache"
os.environ.setdefault("HF_HOME", str(_HF_HOME))
os.environ.setdefault("HF_HUB_CACHE", str(_HF_HOME / "hub"))
os.environ.setdefault("HF_XET_CACHE", str(_HF_HOME / "xet"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

DEFAULT_REPO = "sentence-transformers/all-MiniLM-L6-v2"

# 只取推理真正需要的文件，跳过 onnx / openvino / tensorflow 等冗余格式
ALLOW_PATTERNS = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "sentence_bert_config.json",
    "model.safetensors",
    "pytorch_model.bin",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "1_Pooling/*",
]

WEIGHT_CANDIDATES = ["model.safetensors", "pytorch_model.bin"]
TOKENIZER_CANDIDATES = ["tokenizer.json", "vocab.txt", "sentencepiece.bpe.model"]


def human(size: float) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    return f"{value:.1f} {units[unit]}"


def directory_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def verify(target: Path) -> tuple[bool, list[str], list[str]]:
    """校验模型目录。返回 (是否通过, 错误列表, 警告列表)。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not target.is_dir():
        return False, [f"目录不存在：{target}"], []

    if not (target / "config.json").is_file():
        errors.append("缺少 config.json")

    if not any((target / name).is_file() for name in WEIGHT_CANDIDATES):
        errors.append("缺少模型权重（model.safetensors 或 pytorch_model.bin）")

    if not any((target / name).is_file() for name in TOKENIZER_CANDIDATES):
        errors.append("缺少分词器文件（tokenizer.json / vocab.txt / sentencepiece.bpe.model）")

    if not (target / "1_Pooling" / "config.json").is_file():
        warnings.append("缺少 1_Pooling/config.json（句向量池化配置），sentence-transformers 模型通常需要它")
    if not (target / "modules.json").is_file():
        warnings.append("缺少 modules.json")

    return (not errors), errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 HuggingFace 嵌入模型到本地")
    parser.add_argument("--model", default=DEFAULT_REPO, help=f"模型仓库 ID（默认 {DEFAULT_REPO}）")
    parser.add_argument("--mirror", action="store_true", help="使用 hf-mirror.com 镜像（国内加速）")
    parser.add_argument("--verify", action="store_true", help="只校验本地文件，不下载")
    parser.add_argument("--target", default=None, help="目标目录（默认 models/<模型名>）")
    args = parser.parse_args()

    target = Path(args.target) if args.target else PROJECT_ROOT / "models" / args.model.split("/")[-1]

    print("=" * 70)
    print("嵌入模型本地化")
    print("=" * 70)
    print(f"仓库    : {args.model}")
    print(f"目标目录: {target}")

    if args.verify:
        ok, errors, warnings = verify(target)
        for warning in warnings:
            print(f"[warn] {warning}")
        if ok:
            print(f"\n[OK] 模型完整，占用 {human(directory_size(target))}")
            return 0
        print("\n[FAIL] 校验未通过：")
        for error in errors:
            print(f"  - {error}")
        return 1

    # 下载阶段必须关掉离线开关，否则 huggingface_hub 会直接拒绝联网
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["TRANSFORMERS_OFFLINE"] = "0"

    if args.mirror:
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        print("镜像    : https://hf-mirror.com")

    target.mkdir(parents=True, exist_ok=True)

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("\n[FAIL] 未安装 huggingface_hub，请先执行 scripts\\prepare.ps1")
        return 1

    print("\n开始下载 ...")
    try:
        snapshot_download(
            repo_id=args.model,
            local_dir=str(target),
            allow_patterns=ALLOW_PATTERNS,
            max_workers=4,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n[FAIL] 下载失败：{type(exc).__name__}: {exc}")
        print("\n可能的解决办法：")
        print("  1) 加 --mirror 使用国内镜像重试")
        print("  2) 检查网络 / 代理设置")
        return 1

    ok, errors, warnings = verify(target)
    print()
    for warning in warnings:
        print(f"[warn] {warning}")

    if ok:
        print(f"[OK] 下载完成并校验通过，占用 {human(directory_size(target))}")
        print(f"     路径：{target}")
        print()
        print("     现在可以在完全离线的环境下运行本程序。")
        print("     若更换了模型，请同步调整 .env：")
        print(f"       RAG_EMBEDDING_MODEL_NAME={args.model}")
        print("     并重建索引（不同模型的向量空间不通用）。")
        return 0

    print("[FAIL] 下载结束但校验未通过：")
    for error in errors:
        print(f"  - {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
