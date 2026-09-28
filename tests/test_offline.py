"""离线核心链路自测：Embeddings 本地加载 + FAISS 检索 + 文档切分。

不依赖 Ollama，因此在只准备好嵌入模型时就能跑，
可用来快速定位「检索侧」的问题。

    .venv\\Scripts\\python.exe tests\\test_offline.py

注意：本测试会如实报告 all-MiniLM-L6-v2 在中文语料上的检索命中率。
该模型以英文为主训练，中文语义匹配能力有限（见文末结论），
这是选型时就必须知道的取舍，不建议靠调阈值掩盖。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

# 临时目录放到项目内，避免系统临时目录不可写
TMP_ROOT = PROJECT_ROOT / ".tmp" / "tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(TMP_ROOT)

from app.config import settings  # noqa: E402
from app.services.embeddings import get_embeddings  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))


def note(text: str) -> None:
    print(f"  [NOTE] {text}")


def section(title: str) -> None:
    print()
    print("-" * 70)
    print(title)
    print("-" * 70)


# 用于检索验证的小语料：主题差异明显，便于判断排序是否正确
CORPUS = [
    # d0 差旅报销
    "公司差旅报销标准为：市内交通每日上限 80 元，住宿费一线城市每晚 600 元，"
    "其他城市每晚 400 元。超标部分需部门负责人书面审批。",
    # d1 年假
    "年假制度：入职满一年享有 5 天带薪年假，满三年 10 天，满五年 15 天。"
    "年假需提前三个工作日申请。",
    # d2 退货
    "产品退货政策：自签收之日起 7 天内无理由退货，15 天内可换货。"
    "定制类商品不支持无理由退货。",
    # d3 备份
    "服务器运维：生产环境数据库每日凌晨 2 点全量备份，保留 30 天。"
    "备份文件存放于异地机房。",
]

# (问题, 期望命中的语料下标)
QUERIES = [
    ("出差住宿一晚能报多少钱？", 0),
    ("我想退货，几天之内可以？", 2),
    ("数据库备份保留多久？", 3),
    ("年假有多少天？", 1),
]

# 逐字短语查询：只检验检索管道是否正确，不受模型语义泛化能力影响
EXACT_QUERIES = [
    ("住宿费一线城市每晚 600 元", 0),
    ("自签收之日起 7 天内无理由退货", 2),
    ("每日凌晨 2 点全量备份", 3),
    ("入职满一年享有 5 天带薪年假", 1),
]


def main() -> int:
    print("=" * 70)
    print("离线核心链路自测")
    print("=" * 70)
    print(f"嵌入模型目录: {settings.embedding_dir}")
    print(f"HF_HUB_OFFLINE={__import__('os').environ.get('HF_HUB_OFFLINE')}")

    # ------------------------------------------------------------------
    section("1. 本地模型完整性")
    required = [
        "config.json",
        "modules.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "vocab.txt",
        "1_Pooling/config.json",
    ]
    for name in required:
        check(f"存在 {name}", (settings.embedding_dir / name).is_file())
    check(
        "存在模型权重",
        (settings.embedding_dir / "model.safetensors").is_file()
        or (settings.embedding_dir / "pytorch_model.bin").is_file(),
    )
    check("is_embedding_ready()", settings.is_embedding_ready())

    # ------------------------------------------------------------------
    section("2. 离线加载 Embeddings")
    try:
        embeddings = get_embeddings()
        check("加载成功", True)
    except Exception as exc:  # noqa: BLE001
        check("加载成功", False, f"{type(exc).__name__}: {exc}")
        print("\n嵌入模型无法加载，后续测试跳过。")
        return 1

    vector = embeddings.embed_query("测试文本")
    check("向量维度 = 384", len(vector) == settings.embedding_dimension, f"实际 {len(vector)}")

    norm = sum(float(v) * float(v) for v in vector) ** 0.5
    check("向量已归一化 (‖v‖≈1)", abs(norm - 1.0) < 1e-3, f"‖v‖={norm:.6f}")

    batch = embeddings.embed_documents(list(CORPUS))
    check("批量嵌入", len(batch) == len(CORPUS), f"{len(batch)} 条")

    # ------------------------------------------------------------------
    section("3. FAISS 建库与检索（内积 == 余弦）")
    from langchain_community.vectorstores import FAISS
    from langchain_community.vectorstores.utils import DistanceStrategy
    from langchain_core.documents import Document

    docs = [
        Document(
            page_content=text,
            metadata={"doc_id": f"d{i}", "filename": f"doc{i}.txt", "chunk_index": 0, "page": None},
        )
        for i, text in enumerate(CORPUS)
    ]

    store = FAISS.from_documents(
        documents=docs,
        embedding=embeddings,
        distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT,
    )
    check("索引向量数 = 4", store.index.ntotal == 4, f"ntotal={store.index.ntotal}")

    # 同一段文本检索自身，余弦应接近 1
    self_pairs = store.similarity_search_with_score_by_vector(
        embeddings.embed_query(CORPUS[0]), k=1
    )
    self_score = float(self_pairs[0][1])
    check("自检索余弦 ≈ 1.0", self_score > 0.98, f"score={self_score:.4f}")

    # ------------------------------------------------------------------
    # 3a. 管道正确性：逐字查询必须命中原文所在文档
    #     这一组检验的是「切分 + 向量化 + FAISS + 排序」整条管道，
    #     与模型的语义泛化能力无关，因此必须全中。
    # ------------------------------------------------------------------
    print()
    print("      [管道正确性] 逐字查询（不依赖模型语义泛化能力）")
    exact_hits = 0
    for phrase, expected in EXACT_QUERIES:
        pairs = store.similarity_search_with_score_by_vector(
            embeddings.embed_query(phrase), k=len(CORPUS)
        )
        top_id = pairs[0][0].metadata["doc_id"]
        hit = top_id == f"d{expected}"
        exact_hits += int(hit)
        print(f"        [{'OK  ' if hit else 'MISS'}] 「{phrase}」 -> Top1={top_id} "
              f"(score={float(pairs[0][1]):.4f})")

    check(f"逐字查询 Top-1 命中 {len(EXACT_QUERIES)}/{len(EXACT_QUERIES)}",
          exact_hits == len(EXACT_QUERIES), f"实际 {exact_hits}/{len(EXACT_QUERIES)}")

    # ------------------------------------------------------------------
    # 3b. 语义检索质量：如实统计，不做粉饰
    # ------------------------------------------------------------------
    print()
    print("      [语义检索质量] 口语化中文提问")
    top1_hits = 0
    top3_hits = 0
    best_scores: list[float] = []

    for question, expected in QUERIES:
        pairs = store.similarity_search_with_score_by_vector(
            embeddings.embed_query(question), k=len(CORPUS)
        )
        ranked = [doc.metadata["doc_id"] for doc, _ in pairs]
        target = f"d{expected}"
        in_top1 = ranked[0] == target
        in_top3 = target in ranked[:3]
        top1_hits += int(in_top1)
        top3_hits += int(in_top3)
        best_scores.append(float(pairs[0][1]))

        flag = "OK  " if in_top1 else ("TOP3" if in_top3 else "MISS")
        print(
            f"        [{flag}] 「{question}」 -> Top1={ranked[0]} "
            f"(score={float(pairs[0][1]):.4f})  期望={target}"
        )

    print()
    note(f"口语化提问 Top-1 命中 {top1_hits}/{len(QUERIES)}，"
         f"Top-3 命中 {top3_hits}/{len(QUERIES)}")
    note("all-MiniLM-L6-v2 以英文语料为主训练，中文语义泛化偏弱；"
         "命中率低于预期属于模型能力问题，不是检索管道故障。")

    # 分数必须随排序单调递减
    scores = [
        float(s)
        for _, s in store.similarity_search_with_score_by_vector(
            embeddings.embed_query(QUERIES[0][0]), k=len(CORPUS)
        )
    ]
    check("分数降序有序", all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)),
          " -> ".join(f"{s:.3f}" for s in scores))

    # 默认阈值必须足够宽松，不能把相关文档误杀在检索阶段
    worst_top1 = min(best_scores)
    check(
        "默认阈值不误杀相关文档",
        worst_top1 > settings.score_threshold,
        f"最低相关分={worst_top1:.4f} > 阈值={settings.score_threshold}",
    )

    # 无关问题的绝对分数：用于说明「绝对阈值不可跨语言/跨模型照搬」
    irrelevant = store.similarity_search_with_score_by_vector(
        embeddings.embed_query("今天天气怎么样"), k=1
    )
    irrelevant_score = float(irrelevant[0][1])
    note(
        f"无关问题「今天天气怎么样」最高分={irrelevant_score:.4f}，"
        f"相关问题上限={max(best_scores):.4f}"
    )
    if irrelevant_score >= max(best_scores):
        note(
            "无关问题得分不低于相关问题 —— 说明 all-MiniLM-L6-v2 在中文上的"
            "分数区分度较弱，绝对阈值不能设高，主要靠 LLM 依据上下文判断。"
        )

    # ------------------------------------------------------------------
    section("4. 文档解析与切分")
    from app.services.loader import parse_document

    # 这里刻意不用 tempfile.TemporaryDirectory：它内部用 mkdtemp(0o700) 建目录，
    # 在部分受限环境（例如带文件系统过滤的沙箱）下该目录不可写。
    # 改用普通 mkdir 建目录，处处可用。
    work_dir = TMP_ROOT / "loader_cases"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        tmp_path = work_dir

        txt = tmp_path / "policy.txt"
        long_text = "公司差旅报销管理办法。\n\n" + ("第一条 市内交通费每日上限八十元。" * 40)
        txt.write_text(long_text, encoding="utf-8")
        chunks, chars = parse_document(txt, "t1", "policy.txt")
        check("TXT 解析出分块", len(chunks) >= 1, f"{len(chunks)} chunks / {chars} chars")
        check("分块不超过 chunk_size",
              all(len(c.page_content) <= settings.chunk_size for c in chunks))
        check("metadata 含 doc_id", all(c.metadata["doc_id"] == "t1" for c in chunks))
        check("chunk_index 连续",
              [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks))))

        md = tmp_path / "readme.md"
        md.write_text("# 标题\n\n这是一段用于测试的 Markdown 内容，包含足够的字符以通过长度过滤。" * 5,
                      encoding="utf-8")
        md_chunks, _ = parse_document(md, "t2", "readme.md")
        check("Markdown 解析", len(md_chunks) >= 1, f"{len(md_chunks)} chunks")

        csv_file = tmp_path / "data.csv"
        csv_file.write_text("姓名,部门,工号\n张三,研发部,A001\n李四,市场部,B002\n", encoding="utf-8")
        csv_chunks, _ = parse_document(csv_file, "t3", "data.csv")
        check("CSV 解析为可读文本", "研发部" in csv_chunks[0].page_content)

        bad = tmp_path / "image.png"
        bad.write_bytes(b"\x89PNG\r\n\x1a\n")
        try:
            parse_document(bad, "t4", "image.png")
            check("拒绝不支持的类型", False, "未抛出异常")
        except Exception as exc:  # noqa: BLE001
            check("拒绝不支持的类型", "不支持" in str(exc), type(exc).__name__)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    print()
    print("=" * 70)
    print(f"结果：通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 70)
    print()
    print("选型提示：")
    print("  all-MiniLM-L6-v2 只有 384 维、以英文语料为主训练，中文语义匹配偏弱。")
    print("  若知识库以中文为主，建议改用中文/多语言嵌入模型（改 .env 即可，无需改代码）：")
    print("      RAG_EMBEDDING_MODEL_NAME=BAAI/bge-small-zh-v1.5")
    print("  换模型后必须重建索引（不同模型的向量空间不通用）。")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
