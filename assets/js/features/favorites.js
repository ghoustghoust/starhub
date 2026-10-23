/* Favorites, export and import. */
function toggleFav(fn){
  if(!fn) return;
  if(favs.has(fn)){ favs.delete(fn); toast('已取消收藏'); }
  else { favs.add(fn); toast('已收藏并置顶'); }
  saveFavs();
  renderList();
}

function saveFavs(){
  try{ localStorage.setItem(STORE_KEY, JSON.stringify([...favs])); }catch(e){}
}

function exportJson(){
  const payload = {
    type: 'github-starhub-backup',
    user: 'ghoustghoust',
    exportedAt: new Date().toISOString(),
    favorites: [...favs],
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'starhub-backup-'+new Date().toISOString().slice(0,10)+'.json';
  a.click();
  URL.revokeObjectURL(a.href);
  toast('已导出备份（收藏 '+favs.size+' 项）');
}

function importJson(e){
  const f = e.target.files[0]; if(!f) return;
  const r = new FileReader();
  r.onload = () => {
    try{
      const obj = JSON.parse(r.result);
      const arr = obj.favorites || obj.favs || [];
      if(!Array.isArray(arr)) throw 0;
      favs = new Set(arr.filter(x => DATA.some(d=>d.full_name===x)));
      saveFavs(); renderList();
      toast('已恢复收藏 '+favs.size+' 项');
    }catch(err){ toast('导入失败：文件格式不正确'); }
  };
  r.readAsText(f);
  e.target.value = '';
}

let toastTimer;
function toast(msg){
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=>t.classList.remove('show'), 2000);
}
