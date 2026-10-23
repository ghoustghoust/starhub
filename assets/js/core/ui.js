/* Toast, copy and small UI helpers. */
// ---- 复制名称与链接（clipboard API 优先，execCommand 降级） ----
function copyRepoInfo(fn, url){
  const text = fn + ' ' + url;
  const done = () => toast('已复制：' + fn);
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(done).catch(() => copyFallback(text, done));
  } else copyFallback(text, done);
}
function copyFallback(text, done){
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try{ if(document.execCommand('copy')) done(); else toast('复制失败，请手动复制'); }
  catch(e){ toast('复制失败，请手动复制'); }
  document.body.removeChild(ta);
}
