  var btn=document.getElementById('btnTheme');
  function syncIcons(){
    var d=document.documentElement.dataset.theme==='dark';
    var sun=document.querySelector('.icon-sun'),moon=document.querySelector('.icon-moon');
    if(sun)sun.style.display=d?'none':'block';
    if(moon)moon.style.display=d?'block':'none';
  }
  if(btn) btn.onclick=function(){
    var t=document.documentElement.dataset.theme==='dark'?'light':'dark';
    try{localStorage.setItem('wb_starhub_theme_v1',t);}catch(e){}
    document.documentElement.dataset.theme=t;
    syncIcons();
  };
  syncIcons();
