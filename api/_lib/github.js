import { GH_TOKEN } from './config.js';

const API = 'https://api.github.com';

// SSRF 护栏：目标 URL 主机钉死为 GitHub API，路径/查询只放行安全字符集。
// 调用方拼接的 path 即使被污染也逃不出这个主机与字符集（对齐 pipeline 的 netguard）。
const SAFE_TARGET_RE = /^[A-Za-z0-9/._~?=&:%-]*$/;

export function assertApiPath(path) {
  let target;
  try {
    target = new URL(API + path);
  } catch {
    throw new Error('github api path rejected');
  }
  if (target.origin !== API || !SAFE_TARGET_RE.test(target.pathname + target.search)) {
    throw new Error('github api path rejected');
  }
  return path;
}

export async function gh(path, { method = 'GET', body, timeout = 15000 } = {}) {
  assertApiPath(path);
  const headers = {
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'starhub',
  };
  if (GH_TOKEN) headers.Authorization = 'Bearer ' + GH_TOKEN;
  const response = await fetch(target, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(timeout),
  });
  if (!response.ok) throw new Error('github ' + response.status);
  return response;
}

export async function ghJson(path, options) {
  return (await gh(path, options)).json();
}
