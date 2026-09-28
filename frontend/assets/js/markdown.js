/**
 * 极简 Markdown 渲染器（零依赖，离线可用）。
 *
 * 安全策略：**先转义 HTML，再做 Markdown 替换**。
 * 因此文档或模型输出里的 `<script>` 只会以纯文本呈现，不会被执行。
 * 只实现 RAG 回答里真正会出现的语法子集，避免引入 marked/markdown-it 等外部包。
 */

const ESCAPES = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/** HTML 转义。所有进入 DOM 的文本都必须先过这一层。 */
export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
}

/** 行内语法：代码 → 粗体 → 斜体 → 删除线 → 链接 → 引用角标。 */
function renderInline(raw) {
  let text = escapeHtml(raw);

  // 行内代码优先占位，避免其内部内容被后续规则改写
  const codes = [];
  text = text.replace(/`([^`\n]+)`/g, (_m, code) => {
    codes.push(code);
    return `\u0000CODE${codes.length - 1}\u0000`;
  });

  text = text
    .replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>');

  // 链接：只允许 http/https，挡掉 javascript: 之类的伪协议
  text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_m, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);

  // 引用角标 [1] / [1][2]
  text = text.replace(/\[(\d+)\]/g, '<span class="cite" data-cite="$1" role="button" tabindex="0">$1</span>');

  text = text.replace(/\u0000CODE(\d+)\u0000/g, (_m, i) => `<code>${codes[Number(i)]}</code>`);
  return text;
}

/** 判断是否为表格分隔行，例如 | --- | :--: | */
function isTableSeparator(line) {
  return /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(line) && line.includes('-');
}

function splitRow(line) {
  return line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map((c) => c.trim());
}

/**
 * 把 Markdown 文本渲染成 HTML 字符串。
 * @param {string} source
 * @returns {string}
 */
export function renderMarkdown(source) {
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const html = [];

  let inCode = false;
  let codeLang = '';
  let codeLines = [];
  let listType = null;   // 'ul' | 'ol'
  let paragraph = [];

  const closeList = () => {
    if (listType) { html.push(`</${listType}>`); listType = null; }
  };
  const flushParagraph = () => {
    if (paragraph.length) {
      html.push(`<p>${renderInline(paragraph.join(' '))}</p>`);
      paragraph = [];
    }
  };
  const flushAll = () => { flushParagraph(); closeList(); };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    // ---- 代码块 ----
    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      if (inCode) {
        html.push(`<pre><code class="lang-${escapeHtml(codeLang)}">${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        inCode = false; codeLines = []; codeLang = '';
      } else {
        flushAll();
        inCode = true; codeLang = fence[1] || '';
      }
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }

    // ---- 空行 ----
    if (!line.trim()) { flushAll(); continue; }

    // ---- 标题 ----
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushAll();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }

    // ---- 分割线 ----
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      flushAll();
      html.push('<hr />');
      continue;
    }

    // ---- 引用 ----
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      flushAll();
      html.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
      continue;
    }

    // ---- 表格 ----
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      flushAll();
      const head = splitRow(line);
      const rows = [];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      i -= 1;
      const thead = `<thead><tr>${head.map((c) => `<th>${renderInline(c)}</th>`).join('')}</tr></thead>`;
      const tbody = `<tbody>${rows
        .map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join('')}</tr>`)
        .join('')}</tbody>`;
      html.push(`<table>${thead}${tbody}</table>`);
      continue;
    }

    // ---- 列表 ----
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    const ordered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || ordered) {
      flushParagraph();
      const wanted = bullet ? 'ul' : 'ol';
      if (listType !== wanted) { closeList(); html.push(`<${wanted}>`); listType = wanted; }
      html.push(`<li>${renderInline((bullet || ordered)[1])}</li>`);
      continue;
    }

    // ---- 普通段落 ----
    closeList();
    paragraph.push(line.trim());
  }

  if (inCode && codeLines.length) {
    html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
  }
  flushAll();

  return html.join('\n');
}

/**
 * 用于流式输出的轻量纯文本转义（不解析 Markdown，避免半截语法闪烁）。
 * 保留换行。
 */
export function renderPlain(text) {
  return `<p>${escapeHtml(text).replace(/\n/g, '<br />')}</p>`;
}
