/* Shared display formatting. */
export function escHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function fmtStars(number) {
  if (number >= 100000) return (number / 1000).toFixed(0) + 'k';
  if (number >= 1000) return (number / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return number;
}

export function fmtDownloads(number) {
  if (number >= 100000000) return (number / 100000000).toFixed(1).replace(/\.0$/, '') + '亿';
  if (number >= 10000) return (number / 10000).toFixed(1).replace(/\.0$/, '') + '万';
  return String(number);
}

export function fmtRelTime(iso) {
  if (!iso) return '';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return '';
  const delta = Date.now() - time;
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (delta < hour) return Math.max(1, Math.round(delta / minute)) + ' 分钟前';
  if (delta < day) return Math.round(delta / hour) + ' 小时前';
  if (delta < 30 * day) return Math.round(delta / day) + ' 天前';
  if (delta < 365 * day) return Math.round(delta / (30 * day)) + ' 个月前';
  return Math.round(delta / (365 * day)) + ' 年前';
}
