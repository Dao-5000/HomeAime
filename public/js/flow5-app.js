/* ============================================================
   Homeaime · flow5 接入层（加载于 flow-app.js 之后）
   只做视觉增强：渐变注入 / 深色默认 / 光粒 / 状态条 / 全出血
   不碰任何功能代码。回滚＝删掉本文件与 index.html 里的两行引用。
   ============================================================ */
(function(){
  /* 深色为默认（原版默认浅色，这里反转；右上角月亮仍可切换） */
  document.body.classList.add('dark');
  /* ?instant=1 → 入场动画瞬间完成 */
  if(new URLSearchParams(location.search).has('instant')){
    var is=document.createElement('style');
    is.textContent='.st{animation-duration:.01s!important;animation-delay:0s!important}';
    document.head.appendChild(is);
  }

  /* 渐变 defs（环/进度条描边引用） */
  var svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('width','0'); svg.setAttribute('height','0');
  svg.style.position='absolute';
  svg.innerHTML='<defs><linearGradient id="gWarm" x1="0" y1="0" x2="1" y2="1">'+
    '<stop offset="0" stop-color="#FF8FA8"/><stop offset="1" stop-color="#FFC39B"/></linearGradient></defs>';
  document.body.appendChild(svg);

  /* 全出血：接管 hzFitStage（原为 1280×800 居中缩放） */
  window.hzFitStage=function(){ var st=document.getElementById('stage'); if(!st) return;
    st.style.left='0'; st.style.top='0'; st.style.transform='none';
    st.style.width='100vw'; st.style.height='100vh'; st.style.borderRadius='0'; };

  /* 漂浮光粒 */
  function petals(){ var box=document.createElement('div');
    box.style.cssText='position:fixed;inset:0;z-index:1;pointer-events:none;overflow:hidden';
    for(var i=0;i<24;i++){ var p=document.createElement('i');
      var s=(2+Math.random()*4).toFixed(1)+'px';
      p.style.cssText='position:absolute;bottom:-3vh;border-radius:50%;pointer-events:none;'+
        'width:'+s+';height:'+s+';background:linear-gradient(180deg,#FF8FA8,#FFC39B);'+
        'left:'+(Math.random()*100).toFixed(1)+'vw;opacity:0;'+
        'animation:ptl-up '+(15+Math.random()*20).toFixed(1)+'s linear infinite,'+
        'animation-delay:-'+(Math.random()*30).toFixed(1)+'s';
      p.style.animation='ptl-up '+(15+Math.random()*20).toFixed(1)+'s linear infinite';
      p.style.animationDelay=(-Math.random()*30).toFixed(1)+'s';
      p.style.setProperty('--sway',(Math.random()*70-35).toFixed(0)+'px');
      box.appendChild(p); }
    document.body.appendChild(box);
    if(!document.getElementById('ptl-keyframes')){
      var kf=document.createElement('style'); kf.id='ptl-keyframes';
      kf.textContent='@keyframes ptl-up{0%{transform:translateY(0) translateX(0);opacity:0}8%{opacity:.85}85%{opacity:.5}100%{transform:translateY(-112vh) translateX(var(--sw,24px));opacity:0}}';
      document.head.appendChild(kf); } }

  /* 底部状态条（读 Store，30s 刷新） */
  function statusbar(){ var bar=document.createElement('div'); bar.className='f5-status';
    bar.innerHTML='<span><i class="dot"></i><b id="f5sync">SYNC --</b> · DAY <b id="f5day">--</b></span>'+
      '<span class="sb-c">HOMEAIME TERMINAL · FLOW v6</span>'+
      '<span>本地优先 · 已同步 · BUILD 0913</span>';
    var stage=document.getElementById('stage'); if(stage) stage.appendChild(bar); return bar; }
  function updStatus(){ try{
    var cs=(window.Store&&Store.listContacts?Store.listContacts():[])||[];
    var sync=(window.intimacyInfo&&cs[0])?intimacyInfo(cs[0]).pct:'--';
    var days=cs[0]&&cs[0].createdAt?Math.max(1,Math.floor((Date.now()-cs[0].createdAt)/864e5)):'--';
    var s1=document.getElementById('f5sync'),d1=document.getElementById('f5day');
    if(s1) s1.textContent='SYNC '+sync; if(d1) d1.textContent='DAY '+days;
  }catch(e){} }

  /* 开机序列 */
  function boot(){
    petals(); statusbar(); updStatus(); setInterval(updStatus,30000);
    /* 流式气泡/环重渲染后保持渐变：观察 hz-view 变更不必要，CSS 已覆盖 */
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',function(){ setTimeout(boot,300); });
  else setTimeout(boot,300);
})();
