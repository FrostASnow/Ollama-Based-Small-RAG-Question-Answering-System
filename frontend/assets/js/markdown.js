/**
 * 极简 Markdown 渲染器（零依赖）。
 * 安全策略：先转义 HTML 再做替换，所以输出里的 <script> 只会是纯文本。
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

function isThematicBreak(line) {
  return /^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line);
}

/** 前导空白宽度（制表符按 4 列算）。 */
function indentWidth(prefix) {
  let width = 0;
  for (const ch of prefix) width += ch === '\t' ? 4 : 1;
  return width;
}

const BULLET_MARKER = /^([ \t]*)([-*+•·])\s+(.*)$/;
// 兼容模型实际会写出的各种序号：`1.` `1．` `1、` `1)` `（1）`，以及序号后没有空格的写法。
const ORDERED_MARKER = /^([ \t]*)(\d{1,3})([.．、)）])(\s*)(.*)$/;

/**
 * 解析列表项行；不是列表项则返回 null。
 * 刻意要求序号后必须有标点，否则「2020 年第 2 期」会被误判成列表项。
 */
export function matchListItem(line) {
  const bullet = line.match(BULLET_MARKER);
  if (bullet) {
    const text = bullet[3].trim();
    if (!text) return null;
    return { indent: indentWidth(bullet[1]), type: 'ul', start: 1, text };
  }
  const ordered = line.match(ORDERED_MARKER);
  if (ordered) {
    const punctuation = ordered[3];
    const gap = ordered[4];
    // 「3.14 是圆周率」不是列表：ASCII 的 . 和 ) 后面必须跟空白
    if ((punctuation === '.' || punctuation === ')') && !gap) return null;
    const text = ordered[5].trim();
    if (!text) return null;
    return { indent: indentWidth(ordered[1]), type: 'ol', start: Number(ordered[2]), text };
  }
  return null;
}

/** 这些行出现即代表列表结束（属于别的块级语法）。 */
function endsList(line) {
  return /^\s*```/.test(line)
    || /^#{1,6}\s/.test(line)
    || /^\s*>/.test(line)
    || isThematicBreak(line)
    || isTableSeparator(line);
}

function newFrame(item, owner) {
  return { type: item.type, start: item.start, indent: item.indent, items: [], owner };
}

function serializeItem(item) {
  const parts = item.parts.map((part) => renderInline(part));
  let html = parts[0] || '';
  for (let i = 1; i < parts.length; i += 1) {
    // 续行（「- **要点**」换行后接说明）要留在同一个 <li> 里，
    // 否则每个要点都会变成独立的单元素列表，序号全是 1。
    html += `<br />${parts[i]}`;
  }
  for (const child of item.children) html += serializeFrame(child);
  return `<li>${html}</li>`;
}

function serializeFrame(frame) {
  const start = frame.type === 'ol' && frame.start && frame.start !== 1
    ? ` start="${frame.start}"` : '';
  const inner = frame.items.map(serializeItem).join('\n');
  return `<${frame.type}${start}>${inner}</${frame.type}>`;
}

/**
 * 从 start 行开始解析一整块列表，返回 `{ html, next }`。
 * 空行不关闭列表，缩进或紧跟随行归入当前条目，缩进更深则建子列表。
 */
function parseListBlock(lines, start) {
  const rootLists = [];
  const stack = [];          // 打开的列表帧，栈顶是最深的一层
  let index = start;
  let pendingBlank = false;  // 上一行是否为空行

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      pendingBlank = true;
      index += 1;
      continue;
    }
    if (endsList(line)) break;

    const item = matchListItem(line);

    if (item) {
      while (stack.length && item.indent < stack[stack.length - 1].indent) stack.pop();

      let frame = stack[stack.length - 1];
      if (!frame) {
        frame = newFrame(item, null);
        rootLists.push(frame);
        stack.push(frame);
      } else if (item.indent > frame.indent) {
        // 缩进更深 → 作为上一个条目的子列表
        const parent = frame.items[frame.items.length - 1];
        const child = newFrame(item, parent);
        parent.children.push(child);
        stack.push(child);
        frame = child;
      } else if (item.type !== frame.type) {
        // 同层级但换了标记类型（`-` 变 `1.`）→ 另起一个并列列表
        const sibling = newFrame(item, frame.owner);
        if (frame.owner) frame.owner.children.push(sibling);
        else rootLists.push(sibling);
        stack[stack.length - 1] = sibling;
        frame = sibling;
      }

      frame.items.push({ parts: [item.text], children: [] });
      pendingBlank = false;
      index += 1;
      continue;
    }

    // 非列表行：可能是上一个条目的续行
    const frame = stack[stack.length - 1];
    const last = frame && frame.items[frame.items.length - 1];
    if (last) {
      const indent = indentWidth(line.match(/^[ \t]*/)[0]);
      const contentIndent = frame.indent + 3;   // 标记 + 一个空格所占的列
      if (!pendingBlank || indent >= contentIndent) {
        last.parts.push(line.trim());
        pendingBlank = false;
        index += 1;
        continue;
      }
    }
    break;
  }

  return { html: rootLists.map(serializeFrame).join('\n'), next: index };
}

/** 把 Markdown 文本渲染成 HTML 字符串。 */
export function renderMarkdown(source) {
  const lines = String(source ?? '').replace(/\r\n?/g, '\n').split('\n');
  const html = [];

  let inCode = false;
  let codeLang = '';
  let codeLines = [];
  let paragraph = [];

  const flushParagraph = () => {
    if (!paragraph.length) return;
    // 两个以上空格结尾（或反斜杠）= 硬换行，段落内部折行要保留
    const groups = [];
    let current = [];
    for (let i = 0; i < paragraph.length; i += 1) {
      const line = paragraph[i];
      current.push(line.trim());
      const hardBreak = / {2,}$/.test(line) || line.endsWith('\\');
      if (hardBreak && i < paragraph.length - 1) {
        groups.push(current);
        current = [];
      }
    }
    groups.push(current);
    html.push(`<p>${groups.map((group) => renderInline(group.join(' '))).join('<br />')}</p>`);
    paragraph = [];
  };

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];

    // ---- 代码块 ----
    const fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      if (inCode) {
        html.push(`<pre><code class="lang-${escapeHtml(codeLang)}">${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        inCode = false; codeLines = []; codeLang = '';
      } else {
        flushParagraph();
        inCode = true; codeLang = fence[1] || '';
      }
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }

    // ---- 空行 ----
    if (!line.trim()) { flushParagraph(); continue; }

    // ---- 标题 ----
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      const level = heading[1].length;
      html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }

    // ---- 分割线（必须先于列表判断，否则 "- - -" 会被当成列表项）----
    if (isThematicBreak(line)) {
      flushParagraph();
      html.push('<hr />');
      continue;
    }

    // ---- 引用 ----
    const quote = line.match(/^\s*>\s?(.*)$/);
    if (quote) {
      flushParagraph();
      html.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
      continue;
    }

    // ---- 表格 ----
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      flushParagraph();
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

    // ---- 列表（整块交给 parseListBlock，含嵌套与松散列表）----
    if (matchListItem(line)) {
      flushParagraph();
      const block = parseListBlock(lines, i);
      if (block.html) html.push(block.html);
      i = block.next - 1;   // for 循环还会 +1
      continue;
    }

    // ---- 普通段落 ----
    // 保留原始行：行尾两个空格是硬换行标记，trim 掉就丢了
    paragraph.push(line);
  }

  if (inCode && codeLines.length) {
    html.push(`<pre><code>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
  }
  flushParagraph();

  return html.join('\n');
}

/** 流式输出的轻量纯文本转义（不解析 Markdown，避免半截语法闪烁）；保留换行。 */
export function renderPlain(text) {
  return `<p>${escapeHtml(text).replace(/\n/g, '<br />')}</p>`;
}
