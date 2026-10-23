/* Query syntax, relevance and fuzzy helpers. */
// ---- 搜索语法（限定符 + 普通词）与相关性打分 ----
// 设计：所有筛选语法化（stars:>1k / language:python / license:mit / pushed:>30d /
// forks:>100 / org:acme / topic:rag / in:name / in:desc），卡片上的 owner/topic
// 标签点击即写入对应限定符 —— 一个搜索框承载全部筛选，功能之间零耦合。
function _unitNum(s){
  const m = String(s).toLowerCase().match(/^(\d+(?:\.\d+)?)([km]?)$/);
  if(!m) return NaN;
  return Math.round(parseFloat(m[1]) * (m[2]==='k'?1e3:m[2]==='m'?1e6:1));
}
function parseQueryTokens(q){
  const filters = {starsMin:null, starsMax:null, forksMin:null, lang:'', license:'', pushedDays:null, org:'', topic:'', inName:false, inDesc:false};
  const words = [];
  String(q||'').trim().split(/\s+/).filter(Boolean).forEach(t=>{
    let m;
    if((m=t.match(/^stars?:(>=?)(\d+(?:\.\d+)?[km]?)$/i))) filters.starsMin=_unitNum(m[2]);
    else if((m=t.match(/^stars?:<(\d+(?:\.\d+)?[km]?)$/i))) filters.starsMax=_unitNum(m[1]);
    else if((m=t.match(/^forks?:(>=?)(\d+(?:\.\d+)?[km]?)$/i))) filters.forksMin=_unitNum(m[2]);
    else if((m=t.match(/^language:(\S+)$/i))) filters.lang=m[1];
    else if((m=t.match(/^license:(\S+)$/i))) filters.license=m[1];
    else if((m=t.match(/^pushed:>(\d+)d?$/i))) filters.pushedDays=parseInt(m[1],10);
    else if((m=t.match(/^org:(\S+)$/i))) filters.org=m[1];
    else if((m=t.match(/^topic:(\S+)$/i))) filters.topic=m[1];
    else if(/^in:name$/i.test(t)) filters.inName=true;
    else if(/^in:desc$/i.test(t)) filters.inDesc=true;
    else words.push(t.toLowerCase());
  });
  return {filters, words};
}
function relevanceScore(d, words){
  if(!words.length) return 0;
  const name=(d.name||'').toLowerCase(), full=(d.full_name||'').toLowerCase();
  const topics=(d.topics||[]).join(' ').toLowerCase(), desc=(d.desc||'').toLowerCase();
  let s=0;
  words.forEach(w=>{
    if(name.includes(w)||full.includes(w)) s+=10;
    else if(topics.includes(w)) s+=4;
    else if(desc.includes(w)) s+=2;
  });
  if(words.length>1 && words.every(w=>name.includes(w)||full.includes(w))) s+=5;
  return s;
}
