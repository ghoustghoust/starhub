import { GH_TOKEN } from './config.js';

const API = 'https://api.github.com';

export async function gh(path, { method = 'GET', body, timeout = 15000 } = {}) {
  const headers = {
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
    'User-Agent': 'starhub',
  };
  if (GH_TOKEN) headers.Authorization = 'Bearer ' + GH_TOKEN;
  const response = await fetch(API + path, {
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
