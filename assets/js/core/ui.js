import { $ } from './dom.js';

let toastTimer;

export function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('show'), 2000);
}

export function copyRepoInfo(fullName, url) {
  const text = fullName + ' ' + url;
  const done = () => toast('已复制：' + fullName);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => copyFallback(text, done));
  } else {
    copyFallback(text, done);
  }
}

export function copyFallback(text, done) {
  const area = document.createElement('textarea');
  area.value = text;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  try {
    if (document.execCommand('copy')) done();
    else toast('复制失败，请手动复制');
  } catch {
    toast('复制失败，请手动复制');
  }
  document.body.removeChild(area);
}
