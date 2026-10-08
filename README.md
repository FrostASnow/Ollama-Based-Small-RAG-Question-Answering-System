# 离线 RAG 文档问答

一个**完全离线**运行的小型文档问答系统。上传 PDF / Word / Markdown / TXT 等文档，用自然语言提问，回答会标注引用来源并可点开查看原文片段。所有计算都在本机完成：不调用任何云端 API，不上传任何数据，断网可用。

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

> 前端刻意不用 React/Vite：离线场景下，免构建意味着**拷过去就能跑**。

---

## 目录结构

```
rag-qa/
├── README.md                   本文件
├── RAG-QA.exe                  一体化启动器（图形面板；双击启动/关闭）
├── .env.example                配置模板（复制成 .env 生效）
├── backend/
│   ├── requirements.txt
│   └── app/
│       ├── main.py             FastAPI 入口，同时托管前端静态文件
│       ├── config.py           配置与离线环境变量引导
│       ├── schemas.py          请求/响应模型
│       ├── core/
│       │   ├── paths.py        项目路径解析（支持 RAG_DATA_DIR 覆盖）
│       │   ├── logging.py      日志
│       │   └── sse.py          SSE 流式封装（带心跳）
│       ├── services/
│       │   ├── embeddings.py   all-MiniLM-L6-v2 本地加载（含维度探测与实例重置）
│       │   ├── loader.py       文档解析（PDF 版面还原/去页眉/扫描件判定）与中文友好切分
│       │   ├── registry.py     文档注册表（JSON 原子写 + 索引元信息）
│       │   ├── index_health.py 索引一致性体检（换模型/改切分参数后当场发现）
│       │   ├── vectorstore.py  FAISS 索引管理 + 概览取样/阈值兜底
│       │   ├── ingest.py       入库流水线 + 重建索引 + 索引备份
│       │   ├── rag.py          意图分流 + 提示词 + 流式生成
│       │   └── ollama_client.py Ollama 探测（带 TTL 缓存与主动失效）
│       └── routers/
│           ├── health.py       健康检查、配置、模型列表
│           ├── documents.py    上传、列表、分块预览、重建索引、删除
│           └── chat.py         问答（SSE/JSON）与纯检索
├── launcher/
│   └── RagQaLauncher.cs        RAG-QA.exe 的源码（C# 5，csc.exe 直接编译）
├── frontend/                   免构建前端
│   ├── index.html
│   ├── package.json            只为 Node（"type":"module"）：给交互测试 import 用
│   └── assets/
│       ├── css/style.css
│       └── js/{app.js, api.js, markdown.js}
├── scripts/
│   ├── prepare.ps1 / .cmd      一次性联网准备（装依赖、下模型、编译启动器）
│   ├── start.ps1 / .cmd        离线启动（自动拉起 Ollama）
│   ├── stop.ps1                停止服务
│   ├── build-launcher.ps1/.cmd 编译 RAG-QA.exe
│   ├── fix-encoding.ps1        修 .ps1/.cs 的 UTF-8 BOM、查 .cmd 是否纯 ASCII
│   ├── download_models.py      下载 HuggingFace 嵌入模型
│   └── fetch.mjs               通用下载器（支持断点续传）
├── tests/                      9 套测试，共 652 项断言（含前端真实交互）
├── models/                     本地嵌入模型（离线加载）
├── data/                       上传文件、FAISS 索引（含 backups/）、日志
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

> 命令用 `pwsh`（PowerShell 7）书写；所有 `.ps1` 都已存为 **UTF-8 带 BOM**，在自带的 **Windows PowerShell 5.1** 下也能正确显示中文。没装 PowerShell 7 就把 `pwsh` 换成 `powershell`，或直接**双击 `scripts\*.cmd`**（自动优先用 pwsh，没有则回退到 powershell）。

### 第一步：一次性联网准备

```powershell
pwsh -File scripts\prepare.ps1
```

或直接双击 `scripts\prepare.cmd`。脚本会依次完成：准备 Python 3.12（优先用 [uv](https://docs.astral.sh/uv/)，免管理员、免安装）；创建 `.venv` 并安装 97 个依赖；下载 `all-MiniLM-L6-v2` 到 `models/`（约 90MB）；下载便携版 Ollama 到 `tools/ollama/`，并拉取 `deepseek-r1:1.5b`（约 1.5GB + 1.1GB）。常用参数：

```powershell
pwsh -File scripts\prepare.ps1 -Mirror              # 国内镜像加速
pwsh -File scripts\prepare.ps1 -SkipOllama          # 只用检索，不做问答
pwsh -File scripts\prepare.ps1 -LlmModel llama3.2   # 换 LLM
```

> Ollama 下载太慢可以跳过，自行从 <https://ollama.com/download> 安装，再执行 `ollama pull deepseek-r1:1.5b`；启动脚本会自动发现系统里的 `ollama`。

### 不想看脚本输出？直接启动，界面会教你

即使**完全跳过第一步**也可以直接 `scripts\start.cmd`：前端启动时会做一次配置体检，缺组件就**自动弹出引导窗口**，先列出本机环境探测结果（uv / Python 环境 / 嵌入模型 / Ollama 各装在哪），再逐项给出多种安装方式（命令里是这台机器的真实绝对路径）和**一键开始安装**（网页实时滚动日志，可关掉窗口让它后台跑）；安装失败会给出**重新安装 / 用国内镜像重试 / 查看手动命令**三条出路，装完点「重新检测」即可。**先启动、后配置**也是可行路径。

### 第二步：启动

**方式 A（推荐）：双击项目根目录的 `RAG-QA.exe`**

一个 34KB 的一体化启动器（.NET Framework，Win10/11 自带，无需安装任何运行时）：图形面板显示运行状态、模型、知识库规模，一键启动 / 停止 / 打开页面；启动后**先等健康检查通过再打开浏览器**；**关掉窗口 = 后端和 Ollama 一起停止**（想常驻可勾「关闭窗口时最小化到托盘」）；托盘图标常驻；只允许一个实例。同样支持命令行调用：

```powershell
.\RAG-QA.exe --status          # 环境自检 + 运行状态
.\RAG-QA.exe --start           # 后台启动（不弹窗口）
.\RAG-QA.exe --stop            # 停止后端与 Ollama
.\RAG-QA.exe --stop --keep-ollama
```

忘了编译或想重新编译：`scripts\build-launcher.cmd`（`prepare.cmd` 也会顺手编译一次）。**方式 B：命令行窗口**

```powershell
pwsh -File scripts\start.ps1
```

或双击 `scripts\start.cmd`。浏览器会在服务**真正就绪之后**自动打开 <http://127.0.0.1:8000>。启动脚本会检查 `.venv` 与嵌入模型是否就绪；若 Ollama 未运行，自动以 `ollama serve` 拉起；启动后端（后端同时托管前端）；把访问地址通过 `RAG_OPEN_BROWSER_URL` 交给后端，后端**真的能用 HTTP 拿到 200** 时才打开浏览器。按 `Ctrl+C` 停止，脚本会一并关闭它拉起的 Ollama；若你另外还用 Ollama 跑别的东西，加 `-KeepOllama` 保留它：`scripts\start.cmd -KeepOllama`。

### 第三步：使用

1. 左侧**拖入或点击上传**文档，等待索引进度完成
2. 在底部输入框提问，回答会**逐字流式输出**
3. 回答中的 `[1]` `[2]` 角标可点击，直接查看对应原文片段
4. 左侧勾选文档，可把检索范围限定在选中的文档内
5. 点击「系统状态」查看 Ollama、模型、索引的实时情况

---

## 部署到离线机器

准备完成后整个 `rag-qa` 目录就是自包含的：

```powershell
# 在联网机器上打包（排除可再生的缓存）
Compress-Archive -Path rag-qa -DestinationPath rag-qa-offline.zip

# 拷到离线机器，解压后直接启动
pwsh -File scripts\start.ps1
```
需要一并拷贝的内容：`.venv/`、`models/`、`tools/`（若用便携版 Ollama）、`data/`（若想保留已索引的文档）。`.venv` 里的路径是相对的，但 Python 解释器在 `.uv/python/` 下：若准备时用的是 uv 装的项目内 Python，请把 **`.uv/` 也一起拷贝**，或改用 `-SkipPython` + 系统 Python。

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

完整说明见 `.env.example`（每一项都有中文注释）；前端「检索参数」滑块是**运行期**调整，立即生效但不写回 `.env`。

---

## 测试

```powershell
pwsh -File tests\run_all.ps1
```

| 套件 | 断言数 | 依赖 Ollama | 覆盖内容 |
|------|-------|------------|---------|
| `check_imports.py` | 25 | 否 | 全部依赖能否按预期路径导入（含切分器的延迟导入路径）、**全仓库 `__all__` 导出名都真实存在** |
| `test_offline.py` | 58 | 否 | 模型完整性、离线加载、归一化、FAISS 检索、切分、**PDF 去页眉/段落还原/参考文献截断、扫描件抽取质量判定**、意图识别、近重复去重与取样名额 |
| `test_e2e.py` | 107 | 否（假 LLM） | 入库 → 检索 → 提示词 → 流式事件 → 引用 → 删除 → 持久化 → 安装日志解析 → 冷启动导入链 → 浏览器打开时机 → Ollama 安装完整性 → 原生 thinking 通道 → 概览检索、阈值兜底、重建索引 → **概览取样不做全库扫描且分数等价** |
| `test_index_consistency.py` | 72 | 否 | **换嵌入模型后当场发现并给可照做的错误、改 chunk_size 的僵尸知识库、`registry.clear()` 清掉索引元信息、GET/PUT 配置字段对称（29 项逐一回显）、探测缓存失效时机、扫描件警告透出、重建前备份与保留份数** |
| `test_ollama_integration.py` | 53 | 否（协议兼容假服务） | 真实 ChatOllama 往返、NDJSON 流解析、thinking 能力探测、**能力缓存失效时机与 TTL**、模型预热与退出卸载、错误分支、状态自洽 |
| `test_http.py` | 134 | 否 | 真实 HTTP 服务：静态托管、上传、检索、SSE 协议、错误码、配置引导、一键安装、JSON charset、重建索引接口、meta 里的检索策略、**GET/PUT 配置字段对称、索引一致性上报、改切分参数后健康检查转 degraded** |
| `test_frontend.mjs` | 85 | 否（需 Node） | 前端渲染（XSS 转义、引用角标、列表分点渲染）+ 静态一致性（DOM id、模块导出、`[hidden]` 兜底、无 CDN、环境面板三态、检索徽标与重建索引入口） |
| `test_frontend_interaction.mjs` | 88 | 否（需 Node） | **真实交互**：把 `app.js` 装进 DOM 垫片（`tests/dom-lite.mjs`）跑，假 fetch/XHR/SSE 喂数据 —— 启动渲染、滑块写回 `PUT /api/config`、回车发送 → 流式 → Markdown → 引用溯源、停止生成中断流、上传/删除/清空的二次确认、索引告警与扫描件警告可见性、空知识库守卫、侧栏折叠 |
| `test_launcher.ps1` | 30 | 否 | **一体化启动器**：exe 新鲜度、`--status` 自检、GUI 窗口创建与标题、`--start`/`--stop` 闭环（隔离数据目录、不误杀 Ollama） |

合计 **652 项断言**（脚本打印实际数字），另有脚本编码/语法与安装链路预检。测试**不会**碰你的知识库：会清空数据的套件都跑在 `RAG_DATA_DIR` 指向的临时目录上，并先断言「当前数据目录不是真实的 `data/`」，否则拒绝执行；HTTP 验收由 `run_all.ps1` 另起一个专用实例（自动挑空闲端口）；启动器的 `--stop` 在测试里一律加 `--keep-ollama`。除 `test_http.py` 需要后端在跑（运行器会自动拉起并在结束后关闭）外，其余都可独立执行。

---

## 实现要点

### 1. 推理链标签不能用字面量写

`deepseek-r1` 的推理链标签在部分工具链中属于分词器特殊 token，直接以字面量写进源码会被改写成语义等价但**无法匹配**的字符，导致推理链解析静默失效；`rag.py` 里用 `chr()` 拼接标签：

```python
_LT, _GT, _SLASH = chr(60), chr(62), chr(47)
THINK_OPEN = _LT + "think" + _GT
```

### 2. 标签会被逐 token 切碎

小模型流式输出时标签可能被拆成 `<th` + `ink>`；`ThinkSplitter` 会在缓冲区里**暂扣可能是标签前缀的尾部字符**，等后续 token 补齐再判断，否则标签会漏进正文。

### 3. 统计信息在最后一个空 chunk 上

流式的最后一个 chunk `content` 为空，只带 `eval_count` / `done_reason`；若把元数据收集放在 `if not content: continue` 之后，token 统计将永远为空。`rag.py` 中先收元数据、再处理内容。

### 4. 健康检查不能串行等两次超时

Ollama 不可达时连接要等超时才返回，而前端启动第一个请求就是 `/api/health`；串行等两次超时（约 8 秒）界面会明显卡顿，现在只发一次请求、超时压到 2 秒并加 5 秒 TTL 缓存，延迟稳定在 **40ms** 左右。

### 5. 冷启动放到后台

加载 `sentence-transformers` 会连带导入 torch，冷启动需十几到几十秒；放在 lifespan 里同步等待会让端口迟迟不监听，用户看到的是「无法连接」。现在预热与索引恢复都在 `asyncio.to_thread` 后台任务里，叠加第 19 条后端口 0.6 秒开始监听。

### 6. 归一化只做一次

归一化在 Embeddings 层完成（`normalize_embeddings=True`），FAISS 侧**不传** `normalize_L2` —— 在 `MAX_INNER_PRODUCT` 下它是空操作且 langchain 会告警；这样内积直接等于余弦相似度。

### 7. 删除向量前先过滤

注册表与索引可能因异常中断而不一致，把不存在的 id 直接交给 FAISS 会抛错，导致**整批删除失败**（连正常的部分也删不掉）；`delete_ids()` 会先与 `index_to_docstore_id` 求交集。

### 8. SSE 心跳不能对异步生成器用 wait_for

对 `__anext__()` 施加超时会取消正在 await 的协程，进而关闭整个异步生成器、截断流式回答；`core/sse.py` 改用「生产者任务 + 队列 + 超时读队列」的模式。

### 9. 全部缓存重定向到项目内

`HF_HOME`、`TMP`、`TEMP` 都指向项目目录：既是自包含部署的需要，也避免在受限环境（系统临时目录不可写）下运行失败。

### 10. `.cmd` 文件必须纯 ASCII

`cmd.exe` 按 **OEM 代码页**（中文 Windows 上是 936）读取 `.cmd`，UTF-8 中文注释会被按 GBK 重新分组、撕成碎片，cmd 反过来把这些碎片当命令执行：

```
'鈥使鈥?PowerShell' 不是内部或外部命令
'pwsh' 不是内部或外部命令
```

**脚本根本跑不起来**，最容易被误判成「项目有 bug」；所以 `start.cmd` / `prepare.cmd` 全部改成纯 ASCII，中文提示交给 `.ps1`，`.ps1` 用 UTF-8 带 BOM 保存并在开头把控制台切到 UTF-8。

### 11. JSON 响应必须声明 charset

Starlette 默认只给 `text/*` 追加 `charset`，`application/json` 不带；Windows PowerShell 5.1 的 `Invoke-RestMethod` 遇到没有 charset 的 JSON 会按 **ISO-8859-1** 解码，中文全变乱码。`main.py` 用 `UTF8JSONResponse` 作为 `default_response_class`，并覆盖了 `HTTPException` / `RequestValidationError` 的处理器 —— 内置处理器直接构造 `JSONResponse`，会绕过默认类，而我们的 404/409/503 错误信息里都是中文。

### 12. 端口占用要分两种情况

「自己的实例已经在跑」和「端口被别的程序占了」用户要做的事完全不同。先做本机 TCP 探测（端口空闲时瞬间返回），确认被占用后再调 `/api/health` 判断是谁；反过来先调 health，每次启动都要白等一次 HTTP 往返，超时留短了还会把自己的实例误判成别人的。

### 13. 启动脚本不能依赖 PowerShell 作业机制

最初用 `Start-Job` 在后台等就绪后开浏览器，但 `$ErrorActionPreference='Stop'` 下一旦作业机制不可用（部分受限环境如此），整个启动流程就被中断。现在改成 uvicorn 前台运行（日志直接进本窗口、Ctrl+C 自然传递），浏览器交给**分离的隐藏进程**延时打开，失败也不影响服务。

### 14. `Get-Command` 的结果没有 `.FullName`

PowerShell 里 `Get-Command uv` 返回 `ApplicationInfo`，路径在 **`.Source`** 上；误用 `.FullName` 会**静默拿到空字符串**，脚本悄悄走到「没有 uv」分支、回退到 `python -m pip install`，而 uv 创建的 venv 默认**不含 pip**，最后报 `No module named pip`。`prepare.ps1` 现在统一用 `Resolve-Uv` 解析成字符串路径，并给 `uv venv` 加 `--seed`、补 `ensurepip` 兜底。

### 15. PS 5.1 里重定向原生命令的 stderr 会中断脚本

`$ErrorActionPreference = 'Stop'` 时，只要对原生命令做 `*> $null` 或 `2>&1`，PowerShell 5.1 就会抛 `NativeCommandError` 打断整个脚本 —— 而「探测 pip 在不在」恰恰既需要重定向、又预期会失败；`prepare.ps1` 用 `Test-NativeSuccess` 包一层，临时放宽偏好设置。

### 16. 写日志的两个写者不能各自维护文件位置

一键安装把子进程输出重定向到 `data/logs/install.log`；先用 `"wb"` 打开句柄（位置在 0）交给子进程、再用追加模式写服务端头部，子进程第一笔输出就把头部整段覆盖。现在改成**先写完头部、再以 `"ab"` 打开句柄**；日志读取也**按字节**推进偏移量 —— 末尾多字节字符被截断后解码成 U+FFFD、重新编码是 3 字节，偏移量会持续漂移导致错位。

### 17. `display` 会盖掉 `hidden` 属性

浏览器默认 `[hidden] { display: none }` 特异性只有 **0-1-0**，只要任何一条类选择器里写了 `display`（`.modal` 是 `grid`、`.sources` 是 `flex`……），两者特异性相同、**后写的赢** —— `el.xxx.hidden = true` 设了也白设，症状是**点了「后台运行」按钮毫无反应**。修法是加一条钉死的兜底规则，`tests/test_frontend.mjs` 会断言它存在：

```css
[hidden] { display: none !important; }
```

### 18. 进度行必须覆盖而不是追加

`curl` 的进度表和 `tqdm` 的进度条都用 `\r` 回到行首重绘；日志解析若按 `\r` 拆成独立行，一次 1.4GB 下载就能刷出上千行 `0  0  0  0  0`。现在按**终止符**区分：`\n` 结尾是普通行、追加；`\r` 结尾是进度行、前端**覆盖上一行**，并做两层收敛（回放只留最后一帧、实时推送节流约 300ms），但**每一轮的最后一条一定发出**。下载顺序也调整成 **node + `fetch.mjs` → curl（`-sS`）→ Invoke-WebRequest**。

### 19. 顶层导入 `langchain_text_splitters` 会拖慢整个冷启动

它的包 `__init__` 会**立刻**导入 `SentenceTransformersTokenTextSplitter`，于是整个 `sentence_transformers`（含 `transformers`、`torch`）被一起拉起来：实测 `import langchain_text_splitters` 要 **9.49s**，而 `import langchain_core.documents` 只要 0.12s。它原本写在 `services/loader.py` 顶层（导入链是 `app.main -> routers.documents -> services.loader`），因此**端口要 10 秒后才开始监听**；现在改成首次真正需要切分时才导入（`_get_splitter_class()`），并在后台预热里补 `loader.preload()`，导入 `app.main` 从 **10.14s 降到 0.57s**。`tests/test_e2e.py` 第 9 节会在独立子进程里断言 `sys.modules` 中不含 `sentence_transformers` / `torch` / `langchain_text_splitters` / `faiss` 且耗时 < 6s。

### 20. 打开浏览器不能靠猜时间

后端没监听之前开浏览器，第一屏就是**「无法访问此页面 / 127.0.0.1 拒绝连接」**，而 Chrome 的错误页不会自己重试，很容易被判定成「启动失败」。原先 `start.ps1` 是「分离一个 `cmd /c ping -n 4` 等 3 秒再 `start` 浏览器」，时间点纯靠猜；现在只把地址交给后端：

```powershell
$env:RAG_OPEN_BROWSER_URL  = $BaseUrl   # 真实访问地址（含 -Port 指定的端口）
$env:RAG_AUTO_OPEN_BROWSER = '1'        # -NoBrowser 时置 0：只记录地址，不真的打开
```
后端在 lifespan 里调度 `core/browser.py` 的任务，**用真实 HTTP 请求探测目标地址**，拿到 2xx 之后才调用 `webbrowser.open`；探测显式禁用代理解析，超时 120 秒后放弃并写日志，404/500 一律不算就绪。

### 21. 只检查 `ollama.exe` 存在是不够的

便携版 zip 里除 `ollama.exe`（约 25MB）还有 `lib\ollama\` 下一整套推理运行时（约 1.3GB）；**解压中断会留下一个只有 `ollama.exe` 的假安装**：文件存在、`ollama serve` 能启动、`/api/tags` 能列出模型，只有真正生成回答时才报 `error starting llama-server: llama-server binary not found`。现在三道防线：`prepare.ps1` 校验 zip 能打开且解压后存在 `lib\ollama\llama-server.exe`，不完整就清理重来；`setup.portable_ollama_status()` 把它报成 blocking 问题 `ollama_incomplete` 并给出「重跑一键准备 / 改装官方安装包 / 手动重新解压」；`/api/health` 的 `problems` 里也会点名。

### 22. 删不掉的文件会让「一键安装」假装成功

用户点「一键安装」得到的是**退出码 0、日志全 [OK]，但 `llama-server.exe` 依然不存在**：Windows 不允许删除正在运行的程序，而 `Remove-Item -Recurse -Force $OllamaDir -ErrorAction SilentlyContinue` 中 `ollama.exe` 正被 `ollama serve` 占用、删除**静默失败**，紧接着 `Test-Path` 仍为真 → 判定「已装好」→ 跳过下载。现在把清理抽成 `scripts\lib\ollama-runtime.ps1` 里的纯函数、两个脚本共用：

```powershell
Stop-PortableOllama   # 只停「路径位于 tools\ollama 内」的进程，不碰系统安装
Remove-OllamaDir      # 停 → 删 → 再确认；返回 $false 表示仍被占用
```
调用方**必须**检查返回值，删不干净就 `exit 1` 并让用户关掉 Ollama 再试；`prepare.ps1` 末尾自检不完整同样以非零码退出。`tests\run_all.ps1` 的预检里有一个真实回归测试：用被占用的 `ollama.exe` 断言 `Remove-OllamaDir` 能停掉它并确认删除。

### 23. 下载源要按实测速度排序，`latest` 也不能照抄

修好 22 之后一键安装终于真的去下载了，又暴露两个问题：`fetch.mjs --github-asset ... latest` 返回 404（`latest` 不是真实标签名，正确端点是 `/releases/latest`）；默认下载源 `github.com/.../latest/download/...` 在这个网络里根本连不上。同一环境实测：`api.github.com` → `release-assets.githubusercontent.com` 有 **3.0 MB/s**，`ghproxy.net` 镜像只有 291 KB/s；所以按 `api.github.com 解析 → ghproxy.net 镜像 → github.com 直连` 依次尝试，换源时丢掉上一个源的 `.part`，`-Mirror` 把镜像提到最前。另外**先下载、后替换**，下载并校验通过之后才动现有文件。

### 24. 网页上的「已就绪」必须和后端判定一致

用户截图里出现过自相矛盾的画面：环境面板写「Ollama … 已就绪」，紧跟着的问题卡片却写「Ollama 安装不完整」—— 因为面板只判断了「`ollama.exe` 在不在」。现在 `renderEnvPanel()` 读后端返回的 `environment.ollama_portable`，把那一行分成**已就绪 / 不完整 / 缺失**三态，悬停还能看到「缺 lib\ollama 下的推理引擎（llama-server.exe），提问会失败」。

### 25. 只有 `\r` 的进度会被 PowerShell 吞掉

实测（`node` 写 `A\r`，6 秒后再写 `B\n`，重定向到文件）：3 秒后文件**仍是空的**，直到进程退出才出现 `A\r\nB\r\n` —— **PowerShell 5.1 按行转发原生命令的输出**，没有 `\n` 的内容一直攒在缓冲区，一次 1.4GB 的下载意味着日志里几十分钟一片空白。现在 `fetch.mjs` 分两种模式：交互式终端（`isTTY`）保留 `\r` 原地刷新；非交互（写日志、被 `installer.py` 重定向）改为**每 5 秒输出一行带 `\n` 的进度**。

### 26. 活着的子进程会让 `powershell -File` 永远不退出

一键安装日志全绿、退出码却永远不返回，网页停在「安装中…」：脚本最后一行都执行完了，宿主 `powershell.exe` 却继续挂着，杀掉它启动的 `ollama serve` 之后宿主**立刻**退出。原因是子进程只要还活着、并与父进程共享同一个控制台（`Start-Process` 不带 `-RedirectStandardInput` 时就是这样），父进程的控制台就不会结束；试过的绕法都无效，所以 `prepare.ps1` 仍在结尾停掉自己启动的 Ollama，并提示用 `scripts\start.cmd` 重新拉起。

### 27. 原生 thinking 通道：思维链不在 `content` 里

`deepseek-r1` 在 Ollama 上走**原生 thinking 通道**：思维链放在 `message.thinking` 字段，`langchain-ollama` 1.x 只在传了 `reasoning=True`（对应 `think: true`）时才取出来，键名是 `additional_kwargs["reasoning_content"]`。原代码只认 `additional_kwargs["thinking"]`，于是思维链被**静默丢弃**、界面长时间空白；实测同一问题：`thinking` 事件 0 → 251 个，总耗时 124s → **3.80s**，并恢复 `[1]` 角标。现在 `_build_llm()` 只在模型**确实支持**时才开这个通道（`POST /api/show` 的 `capabilities` 里有 `thinking` 才开），读取时两个键都认。

### 28. Ollama 是懒加载的，第一条提问要等模型载入

服务起来了、`/api/tags` 也列出了模型，但**权重直到第一次推理才载入显存**，本机实测要 **100 秒左右**，之后同一模型只要 0.4 秒。现在后端启动后会在**后台**发一次「只加载不生成」的请求，Ollama 收到不带 `prompt` 的请求会返回 `done_reason: "load"` 把模型预先载入；不想要可以设 `RAG_WARMUP_LLM=false`。

```http
POST /api/generate   {"model": "deepseek-r1:1.5b", "keep_alive": "10m"}
```

### 教程速查：谁负责关掉 Ollama

| 动作 | 结果 |
|------|------|
| 关掉 `RAG-QA.exe` 的面板窗口 | 弹确认 → 后端**和 Ollama 一起停**（勾了「退出时保留 Ollama」则只停后端） |
| 托盘图标 → 停止服务并退出 | 同上 |
| 在启动窗口按 `Ctrl+C`（start.cmd） | 后端退出 → 脚本**自动关掉 Ollama**（含本次拉起的、项目内置的、以及之前就在运行的） |
| `scripts\stop.ps1` | 停后端 **+ 停 Ollama** |
| `scripts\stop.ps1 -KeepOllama` | 只停后端 |
| `scripts\start.cmd -KeepOllama` | 启动时正常，退出时**保留** Ollama |
| 关掉那个 cmd 窗口（点 ×） | Windows 会连带结束同控制台的子进程，Ollama 也会停 |

### 29. 退出时要把 Ollama 一起收掉

不清理的话任务管理器里会长期挂着一个占着几百 MB 到 1GB 显存的进程。原来的清理有两个漏洞：**只在自己拉起过时才清理**（Ollama 之前就在运行时退出什么都不做），以及**靠进程名杀**（`Get-Process -Name ollama` 匹配不到 `ollama app` 和派生的 runner 子进程）。现在统一走 `Stop-PortableOllamaForProject`，按三种线索收口：启动时记下的 PID（按**进程树**杀）、进程路径在 `tools\ollama` 下、监听 11434 端口的进程（改用 `netstat -ano -p TCP` 解析，因为 `Get-NetTCPConnection` 在受限账户下会直接抛「拒绝访问」）。`-KeepOllama` 时进程留着，但后端仍会请求 `keep_alive=0`（`ollama_client.unload_model()`）让模型从显存里卸下来。实测证据（`/api/ps`）：

```
预热前运行列表: []
warmup_model -> True  耗时 62.7s
预热后运行列表: ['deepseek-r1:1.5b']
unload_model -> True
卸载后运行列表: []
```
真机验证（把后端进程结束掉，模拟程序退出）：

```
正在停止 Ollama ...
  （本次由启动脚本拉起，PID 22732）
[OK]   已停止 Ollama（PID 22732）
已停止。
```

### 30. 分点汇总全都显示成「1.」：是渲染器把列表切碎了

用户看到的分点汇总：

```
1. 特征共享
1. 任务协同
1. 数据利用
1. 模型简化
```

看起来像模型不会数数，实际是**渲染器的锅** —— deepseek-r1:1.5b 的原始输出是：
```
1. **特征提取的普适性**  ← 行尾两个空格（硬换行）
   多任务网络通过共享特征提取器……
                       ← 空行
2. **减少数据和参数**
   通过共享卷积网络……
```

旧渲染器只认「以 `- ` 或 `1. ` 开头的行」是列表项，上面这种缩进的说明行被当成**普通段落**，于是每轮都变成「只有一项的 `<ol>` + 段落 + 只有一项的 `<ol>`」，浏览器对每个 `<ol>` 都从 1 开始编号。`frontend/assets/js/markdown.js` 的列表部分因此重写成真正的块解析器：松散列表合并成一个 `<ol>`、缩进说明行归入同一个 `<li>`、子列表按缩进建树、`1．` / `1、` 也认（但 `3.14`、`2020 年第 2 期` 仍不会被误判），并补上硬换行。

### 31. 阈值 0.2 下总结不出文档内容：总结类问题根本不该用阈值筛

实测（真实 PDF + 真实嵌入）：**「总结一下」的最佳分（0.273）只比乱码（0.250）高一点**，而总结类提问问的是**整篇**、与任何片段相似度都低，所以「阈值定多少才能既不漏总结、又不放进噪声」本身无解。现在**按提问意图分流**（`rag.detect_intent`，纯规则、零额外延迟）：含「总结/概述/概括/摘要/主要内容/讲了什么…」且提问不超过 60 字判为 `overview` —— 忽略阈值、按阅读顺序**全篇均匀取样**（`summary_max_chunks` 块，默认 10；先去掉近重复；按上下文预算装箱；多文档按块数比例分名额，每篇保底 1 块）；其余判为 `qa` —— 相关度 top-k，阈值 + 相对窗口 + 兜底放宽。界面会显示 `全文概览` 或 `已放宽阈值` 徽标。

### 32. 检索噪声：页眉、参考文献、被切碎的段落

挤掉正文的是三类「结构性噪声」。**页眉**每页重复、是关键词拼盘，现在删掉「出现在 ≥60% 页面上、长度 ≤100 字」的重复行（**比较时去掉所有空白**，否则奇偶页差一个空格抓不到）。**参考文献**关键词最密集、语义最贫乏（实测「请总结这篇文档的主要内容」拿到 **0.420**），现在识别到独占一行的「参考文献 / References」且后面跟着 ≥2 条编号条目就整段截掉。**段落被硬切**：PDF 抽出来是硬换行，切分器只能在第 800 个字处硬切，实测含「1.1 多任务网络架构」的正文块因前半段是英文摘要，相似度从 **0.367 掉到 0.147**；现在按版面补回段落边界（小节标题另起一段、中英文切换处另起一段）。修复前后同一篇论文、`Q: 多任务网络的优势是什么`：
```
修复前 top-4：0.367 正文 | 0.331 页眉 | 0.281 页眉 | 0.254 页眉
修复后 top-4：0.310 结语（「（1）多任务网络优势。对于多个任务而言…」）
             0.283 1 研究思路 / 1.1 多任务网络架构
             0.258 2.1 实验数据
             0.249 0 引言
```

分块数 11 → 10，每块都是完整的一节。**这类改进存在索引里**：解析逻辑变了旧索引不会自动更新，所以界面加了「重建索引」按钮（`POST /api/documents/reindex`），用 `data/uploads` 里的原文件按当前配置重跑。

### 33. 跑测试会把用户的知识库删掉

`tests/test_e2e.py`、`tests/test_ollama_integration.py` 和 `tests/test_http.py` 都会清空知识库（`clear_all()` / `DELETE /api/documents`），而它们此前都指向真实的 `data/` 目录 —— **跑一次测试，用户上传的文档连文件一起没了**。现在三层防护：`app/core/paths.py` 支持 `RAG_DATA_DIR` 覆盖数据根目录；两个进程内套件在导入 `app.*` 之前把它指向 `.tmp/tests/...` 并断言数据目录不是真实的 `data/`；`tests/run_all.ps1` 总是**新起一个专用实例**（`RAG_DATA_DIR` → `.tmp/http-suite-data`，自动挑空闲端口）再跑 HTTP 验收。顺带修掉：`Invoke-Suite ... | Out-Null` 会把子进程 stdout 一起吞掉；HTTP 验收卡在「服务未就绪」是因为 **httpx 会读系统代理设置、连 `127.0.0.1` 也走代理**（拿到 502），测试客户端现在显式 `trust_env=False`。

### 34. 上下文窗口和资料长度必须自洽，否则模型会「失忆」

概览检索真机复验时出现过一次**空回答**（回答 0 字），加诊断打出 token 用量才看清机制：
```
num_ctx=4096, 10 段中文资料 → prompt_eval=3542, eval=1024, done_reason=length, 回答 0 字
```
资料 10 段约 6000 字，中文「1 字 ≈ 1 token」，光提示词就 3542 token，总需求超过 4096 的窗口 —— Ollama 会**滑动丢弃最早的 token**，被丢掉的正是系统提示词里的规则，模型既没了指令也没了状态，直接返回空且**没有任何报错**。现在 `RAG_LLM_NUM_CTX` 默认 4096 → **8192**、`RAG_LLM_NUM_PREDICT` 1024 → **2048**，新增 `RAG_LLM_REPEAT_PENALTY=1.2` 与 `RAG_LLM_REPEAT_LAST_N=512`，并让 `max_context_chars` 取 `min(max_context_chars, 由 num_ctx 反推的上限)`：
```
预算 = min(max_context_chars, num_ctx − 生成预留 − 系统提示词预留)
例：num_ctx=8192 → 6000 字；num_ctx=4096 → 2263 字；num_ctx=2048 → 880 字
```
真机复验（同一问题连问 3 次，`num_ctx=8192`）：
```
第 1 次: 回答 365 字 / 思维链 689 字 / 引用 1 / done_reason=stop
第 2 次: 回答 645 字 / 思维链 524 字 / 引用 0 / done_reason=stop
第 3 次: 回答 199 字 / 思维链 438 字 / 引用 0 / done_reason=stop
空回答 0/3，截断 0/3（改之前是 1/3 空回答、1/3 截断）
```

### 35. 引用编号 [n] 这条：1.5B 只能算「部分遵循」

界面上的引用角标依赖模型自己写 `[1]`；系统提示词里从第 2 条规则到结尾提醒都写了要求，真机实测仍然**不稳定**（同一问题连问 3 次：1 次标注、2 次不标）。结论（不是 bug，是模型能力）：`deepseek-r1:1.5b` 上引用标注属于「时有时无」，想要稳定的溯源角标请换更大的模型（`llama3.2` / `qwen2.5:3b` 起），或在 `.env` 里调大 `RAG_LLM_NUM_CTX` 后重试；检索侧（引用来源列表、点击查看原文分块）本身是确定性的。

### 36. 一体化启动器 RAG-QA.exe：薄封装，而不是把 Python 打进 exe

取向：PyInstaller onefile 要 1.5~3GB 且极易在解压阶段失败，Ollama 与模型权重仍得放在外面；.NET 9 self-contained 要目标机器装 .NET 9，而**本方案（.NET Framework 4.x + csc.exe）只有 34KB、瞬时启动、无运行时依赖**。另一条理由是**编排逻辑只能有一份**：启动/停止的坑都已写在 `scripts\start.ps1` / `stop.ps1` 里并有测试覆盖，exe 再实现一遍就是第二个真相来源；所以 exe 只做四件事：探测状态、按需拉起脚本、盯健康检查、优雅收尾。
```
RAG-QA.exe                      图形面板（双击，默认）
RAG-QA.exe --start [--port N]   后台启动并等健康检查通过
RAG-QA.exe --stop               停止后端与 Ollama（--keep-ollama 只停后端）
RAG-QA.exe --status [--json]    路径自检 + 运行状态
```
面板有状态、模型与知识库规模、启动/停止、打开页面、日志（`data\logs` 尾部）、两个勾选项（最小化到托盘 / 退出时保留 Ollama）和托盘菜单；**关窗口 = 停服务（含 Ollama）**，同一时刻只允许一个实例。编译 `scripts\build-launcher.cmd`，源码 `launcher\RagQaLauncher.cs`。**下面这些坑全是实测踩出来的**：

1. **`csc.exe` 只支持 C# 5**：源码里没有字符串插值、`?.`、表达式体成员，换来「目标机器零运行时安装」。
2. **`.cs` 必须有 UTF-8 BOM**：没有 BOM 就按 ANSI(936) 解析，中文全成乱码（`fix-encoding.ps1` 会把 `.cs` 一起检查）。
3. **`AttachConsole` 会覆盖已重定向的标准句柄**：`RAG-QA.exe --status > out.txt` 会写出 0 字节而退出码是 0，现在先判断 stdout 是否已被重定向。
4. **`Console.OutputEncoding` 在没有控制台时会抛异常**：它和 `Console.SetOut` 现在分开 try，否则 SetOut 被跳过、重定向的中文按 OEM 代码页编码。
5. **别给 `powershell.exe` 传 `-WindowStyle Hidden`**：那样 `start.ps1` 会卡在启动阶段而进程还活着；隐藏窗口要用 `ProcessStartInfo.WindowStyle`。
6. **不要用 `*> 文件` 收集 start.ps1 的输出**：PS 5.1 会把被重定向的原生命令 stderr 当成 NativeCommandError 终止错误；改用 `Start-Transcript`。
7. **PowerShell 不等待 GUI 子系统的程序**：`& RAG-QA.exe --status` 拿不到输出和退出码，必须借道 `cmd /c`（`Start-Process -RedirectStandardOutput` 在受限环境会「拒绝访问」）。
8. **别用 `Process.MainWindowTitle` 判断界面是否正常**：它只报告**可见**窗口，在 CI / 沙箱里永远为空；验收脚本改成 `EnumWindows` 枚举顶层窗口。
顺带修掉一个真 bug：`stop.ps1` 的「按端口兜底」用的是 `Get-NetTCPConnection`，它在受限账户下直接抛「拒绝访问」，表现就是点「停止」没反应；现在改用 `lib\ollama-runtime.ps1` 里的 `Get-PortListenerProcessId`（netstat 解析），启动器再加一层 `taskkill /T /F` 兜底。

### 37. 「使用过程中疯狂未响应」：探测绝不能在 UI 线程上做

判据是明确的：**Windows 认为「窗口线程 5 秒没取消息」就是未响应**。用 `SendMessageTimeout(WM_NULL)` 量化（探针见 `tests/hang-probe.ps1`）：
```
修复前：采样 62 次 / 阻塞 51 次（82.3%）   ← 几乎全程卡死
修复后：采样 100 次 / 阻塞 0 次（0.0%）/ 单次最长 1ms
```
根因不在探测逻辑「慢」，而在于**它跑在 UI 线程上，而且本机连关闭端口不会立刻被拒绝**：

```
20 个端口总耗时 40100ms（每个约 2.0 秒）—— SYN 被静默丢弃，而不是回 RST
（正常本机应当是毫秒级的 connection refused）
```
而旧代码每 2.5 秒在 UI 线程上顺序扫 `8000..8019` 共 21 个端口（每个先 TCP 再 HTTP），一个刷新周期就是几十秒。改法四层：探测全部搬到后台线程（UI 线程只做 `ApplyState()`，结果用 `BeginInvoke` 回投）；每次只探已知端口（≤2 个），完整扫描降级为 15 秒一次；先 TCP 预检再发 HTTP；启动/停止也异步化。回归测试：`tests/test_launcher.ps1` 会真的采样 UI 响应性 —— **服务未运行时 8 秒 + 服务运行中 6 秒，断言 0 次阻塞**。

---

### 38. 索引是离线产物：换了配置就必须当场发现

改 `RAG_EMBEDDING_MODEL_NAME` / `chunk_size` 后，磁盘上的旧索引不会重算：换模型可能在检索时抛 FAISS 内部断言，维度相同的模型则静默错乱，改切分参数更是完全静默（僵尸知识库）。现在把「索引是用什么建的」登记进注册表（模型名、**模型实际输出的维度**、`chunk_size` / `chunk_overlap`、解析开关、建立时间），检索前比对一次：

```
AssertionError: assert d == self.d
```

| 情况 | 判定 | 行为 |
|------|------|------|
| 模型名或维度不符 | `incompatible` | 拒绝检索，返回**中文可照做错误**（`IndexIncompatibleError`），`/api/chat` 与 `/api/search` 返回 **409** 而不是 500 |
| 切分/解析参数漂移 | `stale` | 检索仍可用，但 `/api/health` 转为 `degraded` 并写明「建议重建索引」 |
| 一致 | `ok` | 正常 |

维度取**模型真实输出**（载入时探测一次前向），而非配置里手写的声明值 —— 否则「换模型忘了同步 `RAG_EMBEDDING_DIMENSION`」会把错误固化进注册表。重建索引也分两种走法：只是切分参数变了就逐篇替换向量；**向量空间变了**则先整体清空再重新向量化。回归测试见 `tests/test_index_consistency.py` 第 3、4 节。

### 39. 清空知识库要连索引元信息一起清

清空知识库后（`registry.clear()`），注册表里依然写着「这是 all-MiniLM-L6-v2 建的、384 维、chunk_size=800」，而索引文件早被 `vector_store.reset()` 删掉了，于是出现自相矛盾的结论：一个文档都没有，却报告「索引与配置不一致」。现在 `registry.clear()` 与「删掉最后一个文档」都会调用 `clear_index_meta()` 一并清空；`compatibility()` 在没有索引时返回 `empty`，不做比对。旧注册表缺切分参数时一律走 `.get()`、缺字段当「不知道」，宁可不下结论也不误报。

### 40. GET 与 PUT 的配置字段必须对称

`GET /api/config` 会返回 `llm_num_ctx`、`dedupe_ratio`、`strip_boilerplate`、`embedding_model_name`、`max_upload_mb` 等字段，而 `PUT /api/config` 的模型里**根本没有这些字段**（pydantic 默认 `extra="ignore"`）；反过来 PUT 支持的 `expose_thinking` 又不在 GET 的返回里 ——「读到的」和「能写的」是两套。现在抽出 `ConfigValues` 作为共同字段表，`ConfigResponse` 继承它、`ConfigUpdate` 与它逐一对应（29 个字段），取值与应用都按字段表**逐项迭代**：

```python
# 少写一项会立刻 AttributeError，而不是悄悄少返回一个字段
return ConfigResponse(**{name: getattr(settings, name) for name in ConfigResponse.model_fields})
```
应用改动时只对**真正变了**的项动手（把 `embedding_model_name` 原样回写也会触发「重置嵌入实例 + 丢弃内存索引」），并加了两条交叉校验：`chunk_overlap >= chunk_size` 直接 400，扩展名列表归一化成小写带点且**保序去重**。

### 41. 幽灵导出，以及不会失效的缓存

两个「没人调用/没人检查」的死角：`installer.py` 的 `__all__` 里写着 `estimate_seconds`，而这个函数**从未被定义**（`__all__` 只在 `from x import *` 时参与运行），现在 `check_imports.py` 会遍历 `app` 包逐一 `hasattr` 校验；`ollama_client.invalidate_capability_cache()` 定义之后**没有任何生产代码调用**，于是重新 `ollama pull` 之后界面最长 5 分钟仍按旧结论跑。现在 TTL 收紧到 **60 秒**，并在运行期切换 LLM/嵌入模型、`GET /api/models`、一键安装成功结束、`/api/setup?fresh=true` 这些时刻主动失效（`invalidate_all()`）。顺带删掉死代码 `vector_store.search_documents()`（`POST /api/search` 的 `info` 已提供更完整的诊断）。

### 42. 概览取样不该为整个知识库打分

概览模式只取 `summary_max_chunks`（默认 10）段，但旧实现对整个知识库算分：

```python
scores, indices = store.index.search(vector, self.size)   # ① O(n·d) 全量扫描
for score, index in zip(scores[0], indices[0]): ...       # ② 再把 n 条塞进字典
```

两趟 O(n) 换来的 n 个分数里有 99% 算完就被丢掉。改成对**目标片段**各自 `reconstruct` 一条向量做点积，总代价从 O(n·d) 变成 O(k·d)，与知识库规模无关。实测微基准（384 维、n=20000、k=15）：

```
全量扫描 search(k=n)  3.67ms
reconstruct × 15      0.050ms
```
回归测试把「不许发生全库扫描」写成了断言：`overview_chunks()` 期间 `index.search` 被调用 **0 次**，且按需取分与全库扫描结果完全一致（最大偏差 0.00e+00）。

### 43. 扫描件的警告必须走到用户面前

扫描版 / 图片版 PDF 没有文字层，PyPDF 抽出来是空的；旧代码只在**日志**里 `logger.warning` 一句就继续走，用户看到的是「索引完成，共 0 个分块」。现在 `parse_document()` 返回 `ParseOutcome(chunks, char_count, warnings)`，平均每页字符数低于 `RAG_PDF_MIN_CHARS_PER_PAGE`（默认 120）时给一条中文警告，点明「疑似扫描件 / 检索将查不到内容 / 建议先用 OCR」；警告落进注册表 → 出现在 `POST /api/documents/upload` 的响应消息里、`GET /api/documents` 的 `warnings` 字段里（文档列表显示「⚠ 解析警告」徽标），重建索引时跟着**最新一次解析**走。

### 44. 前端测试不能只做「静态一致性」

原来的 `test_frontend.mjs` 用正则扫源码，确认 `$('setupMute')` 对应的 id 存在、`import` 的符号真的被导出 —— 能挡住拼写错误，但证明不了**行为**。现在多了一套 `test_frontend_interaction.mjs`（88 项断言）：把**真实的 app.js** 装进 `tests/dom-lite.mjs` 这个手写的 DOM 垫片里跑，用假的 `fetch` / `XHR` / SSE 喂数据，断言真实发生的 DOM 变化与网络请求（启动渲染、滑块写回 `PUT /api/config`、回车发送 → SSE 流式 → Markdown → 引用角标 → 点击溯源、停止生成中断流、上传/删除/清空的二次确认、索引告警与扫描件警告可见性、空知识库守卫、侧栏折叠）。垫片需要 `frontend/package.json` 里的 `"type": "module"` —— 那是给 Node 用的，浏览器按 `<script type="module">` 加载。

---

## 已知限制

### 嵌入模型的中文能力

`all-MiniLM-L6-v2` 只有 384 维、**以英文语料为主训练**，中文语义匹配偏弱（`tests/test_offline.py` 会打印实测数据：逐字/近义短语查询 Top-1 命中 4/4，口语化中文提问 3/4，而无关问题 0.60 vs 相关问题上限 0.76）。因此 `RAG_SCORE_THRESHOLD` 默认刻意设为 `0.20`：宁可多召回几段让 LLM 判断，也不要漏掉正确片段。**知识库以中文为主时强烈建议换模型**（改 `.env` 即可，无需改代码）：

```ini
RAG_EMBEDDING_MODEL_NAME=BAAI/bge-small-zh-v1.5
RAG_EMBEDDING_DIMENSION=512
```
下载：`scripts\download_models.py --model BAAI/bge-small-zh-v1.5`。**换模型后必须重建索引**——不同模型的向量空间不通用。

### LLM 能力

`deepseek-r1:1.5b` 只有 15 亿参数：能较好地「照文档摘要」，但复杂推理、多跳问题会吃力；**引用编号 `[n]` 时有时无**（引用来源列表和点击溯源本身是确定性的）；偶尔「同一句话连写四遍」，已默认加 `RAG_LLM_REPEAT_PENALTY=1.2` 缓解但不能根除；生成上限已从 1024 提到 2048，否则概览类回答会写到一半停住（`done_reason=length`）；建议 `RAG_TOP_K` 控制在 3~5（总结类问题例外，由 `RAG_SUMMARY_MAX_CHUNKS` 控制）。追求质量可换 `qwen2.5:3b` 或 `llama3.2`，引用标注的稳定性会明显更好。

### 其他

* 目前只支持单用户本地使用，没有鉴权（服务默认只绑定 `127.0.0.1`）
* 对话历史保存在前端内存中，刷新页面即清空
* 扫描版 PDF（纯图片）无法提取文字，需要先做 OCR；程序会在平均每页抽取字符数过低时给出解析警告（`RAG_PDF_MIN_CHARS_PER_PAGE`）
* PDF 里的**图和表是图片**，其中的文字抽不出来（这类内容检索不到）

---

## 常见问题

**启动时提示「本地嵌入模型不存在」** 执行 `pwsh -File scripts\prepare.ps1`，或只下模型：`.venv\Scripts\python.exe scripts\download_models.py`

**提问时报「无法连接 Ollama」** 运行 `scripts\start.ps1`（会自动拉起），或手动 `ollama serve`；确认 `http://127.0.0.1:11434/api/tags` 能访问。

**报「Ollama 中没有模型 xxx」** 执行 `ollama pull deepseek-r1:1.5b`。

**RAG-QA.exe 的面板一直显示「未响应」** 旧版 exe 会在 UI 线程上做网络探测，重新编译一次：`scripts\build-launcher.cmd`。想量化确认可跑 `powershell -File tests\hang-probe.ps1 -Seconds 20`，正常应为「阻塞 0 次」。

**改了 .ps1 之后脚本报一堆 `Unexpected token` / 中文变乱码** 编辑工具保存 `.ps1` 时常常丢掉 UTF-8 BOM，Windows PowerShell 5.1 于是按 GBK 解析。先跑修复器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fix-encoding.ps1
```

修复器**自己**也可能被弄丢 BOM，那时它连启动都失败，用这一行手动补（只补 BOM，不动内容）：

```powershell
$p='scripts\fix-encoding.ps1'; $b=[IO.File]::ReadAllBytes($p)
if ($b[0] -ne 0xEF) { [IO.File]::WriteAllText($p,[IO.File]::ReadAllText($p,[Text.UTF8Encoding]::new($false)),[Text.UTF8Encoding]::new($true)) }
```

**提问时报 `error starting llama-server: llama-server binary not found`** Ollama **装了一半**：`tools\ollama\` 下只有 `ollama.exe`，缺 `lib\ollama\` 里的推理运行时；这种状态服务能启动、`/api/tags` 也能列出模型，容易被误判成「一切正常」。修复任选其一：重新双击 `scripts\prepare.cmd`（会先体检、停掉正在运行的 Ollama、清目录、重下重解压，需联网约 1.4GB）；改装官方安装包 <https://ollama.com/download> 后删掉不完整的 `tools\ollama\`；或自己下好 zip 后删目录再 `Expand-Archive`。网页首屏的配置引导会主动报出这个问题（`ollama_incomplete`）。

**点了「一键开始安装」，日志一路 [OK] 但问题依旧** 只可能发生在旧版本上：Windows 不允许删除正在运行的程序，而旧代码把删除失败静默忽略了；当前版本会直接报错退出并把「先关掉 Ollama」写清楚，确认 `scripts\lib\ollama-runtime.ps1` 存在。

**点了「一键开始安装」，卡在「安装中…」一直不结束** 旧版本把新装的 Ollama 留着继续跑，导致宿主 `powershell.exe` 卡在退出阶段；当前版本会在结尾收掉它，并提示用 `scripts\start.cmd` 重新拉起。

**下载了 1.4GB 却看不到任何进度** 旧版本下载器只用 `\r` 刷进度，而 PowerShell 按行转发输出，于是日志长时间空白；当前版本在写日志时每 5 秒输出一行进度。

**回答总是「根据已有文档无法回答该问题」** 说明检索没召回相关片段：换更贴近原文的措辞提问、在设置里调低相似度阈值，或换中文嵌入模型（见上文）。

**中文 PDF 解析出乱码** 文档本身可能是非 UTF-8 编码或扫描件，可先用文本编辑器另存为 UTF-8 再上传。

**上传成功了，但这份文档怎么问都问不出来** 先看文档列表里有没有「⚠ 解析警告」徽标。扫描版 / 图片版 PDF 没有文字层、入库分块可能是 0 个，这时换问法、调阈值都没用，要改用文字版 PDF 或先做 OCR（界面提示来自 `RAG_PDF_MIN_CHARS_PER_PAGE`，默认平均每页 120 字符）。

**回答突然报错「索引与当前嵌入配置不一致」/ 界面提示「必须重建」** 你改过 `RAG_EMBEDDING_MODEL_NAME`（或 `RAG_EMBEDDING_DIMENSION`），而磁盘上的索引还是旧模型建的，两个向量空间不通用，所以程序直接拒绝检索（HTTP 409）。出路二选一：点左侧「知识库」面板里的**重建索引**（原始文件都在 `data/uploads`，不会丢文档）；或把配置改回原来的模型。

**改了 `RAG_CHUNK_SIZE` 但感觉没生效** 分块参数只影响**新入库**的文档，旧索引不会自动重新分块；程序会在 `/api/health` 里报「索引与当前配置不一致」并建议重建 —— 点一次「重建索引」即可，重建前会自动把旧索引备份到 `data/index/backups/`。

**重建索引时会不会丢文档？** 不会。重建用的是 `data/uploads/` 里的原始文件，并且默认会先把当前索引备份一份到 `data/index/backups/<时间戳>/`（保留最近 `RAG_INDEX_BACKUP_KEEP` 份）；万一中途失败，把备份里的 `.faiss` / `.pkl` 拷回 `data/index/` 就能还原。

**端口 8000 被占用** `pwsh -File scripts\start.ps1 -Port 8080`

**启动后网页先显示「无法访问此页面 / 127.0.0.1 拒绝连接」** 正常启动不会再出现。若仍然出现，说明浏览器是在服务起来之前被手动打开的 —— 等 1~2 秒按 `F5` 刷新即可。想确认服务有没有起来，看启动窗口有没有打印「服务已就绪（x.xs），正在打开浏览器」，或直接访问 `http://127.0.0.1:8000/api/health`。想换回自己控制浏览器：`powershell -File scripts\start.ps1 -NoBrowser`。

**打包拷贝时提示某些目录「访问被拒绝」** 只会在一种情况下出现：`.tmp\` 下残留了由 `tempfile.mkdtemp()` 创建、权限受限的目录；当前代码已不使用 `mkdtemp`，若确实存在手工删除即可：

```powershell
cmd /c "rmdir /s /q .tmp" ; cmd /c "rmdir /s /q diag"
```

同上的编码问题也会被 `tests\run_all.ps1` 的预检在测试开始前拦下，不必等运行时才发现。
