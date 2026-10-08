# API 参考

服务默认监听 `http://127.0.0.1:8000`。交互式文档：<http://127.0.0.1:8000/docs>

前端静态文件挂在 `/`，API 全部在 `/api` 下。

---

## 系统

### `GET /api/health`

服务与依赖的实时状态。

```json
{
  "status": "ok",
  "version": "1.0.0",
  "app_name": "离线 RAG 文档问答",
  "offline": true,
  "ollama_reachable": true,
  "llm_model": "deepseek-r1:1.5b",
  "llm_model_available": true,
  "ollama_models": ["deepseek-r1:1.5b"],
  "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
  "embedding_ready": true,
  "embedding_device": "cpu",
  "index_ready": true,
  "document_count": 3,
  "chunk_count": 42,
  "detail": {
    "problems": [],
    "embedding": { "loaded": true, "local_ready": true, "dimension": 384, "actual_dimension": 384 },
    "vector_store": { "ready": true, "vectors": 42 },
    "index_meta": {
      "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
      "dimension": 384,
      "chunk_size": 800,
      "chunk_overlap": 120,
      "parse_options": { "strip_boilerplate": true, "restore_pdf_paragraphs": true, "strip_references": true },
      "indexed_at": "2026-01-01T00:00:00+08:00"
    },
    "index": {
      "state": "ok",
      "compatible": true,
      "stale": false,
      "reasons": [],
      "notices": [],
      "dimension": { "index": 384, "model": 384, "recorded": 384, "declared": 384 }
    },
    "index_compatible": true,
    "index_stale": false,
    "data_dir": "…"
  }
}
```

`status` 为 `ok` 或 `degraded`；`degraded` 时 `detail.problems` 列出待处理项。
`detail.index.state` 是索引一致性体检的结论：

| state | 含义 | 后果 |
|-------|------|------|
| `empty` | 还没有索引 | 无 |
| `ok` | 索引与当前嵌入 / 切分 / 解析配置一致 | 无 |
| `stale` | 索引还能用，但内容对不上当前配置（典型：改过 `chunk_size`） | 状态转 `degraded`，建议重建索引 |
| `incompatible` | 向量空间不符（换过嵌入模型 / 维度） | 状态转 `degraded`，**检索被拒绝**（`/api/chat`、`/api/search` 返回 409） |

`reasons` 是阻断性原因，`notices` 是提醒性原因（均为可直接展示的中文文案）。
`dimension` 对比索引实际维度、模型实际输出维度、注册表登记值与配置声明值。
Ollama 探测缓存 5 秒、模型能力缓存 60 秒（并在切换模型 / 刷新模型列表 / 安装结束 / 重新检测时失效），故 Ollama 不可达时本接口也只需约 40ms。

---

### `GET /api/setup?fresh=false`

首次配置体检。前端启动时调用，`ready` 为 `false` 就弹出安装引导窗口；
安装命令由后端按当前实际安装位置生成，换目录或换机器后仍可直接复制执行。

```json
{
  "ready": false,
  "blocking_count": 1,
  "warning_count": 0,
  "headline": "还需完成 1 项配置即可开始问答",
  "checked_at": "2026-01-01T10:00:00+08:00",
  "issues": [
    {
      "id": "ollama_not_installed",
      "severity": "blocking",
      "title": "未检测到 Ollama",
      "detail": "Ollama 是本地运行大模型的引擎……",
      "impact": "可以上传和检索文档，但无法生成回答",
      "options": [
        {
          "id": "prepare",
          "title": "方式一：让本项目自动准备",
          "recommended": true,
          "description": "执行脚本会自动下载便携版 Ollama……",
          "commands": [
            {
              "label": "下载并解压便携版 Ollama + 拉取 LLM",
              "shell": "powershell",
              "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"D:\\...\\scripts\\prepare.ps1\"",
              "note": null
            }
          ],
          "notes": ["免安装、免管理员权限……"],
          "link": null
        }
      ]
    }
  ],
  "environment": {
    "project_root": "D:\\...\\rag-qa",
    "ollama_binary": { "found": false, "path": null, "source": null, "on_path": false },
    "ollama_base_url": "http://127.0.0.1:11434",
    "ollama_reachable": false,
    "ollama_models": [],
    "llm_model": "deepseek-r1:1.5b",
    "llm_model_available": false,
    "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
    "embedding_ready": true,
    "venv_ready": true,
    "prepare_script": "D:\\...\\scripts\\prepare.ps1",
    "portable_dir": "D:\\...\\tools\\ollama",
    "models_dir": "D:\\...\\models\\ollama",
    "platform": "win32"
  }
}
```

**检测的问题类型**

| id | severity | 触发条件 |
|----|----------|---------|
| `venv_missing` | blocking | 项目 `.venv` 不存在 |
| `embedding_missing` | blocking | 本地嵌入模型目录不完整 |
| `ollama_not_installed` | blocking | 既没有 ollama 二进制，服务也不可达 |
| `ollama_not_running` | blocking | 找到了二进制，但服务没起来 |
| `llm_model_missing` | blocking | 服务通了，但里面没有配置的 LLM |

便携版解压中断会额外报 `ollama_incomplete`（只有 `ollama.exe`、缺 `lib\ollama\` 推理运行时）。
`fresh=true` 绕过后端 5 秒探测缓存，供用户装完后点「重新检测」。
`ready` 与 `/api/health` 保持一致：`ollama_reachable && llm_model_available && embedding_ready`。

`environment.toolchain` 是本机工具链探测结果，指引据此「因地制宜」：

```json
{
  "uv":       { "found": true,  "path": "C:\\Users\\me\\.local\\bin\\uv.exe", "source": "PATH" },
  "node":     { "found": true,  "path": "D:\\nodejs\\node.exe", "source": "PATH" },
  "venv":     { "found": true,  "path": "D:\\...\\.venv\\Scripts\\python.exe" },
  "python":   { "path": "...", "version": "3.12.13" },
  "powershell": { "windows_powershell": true, "pwsh": false }
}
```

`environment.can_auto_install` 表示准备脚本是否存在，前端据此决定是否显示「一键开始安装」。

---

## 一键安装

让用户不必把命令复制到终端，直接在网页上把环境准备好。

### `GET /api/setup/install`

查询安装任务状态。

```json
{
  "status": "idle",
  "started_at": null,
  "finished_at": null,
  "returncode": null,
  "running": false,
  "mode": {},
  "log_file": "D:\\...\\data\\logs\\install.log"
}
```

`status` ∈ `idle` / `running` / `succeeded` / `failed` / `cancelled`。

### `POST /api/setup/install`

启动安装，等价于用户手动双击 `scripts\prepare.cmd`。

```json
{ "mirror": false, "skip_ollama": false }
```

* `mirror` —— 传给准备脚本的 `-Mirror`，国内镜像加速
* `skip_ollama` —— 只准备 Python 侧，跳过 1.4GB 的 Ollama 下载

同一时间只允许一个任务，重复调用返回 `409`：

```json
{ "detail": "已有安装任务正在运行" }
```

### `GET /api/setup/install/stream`

SSE 实时推送安装输出。

| 事件 | 数据 |
|------|------|
| `log` | `{line}` — 一行输出 |
| `status` | 状态快照（回放历史后发一次） |
| `end` | 最终状态快照，随后连接关闭 |

服务端先回放已有日志再跟进增量，中途刷新页面也能看到完整过程；空闲时每约 10 秒发一次 `: ping`。

### `POST /api/setup/install/cancel`

取消正在运行的任务。按进程树终止（`taskkill /T`），只杀 PowerShell 会留下 python、ollama 等孤儿进程。

### `POST /api/setup/install/reset`

清空已结束任务的状态，让用户可以重新发起。

---

### `GET /api/config` / `PUT /api/config`

读取 / 运行期修改运行参数。**只作用于当前进程**，要永久生效请写入 `.env`。
两个方向的字段表完全一致（29 项），响应结构与 `GET` 相同；未传或传 `null` 表示「不动」，
传了但值没变视为空操作，不触发重建/重置副作用。

```json
{
  "llm_model": "llama3.2",
  "llm_temperature": 0.1,
  "llm_num_ctx": 8192,
  "llm_num_predict": 2048,
  "llm_repeat_penalty": 1.2,
  "llm_repeat_last_n": 512,
  "expose_thinking": true,
  "embedding_model_name": "sentence-transformers/all-MiniLM-L6-v2",
  "embedding_device": "cpu",
  "embedding_dimension": 384,
  "chunk_size": 800,
  "chunk_overlap": 120,
  "strip_boilerplate": true,
  "restore_pdf_paragraphs": true,
  "strip_references": true,
  "pdf_min_chars_per_page": 120,
  "top_k": 4,
  "score_threshold": 0.2,
  "score_window": 0.12,
  "score_floor": 0.1,
  "score_relax_limit": 0.35,
  "dedupe_ratio": 0.8,
  "max_context_chars": 6000,
  "summary_max_chunks": 10,
  "max_upload_mb": 50,
  "allowed_extensions": [".txt", ".md"],
  "max_history_turns": 6,
  "backup_before_reindex": true,
  "index_backup_keep": 3
}
```

副作用与校验：

* 改 `llm_model` / 生成参数 → 重置已缓存的 ChatOllama 客户端，并让 Ollama 探测缓存失效；
* 改 `embedding_model_name` / `embedding_device` / `embedding_dimension` → 重置嵌入实例与内存索引，不一致时 `/api/health` 报 `incompatible`、检索 409，**需要重建索引**；
* 改 `chunk_size` / `chunk_overlap` / 解析开关 → 旧索引不重算，`/api/health` 报 `stale` 并建议重建；
* `chunk_overlap >= chunk_size` → **400**（切分器会死循环或报错）；
* `allowed_extensions` 归一化成小写带点（`.txt`）并保序去重，只填空白 → **400**。

---

### `GET /api/models`

列出 Ollama 中已安装的模型。

```json
{
  "models": [
    {
      "name": "deepseek-r1:1.5b",
      "size_bytes": 1100000000,
      "family": "qwen2",
      "parameter_size": "1.5B",
      "quantization": "Q4_K_M",
      "current": true
    }
  ],
  "current": "deepseek-r1:1.5b"
}
```

---

## 文档

### `GET /api/documents`

```json
{
  "documents": [
    {
      "doc_id": "a1b2c3...",
      "filename": "差旅报销办法.pdf",
      "stored_name": "差旅报销办法__a1b2c3d4.pdf",
      "extension": ".pdf",
      "size_bytes": 245678,
      "sha256": "…",
      "chunk_count": 12,
      "char_count": 8642,
      "created_at": "2026-01-01T10:00:00+08:00",
      "status": "indexed",
      "error": null,
      "warnings": []
    }
  ],
  "total": 1,
  "total_chunks": 12
}
```

`warnings` 是给用户看的解析警告（前端显示「⚠ 解析警告」徽标，tooltip 为完整文案）；
目前只有一类：多页 PDF 平均每页字符数低于 `RAG_PDF_MIN_CHARS_PER_PAGE`（默认 120）时判定「疑似扫描件 / 图片版」。

---

### `POST /api/documents/upload`

`multipart/form-data`，字段名 `files`，可重复以一次上传多个。

```bash
curl -X POST http://127.0.0.1:8000/api/documents/upload \
     -F "files=@doc1.pdf" -F "files=@doc2.md"
```

响应是每个文件一项的数组，单个文件失败不会中断整批：

```json
[
  { "document": { "doc_id": "…", "status": "indexed", "chunk_count": 12, "warnings": [] }, "message": "《doc1.pdf》索引完成，共 12 个分块" },
  { "document": { "doc_id": "…", "status": "indexed", "chunk_count": 0, "warnings": ["疑似扫描件 / 图片版 PDF：…"] }, "message": "《scan.pdf》索引完成，共 0 个分块；⚠ 疑似扫描件 / 图片版 PDF：…" },
  { "document": { "doc_id": "", "status": "failed", "error": "不支持的文件类型：.exe" }, "message": "《x.exe》入库失败：…" }
]
```

第二种是「入库成功但有警告」：警告同时出现在 `message` 与 `document.warnings` 里，前端会额外弹一条 toast。

* 支持类型：`.txt .md .markdown .pdf .docx .csv .log .json`
* 大小上限：默认 50MB（`RAG_MAX_UPLOAD_MB`）
* **内容去重**：sha256 相同的文件不会重复索引，直接复用已有记录并返回 `indexed`
* 索引与当前嵌入配置不一致时上传被拒绝（`IndexIncompatibleError`），请先重建索引

---

### `GET /api/documents/{doc_id}/chunks?limit=50`

查看某文档已索引的分块，用于核对切分质量。

```json
{
  "doc_id": "…",
  "filename": "差旅报销办法.pdf",
  "chunk_count": 12,
  "returned": 12,
  "chunks": [
    { "faiss_id": "…", "vector_id": 0, "chunk_index": 0, "page": 1, "char_count": 412, "content": "…" }
  ]
}
```

---

### `POST /api/documents/reindex`

用 `data/uploads/` 里的原始文件按当前解析 / 切分 / 嵌入配置重建整个索引。
改了 `chunk_size`、解析开关或换过嵌入模型后都必须走这一步（旧索引不会自动更新）；知识库为空时返回 `409`。

```json
{
  "documents": 2,
  "rebuilt": 2,
  "failed": 0,
  "chunks_before": 31,
  "chunks_after": 58,
  "index_state_before": "stale",
  "rebuilt_from_scratch": false,
  "backup_dir": "…/data/index/backups/20260101-101500",
  "details": [ { "doc_id": "…", "filename": "a.pdf", "status": "ok", "chunks_before": 12, "chunks_after": 24, "char_count": 8642, "warnings": [] } ]
}
```

* `index_state_before` / `rebuilt_from_scratch`：向量空间没变则逐篇替换向量，变了（维度/模型不同）则先整体清空再重新向量化；
* `backup_dir`：重建前自动备份的目录（`RAG_BACKUP_BEFORE_REINDEX`，保留 `RAG_INDEX_BACKUP_KEEP` 份）；还原时把其中的 `.faiss` / `.pkl` 拷回 `data/index/` 即可；
* `details[].warnings`：本次重新解析得到的面向用户的警告（换了文字版后会自行消失）。

---

### `DELETE /api/documents/{doc_id}`

删除文档及其全部向量。文档不存在返回 `404`。

```json
{ "doc_id": "…", "deleted_chunks": 12, "message": "已删除文档及其 12 个分块" }
```

### `DELETE /api/documents`

清空整个知识库。

---

## 问答

### `POST /api/chat`

**流式**（`stream: true`，默认）返回 `text/event-stream`；**非流式**（`stream: false`）返回一次性 JSON。

请求体：

```json
{
  "question": "住宿费一线城市每晚能报多少？",
  "history": [
    { "role": "user", "content": "之前的问题" },
    { "role": "assistant", "content": "之前的回答" }
  ],
  "top_k": 4,
  "score_threshold": 0.2,
  "doc_ids": ["a1b2c3..."],
  "stream": true
}
```

`doc_ids` 为空或省略时检索全部文档。
状态码：知识库为空 → `409`；索引与当前嵌入配置不一致 → `409`（`detail` 是中文的「去点重建索引」指引）；LLM 不可用 → `503`。
流式路径下的索引不一致以 `error` 事件下发（`stage: retrieve`），不会白等一次推理。

**SSE 事件序列**：`meta` → (`thinking`)\* → (`token`)\* → `sources` → `done`；出错时在任意位置插入 `error` 并结束。

| 事件 | 数据 |
|------|------|
| `meta` | `{model, mode, top_k, score_threshold, effective_threshold, best_score, relaxed, candidates, chunks_total, source_count}` |
| `thinking` | `{delta}` — 推理链增量，可通过 `expose_thinking=false` 关闭 |
| `token` | `{delta}` — 正文增量（已剥离推理链） |
| `sources` | `{sources, cited, thinking}` — `sources` 为召回片段，`cited` 为回答中真正引用的编号 |
| `done` | `{elapsed_ms, cited, answer_chars, usage}` — `usage` 含 `eval_count` 等 |
| `error` | `{message, stage}` — `stage` 为 `retrieve` / `generate` / `stream` |

`source` 对象：

```json
{
  "index": 1,
  "doc_id": "…",
  "filename": "差旅报销办法.pdf",
  "page": 3,
  "chunk_index": 2,
  "score": 0.6865,
  "content": "住宿费一线城市每晚 600 元…"
}
```

`score` 是余弦相似度（向量已归一化，FAISS 用内积检索），越高越相关。

**SSE 原始报文示例**

```
event: meta
data: {"model":"deepseek-r1:1.5b","top_k":4,"source_count":2}

event: token
data: {"delta":"根据文档，"}

event: sources
data: {"sources":[…],"cited":[1],"thinking":"…"}

event: done
data: {"elapsed_ms":3120,"cited":[1],"answer_chars":38,"usage":{"eval_count":42}}
```

> 服务每 10 秒发送一次 `: ping` 注释行作为心跳，客户端应忽略以 `:` 开头的行。

**状态码**

| 码 | 含义 |
|----|------|
| `200` | 正常。流式下错误通过 `error` 事件返回 |
| `409` | 知识库为空，需先上传文档 |
| `422` | 请求体校验失败（如 `question` 为空） |
| `503` | 非流式模式下 Ollama 不可达或模型缺失 |

**典型错误信息**

```
无法连接 Ollama（http://127.0.0.1:11434）。
请确认 Ollama 已启动：运行 scripts\start.ps1（会自动拉起），或手动执行 ollama serve。
原始错误：…
```

---

### `POST /api/search`

**纯检索**，不调用 LLM。用于调参、排错，或把本系统当作语义搜索用。

```json
{ "query": "住宿费标准", "top_k": 4, "score_threshold": 0.2 }
```

```json
{
  "query": "住宿费标准",
  "results": [ { "index": 1, "filename": "…", "score": 0.6865, "content": "…" } ],
  "elapsed_ms": 37
}
```

---

## Python 调用（不起 HTTP 服务）

```python
import sys
sys.path.insert(0, "backend")

from app.services.rag import rag_service

result = await rag_service.answer("住宿费标准是什么？", top_k=4)
print(result["answer"])
for source in result["sources"]:
    print(f"[{source.index}] {source.filename} p.{source.page} ({source.score:.3f})")
```
