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
    "embedding": { "loaded": true, "local_ready": true, "dimension": 384 },
    "vector_store": { "ready": true, "vectors": 42 },
    "index_meta": { "embedding_model": "...", "dimension": 384 }
  }
}
```

`status` 为 `ok` 或 `degraded`。`degraded` 时 `detail.problems` 列出待处理项。

> Ollama 探测结果缓存 5 秒，因此本接口在 Ollama 不可达时也只需约 40ms。

---

### `GET /api/setup?fresh=false`

**首次配置体检**。前端启动时调用本接口；只要 `ready` 为 `false` 就弹出安装引导窗口。

返回的**安装命令由后端按当前实际安装位置生成**（不是前端硬编码），
因此换个目录部署、换台机器，命令依然可直接复制执行。

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

`fresh=true` 会绕过后端对 Ollama 的 5 秒探测缓存，用于用户装完东西后点「重新检测」。
`ready` 的判定与 `/api/health` 的三个字段保持一致：
`ollama_reachable && llm_model_available && embedding_ready`。

**`environment.toolchain`** 是本机工具链探测结果，指引据此「因地制宜」：

```json
{
  "uv":       { "found": true,  "path": "C:\\Users\\me\\.local\\bin\\uv.exe", "source": "PATH" },
  "node":     { "found": true,  "path": "D:\\nodejs\\node.exe", "source": "PATH" },
  "venv":     { "found": true,  "path": "D:\\...\\.venv\\Scripts\\python.exe" },
  "python":   { "path": "...", "version": "3.12.13" },
  "powershell": { "windows_powershell": true, "pwsh": false }
}
```

`environment.can_auto_install` 表示准备脚本是否存在 —— 前端据此决定
是否显示「一键开始安装」按钮。

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

启动安装。等价于用户手动双击 `scripts\prepare.cmd`。

```json
{ "mirror": false, "skip_ollama": false }
```

* `mirror` —— 传给准备脚本的 `-Mirror`，国内镜像加速
* `skip_ollama` —— 只准备 Python 侧，跳过 1.4GB 的 Ollama 下载

**同一时间只允许一个任务**，重复调用返回 `409`：

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

服务端会**先回放已有日志**再跟进增量，所以中途刷新页面或重新打开弹窗
都能看到完整过程，而不是只能看后续输出。空闲时每约 10 秒发一次 `: ping` 心跳。

### `POST /api/setup/install/cancel`

取消正在运行的任务。按**进程树**终止（`taskkill /T`）——
准备脚本下面还有 python、ollama 等子进程，只杀 PowerShell 本身会留下孤儿。

### `POST /api/setup/install/reset`

清空已结束任务的状态，让用户可以重新发起。

---

### `GET /api/config` / `PUT /api/config`

读取 / 运行期修改检索与生成参数。**只作用于当前进程**，要永久生效请写入 `.env`。

`PUT` 请求体（字段都可选）：

```json
{
  "llm_model": "llama3.2",
  "llm_temperature": 0.1,
  "top_k": 4,
  "score_threshold": 0.2,
  "chunk_size": 800,
  "chunk_overlap": 120,
  "expose_thinking": true
}
```

修改 `llm_model` 或 `llm_temperature` 会重置已缓存的 ChatOllama 客户端。

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
      "error": null
    }
  ],
  "total": 1,
  "total_chunks": 12
}
```

---

### `POST /api/documents/upload`

`multipart/form-data`，字段名 `files`，可重复以一次上传多个。

```bash
curl -X POST http://127.0.0.1:8000/api/documents/upload \
     -F "files=@doc1.pdf" -F "files=@doc2.md"
```

响应是**每个文件一项**的数组。单个文件失败不会中断整批：

```json
[
  { "document": { "doc_id": "…", "status": "indexed", "chunk_count": 12 }, "message": "《doc1.pdf》索引完成，共 12 个分块" },
  { "document": { "doc_id": "", "status": "failed", "error": "不支持的文件类型：.exe" }, "message": "《x.exe》入库失败：…" }
]
```

* 支持类型：`.txt .md .markdown .pdf .docx .csv .log .json`
* 大小上限：默认 50MB（`RAG_MAX_UPLOAD_MB`）
* **内容去重**：sha256 相同的文件不会重复索引，直接复用已有记录并返回 `indexed`

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

**流式**（`stream: true`，默认）返回 `text/event-stream`。
**非流式**（`stream: false`）返回一次性 JSON。

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

**SSE 事件序列**：`meta` → (`thinking`)\* → (`token`)\* → `sources` → `done`
出错时在任意位置插入 `error` 并结束。

| 事件 | 数据 |
|------|------|
| `meta` | `{model, top_k, score_threshold, source_count}` |
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

`score` 是**余弦相似度**（向量已归一化，FAISS 用内积检索），越高越相关。

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
