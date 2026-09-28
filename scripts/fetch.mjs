/**
 * 通用下载器（Node 内置模块，无第三方依赖）。
 *
 * 两种取源方式：
 *   1) 直接给 URL
 *        node scripts/fetch.mjs <url> <输出路径>
 *   2) 走 GitHub API 解析发布资源（当 github.com 被网络策略屏蔽、
 *      但 api.github.com / release-assets.githubusercontent.com 可用时非常有用）
 *        node scripts/fetch.mjs --github-asset <owner/repo> <tag> <资源名> <输出路径>
 *
 * 通用选项：
 *   --sha256 <期望值>   下载后校验（续传时重新读盘计算，保证可信）
 *   --header "K: V"     追加请求头，可重复
 *
 * 支持断点续传：中断后重跑会从 .part 文件已下载的字节继续。
 */

import { createWriteStream, existsSync, mkdirSync, renameSync, statSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { dirname, resolve } from 'node:path';

const USER_AGENT = 'rag-qa-setup';

// 进度输出策略（踩过的坑）：
// PowerShell 5.1 会**按行**转发原生命令的输出 —— 只有 \r 没有 \n 的内容会被
// 一直攒在缓冲区里，直到进程退出才落盘。于是一次 1.4GB 的下载在安装日志里
// 全程「一片空白」，用户完全看不出它是在下载还是卡死了。
// 所以在非交互场景下改用「\n 结尾的整行进度」（每 5 秒一行），
// 交互式终端下才保留原地刷新的 \r 效果。
const IS_TTY = process.stdout.isTTY === true;
const PROGRESS_INTERVAL_MS = IS_TTY ? 800 : 5000;

function human(bytes) {
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(1)} ${units[unit]}`;
}

function parseArgs(argv) {
  const positional = [];
  const headers = {};
  let sha256 = null;
  let githubAsset = null;

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--sha256') {
      sha256 = argv[++i];
    } else if (arg === '--header') {
      const raw = argv[++i] ?? '';
      const idx = raw.indexOf(':');
      if (idx > 0) headers[raw.slice(0, idx).trim()] = raw.slice(idx + 1).trim();
    } else if (arg === '--github-asset') {
      githubAsset = { repo: argv[++i], tag: argv[++i], name: argv[++i] };
    } else {
      positional.push(arg);
    }
  }

  const out = positional.pop();
  const url = positional.pop();

  if (!out || (!url && !githubAsset)) {
    console.error('用法:');
    console.error('  node scripts/fetch.mjs <url> <输出路径> [--sha256 <值>] [--header "K: V"]');
    console.error('  node scripts/fetch.mjs --github-asset <owner/repo> <tag> <资源名> <输出路径>');
    process.exit(2);
  }

  return { url, out: resolve(out), sha256, headers, githubAsset };
}

/** 通过 GitHub API 把 (仓库, 版本, 资源名) 解析成可直接下载的签名地址。 */
async function resolveGithubAsset({ repo, tag, name }) {
  // tag 为 latest 时必须用 /releases/latest：
  // /releases/tags/latest 会 404 —— 并不存在一个名叫 "latest" 的标签。
  const api = tag === 'latest'
    ? `https://api.github.com/repos/${repo}/releases/latest`
    : `https://api.github.com/repos/${repo}/releases/tags/${tag}`;
  console.log(`[api] ${api}`);
  const response = await fetch(api, {
    headers: { 'User-Agent': USER_AGENT, Accept: 'application/vnd.github+json' },
  });
  if (!response.ok) throw new Error(`GitHub API HTTP ${response.status}`);

  const release = await response.json();
  console.log(`[release] ${release.tag_name || tag}`);
  const asset = (release.assets || []).find((item) => item.name === name);
  if (!asset) {
    const names = (release.assets || []).map((a) => a.name).join(', ');
    throw new Error(`发布 ${tag} 中找不到资源「${name}」。可用资源：${names}`);
  }
  console.log(`[asset] ${asset.name}  ${human(asset.size)}`);
  // 该地址需配合 Accept: application/octet-stream 才会 302 到真实文件
  return { url: asset.url, headers: { Accept: 'application/octet-stream' } };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  let { url, headers } = args;
  const { out, sha256, githubAsset } = args;

  if (githubAsset) {
    const resolved = await resolveGithubAsset(githubAsset);
    url = resolved.url;
    headers = { ...headers, ...resolved.headers };
  }

  mkdirSync(dirname(out), { recursive: true });
  const partPath = `${out}.part`;

  if (existsSync(out) && !sha256) {
    console.log(`[skip] 目标已存在：${out} (${human(statSync(out).size)})`);
    return;
  }

  let startByte = 0;
  if (existsSync(partPath)) {
    startByte = statSync(partPath).size;
    console.log(`[resume] 从 ${human(startByte)} 处续传`);
  }

  const requestHeaders = { 'User-Agent': USER_AGENT, ...headers };
  if (startByte > 0) requestHeaders.Range = `bytes=${startByte}-`;

  console.log(`[get] ${url}`);
  const response = await fetch(url, { headers: requestHeaders, redirect: 'follow' });

  if (startByte > 0 && response.status === 200) {
    console.log('[warn] 服务端不支持续传，改为完整重新下载');
    startByte = 0;
    if (existsSync(partPath)) unlinkSync(partPath);
  } else if (!response.ok) {
    throw new Error(`HTTP ${response.status} ${response.statusText}`);
  }

  const lengthHeader = response.headers.get('content-length');
  const total = lengthHeader ? Number(lengthHeader) + startByte : 0;
  console.log(`[size] 共 ${total ? human(total) : '未知'}`);

  const hash = createHash('sha256');
  const hashWholeFile = startByte === 0;

  let received = startByte;
  let lastPrint = 0;
  const body = Readable.fromWeb(response.body);

  body.on('data', (chunk) => {
    received += chunk.length;
    if (hashWholeFile) hash.update(chunk);
    const now = Date.now();
    if (now - lastPrint > PROGRESS_INTERVAL_MS) {
      lastPrint = now;
      const pct = total ? ((received / total) * 100).toFixed(1) : '?';
      const line = `  ${pct}%  ${human(received)}${total ? ' / ' + human(total) : ''}`;
      process.stdout.write(IS_TTY ? `\r${line}    ` : `${line}\n`);
    }
  });

  await pipeline(body, createWriteStream(partPath, { flags: startByte > 0 ? 'a' : 'w' }));
  if (IS_TTY) process.stdout.write('\n');

  if (sha256) {
    let actual;
    if (hashWholeFile) {
      actual = hash.digest('hex');
    } else {
      // 续传场景下先前写入的字节没进过哈希，只能重新读盘
      const { createReadStream } = await import('node:fs');
      const verifyHash = createHash('sha256');
      for await (const chunk of createReadStream(partPath)) verifyHash.update(chunk);
      actual = verifyHash.digest('hex');
    }
    if (actual.toLowerCase() !== sha256.toLowerCase()) {
      console.error(`[FAIL] sha256 不匹配\n  期望 ${sha256}\n  实际 ${actual}`);
      console.error(`  已保留文件便于排查：${partPath}`);
      process.exit(1);
    }
    console.log('[ok] sha256 校验通过');
  }

  renameSync(partPath, out);
  console.log(`[done] ${out} (${human(statSync(out).size)})`);
}

main().catch((error) => {
  console.error(`\n[FAIL] ${error.message}`);
  process.exit(1);
});
