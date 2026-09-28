/**
 * 后端 API 客户端。
 *
 * 全部基于浏览器原生 fetch，不依赖 axios 等库，保证离线可用。
 * 关键点：问答接口用的是 **POST + SSE**，而原生 EventSource 只支持 GET，
 * 因此这里用 fetch 的 ReadableStream 手动解析 SSE 帧。
 */

const BASE = '';

class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

async function readError(response) {
  let detail = '';
  try {
    const data = await response.json();
    detail = data.detail || data.message || JSON.stringify(data);
  } catch {
    try { detail = await response.text(); } catch { detail = ''; }
  }
  return detail || `HTTP ${response.status}`;
}

async function request(path, options = {}) {
  const response = await fetch(BASE + path, options);
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  if (response.status === 204) return null;
  return response.json();
}

/* ------------------------------------------------------------------ */
/* 系统                                                                */
/* ------------------------------------------------------------------ */

export function getHealth() {
  return request('/api/health');
}

/**
 * 首次配置体检。
 * @param {boolean} fresh 是否绕过后端探测缓存（用户装完东西点「重新检测」时用）
 */
export function getSetup(fresh = false) {
  return request(`/api/setup${fresh ? '?fresh=true' : ''}`);
}

/* ------------------------------------------------------------------ */
/* 一键安装                                                            */
/* ------------------------------------------------------------------ */

export function getInstallStatus() {
  return request('/api/setup/install');
}

export function startInstall(options = {}) {
  return request('/api/setup/install', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mirror: false, skip_ollama: false, ...options }),
  });
}

export function cancelInstall() {
  return request('/api/setup/install/cancel', { method: 'POST' });
}

export function resetInstall() {
  return request('/api/setup/install/reset', { method: 'POST' });
}

/**
 * 订阅安装过程的实时输出。
 *
 * 服务端会在开头回放已有日志，所以中途刷新页面也能看到完整历史。
 *
 * @param {object} handlers { onLog(line), onStatus(snapshot), onEnd(snapshot) }
 * @param {AbortSignal} signal
 */
export async function streamInstall(handlers = {}, signal) {
  const response = await fetch(`${BASE}/api/setup/install/stream`, {
    headers: { Accept: 'text/event-stream' },
    signal,
  });

  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  if (!response.body) {
    throw new ApiError('当前浏览器不支持流式响应', 0);
  }

  const parser = createSseParser((event, data) => {
    switch (event) {
      case 'log':
        // progress=true 表示这是 \r 刷新的进度行，前端应当覆盖上一行
        handlers.onLog?.(data.line ?? '', data.progress === true);
        break;
      case 'status': handlers.onStatus?.(data); break;
      case 'end': handlers.onEnd?.(data); break;
      default: break;
    }
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.flush();
  } finally {
    reader.releaseLock?.();
  }
}

export function getConfig() {
  return request('/api/config');
}

export function updateConfig(patch) {
  return request('/api/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

export function listModels() {
  return request('/api/models');
}

/* ------------------------------------------------------------------ */
/* 文档                                                                */
/* ------------------------------------------------------------------ */

export function listDocuments() {
  return request('/api/documents');
}

export function getDocumentChunks(docId, limit = 50) {
  return request(`/api/documents/${encodeURIComponent(docId)}/chunks?limit=${limit}`);
}

export function deleteDocument(docId) {
  return request(`/api/documents/${encodeURIComponent(docId)}`, { method: 'DELETE' });
}

export function clearAllDocuments() {
  return request('/api/documents', { method: 'DELETE' });
}

/**
 * 上传文件。用 XMLHttpRequest 是为了拿到上传进度
 * （fetch 目前无法可靠上报请求体进度）。
 * @param {File[]} files
 * @param {(percent:number)=>void} onProgress
 * @param {AbortSignal} [signal]
 */
export function uploadDocuments(files, onProgress, signal) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach((file) => form.append('files', file, file.name));

    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${BASE}/api/documents/upload`);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch (error) { reject(new ApiError('响应解析失败', xhr.status, String(error))); }
      } else {
        let detail = `HTTP ${xhr.status}`;
        try { detail = JSON.parse(xhr.responseText).detail || detail; } catch { /* ignore */ }
        reject(new ApiError(detail, xhr.status, detail));
      }
    };

    xhr.onerror = () => reject(new ApiError('网络错误，上传失败', 0));
    xhr.onabort = () => reject(new ApiError('上传已取消', 0, 'aborted'));

    if (signal) {
      if (signal.aborted) { xhr.abort(); return; }
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }

    xhr.send(form);
  });
}

/* ------------------------------------------------------------------ */
/* 检索                                                                */
/* ------------------------------------------------------------------ */

export function search(query, options = {}) {
  return request('/api/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, ...options }),
  });
}

/* ------------------------------------------------------------------ */
/* 问答（SSE 流式）                                                     */
/* ------------------------------------------------------------------ */

/**
 * 解析 SSE 帧。
 * 一帧形如：
 *   event: token
 *   data: {"delta":"你"}
 *   <空行>
 * 以 ":" 开头的行是心跳注释，直接忽略。
 */
function createSseParser(onEvent) {
  let buffer = '';

  const dispatch = (frame) => {
    let event = 'message';
    const dataLines = [];
    for (const rawLine of frame.split('\n')) {
      const line = rawLine.replace(/\r$/, '');
      if (!line || line.startsWith(':')) continue;
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    try {
      onEvent(event, JSON.parse(dataLines.join('\n')));
    } catch (error) {
      console.warn('[sse] 无法解析数据帧', error, dataLines.join('\n'));
    }
  };

  return {
    push(chunk) {
      buffer += chunk;
      let index = buffer.indexOf('\n\n');
      while (index !== -1) {
        dispatch(buffer.slice(0, index));
        buffer = buffer.slice(index + 2);
        index = buffer.indexOf('\n\n');
      }
    },
    flush() {
      if (buffer.trim()) { dispatch(buffer); buffer = ''; }
    },
  };
}

/**
 * 发起流式问答。
 *
 * @param {object} payload  { question, history, top_k, score_threshold, doc_ids }
 * @param {object} handlers { onMeta, onThinking, onToken, onSources, onDone, onError }
 * @param {AbortSignal} signal
 */
export async function streamChat(payload, handlers = {}, signal) {
  const response = await fetch(`${BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ ...payload, stream: true }),
    signal,
  });

  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  if (!response.body) {
    throw new ApiError('当前浏览器不支持流式响应', 0);
  }

  const parser = createSseParser((event, data) => {
    switch (event) {
      case 'meta': handlers.onMeta?.(data); break;
      case 'thinking': handlers.onThinking?.(data); break;
      case 'token': handlers.onToken?.(data); break;
      case 'sources': handlers.onSources?.(data); break;
      case 'done': handlers.onDone?.(data); break;
      case 'error':
        handlers.onError?.(new ApiError(data.message || '生成失败', 0, data.stage));
        break;
      default: break;
    }
  });

  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parser.push(decoder.decode(value, { stream: true }));
    }
    parser.push(decoder.decode());
    parser.flush();
  } finally {
    reader.releaseLock?.();
  }
}

export { ApiError };
