/* Query syntax, relevance and fuzzy helpers. */
// ---- 搜索语法（限定符 + 普通词）与相关性打分 ----
// 设计：所有筛选语法化（stars:>1k / language:python / license:mit / pushed:>30d /
// forks:>100 / org:acme / topic:rag / in:name / in:desc），卡片上的 owner/topic
// 标签点击即写入对应限定符 —— 一个搜索框承载全部筛选，功能之间零耦合。
export function _unitNum(s){
  const m = String(s).toLowerCase().match(/^(\d+(?:\.\d+)?)([km]?)$/);
  if(!m) return NaN;
  return Math.round(parseFloat(m[1]) * (m[2]==='k'?1e3:m[2]==='m'?1e6:1));
}
export function parseQueryTokens(q){
  const filters = {starsMin:null, starsMax:null, forksMin:null, lang:'', license:'', pushedDays:null, org:'', topic:'', inName:false, inDesc:false};
  const words = [];
  // 引号短语作为一个整体参与匹配，避免 "machine learning" 被拆成两个词后失去短语语义
  const rawTokens = String(q||'').match(/"[^"]*"|\S+/g) || [];
  rawTokens.forEach(raw=>{
    if(/^".*"$/.test(raw)){
      const phrase = raw.slice(1,-1).trim();
      if(phrase) words.push(phrase.toLowerCase());
      return;
    }
    const t = raw;
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

function escapeRegex(s){ return String(s).replace(/[.*+?^${}()|[\]\\]/g,'\\$&'); }

// 仓库名/topic 里的 - _ . / 与用户输入的空格等价：machine-learning 应能被
// "machine learning" 命中，node.js 也应能被 "node js" 命中。
export function normText(s){ return String(s||'').toLowerCase().replace(/[-_./]+/g,' ').trim(); }

export function relevanceScore(d, words){
  if(!words.length) return 0;
  const name=normText(d.name);
  const owner=normText(d.owner);
  const full=normText(d.full_name);
  const desc=normText(d.desc);
  const topics=(d.topics||[]).map(normText);
  const lang=(d.language||'').toLowerCase();
  const cat=normText(d.categoryLabel);
  let s=0, allName=true, allTopic=true, allDesc=true;
  words.forEach(w=>{
    let sc=0;
    if(name===w || full===w) sc=40;
    else if(name.startsWith(w)) sc=28;
    else if(new RegExp('(^|[^a-z0-9])'+escapeRegex(w)).test(name)) sc=20;
    else if(name.includes(w)) sc=12;
    else if(owner===w) sc=16;
    else if(full.startsWith(w+' ')) sc=10;
    else if(full.includes(w)) sc=8;
    if(topics.includes(w)) sc=Math.max(sc,12);
    else if(topics.some(t=>t.includes(w))) sc=Math.max(sc,6);
    if(lang===w) sc=Math.max(sc,4);
    if(cat.includes(w)) sc=Math.max(sc,3);
    if(desc.includes(w)) sc=Math.max(sc,2);
    s+=sc;
    if(!(name.includes(w)||full.includes(w))) allName=false;
    if(!topics.some(t=>t.includes(w))) allTopic=false;
    if(!desc.includes(w)) allDesc=false;
  });
  if(words.length>1 && allName) s+=8;
  if(words.length>1 && allTopic) s+=5;
  if(words.length>1 && allDesc) s+=3;
  const phrase=words.join(' ');
  if(phrase && name===phrase) s+=30;
  else if(phrase && name.startsWith(phrase)) s+=15;
  else if(phrase && name.includes(phrase)) s+=8;
  return s;
}
