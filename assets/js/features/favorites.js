import { DATA, STORE_KEY, favs } from '../core/state.js';
import { toast } from '../core/ui.js';
import { renderList } from './collection.js';

export function toggleFav(fullName) {
  if (!fullName) return;
  if (favs.has(fullName)) {
    favs.delete(fullName);
    toast('已取消置顶');
  } else {
    favs.add(fullName);
    toast('已置顶到本页');
  }
  saveFavs();
  renderList();
}

export function saveFavs() {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify([...favs]));
  } catch {
    /* storage unavailable */
  }
}

export function exportJson() {
  const payload = {
    type: 'github-starhub-backup',
    user: 'ghoustghoust',
    exportedAt: new Date().toISOString(),
    favorites: [...favs],
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const anchor = document.createElement('a');
  anchor.href = URL.createObjectURL(blob);
  anchor.download = 'starhub-backup-' + new Date().toISOString().slice(0, 10) + '.json';
  anchor.click();
  URL.revokeObjectURL(anchor.href);
  toast('已导出本地置顶备份（' + favs.size + ' 项）');
}

export function importJson(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const payload = JSON.parse(reader.result);
      const favorites = payload.favorites || payload.favs || [];
      if (!Array.isArray(favorites)) throw new Error('invalid backup');
      favs.clear();
      favorites
        .filter((fullName) => DATA.some((item) => item.full_name === fullName))
        .forEach((fullName) => favs.add(fullName));
      saveFavs();
      renderList();
      toast('已恢复本地置顶 ' + favs.size + ' 项');
    } catch {
      toast('导入失败：文件格式不正确');
    }
  };
  reader.readAsText(file);
  event.target.value = '';
}
