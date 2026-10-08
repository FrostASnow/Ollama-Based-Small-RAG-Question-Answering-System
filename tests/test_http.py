"""HTTP 层验收测试（针对真实运行的 uvicorn 服务）。覆盖前端静态托管、文档上传/
列表/删除、检索接口与 SSE 流式问答的事件协议；Ollama 未启动时断言问答接口返回
结构化 error 事件，而不是静默挂断或 500。**本套件会 DELETE /api/documents
（清空知识库）**，只能对着使用临时数据目录的实例运行（见 .\\tests\\run_all.ps1）。

    .venv\\Scripts\\python.exe tests\\test_http.py --base http://127.0.0.1:8099
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = PROJECT_ROOT / ".tmp" / "tests"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

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


def section(title: str) -> None:
    print()
    print("-" * 70)
    print(title)
    print("-" * 70)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析成 [(event, data)]。"""
    events: list[tuple[str, dict]] = []
    for frame in text.split("\n\n"):
        event = None
        data_lines = []
        for line in frame.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if event and data_lines:
            try:
                events.append((event, json.loads("\n".join(data_lines))))
            except json.JSONDecodeError:
                events.append((event, {"_raw": "\n".join(data_lines)}))
    return events


SAMPLE = TMP_ROOT / "http_sample.txt"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print("=" * 70)
    print("HTTP 层验收测试")
    print("=" * 70)
    print(f"目标服务: {base}")

    SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE.write_text(
        "《差旅报销管理办法》\n\n"
        "住宿费一线城市每晚 600 元，其他城市每晚 400 元。"
        "市内交通费每日上限 80 元。超出标准需部门负责人书面审批。\n\n"
        "员工应在出差结束后十五个工作日内提交报销单并附全部原始票据。\n",
        encoding="utf-8",
    )

    # 等待服务真正就绪：带代理的环境里，端口尚未监听时首个请求可能收到代理
    # 返回的 502 而不是连接错误。
    #
    # trust_env=False 是关键：Windows 上一旦设置了系统代理，httpx 会连
    # 127.0.0.1 也走代理，本地服务永远拿到 502（浏览器与 Invoke-RestMethod
    # 会读代理绕过列表，httpx 不读）。本套件只测本机服务。
    ready = False
    with httpx.Client(base_url=base, timeout=5.0, trust_env=False) as probe:
        for _ in range(80):
            try:
                if probe.get("/api/health").status_code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)

    if not ready:
        print(f"\n[FAIL] 服务未就绪：{base}")
        print("       请先启动： 双击 scripts\\start.cmd")
        return 1
    print("服务已就绪。")

    uploaded_doc_id = None

    with httpx.Client(base_url=base, timeout=60.0, trust_env=False) as client:
        # ------------------------------------------------------------------
        section("1. 健康检查与配置")
        try:
            r = client.get("/api/health")
        except httpx.ConnectError:
            print(f"\n[FAIL] 无法连接 {base}，请先启动服务：")
            print("       双击 scripts\\start.cmd")
            return 1

        check("GET /api/health 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
        health = r.json()

        # ------------------------------------------------------------------
        # 安全检查：本套件会 DELETE /api/documents（清空知识库），对着真实
        # data/ 目录跑删掉的就是用户上传的文档 —— 数据丢失，立即停手。
        # ------------------------------------------------------------------
        target_data_dir = (health.get("detail") or {}).get("data_dir", "")
        real_data_dir = PROJECT_ROOT / "data"
        print(f"目标服务数据目录: {target_data_dir or '(未上报)'}")
        if not target_data_dir:
            print("\n[FAIL] 服务未上报 data_dir，无法确认它是不是临时数据目录。")
            print("       本套件会清空知识库，拒绝在不确定的情况下继续。")
            print("       请改用：.\\tests\\run_all.ps1")
            return 1
        if Path(target_data_dir).resolve() == real_data_dir.resolve():
            print("\n[FAIL] 目标服务正在使用真实数据目录，本套件会把它清空。")
            print(f"       {real_data_dir}")
            print("       请改用：.\\tests\\run_all.ps1（会自动起一个临时目录实例）")
            return 1

        # JSON 必须显式声明 charset=utf-8。
        # 否则 Windows PowerShell 5.1 的 Invoke-RestMethod 会按 ISO-8859-1 解码，
        # 「离线 RAG 文档问答」会变成「ç¦»çº¿ RAG ææ¡£é®ç」。
        for path, method, kwargs in [
            ("/api/health", "GET", {}),
            ("/api/config", "GET", {}),
            ("/api/documents", "GET", {}),
            ("/api/setup", "GET", {}),
            ("/api/chat", "POST", {"json": {}}),           # 422 校验错误
            ("/api/documents/nope/chunks", "GET", {}),     # 404
        ]:
            resp = client.request(method, path, timeout=30.0, **kwargs)
            ctype = resp.headers.get("content-type", "")
            check(f"{method} {path} 声明 charset=utf-8",
                  "charset=utf-8" in ctype, f"{resp.status_code} {ctype}")

        check("中文在 JSON 往返中保持完整",
              "离线" in health.get("app_name", ""), health.get("app_name", ""))
        check("响应含 status 字段", "status" in health, health.get("status", ""))
        check("声明离线模式", health.get("offline") is True)
        check("嵌入模型已就绪", health.get("embedding_ready") is True,
              str(health.get("embedding_model")))
        check("报告 Ollama 可达性", "ollama_reachable" in health,
              f"reachable={health.get('ollama_reachable')}")
        print(f"        status={health.get('status')} "
              f"ollama_reachable={health.get('ollama_reachable')} "
              f"llm={health.get('llm_model')} "
              f"model_available={health.get('llm_model_available')}")

        r = client.get("/api/config")
        check("GET /api/config 返回 200", r.status_code == 200)
        config = r.json()
        check("配置含 top_k", "top_k" in config, str(config.get("top_k")))
        check("配置含嵌入模型名", "embedding_model_name" in config,
              str(config.get("embedding_model_name")))
        check("配置暴露检索工程化参数",
              all(key in config for key in ("score_window", "score_floor", "dedupe_ratio",
                                            "summary_max_chunks", "strip_boilerplate")),
              str({key: config.get(key) for key in ("score_window", "score_floor",
                                                     "dedupe_ratio", "summary_max_chunks")}))

        # GET 与 PUT 必须字段对称：GET 返回的每一项都要能原样写回去。
        # 以前两边各写一份字段清单，结果 GET 有 llm_num_ctx / dedupe_ratio
        # 而 PUT 根本收不到。
        r = client.put("/api/config", json=config)
        check("GET 返回的每一项都能 PUT 回去", r.status_code == 200, f"HTTP {r.status_code}")
        echoed = r.json()
        check("PUT 的响应字段与 GET 完全一致", set(echoed) == set(config),
              str(sorted(set(echoed) ^ set(config))) or f"{len(echoed)} 个字段")
        check("回显与提交值一致", all(echoed[key] == config[key] for key in config),
              str([key for key in config if echoed.get(key) != config[key]]) or "全部一致")

        r = client.put("/api/config", json={"chunk_size": 500, "chunk_overlap": 600})
        check("自相矛盾的切分参数被拒绝（400）", r.status_code == 400, f"HTTP {r.status_code}")

        r = client.put("/api/config", json={"allowed_extensions": ["txt", "MD"]})
        check("扩展名会被归一化成小写带点的形式（保序）",
              r.status_code == 200 and r.json()["allowed_extensions"] == [".txt", ".md"],
              str(r.json().get("allowed_extensions")))
        client.put("/api/config", json={"allowed_extensions": config["allowed_extensions"]})

        index_report = (health.get("detail") or {}).get("index") or {}
        check("健康检查上报索引一致性报告",
              index_report.get("state") in ("empty", "ok", "stale", "incompatible"),
              str(index_report.get("state")))
        check("索引报告含四个维度的对比数据",
              all(key in (index_report.get("dimension") or {})
                  for key in ("index", "model", "recorded", "declared")),
              str(index_report.get("dimension")))
        check("顶层索引字段与报告一致",
              (health.get("detail") or {}).get("index_compatible") == index_report.get("compatible"))

        r = client.get("/api/models")
        check("GET /api/models 返回 200", r.status_code == 200)

        # ------------------------------------------------------------------
        section("2. 前端静态资源托管")
        r = client.get("/")
        check("GET / 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("返回 HTML", "text/html" in r.headers.get("content-type", ""))
        check("HTML 含应用标题", "离线 RAG" in r.text)
        check("HTML 引用 app.js", "/assets/js/app.js" in r.text)

        for asset, kind in [
            ("/assets/css/style.css", "text/css"),
            ("/assets/js/app.js", "javascript"),
            ("/assets/js/api.js", "javascript"),
            ("/assets/js/markdown.js", "javascript"),
        ]:
            r = client.get(asset)
            ok = r.status_code == 200 and kind in r.headers.get("content-type", "")
            check(f"GET {asset}", ok, f"HTTP {r.status_code} {r.headers.get('content-type','')}")

        r = client.get("/")
        check("index.html 以 module 方式加载入口脚本",
              'type="module"' in r.text and '/assets/js/app.js' in r.text)

        r = client.get("/assets/js/app.js")
        check("app.js 使用 ES Module 语法",
              "import {" in r.text and "from './api.js'" in r.text)

        # 目录穿越不应泄露文件
        r = client.get("/../backend/app/config.py")
        check("阻止路径穿越", r.status_code in (400, 403, 404) or "Settings" not in r.text,
              f"HTTP {r.status_code}")

        # ------------------------------------------------------------------
        section("3. 文档上传与索引")
        client.delete("/api/documents")  # 从干净状态开始

        with SAMPLE.open("rb") as handle:
            r = client.post(
                "/api/documents/upload",
                files={"files": (SAMPLE.name, handle, "text/plain")},
            )
        check("POST /upload 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
        results = r.json()
        check("返回结果数组", isinstance(results, list) and len(results) == 1)
        doc = results[0]["document"]
        check("索引状态为 indexed", doc["status"] == "indexed", doc.get("error") or "")
        check("生成分块数 > 0", doc["chunk_count"] > 0, str(doc["chunk_count"]))
        uploaded_doc_id = doc["doc_id"]

        r = client.get("/api/documents")
        listing = r.json()
        check("GET /api/documents 返回 200", r.status_code == 200)
        check("列表含 1 个文档", listing["total"] == 1, str(listing["total"]))
        check("分块统计一致", listing["total_chunks"] == doc["chunk_count"])
        check("文档记录带 warnings 字段（前端据此显示解析警告）",
              isinstance(doc.get("warnings"), list)
              and isinstance(listing["documents"][0].get("warnings"), list),
              str(doc.get("warnings")))

        r = client.get(f"/api/documents/{uploaded_doc_id}/chunks")
        check("分块预览接口可用", r.status_code == 200 and r.json()["returned"] > 0)

        # ------------------------------------------------------------------
        section("3b. 改切分参数 → 僵尸知识库必须被报出来")
        # 改了 chunk_size 后旧索引照常能检索，只是分块还是老参数切的 ——
        # 以前这件事完全静默，用户无从发现。
        original_chunk = config["chunk_size"]
        r = client.put("/api/config", json={"chunk_size": original_chunk + 100})
        check("PUT chunk_size 生效",
              r.status_code == 200 and r.json()["chunk_size"] == original_chunk + 100,
              f"HTTP {r.status_code}")
        stale_health = client.get("/api/health").json()
        check("索引与配置漂移后健康检查转为 degraded",
              stale_health["status"] == "degraded", str(stale_health["status"]))
        check("提示里给出「重建索引」这条出路",
              any("重建索引" in item
                  for item in (stale_health.get("detail") or {}).get("problems", [])),
              str((stale_health.get("detail") or {}).get("problems"))[:90])
        check("索引仍标记为「兼容但过期」（不阻断检索）",
              (stale_health.get("detail") or {}).get("index_compatible") is True
              and (stale_health.get("detail") or {}).get("index_stale") is True)

        search_after = client.post("/api/search", json={"query": "住宿费", "score_threshold": 0.0})
        check("过期索引下检索依然可用", search_after.status_code == 200,
              f"HTTP {search_after.status_code}")

        client.put("/api/config", json={"chunk_size": original_chunk})
        restored_health = client.get("/api/health").json()
        check("改回原参数后索引重新一致",
              (restored_health.get("detail") or {}).get("index_stale") is False,
              str((restored_health.get("detail") or {}).get("index", {}).get("state")))

        # 不支持的类型应被拒绝且不中断整批
        with SAMPLE.open("rb") as handle:
            r = client.post(
                "/api/documents/upload",
                files={"files": ("bad.exe", handle, "application/octet-stream")},
            )
        check("拒绝不支持的类型", r.status_code == 200)
        check("失败项标记为 failed", r.json()[0]["document"]["status"] == "failed",
              r.json()[0]["message"][:60])

        # ------------------------------------------------------------------
        section("4. 检索接口")
        r = client.post("/api/search", json={"query": "住宿费一线城市每晚多少钱", "top_k": 3,
                                             "score_threshold": 0.0})
        check("POST /api/search 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
        search_data = r.json()
        check("返回检索结果", len(search_data["results"]) >= 1,
              f"{len(search_data['results'])} 条")
        if search_data["results"]:
            top = search_data["results"][0]
            check("结果含来源文件名", bool(top["filename"]), top["filename"])
            check("结果含相似度分数", isinstance(top["score"], float), str(top["score"]))
            check("结果含原文片段", len(top["content"]) > 0, f"{len(top['content'])} 字符")
        check("返回耗时", isinstance(search_data["elapsed_ms"], int))
        info = search_data.get("info") or {}
        check("检索诊断含 mode", info.get("mode") in ("qa", "overview"), str(info.get("mode")))
        check("检索诊断含生效阈值",
              isinstance(info.get("effective_threshold"), (int, float)),
              str(info.get("effective_threshold")))
        check("检索诊断含最佳分数与去重数量",
              isinstance(info.get("best_score"), (int, float))
              and isinstance(info.get("dropped_duplicates"), int),
              f"best={info.get('best_score')} dup={info.get('dropped_duplicates')}")
        check("检索诊断含索引总量",
              isinstance(info.get("chunks_total"), int) and info["chunks_total"] > 0,
              str(info.get("chunks_total")))

        # 总结类问题：非流式问答要声明走了概览检索
        r = client.post("/api/search", json={"query": "总结一下这份文档的主要内容",
                                             "score_threshold": 0.2})
        check("POST /api/search 总结类问题仍走向量检索（纯检索接口不切策略）",
              r.status_code == 200 and (r.json().get("info") or {}).get("mode") == "qa")

        # ------------------------------------------------------------------
        section("5. 重建索引接口")
        r = client.post("/api/documents/reindex")
        check("POST /api/documents/reindex 返回 200", r.status_code == 200,
              f"HTTP {r.status_code}")
        report = r.json()
        check("报告重建文档数与分块数变化",
              report.get("rebuilt", 0) >= 1 and isinstance(report.get("chunks_after"), int),
              f"rebuilt={report.get('rebuilt')} {report.get('chunks_before')}->"
              f"{report.get('chunks_after')}")
        check("重建报告带索引状态与备份路径",
              report.get("index_state_before") in ("ok", "stale", "incompatible")
              and bool(report.get("backup_dir")),
              f"state={report.get('index_state_before')} backup={report.get('backup_dir')}")
        check("重建后分块数不为 0", report.get("chunks_after", 0) > 0,
              str(report.get("chunks_after")))
        check("重建明细逐文档返回",
              isinstance(report.get("details"), list) and len(report["details"]) >= 1)
        r = client.get("/api/documents")
        check("重建后注册表与实际分块一致",
              r.json()["total_chunks"] == report.get("chunks_after"),
              f"{r.json()['total_chunks']} vs {report.get('chunks_after')}")

        # ------------------------------------------------------------------
        section("6. 流式问答（SSE 协议）")
        # 无论 Ollama 是否在跑，协议本身都必须成立：
        #   在跑  -> meta/token/sources/done
        #   没跑  -> meta/error
        with client.stream(
            "POST",
            "/api/chat",
            json={"question": "住宿费一线城市每晚多少钱？", "stream": True, "top_k": 2},
            headers={"Accept": "text/event-stream"},
            timeout=120.0,
        ) as response:
            check("POST /api/chat 返回 200", response.status_code == 200,
                  f"HTTP {response.status_code}")
            check("Content-Type 为 text/event-stream",
                  "text/event-stream" in response.headers.get("content-type", ""),
                  response.headers.get("content-type", ""))
            body = "".join(response.iter_text())

        events = parse_sse(body)
        names = [name for name, _ in events]
        print(f"        收到事件序列: {names}")

        check("收到事件", len(events) > 0, f"{len(events)} 个")
        check("首个事件为 meta", names and names[0] == "meta", names[0] if names else "无")

        meta = next((d for n, d in events if n == "meta"), {})
        check("meta 含模型名", bool(meta.get("model")), str(meta.get("model")))
        check("meta 报告召回数量", meta.get("source_count", 0) >= 1,
              str(meta.get("source_count")))
        check("meta 声明检索策略 mode", meta.get("mode") in ("qa", "overview"),
              str(meta.get("mode")))
        check("meta 声明是否放宽阈值", isinstance(meta.get("relaxed"), bool),
              str(meta.get("relaxed")))
        check("meta 带上最佳相似度与索引总量",
              isinstance(meta.get("best_score"), (int, float))
              and isinstance(meta.get("chunks_total"), int),
              f"best={meta.get('best_score')} total={meta.get('chunks_total')}")

        # 概览类提问必须切到 overview 策略（阈值对它没有意义）
        with client.stream(
            "POST",
            "/api/chat",
            json={"question": "用三句话总结这份文档的主要内容", "stream": True},
            headers={"Accept": "text/event-stream"},
            timeout=120.0,
        ) as response:
            overview_body = "".join(response.iter_text())
        overview_meta = next(
            (d for n, d in parse_sse(overview_body) if n == "meta"), {}
        )
        check("总结类问题的 meta.mode 为 overview",
              overview_meta.get("mode") == "overview", str(overview_meta.get("mode")))
        overview_total = int(overview_meta.get("chunks_total") or 0)
        overview_count = int(overview_meta.get("source_count") or 0)
        check("概览模式覆盖全部分块（小文档）或达到取样预算（大文档）",
              overview_count >= 1 and overview_count >= min(overview_total, 4),
              f"{overview_count} / {overview_total} 块")

        if "error" in names:
            error = next(d for n, d in events if n == "error")
            print(f"        Ollama 未运行，走 error 分支：{str(error.get('message'))[:90]}")
            check("error 事件含可读信息", len(str(error.get("message", ""))) > 5)
            check("error 事件标注阶段", "stage" in error, str(error.get("stage")))
        else:
            check("收到 token 事件", "token" in names)
            check("收到 sources 事件", "sources" in names)
            check("以 done 结尾", names[-1] == "done", names[-1])
            answer = "".join(d.get("delta", "") for n, d in events if n == "token")
            check("回答非空", len(answer.strip()) > 0, f"{len(answer)} 字符")
            print(f"        回答（前 120 字）：{answer[:120]}")

        # ------------------------------------------------------------------
        section("7. 文档删除")
        r = client.delete(f"/api/documents/{uploaded_doc_id}")
        check("DELETE 单个文档返回 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("报告删除的分块数", r.json()["deleted_chunks"] > 0,
              str(r.json()["deleted_chunks"]))

        r = client.get("/api/documents")
        check("列表已清空", r.json()["total"] == 0, str(r.json()["total"]))

        r = client.get(f"/api/documents/{uploaded_doc_id}/chunks")
        check("已删除文档返回 404", r.status_code == 404, f"HTTP {r.status_code}")

        # 知识库为空时问答应给出 409 而不是崩溃
        r = client.post("/api/chat", json={"question": "测试", "stream": False})
        check("空知识库问答返回 409", r.status_code == 409, f"HTTP {r.status_code}")
        check("409 附带可读提示", "上传文档" in r.json().get("detail", ""),
              r.json().get("detail", "")[:60])

        # ------------------------------------------------------------------
        section("8. 首次配置引导（/api/setup）")
        r = client.get("/api/setup", timeout=30.0)
        check("GET /api/setup 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
        setup = r.json()

        for field in ("ready", "blocking_count", "headline", "issues", "environment",
                      "checked_at"):
            check(f"响应含 {field}", field in setup)

        # 与 /api/health 的判定必须一致，否则前端会误导用户。
        # 注意 Ollama 是否「装全」也要算进去：只有 ollama.exe 时服务照样可达、
        # 模型照样列得出来，但一发提问就失败。
        install = health.get("detail", {}).get("ollama_install") or {}
        expect_ready = bool(
            health.get("ollama_reachable")
            and health.get("llm_model_available")
            and health.get("embedding_ready")
            and install.get("complete") is not False
        )
        check("ready 与 /api/health 判定一致", setup["ready"] == expect_ready,
              f"setup.ready={setup['ready']} 期望={expect_ready}")

        # 反向也一致：health 报出的每个问题，setup 都必须给出可执行解决方案
        health_problems = health.get("detail", {}).get("problems", [])
        if health_problems:
            check("health 有问题时 setup 判为未就绪", setup["ready"] is False,
                  str(health_problems))

        computed_blocking = sum(1 for i in setup["issues"] if i["severity"] == "blocking")
        check("blocking_count 与 issues 一致",
              setup["blocking_count"] == computed_blocking,
              f"字段={setup['blocking_count']} 实际={computed_blocking}")

        if setup["ready"]:
            check("就绪时 issues 为空", setup["issues"] == [])
        else:
            check("未就绪时至少有一个问题", len(setup["issues"]) >= 1,
                  f"{len(setup['issues'])} 项")

        # 每个问题都必须给出可执行的解决方案，否则这个弹窗就是死胡同
        for issue in setup["issues"]:
            check(f"问题「{issue['id']}」有解决方案", len(issue["options"]) >= 1)
            check(f"问题「{issue['id']}」有影响的说明", bool(issue["impact"]))
            total_commands = sum(len(o["commands"]) for o in issue["options"])
            check(f"问题「{issue['id']}」提供了命令", total_commands >= 1,
                  f"{total_commands} 条")
            check(f"问题「{issue['id']}」标注了推荐方案",
                  any(o["recommended"] for o in issue["options"]))

        # 命令里必须是真实绝对路径，不能有未替换的占位符
        all_commands = [
            cmd["command"]
            for issue in setup["issues"]
            for option in issue["options"]
            for cmd in option["commands"]
        ]
        placeholder_hits = [
            c for c in all_commands
            if any(tok in c for tok in ("<PROJECT", "${", "{project", "TODO", "None"))
        ]
        check("命令不含未替换的占位符", not placeholder_hits,
              placeholder_hits[0][:70] if placeholder_hits else "")

        project_root = setup["environment"]["project_root"]
        # 引用项目内脚本/可执行文件的命令必须是绝对路径，否则用户复制到
        # 别的目录执行就会找不到文件
        relative_hits = [
            c for c in all_commands
            if ("scripts\\" in c or "tools\\" in c or ".venv" in c)
            and project_root not in c
        ]
        check("脚本命令使用绝对路径", not relative_hits,
              relative_hits[0][:70] if relative_hits else f"{len(all_commands)} 条命令")

        check("环境信息含项目根", bool(project_root), project_root)
        check("环境信息含 ollama 探测结果",
              "ollama_binary" in setup["environment"] and "ollama_reachable" in setup["environment"])

        # 如果检测到二进制，路径必须真实存在
        binary = setup["environment"]["ollama_binary"]
        if binary.get("found"):
            check("报告的 ollama 路径真实存在", Path(binary["path"]).is_file(), binary["path"])

        # fresh=true 绕缓存，必须同样可用
        r = client.get("/api/setup?fresh=true", timeout=30.0)
        check("?fresh=true 可用", r.status_code == 200 and "ready" in r.json())

        print(f"        当前判定: ready={setup['ready']} · {setup['headline']}")
        for issue in setup["issues"]:
            print(f"        - [{issue['severity']}] {issue['title']} "
                  f"({len(issue['options'])} 个方案)")

        # --- 环境探测（指引要基于真实路径，而不是写死的模板）---
        toolchain = setup["environment"].get("toolchain")
        check("环境信息含工具链探测", isinstance(toolchain, dict), str(type(toolchain)))
        if isinstance(toolchain, dict):
            for tool in ("uv", "node", "venv", "powershell"):
                check(f"探测了 {tool}", tool in toolchain)
            check("uv 探测结果结构正确",
                  set(toolchain["uv"]) >= {"found", "path", "source"},
                  str(toolchain["uv"]))
            if toolchain["uv"]["found"]:
                check("报告的 uv 路径真实存在",
                      Path(toolchain["uv"]["path"]).is_file(), toolchain["uv"]["path"])

        check("提供一键安装能力开关",
              isinstance(setup["environment"].get("can_auto_install"), bool),
              str(setup["environment"].get("can_auto_install")))

        # ------------------------------------------------------------------
        section("9. 一键安装接口")

        r = client.get("/api/setup/install", timeout=15.0)
        check("GET /api/setup/install 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
        status = r.json()
        for field in ("status", "running", "started_at", "returncode", "log_file"):
            check(f"状态含 {field}", field in status)

        # 清理上一次测试可能残留的状态
        client.post("/api/setup/install/reset", timeout=15.0)

        # 用 skip_ollama 跳过 1.4GB 下载，让测试保持快速
        r = client.post("/api/setup/install", json={"skip_ollama": True}, timeout=20.0)
        check("POST /api/setup/install 返回 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("返回已开始", r.json().get("started") is True)

        # 并发保护：同一时间只允许一个安装任务
        r = client.post("/api/setup/install", json={"skip_ollama": True}, timeout=20.0)
        check("重复发起返回 409", r.status_code == 409, f"HTTP {r.status_code}")

        r = client.get("/api/setup/install", timeout=15.0)
        check("运行中时 running 为 true", r.json().get("running") is True,
              str(r.json().get("status")))

        # SSE：至少要能收到服务端头部日志
        got_lines = 0
        saw_end = False
        with client.stream("GET", "/api/setup/install/stream", timeout=60.0) as response:
            check("安装日志流 Content-Type 正确",
                  "text/event-stream" in response.headers.get("content-type", ""),
                  response.headers.get("content-type", ""))
            event = None
            for raw in response.iter_lines():
                if raw.startswith("event:"):
                    event = raw[6:].strip()
                elif raw.startswith("data:") and event == "log":
                    got_lines += 1
                    if got_lines >= 5:
                        break

        check("收到安装日志", got_lines >= 1, f"{got_lines} 行")

        # 取消：按进程树终止，避免留下孤儿子进程。
        # 若安装跑得很快（依赖已缓存时几秒就结束），这里可能已经没什么可取消的，
        # 那 409 也是正确行为 —— 断言放宽，避免测试本身变成随机失败源。
        r = client.post("/api/setup/install/cancel", timeout=30.0)
        check("取消安装返回 200 或 409（任务已结束）",
              r.status_code in (200, 409), f"HTTP {r.status_code}")

        # 等状态落定
        for _ in range(20):
            snapshot = client.get("/api/setup/install", timeout=10.0).json()
            if not snapshot["running"]:
                break
            time.sleep(0.5)

        check("取消后不再运行", snapshot["running"] is False, str(snapshot["status"]))
        check("状态标记为 cancelled 或已结束",
              snapshot["status"] in ("cancelled", "failed", "succeeded"),
              str(snapshot["status"]))

        r = client.post("/api/setup/install/reset", timeout=15.0)
        check("reset 后回到 idle", r.json().get("status") == "idle", str(r.json().get("status")))

    print()
    print("=" * 70)
    print(f"结果：通过 {PASSED} 项，失败 {FAILED} 项")
    print("=" * 70)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
