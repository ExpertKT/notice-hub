// Run through gate/shot.mjs @tests/ui_repair_probe.js on an isolated demo fixture.
// ?scene=home|chat|retry|map-error|routine-error|detail; add &recover=1 for retry recovery.
// Error scenes replace only their endpoint in this page; never use against a user instance.
(async()=>{
for(const id of ['onboarding','journey','platform-setup-wizard','update-guide','announce-modal','announce-panel']){const n=document.getElementById(id);if(n){n.hidden=true;n.style.display='none'}}
const scene=new URL(location.href).searchParams.get('scene')||'home';
const wait=ms=>new Promise(r=>setTimeout(r,ms));
const check=(ok,label)=>{if(!ok)throw Error(label)};
const box=s=>document.querySelector(s).getBoundingClientRect();
const visible=n=>{const r=n.getBoundingClientRect();return r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight};
if(scene==='chat'||scene==='retry'){
 document.querySelector('#agent-input').focus();await wait(1350);
 const log=document.querySelector('#agent-log');log.replaceChildren();
 if(scene==='chat'){
 for(let i=0;i<8;i++){agentLine('user','第'+i+'轮：今天还有哪些事？');agentLine('bot','已有两件事分别在立德楼和图书馆。\n先办完材料，再去还书。')}
 agentTrace([{kind:'read',text:'读了 5 件待办、今天还有 3 节课'},{kind:'plan',text:'核对两件事情的位置，选择顺路的次序。'},{kind:'call',tool:'create',args:{summary:'去图书馆还书',deadline:'2026-11-20T15:00'},ok:true,detail:'已写入待办，这一轮只新建一次。'}]);
 await wait(150);const last=document.querySelector('.agent-trace:last-child li:last-child'),r=last.getBoundingClientRect(),form=box('#agent-form'),center=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
 check(r.bottom<=form.top,'TRACE_UNDER_FORM');check(last===center||last.contains(center),'TRACE_OCCLUDED');check(document.querySelector('.agent-trace-head').getAttribute('aria-expanded')==='true','TRACE_COLLAPSED');
 }else{
 const real=window.fetch;let calls=0;window.fetch=(path,opts)=>String(path).includes('/api/agent/chat')?Promise.resolve(new Response(JSON.stringify(++calls===1?{error:'模型请求失败：HTTP 503'}:{say:'重试已收到',trace:[]}),{status:calls===1?503:200,headers:{'Content-Type':'application/json'}})):real(path,opts);
 submitAgent('今天还有哪些事');await wait(100);const retry=log.querySelector('button');check(retry?.textContent==='重试这句话','CHAT_RETRY_MISSING');
 if(new URL(location.href).searchParams.has('recover')){retry.click();await wait(150);check(calls===2&&log.textContent.includes('重试已收到'),'CHAT_RETRY_NOT_RECOVERED')}
 }
}
if(scene==='map-error'){
 const real=window.fetch;let bad=true,calls=0;window.fetch=(path,opts)=>String(path).includes('/api/agent/plan')?(calls++,bad?Promise.resolve(new Response(JSON.stringify({error:'暂时无法读取行程'}),{status:503})):real(path,opts)):real(path,opts);
 await nhTrip.reload();check(document.querySelector('#trip-list button')?.textContent==='重试读取行程','MAP_RETRY_MISSING');check(document.querySelector('#trip-legend').hidden,'STALE_LEGEND');check(!document.querySelector('#trip-map-meta').textContent,'STALE_MAP_META');
 if(new URL(location.href).searchParams.has('recover')){bad=false;document.querySelector('#trip-list button').click();await wait(300);check(calls===2&&!document.querySelector('#trip-list').textContent.includes('读行程失败'),'MAP_RETRY_NOT_RECOVERED')}
}
if(scene==='routine-error'){
 check(!document.querySelector('#trip-pin'),'ROUTINE_PIN_STILL_THERE');
 check(!document.querySelector('#trip-routine-open'),'ROUTINE_OPEN_STILL_THERE');
 check(!document.querySelector('#trip-schedule-open'),'SCHEDULE_OPEN_STILL_THERE');
}
if(scene==='detail'){document.querySelector('[data-tab="detail"]').click();await wait(100)}
if(scene==='home'){
 check(getComputedStyle(document.querySelector('.trip-map-note')).display==='none','MAP_NOTE_STILL_THERE');
 check(getComputedStyle(document.querySelector('#trip-legend')).display==='none','LEGEND_STILL_THERE');
}
const selectors='.brand,.announce-bell,.tabs button,.trip-tools button,.agent-form input,.agent-voice,#agent-send,.mini-btn,.link-btn,.task .btn,.task summary,.trip-routine-form input,.trip-routine-form button';
const small=[...document.querySelectorAll(selectors)].filter(n=>visible(n)&&getComputedStyle(n).visibility!=='hidden'&&getComputedStyle(n).opacity!=='0').map(n=>({id:n.id,text:n.textContent.trim().slice(0,16),w:n.getBoundingClientRect().width,h:n.getBoundingClientRect().height})).filter(r=>r.w<39.9||r.h<39.9);
check(small.length===0,'SMALL_CONTROLS '+JSON.stringify(small));check(document.documentElement.scrollWidth<=innerWidth+1,'PAGE_OVERFLOW');
return {pass:true,scene,small,scrollWidth:document.documentElement.scrollWidth};
})()