"""离线核心链路自测：Embeddings 本地加载 + FAISS 检索 + 文档切分（不依赖 Ollama）。
只准备好嵌入模型就能跑，用来定位「检索侧」的问题；会如实报告 all-MiniLM-L6-v2 在
中文语料上的命中率（见文末结论）。

    .venv\\Scripts\\python.exe tests\\test_offline.py
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
    # 3a. 逐字查询必须命中原文所在文档（与模型语义泛化能力无关）
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
    # 3b. 语义检索质量：口语化中文提问，如实统计
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

    # 无关问题的绝对分数：说明绝对阈值不可跨语言/跨模型照搬
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

    # 不用 tempfile.TemporaryDirectory：它内部 mkdtemp(0o700) 在受限环境下不可写
    work_dir = TMP_ROOT / "loader_cases"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        tmp_path = work_dir

        txt = tmp_path / "policy.txt"
        long_text = "公司差旅报销管理办法。\n\n" + ("第一条 市内交通费每日上限八十元。" * 40)
        txt.write_text(long_text, encoding="utf-8")
        outcome = parse_document(txt, "t1", "policy.txt")
        chunks, chars = outcome.chunks, outcome.char_count
        check("TXT 解析出分块", len(chunks) >= 1, f"{len(chunks)} chunks / {chars} chars")
        check("分块不超过 chunk_size",
              all(len(c.page_content) <= settings.chunk_size for c in chunks))
        check("metadata 含 doc_id", all(c.metadata["doc_id"] == "t1" for c in chunks))
        check("chunk_index 连续",
              [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks))))
        check("正常文档不产生解析警告", outcome.warnings == [], str(outcome.warnings))

        md = tmp_path / "readme.md"
        md.write_text("# 标题\n\n这是一段用于测试的 Markdown 内容，包含足够的字符以通过长度过滤。" * 5,
                      encoding="utf-8")
        md_chunks = parse_document(md, "t2", "readme.md").chunks
        check("Markdown 解析", len(md_chunks) >= 1, f"{len(md_chunks)} chunks")

        csv_file = tmp_path / "data.csv"
        csv_file.write_text("姓名,部门,工号\n张三,研发部,A001\n李四,市场部,B002\n", encoding="utf-8")
        csv_chunks = parse_document(csv_file, "t3", "data.csv").chunks
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
    section("5. PDF 版面还原：页眉 / 段落 / 参考文献")
    # 防「页眉与参考文献副本挤掉正文」：期刊 PDF 每页都有同一段页眉，
    # 它在向量空间里是关键词拼盘，对任何提问相似度都不低，必须先去重。
    from app.services.loader import (
        insert_paragraph_breaks,
        strip_reference_sections,
        strip_repeated_lines,
    )

    def page(body: str) -> tuple[str, dict]:
        return (body, {})

    # 页眉在奇偶页差一个空格 —— 只折叠空白是抓不到的
    pages = [
        page("2020 年第 2 期\n信息与电脑\nChina Computer & Communication人工智能与识别技术\n正文甲。"),
        page("2020 年第 2 期\n信息与电脑\nChina Computer & Communication 人工智能与识别技术\n正文乙。"),
        page("2020 年第 2 期\n信息与电脑\nChina Computer & Communication人工智能与识别技术\n正文丙。"),
        page("2020 年第 2 期\n信息与电脑\nChina Computer & Communication 人工智能与识别技术\n正文丁。"),
    ]
    cleaned_pages, dropped_lines = strip_repeated_lines(pages)
    check("页眉被识别为重复行（含空格差异）", dropped_lines == 12, f"删除 {dropped_lines} 行")
    check("页眉已从正文中移除", all("China Computer" not in text for text, _ in cleaned_pages))
    check("正文未被误删", all(text.strip().endswith("。") for text, _ in cleaned_pages),
          str([text.strip()[-3:] for text, _ in cleaned_pages]))

    short_pages, short_dropped = strip_repeated_lines(pages[:2])
    check("页数太少时不做重复行过滤（样本不足易误删）",
          short_dropped == 0 and short_pages == pages[:2])

    flowed = "设计要点如下。\n1.1 多任务网络架构\n笔者首先将人脸检测与关键点定位相结合。"
    repaired = insert_paragraph_breaks(flowed)
    check("小节标题前补回段落边界", "\n\n1.1 多任务网络架构" in repaired, repaired.replace("\n", "\\n"))

    switched = insert_paragraph_breaks(
        "Key words:  multi-task network; face detection\n中文正文开始，这里讲具体做法。"
    )
    check("中英文切换处补回段落边界",
          "face detection\n\n中文正文开始" in switched, switched.replace("\n", "\\n"))

    plain_flowed = insert_paragraph_breaks("这是一句普通的话。\n紧接着还是同一段。")
    check("同一段内的普通换行不插空行", "\n\n" not in plain_flowed)

    ref_pages = [
        page(
            "结语：本文提出了多任务网络。\n参考文献\n"
            "[1] 张三 . 论文题目 [D]. 北京 : 某大学 ,2019:1-2.\n"
            "[2] 李四 . 另一篇论文 [J]. 某期刊 ,2020:3-4."
        )
    ]
    cut_pages, cut_lines = strip_reference_sections(ref_pages)
    check("参考文献列表被截掉", cut_lines > 0 and "参考文献" not in cut_pages[0][0],
          f"截掉 {cut_lines} 行")
    check("参考文献之前的正文保留", "结语" in cut_pages[0][0])

    mention_pages = [page("正文里顺口提到参考文献的排版习惯。\n后面还有正文内容。")]
    kept_pages, kept_lines = strip_reference_sections(mention_pages)
    check("只是提到「参考文献」不会被误删",
          kept_lines == 0 and "后面还有正文内容" in kept_pages[0][0])

    # ------------------------------------------------------------------
    section("6. 提问意图识别与提示词切换")
    from app.schemas import SourceChunk
    from app.services.rag import OVERVIEW_PROMPT, SYSTEM_PROMPT, build_messages, detect_intent

    for question in (
        "请总结这篇文档的主要内容",
        "总结一下",
        "文档的大致内容",
        "用三句话总结这些文档的主要内容",
        "这篇文章讲了什么？",
    ):
        check(f"概览意图：{question}", detect_intent(question) == "overview")

    for question in ("多任务网络的优势是什么", "住宿费每晚多少钱", "年假有几天"):
        check(f"问答意图：{question}", detect_intent(question) == "qa")

    # 很长的提问更可能是细节问题，不该判为概览
    long_question = "请总结" + "关于差旅报销与休假制度的各项具体规定以及需要留意的例外情形和审批要求" * 4
    check("超长提问不判为概览", detect_intent(long_question) == "qa",
          f"{len(long_question)} 字")

    demo_sources = [
        SourceChunk(index=1, doc_id="d1", filename="a.txt", page=1,
                    chunk_index=0, score=0.5, content="示例内容。")
    ]
    overview_system = build_messages("总结一下", demo_sources, [], mode="overview")[0].content
    qa_system = build_messages("总结一下", demo_sources, [], mode="qa")[0].content
    check("概览模式换成概览提示词", overview_system.startswith(OVERVIEW_PROMPT[:12]))
    check("问答模式仍用问答提示词", qa_system.startswith(SYSTEM_PROMPT[:12]))
    check("两种提示词都带参考资料占位", "示例内容" in overview_system and "示例内容" in qa_system)

    # ------------------------------------------------------------------
    section("7. 近重复去重与概览取样名额")
    from langchain_core.documents import Document

    from app.services.vectorstore import (
        _allocate_quotas,
        _dedupe_pairs,
        _evenly_pick,
        _jaccard,
        _shingles,
    )

    header = "2020 年第 2 期 信息与电脑 China Computer & Communication 人工智能与识别技术 "
    duplicate_pairs = [
        (Document(page_content=header + "正文甲：住宿费一线城市每晚 600 元。"), 0.61),
        (Document(page_content=header + "正文甲：住宿费一线城市每晚 600 元。"), 0.60),
        (Document(page_content="年假满一年 5 天，满三年 10 天。"), 0.35),
    ]
    kept_pairs, dropped_pairs = _dedupe_pairs(duplicate_pairs, 0.8)
    check("页眉副本只保留一条", dropped_pairs == 1 and len(kept_pairs) == 2,
          f"保留 {len(kept_pairs)} / 丢弃 {dropped_pairs}")
    check("保留的是分数更高的那条", kept_pairs[0][1] == 0.61)
    check("内容不同不会被误判为重复",
          _jaccard(_shingles("差旅报销标准"), _shingles("年假天数规定")) < 0.2)

    quotas = _allocate_quotas([11, 131], 10)
    check("长短文档都能分到名额", all(value >= 1 for value in quotas), str(quotas))
    check("名额总数不超过预算", sum(quotas) <= 10, str(quotas))
    check("长文档分到更多名额", quotas[1] > quotas[0], str(quotas))

    picked = _evenly_pick([(index, None) for index in range(20)], 4)
    check("均匀取样首尾必取", [item[0] for item in picked][0] == 0
          and [item[0] for item in picked][-1] == 19, str([item[0] for item in picked]))

    # ------------------------------------------------------------------
    section("8. 扫描版 PDF 的抽取质量判定")
    # 防「扫描件在界面上只显示索引完成」：多页且每页字符数过低 → 给出面向用户的
    # 警告文案；页数太少则不判断（样本不足容易误伤）。
    from app.services.loader import low_extraction_warning

    blank_pages = [("", {}), ("", {}), ("", {}), ("", {})]
    blank_warning = low_extraction_warning(blank_pages)
    check("多页空白 PDF 判定为扫描件",
          bool(blank_warning) and "扫描件" in blank_warning and "OCR" in blank_warning,
          (blank_warning or "(无)")[:40])

    sparse_pages = [("第 1 页", {}), ("第 2 页", {}), ("第 3 页", {})]
    sparse_warning = low_extraction_warning(sparse_pages)
    check("字符/页低于阈值时也报警",
          bool(sparse_warning) and f"低于 {settings.pdf_min_chars_per_page}" in sparse_warning,
          (sparse_warning or "(无)")[:40])

    rich_pages = [("正文内容。" * 60, {}) for _ in range(4)]
    check("正常文本量的 PDF 不误报", low_extraction_warning(rich_pages) is None)

    check("页数太少时不下结论（阈值判定需要样本量）",
          low_extraction_warning([("", {}), ("", {})]) is None)

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
