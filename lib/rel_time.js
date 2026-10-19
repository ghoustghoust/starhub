// lib/rel_time.js
// 相对时间标签的唯一口径。它必须与被删掉的构建期实现逐字相同（那是 a.time 兜底位、
// 书签快照与分享文案在用的值），
// 由 tests/rss_history/test_time_label_parity.py 钉住。
//
// 为什么不用 new Date(iso) 直接算：
//   1) 数据里 pub_date 混着 "+08:00" / "Z" / 裸值三种写法，而 Python 侧把**裸值按北京时间**解释
//      （原构建期实现把 naive 直接当北京时间用）。JS 的 new Date("2026-09-30T10:00:00")
//      按访客本地时区解释，访客不在东八区就会算出另一个时刻——这正是本仓反复栽过的"时区盲"。
//   2) 输出的 %m-%d %H:%M / %Y-%m-%d 必须是北京时间的日历值，不能随访客时区漂。
// 所以这里自己解析偏移、自己按 +08:00 渲染，不依赖 Date 的本地语义。
(function (global) {
  "use strict";

  var BJ_OFFSET_S = 8 * 3600;
  // 时间部分可选、秒可选：Python 的 fromisoformat 认 "2026-09-20" 和 "…T09:00"，
  // 少认一种就是页面比构建期少算一条，实测纯日期写法确实存在于上游 feed 里。
  var ISO_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?)?$/;

  function pad(n, w) {
    var s = String(n);
    while (s.length < w) s = "0" + s;
    return s;
  }

  /** ISO 串 → epoch 秒。裸值（无偏移）按北京时间解释，与 Python 侧一致。解析失败返回 null。 */
  function isoToEpochSec(s) {
    if (!s) return null;
    var m = ISO_RE.exec(String(s).trim());
    if (!m) return null;
    var off = m[8];
    var offSec;
    if (!off) {
      offSec = BJ_OFFSET_S;                      // 裸值 = 北京时间（与改造前实现一致）
    } else if (off === "Z" || off === "z") {
      offSec = 0;
    } else {
      var sign = off[0] === "-" ? -1 : 1;
      var body = off.slice(1).replace(":", "");
      offSec = sign * (parseInt(body.slice(0, 2), 10) * 3600 + parseInt(body.slice(2, 4), 10) * 60);
    }
    var frac = m[7] ? parseInt((m[7] + "000").slice(0, 3), 10) : 0;
    var utcMs = Date.UTC(+m[1], +m[2] - 1, +m[3],
                         m[4] ? +m[4] : 0, m[5] ? +m[5] : 0, m[6] ? +m[6] : 0, frac)
                - offSec * 1000;
    return utcMs / 1000;
  }

  /** epoch 秒 → 北京年的 {y,mo,d,h,mi} */
  function bjParts(epochSec) {
    var d = new Date((epochSec + BJ_OFFSET_S) * 1000);
    return {
      y: d.getUTCFullYear(), mo: d.getUTCMonth() + 1, d: d.getUTCDate(),
      h: d.getUTCHours(), mi: d.getUTCMinutes()
    };
  }

  /**
   * 与黄金表 goldens/rel_time_labels.json 等价的标签（那份快照取自改造前的实现）。
   * @param {string} iso      条目的 pub_date（三种写法混用都接受）
   * @param {number} nowEpoch 基准时刻的 epoch 秒。传 BUILD_TS/1000 即"以构建时刻为基准"，
   *                          与改造前烘进产物的 time_str 逐字一致；不传则用访客时钟。
   */
  function fmtRelTime(iso, nowEpoch) {
    var epoch = isoToEpochSec(iso);
    if (epoch === null) return "";
    var now = (typeof nowEpoch === "number" && isFinite(nowEpoch)) ? nowEpoch : Math.floor(Date.now() / 1000);
    var p = bjParts(epoch);
    var diff = now - epoch;                       // Python: now(北京时间 naive) - bj_naive
    if (diff < 0) return pad(p.mo, 2) + "-" + pad(p.d, 2) + " " + pad(p.h, 2) + ":" + pad(p.mi, 2);
    var seconds = Math.floor(diff);               // Python int(total_seconds()) 对正数即 floor
    if (seconds < 60) return seconds + "秒前";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + "分钟前";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + "小时前";
    var days = Math.floor(hours / 24);
    if (days < 30) return days + "天前";
    return p.y + "-" + pad(p.mo, 2) + "-" + pad(p.d, 2);
  }

  var api = { fmtRelTime: fmtRelTime, isoToEpochSec: isoToEpochSec, _bjParts: bjParts };
  if (typeof module !== "undefined" && module.exports) module.exports = api;   // 供 node 测试
  global.STARHUB_REL_TIME = api;                                                // 供页面
})(typeof window !== "undefined" ? window : globalThis);
