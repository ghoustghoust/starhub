/**
 * lib/body_rules.js —— 运行时正文规范层（零依赖，纯字符串函数；不引任何第三方包）。
 *
 * 为什么要有这个文件：正文规范在构建期已经有一份 Python 实现
 * （build_rss_aggregator.py 的 `_normalize_body_html` / `_cap_body` / `_is_placeholder_src`），
 * 但运行时两条通道不吃构建产物 —— `api/article.js` 用 Readability 现抓现出，
 * `api/rss.js` 的 `?source=`/`?batch=` 自己发 HTTP 抓取。所以这里必须是**第二份**实现，
 * 而对账判据 tests/site_nav/test_body_rules_parity.py 负责让"两份不一样"当场变红。
 * 本仓已经被这种分叉咬过两次：_needs_translation（5/14 条分叉）、封面判空大写 scheme。
 *
 * 参照物只有一个：`build_rss_aggregator.py` 的这四个函数
 *   `_is_placeholder_src` / `_normalize_body_html` / `_unambiguous_entity_start` / `_cap_body`
 * 加上 R43 接进实时通道的三把刀：
 *   `_rewrite_hn_summary` / `_strip_glued_url` / `_delink_nav_links`
 *   （端口分别是 `rewriteHnSummary` / `stripGluedUrl` / `delinkNavLinks`）。
 * 再加上审查 ④ 那一格：构建期 `_deep_clean_html` 的**规则 2.6**（空锚点/站点根链接整枚删）
 *   以前只有 Python 一份，实时出口的 `fc` 漏它 ⇒ 端口 `dropEmptyAnchorLinks`
 *   （调用点同样是 `api/rss.js` 的 deepCleanHtml，位置照构建期的 2.6）。
 * 对账红了只改这里，不改 Python，也不改样本口径（账本 R24/R25/R26/R27/R28）。
 *
 * 三处刻意的"不能照抄 JS 直觉"：
 *   1) 绝对化是**手写 join**（镜像 CPython 的 `urljoin`/`urlparse`/`urlunsplit`），
 *      因为 WHATWG 的解析器会百分号编码中文与空格、会把 `%2e%2e` 当点段折叠，
 *      而 `urljoin` 保留原始字节。构建期其余环节都按原始字节走，出厂值必须一致。
 *   2) 空白判定用下面那张**显式码位表**，不用引擎自带的空白类，也不用 trim 系列方法
 *      （两者的集合与 Python 的 `isspace()` 差 6 个码位：少 1C-1F 与 85，多 FEFF）。
 *      刀二回溯游标、出口 trim、属性值 strip、属性名周围的空白，用的都是这一份常量。
 *   3) 属性名映射表是 null 原型（键来自不可信 HTML，裸对象会踩 __proto__ / toString）。
 *
 * 本文件的 `normalizeBodyHtml` 是**运行时专属**：只加 `src` 与绝对化，
 * 不做构建期 `_sanitize_html` 那套标签白名单（白名单属构建期；运行时那半住在
 * `api/rss.js` 的 sanitizeHtml，两条通道产物字段不同，本计划不合并它们）。
 * 解双重转义（构建期 `_unescape_escaped_body`）同样**不在**这一层：本层的契约是
 * "转义态正文里一个标签都看不见"，与 Python 侧逐字同形，由对账语料钉住。
 */
"use strict";

// ---------------------------------------------------------------- 空白码位表
// 唯一真相：Python 侧 `str.isspace()` == `str.rstrip()` 的默认集 == Python 正则的空白类，
// 逐码位实测（账本 R24）就是下面这 29 个（BMP 内）。U+0000-0008、0E-1B、180E、2060、
// FEFF 都**不是**空白 —— 引擎自带的空白类与内置 trim 系列都不等于这张表。
const WS_RANGES = [
  [0x09, 0x0d], // \t \n \v \f \r
  [0x1c, 0x20], // 文件分隔符 1C-1F + 空格
  [0x85, 0x85], // NEL
  [0xa0, 0xa0], // NBSP（中文正文里到处都是）
  [0x1680, 0x1680],
  [0x2000, 0x200a],
  [0x2028, 0x2029],
  [0x202f, 0x202f],
  [0x205f, 0x205f],
  [0x3000, 0x3000]
];
const WS_CLASS = "[" + WS_RANGES.map(function (r) {
  return _cpEsc(r[0]) + (r[1] === r[0] ? "" : "-" + _cpEsc(r[1]));
}).join("") + "]";
// 出口 trim / 刀二游标 / 属性值 strip 全部走这一张表；码位 ≥0x10000 一律不是空白。
const WS_TABLE = (function () {
  const t = new Uint8Array(0x10000);
  WS_RANGES.forEach(function (r) { for (let c = r[0]; c <= r[1]; c++) t[c] = 1; });
  return t;
})();
const WHITESPACE_CODE_POINTS = (function () {
  const out = [];
  WS_RANGES.forEach(function (r) { for (let c = r[0]; c <= r[1]; c++) out.push(c); });
  return out;
})();

function _cpEsc(cp) {
  if (cp === 0x09) return "\\t";
  if (cp === 0x0a) return "\\n";
  if (cp === 0x0b) return "\\v";
  if (cp === 0x0c) return "\\f";
  if (cp === 0x0d) return "\\r";
  if (cp < 0x100) return "\\x" + (cp < 16 ? "0" : "") + cp.toString(16);
  return "\\u" + ("000" + cp.toString(16)).slice(-4);
}

// 词字符类：Python 的 `\w`（str 模式）是 **Unicode** 口径 —— 汉字算词字符。
// 照抄 JS 的 ASCII `\w` 会让 `<img图片="1" data-src=…>` 被当成真 img 标签、
// 让 `图data-src="…"` 被当成真懒加载属性，运行时凭空改写出厂 HTML。
// （比 Python 3.11 的 UnicodeDB 宽 161 个新版分配码位，方向是"少改写"，登记在报告里。）
const WORD_BODY = "\\p{L}\\p{N}_";
const WORD_ONE = new RegExp("^[" + WORD_BODY + "]$", "u");
const WORD_CLASS = "(?<![" + WORD_BODY + "-])";

function _isWs(ch) {
  return ch.length === 1 && WS_TABLE[ch.charCodeAt(0)] === 1;
}
function _isWordChar(cp) {
  if (!(cp >= 0 && cp <= 0x10ffff)) return false;
  return WORD_ONE.test(String.fromCodePoint(cp));
}
// Python 的 str.strip()：两端退掉表内空白。绝不用 trim()/trimEnd()/trimStart()。
function _strip(s) {
  let i = 0, j = s.length;
  while (i < j && _isWs(s.charAt(i))) i++;
  while (j > i && _isWs(s.charAt(j - 1))) j--;
  return s.slice(i, j);
}
function _startsWithAny(s, list) {
  for (let i = 0; i < list.length; i++) if (s.indexOf(list[i]) === 0) return true;
  return false;
}

// ---------------------------------------------------------------- 常量（与 Python 侧同名同值）
const LAZY_SRC_ATTRS = ["data-src", "data-original", "data-lazy-src", "data-croporisrc"];
const BODY_CAP = 50000;
const PLACEHOLDER_SRC_NAMES = ["blank", "lazy", "placeholder", "spacer"];
// 窗口宽度是**推**出来的：名段那条正则最长匹配 = 1 枚 `&` + 8 个实体名字符 = 1 + 8，
// 且任何匹配都以串尾结束 ⇒ 起点一定落在最后 9 个字符内 ⇒ 只看这 9 个字符 = 看整串。
// 改量词必须同时改这里（对账判据 test_entity_window_and_placeholder_name_table_are_shared）。
const ENTITY_NAME_MAX = 8;
const ENTITY_TAIL_WINDOW = 1 + ENTITY_NAME_MAX;

const WS_STAR = WS_CLASS + "*";
const NOT_WORD = "(?![" + WORD_BODY + "])";
const IMG_TAG_RE = new RegExp("<img" + NOT_WORD + "[^>]*>", "giu");
const IMG_OPEN_RE = new RegExp("^<img" + NOT_WORD, "iu");
// 前置断言：少了它就命中 data-src 的后半，凭空造出第二个 src。
const IMG_SRC_ATTR_RE = new RegExp(WORD_CLASS + "src" + WS_STAR + "=" + WS_STAR + '"([^"]*)"', "iu");
const URL_ATTR_RE = new RegExp(WORD_CLASS + "(src|href|poster)" + WS_STAR + "=" + WS_STAR + '"([^"]*)"', "giu");
const ATTR_VALUE_RE = new RegExp("([" + WORD_BODY + "-]+)" + WS_STAR + "=" + WS_STAR + '"([^"]*)"', "giu");
const PLACEHOLDER_DIM_RE = new RegExp("(?:^|[^0-9a-z])(?:1x1|0x0)(?:$|[^0-9a-z])", "iu");
// 尾部**未闭合**标签片段那一刀见 `_danglingTagStart`（码点数组下标，不走正则）。
// 第一次动刀专用：允许 0 个后继字符（截断正好停在 `&` 上也是残缺），名段额度只有一次。
const DANGLING_ENTITY_RE = new RegExp("&[#A-Za-z0-9]{0," + ENTITY_NAME_MAX + "}$", "u");
// 第二刀起、以及第一刀什么都没剥到时，只认这三条**无歧义**形态，且量词无界
// （`a&#000000000z` 的长数字尾巴要能剥净）。数字是 ASCII 0-9，不是"任何 Unicode 十进制数字"。
const DIGIT_ENTITY_CHARS = "0123456789";
const HEX_ENTITY_CHARS = "0123456789abcdefABCDEF";

// ---------------------------------------------------------------- 实体解码（审查 ③ / 裁定 R50）
// 参照物 = CPython 3.11 的 `html.unescape`（构建期 `_strip_html`、快照 `s` 出口、历史清扫那
// 三条链路用的都是它）。修前 `api/rss.js` 是**顺序级联**（`&amp;` 先解 ⇒ `&amp;lt;` 被二次解成
// 真 `<`）+ `&[a-z]+;`→空串 + `&#\d+;`→空串，2026-10-05 拿同一条 `<description>` 喂两侧实测：
//   `价格 A &amp;amp; B 折扣`        py `价格 A &amp; B 折扣`     / js `价格 A  B 折扣`（多解一层）
//   `&copy; 2026 版权所有`           py `© 2026 版权所有`         / js ` 2026 版权所有`（名字被吃掉）
//   `&amp;lt;script&amp;gt;alert(1)` py `&lt;script&gt;alert(1)` / js 一路解成真标签、整条摘要没了
// 另实测 `&#20998;`（汉字"分"的数字形态）被 `&#\d+;`→'' 整枚吃掉 —— 那不是分叉，是吃内容。
//
// **本格只对齐"原始 feed 里证明有暴露的那一圈"**（R50：不许为一条缺陷搬进一套新机制）：
//   1) legacy 预定义那四个 `&amp; &lt; &gt; &quot;`，含 HTML4 的"可省分号"与大写两种 legacy 形态
//      （真语料实测到 `&gt；` —— 全角分号手误，7 次 —— 走的就是"省分号"这一格）；
//      2026-10-05 复核 P0-3 又按**原始 feed 普查**补进 6 个名字（nbsp/apos/rsquo/rarr/ldquo/rdquo）
//      ⇒ 这张表现在是 **10 个名字 / 14 个键**，"预定义 5 个"那句是普查前的旧口径、已作废；
//   2) 数字/十六进制引用 `&#\d+;?`、`&#x[0-9a-fA-F]+;?`，CPython 那三张坏码表按**范围式**
//      重写（126 个坏码位 = 5 段连续 + 每平面末两个；34 项规范映射 = 0x00、0x0d 与 0x80–0x9f），
//      分支顺序逐字照抄 `_replace_charref`（顺序换了 `&#13;` 就会给空串而不是 `\r`）；
//   3) **单趟**扫描：替换产物不再参与判决 ⇒ 正文里合法存在的 `&amp;lt;img&amp;gt;`（要展示的
//      代码字面量）只会解成 `&lt;img&gt;`，仍是字面量，不会被激活成真标签。
// 与 Python 的**唯一分叉**：表外的命名实体（`&copy;` → py `©`）我们**整枚原样留着**，
// 绝不删除、绝不置空。这条分叉面的账要**分两头**读，不许再并成一句"零暴露"
// （"在已解码语料上数零暴露"正是本批点名的空集读数）：
//   · 取数总体那一头：2026-10-05 在 **89 个上游源的原始 feed** 上普查（抽样 89/957 源 = 9.3%，
//     957 取 `rss_sources.json`），`&名字;` 共 **10 种** ⇒ 这 10 种全部进了表，表外**仍有**
//     Python 会解的名字：`&copy;` 与"只认特定大小写形态"那一圈 `&Rarr;`(U+21A0)/`&rArr;`(U+21D2)/
//     `&Larr;`(U+219E)/`&hArr;`(U+21D4) —— 它们今天零出现，按 R50 **不扩表**，记成**已声明分叉**；
//   · 另一头是 `rss_history.json`（18,037 条 / 41,763 个 `&` token，只读、零网络）：这批字段
//     **入库前已经过 `html.unescape`** ⇒ raw 形态的命名 token 在这里结构上数不到，所以它只证明
//     "入库文本里不再引入新的还会被解开的命名 token"，**不**证明"raw 层零暴露"；
//   · 钉住的两处（都不读语料、CI 每场必跑）：
//     tests/site_nav/test_body_rules_parity.py::test_entity_name_matrix_matches_python_per_form
//     （名字 × 形态矩阵，含 title-case 那一档；已声明分叉逐格写死在 `_ENT_DECLARED_TITLECASE`，
//     箭头那四个名字在 `_ENT_OFF_TABLE_NAMES` 里当对照名比）与
//     ::test_runtime_entity_exit_gate_runs_without_any_corpus（真出口 + `_SYN_OFFTABLE` 样本）；
//     期望值另由 ::test_entity_named_divergence_is_declared 写死成"字面量"，谁把它改回
//     "替成空串"当场红（那正是修前的缺陷形状）。哨兵 ::test_corpus_named_entity_sentinel
//     是**本地可选度量**（没有大语料就 skip），不许当闸用。
// 为什么不搬整张 `html.entities.html5`（2,231 键，本波前一版搬过一次、已按 R50 撤掉）：
// 普查里除那 10 种之外的名字在**这 89 个源**（9.3% 抽样，不是全体源都数过）的原始 feed 上
// 今天 0 次，而代价是一整套第二机制（生成表 + 三张坏码表 + 码点
// 下标的前缀匹配），本仓点名的"两份实现分叉"就多发一处。

// 两个捕获组分别是"数字本体"与"名字本体"（都不含前导 `&`，`;` 在组外）。必须是**捕获**组：
// 写成 `(?:` 的话回调第二参变成 offset，跑起来就是 `digits.charAt is not a function`
// （本仓点名的"接口看着对、跑起来空"形状）。
// 2026-10-05 复核 P0-3：口径必须是"**Python `html.unescape` 今天真会解的名字**"，
// 不是"已解码语料里出现的名字"（用后者测等于在空集上判"零暴露"）。
// 现取证据：89 个上游源原始 feed 普查，`&名字;` 共 10 种 —— 除 amp/lt/gt/quot（含大写 legacy）外，
// Python 还会解 nbsp(241)/apos(31)/rsquo(21)/rarr(6)/ldquo(2)/rdquo(2)，共 303 处，而这张表原样漏解。
// 分号规则按**实测**而不是按 HTML5 规范抄：CPython 3.11 `html.unescape` 对
//   `&amp/&lt/&gt/&quot`（含大写）与 `&nbsp` **无分号也解**（实测 `&nbsp`→ 、`a&nbspb`→a b），
// 而 `&apos/&rsquo/&rarr/&ldquo/&rdquo` **必须带分号**（实测 `&apos ` 原样留着）。
// ⚠ 2026-10-05 复评纠正：上面两句是**逐名**口径，不是一条"小写解 / 大写不解"的全局规则，
//   别把它规范成全局的（旧注释写的"大写形态一律不解"就是把它当了一刀切：对
//   nbsp/apos/rsquo/ldquo/rdquo 成立，对下面这三格**不成立**）：
//   · legacy 四个的大写带不带分号都解（实测 `&AMP` → &，`&LT x` → 小于号加空格）；
//   · `&Lt;`/`&Gt;` 在 HTML5 里是**另两枚实体**（U+226A / U+226B），Python 解、我们不解，
//     但**无分号**的 `&Lt` 又不解 —— 同一名在四档上的差别就是"逐名"这两个字的意思；
//   · `&Rarr;`(U+21A0)/`&rArr;`(U+21D2)/`&Larr;`(U+219E)/`&hArr;`(U+21D4) 是**带分号才解**、
//     且只认这几个大小写形态，而 `&nbsp` 无分号也解。
//   这四枚箭头名与 Lt/Gt 按裁定**不进表**（89 源普查零出现，R50）⇒ 记成已声明分叉，
//   钉法见上面那段（矩阵的 title-case 那一档 + `_SYN_OFFTABLE` 那两条样本）。
const _ENT_SEMI_ONLY = new Set(["apos", "rsquo", "rarr", "ldquo", "rdquo"]);
const _ENT_RE = new RegExp(
  "&#([0-9]+|[xX][0-9a-fA-F]+);?|&(amp|AMP|lt|LT|gt|GT|quot|QUOT|nbsp|apos|rsquo|rarr|ldquo|rdquo);?", "gu");

// 表外的名字**既不进正则的交替支、也不进这张表**，所以键与交替支一一对应，一共 **14 个**：
// legacy 四个的小写 + 大写 = 8 个，再加 2026-10-05 普查补的 nbsp/apos/rsquo/rarr/ldquo/rdquo
// 六个小写键（旧注释写"键只有那 8 个形态"是普查前的读数，已作废）。null 原型是本仓对查表的规矩。
// 交替支只枚举表内名 ⇒ 回调里 `_ENT_MIN[name]` 拿不到 undefined；谁给交替支加了名字却没配键，
// 判据 1 的名字 × 形态矩阵当场红（电池靶 R70 钉的就是这一格）。
const _ENT_MIN = (function () {
  const t = Object.create(null);
  t["amp"] = "&"; t["AMP"] = "&";
  t["lt"] = "<"; t["LT"] = "<";
  t["gt"] = ">"; t["GT"] = ">";
  t["quot"] = "\""; t["QUOT"] = "\"";
  t["nbsp"] = " "; t["apos"] = "'";
  t["rsquo"] = "’"; t["rarr"] = "→";
  t["ldquo"] = "“"; t["rdquo"] = "”";
  return t;
})();

// `_invalid_charrefs` 里 0x80–0x9f 那 32 项（CP1252 → Unicode 的规范映射）。注意
// 0x81/0x8d/0x8f/0x90/0x9d 映射到**自己**（仍是 C1 控制字符），不是替换字符。
const _ENT_CP1252 = "\u20ac\u0081\u201a\u0192\u201e\u2026\u2020\u2021\u02c6\u2030\u0160\u2039\u0152\u008d\u017d\u008f\u0090\u2018\u2019\u201c\u201d\u2022\u2013\u2014\u02dc\u2122\u0161\u203a\u0153\u009d\u017e\u0178";

/** 坏码位（CPython `_invalid_codepoints` 的 126 项压成范围）：0x01–0x08、0x0b、0x0e–0x1f、
 *  0x7f、0xfdd0–0xfdef，加上每个平面的最后两个（`n >= 0xfffe && (n & 0xfffe) === 0xfffe`
 *  ⇒ 0xfffe/0xffff/0x1fffe/…/0x10fffe/0x10ffff）。0x80–0x9f 那一段被上面的规范映射**先**
 *  接走，所以这里只到 0x7f —— 与 Python 的分支顺序同形（写反了 `&#13;` 就变空串）。 */
function _isBadCodePoint(n) {
  if ((n >= 0x01 && n <= 0x08) || n === 0x0b || (n >= 0x0e && n <= 0x1f) || n === 0x7f) return true;
  if (n >= 0xfdd0 && n <= 0xfdef) return true;
  return n >= 0xfffe && (n & 0xfffe) === 0xfffe;
}

/** 数字实体：`digits` 是 `&#` 之后、`;` 之前的那截（可能以 `x`/`X` 开头）。
 *  分支顺序 = CPython `_replace_charref`：规范映射 → 代理区/超范围 → 坏码位 → `chr()`。 */
function _decodeNumericEntity(digits) {
  const isHex = digits.charAt(0) === "x" || digits.charAt(0) === "X";
  const num = parseInt(isHex ? digits.slice(1) : digits, isHex ? 16 : 10);
  if (num === 0) return "\ufffd";
  if (num === 0x0d) return "\r";
  if (num >= 0x80 && num <= 0x9f) return _ENT_CP1252.charAt(num - 0x80);
  if (num > 0x10FFFF || (num >= 0xD800 && num <= 0xDFFF)) return "\ufffd";
  if (_isBadCodePoint(num)) return "";
  return String.fromCodePoint(num);      // parseInt 到这里必是 ≤0x10FFFF 的整数，无精度问题
}

/** `html.unescape` 在 R50 那一圈上的端口：**单趟**扫描替换，替换产物不再参与判决（不级联）；
 *  认不出的 `&…` 一律整枚原样留着，一个字符都不许少。 */
function decodeEntities(text) {
  if (text === null || text === undefined) return "";
  const s = String(text);
  if (s.indexOf("&") < 0) return s;          // 与 html.unescape 的快路径同形
  return s.replace(_ENT_RE, function (whole, digits, name) {
    if (typeof name === "string") {
      // 非 legacy 的名字少了分号就不是实体：整枚原样留着（与 Python 同判）。
      if (_ENT_SEMI_ONLY.has(name) && !whole.endsWith(";")) return whole;
      return _ENT_MIN[name];
    }
    return _decodeNumericEntity(digits);
  });
}

// ---------------------------------------------------------------- 懒加载提升 + 绝对化
/** 解析一个标签里的 `名字="值"` 对；首个出现取胜，null 原型表（不可信键名）。 */
function parseImgAttrs(tagText) {
  const seen = Object.create(null);
  for (const m of String(tagText).matchAll(ATTR_VALUE_RE)) {
    const k = m[1].toLowerCase();
    if (!(k in seen)) seen[k] = _strip(m[2]);
  }
  return seen;
}

/** src 是不是"占位图"。判据一律朝**保留原 src**那一侧收：把真图误判成占位比不修更糟。 */
function isPlaceholderSrc(value) {
  const v = _strip(value == null ? "" : String(value));
  if (!v) return false;
  if (v.slice(0, 5).toLowerCase() === "data:") return true;
  const path = v.split("?")[0].split("#")[0];
  const seg = path.split("/")[path.split("/").length - 1];
  const stem = seg.split(".")[0].toLowerCase();
  if (!stem) return false;
  if (PLACEHOLDER_SRC_NAMES.indexOf(stem) >= 0) return true;
  return PLACEHOLDER_DIM_RE.test(stem);
}

function normalizeBodyHtml(html, baseUrl) {
  if (!html) return "";
  const base = _strip(baseUrl == null ? "" : String(baseUrl));
  // 带裸引号的 base 按"没有 base"处理：urljoin 的产物会把引号带进属性值，
  // 下游提取属性时在第一个引号处截断 ⇒ 出厂一条静默变形的坏 URL。宁可 404。
  const hasBase = _startsWithAny(base.toLowerCase(), ["http://", "https://"]) && base.indexOf('"') < 0;

  const fixImg = function (tag) {
    const seen = parseImgAttrs(tag);
    let lazy = "";
    for (let i = 0; i < LAZY_SRC_ATTRS.length; i++) {
      const v = seen[LAZY_SRC_ATTRS[i]];
      if (v) { lazy = v; break; }
    }
    if (!lazy) return tag;
    const cur = IMG_SRC_ATTR_RE.exec(tag);
    const curV = cur ? _strip(cur[1]) : "";
    if (curV && !isPlaceholderSrc(curV)) return tag; // 真图已在 src，镜像属性不动它
    // lazy 来自 feed，是**不可信数据**：绝不用字符串替换模板（`$&`/`$1`/`` $` `` 会被展开），
    // 一律走回调返回拼好的字面量（对应 Python 侧的 lambda 替换，缺陷 C1 同一类）。
    if (cur) return tag.replace(IMG_SRC_ATTR_RE, function () { return 'src="' + lazy + '"'; });
    return tag.replace(IMG_OPEN_RE, function (open) { return open + ' src="' + lazy + '"'; });
  };

  const fixUrl = function (full, name, val) {
    const v = _strip(val);
    if (!v || !hasBase) return full;
    if (v.charAt(0) === "#" || _startsWithAny(v.toLowerCase(),
        ["http:", "https:", "mailto:", "tel:", "data:", "//"])) return full;
    const joined = urlJoin(base, v);
    if (!_startsWithAny(joined.toLowerCase(), ["http://", "https://"])) return full;
    return name + '="' + joined + '"';
  };

  let out = String(html).replace(IMG_TAG_RE, function (tag) { return fixImg(tag); });
  out = out.replace(URL_ATTR_RE, function (full, name, val) { return fixUrl(full, name, val); });
  return out;
}

// ---------------------------------------------------------------- 手写 join（CPython 3.11 口径）
const SCHEME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.";
const USES_RELATIVE = ["", "ftp", "http", "gopher", "nntp", "imap", "wais", "file", "https",
  "shttp", "mms", "prospero", "rtsp", "rtsps", "rtspu", "sftp", "svn", "svn+ssh", "ws", "wss"];
const USES_NETLOC = ["", "ftp", "http", "gopher", "nntp", "telnet", "imap", "wais", "file",
  "mms", "https", "shttp", "snews", "prospero", "rtsp", "rtsps", "rtspu", "rsync", "svn",
  "svn+ssh", "sftp", "nfs", "git", "git+ssh", "ws", "wss"];

function _isAsciiAlpha(c) { return (c >= "a" && c <= "z") || (c >= "A" && c <= "Z"); }
/** urlsplit 会 lstrip 掉前导 C0 与空格，并**删除**全文里的制表/回车/换行。 */
function _sanitizeRaw(s) {
  let i = 0;
  while (i < s.length && s.charCodeAt(i) <= 0x20) i++;
  return s.slice(i).split("\t").join("").split("\n").join("").split("\r").join("");
}

/** 镜像 urllib.parse.urlparse（含 `;params` 那一刀），返回 6 字段。 */
function urlParse(raw, defaultScheme) {
  const schemeDefault = defaultScheme ? _sanitizeRaw(_stripAsciiBoth(defaultScheme)) : "";
  let url = _sanitizeRaw(String(raw));
  let scheme = schemeDefault, netloc = "", query = "", fragment = "";
  const i = url.indexOf(":");
  if (i > 0 && _isAsciiAlpha(url.charAt(0))) {
    let ok = true;
    for (let k = 0; k < i; k++) if (SCHEME_CHARS.indexOf(url.charAt(k)) < 0) { ok = false; break; }
    if (ok) { scheme = url.slice(0, i).toLowerCase(); url = url.slice(i + 1); }
  }
  if (url.slice(0, 2) === "//") {
    // _splitnetloc(url, 2)：`/?#` 三者里最早出现的位置（都从下标 2 往后找）
    let end = url.length;
    const marks = ["/", "?", "#"];
    for (let k = 0; k < marks.length; k++) {
      const w = url.indexOf(marks[k], 2);
      if (w >= 0 && w < end) end = w;
    }
    netloc = url.slice(2, end);
    url = url.slice(end);
  }
  const h = url.indexOf("#");
  if (h >= 0) { fragment = url.slice(h + 1); url = url.slice(0, h); }
  const qq = url.indexOf("?");
  if (qq >= 0) { query = url.slice(qq + 1); url = url.slice(0, qq); }
  let params = "";
  if (USES_NETLOC.indexOf(scheme) >= 0 && url.indexOf(";") >= 0) {
    const start = url.lastIndexOf("/");
    const si = url.indexOf(";", start);
    if (si >= 0) { params = url.slice(si + 1); url = url.slice(0, si); }
  }
  return { scheme: scheme, netloc: netloc, path: url, params: params, query: query, fragment: fragment };
}

function _stripAsciiBoth(s) {
  let a = 0, b = s.length;
  while (a < b && s.charCodeAt(a) <= 0x20) a++;
  while (b > a && s.charCodeAt(b - 1) <= 0x20) b--;
  return s.slice(a, b);
}

function urlUnsplit(scheme, netloc, url, query, fragment) {
  if (netloc || (scheme && USES_NETLOC.indexOf(scheme) >= 0 && url.slice(0, 2) !== "//")) {
    if (url && url.charAt(0) !== "/") url = "/" + url;
    url = "//" + netloc + url;
  }
  if (scheme) url = scheme + ":" + url;
  if (query) url = url + "?" + query;
  if (fragment) url = url + "#" + fragment;
  return url;
}
function urlUnparse(scheme, netloc, url, params, query, fragment) {
  return urlUnsplit(scheme, netloc, params ? url + ";" + params : url, query, fragment);
}

/** 与 urllib.parse.urljoin(base, url) 逐字节同形的实现（不做任何百分号编码）。 */
function urlJoin(base, url) {
  if (!base) return url;
  if (!url) return base;
  const b = urlParse(base, "");
  const u = urlParse(url, b.scheme);
  if (u.scheme !== b.scheme || USES_RELATIVE.indexOf(u.scheme) < 0) return url;
  let scheme = u.scheme, netloc = u.netloc, path = u.path, params = u.params;
  let query = u.query, fragment = u.fragment;
  if (USES_NETLOC.indexOf(scheme) >= 0) {
    if (netloc) return urlUnparse(scheme, netloc, path, params, query, fragment);
    netloc = b.netloc;
  }
  if (!path && !params) {
    path = b.path; params = b.params;
    if (!query) query = b.query;
    return urlUnparse(scheme, netloc, path, params, query, fragment);
  }
  const baseParts = b.path.split("/");
  if (baseParts[baseParts.length - 1] !== "") baseParts.pop();
  let segments;
  if (path.slice(0, 1) === "/") {
    segments = path.split("/");
  } else {
    segments = baseParts.concat(path.split("/"));
    if (segments.length > 2) {
      const mid = segments.slice(1, segments.length - 1).filter(function (s) { return s !== ""; });
      segments = [segments[0]].concat(mid, [segments[segments.length - 1]]);
    }
  }
  const resolved = [];
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    if (seg === "..") resolved.pop();
    else if (seg !== ".") resolved.push(seg);
  }
  const last = segments[segments.length - 1];
  if (last === "." || last === "..") resolved.push("");
  return urlUnparse(scheme, netloc, resolved.join("/") || "/", params, query, fragment);
}

// ---------------------------------------------------------------- 截断
/** 无歧义残缺实体的最左起点（`&` / `&#`+十进制* / `&#x`+十六进制*）；没有则 null。
 *  右往左扫描，与整串正则逐字等价且每轮只吃切点左边那一段 ⇒ 刀二整体 O(n)。
 *  三条分支的**先后与提前返回**照抄 `_unambiguous_entity_start`：`x` 那一支不落到十六进制支。 */
function _unambiguousEntityStart(chars, end) {
  if (end <= 0) return null;
  const last = chars[end - 1];
  if (last === "&") return end - 1;
  if (last === "#") return (end >= 2 && chars[end - 2] === "&") ? end - 2 : null;
  if (last === "x") return (end >= 3 && chars[end - 2] === "#" && chars[end - 3] === "&") ? end - 3 : null;
  if (DIGIT_ENTITY_CHARS.indexOf(last) >= 0) {
    let d = end - 1;
    while (d > 0 && DIGIT_ENTITY_CHARS.indexOf(chars[d - 1]) >= 0) d--;
    if (d >= 2 && chars[d - 1] === "#" && chars[d - 2] === "&") return d - 2;
  }
  if (HEX_ENTITY_CHARS.indexOf(last) >= 0) {
    let h = end - 1;
    while (h > 0 && HEX_ENTITY_CHARS.indexOf(chars[h - 1]) >= 0) h--;
    if (h >= 3 && chars[h - 1] === "x" && chars[h - 2] === "#" && chars[h - 3] === "&") return h - 3;
  }
  return null;
}

/** 刀一：`<[^>]*$` 在**码点数组**上的最左起点（没有则 null）。
 *  刻意不走正则：正则给的是 UTF-16 下标，而 `out` 是码点数组 —— emoji 正文里
 *  两种下标会差出几千，拿正则的 index 当数组下标直接就是越界读。
 *  等价规则：匹配必须一路走到串尾且中间没有 `>` ⇒ 起点就是"最后一个 `>` 之后的第一个 `<`"。
 *  Python 的 `$` 允许停在结尾换行之前，但那一格在退空白之后拿不到（换行本身是空白），两侧同判。 */
function _danglingTagStart(arr) {
  let lastClose = -1;
  for (let i = arr.length - 1; i >= 0; i--) { if (arr[i] === ">") { lastClose = i; break; } }
  for (let i = lastClose + 1; i < arr.length; i++) { if (arr[i] === "<") return i; }
  return null;
}

/** 按**码点**截断；只有真的截断了才抹尾部的半个标签/半个实体（未截断者逐字节原样返回）。 */
function capBody(text, limit) {
  const n = (limit === undefined || limit === null) ? BODY_CAP : limit;
  if (!text) return "";
  const chars = Array.from(String(text));
  if (chars.length <= n) return text;
  const out = chars.slice(0, n);
  const mTag = _danglingTagStart(out);
  let cut = mTag === null ? out.length : mTag;
  let firstEntityKnife = true;
  while (cut > 0) {
    // 定位前先把切点上的尾部空白退掉：两条 `$` 锚定的正则在"空白结尾"上永远匹配不到，
    // 不退就会让出口的 trim 把残缺实体重新暴露成尾巴。口径与出口 trim 同一张表。
    let tail = cut;
    while (tail > 0 && _isWs(out[tail - 1])) tail--;
    let next = null;
    if (firstEntityKnife) {
      const wStart = Math.max(0, tail - ENTITY_TAIL_WINDOW);
      const window = out.slice(wStart, tail).join("");
      const m = DANGLING_ENTITY_RE.exec(window);
      firstEntityKnife = false; // 名段那一刀一生只许一次，剥没剥到都不回来
      if (m !== null) next = tail - (window.length - m.index);
    }
    if (next === null) {
      const u = _unambiguousEntityStart(out, tail);
      if (u === null) break;
      next = u;
    }
    cut = next;
  }
  // 刀二停稳后唯一还在退空白的地方，不许省
  let end = cut;
  while (end > 0 && _isWs(out[end - 1])) end--;
  return out.slice(0, end).join("");
}

// ---------------------------------------------------------------- 挑战页 / 付费墙
// 挑战页特征：WAF/CDN 的人机验证壳。宁可漏判（当成抓不到）也不要误判真页。
const CHALLENGE_MARKERS = [
  "aliyun_waf", "waf_capture", "wafrisk", "_waf_", "acw_sc__v2", "acw_tc",
  "cf-browser-verification", "just a moment", "attention required",
  "checking your browser", "verifyyouarehuman", "请启用 javascript"
];
const META_REFRESH_RE = new RegExp("<meta[^>]+http-equiv" + WS_STAR + "=" + WS_STAR + '["\']?refresh', "iu");
const SCRIPT_BLOCK_RE = new RegExp("<script[^>]*>[\\u0000-\\u{10FFFF}]*?<\\/script>", "giu");
const STYLE_BLOCK_RE = new RegExp("<style[^>]*>[\\u0000-\\u{10FFFF}]*?<\\/style>", "giu");
const TAG_ANY_RE = new RegExp("<[^>]+>", "gu");
const WS_RUN_RE = new RegExp(WS_CLASS + "+", "gu");

function _visibleLen(html) {
  const s = String(html).replace(SCRIPT_BLOCK_RE, "").replace(STYLE_BLOCK_RE, "")
    .replace(TAG_ANY_RE, " ");
  return _strip(s.replace(WS_RUN_RE, " ")).length;
}

function isChallengePage(html) {
  const s = String(html || "");
  if (!s) return false;
  const low = s.toLowerCase();
  for (let i = 0; i < CHALLENGE_MARKERS.length; i++) if (low.indexOf(CHALLENGE_MARKERS[i]) >= 0) return true;
  return META_REFRESH_RE.test(low) && _visibleLen(s) < 200;
}

// 付费墙特征：只用于"标注正文可能不完整"，不用于丢弃内容。
const PAYWALL_MARKERS = [
  "付费阅读", "订阅后可读", "登录后继续", "会员可见", "购买后阅读",
  "试读", "subscribe to read", "members only", "member-only", "premium content",
  "create a free account", "sign up to read", "paywall"
];
const SHORT_BODY_CHARS = 1200;

function classifyBody(textContentLen, html) {
  const n = Number(textContentLen) || 0;
  if (n <= 0) return null;
  const low = String(html || "").toLowerCase();
  for (let i = 0; i < PAYWALL_MARKERS.length; i++) if (low.indexOf(PAYWALL_MARKERS[i]) >= 0) return "paywall";
  return n < SHORT_BODY_CHARS ? "short" : null;
}

// ── 三把"摘要/正文上的刀"的运行时端口（R43）────────────────────────
// 参照物（唯一）：build_rss_aggregator.py 的 `_rewrite_hn_summary` /
//   `_strip_glued_url` / `_delink_nav_links`。这三把刀本来只接在**构建期**
//   （`_fetch_rss` / `_parse_rss_item` / 快照 `s` 出口），实时通道 `?source=`/`?batch=`
//   自己抓 RSS、用自己的 sanitizeHtml/deepCleanHtml，于是线上走实时出口的用户
//   仍会看到 HN 那四行模板与 NodeSeek 的粘连 URL。这里补 JS 端口并接进 `api/rss.js`。
//
// 三条刻意的口径（与 tests/site_nav/test_body_rules_parity.py 的对账判据同一份）：
//   1) 空白一律用上面那张显式码位表（`WS_STAR` / `NOT_WS_STAR`），**不用** JS 的 `\s`
//      —— 实测 JS `\s` 少认 1C-1F 与 85、多认 FEFF，与 Python 不等价。
//   2) 数字一律 ASCII `[0-9]`，不用 `\d` 的 Unicode 语义（Python 侧那两条也一起写成
//      `[0-9]`，全角数字两侧同判"不是模板行"）。
//   3) `<a\b` 的词边界用 `NOT_WORD`（`\p{L}\p{N}_` 的反向断言）而不是 JS 的 ASCII `\b`：
//      后者会在 `<a漢字=…>` 上凭空认为是边界，Python 不认。
//
// 全角冒号 `：`(U+FF1A) 一律写转义、不贴字形：它与半角 ':' 在编辑器里看不出区别，
// 构建期就因为它被写成两个半角冒号而让中文版模板永不命中（Task 8 的实测教训）。
const NOT_WS_STAR = "[^" + WS_CLASS.slice(1, -1) + "]";
const COLON_CLASS = "[:\\uFF1A]";
// ── R45：词表与 Python 的 `_HN_*_WORDS` 逐字同序（`test_word_lists_are_the_same_ordered_list`
// 把这两份表按序对死，少一个词、多一个词、换了序都红）。
// 词来自 rss_history.json 的真语料清点，两条收词依据都在判据里：
//   ① 每个词都在 HN 条目的模板槽位上真实出现；② 每个词在非 HN 条目上命中数为 0。
// 实测最大缺口是 `# 评论: 0`（半角冒号 + 词是"评论"不是"评论数"，18,037 条里 551 行）——
// 构建期出口与实时 `api/rss.js` 出口同吃这个缺口，所以两边必须一起补。
const HN_ART_WORDS = ["article url", "\u6587\u7ae0\u7f51\u5740", "\u6587\u7ae0\u94fe\u63a5",
                      "\u6587\u7ae0URL", "\u6587\u7ae0\u5730\u5740"];
const HN_CMT_WORDS = ["comments url", "\u8bc4\u8bba\u7f51\u5740", "\u8bc4\u8bba\u533a\u94fe\u63a5",
                      "\u8bc4\u8bba\u533a\u7f51\u5740", "\u8bc4\u8bba\u94fe\u63a5", "\u8bc4\u8bbaURL",
                      "\u8bc4\u8bba\u5730\u5740", "\u8bc4\u8bba\u9875\u9762", "\u8ba8\u8bba\u533a\u94fe\u63a5",
                      "\u8ba8\u8bba\u94fe\u63a5"];
const HN_PTS_WORDS = ["points", "\u79ef\u5206", "\u5f53\u524d\u5f97\u5206", "\u5f97\u5206",
                      "\u8bc4\u5206", "\u70b9\u8d5e\u6570", "\u70ed\u5ea6\u503c", "\u70ed\u5ea6",
                      "\u5206\u6570", "\u70b9\u6570", "\u5206\u503c", "\u5173\u6ce8\u5ea6"];
const HN_CMTS_WORDS = ["comments", "\u8bc4\u8bba\u6570\u91cf", "\u8bc4\u8bba\u6570", "\u8bc4\u8bba"];

/** 词表 → 交替式。与 Python 的 `_hn_alt` 同形：每个词先过元字符转义，
 *  免得将来混进 `.`/`+`/`(` 时把整行锚定静默降级成 search。 */
function _hnAlt(words) {
  return words.map(function (w) {
    return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }).join("|");
}
const HN_ART_URL_RE = new RegExp(
  "^(?:" + _hnAlt(HN_ART_WORDS) + ")" + WS_STAR + COLON_CLASS + WS_STAR +
  "(" + NOT_WS_STAR + "+)" + WS_STAR + "$", "iu");
const HN_CMT_URL_RE = new RegExp(
  "^(?:" + _hnAlt(HN_CMT_WORDS) + ")" + WS_STAR + COLON_CLASS + WS_STAR +
  "(" + NOT_WS_STAR + "+)" + WS_STAR + "$", "iu");
const HN_POINTS_RE = new RegExp(
  "^(?:" + _hnAlt(HN_PTS_WORDS) + ")" + WS_STAR + COLON_CLASS + WS_STAR +
  "([0-9]+)" + WS_STAR + "$", "iu");
const HN_COMMENTS_RE = new RegExp(
  "^#?" + WS_STAR + "(?:" + _hnAlt(HN_CMTS_WORDS) + ")" + WS_STAR + COLON_CLASS + WS_STAR +
  "([0-9]+)" + WS_STAR + "$", "iu");
const HN_SCORE_SUFFIX = " \u5206";            // "254 分"
const HN_COMMENTS_SUFFIX = " \u8bc4\u8bba";    // "162 评论"
const HN_META_JOIN = " \u00B7 ";              // "254 分 · 162 评论 · Hacker News"
// R47：标签被上游翻译层整个丢掉时，`Article URL` / `Comments URL` 那两行是以**正文行**
// 的身份进来的（实测形状 `https://…\nhttps://…\n积分：2\n# 评论：0`）：四槽刀照旧开火
// （积分 + 评论两行就够门槛），两行裸 URL 却留在摘要里 —— 用户报的"HN 里全是链接"就是
// 这一格没清干净。**只在已判定为 HN 形状的那条分支里**用得上这把刀（见 rewriteHnSummary
// 里的门槛）；通用摘要一条都不许剥，很多源的正常摘要就是单独一行链接。
// 值段限 **ASCII 可打印** `[\x21-\x7e]`，与 Python 侧同一条正则、也与 `GLUED_URL_RE` 同
// 一个口径：这里写 `\S`（或反向空白表 `NOT_WS_STAR`）会把 `https://ex.com/a看了正文` 这种
// **URL 与正文粘连**的行也算成整行裸 URL 一口吃掉 —— 派工初版踩过这一格，是
// test_order_matters_when_meta_lines_come_before_the_glued_line 当场抓成回归的（两侧同判）。
const HN_BARE_URL_LINE = new RegExp("^https?://[\\x21-\\x7e]+$", "iu");

/** hnrss 模板元数据压成一行：正文一个字不动、裸链接一条不留。
 *  门槛与 Python 同形 —— 至少命中两行、且必须含 Points 或 Comments URL（单行 "Points: 3"
 *  不足以判定，否则任何英文评分句都会被当成模板吃掉）。幂等：结果里没有模板行了。
 *  R47：门槛之后的第二件事 —— 剥掉"整行就是裸 URL"的正文行（标签被翻译层丢掉时，
 *  Article/Comments URL 那两行正是以正文身份进来的）。**门槛没过者一个字都不动**，
 *  所以通用摘要里那一行链接原样保留。
 *  `link` 不参与判决（与 Python 侧同形，留形参只为两条链路的写法一致）。 */
function rewriteHnSummary(desc, link) {
  if (!desc) return desc;
  let art = "", cmt = "", pts = "", cmts = "";
  const keep = [];
  const lines = String(desc).split("\r\n").join("\n").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const ln = _strip(lines[i]);
    if (!ln) {
      if (keep.length) keep.push(ln);
      continue;
    }
    let m = HN_ART_URL_RE.exec(ln);
    if (m) { art = m[1]; continue; }
    m = HN_CMT_URL_RE.exec(ln);
    if (m) { cmt = m[1]; continue; }
    m = HN_POINTS_RE.exec(ln);
    if (m) { pts = m[1]; continue; }
    m = HN_COMMENTS_RE.exec(ln);
    if (m) { cmts = m[1]; continue; }
    keep.push(ln);
  }
  while (keep.length && !keep[keep.length - 1]) keep.pop();
  const hits = (art ? 1 : 0) + (cmt ? 1 : 0) + (pts ? 1 : 0) + (cmts ? 1 : 0);
  if (hits < 2) return desc;
  if (!pts && !cmt) return desc;
  // R47：门槛已过 ⇒ 这一条**已判定为 HN 形状**，此时才剥"整行就是裸 URL"的正文行。
  // 放在门槛之后是本刀唯一的正确位置：门槛之前动手就是通用剥离，会把"整条摘要就是一行
  // 链接"那种正常源咬掉（反例判据 test_bare_url_line_from_an_ordinary_summary_is_kept）。
  // 剥完再退一次尾部空行（`正文\n\nhttps://x` 这类剥完留的空洞），与 Python 侧同形且保幂等。
  const kept = [];
  for (let i = 0; i < keep.length; i++) {
    if (!HN_BARE_URL_LINE.test(keep[i])) kept.push(keep[i]);
  }
  while (kept.length && !kept[kept.length - 1]) kept.pop();
  const meta = [];
  if (pts) meta.push(pts + HN_SCORE_SUFFIX);
  if (cmts) meta.push(cmts + HN_COMMENTS_SUFFIX);
  meta.push("Hacker News");
  const metaLine = meta.join(HN_META_JOIN);
  return kept.length ? kept.join("\n") + "\n" + metaLine : metaLine;
}

// 粘连裸链接（NodeSeek 类实测形状）：`[\x21-\x7e]` 而不是 `\S` —— `\S` 会把紧跟在 URL
// 后面的全角逗号/句号也当 URL 字符吃掉；先行断言是**唯一**动手条件；**不加 i**
// （Python 侧也没加，`HTTPS://…看了` 两侧都不剥）。
const GLUED_URL_RE = new RegExp(
  "^" + WS_STAR + "https?://[\\x21-\\x7e]+?(?=[\\u4e00-\\u9fff\\u3040-\\u30ff])", "u");

/** 剥掉摘要开头与正文粘连的裸链接；幂等（剥完第一个字符必是汉字，再过一次返回原值）。 */
function stripGluedUrl(text) {
  if (!text) return text;
  const s = String(text);
  const m = GLUED_URL_RE.exec(s);
  if (!m) return text;
  return s.slice(m[0].length);
}

// 站内导航词表：与 Python 的 `_NAV_LINK_SEGMENTS` **同序同值**（对账判据直接比清单）。
// 单复数并列不是冗余：`categories?` 那种写法匹不到单数 `category`（-y→-ies 不是加 s）。
const NAV_LINK_SEGMENTS = ["tag", "tags", "category", "categories", "author", "authors",
  "about", "subscribe", "donate", "archive"];
// `<a\b` → `<a` + NOT_WORD；"任意字符（含换行）"写全码位区间而不是 `[\s\S]`：
// 本文件禁止在代码里出现 `\s`（判据 test_js_lib_has_zero_requires_and_no_new_url），
// 而 `[\u0000-\u{10FFFF}]` 与 Python 的 `[\s\S]` 在"任意码点"这一件事上逐字等价。
const NAV_ANCHOR_RE = new RegExp(
  "<a" + NOT_WORD + "[^>]*?href=\"(?<href>[^\"]*)\"[^>]*>(?<text>[\\u0000-\\u{10FFFF}]*?)<\\/a>",
  "giu");
const NAV_SCHEME_HOST_RE = new RegExp("^(?:[a-z][a-z0-9+.-]*:)?//[^/]*", "iu");

function _delinkNavLink(m, href, text) {
  const path = href.replace(NAV_SCHEME_HOST_RE, "");
  if (path.charAt(0) !== "/") return m;   // 纯相对（"tag/x"）、mailto:、空值：一律不动，宁可少剥
  // 只看 host 之后的**第一个**路径段：`https://about.fb.com/news/…`（about 在 host 上）、
  // `…/archive/html/…`（导航词在中段）都不是站内导航 ⇒ 外部正文链接一条不许被动。
  const seg = path.slice(1).split(/[/?#]/)[0].toLowerCase();
  return NAV_LINK_SEGMENTS.indexOf(seg) >= 0 ? text : m;
}

/** 站内导航链接去 href 留文字（构建期 `_delink_nav_links` 的端口，同一批反例判据对账）。 */
function delinkNavLinks(html) {
  if (html === null || html === undefined) return "";
  return String(html).replace(NAV_ANCHOR_RE, _delinkNavLink);
}

// ── 构建期规则 2.6 的端口（审查 ④）────────────────────────────────
// 空锚点 / 站点根链接（"阅读更多""返回首页"那类壳）**整枚**去掉，连文字一起。
// 这条刀原本只长在 `build_rss_aggregator.py` 的 `_deep_clean_html` 里，实时出口没有 ⇒
// 同一份 `content:encoded` 两侧产物不同：Python 出厂 `<p>正文一</p><p>阅读更多</p>…`
// 早已把壳清掉，JS 的 `fc` 里还留着 `<p><a href="#"></a>阅读更多</p>`。
//
// 三条口径逐字照抄构建期那条 `re.sub`：
//   1) 只吃 `href="#"` 与 `href="/"` 两种**精确**形状。放宽成 `href="[^"]*"` 会把正文里的
//      "见下节"（`#sec-2`）连同文字一起吃掉 —— Python 侧的同名反例判据
//      tests/rss_history/test_body_noise_strip.py::test_in_page_fragment_link_is_not_dropped。
//   2) `[^>]*` 保持**贪婪**（构建期也是贪婪）：一枚 `<a>` 上写两个 href 时锚在**最后一个**，
//      改 lazy 就锚在第一个 ⇒ `<a href="/x" href="#">` 两侧一个删一个不删，正是分叉。
//   3) 大小写两可（构建期带 `re.IGNORECASE`，这里带 `i`）：sanitize 会把属性名原样重铸，
//      `<A HREF="#">` 这种形状上游真给。
// 位置也照构建期：2.5（delinkNavLinks）之后、规则 3（收尾空块）之前 —— 被这两把刀删空的
// `<p><a href="#">…</a></p>` 壳要由规则 3 收尾，挪到最后就留一堆空 `<p>`。
const EMPTY_ANCHOR_RE = new RegExp(
  "<a" + NOT_WORD + "[^>]*href=\"(?:#|/)\"[^>]*>[\\u0000-\\u{10FFFF}]*?<\\/a>", "giu");

/** 整枚删除空锚点/根链接（构建期 `_deep_clean_html` 规则 2.6 的端口）。 */
function dropEmptyAnchorLinks(html) {
  if (html === null || html === undefined) return "";
  return String(html).replace(EMPTY_ANCHOR_RE, "");
}

// ── 导出面治理 ──
// 分两类，两类都必须在报告里能指到调用方（判据：
// tests/site_nav/test_article_contract.py::test_body_rules_exports_all_have_callers）：
//   A. 运行时真调用方：api/rss.js（normalizeBodyHtml / capBody / deepCleanHtml 里的
//      delinkNavLinks+dropEmptyAnchorLinks / parseFeed 两处的 rewriteHnSummary+stripGluedUrl）、
//      api/article.js（normalizeBodyHtml/capBody/isChallengePage/classifyBody）。
//   B. **仅为 Python↔JS 对账暴露**（isPlaceholderSrc / parseImgAttrs / isWordChar 三个原语 +
//      LAZY_SRC_ATTRS / BODY_CAP / PLACEHOLDER_SRC_NAMES / ENTITY_TAIL_WINDOW /
//      WHITESPACE_CODE_POINTS / WS_CLASS / CHALLENGE_MARKERS / PAYWALL_MARKERS /
//      SHORT_BODY_CHARS / NAV_LINK_SEGMENTS /
//      HN_ART_WORDS / HN_CMT_WORDS / HN_PTS_WORDS / HN_CMTS_WORDS 十四张表）：
//      它们没有 api/ 里的直接调用方，
//      但**函数本体活在运行时链路上**（normalizeBodyHtml / capBody / isChallengePage /
//      classifyBody / delinkNavLinks 内部就在调），导出只被
//      tests/site_nav/test_body_rules_parity.py 与 tests/rss_js/test_body_rules.js 读，
//      用来钉"两侧是同一份表"（改表必须同时改两侧，否则对账当场红）。
//      这不是"为测试另写一套实现"：被测的仍然是出厂那条路径。
// 谁都不用的导出已经删掉（`urlJoin` / `urlParse` / `strip` 三个：绝对化与属性值 strip 的
// 行为已经由 normalizeBodyHtml 的语料覆盖，再导出一次就是第二条可分叉的路径）。
// 删导出**不影响内部调用**：三个函数体仍在本文件里被 normalizeBodyHtml/capBody 使用。
module.exports = {
  // A. 运行时出口在用
  normalizeBodyHtml: normalizeBodyHtml,
  decodeEntities: decodeEntities,
  capBody: capBody,
  isChallengePage: isChallengePage,
  classifyBody: classifyBody,
  rewriteHnSummary: rewriteHnSummary,
  stripGluedUrl: stripGluedUrl,
  delinkNavLinks: delinkNavLinks,
  dropEmptyAnchorLinks: dropEmptyAnchorLinks,
  // B. 仅为 Python↔JS 对账暴露（行为同时确实在运行时链路上被调用）
  isPlaceholderSrc: isPlaceholderSrc,
  parseImgAttrs: parseImgAttrs,
  isWordChar: _isWordChar,
  LAZY_SRC_ATTRS: LAZY_SRC_ATTRS,
  BODY_CAP: BODY_CAP,
  PLACEHOLDER_SRC_NAMES: PLACEHOLDER_SRC_NAMES,
  ENTITY_TAIL_WINDOW: ENTITY_TAIL_WINDOW,
  WHITESPACE_CODE_POINTS: WHITESPACE_CODE_POINTS,
  WS_CLASS: WS_CLASS,
  CHALLENGE_MARKERS: CHALLENGE_MARKERS,
  PAYWALL_MARKERS: PAYWALL_MARKERS,
  SHORT_BODY_CHARS: SHORT_BODY_CHARS,
  NAV_LINK_SEGMENTS: NAV_LINK_SEGMENTS,
  // R45：HN 模板四槽位的词表（与 Python 的 `_HN_*_WORDS` 同值同序，单边改词必红）
  HN_ART_WORDS: HN_ART_WORDS,
  HN_CMT_WORDS: HN_CMT_WORDS,
  HN_PTS_WORDS: HN_PTS_WORDS,
  HN_CMTS_WORDS: HN_CMTS_WORDS
};
