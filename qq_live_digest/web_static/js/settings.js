/* 设置卡（服务设置页）—— 从 page.html 原样搬出，逻辑未改。
   包含：版本更新卡片、学习通 cURL/Cookie 助手、loadSettings（整页设置卡构建）。
   依赖 page.html 里的全局：api、el、token、setupToken、showFeedback、safeUrl、setupApi、
   setupGroupPull、groupCategoryName、groupPull 相关 DOM，以及 guide.js 的 groupCategoryName。 */

// force=true 走 `?refresh=1`：服务端会真的去拉一次（自己带防抖，连点不会连打 GitHub）。

// ---- 版本更新（设置页卡片 + 顶部横幅）----
var updateState = null;
var updateReloadTimer = null;
function updateStatusNode() { return document.getElementById('update-status'); }
function formatMb(bytes) { return (Number(bytes || 0) / 1048576).toFixed(1) + ' MB'; }
function latestTitle(latest) {
  if (!latest) return '';
  var head = latest.tag || latest.version || '';
  var name = latest.name && latest.name !== head ? '：' + latest.name : '';
  var date = latest.published_at ? '（' + String(latest.published_at).slice(0, 10) + ' 发布）' : '';
  return '新版本 ' + head + name + date;
}
function renderUpdateLatest(data) {
  var box = document.getElementById('update-latest');
  if (!box) return;
  var latest = (data && data.latest) || null;
  var pending = (data && data.pending) || null;
  var showLatest = !!(data && data.has_update && latest);
  if (!showLatest && !pending) { box.hidden = true; return; }
  box.hidden = false;
  var title = document.getElementById('update-latest-title');
  if (title) title.textContent = pending ? '更新包已就绪（' + pending.version + '）' : latestTitle(latest);
  var notes = document.getElementById('update-notes');
  if (notes) { notes.hidden = !showLatest; if (showLatest) notes.innerHTML = String(latest.notes_html || ''); }
  var size = document.getElementById('update-size');
  if (size) { size.hidden = !showLatest; if (showLatest) size.textContent = '更新包 ' + formatMb(latest.size) + '，下载地址来自 GitHub。'; }
  var pendingNote = document.getElementById('update-pending-note');
  if (pendingNote) pendingNote.hidden = !pending;
  var link = document.getElementById('update-link');
  if (link) { var u = showLatest ? safeUrl(latest.html_url) : ''; if (u) { link.href = u; link.hidden = false; } else { link.hidden = true; } }
  var applyButton = document.getElementById('update-apply');
  if (applyButton) {
    applyButton.hidden = !showLatest || !!pending;
    applyButton.disabled = !!pending || !!(data.download && (data.download.state === 'downloading' || data.download.state === 'done'));
  }
}
function renderUpdateBanner() {
  var banner = document.getElementById('update-banner');
  if (!banner) return;
  var latest = (updateState && updateState.latest) || null;
  var pending = (updateState && updateState.pending) || null;
  var tag = pending ? String(pending.version || '') : (latest ? String(latest.tag || latest.version || '') : '');
  var seen = '';
  try { seen = localStorage.getItem('seen_update_tag') || ''; } catch (error) { seen = ''; }
  if (!updateState || !tag || (!pending && (!updateState.has_update || seen === tag))) { banner.hidden = true; return; }
  banner.textContent = '';
  var text = document.createElement('span'); text.className = 'announcement-copy';
  text.textContent = pending ? '更新包已就绪（' + tag + '）。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。' : ('发现新版本 ' + tag + '（当前版本 ' + String((updateState.current && (updateState.current.version || updateState.current.tag)) || '未知') + '）' + (latest.name ? '：' + latest.name : ''));
  var actions = document.createElement('div'); actions.className = 'announcement-actions';
  var open = document.createElement('button'); open.type = 'button'; open.className = 'btn'; open.textContent = '点这里更新';
  open.addEventListener('click', function () {
    showSettingsTab();
    var card = document.getElementById('update-card');
    if (card && card.scrollIntoView) card.scrollIntoView({behavior: 'smooth', block: 'start'});
  });
  var close = document.createElement('button'); close.type = 'button'; close.className = 'btn'; close.textContent = '关闭'; close.setAttribute('aria-label', '关闭更新提示');
  close.addEventListener('click', function () { try { localStorage.setItem('seen_update_tag', tag); } catch (error) {} renderUpdateBanner(); });
  actions.appendChild(open); actions.appendChild(close);
  banner.appendChild(text); banner.appendChild(actions); banner.hidden = false;
}
function startUpdateReloadWatch() {
  var status = updateStatusNode();
  if (status) status.textContent = '正在更新，稍后会自动重新打开…（约 10–30 秒，数据不会丢）';
  if (updateReloadTimer) return;
  updateReloadTimer = window.setInterval(function () {
    fetch('/api/health', {cache: 'no-store'}).then(function (response) { if (response.ok) window.location.reload(); }).catch(function () {});
  }, 5000);
}
function applyUpdateState(data) {
  if (!data || typeof data !== 'object') return;
  updateState = data;
  var current = document.getElementById('update-current');
  if (current) current.textContent = '当前版本 ' + ((data.current && (data.current.version || data.current.tag)) || '未知');
  var checked = document.getElementById('update-checked');
  if (checked) checked.textContent = data.checked_at ? ('上次检查：' + data.checked_at + (data.channel ? '（' + data.channel + ' 通道）' : '')) : '';
  var status = updateStatusNode();
  if (status) {
    if (data.error) status.textContent = '检查更新失败：' + data.error + '。请稍后重试；群务台仍可正常使用。';
    else if (data.pending) status.textContent = '已就绪，等待重启。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。';
    else if (data.download && data.download.state === 'done') status.textContent = '已就绪，等待重启。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。';
    else if (data.notice) status.textContent = data.notice;
    else if (data.checking) status.textContent = '正在检查更新…';
    else if (!data.has_update) status.textContent = '已经是最新版本。';
    else status.textContent = '';
  }
  renderUpdateLatest(data);
  renderUpdateBanner();
  if (data.checking) window.setTimeout(function () { refreshUpdate(false); }, 1500);
  if (data.download && data.download.state === 'downloading' && !updateDownloadAbandoned) updatePollTimer = window.setTimeout(pollUpdateDownload, UPDATE_POLL_INTERVAL_MS);
  if (data.applying) startUpdateReloadWatch();
}
function refreshUpdate(force) {
  return api('/api/update' + (force ? '?check=1' : '')).then(function (data) { applyUpdateState(data); }).catch(function (error) {
    var status = updateStatusNode();
    if (status) status.textContent = '检查更新失败：' + ((error && error.message) || error) + '（不影响使用）';
    return null;
  });
}
// A3：下载轮询上限——连续无进展或总时长超阈值就停，别让按钮永久卡在 disabled。
var UPDATE_POLL_INTERVAL_MS = 700;
var UPDATE_POLL_MAX_MISSES = 90;   // 约 63 秒零字节进展
var UPDATE_POLL_MAX_MS = 300000;   // 总时长 5 分钟兜底
var updatePollTimer = null;
var updateDownloadPoll = null;
var updateDownloadAbandoned = false;
function stopUpdateDownloadPolling() {
  if (updatePollTimer) { window.clearTimeout(updatePollTimer); updatePollTimer = null; }
  updateDownloadPoll = null;
  var abandonButton = document.getElementById('update-abandon');
  if (abandonButton) abandonButton.hidden = true;
}
function abandonUpdateDownload() {
  updateDownloadAbandoned = true;
  stopUpdateDownloadPolling();
  var button = document.getElementById('update-apply');
  if (button) button.disabled = false;
  var status = updateStatusNode();
  if (status) status.textContent = '已放弃下载。可重新点「下载更新包」重试。';
}
function pollUpdateDownload() {
  return api('/api/update').then(function (data) {
    var download = (data && data.download) || {};
    var status = updateStatusNode();
    if (download.state === 'downloading') {
      var now = Date.now();
      if (!updateDownloadPoll) updateDownloadPoll = {startedAt: now, lastDone: -1, misses: 0};
      if (download.done > updateDownloadPoll.lastDone) {
        updateDownloadPoll.lastDone = download.done;
        updateDownloadPoll.misses = 0;
      } else {
        updateDownloadPoll.misses += 1;
      }
      if (updateDownloadPoll.misses >= UPDATE_POLL_MAX_MISSES || now - updateDownloadPoll.startedAt >= UPDATE_POLL_MAX_MS) {
        stopUpdateDownloadPolling();
        var timeoutButton = document.getElementById('update-apply');
        if (timeoutButton) timeoutButton.disabled = false;
        if (status) status.textContent = '下载超时，可重新点「下载更新包」重试。';
        return null;
      }
      var abandonButton = document.getElementById('update-abandon');
      if (abandonButton) abandonButton.hidden = false;
      var percent = download.total ? Math.round(download.done * 100 / download.total) : 0;
      if (status) status.textContent = '正在下载… ' + formatMb(download.done) + ' / ' + (download.total ? formatMb(download.total) : '未知') + '（' + percent + '%）';
      updatePollTimer = window.setTimeout(pollUpdateDownload, UPDATE_POLL_INTERVAL_MS);
      return null;
    }
    stopUpdateDownloadPolling();
    if (download.state === 'error') {
      var button = document.getElementById('update-apply');
      if (button) button.disabled = false;
      if (status) status.textContent = '下载失败：' + (download.error || '未知错误') + '。请检查网络后重试。';
      return null;
    }
    if (download.state !== 'done') {
      var abnormalButton = document.getElementById('update-apply');
      if (abnormalButton) abnormalButton.disabled = false;
      if (status) status.textContent = '下载状态异常。请重新检查更新并重试。';
      return null;
    }
    applyUpdateState(data);
    if (status) status.textContent = '已就绪，等待重启。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。';
    return null;
  }).catch(function (error) {
    stopUpdateDownloadPolling();
    var failedButton = document.getElementById('update-apply');
    if (failedButton) failedButton.disabled = false;
    var status = updateStatusNode();
    if (status) status.textContent = '读取下载进度失败：' + ((error && error.message) || error) + '。请稍后重试下载。';
    return null;
  });
}
function setupUpdateCard() {
  var checkButton = document.getElementById('update-check');
  var applyButton = document.getElementById('update-apply');
  var abandonButton = document.getElementById('update-abandon');
  if (abandonButton) abandonButton.addEventListener('click', abandonUpdateDownload);
  var updateCard = document.getElementById('update-card');
  var selectedChannel = updateCard && updateCard.dataset.updateChannel;
  var channelInputs = document.querySelectorAll('input[name="update-channel"]');
  Array.prototype.forEach.call(channelInputs, function (input) { input.checked = input.value === selectedChannel; });
  var channelSave = document.getElementById('update-channel-save');
  if (channelSave) channelSave.addEventListener('click', function () {
    var chosen = document.querySelector('input[name="update-channel"]:checked');
    var result = document.getElementById('update-channel-result');
    if (!chosen) return;
    channelSave.disabled = true;
    result.textContent = '正在保存…';
    api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({update_channel: chosen.value})}).then(function (payload) {
      if (!payload.ok) throw new Error(payload.error || '保存失败');
      updateCard.dataset.updateChannel = payload.update_channel;
      result.textContent = '已保存。';
    }).catch(function (error) {
      result.textContent = '保存失败：' + ((error && error.message) || error);
    }).then(function () { channelSave.disabled = false; });
  });
  if (checkButton) checkButton.addEventListener('click', function () {
    checkButton.disabled = true;
    var status = updateStatusNode(); if (status) status.textContent = '正在检查更新…';
    refreshUpdate(true).then(function () { checkButton.disabled = false; });
  });
  if (applyButton) applyButton.addEventListener('click', function () {
    var status = updateStatusNode();
    if (!window.confirm('现在只下载更新包，不会立即安装。下载后请点托盘「重启并更新」。继续下载？')) return;
    applyButton.disabled = true;
    if (status) status.textContent = '正在下载更新包…';
    api('/api/update/download', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}', timeoutMs: 120000}).then(function (result) {
      if (!result.ok) throw new Error(result.error || '下载启动失败');
      updateDownloadAbandoned = false;
      stopUpdateDownloadPolling();
      pollUpdateDownload();
    }).catch(function (error) {
      applyButton.disabled = false;
      if (status) status.textContent = '更新失败：' + ((error && error.message) || error);
    });
  });
  if (updateState) applyUpdateState(updateState);
}
var dingtalkScanTimer = null;
var wechatScanTimer = null;
// W6：cURL 粘贴提取与手动保存需要在全局（loadSettings 属于第一个 script 的全局作用域）。
// 注意：页尾还有第二个 <script> 自带 IIFE（platform setup 向导区），函数定义放它里面出不来。
function cxBrowserName(source){return ({edge:'Edge',chrome:'Chrome',brave:'Brave',vivaldi:'Vivaldi',opera:'Opera',firefox:'Firefox'})[source]||source||'浏览器';}
function extractCookieFromCurl(text){
  var src=String(text||'').replace(/\\\r?\n/g,' ');
  if(!/^\s*curl(?:\.exe)?[\s"']/i.test(src))return {looksLikeCurl:false,found:false,cookie:''};
  var re=/(?:^|\s)(?:-H|--header)\s*('([^']*)'|"((?:[^"\\]|\\.)*)"|([^\s]+))/g;
  var match;
  while((match=re.exec(src))){
    var header=match[2]!==undefined?match[2]:(match[3]!==undefined?match[3].replace(/\\"/g,'"'):match[4]);
    if(!/^\s*cookie\s*:/i.test(header))continue;
    return {looksLikeCurl:true,found:true,cookie:header.replace(/^\s*cookie\s*:\s*/i,'').replace(/\s+$/,'')};
  }
  return {looksLikeCurl:true,found:false,cookie:''};
}
function handleCurlPaste(event){
  var input=document.getElementById('cx-manual-cookie');
  var result=document.getElementById('cx-manual-cookie-result');
  if(!input||!result)return;
  var text=(event&&event.clipboardData&&event.clipboardData.getData)?event.clipboardData.getData('text'):'';
  if(!text)return;
  var parsed=extractCookieFromCurl(text);
  if(!parsed.looksLikeCurl)return;
  if(event&&event.preventDefault)event.preventDefault();
  if(!parsed.found){result.textContent='这段内容里没有 Cookie，没能提取。请按 F12 → Network → 右键任意 chaoxing.com 请求 → Copy → Copy as cURL 后再粘贴。';return;}
  var current=String(input.value||'').replace(/^\s+|\s+$/g,'');
  if(current&&current!==parsed.cookie){result.textContent='输入框已有 Cookie，未自动覆盖：要换成 cURL 里的 Cookie，请先清空输入框再粘贴。';return;}
  input.value=parsed.cookie;
  result.textContent='已从 cURL 中提取 Cookie，请确认后保存。';
}
function cxManualNextStep(){return '按 F12 → Network → 选任意 chaoxing.com 请求 → 右键 Copy → Copy as cURL → 粘贴到下方「高级：手动粘贴 Cookie」，粘贴时会自动提取 Cookie。';}

function loadSettings() {

  if (dingtalkScanTimer !== null) { clearInterval(dingtalkScanTimer); dingtalkScanTimer = null; }
  if (wechatScanTimer !== null) { clearInterval(wechatScanTimer); wechatScanTimer = null; }
  var root = document.getElementById('tab-settings');
  root.textContent = '';
  var announcementCard = document.createElement('section'); announcementCard.className = 'surface';
  var announcementHeading = document.createElement('div'); announcementHeading.className = 'panel-heading';
  var announcementTitle = document.createElement('h2'); announcementTitle.textContent = '服务公告'; announcementHeading.appendChild(announcementTitle);
  announcementCard.appendChild(announcementHeading);
  var announcementList = document.createElement('div'); announcementList.id = 'announce-list'; announcementList.className = 'announcement-list'; announcementCard.appendChild(announcementList);
  root.appendChild(announcementCard);var setupCard=el('section','surface hosting-settings');setupCard.id='platform-setup-settings';setupCard.innerHTML='<h2 class="section-title">首次配置</h2><p class="setting-note">重新检查平台接入状态，继续尚未完成的配置。</p><button id="platform-setup-rerun" class="btn" type="button">重新运行首次向导</button>';root.appendChild(setupCard); renderAnnouncementList();
  var updateCard = el('section', 'surface update-card');
  updateCard.id = 'update-card';
  updateCard.dataset.updateChannel = '<!--UPDATE_CHANNEL-->';
   updateCard.innerHTML = '<div class="section-head"><h2 class="section-title">版本更新</h2><span id="update-current" class="count-pill">读取中…</span></div><p id="update-checked" class="setting-note" role="status" aria-live="polite"></p><fieldset class="login-choice"><legend>更新通道</legend><label><input type="radio" name="update-channel" value="stable"> 正式版（稳定）</label><label><input type="radio" name="update-channel" value="beta"> 测试版（尝鲜）</label></fieldset><button id="update-channel-save" class="btn" type="button">保存通道</button><p id="update-channel-result" class="setting-note" role="status" aria-live="polite"></p><div class="update-actions"><button id="update-check" class="btn" type="button">检查更新</button><button id="update-apply" class="btn primary" type="button" hidden>下载更新包</button><button id="update-abandon" class="btn" type="button" hidden>放弃本次下载</button></div><div id="update-latest" class="update-latest" hidden><p id="update-latest-title" class="update-latest-title"></p><div id="update-notes" class="update-notes"></div><p id="update-size" class="setting-note"></p><p id="update-pending-note" class="setting-note" hidden>更新包已就绪。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。</p><p class="setting-note">更新包下载好后不会自己装；点托盘『重启并更新』才会换上新版，你的数据和订阅不会丢。</p><a id="update-link" class="setting-note" target="_blank" rel="noopener noreferrer" hidden>查看完整更新说明 ↗</a></div><p id="update-status" class="update-status" role="status" aria-live="polite"></p>';
  root.appendChild(updateCard);
  setupUpdateCard();
  function renderSuggestionSource(id, source, error) { var box=document.getElementById(id);box.textContent='';box.hidden=false;box.dataset.source=source==='llm'||source==='heuristic'?source:'unknown';var strong=document.createElement('strong');strong.textContent=source==='llm'?'AI 识别':source==='heuristic'?'按关键词识别':'来源未提供';box.appendChild(strong);if(error){var note=document.createElement('span');note.textContent=' '+error;box.appendChild(note);} }
  function appendRecommendation(meta, row) { var category=document.createElement('span');category.className='category-badge';category.textContent=groupCategoryName(row.category);meta.appendChild(category);if(row.suggested){var badge=document.createElement('span');badge.className='suggest-badge';badge.textContent='建议订阅';meta.appendChild(badge);}if(row.reason){var reason=document.createElement('span');reason.className='suggest-reason';reason.textContent=row.reason;meta.appendChild(reason);} }
  var syncBox = el('section', 'surface sync-card');
  syncBox.setAttribute('aria-labelledby', 'sync-title');
  syncBox.innerHTML='<div class="section-head"><h2 id="sync-title" class="section-title">iPhone 日历订阅</h2><span id="sync-events" class="count-pill">读取事项数…</span></div><div class="sync-layout"><div class="sync-qr-wrap"><img id="sync-qr" class="sync-qr" alt="iPhone 日历订阅二维码" hidden><p id="sync-qr-message" class="sync-status" role="status" aria-live="polite">正在生成二维码…</p></div><div class="sync-copy"><label for="sync-url">订阅地址</label><input id="sync-url" class="sync-url" type="url" autocomplete="url" spellcheck="false" aria-describedby="sync-events sync-help"><div class="sync-actions"><button id="sync-copy" class="btn" type="button">复制订阅链接</button><button id="sync-test" class="btn" type="button">测试地址</button><button id="sync-refresh" class="btn" type="button">刷新二维码</button></div><p id="sync-public-note" class="setting-note" hidden></p><p id="sync-public-actions" hidden><button id="sync-public-open" class="btn" type="button">开通公网访问</button></p><p id="sync-alts" class="sync-alts" hidden></p><p id="sync-help">日历按 iPhone 的计划刷新，不是实时推送；可在“设置 → 日历 → 账户 → 已订阅的日历”调整刷新频率，也可在日历 App 下拉刷新。手机需能访问此地址（同一 Wi-Fi 或公网地址）。订阅地址包含访问令牌，令牌轮换后需重新订阅。</p><p id="sync-status" class="sync-status" role="status" aria-live="polite"></p></div></div>';
  root.appendChild(syncBox);
   var backupCard=el('section','surface hosting-settings');
   backupCard.id='backup-card';
   backupCard.innerHTML='<h2 class="section-title">数据备份</h2><p class="setting-note">仅导出本机待办数据库，不包含令牌、配置文件或日志。</p><button id="backup-download" class="btn" type="button">备份数据</button><p id="backup-result" class="setting-note" role="status" aria-live="polite"></p>';
   root.appendChild(backupCard);
   document.getElementById('backup-download').onclick=function(){
     var button=this;var result=document.getElementById('backup-result');button.disabled=true;result.textContent='正在生成备份…';
     fetch('/api/backup',{headers:{'X-Token':token},cache:'no-store'}).then(function(response){
       if(!response.ok)return response.json().catch(function(){return {};}).then(function(body){throw new Error(body.error||'备份请求失败');});
       return response.blob();
     }).then(function(blob){
       var now=new Date();var date=now.getFullYear()+'-'+('0'+(now.getMonth()+1)).slice(-2)+'-'+('0'+now.getDate()).slice(-2);
       var url=URL.createObjectURL(blob);var link=document.createElement('a');link.href=url;link.download='notice-hub-backup-'+date+'.zip';document.body.appendChild(link);link.click();link.remove();window.setTimeout(function(){URL.revokeObjectURL(url);},1000);result.textContent='备份下载已开始。';
     }).catch(function(error){result.textContent='备份失败：'+error.message;}).then(function(){button.disabled=false;});
   };
  var cxBox = el('section', 'surface hosting-settings');cxBox.id='chaoxing-settings-card';
   cxBox.innerHTML='<h2 class="section-title">学习通（实验性）</h2><p class="setting-note">仅读取通知；Cookie 只保存于本机数据目录，不会写入 .env。</p><label class="preference"><input id="cx-enabled" type="checkbox"><span><strong>启用学习通</strong><small>仅处理已勾选课程的通知。</small></span></label><div id="cx-cookie-methods"><section id="cx-auto-option" class="field"><strong>方式一：浏览器自动获取 <span class="count-pill">推荐</span></strong><ol class="setting-note"><li>① 用 Edge、Chrome 或其他 Chromium 浏览器登录 chaoxing.com <span id="cx-step-login-state" class="count-pill">待操作</span></li><li>② 保持本页打开，系统会复制 Cookie 数据库及日志副本后读取；若浏览器独占文件，会提示下一步 <span id="cx-step-exit-state" class="count-pill">待操作</span></li><li>③ 点“重试自动获取” <span id="cx-step-retry-state" class="count-pill">待操作</span></li></ol><a class="btn" href="https://i.chaoxing.com/" target="_blank" rel="noopener noreferrer">打开学习通登录页</a><button id="cx-auto-cookie" class="btn" type="button">重试自动获取</button><p id="cx-auto-result" class="setting-note" role="status" aria-live="polite">尚未开始自动检查。</p><ul id="cx-auto-attempts" class="setting-note" aria-label="浏览器检查结果"></ul></section><p class="setting-note">首选浏览器自动读取；失败时用托盘右键「读取学习通 Cookie」；仍失败可展开「高级：手动粘贴 Cookie」。手工 Cookie 可能缺少 HttpOnly 项。</p></div><div class="field"><span>我的课程</span><button id="cx-load-courses" class="btn" type="button">拉取我的课程</button><div id="cx-courses" class="setting-note">请先配置 Cookie。</div></div><button id="cx-save" class="btn primary" type="button">保存学习通设置</button><p id="cx-status" class="setting-note" role="status" aria-live="polite">正在读取状态…</p><button id="cx-validate" class="btn" type="button">立即校验 Cookie</button><p id="cx-cookie-state" class="setting-note" role="status" aria-live="polite"></p><details id="cx-manual-option"><summary>高级：手动粘贴 Cookie</summary><label class="field" for="cx-manual-cookie"><span>学习通 Cookie</span><textarea id="cx-manual-cookie" rows="5" autocomplete="off" spellcheck="false" placeholder="粘贴 Cookie 内容"></textarea></label><button id="cx-manual-cookie-save" class="btn" type="button">保存并校验</button><p id="cx-manual-cookie-result" class="setting-note" role="status" aria-live="polite"></p></details><p id="cx-result" class="setting-note" role="status"></p>';
   root.appendChild(cxBox);
   // 手动 Cookie 区（卡片此刻才存在）：粘贴 cURL 自动提取 + 保存按钮接线。
   var manualCookieInput=document.getElementById('cx-manual-cookie');
   if(manualCookieInput)manualCookieInput.addEventListener('paste',handleCurlPaste);
   var manualCookieButton=document.getElementById('cx-manual-cookie-save');
   if(manualCookieButton)manualCookieButton.onclick=function(){
  var input=document.getElementById('cx-manual-cookie');
  var result=document.getElementById('cx-manual-cookie-result');
  var cookie=String(input.value||'').replace(/\r/g,'').replace(/\n/g,'').replace(/^\s*Cookie:\s*/i,'').trim();
  if(!cookie){result.textContent='请粘贴学习通 Cookie。';return;}
  manualCookieButton.disabled=true;result.textContent='正在保存并校验…';
  api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chaoxing_cookie:cookie})})
    .then(function(saved){if(!saved.ok)throw new Error(saved.error||'Cookie 保存失败');return api('/api/chaoxing/cookie/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});})
    .then(function(validation){if(validation.ok){result.textContent='Cookie 保存并校验通过。';input.value='';refreshChaoxingCookie();}else{result.textContent=String(validation.error||'Cookie 校验失败');}})
    .catch(function(error){result.textContent=String(error&&error.message||error);})
    .then(function(){manualCookieButton.disabled=false;});
};
   var dtBox=el('section','surface hosting-settings dingtalk-settings');dtBox.id='dingtalk-settings-card';
    dtBox.innerHTML='<h2 class="section-title">钉钉（实验性）</h2><p class="setting-note">只读本机钉钉数据库；仅处理勾选群聊。</p><label class="preference"><input id="dt-enabled" type="checkbox"><span><strong>启用钉钉</strong><small>需要 Windows 上钉钉已登录。</small></span></label><p id="dt-status" class="setting-note" role="status" aria-live="polite">正在检测…</p><p class="setting-note"><button id="dt-reconnect" class="btn" type="button" hidden>重连监听</button></p><p id="dt-scan" class="setting-note" role="status" aria-live="polite"></p><button id="dt-scan-now" class="btn" type="button">立即扫描一次</button><div class="field"><span>我的群</span><button id="dt-load-groups" class="btn" type="button">拉取我的群</button><button id="dt-cancel-groups" class="btn" type="button" hidden>取消拉取</button><button id="dt-select-suggested" class="btn" type="button">全选建议</button><div id="dt-suggest-source" class="group-source" hidden></div><div id="dt-groups" class="setting-note">点击拉取钉钉群。</div></div><button id="dt-save" class="btn primary" type="button">保存钉钉设置</button><p id="dt-result" class="setting-note" role="status" aria-live="polite"></p>';
    root.appendChild(dtBox);
    var dtSelected=[];var dtLoaded=false;
    function renderDingtalkGroups(payload){dtLoaded=true;var rows=payload.groups||[],box=document.getElementById('dt-groups');box.textContent='';renderSuggestionSource('dt-suggest-source',payload.suggest_source,payload.suggest_error);rows.forEach(function(row){var label=document.createElement('label');label.className='preference dt-group-row';label.dataset.suggested=row.suggested?'true':'false';var cid=String(row.cid||'');var input=document.createElement('input');input.type='checkbox';input.value=cid;input.checked=dtSelected.indexOf(cid)>=0;var meta=document.createElement('span');meta.className='group-meta';var name=document.createElement('span');name.className='group-name';name.textContent=String(row.title||cid)+' ('+cid+') · '+String(row.members||0)+' 人';meta.appendChild(name);appendRecommendation(meta,row);label.appendChild(input);label.appendChild(meta);box.appendChild(label);});if(!rows.length)box.textContent='没有解析到群。';}
    api('/api/settings').then(function(v){document.getElementById('dt-enabled').checked=!!v.dingtalk_enabled;dtSelected=v.dingtalk_group_whitelist||[];});
    function updateScanStatus(source){api('/api/'+source+'/status').then(function(v){var target=document.getElementById(source==='dingtalk'?'dt-scan':'wx-scan');if(!target)return;if(v.scan_state==='running')target.textContent='⟳ '+(v.scan_stage||'正在扫描…')+' 已用 '+Number(v.scan_elapsed_seconds||0).toFixed(1)+' 秒';else if(v.scan_state==='error')target.textContent='扫描失败：'+(v.error_hint||v.last_error||v.scan_stage||'未知错误');else if(v.last_read_at)target.textContent='最近读取：'+v.last_read_at+(v.last_read_seconds?'，用时 '+Number(v.last_read_seconds).toFixed(1)+' 秒':'');else target.textContent=v.scan_state==='disabled'?'扫描未启用':'等待扫描';}).catch(function(e){var target=document.getElementById(source==='dingtalk'?'dt-scan':'wx-scan');if(target)target.textContent='状态读取失败：'+e.message;});}
 function refreshDingtalkStatus(){api('/api/dingtalk/status').then(function(v){var node=document.getElementById('dt-status');node.textContent=v.state||'状态未知';node.dataset.state=v.running?'ok':(v.enabled?'warn':'');var reconnect=document.getElementById('dt-reconnect');if(reconnect)reconnect.hidden=!(v.enabled&&!v.running&&Number(v.groups||0)>0);updateScanStatus('dingtalk');}).catch(function(e){document.getElementById('dt-status').textContent='状态读取失败：'+e.message;});}
    refreshDingtalkStatus();
    setupGroupPull({path:'/api/dingtalk/groups',boxId:'dt-groups',buttonId:'dt-load-groups',cancelButtonId:'dt-cancel-groups',idleButtonText:'拉取我的群',busyButtonText:'正在拉取…',busyCopy:'正在读取钉钉群…',longWaitCopy:function(){return '首次读取可能较久，正在扫描数据库…';},render:renderDingtalkGroups});
     document.getElementById('dt-select-suggested').onclick=function(){document.querySelectorAll('#dt-groups .dt-group-row[data-suggested="true"] input').forEach(function(input){input.checked=true;});};
document.getElementById('dt-scan-now').onclick=function(){var button=this;button.disabled=true;api('/api/dingtalk/scan',{method:'POST',body:'{}'}).then(function(v){if(!v.ok)throw new Error(v.error||'扫描失败');updateScanStatus('dingtalk');}).catch(function(e){document.getElementById('dt-scan').textContent='扫描启动失败：'+e.message;}).then(function(){button.disabled=false;});}; dingtalkScanTimer = setInterval(function(){updateScanStatus('dingtalk');},1500);
// 「重连监听」与「保存钉钉设置」走同一条链路：重新提交当前配置，后端就会重连监听。
function saveDingtalkSettings(){var button=document.getElementById('dt-save');if(button)button.disabled=true;var groups=Array.prototype.map.call(document.querySelectorAll('#dt-groups input:checked'),function(x){return x.value;});api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dingtalk_enabled:document.getElementById('dt-enabled').checked,dingtalk_group_whitelist:dtLoaded?groups:dtSelected})}).then(function(v){if(!v.ok)throw new Error(v.error||'保存失败');dtSelected=dtLoaded?groups:dtSelected;document.getElementById('dt-result').textContent='已保存，正在重新连接监听…';refreshDingtalkStatus();}).catch(function(e){document.getElementById('dt-result').textContent='保存失败：'+e.message;}).then(function(){if(button)button.disabled=false;});}
document.getElementById('dt-save').onclick=saveDingtalkSettings;
document.getElementById('dt-reconnect').onclick=saveDingtalkSettings;
    var wxBox=el('section','surface hosting-settings wechat-settings');wxBox.id='wechat-settings-card';
    wxBox.innerHTML='<h2 class="section-title">微信（实验性，默认关闭）</h2><p class="setting-note">读取本机微信数据库，只读不发送；取解密密钥需要扫描微信进程内存，首次通常 3–6 分钟（本机实测 190–390 秒），读出密钥后会缓存，之后再拉是几秒。可能触发风控，请自行评估。</p><label class="preference"><input id="wx-enabled" type="checkbox"><span><strong>启用微信</strong><small>需要 Windows 上微信已登录。</small></span></label><p id="wx-status" class="setting-note" role="status" aria-live="polite">正在检测…</p><p id="wx-scan" class="setting-note" role="status" aria-live="polite"></p><button id="wx-scan-retry" class="btn" type="button" hidden>重试扫描</button><button id="wx-scan-now" class="btn" type="button">立即扫描一次</button><div class="field"><span>我的群</span><button id="wx-load-groups" class="btn" type="button">拉取我的群</button><button id="wx-cancel-groups" class="btn" type="button" hidden>取消拉取</button><button id="wx-select-suggested" class="btn" type="button">全选建议</button><div id="wx-suggest-source" class="group-source" hidden></div><div id="wx-groups" class="setting-note">点击拉取微信群（首次可能要几分钟）。</div></div><label class="field"><span>扫描间隔（秒）</span><input id="wx-interval" type="number" min="30" step="30" inputmode="numeric"></label><button id="wx-save" class="btn primary" type="button">保存微信设置</button><p id="wx-result" class="setting-note" role="status" aria-live="polite"></p>';
    root.appendChild(wxBox);
    var wxSelected=[];var wxLoaded=false;var wxAliases={};
    function renderWechatGroups(payload){wxLoaded=true;var rows=payload.groups||[],box=document.getElementById('wx-groups');box.textContent='';renderSuggestionSource('wx-suggest-source',payload.suggest_source,payload.suggest_error);rows.forEach(function(row){var id=String(row.id||'');var wrap=document.createElement('div');wrap.className='preference wx-group-row';wrap.dataset.suggested=row.suggested?'true':'false';var label=document.createElement('label');label.className='preference';var input=document.createElement('input');input.type='checkbox';input.value=id;input.checked=wxSelected.indexOf(id)>=0;var meta=document.createElement('span');meta.className='group-meta';var name=document.createElement('span');name.className='group-name';name.textContent=String(row.name||id)+' ('+id+')';meta.appendChild(name);appendRecommendation(meta,row);label.appendChild(input);label.appendChild(meta);var alias=document.createElement('input');alias.type='text';alias.className='wx-alias';alias.placeholder='别名（可选）';alias.setAttribute('data-group',id);alias.value=wxAliases[id]||row.alias||'';wrap.appendChild(label);wrap.appendChild(alias);box.appendChild(wrap);});if(!rows.length)box.textContent='没有解析到群。';}
    function refreshWechatStatus(){api('/api/wechat/status').then(function(v){updateScanStatus('wechat');var node=document.getElementById('wx-status');var parts=[v.state||'状态未知'];var groups=Number(v.groups||0);if(!v.running&&groups>0)parts.push('已选 '+groups+' 个群');if(v.last_read_at)parts.push('上次读取 '+v.last_read_at+'（'+v.last_read_seconds+'s）');var failed=v.scan_state==='error';if(failed&&v.error_hint)parts.push(v.error_hint);node.textContent=parts.join(' · ');node.dataset.state=failed?'error':(v.running?'ok':'');if(v.last_error)node.title='原始报错：'+v.last_error;else node.removeAttribute('title');var retry=document.getElementById('wx-scan-retry');if(retry)retry.hidden=!failed;}).catch(function(e){document.getElementById('wx-status').textContent='状态读取失败：'+e.message;});}
    api('/api/settings').then(function(v){document.getElementById('wx-enabled').checked=!!v.wechat_enabled;wxSelected=v.wechat_group_whitelist||[];wxAliases=v.wechat_group_aliases||{};document.getElementById('wx-interval').value=v.wechat_scan_interval_seconds||300;});
     function saveEnabledToggle(inputId,resultId,key,after){var input=document.getElementById(inputId),result=document.getElementById(resultId);input.addEventListener('change',function(){var enabled=input.checked;input.disabled=true;var payload={};payload[key]=enabled;api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(v){if(!v.ok)throw new Error(v.error||'保存失败');result.textContent=enabled?'已启用':'已关闭';if(after)after();}).catch(function(e){input.checked=!enabled;result.textContent='保存失败，已恢复原状态：'+e.message;}).then(function(){input.disabled=false;});});}
     saveEnabledToggle('wx-enabled','wx-result','wechat_enabled');
     // 钉钉勾选即存，和微信动作一致：以前只勾不点「保存钉钉设置」，后端仍是未启用，用户读作「未应用」。
     saveEnabledToggle('dt-enabled','dt-result','dingtalk_enabled',refreshDingtalkStatus);
    refreshWechatStatus();
    setupGroupPull({path:'/api/wechat/groups',boxId:'wx-groups',buttonId:'wx-load-groups',cancelButtonId:'wx-cancel-groups',idleButtonText:'拉取我的群',busyButtonText:'正在拉取…',busyCopy:'正在读取微信群…',longWaitCopy:function(seconds){return '正在扫描微信进程内存定位密钥，首次通常 3–6 分钟（已等 '+seconds+' 秒），请别关窗口…';},render:renderWechatGroups,after:refreshWechatStatus});
     document.getElementById('wx-select-suggested').onclick=function(){document.querySelectorAll('#wx-groups .wx-group-row[data-suggested="true"] input[type="checkbox"]').forEach(function(input){input.checked=true;});};
    // 「立即扫描一次」和「重试扫描」是同一条链路：失败了就点重试，不用自己找按钮。
    function scanWechat(){var button=document.getElementById('wx-scan-now');var retry=document.getElementById('wx-scan-retry');if(button)button.disabled=true;if(retry)retry.disabled=true;api('/api/wechat/scan',{method:'POST',body:'{}'}).then(function(v){if(!v.ok)throw new Error(v.error||'扫描失败');document.getElementById('wx-scan').textContent='正在扫描…';updateScanStatus('wechat');}).catch(function(e){document.getElementById('wx-scan').textContent='扫描启动失败：'+e.message;}).then(function(){if(button)button.disabled=false;if(retry)retry.disabled=false;refreshWechatStatus();});}
    document.getElementById('wx-scan-now').onclick=scanWechat;
    document.getElementById('wx-scan-retry').onclick=scanWechat; wechatScanTimer = setInterval(function(){updateScanStatus('wechat');},1500);
document.getElementById('wx-save').onclick=function(){var groups=Array.prototype.map.call(document.querySelectorAll('#wx-groups input[type=checkbox]:checked'),function(x){return x.value;});var aliases={};Array.prototype.forEach.call(document.querySelectorAll('#wx-groups .wx-alias'),function(x){var val=x.value.trim();if(val)aliases[x.getAttribute('data-group')]=val;});var interval=parseInt(document.getElementById('wx-interval').value,10);if(!interval||interval<30)interval=300;var body={wechat_enabled:document.getElementById('wx-enabled').checked,wechat_scan_interval_seconds:interval,wechat_group_aliases:aliases};if(wxLoaded)body.wechat_group_whitelist=groups;api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(v){if(!v.ok)throw new Error(v.error||'保存失败');if(wxLoaded)wxSelected=groups;wxAliases=aliases;document.getElementById('wx-result').textContent='已保存。';refreshWechatStatus();}).catch(function(e){document.getElementById('wx-result').textContent=e.message;});};    var cxLoaded=false,cxSelected=[];
    function renderCxCourses(rows){cxLoaded=true;var box=document.getElementById('cx-courses');box.textContent='';(rows||[]).forEach(function(row){var label=document.createElement('label');label.className='preference';var input=document.createElement('input');input.type='checkbox';input.value=row.course_id;input.checked=cxSelected.indexOf(row.course_id)>=0;var span=document.createElement('span');span.textContent=row.course_name+' ('+row.course_id+')';label.appendChild(input);label.appendChild(span);box.appendChild(label);});if(!rows||!rows.length)box.textContent='没有解析到课程。';}
    function renderChaoxingStatus(v){var status=document.getElementById('cx-status');if(!status)return;status.textContent='上次同步时间：'+(v.last_sync_at||'暂无')+' · Cookie 状态：'+(v.expired?'已过期，需重新粘贴':(v.has_cookie?'有效':'未配置'));} api('/api/settings').then(function(v){document.getElementById('cx-enabled').checked=!!v.chaoxing_enabled;cxSelected=v.chaoxing_group_whitelist||[];document.getElementById('cx-result').textContent=v.chaoxing_cookie&&v.chaoxing_cookie.has_cookie?'已配置（不显示内容）':'未配置 Cookie';renderChaoxingStatus(v.chaoxing_status||v);});
    saveEnabledToggle('cx-enabled','cx-result','chaoxing_enabled');
    function cxFailureNote(category,source,nextStep){if(category==='decrypt_failed')return chaoxingCategoryHelp(category,source)+' 下一步：'+cxManualNextStep();return chaoxingCategoryHelp(category,source)+(nextStep?' 下一步：'+nextStep:'');}
    function chaoxingCategoryHelp(category,source){var browser=cxBrowserName(source);if(category==='browser_locked')return browser+' 的 Cookie 数据库及副本都无法读取（可能被独占）。请先完全退出它（含托盘和后台进程），再用另一浏览器或手机重新打开群务台设置页后重试。';if(category==='no_chaoxing_cookie')return browser+' 的 Cookie 库里没有 chaoxing.com 登录记录。请先用这个浏览器登录学习通，再重试。';if(category==='decrypt_failed')return '已找到学习通 Cookie，但新版浏览器（Edge / Chrome 127+）把 Cookie 加密了（App-Bound 加密保护 v20），第三方程序无法自动读取，这不是你的配置问题。请改用「复制为 cURL」方式手动粘一次。';return '没有找到 '+browser+' 的可读 Cookie 库。请先安装或启动一次浏览器、登录学习通，再重试。';}
    function renderCxAttempts(rows){var box=document.getElementById('cx-auto-attempts');box.textContent='';(rows||[]).forEach(function(row){var item=document.createElement('li');var text=cxBrowserName(row.source)+'：'+(row.ok?'已找到并读取学习通 Cookie':(row.error||chaoxingCategoryHelp(row.category,row.source)));if(row.detail)text+='（'+row.detail+'）';var rowNextStep=row.category==='decrypt_failed'?cxManualNextStep():row.next_step;if(!row.ok&&rowNextStep)text+=' 下一步：'+rowNextStep;item.textContent=text;box.appendChild(item);});}
    function setCxStep(step, state, text) {
      var p = document.getElementById('cx-step-' + step + '-state');
      if (!p) return;
      p.textContent = text;
      var t = { done: 'var(--teal)', error: 'var(--red)', active: 'var(--amber)' }[state];
      p.style.color = t || '';
      p.style.borderColor = t || '';
    }
    function updateCxGuide(category){if(!category){setCxStep('login','done','已检测到登录态');setCxStep('exit','done','Cookie 数据库副本可读取');setCxStep('retry','done','自动获取成功');return;}if(category==='browser_locked'){setCxStep('login','done','已找到浏览器配置');setCxStep('exit','error','数据库副本也无法读取');setCxStep('retry','pending','换浏览器或手机打开本页后再重试');return;}if(category==='no_chaoxing_cookie'){setCxStep('login','error','未找到登录态');setCxStep('exit','pending','先登录学习通');setCxStep('retry','pending','登录后回本页重试');return;}if(category==='decrypt_failed'){setCxStep('login','done','已找到登录态');setCxStep('exit','error','浏览器已加密（v20），自动读不了');setCxStep('retry','pending','改用手动粘贴 Cookie（见「高级」）');return;}setCxStep('login','pending','先安装浏览器并登录');setCxStep('exit','pending','登录后回本页重试');setCxStep('retry','pending','完成前两步后重试');}
    document.getElementById('cx-auto-cookie').onclick=function(){var button=this;button.disabled=true;var note=document.getElementById('cx-auto-result');var done=function(){button.disabled=false;};try{setCxStep('login','active','请确认已登录');setCxStep('exit','active','正在复制 Cookie 数据库副本');setCxStep('retry','active','正在逐个检查浏览器');note.textContent='正在依次检查 Edge、Chrome 和其他 Chromium 浏览器…';api('/api/chaoxing/auto_cookie',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',timeoutMs:120000}).then(function(v){renderCxAttempts(v.attempts||[]);if(!v.ok){updateCxGuide(v.category||'browser_unavailable');note.textContent=cxFailureNote(v.category,v.source,v.next_step);return;}updateCxGuide('');note.textContent='已从 '+cxBrowserName(v.source)+' 自动获取并验证 Cookie，发现 '+(v.courses||0)+' 门课程。';document.getElementById('cx-result').textContent='已配置（不显示内容）';document.getElementById('cx-enabled').checked=true;api('/api/settings').then(function(s){cxSelected=s.chaoxing_group_whitelist||cxSelected;});}).catch(function(e){updateCxGuide('browser_unavailable');note.textContent='自动读取失败：'+e.message+'。请先用托盘右键「读取学习通 Cookie」；仍失败时可展开「高级：手动粘贴 Cookie」，也可以再试一次。';}).then(done,done);}catch(e){note.textContent='自动读取没能开始，可以再试一次。';done();}};
    document.getElementById('cx-load-courses').onclick=function(){var box=document.getElementById('cx-courses');box.textContent='正在拉取…';api('/api/chaoxing/courses').then(function(v){if(!v.ok)throw new Error(v.error||'课程读取失败');renderCxCourses(v.courses);}).catch(function(e){box.textContent='拉取失败：'+e.message;});};
    document.getElementById('cx-save').onclick=function(){var button=this;var groups=Array.prototype.map.call(document.querySelectorAll('#cx-courses input:checked'),function(x){return x.value;});button.disabled=true;button.textContent='保存中…';document.getElementById('cx-result').textContent='正在保存…';var payload={chaoxing_enabled:document.getElementById('cx-enabled').checked};if(cxLoaded)payload.chaoxing_group_whitelist=groups;Promise.race([api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),new Promise(function(_,reject){setTimeout(function(){reject(new Error('保存超时：请求没有响应，请刷新页面或改用托盘导入'));},20000);})]).then(function(v){if(!v.ok)throw new Error(v.error||'服务端未保存设置');cxSelected=groups;document.getElementById('cx-result').textContent='已保存。';api('/api/chaoxing/status').then(renderChaoxingStatus);}).catch(function(e){document.getElementById('cx-result').textContent=e.message.indexOf('保存超时：')===0?e.message:'保存失败：'+e.message;}).then(function(){button.disabled=false;button.textContent='保存学习通设置';});};

   root.appendChild(pairCard());
  setupPairCard();
  (function setupSyncCard() {
    var input = document.getElementById('sync-url');
    var qrImage = document.getElementById('sync-qr');
    var qrMessage = document.getElementById('sync-qr-message');
    var status = document.getElementById('sync-status');
    var events = document.getElementById('sync-events');
    var copyButton = document.getElementById('sync-copy');
    var testButton = document.getElementById('sync-test');
    function setStatus(message, state) { status.textContent = message; status.dataset.state = state || ''; }
    function setQrMessage(message) { qrMessage.textContent = message; }
    function webcalAddress(raw) {
      var value = String(raw || '').trim();
      var parsed = new URL(value);
      if (!parsed.hostname || parsed.username || parsed.password || !['http:', 'https:', 'webcal:'].includes(parsed.protocol)) throw new Error('请输入包含主机名的 HTTP、HTTPS 或 webcal 订阅地址。');
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') input.dataset.testScheme = parsed.protocol;
      else if (!input.dataset.testScheme) input.dataset.testScheme = 'http:';
      return value.replace(/^https?:/i, 'webcal:');
    }
    function renderSyncQr() {
      try {
        var address = webcalAddress(input.value);
        if (input.value !== address) input.value = address;
        qrMessage.textContent = '';
        qrImage.hidden = false;
        qrImage.onload = function () { setQrMessage(''); };
        qrImage.onerror = function () { qrImage.hidden = true; setQrMessage('二维码生成失败；请检查地址长度后重试。'); };
        qrImage.src = '/api/sync/qr.png?text=' + encodeURIComponent(address) + '&token=' + encodeURIComponent(setupToken) + '&t=' + Date.now();
      } catch (error) {
        qrImage.hidden = true;
        qrImage.removeAttribute('src');
        setQrMessage(error.message || '订阅地址无效。');
      }
    }
    var altList = document.getElementById('sync-alts');
    function renderAddressChoices(calendars, current) {
      var others = (calendars || []).filter(function (item) { return item && item.url && item.url !== current; });
      altList.textContent = '';
      if (!others.length) { altList.hidden = true; return; }
      altList.hidden = false;
      altList.appendChild(document.createTextNode('其他可用地址：'));
      others.forEach(function (item) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn ghost sync-alt';
        button.textContent = item.name || '备用地址';
        button.title = (item.hint ? item.hint + '；' : '') + item.url;
        button.onclick = function () {
          input.value = item.url;
          try { input.dataset.testScheme = new URL(item.url).protocol; } catch (error) { input.dataset.testScheme = ''; }
          renderSyncQr();
          renderAddressChoices(calendars, item.url);
          setStatus('已切换为' + (item.name || '备用地址') + '，二维码同步更新。', '');
        };
        altList.appendChild(button);
      });
    }
    function manualCopy() {
      input.focus();
      input.select();
      try {
        if (document.execCommand('copy')) { setStatus('订阅链接已复制。', 'success'); return; }
      } catch (error) {}
      setStatus('浏览器不允许复制；地址已选中，请手动复制。', 'error');
    }
    copyButton.onclick = function () {
      try { input.value = webcalAddress(input.value); } catch (error) { setStatus(error.message, 'error'); return; }
      renderSyncQr();
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') { manualCopy(); return; }
      try { navigator.clipboard.writeText(input.value).then(function () { setStatus('订阅链接已复制。', 'success'); }, manualCopy); } catch (error) { manualCopy(); }
    };
    testButton.onclick = function () {
      var requestUrl;
      try {
        input.value = webcalAddress(input.value);
        requestUrl = input.value.replace(/^webcal:/i, input.dataset.testScheme || 'http:');
      } catch (error) { setStatus(error.message, 'error'); return; }
      testButton.disabled = true;
      setStatus('正在让本机服务读取这个地址…', '');
      setupApi('/api/sync/test?url=' + encodeURIComponent(requestUrl)).then(function (data) {
        setStatus('本机服务能读取这个地址，包含 ' + data.events + ' 条事项。手机能不能订阅，请用手机打开同一地址确认。', 'success');
      }).catch(function (error) {
        var raw = String((error && error.message) || error || '').replace(/[。.\s]+$/, '');
        setStatus('本机服务读不到这个地址：' + raw + '。点上方其他地址可以换一个，或直接用手机打开这个地址确认。', 'error');
      }).then(function () { testButton.disabled = false; });
    };
    input.addEventListener('input', function () { input.dataset.testScheme = ''; renderSyncQr(); });
    var lastSyncUrl = '';
    // 公网地址排第一是后端给的顺序，但页面别依赖它：手机用流量只能走公网那条。
    function primaryAddress(calendars) {
      var list = (calendars || []).filter(function (item) { return item && item.url; });
      var publicItem = list.filter(function (item) { return item.kind === 'public'; })[0];
      return publicItem || list[0] || null;
    }
    function renderPublicNote(item) {
      var note = document.getElementById('sync-public-note');
      var actions = document.getElementById('sync-public-actions');
      if (!note || !actions) return;
      note.hidden = false;
      if (item && item.kind === 'public') {
        note.textContent = '公网地址 · 手机流量可用（用数据也能订阅和刷新）';
        note.dataset.state = 'ok';
        actions.hidden = true;
        return;
      }
      note.textContent = '现在只有局域网地址：手机切到流量就收不到更新，去开启公网访问。';
      note.dataset.state = 'warn';
      actions.hidden = false;
    }
    var publicPollTimer = null;
    function pollPublicAccess() {
      api('/api/tunnel/status').then(function (snapshot) {
        var state = String((snapshot && snapshot.state) || '');
        if (state === 'ready') {
          publicPollTimer = null;
          setStatus('公网入口已开通，正在换成公网订阅地址…', 'success');
          refreshSyncInfo(true);
          return;
        }
        if (state === 'error') {
          publicPollTimer = null;
          setStatus('公网入口开通失败：' + ((snapshot && (snapshot.error || snapshot.message)) || '请稍后重试') + '。局域网订阅仍然可用。', 'error');
          return;
        }
        setStatus('正在开通公网入口（' + ((snapshot && snapshot.message) || '首次要装 Tailscale 并扫码登录，可能要几分钟') + '），可以去忙别的，好了这里会自动换地址。', '');
        publicPollTimer = window.setTimeout(pollPublicAccess, 3000);
      }).catch(function (error) {
        publicPollTimer = null;
        setStatus('读取公网入口进度失败：' + error.message + '。局域网订阅仍然可用，稍后可在「手机连接」页重试。', 'error');
      });
    }
    function startPublicAccess() {
      var button = document.getElementById('sync-public-open');
      if (button) button.disabled = true;
      setStatus('正在请求开通公网入口…', '');
      api('/api/tunnel/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: true})}).then(function () {
        pollPublicAccess();
      }).catch(function (error) {
        if (button) button.disabled = false;
        setStatus('开通公网入口失败：' + error.message + '。局域网订阅仍然可用。', 'error');
      });
    }
    function refreshSyncInfo(manual) {
      var refresh = document.getElementById('sync-refresh');
      if (refresh) refresh.disabled = true;
      fetch('/api/sync/info', {headers: {'X-Token': token}, cache: 'no-store'}).then(function (response) {
        return response.json().then(function (data) { if (!response.ok || !data.ok) throw new Error(data.error || ('HTTP ' + response.status)); return data; });
      }).then(function (data) {
        var calendar = primaryAddress(data.calendars);
        var count = Number(data.events);
        if (!calendar || !calendar.url || !Number.isInteger(count) || count < 0) throw new Error('订阅信息格式无效');
        renderPublicNote(calendar);
        events.textContent = '当前 ' + count + ' 条事项';
        var first = !lastSyncUrl;
        var changed = !first && calendar.url !== lastSyncUrl;
        lastSyncUrl = calendar.url;
        input.value = calendar.url;
        try { input.dataset.testScheme = new URL(calendar.url).protocol; } catch (error) { input.dataset.testScheme = ''; }
        renderSyncQr();
        renderAddressChoices(data.calendars, calendar.url);
        if (manual) setStatus(changed ? '地址已更新，二维码已换新。' : '订阅地址没变，二维码内容与上次相同。', changed ? 'success' : '');
        else if (first) setStatus('订阅地址已生成（' + (calendar.name || '默认地址') + '）。', '');
      }).catch(function (error) {
        events.textContent = '事项数暂不可用';
        renderPublicNote(null);
        setStatus('无法确定局域网订阅地址：' + (error.message || '请粘贴公网地址重试'), 'error');
        renderSyncQr();
      }).then(function () { if (refresh) refresh.disabled = false; });
    }
    var refreshButton = document.getElementById('sync-refresh');
    if (refreshButton) refreshButton.onclick = function () { refreshSyncInfo(true); };
    var publicButton = document.getElementById('sync-public-open');
    if (publicButton) publicButton.onclick = startPublicAccess;
    // 主界面「显示订阅二维码」跳过来时静默重取一次地址，地址变了就自动换码。
    window.__nhRefreshSyncInfo = function () { refreshSyncInfo(false); };
    refreshSyncInfo(false);
  })();
  var themeBox = document.createElement('section'); themeBox.className='surface hosting-settings'; themeBox.innerHTML='<h2 class="section-title">外观</h2><fieldset id="pref-theme" class="theme-choice-list"><legend>主题</legend><label class="theme-option"><input name="pref-theme" type="radio" value="light"><span class="theme-option-body"><span class="theme-preview theme-preview-light" aria-hidden="true"><i></i><i></i><i></i></span><strong>浅色</strong><small>固定使用浅色界面</small></span></label><label class="theme-option"><input name="pref-theme" type="radio" value="dark"><span class="theme-option-body"><span class="theme-preview theme-preview-dark" aria-hidden="true"><i></i><i></i><i></i></span><strong>深色</strong><small>固定使用深色界面</small></span></label><p id="pref-theme-note" class="setting-note">仅保存在此浏览器</p></fieldset>';
  root.appendChild(themeBox); initThemePreference();
  var hostingBox = document.createElement('div'); hostingBox.className='hosting-settings';
  hostingBox.innerHTML='<h2 class="section-title">托管设置</h2><div class="hosting-warning" role="note"><strong>启用退出选项后，开始托管会关闭电脑版 QQ；结束时可按恢复选项重新启动。</strong></div><fieldset class="preference-list"><legend>自动化选项</legend><label class="preference"><input id="pref-quit_qq" type="checkbox"><span><strong>开始托管前退出电脑版 QQ</strong><small>避免桌面 QQ 与独立登录同时占用账号。</small></span></label><label class="preference"><input id="pref-restore_qq" type="checkbox"><span><strong>结束托管后恢复电脑版 QQ</strong><small>结束托管时重新启动电脑版 QQ。</small></span></label><label class="preference"><input id="pref-quit_restore_qq" type="checkbox"><span><strong>退出群务台时打开电脑版 QQ</strong><small>默认关闭：退出就是安静退出，不额外启动 QQ。托管期间关掉的 QQ 由上一项负责恢复。</small></span></label><label class="preference"><input id="pref-auto_on_start" type="checkbox"><span><strong>启动 notice-hub 时自动开始托管</strong><small>启动应用后立即按上述选项接管。</small></span></label><label class="preference"><input id="pref-autostart" type="checkbox"><span><strong>开机自动启动 notice-hub</strong><small>随系统启动此本地待办服务。</small></span></label></fieldset><fieldset class="preference-list"><legend>历史回溯</legend><label class="preference"><input id="pref-catchup-enabled" type="checkbox" disabled><span><strong>启动时自动回溯最近 N 天</strong><small>只影响应用启动时的补采行为，不会立即回溯。</small></span></label><label class="preference"><span><strong>回溯天数</strong><small>保存为现有的小时设置。</small></span><input id="pref-catchup-days" type="number" disabled min="1" max="365" step="1" value="1" aria-label="启动时自动回溯最近多少天"></label><button id="pref-catchup-save" class="btn" type="button" disabled>保存回溯设置</button><p id="pref-catchup-result" role="status" aria-live="polite"></p></fieldset><fieldset class="preference-list" id="llm-settings"><legend>AI 摘要（让它替你读消息、写待办）</legend><p class="setting-note">电脑上没装大模型也没关系：挑一家云服务，注册后把它给你的那串「钥匙」粘进来就行，一个月通常花不到一块钱。下面选好服务商，接口地址和模型名已经替你填好了，不用管。</p><label class="preference"><span><strong>用哪一家</strong><small id="llm-provider-hint">正在读取…</small></span><select id="llm-provider" aria-label="选择 AI 服务商"></select></label><div class="push-steps" id="llm-steps" hidden><ol id="llm-steps-list"></ol><a id="llm-help-link" class="push-help" target="_blank" rel="noreferrer" hidden></a></div><label class="preference"><span><strong>钥匙（API Key）</strong><small>粘一次就行；以后留空表示不改。</small></span><input id="llm-api-key" type="text" autocomplete="off" spellcheck="false" aria-label="API Key" placeholder="还没有填"></label><label class="preference"><span><strong>模型名</strong><small>已经替你填好，一般不用动。</small></span><input id="llm-model" type="text" autocomplete="off" spellcheck="false" aria-label="模型名"></label><label class="preference" id="llm-endpoint-row" hidden><span><strong>接口地址</strong><small>只有选「其它」时才需要填。</small></span><input id="llm-endpoint" type="text" autocomplete="off" spellcheck="false" aria-label="接口地址"></label><div class="hosting-actions"><button id="llm-save" class="btn primary" type="button">保存并测试</button><button id="llm-off" class="btn" type="button">不用 AI</button></div><p id="llm-result" role="status" aria-live="polite"></p></fieldset><fieldset class="preference-list" id="push-settings"><legend>提醒怎么送到手机</legend><p class="setting-note"><strong>不配也能用：</strong>点快捷操作里的「显示订阅二维码」，用手机日历扫一下，到期日程会同步进手机自带日历，到点手机自己响。想在微信里立刻收到提醒，就在下面挑一种，照着 1-2-3 做——每个值去哪里拿，都写在旁边了。</p><details class="push-channel" data-channel="wxpusher" open><summary>方式一：微信推送（WxPusher，推荐）</summary><ol class="push-steps"><li>打开下面的网站，用手机微信扫码登录。</li><li>点「应用管理」→「创建应用」，类型选「标准推送」，建好后复制那串「应用Token」贴到下面第一格。</li><li>把应用的二维码发给接收人（一般就是你自己）扫码关注，然后在「用户管理」里复制 UID_ 开头的那串，贴到第二格。</li><li>点下面的「保存」，再点「发一条测试消息」；手机收到就成功了。</li></ol><p><a class="push-help" data-help="wxpusher" target="_blank" rel="noreferrer">打开 WxPusher 后台，照着做</a></p><label class="preference"><span><strong>应用Token</strong><small>创建应用以后，页面上那串长得像密码的字符。</small></span><input id="push-wxpusher_app_token" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher 应用Token"></label><label class="preference"><span><strong>我的 UID</strong><small>关注公众号后，在「用户管理」里复制。多个用逗号分隔。</small></span><input id="push-wxpusher_uids" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher UID"></label><label class="preference"><span><strong>话题 ID（可选）</strong><small>只有要发给一群人时才填，一个人用就不用管它。</small></span><input id="push-wxpusher_topic_ids" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher 话题 ID"></label></details><details class="push-channel" data-channel="serverchan"><summary>方式二：Server 酱（微信，只需一个值）</summary><ol class="push-steps"><li>打开下面的网站，用手机微信扫码登录。</li><li>登录后网页上直接给你一串密码一样的字符，整串复制下来。</li><li>贴到下面，点「保存」，再点「发一条测试消息」。</li></ol><p><a class="push-help" data-help="serverchan" target="_blank" rel="noreferrer">打开 Server 酱（直达密钥页面）</a></p><label class="preference"><span><strong>密钥</strong><small>扫码登录后页面上直接显示的那串。</small></span><input id="push-serverchan_keys" type="text" autocomplete="off" spellcheck="false" aria-label="Server 酱密钥"></label></details><details class="push-channel" data-channel="pushplus"><summary>方式三：PushPlus（微信，只需一个值）</summary><ol class="push-steps"><li>打开下面的网站，用手机微信扫码登录。</li><li>进「一对一消息」页面，复制那里的「用户token」。</li><li>贴到下面，点「保存」，再点「发一条测试消息」。</li></ol><p><a class="push-help" data-help="pushplus" target="_blank" rel="noreferrer">打开 PushPlus，照着做</a></p><label class="preference"><span><strong>用户token</strong><small>登录后「一对一消息」页面显示的那串。</small></span><input id="push-pushplus_tokens" type="text" autocomplete="off" spellcheck="false" aria-label="PushPlus 用户token"></label></details><details class="push-channel" data-channel="webhook"><summary>方式四：企业微信 / 钉钉 / 自己的机器人</summary><ol class="push-steps"><li>在电脑版企业微信里右键要收提醒的群 →「管理聊天信息」。</li><li>右侧点「消息推送」→「自定义消息推送」，填个名字后复制那条地址。</li><li>把地址整段贴到下面（后面的参数别漏），点「保存」再点「发一条测试消息」。</li></ol><label class="preference"><span><strong>机器人地址</strong><small>整段粘贴，别漏掉后面的参数。支持钉钉 / 企业微信 / 飞书群机器人；钉钉开了「加签」就把地址和密钥写成「地址|SEC密钥」。</small></span><input id="push-webhook_urls" type="text" autocomplete="off" spellcheck="false" aria-label="机器人地址"></label></details><div class="hosting-actions"><button id="push-save" class="btn primary" type="button">保存</button><button id="push-test" class="btn" type="button">发一条测试消息</button><button id="push-clear" class="btn" type="button">清空重填</button></div><p id="push-result" role="status" aria-live="polite"></p></fieldset><p class="setting-note">托盘图标也可用于开始或结束托管。</p><div id="hosting-status" class="hosting-status" role="status" aria-live="polite">正在读取托管状态…</div><div class="hosting-actions"><button id="hosting-start" class="btn primary" type="button">开始托管</button><button id="hosting-stop" class="btn" type="button">结束托管</button></div><fieldset class="preference-list local-preferences"><legend>界面动效</legend><label class="preference"><input id="pref-motion-enabled" type="checkbox"><span><strong>轻微动效</strong><small id="pref-motion-note"></small></span></label></fieldset><p id="hosting-result" role="status" aria-live="polite"></p>';
  root.appendChild(hostingBox);
  var donateBox = document.createElement('section'); donateBox.className='surface hosting-settings'; donateBox.innerHTML='<h2 class="section-title">支持开发者</h2><p>如果这个工具帮到了你，可以请作者喝杯咖啡。完全自愿，不影响任何功能。</p><div class="donate-qr-row"><img id="donate-qr" src="/api/donate/qr.png" alt="微信收款码" style="width:180px;background:#fff;border-radius:8px;border:1px solid var(--line);"><small id="donate-qr-note">微信扫码支付</small></div><p class="setting-note">打赏不构成任何服务承诺，也不影响功能与更新。</p>'; root.appendChild(donateBox); var donateImage=document.getElementById('donate-qr'); donateImage.onerror=function(){donateImage.hidden=true;document.getElementById('donate-qr-note').textContent='收款码加载失败';};
   var motionToggle = document.getElementById('pref-motion-enabled');
  motionToggle.checked = !motionPreferencePaused;
  motionToggle.addEventListener('change', function () { setMotionPreference(motionToggle.checked); });
  updateMotionNote();
  function setHostingResult(text){document.getElementById('hosting-result').textContent=text;}
  var preferenceNames=['quit_qq','restore_qq','quit_restore_qq','auto_on_start','autostart'];
  var savedPreferences=null;
   var savedCatchup=null;
  function readPreferences(){var values={};preferenceNames.forEach(function(name){values[name]=document.getElementById('pref-'+name).checked;});return values;}
  function setPreferences(values){preferenceNames.forEach(function(name){document.getElementById('pref-'+name).checked=!!values[name];});}
  function setPreferencesBusy(busy){preferenceNames.forEach(function(name){document.getElementById('pref-'+name).disabled=busy;});}
   function setCatchup(values){document.getElementById('pref-catchup-enabled').checked=!!values.catchup_enabled;document.getElementById('pref-catchup-days').value=String(values.catchup_days);}
   function setCatchupBusy(busy){document.getElementById('pref-catchup-enabled').disabled=busy;document.getElementById('pref-catchup-days').disabled=busy;document.getElementById('pref-catchup-save').disabled=busy;}
   function saveCatchup(){var days=Number(document.getElementById('pref-catchup-days').value);if(!Number.isInteger(days)||days<1||days>365){document.getElementById('pref-catchup-result').textContent='回溯天数必须在 1 到 365 之间。';return;}var values={catchup_enabled:document.getElementById('pref-catchup-enabled').checked,catchup_hours:days*24};setCatchupBusy(true);document.getElementById('pref-catchup-result').textContent='正在保存…';api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');savedCatchup={catchup_enabled:values.catchup_enabled,catchup_days:days};document.getElementById('pref-catchup-result').textContent='已保存。仅在下次启动时应用。';showFeedback('启动回溯设置已保存','success');}).catch(function(error){if(savedCatchup)setCatchup(savedCatchup);document.getElementById('pref-catchup-result').textContent='保存失败：'+error.message;showFeedback('启动回溯设置未保存：'+error.message,'error',saveCatchup);}).then(function(){setCatchupBusy(false);});}
   document.getElementById('pref-catchup-save').onclick=saveCatchup;
  function savePreferences(values){setPreferencesBusy(true);showFeedback('正在保存设置…','loading');return api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');savedPreferences=values;setHostingResult('设置已保存');showFeedback('设置已保存','success');}).catch(function(error){if(savedPreferences)setPreferences(savedPreferences);setHostingResult('保存失败：'+error.message);showFeedback('设置未保存：'+error.message,'error',function(){savePreferences(values);});}).then(function(){setPreferencesBusy(false);});}
  setPreferencesBusy(true);
  api('/api/settings').then(function(values){savedPreferences={};preferenceNames.forEach(function(name){savedPreferences[name]=!!values[name];});setPreferences(savedPreferences);var days=Math.min(365,Math.max(1,Math.ceil(Number(values.catchup_hours||24)/24)));savedCatchup={catchup_enabled:!!values.catchup_enabled,catchup_days:days};setCatchup(savedCatchup);setPreferencesBusy(false);setCatchupBusy(false);}).catch(function(error){setHostingResult('设置读取失败：'+error.message);showFeedback('设置读取失败：'+error.message,'error',loadSettings);});
  preferenceNames.forEach(function(name){document.getElementById('pref-'+name).onchange=function(){if(savedPreferences)savePreferences(readPreferences());};});
  function refreshHosting(){return api('/api/hosting/status').then(function(status){document.getElementById('hosting-status').textContent='托管：'+(status.hosting_active?'进行中':'未开始')+'；NapCat：'+(status.napcat_running?'运行中':'未运行')+'；登录：'+(status.napcat_online?'在线':'未登录')+'；电脑版 QQ：'+(status.user_qq_running?'运行中':'未运行');return status;}).catch(function(error){setHostingResult('状态读取失败：'+error.message);throw error;});}
  function changeHosting(button,path,desired,pending,success){button.disabled=true;setHostingResult(pending);showFeedback(pending,'loading');function retryStatus(){refreshHosting().then(function(status){if(!!status.hosting_active===desired){setHostingResult(success);showFeedback(success,'success');}else{showFeedback('托管状态尚未确认','error',retryStatus);}},function(error){showFeedback('状态刷新失败：'+error.message,'error',retryStatus);});}api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未确认操作');return refreshHosting().then(function(status){if(!!status.hosting_active!==desired){setHostingResult('服务端已响应，托管状态尚未达到预期');showFeedback('服务端已响应，托管状态尚未达到预期','error',retryStatus);return;}setHostingResult(success);showFeedback(success,'success');},function(error){showFeedback('服务端已响应，但状态读取失败：'+error.message,'error',retryStatus);});}).catch(function(error){setHostingResult('托管操作未确认：'+error.message);showFeedback('托管操作未确认：'+error.message,'error',function(){changeHosting(button,path,desired,pending,success);});}).then(function(){button.disabled=false;});}
  document.getElementById('hosting-start').onclick=function(){changeHosting(this,'/api/hosting/start',true,'正在开始托管，请稍候…','托管已开始并确认');};
  document.getElementById('hosting-stop').onclick=function(){changeHosting(this,'/api/hosting/stop',false,'正在结束托管，请稍候…','托管已结束并确认');}; refreshHosting().catch(function(){});
  var pushFields=['wxpusher_app_token','wxpusher_uids','wxpusher_topic_ids','serverchan_keys','pushplus_tokens','webhook_urls'];
  // 通道 id 是内部标识符，给用户看的必须是服务名。
  var PUSH_CHANNEL_NAMES={wxpusher:'WxPusher',serverchan:'Server酱',pushplus:'PushPlus',webhook:'Webhook','qq-bot':'QQ 机器人'};
  function pushChannelName(id){return PUSH_CHANNEL_NAMES[id]||String(id||'未知通道');}
  function applyPushState(state,channels){if(!state)return;pushFields.forEach(function(name){var input=document.getElementById('push-'+name);var item=state[name];if(!input)return;input.placeholder=(item&&item.set)?('已配置：'+item.masked.join('、')+'（留空不修改）'):'未配置';});var list=(channels||[]).map(pushChannelName);document.getElementById('push-result').textContent='当前推送通道：'+(list.join('、')||'未配置 · 去设置里添加推送通道')+'。';}
  function savePushSettings(all){var values={};pushFields.forEach(function(name){var input=document.getElementById('push-'+name);if(!input)return;var text=input.value.trim();if(text||all)values[name]=text;});var resultBox=document.getElementById('push-result');if(!Object.keys(values).length){resultBox.textContent='没有要保存的内容：留空的项不会修改，清空请用「清除全部推送设置」。';return;}resultBox.textContent='正在保存…';api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');pushFields.forEach(function(name){var input=document.getElementById('push-'+name);if(input)input.value='';});applyPushState(result.push, result.channels);resultBox.textContent='已保存，当前推送通道：'+(((result.channels||[]).map(pushChannelName)).join('、')||'未配置 · 去设置里添加推送通道')+'。';showFeedback('推送设置已保存','success');}).catch(function(error){resultBox.textContent='保存失败：'+error.message;showFeedback('推送设置未保存：'+error.message,'error',function(){savePushSettings(all);});});}
  document.getElementById('push-save').onclick=function(){savePushSettings(false);};
  document.getElementById('push-clear').onclick=function(){if(!window.confirm('确定清空全部推送通道配置吗？清空后「截止提醒」将发不出去。'))return;savePushSettings(true);};
  function applyPushHelp(map){if(!map)return;var links=document.querySelectorAll('a.push-help[data-help]');for(var i=0;i<links.length;i++){var url=safeUrl(map[links[i].getAttribute('data-help')]);if(url){links[i].href=url;links[i].rel='noreferrer';}}}
  function testPush(){var box=document.getElementById('push-result');box.textContent='正在发送测试消息…';return api('/api/settings/test-push',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){var list=result.results||[];if(!list.length){box.textContent=result.error||'还没有配置任何推送通道：先在上面填好并保存。';return;}var text=list.map(function(item){return (item.ok?'收到：':'失败：')+item.channel+(item.ok?'':(' · '+(item.error||'原因不明')));}).join('；');box.textContent=result.ok?('测试消息已发出——'+text):('有的通道没发成功——'+text);if(result.ok)showFeedback('测试消息已发出','success');}).catch(function(error){box.textContent='测试失败：'+error.message;});}
  document.getElementById('push-test').onclick=function(){testPush();};
  var llmProviders=[],llmState=null;
  function llmProvider(id){for(var i=0;i<llmProviders.length;i++){if(llmProviders[i].id===id)return llmProviders[i];}return null;}
  function setLlmBusy(busy){document.getElementById('llm-provider').disabled=busy;document.getElementById('llm-model').disabled=busy;document.getElementById('llm-endpoint').disabled=busy;document.getElementById('llm-save').disabled=busy;document.getElementById('llm-off').disabled=busy;}
  function renderLlmProvider(id){var item=llmProvider(id);if(!item)return;var hint=document.getElementById('llm-provider-hint');hint.textContent=item.hint||'';document.getElementById('llm-model').value=item.model||'';document.getElementById('llm-endpoint').value=item.endpoint||'';document.getElementById('llm-endpoint-row').hidden=(id!=='custom');var list=document.getElementById('llm-steps-list');list.textContent='';var steps=item.key_steps||[];steps.forEach(function(text){list.appendChild(el('li','',text));});document.getElementById('llm-steps').hidden=!steps.length;var link=document.getElementById('llm-help-link');var lu=safeUrl(item.key_url);if(lu){link.href=lu;link.textContent=(item.needs_key?'打开拿钥匙的页面':'打开官网，照着上面做');link.hidden=false;}else{link.hidden=true;}var keyInput=document.getElementById('llm-api-key');var keyRow=keyInput.closest('.preference');keyInput.value='';if(!item.needs_key){keyInput.placeholder='这家不用钥匙';if(keyRow)keyRow.hidden=true;}else{keyRow&&(keyRow.hidden=false);keyInput.placeholder=(llmState&&llmState.key_set&&llmState.provider===id)?('已配置：'+llmState.key_masked+'（留空不修改）'):'粘贴你的钥匙';}}
  function applyLlmState(state){if(!state)return;llmState=state;var select=document.getElementById('llm-provider');select.value=state.provider||'';renderLlmProvider(state.provider||'');if(state.model)document.getElementById('llm-model').value=state.model;if(state.endpoint)document.getElementById('llm-endpoint').value=state.endpoint;var box=document.getElementById('llm-result');var item=llmProvider(state.provider)||{};if(state.provider==='off'||!state.enabled){box.textContent='现在没有用 AI：群里消息只归档，不生成摘要。想让它替你读消息、写待办，就在上面选一家（有免费额度的那几家不用花钱）。';}else if(item.needs_key&&!state.key_set){box.textContent='还差最后一步——钥匙：'+((item.name||'这家服务')+'要一串 API Key。')+' 点上面的链接拿到后粘进「钥匙」框，再点「保存并测试」。';}else{box.textContent='正在使用 '+(state.model||'未设置')+'（'+(item.name||'已选服务商')+'）'+(state.vision?'（能读图片）':'（不能读图片，图片只记录名字）')+'。';}}
  function saveLlm(thenTest){var providerId=document.getElementById('llm-provider').value;var payload={llm_provider:providerId};var key=document.getElementById('llm-api-key').value.trim();if(key)payload.llm_api_key=key;var model=document.getElementById('llm-model').value.trim();if(model)payload.llm_model=model;if(providerId==='custom')payload.llm_endpoint=document.getElementById('llm-endpoint').value.trim();var box=document.getElementById('llm-result');box.textContent='正在保存…';setLlmBusy(true);return api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');applyLlmState(result.llm);box.textContent='已保存。';showFeedback('AI 接入已保存','success');if(thenTest)return testLlm();}).catch(function(error){box.textContent='保存失败：'+error.message;showFeedback('AI 接入未保存：'+error.message,'error',function(){saveLlm(thenTest);});}).then(function(){setLlmBusy(false);});}
  function testLlm(){var box=document.getElementById('llm-result');box.textContent='正在问一下 AI，稍等几秒…';return api('/api/settings/test-llm',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){if(!result.ok){box.textContent='没连通：'+(result.error||'原因不明');showFeedback('AI 连接测试没通过','error');return;}box.textContent='连通了：'+(result.model||'模型')+' 回了「'+(result.reply||'')+'」（'+(result.seconds||0)+' 秒）。';showFeedback('AI 连接测试通过','success');}).catch(function(error){box.textContent='测试失败：'+error.message;});}
  document.getElementById('llm-provider').onchange=function(){var item=llmProvider(this.value)||{};renderLlmProvider(this.value);document.getElementById('llm-result').textContent=item.hint||'';};
  document.getElementById('llm-save').onclick=function(){saveLlm(true);};
  document.getElementById('llm-off').onclick=function(){document.getElementById('llm-provider').value='off';renderLlmProvider('off');saveLlm(false);};
  api('/api/meta').then(function (data) {
    llmProviders = data.providers || [];
    var llmSelect = document.getElementById('llm-provider');
    llmSelect.innerHTML = '';
    llmProviders.forEach(function (item) {
      var option = document.createElement('option');
      option.value = item.id;
      option.textContent = item.name;
      llmSelect.appendChild(option);
    });
    applyLlmState(data.llm);
    applyPushHelp(data.push_help);
    applyPushState(data.push, data.channels);
    var head = el('div', 'section-head');
    head.appendChild(el('h2', 'section-title', '运行信息'));
    root.appendChild(head);
    var rows = [
      ['QQ 监控群', (data.groups || []).join('、') || '未配置'],
      ['推送通道', (data.channels || []).join('、') || '无'],
      ['截止提醒', data.reminders || '未开启'],
      ['推送预算', data.push_budget || '不限'],
      ['夜间静默', data.quiet_hours || '未设置'],
      ['待办统计', '共 ' + data.stats.total + ' 件，未完成 ' + data.stats.open + ' 件'],
      ['服务时间', data.started_at || '未知'],
      ['微信', '读取中…'],
      ['学习通', '读取中…']
    ];
    var rowValue = {};
    rows.forEach(function (row) {
      var kv = el('div', 'kv');
      kv.appendChild(el('span', '', row[0]));
      var valueSpan = el('span', '', row[1]);
      rowValue[row[0]] = valueSpan;
      kv.appendChild(valueSpan);
      root.appendChild(kv);
    });
    // 微信 / 学习通的状态统一从 /api/overview 拿（后端已经把四个接口的判定合并了）。
    var describeSource = function (src) {
      if (!src) return '读取失败';
      if (src.state === 'off') return src.action || src.detail || '未启用';
      return src.detail || src.action || src.label || '';
    };
    api('/api/overview').then(function (data) {
      var sources = (data && data.sources) || {};
      rowValue['微信'].textContent = describeSource(sources.wechat);
      rowValue['学习通'].textContent = describeSource(sources.chaoxing);
    }).catch(function () {
      rowValue['微信'].textContent = '读取失败';
      rowValue['学习通'].textContent = '读取失败';
    });
    var insights = data.insights || [];
    if (insights.length) {
      var tipHead = el('div', 'section-head');
      tipHead.appendChild(el('h2', 'section-title', '纠错提示'));
      root.appendChild(tipHead);
      var tipList = el('ul', 'insight-list');
      insights.forEach(function (text) { tipList.appendChild(el('li', '', text)); });
      root.appendChild(tipList);
    }
  });
}