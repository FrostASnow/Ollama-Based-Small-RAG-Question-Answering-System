# 离线 RAG 文档问答

一个**完全离线**运行的小型文档问答系统。上传 PDF / Word / Markdown / TXT 等文档，
用自然语言提问，回答会标注引用来源并可点开查看原文片段。

所有计算都在本机完成：不调用任何云端 API，不上传任何数据，断网可用。

---

## 技术栈

| 层次 | 选型 | 说明 |
|------|------|------|
| 编排框架 | **LangChain** | 文档加载、切分、提示词装配 |
| 本地推理 | **Ollama** + `deepseek-r1:1.5b`（默认） | 约 1.1GB，带推理链；可换 `llama3.2`、`qwen2.5:3b` 等 |
| 向量检索 | **FAISS** (`IndexFlatIP`) | 内积检索，向量已归一化 ⇒ 等价于余弦相似度 |
| 文本嵌入 | **HuggingFace `all-MiniLM-L6-v2`** | 384 维，约 90MB，CPU 上毫秒级 |
| 后端 | **FastAPI** + Uvicorn | REST + SSE 流式输出 |
| 前端 | **原生 HTML/CSS/ES Module** | 零 npm 依赖、零构建步骤，打开即用 |

> 前端刻意不用 React/Vite：离线场景下，免构建意味着**拷过去就能跑**，
> 不存在 `node_modules`、构建产物与源文件不一致的问题。

---

## 目录结构

```
rag-qa/
├── README.md                   本文件
├── .env.example                配置模板（复制成 .env 生效）
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── main.py             FastAPI 入口，同时托管前端静态文件
│       ├── config.py           配置与离线环境变量引导
│       ├── schemas.py          请求/响应模型
│       ├── core/
│       │   ├── paths.py        项目路径解析
│       │   ├── logging.py      日志
│       │   └── sse.py          SSE 流式封装（带心跳）
│       ├── services/
│       │   ├── embeddings.py   all-MiniLM-L6-v2 本地加载
│       │   ├── loader.py       文档解析与中文友好切分
│       │   ├── registry.py     文档注册表（JSON 原子写）
│       │   ├── vectorstore.py  FAISS 索引管理
│       │   ├── ingest.py       入库流水线
│       │   ├── rag.py          检索 + 提示词 + 流式生成
│       │   └── ollama_client.py Ollama 探测（带 TTL 缓存）
│       └── routers/
│           ├── health.py       健康检查、配置、模型列表
│           ├── documents.py    上传、列表、分块预览、删除
│           └── chat.py         问答（SSE/JSON）与纯检索
├── frontend/                   免构建前端
│   ├── index.html
│   └── assets/
│       ├── css/style.css
│       └── js/{app.js, api.js, markdown.js}
├── scripts/
│   ├── prepare.ps1 / .cmd      一次性联网准备（装依赖、下模型）
│   ├── start.ps1 / .cmd        离线启动（自动拉起 Ollama）
│   ├── stop.ps1                停止服务
│   ├── download_models.py      下载 HuggingFace 嵌入模型
│   └── fetch.mjs               通用下载器（支持断点续传）
├── tests/                      5 套测试，共 175 项断言
├── models/                     本地嵌入模型（离线加载）
├── data/                       上传文件、FAISS 索引、日志
├── tools/                      便携版 Ollama（可选）
└── docs/{ARCHITECTURE.md, API.md}
```

---

## 快速开始

### 前置条件

* Windows 10/11
* 磁盘可用空间 ≥ 6GB（依赖约 1GB + 嵌入模型 90MB + Ollama 与 LLM 约 3GB）
* 内存 ≥ 8GB（推荐 16GB）
* 首次准备需要联网；之后可完全离线

> **关于 PowerShell 版本**
> 下面的命令用 `pwsh`（PowerShell 7）书写。本项目所有 `.ps1` 脚本都已存为
> **UTF-8 带 BOM**，因此在系统自带的 **Windows PowerShell 5.1** 下也能正确显示中文。
> 如果没有装 PowerShell 7，把命令里的 `pwsh` 换成 `powershell` 即可，
> 或者直接**双击 `scripts\*.cmd`**（它会自动优先用 pwsh，没有则回退到 powershell）。

### 第一步：一次性联网准备

```powershell
pwsh -File scripts\prepare.ps1
```

或直接双击 `scripts\prepare.cmd`。

脚本会依次完成：

1. 准备 Python 3.12（优先用 [uv](https://docs.astral.sh/uv/)，免管理员、免安装）
2. 创建 `.venv` 并安装 97 个依赖
3. 下载 `all-MiniLM-L6-v2` 到 `models/`（约 90MB）
4. 下载便携版 Ollama 到 `tools/ollama/`，并拉取 `deepseek-r1:1.5b`（约 1.5GB + 1.1GB）

常用参数：

```powershell
pwsh -File scripts\prepare.ps1 -Mirror              # 国内镜像加速
pwsh -File scripts\prepare.ps1 -SkipOllama          # 只用检索，不做问答
pwsh -File scripts\prepare.ps1 -LlmModel llama3.2   # 换 LLM
```

> 如果 Ollama 下载太慢，可以跳过它，自行从 <https://ollama.com/download> 安装，
> 再执行 `ollama pull deepseek-r1:1.5b`。启动脚本会自动发现系统里的 `ollama`。

### 不想看脚本输出？直接启动，界面会教你

即使**完全跳过第一步**，也可以直接 `scripts\start.cmd`。前端启动时会自动做一次配置体检：

* 检测到缺组件 → **自动弹出引导窗口**，逐项说明缺什么、影响是什么
* 顶部先列出**本机环境探测结果**（uv / Python 环境 / 嵌入模型 / Ollama 各自装在哪），
  指引基于真实路径生成，而不是写死的模板
* 每项都给出**多种安装方式**（推荐方案带标注），命令里是**你这台机器的真实绝对路径**
* **一键开始安装**：点一下按钮，后端直接替你跑准备脚本，
  网页上实时滚动日志，可随时关掉窗口让它后台运行、稍后回来看进度
* 安装失败时不会只丢一句 WARN —— 会给出可操作的出路：
  **重新安装 / 用国内镜像重试 / 查看手动命令**
* 命令也可一键复制；装完后点「重新检测」即可，无需重启后端
* 关掉后左侧会保留一个「配置未完成」入口，随时可以再打开

也就是说：**先启动、后配置**也是可行路径，不必提前读完文档。

### 第二步：启动

```powershell
pwsh -File scripts\start.ps1
```

或双击 `scripts\start.cmd`。浏览器会在服务**真正就绪之后**自动打开 <http://127.0.0.1:8000>
（不会再出现「先弹一个『127.0.0.1 拒绝连接』、几秒后刷新才好」的观感）。

启动脚本会：

* 检查 `.venv` 与嵌入模型是否就绪（缺失时给出明确指引，而不是莫名报错）
* 若 Ollama 未运行，自动以 `ollama serve` 拉起
* 启动后端（后端同时托管前端）
* 把访问地址通过 `RAG_OPEN_BROWSER_URL` 交给后端；后端**真的能用 HTTP 拿到 200** 时才打开浏览器

按 `Ctrl+C` 停止；脚本会一并关闭它拉起（以及正在使用）的 Ollama。
若你另外还用 Ollama 跑别的东西，加 `-KeepOllama` 保留它：`scripts\start.cmd -KeepOllama`。

### 第三步：使用

1. 左侧**拖入或点击上传**文档，等待索引进度完成
2. 在底部输入框提问，回答会**逐字流式输出**
3. 回答中的 `[1]` `[2]` 角标可点击，直接查看对应原文片段
4. 左侧勾选文档，可把检索范围限定在选中的文档内
5. 点击「系统状态」查看 Ollama、模型、索引的实时情况

---

## 部署到离线机器

准备完成后，整个 `rag-qa` 目录就是自包含的：

```powershell
# 在联网机器上打包（排除可再生的缓存）
Compress-Archive -Path rag-qa -DestinationPath rag-qa-offline.zip

# 拷到离线机器，解压后直接启动
pwsh -File scripts\start.ps1
```

需要一并拷贝的内容：`.venv/`、`models/`、`tools/`（若用便携版 Ollama）、`data/`（若想保留已索引的文档）。

> `.venv` 里的路径是相对的，但 Python 解释器本身在 `.uv/python/` 下。
> 如果准备时用的是 uv 装的项目内 Python，请把 **`.uv/` 目录也一起拷贝**，
> 或改用 `-SkipPython` + 系统 Python 创建 `.venv`。

---

## 配置

把 `.env.example` 复制为 `.env` 即可覆盖默认值。常用项：

```ini
# 换 LLM（需先 ollama pull）
RAG_LLM_MODEL=llama3.2

# 换中文嵌入模型（必须重建索引！）
RAG_EMBEDDING_MODEL_NAME=BAAI/bge-small-zh-v1.5
RAG_EMBEDDING_DIMENSION=512

# 召回数量与相似度阈值
RAG_TOP_K=4
RAG_SCORE_THRESHOLD=0.20

# 分块大小
RAG_CHUNK_SIZE=800
RAG_CHUNK_OVERLAP=120
```

完整说明见 `.env.example`（每一项都有中文注释）。

前端「检索参数」里的滑块是**运行期**调整，立即生效但不写回 `.env`。

---

## 测试

```powershell
pwsh -File tests\run_all.ps1
```

| 套件 | 断言数 | 依赖 Ollama | 覆盖内容 |
|------|-------|------------|---------|
| `check_imports.py` | 24 | 否 | 全部依赖能否按预期路径导入（含切分器的延迟导入路径） |
| `test_offline.py` | 24 | 否 | 模型完整性、离线加载、归一化、FAISS 检索、切分 |
| `test_e2e.py` | 76 | 否（假 LLM） | 入库 → 检索 → 提示词 → 流式事件 → 引用 → 删除 → 持久化 → 安装日志解析 → **冷启动导入链 + 浏览器打开时机 + Ollama 安装完整性 + 原生 thinking 通道** |
| `test_ollama_integration.py` | 45 | 否（协议兼容假服务） | 真实 ChatOllama 往返、NDJSON 流解析、thinking 能力探测、模型预热与退出卸载、错误分支、状态自洽 |
| `test_http.py` | 102 | 否 | 真实 HTTP 服务：静态托管、上传、检索、SSE 协议、错误码、配置引导、一键安装、JSON charset |
| `test_frontend.mjs` | 56 | 否（需 Node） | 前端渲染（XSS 转义、引用角标）+ 静态一致性（DOM id、模块导出、`[hidden]` 兜底、无 CDN、环境面板三态） |

合计 **327 项断言**，外加一个脚本编码/语法与安装链路逻辑的预检。`test_http.py` 里有 1 条断言
只在 `/api/health` 确实报出问题时才执行（断言这时 `/api/setup` 必须同样判为未就绪）。
全部套件都**不需要真实 Ollama**：
第 4 套用一个实现了 Ollama 线格式的假服务，保留真实的 ChatOllama 与 HTTP 客户端，
因此验证的是真实集成代码，而不是把整层替换掉的 mock。
除 `test_http.py` 需要后端在跑（运行器会自动拉起并在结束后关闭）外，其余都可独立执行。

---

## 实现要点

这一节记录几个**不做就会出问题**的细节。

### 1. 推理链标签不能用字面量写

`deepseek-r1` 的推理链标签在部分工具链中属于分词器特殊 token，直接以字面量写进源码
会被改写成语义等价但**无法匹配**的字符，导致推理链解析静默失效。
因此 `rag.py` 里用 `chr()` 拼接标签：

```python
_LT, _GT, _SLASH = chr(60), chr(62), chr(47)
THINK_OPEN = _LT + "think" + _GT
```

### 2. 标签会被逐 token 切碎

小模型流式输出时标签可能被拆成 `<th` + `ink>`。`ThinkSplitter` 会在缓冲区里
**暂扣可能是标签前缀的尾部字符**，等后续 token 补齐再判断，否则标签会漏进正文。

### 3. 统计信息在最后一个空 chunk 上

流式的最后一个 chunk `content` 为空，只带 `eval_count` / `done_reason`。
若把元数据收集放在 `if not content: continue` 之后，token 统计将永远为空。
`rag.py` 中先收元数据、再处理内容。

### 4. 健康检查不能串行等两次超时

Ollama 不可达时，连接要等超时才返回。前端启动第一个请求就是 `/api/health`，
如果每次串行等两次超时（约 8 秒），界面会明显卡顿。
现在只发一次请求、超时压到 2 秒、并加 5 秒 TTL 缓存，延迟稳定在 **40ms** 左右。

### 5. 冷启动放到后台

加载 `sentence-transformers` 会连带导入 torch，冷启动需十几到几十秒。
放在 lifespan 里同步等待会让端口迟迟不监听，用户看到的是「无法连接」。
现在预热与索引恢复都在 `asyncio.to_thread` 后台任务中执行。
再叠加第 19 条（把 `langchain_text_splitters` 从顶层导入挪走）之后，
实测 **端口 0.6 秒开始监听、0.8 秒就能返回 `/api/health` 200**。

### 6. 归一化只做一次

归一化在 Embeddings 层完成（`normalize_embeddings=True`），
FAISS 侧**不传** `normalize_L2` —— 在 `MAX_INNER_PRODUCT` 下它是空操作，
且 langchain 会打印告警。这样内积直接等于余弦相似度，分数落在 [-1, 1]。

### 7. 删除向量前先过滤

注册表与索引可能因异常中断而不一致。把不存在的 id 直接交给 FAISS 会抛错，
导致**整批删除失败**（连正常的部分也删不掉）。`delete_ids()` 会先与
`index_to_docstore_id` 求交集。

### 8. SSE 心跳不能对异步生成器用 wait_for

对 `__anext__()` 施加超时会取消正在 await 的协程，进而关闭整个异步生成器，
导致流式回答被截断。`core/sse.py` 改用「生产者任务 + 队列 + 超时读队列」的模式。

### 9. 全部缓存重定向到项目内

`HF_HOME`、`TMP`、`TEMP` 都指向项目目录。既是自包含部署的需要，
也避免在受限环境（系统临时目录不可写）下运行失败。

### 10. `.cmd` 文件必须纯 ASCII

`cmd.exe` 按 **OEM 代码页**（中文 Windows 上是 936）读取 `.cmd` 文件。
如果文件是 UTF-8 且含中文，字节会被按 GBK 重新分组，注释行被撕成碎片，
cmd 反过来把这些碎片当成命令去执行：

```
'鈥使鈥?PowerShell' 不是内部或外部命令
'pwsh' 不是内部或外部命令
```

**脚本根本跑不起来** —— 这是最容易被误判成「项目有 bug」的一类故障。
所以 `start.cmd` / `prepare.cmd` 全部改成纯 ASCII，中文提示一律交给 `.ps1` 输出；
`.ps1` 用 UTF-8 带 BOM 保存，并在开头把控制台切到 UTF-8
（`[Console]::OutputEncoding` 会连带调用 `SetConsoleOutputCP`，不会出现编码错配）。

### 11. JSON 响应必须声明 charset

Starlette 默认只给 `text/*` 追加 `charset`，`application/json` 不带。
Windows PowerShell 5.1 的 `Invoke-RestMethod` 遇到没有 charset 的 JSON 会按
**ISO-8859-1** 解码，中文全部变成乱码。
`main.py` 用 `UTF8JSONResponse` 作为 `default_response_class`，
并覆盖了 `HTTPException` / `RequestValidationError` 的处理器
—— FastAPI 内置的处理器直接构造 `JSONResponse`，会绕过默认类，
而我们 404/409/503 的错误信息里都是中文。

### 12. 端口占用要分两种情况

「自己的实例已经在跑」和「端口被别的程序占了」，用户要做的事完全不同。
先做本机 TCP 探测（端口空闲时瞬间返回，不给启动增加负担），
确认被占用后再调 `/api/health` 判断是谁。反过来先调 health 的话，
每次启动都要白等一次 HTTP 往返；而超时留短了（比如 2 秒，
首次探测 Ollama 本身就要 2 秒）还会把自己的实例误判成别人的。

### 13. 启动脚本不能依赖 PowerShell 作业机制

最初用 `Start-Job` 在后台等就绪后开浏览器。但 `$ErrorActionPreference='Stop'`
下一旦作业机制不可用（部分受限环境如此），整个启动流程就被中断。
现在改成：uvicorn 前台运行（日志直接进本窗口、Ctrl+C 自然传递），
浏览器交给一个**分离的隐藏进程**延时打开，与主流程完全解耦，失败也不影响服务。

### 14. `Get-Command` 的结果没有 `.FullName`

PowerShell 里 `Get-Command uv` 返回的是 `ApplicationInfo`，路径在 **`.Source`** 上。
误用 `.FullName` 会**静默拿到空字符串** —— 没有报错，只是后面 `if ($uvCmd)`
判空失败，脚本悄悄走到「没有 uv」的分支，回退到 `python -m pip install`，
而 uv 创建的 venv 默认**不含 pip**，最后报一句 `No module named pip`，
让人完全看不出真正的原因。`prepare.ps1` 现在统一用 `Resolve-Uv`
解析成字符串路径，并覆盖 PATH 之外的常见安装位置。

同时给 `uv venv` 加了 `--seed`（让 venv 自带 pip），并补了 `ensurepip` 兜底 ——
三层保险，任何一层失效都不会再卡在「没有 pip」上。

### 15. PS 5.1 里重定向原生命令的 stderr 会中断脚本

`$ErrorActionPreference = 'Stop'` 时，只要对原生命令做 `*> $null` 或 `2>&1`，
PowerShell 5.1 就会抛 `NativeCommandError` 并把整个脚本打断 ——
而「探测 pip 在不在」这类操作恰恰既需要重定向、又预期会失败。
`prepare.ps1` 用 `Test-NativeSuccess` 包一层，临时放宽偏好设置。

### 16. 写日志的两个写者不能各自维护文件位置

一键安装把子进程输出重定向到 `data/logs/install.log`。最初先用 `"wb"` 打开句柄
（位置在 0）交给子进程，再用追加模式写服务端自己的头部 —— 结果子进程第一笔输出
就从偏移 0 开始，把头部整段覆盖，日志里出现 `g-qa` 这种被啃掉前半截的碎片。
现在改成：**先写完头部、再以 `"ab"` 打开句柄**，两个写者都只追加。

日志读取也必须**按字节**推进偏移量：子进程随时可能写出半行，末尾的多字节字符
被截断后解码成替换字符（U+FFFD），而它重新编码是 3 字节，与原始残缺序列长度
不同 —— 偏移量会持续漂移，最终导致整段重复或错位。

### 17. `display` 会盖掉 `hidden` 属性

浏览器默认样式是 `[hidden] { display: none }`，特异性只有 **0-1-0**。
只要任何一条类选择器里写了 `display`（`.modal` 是 `grid`、`.sources` 是 `flex`、
`.setup-banner` 是 `flex`……），两者特异性相同、**后写的赢** ——
`el.xxx.hidden = true` 设了也白设，元素依然显示。

症状极具迷惑性：**点了「后台运行」按钮毫无反应** —— 看起来像事件没绑上，
其实是弹窗根本没被关掉。

修法是加一条钉死的兜底规则，比逐个给 `[hidden]` 写变体可靠得多：

```css
[hidden] { display: none !important; }
```

`tests/test_frontend.mjs` 会断言这条规则存在，并检查 `.modal` / `.sources`
等已知冲突点。

### 18. 进度行必须覆盖而不是追加

`curl` 的进度表和 `tqdm` 的进度条都用 `\r` 回到行首重绘。
日志解析如果按 `\r` 逐段拆成独立行，一次 1.4GB 下载就能刷出上千行
`0  0  0  0  0`，真正的错误信息被冲出视野 —— 用户看到的就是满屏噪音。

现在按**终止符**区分两种行：

* `\n` 结尾 → 普通行，追加到日志
* `\r` 结尾 → 进度行，前端**覆盖上一行**

并做了两层收敛：回放历史时同一段进度只保留最后一帧；
实时推送时内容没变就丢弃、变化太频繁也节流（约 300ms 一条），
但**每一轮的最后一条一定发出**，避免界面停在过时的进度上。

另外下载顺序也调整成 **node + `fetch.mjs` → curl（`-sS`）→ Invoke-WebRequest**：
`fetch.mjs` 的进度是干净单行且支持断点续传（1.4GB 很需要），
curl 则必须加 `-sS` 关掉那张进度表。

### 19. 顶层导入 `langchain_text_splitters` 会拖慢整个冷启动

`langchain_text_splitters` 的包 `__init__` 会**立刻**导入
`SentenceTransformersTokenTextSplitter`，于是整个 `sentence_transformers`
（含 `transformers`、`torch` 以及全部 loss / trainer 子模块）被一起拉起来：

| 语句 | 实测耗时 |
|------|---------|
| `import langchain_core.documents` | 0.12s |
| `import langchain_text_splitters` | **9.49s** |
| `import langchain_text_splitters`（`sentence_transformers` 已在内存） | 0.23s |

它原本写在 `services/loader.py` 顶层，而导入链是
`app.main -> routers.documents -> services.loader`，因此**端口要 10 秒后才开始监听**。

现在改成首次真正需要切分时才导入（`_get_splitter_class()`），
并在后台预热里、嵌入模型加载完成之后补上 `loader.preload()` ——
导入 `app.main` 从 **10.14s 降到 0.57s**，端口 **0.6s** 开始监听，
而补导入只花 0.23s，用户第一次上传文档不会有额外等待。

`tests/test_e2e.py` 第 9 节会在独立子进程里断言
「导入 `app.main` 后 `sys.modules` 中不含 `sentence_transformers` / `torch` /
`langchain_text_splitters` / `faiss`」且耗时 < 6s，防止这个坑被重新踩回去。

### 20. 打开浏览器不能靠猜时间

后端没监听之前开浏览器，用户看到的第一屏就是
**「嗯… 无法访问此页面 / 127.0.0.1 拒绝连接 / ERR_CONNECTION_REFUSED」**，
而 Chrome 的错误页不会自己重试，很容易被判定成「启动失败」。

原先 `start.ps1` 是「分离一个 `cmd /c ping -n 4` 等 3 秒再 `start` 浏览器」，
时间点纯靠猜；后端冷启动要 9 秒时必然对不上。

现在 `start.ps1` 只把地址交给后端：

```powershell
$env:RAG_OPEN_BROWSER_URL  = $BaseUrl   # 真实访问地址（含 -Port 指定的端口）
$env:RAG_AUTO_OPEN_BROWSER = '1'        # -NoBrowser 时置 0：只记录地址，不真的打开
```

后端在 lifespan 里调度 `core/browser.py` 的任务，**用真实 HTTP 请求探测目标地址**，
拿到 2xx 之后才调用 `webbrowser.open` —— 判断依据从「猜时间」变成「真的能响应」，
换端口、换机器、机器快慢都不影响。探测显式禁用代理解析
（否则设了 `HTTP_PROXY` 的环境会把 127.0.0.1 的请求也交给代理），
超时 120 秒后放弃并写日志，不会静默失败。
判据是「2xx」而不是「端口能连上」，所以 404/500 一律不算就绪。

### 21. 只检查 `ollama.exe` 存在是不够的

便携版 zip 里除了 `ollama.exe`（约 25MB），还有 `lib\ollama\` 下一整套推理运行时
（`llama-server.exe`、`ggml*.dll` 等，合计约 1.3GB）。**解压中断会留下一个只有
`ollama.exe` 的假安装**，而它的表现极具欺骗性：

| 检查手段 | 假安装的结果 |
|---------|------------|
| `ollama.exe` 是否存在 | ✅ 存在 |
| `ollama serve` 能否启动 | ✅ 能 |
| `/api/tags` 能否列出模型 | ✅ 能（`ollama pull` 不需要推理引擎） |
| 真正生成回答 | ❌ `error starting llama-server: llama-server binary not found` |

也就是说，**只要不去看推理引擎文件，所有常规检查都会说「一切正常」** ——
用户会在准备脚本报告「已就绪」之后，提问时才发现不能用。

现在有三道防线：

1. `prepare.ps1` 下载后先用 `[System.IO.Compression.ZipFile]::OpenRead` 验证 zip
   能打开（断点续传留下的半成品打不开中央目录），解压后再校验
   `lib\ollama\llama-server.exe`；发现不完整就清理重来，并在依旧失败时明确报错退出。
2. `setup.portable_ollama_status()` 把这种状态报成 blocking 问题 `ollama_incomplete`，
   网页首屏的引导窗口里直接给出「重跑一键准备 / 改装官方安装包 / 手动重新解压」。
3. `/api/health` 的 `problems` 里也会点名，状态显示为 `degraded`。

非便携版布局（官方安装包）刻意返回「无法判断」，避免误报把用户带偏。

### 22. 删不掉的文件会让「一键安装」假装成功

上一节的修复上线后，用户点「一键安装」得到的却是：**退出码 0、日志全是 [OK]，
但 `llama-server.exe` 依然不存在**。原因在 Windows 的一个基本约束上：

> Windows 不允许删除正在运行的程序。

当时的清理代码是 `Remove-Item -Recurse -Force $OllamaDir -ErrorAction SilentlyContinue`。
`ollama.exe` 正被 `ollama serve` 占用（用户是先启动服务、后点的安装），删除**静默失败**；
紧接着 `Test-Path $OllamaExe` 仍为真，脚本于是认为「已经装好了」→ 跳过下载 → 一路绿灯。
**失败被两个 independently 无害的写法叠成了「成功」**：`-ErrorAction SilentlyContinue`
吞掉了错误，`Test-Path` 又把残留当成了成果。

现在把清理抽成 `scripts\lib\ollama-runtime.ps1` 里的纯函数，并被两个脚本共用：

```powershell
Stop-PortableOllama   # 只停「路径位于 tools\ollama 内」的进程，不碰系统安装
Remove-OllamaDir      # 停 → 删 → 再确认；返回 $false 表示仍被占用
```

调用方**必须**检查返回值，删不干净就 `exit 1` 并告诉用户关掉 Ollama 再试；
`prepare.ps1` 的最后一步（自检）还会再验一次完整性，不完整同样以非零码退出 ——
这样「安装成功」这四个字再也不会出现在半成品上。装完之后，如果 Ollama 本来就是
运行状态，脚本会让它继续跑着，不用用户再手动启动。

`tests\run_all.ps1` 的预检里有一个**真实的回归测试**：把一个可执行文件副本命名成
`ollama.exe` 并让它保持运行（占用文件），再断言 `Remove-OllamaDir` 能停掉它并确认删除，
否则整个测试套件直接失败。

### 23. 下载源要按实测速度排序，`latest` 也不能照抄

修好 22 之后，一键安装终于真的去下载了 —— 然后又暴露两个问题：

**问题一：`fetch.mjs --github-asset ... latest` 返回 404。**
`resolveGithubAsset()` 把 tag 直接拼进 `/releases/tags/<tag>`，
而 `latest` 并不是一个真实存在的标签名，正确端点是 `/releases/latest`。
结果默认下载源一起步就失败。

**问题二：默认下载源本来就是不可用的那个。**
脚本默认用 `github.com/.../latest/download/...`，而这个网络里 `github.com` 连不上。
同一个环境下的实测对比：

| 来源 | 实测 | 1.36GB 预计耗时 |
|------|------|----------------|
| `github.com` 直连 | 连接被重置 | 不可用 |
| `api.github.com` → `release-assets.githubusercontent.com` | **3.0 MB/s** | **约 8 分钟** |
| `ghproxy.net` 镜像 | 291 KB/s | 约 82 分钟 |

所以现在按 `api.github.com 解析 → ghproxy.net 镜像 → github.com 直连` 的顺序依次尝试，
某一个源失败就换下一个（换源时会丢掉上一个源的 `.part`，避免不同来源的字节拼在一起），
`-Mirror` 则把镜像提到最前面。**顺序来自实测，不是猜的。**

顺带把执行顺序也改了：**先下载、后替换**。原来的顺序是先删掉旧目录再下载，
万一 1.4GB 下载失败，用户连那个「不完整但能启动」的 Ollama 都没了。
现在下载并校验通过之后才动现有文件。

### 24. 网页上的「已就绪」必须和后端判定一致

用户截图里出现过一个自相矛盾的画面：环境面板写
「Ollama … 已就绪」，紧跟着的问题卡片却写「Ollama 安装不完整」。
原因很简单 —— 面板只判断了「`ollama.exe` 在不在」。
现在 `renderEnvPanel()` 会读后端返回的 `environment.ollama_portable`，
把 Ollama 那一行分成**已就绪 / 不完整 / 缺失**三种状态，
鼠标悬停还能看到「缺 lib\ollama 下的推理引擎（llama-server.exe），提问会失败」。

### 25. 只有 `\r` 的进度会被 PowerShell 吞掉

下载器原本用 `\r` 原地刷新进度（终端里很好看），但落到文件里是另一回事。
实测（`node` 写 `A\r`，6 秒后再写 `B\n`，输出重定向到文件）：

| 时刻 | 文件内容 |
|------|---------|
| 3 秒后（已写入 `A\r`） | **空**（0 字节） |
| 9 秒后（写入 `B\n` 并退出） | `A\r\nB\r\n` |

也就是说 **PowerShell 5.1 会按行转发原生命令的输出**：没有 `\n` 的内容一直攒在
缓冲区里，直到进程退出才落盘。一次 1.4GB 的下载意味着安装日志里**几十分钟一片空白**，
用户根本分不清是在下载还是卡死 —— 这正是「一键安装看着没反应」的来源之一。

现在 `fetch.mjs` 分两种模式：交互式终端（`isTTY`）保留 `\r` 原地刷新，
非交互（写日志、被 `installer.py` 重定向）改为**每 5 秒输出一行带 `\n` 的进度**。
`tests\run_all.ps1` 的预检会断言这两种模式都在，防止有人图省事改回纯 `\r`。

### 26. 活着的子进程会让 `powershell -File` 永远不退出

修完 22 之后，一键安装的日志已经全绿、退出码却永远不返回 —— 网页上一直停在「安装中…」。
实测现象：脚本最后一行都执行完了，宿主 `powershell.exe` 却继续挂着；
把它启动的 `ollama serve` 杀掉之后，宿主**立刻**退出。

原因是 Windows 的进程/控制台语义：子进程只要还活着、并且与父进程共享同一个控制台
（`Start-Process` 不带 `-RedirectStandardInput` 时就是这样），父进程的控制台就不会结束，
`powershell.exe` 于是卡在退出阶段。**「把新装好的 Ollama 留着继续跑」这个看起来贴心的
设计，正好踩在上面。**

试过的绕法都无效（都实测过）：

| 做法 | 结果 |
|------|------|
| `Start-Process` + `-RedirectStandardInput` 指向空文件 | 仍然挂住 |
| `cmd /c start "" /b ollama.exe serve > log 2> log2` | 仍然挂住 |
| 删掉临时启动的 Ollama（本次采用的方案） | 干净退出 |

所以 `prepare.ps1` 依旧在结尾停掉自己启动的 Ollama，并明确告诉用户
「双击 `scripts\start.cmd` 会重新拉起它」。`start.ps1` 的顺序是先拉起 Ollama 再启动后端，
所以即使后端已经在跑，再执行一次 `start.cmd` 也能把 Ollama 拉起来
（它会在端口检查那一步发现是自己，然后正常退出）。

顺带一提：这条约束只影响**会退出的**脚本。`start.ps1` 前台跑 uvicorn、按 `Ctrl+C` 才退出，
所以它用 `Start-Process` 拉起 Ollama 没有任何问题。

### 27. 原生 thinking 通道：思维链不在 `content` 里

`deepseek-r1` 这类模型在 Ollama 上走的是**原生 thinking 通道**：
思维链放在 `message.thinking` 字段，而不是正文里的内联标签。
`langchain-ollama` 1.x 只有在你传了 `reasoning=True`（对应线上的 `think: true`）
时才会把它取出来，并且放的键名是 `additional_kwargs["reasoning_content"]`。

原来的代码只认 `additional_kwargs["thinking"]`，于是：

* 思维链被**静默丢弃**，前端一个 `thinking` 事件都收不到；
* 用户点「发送」后界面长时间空白，然后突然蹦出答案。

实测同一台机器、同一个问题：

| | 修复前 | 修复后 |
|---|---|---|
| `thinking` 事件 | **0** 个 | 251 个 |
| 第一个推理 token | —— | **0.37s** |
| 第一个正文 token | 116s | **3.16s** |
| 总耗时 | 124s | **3.80s** |
| 引用角标 | 无 | `[1]` |

改动很小：`_build_llm()` 在模型**确实支持**时才打开这个通道，
读取时 `thinking` 与 `reasoning_content` 两个键都认（流式与非流式都改）。

能不能打开要先问 Ollama：`POST /api/show` 返回的 `capabilities` 里有
`thinking` 才开 —— 不支持的模型（如 `llama3.2`）收到 `think: true` 可能直接报错。
结果按 URL+模型名缓存 5 分钟，正常只在第一次提问前多一次本机 HTTP 往返。

### 28. Ollama 是懒加载的，第一条提问要等模型载入

服务起来了、`/api/tags` 也列出了模型，但**权重直到第一次推理才载入显存**。
本机实测这一段要 **100 秒左右**（1.1GB 权重 + CUDA 初始化；4GB 显存的笔记本
GPU 上还会部分回落到 CPU），之后同一模型只要 0.4 秒。
于是「启动服务」到「能提问」之间差着近两分钟，用户很容易以为是卡死了。

现在后端启动后会在**后台**发一次「只加载不生成」的请求：

```http
POST /api/generate   {"model": "deepseek-r1:1.5b", "keep_alive": "10m"}
```

Ollama 收到不带 `prompt` 的请求会直接返回 `done_reason: "load"`，
把模型预先载入。实测启动日志：`LLM 预热：deepseek-r1:1.5b（47.3s）`，
发生在端口就绪之后、不阻塞任何请求；等用户真正提问时模型已经是热的。
不想要这个行为可以设 `RAG_WARMUP_LLM=false`。

### 教程速查：谁负责关掉 Ollama

| 动作 | 结果 |
|------|------|
| 在启动窗口按 `Ctrl+C` | 后端退出 → 脚本**自动关掉 Ollama**（含本次拉起的、项目内置的、以及之前就在运行的） |
| `scripts\stop.ps1` | 停后端 **+ 停 Ollama** |
| `scripts\stop.ps1 -KeepOllama` | 只停后端 |
| `scripts\start.cmd -KeepOllama` | 启动时正常，退出时**保留** Ollama |
| 关掉那个 cmd 窗口（点 ×） | Windows 会连带结束同控制台的子进程，Ollama 也会停 |

### 29. 退出时要把 Ollama 一起收掉

用户的原话是「程序退出时自动关闭所调用的 ollama 进程」—— 不然任务管理器里会长期
挂着一个占着几百 MB 到 1GB 显存的进程，而用户以为「程序已经关了」。

原来的清理有两个漏洞：

1. **只在自己拉起过时才清理。** 如果 Ollama 之前就在运行（上次没退干净、或由一键安装
   拉起来的），`start.ps1` 只会打印「Ollama 服务已在运行」，退出时什么都不做 ——
   这正是最常见的「关了程序，Ollama 还活着」。
2. **靠进程名杀。** `Get-Process -Name ollama` 匹配不到官方的 `ollama app`（名字里有空格）
   和派生的 runner 子进程；而且它会把用户自己装的、用于别的工具的 Ollama 一并杀掉却
   不做任何说明。

现在统一走 `scripts\lib\ollama-runtime.ps1` 里的 `Stop-PortableOllamaForProject`，
按三种线索依次收口，任何一个命中都算：

| 线索 | 覆盖的情况 |
|------|-----------|
| 启动时记下的 PID（`-PassThru`） | 本次由脚本拉起的，按**进程树**杀（连带 runner 子进程） |
| 进程路径在 `tools\ollama` 下 | 项目内置的那份（PID 对不上时也能找到） |
| 监听 11434 端口的进程 | 之前就在运行的 / 系统安装的（只能靠端口定位） |

想保留 Ollama（自己还要用它跑别的模型）：`scripts\start.cmd -KeepOllama`，
或 `scripts\stop.ps1 -KeepOllama`。此时后端仍会在退出时请求 `keep_alive=0`
让模型**从显存里卸下来**（`ollama_client.unload_model()`），进程留着但不占显存。
实测证据（`/api/ps`）：

```
预热前运行列表: []
warmup_model -> True  耗时 62.7s
预热后运行列表: ['deepseek-r1:1.5b']
unload_model -> True
卸载后运行列表: []
```

顺带修掉一个隐藏的坑：**端口定位原本用 `Get-NetTCPConnection`，而它在受限账户下会直接抛
「拒绝访问」**（本机实测），于是「按端口兜底」整条路都是摆设。现在改用 `netstat -ano -p TCP`
解析（不需要额外权限，状态列也不随系统语言变化），`Get-NetTCPConnection` 不再是依赖。

真机验证（把后端进程结束掉，模拟程序退出）：

```
正在停止 Ollama ...
  （本次由启动脚本拉起，PID 22732）
[OK]   已停止 Ollama（PID 22732）
已停止。
```
之后 `Get-Process ollama` 为空、11434 端口关闭；加 `-KeepOllama` 时则相反，Ollama 继续运行。

### 30. 分点汇总全都显示成「1.」：是渲染器把列表切碎了

用户在真机上看一条「多任务网络的优势是什么」的回答，发现每个要点都编号成 `1.`：

```
1. 特征共享
1. 任务协同
1. 数据利用
1. 模型简化
```

看起来像模型不会数数，实际是**渲染器的锅**。先复现拿到原始输出（deepseek-r1:1.5b）：

```
1. **特征提取的普适性**  ← 行尾两个空格（硬换行）
   多任务网络通过共享特征提取器……
                       ← 空行
2. **减少数据和参数**
   通过共享卷积网络……
```

旧渲染器只认「以 `- ` 或 `1. ` 开头的行」是列表项，后面这种缩进的说明行会被当成
**普通段落**。于是每一轮都变成「一个只有一项的 `<ol>` + 一个段落 + 一个只有一项的 `<ol>`」，
浏览器对每个 `<ol>` 都从 1 开始编号 —— 所以看到的是 1. 1. 1. 1.。

`frontend/assets/js/markdown.js` 的列表部分因此重写成一个真正的块解析器：

| 场景 | 旧行为 | 新行为 |
|------|--------|--------|
| 空行分隔的松散列表 | 每个要点各自成一个 `<ol>`（全编号 1） | 合并成一个 `<ol>`，序号连续 |
| 缩进的说明行 | 掉到列表外面，和要点脱节 | 归入同一个 `<li>`（`<br />` 换行） |
| 子列表 | 被拉平成同级项 | 按缩进建树，渲染成嵌套 `<ul>` |
| `1．` / `1、`（全角、无空格） | 不认，当成普通段落 | 认（但 `3.14`、`2020 年第 2 期` 仍不会被误判） |
| 模型重复写 `1.` | 全编号 1 | `<ol>` 顺序编号，看到 1. 2. 3. |

同时补上了行尾两空格的硬换行（`<br />`）。`tests/test_frontend.mjs` 第 4b 节把这几种
写法全部钉死（含「真实模型输出」的原始字符串）。

### 31. 阈值 0.2 下总结不出文档内容：总结类问题根本不该用阈值筛

用户反馈「在 0.2 的相似度阈值下无法对文档的大致内容进行总结」。实测（真实 PDF + 真实嵌入）：

| 提问 | 最佳相似度 | 0.2 阈值下 top-4 的构成 |
|------|-----------|------------------------|
| 多任务网络的优势是什么 | 0.367 | 1 条正文 + 3 条页眉副本 |
| 请总结这篇文档的主要内容 | 0.433 | 1 条正文 + 3 条页眉副本 |
| 总结一下 | 0.273 | 3 条页眉副本 |
| 完全无关的乱码 `hjkl qwer zxcv` | 0.250 | — |

两件事同时成立：**「总结一下」的最佳分（0.273）只比乱码（0.250）高一点**，
所以「阈值定多少才能既不漏总结、又不放进噪声」这个问题本身无解。
总结类提问问的是**整篇**，它和任何一个片段的相似度都低，用阈值筛必然漏。

现在的做法是**按提问意图分流**（`rag.detect_intent`，纯规则、零额外延迟）：

| 意图 | 判定 | 检索策略 | 提示词 |
|------|------|---------|--------|
| `overview` | 含「总结/概述/概括/摘要/主要内容/讲了什么…」且提问不超过 60 字 | 忽略阈值，按阅读顺序**全篇均匀取样**（`summary_max_chunks` 块，默认 10；先去掉近重复；按上下文预算装箱；多文档按块数比例分名额，每篇保底 1 块） | 概览提示词：先 2~4 句整体概括，再用 `- **要点**：说明 [n]` 逐条列出 |
| `qa` | 其它 | 相关度 top-k，阈值 + 相对窗口 + 兜底放宽 | 问答提示词（只依据资料作答） |

界面会把这轮走的是哪条路显示出来：`全文概览` 徽标（含「共 N 块中取 M 块」提示）、
`已放宽阈值` 徽标（含生效阈值与最佳分数）。

真机复验（同一篇 4 页中文期刊论文，`scripts` 之外直接调 `rag_service`）：
概览模式取样 10 块覆盖第 1~4 页（含摘要、引言、实验、结语），总字符 5932 ≤ 上限 6000。

### 32. 检索噪声：页眉、参考文献、被切碎的段落

顺着上一条往下查，发现真正挤掉正文的是三类「结构性噪声」。实测同一篇论文：

* **页眉**：每页重复「2020 年第 2 期 / 信息与电脑 / China Computer & Communication 人工智能与识别技术」。
  它是关键词拼盘，对任何提问相似度都高 —— 上面表里那 3 条「正文」其实都是同一段页眉的不同页副本。
  现在在切分前删掉「出现在 ≥60% 页面上、长度 ≤100 字」的重复行（**比较时去掉所有空白**：
  这份 PDF 奇数页和偶数页的页眉差一个空格，只折叠空白是抓不到的）。
* **参考文献**：关键词最密集、语义最贫乏。实测它对「请总结这篇文档的主要内容」拿到 **0.420（全场第一）**。
  现在识别到独占一行的「参考文献 / References」且后面跟着 ≥2 条编号条目就整段截掉
  （只是正文里顺口提一句则不动）。
* **段落被硬切**：PDF 抽出来是硬换行、没有空行，切分器只能在第 800 个字处硬切，
  中文正文经常和英文摘要、上一节的开头粘进同一个块。实测包含「1.1 多任务网络架构」的正文块，
  因为前半段是英文摘要，对中文提问的相似度从 **0.367 掉到 0.147**。
  现在按版面补回段落边界：小节标题（`1.1` / `2.2.3` / `0 引言`）另起一段、中英文切换处另起一段。

修复前后（同一篇论文，`Q: 多任务网络的优势是什么`）：

```
修复前 top-4：0.367 正文 | 0.331 页眉 | 0.281 页眉 | 0.254 页眉
修复后 top-4：0.310 结语（「（1）多任务网络优势。对于多个任务而言…」）
             0.283 1 研究思路 / 1.1 多任务网络架构
             0.258 2.1 实验数据
             0.249 0 引言
```

分块数 11 → 10，每块都是完整的一节，没有一条是页眉或参考文献。
**这类改进存在索引里**：解析逻辑变了，旧索引不会自动更新，所以界面加了「重建索引」
按钮（`POST /api/documents/reindex`），用 `data/uploads` 里的原文件按当前配置重跑。

### 33. 跑测试会把用户的知识库删掉

这条是本次排查里最该单独记下来的：`tests/test_e2e.py`、`tests/test_ollama_integration.py`
和 `tests/test_http.py` 都会清空知识库（`clear_all()` / `DELETE /api/documents`），
而它们此前都指向真实的 `data/` 目录 —— **跑一次测试，用户上传的文档连文件一起没了**。

现在三层防护：

1. `app/core/paths.py` 支持 `RAG_DATA_DIR` 覆盖数据根目录（导入时解析）。
2. 两个进程内套件在导入 `app.*` 之前把 `RAG_DATA_DIR` 指向 `.tmp/tests/...`，
   并断言数据目录不是真实的 `data/`，否则直接拒跑。
3. `tests/run_all.ps1` 总是**新起一个专用实例**（`RAG_DATA_DIR` → `.tmp/http-suite-data`，
   自动挑空闲端口）再跑 HTTP 验收，不再复用用户正在运行的服务；
   `test_http.py` 还会读 `/api/health` 里的 `data_dir`，一旦发现对着真实数据目录就拒绝执行。

顺带修掉两个让排查变难的小问题：

* `run_all.ps1` 里调用套件写的是 `Invoke-Suite ... | Out-Null`。PowerShell 会把**子进程的
  stdout 也一起吞掉**，套件失败时只剩汇总表里一个「失败」，什么线索都没有。现在保留完整输出。
* HTTP 验收一直卡在「服务未就绪」。原因是 **httpx 会读 Windows 注册表里的系统代理设置，
  连 `127.0.0.1` 也走代理**（拿到 502）；而浏览器和 `Invoke-RestMethod` 会读代理绕过列表，
  所以「手动访问明明是好的」。测试客户端现在显式 `trust_env=False`。

### 34. 上下文窗口和资料长度必须自洽，否则模型会「失忆」

改完概览检索后做真机复验，出现了一次**空回答**（回答 0 字）。加诊断打出 token 用量才看清机制：

```
num_ctx=4096, 10 段中文资料 → prompt_eval=3542, eval=1024, done_reason=length, 回答 0 字
```

资料 10 段约 6000 字，中文「1 字 ≈ 1 token」，光提示词就 3542 token；再加上生成，
总需求超过 4096 的窗口 —— Ollama 会**滑动丢弃最早的 token**，被丢掉的正是系统提示词里的
规则，于是模型既没了指令也没了状态，直接返回空。两个配置各自看都「合理」，
合在一起就矛盾，而且**没有任何报错**。

现在：

| 配置 | 旧值 | 新值 | 原因 |
|------|------|------|------|
| `RAG_LLM_NUM_CTX` | 4096 | **8192** | 概览模式要装下整篇文档的取样片段 |
| `RAG_LLM_NUM_PREDICT` | 1024 | **2048** | 1024 时概览回答写到一半就被截断（`done_reason=length`） |
| `RAG_LLM_REPEAT_PENALTY` | （未设） | **1.2** | 1.5B 会陷进「同一句话连写四遍」的退化 |
| `RAG_LLM_REPEAT_LAST_N` | （未设） | **512** | 同上 |
| `max_context_chars` | 6000（直接用） | 6000（**仍受 num_ctx 约束**） | 见下 |

`max_context_chars` 不再被直接使用，而是取 `min(max_context_chars, 由 num_ctx 反推的上限)`：

```
预算 = min(max_context_chars, num_ctx − 生成预留 − 系统提示词预留)
例：num_ctx=8192 → 6000 字；num_ctx=4096 → 2263 字；num_ctx=2048 → 880 字
```

这样用户只调小 `num_ctx`（比如显存不够）也不会踩坑：资料会自动跟着变短，
而不是被静默截断。真机复验（同一问题连问 3 次，`num_ctx=8192`）：

```
第 1 次: 回答 365 字 / 思维链 689 字 / 引用 1 / done_reason=stop
第 2 次: 回答 645 字 / 思维链 524 字 / 引用 0 / done_reason=stop
第 3 次: 回答 199 字 / 思维链 438 字 / 引用 0 / done_reason=stop
空回答 0/3，截断 0/3（改之前是 1/3 空回答、1/3 截断）
```

### 35. 引用编号 [n] 这条：1.5B 只能算「部分遵循」

界面上的引用角标依赖模型自己写 `[1]`。系统提示词里从第 2 条规则到结尾提醒都写了要求，
真机实测仍然**不稳定**（同一问题连问 3 次：1 次标注、2 次不标）。加在用户消息末尾的
「贴身提醒」（离生成最近、且属于用户要求）命中率略有提高，但不是保证。

结论（不是 bug，是模型能力）：`deepseek-r1:1.5b` 上引用标注属于「时有时无」。
想要稳定的溯源角标，请换更大的模型（`llama3.2` / `qwen2.5:3b` 起），
或在 `.env` 里调大 `RAG_LLM_NUM_CTX` 后重试。检索侧（引用来源列表、点击查看原文分块）
本身是确定性的，不受影响。

---

## 已知限制

诚实说明，避免踩坑：

### 嵌入模型的中文能力

`all-MiniLM-L6-v2` 只有 384 维，**以英文语料为主训练**，中文语义匹配偏弱。
实测（`tests/test_offline.py` 会打印真实数据）：

* **逐字/近义短语查询**：Top-1 命中 4/4 —— 检索管道本身正确
* **口语化中文提问**：Top-1 命中 3/4

并且无关问题的得分可能不低于相关问题（实测无关问题 0.60 vs 相关问题上限 0.76），
说明**绝对阈值在中文上区分度弱**。因此 `RAG_SCORE_THRESHOLD` 默认刻意设为 `0.20`：
宁可多召回几段让 LLM 依据上下文判断，也不要漏掉正确片段。

**如果知识库以中文为主，强烈建议换模型**（改 `.env` 即可，无需改代码）：

```ini
RAG_EMBEDDING_MODEL_NAME=BAAI/bge-small-zh-v1.5
RAG_EMBEDDING_DIMENSION=512
```

下载：`scripts\download_models.py --model BAAI/bge-small-zh-v1.5`
**换模型后必须重建索引**——不同模型的向量空间不通用。

### LLM 能力

`deepseek-r1:1.5b` 只有 15 亿参数，实测边界（都在真机上跑过）：

* 能较好地「照文档摘要」，但复杂推理、多跳问题会吃力
* **引用编号 `[n]` 时有时无**：同一问题连问 3 次，1 次标注、2 次不标（见实现要点 35）。
  引用来源列表和点击溯源本身是确定性的，只是回答正文里的角标不保证
* **会重复**：偶尔出现「同一句话连写四遍」的退化。已默认加重复惩罚
  （`RAG_LLM_REPEAT_PENALTY=1.2`）缓解，但不能根除
* **长回答会被截断**：生成上限原本 1024，概览类回答会写到一半停住
  （`done_reason=length`）。现已提到 2048
* 建议把 `RAG_TOP_K` 控制在 3~5，给太多上下文反而更容易跑偏
  （总结类问题例外：它走的是全篇取样，由 `RAG_SUMMARY_MAX_CHUNKS` 控制）

追求质量可换 `qwen2.5:3b` 或 `llama3.2`（约 2GB，4GB 显存可跑），引用标注的稳定性会明显好于 1.5B。

### 其他

* 目前只支持单用户本地使用，没有鉴权（服务默认只绑定 `127.0.0.1`）
* 对话历史保存在前端内存中，刷新页面即清空
* 扫描版 PDF（纯图片）无法提取文字，需要先做 OCR。
  程序会在平均每页抽取字符数过低时写日志提醒（`可能是扫描件或图片版 PDF`）
* PDF 里的**图和表是图片**，其中的文字抽不出来（这类内容检索不到）

---

## 常见问题

**启动时提示「本地嵌入模型不存在」**
执行 `pwsh -File scripts\prepare.ps1`，或只下模型：
`.venv\Scripts\python.exe scripts\download_models.py`

**提问时报「无法连接 Ollama」**
运行 `scripts\start.ps1`（会自动拉起），或手动 `ollama serve`。
确认 `http://127.0.0.1:11434/api/tags` 能访问。

**报「Ollama 中没有模型 xxx」**
执行 `ollama pull deepseek-r1:1.5b`。

**提问时报 `error starting llama-server: llama-server binary not found`**
Ollama **装了一半**：`tools\ollama\` 下只有 `ollama.exe`，缺 `lib\ollama\` 里的推理运行时
（通常是解压中断或 zip 没下完）。这种状态服务能启动、`/api/tags` 也能列出模型，
所以容易被误判成「一切正常」。修复方式任选其一：

* 重新双击 `scripts\prepare.cmd`：脚本会先体检，发现不完整就**停掉正在运行的 Ollama**、
  清掉目录、重下重解压（需要联网，约 1.4GB）
* 改装官方安装包：<https://ollama.com/download>，装完删掉不完整的 `tools\ollama\` 即可
* 已经自己下好 zip：删掉 `tools\ollama\` 后重新 `Expand-Archive`
  （正确的目录里应当有 `lib\ollama\llama-server.exe`）

网页首屏的配置引导会主动报出这个问题（`ollama_incomplete`），环境面板里那一行也会
标成「不完整」而不是「已就绪」，不用自己猜。

**点了「一键开始安装」，日志一路 [OK] 但问题依旧**
只可能发生在旧版本上。原因是 Windows 不允许删除正在运行的程序，而旧代码把删除失败
静默忽略了（详见实现要点 22）。当前版本遇到这种情况会直接报错退出、并把「先关掉 Ollama」
写清楚，不会再假装成功。若你仍看到旧行为，确认 `scripts\lib\ollama-runtime.ps1` 存在
（老版本没有这个文件）。

**点了「一键开始安装」，卡在「安装中…」一直不结束**
说明用的是旧版本（实现要点 26）：修好 Ollama 之后把新进程留着继续跑，导致宿主
`powershell.exe` 卡在退出阶段。当前版本会在结尾收掉它，并提示用 `scripts\start.cmd`
重新拉起 Ollama。

**下载了 1.4GB 却看不到任何进度**
旧版本的下载器只用 `\r` 刷进度，而 PowerShell 按行转发输出，于是日志长时间空白
（实现要点 25）。当前版本在写日志的场景下每 5 秒输出一行进度。

**回答总是「根据已有文档无法回答该问题」**
说明检索没召回相关片段。可以：换更贴近原文的措辞提问；
在设置里调低相似度阈值；或换中文嵌入模型（见上文）。

**中文 PDF 解析出乱码**
文档本身可能是非 UTF-8 编码或扫描件。可先用文本编辑器另存为 UTF-8 再上传。

**端口 8000 被占用**
`pwsh -File scripts\start.ps1 -Port 8080`

**启动后网页先显示「无法访问此页面 / 127.0.0.1 拒绝连接」**
正常启动不会再出现（见实现要点 19、20）。若仍然出现：说明浏览器是在服务起来之前
被手动打开的 —— 等 1~2 秒按 `F5` 刷新即可。想确认服务到底有没有起来，看启动窗口里
有没有打印「服务已就绪（x.xs），正在打开浏览器」，或直接访问 `http://127.0.0.1:8000/api/health`。
想换回自己控制浏览器：`powershell -File scripts\start.ps1 -NoBrowser`。

**打包拷贝时提示某些目录「访问被拒绝」**
只会在一种情况下出现：`.tmp\` 下残留了由 `tempfile.mkdtemp()` 创建、权限受限的目录。
当前代码已不使用 `mkdtemp`（`tests/test_offline.py` 里有说明），新运行不会再产生。
若确实存在，手工删除即可：

```powershell
cmd /c "rmdir /s /q .tmp" ; cmd /c "rmdir /s /q diag"
```

**改过脚本之后 PowerShell 报 `Unexpected token` / 中文变乱码**
`.ps1` 丢了 UTF-8 BOM。用编辑器重写文件时很容易发生，跑一下修复脚本即可：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix-encoding.ps1
```

`tests\run_all.ps1` 的预检也会在测试开始前拦下这类问题。
