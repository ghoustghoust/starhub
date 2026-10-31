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
  // execCommand('copy') 已废弃且被安全扫描判为命令注入入口：非安全上下文里
  // 直接引导手动复制（生产站是 https，navigator.clipboard 主路径覆盖现代浏览器）。
  void done;
  toast('复制失败，请手动复制：' + text);
}
