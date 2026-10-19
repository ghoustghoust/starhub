// lib/rss_cover.js —— 实时链路（?source= / ?batch=）的封面抽取
// 规则与构建期 build_rss_aggregator._pick_item_image 同一套口径，优先级固定：
//   enclosure > media:content > media:thumbnail > 正文首图 > 描述首图
// 为什么单独放 lib：api/rss.js 是 Vercel handler、不可 require，判据只能打在可导出的模块上
// （同 lib/rss_retention.js 的处理办法）。
// 注意：这里**不做**"必挂域名判空"。那张表只有一份，住在构建脚本里并被注入页面产物，
// 页面渲染时统一过滤；在这里再抄一份就是第二个会分叉的事实来源。
'use strict';

// 与 Python 侧 _IMG_SRC_RE 同形：<img ... src="...">，src 值不含空白/引号/尖括号
const IMG_SRC_RE = /<img\b[^>]*?\bsrc\s*=\s*(["'])([^"'\s>]+)\1/i;
// 与 Python 侧 enclosure 的图片判定同形：扩展名 + 可选 query
const IMG_EXT_RE = /\.(jpe?g|png|webp|gif)(\?|$)/i;

// 单次扫描解码：`&amp;lt;` 这类二次编码不该被解成 `<`，与 Python 侧 html.unescape 的行为一致
// （Python 走两趟：ElementTree 解 description 文本一次，_extract_img_from_html 再对 src 解一次）。
var ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };
var ENT_RE = /&(amp|lt|gt|quot|apos|nbsp|#0*38|#0*60|#0*62|#0*34|#x0*26|#x0*3c|#x0*3e|#x0*22);/gi;

function decodeEntities(s) {
  return String(s).replace(ENT_RE, function (_, k) {
    var key = k.toLowerCase();
    if (key.charAt(0) === '#') {
      var hex = key.charAt(1) === 'x';
      var code = parseInt(hex ? key.slice(2) : key.slice(1), hex ? 16 : 10);
      return isNaN(code) ? ' ' : String.fromCharCode(code);
    }
    return ENTITIES[key] !== undefined ? ENTITIES[key] : ' ';
  });
}

function decodeBasic(s) {
  // URL 里的 & 常被写成 &amp;/&#38;/&#x26;，不解码会把签名参数截歪
  return decodeEntities(s);
}

function attr(tagText, name) {
  const m = tagText.match(new RegExp('\\b' + name + '\\s*=\\s*"([^"]*)"', 'i'))
    || tagText.match(new RegExp('\\b' + name + "\\s*=\\s*'([^']*)'", 'i'));
  return m ? decodeBasic(m[1]).trim() : '';
}

function firstImgSrc(html) {
  if (!html) return '';
  // 必须先解实体再找 <img>：主流 feed 的 description 是**转义过**的 HTML
  // （&lt;img src=…&gt;），不解锁死就是"构建期有封面、实时刷新后封面没了"
  // （2026-10-01 判据实测：Python 侧由 ET 解好实体所以能命中，JS 侧不命中）。
  var text = decodeEntities(html);
  const m = IMG_SRC_RE.exec(text);
  if (!m) return '';
  const src = decodeBasic(m[2]).trim();
  return /^https?:\/\//i.test(src) ? src : '';
}

// media:content / media:thumbnail 的可用性判断，与 Python 侧同一条：
// type（或 medium）缺省、以 image 开头、或等于 photo 才算图；audio/video 一律不算。
// 前缀按 RSS 的两种常见写法认（`media:` 与 `m:`）；Python 侧是按命名空间解析的，
// 真正的怪前缀由构建期那条路负责，实时侧不为了它把正则放宽到误匹配 `<content:encoded>`。
function mediaUrl(entry, localName) {
  const m = entry.match(new RegExp('<(?:media|m):' + localName + '[^>]*>', 'i'));
  if (!m) return '';
  const url = attr(m[0], 'url');
  if (!url) return '';
  const type = (attr(m[0], 'type') || attr(m[0], 'medium')).toLowerCase();
  if (!type || type.indexOf('image') === 0 || type === 'photo') return url;
  return '';
}

/**
 * 从一条 entry/item 原文里挑封面。
 * @param {string} entry      该条目的原始 XML 片段（用于 enclosure 与 media:*）
 * @param {string} contentHtml 正文（content:encoded / atom content），可为空
 * @param {string} descHtml    描述（description / summary），可为空
 * @return {string} 图片 URL（http/https），没有可用封面时返回 ''
 */
function pickItemImage(entry, contentHtml, descHtml) {
  const text = String(entry || '');
  if (!text) return '';
  const enc = text.match(/<enclosure[^>]*>/i);
  if (enc) {
    const type = attr(enc[0], 'type').toLowerCase();
    const url = attr(enc[0], 'url');
    if (url && (type.indexOf('image') === 0 || IMG_EXT_RE.test(url))) return url;
  }
  const mediaTags = ['content', 'thumbnail'];
  for (const tag of mediaTags) {
    const url = mediaUrl(text, tag);
    if (url) return url;
  }
  return firstImgSrc(contentHtml) || firstImgSrc(descHtml);
}

// 只导出真被调的两个名字；其余是模块内部细节（有判据守着"导出不许没人用"）
module.exports = { pickItemImage, firstImgSrc };
