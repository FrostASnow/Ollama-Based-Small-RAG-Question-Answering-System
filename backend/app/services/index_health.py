"""索引一致性体检：磁盘上那份离线索引，和当前配置还对得上吗？
向量空间变了 → ``compatible=False``（必须重建）；切分 / 解析参数变了 → ``stale=True``（内容过期）。
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.core.logging import get_logger
from app.services.registry import registry

logger = get_logger(__name__)

#: 与**怎么切**有关的配置：变了 → 索引还能用，但内容已过期。
CHUNK_FIELDS = ("chunk_size", "chunk_overlap")
#: 与**解析**有关的开关（页眉过滤、段落还原、参考文献截断）。
PARSE_FIELDS = ("strip_boilerplate", "restore_pdf_paragraphs", "strip_references")

_DRIFT_KEYS = CHUNK_FIELDS


def current_signature() -> dict[str, Any]:
    """当前配置里「决定索引内容」的那部分快照。"""
    return {
        "embedding_model": settings.embedding_model_name,
        "dimension": settings.embedding_dimension,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "parse_options": {name: bool(getattr(settings, name)) for name in PARSE_FIELDS},
    }


def check(
    *,
    has_index: bool,
    index_dimension: int | None = None,
    model_dimension: int | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """比对「索引登记的参数」与「当前配置」。维度参数为 None 表示模型还没载入，此时不下硬结论。"""
    index_meta = dict(meta if meta is not None else registry.get_index_meta())
    current = current_signature()

    result: dict[str, Any] = {
        # empty（还没有索引） / ok / stale / incompatible
        "state": "empty",
        "compatible": True,
        "stale": False,
        "reasons": [],
        "notices": [],
        "dimension": {
            "index": index_dimension,
            "model": model_dimension,
            "recorded": index_meta.get("dimension"),
            "declared": current["dimension"],
        },
        "index_meta": index_meta,
        "current": current,
    }

    # 知识库为空：没有任何东西可比，直接放行（此时若还留着旧元信息，说明
    # registry.clear() 漏清了 —— 那由 registry 的测试去挡）。
    if not has_index:
        return result

    reasons: list[str] = result["reasons"]
    notices: list[str] = result["notices"]
    stale_flags: list[bool] = []

    def notice(text: str, *, drift: bool = False) -> None:
        """记一条「不阻断但要提醒」的信息；drift=True 表示索引内容已过期。"""
        notices.append(text)
        stale_flags.append(drift)

    # ---------------- 向量空间 ----------------
    recorded_model = index_meta.get("embedding_model")
    current_model = current["embedding_model"]
    if recorded_model and current_model and recorded_model != current_model:
        reasons.append(
            f"索引由嵌入模型 {recorded_model} 建立，当前配置为 {current_model}；"
            "不同模型的向量空间不通用，检索结果必然错乱。"
        )
        stale_flags.append(True)

    if (
        index_dimension is not None
        and model_dimension is not None
        and index_dimension != model_dimension
    ):
        reasons.append(
            f"索引向量维度为 {index_dimension}，当前嵌入模型输出 {model_dimension} 维；"
            "两者不一致时检索会直接报错。"
        )
        stale_flags.append(True)
    elif index_dimension is not None and model_dimension is None:
        # 模型还没载入，只能拿登记值做参考，避免误报阻断
        recorded_dim = index_meta.get("dimension")
        if recorded_dim is not None and recorded_dim != index_dimension:
            notice(
                f"索引实际维度为 {index_dimension}，注册表登记的是 {recorded_dim}"
                "（登记值已过期，重建索引后会修正）。"
            )

    declared = current["dimension"]
    if model_dimension is not None and declared != model_dimension:
        notice(
            f"配置 RAG_EMBEDDING_DIMENSION={declared}，而模型实际输出 {model_dimension} 维。"
            "程序按实际维度工作，但建议修正该配置以免误导。"
        )

    # ---------------- 切分参数 ----------------
    drifted: list[str] = []
    for key in _DRIFT_KEYS:
        recorded_value = index_meta.get(key)
        if recorded_value is None:
            continue  # 旧版本建立的索引没有登记，无从判断
        if recorded_value != current[key]:
            drifted.append(f"{key}（索引 {recorded_value} → 当前 {current[key]}）")
    if drifted:
        notice(
            "索引是用旧的切分参数建立的：" + "、".join(drifted) + "；"
            "已有分块不会自动重新切分。",
            drift=True,
        )

    # ---------------- 解析开关 ----------------
    recorded_parse = index_meta.get("parse_options") or {}
    if isinstance(recorded_parse, dict) and recorded_parse:
        changed = [
            name
            for name, value in current["parse_options"].items()
            if name in recorded_parse and bool(recorded_parse[name]) != value
        ]
        if changed:
            notice(
                "解析开关与建立索引时不一致：" + "、".join(changed) + "；"
                "页眉过滤 / 段落还原这类改动要重建索引才会作用到已有文档上。",
                drift=True,
            )

    result["compatible"] = not reasons
    result["stale"] = any(stale_flags)
    if not result["compatible"]:
        result["state"] = "incompatible"
    elif result["stale"]:
        result["state"] = "stale"
    else:
        result["state"] = "ok"
    return result


def health_problems(result: dict[str, Any]) -> list[str]:
    """把体检结果翻译成 /api/health 的 problems 文案，必须让用户看到该做什么。"""
    problems: list[str] = []
    if result.get("state") == "incompatible":
        problems.append(
            "索引与当前嵌入模型不一致（"
            + "；".join(result.get("reasons") or [])
            + "）请在左侧点击「重建索引」，用当前模型重新生成向量。"
        )
    elif result.get("state") == "stale":
        problems.append(
            "索引内容与当前配置不一致（"
            + "；".join(result.get("notices") or [])
            + "）建议点击「重建索引」让已有文档按新配置重新分块。"
        )
    return problems


def summarize(result: dict[str, Any]) -> str:
    """一行日志用的摘要。"""
    state = result.get("state")
    if state == "empty":
        return "尚无索引"
    if state == "ok":
        return "索引与当前配置一致"
    detail = "；".join(result.get("reasons") or result.get("notices") or [])
    return f"{state}：{detail}"
