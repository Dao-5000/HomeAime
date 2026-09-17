/* Homeaime · Flow App — 壳层与真实数据桥梁 */
'use strict';

/* ===== 工具 ===== */
function hz$(s){ return document.querySelector(s); }
function hzGreet(){ const h=new Date().getHours();
  if(h<5)return'夜深了，还在忙吗';if(h<8)return'早上好，新的一天';if(h<12)return'上午好，元气满满';
  if(h<14)return'中午好，记得吃饭';if(h<18)return'下午好';if(h<22)return'晚上好';return'夜深了，早点休息'; }
function hzIcoSvg(n,s){ const P={spark:'M12 3l1.8 5.7L19.5 10l-5.7 1.8L12 17.5l-1.8-5.7L4.5 10l5.7-1.3z',heart:'M12 21C7 16.5 3 13 3 8.8 3 6 5.2 4 7.8 4c1.7 0 3.2.9 4.2 2.3C13 4.9 14.5 4 16.2 4 18.8 4 21 6 21 8.8c0 4.2-4 7.7-9 12.2z',phone:'M6.8 10.6a13 13 0 0 0 6.6 6.6l2-2c.3-.3.7-.4 1-.3 1 .4 2.2.6 3.4.6.6 0 1 .4 1 1V19c0 .6-.4 1-1 1C10.8 20 4 13.2 4 4.2c0-.6.4-1 1-1h2.5c.6 0 1 .4 1 1 0 1.2.2 2.3.6 3.4.1.3 0 .7-.3 1z'}; return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:'+(s||11)+'px;height:'+(s||11)+'px"><path d="'+(P[n]||'')+'"/></svg>'; }
let hzToastT=null;
function hzCountUp(el,target,dur){
  if(!el) return; const t0=performance.now();
  (function f(t){ const p=Math.min(1,(t-t0)/dur), e=1-Math.pow(1-p,3);
    el.textContent=Math.round(target*e).toLocaleString(); if(p<1) requestAnimationFrame(f); })(t0);
}
function hzBurstAt(el){
  const r=el.getBoundingClientRect();
  for(let i=0;i<10;i++){ const p=document.createElement('i'); p.className='hz-burst';
    p.style.left=(r.left+r.width/2-3)+'px'; p.style.top=(r.top+8)+'px';
    const a=Math.random()*Math.PI*2, d=34+Math.random()*46;
    p.style.setProperty('--dx',Math.cos(a)*d+'px'); p.style.setProperty('--dy',(Math.sin(a)*d-24)+'px');
    document.body.appendChild(p); setTimeout(()=>p.remove(),750); }
}
function hzToast(msg){ const t=hz$('#hzToast'); t.textContent=msg; t.classList.add('show'); clearTimeout(hzToastT); hzToastT=setTimeout(()=>t.classList.remove('show'),2200); }

/* ===== 初始化 ===== */
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(hzInit, 200);
});

function hzFitStage(){
  const st=hz$('#stage'); if(!st) return;
  const s=Math.min(1, window.innerWidth/1300, window.innerHeight/820);
  st.style.transform='translate(-50%,-50%) scale('+s+')';
}
window.addEventListener('resize', hzFitStage);

function hzInit() {
  if (!window.Store) { setTimeout(hzInit, 200); return; }
  hzFitStage();
  hzClock(); setInterval(hzClock, 10000);
  hzRenderOverview();
  hzBindDock();
  hzBindChat();
  hzBindCmdk();
  hzBindTheme();
  const setBtn=hz$('#hzSetBtn'); if(setBtn) setBtn.addEventListener('click',()=>{ if(window.Settings) Settings.open(); });
  /* 开发/截图辅助：?hzview=memory 直达对应视图 */
  const pv=new URLSearchParams(location.search).get('hzview');
  if(pv&&pv!=='overview') setTimeout(()=>hzShowView(pv),500);
  if (window.bindHzTilt) window.bindHzTilt();
}

/* ===== 时钟 ===== */
function hzClock(){ const d=new Date();
  const t=hz$('#hzClock'); if(t) t.textContent=String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
  const dd=hz$('#hzDate'); if(dd) dd.textContent=dateStrOf(Date.now())+' · 周'+'日一二三四五六'[d.getDay()];
  const g=hz$('#hzGreet'); if(g&&!g.dataset.done){ g.textContent=hzGreet()+'，'; g.dataset.done='1'; }
  const nk=hz$('#hzNick'); if(nk&&!nk.dataset.done){ nk.textContent=Store.getSettings().meNickname||'朋友'; nk.dataset.done='1'; }
}

/* ===== 总览渲染 ===== */
function hzRenderOverview() {
  const contacts = Store.listContacts();
  const todos = Store.todos.list();
  const settings = Store.getSettings();
  const todayStr = dateStrOf(Date.now());
  const dueToday = todos.filter(t=>!t.done&&t.due&&t.due<=todayStr);
  const deck = hz$('#hzDeck'); const bento = hz$('#hzBento');
  if(!deck||!bento) return;

  if(!contacts.length){
    deck.innerHTML='<div class="dcard hero" style="cursor:pointer"><div class="dc-top"><div class="avatar" style="font-size:26px">＋</div><div><div class="dc-nm">还没有 AI 伴侣</div><div class="dc-st">去「她们」创建一个</div></div></div></div>';
    bento.innerHTML='';
    return;
  }

  const sorted=contacts.slice().sort((a,b)=>intimacyInfo(b).v-intimacyInfo(a).v);
  deck.innerHTML='';
  /* 主角卡 3D 堆叠：最前面一张是主角，后面堆着其他伴侣，点后面的卡切到前面 */
  const rot=window.__hzStackRot||0;
  const n=Math.min(3,sorted.length);
  const todayKey=new Date().toDateString();
  /* 今日消息按全部会话统计（与 SYNC 卡同一口径），不要只看主角那一条会话 */
  const msgsToday=Store.listConversations().reduce((s,cv)=>s+Store.getMessages(cv.cv.id).filter(m=>new Date(m.ts||0).toDateString()===todayKey).length,0);
  for(let i=0;i<n;i++){
    const c=sorted[(rot+i)%sorted.length];
    const info=intimacyInfo(c);
    const av=c.avatarUrl||c.avatar||'';
    const cv=Store.listConversations().find(x=>x.contact.id===c.id);
    const pv=cv?chatPreviewText(cv.cv,Store.getMessages(cv.cv.id)):'';
    /* 相识天数优先用后端 created_at 换算的真实跨度（window.__hzTenure），再退回本地 */
    const days=(window.__hzTenure&&window.__hzTenure[c.name])||(c.createdAt?Math.max(1,Math.floor((Date.now()-c.createdAt)/864e5)):1);
    const card=document.createElement('div');
    card.className='dcard stack s'+i;
    /* ★ 2026-09-15：亲密度数值面板已隐藏（后端 RELATIONSHIP_UI_VISIBLE=false）时，
       阶段名/进度条/亲密度数字都不渲染；相识天数与今日消息照常显示。 */
    const _relUI=(typeof window.relUIShow==='function')?window.relUIShow():false;
    let inner='<div class="dc-top">'
      +'<div class="avatar'+(i===0?' xl':'')+'">'+(av?'<img src="'+av+'" alt="">':c.name.charAt(0))+'</div>'
      +'<div class="dc-meta"><div class="dc-nm">'+c.name+(i===0?' <span class="led"></span>':'')+'</div>'
      +'<div class="dc-st">'+(i===0?'在线 · ':'')+(_relUI?info.name:'')+'</div>'
      +(_relUI?'<div class="dc-stage"><i style="width:'+info.pct+'%"></i></div>':'')
      +'</div>'+(i===0?hzWaveHTML():'')+'</div>';
    if(i===0){
      /* 近 7 天消息走势（动态 sparkline） */
      const dayCt=(off)=>{const d=new Date(Date.now()-off*864e5).toDateString();
        return Store.listConversations().reduce((n2,cv)=>n2+Store.getMessages(cv.cv.id).filter(m=>new Date(m.ts||0).toDateString()===d).length,0);};
      const spark=[6,5,4,3,2,1,0].map(dayCt);
      const smax=Math.max(1,...spark);
      const sparkSum=spark.reduce((a,b)=>a+b,0);
      const sparkBars=spark.map(v=>'<i style="--h:'+Math.max(6,Math.round(v/smax*100))+'"></i>').join('');
      inner+='<div class="dc-live"><span class="live"><i></i>在线</span>'+(_relUI?'<span class="sep">·</span><span class="muted">'+info.name+'</span>':'')+'</div>'
        +'<div class="dc-quote">「'+(pv||'开始和 '+c.name+' 聊天吧')+'」</div>'
        +'<div class="dc-stats">'
          +(_relUI?'<div class="dc-stat"><b data-count="'+info.pct+'">0</b><span>亲密度</span></div>':'')
          +'<div class="dc-stat"><b data-count="'+days+'">0</b><span>相识天数</span></div>'
          +'<div class="dc-stat"><b data-count="'+msgsToday+'">0</b><span>今日消息</span></div>'
        +'</div>'
        +'<div class="dc-spark"><div class="spark-hd"><span>近 7 天消息</span><b data-count="'+sparkSum+'">0</b></div>'
          +'<div class="bars spark">'+sparkBars+'</div></div>'
        +'<div class="dc-chips"><span onclick="event.stopPropagation();switchTab(\'memory\')">'+hzIcoSvg('spark')+'共同心愿</span><span onclick="event.stopPropagation();Profile.open(\''+c.id+'\')">'+hzIcoSvg('heart')+'在一起</span><span onclick="event.stopPropagation();VoiceCall.start()">'+hzIcoSvg('phone')+'通话</span></div>'
        +'<button class="dc-cta" onclick="event.stopPropagation();hzOpenChat(\''+c.id+'\')">开始聊天</button>'
        +'<span class="dc-orb o1"></span><span class="dc-orb o2"></span><span class="dc-orb o3"></span>';
    } else {
      inner+='<div class="dc-quote" style="color:var(--sub)">「'+(pv||'点这张卡切到 ' + c.name)+'」</div>'
        +'<div class="dc-foot"><span class="sync-n" style="color:var(--text-sub)">SYNC '+info.pct+'</span></div>';
    }
    card.innerHTML=inner;
    card.addEventListener('click',()=>{
      if(i===0) hzOpenChat(c.id);
      else { window.__hzStackRot=(rot+i)%sorted.length; hzRenderOverview(); }
    });
    deck.appendChild(card);
  }
  if(sorted.length>1){
    const dots=document.createElement('div');
    dots.className='hz-stack-dots';
    dots.style.cssText='position:absolute;left:0;right:0;bottom:-24px';
    for(let i=0;i<sorted.length;i++){ const d=document.createElement('i'); if(i===rot%sorted.length) d.className='on'; dots.appendChild(d); }
    deck.appendChild(dots);
  }
  /* 主角卡数字滚动 */
  setTimeout(()=>{
    document.querySelectorAll('#hzDeck .dcard.s0 [data-count]').forEach((el,idx)=>{
      const t=Number(el.dataset.count)||0;
      setTimeout(()=>hzCountUp(el,t,900),idx*90);
    });
  },260);

  bento.innerHTML='';
  const heroInfo=intimacyInfo(sorted[0]);
  const heroConv=Store.listConversations().find(x=>x.contact.id===sorted[0].id);
  const heroMeta={ wants: Math.max(1, Math.min(9, Math.round((heroInfo.pct||50)/14))) };

  /* SYNC */
  const syncTile=document.createElement('div');
  syncTile.className='tile dark-tile t-sync';
  const dayCnt=(off)=>{ const d=new Date(Date.now()-off*864e5).toDateString(); return Store.listConversations().reduce((n,cv)=>n+Store.getMessages(cv.cv.id).filter(m=>new Date(m.ts||0).toDateString()===d).length,0); };
  const d0=dayCnt(0), d1=dayCnt(1);
  const delta=d0-d1;
  const doneToday=todos.filter(t=>t.done&&t.due===todayStr).length;
  const bars=[6,5,4,3,2,1,0].map(off=>Math.max(8,Math.min(100,dayCnt(off)*18+10)));
  const _relUIB=(typeof window.relUIShow==='function')?window.relUIShow():false;
  syncTile.innerHTML='<div class="t-label" style="opacity:.6">SYNC · 关系温度</div><div class="t-sync" style="margin-top:2px">'
    +(_relUIB?'<div class="ring"><svg viewBox="0 0 104 104"><circle class="rt" cx="52" cy="52" r="46" fill="none" stroke-width="8"/><circle class="rv" cx="52" cy="52" r="46" fill="none" stroke-width="8" style="stroke-dashoffset:'+(289-289*heroInfo.pct/100)+'px"/></svg><div class="mid"><span class="v" id="hzSyncV">0</span><span class="k">亲密度</span></div></div>':'')
    +'<div style="flex:1;min-width:0"><div class="s-t">比昨天 '+(delta>=0?'+':'')+delta+'</div><div class="s-d">一起完成了 '+doneToday+' 件待办，聊了 '+d0+' 句。</div><div class="bars">'+bars.map(h=>'<i style="--h:'+h+'"></i>').join('')+'</div></div></div>';
  bento.appendChild(syncTile);
  if(_relUIB) setTimeout(()=>hzCountUp(syncTile.querySelector('#hzSyncV'),heroInfo.pct,1200),300);

  /* 盲盒 */
  const blind=settings.blindBox&&settings.blindBox.date===todayStr?settings.blindBox:null;
  const boxTile=document.createElement('div'); boxTile.className='tile t-box';
  const bxText=blind&&blind.opened?('「'+(blind.task||'把今天的一句话，写进明天的日记里')+'」'):'「一个随机的小任务，等你来开」';
  boxTile.innerHTML='<div class="t-label">DAILY SUPPLY · 今日盲盒</div><div class="bx" id="hzBxTx">'+bxText+'</div><button class="pill-btn">'+(blind&&blind.opened?'完成打卡 ✦':'开盒 ✦')+'</button>';
  boxTile.querySelector('.pill-btn').addEventListener('click',(e)=>{
    if(blind&&blind.opened){ Store.saveSettings({blindBox:Object.assign({},blind,{done:!blind.done})}); hzToast(blind.done?'已取消打卡':'完成打卡 ✓'); hzRenderOverview(); return; }
    if(!blind) initBlindBox();
    const ss=Store.getSettings(); Store.saveSettings({blindBox:Object.assign({},ss.blindBox,{opened:true})});
    hzBurstAt(e.target);
    const bx=boxTile.querySelector('.bx'); boxTile.classList.add('flipping');
    setTimeout(()=>{ const nb=Store.getSettings().blindBox; bx.textContent='「'+(nb.task||'把今天的一句话，写进明天的日记里')+'」'; boxTile.classList.remove('flipping'); hzToast('开盒成功'); },190);
  });
  bento.appendChild(boxTile);

  /* 今日待办 */
  const tlTile=document.createElement('div'); tlTile.className='tile t-line'; tlTile.style.gridColumn='span 2';
  tlTile.innerHTML='<div class="t-label">Today · 今日待办（'+dueToday.length+'）</div>';
  const tl=document.createElement('div'); tl.className='hz-tl';
  const dayTodos=todos.slice(0,4);
  if(dayTodos.length){ dayTodos.forEach(t=>{
    const r=document.createElement('div'); r.className='hz-tl-row'+(t.done?' done':''); r.style.cursor='pointer';
    const overdue=!t.done&&t.due&&t.due<todayStr;
    r.innerHTML='<span class="hz-tl-dot"></span><span class="hz-tl-tx">'+t.text+'</span>'+(overdue?'<span class="hz-tl-due latin">LATE 1D</span>':'')+(t.due===todayStr&&!t.done?'<span class="hz-tl-due latin">TODAY</span>':'')+'<span class="hz-tl-time latin">'+(t.due?t.due.slice(5):'')+'</span>';
    r.addEventListener('click',()=>{ Store.todos.update(t.id,{done:true}); hzRenderOverview(); });
    tl.appendChild(r);
  }); } else { tl.innerHTML='<div style="font-size:12px;color:var(--text-sub);padding:8px 0">今天没有到期待办</div>'; }
  tlTile.appendChild(tl);
  bento.appendChild(tlTile);

  /* 记忆流 */
  const allMems=contacts.flatMap(c=>(c.memStore||[]));
  const memItems=allMems.map(m=>{ const imp=Math.max(1,Math.min(5,Math.round(Number(m.importance)||3))); return ((m.title||m.content||'').slice(0,10)+' ★★★★★'.slice(0,imp)+'☆☆☆☆☆'.slice(0,5-imp)); }).filter(Boolean).slice(0,8);
  const memTile=document.createElement('div'); memTile.className='tile t-mem'; memTile.style.gridColumn='span 2';
  const mqHtml=(memItems.length?memItems:['和 TA 多聊聊 ★★☆☆☆']).map(m=>'<span class="mq-chip">'+String(m).replace(/[<>&]/g,'')+'</span>').join('').repeat(2);
  memTile.innerHTML='<div class="t-label">MEMORY STREAM · 记忆流</div><div class="mq-wrap"><div class="mq">'+mqHtml+'</div></div><div class="foot"><span class="cnt"><b id="hzMemCnt2">0</b><small>条记忆</small></span><button class="refresh" id="hzMemRefresh" title="重评重要性" aria-label="重评重要性">↻</button></div>';
  bento.appendChild(memTile);
  setTimeout(()=>hzCountUp(memTile.querySelector('#hzMemCnt2'),allMems.length,1400),450);
  const mref=memTile.querySelector('#hzMemRefresh');
  if(mref) mref.addEventListener('click',function(){ this.classList.add('spun'); setTimeout(()=>this.classList.remove('spun'),500); hzShowView('memory'); });


}

function hzWaveHTML(){ const hs=[6,12,17,9,14,6,11]; return '<div class="wave-live" style="margin-left:auto">'+hs.map((h,i)=>'<i style="height:'+h+'px;animation-delay:'+(i*.12)+'s"></i>').join('')+'</div>'; }
function hzCycleDeck(n){ const d=hz$('#hzDeck'); if(!d)return; const c=[...d.children]; if(n===1)d.appendChild(c[0]); else d.insertBefore(c[2],c[0]);
  c[0]&&c[0].classList.remove('set'); }
document.addEventListener('keydown',e=>{
  if(e.key==='ArrowLeft'){ hzCycleDeck(2); }
  else if(e.key==='ArrowRight'){ hzCycleDeck(1); }
});

/* ===== Dock ===== */
function hzBindDock(){
  document.querySelectorAll('.d-it').forEach(b=>b.addEventListener('click',()=>{
    const page=b.dataset.v; if(!page) return;
    hzShowView(page);
  }));
  document.querySelectorAll('.f-it').forEach(b=>b.addEventListener('click',()=>{
    const page=b.dataset.v; if(!page) return;
    hzShowView(page);
  }));
  const fab=hz$('#hzFab'); if(fab) fab.addEventListener('click',()=>{
    fab.classList.toggle('open'); const fan=hz$('#hzFan'); if(fan) fan.classList.toggle('open');
  });
}
function closeHzFan(){ const f=hz$('#hzFab'); const fan=hz$('#hzFan'); if(f)f.classList.remove('open'); if(fan)fan.classList.remove('open'); }

/* ===== 聊天面板：走真实 Chat.open（完整功能：语音/表情/引用/流式），旧 chatpanel 仅作兜底 ===== */
let hzChatContact=null;
function hzOpenChat(contactId){
  hzChatContact=Store.getContact(contactId); if(!hzChatContact) return;
  if(window.Chat&&Chat.open){ try{ Chat.open(contactId); return; }catch(e){ console.warn('[hz] Chat.open:',e); } }
  const cp=hz$('#chatpanel'); if(!cp) return;
  hz$('#cpAv').textContent=hzChatContact.name.charAt(0);
  hz$('#cpNm').textContent=hzChatContact.name;
  hz$('#cpSt').innerHTML='<span class="led"></span> 在线';
  cp.classList.add('open'); const sc=hz$('#scrim'); if(sc) sc.classList.add('on');
  hzLoadMessages(contactId);
}
function closeHzChat(){
  if(window.Chat&&Chat.close){ try{ Chat.close(); return; }catch(e){} }
  const cp=hz$('#chatpanel'); if(cp) cp.classList.remove('open'); const sc=hz$('#scrim'); if(sc) sc.classList.remove('on');
}
function hzBindChat(){
  const sc=hz$('#scrim'); if(sc) sc.addEventListener('click',closeHzChat);
  const inp=hz$('#cpInput'); if(inp) inp.addEventListener('keydown',e=>{ if(e.key==='Enter') hzSend(); });
}
function hzLoadMessages(contactId){
  const body=hz$('#cpBody'); if(!body) return;
  body.innerHTML='';
  const msgs=Store.getMessages(contactId);
  msgs.forEach(m=>{
    const d=document.createElement('div'); d.className='cmsg '+(m.role==='user'?'me':'ai');
    d.textContent=m.content||''; body.appendChild(d);
  });
  body.scrollTop=body.scrollHeight;
}
function hzSend(){
  const inp=hz$('#cpInput'); const v=inp.value.trim(); if(!v||!hzChatContact) return;
  pushMsg(v,'me'); inp.value='';
  const body=hz$('#cpBody');
  setTimeout(()=>{ const t=document.createElement('div'); t.className='typing'; t.innerHTML='<i></i><i></i><i></i>'; body.appendChild(t); body.scrollTop=body.scrollHeight;
    setTimeout(()=>{ t.remove(); pushMsg('嗯嗯，我都记着呢。','ai'); },1500); },600);
  if(window.Chat) Chat.send({type:'text',content:v});
}
function pushMsg(text,who){ const b=hz$('#cpBody'); if(!b) return;
  const d=document.createElement('div'); d.className='cmsg '+who; d.textContent=text; b.appendChild(d); b.scrollTop=b.scrollHeight; }

/* ===== ⌘K ===== */
function hzBindCmdk(){
  const btn=hz$('#hzCmdkBtn'); if(btn) btn.addEventListener('click',()=>hzCmdkToggle());
  document.addEventListener('keydown',e=>{
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){ e.preventDefault(); hzCmdkToggle(); }
  });
}
function hzCmdkToggle(){ hzToast('命令面板将在下一批开放'); }

/* ===== 主题 ===== */
function hzBindTheme(){
  const btn=hz$('#hzMoonBtn'); if(btn) btn.addEventListener('click',()=>{
    document.body.classList.toggle('dark'); hzToast('暗色模式将在下一批完整开放');
  });
}

/* ===== 3D tilt ===== */
function bindHzTilt(){
  document.querySelectorAll('.tilt').forEach(t=>{
    if(t.dataset.tiltBound) return; t.dataset.tiltBound='1';
    t.addEventListener('mousemove',e=>{ const r=t.getBoundingClientRect();
      const x=(e.clientX-r.left)/r.width-.5, y=(e.clientY-r.top)/r.height-.5;
      t.style.transform='perspective(700px) rotateX('+(-y*4)+'deg) rotateY('+x*5+'deg) translateY(-3px)'; });
    t.addEventListener('mouseleave',()=>{ t.style.transform=''; });
  });
}

/* ===== 视图系统：Flow 壳承载旧页面（功能全保留） ===== */
const HZ_VIEW_META={
  memory:{eyebrow:'她的脑海里，存着你',title:'记忆',sub:'MEMORY ARCHIVE'},
  companion:{eyebrow:'她正在陪你',title:'Now Linking',sub:'LINK TERMINAL'},
  persona:{eyebrow:'她们在这里',title:'她们',sub:'COMPANIONS'},
  life:{eyebrow:'把日子过好',title:'生活',sub:'ROUTINE'},
  moments:{eyebrow:'她们的日常',title:'朋友圈',sub:'MOMENTS'},
  records:{eyebrow:'被保存下来的时间',title:'记录',sub:'RECORDS'},
  features:{eyebrow:'一切能力，触手可及',title:'功能',sub:'MODULES'},
  agent:{eyebrow:'她的双手',title:'干活',sub:'AGENT TERMINAL'},
};
let hzCurView='overview';
function hzShowView(name){
  if(window.__hzTrace) console.info('[hz-trace] -> '+name, new Error().stack.split(String.fromCharCode(10)).slice(2,5).join(' | '));
  closeHzFan();
  /* 幂等防抖：同一视图重复激活不重渲染不重触发过渡（避免未知重入打断透明度过渡） */
  if(hzCurView===name && name!=='overview'){
    const cur=document.getElementById('hz-view-'+name);
    if(cur&&cur.classList.contains('on')) return;
  }
  try{ switchTab(name); }catch(e){ console.warn('[hz] switchTab:',e); }
  document.body.dataset.page=name;
  document.querySelectorAll('.d-it').forEach(x=>x.classList.toggle('on',x.dataset.v===name));
  const stage=hz$('#stage'); if(!stage) return;
  if(name==='overview'){
    hzCurView='overview';
    document.querySelectorAll('.hz-view').forEach(v=>v.classList.remove('on'));
    hzRenderOverview();
    return;
  }
  const meta=HZ_VIEW_META[name]; if(!meta) return;
  hzCurView=name;
  let v=document.getElementById('hz-view-'+name);
  if(!v){
    v=document.createElement('div');
    v.id='hz-view-'+name; v.className='hz-view';
    v.innerHTML='<div class="vh st" style="--i:0"><div><div class="v-eyebrow">'+meta.eyebrow+'</div><div class="v-title">'+meta.title+'<small>'+meta.sub+'</small></div></div><div class="v-acts" id="hzva-'+name+'"></div></div>';
    stage.appendChild(v);
  }
  document.querySelectorAll('.hz-view').forEach(x=>x.classList.toggle('on',x===v));
  /* 视图内容 = Flow 一等公民渲染（真实数据 + 真实动作） */
  try{ if(hzViewFns[name]) hzViewFns[name](v); }catch(e){ console.warn('[hz] view '+name+':',e); }
}

/* ═══════════════════════════════════════════════
   P5 · 七视图一等公民渲染器（原型构图 × 真实数据 × 真实动作）
   ═══════════════════════════════════════════════ */
const hzViewFns = {};
function hzEsc(s){ return String(s==null?'':s).replace(/[<>&"]/g, c=>({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c])); }
function hzSid(){ try{ return (window.Session&&Session.getSessionId&&Session.getSessionId())||'default'; }catch(e){ return 'default'; } }
function hzFirstContact(){ return Store.listContacts()[0]||null; }
function hzStars(n){ n=Math.max(1,Math.min(5,Math.round(n||3))); return '★★★★★'.slice(0,n)+'☆☆☆☆☆'.slice(0,5-n); }
function hzRecNo(s){ let h=0; for(const ch of String(s)) h=(h*31+ch.charCodeAt(0))>>>0; return 'REC-'+String(h%9000+1000); }
function hzMd(ts){ const d=new Date(ts); return (d.getMonth()+1)+'月'+d.getDate()+'日'; }

/* ---------- 记忆 ---------- */
let hzMemFilter='all';
let hzMemSearch='';
hzViewFns.memory = async function(v){
  const acts=v.querySelector('#hzva-memory');
  const contacts=Store.listContacts();
  acts.innerHTML='<div class="searchbar">🔍 <input id="hzMemQ" placeholder="搜索记忆…" style="width:150px"></div><button class="pill-btn ghost" id="hzMReEval">⟳ 重评重要性</button>';
  const q=acts.querySelector('#hzMemQ'); q.value=hzMemSearch;
  q.addEventListener('input',()=>{ hzMemSearch=q.value.trim(); renderMemList(v); });
  acts.querySelector('#hzMReEval').addEventListener('click', async ()=>{
    hzToast('正在让 AI 重新评估记忆重要性…');
    try{
      const r=await fetch('/api/memory/re_evaluate',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:hzSid(),character_id:'all'})});
      const j=await r.json();
      hzToast(j.ok?('重评完成：评估 '+j.evaluated+' 条，更新 '+j.updated+' 条'):'重评失败，稍后再试');
      if(j.ok) hzViewFns.memory(v);
    }catch(e){ hzToast('重评失败，稍后再试'); }
  });

  const body=document.createElement('div');
  body.className='hz-memgrid st'; body.style.setProperty('--i','1');
  body.innerHTML='<div class="hz-memmain" id="hzMemMain"><div class="hz-empty" style="padding:40px;text-align:center;color:var(--text-sub);font-size:12px">正在读取记忆…</div></div>'+
    '<div class="hz-memside">'+
    '<div class="hz-count st" style="--i:2"><span class="big" id="hzMemCnt">0</span><span style="font-size:11px;color:var(--text-sub)">条记忆</span><span style="margin-left:auto;font-size:10px;color:var(--faint)" class="latin">SYNCED ✓</span></div>'+
    '<div class="hz-dist st" style="--i:3"><div class="t-label">Type Mix · 类型分布</div><div id="hzDistRows"></div></div>'+
    '<div class="hz-count st" style="--i:4;cursor:pointer" id="hzMemIo"><svg class="ico" style="font-size:18px"><use href="#i-folder"/></svg><div style="flex:1"><div style="font-weight:900;font-size:13px">导入 / 导出</div><div style="font-size:10px;color:var(--text-sub);margin-top:2px">TXT · JSON · 记忆库迁移</div></div><span class="latin" style="font-size:9px;color:var(--faint);font-weight:800">GO ›</span></div>'+
    '</div>';
  v.appendChild(body);
  body.querySelector('#hzMemIo').addEventListener('click',()=>{ const c=hzFirstContact(); if(window.MemStore){ MemStore.open(c?c.id:''); } else hzToast('记忆库模块未加载'); });

  let mems=[], source='local';
  try{
    const p=new URLSearchParams({page:1,per_page:60,search:'',session_id:hzSid()});
    p.set('character_id','all');
    const r=await fetch('/api/pc/memory/list?'+p);
    const d=await r.json();
    if(Array.isArray(d.memories)&&d.memories.length){ mems=d.memories; source='api'; }
  }catch(e){}
  if(!mems.length){
    mems=contacts.flatMap(c=>(c.memStore||[]).map(m=>({ ...m, character_name:c.name, importance:m.importance||3, ts:m.date?new Date(m.date).getTime():Date.now() })));
  }
  const main=body.querySelector('#hzMemMain');
  main.innerHTML='';

  /* 联系人筛选 chips */
  const chipRow=document.createElement('div');
  chipRow.style.cssText='display:flex;gap:8px;flex-wrap:wrap';
  const mkChip=(label,val)=>{ const s=document.createElement('span'); s.className='chip'+(hzMemFilter===val?' on':''); s.textContent=label;
    s.addEventListener('click',()=>{ hzMemFilter=val; hzViewFns.memory(v); }); return s; };
  chipRow.appendChild(mkChip('全部','all'));
  contacts.forEach(c=>chipRow.appendChild(mkChip(c.name,c.name)));
  chipRow.appendChild(mkChip('★ 置顶','pin'));
  main.appendChild(chipRow);

  let filtered=hzMemFilter==='all'?mems:mems.filter(m=>(m.character_name||m.character_id||'')===hzMemFilter);
  if(hzMemFilter==='pin') filtered=mems.filter(m=>(Number(m.importance)||0)>=5);
  if(hzMemSearch) filtered=filtered.filter(m=>((m.content||'')+(m.title||'')).includes(hzMemSearch));
  setTimeout(()=>hzCountUp(body.querySelector('#hzMemCnt'),filtered.length,900),150);

  /* 类型分布 */
  const types={};
  filtered.forEach(m=>{ const t=(m.category||m.source||'chat'); types[t]=(types[t]||0)+1; });
  const distBody=body.querySelector('#hzDistRows'); distBody.innerHTML='';
  const entries=Object.entries(types).sort((a,b)=>b[1]-a[1]).slice(0,5);
  const maxN=entries.length?entries[0][1]:1;
  entries.forEach(([k,n],i)=>{
    const row=document.createElement('div'); row.className='hz-drow'; row.style.setProperty('--i',i);
    row.innerHTML='<span class="dn">'+hzEsc(k)+'</span><div class="db"><i style="--w:'+Math.round(n/maxN*100)+'%"></i></div><span class="dv">'+Math.round(n/(filtered.length||1)*100)+'%</span>';
    distBody.appendChild(row);
  });

  /* 时间轴记忆卡 */
  const tl=document.createElement('div'); tl.className='hz-memtl';
  if(!filtered.length){
    tl.innerHTML='<div style="padding:40px;text-align:center;color:var(--text-sub);font-size:12px">还没有记忆<br><span style="font-size:11px;color:var(--faint)">和 TA 聊聊，第一条记忆会自动出现</span></div>';
  }
  filtered.slice(0,14).forEach((m,i)=>{
    const imp=Number(m.importance)||3;
    const typeLabel=(m.category||m.source||'记忆');
    const card=document.createElement('div'); card.className='hz-mem2';
    card.innerHTML='<div class="top"><span class="type"><i'+(imp>=4?'':' class="hollow"')+'></i>'+hzEsc(typeLabel)+'</span><span class="stars">'+hzStars(imp)+'</span><span class="sn">'+hzRecNo(m.id||i)+'</span></div>'+
      '<div class="tx">'+hzEsc(m.content||m.title||'')+'</div>'+
      '<div class="tm">'+(m.date?hzEsc(String(m.date).slice(5).replace('-','月')+'日'):(m.ts?hzMd(m.ts):''))+(m.character_name?' · 来自 '+hzEsc(m.character_name):'')+'</div>';
    card.addEventListener('click',()=>{ const c=Store.listContacts().find(x=>x.name===(m.character_name||m.character_id)); if(c&&window.Profile) Profile.open(c.id); });
    tl.appendChild(card);
  });
  main.appendChild(tl);
};

/* ---------- 陪伴 ---------- */
hzViewFns.companion = function(v){
  const acts=v.querySelector('#hzva-companion');
  acts.innerHTML='<span class="chip on">● 全部在线</span>';
  const contacts=Store.listContacts();
  const hero=contacts[0];
  const body=document.createElement('div');
  body.className='hz-linkgrid st'; body.style.setProperty('--i','1');
  body.innerHTML=
    '<div class="hz-npwrap"><div class="hz-np" id="hzNp">'+
    '<div class="hz-np-art"><span style="position:relative;z-index:1">🎧</span><div class="rings"></div></div>'+
    '<div class="hz-np-t" id="hzNpT">还没有进行中的陪伴</div>'+
    '<div class="hz-np-s" id="hzNpS">从右侧选择一种陪伴方式开始</div>'+
    '<div class="hz-np-wave"><div class="wave-live">'+'<i style="height:5px;animation-delay:0s"></i><i style="height:11px;animation-delay:.1s"></i><i style="height:16px;animation-delay:.2s"></i><i style="height:8px;animation-delay:.3s"></i><i style="height:14px;animation-delay:.4s"></i><i style="height:6px;animation-delay:.5s"></i><i style="height:12px;animation-delay:.6s"></i><i style="height:18px;animation-delay:.7s"></i><i style="height:9px;animation-delay:.8s"></i><i style="height:13px;animation-delay:.9s"></i><i style="height:6px;animation-delay:1s"></i><i style="height:10px;animation-delay:1.1s"></i>'+'</div></div>'+
    '<div class="hz-np-ops"><button class="hz-np-op" id="hzNpPrev">⏮</button><button class="hz-np-op hz-np-play" id="hzNpPlay">▶</button><button class="hz-np-op" id="hzNpNext">⏭</button></div>'+
    '</div></div>'+
    '<div class="hz-linkside">'+
    '<div class="hz-g2 st" style="--i:2">'+
    '<div class="hz-gcard" id="hzGmc"><span class="act off" id="hzGmcAct">OFF</span><div class="gi">⛏️</div><div class="gn">我的世界 <span class="led off"></span></div><div class="gs" id="hzGmcS">未连接</div></div>'+
    '<div class="hz-gcard" id="hzGsd"><span class="act off" id="hzGsdAct">OFF</span><div class="gi">🌾</div><div class="gn">星露谷</div><div class="gs" id="hzGsdS">未启动</div></div>'+
    '</div>'+
    '<div class="hz-jealous st" style="--i:3"><div class="h">会为你吃醋<span class="tgl'+(Store.getSettings().jealousyEnabled===false?' off':'')+'" id="hzJelTgl"></span></div><div class="d">当检测到你和别人聊得过于热络时产生小情绪。<b>阈值：中等</b> · 冷却 <b>45</b> 分钟。</div></div>'+
    '<div class="st" style="--i:4"><div class="t-label" style="margin-bottom:10px">Quick Command · 快捷指挥</div><div class="hz-cmds" id="hzCmds"></div></div>'+
    '</div>';
  v.appendChild(body);

  const cmds=[['💧','浇水'],['🧺','收菜'],['📍','跟着我'],['🎣','钓鱼'],['⛏️','挖矿'],['⏹','停下']];
  cmds.forEach(([ic,c],i)=>{
    const b=document.createElement('button'); b.className='hz-cmd'+(i===2?' hot':''); b.innerHTML=ic+' '+c;
    b.addEventListener('click',()=>{ body.querySelectorAll('.hz-cmd').forEach(x=>x.classList.remove('hot')); b.classList.add('hot'); hzToast('指令已发送：'+c); if(hero&&window.Chat){ try{ Chat.send({type:'text',content:c}); }catch(e){} } });
    body.querySelector('#hzCmds').appendChild(b);
  });
  body.querySelector('#hzJelTgl').addEventListener('click',function(){ this.classList.toggle('off'); Store.saveSettings({jealousyEnabled:!this.classList.contains('off')}); hzToast(this.classList.contains('off')?'吃醋已关闭':'吃醋已开启'); });
  const play=body.querySelector('#hzNpPlay');
  play.addEventListener('click',()=>{ play.textContent=play.textContent==='▶'?'⏸':'▶'; });
  body.querySelector('#hzNpPrev').addEventListener('click',()=>hzToast('上一段陪伴'));
  body.querySelector('#hzNpNext').addEventListener('click',()=>hzToast('下一段陪伴'));

  const heroCard=document.createElement('div');
  heroCard.className='hz-jealous st'; heroCard.style.setProperty('--i','5'); heroCard.style.marginTop='auto';
  heroCard.innerHTML='<div class="h" style="gap:12px"><div class="avatar" style="width:40px;height:40px;font-size:15px;background:var(--soft);color:var(--text-sub)">'+(hero?hzEsc(hero.name.charAt(0)):'—')+'</div><div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:900">'+(hero?hzEsc(hero.name):'还没有伴侣')+'</div><div style="font-size:10px;color:var(--text-sub);margin-top:2px">'+(hero?('SYNC '+intimacyInfo(hero).pct+' · '+intimacyInfo(hero).name):'先去「她们」创造一位')+'</div></div>'+(hero?'<button class="hz-cmd hot" style="padding:8px 14px">去聊天</button>':'')+'</div>';
  if(hero) heroCard.querySelector('.hz-cmd').addEventListener('click',()=>hzOpenChat(hero.id));
  body.querySelector('.hz-linkside').appendChild(heroCard);

  const setG=(card,act,s,online,txt)=>{ act.textContent=online?'LIVE':'OFF'; act.classList.toggle('off',!online);
    card.classList.toggle('on',online); s.textContent=txt; };
  fetch('/api/numen/status').then(r=>r.json()).then(j=>{ setG(body.querySelector('#hzGmc'),body.querySelector('#hzGmcAct'),body.querySelector('#hzGmcS'),!!j.online,(j.companion?'在线 · 陪伴者 '+j.companion:'服务器已连接 · 未绑定')); }).catch(()=>{});
  fetch('/api/stardew/status').then(r=>r.json()).then(j=>{ const on=!!(j&&(j.online||j.enabled&&(j.companion||j.running))); setG(body.querySelector('#hzGsd'),body.querySelector('#hzGsdAct'),body.querySelector('#hzGsdS'),on,(j&&j.companion)?('农场经营中 · '+j.companion):'可一键启动代管'); }).catch(()=>{});
  const mode=(m,label)=>{ fetch('/api/companion/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:hzSid(),mode:m})}).then(()=>hzToast(label)).catch(()=>hzToast(label)); };
  body.querySelector('#hzGmc').addEventListener('click',()=>mode('minecraft','我的世界陪伴已开启'));
  body.querySelector('#hzGsd').addEventListener('click',()=>mode('stardew','星露谷代管已开启'));
};

/* ---------- 她们 ---------- */
hzViewFns.persona = function(v){
  const acts=v.querySelector('#hzva-persona');
  acts.innerHTML='<button class="pill-btn ghost" id="hzAddC">＋ 新的伴侣</button>';
  acts.querySelector('#hzAddC').addEventListener('click',()=>{ if(window.openContactSheet) openContactSheet(null); else hzToast('创建模块未加载'); });
  const contacts=Store.listContacts().slice().sort((a,b)=>(intimacyInfo(b).v||0)-(intimacyInfo(a).v||0));
  const row=document.createElement('div'); row.className='hz-hers';
  if(!contacts.length){
    row.innerHTML='<div style="flex:1;display:flex;align-items:center;justify-content:center;color:var(--text-sub);font-size:12px;background:var(--card);border-radius:28px;box-shadow:var(--sh)">还没有 AI 伴侣 · 点右上「新的伴侣」亲手创造一个</div>';
    v.appendChild(row); return;
  }
  const ringSvg=(pct)=>'<div class="ring"><svg viewBox="0 0 104 104"><circle class="rt" cx="52" cy="52" r="46" fill="none" stroke-width="7"/><circle class="rv" cx="52" cy="52" r="46" fill="none" stroke-width="7" style="stroke-dashoffset:'+(289-289*pct/100)+'px"/></svg><div class="mid"><span class="v">'+pct+'</span><span class="k">SYNC</span></div></div>';
  const _relUIH=(typeof window.relUIShow==='function')?window.relUIShow():false;
  contacts.slice(0,3).forEach((c,i)=>{
    const info=intimacyInfo(c);
    /* 相识天数：后端已按角色卡 created_at 给出真实跨度（window.__hzTenure），优先用它 */
    const days=(window.__hzTenure&&window.__hzTenure[c.name])||(c.createdAt?Math.max(1,Math.floor((Date.now()-c.createdAt)/864e5)):1);
    const hero=i===0;
    const card=document.createElement('div');
    card.className='hz-pcard'+(hero?' hero-p':'')+' st'; card.style.setProperty('--i',i+1);
    const aff=Number(c.affection)||0, tru=Number(c.trust)||0;
    const bio=String(c.persona||'').replace(/\s+/g,' ').slice(0,42);
    card.innerHTML=
      '<div class="p-top"><div class="avatar">'+hzEsc(c.name.charAt(0))+'</div><div><div class="sn">UNIT-0'+(i+1)+' · DAY '+days+'</div><div class="p-nm">'+hzEsc(c.name)+' <span class="led"></span></div><div class="p-st">在线'+(_relUIH?' · '+hzEsc(info.name):'')+'</div></div></div>'+
      (bio?'<div class="p-bio">'+hzEsc(bio)+(String(c.persona||'').length>42?'…':'')+'</div>':'')+
      (_relUIH?'<div class="p-ring">'+ringSvg(info.pct)+'</div>':'')+
      (hero&&_relUIH?'<div class="prows"><div class="prow"><span class="pl">心动</span><span class="latin">'+(aff||'—')+'</span></div><div class="prow"><span class="pl">信任</span><span class="latin">'+(tru||'—')+'</span></div><div class="prow"><span class="pl">相伴</span><span class="latin">'+days+' 天</span></div></div>'
        :(hero?'<div class="prows"><div class="prow"><span class="pl">相伴</span><span class="latin">'+days+' 天</span></div></div>':''))+
      '<div class="p-act"><span class="pill-btn'+(hero?'" style="background:var(--bg);color:var(--ink)':' ghost')+'">去聊天</span><span class="pill-btn ghost" style="'+(hero?'background:rgba(244,244,242,.14);color:inherit;border-color:transparent':'')+'">档案 ›</span></div>';
    card.addEventListener('click',()=>{ if(window.Profile) Profile.open(c.id); });
    const [chatBtn,profBtn]=card.querySelectorAll('.p-act .pill-btn');
    chatBtn.addEventListener('click',e=>{ e.stopPropagation(); hzOpenChat(c.id); });
    profBtn.addEventListener('click',e=>{ e.stopPropagation(); if(window.Profile) Profile.open(c.id); });
    row.appendChild(card);
  });
  v.appendChild(row);
};

/* ---------- 生活 ---------- */
let hzLifeSeg='todo';
hzViewFns.life = function(v){
  const acts=v.querySelector('#hzva-life');
  acts.innerHTML='<button class="pill-btn" id="hzLifeAdd">＋ 记一笔</button>';
  acts.querySelector('#hzLifeAdd').addEventListener('click',()=>{
    if(!window.Sheet) return hzToast('弹层模块未加载');
    const inp=document.createElement('input');
    inp.type='text'; inp.placeholder='要做的事…（回车保存）';
    inp.style.cssText='width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:12px;font-size:14px;outline:none';
    const form=document.createElement('div'); form.appendChild(inp);
    inp.addEventListener('keydown',e=>{ if(e.key==='Enter'&&inp.value.trim()){ Store.todos.add({text:inp.value.trim(),due:'',done:false}); Sheet.close(); hzToast('已添加待办'); hzViewFns.life(v); } });
    Sheet.open(form); setTimeout(()=>inp.focus(),80);
  });

  const todos=Store.todos.list().slice().sort((a,b)=>(a.done-b.done)||((a.due||'9999').localeCompare(b.due||'9999')));
  const books=Store.handbook.list().slice().reverse();
  const notes=Store.notes.list();
  const today=dateStrOf(Date.now());

  const body=document.createElement('div'); body.className='hz-life';
  body.innerHTML=
    '<div class="hz-lifecol" style="flex:1.1"><div class="st" style="display:flex;gap:8px;--i:1"><span class="chip'+(hzLifeSeg==='todo'?' on':'')+'" data-s="todo">待办</span><span class="chip'+(hzLifeSeg==='hb'?' on':'')+'" data-s="hb">手帐</span></div><div class="st" id="hzLifeMain" style="--i:2;display:flex;flex-direction:column;gap:14px;flex:1;min-height:0;overflow-y:auto;scrollbar-width:thin"></div></div>'+
    '<div class="hz-lifecol st" style="flex:1;--i:3" id="hzLifeSide"></div>';
  v.appendChild(body);
  body.querySelectorAll('.chip[data-s]').forEach(ch=>ch.addEventListener('click',()=>{ hzLifeSeg=ch.dataset.s; hzViewFns.life(v); }));
  const main=body.querySelector('#hzLifeMain'), side=body.querySelector('#hzLifeSide');

  if(hzLifeSeg==='todo'){
    const card=document.createElement('div'); card.className='hz-todo st'; card.style.setProperty('--i','2');
    if(!todos.length) card.innerHTML='<div style="padding:26px;text-align:center;color:var(--text-sub);font-size:12px">今天没有待办，轻松一天</div>';
    todos.slice(0,10).forEach(t=>{
      const row=document.createElement('div'); row.className='row'+(t.done?' done':'');
      let dueBadge='';
      if(!t.done&&t.due&&t.due<today){ const n=Math.round((new Date(today)-new Date(t.due))/864e5); dueBadge='<span class="due">LATE '+(n||1)+'D</span>'; }
      else if(!t.done&&t.due===today){ dueBadge='<span class="due">TODAY</span>'; }
      else if(t.due){ dueBadge='<span class="due gray">'+t.due.slice(5).replace('-','-')+'</span>'; }
      row.innerHTML='<div class="ck"></div><div class="tx">'+hzEsc(t.text)+(t.due?'<div class="sub2">截止 '+t.due+'</div>':'')+'</div>'+dueBadge;
      row.addEventListener('click',()=>{ Store.todos.update(t.id,{done:!t.done}); hzViewFns.life(v); });
      card.appendChild(row);
    });
    main.appendChild(card);
  } else {
    if(!books.length){ main.innerHTML='<div style="padding:26px;text-align:center;color:var(--text-sub);font-size:12px">手帐还是空的 · 聊天中的灵感会自动归档到这里</div>'; }
    books.slice(0,8).forEach((b,i)=>{
      const c=document.createElement('div'); c.className='hz-jr st'; c.style.setProperty('--i',String(i+2));
      c.innerHTML='<div class="meta"><span class="badge">'+hzEsc(b.category||'手帐')+'</span><span class="tm">'+hzEsc(b.date||'')+(b.contactName?' · '+hzEsc(b.contactName):'')+'</span></div><div class="tx">'+hzEsc(b.content||b.title||'')+'</div>';
      main.appendChild(c);
    });
  }

  /* 右栏：手帐摘要 + 连续记录 + 定时任务 */
  side.innerHTML='';
  const latest=books[0];
  if(latest&&hzLifeSeg==='todo'){
    const c=document.createElement('div'); c.className='hz-jr st'; c.style.setProperty('--i','3');
    c.innerHTML='<div class="meta"><span class="badge">'+hzEsc(latest.category||'手帐')+'</span><span class="tm">'+hzEsc(latest.date||'')+'</span></div><div class="tx">'+hzEsc((latest.content||latest.title||'').slice(0,60))+'</div>';
    side.appendChild(c);
  }
  const allDates=[...books.map(b=>b.date),...notes.map(n=>n.date)].filter(Boolean).sort();
  let streak=0; if(allDates.length){
    const earliest=new Date(allDates[0]); streak=Math.max(1,Math.ceil((Date.now()-earliest.getTime())/6048e5));
  }
  const c1=document.createElement('div'); c1.className='hz-count st'; c1.style.setProperty('--i','4');
  c1.innerHTML='<svg class="ico" style="font-size:18px"><use href="#i-search"/></svg><div style="flex:1"><div style="font-weight:900;font-size:13px">连续记录 '+(streak||1)+' 周</div><div style="font-size:10px;color:var(--text-sub);margin-top:2px">手帐不要断，情绪有处放</div></div><span class="latin" style="font-weight:800;font-size:18px">'+(notes.length+books.length)+'</span>';
  side.appendChild(c1);
  const rems=Store.reminders.list().filter(r=>!r.done);
  const c2=document.createElement('div'); c2.className='hz-count st'; c2.style.setProperty('--i','5');
  c2.innerHTML='<div style="flex:1"><div style="font-weight:900;font-size:13px">定时任务 · '+rems.length+' 个待触发</div><div style="font-size:10px;color:var(--text-sub);margin-top:2px">'+(rems[0]?hzEsc(rems[0].task||rems[0].text||'')+' · '+hzEsc(rems[0].at||''):'在功能页创建早晚安与提醒')+'</div></div>';
  side.appendChild(c2);
  const habits=Store.habits.list();
  if(habits.length){
    const c3=document.createElement('div'); c3.className='hz-count st'; c3.style.setProperty('--i','6');
    const hk=habits[0]; const hd=Array.isArray(hk.dates)?hk.dates.length:0;
    c3.innerHTML='<div style="flex:1"><div style="font-weight:900;font-size:13px">习惯 · '+hzEsc(hk.name||'')+'</div><div style="font-size:10px;color:var(--text-sub);margin-top:2px">已坚持 '+hd+' 天'+(habits.length>1?(' · 共 '+habits.length+' 个习惯'):'')+'</div></div><span class="latin" style="font-weight:800;font-size:18px">'+hd+'</span>';
    side.appendChild(c3);
  }
  const caps=Store.capsules.list().filter(c=>!c.opened);
  if(caps.length){
    const c4=document.createElement('div'); c4.className='hz-count st'; c4.style.setProperty('--i','7');
    c4.innerHTML='<div style="flex:1"><div style="font-weight:900;font-size:13px">时间胶囊 · '+caps.length+' 封未拆</div><div style="font-size:10px;color:var(--text-sub);margin-top:2px">'+(caps[0].openDate?('最早 '+caps[0].openDate+' 可拆'):'到期后可拆开')+'</div></div>';
    side.appendChild(c4);
  }
};

/* ---------- 朋友圈 ---------- */
hzViewFns.moments = async function(v){
  const acts=v.querySelector('#hzva-moments');
  const contacts=Store.listContacts();
  let hzMomentCid=hzMomentCid||(contacts[0]?contacts[0].name:'');
  acts.innerHTML='';
  const scrollWrap=document.createElement('div');
  scrollWrap.className='hz-scroll st'; scrollWrap.style.setProperty('--i','1');
  scrollWrap.innerHTML='<div class="hz-postbtn"><div><div class="pb-l">发一条动态</div><div class="pb-s">文字、图片，或此刻的心情</div></div><button class="pill-btn">✏️ 发布</button></div><div id="hzMoments"></div>';
  v.appendChild(scrollWrap);
  const listEl=scrollWrap.querySelector('#hzMoments');
  scrollWrap.querySelector('.pill-btn').addEventListener('click',()=>{
    if(window._showPublishBox){ _showPublishBox(scrollWrap); }
    else hzToast('发布模块未加载');
  });

  if(!contacts.length){ listEl.innerHTML='<div style="padding:40px;text-align:center;color:var(--text-sub);font-size:12px">先在「她们」创建一位伴侣，朋友圈才会热闹起来</div>'; return; }

  /* 联系人切换 chips */
  const chips=document.createElement('div');
  chips.style.cssText='display:flex;gap:8px;margin-bottom:14px';
  contacts.slice(0,5).forEach(c=>{
    const s=document.createElement('span'); s.className='chip'+(hzMomentCid===c.name?' on':''); s.textContent=c.name;
    s.addEventListener('click',()=>{ hzMomentCid=c.name; hzMomentCid=c.name; hzViewFns.moments(v); });
    chips.appendChild(s);
  });
  listEl.parentNode.insertBefore(chips,listEl);

  listEl.innerHTML='<div style="padding:30px;text-align:center;color:var(--text-sub);font-size:12px">正在拉取动态…</div>';
  let items=[];
  try{
    const r=await fetch('/api/moments?session_id='+encodeURIComponent(hzSid())+'&character_name='+encodeURIComponent(hzMomentCid)+'&limit=10&offset=0');
    const d=await r.json(); items=d.moments||d||[];
  }catch(e){}
  listEl.innerHTML='';
  if(!items.length){
    listEl.innerHTML='<div style="padding:36px;text-align:center;color:var(--text-sub);font-size:12px">还没有动态 · 和 TA 聊聊天，让它发第一条朋友圈</div>';
    return;
  }
  items.forEach(m=>{
    const card=document.createElement('div'); card.className='hz-moment';
    const imgs=(m.images||[]);
    card.innerHTML=
      '<div class="hd"><div class="avatar">'+hzEsc((m.character_name||hzMomentCid||'TA').charAt(0))+'</div><div><div class="nm">'+hzEsc(m.character_name||hzMomentCid)+'</div><div class="tm">'+hzEsc(String(m.created_at||m.time||'').replace('T',' ').slice(5,16))+'</div></div></div>'+
      '<div class="tx">'+hzEsc(m.content||'')+'</div>'+
      (imgs.length?'<div class="grid3">'+imgs.slice(0,3).map(u=>'<img class="im" src="'+hzEsc(u)+'" loading="lazy">').join('')+'</div>':'')+
      '<div class="ops"><button class="hz-like'+(m.liked?' on':'')+'">♥ <span>'+(Number(m.likes)||0)+'</span></button><span class="hz-like">💬 '+(Number(m.comments)||0)+'</span><span style="margin-left:auto;color:var(--faint)">···</span></div>';
    const likeBtn=card.querySelector('.hz-like');
    likeBtn.addEventListener('click',()=>{
      fetch('/api/moments/'+m.id+'/like',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:hzSid(),character_name:hzMomentCid})}).then(r=>r.json()).then(j=>{
        const sp=likeBtn.querySelector('span'); sp.textContent=Math.max(0,(Number(sp.textContent)||0)+((j&&j.liked)||(j&&j.ok)?1:-1));
        likeBtn.classList.toggle('on');
      }).catch(()=>hzToast('点赞失败'));
    });
    listEl.appendChild(card);
  });
};
let hzMomentCid='';

/* ---------- 记录 ---------- */
let hzRecFilter='all';
hzViewFns.records = function(v){
  const acts=v.querySelector('#hzva-records');
  acts.innerHTML='<div style="display:flex;gap:8px"><span class="chip'+(hzRecFilter==='all'?' on':'')+'" data-f="all">全部</span><span class="chip'+(hzRecFilter==='letter'?' on':'')+'" data-f="letter">✉ 信件</span><span class="chip'+(hzRecFilter==='note'?' on':'')+'" data-f="note">笔记</span><span class="chip'+(hzRecFilter==='chat'?' on':'')+'" data-f="chat">对话</span></div>';
  acts.querySelectorAll('.chip[data-f]').forEach(ch=>ch.addEventListener('click',()=>{ hzRecFilter=ch.dataset.f; hzViewFns.records(v); }));

  const wrap=document.createElement('div'); wrap.className='hz-scroll st'; wrap.style.setProperty('--i','1');
  v.appendChild(wrap);

  const items=[];
  if(hzRecFilter==='all'||hzRecFilter==='letter'){
    Store.letters.list().forEach(l=>items.push({kind:'letter',icon:l.type==='night'?'🌙':(l.type==='morning'?'☀️':'✉️'),title:(l.title||'信件')+' · '+(l.contactName||l.characterName||'TA'),sub:l.content||'',time:l.date||''}));
  }
  if(hzRecFilter==='all'||hzRecFilter==='note'){
    Store.notes.list().forEach(n=>items.push({kind:'note',icon:'📝',title:(n.title||'笔记')+(n.category?' · '+n.category:''),sub:n.content||'',time:n.date||''}));
  }
  if(hzRecFilter==='all'||hzRecFilter==='chat'){
    Store.listConversations().forEach(({cv,contact})=>{
      const logs=(contact&&contact.logs)||[]; const msgs=Store.getMessages(cv.id);
      const last=msgs[msgs.length-1];
      items.push({kind:'chat',icon:'💬',title:'对话归档 · '+(contact?contact.name:''),sub:(logs.length?('每日归档 '+logs.length+' 天 · '):'')+msgs.length+' 条消息'+(last?(' · 最后：'+String(last.content||'').slice(0,30)):''),time:logs.length?logs[logs.length-1].date:''});
    });
  }
  items.sort((a,b)=>String(b.time).localeCompare(String(a.time)));
  if(!items.length){ wrap.innerHTML='<div style="padding:40px;text-align:center;color:var(--text-sub);font-size:12px">还没有记录 · 聊天、信件和笔记都会自动归档到这里</div>'; return; }
  items.slice(0,20).forEach(it=>{
    const d=document.createElement('div'); d.className='hz-rec';
    d.innerHTML='<div class="qi">'+it.icon+'</div><div style="flex:1;min-width:0"><div class="rt">'+hzEsc(it.title)+'</div><div class="rs">'+hzEsc(it.sub)+'</div></div><span class="rtime">'+hzEsc(it.time).toUpperCase()+'</span>';
    d.addEventListener('click',()=>d.classList.toggle('open'));
    wrap.appendChild(d);
  });
};

/* ---------- 功能 ---------- */
hzViewFns.features = function(v){
  const rems=Store.reminders.list().filter(r=>!r.done).slice(0,2);
  const bb=Store.getSettings().blindBox||{};
  const today=dateStrOf(Date.now());
  const bbToday=bb.date===today?bb:null;
  const fc=hzFirstContact();
  const tiles=[
    {big:1,icon:'📰',t:'今日早报',s:'AI 汇总新闻与她的近况',badge:'',prev:'',fn:()=>{ if(window.generateMorningReport){ generateMorningReport(); hzToast('正在生成今日早报…'); } else hzToast('早报模块未加载'); }},
    {icon:'🎁',t:'今日盲盒',s:'每天一个小任务或惊喜',badge:bbToday?(bbToday.opened?'已开启':'未开启'):'待抽取',fn:()=>{ initBlindBox(true); const s2=Store.getSettings().blindBox; hzToast(s2&&s2.opened?('今日任务：'+(s2.task||'已完成打卡')):'盲盒已就绪，去功能页开盒'); }},
    {icon:'🎵',t:'AI 学歌',s:'学新歌唱给你听',fn:()=>{ if(window.SongsPanel){ SongsPanel.open(); } else hzToast('学歌模块未加载'); }},
    {big:1,icon:'🧠',t:'AI 学习中心',s:'让 AI 从对话、手帐、晚报里持续学习，越来越懂你',prog:62,fn:()=>{ if(window.openLearningSheet){ openLearningSheet(); } else hzToast('学习中心未加载'); }},
    {icon:'🗄️',t:'磁盘记忆库',s:'本地文件夹 .txt 导入导出',fn:()=>{ if(window.MemStore){ MemStore.open(fc?fc.id:''); } else hzToast('记忆库未加载'); }},
    {icon:'📦',t:'外置记忆库',s:'周总结 / 月总结 / 原文',fn:()=>{ if(window.ExtMem){ ExtMem.open(fc?fc.id:''); } else hzToast('外置记忆库未加载'); }},
    {icon:'🌾',t:'星露谷联动',s:'农场经营代管与互动',fn:()=>hzShowView('companion')},
    {big:1,icon:'⏰',t:'定时任务',s:'早晚安、提醒、主动发言',next:rems.map(r=>({l:r.task||r.text||'定时任务',t:r.at||''})),fn:()=>{ if(window.SelfAware){ /* 保持在本视图 */ } hzToast('定时任务 '+rems.length+' 个待触发'); }},
    {icon:'🔍',t:'自我觉察',s:'TA 对自己的想法与感受',fn:()=>{ if(window.SelfAware){ SelfAware.open(); } else hzToast('觉察模块未加载'); }},
    {icon:'💾',t:'数据备份',s:'导出 / 导入全部数据',fn:()=>{ if(window.openBackupSheet){ openBackupSheet(); } else if(window.Store){ const data=Store.exportData(); navigator.clipboard&&navigator.clipboard.writeText(data); hzToast('数据已导出到剪贴板'); } }},
    {icon:'🎙️',t:'音色克隆',s:'用 TA 的声音朗读与通话',fn:()=>{ if(window.VoiceClonePanel){ VoiceClonePanel.open(fc?fc.id:''); } else hzToast('音色克隆未加载'); }},
  ];
  const grid=document.createElement('div'); grid.className='hz-fgrid';
  tiles.forEach((t,i)=>{
    const d=document.createElement('div');
    d.className='hz-fn'+(t.big?' big':'')+' st tilt'; d.style.setProperty('--i',String(Math.min(i+1,8)));
    d.innerHTML='<div class="farrow">↗</div>'+(t.badge?'<span class="fbadge">'+t.badge+'</span>':'')+
      '<div class="fi">'+t.icon+'</div><div class="ft">'+t.t+'</div><div class="fs">'+t.s+'</div>'+
      (t.prev?'<div class="fprev"><div class="fpline">'+t.prev+'</div></div>':'')+
      (t.prog?'<div class="fprog"><i style="--w:'+t.prog+'%"></i></div>':'')+
      (t.next&&t.next.length?'<div class="fnext">'+t.next.map(n=>'<div class="fpline"><span>'+hzEsc(n.l)+'</span><b>'+hzEsc(String(n.t).slice(11,16)||String(n.t))+'</b></div>').join('')+'</div>':'');
    d.addEventListener('click',t.fn);
    grid.appendChild(d);
  });
  v.appendChild(grid);
  if(window.bindHzTilt) bindHzTilt();
};

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
    for(var i=0;i<14;i++){ var p=document.createElement('i');
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
    var days=(window.__hzTenure&&cs[0]&&window.__hzTenure[cs[0].name])||(cs[0]&&cs[0].createdAt?Math.max(1,Math.floor((Date.now()-cs[0].createdAt)/864e5)):'--');
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
