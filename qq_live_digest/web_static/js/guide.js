/* 引导与首次设置 —— 从 page.html 原样搬出，逻辑未改。
   包含：冷启动引导/二维码/NapCat 安装与群订阅、首次风险提示（onboarding）、
   托管状态胶囊 + 新手引导 journey。
   仍留在 page.html 的部分：上面的 onclick 绑定段、initBrandMark（logo 动画）、
   页尾首次设置向导 IIFE（加载即执行，前移会改变时序）。 */

// —— 冷启动引导：把新用户直接送到二维码入口（纯前端，不新增接口） ——
var COLD_KEY = 'nh_coldstart_guide_closed';
var coldState = {tasks: null, qqOnline: false};
var napcatRunning = false;
// 没启动 NapCat 时不要显示空的二维码位置，直接给一个能点的入口（B）。
function applyNapcatQrState(running) {
  napcatRunning = !!running;
  var btn = document.getElementById('qr-start');
  var tip = document.getElementById('qr-start-note');
  var image = document.getElementById('qr');
  var note = document.getElementById('qr-note');
  if (btn) btn.hidden = napcatRunning;
  if (tip) tip.hidden = napcatRunning;
  if (napcatRunning) return;
  if (image) image.hidden = true;
  if (note) { note.hidden = false; note.textContent = '还没启动 NapCat，所以这里还没有二维码。'; }
}
function updateColdStartGuide() {
  var bar = document.getElementById('coldstart-guide');
  if (!bar) return;
  var closed = false;
  try { closed = localStorage.getItem(COLD_KEY) === '1'; } catch (error) {}
  bar.hidden = !(coldState.tasks === 0 && !coldState.qqOnline && !closed);
}
function setColdStartTasks(count) {
  coldState.tasks = count;
  updateColdStartGuide();
}
function setColdStartQq(online) {
  coldState.qqOnline = !!online;
  updateColdStartGuide();
}
function pointToConnect() {
  var panel = document.getElementById('connect');
  if (!panel) return;
  var frozen = motionIsPaused();
  // 滚到二维码入口本身（#connect 卡片有 3000px 高，滚到卡片顶仍然看不到二维码）。
  var target = document.getElementById('qrbox') || panel;
  target.scrollIntoView({block: 'center', behavior: frozen ? 'auto' : 'smooth'});
  if (frozen) return;
  panel.classList.remove('is-spotlight');
  void panel.offsetWidth;
  panel.classList.add('is-spotlight');
  window.setTimeout(function () { panel.classList.remove('is-spotlight'); }, 2000);
}
window.pointToConnect = pointToConnect;
function refreshQr() { var image = document.getElementById('qr'); var note = document.getElementById('qr-note'); if (!image) return; image.hidden = true; if (!napcatRunning) { applyNapcatQrState(false); return; } image.onload = function () { image.hidden = false; if (note) note.hidden = true; }; image.onerror = function () { image.hidden = true; var details=document.getElementById('qq-qr-details');if(details)details.open=true;if (note) { note.hidden = false; note.textContent = '二维码暂时取不到。点下方的「启动 NapCat 并获取二维码」重启引擎，就会显示登录二维码。'; } }; image.src = '/api/napcat/qrcode?token=' + encodeURIComponent(setupToken) + '&t=' + Date.now(); }
function showQrAfterAction(){var details=document.getElementById('qq-qr-details');if(details)details.open=true;applyNapcatQrState(true);refreshQr();window.requestAnimationFrame(function(){var box=document.getElementById('qrbox');if(box)box.scrollIntoView({block:'center'});});}
// 二维码只在 NapCat 换了新图时才重取（qr_stamp 变了）。之前每 5 秒无脑重载一次，图没变也在闪。
var lastQrStamp = null;
function refreshQrIfStale(stamp) { stamp = String(stamp || ''); if (stamp === lastQrStamp) return; lastQrStamp = stamp; refreshQr(); }
var qrWasVisible = false;
function checkSetup() { fetch('/api/napcat/status', {headers:{'X-Token':setupToken}}).then(function(r){return r.json().then(function(x){if(x&&typeof x.napcat_installed!=='undefined'){var dir=document.getElementById('napcat-dir');if(dir&&!dir.value)dir.value=x.napcat_root||'';var ne=document.getElementById('napcat-engine');if(ne)ne.textContent=x.napcat_installed?('已找到 NapCat 引擎（程序文件）：'+(x.napcat_root||'')+'。这只是「文件装好了」，还需点「启动并登录」，等接口和登录状态都就绪才能收消息。'):'这台电脑上还没找到 NapCat 引擎：点上面的「自动下载并安装 NapCat」；已经自己装过 NapCat 的，在 .env 里加一行 QQ_DIGEST_NAPCAT_DIR=<它的目录> 再重启。';}if(x&&typeof x.error==='string'&&x.error)x.error=friendlyError(x.error);if(!r.ok)throw Error(x.error||'请求失败');if(!x.ok){var notReady=Error(x.error||'连接不可用');notReady.napcatRunning=!!x.napcat_running;notReady.stage=x.stage||'';notReady.waitingSeconds=Number(x.waiting_seconds||0);throw notReady;}return x;});}).then(function(x){ var s = document.getElementById('status'); var ok = !!(x.online || x.nickname); var qrDetails=document.getElementById('qq-qr-details');if(qrDetails)qrDetails.open=!ok; s.textContent = ok ? 'QQ 已连接：' + (x.nickname || '在线') : '先登录 QQ 才能读取群列表'; document.getElementById('qrbox').hidden = ok; setColdStartQq(ok); applyNapcatQrState(!!x.napcat_running || !!x.online); document.getElementById('groupbox').hidden = false; document.getElementById('groups-message').textContent = ok ? '正在读取群列表…' : '先登录 QQ 才能读取群列表。'; document.getElementById('groups-connect-link').hidden=ok; if (!ok) { qrWasVisible = true; sessionStorage.removeItem('qq_login_reloaded'); refreshQrIfStale(x.qr_stamp); return; } if (qrWasVisible && !sessionStorage.getItem('qq_login_reloaded')) { sessionStorage.setItem('qq_login_reloaded','1'); window.location.reload(); return; } loadGroups(); }).catch(function(e){var qrDetails=document.getElementById('qq-qr-details');if(qrDetails)qrDetails.open=true; var raw = String(e && e.message || e); var waiting = !!(e && e.napcatRunning); var eStage = String(e && e.stage || ''); var eWaited = Math.max(0, Math.round(Number(e && e.waitingSeconds || 0))); applyNapcatQrState(waiting); var refused = /10061|积极拒绝|连接被拒绝|QQ 未登录|Failed to fetch|NetworkError|Load failed/.test(raw); var s = document.getElementById('status'); s.textContent = eStage === 'error' ? 'NapCat 的接口端口（127.0.0.1:3001）在监听，但接口调用失败：这是配置问题，不是没登录，继续扫码解决不了。请检查 token 是否和 NapCat 的 OneBot 配置一致，以及 3001 端口是否被别的程序占用（换端口或关掉占用它的程序），改完点「重启引擎」。' : waiting ? (eStage === 'waiting_qr' ? ('等待 QQ 登录' + (eWaited > 0 ? '（已等 ' + eWaited + ' 秒）' : '') +  '：接口要等登录成功才会开，现在连不上是正常的。用手机 QQ 扫下面的二维码，扫完在手机上点「确认登录」。' +  '二维码一会就过期，图换了就扫最新的那张；等了 90 秒还没动静，点「重启引擎」再扫。') : (eStage === 'stopped' ? 'NapCat 引擎装了但没在运行：点上面的「启动并登录」。' : 'NapCat 正在等你用手机扫码登录。接口（127.0.0.1:3001）要等登录成功才会开，所以现在连不上是正常的——扫下面的二维码就行。二维码一直不变就点「刷新二维码」，还是不变再点「重启引擎」。')) : (refused ? '连不上 NapCat 的接口（127.0.0.1:3001）：它可能还没启动、正在启动，或端口与设置不一致。上面「NapCat 引擎」只表示程序文件已就位，不代表接口已经可用。请点「启动并登录」；若刚启动过，等十几秒再刷新。' : '连接检查失败：' + raw); s.title = raw; document.getElementById('qrbox').hidden = false; document.getElementById('groupbox').hidden = false; document.getElementById('groups-message').textContent = eStage === 'error' ? 'NapCat 接口配置有问题（端口在监听但调不通），先修好才能读取群列表。' : waiting ? '先扫码登录 QQ，登录成功后会自动读取群列表。' : (refused ? '先启动 NapCat 并登录 QQ，才能读取群列表。' : '群列表暂不可用：' + raw); document.getElementById('groups-connect-link').hidden=false; if(waiting){fetch('/api/napcat/doctor',{headers:{'X-Token':setupToken}}).then(function(r){return r.json();}).then(function(d){if(d&&!d.ok&&d.hint)s.textContent=(d.broken?'当前卡在「'+d.broken+'」：':'')+d.hint;}).catch(function(){});} }); }
function showQqMissingGuide(){var box=document.getElementById('install-result');if(!box)return;box.textContent='';var wrap=document.createElement('div');wrap.id='qq-missing-guide';wrap.className='setting-note';var why=document.createElement('p');why.textContent='先装官方 QQ：NapCat 只是一层壳，本身不含 QQ.exe，它是挂在官方 QQ 上的外挂引擎，所以没装 QQ 就没法继续。';var steps=document.createElement('ol');['打开官方 QQ 下载页，装好 QQ NT（装完不用打开 QQ）','回到本页点下面的「重新检测」','检测到 QQ 之后，再点「自动下载并安装 NapCat」'].forEach(function(t){var li=document.createElement('li');li.textContent=t;steps.appendChild(li);});var link=document.createElement('a');link.className='btn primary';link.href='https://im.qq.com/pcqq/index.shtml';link.target='_blank';link.rel='noopener noreferrer';link.textContent='下载官方 QQ';var recheck=document.createElement('button');recheck.type='button';recheck.className='btn';recheck.id='qq-redetect';recheck.textContent='重新检测';recheck.onclick=function(){recheck.disabled=true;recheck.textContent='正在检测…';checkSetup();window.setTimeout(function(){recheck.disabled=false;recheck.textContent='重新检测';},1500);};var manual=document.createElement('details');var summary=document.createElement('summary');summary.textContent='已经装了 QQ 还是提示找不到？';var hint=document.createElement('p');hint.textContent='在 .env 里加一行 QQ_DIGEST_QQ_EXE=<你的 QQ.exe 完整路径>，重启后再点「重新检测」。这只对高级用户有效，普通情况按上面三步做就行。';manual.appendChild(summary);manual.appendChild(hint);wrap.appendChild(why);wrap.appendChild(steps);wrap.appendChild(link);wrap.appendChild(recheck);wrap.appendChild(manual);box.appendChild(wrap);}
function installNapcat(){var b=document.getElementById('install-napcat');b.disabled=true;document.getElementById('install-result').textContent='下载中 / 解压中，请稍候…';setupApi('/api/napcat/install',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'},timeoutMs:0}).then(function(x){if(!x.ok)throw Error(x.error||'安装失败');document.getElementById('install-result').textContent='已安装，点「启动 NapCat 并登录」继续';checkSetup();}).catch(function(e){var installBox=document.getElementById('install-result'); var installMsg=String(e&&e.message||''); if(/未找到电脑版 ?QQ|NapCat Shell/.test(installMsg)){ showQqMissingGuide(); } else { installBox.textContent='安装失败：'+installMsg; }}).then(function(){b.disabled=false;});}
function groupCategoryName(category) { return ({course:'课程通知',activity:'活动通知',market:'交易群',chat:'聊天群',other:'其它'})[category] || '其它'; }
function addGroupRow(list, group, suggested, selected) { var label=document.createElement('label'); label.className='group-row'; label.dataset.suggested=suggested?'true':'false'; label.dataset.search=(group.name+' '+group.group_id).toLocaleLowerCase(); var input=document.createElement('input'); input.type='checkbox'; input.value=group.group_id; input.checked=selected; input.setAttribute('aria-label','订阅 '+group.name+'，群号 '+group.group_id); label.appendChild(input); var meta=document.createElement('span'); meta.className='group-meta'; meta.appendChild(document.createElement('span')).className='group-name'; meta.lastChild.textContent=group.name+'（'+group.group_id+'）'; meta.appendChild(document.createElement('span')).className='category-badge'; meta.lastChild.textContent=groupCategoryName(group.category); if(suggested){meta.appendChild(document.createElement('span')).className='suggest-badge';meta.lastChild.textContent='建议订阅';} if(group.reason){var reason=document.createElement('span');reason.className='suggest-reason';reason.textContent=group.reason;meta.appendChild(reason);} label.appendChild(meta); list.appendChild(label); }
function renderGroupFilter() { var input=document.getElementById('group-search'),query=input.value.trim().toLocaleLowerCase(),details=document.querySelector('#groups details.other-groups'); if(details) details.open=!!query; var rows=document.querySelectorAll('#groups .group-row'); rows.forEach(function(row){row.hidden=!!query&&row.dataset.search.indexOf(query)<0;}); document.querySelectorAll('#groups .group-category').forEach(function(section){section.hidden=!section.querySelector('.group-row:not([hidden])');}); if(details) details.hidden=!details.querySelector('.group-row:not([hidden])'); }
function loadGroups() { var message=document.getElementById('groups-message'),controls=document.getElementById('groupbox-controls');message.textContent='正在读取群列表…';controls.hidden=true;Promise.all([setupApi('/api/groups/suggest'),setupApi('/api/napcat/groups')]).then(function(results){var suggestion=results[0],persisted=results[1];if(!suggestion.ok)throw Error(suggestion.error||'群组建议暂不可用');if(!Array.isArray(suggestion.groups)||!persisted.ok||!Array.isArray(persisted.groups))throw Error('群组数据格式无效');var saved=Object.create(null);persisted.groups.forEach(function(g){if(g&&g.group_id!==undefined&&g.group_id!==null)saved[String(g.group_id)]=g;});var normalized=suggestion.groups.map(function(g){if(!g||g.group_id===undefined||g.group_id===null)throw Error('群组数据缺少群号');return {group_id:String(g.group_id),name:String(g.name||g.group_id),category:['course','activity','market','chat','other'].indexOf(g.category)>=0?g.category:'other',suggested:g.suggested===true&&['course','activity'].indexOf(g.category)>=0,reason:String(g.reason||'')};});var ids=Object.create(null);normalized.forEach(function(g){ids[g.group_id]=true;});Object.keys(saved).forEach(function(id){if(!ids[id])normalized.push({group_id:id,name:String(saved[id].name||id),category:'other',suggested:false,reason:''});});if(!normalized.length)throw Error('暂未读取到群组，请确认 QQ 已登录后重试。');var root=document.getElementById('groups');root.textContent='';var source=document.getElementById('group-source');source.textContent='';source.hidden=false;source.dataset.source=suggestion.source==='llm'||suggestion.source==='heuristic'?suggestion.source:'unknown';var sourceName=suggestion.source==='llm'?'AI 识别':suggestion.source==='heuristic'?'按关键词识别':'来源未提供';var strong=document.createElement('strong');strong.textContent=sourceName;source.appendChild(strong);if(suggestion.error){var error=document.createElement('span');error.className='group-source-error';error.textContent=' '+String(suggestion.error);source.appendChild(error);}var suggested=normalized.filter(function(g){return g.suggested;});['course','activity'].forEach(function(category){var members=suggested.filter(function(g){return g.category===category;});if(!members.length)return;var section=document.createElement('section');section.className='group-category';var heading=document.createElement('h3');heading.textContent=groupCategoryName(category)+'（'+members.length+'）';section.appendChild(heading);var list=document.createElement('div');list.className='group-list';members.forEach(function(g){var hasSaved=Object.prototype.hasOwnProperty.call(saved,g.group_id)&&Object.prototype.hasOwnProperty.call(saved[g.group_id],'selected');addGroupRow(list,g,true,hasSaved?!!saved[g.group_id].selected:true);});section.appendChild(list);root.appendChild(section);});var others=normalized.filter(function(g){return !g.suggested;});if(others.length){var details=document.createElement('details');details.className='group-category other-groups';var summary=document.createElement('summary');summary.textContent='其它 '+others.length+' 个群（默认不订阅）';details.appendChild(summary);var list=document.createElement('div');list.className='group-list';others.forEach(function(g){var hasSaved=Object.prototype.hasOwnProperty.call(saved,g.group_id)&&Object.prototype.hasOwnProperty.call(saved[g.group_id],'selected');addGroupRow(list,g,false,hasSaved&&!!saved[g.group_id].selected);});details.appendChild(list);root.appendChild(details);}controls.hidden=false;document.getElementById('groupbox').hidden=false;renderGroupFilter();if(!normalized.length){message.textContent='没有读取到群组。先确认 QQ 已登录，再重试。';}else{message.textContent='群组 '+normalized.length+' 个；已订阅状态已载入。';}}).catch(function(e){message.textContent='群列表读取失败：'+e.message;document.getElementById('groups-connect-link').hidden=false;controls.hidden=false;}); }
function runAutoSetup() { var button=document.getElementById('auto-setup'),steps=document.getElementById('steps'),status=document.getElementById('status'); button.disabled=true;steps.textContent='正在检查并配置 NapCat…';setupApi('/api/napcat/autosetup',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'},timeoutMs:0}).then(function(result){var lines=(result.steps||[]).map(function(step){return (step.ok?'✓ ':'! ')+(step.name||'步骤')+(step.detail?'：'+step.detail:'');});steps.textContent=lines.join('；');if(!result.ok)throw new Error(result.error||'一键接入未完成');if(result.waiting_for_login){status.textContent='配置已完成：请用手机 QQ 扫描下面的二维码登录，登录成功后这一页会自己刷新。';checkSetup();showQrAfterAction();}else if(result.restart_required){status.textContent='NapCat 的接入端口没有起来：请点「启动 NapCat 并登录」重试，或稍后再点一次「一键接入」。';steps.textContent+=(steps.textContent?'；':'')+'请重试启动 NapCat。';}else{status.textContent=result.restarted?'NapCat 已重启，请用手机扫描下方二维码重新登录。':'一键接入已完成，请检查登录状态。';checkSetup();showQrAfterAction();}}).catch(function(error){status.textContent='一键接入失败：'+error.message;}).then(function(){button.disabled=false;}); }
function run() { var button=document.getElementById('go'); var chosen=document.querySelector('input[name="login-method"]:checked').value; var uin=chosen==='uin' ? document.getElementById('uin').value.trim() : ''; button.disabled=true; document.getElementById('steps').textContent='正在启动 NapCat…'; setupApi('/api/napcat/launch',{method:'POST',body:JSON.stringify({uin:uin}),headers:{'Content-Type':'application/json'},timeoutMs:120000}).then(function(x){ if(x.already_running){ document.getElementById('steps').textContent='NapCat 进程已经在运行，正在确认接口和登录状态…'; } else if(x.ok){ document.getElementById('steps').textContent='已启动，正在等二维码…'; } else { throw Error(x.error || '启动失败'); } showQrAfterAction(); checkSetup(); }).catch(function(e){document.getElementById('steps').textContent='启动失败：'+e.message;}).then(function(){button.disabled=false;}); }
function runRestart(){ var button=document.getElementById('restart-napcat'),steps=document.getElementById('steps'); button.disabled=true; steps.textContent='正在停掉本安装的 NapCat 并重新启动…'; setupApi('/api/napcat/restart',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'},timeoutMs:120000}).then(function(x){ if(!x.ok) throw Error(x.error||'重启失败'); lastQrStamp=null; steps.textContent=x.waiting_for_login?'引擎已重启，正在等手机扫码登录…':(x.api_up?'引擎已重启并已接通。':'引擎已重启。'); showQrAfterAction(); checkSetup(); }).catch(function(e){steps.textContent='重启失败：'+e.message;}).then(function(){button.disabled=false;}); }

function initOnboarding() {
  var modal=document.getElementById('onboarding');
  var button=document.getElementById('onboarding-accept');
  var close=document.getElementById('onboarding-close');
  if(!modal||!button)return;
  var accepted=false;
  // 这是「风险提示」，不是要用户签的协议：所以只留一个「我知道了」。
  // 从哪条路关掉（按钮 / × / Esc / 点遮罩）都算看过了，都记一次，不会下次再弹；
  // 旧版那两个按钮（一个假装要同意 + 一个「以后再说」）等于不同意也能用，是假门，已删。
  function persistAccept(){
    if(accepted)return;
    accepted=true;
    function send(attempt){
      fetch('/api/onboarding/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(response){if(!response.ok)throw Error('accept failed');}).catch(function(error){
        if(attempt<2){window.setTimeout(function(){send(attempt+1);},2000);}
        else if(window.console&&window.console.warn)window.console.warn('Onboarding acceptance could not be saved:',error);
      });
    }
    send(1);
  }
  function closeOnboarding(){modal.hidden=true;persistAccept();}
  function openOnboarding(){modal.hidden=false;button.disabled=false;}
  if(close)close.onclick=closeOnboarding;
  modal.addEventListener('click',function(event){if(event.target===modal)closeOnboarding();});
  document.addEventListener('keydown',function(event){if(event.key==='Escape'&&!modal.hidden)closeOnboarding();});
  button.onclick=function(){button.disabled=true;closeOnboarding();};
  // 首屏不再自动弹风险提示（用户要求「不要弹窗」）：入口在右栏「关于与支持」的「使用须知」，点了才开。
  var opener=document.getElementById('onboarding-open');
  if(opener)opener.onclick=openOnboarding;
}

// 托管状态胶囊 + 新手引导：让「启动引擎 → 扫码登录 → 选群 → 订阅日历」这条路径自己找上门。
var journeyCalendarReady=false, journeyCalendarChecked=false;
function journeyDismissed(){try{return localStorage.getItem('qq_digest_journey_hidden')==='1';}catch(error){return false;}}
function applyHostingState(status){
  var pill=document.getElementById('host-state'), label=document.getElementById('host-state-label'), quick=document.getElementById('host-quick');
  if(!pill||!quick)return;
  var reachable=!!(status&&status.ok), active=!!(status&&status.hosting_active), running=!!(status&&status.napcat_running)||active, online=!!(status&&status.napcat_online);
  pill.setAttribute('data-state',reachable?(active?'active':(running?'idle':'offline')):'offline');
  if(label){
    if(!reachable)label.textContent='读不到本机服务';
    else if(active)label.textContent=online?'托管中':'托管已开启，等待扫码';
    else if(running)label.textContent=online?'已登录，未开启托管':'引擎已启动，未登录';
    else label.textContent='未托管';
  }
  quick.setAttribute('data-desired',active?'stop':'start');
  quick.textContent=active?'结束托管':'开始托管';
  quick.setAttribute('aria-label',active?'结束托管：停止引擎并恢复电脑版 QQ':'开始托管：启动引擎（按设置会先退出电脑版 QQ）');
  quick.disabled=!reachable;
}
function startHostingAction(desired,trigger){
  var quick=document.getElementById('host-quick');
  if(quick)quick.disabled=true;
  if(trigger&&trigger!==quick)trigger.disabled=true;
  api('/api/hosting/'+desired,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){
    if(result&&result.ok===false)throw new Error(result.error||'托管操作没有生效');
    showFeedback(desired==='start'?(result&&result.napcat_online?'已开始托管：QQ 已连接，可在右栏订阅群。':'已开始托管：引擎已启动，接下来按页面提示完成 QQ 登录。'):'已结束托管：引擎已停止。','success');
    return fetchHostingStatus();
  }).catch(function(error){
    showFeedback('托管操作失败：'+friendlyError((error&&error.message)||error),'error',function(){startHostingAction(desired,trigger);});
  }).then(function(){if(quick)quick.disabled=false; if(trigger&&trigger!==quick)trigger.disabled=false;});
}
function fetchHostingStatus(){
  return api('/api/hosting/status').then(function(status){
    applyHostingState(status);
    if(!journeyCalendarReady&&!journeyCalendarChecked&&!journeyDismissed()&&status&&status.napcat_online&&Number(status.groups_selected||0)>0){
      journeyCalendarChecked=true;
      api('/api/sync/info').then(function(info){journeyCalendarReady=!!(info&&Array.isArray(info.calendars)&&info.calendars.length); renderJourney(status);}).catch(function(){journeyCalendarChecked=false;});
    }
    renderJourney(status);
    return status;
  }).catch(function(){applyHostingState({ok:false}); renderJourney({ok:false}); return null;});
}
function highlightTarget(target){if(!target)return; target.classList.add('journey-target'); window.setTimeout(function(){target.classList.remove('journey-target');},2400); if(target.scrollIntoView)target.scrollIntoView({block:'center',behavior:'smooth'});}
function showSettingsTab(){var button=document.getElementById('tab-settings-button'); if(button)button.click();}
function renderJourney(status){
  var box=document.getElementById('journey'); if(!box)return;
  if(journeyDismissed()){box.hidden=true; return;}
  var reachable=!!(status&&status.ok);
  var steps=[
    {label:'启动引擎',hint:'让 NapCat 代为收发群消息',done:reachable&&(!!status.hosting_active||!!status.napcat_running)},
    {label:'扫码登录',hint:'用手机 QQ 扫二维码',done:reachable&&!!status.napcat_online},
    {label:'选择订阅群',hint:'勾选要转成待办的群',done:reachable&&Number(status.groups_selected||0)>0},
    {label:'订阅日历',hint:'把 .ics 地址加到手机日历',done:journeyCalendarReady}
  ];
  var current=-1;
  steps.forEach(function(step,index){if(current<0&&!step.done)current=index;});
  if(current<0){box.hidden=true; return;}
  box.hidden=false;
  var list=document.getElementById('journey-steps');
  if(list){
    list.textContent='';
    steps.forEach(function(step,index){
      var item=document.createElement('li');
      item.className='journey-step';
      item.dataset.state=step.done?'done':(index===current?'current':'todo');
      var badge=document.createElement('b'); badge.textContent=step.done?'✓':String(index+1);
      var body=document.createElement('span'); body.appendChild(document.createTextNode(step.label));
      var hint=document.createElement('small'); hint.textContent=step.hint; body.appendChild(hint);
      item.appendChild(badge); item.appendChild(body); list.appendChild(item);
    });
  }
  var titles=['先启动引擎，QQ 消息才能被接手','用手机 QQ 扫码登录','挑出要订阅的群','把日历订阅到手机'];
  var texts=[
    '点顶栏「开始托管」启动引擎（按设置会先退出电脑版 QQ，随时可以结束托管恢复）。',
    '用手机 QQ 扫描右栏「QQ 接入」里的二维码；二维码每 5 秒自动刷新。',
    '在右栏「订阅群」里勾选要变成待办的群，然后点「保存订阅」。',
    '在「设置 → iPhone 日历订阅」里选一个地址，用手机相机扫二维码，或复制链接到手机日历订阅。'
  ];
  var labels=['开始托管','去扫码','去选群','去订阅'];
  var title=document.getElementById('journey-title'); if(title)title.textContent='第 '+(current+1)+' 步 · '+titles[current];
  var next=document.getElementById('journey-next'); if(next)next.textContent=texts[current];
  var action=document.getElementById('journey-action');
  if(action){action.textContent=labels[current]; action.dataset.step=String(current);}
}
function initJourney(){
  var action=document.getElementById('journey-action'), dismiss=document.getElementById('journey-dismiss'), pill=document.getElementById('host-state'), quick=document.getElementById('host-quick');
  if(action){
    action.onclick=function(){
      var step=action.dataset.step;
      if(step==='0'){startHostingAction('start',action); return;}
      if(step==='1'){highlightTarget(document.getElementById('qrbox')); return;}
      if(step==='2'){highlightTarget(document.getElementById('groupbox')); return;}
      if(step==='3'){showSettingsTab(); window.setTimeout(function(){highlightTarget(document.getElementById('sync-url'));},160); return;}
      showSettingsTab();
    };
  }
  if(dismiss)dismiss.onclick=function(){try{localStorage.setItem('qq_digest_journey_hidden','1');}catch(error){} var box=document.getElementById('journey'); if(box)box.hidden=true;};
  if(pill)pill.onclick=function(){try{localStorage.removeItem('qq_digest_journey_hidden');}catch(error){} fetchHostingStatus();};
  if(quick)quick.onclick=function(){startHostingAction(quick.getAttribute('data-desired')==='stop'?'stop':'start',quick);};
}