// tests/rss_js/test_rel_time.js
// 读 stdin 的用例表，用 lib/rel_time.js 算标签，把结果按 JSON 打回给 Python 比对。
// 用例由 Python 侧生成，保证两边吃的是**同一份输入**——各写各的用例就等于没比对。
"use strict";
const path = require("path");
const ROOT = path.resolve(__dirname, "..", "..");
const LIB = process.env.STARHUB_RELTIME_LIB || path.join(ROOT, "lib", "rel_time.js");
const rt = require(LIB);

let raw = "";
process.stdin.on("data", d => { raw += d; });
process.stdin.on("end", () => {
  const cases = JSON.parse(raw || "[]");
  const out = cases.map(c => ({
    id: c.id,
    js: rt.fmtRelTime(c.iso, c.now_epoch),
  }));
  process.stdout.write(JSON.stringify(out));
});
