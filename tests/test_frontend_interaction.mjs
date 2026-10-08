/**
 * 前端**交互**验收测试（真实 DOM + 假后端，Node 直接跑）。
 * 把真实 app.js 装进 tests/dom-lite.mjs 的 DOM 垫片，用假 fetch/XHR/SSE 喂数据，
 * 断言用户操作之后真正发生了什么（渲染、请求、中断、二次确认）——
 * 这是 test_frontend.mjs 的正则静态检查证明不了的部分。
 */

import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { installDom, jsonResponse, sleep, sseFrame, waitFor } from './dom-lite.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');
const jsDir = join(root, 'frontend', 'assets', 'js');
const html = await readFile(join(root, 'frontend', 'index.html'), 'utf8');

let passed = 0;
let failed = 0;

function check(label, condition, detail = '') {
  if (condition) {
    passed += 1;
    console.log(`  [PASS] ${label}${detail ? `  (${detail})` : ''}`);
  } else {
    failed += 1;
    console.log(`  [FAIL] ${label}${detail ? `  (${detail})` : ''}`);
  }
}

function section(title) {
  console.log('');
  console.log('-'.repeat(70));
  console.log(title);
  console.log('-'.repeat(70));
}

console.log('='.repeat(70));
console.log('前端交互测试（真实 DOM + 假后端）');
console.log('='.repeat(70));

/* ================================================================== */
/* 假后端                                                              */
/* ================================================================== */
// 与 backend/app/services/loader.py 的 low_extraction_warning() 保持同一套措辞
const SCANNED_WARNING = '疑似扫描件 / 图片版 PDF：4 页平均只抽取到 0 个字符'
  + '（低于 120）。正文很可能根本没有文字层，检索将查不到这份文档的内容。'
  + '建议改用文字版 PDF，或先用 OCR 工具把图片转成文字再上传。';

const CHUNKS = [
  {
    faiss_id: 'c1', chunk_index: 0, page: 1, char_count: 120,
    content: '《差旅报销管理办法》住宿费一线城市每晚 600 元。',
  },
];

const backend = {
  calls: [],
  documents: [
    {
      doc_id: 'd1', filename: 'travel.txt', stored_name: 'travel__1.txt', extension: '.txt',
      size_bytes: 2048, sha256: 'a'.repeat(64), chunk_count: 5, char_count: 900,
      created_at: '2026-01-01T00:00:00+08:00', status: 'indexed', error: null, warnings: [],
    },
    {
      doc_id: 'd2', filename: 'scan.pdf', stored_name: 'scan__2.pdf', extension: '.pdf',
      size_bytes: 99999, sha256: 'b'.repeat(64), chunk_count: 0, char_count: 12,
      created_at: '2026-01-01T00:00:00+08:00', status: 'indexed', error: null,
      warnings: [SCANNED_WARNING],
    },
  ],
  health: null,
  chatFrames: [],
  chatDelayMs: 0,
  xhrInstances: [],
  uploadResult: null,
};

function buildHealth(overrides = {}) {
  const index = {
    state: 'ok', compatible: true, stale: false, reasons: [], notices: [],
    dimension: { index: 384, model: 384, recorded: 384, declared: 384 },
    index_meta: { embedding_model: 'sentence-transformers/all-MiniLM-L6-v2', dimension: 384 },
    current: {},
  };
  return {
    status: 'ok',
    version: '1.0.0',
    app_name: '离线 RAG 文档问答',
    offline: true,
    ollama_reachable: true,
    llm_model: 'deepseek-r1:1.5b',
    llm_model_available: true,
    ollama_models: ['deepseek-r1:1.5b'],
    embedding_model: 'sentence-transformers/all-MiniLM-L6-v2',
    embedding_ready: true,
    embedding_device: 'cpu',
    index_ready: true,
    document_count: backend.documents.length,
    chunk_count: backend.documents.reduce((sum, d) => sum + d.chunk_count, 0),
    detail: {
      problems: [],
      embedding: { local_ready: true, loaded: true, dimension: 384, actual_dimension: 384 },
      ollama_install: { present: true, complete: true },
      vector_store: { ready: true, vectors: 5 },
      index,
      index_compatible: index.compatible,
      index_stale: index.stale,
      data_dir: join(root, '.tmp', 'frontend-interaction'),
    },
    ...overrides,
  };
}

backend.health = buildHealth();

globalThis.fetch = async (url, options = {}) => {
  const method = (options.method || 'GET').toUpperCase();
  const raw = String(url);
  const path = raw.split('?')[0];
  let body = null;
  if (options.body && typeof options.body === 'string') {
    try { body = JSON.parse(options.body); } catch { body = options.body; }
  }
  backend.calls.push({ method, path, url: raw, body });

  if (method === 'GET' && path === '/api/health') return jsonResponse(backend.health);

  if (method === 'GET' && path === '/api/documents') {
    return jsonResponse({
      documents: backend.documents,
      total: backend.documents.length,
      total_chunks: backend.documents.reduce((sum, d) => sum + d.chunk_count, 0),
    });
  }

  if (method === 'GET' && path === '/api/setup') {
    return jsonResponse({
      ready: true, blocking_count: 0, warning_count: 0, headline: '所有组件均已就绪',
      issues: [], environment: { project_root: root, can_auto_install: true },
      checked_at: '2026-01-01T00:00:00+08:00',
    });
  }

  if (method === 'GET' && path === '/api/setup/install') {
    return jsonResponse({ status: 'idle', running: false, returncode: null, mode: {} });
  }

  if (method === 'PUT' && path === '/api/config') return jsonResponse({ ...body });

  if (method === 'GET' && /^\/api\/documents\/[^/]+\/chunks$/.test(path)) {
    return jsonResponse({
      doc_id: 'd1', filename: 'travel.txt', chunk_count: CHUNKS.length,
      returned: CHUNKS.length, chunks: CHUNKS,
    });
  }

  if (method === 'POST' && path === '/api/documents/reindex') {
    return jsonResponse({
      documents: backend.documents.length, rebuilt: backend.documents.length, failed: 0,
      chunks_before: 5, chunks_after: 7, index_state_before: 'ok',
      rebuilt_from_scratch: false, backup_dir: join(root, 'data', 'index', 'backups', '20260101-000000'),
      details: [],
    });
  }

  if (method === 'DELETE' && path === '/api/documents') {
    const removed = backend.documents.reduce((sum, d) => sum + d.chunk_count, 0);
    backend.documents = [];
    return jsonResponse({ doc_id: '*', deleted_chunks: removed, message: '知识库已清空' });
  }

  if (method === 'DELETE' && /^\/api\/documents\/[^/]+$/.test(path)) {
    const docId = decodeURIComponent(path.split('/').pop());
    backend.documents = backend.documents.filter((d) => d.doc_id !== docId);
    return jsonResponse({ doc_id: docId, deleted_chunks: 5, message: '已删除文档及其 5 个分块' });
  }

  if (method === 'POST' && path === '/api/chat') {
    if (backend.chatDelayMs > 0) {
      const frames = backend.chatFrames.map(([event, data]) => sseFrame(event, data));
      const signal = options.signal;
      const abortError = () => {
        const error = new Error('aborted');
        error.name = 'AbortError';
        return error;
      };
      return {
        ok: true,
        status: 200,
        body: {
          getReader() {
            let index = 0;
            return {
              async read() {
                if (signal?.aborted) throw abortError();
                if (index >= frames.length) return { done: true, value: undefined };
                await sleep(backend.chatDelayMs);
                if (signal?.aborted) throw abortError();
                const value = new TextEncoder().encode(frames[index]);
                index += 1;
                return { done: false, value };
              },
              releaseLock() {},
            };
          },
        },
      };
    }
    const text = backend.chatFrames.map(([event, data]) => sseFrame(event, data)).join('');
    return sseResponseFrom(text);
  }

  if (method === 'GET' && path === '/api/models') {
    return jsonResponse({ models: [], current: 'deepseek-r1:1.5b' });
  }

  return jsonResponse({ detail: `未模拟的接口：${method} ${path}` }, 404);
};

function sseResponseFrom(text) {
  const bytes = new TextEncoder().encode(text);
  const half = Math.max(1, Math.ceil(bytes.length / 2));
  const pieces = [bytes.slice(0, half), bytes.slice(half)];
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        let index = 0;
        return {
          async read() {
            if (index >= pieces.length) return { done: true, value: undefined };
            const value = pieces[index];
            index += 1;
            return { done: false, value };
          },
          releaseLock() {},
        };
      },
    },
  };
}

/* ---- 假的 XHR / FormData（上传走的是 XMLHttpRequest，不是 fetch）---- */
globalThis.FormData = class FakeFormData {
  constructor() { this.entries = []; }
  append(name, file, filename) { this.entries.push({ name, file, filename }); }
};

globalThis.XMLHttpRequest = class FakeXhr {
  constructor() {
    this.upload = {};
    this.status = 0;
    this.responseText = '';
    backend.xhrInstances.push(this);
  }

  open(method, url) {
    this.method = method;
    this.url = url;
    // 真实实现里 open 之后才会被设 onprogress/onload，这里先把钩子准备好
    this.onload = null;
    this.onerror = null;
    this.onabort = null;
  }

  send(form) {
    this.form = form;
    FakeXhr.lastInstance = this;
    this.upload.onprogress?.({ lengthComputable: true, loaded: 40, total: 100 });
    const result = backend.uploadResult || [];
    this.status = 200;
    this.responseText = JSON.stringify(result);
    // 异步回调更贴近真实浏览器（app.js 的 finally 依赖 promise 微任务）
    setTimeout(() => this.onload?.(), 0);
  }

  abort() { this.onabort?.(); }
};

/* ================================================================== */
/* 装载 app.js                                                         */
/* ================================================================== */
const { document } = installDom(html);
const $ = (id) => document.getElementById(id);

let bootError = null;
try {
  await import(pathToFileURL(join(jsDir, 'app.js')).href);
} catch (error) {
  bootError = error;
}

check('import frontend/assets/js/app.js 不抛异常', bootError === null,
  bootError ? `${bootError.name}: ${bootError.message}` : '');

if (bootError) {
  console.log('\n启动即失败，后续交互测试无法进行。');
  process.exit(1);
}

/* ================================================================== */
section('1. 启动：健康检查 / 文档列表 / 示例问题真的渲染出来了');
await waitFor(() => $('statusText').textContent !== '正在检查服务…');
// boot() 里健康检查和文档列表是两次独立请求，状态栏先更新完，列表稍后到
await waitFor(() => $('docList').children.length === 2);

check('状态栏显示就绪与文档数', $('statusText').textContent === '就绪 · 2 文档',
  $('statusText').textContent);
check('状态点变成 is-ok（不是红点）', $('statusDot').className.includes('is-ok'),
  $('statusDot').className);
check('模型标签带模型名与嵌入模型', $('modelLabel').textContent.includes('deepseek-r1:1.5b')
  && $('modelLabel').textContent.includes('all-MiniLM-L6-v2'), $('modelLabel').textContent);

check('文档计数徽标 = 2', $('docCount').textContent === '2', $('docCount').textContent);
check('文档列表渲染出 2 条', $('docList').children.length === 2,
  `${$('docList').children.length} 条`);
check('空提示被隐藏', $('docEmpty').hidden === true);

const firstItem = $('docList').children[0];
check('文档条目显示分块数与体积',
  firstItem.textContent.includes('5 块') && firstItem.textContent.includes('2.0 KB'),
  firstItem.textContent.replace(/\s+/g, ' '));

// 扫描件警告必须出现在界面上（后端只在日志里 warning 是不够的）
const scannedItem = $('docList').children[1];
check('扫描件文档带 ⚠ 解析警告徽标', scannedItem.textContent.includes('解析警告'),
  scannedItem.textContent.replace(/\s+/g, ' '));
check('警告徽标的 tooltip 带完整原因',
  (scannedItem.querySelector('.doc-item__warn')?.title || '').includes('扫描件'),
  scannedItem.querySelector('.doc-item__warn')?.title?.slice(0, 30) || '(无)');
check('警告条目带 is-warned 样式钩子', scannedItem.classList.contains('is-warned'));

check('示例问题渲染出 4 个', $('examples').children.length === 4,
  `${$('examples').children.length} 个`);
check('配置就绪时引导弹窗保持关闭', $('setupModal').hidden === true);

/* ================================================================== */
section('2. 检索参数：滑块与开关要真的写回后端与本地');
const putCalls = () => backend.calls.filter((c) => c.method === 'PUT' && c.path === '/api/config');

$('topK').value = '7';
$('topK').fire('input');
await waitFor(() => putCalls().length >= 1);
check('拖动召回片段数滑块更新了输出', $('topKOut').textContent === '7', $('topKOut').textContent);
check('召回片段数写回 PUT /api/config',
  putCalls().some((c) => c.body?.top_k === 7), JSON.stringify(putCalls()[0]?.body));
check('本地设置已落盘（刷新页面后仍生效）',
  JSON.parse(globalThis.localStorage.getItem('rag-qa.settings.v1')).topK === 7,
  globalThis.localStorage.getItem('rag-qa.settings.v1'));

$('threshold').value = '0.35';
$('threshold').fire('input');
await waitFor(() => putCalls().some((c) => c.body?.score_threshold === 0.35));
check('阈值滑块更新了输出', $('thresholdOut').textContent === '0.35', $('thresholdOut').textContent);
check('阈值写回 PUT /api/config', putCalls().some((c) => c.body?.score_threshold === 0.35));

$('thinkingToggle').checked = false;
$('thinkingToggle').fire('change');
await waitFor(() => putCalls().some((c) => c.body?.expose_thinking === false));
check('关闭推理过程开关写回 PUT /api/config',
  putCalls().some((c) => c.body?.expose_thinking === false));

/* ================================================================== */
section('3. 提问：回车发送 → SSE 流式渲染 → 引用角标');
backend.chatFrames = [
  ['meta', {
    model: 'deepseek-r1:1.5b', mode: 'overview', top_k: 7, score_threshold: 0.35,
    effective_threshold: 0.1, best_score: 0.4211, relaxed: true, candidates: 12,
    chunks_total: 140, source_count: 2,
  }],
  ['token', { delta: '住宿费标准如下：\n\n- **一线城市**：每晚 600 元 [1]\n' }],
  ['token', { delta: '- **其他城市**：每晚 400 元 [2]\n' }],
  ['sources', {
    sources: [
      { index: 1, doc_id: 'd1', filename: 'travel.txt', page: 2, chunk_index: 0, score: 0.4211, content: '一线城市每晚 600 元。' },
      { index: 2, doc_id: 'd1', filename: 'travel.txt', page: 3, chunk_index: 1, score: 0.3980, content: '其他城市每晚 400 元。' },
    ],
    cited: [1],
    thinking: null,
  }],
  ['done', { elapsed_ms: 1234, cited: [1], answer_chars: 60, usage: { eval_count: 88 }, mode: 'overview', relaxed: true }],
];

$('question').value = '总结一下差旅报销标准';
$('question').fire('keydown', { key: 'Enter', shiftKey: false, isComposing: false });
await waitFor(() => $('sendBtn').disabled === true);

check('提问后输入框被清空', $('question').value === '', `"${$('question').value}"`);
check('生成期间发送按钮禁用', $('sendBtn').disabled === true);
check('生成期间出现「停止生成」', $('stopBtn').hidden === false);
check('用户消息已追加到对话区', $('chat').querySelectorAll('.msg--user').length === 1);
check('提问内容经过 Markdown 渲染', $('chat').querySelector('.msg--user').innerHTML.includes('总结一下差旅报销标准'));

const chatCall = backend.calls.filter((c) => c.path === '/api/chat').pop();
check('提问请求带上了滑块里的参数',
  chatCall?.body?.top_k === 7 && chatCall?.body?.score_threshold === 0.35,
  JSON.stringify({ top_k: chatCall?.body?.top_k, score_threshold: chatCall?.body?.score_threshold }));
check('提问请求走 SSE（stream=true）', chatCall?.body?.stream === true);

await waitFor(() => $('sendBtn').disabled === false);
const answer = $('chat').querySelector('.msg--assistant');
check('流式结束后恢复发送按钮', $('sendBtn').disabled === false);
check('结束后隐藏「停止生成」', $('stopBtn').hidden === true);
check('回答渲染成 Markdown 列表',
  answer.querySelectorAll('.md li').length === 2,
  `${answer.querySelectorAll('.md li').length} 个 li`);
check('回答里的加粗生效', answer.innerHTML.includes('<strong>一线城市</strong>'));

check('引用角标渲染了 2 个', answer.querySelectorAll('.cite').length === 2,
  `${answer.querySelectorAll('.cite').length} 个`);
check('角标带 data-cite 便于溯源',
  answer.querySelector('.cite')?.getAttribute('data-cite') === '1',
  answer.querySelector('.cite')?.getAttribute('data-cite'));

const metaLine = answer.querySelector('.msg__meta').textContent;
check('元信息标出「全文概览」策略', metaLine.includes('全文概览'), metaLine.replace(/\s+/g, ' '));
check('元信息标出「已放宽阈值」', metaLine.includes('已放宽阈值'));
check('元信息带索引规模', metaLine.includes('索引 140 块'));
check('元信息带召回段数与耗时',
  metaLine.includes('召回 2 段') && metaLine.includes('耗时 1.2s'), metaLine.replace(/\s+/g, ' '));
check('元信息带 token 数', metaLine.includes('88 tokens'));

/* ================================================================== */
section('4. 来源：点角标 / 点来源卡片 / Esc 关闭抽屉');
check('来源卡片渲染了 2 个', answer.querySelectorAll('.source-chip').length === 2,
  `${answer.querySelectorAll('.source-chip').length} 个`);
check('被引用的来源高亮、未引用的变暗',
  answer.querySelectorAll('.source-chip.is-cited').length === 1
  && answer.querySelectorAll('.source-chip.is-dim').length === 1);

$('sourceDrawer').hidden = true;
answer.querySelector('.cite').fire('click');
check('点击引用角标打开来源抽屉', $('sourceDrawer').hidden === false);
check('抽屉里显示该片段原文',
  $('sourceBody').textContent.includes('一线城市每晚 600 元'),
  $('sourceBody').textContent.slice(0, 40));
check('抽屉标题带引用序号与文件名',
  $('sourceDrawerTitle').textContent.includes('[1]')
  && $('sourceDrawerTitle').textContent.includes('travel.txt'),
  $('sourceDrawerTitle').textContent);

document.fire('keydown', { key: 'Escape' });
check('Esc 关闭来源抽屉', $('sourceDrawer').hidden === true);

answer.querySelectorAll('.source-chip')[1].fire('click');
check('点击来源卡片打开抽屉', $('sourceDrawer').hidden === false);
check('详情里带相似度与页码',
  $('sourceBody').textContent.includes('0.3980') && $('sourceBody').textContent.includes('第 3 页'),
  $('sourceBody').textContent.replace(/\s+/g, ' ').slice(0, 80));

/* ================================================================== */
section('5. 分块预览：点文件名拉取 /api/documents/{id}/chunks');
document.fire('keydown', { key: 'Escape' });
$('docList').children[0].querySelector('.doc-item__name').fire('click');
await waitFor(() => $('sourceBody').textContent.includes('差旅报销管理办法'));
check('打开分块预览抽屉', $('sourceDrawer').hidden === false);
check('抽屉标题标明是分块预览', $('sourceDrawerTitle').textContent.includes('分块预览'),
  $('sourceDrawerTitle').textContent);
check('预览内容来自后端',
  $('sourceBody').textContent.includes('住宿费一线城市每晚 600 元'),
  $('sourceBody').textContent.replace(/\s+/g, ' ').slice(0, 60));

/* ================================================================== */
section('6. 系统状态抽屉：索引一致性告警必须在界面上看得见');
backend.health = buildHealth({ status: 'degraded' });
backend.health.detail.index = {
  state: 'incompatible', compatible: false, stale: true,
  reasons: ['索引由嵌入模型 sentence-transformers/all-MiniLM-L6-v2 建立，当前配置为 BAAI/bge-small-zh-v1.5。'],
  notices: [],
  dimension: { index: 384, model: 512, recorded: 384, declared: 512 },
  index_meta: { embedding_model: 'sentence-transformers/all-MiniLM-L6-v2', dimension: 384 },
  current: {},
};
backend.health.detail.index_compatible = false;
backend.health.detail.index_stale = true;
backend.health.detail.problems = ['索引与当前嵌入模型不一致（维度 384 → 512）。请在左侧点击「重建索引」。'];

$('healthBtn').fire('click');
await waitFor(() => $('healthDrawer').hidden === false);
const healthBody = $('healthBody').textContent;
check('系统状态抽屉打开', $('healthDrawer').hidden === false);
check('列出需要处理的问题', healthBody.includes('需要处理'), healthBody.slice(0, 40));
check('索引一致性一行显示「必须重建」', healthBody.includes('必须重建'),
  healthBody.replace(/\s+/g, ' ').slice(0, 200));
check('给出重建索引的具体指引', healthBody.includes('重建索引') && healthBody.includes('不会丢文档'));
check('原因里点明两个模型名',
  healthBody.includes('all-MiniLM-L6-v2') && healthBody.includes('bge-small-zh-v1.5'));
check('索引维度差异被展示出来', healthBody.includes('384') && healthBody.includes('512'));

document.fire('keydown', { key: 'Escape' });
check('Esc 关闭状态抽屉', $('healthDrawer').hidden === true);
backend.health = buildHealth();

/* ================================================================== */
section('7. 重建索引：二次确认 → 请求 → 按钮状态复原');
globalThis.window.confirm = () => false;
const reindexBefore = backend.calls.filter((c) => c.path === '/api/documents/reindex').length;
$('reindexBtn').fire('click');
await sleep(30);
check('取消确认时不发重建请求',
  backend.calls.filter((c) => c.path === '/api/documents/reindex').length === reindexBefore);

globalThis.window.confirm = () => true;
$('reindexBtn').fire('click');
await waitFor(() => backend.calls.filter((c) => c.path === '/api/documents/reindex').length > reindexBefore);
check('确认后发出重建请求',
  backend.calls.filter((c) => c.path === '/api/documents/reindex').length === reindexBefore + 1);
await waitFor(() => $('reindexBtn').disabled === false);
check('重建结束后按钮恢复可用', $('reindexBtn').disabled === false);
check('按钮文案复原', $('reindexBtn').textContent === '重建索引', $('reindexBtn').textContent);
check('toast 报告重建结果',
  $('toastWrap').textContent.includes('已重建 2 个文档')
  && $('toastWrap').textContent.includes('分块 5 → 7'),
  $('toastWrap').textContent.replace(/\s+/g, ' ').slice(0, 60));

/* ================================================================== */
section('8. 上传：点投放区 → change → XHR 上传 → 列表刷新');
let fileInputClicks = 0;
$('fileInput').addEventListener('click', () => { fileInputClicks += 1; });
$('dropzone').fire('click');
check('点击投放区会唤起文件选择框', fileInputClicks === 1, `${fileInputClicks} 次`);

backend.uploadResult = [{
  document: {
    doc_id: 'd3', filename: 'policy.txt', stored_name: 'policy__3.txt', extension: '.txt',
    size_bytes: 100, sha256: 'c'.repeat(64), chunk_count: 3, char_count: 300,
    created_at: '2026-01-01T00:00:00+08:00', status: 'indexed', error: null, warnings: [],
  },
  message: '《policy.txt》索引完成，共 3 个分块',
}];
backend.documents = [...backend.documents, backend.uploadResult[0].document];

$('fileInput').files = [{ name: 'policy.txt', size: 100 }];
$('fileInput').fire('change');
await waitFor(() => backend.xhrInstances.length >= 1);
const xhr = backend.xhrInstances[backend.xhrInstances.length - 1];
check('上传走 POST /api/documents/upload', xhr.method === 'POST'
  && xhr.url === '/api/documents/upload', `${xhr.method} ${xhr.url}`);
check('表单里带着选中的文件', xhr.form?.entries?.length === 1
  && xhr.form.entries[0].name === 'files', JSON.stringify(xhr.form?.entries?.map((e) => e.name)));

await waitFor(() => $('docCount').textContent === '3');
check('上传后列表刷新为 3 条', $('docCount').textContent === '3', $('docCount').textContent);
check('上传成功后清除 file input（同一文件可再次选择）', $('fileInput').value === '');
check('上传进度条已收起', $('uploadProgress').hidden === true);

/* ================================================================== */
section('9. 上传带解析警告的文档：不能只报「处理成功」');
backend.uploadResult = [{
  document: {
    doc_id: 'd4', filename: 'scan2.pdf', stored_name: 'scan2__4.pdf', extension: '.pdf',
    size_bytes: 100, sha256: 'd'.repeat(64), chunk_count: 0, char_count: 5,
    created_at: '2026-01-01T00:00:00+08:00', status: 'indexed', error: null,
    warnings: [SCANNED_WARNING],
  },
  message: `《scan2.pdf》索引完成，共 0 个分块；⚠ ${SCANNED_WARNING}`,
}];
backend.documents = [...backend.documents, backend.uploadResult[0].document];
$('fileInput').files = [{ name: 'scan2.pdf', size: 100 }];
$('fileInput').fire('change');
await waitFor(() => $('toastWrap').textContent.includes('疑似扫描件'));
check('扫描件上传后弹出警告 toast',
  $('toastWrap').textContent.includes('疑似扫描件')
  && $('toastWrap').textContent.includes('OCR'),
  $('toastWrap').textContent.replace(/\s+/g, ' ').slice(0, 70));check('列表里出现新的警告条目',
  $('docList').children[3].classList.contains('is-warned'));

/* ================================================================== */
section('10. 删除与清空：二次确认、请求、列表同步');
globalThis.window.confirm = () => false;
const deleteBefore = backend.calls.filter((c) => c.method === 'DELETE').length;
$('clearAllBtn').fire('click');
await sleep(30);
check('清空被取消时不发 DELETE', backend.calls.filter((c) => c.method === 'DELETE').length === deleteBefore);

globalThis.window.confirm = () => true;
$('docList').children[0].querySelector('.doc-item__del').fire('click');
await waitFor(() => $('docCount').textContent === '3');
check('删除单篇文档后列表同步', $('docCount').textContent === '3', $('docCount').textContent);
check('删除请求打到了正确的文档 id',
  backend.calls.some((c) => c.method === 'DELETE' && c.path === '/api/documents/d1'));
check('toast 报告删除结果', $('toastWrap').textContent.includes('已删除文档及其 5 个分块'));

$('clearAllBtn').fire('click');
await waitFor(() => $('docCount').textContent === '0');
check('清空知识库后列表为空', $('docCount').textContent === '0', $('docCount').textContent);
check('空提示重新出现', $('docEmpty').hidden === false);
check('清空走 DELETE /api/documents',
  backend.calls.some((c) => c.method === 'DELETE' && c.path === '/api/documents'));

/* ================================================================== */
section('11. 空知识库守卫：不该白跑一次提问');
const chatBefore = backend.calls.filter((c) => c.path === '/api/chat').length;
$('question').value = '还有内容吗';
$('sendBtn').fire('click');
await sleep(30);
check('知识库为空时提示用户', $('toastWrap').textContent.includes('知识库为空'),
  $('toastWrap').textContent.replace(/\s+/g, ' ').slice(-60));
check('知识库为空时不发起提问',
  backend.calls.filter((c) => c.path === '/api/chat').length === chatBefore);
check('输入框内容保留（用户不必重打）', $('question').value === '还有内容吗');

/* ================================================================== */
section('12. 停止生成：中断流式回答');
$('newChatBtn').fire('click');
// 第 10 节把知识库清空了，这里用真实的上传路径把文档喂回来
// （客户端的 state.documents 只有靠 loadDocuments 才会更新）
const revivedDoc = {
  ...backend.uploadResult[0].document, doc_id: 'd9', filename: 'x.txt', warnings: [],
};
backend.documents = [revivedDoc];
backend.uploadResult = [{ document: revivedDoc, message: '《x.txt》索引完成，共 2 个分块' }];
$('fileInput').files = [{ name: 'x.txt', size: 100 }];
$('fileInput').fire('change');
await waitFor(() => $('docCount').textContent === '1');
check('恢复出 1 篇文档供提问', $('docCount').textContent === '1', $('docCount').textContent);

backend.chatFrames = [
  ['meta', { model: 'deepseek-r1:1.5b', mode: 'qa', source_count: 1, chunks_total: 5, relaxed: false }],
  ['token', { delta: '第一段。' }],
  ['token', { delta: '第二段。' }],
  ['token', { delta: '第三段。' }],
  ['done', { elapsed_ms: 100, cited: [] }],
];
backend.chatDelayMs = 60;

$('question').value = '测试中断';
$('sendBtn').fire('click');
await waitFor(() => $('stopBtn').hidden === false);
check('流式过程中出现停止按钮', $('stopBtn').hidden === false);
$('stopBtn').fire('click');
await waitFor(() => $('sendBtn').disabled === false, { timeout: 3000 });
check('点停止后结束生成状态', $('sendBtn').disabled === false);
const abortedAnswer = $('chat').querySelector('.msg--assistant');
check('回答里标注了「已停止生成」',
  abortedAnswer?.textContent.includes('已停止生成') === true,
  (abortedAnswer?.textContent || '(无回答节点)').replace(/\s+/g, ' ').slice(-40));
check('中断时不把半截回答写进历史（元信息标记为手动停止）',
  abortedAnswer?.querySelector('.msg__meta')?.textContent.includes('已手动停止') === true,
  abortedAnswer?.querySelector('.msg__meta')?.textContent || '(无元信息)');
backend.chatDelayMs = 0;

/* ================================================================== */
section('13. 新对话与侧栏折叠');
$('newChatBtn').fire('click');
check('新对话清空消息区', $('chat').querySelectorAll('.msg').length === 0,
  `${$('chat').querySelectorAll('.msg').length} 条`);
check('新对话恢复欢迎面板', $('welcome').hidden === false);

$('toggleSidebar').fire('click');
check('收起侧栏加上了样式类', $('app').classList.contains('sidebar-collapsed'));
check('收起后箭头方向反转', $('toggleSidebar').textContent === '›', $('toggleSidebar').textContent);
$('toggleSidebar').fire('click');
check('再次点击恢复侧栏', !$('app').classList.contains('sidebar-collapsed'));

/* ================================================================== */
console.log('');
console.log('='.repeat(70));
console.log(`结果：通过 ${passed} 项，失败 ${failed} 项`);
console.log('='.repeat(70));
process.exit(failed === 0 ? 0 : 1);
