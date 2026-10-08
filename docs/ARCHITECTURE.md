# 架构设计

## 1. 整体数据流

```
                        ┌──────────────── 浏览器（免构建 SPA）────────────────┐
                        │  index.html + app.js + api.js + markdown.js        │
                        └───────┬───────────────────────────────┬────────────┘
                      上传文档  │                               │ 提问（POST + SSE）
                                ▼                               ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│                          FastAPI（backend/app/main.py）                        │
│   /api/documents/*          /api/chat          /api/search      /api/health     │
└───────┬─────────────────────────┬──────────────────┬──────────────────────────┘
        │                         │                  │
        ▼                         ▼                  ▼
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────────┐
│ services/ingest  │   │  services/rag    │   │ services/ollama_client   │
│  解析→切分→入库   │   │  检索→提示词→生成 │   │  探测（TTL 缓存）         │
└────────┬─────────┘   └────────┬─────────┘   └────────────┬─────────────┘
         │                      │                          │
         ▼                      ▼                          ▼
┌──────────────────┐   ┌──────────────────┐        Ollama HTTP API
│ services/loader  │   │ vectorstore      │        /api/tags /api/chat
│ services/registry│◄──┤ (FAISS)          │
└──────────────────┘   └────────┬─────────┘
                                ▼
                     ┌──────────────────────┐
                     │ services/embeddings  │
                     │ all-MiniLM-L6-v2     │
                     └──────────────────────┘

     持久化：data/uploads/（原文件）  data/index/（FAISS + registry.json）
```

---

## 2. 入库流程

```
上传 → save_upload()                     写盘 + 大小/类型校验
     → sha256_of()                       内容指纹
     → registry.find_by_hash()           命中则直接复用，不重复索引
     → parse_document()                  按格式解析 + 版面还原 + 中文友好切分
     → vector_store.add_chunks()         向量化并写入 FAISS，落盘
     → registry.add()                    记录 doc_id → faiss_ids 映射
```

注册表单独存 `faiss_ids`，因为 FAISS 只能按向量 id 删除；`registry.json` 用「临时文件 + `os.replace`」保证原子写。
切分用 `RecursiveCharacterTextSplitter`：段落 → 句号/问号/感叹号 → 分号 → 逗号 → 空格 → 字符，重叠 120 字符，<10 字符的碎片丢弃。

**PDF 解析的版面还原**（解析逻辑变了必须重建索引：`POST /api/documents/reindex`）
PDF 抽出来的是硬换行，直接切分会产生大量结构性噪声。入库前按顺序做三步（`app/services/loader.py`）：

| 步骤 | 做什么 | 不做会怎样（实测同一篇 4 页期刊论文） |
|------|--------|--------------------------------------|
| `strip_repeated_lines()` | 删除出现在 ≥60% 页面上、长度 ≤100 字的重复行（比较时去掉所有空白） | 页眉是「关键词拼盘」，对任何提问相似度都高，top-6 里 3 条是同一段页眉的不同页副本 |
| `strip_reference_sections()` | 截掉独占一行的「参考文献 / References」及其后的编号条目（≥2 条才动手） | 参考文献对「总结主要内容」拿到 0.420（全场第一），却提供不了任何可用信息 |
| `insert_paragraph_breaks()` | 小节标题（`1.1` / `2.2.3` / `0 引言`）与中英文切换处补空行 | 切分器只能在第 800 字硬切，中文正文和英文摘要粘成一块，相似度从 0.367 掉到 0.147 |

---

## 3. 问答流程

```
问题
 ├─ detect_intent()                      规则判定：overview / qa
 │
 ├─ overview（总结类：总结/概述/概括/摘要/主要内容/讲了什么… 且 ≤60 字）
 │    └─ vector_store.overview_chunks()
 │         ├─ 一致性守卫                  索引与当前嵌入配置不符 → 抛可读错误
 │         ├─ 列出全部片段（可选 doc_ids）
 │         ├─ 近重复去重（4-gram Jaccard ≥ 0.8 只留一条）
 │         ├─ 按文档分配名额（每篇保底，其余按块数比例）
 │         ├─ 每篇内部均匀取样（首尾必取）
 │         ├─ 只给取到的片段算相似度        reconstruct 按需取分，不做全库扫描
 │         └─ 按 max_context_chars 装箱 → 忽略相似度阈值
 │
 ├─ qa（其余）
 │    └─ vector_store.search_detailed()
 │         ├─ 一致性守卫                  同上（模型已在内存，判断是硬的）
 │         ├─ embed_query()               384 维归一化向量
 │         ├─ FAISS IndexFlatIP 检索        内积 == 余弦相似度
 │         ├─ 按 doc_ids 过滤（可选）
 │         ├─ 近重复去重                   页眉副本只留最高分
 │         ├─ 相对窗口：score ≥ max(阈值, 最佳−score_window)
 │         └─ 阈值内无命中 → 放宽到 max(score_floor, 最佳−窗口) 并标记 relaxed
 │                              （阈值高于 score_relax_limit 时不擅自放宽）
 │
 ├─ 无召回（且未放宽）→ 直接返回兜底话术，不浪费一次推理
 │
 ├─ build_context()                     拼装 [n] 来源：文件（页码）+ 正文
 │                                      按 max_context_chars 截断
 ├─ build_messages()                    System(规则 + 资料来源) + 历史 + 问题
 │                                      overview 用 OVERVIEW_PROMPT，qa 用 SYSTEM_PROMPT
 │
 └─ ChatOllama.astream()
      ├─ ThinkSplitter 剥离推理链        逐 token 缓冲，处理标签被切碎
      └─ 产出 SSE 事件
           meta    → 模型名、召回数量、参数
           thinking→ 推理链增量（可关闭）
           token   → 正文增量
           sources → 引用片段 + 被真正引用的编号
           done    → 耗时、token 用量
           error   → 可读的错误信息
```

一致性守卫放在「载入索引之后」，因为 `ensure_loaded()` 要用 embeddings 实例 `load_local`，索引载入即模型在内存，维度判断才是硬的。
不兼容时 `/api/chat`、`/api/search` 返回 **409**，而不是让 FAISS 的内部断言变成 500。

**提示词设计（针对 1.5B 小模型）**
约束写死：只依据参考资料、无依据就说不知道、用 `[n]` 标注来源、不复述原文；再用 `max_context_chars` 限制长度，避免超出 `num_ctx` 引发幻觉。

---

## 4. 模块职责

| 模块 | 职责 | 关键设计 |
|------|------|---------|
| `config.py` | 配置 + 离线环境变量引导 | 在任何 HF 相关导入**之前**设置 `HF_HUB_OFFLINE=1`；把 `HF_HOME`/`TMP`/`TEMP` 重定向到项目内 |
| `core/paths.py` | 路径解析 | 以项目根为基准，不依赖 cwd |
| `core/sse.py` | SSE 封装 | 生产者任务 + 队列 + 超时心跳；**不对异步生成器用 `wait_for`** |
| `core/browser.py` | 就绪后自动开浏览器 | 真实 HTTP 探测（2xx 才算就绪、禁用代理）后才 `webbrowser.open`，不靠猜时间 |
| `services/embeddings.py` | 嵌入模型 | 懒加载 + 线程安全；优先本地目录；归一化在此层完成 |
| `services/loader.py` | 解析与切分 | 每种格式产出统一 metadata；PyPDF 的 0 基页码转 1 基；**切分器延迟导入**（顶层导入会连带 torch，冷启动多 9.5 秒）；返回 `ParseOutcome`（分块 + 字符数 + **面向用户的警告**） |
| `services/registry.py` | 文档注册表 | JSON 原子写；记录 faiss_ids 支持精确删除；**登记索引元信息**（模型、实际维度、切分参数、解析开关、建立时间），清空时一并清掉 |
| `services/index_health.py` | 索引一致性体检 | 比对「索引登记的参数」与「当前配置」：向量空间不符 → `incompatible`（拒绝检索），切分/解析漂移 → `stale`（可用但提示重建） |
| `services/vectorstore.py` | FAISS 管理 | `IndexFlatIP`；写操作加锁、读操作无锁；删除前先过滤不存在的 id；**检索前先做一致性守卫**；概览取样只对目标片段 `reconstruct` 打分 |
| `services/ingest.py` | 入库流水线 | 去重、失败隔离、单文件失败不影响整批；重建索引前自动备份（`backup_index()`） |
| `services/rag.py` | RAG 核心 | 检索、提示词、流式生成、推理链解析（内联标签 + 原生通道）、错误翻译 |
| `services/ollama_client.py` | Ollama 探测 | 单次请求 + 2 秒超时 + 5 秒 TTL 缓存（能力缓存 60 秒，且在切换模型 / 刷新模型列表 / 安装结束 / 重新检测时主动失效）；模型能力（`thinking`）与显存预热 |
| `services/setup.py` | 首次配置体检 | 检测缺失组件，按实际安装位置生成可复制的安装命令 |
| `services/installer.py` | 一键安装 | 以子进程跑准备脚本，日志重定向到文件后增量推送给前端 |
| `scripts/lib/ollama-runtime.ps1` | 便携版完整性 / 生命周期工具 | 纯函数：判定推理运行时是否齐全、zip 是否完整、**停掉占用进程后删除并确认**、**退出时按 PID/路径/端口三重线索收掉 Ollama**；被 prepare/start/stop 共用，也被测试直接调用 |
| `routers/*` | HTTP 接口 | 只做参数校验与编排，业务逻辑都在 services |

**分层原则**：`routers` 薄、`services` 厚；业务逻辑不依赖 FastAPI，所以 `test_e2e.py` 能直接调 `rag_service.stream()`。

---

## 5. 前端设计

**为什么不用 React/Vite**
离线部署要「拷过去就能跑」：免构建没有 `node_modules`、没有构建产物与源码不同步、也不会因 npm 源不可达卡住。

**模块划分**

| 文件 | 职责 |
|------|------|
| `api.js` | 全部 HTTP 调用；SSE 手工解析（`EventSource` 只支持 GET，而问答是 POST） |
| `markdown.js` | 极简 Markdown 渲染 + HTML 转义 + 引用角标 |
| `app.js` | 状态、渲染、事件绑定 |

**流式渲染节流**：token 到达频率高，每个都重渲染会卡，`app.js` 用 90ms 定时器合并渲染，期间新 token 只更新字符串。

**安全**：`markdown.js` **先转义 HTML 再套用 Markdown 规则**，所以 `<script>` 只会显示为纯文本；链接只允许 `http/https`。

---

## 6. 离线的实现方式

```python
HF_HUB_OFFLINE=1          # huggingface_hub 不再发起任何网络请求
TRANSFORMERS_OFFLINE=1    # transformers 只从本地加载
HF_HOME=<项目>/.hf-cache  # 缓存重定向，保证自包含
TMP/TEMP=<项目>/.tmp      # 临时文件重定向
```

嵌入模型优先从 `models/<模型名>` 加载，离线模式下找不到本地目录时 `embeddings.py` 抛**带操作指引**的错误。
Ollama 与 LLM 也是纯本地：`OLLAMA_MODELS` 指向 `models/ollama`，模型文件随项目走。

---

## 7. 扩展点

| 需求 | 改动位置 |
|------|---------|
| 支持新文档格式 | `services/loader.py` 的 `_PARSERS` 注册一个解析函数 |
| 换嵌入模型 | 改 `.env` 的 `RAG_EMBEDDING_MODEL_NAME` + `RAG_EMBEDDING_DIMENSION`，**重建索引** |
| 换 LLM | 改 `.env` 的 `RAG_LLM_MODEL`（或前端运行时切换） |
| 换向量库 | 替换 `services/vectorstore.py`，保持 `add_chunks/delete_ids/search` 接口 |
| 多用户/鉴权 | 在 `main.py` 加中间件；`registry` 需要按用户分区 |
| 混合检索（BM25 + 向量） | 在 `vectorstore.search()` 后做倒数排名融合（RRF） |

---

## 8. 性能特征（实测）

| 项目 | 实测值 | 说明 |
|------|--------|------|
| 端口开始监听 | 0.6 秒 | 导入链已经过瘦身（见下） |
| `/api/health` 首次 200 | 0.8 秒 | lifespan 不阻塞、且不依赖嵌入模型 |
| 嵌入模型加载 | 0.3 ~ 7 秒 | 在后台线程；取决于文件是否在系统页缓存中 |
| `/api/health` 延迟 | 约 40ms | TTL 缓存命中；首次约 2 秒 |
| 单次提问延迟 | 取决于 LLM | 1.5B 模型 CPU 上通常数秒；检索本身 <100ms |
| 索引规模 | 实测 5 向量 | `IndexFlatIP` 是暴力检索，万级向量仍在毫秒级 |

### 启动顺序（关系到用户体验，不能随意调整）

```
start.cmd → start.ps1
              ├─ 环境检查 / 按需拉起 ollama serve
              ├─ 设置 RAG_OPEN_BROWSER_URL = http://127.0.0.1:<端口>
              └─ 前台运行 uvicorn
                    ├─ 导入 app.main            0.6s（必须保持轻量！）
                    ├─ lifespan 启动，创建三个后台任务
                    │     ├─ 预热嵌入模型 + 预热切分器 + 恢复索引（十几秒）
                    │     ├─ 预载 LLM 进显存（Ollama 懒加载，冷启动约 100 秒）
                    │     └─ 轮询目标地址，拿到 2xx 才打开浏览器
                    ├─ 绑定端口并开始服务        ← 0.6s
                    └─ 后台任务跑完后写日志「知识库就绪 / LLM 预热完成」
```

两条硬约束：**导入链上不能有重量级依赖**（`langchain_text_splitters` 连带 torch，单这一条 9.5 秒，须延迟到 `loader._get_splitter_class()`）；**开浏览器只能由实际探测决定**（延时开浏览器在冷启动机器上必弹 `ERR_CONNECTION_REFUSED`）。
`IndexFlatIP` 复杂度 O(n)，十万分块级别应换成 `IndexIVFFlat` 或 `HNSW`（`vectorstore.py` 中 `from_documents` 处替换）。

---

## 9. 首次配置引导的设计

### 要解决的问题

缺组件时原来只显示一个不起眼的状态点，用户提问后拿到「无法连接 Ollama」，却不知道该装什么、装完放哪里。

### 为什么检测逻辑放后端

安装命令里全是部署相关路径（项目、`ollama.exe`、脚本、模型各在哪）；写死在前端就会换目录即失效、改配置不跟变。
所以 `services/setup.py` 按当前进程实际路径生成命令，前端只渲染，`tests/test_http.py` 还会断言命令里没有未替换的占位符。

### 状态机

```
venv 缺失?           → blocking: venv_missing          （附「一键准备」与「只装依赖」）
嵌入模型缺失?         → blocking: embedding_missing     （附「一键准备」与「只下模型」）
找不到 ollama 二进制?
    ├─ 是 →            blocking: ollama_not_installed   （三种获取方式）
    └─ 否，但内置包缺推理引擎? → blocking: ollama_incomplete （重跑一键准备 / 官方安装包 / 手动解压）
           └─ 服务不可达? → blocking: ollama_not_running   （用脚本拉起 / 手动 serve）
                  └─ 可达但没模型? → blocking: llm_model_missing （pull 指定模型 / 换已有模型）
```

关键点一：**「没装」和「装了没启动」要区分开** —— 都表现为 `ollama_reachable == false`，但前者要下载、后者只要 `serve`。

关键点二：**「装了」还要区分「装全了没有」**：便携版 zip 里除 `ollama.exe` 外还有 `lib\ollama\` 的一整套推理运行时，解压中断会留下表现极具欺骗性的假安装：

| 检查手段 | 假安装的结果 |
|---------|------------|
| `ollama.exe` 是否存在 | ✅ 存在 |
| `ollama serve` 能否启动 | ✅ 能 |
| `/api/tags` 能否列出模型 | ✅ 能 |
| 真正生成回答 | ❌ `error starting llama-server: llama-server binary not found` |

因此 `setup.portable_ollama_status()` 直接检查 `lib\ollama\llama-server.exe`，`/api/health` 也把它列进 `problems`；非便携版布局返回「无法判断」而非误报。

### 每种方式都要给全

| 方式 | 适合谁 | 代价 |
|------|--------|------|
| 项目自动准备 | 大多数人 | 下载 1.4GB，但模型随项目走 |
| 官方安装包 | 已有 Ollama 环境 | 模型在用户目录，拷离线机器要额外处理 |
| 手动解压便携版 | 网络受限、想自己控制 | 需要理解 `OLLAMA_MODELS` 的作用 |

第三种方式要说明：`ollama pull` 只是请求**正在运行的** `serve` 进程，模型存到哪由它的 `OLLAMA_MODELS` 决定。

### 前端的克制

* **每次会话最多自动弹一次**，「不再自动提示」写 localStorage 永久生效
* 关掉后左侧保留常驻入口；「重新检测」带 `?fresh=true`，绕过后端 5 秒探测缓存
* 检测通过后自动关闭弹窗并把用户带回对话
* 命令块用 `textContent` 而非 `innerHTML` 写入；复制对 `navigator.clipboard` 做了回退

### 一键安装：为什么不捕获管道输出

`services/installer.py` 把子进程的 stdout/stderr **直接重定向到 `data/logs/install.log`**，而不是用管道捕获：

| | 管道捕获 | 重定向到文件 |
|---|---|---|
| 受限环境 | 部分环境禁止匿名管道 | 只用到文件句柄，处处可用 |
| 后端重启 | 管道内容丢失，进度全没了 | 日志还在，重新连上就能接着看 |
| 中途接入 | 只能看后续输出 | 可回放历史，刷新页面不丢上下文 |
| 输出落盘 | 需要自己再写一份 | 天然就是日志文件 |

读取日志用「记住字节偏移 + 增量读」，因此**必须按字节推进**：子进程可能写出半行，末尾多字节字符截断成 U+FFFD 后重编码是 3 字节，按字符串长度回退会让偏移量持续漂移。
回放历史时行内容和偏移量必须在同一次读取里确定（`replay()` 返回两者）；写入端同理，先写头部、再以追加模式打开句柄，否则子进程从偏移 0 写会覆盖头部。

**并发与取消**：同一时间只允许一个任务（重复调用返回 409）；取消用 `taskkill /T` 按进程树终止，只杀 PowerShell 会留下 python、ollama 等孤儿。

### 下载源策略（实测决定）

便携版 Ollama 有 1.36GB，选错源就是几十分钟到几小时的差别：

| 来源 | 实测速度 | 1.36GB 耗时 |
|------|---------|------------|
| `github.com/.../latest/download/...` | 连接被重置 | 不可用 |
| `api.github.com` → `release-assets.githubusercontent.com` | 3.0 MB/s | 约 8 分钟 |
| `ghproxy.net` 镜像 | 291 KB/s | 约 82 分钟 |

因此 `prepare.ps1` 按 **API 解析 → 镜像 → 直连** 依次尝试（`-Mirror` 提前镜像），换源时丢弃上一个源的 `.part`；`fetch.mjs --github-asset` 里 `latest` 必须映射到 `/releases/latest`（`/releases/tags/latest` 会 404）。
执行顺序是**先下载、后替换**，避免「下载失败 + 旧文件已删」的双输局面。

## 10. 前端检查：静态一致性 + 真实交互

1. **静态一致性**（`tests/test_frontend.mjs`）—— 正则校验 `app.js` 里每个 `$('xxx')` 都能在 `index.html` 找到对应 `id`、`import` 的符号确实被导出，并断言前端**不引用任何外部 CDN**。
2. **真实交互**（`tests/test_frontend_interaction.mjs`）—— 把真实的 `app.js` 装进 `tests/dom-lite.mjs`（手写极简 DOM），用假的 `fetch` / `XMLHttpRequest` / SSE 喂数据，断言点击、输入、流式渲染之后**真正发生了什么**。

垫片只做到「够跑这个前端」，并实现了 HTML 规范的 *click in progress* 标志（否则「点投放区 → `fileInput.click()`」会沿冒泡无限递归）；它需要 `frontend/package.json` 里的 `"type": "module"`。

## 11. 脚本编码的自动化预检

`tests/run_all.ps1` 跑测试之前先预检两条规则：

* **`.ps1` 必须带 UTF-8 BOM** —— 否则 Windows PowerShell 5.1 按 GBK 解析，中文错位吃掉引号，报 `Unexpected token`
* **`.cmd` 必须纯 ASCII** —— 否则 `cmd.exe` 按 OEM 代码页读取，注释碎片会被当成命令执行，脚本一行都跑不到

这两类故障都出现在启动阶段、报错毫无指向性，放进测试套件后任何一次编辑破坏编码都会立刻被拦住。
