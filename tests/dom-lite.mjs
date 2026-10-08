/**
 * 极简 DOM 垫片：让前端代码能在 Node 里真的跑起来 —— 正则扫源码证明不了行为，
 * 本项目离线优先不装 jsdom，故手写最小实现，够跑这个前端为止（不是通用 DOM）。
 */

/* ------------------------------------------------------------------ */
/* 标签分类                                                            */
/* ------------------------------------------------------------------ */
// 空元素：没有闭合标签，也不该被压进栈
const VOID_TAGS = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
  'link', 'meta', 'param', 'source', 'track', 'wbr',
]);
// 内容按原始文本处理（里面的 < 不能当标签）；本项目里这两类标签都是空的
const RAW_TEXT_TAGS = new Set(['script', 'style']);

const ENTITIES = {
  amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0',
};

function decodeEntities(text) {
  return text.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (whole, body) => {
    if (body.startsWith('#x') || body.startsWith('#X')) {
      return String.fromCodePoint(parseInt(body.slice(2), 16));
    }
    if (body.startsWith('#')) return String.fromCodePoint(parseInt(body.slice(1), 10));
    return Object.prototype.hasOwnProperty.call(ENTITIES, body) ? ENTITIES[body] : whole;
  });
}

function escapeText(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function escapeAttr(text) {
  return escapeText(text).replace(/"/g, '&quot;');
}

/* ------------------------------------------------------------------ */
/* 选择器（只支持 app.js 用到的那几种）                                  */
/* ------------------------------------------------------------------ */
/**
 * 支持：`tag`、`.class`、`#id`、`[attr]`、`[attr="v"]`、`:not(...)`，以及空格（后代）
 * 和 `>`（子代）连接的多段选择器；不支持 `~` `+` `:nth-child`（前端没用到）。
 */
function parseSimple(part) {
  const simple = { tag: null, id: null, classes: [], attrs: [], nots: [] };
  let rest = part;
  // :not(...) 先摘掉，避免里面的 . # [ 被外层规则抢走
  rest = rest.replace(/:not\(([^)]*)\)/g, (_whole, inner) => {
    simple.nots.push(parseSimple(inner));
    return '';
  });
  const attrRe = /\[([^\]]+)\]/g;
  rest = rest.replace(attrRe, (_whole, inner) => {
    const m = /^([\w:.-]+)(?:\s*=\s*"?([^"\]]*)"?)?$/.exec(inner.trim());
    if (m) simple.attrs.push({ name: m[1], value: m[2] === undefined ? null : m[2] });
    return '';
  });
  const tagMatch = /^[a-zA-Z][\w-]*/.exec(rest);
  if (tagMatch) {
    simple.tag = tagMatch[0].toLowerCase();
    rest = rest.slice(tagMatch[0].length);
  }
  for (const cls of rest.matchAll(/\.([\w-]+)/g)) simple.classes.push(cls[1]);
  for (const id of rest.matchAll(/#([\w-]+)/g)) simple.id = id[1];
  return simple;
}

function splitSteps(selector) {
  // 把 "a > b c" 拆成 [{combinator:null,simple:a},{'>',b},{' ',c}]
  const tokens = selector.trim().split(/\s*(>)\s*|\s+/).filter((t) => t !== undefined && t !== '');
  const steps = [];
  for (const token of tokens) {
    if (token === '>') {
      if (steps.length) steps[steps.length - 1].next = '>';
      continue;
    }
    steps.push({ combinator: steps.length ? (steps[steps.length - 1].next || ' ') : null, simple: parseSimple(token) });
  }
  return steps;
}

function matchesSimple(el, simple) {
  if (el.nodeType !== 1) return false;
  if (simple.tag && el.tagName !== simple.tag) return false;
  if (simple.id && el.id !== simple.id) return false;
  for (const cls of simple.classes) if (!el.classList.contains(cls)) return false;
  for (const attr of simple.attrs) {
    if (!el.hasAttribute(attr.name)) return false;
    if (attr.value !== null && el.getAttribute(attr.name) !== attr.value) return false;
  }
  for (const not of simple.nots) if (matchesSimple(el, not)) return false;
  return true;
}

function matchesSelector(el, selector) {
  const steps = splitSteps(selector);
  if (!steps.length) return false;
  if (steps.length === 1) return matchesSimple(el, steps[0].simple);
  // 带组合符的选择器只用于「祖先链上能找到前一段」的匹配
  // （closest('.panel') 这类单段用法不受影响）
  let index = steps.length - 1;
  if (!matchesSimple(el, steps[index].simple)) return false;
  let node = el.parentNode;
  index -= 1;
  while (index >= 0 && node) {
    if (matchesSimple(node, steps[index].simple)) index -= 1;
    node = node.parentNode;
  }
  return index < 0;
}

/* ------------------------------------------------------------------ */
/* 节点                                                                */
/* ------------------------------------------------------------------ */
class LiteNode {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
    this.__listeners = Object.create(null);
  }

  get children() {
    return this.childNodes.filter((node) => node.nodeType === 1);
  }

  get childElementCount() { return this.children.length; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  append(...nodes) {
    for (const node of nodes) {
      this.appendChild(typeof node === 'string' ? this.ownerDocument.createTextNode(node) : node);
    }
  }

  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index >= 0) {
      this.childNodes.splice(index, 1);
      node.parentNode = null;
    }
    return node;
  }

  remove() { if (this.parentNode) this.parentNode.removeChild(this); }

  get textContent() {
    return this.childNodes.map((node) => node.textContent).join('');
  }

  set textContent(value) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    if (value !== '' && value !== null && value !== undefined) {
      this.appendChild(this.ownerDocument.createTextNode(String(value)));
    }
  }

  addEventListener(type, handler) {
    if (!handler) return;
    (this.__listeners[type] || (this.__listeners[type] = [])).push(handler);
  }

  removeEventListener(type, handler) {
    const list = this.__listeners[type];
    if (!list) return;
    const index = list.indexOf(handler);
    if (index >= 0) list.splice(index, 1);
  }

  /** 触发事件并沿祖先链冒泡（到 document / window 为止）；测试用它模拟点击、输入、按键。 */
  fire(type, props = {}) {
    const event = {
      type,
      target: this,
      defaultPrevented: false,
      propagateStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagateStopped = true; },
      ...props,
    };
    let node = this;
    while (node) {
      const list = node.__listeners[type];
      if (list) {
        event.currentTarget = node;
        for (const handler of [...list]) handler.call(node, event);
        if (event.propagateStopped) break;
      }
      node = node.parentNode || (node.ownerDocument && node.ownerDocument.defaultView) || null;
    }
    return event;
  }

  dispatchEvent(event) { return this.fire(event.type, event); }
}

class LiteText extends LiteNode {
  constructor(doc, text) {
    super(doc);
    this.nodeType = 3;
    this._text = String(text);
  }

  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); }
  get nodeValue() { return this._text; }
  set nodeValue(value) { this._text = String(value); }
}

class LiteElement extends LiteNode {
  constructor(doc, tagName) {
    super(doc);
    this.nodeType = 1;
    this.tagName = String(tagName).toLowerCase();
    this._attributes = new Map();
    this._classes = new Set();
    this.style = {};
    this._value = '';
    this._scrollHeight = 120;
    this.scrollTop = 0;
  }

  /* ---- 属性 ---- */
  setAttribute(name, value) {
    const key = String(name);
    this._attributes.set(key, String(value));
    if (key === 'class') {
      this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
    }
    if (key === 'value') this._value = String(value);
    if (key === 'checked' || key === 'disabled' || key === 'hidden' || key === 'selected') {
      this[`_${key}`] = true;
    }
  }

  getAttribute(name) {
    return this._attributes.has(String(name)) ? this._attributes.get(String(name)) : null;
  }

  hasAttribute(name) { return this._attributes.has(String(name)); }

  removeAttribute(name) {
    this._attributes.delete(String(name));
    if (String(name) === 'class') this._classes = new Set();
  }

  get id() { return this.getAttribute('id') || ''; }
  set id(value) { this.setAttribute('id', value); }

  get className() { return [...this._classes].join(' '); }
  set className(value) {
    this._classes = new Set(String(value).split(/\s+/).filter(Boolean));
    this._attributes.set('class', this.className);
  }

  get classList() {
    const el = this;
    const sync = () => { el._attributes.set('class', el.className); };
    return {
      add: (...names) => { names.forEach((n) => el._classes.add(n)); sync(); },
      remove: (...names) => { names.forEach((n) => el._classes.delete(n)); sync(); },
      toggle: (name, force) => {
        const want = force === undefined ? !el._classes.has(name) : !!force;
        if (want) el._classes.add(name); else el._classes.delete(name);
        sync();
        return want;
      },
      contains: (name) => el._classes.has(name),
      get value() { return el.className; },
    };
  }

  get dataset() {
    const el = this;
    return new Proxy(Object.create(null), {
      get(_target, key) {
        const attr = `data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
        return el.getAttribute(attr);
      },
      set(_target, key, value) {
        const attr = `data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
        el.setAttribute(attr, value);
        return true;
      },
      has(_target, key) {
        const attr = `data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
        return el.hasAttribute(attr);
      },
    });
  }

  /* ---- 布尔型属性（app.js 直接赋值）---- */
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) { if (value) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); }

  get checked() { return this.hasAttribute('checked'); }
  set checked(value) { if (value) this.setAttribute('checked', ''); else this.removeAttribute('checked'); }

  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { if (value) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }

  get value() { return this._value; }
  set value(next) { this._value = String(next ?? ''); }

  get scrollHeight() { return this._scrollHeight; }
  get clientHeight() { return 400; }
  get selectionStart() { return this._value.length; }

  /* ---- 查询 ---- */
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matchesSelector(child, selector)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { return matchesSelector(this, selector); }

  closest(selector) {
    let node = this;
    while (node && node.nodeType === 1) {
      if (matchesSelector(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }

  /* ---- 内容 ---- */
  get innerHTML() {
    return this.childNodes.map((node) => {
      if (node.nodeType === 3) return escapeText(node.textContent);
      return node.outerHTML;
    }).join('');
  }

  set innerHTML(html) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    parseHTML(String(html ?? ''), this.ownerDocument, this);
  }

  get outerHTML() {
    const attrs = [...this._attributes.entries()]
      .map(([name, value]) => (value === '' ? ` ${name}` : ` ${name}="${escapeAttr(value)}"`))
      .join('');
    const inner = this.innerHTML;
    if (VOID_TAGS.has(this.tagName)) return `<${this.tagName}${attrs} />`;
    return `<${this.tagName}${attrs}>${inner}</${this.tagName}>`;
  }

  /* ---- 交互 ---- */
  click() {
    // click() 必须实现「click in progress」标志，重入时直接返回；
    // 否则 dropzone → fileInput.click() 的事件冒泡会无限递归到栈溢出。
    if (this.__clickInProgress) return;
    this.__clickInProgress = true;
    try {
      return this.fire('click');
    } finally {
      this.__clickInProgress = false;
    }
  }
  focus() { this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = null; }
  select() { this.ownerDocument.selectedText = this._value; }
  setSelectionRange() { /* 不需要实现：只有复制回退路径会用到 */ }
  insertAdjacentHTML() { /* 不被使用 */ }
}

/* ------------------------------------------------------------------ */
/* HTML 解析                                                           */
/* ------------------------------------------------------------------ */
function findTagEnd(html, start) {
  let quote = null;
  for (let i = start + 1; i < html.length; i += 1) {
    const char = html[i];
    if (quote) {
      if (char === quote) quote = null;
    } else if (char === '"' || char === "'") {
      quote = char;
    } else if (char === '>') {
      return i;
    }
  }
  return html.length;
}

function parseAttrs(text, element) {
  const re = /([a-zA-Z_:][\w:.-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
  let match;
  while ((match = re.exec(text)) !== null) {
    const name = match[1];
    const value = match[2] ?? match[3] ?? match[4] ?? '';
    element.setAttribute(name, decodeEntities(value));
  }
}

export function parseHTML(html, doc, parent) {
  const stack = [parent];
  let i = 0;
  const top = () => stack[stack.length - 1];

  while (i < html.length) {
    const lt = html.indexOf('<', i);
    if (lt === -1) {
      const text = decodeEntities(html.slice(i));
      if (text) top().appendChild(doc.createTextNode(text));
      break;
    }
    if (lt > i) {
      const text = decodeEntities(html.slice(i, lt));
      if (text) top().appendChild(doc.createTextNode(text));
    }
    if (html.startsWith('<!--', lt)) {
      const end = html.indexOf('-->', lt);
      i = end === -1 ? html.length : end + 3;
      continue;
    }
    if (html.startsWith('<!', lt)) { // doctype
      const end = html.indexOf('>', lt);
      i = end === -1 ? html.length : end + 1;
      continue;
    }

    const gt = findTagEnd(html, lt);
    const raw = html.slice(lt + 1, gt);
    i = gt + 1;

    if (raw.startsWith('/')) { // 闭合标签
      const name = raw.slice(1).trim().toLowerCase();
      for (let depth = stack.length - 1; depth > 0; depth -= 1) {
        if (stack[depth].tagName === name) { stack.length = depth; break; }
      }
      continue;
    }

    const selfClosing = raw.trimEnd().endsWith('/');
    const body = selfClosing ? raw.trimEnd().slice(0, -1) : raw;
    const nameMatch = /^([a-zA-Z][\w-]*)/.exec(body);
    if (!nameMatch) continue;
    const tag = nameMatch[1].toLowerCase();
    const element = doc.createElement(tag);
    parseAttrs(body.slice(nameMatch[0].length), element);
    top().appendChild(element);

    if (VOID_TAGS.has(tag) || selfClosing) continue;
    if (RAW_TEXT_TAGS.has(tag)) {
      const close = html.toLowerCase().indexOf(`</${tag}`, i);
      const text = close === -1 ? html.slice(i) : html.slice(i, close);
      if (text) element.appendChild(doc.createTextNode(text));
      i = close === -1 ? html.length : html.indexOf('>', close) + 1;
      continue;
    }
    stack.push(element);
  }
  return parent;
}

/* ------------------------------------------------------------------ */
/* 文档 / 窗口                                                         */
/* ------------------------------------------------------------------ */
class LiteDocument extends LiteNode {
  constructor() {
    super(null);
    this.ownerDocument = this;
    this.nodeType = 9;
    this.activeElement = null;
    this.selectedText = '';
    this.execCommand = () => true;
    // 真正的 html / body 由 installDom() 解析 index.html 之后回填
    this.documentElement = null;
    this.body = null;
  }

  createElement(tag) { return new LiteElement(this, tag); }
  createTextNode(text) { return new LiteText(this, text); }

  getElementById(id) {
    return this.querySelectorAll(`#${id}`)[0] || null;
  }

  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matchesSelector(child, selector)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

function camelToKebab(name) {
  return String(name).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
}

/**
 * 覆盖一个全局变量。不能直接 `globalThis.navigator = ...`：Node 22+ 把 navigator
 * 和 localStorage 定义成只有 getter 的属性，直接赋值会抛错，用 defineProperty 才行。
 */
function defineGlobal(key, value) {
  try {
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
    return true;
  } catch {
    try {
      globalThis[key] = value;
      return true;
    } catch {
      return false;
    }
  }
}

/** 装好全局环境并把 index.html 解析成真实 DOM。 */
export function installDom(html) {
  const document = new LiteDocument();
  const window = {
    document,
    innerWidth: 1280,
    innerHeight: 800,
    __listeners: Object.create(null),
    addEventListener(type, handler) {
      (this.__listeners[type] || (this.__listeners[type] = [])).push(handler);
    },
    removeEventListener() {},
    confirm: () => true,
    alert: () => {},
    prompt: () => null,
    getComputedStyle: () => ({ getPropertyValue: () => '' }),
  };
  document.defaultView = window;

  const store = new Map();
  const localStorage = {
    getItem: (key) => (store.has(String(key)) ? store.get(String(key)) : null),
    setItem: (key, value) => store.set(String(key), String(value)),
    removeItem: (key) => store.delete(String(key)),
    clear: () => store.clear(),
    get length() { return store.size; },
  };

  const files = { clipboard: [] };
  const navigator = {
    userAgent: 'node-dom-lite',
    clipboard: { writeText: async (text) => { files.clipboard.push(String(text)); } },
  };

  // 把整份 index.html 解析进 document（含 <html>/<head>/<body>）
  parseHTML(html, document, document);
  document.documentElement = document.querySelector('html') || document.createElement('html');
  document.body = document.querySelector('body');
  if (!document.body) {
    document.body = document.createElement('body');
    document.appendChild(document.body);
  }

  defineGlobal('document', document);
  defineGlobal('window', window);
  defineGlobal('localStorage', localStorage);
  defineGlobal('navigator', navigator);
  globalThis.window.localStorage = localStorage;
  globalThis.window.navigator = navigator;
  return { document, window, localStorage, files };
}

/**
 * 把 (event, data) 拼成一个 SSE 帧；这是给测试用的独立实现，
 * 避免「用被测代码验证被测代码」。
 */
export function sseFrame(event, data) {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** 造一个 fetch 响应的 body reader，把 SSE 文本按 chunk 分片吐出去。 */
export function sseResponse(text, { chunks = 2 } = {}) {
  const bytes = new TextEncoder().encode(text);
  const size = Math.max(1, Math.ceil(bytes.length / chunks));
  const pieces = [];
  for (let i = 0; i < bytes.length; i += size) pieces.push(bytes.slice(i, i + size));

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

/** 造一个 JSON 响应。 */
export function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
    async text() { return JSON.stringify(payload); },
  };
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 轮询等待条件成立（默认最多 2 秒）。 */
export async function waitFor(predicate, { timeout = 2000, interval = 10 } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    // eslint-disable-next-line no-await-in-loop
    if (predicate()) return true;
    if (Date.now() > deadline) return false;
    // eslint-disable-next-line no-await-in-loop
    await sleep(interval);
  }
}

export { camelToKebab };
