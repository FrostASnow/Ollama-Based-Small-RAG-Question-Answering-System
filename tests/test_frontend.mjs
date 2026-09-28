/**
 * 前端测试（Node 直接运行，无需浏览器）。
 *
 * 分两部分：
 *   1. 渲染逻辑 —— markdown.js 的 HTML 转义（唯一的安全边界）、语法、引用角标
 *   2. 静态一致性 —— app.js 引用的 DOM id / api.js 导出是否都真实存在
 *
 * 第 2 部分能抓住最常见的两类低级错误：
 *   * `$('setupMute')` 与 `id="setupMute"` 拼写不一致 → 运行时 null 报错
 *   * 重命名了 api.js 的导出但忘了改 import → 模块加载即失败
 * 这类问题靠手点页面很难覆盖全，静态检查反而更彻底。
 *
 * 用法：
 *   node tests/test_frontend.mjs
 */

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..');
const jsDir = join(root, 'frontend', 'assets', 'js');

// markdown.js 没有依赖，用 data URL 直接加载，
// 避免为了 import 一个 .js 而在前端目录塞 package.json
const markdownSource = await readFile(join(jsDir, 'markdown.js'), 'utf8');
const mod = await import(`data:text/javascript;base64,${Buffer.from(markdownSource).toString('base64')}`);
const { renderMarkdown, escapeHtml, renderPlain } = mod;

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
console.log('前端测试：渲染 + 静态一致性');
console.log('='.repeat(70));

/* ================================================================== */
section('1. HTML 转义（XSS 防护）');

check('转义尖括号', escapeHtml('<b>') === '&lt;b&gt;', escapeHtml('<b>'));
check('转义引号', escapeHtml('"a\'') === '&quot;a&#39;', escapeHtml('"a\''));
check('转义 &', escapeHtml('a&b') === 'a&amp;b');

const xssCases = [
  ['script 标签', '<script>alert(1)</script>'],
  ['img onerror', '<img src=x onerror="alert(1)">'],
  ['iframe', '<iframe src="javascript:alert(1)"></iframe>'],
  ['svg onload', '<svg onload=alert(1)>'],
];

for (const [label, payload] of xssCases) {
  const html = renderMarkdown(payload);
  const hasRawTag = /<(script|img|iframe|svg)\b/i.test(html);
  check(`阻止 ${label}`, !hasRawTag, html.slice(0, 70));
}

const linkHtml = renderMarkdown('[点我](javascript:alert(1))');
check('阻止 javascript: 链接', !linkHtml.includes('href="javascript:'), linkHtml.slice(0, 80));

const goodLink = renderMarkdown('[官网](https://example.com)');
check('保留 http(s) 链接',
  goodLink.includes('href="https://example.com"')
  && goodLink.includes('rel="noopener noreferrer"')
  && goodLink.includes('target="_blank"'),
  goodLink.slice(0, 90));

/* ================================================================== */
section('2. 基础语法');

check('标题', renderMarkdown('## 小标题').includes('<h2>小标题</h2>'));
check('加粗', renderMarkdown('这是**重点**内容').includes('<strong>重点</strong>'));
check('斜体', renderMarkdown('这是*斜体*').includes('<em>斜体</em>'));
check('行内代码', renderMarkdown('用 `pip install` 安装').includes('<code>pip install</code>'));

const codeBlock = renderMarkdown('```python\nprint("hi")\n```');
check('代码块', codeBlock.includes('<pre><code') && codeBlock.includes('print(&quot;hi&quot;)'),
  codeBlock.slice(0, 80));

const list = renderMarkdown('- 第一项\n- 第二项');
check('无序列表', list.includes('<ul>') && list.trim().endsWith('</ul>'),
  list.replace(/\n/g, ''));

const ordered = renderMarkdown('1. 甲\n2. 乙');
check('有序列表', ordered.includes('<ol>') && ordered.includes('<li>甲</li>'));

const table = renderMarkdown('| 名称 | 值 |\n| --- | --- |\n| 住宿 | 600 |');
check('表格', table.includes('<table>') && table.includes('<th>名称</th>')
  && table.includes('<td>600</td>'), table.replace(/\n/g, '').slice(0, 100));

check('引用', renderMarkdown('> 提示内容').includes('<blockquote>提示内容</blockquote>'));
check('分割线', renderMarkdown('---').includes('<hr />'));

/* ================================================================== */
section('3. 引用角标（本项目的核心交互）');

const cited = renderMarkdown('住宿费每晚 600 元 [1]，其他城市 400 元 [2]。');
const citeCount = (cited.match(/class="cite"/g) || []).length;
check('渲染出 2 个引用角标', citeCount === 2, `实际 ${citeCount}`);
check('角标带 data-cite 属性',
  cited.includes('data-cite="1"') && cited.includes('data-cite="2"'));
check('角标可聚焦（键盘可达）', cited.includes('tabindex="0"'));
check('连续引用 [1][3]',
  (renderMarkdown('见 [1][3]').match(/class="cite"/g) || []).length === 2);

/* ================================================================== */
section('4. 边界情况');

check('空字符串', renderMarkdown('') === '');
check('null', renderMarkdown(null) === '');
check('undefined', renderMarkdown(undefined) === '');
check('纯空白', renderMarkdown('   \n\n  ').trim() === '');
check('未闭合代码块不崩溃', typeof renderMarkdown('```\ncode') === 'string');
check('未闭合加粗不崩溃', typeof renderMarkdown('**bold') === 'string');

const mixed = renderMarkdown('结论：**600 元** [1]\n\n- 一线城市\n- 其他城市');
check('混合语法', mixed.includes('<strong>600 元</strong>') && mixed.includes('class="cite"')
  && mixed.includes('<li>一线城市</li>'));

check('renderPlain 保留换行', renderPlain('第一行\n第二行').includes('<br />'));
check('renderPlain 也做转义', !renderPlain('<script>x</script>').includes('<script>'));

/* ================================================================== */
section('5. 静态一致性（DOM id 与模块导出）');

const html = await readFile(join(root, 'frontend', 'index.html'), 'utf8');
const appSrc = await readFile(join(jsDir, 'app.js'), 'utf8');
const apiSrc = await readFile(join(jsDir, 'api.js'), 'utf8');

// --- 5a. app.js 用到的 DOM id 必须都在 index.html 里定义 ---
const htmlIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
const usedIds = [...new Set([...appSrc.matchAll(/\$\('([^']+)'\)/g)].map((m) => m[1]))];

check(`app.js 引用了 ${usedIds.length} 个元素 id`, usedIds.length > 0);

const missingIds = usedIds.filter((id) => !htmlIds.has(id));
check('所有引用的 id 都存在于 index.html', missingIds.length === 0,
  missingIds.length ? `缺失: ${missingIds.join(', ')}` : `${htmlIds.size} 个 id 可用`);

// 反向检查：HTML 里定义了但 JS 从没用过的（仅提示，不算失败）
const unusedIds = [...htmlIds].filter((id) => !usedIds.includes(id));
if (unusedIds.length) {
  console.log(`  [INFO] index.html 中未被 app.js 引用的 id：${unusedIds.join(', ')}`);
}

// --- 5b. app.js 从 api.js 导入的函数必须真的被导出 ---
function importedFrom(source, moduleName) {
  const re = new RegExp(`import\\s*\\{([^}]+)\\}\\s*from\\s*'\\./${moduleName}'`, 's');
  const match = source.match(re);
  if (!match) return [];
  return match[1].split(',').map((s) => s.trim()).filter(Boolean);
}

function exportedFrom(source) {
  const names = new Set(
    [...source.matchAll(/export\s+(?:async\s+)?function\s+(\w+)/g)].map((m) => m[1]),
  );
  for (const block of source.matchAll(/export\s*\{([^}]+)\}/g)) {
    block[1].split(',').map((s) => s.trim().split(/\s+as\s+/).pop()).filter(Boolean)
      .forEach((n) => names.add(n));
  }
  return names;
}

for (const [moduleName, source] of [['api.js', apiSrc], ['markdown.js', markdownSource]]) {
  const imported = importedFrom(appSrc, moduleName);
  const exported = exportedFrom(source);
  const missing = imported.filter((name) => !exported.has(name));
  check(`app.js 从 ${moduleName} 导入的 ${imported.length} 个符号都存在`,
    missing.length === 0,
    missing.length ? `未导出: ${missing.join(', ')}` : imported.join(', '));
}

// --- 5c. index.html 正确的资源引用与模块加载 ---
check('引用样式表', html.includes('/assets/css/style.css'));
check('以 module 方式加载入口脚本', html.includes('type="module"'));
check('引用 app.js', html.includes('/assets/js/app.js'));
check('包含配置引导弹窗', html.includes('id="setupModal"'));
check('包含配置未完成入口', html.includes('id="setupBanner"'));
check('弹窗有关闭用的 data 属性', html.includes('data-setup-dismiss'));

// app.js 用 data-setup-dismiss 绑定关闭，两边必须对得上
const htmlDismiss = (html.match(/data-setup-dismiss/g) || []).length;
const jsDismiss = (appSrc.match(/data-setup-dismiss/g) || []).length;
check('data-setup-dismiss 两端一致', htmlDismiss >= 1 && jsDismiss >= 1,
  `html ${htmlDismiss} 处 / js ${jsDismiss} 处`);

// --- 5d. 前端所有静态资源都不能引用外部 CDN（离线要求）---
const externalRefs = [...html.matchAll(/(?:src|href)="(https?:\/\/[^"]+)"/g)]
  .map((m) => m[1])
  .filter((url) => !url.startsWith('data:'));
check('index.html 不引用任何外部 CDN', externalRefs.length === 0,
  externalRefs.join(', ') || '全部为本地资源');

const jsExternal = [...`${appSrc}${apiSrc}${markdownSource}`.matchAll(/https?:\/\/[a-z0-9.-]+/gi)]
  .map((m) => m[0])
  .filter((url) => !url.includes('example.com'));
check('JS 中不含外部 CDN 依赖', jsExternal.length === 0, jsExternal.join(', ') || '无');

/* ================================================================== */
section('6. hidden 属性兜底（曾导致弹窗关不掉）');

const css = await readFile(join(root, 'frontend', 'assets', 'css', 'style.css'), 'utf8');

// 浏览器默认的 [hidden] { display: none } 特异性只有 0-1-0。
// 任何类选择器里写了 display（.modal 是 grid、.sources 是 flex……）
// 都会把它盖掉，于是 el.xxx.hidden = true 完全没有视觉效果 ——
// 表现就是「按钮点了没反应」，实际上是弹窗根本没关。
check('[hidden] 兜底规则存在',
  /\[hidden\]\s*\{\s*display:\s*none\s*!important/.test(css),
  '缺少这条规则时，带 display 的元素无法被 hidden 隐藏');

// 找出 JS 里切换 hidden 的元素，确认相关类确实有 display 冲突风险
const hiddenIds = [...new Set([...appSrc.matchAll(/el\.(\w+)\.hidden\s*=/g)]
  .map((m) => m[1]))];
check(`app.js 切换了 ${hiddenIds.length} 个元素的 hidden`, hiddenIds.length >= 5,
  hiddenIds.join(', '));

// 这些类在 CSS 里显式写了 display，正是最需要兜底的一批
const conflicting = ['modal', 'modal__body--install', 'sources', 'setup-banner'];
for (const cls of conflicting) {
  const hasDisplay = new RegExp(`\\.${cls}\\s*\\{[^}]*display\\s*:`).test(css);
  check(`.${cls} 使用了 display 且有兜底保护`, hasDisplay, 'display 与 [hidden] 冲突的典型');
}

/* ================================================================== */
section('7. 环境面板与后端探测字段一致');

// 真实故障：便携版 Ollama 只装了一半（只有 ollama.exe，缺 lib\ollama 下的
// 推理引擎）。此时服务能启动、模型也能列出，但一提问就报
// llama-server binary not found。如果面板只看「文件在不在」就写「已就绪」，
// 就会和下面的问题卡片自相矛盾，用户根本不知道该信哪个。
check('app.js 消费后端的 ollama_portable 探测结果',
  /environment\.ollama_portable/.test(appSrc));
check('Ollama 行区分出「不完整」这第三种状态',
  /ollamaIncomplete/.test(appSrc) && /'不完整'/.test(appSrc));
check('不完整时 Ollama 行不算就绪',
  /found\s*&&\s*!ollamaIncomplete/.test(appSrc));
check('不完整时在提示里说明缺什么',
  /llama-server\.exe/.test(appSrc), 'tooltip 应指出缺推理引擎');
check('徽标支持自定义文案（不完整 / 缺失 / 已就绪）',
  /okLabel\s*\|\|\s*'已就绪'/.test(appSrc) && /okLabel\s*\|\|\s*'缺失'/.test(appSrc));

/* ================================================================== */
console.log('');
console.log('='.repeat(70));
console.log(`结果：通过 ${passed} 项，失败 ${failed} 项`);
console.log('='.repeat(70));

process.exit(failed === 0 ? 0 : 1);
