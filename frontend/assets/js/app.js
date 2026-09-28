/**
 * 前端主逻辑（免构建原生 ES Module）。
 *
 * 职责：
 *   - 文档上传 / 列表 / 删除 / 按文档过滤检索
 *   - 流式问答渲染（Markdown、推理链折叠、引用溯源）
 *   - 系统状态与参数设置
 */

import {
  ApiError,
  cancelInstall,
  clearAllDocuments,
  deleteDocument,
  getDocumentChunks,
  getHealth,
  getInstallStatus,
  getSetup,
  listDocuments,
  resetInstall,
  startInstall,
  streamChat,
  streamInstall,
  updateConfig,
  uploadDocuments,
} from './api.js';
import { escapeHtml, renderMarkdown } from './markdown.js';

/* ================================================================== */
/* DOM 引用                                                            */
/* ================================================================== */
const $ = (id) => document.getElementById(id);

const el = {
  app: $('app'),
  sidebar: $('sidebar'),
  toggleSidebar: $('toggleSidebar'),
  openSidebar: $('openSidebar'),

  statusDot: $('statusDot'),
  statusText: $('statusText'),
  modelLabel: $('modelLabel'),

  dropzone: $('dropzone'),
  dropzoneHint: $('dropzoneHint'),
  fileInput: $('fileInput'),
  uploadProgress: $('uploadProgress'),
  uploadBar: $('uploadBar'),
  uploadLabel: $('uploadLabel'),

  docList: $('docList'),
  docCount: $('docCount'),
  docEmpty: $('docEmpty'),
  clearAllBtn: $('clearAllBtn'),

  topK: $('topK'),
  topKOut: $('topKOut'),
  threshold: $('threshold'),
  thresholdOut: $('thresholdOut'),
  thinkingToggle: $('thinkingToggle'),

  chat: $('chat'),
  welcome: $('welcome'),
  examples: $('examples'),

  question: $('question'),
  sendBtn: $('sendBtn'),
  stopBtn: $('stopBtn'),
  newChatBtn: $('newChatBtn'),
  healthBtn: $('healthBtn'),
  composerHint: $('composerHint'),

  healthDrawer: $('healthDrawer'),
  healthBody: $('healthBody'),
  sourceDrawer: $('sourceDrawer'),
  sourceDrawerTitle: $('sourceDrawerTitle'),
  sourceBody: $('sourceBody'),

  setupModal: $('setupModal'),
  setupSubtitle: $('setupSubtitle'),
  setupBody: $('setupBody'),
  setupRecheck: $('setupRecheck'),
  setupLater: $('setupLater'),
  setupMute: $('setupMute'),
  setupBanner: $('setupBanner'),
  setupBannerTitle: $('setupBannerTitle'),
  setupBannerText: $('setupBannerText'),

  setupAutoInstall: $('setupAutoInstall'),
  setupCancelInstall: $('setupCancelInstall'),
  setupInstallPanel: $('setupInstallPanel'),
  installState: $('installState'),
  installElapsed: $('installElapsed'),
  installLog: $('installLog'),
  installActions: $('installActions'),
  installActionHint: $('installActionHint'),
  installRetry: $('installRetry'),
  installRetryMirror: $('installRetryMirror'),
  installManual: $('installManual'),

  toastWrap: $('toastWrap'),
};

/* ================================================================== */
/* 状态                                                                */
/* ================================================================== */
const STORE_KEY = 'rag-qa.settings.v1';
const SETUP_MUTE_KEY = 'rag-qa.setup.muted.v1';
const HISTORY_LIMIT = 8; // 送入后端的最大历史轮数

const state = {
  documents: [],
  selectedDocIds: new Set(),
  history: [],          // [{role, content}]
  streaming: false,
  controller: null,
  health: null,
  setup: null,          // 首次配置体检报告
  settings: {
    topK: 4,
    threshold: 0.2,
    showThinking: true,
  },
};

/* ================================================================== */
/* 工具                                                                */
/* ================================================================== */
function toast(message, kind = 'ok', timeout = 3600) {
  const node = document.createElement('div');
  node.className = `toast toast--${kind}`;
  node.textContent = message;
  el.toastWrap.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .25s';
    setTimeout(() => node.remove(), 260);
  }, timeout);
}

function humanSize(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function extLabel(name) {
  const match = /\.([a-z0-9]+)$/i.exec(name || '');
  return match ? match[1].slice(0, 4) : '?';
}

function loadSettings() {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (raw) Object.assign(state.settings, JSON.parse(raw));
  } catch { /* 忽略损坏的本地设置 */ }
}

function saveSettings() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(state.settings)); } catch { /* ignore */ }
}

function autoGrow() {
  el.question.style.height = 'auto';
  el.question.style.height = `${Math.min(el.question.scrollHeight, 180)}px`;
}

function scrollToBottom(force = false) {
  const nearBottom = el.chat.scrollHeight - el.chat.scrollTop - el.chat.clientHeight < 160;
  if (force || nearBottom) el.chat.scrollTop = el.chat.scrollHeight;
}

/* ================================================================== */
/* 系统状态                                                            */
/* ================================================================== */
async function refreshHealth() {
  try {
    const health = await getHealth();
    state.health = health;

    const ok = health.status === 'ok';
    el.statusDot.className = `brand__dot ${ok ? 'is-ok' : 'is-warn'}`;
    el.statusText.textContent = ok
      ? `就绪 · ${health.document_count} 文档`
      : (health.detail?.problems?.[0] || '服务降级');
    el.modelLabel.textContent = `模型：${health.llm_model} · 嵌入：${
      health.embedding_model.split('/').pop()} (${health.embedding_device})`;
  } catch (error) {
    el.statusDot.className = 'brand__dot is-err';
    el.statusText.textContent = '无法连接后端';
    el.modelLabel.textContent = '模型：—';
  }
}

function renderHealthDrawer() {
  const health = state.health;
  if (!health) {
    el.healthBody.innerHTML = '<p>尚未获取到状态信息。</p>';
    return;
  }

  const chip = (ok, yes = '正常', no = '异常') =>
    `<span class="${ok ? 'chip-ok' : 'chip-err'}">${ok ? yes : no}</span>`;

  const rows = [
    ['服务状态', chip(health.status === 'ok', '就绪', '降级')],
    ['离线模式', chip(health.offline, '已启用', '未启用')],
    ['Ollama 服务', chip(health.ollama_reachable, '可连接', '不可连接')],
    ['LLM 模型', `${escapeHtml(health.llm_model)} ${chip(health.llm_model_available, '已安装', '未找到')}`],
    ['Ollama 模型列表', health.ollama_models.length
      ? health.ollama_models.map((m) => `<code>${escapeHtml(m)}</code>`).join(' ')
      : '<em>无</em>'],
    ['嵌入模型', `${escapeHtml(health.embedding_model)} ${chip(health.embedding_ready)}`],
    ['嵌入设备', escapeHtml(health.embedding_device)],
    ['向量索引', `${chip(health.index_ready)} · ${health.detail?.vector_store?.vectors ?? 0} 个向量`],
    ['文档 / 分块', `${health.document_count} / ${health.chunk_count}`],
    ['版本', escapeHtml(health.version)],
  ];

  const problems = health.detail?.problems || [];
  const problemBox = problems.length
    ? `<div class="hint-box"><strong>需要处理：</strong><ul>${
      problems.map((p) => `<li>${escapeHtml(p)}</li>`).join('')}</ul></div>`
    : '';

  el.healthBody.innerHTML = `
    ${problemBox}
    <dl class="kv">
      ${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}
    </dl>
    ${problems.length ? `<div class="hint-box">
      若 Ollama 未启动，可运行项目根目录下的 <code>scripts\\start.ps1</code>（会自动拉起 Ollama 与后端）。
    </div>` : ''}
  `;
}

/* ================================================================== */
/* 文档列表                                                            */
/* ================================================================== */
function renderDocuments() {
  const docs = state.documents;
  el.docCount.textContent = String(docs.length);
  el.docEmpty.hidden = docs.length > 0;
  el.docList.innerHTML = '';

  for (const doc of docs) {
    const li = document.createElement('li');
    li.className = 'doc-item';
    if (state.selectedDocIds.has(doc.doc_id)) li.classList.add('is-selected');

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'doc-item__check';
    checkbox.checked = state.selectedDocIds.has(doc.doc_id);
    checkbox.title = '只在该文档内检索';
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) state.selectedDocIds.add(doc.doc_id);
      else state.selectedDocIds.delete(doc.doc_id);
      renderDocuments();
    });

    const icon = document.createElement('span');
    icon.className = 'doc-item__icon';
    icon.textContent = extLabel(doc.filename);

    const body = document.createElement('div');
    body.className = 'doc-item__body';

    const name = document.createElement('a');
    name.className = 'doc-item__name';
    name.textContent = doc.filename;
    name.title = `查看分块：${doc.filename}`;
    name.addEventListener('click', () => showChunks(doc));

    const meta = document.createElement('span');
    meta.className = 'doc-item__meta';
    meta.textContent = `${doc.chunk_count} 块 · ${humanSize(doc.size_bytes)}`;

    body.append(name, meta);

    const del = document.createElement('button');
    del.className = 'doc-item__del';
    del.textContent = '×';
    del.title = '删除该文档';
    del.addEventListener('click', () => removeDocument(doc));

    li.append(checkbox, icon, body, del);
    el.docList.appendChild(li);
  }
}

async function loadDocuments() {
  try {
    const data = await listDocuments();
    state.documents = data.documents || [];
    // 清理已被删除文档的选中态
    const alive = new Set(state.documents.map((d) => d.doc_id));
    for (const id of [...state.selectedDocIds]) if (!alive.has(id)) state.selectedDocIds.delete(id);
    renderDocuments();
  } catch (error) {
    toast(`加载文档列表失败：${error.message}`, 'err');
  }
}

async function removeDocument(doc) {
  if (!window.confirm(`确定删除《${doc.filename}》？该文档的 ${doc.chunk_count} 个分块会从索引中移除。`)) {
    return;
  }
  try {
    const result = await deleteDocument(doc.doc_id);
    toast(result.message, 'ok');
    state.selectedDocIds.delete(doc.doc_id);
    await Promise.all([loadDocuments(), refreshHealth()]);
  } catch (error) {
    toast(`删除失败：${error.message}`, 'err');
  }
}

async function showChunks(doc) {
  el.sourceDrawerTitle.textContent = `分块预览 · ${doc.filename}`;
  el.sourceBody.innerHTML = '<p>加载中…</p>';
  openDrawer(el.sourceDrawer);
  try {
    const data = await getDocumentChunks(doc.doc_id, 50);
    if (!data.chunks.length) {
      el.sourceBody.innerHTML = '<p>该文档没有分块。</p>';
      return;
    }
    el.sourceBody.innerHTML = data.chunks.map((chunk) => `
      <div class="chunk">
        <div class="chunk__head">
          <span>#${chunk.chunk_index ?? '-'}${chunk.page ? ` · 第 ${chunk.page} 页` : ''}</span>
          <span>${chunk.char_count} 字</span>
        </div>
        ${escapeHtml(chunk.content)}
      </div>
    `).join('');
  } catch (error) {
    el.sourceBody.innerHTML = `<p>加载失败：${escapeHtml(error.message)}</p>`;
  }
}

/* ================================================================== */
/* 上传                                                                */
/* ================================================================== */
function setUploading(busy, percent = 0, label = '') {
  el.uploadProgress.hidden = !busy;
  el.dropzone.classList.toggle('is-busy', busy);
  if (busy) {
    el.uploadBar.style.width = `${percent}%`;
    el.uploadLabel.textContent = label || `正在上传… ${percent}%`;
  }
}

async function handleFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;

  setUploading(true, 0, '正在上传…');
  try {
    const results = await uploadDocuments(files, (percent) => {
      setUploading(true, percent, percent < 100 ? `正在上传… ${percent}%` : '正在解析并建立索引…');
    });

    const failed = results.filter((r) => r.document.status === 'failed');
    const succeeded = results.length - failed.length;

    if (succeeded) toast(`已处理 ${succeeded} 个文件`, 'ok');
    for (const item of failed) toast(item.message, 'err', 6000);

    await Promise.all([loadDocuments(), refreshHealth()]);
  } catch (error) {
    toast(`上传失败：${error.message}`, 'err', 6000);
  } finally {
    setUploading(false);
    el.fileInput.value = '';
  }
}

function bindUpload() {
  el.dropzone.addEventListener('click', () => el.fileInput.click());
  el.dropzone.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      el.fileInput.click();
    }
  });
  el.fileInput.addEventListener('change', () => handleFiles(el.fileInput.files));

  ['dragenter', 'dragover'].forEach((type) => {
    el.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      el.dropzone.classList.add('is-dragover');
    });
  });
  ['dragleave', 'drop'].forEach((type) => {
    el.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      el.dropzone.classList.remove('is-dragover');
    });
  });
  el.dropzone.addEventListener('drop', (event) => {
    if (event.dataTransfer?.files?.length) handleFiles(event.dataTransfer.files);
  });

  // 拖到页面其他位置时不要让浏览器直接打开文件
  window.addEventListener('dragover', (event) => event.preventDefault());
  window.addEventListener('drop', (event) => event.preventDefault());
}

/* ================================================================== */
/* 抽屉                                                                */
/* ================================================================== */
function openDrawer(drawer) { drawer.hidden = false; }
function closeDrawer(drawer) { drawer.hidden = true; }

function bindDrawers() {
  document.querySelectorAll('.drawer').forEach((drawer) => {
    drawer.querySelectorAll('[data-close]').forEach((node) => {
      node.addEventListener('click', () => closeDrawer(drawer));
    });
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      document.querySelectorAll('.drawer:not([hidden])').forEach(closeDrawer);
      if (!el.setupModal.hidden) closeSetupModal();
    }
  });
}

/* ================================================================== */
/* 消息渲染                                                            */
/* ================================================================== */
function buildMessageNode(role) {
  const wrapper = document.createElement('div');
  wrapper.className = `msg msg--${role}`;

  const avatar = document.createElement('div');
  avatar.className = 'msg__avatar';
  avatar.textContent = role === 'user' ? '我' : 'AI';

  const body = document.createElement('div');
  body.className = 'msg__body';

  wrapper.append(avatar, body);
  return { wrapper, body };
}

function appendUserMessage(text) {
  el.welcome.hidden = true;
  const { wrapper, body } = buildMessageNode('user');
  const bubble = document.createElement('div');
  bubble.className = 'msg__bubble md';
  bubble.innerHTML = renderMarkdown(text);
  body.appendChild(bubble);
  el.chat.appendChild(wrapper);
  scrollToBottom(true);
}

function createAssistantMessage() {
  el.welcome.hidden = true;

  const { wrapper, body } = buildMessageNode('assistant');
  wrapper.classList.add('msg--assistant');

  const bubble = document.createElement('div');
  bubble.className = 'msg__bubble md';

  const thinking = document.createElement('div');
  thinking.className = 'think';
  thinking.hidden = true;
  thinking.innerHTML = `
    <div class="think__head">模型推理过程</div>
    <div class="think__body"></div>
  `;
  thinking.querySelector('.think__head').addEventListener('click', () => {
    thinking.classList.toggle('is-open');
  });

  const content = document.createElement('div');
  content.className = 'md';
  content.innerHTML = '<span class="dots"><span></span><span></span><span></span></span>';

  const sourcesBox = document.createElement('div');
  sourcesBox.className = 'sources';
  sourcesBox.hidden = true;

  const meta = document.createElement('div');
  meta.className = 'msg__meta';

  bubble.append(thinking, content, sourcesBox);
  body.append(bubble, meta);

  el.chat.appendChild(wrapper);
  scrollToBottom(true);

  return {
    wrapper, bubble, content, sourcesBox, meta, thinking,
    thinkingBody: thinking.querySelector('.think__body'),
  };
}

function renderSources(node, sources, cited) {
  if (!sources.length) {
    node.sourcesBox.hidden = true;
    node.sources = [];
    return;
  }

  node.sources = sources;
  node.sourcesBox.hidden = false;
  node.sourcesBox.innerHTML = '<div class="sources__title">引用来源</div>';

  const citedSet = new Set(cited || []);

  for (const source of sources) {
    const button = document.createElement('button');
    button.className = 'source-chip';
    if (citedSet.size && citedSet.has(source.index)) button.classList.add('is-cited');
    if (citedSet.size && !citedSet.has(source.index)) button.classList.add('is-dim');

    button.innerHTML = `
      <span class="source-chip__idx">${source.index}</span>
      <span class="source-chip__name">${escapeHtml(source.filename)}${
        source.page ? ` · 第 ${source.page} 页` : ''}</span>
      <span class="source-chip__score">${source.score.toFixed(3)}</span>
    `;
    button.addEventListener('click', () => showSourceDetail(node, source));
    node.sourcesBox.appendChild(button);
  }
}

function showSourceDetail(node, source) {
  el.sourceDrawerTitle.textContent = `[${source.index}] ${source.filename}`;
  const percent = Math.max(0, Math.min(1, source.score)) * 100;
  el.sourceBody.innerHTML = `
    <dl class="kv">
      <dt>文档</dt><dd>${escapeHtml(source.filename)}</dd>
      <dt>位置</dt><dd>${source.page ? `第 ${source.page} 页` : `片段 #${source.chunk_index ?? '-'}`}</dd>
      <dt>相似度</dt><dd>${source.score.toFixed(4)}（余弦）</dd>
      <dt>文档 ID</dt><dd><code>${escapeHtml(source.doc_id)}</code></dd>
    </dl>
    <div class="score-bar" style="margin:10px 0 16px"><span style="width:${percent}%"></span></div>
    <h3 style="font-size:13px;margin:0 0 8px">原文片段</h3>
    <div class="chunk">${escapeHtml(source.content)}</div>
  `;
  openDrawer(el.sourceDrawer);
}

/** 把回答里渲染出的 [n] 角标绑定到来源详情 */
function bindCitations(node) {
  node.content.querySelectorAll('.cite').forEach((cite) => {
    cite.addEventListener('click', () => {
      const index = Number(cite.dataset.cite);
      const source = (node.sources || []).find((s) => s.index === index);
      if (source) showSourceDetail(node, source);
      else toast(`未找到引用 [${index}] 对应的来源`, 'warn');
    });
  });
}

/* ================================================================== */
/* 提问                                                                */
/* ================================================================== */
const EXAMPLES = [
  '用三句话总结这些文档的主要内容',
  '文档里提到了哪些关键数据或指标？',
  '有哪些需要注意的限制条件？',
  '把这些文档的要点整理成表格',
];

function renderExamples() {
  el.examples.innerHTML = '';
  for (const text of EXAMPLES) {
    const chip = document.createElement('button');
    chip.className = 'example-chip';
    chip.textContent = text;
    chip.addEventListener('click', () => {
      el.question.value = text;
      autoGrow();
      submit();
    });
    el.examples.appendChild(chip);
  }
}

function setStreaming(busy) {
  state.streaming = busy;
  el.sendBtn.disabled = busy;
  el.stopBtn.hidden = !busy;
  el.composerHint.textContent = busy ? '正在生成…' : 'Enter 发送 · Shift+Enter 换行';
}

async function submit() {
  const question = el.question.value.trim();
  if (!question || state.streaming) return;

  if (!state.documents.length) {
    toast('知识库为空，请先上传文档', 'warn');
    return;
  }

  appendUserMessage(question);
  state.history.push({ role: 'user', content: question });

  el.question.value = '';
  autoGrow();
  setStreaming(true);

  const node = createAssistantMessage();
  let answer = '';
  let renderTimer = null;
  let pendingRender = false;

  const scheduleRender = () => {
    if (renderTimer) { pendingRender = true; return; }
    renderTimer = setTimeout(() => {
      renderTimer = null;
      node.content.innerHTML = answer ? renderMarkdown(answer) : '';
      bindCitations(node);
      if (pendingRender) { pendingRender = false; scheduleRender(); }
    }, 90);
  };

  state.controller = new AbortController();

  const payload = {
    question,
    history: state.history.slice(-HISTORY_LIMIT),
    top_k: state.settings.topK,
    score_threshold: state.settings.threshold,
    doc_ids: state.selectedDocIds.size ? [...state.selectedDocIds] : null,
  };

  try {
    await streamChat(payload, {
      onMeta: (data) => {
        node.meta.innerHTML = `<span>模型 ${escapeHtml(data.model)}</span>`
          + `<span>召回 ${data.source_count} 段</span>`;
      },
      onThinking: (data) => {
        if (!state.settings.showThinking) return;
        node.thinking.hidden = false;
        node.thinkingBody.textContent += data.delta;
        node.thinkingBody.scrollTop = node.thinkingBody.scrollHeight;
        scrollToBottom();
      },
      onToken: (data) => {
        answer += data.delta;
        scheduleRender();
        scrollToBottom();
      },
      onSources: (data) => {
        renderSources(node, data.sources || [], data.cited || []);
        if (data.thinking && state.settings.showThinking && !node.thinkingBody.textContent) {
          node.thinking.hidden = false;
          node.thinkingBody.textContent = data.thinking;
        }
        scrollToBottom();
      },
      onDone: (data) => {
        node.meta.innerHTML += `<span>耗时 ${(data.elapsed_ms / 1000).toFixed(1)}s</span>`;
        if (data.usage?.eval_count) {
          node.meta.innerHTML += `<span>${data.usage.eval_count} tokens</span>`;
        }
      },
      onError: (error) => {
        node.wrapper.classList.add('msg--error');
        answer += `\n\n> ⚠️ ${error.message}`;
        scheduleRender();
      },
    }, state.controller.signal);
  } catch (error) {
    if (error?.name === 'AbortError') {
      answer += '\n\n> ⏹ 已停止生成。';
      node.meta.innerHTML = '<span>已手动停止</span>';
    } else {
      node.wrapper.classList.add('msg--error');
      answer += `\n\n> ⚠️ ${error instanceof ApiError ? error.message : error}`;
      if (/知识库为空/.test(String(error.message))) {
        toast('知识库为空，请先上传文档', 'warn');
      }
    }
  } finally {
    if (renderTimer) clearTimeout(renderTimer);
    node.content.innerHTML = renderMarkdown(answer || '（无输出）');
    bindCitations(node);
    if (node.thinkingBody.textContent) node.thinking.hidden = false;
    scrollToBottom();

    if (answer.trim()) state.history.push({ role: 'assistant', content: answer });
    if (state.history.length > HISTORY_LIMIT * 2) {
      state.history = state.history.slice(-HISTORY_LIMIT * 2);
    }

    state.controller = null;
    setStreaming(false);
    el.question.focus();
  }
}

/* ================================================================== */
/* 设置绑定                                                            */
/* ================================================================== */
function bindSettings() {
  el.topK.value = String(state.settings.topK);
  el.topKOut.textContent = String(state.settings.topK);
  el.threshold.value = String(state.settings.threshold);
  el.thresholdOut.textContent = Number(state.settings.threshold).toFixed(2);
  el.thinkingToggle.checked = state.settings.showThinking;

  el.topK.addEventListener('input', () => {
    state.settings.topK = Number(el.topK.value);
    el.topKOut.textContent = el.topK.value;
    saveSettings();
    updateConfig({ top_k: state.settings.topK }).catch(() => { /* 后端不持久化也无妨 */ });
  });

  el.threshold.addEventListener('input', () => {
    state.settings.threshold = Number(el.threshold.value);
    el.thresholdOut.textContent = state.settings.threshold.toFixed(2);
    saveSettings();
    updateConfig({ score_threshold: state.settings.threshold }).catch(() => {});
  });

  el.thinkingToggle.addEventListener('change', () => {
    state.settings.showThinking = el.thinkingToggle.checked;
    saveSettings();
    updateConfig({ expose_thinking: state.settings.showThinking }).catch(() => {});
  });
}

function newChat() {
  if (state.streaming) state.controller?.abort();
  state.history = [];
  el.chat.querySelectorAll('.msg').forEach((node) => node.remove());
  el.welcome.hidden = false;
  el.question.focus();
  toast('已开始新对话', 'ok', 1800);
}

/* ================================================================== */
/* 首次配置引导                                                        */
/* ================================================================== */
function setupMuted() {
  try { return localStorage.getItem(SETUP_MUTE_KEY) === '1'; } catch { return false; }
}

function setSetupMuted(muted) {
  try {
    if (muted) localStorage.setItem(SETUP_MUTE_KEY, '1');
    else localStorage.removeItem(SETUP_MUTE_KEY);
  } catch { /* 隐私模式下 localStorage 可能不可用，忽略 */ }
}

function openSetupModal() { el.setupModal.hidden = false; }
function closeSetupModal() { el.setupModal.hidden = true; }

function updateSetupBanner(report) {
  if (!report || report.ready) {
    el.setupBanner.hidden = true;
    return;
  }
  el.setupBanner.hidden = false;
  el.setupBannerTitle.textContent = report.headline;
  const first = report.issues?.[0];
  el.setupBannerText.textContent = first ? `${first.title} · 点击查看` : '点击查看安装方式';
}

/** 复制到剪贴板。http 页面下 navigator.clipboard 常常不可用，需要回退。 */
async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* 继续走回退方案 */ }

  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

function buildCommandBlock(cmd) {
  const wrap = document.createElement('div');
  wrap.className = 'cmd';

  const head = document.createElement('div');
  head.className = 'cmd__head';

  const label = document.createElement('span');
  label.className = 'cmd__label';
  label.textContent = cmd.label || cmd.shell;

  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'cmd__copy';
  copy.textContent = '复制';
  copy.addEventListener('click', async () => {
    const ok = await copyText(cmd.command);
    copy.textContent = ok ? '已复制' : '复制失败';
    copy.classList.toggle('is-done', ok);
    setTimeout(() => { copy.textContent = '复制'; copy.classList.remove('is-done'); }, 1800);
  });

  head.append(label, copy);

  const code = document.createElement('pre');
  code.className = 'cmd__code';
  code.textContent = cmd.command;

  wrap.append(head, code);

  if (cmd.note) {
    const note = document.createElement('p');
    note.className = 'cmd__note';
    note.textContent = cmd.note;
    wrap.appendChild(note);
  }
  return wrap;
}

/** 环境探测面板：把后端探测到的真实路径摆给用户看，指引才算因地制宜。 */
function renderEnvPanel(environment) {
  if (!environment) return null;

  const toolchain = environment.toolchain || {};

  // 便携版 Ollama 只装了一半时：ollama.exe 在、服务也能起、模型也能列，
  // 但一提问就报 llama-server binary not found。只按「文件在不在」显示
  // 「已就绪」，会和下面的问题卡片自相矛盾，所以这里单独区分出第三种状态。
  const portable = environment.ollama_portable || {};
  const ollamaIncomplete = portable.present === true && portable.complete === false;

  const items = [
    ['项目目录', environment.project_root, true, null, null],
    ['uv', toolchain.uv?.path, !!toolchain.uv?.found, '用于免管理员准备 Python 运行时', null],
    ['Python 环境', toolchain.venv?.path, !!toolchain.venv?.found, '项目专用 .venv', null],
    ['嵌入模型', environment.embedding_ready ? environment.embedding_dir : '未就绪',
      !!environment.embedding_ready, '用于把文档转成向量', null],
    ['Ollama', environment.ollama_binary?.path, !!environment.ollama_binary?.found && !ollamaIncomplete,
      ollamaIncomplete
        ? '缺 lib\\ollama 下的推理引擎（llama-server.exe），提问会失败'
        : '用于本地生成回答',
      ollamaIncomplete ? '不完整' : null],
    ['Node.js', toolchain.node?.path, !!toolchain.node?.found, '仅运行前端测试时需要', null],
  ];

  const box = document.createElement('div');
  box.className = 'setup-env';

  const title = document.createElement('p');
  title.className = 'setup-env__title';
  title.textContent = '本机环境探测结果';
  box.appendChild(title);

  const grid = document.createElement('div');
  grid.className = 'setup-env__grid';

  for (const [name, value, ok, hint, okLabel] of items) {
    const nameCell = document.createElement('span');
    nameCell.className = 'setup-env__name';
    nameCell.textContent = name;
    if (hint) nameCell.title = hint;

    const valueCell = document.createElement('span');
    valueCell.className = ok ? 'setup-env__path' : 'setup-env__missing';
    valueCell.textContent = value || '未检测到';
    if (hint) valueCell.title = hint;

    const badge = document.createElement('span');
    badge.className = ok ? 'chip-ok' : 'chip-warn';
    badge.textContent = ok ? (okLabel || '已就绪') : (okLabel || '缺失');

    grid.append(nameCell, valueCell, badge);
  }

  box.appendChild(grid);
  return box;
}

/* ---------------- 一键安装 ---------------- */
let installStreamController = null;
let installTimer = null;
const MAX_LOG_LINES = 1500;

function setSetupMode(mode) {
  const installing = mode === 'install';
  el.setupBody.hidden = installing;
  el.setupInstallPanel.hidden = !installing;

  el.setupAutoInstall.hidden = installing || !state.setup?.environment?.can_auto_install;
  el.setupCancelInstall.hidden = !installing;
  el.setupRecheck.hidden = installing;
  el.setupMute.hidden = installing;
  el.setupLater.textContent = installing ? '后台运行' : '稍后再说';
}

function classifyLogLine(line) {
  if (line.startsWith('[rag-qa]')) return 'log-sys';
  if (/\[FAIL\]|Traceback|Error:|拒绝访问|错误/.test(line)) return 'log-err';
  if (/\[WARN\]/.test(line)) return 'log-warn';
  if (/\[OK\]|\[PASS\]|完成|已就绪/.test(line)) return 'log-ok';
  return '';
}

function appendInstallLog(line, isProgress = false) {
  const last = el.installLog.lastElementChild;

  // 进度行的语义是「回到行首重写」，所以覆盖上一行而不是不断追加。
  // 不这么做的话，一次 1.4GB 下载能刷出上千行进度帧，把真正的错误冲出视野。
  if (isProgress && last && last.dataset.progress === '1') {
    last.textContent = `${line}\n`;
    last.className = `log-progress ${classifyLogLine(line)}`.trim();
    el.installLog.scrollTop = el.installLog.scrollHeight;
    return;
  }

  const span = document.createElement('span');
  const cls = classifyLogLine(line);
  span.className = cls;
  if (isProgress) {
    span.dataset.progress = '1';
    span.className = `log-progress ${cls}`.trim();
  }
  span.textContent = `${line}\n`;
  el.installLog.appendChild(span);

  // 安装过程可能输出上万行，超出上限就丢弃最早的，避免页面越用越卡
  while (el.installLog.childElementCount > MAX_LOG_LINES) {
    el.installLog.removeChild(el.installLog.firstElementChild);
  }
  el.installLog.scrollTop = el.installLog.scrollHeight;
}

/** 安装失败后给出可操作的出路，而不是只丢一句 WARN。 */
function showInstallActions(kind, message) {
  el.installActions.hidden = false;
  el.installActions.classList.toggle('install-actions--ok', kind === 'ok');
  el.installActionHint.textContent = message;
  el.installRetry.hidden = kind === 'ok';
  el.installRetryMirror.hidden = kind === 'ok';
  el.installManual.hidden = false;
}

function hideInstallActions() {
  el.installActions.hidden = true;
  el.installActions.classList.remove('install-actions--ok');
}

function startInstallTimer() {
  stopInstallTimer();
  const startedAt = Date.now();
  const tick = () => {
    const total = Math.floor((Date.now() - startedAt) / 1000);
    const mm = String(Math.floor(total / 60)).padStart(2, '0');
    const ss = String(total % 60).padStart(2, '0');
    el.installElapsed.textContent = `已用时 ${mm}:${ss}`;
  };
  tick();
  installTimer = setInterval(tick, 1000);
}

function stopInstallTimer() {
  if (installTimer) {
    clearInterval(installTimer);
    installTimer = null;
  }
}

function setInstallState(text, kind = '') {
  el.installState.textContent = text;
  el.installState.className = `install-state${kind ? ` is-${kind}` : ''}`;
}

/** 订阅安装日志流，直到任务结束。 */
async function watchInstall({ reset = false } = {}) {
  installStreamController?.abort();
  installStreamController = new AbortController();

  if (reset) el.installLog.textContent = '';
  setSetupMode('install');
  hideInstallActions();
  startInstallTimer();

  let finalState = null;
  try {
    await streamInstall({
      onLog: appendInstallLog,
      onEnd: (snapshot) => { finalState = snapshot; },
    }, installStreamController.signal);
  } catch (error) {
    if (error?.name !== 'AbortError') {
      appendInstallLog(`[rag-qa] 日志流中断：${error.message}`);
    }
  } finally {
    stopInstallTimer();
    installStreamController = null;
  }

  const status = finalState?.status;
  if (status === 'succeeded') {
    setInstallState('安装完成，正在重新检测…', 'ok');
  } else if (status === 'cancelled') {
    setInstallState('已取消安装', 'err');
  } else if (status === 'failed') {
    setInstallState(`安装失败（退出码 ${finalState?.returncode ?? '?'}）`, 'err');
  }

  // 不论成败都重新体检一次：部分步骤可能已经成功
  await loadSetup({ fresh: true });
  await refreshHealth();

  if (status === 'succeeded' && state.setup?.ready) {
    toast('环境已就绪，可以开始问答了', 'ok');
    setTimeout(closeSetupModal, 1600);
    return;
  }

  if (status === 'succeeded') {
    showInstallActions('warn',
      '准备脚本已成功结束，但仍有项目未就绪。请查看左侧日志末尾的提示，'
      + '或点「查看手动命令」按需单独处理。');
    return;
  }

  if (status === 'failed') {
    const hint = state.setup?.issues?.[0]?.title || '环境准备未能完成';
    showInstallActions('err',
      `安装未成功（${hint}）。常见原因是网络不稳定或下载源不可达 —— `
      + '可以先用「用国内镜像重试」，仍不行再看「查看手动命令」逐步排查。');
  }
}

/** 发起安装并跟进进度。 */
async function runInstall({ mirror = false } = {}) {
  el.setupAutoInstall.disabled = true;
  try {
    await startInstall({ mirror });
    await watchInstall({ reset: true });
  } catch (error) {
    setInstallState('无法开始安装', 'err');
    showInstallActions('err', `无法开始安装：${error.message}`);
    toast(`无法开始安装：${error.message}`, 'err', 6000);
  } finally {
    el.setupAutoInstall.disabled = false;
  }
}

function renderSetup(report) {
  el.setupSubtitle.textContent = report.headline;

  if (report.ready) {
    el.setupBody.innerHTML = `
      <div class="setup-ready">
        <strong>✓</strong>
        <span>所有组件均已就绪，可以正常上传文档并提问了。</span>
      </div>`;
    el.setupMute.hidden = true;
    el.setupAutoInstall.hidden = true;
    return;
  }

  el.setupMute.hidden = false;
  el.setupBody.innerHTML = '';

  // 先摆出本机探测到的真实路径，让用户一眼看清谁已就位、缺的该装到哪
  const envPanel = renderEnvPanel(report.environment);
  if (envPanel) el.setupBody.appendChild(envPanel);

  // 只有准备脚本存在时才提供「一键安装」——后端要能真的替用户跑起来
  el.setupAutoInstall.hidden = !report.environment?.can_auto_install;

  report.issues.forEach((issue, index) => {
    const card = document.createElement('div');
    card.className = `setup-issue${issue.severity === 'warning' ? ' setup-issue--warning' : ''}`;

    const head = document.createElement('div');
    head.className = 'setup-issue__head';
    const badge = document.createElement('span');
    badge.className = 'setup-issue__index';
    badge.textContent = String(index + 1);
    const title = document.createElement('span');
    title.className = 'setup-issue__title';
    title.textContent = issue.title;
    head.append(badge, title);

    const detail = document.createElement('p');
    detail.className = 'setup-issue__detail';
    detail.textContent = issue.detail;

    card.append(head, detail);

    if (issue.impact) {
      const impact = document.createElement('div');
      impact.className = 'setup-issue__impact';
      impact.textContent = `影响：${issue.impact}`;
      card.appendChild(impact);
    }

    issue.options.forEach((option) => {
      const box = document.createElement('div');
      box.className = `setup-option${option.recommended ? ' setup-option--recommended' : ''}`;

      const oHead = document.createElement('div');
      oHead.className = 'setup-option__head';
      const oTitle = document.createElement('span');
      oTitle.className = 'setup-option__title';
      oTitle.textContent = option.title;
      oHead.appendChild(oTitle);
      if (option.recommended) {
        const tag = document.createElement('span');
        tag.className = 'setup-option__badge';
        tag.textContent = '推荐';
        oHead.appendChild(tag);
      }
      box.appendChild(oHead);

      if (option.description) {
        const desc = document.createElement('p');
        desc.className = 'setup-option__desc';
        desc.textContent = option.description;
        box.appendChild(desc);
      }

      option.commands.forEach((cmd) => box.appendChild(buildCommandBlock(cmd)));

      if (option.notes?.length) {
        const notes = document.createElement('ul');
        notes.className = 'setup-option__notes';
        option.notes.forEach((text) => {
          const li = document.createElement('li');
          li.textContent = text;
          notes.appendChild(li);
        });
        box.appendChild(notes);
      }

      if (option.link) {
        const link = document.createElement('a');
        link.className = 'setup-option__link';
        link.href = option.link;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = `下载地址：${option.link}`;
        box.appendChild(link);
      }

      card.appendChild(box);
    });

    el.setupBody.appendChild(card);
  });
}

/**
 * 拉取配置体检报告。
 * @param {object} options
 * @param {boolean} options.fresh 绕过后端探测缓存
 * @param {boolean} options.auto  启动时的自动检测：已就绪或用户已静音则不弹窗
 */
async function loadSetup({ fresh = false, auto = false } = {}) {
  try {
    const report = await getSetup(fresh);
    state.setup = report;
    updateSetupBanner(report);
    renderSetup(report);

    // 若已有安装任务在进行（也包括用户刷新页面后重新接入），直接切到进度视图。
    // installStreamController 非空说明前端已经在跟这个流了，不要重复订阅。
    const install = await getInstallStatus().catch(() => null);
    if (install?.running) {
      openSetupModal();
      if (!installStreamController) {
        setSetupMode('install');
        watchInstall({ reset: true });
      } else {
        setSetupMode('install');
      }
      return report;
    }

    setSetupMode('guidance');

    if (report.ready) {
      closeSetupModal();
    } else if (!auto || !setupMuted()) {
      openSetupModal();
    }
    return report;
  } catch (error) {
    el.setupSubtitle.textContent = '检测失败';
    el.setupBody.innerHTML = '<div class="setup-issue">'
      + `<p class="setup-issue__detail">无法获取配置信息：${escapeHtml(error.message)}</p></div>`;
    return null;
  }
}

function bindSetup() {
  el.setupLater.addEventListener('click', closeSetupModal);
  el.setupBanner.addEventListener('click', () => loadSetup({ fresh: false }));

  el.setupMute.addEventListener('click', () => {
    setSetupMuted(true);
    closeSetupModal();
    toast('已关闭自动提示，仍可从左侧入口随时打开', 'ok', 4200);
  });

  // 一键安装：由后端直接执行 prepare 脚本，进度实时回传到这个窗口
  el.setupAutoInstall.addEventListener('click', () => runInstall({ mirror: false }));
  el.installRetry.addEventListener('click', () => runInstall({ mirror: false }));
  el.installRetryMirror.addEventListener('click', () => runInstall({ mirror: true }));

  // 「查看手动命令」切回指引视图：里面有针对每种情况的复制命令与说明
  el.installManual.addEventListener('click', () => {
    setSetupMode('guidance');
    loadSetup({ fresh: true });
  });

  el.setupCancelInstall.addEventListener('click', async () => {
    if (!window.confirm('确定取消安装？已下载的部分会保留，下次可以继续。')) return;
    el.setupCancelInstall.disabled = true;
    try {
      const result = await cancelInstall();
      toast(result.message, 'warn');
    } catch (error) {
      toast(`取消失败：${error.message}`, 'err');
    } finally {
      el.setupCancelInstall.disabled = false;
    }
  });

  el.setupRecheck.addEventListener('click', async () => {
    el.setupRecheck.disabled = true;
    el.setupSubtitle.textContent = '正在重新检测…';
    try {
      // 上一轮安装已结束的话清掉状态，避免残留结论干扰判断
      await resetInstall().catch(() => {});
      const report = await loadSetup({ fresh: true });
      await refreshHealth();
      if (report?.ready) {
        toast('检测通过，配置已就绪', 'ok');
        setTimeout(closeSetupModal, 1400);
      } else if (report) {
        toast(`仍有 ${report.blocking_count} 项未完成`, 'warn');
      }
    } finally {
      el.setupRecheck.disabled = false;
    }
  });

  el.setupModal.querySelectorAll('[data-setup-dismiss]').forEach((node) => {
    node.addEventListener('click', closeSetupModal);
  });
}

/* ================================================================== */
/* 启动                                                                */
/* ================================================================== */
function bindGlobal() {
  el.sendBtn.addEventListener('click', submit);
  el.stopBtn.addEventListener('click', () => state.controller?.abort());

  el.question.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      submit();
    }
  });
  el.question.addEventListener('input', autoGrow);

  el.toggleSidebar.addEventListener('click', () => {
    el.app.classList.toggle('sidebar-collapsed');
    el.toggleSidebar.textContent = el.app.classList.contains('sidebar-collapsed') ? '›' : '‹';
  });
  el.openSidebar.addEventListener('click', () => el.app.classList.toggle('sidebar-open'));
  el.sidebar.addEventListener('click', (event) => {
    if (window.innerWidth <= 860 && event.target.closest('.panel')) {
      // 移动端选择文档后自动收起侧栏，避免遮挡对话
      el.app.classList.remove('sidebar-open');
    }
  });

  el.clearAllBtn.addEventListener('click', async () => {
    if (!state.documents.length) { toast('知识库已经是空的', 'warn'); return; }
    if (!window.confirm('确定清空整个知识库？所有文档与索引都会被删除。')) return;
    try {
      const result = await clearAllDocuments();
      toast(result.message, 'ok');
      state.selectedDocIds.clear();
      await Promise.all([loadDocuments(), refreshHealth()]);
    } catch (error) {
      toast(`清空失败：${error.message}`, 'err');
    }
  });

  el.healthBtn.addEventListener('click', async () => {
    await refreshHealth();
    renderHealthDrawer();
    openDrawer(el.healthDrawer);
  });

  el.newChatBtn.addEventListener('click', newChat);
}

async function boot() {
  loadSettings();
  bindGlobal();
  bindUpload();
  bindDrawers();
  bindSettings();
  bindSetup();
  renderExamples();
  await refreshHealth();
  await loadDocuments();
  // 配置体检放在最后：即使用户还没装好 Ollama，也能先看到界面和引导
  await loadSetup({ auto: true });
  autoGrow();
  el.question.focus();
}

boot();
