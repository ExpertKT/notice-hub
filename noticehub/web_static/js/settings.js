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
// 下载地址可能是 GitHub 或 Gitee 镜像：按真实 host 说，别写死。
function updateSourceLabel(latest) {
  var raw = (latest && (latest.download_url || latest.html_url)) || '';
  if (!raw) return '';
  var host = '';
  try { host = new URL(String(raw), window.location.href).hostname.toLowerCase(); } catch (error) { return ''; }
  if (!host) return '';
  if (host === 'gitee.com' || /(^|\.)gitee\.com$/.test(host)) return '来自 Gitee 镜像';
  if (host === 'github.com' || /(^|\.)github(usercontent)?\.com$/.test(host)) return '来自 GitHub';
  return '';
}
//同版本修复包和跨版本更新包是两种就绪态：pending 带 patch>0 且版本与当前相同才叫「修复包」，
//其余照旧叫「更新包」，跨版本更新的文案一个字不改。
function isPatchPending(data) {
  var pending = (data && data.pending) || null;
  if (!pending || !(Number(pending.patch) > 0)) return false;
  var currentVersion = data.current && (data.current.version || data.current.tag);
  return !!currentVersion && String(pending.version) === String(currentVersion);
}
function patchPendingHeadline(data, fallback) {
  if (!isPatchPending(data)) return fallback;
  return '修复包已就绪（修复 ' + Number(data.pending.patch) + '）';
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
  var pendingHeadline = pending ? '更新包已就绪（' + pending.version + '）' : '';
  if (title) title.textContent = pending ? patchPendingHeadline(data, pendingHeadline) : latestTitle(latest);
  var notes = document.getElementById('update-notes');
  if (notes) { notes.hidden = !showLatest; if (showLatest) notes.innerHTML = String(latest.notes_html || ''); }
  var size = document.getElementById('update-size');
  if (size) { size.hidden = !showLatest; if (showLatest) { var sourceLabel = updateSourceLabel(latest); size.textContent = '更新包 ' + formatMb(latest.size) + (sourceLabel ? '，下载地址' + sourceLabel : '') + '。'; } }
  var pendingNote = document.getElementById('update-pending-note');
  if (pendingNote) {
    pendingNote.hidden = !pending;
    if (pending) pendingNote.textContent = patchPendingHeadline(data, '更新包已就绪') + '。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。';
  }
  var link = document.getElementById('update-link');
  if (link) { var u = showLatest ? safeUrl(latest.html_url) : ''; if (u) { link.href = u; link.hidden = false; } else { link.hidden = true; } }
  var applyButton = document.getElementById('update-apply');
  if (applyButton) {
    applyButton.hidden = !showLatest || !!pending;
    applyButton.disabled = !!pending || !!(data.download && (data.download.state === 'downloading' || data.download.state === 'done'));
  }
}
// 更新横幅要分清三种状态，说混了用户就不知道该不该点托盘：
// ① 后台正在下载（还不能装，别催重启）② 新版本已下好（可以重启换版）③ 只是发现了新版本（还没下）。
function updateDownloadSnapshot() {
  var download = updateState && updateState.download;
  return (download && typeof download === 'object') ? download : null;
}
function updateDownloadPercent(download) {
  var done = Number(download && download.done);
  var total = Number(download && download.total);
  if (!isFinite(done) || !isFinite(total) || !(total > 0) || done < 0) return '';
  var ratio = Math.round((done / total) * 100);
  if (!isFinite(ratio)) return '';
  return String(Math.max(0, Math.min(100, ratio))) + '%';
}
function renderUpdateBanner() {
  var banner = document.getElementById('update-banner');
  if (!banner) return;
  var latest = (updateState && updateState.latest) || null;
  var pending = (updateState && updateState.pending) || null;
  var download = updateDownloadSnapshot();
  var downloadState = download ? String(download.state || '') : '';
  // pending 是「可以重启了」的唯一权威；downloading / done 只在还没写成 pending 时才轮到它说话。
  var downloading = !pending && downloadState === 'downloading';
  var downloaded = !pending && downloadState === 'done';
  var tag = pending ? String(pending.version || '') : (latest ? String(latest.tag || latest.version || '') : '');
  if (!tag && download && (downloading || downloaded)) tag = String(download.tag || '');
  var seen = '';
  try { seen = localStorage.getItem('seen_update_tag') || ''; } catch (error) { seen = ''; }
  var hasUpdate = !!(updateState && updateState.has_update);
  // 「关闭」必须真关得掉：除就绪态（pending 一直该被看到）外，一律按 tag 记已读，否则点了关闭还会立刻回来。
  var visible = !!pending || ((hasUpdate || downloading || downloaded) && seen !== tag);
  if (!updateState || !tag || !visible) { banner.hidden = true; return; }
  banner.textContent = '';
  var text = document.createElement('span'); text.className = 'announcement-copy';
  if (pending) {
    text.textContent = patchPendingHeadline(updateState, '更新包已就绪（' + tag + '）') + '。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。';
  } else if (downloading) {
    var percent = updateDownloadPercent(download);
    text.textContent = '正在后台下载 新版本 ' + tag + (percent ? '（' + percent + '）' : '') + '…下载完成后这里会更新提示。';
  } else if (downloaded) {
    text.textContent = '新版本 ' + tag + ' 已下好，点右下角托盘图标 →『重启并更新』，约 10–30 秒换好并重开。';
  } else {
    text.textContent = '发现新版本 ' + tag + '（当前版本 ' + String((updateState.current && (updateState.current.version || updateState.current.tag)) || '未知') + '）' + (latest.name ? '：' + latest.name : '');
  }
  var actions = document.createElement('div'); actions.className = 'announcement-actions';
  var open = document.createElement('button'); open.type = 'button'; open.className = 'btn'; open.textContent = downloading ? '查看进度' : '点这里更新';
  open.addEventListener('click', function () {
    showSettingsTab();
    var card = document.getElementById('update-card');
    // 卡片是矮目标，共用 helper 会给 center（保持原落点）；超高目标才走顶边对齐（helper 判据）。
    if (card) scrollTargetIntoView(card, 'smooth');
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
// B6：「正式版 / 测试版」的初始选中来自 /api/update 的 channel。服务端只替换内联脚本里的占位符，
// 独立加载的 settings.js 拿到的是字面量占位文本，靠它会让两个单选都不选中。
var currentUpdateChannel = '';
var updateChannelSwitching = false;
function setUpdateChannelInputsDisabled(disabled) {
  Array.prototype.forEach.call(document.querySelectorAll('input[name="update-channel"]'), function (input) {
    input.disabled = !!disabled;
  });
}
// 切通道不再点「保存」：改了单选就发请求。失败必须把选中项退回去，不能让界面显示已切换、实际没保存。
function switchUpdateChannel(previous) {
  var chosen = document.querySelector('input[name="update-channel"]:checked');
  var result = document.getElementById('update-channel-result');
  if (!chosen || updateChannelSwitching) return;
  var next = chosen.value;
  if (next === previous) { if (result) result.textContent = ''; return; }
  updateChannelSwitching = true;
  setUpdateChannelInputsDisabled(true);
  if (result) result.textContent = '正在切换…';
  api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({update_channel: next})}).then(function (payload) {
    if (!payload || !payload.ok) throw new Error((payload && payload.error) || '保存失败');
    var saved = selectUpdateChannel(payload.update_channel);
    if (result) result.textContent = saved === 'stable' ? '已切到正式版。' : saved === 'beta' ? '已切到测试版。' : '已切换更新通道。';
    return refreshUpdate(true);
  }).catch(function (error) {
    selectUpdateChannel(previous);
    if (result) result.textContent = '切换失败：' + ((error && error.message) || error);
  }).then(function () {
    updateChannelSwitching = false;
    setUpdateChannelInputsDisabled(false);
  });
}
// patch_auto 走和更新通道同一套保存方式：改了就发请求，payload 只带这一个键，失败要把选中退回去。
var patchAutoSwitching = false;
function switchPatchAuto(checked) {
  var input = document.getElementById('patch-auto');
  var result = document.getElementById('patch-auto-result');
  if (!input || patchAutoSwitching) return;
  patchAutoSwitching = true;
  input.disabled = true;
  if (result) result.textContent = '正在保存…';
  api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({patch_auto: !!checked})}).then(function (payload) {
    if (!payload || !payload.ok) throw new Error((payload && payload.error) || '保存失败');
    if (result) result.textContent = checked ? '已打开，更新包会自动下载。' : '已关闭，更新包不再自动下载，仍可手动检查。';
  }).catch(function (error) {
    input.checked = !checked;
    if (result) result.textContent = '保存失败：' + ((error && error.message) || error);
  }).then(function () {
    patchAutoSwitching = false;
    input.disabled = false;
  });
}
// 回到上一版是破坏性操作：两段式确认（首点变确认文案 4 秒内再点一次），和通知删除同一套节奏。
var rollbackConfirmTimer = null;
function disarmRollbackButton(button) {
  if (rollbackConfirmTimer !== null) {
    window.clearTimeout(rollbackConfirmTimer);
    rollbackConfirmTimer = null;
  }
  button.textContent = '回到上一版';
  button.className = 'btn';
  button.disabled = false;
}
function handleRollbackClick(button) {
  var result = document.getElementById('update-rollback-result');
  if (rollbackConfirmTimer === null) {
    button.textContent = '确认回到上一版？（4 秒内再点一次）';
    button.className = 'btn destructive';
    rollbackConfirmTimer = window.setTimeout(function () {
      rollbackConfirmTimer = null;
      if (!button.disabled) {
        button.textContent = '回到上一版';
        button.className = 'btn';
      }
    }, 4000);
    return;
  }
  window.clearTimeout(rollbackConfirmTimer);
  rollbackConfirmTimer = null;
  button.disabled = true;
  button.textContent = '正在回退…';
  api('/api/update/rollback', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({})}).then(function (payload) {
    if (!payload || !payload.ok) throw new Error((payload && payload.error) || '回退没有成功');
    if (result) result.textContent = '正在退回修复 ' + Number(payload.patch) + '，约 10–30 秒后页面会重开。';
    startUpdateReloadWatch();
  }).catch(function (error) {
    disarmRollbackButton(button);
    if (result) result.textContent = '回退没有成功：' + ((error && error.message) || error);
  });
}
function selectUpdateChannel(channel) {
  var value = channel === 'stable' || channel === 'beta' ? channel : '';
  Array.prototype.forEach.call(document.querySelectorAll('input[name="update-channel"]'), function (input) {
    input.checked = input.value === value;
  });
  currentUpdateChannel = value;
  return value;
}
function applyUpdateState(data) {
  if (!data || typeof data !== 'object') return;
  updateState = data;
  if (typeof data.channel === 'string') selectUpdateChannel(data.channel);
  var current = document.getElementById('update-current');
  if (current) {
    var currentVersion = (data.current && (data.current.version || data.current.tag)) || '未知';
    var patchCurrent = Number(data.patch && data.patch.current) || 0;
    current.textContent = '当前版本 ' + currentVersion;
    if (patchCurrent > 0) {
      var patchNo = document.createElement('span');
      patchNo.className = 'update-patch-no';
      patchNo.textContent = ' · 修复 ' + patchCurrent;
      current.appendChild(patchNo);
    }
  }
  var checked = document.getElementById('update-checked');
  if (checked) checked.textContent = data.checked_at ? ('上次检查：' + data.checked_at + (data.channel ? '（' + data.channel + ' 通道）' : '')) : '';
  var status = updateStatusNode();
  var rollbackButton = document.getElementById('update-rollback');
  if (status) {
    if (data.error) status.textContent = '检查更新失败：' + data.error + '。请稍后重试；群务台仍可正常使用。';
    else if (data.pending) status.textContent = patchPendingHeadline(data, '已就绪，等待重启') + '。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。';
    else if (data.download && data.download.state === 'done') status.textContent = '已就绪，等待重启。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。';
    else if (data.notice) status.textContent = data.notice;
    else if (data.checking) status.textContent = '正在检查更新…';
    else if (!data.has_update) status.textContent = '已经是最新版本。';
    else status.textContent = '';
  }
  if (rollbackButton) {
    rollbackButton.hidden = !(data.rollback && data.rollback.available);
    if (!rollbackButton.hidden && rollbackConfirmTimer !== null) disarmRollbackButton(rollbackButton);
  }
  var patchAuto = document.getElementById('patch-auto');
  if (patchAuto && !patchAutoSwitching) patchAuto.checked = !!(data.patch && data.patch.auto);
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
// B11：后端给的是 ISO 串（2026-10-28T20:23:18），界面上只显示到分钟，原串留在 title 里。
function formatServiceStartedAt(raw) {
  var text = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!text) return {text: '未知', title: ''};
  var match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(text);
  if (!match) return {text: text, title: text};
  return {text: match[1] + '-' + match[2] + '-' + match[3] + ' ' + match[4] + ':' + match[5], title: text};
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
      updateState = data;
      renderUpdateBanner();
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
  if (updateState && typeof updateState.channel === 'string') selectUpdateChannel(updateState.channel);
  Array.prototype.forEach.call(document.querySelectorAll('input[name="update-channel"]'), function (input) {
    input.addEventListener('change', function () {
      if (!input.checked) return;
      switchUpdateChannel(currentUpdateChannel);
    });
  });
  checkButton.addEventListener('click', function () {
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
  var patchAuto = document.getElementById('patch-auto');
  if (patchAuto) patchAuto.addEventListener('change', function () { switchPatchAuto(patchAuto.checked); });
  var rollbackButton = document.getElementById('update-rollback');
  if (rollbackButton) rollbackButton.addEventListener('click', function () { handleRollbackClick(rollbackButton); });
  if (updateState) applyUpdateState(updateState);
}
var dingtalkScanTimer = null;
var wechatScanTimer = null;
// W6：cURL 粘贴提取与手动保存需要在全局（loadSettings 属于第一个 script 的全局作用域）。
// 注意：页尾还有一个脚本块自带 IIFE（platform setup 向导区），函数定义放它里面出不来。
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
  var input=document.getElementById('cx-manual-cred');
  var result=document.getElementById('cx-manual-cred-result');
  if(!input||!result)return;
  var text=(event&&event.clipboardData&&event.clipboardData.getData)?event.clipboardData.getData('text'):'';
  if(!text)return;
  var parsed=extractCookieFromCurl(text);
  if(!parsed.looksLikeCurl)return;
  if(event&&event.preventDefault)event.preventDefault();
  if(!parsed.found){result.textContent='这段内容里没有登录凭证，没能提取。请按 F12 → Network → 右键任意 chaoxing.com 请求 → Copy → Copy as cURL 后再粘贴。';return;}
  var current=String(input.value||'').replace(/^\s+|\s+$/g,'');
  if(current&&current!==parsed.cookie){result.textContent='输入框里已经有登录凭证了，没有自动覆盖：要换成 cURL 里的，请先清空输入框再粘贴。';return;}
  input.value=parsed.cookie;
  result.textContent='已从 cURL 中提取出登录凭证，请确认后保存。';
}
function cxManualNextStep(){return '按 F12 → Network → 选任意 chaoxing.com 请求 → 右键 Copy → Copy as cURL → 粘贴到下方「高级：手动粘贴登录凭证」，粘贴时会自动提取。';}

// —— 装载列表的统一规则：响应回来时不能吞掉用户刚做的编辑 ——
// 用户在响应回来之前点了「新增」或改过某行时，把这些行先摘出来，
// 服务端结果照常渲染，摘出来的行按原样接回末尾（同 id 的服务端行让位，避免两张同名卡）。
// 抽成顶层函数是为了能在测试里用假列表跑真实逻辑（行为级断言，不测实现细节）。
function loadRulesKeepingEdits(deps){
  var list=deps.list;
  if(!list||!deps.request)return Promise.resolve(0);
  return deps.request().then(function(v){
    // 现场采集：必须在响应到达这一刻摘（请求发出时摘会漏掉等待期间新增的行）。
    var kept=[],keptIds={};
    Array.prototype.slice.call(list.children||[]).forEach(function(node,index){
      var id=deps.readId(node);
      var dirty=!!(node.dataset&&node.dataset.dirty==='1');
      // 改名后的卡：当前名字和原来的 dataset.ruleId 都要挡住，否则服务端那张旧名字的卡会再渲染一遍。
      var key=String((node.dataset&&node.dataset.ruleId)||'').trim();
      if(dirty||!id){kept.push({node:node,index:index,id:id});if(id)keptIds[id]=1;if(key)keptIds[key]=1;}
    });
    var all=deps.toCards((v&&v.rules)||[]);
    var fresh=all.filter(function(card){return !keptIds[String((card&&card.id)||'').trim()];});
    deps.render(fresh);
    // 有 id 的编辑行按原索引插回原位（改过的「规则A」刷新后仍在第一位）；
    // 新加的空行没有位置可言，接在末尾。
    kept.forEach(function(entry){
      if(!entry.id||!list.insertBefore){if(list.appendChild)list.appendChild(entry.node);return;}
      list.insertBefore(entry.node,list.children[entry.index]||null);
    });
    if(deps.refreshEmpty)deps.refreshEmpty();
    return fresh.length;
  });
}
// 群白名单重渲染前，把用户还没保存的勾选与别名收进本地集合；
// 否则点一次「拉取我的群」就会把没保存的勾选/别名冲掉。列表为空（首次/报错文案）时不动本地状态。
function harvestGroupEdits(kind, box, selected, aliases){
  var next={selected:(selected||[]).slice(),aliases:aliases||{}};
  if(!box||!box.querySelectorAll)return next;
  var boxes=box.querySelectorAll('input[type="checkbox"][value]');
  if(!boxes||!boxes.length)return next;
  var ids=[];
  var seen={};
  for(var i=0;i<boxes.length;i+=1){if(boxes[i].checked){var id=String(boxes[i].value);if(!seen[id]){seen[id]=true;ids.push(id);}}}
  next.selected=ids;
  if(kind==='wechat'){
    var aliasInputs=box.querySelectorAll('.wx-alias');
    for(var j=0;j<aliasInputs.length;j+=1){
      var gid=aliasInputs[j].getAttribute('data-group');
      if(gid)next.aliases[gid]=aliasInputs[j].value;
    }
  }
  return next;
}
// 设置页整块重建前先把用户已经输入/勾选但没保存的内容收起来：粘好 64 位密钥、填好数据目录，
// 手滑切个标签再回来不能白填。只认有 id 的控件（没 id 无处复位，宁可不猜）。
var settingsUnsaved = {};
// 清空输入框是合法状态（微信数据目录清空＝回到自动查找），可空值本身推断不出「用户改过」，
// 所以用户一碰就打个显式标记，重建设置页时 harvestSettingsInputs 靠它把空值一起保住。
function markSettingsDirty(node) {
  if (!node || !node.id || !node.dataset) return;
  node.dataset.nhDirty = '1';
}
function bindSettingsDirtyTracking(root) {
  if (!root || !root.addEventListener) return;
  if (root.dataset && root.dataset.nhDirtyBound === '1') return;
  if (root.dataset) root.dataset.nhDirtyBound = '1';
  var handler = function (event) { markSettingsDirty(event && event.target); };
  root.addEventListener('input', handler);
  root.addEventListener('change', handler);
}
function harvestSettingsInputs(root) {
  var keep = {};
  if (!root || !root.querySelectorAll) return keep;
  Array.prototype.forEach.call(root.querySelectorAll('input[id],textarea[id],select[id]'), function (node) {
    var isCheck = node.type === 'checkbox' || node.type === 'radio';
    var dirty = isCheck ? node.checked !== node.defaultChecked : (node.dataset && node.dataset.nhDirty === '1') || !!String(node.value || '');
    if (!dirty) return;
    keep[node.id] = isCheck ? {checked: node.checked} : {value: node.value};
  });
  return keep;
}
function restoreSettingsInputs(keep) {
  Object.keys(keep || {}).forEach(function (id) {
    var node = document.getElementById(id);
    if (!node) return;
    var saved = keep[id];
    if (Object.prototype.hasOwnProperty.call(saved, 'checked')) { if (node.checked !== saved.checked) node.checked = saved.checked; return; }
    // 只补空框：新渲染已经放好值的地方不抢（那些是服务端真值）
    if (!String(node.value || '')) node.value = saved.value;
  });
}
var settingsRenderSeq = 0;
function loadSettings() {

  if (dingtalkScanTimer !== null) { clearInterval(dingtalkScanTimer); dingtalkScanTimer = null; }
  if (wechatScanTimer !== null) { clearInterval(wechatScanTimer); wechatScanTimer = null; }
  var root = document.querySelector('#tab-settings .settings-body') || document.getElementById('tab-settings');
  bindSettingsDirtyTracking(root);
  settingsUnsaved = harvestSettingsInputs(root);
  // 重进设置/重渲染时不能丢滚动位置：`.settings-body` 是 display:contents（page.html:761），
  // 清空它就等于删掉设置页全部面板，文档高度瞬间塌到页头，浏览器会把 scrollTop 夹成 0
  // ——用户看到的就是「正在看下面的设置，突然自己滑到最顶」。
  // 所以先用 min-height 锁住页签高度（清空期间文档高度不变、不会被夹），
  // 等面板建完（微任务，同步构建结束后、绘制前）再放开并校准一次滚动。
  var settingsTab = document.getElementById('tab-settings');
  var scrollBefore = window.scrollY;
  var tabHeightBefore = settingsTab ? settingsTab.getBoundingClientRect().height : 0;
  if (settingsTab && tabHeightBefore > 0) settingsTab.style.minHeight = Math.ceil(tabHeightBefore) + 'px';
  root.textContent = '';
  // 分组容器由 page.html 的脚本按 render 号回收：号一变就说明这一轮的面板全换了，
  // 组里留着的是上一轮的旧卡片，得整批扔掉重建。
  root.setAttribute('data-render', String(Date.now()) + '-' + String(++settingsRenderSeq));
  if (settingsTab && tabHeightBefore > 0) {
    Promise.resolve().then(function () {
      settingsTab.style.minHeight = '';
      if (window.scrollY !== scrollBefore) {
        window.scrollTo({ top: scrollBefore, left: 0, behavior: 'instant' });
      }
    });
  }
  var announcementCard = document.createElement('section'); announcementCard.className = 'surface';
  var announcementHeading = document.createElement('div'); announcementHeading.className = 'panel-heading';
  var announcementTitle = document.createElement('h2'); announcementTitle.textContent = '服务公告'; announcementHeading.appendChild(announcementTitle);
  announcementCard.appendChild(announcementHeading);
  var announcementList = document.createElement('div'); announcementList.id = 'announce-list'; announcementList.className = 'announcement-list'; announcementCard.appendChild(announcementList);
  root.appendChild(announcementCard);var setupCard=el('section','surface hosting-settings');setupCard.id='platform-setup-settings';setupCard.dataset.settingAnchor='setup';setupCard.innerHTML='<h2 class="section-title">首次配置</h2><p class="setting-note">重新检查平台接入状态，继续尚未完成的配置。</p><button id="platform-setup-rerun" class="btn" type="button">重新运行首次向导</button>';root.appendChild(setupCard); renderAnnouncementList();
  var updateCard = el('section', 'surface update-card');
  updateCard.id = 'update-card';
  updateCard.dataset.settingAnchor = 'update';
   updateCard.innerHTML = '<div class="section-head"><h2 class="section-title">版本更新</h2><span id="update-current" class="count-pill">读取中…</span></div><p id="update-checked" class="setting-note" role="status" aria-live="polite"></p><div class="agent-hint" role="note"><p class="agent-hint-lead">建议让 agent 来更新</p><p class="agent-hint-note">把这一段发给它，或者让它直接接手这台电脑 · <a href="https://github.com/ExpertKT/notice-hub" target="_blank" rel="noopener noreferrer">项目地址</a> · <button class="agent-hint-open" type="button" aria-haspopup="dialog">什么是 agent？</button></p></div><fieldset class="login-choice"><legend>更新通道</legend><label><input type="radio" name="update-channel" value="stable"> 正式版（稳定）</label><label><input type="radio" name="update-channel" value="beta"> 测试版（尝鲜）</label></fieldset><label class="preference"><input id="patch-auto" type="checkbox"><span><strong>自动下载更新包</strong><small>新版本和同版本修复包都会在后台下好，不会自己重启；关掉只是不自动下载，仍可手动检查</small></span></label><p id="patch-auto-result" class="setting-note" role="status" aria-live="polite"></p><p id="update-channel-result" class="setting-note" role="status" aria-live="polite"></p><div class="update-actions"><button id="update-check" class="btn" type="button">检查更新</button><button id="update-apply" class="btn primary" type="button" hidden>下载更新包</button><button id="update-abandon" class="btn" type="button" hidden>放弃本次下载</button><button id="update-rollback" class="btn" type="button" hidden>回到上一版</button></div><p id="update-rollback-result" class="setting-note" role="status" aria-live="polite"></p><div id="update-latest" class="update-latest" hidden><p id="update-latest-title" class="update-latest-title"></p><div id="update-notes" class="update-notes"></div><p id="update-size" class="setting-note"></p><p id="update-pending-note" class="setting-note" hidden>更新包已就绪。点右下角托盘图标 →『重启并更新』，约 10–30 秒后会自动换好并重开。</p><p class="setting-note">更新包下载好后不会自己装；点托盘『重启并更新』才会换上新版，你的数据和订阅不会丢。</p><a id="update-link" class="setting-note" target="_blank" rel="noopener noreferrer" hidden>查看完整更新说明 ↗</a></div><p id="update-status" class="update-status" role="status" aria-live="polite"></p>';
  root.appendChild(updateCard);
  setupUpdateCard();
  function renderSuggestionSource(id, source, error) { var box=document.getElementById(id);box.textContent='';box.hidden=false;box.dataset.source=source==='llm'||source==='heuristic'?source:'unknown';var strong=document.createElement('strong');strong.textContent=source==='llm'?'AI 识别':source==='heuristic'?'按关键词识别':'来源未提供';box.appendChild(strong);if(error){var note=document.createElement('span');note.textContent=' '+error;box.appendChild(note);} }
  function appendRecommendation(meta, row) { var category=document.createElement('span');category.className='category-badge';category.textContent=groupCategoryName(row.category);meta.appendChild(category);if(row.suggested){var badge=document.createElement('span');badge.className='suggest-badge';badge.textContent='建议订阅';meta.appendChild(badge);}if(row.reason){var reason=document.createElement('span');reason.className='suggest-reason';reason.textContent=row.reason;meta.appendChild(reason);} }
  var syncBox = el('section', 'surface sync-card');
  syncBox.dataset.settingAnchor = 'calendar';
  syncBox.setAttribute('aria-labelledby', 'sync-title');
  syncBox.innerHTML='<div class="section-head"><h2 id="sync-title" class="section-title">iPhone 日历订阅</h2><span id="sync-events" class="count-pill">读取事项数…</span></div><div class="sync-layout"><div class="sync-qr-wrap"><img id="sync-qr" class="sync-qr" alt="iPhone 日历订阅二维码" hidden><p id="sync-qr-message" class="sync-status" role="status" aria-live="polite">正在生成二维码…</p></div><div class="sync-copy"><label for="sync-url">订阅地址</label><input id="sync-url" class="sync-url" type="url" autocomplete="url" spellcheck="false" aria-describedby="sync-events sync-help"><div class="sync-actions"><button id="sync-copy" class="btn" type="button">复制订阅链接</button><button id="sync-test" class="btn" type="button">测试地址</button><button id="sync-refresh" class="btn" type="button">刷新二维码</button></div><p id="sync-public-note" class="setting-note" hidden></p><p id="sync-public-actions" hidden><button id="sync-public-open" class="btn" type="button">去「手机 App」卡开通公网入口 →</button></p><p id="sync-alts" class="sync-alts" hidden></p><p id="sync-help">日历按 iPhone 的计划刷新，不是实时推送；可在“设置 → 日历 → 账户 → 已订阅的日历”调整刷新频率，也可在日历 App 下拉刷新。手机需能访问此地址（同一 Wi-Fi 或公网地址）。订阅地址包含访问令牌，令牌轮换后需重新订阅。</p><p id="sync-status" class="sync-status" role="status" aria-live="polite"></p></div></div>';
  root.appendChild(syncBox);
   var backupCard=el('section','surface hosting-settings');
   backupCard.id='backup-card';
   backupCard.dataset.settingAnchor='backup';
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
  var cxBox = el('section', 'surface hosting-settings');cxBox.id='chaoxing-settings-card';cxBox.dataset.settingAnchor='chaoxing';
   cxBox.innerHTML='<h2 class="section-title">学习通</h2><p class="setting-note">仅读取通知；登录凭证只保存在本机数据目录，不会写进 .env。</p><label class="preference"><input id="cx-enabled" type="checkbox"><span><strong>启用学习通</strong><small>仅处理已勾选课程的通知。</small></span></label><div id="cx-cred-methods"><section id="cx-auto-option" class="cx-auto-block"><strong>方式一：浏览器自动获取 <span class="count-pill">推荐</span></strong><ol class="setting-note"><li>① 用 Edge、Chrome 或其他 Chromium 浏览器登录 chaoxing.com <span id="cx-step-login-state" class="count-pill">待操作</span></li><li>② 保持本页打开，群务台会复制一份登录凭证库和日志再读取；浏览器独占文件时，会提示下一步 <span id="cx-step-exit-state" class="count-pill">待操作</span></li><li>③ 点“重试自动获取” <span id="cx-step-retry-state" class="count-pill">待操作</span></li></ol><a class="btn" href="https://i.chaoxing.com/" target="_blank" rel="noopener noreferrer">打开学习通登录页</a><button id="cx-auto-cred" class="btn" type="button">重试自动获取</button><p id="cx-auto-result" class="setting-note" role="status" aria-live="polite">尚未开始自动检查。</p><ul id="cx-auto-attempts" class="setting-note" aria-label="浏览器检查结果"></ul></section><p class="setting-note">首选浏览器自动读取；失败时用托盘右键「读取学习通登录凭证」；仍失败可展开「高级：手动粘贴登录凭证」。手工粘的可能缺 HttpOnly 项。</p></div><div class="field"><span>我的课程</span><button id="cx-load-courses" class="btn" type="button">拉取我的课程</button></div><div id="cx-courses" class="setting-note">请先设置登录凭证。</div><button id="cx-save" class="btn primary" type="button">保存学习通设置</button><p id="cx-status" class="setting-note" role="status" aria-live="polite">正在读取状态…</p><button id="cx-validate" class="btn" type="button">立即校验登录凭证</button><p id="cx-cred-state" class="setting-note" role="status" aria-live="polite"></p><details id="cx-manual-option"><summary>高级：手动粘贴登录凭证</summary><label class="field" for="cx-manual-cred"><span>学习通登录凭证</span><textarea id="cx-manual-cred" rows="5" autocomplete="off" spellcheck="false" placeholder="粘贴登录凭证内容"></textarea></label><button id="cx-manual-cred-save" class="btn" type="button">保存并校验</button><p id="cx-manual-cred-result" class="setting-note" role="status" aria-live="polite"></p></details><p id="cx-result" class="setting-note" role="status"></p>';
   root.appendChild(cxBox);
   // 手动 Cookie 区（卡片此刻才存在）：粘贴 cURL 自动提取 + 保存按钮接线。
   var manualCookieInput=document.getElementById('cx-manual-cred');
   if(manualCookieInput)manualCookieInput.addEventListener('paste',handleCurlPaste);
   var manualCookieButton=document.getElementById('cx-manual-cred-save');
   if(manualCookieButton)manualCookieButton.onclick=function(){
  var input=document.getElementById('cx-manual-cred');
  var result=document.getElementById('cx-manual-cred-result');
  var cookie=String(input.value||'').replace(/\r/g,'').replace(/\n/g,'').replace(/^\s*Cookie:\s*/i,'').trim();
  if(!cookie){result.textContent='请粘贴学习通登录凭证。';return;}
  manualCookieButton.disabled=true;result.textContent='正在保存并校验…';
  api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chaoxing_cookie:cookie})})
    .then(function(saved){if(!saved.ok)throw new Error(saved.error||'登录凭证保存失败');return api('/api/chaoxing/cookie/validate',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});})
    .then(function(validation){if(validation.ok){result.textContent='登录凭证已保存，校验通过。';input.value='';refreshChaoxingCookie();}else{result.textContent=String(validation.error||'登录凭证校验失败');}})
    .catch(function(error){result.textContent=String(error&&error.message||error);})
    .then(function(){manualCookieButton.disabled=false;});
};
    /* —— 群选择区（微信/钉钉共用） ——
       拉取成功后不能把上百个群一屏铺开：顶部先给「共 N 个群」，推荐区按类别分组可折叠，
       「全部群聊」默认收起、展开可搜——用户才看得出这是不是全部，也才找得到某个群。 */
    var GROUP_CATS=['course','activity','market','chat','other'];
    function groupBoxId(kind){return kind==='wechat'?'wx-groups':'dt-groups';}
    function groupIdOf(row){var value=row.id!==undefined&&row.id!==null?row.id:row.cid;return String(value===null||value===undefined?'':value);}
    function groupTitleOf(row){
      var raw=row.name!==undefined&&row.name!==null?row.name:row.title;
      var name=String(raw===null||raw===undefined?'':raw).trim();
      if(name)return name;
      var id=groupIdOf(row);
      // 钉钉有的会话没有标题：只显示裸群号等于没信息，给「未命名会话（616…）」仍能认出来。
      return '未命名会话（'+(id.length>6?id.slice(0,6)+'…':id)+'）';
    }
    function groupRowLabel(kind,row){
      var id=groupIdOf(row),title=groupTitleOf(row);
      var text=title===id?id:title+' ('+id+')';
      if(kind==='dingtalk')text+=' · '+String(row.members||0)+' 人';
      return text;
    }
    function groupSelectedIds(kind){return (kind==='wechat'?wxSelected:dtSelected)||[];}
    function groupPickedCount(box){
      var seen={},count=0;
      if(!box||!box.querySelectorAll)return 0;
      Array.prototype.forEach.call(box.querySelectorAll('input[type="checkbox"][value]'),function(input){
        var id=String(input.value||'');
        if(!input.checked||!id||seen[id])return;
        seen[id]=true;count+=1;
      });
      return count;
    }
    function groupSyncTwins(box,id,checked){
      Array.prototype.forEach.call(box.querySelectorAll('input[type="checkbox"][value]'),function(input){
        if(String(input.value||'')===id)input.checked=checked;
      });
    }
    function groupSyncAlias(box,id,value){
      Array.prototype.forEach.call(box.querySelectorAll('.wx-alias'),function(input){
        if(input.getAttribute('data-group')===id&&input.value!==value)input.value=value;
      });
    }
    function groupUpdateCount(box){
      var node=box?box.querySelector('[data-gsel-count]'):null;
      if(node)node.textContent='已选 '+groupPickedCount(box)+' 个';
    }
    function groupRowNode(kind,row,checked){
      var id=groupIdOf(row);
      var wrap=document.createElement('div');
      wrap.className='preference '+(kind==='wechat'?'wx-group-row':'dt-group-row');
      wrap.dataset.suggested=row.suggested?'true':'false';
      wrap.dataset.search=(groupTitleOf(row)+' '+id).toLocaleLowerCase();
      var label=document.createElement('label');
      label.className='preference';
      var input=document.createElement('input');
      input.type='checkbox';input.value=id;input.checked=!!checked;
      var meta=document.createElement('span');
      meta.className='group-meta';
      var name=document.createElement('span');
      name.className='group-name';
      name.textContent=groupRowLabel(kind,row);
      meta.appendChild(name);
      appendRecommendation(meta,row);
      label.appendChild(input);label.appendChild(meta);
      wrap.appendChild(label);
      if(kind==='wechat'){
        var alias=document.createElement('input');
        alias.type='text';alias.className='wx-alias';
        alias.placeholder='别名（可选）';
        alias.setAttribute('data-group',id);
        alias.setAttribute('aria-label','群 '+id+' 的别名');
        alias.value=Object.prototype.hasOwnProperty.call(wxAliases,id)?wxAliases[id]:String(row.alias||'');
        wrap.appendChild(alias);
      }
      return wrap;
    }
    function groupCategoryBlock(kind,label,rows,selected){
      var block=document.createElement('div');
      block.className='gsel-cat';
      var heading=document.createElement('h3');
      heading.textContent=label+'（'+rows.length+'）';
      block.appendChild(heading);
      var list=document.createElement('div');
      list.className='gsel-list';
      rows.forEach(function(row){list.appendChild(groupRowNode(kind,row,selected.indexOf(groupIdOf(row))>=0));});
      block.appendChild(list);
      return block;
    }
    function groupFoldButton(boxId,key,label,open){
      var button=document.createElement('button');
      button.type='button';
      button.className='gsel-fold';
      button.dataset.gselFold=key;
      button.setAttribute('aria-expanded',open?'true':'false');
      button.setAttribute('aria-controls',boxId+'-'+key);
      button.appendChild(document.createTextNode(label));
      var chev=document.createElement('span');
      chev.className='gsel-chev';
      chev.setAttribute('aria-hidden','true');
      button.appendChild(chev);
      return button;
    }
    function groupEmptyNode(kind,payload){
      var wrap=document.createElement('div');
      wrap.className='gsel-empty';
      var reason=String(payload.error_hint||payload.error||'').trim();
      var note=document.createElement('p');
      note.className='setting-note';
      note.textContent=reason?('没有解析到群：'+reason+'（确认已登录，再点「再试一次」）'):'没有解析到群。按下面三步自查，再点「再试一次」。';
      wrap.appendChild(note);
      var steps=document.createElement('ol');
      [
        '确认'+(kind==='wechat'?'微信':'钉钉')+'已在这台电脑上登录，和群务台用的是同一个 Windows 账号。',
        kind==='wechat'?'确认「微信数据目录」填写正确（微信 → 设置 → 文件管理 里的位置）。':'确认钉钉正常打开过一次，未停留在登录页。',
        '刚登录或刚升级时索引尚未建立完成，请等待 1 分钟后重试。'
      ].forEach(function(text){var li=document.createElement('li');li.textContent=text;steps.appendChild(li);});
      wrap.appendChild(steps);
      var retry=document.createElement('button');
      retry.type='button';retry.className='btn primary';retry.dataset.gselRetry='1';retry.textContent='再试一次';
      wrap.appendChild(retry);
      return wrap;
    }
    function groupRetry(kind){var button=document.getElementById(kind==='wechat'?'wx-load-groups':'dt-load-groups');if(button&&!button.disabled)button.click();}
    function groupFilterAll(box,query){
      var list=box.querySelector('[data-gsel-list="all"]');
      if(!list)return;
      var text=String(query||'').trim().toLocaleLowerCase();
      var shown=0;
      Array.prototype.forEach.call(list.querySelectorAll('.wx-group-row,.dt-group-row'),function(row){
        var hit=!text||String(row.dataset.search||'').indexOf(text)>=0;
        row.hidden=!hit;
        if(hit)shown+=1;
      });
      var none=box.querySelector('[data-gsel-nomatch]');
      if(none)none.hidden=shown>0;
    }
    function bindGroupPicker(kind,box){
      box.onclick=function(event){
        var target=event.target;
        if(!target||!target.closest)return;
        var fold=target.closest('[data-gsel-fold]');
        if(fold){
          var key=fold.getAttribute('data-gsel-fold');
          var body=box.querySelector('[data-gsel-body="'+key+'"]');
          var open=fold.getAttribute('aria-expanded')==='true';
          fold.setAttribute('aria-expanded',open?'false':'true');
          if(body)body.hidden=open;
          return;
        }
        if(target.closest('[data-gsel-pick-all]')){
          Array.prototype.forEach.call(box.querySelectorAll('.wx-group-row[data-suggested="true"] input[type="checkbox"],.dt-group-row[data-suggested="true"] input[type="checkbox"]'),function(input){
            input.checked=true;
            groupSyncTwins(box,String(input.value||''),true);
          });
          groupUpdateCount(box);
          patchGroupCount(kind,groupPickedCount(box));
          return;
        }
        if(target.closest('[data-gsel-retry]')){groupRetry(kind);return;}
        if(target.closest('[data-gsel-resuggest]')){
          // 建议是随群列表一起返回的（后端没有单独的「重新识别」接口），只能重跑一次拉取。
          var button=target.closest('[data-gsel-resuggest]');
          button.disabled=true;button.textContent='正在重新识别…';
          groupRetry(kind);
        }
      };
      box.onchange=function(event){
        var input=event.target;
        if(!input||input.type!=='checkbox')return;
        groupSyncTwins(box,String(input.value||''),input.checked);
        groupUpdateCount(box);
        patchGroupCount(kind,groupPickedCount(box));
      };
      box.oninput=function(event){
        var node=event.target;
        if(!node||!node.classList)return;
        if(node.classList.contains('wx-alias')){groupSyncAlias(box,node.getAttribute('data-group'),node.value);return;}
        if(node.classList.contains('gsel-search'))groupFilterAll(box,node.value);
      };
    }
    function renderGroupPicker(kind,payload){
      var boxId=groupBoxId(kind);
      var box=document.getElementById(boxId);
      if(!box)return;
      var rows=Array.isArray(payload.groups)?payload.groups:[];
      var harvest=harvestGroupEdits(kind,box,groupSelectedIds(kind),kind==='wechat'?wxAliases:null);
      if(kind==='wechat'){wxSelected=harvest.selected;wxAliases=harvest.aliases;}else{dtSelected=harvest.selected;}
      renderSuggestionSource(kind==='wechat'?'wx-suggest-source':'dt-suggest-source',payload.suggest_source,payload.suggest_error);
      box.textContent='';
      // 占位文案的 .setting-note（灰、小字）不能遗传给整块列表，否则群名全变成小灰字。
      box.classList.remove('setting-note');
      if(!rows.length){box.appendChild(groupEmptyNode(kind,payload));syncSavedGroupRows(kind);return;}
      var selected=groupSelectedIds(kind);
      var aiDown=payload.suggest_source!=='llm';
      var root=document.createElement('div');
      root.className='gsel';
      var top=document.createElement('p');
      top.className='gsel-top';
      var totalSpan=document.createElement('span');
      totalSpan.textContent='共 '+rows.length+' 个群';
      var suggestedSpan=document.createElement('span');
      var suggestedCount=rows.filter(function(row){return row.suggested;}).length;
      suggestedSpan.textContent=(aiDown?'关键词建议 ':'AI 建议订阅 ')+suggestedCount+' 个';
      var pickedSpan=document.createElement('span');
      pickedSpan.setAttribute('data-gsel-count','1');
      pickedSpan.textContent='已选 0 个';
      top.appendChild(totalSpan);top.appendChild(suggestedSpan);top.appendChild(pickedSpan);
      root.appendChild(top);
      if(aiDown){
        var notice=document.createElement('p');
        notice.className='gsel-notice';
        notice.appendChild(document.createTextNode('AI 识别不可用，本批建议按群名关键词归类，仅供参考。'));
        var resuggest=document.createElement('button');
        resuggest.type='button';resuggest.className='btn';resuggest.dataset.gselResuggest='1';resuggest.textContent='重新识别';
        notice.appendChild(resuggest);
        root.appendChild(notice);
      }
      var recRows=rows.filter(function(row){return row.suggested;});
      var recSection=document.createElement('section');
      recSection.className='gsel-sec';
      var recHead=document.createElement('div');
      recHead.className='gsel-sec-head';
      recHead.appendChild(groupFoldButton(boxId,'rec',(aiDown?'关键词建议':'建议订阅')+'（'+recRows.length+' 个）',true));
      var pickAll=document.createElement('button');
      pickAll.type='button';pickAll.className='btn';pickAll.dataset.gselPickAll='1';pickAll.textContent='全选建议';
      recHead.appendChild(pickAll);
      recSection.appendChild(recHead);
      var recBody=document.createElement('div');
      recBody.className='gsel-sec-body';
      recBody.dataset.gselBody='rec';
      recBody.id=boxId+'-rec';
      GROUP_CATS.forEach(function(category){
        var members=recRows.filter(function(row){return String(row.category||'other')===category;});
        if(!members.length)return;
        recBody.appendChild(groupCategoryBlock(kind,groupCategoryName(category),members,selected));
      });
      if(!recRows.length){
        var none=document.createElement('p');
        none.className='setting-note';
        none.textContent='这批没有可推荐的群，可在下方「全部群聊」中勾选。';
        recBody.appendChild(none);
      }
      recSection.appendChild(recBody);
      root.appendChild(recSection);
      var allSection=document.createElement('section');
      allSection.className='gsel-sec';
      var allHead=document.createElement('div');
      allHead.className='gsel-sec-head';
      allHead.appendChild(groupFoldButton(boxId,'all','全部群聊（'+rows.length+' 个）',false));
      allSection.appendChild(allHead);
      var allBody=document.createElement('div');
      allBody.className='gsel-sec-body';
      allBody.dataset.gselBody='all';
      allBody.id=boxId+'-all';
      allBody.hidden=true;
      var search=document.createElement('input');
      search.type='search';search.className='gsel-search';
      search.placeholder='搜群名或群号…';
      search.setAttribute('aria-label','按群名搜索全部群');
      allBody.appendChild(search);
      var allList=document.createElement('div');
      allList.className='gsel-list';
      allList.dataset.gselList='all';
      rows.forEach(function(row){allList.appendChild(groupRowNode(kind,row,selected.indexOf(groupIdOf(row))>=0));});
      allBody.appendChild(allList);
      var nomatch=document.createElement('p');
      nomatch.className='gsel-nomatch';nomatch.dataset.gselNomatch='1';nomatch.hidden=true;
      nomatch.textContent='没有匹配的群。可以换一个关键词。';
      allBody.appendChild(nomatch);
      allSection.appendChild(allBody);
      root.appendChild(allSection);
      box.appendChild(root);
      bindGroupPicker(kind,box);
      syncSavedGroupRows(kind);
      groupUpdateCount(box);
    }
   var dtBox=el('section','surface hosting-settings dingtalk-settings');dtBox.id='dingtalk-settings-card';dtBox.dataset.settingAnchor='dingtalk';
    dtBox.innerHTML='<h2 class="section-title">钉钉</h2><p class="setting-note">只读本机钉钉数据库；仅处理勾选群聊。</p><label class="preference"><input id="dt-enabled" type="checkbox"><span><strong>启用钉钉</strong><small>需要 Windows 上钉钉已登录。</small></span></label><p id="dt-status" class="setting-note" role="status" aria-live="polite">正在检测…</p><p class="setting-note"><button id="dt-reconnect" class="btn" type="button" hidden>重连监听</button></p><p id="dt-scan" class="setting-note" role="status" aria-live="polite"></p><button id="dt-scan-now" class="btn" type="button">立即扫描一次</button><div class="field"><span>我的群</span><button id="dt-load-groups" class="btn" type="button">拉取我的群</button><button id="dt-cancel-groups" class="btn" type="button" hidden>取消拉取</button></div><div id="dt-suggest-source" class="group-source" hidden></div><div id="dt-groups" class="setting-note">点击拉取钉钉群。</div><button id="dt-save" class="btn primary" type="button">保存钉钉设置</button><p id="dt-result" class="setting-note" role="status" aria-live="polite"></p>';
    root.appendChild(dtBox);
    var dtSelected=[];var dtLoaded=false;
    function renderDingtalkGroups(payload){dtLoaded=true;renderGroupPicker('dingtalk',payload);}
    api('/api/settings').then(function(v){document.getElementById('dt-enabled').checked=!!v.dingtalk_enabled;dtSelected=v.dingtalk_group_whitelist||[];syncSavedGroupRows('dingtalk');});
    // 扫描启动失败的真原因不能被 1.5 秒一次的轮询冲掉：拦 30 秒（同 guide.js 的 holdSetupStatus 写法）。
    var wxScanHoldUntil=0;
    function updateScanStatus(source){if(source==='wechat'&&Date.now()<wxScanHoldUntil)return;api('/api/'+source+'/status').then(function(v){var target=document.getElementById(source==='dingtalk'?'dt-scan':'wx-scan');if(!target)return;if(v.scan_state==='running')target.textContent='⟳ '+(v.scan_stage||'正在扫描…')+' 已用 '+Number(v.scan_elapsed_seconds||0).toFixed(1)+' 秒';else if(v.scan_state==='error'){var scanMsg=v.error_hint||v.last_error||v.scan_stage||'未知错误';if(v.detail)scanMsg+='（'+v.detail+'）';target.textContent='扫描失败：'+scanMsg;}else if(v.last_read_at)target.textContent='最近读取：'+v.last_read_at+(v.last_read_seconds?'，用时 '+Number(v.last_read_seconds).toFixed(1)+' 秒':'');else target.textContent=v.scan_state==='disabled'?'扫描未启用':'等待扫描';}).catch(function(e){var target=document.getElementById(source==='dingtalk'?'dt-scan':'wx-scan');if(target)target.textContent='状态读取失败：'+e.message;});}
 function refreshDingtalkStatus(){api('/api/dingtalk/status').then(function(v){var node=document.getElementById('dt-status');node.textContent=v.state||'状态未知';node.dataset.state=v.running?'ok':(v.enabled?'warn':'');var reconnect=document.getElementById('dt-reconnect');if(reconnect)reconnect.hidden=!(v.enabled&&!v.running&&Number(v.groups||0)>0);updateScanStatus('dingtalk');}).catch(function(e){document.getElementById('dt-status').textContent='状态读取失败：'+e.message;});}
    refreshDingtalkStatus();
    setupGroupPull({path:'/api/dingtalk/groups',boxId:'dt-groups',buttonId:'dt-load-groups',cancelButtonId:'dt-cancel-groups',idleButtonText:'拉取我的群',busyButtonText:'正在拉取…',busyCopy:'正在读取钉钉群…',longWaitCopy:function(){return '首次读取可能较久，正在扫描数据库…';},render:renderDingtalkGroups});
     document.getElementById('dt-scan-now').onclick=function(){var button=this;button.disabled=true;api('/api/dingtalk/scan',{method:'POST',body:'{}'}).then(function(v){if(v.started===false){var box=document.getElementById('dt-scan');if(v.already_running){box.textContent=v.message||'扫描正在进行中，等这一轮跑完会自动刷新。';updateScanStatus('dingtalk');}else if(v.ok){box.textContent='未开始扫描：'+(v.error||'先勾选并保存群，或先启用钉钉');}else{box.textContent='扫描启动失败：'+(v.error||'未知原因');}return;}if(!v.ok)throw new Error(v.error||'扫描失败');updateScanStatus('dingtalk');}).catch(function(e){document.getElementById('dt-scan').textContent='扫描启动失败：'+e.message;}).then(function(){button.disabled=false;});}; dingtalkScanTimer = setInterval(function(){updateScanStatus('dingtalk');},1500);
// 「重连监听」与「保存钉钉设置」走同一条链路：重新提交当前配置，后端就会重连监听。
function saveDingtalkSettings(){var button=document.getElementById('dt-save');if(button)button.disabled=true;var boxes=document.querySelectorAll('#dt-groups input[type=checkbox]');var groups=[];var seenGroups={};Array.prototype.forEach.call(boxes,function(x){if(x.checked&&!seenGroups[x.value]){seenGroups[x.value]=true;groups.push(x.value);}});api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dingtalk_enabled:document.getElementById('dt-enabled').checked,dingtalk_group_whitelist:boxes.length?groups:dtSelected.slice()})}).then(function(v){if(!v.ok)throw new Error(v.error||'保存失败');if(boxes.length)dtSelected=groups.slice();document.getElementById('dt-result').textContent='已保存，正在重新连接监听…';refreshDingtalkStatus();}).catch(function(e){dtLoaded=false;document.getElementById('dt-result').textContent='保存失败：'+e.message;}).then(function(){if(button)button.disabled=false;});}
document.getElementById('dt-save').onclick=saveDingtalkSettings;
document.getElementById('dt-reconnect').onclick=saveDingtalkSettings;
    var wxBox=el('section','surface hosting-settings wechat-settings');wxBox.id='wechat-settings-card';wxBox.dataset.settingAnchor='wechat';
    wxBox.innerHTML='<h2 class="section-title">微信</h2><p class="setting-note"><strong>首选：让 AI 助手代做。</strong>请把下面这句话完整发给你正在使用的 AI 助手（Codex、Claude Code，或任何能在这台电脑上跑命令的助手），它会把密钥取好并写进 .env：</p><p class="setting-note wx-agent-ask"><code>帮我取微信密钥并写进群务台的 .env：在 F:\\qq-notice-hub 目录用 .venv\\Scripts\\python.exe 运行 -m noticehub.wechat_receiver --write-env F:\\notice-hub\\.env；成功会打印「已写入」，失败会说明卡在哪一步。</code></p><p class="setting-note">前提：微信在这台电脑上已登录、助手有管理员权限；首次通常 3–6 分钟（本机实测 190–390 秒），读出密钥后会缓存，之后拉群是几秒。全程只读不发送；扫描微信进程内存可能触发风控，请自行评估。</p><details id="wx-key-guide"><summary>次选：自己手动取（展开手动步骤）</summary><ol class="setting-note wx-key-steps"><li>先准备一个开源提取工具：在 GitHub 上搜 <a href="https://github.com/search?q=wechat-dump-rs&amp;type=repositories" target="_blank" rel="noopener noreferrer">wechat-dump-rs</a> 或 <a href="https://github.com/search?q=PyWxDump&amp;type=repositories" target="_blank" rel="noopener noreferrer">PyWxDump</a>（同名仓库有好几个，挑星标最多的那个），按它的说明在这台电脑上运行一次。群务台不内置这些工具，也不为其背书，运行前请先阅读该工具的说明。</li><li>按工具提示取出当前登录微信的密钥：一般是 32 字节，工具通常会直接显示成 64 位十六进制（0-9、a-f）。</li><li>把 64 位十六进制粘到下面，点「保存密钥」。保存后立即生效，不用重启。</li></ol><label class="field"><span>微信数据库密钥（64 位十六进制）</span><input id="wx-key" type="text" autocomplete="off" spellcheck="false" placeholder="粘贴 64 位十六进制，例如 0123456789abcdef…"></label><button id="wx-key-save" class="btn primary" type="button">保存密钥</button><p id="wx-key-status" class="setting-note" role="status" aria-live="polite">正在读取密钥状态…</p><p class="setting-note">如果确实取不到，也可以在本目录 .env 里加一行 <code>QQ_DIGEST_WECHAT_KEY=&lt;64 位十六进制 raw key&gt;</code>（旧方法，不推荐）。</p></details><label class="field"><span>微信数据目录（可选）</span><input id="wx-dir-input" type="text" autocomplete="off" spellcheck="false" placeholder="D:\\Wechat files"></label><button id="wx-dir-save" class="btn" type="button">保存目录</button><p class="setting-note">留空 = 自动查找。自动找不到、或改过微信存储位置时，填微信「设置 → 文件管理」里显示的存储位置目录（如 D:\\Wechat files），也可以填 …\\xwechat_files 或某个账号的 …\\db_storage。<strong>保存后立即生效，不用重启群务台。</strong></p><p id="wx-dir-status" class="setting-note" role="status" aria-live="polite"></p><label class="preference"><input id="wx-enabled" type="checkbox"><span><strong>启用微信</strong><small>需要 Windows 上微信已登录。</small></span></label><p id="wx-status" class="setting-note" role="status" aria-live="polite">正在检测…</p><p id="wx-storage-hint" class="setting-note" role="status" aria-live="polite" hidden></p><p id="wx-scan" class="setting-note" role="status" aria-live="polite"></p><button id="wx-scan-retry" class="btn" type="button" hidden>重试扫描</button><button id="wx-scan-now" class="btn" type="button">立即扫描一次</button><div class="field"><span>我的群</span><button id="wx-load-groups" class="btn" type="button">拉取我的群</button><button id="wx-cancel-groups" class="btn" type="button" hidden>取消拉取</button></div><div id="wx-suggest-source" class="group-source" hidden></div><div id="wx-groups" class="setting-note">点击拉取微信群（首次可能要几分钟）。</div><label class="field"><span>扫描间隔（秒）</span><input id="wx-interval" type="number" min="30" step="30" inputmode="numeric"></label><button id="wx-save" class="btn primary" type="button">保存微信设置</button><p id="wx-result" class="setting-note" role="status" aria-live="polite"></p>';
    root.appendChild(wxBox);
    var wxSelected=[];var wxLoaded=false;var wxAliases={};
    // C：已保存白名单里的群即使没拉到列表也要有行可操作——渲染成「id + 移除」，
    // 移除只改本地选中集合，等用户点「保存微信设置/保存钉钉设置」才生效，不偷偷自动保存。
    function groupSelectionIds(kind){return (kind==='wechat'?wxSelected:dtSelected)||[];}
    function currentGroupCount(kind){
      var box=document.getElementById(kind==='wechat'?'wx-groups':'dt-groups');
      return box?box.querySelectorAll('input[type="checkbox"]:checked').length:0;
    }
    function patchGroupCount(kind,count){
      var node=document.getElementById(kind==='wechat'?'wx-status':'dt-status');
      if(!node)return;
      node.textContent=node.textContent.replace(/已选 \d+ 个群/,'已选 '+count+' 个群');
    }
    function removeGroupSelection(kind,id,wrap,input){
      id=String(id);
      if(input)input.checked=false;
      var list=groupSelectionIds(kind),next=[];
      for(var i=0;i<list.length;i+=1)if(String(list[i])!==id)next.push(String(list[i]));
      if(kind==='wechat')wxSelected=next;else dtSelected=next;
      // 拉取来的行保留（取消勾选即可再选回）；已保存白名单那行删掉，别留个空勾选框。
      if(wrap&&wrap.getAttribute('data-saved-group'))wrap.remove();
      patchGroupCount(kind,currentGroupCount(kind));
    }
    function appendSavedGroupRow(kind, box, id){
      var wrap=document.createElement('div');
      wrap.className='preference '+(kind==='wechat'?'wx-group-row':'dt-group-row');
      wrap.setAttribute('data-saved-group','1');
      var label=document.createElement('label');
      label.className='preference';
      var input=document.createElement('input');
      input.type='checkbox';input.value=id;input.checked=true;
      input.setAttribute('aria-label','已选群 '+id);
      var meta=document.createElement('span');
      meta.className='group-meta';
      var name=document.createElement('span');
      name.className='group-name';
      name.textContent=id;   // 没拉到群名就显示 id 本身，不编「未命名」
      meta.appendChild(name);
      label.appendChild(input);
      label.appendChild(meta);
      wrap.appendChild(label);
      var remove=document.createElement('button');
      remove.type='button';
      remove.className='btn';
      remove.textContent='移除';
      remove.setAttribute('aria-label','移除已选群 '+id);
      // 取消勾选也同步到本地选中集合，别让「保存」再把它提交回去。
      input.onchange=function(){
        var list=groupSelectionIds(kind),next=[],found=false;
        for(var k=0;k<list.length;k+=1){
          if(String(list[k])===id){found=true;if(input.checked)next.push(id);}
          else next.push(String(list[k]));
        }
        if(input.checked&&!found)next.push(id);
        if(kind==='wechat')wxSelected=next;else dtSelected=next;
        patchGroupCount(kind,currentGroupCount(kind));
      };
      remove.onclick=function(){removeGroupSelection(kind,id,wrap,input);};
      wrap.appendChild(remove);
      var target=box.querySelector('[data-gsel-list="all"]')||box;
      target.appendChild(wrap);
    }
    function syncSavedGroupRows(kind){
      var boxId=kind==='wechat'?'wx-groups':'dt-groups';
      var box=document.getElementById(boxId);
      if(!box)return;
      var stale=box.querySelectorAll('[data-saved-group]');
      for(var i=0;i<stale.length;i+=1)stale[i].remove();
      var ids=groupSelectionIds(kind);
      for(var j=0;j<ids.length;j+=1){
        var id=String(ids[j]||'').trim();
        if(!id)continue;
        if(box.querySelector('input[value="'+id+'"]'))continue;   // 拉取行已覆盖，不重复
        appendSavedGroupRow(kind,box,id);
      }
    }
    function renderWechatGroups(payload){wxLoaded=true;renderGroupPicker('wechat',payload);}
    function refreshWechatStatus(){api('/api/wechat/status').then(function(v){updateScanStatus('wechat');var node=document.getElementById('wx-status');var parts=[v.state||'状态未知'];var groups=Number(v.groups||0);if(!v.running&&groups>0)parts.push('已选 '+groups+' 个群');if(v.last_read_at)parts.push('上次读取 '+v.last_read_at+'（'+v.last_read_seconds+'s）');if(v.manual_key)parts.push('正在用你手填的密钥');if(v.storage_dir)parts.push('数据目录：'+v.storage_dir);var failed=v.scan_state==='error';var hasError=!!(failed||v.last_error);if(v.error_hint&&hasError)parts.push(v.error_hint);node.textContent=parts.join(' · ');node.dataset.state=hasError||v.available===false?'error':(v.running?'ok':'');if(v.last_error)node.title='原始报错：'+v.last_error;else node.removeAttribute('title');var hintEl=document.getElementById('wx-storage-hint');if(hintEl){var hint=String(v.hint||'').trim();hintEl.hidden=!hint;hintEl.textContent=hint;}var retry=document.getElementById('wx-scan-retry');if(retry)retry.hidden=!(failed||(v.error_hint&&hasError));refreshWechatKeyStatus(!!v.manual_key);}).catch(function(e){document.getElementById('wx-status').textContent='状态读取失败：'+e.message;});}
    // —— 微信 4.1.x 手动密钥引导：只做「校验 + 提交」，不假装成功、不回显内容 ——
    var wxKeySavedInSession = false;
function refreshWechatKeyStatus(manual) {
  var node = document.getElementById('wx-key-status');
  if (!node) return;
  // 保存后那句「已保存并生效」只活到下一次状态刷新：之后以服务端 manual_key 为准，
  // 否则密钥被清掉/失效了这里还一直说已配置。
  if (wxKeySavedInSession) wxKeySavedInSession = false;
  node.textContent = manual ? '已配置密钥（不回显内容）；改完保存立即生效，不用重启。' : '还没配置密钥。自动提取能成功的话不用填这一步。';
  node.dataset.state = manual ? 'ok' : '';
}
function normalizeWechatKey(raw) {
  return String(raw === null || raw === undefined ? '' : raw).replace(/\s+/g, '').toLowerCase();
}
function wechatKeyProblem(key) {
  if (!key) return '请先粘贴 64 位十六进制密钥。';
  if (!/^[0-9a-f]{64}$/.test(key)) return '密钥必须是 64 位十六进制（0-9、a-f）。当前为 ' + key.length + ' 位，请检查是否有漏粘或多粘。';
  return '';
}
function setWechatKeyStatus(text, state) {
  var node = document.getElementById('wx-key-status');
  if (!node) return;
  node.textContent = text;
  node.dataset.state = state || '';
}
function setWechatDirStatus(text, state) {
  var node = document.getElementById('wx-dir-status');
  if (!node) return;
  node.textContent = text;
  node.dataset.state = state || '';
}
function saveWechatKey() {
  var input = document.getElementById('wx-key');
  var button = document.getElementById('wx-key-save');
  if (!input) return;
  var key = normalizeWechatKey(input.value);
  var problem = wechatKeyProblem(key);
  if (problem) { setWechatKeyStatus(problem, 'error'); input.focus(); return; }
  if (button) button.disabled = true;
  setWechatKeyStatus('正在保存…', '');
  api('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({wechat_key: key})}).then(function (v) {
    if (!v || !v.ok) throw new Error((v && v.error) || '保存失败');
    input.value = '';
    wxKeySavedInSession = true;
    setWechatKeyStatus('已保存并生效（不回显内容）。', 'ok');
  }).catch(function (error) {
    var message = (error && error.message) || '保存失败';
    // 后端还没接 wechat_key 时会回「没有可保存的键」：说清楚是版本问题，别让人以为密钥填错了。
    if (message.indexOf('没有可保存的键') >= 0) {
      setWechatKeyStatus('这个版本还不能在页面里保存密钥。可以先用上面 .env 的办法，等更新后再回来。', 'error');
    } else {
      setWechatKeyStatus('保存失败：' + message, 'error');
    }
  }).then(function () { if (button) button.disabled = false; });
}
    api('/api/settings').then(function(v){document.getElementById('wx-enabled').checked=!!v.wechat_enabled;wxSelected=v.wechat_group_whitelist||[];wxAliases=v.wechat_group_aliases||{};var keep=settingsUnsaved||{};var intervalInput=document.getElementById('wx-interval');if(!keep['wx-interval'])intervalInput.value=v.wechat_scan_interval_seconds||300;var dirInput=document.getElementById('wx-dir-input');if(dirInput&&!keep['wx-dir-input'])dirInput.value=typeof v.wechat_dir==='string'?v.wechat_dir:'';syncSavedGroupRows('wechat');});
    // 微信数据目录：后端 POST /api/settings 的 wechat_dir，空=清空回到自动查找，保存后立即生效。
    function saveWechatDir(){
      var input=document.getElementById('wx-dir-input');
      var button=document.getElementById('wx-dir-save');
      if(!input)return;
      var value=String(input.value||'').trim();
      if(button)button.disabled=true;
      setWechatDirStatus('正在保存…','');
      api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({wechat_dir:value})}).then(function(v){
        if(v&&v.ok===false)throw new Error(v.error||'保存失败');
        var saved=v&&typeof v.wechat_dir==='string'?v.wechat_dir:value;
        input.value=saved;
        // 已保存成功：标记清掉，下次重建设置页按服务端真值走，不把旧值当「未保存的编辑」。
        if(input.dataset)delete input.dataset.nhDirty;
        setWechatDirStatus(saved?'已保存，立即生效。':'已保存，立即生效。现在自动查找微信数据目录。','ok');
        // 目录换了，旧的群列表就不作数了：清掉并提示重新拉，避免对着上个目录的群去保存。
        var groupsBox=document.getElementById('wx-groups');
        if(groupsBox)groupsBox.textContent='数据目录已更换：点「拉取我的群」重新读取新目录里的群。已保存的白名单不会因此清空。';
        wxLoaded=false;
        refreshWechatStatus();
      }).catch(function(e){
        // 后端原文直接给用户看（例如「微信数据目录不存在：…」），别吞成通用失败。
        setWechatDirStatus('保存失败：'+String((e&&e.message)||e),'error');
      }).then(function(){if(button)button.disabled=false;});
    }
    document.getElementById('wx-dir-save').onclick=saveWechatDir;
    document.getElementById('wx-dir-input').addEventListener('keydown',function(event){if(event.key==='Enter'){event.preventDefault();saveWechatDir();}});
     function saveEnabledToggle(inputId,resultId,key,after){var input=document.getElementById(inputId),result=document.getElementById(resultId);input.addEventListener('change',function(){var enabled=input.checked;input.disabled=true;var payload={};payload[key]=enabled;api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(v){if(!v.ok)throw new Error(v.error||'保存失败');result.textContent=enabled?'已启用':'已关闭';if(after)after();}).catch(function(e){input.checked=!enabled;result.textContent='保存失败，已恢复原状态：'+e.message;}).then(function(){input.disabled=false;});});}
     saveEnabledToggle('wx-enabled','wx-result','wechat_enabled');
     // 钉钉勾选即存，和微信动作一致：以前只勾不点「保存钉钉设置」，后端仍是未启用，用户读作「未应用」。
     saveEnabledToggle('dt-enabled','dt-result','dingtalk_enabled',refreshDingtalkStatus);
    // 实验室：默认收起，勾选即存（写 .env 的 QQ_DIGEST_LAB_*，后端热生效），不用再手改 .env。
    var labBox=el('section','surface hosting-settings lab-settings');labBox.id='lab-settings-card';labBox.dataset.settingAnchor='lab';
    labBox.innerHTML='<details id="lab-details"><summary>实验室（实验性功能）</summary><p class="setting-note">这些功能还在打磨，默认关闭；勾上就生效，也会记到本机 .env 里，随时可以关掉。</p><label class="preference"><input id="lab-search" type="checkbox"><span><strong>站内搜索</strong><small>在已归档的历史消息里按关键词搜索。</small></span></label><label class="preference"><input id="lab-replies" type="checkbox"><span><strong>回帖建议</strong><small>给群消息生成可以照着回的草稿。</small></span></label><label class="preference"><input id="lab-events" type="checkbox"><span><strong>从聊天里抽事件</strong><small>把「明天开会」这类消息认成日程。</small></span></label><label class="preference"><input id="lab-rules" type="checkbox"><span><strong>自动整理规则</strong><small>按你自己写的规则把消息归类。</small></span></label><p id="lab-result" class="setting-note" role="status" aria-live="polite">正在读取实验室状态…</p><div id="lab-rules-editor" hidden><div id="lab-rules-list" class="lab-rule-list"></div><p id="lab-rules-empty" class="setting-note" hidden>还没有规则，点下面的「+ 添加一条规则」开始。</p><div class="lab-rule-actions"><button id="lab-rules-add" class="btn" type="button">+ 添加一条规则</button><button id="lab-rules-save" class="btn primary" type="button">保存规则</button></div><p id="lab-rules-status" class="setting-note" role="status" aria-live="polite"></p><details id="lab-rules-testbox"><summary>试跑：看看一条消息会被哪些规则命中</summary><label class="field"><span>试跑用的消息正文</span><input id="lab-rules-sample" type="text" placeholder="粘一条群消息，看规则会不会命中"></label><button id="lab-rules-test" class="btn" type="button">试跑一条消息</button></details></div></details>';
    root.appendChild(labBox);
    api('/api/lab/status').then(function(v){var on=(v&&v.enabled)||{};['search','replies','events','rules'].forEach(function(name){var input=document.getElementById('lab-'+name);if(input)input.checked=!!on[name];});var box=document.getElementById('lab-result');if(box)box.textContent='勾选即保存；当前已开：'+(['search','replies','events','rules'].filter(function(n){return !!on[n];}).join('、')||'无');
      // 开关本来就开着时（.env 里已有 QQ_DIGEST_LAB_RULES=1），编辑器要在首屏就展开并把规则读进来，
      // 不能等用户手动关了再开——那样刷新一次页面就看不见自己的规则了。
      if(rulesToggle){showRulesEditor(rulesToggle.checked);if(rulesToggle.checked)loadRulesIntoEditor();}
    }).catch(function(e){var box=document.getElementById('lab-result');if(box)box.textContent='实验室状态读取失败：'+e.message;});
    saveEnabledToggle('lab-search','lab-result','lab_search');
    saveEnabledToggle('lab-replies','lab-result','lab_replies');
    saveEnabledToggle('lab-events','lab-result','lab_events');
    saveEnabledToggle('lab-rules','lab-result','lab_rules');
    // 规则编辑器：一条规则一张卡片，用户看不到 JSON。规则存在 <data_dir>/rules.json，页面直接读写。
    // 数据变换（后端规则 <-> 卡片）全在 js/lab-rules.js 的 NhLabRules 里，这里只管渲染和读表单。
    var rulesEditor=document.getElementById('lab-rules-editor');
    var rulesList=document.getElementById('lab-rules-list');
    var rulesEmpty=document.getElementById('lab-rules-empty');
    // 任何在卡片里敲过的输入都算「未保存编辑」：装载响应回来时要保留（loadRulesKeepingEdits）。
    if(rulesList){
      rulesList.addEventListener('input',function(event){var node=event.target&&event.target.closest?event.target.closest('.lab-rule-card'):null;if(node)node.dataset.dirty='1';});
      rulesList.addEventListener('change',function(event){var node=event.target&&event.target.closest?event.target.closest('.lab-rule-card'):null;if(node)node.dataset.dirty='1';});
    }
    var rulesStatus=document.getElementById('lab-rules-status');
    var CATEGORY_NAMES=window.CATEGORY_LABELS||{urgent:'紧急',action:'待办',academic:'学业',info:'通知'};
    function setRulesStatus(text,state){if(!rulesStatus)return;rulesStatus.textContent=text;rulesStatus.dataset.state=state||'';}
    function rulesToolkit(){return window.NhLabRules||null;}
    function showRulesEditor(on){if(rulesEditor)rulesEditor.hidden=!on;}
    function refreshRulesEmpty(){if(rulesEmpty)rulesEmpty.hidden=!!(rulesList&&rulesList.children.length);}
    function categoryOptions(current){return ['urgent','action','academic','info'].map(function(key){return '<option value="'+key+'"'+(key===current?' selected':'')+'>'+(CATEGORY_NAMES[key]||key)+'</option>';}).join('');}
    function ruleField(node,selector){return node.querySelector(selector);}
    function makeRuleCard(card){
      card=card||{};
      var node=document.createElement('div');node.className='lab-rule-card';
      node.__nhRaw=card.raw||null;
      node.dataset.ruleId=card.id||'';   // 服务端原来的名字：改名后刷新时用它认出旧卡，别留一张幽灵卡
      node.innerHTML='<div class="lab-rule-head"><label class="field"><span>规则名称</span><input class="lab-rule-name" type="text" placeholder="例如：班委通知" autocomplete="off"></label><button class="btn ghost lab-rule-remove" type="button">删除这条规则</button></div>'
        +'<fieldset class="lab-rule-group"><legend>什么时候命中</legend>'
        +'<label class="field"><span>关键词</span><input class="lab-rule-keywords" type="text" placeholder="报名, 截止, 开会" autocomplete="off"></label>'
        +'<label class="preference"><input class="lab-rule-atme" type="checkbox"><span><strong>只处理 @我 的消息</strong><small>消息里真的 @ 到你才算命中。</small></span></label>'
        +'<label class="preference"><input class="lab-rule-useregex" type="checkbox"><span><strong>使用正则（高级）</strong><small>不确定会写正则就别勾。</small></span></label>'
        +'<label class="field lab-rule-regex" hidden><span>正则（逗号分隔多条）</span><input class="lab-rule-regextext" type="text" placeholder="如 ^【通知】" autocomplete="off"></label>'
        +'<label class="field"><span>指定发件人</span><input class="lab-rule-senders" type="text" placeholder="QQ 号或昵称，逗号分隔；留空不限" autocomplete="off"></label>'
        +'<p class="setting-note lab-rule-hint">关键词、正则、指定发件人、只处理 @我 —— 这四项至少填一项。</p>'
        +'</fieldset>'
        +'<fieldset class="lab-rule-group"><legend>命中后生成待办</legend>'
        +'<label class="field"><span>待办标题</span><input class="lab-rule-summary" type="text" placeholder="留空就用消息第一行当标题" autocomplete="off"></label>'
        +'<label class="field"><span>分类</span><select class="lab-rule-category">'+categoryOptions(card.category)+'</select></label>'
        +'</fieldset>'
        +'<fieldset class="lab-rule-group"><legend>范围</legend>'
        +'<label class="field"><span>处理哪些群</span><select class="lab-rule-scope"><option value="all">所有群</option><option value="groups">只处理指定群</option></select></label>'
        +'<label class="field lab-rule-groups" hidden><span>群 ID（逗号分隔）</span><input class="lab-rule-grouptext" type="text" placeholder="例如：123456789, 987654321" autocomplete="off"></label>'
        +'</fieldset>'
        +'<label class="preference"><input class="lab-rule-enabled" type="checkbox"><span><strong>启用这条规则</strong><small>关掉就保留规则但不生效。</small></span></label>'
        +'<p class="lab-rule-error" role="alert" hidden></p>';
      ruleField(node,'.lab-rule-name').value=card.id||'';
      ruleField(node,'.lab-rule-keywords').value=card.keywordText||'';
      ruleField(node,'.lab-rule-senders').value=card.senderText||'';
      ruleField(node,'.lab-rule-regextext').value=card.regexText||'';
      ruleField(node,'.lab-rule-summary').value=card.summary||'';
      ruleField(node,'.lab-rule-grouptext').value=card.scopeText||'';
      ruleField(node,'.lab-rule-atme').checked=card.atMe===true;
      ruleField(node,'.lab-rule-useregex').checked=card.useRegex===true;
      ruleField(node,'.lab-rule-enabled').checked=card.enabled!==false;
      ruleField(node,'.lab-rule-scope').value=card.scopeMode==='groups'?'groups':'all';
      ruleField(node,'.lab-rule-regex').hidden=card.useRegex!==true;
      ruleField(node,'.lab-rule-groups').hidden=card.scopeMode!=='groups';
      ruleField(node,'.lab-rule-useregex').addEventListener('change',function(event){ruleField(node,'.lab-rule-regex').hidden=!event.target.checked;});
      ruleField(node,'.lab-rule-scope').addEventListener('change',function(event){ruleField(node,'.lab-rule-groups').hidden=event.target.value!=='groups';});
      ruleField(node,'.lab-rule-remove').addEventListener('click',function(){node.remove();refreshRulesEmpty();});
      return node;
    }
    function readRuleCard(node){
      return {
        id:ruleField(node,'.lab-rule-name').value,
        enabled:ruleField(node,'.lab-rule-enabled').checked,
        scopeMode:ruleField(node,'.lab-rule-scope').value,
        scopeText:ruleField(node,'.lab-rule-grouptext').value,
        keywordText:ruleField(node,'.lab-rule-keywords').value,
        atMe:ruleField(node,'.lab-rule-atme').checked,
        useRegex:ruleField(node,'.lab-rule-useregex').checked,
        regexText:ruleField(node,'.lab-rule-regextext').value,
        senderText:ruleField(node,'.lab-rule-senders').value,
        summary:ruleField(node,'.lab-rule-summary').value,
        category:ruleField(node,'.lab-rule-category').value,
        raw:node.__nhRaw||null
      };
    }
    function ruleNodes(){return Array.prototype.slice.call(document.querySelectorAll('#lab-rules-list .lab-rule-card'));}
    function clearRuleErrors(nodes){nodes.forEach(function(node){node.classList.remove('is-invalid');var box=ruleField(node,'.lab-rule-error');if(box){box.hidden=true;box.textContent='';}});}
    function markRuleError(message,nodes){
      var match=/第\s*(\d+)\s*条/.exec(String(message||''));
      var index=match?Number(match[1])-1:0;
      var node=nodes[index]||nodes[0];
      if(!node)return;
      node.classList.add('is-invalid');
      var box=ruleField(node,'.lab-rule-error');
      if(box){box.hidden=false;box.textContent=String(message||'');}
    }
    function renderRuleCards(cards){
      if(!rulesList)return;
      rulesList.textContent='';
      (cards||[]).forEach(function(card){rulesList.appendChild(makeRuleCard(card));});
      refreshRulesEmpty();
    }
    function loadRulesIntoEditor(){
      var toolkit=rulesToolkit();
      if(!rulesList||!toolkit){setRulesStatus('规则组件没加载成功，请刷新页面重试。','error');return;}
      // 装载期间用户可能已点「新增」/改过卡片：响应回来不能把它冲掉（未保存的行原样接回末尾）。
      loadRulesKeepingEdits({
        list:rulesList,
        request:function(){return api('/api/lab/rules');},
        toCards:function(rules){return toolkit.rulesToCards(rules);},
        render:renderRuleCards,
        refreshEmpty:refreshRulesEmpty,
        readId:function(node){var name=node.querySelector?node.querySelector('.lab-rule-name'):null;return name?String(name.value||'').trim():'';}
      }).then(function(count){setRulesStatus(count?('已载入 '+count+' 条规则。'):'','');})
        .catch(function(e){setRulesStatus('读取规则失败：'+e.message,'error');});
    }
    var rulesToggle=document.getElementById('lab-rules');
    if(rulesToggle){showRulesEditor(rulesToggle.checked);rulesToggle.addEventListener('change',function(){showRulesEditor(rulesToggle.checked);if(rulesToggle.checked)loadRulesIntoEditor();});}
    var rulesAdd=document.getElementById('lab-rules-add');
    if(rulesAdd)rulesAdd.onclick=function(){
      if(!rulesList)return;
      var before=ruleNodes().length;
      var freshRule=makeRuleCard({category:'action',enabled:true,id:''});
      freshRule.dataset.dirty='1';   // 未保存行：装载响应回来时要保住它
      rulesList.appendChild(freshRule);
      refreshRulesEmpty();
      var added=ruleNodes()[before];
      if(added){var field=ruleField(added,'.lab-rule-keywords');if(field)field.focus();}
    };
    var rulesSave=document.getElementById('lab-rules-save');
    if(rulesSave)rulesSave.onclick=function(){
      var toolkit=rulesToolkit();
      var nodes=ruleNodes();
      if(!toolkit){setRulesStatus('规则组件没加载成功，请刷新页面重试。','error');return;}
      var mapped=toolkit.cardsToRules(nodes.map(readRuleCard));
      clearRuleErrors(nodes);
      if(!mapped.ok){markRuleError(mapped.error,nodes);setRulesStatus(mapped.error,'error');return;}
      rulesSave.disabled=true;
      api('/api/lab/rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rules:mapped.rules})}).then(function(v){
        if(!v.ok)throw new Error(v.error||'保存失败');
        nodes.forEach(function(node){node.dataset.ruleId=String(ruleField(node,'.lab-rule-name').value||'').trim();});
        setRulesStatus('已保存 '+v.count+' 条规则。','ok');
      }).catch(function(e){var message=String(e&&e.message||e);markRuleError(message,nodes);setRulesStatus('保存失败：'+message,'error');}).then(function(){rulesSave.disabled=false;});
    };
    var rulesTest=document.getElementById('lab-rules-test');
    if(rulesTest)rulesTest.onclick=function(){
      var sample=document.getElementById('lab-rules-sample');
      var text=(sample&&sample.value||'').trim();
      if(!text){setRulesStatus('先粘一条要试的消息。','error');return;}
      rulesTest.disabled=true;
      api('/api/lab/rules/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:{content:text,group_id:'g1'}})}).then(function(v){
        if(!v.ok)throw new Error(v.error||'试跑失败');
        var hits=(v&&v.hits)||[];
        setRulesStatus(hits.length?('命中 '+hits.length+' 条：'+hits.map(function(h){return h.rule_id;}).join('、')):'没有规则命中这条消息。',hits.length?'ok':'');
      }).catch(function(e){setRulesStatus('试跑失败：'+e.message,'error');}).then(function(){rulesTest.disabled=false;});
    };
    refreshWechatStatus();
    setupGroupPull({path:'/api/wechat/groups',boxId:'wx-groups',buttonId:'wx-load-groups',cancelButtonId:'wx-cancel-groups',idleButtonText:'拉取我的群',busyButtonText:'正在拉取…',busyCopy:'正在读取微信群…首次通常 3–6 分钟（要扫描微信进程内存定位密钥），可以点「取消拉取」，页面先放着不用守着。',longWaitCopy:function(seconds){return '正在扫描微信进程内存定位密钥，首次通常 3–6 分钟（已等 '+seconds+' 秒），请别关窗口…';},render:renderWechatGroups,after:refreshWechatStatus});
    // 「立即扫描一次」和「重试扫描」是同一条链路：失败了就点重试，不用自己找按钮。
    function scanWechat(){var button=document.getElementById('wx-scan-now');var retry=document.getElementById('wx-scan-retry');if(button)button.disabled=true;if(retry)retry.disabled=true;api('/api/wechat/scan',{method:'POST',body:'{}'}).then(function(v){var box=document.getElementById('wx-scan');if(v.started===false){if(v.already_running){box.textContent=v.message||'扫描正在进行中，等这一轮跑完会自动刷新。';updateScanStatus('wechat');}else if(v.ok){wxScanHoldUntil=Date.now()+30000;box.textContent='未开始扫描：'+(v.error||'先勾选「我的群」并保存，或先启用微信');if(retry)retry.hidden=false;}else{wxScanHoldUntil=Date.now()+30000;var why=String(v.error||'未知原因');if(v.detail)why+='（'+v.detail+'）';box.textContent='扫描启动失败：'+why;if(retry)retry.hidden=false;}return;}if(!v.ok)throw new Error(v.error||'扫描失败');box.textContent='正在扫描…';updateScanStatus('wechat');}).catch(function(e){wxScanHoldUntil=Date.now()+30000;var box=document.getElementById('wx-scan');box.textContent='扫描启动失败：'+String((e&&e.message)||e);if(retry)retry.hidden=false;}).then(function(){if(button)button.disabled=false;if(retry)retry.disabled=false;refreshWechatStatus();});}
    document.getElementById('wx-scan-now').onclick=scanWechat;
    document.getElementById('wx-scan-retry').onclick=scanWechat; wechatScanTimer = setInterval(function(){updateScanStatus('wechat');},1500);
    document.getElementById('wx-key-save').onclick=saveWechatKey;
    document.getElementById('wx-key').addEventListener('keydown',function(event){if(event.key==='Enter'){event.preventDefault();saveWechatKey();}});
document.getElementById('wx-save').onclick=function(){var boxes=document.querySelectorAll('#wx-groups input[type=checkbox]');var groups=[];var seenGroups={};Array.prototype.forEach.call(boxes,function(x){if(x.checked&&!seenGroups[x.value]){seenGroups[x.value]=true;groups.push(x.value);}});var aliases={};Object.keys(wxAliases).forEach(function(k){aliases[k]=wxAliases[k];});Array.prototype.forEach.call(document.querySelectorAll('#wx-groups .wx-alias'),function(x){var gid=x.getAttribute('data-group');var val=x.value.trim();if(val)aliases[gid]=val;else delete aliases[gid];});var interval=parseInt(document.getElementById('wx-interval').value,10);var intervalClamped=false;if(!interval){interval=300;}if(interval<30){interval=30;intervalClamped=true;}var body={wechat_enabled:document.getElementById('wx-enabled').checked,wechat_scan_interval_seconds:interval,wechat_group_aliases:aliases};body.wechat_group_whitelist=boxes.length?groups:wxSelected.slice();api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(v){if(!v.ok)throw new Error(v.error||'保存失败');if(boxes.length)wxSelected=groups.slice();wxAliases=aliases;document.getElementById('wx-result').textContent=intervalClamped?'已保存。扫描间隔最少 30 秒，已按 30 秒保存。':'已保存。';refreshWechatStatus();}).catch(function(e){wxLoaded=false;document.getElementById('wx-result').textContent='保存失败：'+e.message;});};    var cxLoaded=false,cxSelected=[];
    function renderCxCourses(rows){var box=document.getElementById('cx-courses');var boxes=box?box.querySelectorAll('input[type=checkbox][value]'):[];if(boxes&&boxes.length){var picked=[];Array.prototype.forEach.call(boxes,function(x){if(x.checked)picked.push(x.value);});cxSelected=picked;}cxLoaded=true;box.textContent='';(rows||[]).forEach(function(row){var label=document.createElement('label');label.className='preference';var input=document.createElement('input');input.type='checkbox';input.value=row.course_id||'';input.checked=cxSelected.indexOf(row.course_id)>=0;var span=document.createElement('span');span.textContent=row.course_name+' ('+row.course_id+')';label.appendChild(input);label.appendChild(span);box.appendChild(label);});if(!rows||!rows.length)box.textContent='没有解析到课程。';}
    function renderChaoxingStatus(v){var status=document.getElementById('cx-status');if(!status)return;status.textContent='上次同步时间：'+(v.last_sync_at||'暂无')+' · 登录凭证状态：'+(v.expired?'已过期，需重新粘贴':(v.has_cookie?'有效':'还没设置'));} api('/api/settings').then(function(v){document.getElementById('cx-enabled').checked=!!v.chaoxing_enabled;cxSelected=v.chaoxing_group_whitelist||[];document.getElementById('cx-result').textContent=v.chaoxing_cookie&&v.chaoxing_cookie.has_cookie?'已配置（不显示内容）':'还没设置登录凭证';renderChaoxingStatus(v.chaoxing_status||v);});
    saveEnabledToggle('cx-enabled','cx-result','chaoxing_enabled');
    function cxFailureNote(category,source,nextStep){if(category==='decrypt_failed')return chaoxingCategoryHelp(category,source)+' 下一步：'+cxManualNextStep();return chaoxingCategoryHelp(category,source)+(nextStep?' 下一步：'+nextStep:'');}
    function chaoxingCategoryHelp(category,source){var browser=cxBrowserName(source);if(category==='browser_locked')return browser+' 的登录凭证库和副本都读不到（可能被独占）。请先完全退出它（含托盘和后台进程），再用另一浏览器或手机重新打开群务台设置页后重试。';if(category==='no_chaoxing_cookie')return browser+' 的登录凭证库里没有 chaoxing.com 的登录记录。请先用这个浏览器登录学习通，再重试。';if(category==='decrypt_failed')return '已找到学习通登录凭证，但新版浏览器（Edge / Chrome 127+）把它加密了（App-Bound 加密保护 v20），第三方程序无法自动读取，这不是你的配置问题。请改用「复制为 cURL」方式手动粘一次。';return '没有找到 '+browser+' 可读的登录凭证库。请先安装或启动一次浏览器、登录学习通，再重试。';}
    function renderCxAttempts(rows){var box=document.getElementById('cx-auto-attempts');box.textContent='';(rows||[]).forEach(function(row){var item=document.createElement('li');var text=cxBrowserName(row.source)+'：'+(row.ok?'已读取学习通登录凭证':(row.error||chaoxingCategoryHelp(row.category,row.source)));if(row.detail)text+='（'+row.detail+'）';var rowNextStep=row.category==='decrypt_failed'?cxManualNextStep():row.next_step;if(!row.ok&&rowNextStep)text+=' 下一步：'+rowNextStep;item.textContent=text;box.appendChild(item);});}
    function setCxStep(step, state, text) {
      var p = document.getElementById('cx-step-' + step + '-state');
      if (!p) return;
      p.textContent = text;
      var t = { done: 'var(--teal)', error: 'var(--red)', active: 'var(--amber)' }[state];
      p.style.color = t || '';
      p.style.borderColor = t || '';
    }
    function updateCxGuide(category){if(!category){setCxStep('login','done','已检测到登录态');setCxStep('exit','done','登录凭证库副本可读取');setCxStep('retry','done','自动获取成功');return;}if(category==='browser_locked'){setCxStep('login','done','已找到浏览器配置');setCxStep('exit','error','数据库副本也无法读取');setCxStep('retry','pending','换浏览器或手机打开本页后再重试');return;}if(category==='no_chaoxing_cookie'){setCxStep('login','error','未找到登录态');setCxStep('exit','pending','先登录学习通');setCxStep('retry','pending','登录后回本页重试');return;}if(category==='decrypt_failed'){setCxStep('login','done','已找到登录态');setCxStep('exit','error','浏览器已加密（v20），自动读不了');setCxStep('retry','pending','改用手动粘贴登录凭证（见「高级」）');return;}setCxStep('login','pending','先安装浏览器并登录');setCxStep('exit','pending','登录后回本页重试');setCxStep('retry','pending','完成前两步后重试');}
    document.getElementById('cx-auto-cred').onclick=function(){var button=this;button.disabled=true;var note=document.getElementById('cx-auto-result');var done=function(){button.disabled=false;};try{setCxStep('login','active','请确认已登录');setCxStep('exit','active','正在复制登录凭证库副本');setCxStep('retry','active','正在逐个检查浏览器');note.textContent='正在依次检查 Edge、Chrome 和其他 Chromium 浏览器…';api('/api/chaoxing/auto_cookie',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',timeoutMs:120000}).then(function(v){renderCxAttempts(v.attempts||[]);if(!v.ok){updateCxGuide(v.category||'browser_unavailable');note.textContent=cxFailureNote(v.category,v.source,v.next_step);return;}updateCxGuide('');note.textContent='已从 '+cxBrowserName(v.source)+' 自动获取并验证登录凭证，发现 '+(v.courses||0)+' 门课程。';document.getElementById('cx-result').textContent='已配置（不显示内容）';document.getElementById('cx-enabled').checked=true;api('/api/settings').then(function(s){cxSelected=s.chaoxing_group_whitelist||cxSelected;});}).catch(function(e){updateCxGuide('browser_unavailable');note.textContent='自动读取失败：'+e.message+'。请先用托盘右键「读取学习通登录凭证」；仍失败时可展开「高级：手动粘贴登录凭证」，也可以再试一次。';}).then(done,done);}catch(e){note.textContent='自动读取没能开始，可以再试一次。';done();}};
    document.getElementById('cx-load-courses').onclick=function(){var box=document.getElementById('cx-courses');box.textContent='正在拉取…';api('/api/chaoxing/courses').then(function(v){if(!v.ok)throw new Error(v.error||'课程读取失败');renderCxCourses(v.courses);}).catch(function(e){box.textContent='拉取失败：'+e.message;});};
    document.getElementById('cx-save').onclick=function(){var button=this;var boxes=document.querySelectorAll('#cx-courses input[type=checkbox]');var groups=[];Array.prototype.forEach.call(boxes,function(x){if(x.checked&&x.value)groups.push(x.value);});button.disabled=true;button.textContent='保存中…';document.getElementById('cx-result').textContent='正在保存…';var payload={chaoxing_enabled:document.getElementById('cx-enabled').checked};payload.chaoxing_group_whitelist=boxes.length?groups:cxSelected.slice();Promise.race([api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),new Promise(function(_,reject){setTimeout(function(){reject(new Error('保存超时：请求没有响应，请刷新页面或改用托盘导入'));},20000);})]).then(function(v){if(!v.ok)throw new Error(v.error||'服务端未保存设置');if(boxes.length)cxSelected=groups.slice();document.getElementById('cx-result').textContent='已保存。';api('/api/chaoxing/status').then(renderChaoxingStatus);}).catch(function(e){cxLoaded=false;document.getElementById('cx-result').textContent=e.message.indexOf('保存超时：')===0?e.message:'保存失败：'+e.message;}).then(function(){button.disabled=false;button.textContent='保存学习通设置';});};

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
    // B3：备选地址收进默认折叠的 details，每条写清是哪一类、哪个主机，别让用户在一排按钮里猜。
    function addressChoiceLabel(item) {
      var host = '';
      try { host = new URL(item.url).hostname; } catch (error) {}
      var kind = {lan: '局域网', public: '公网', tailscale: 'Tailscale', cellular: '手机热点', virtual: '虚拟网卡'}[item.kind] || '';
      var text = [kind, host].filter(function (part) { return !!part; }).join(' · ');
      return text || item.name || '备用地址';
    }
    function renderAddressChoices(calendars, current) {
      var others = (calendars || []).filter(function (item) { return item && item.url && item.url !== current; });
      altList.textContent = '';
      if (!others.length) { altList.hidden = true; return; }
      altList.hidden = false;
      var details = document.createElement('details');
      details.className = 'sync-alt-details';
      var summary = document.createElement('summary');
      summary.textContent = '换一个地址（' + others.length + ' 个）';
      details.appendChild(summary);
      var list = document.createElement('div');
      list.className = 'sync-alt-list';
      others.forEach(function (item) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn ghost sync-alt';
        button.textContent = addressChoiceLabel(item);
        button.title = (item.hint ? item.hint + '；' : '') + item.url;
        button.onclick = function () {
          input.value = item.url;
          delete input.dataset.edited;
          try { input.dataset.testScheme = new URL(item.url).protocol; } catch (error) { input.dataset.testScheme = ''; }
          // 输入框、二维码、公网/局域网提示都取同一条地址，别再各说各的。
          renderPublicNote(item);
          renderSyncQr();
          renderAddressChoices(calendars, item.url);
          setStatus('已切换为' + addressChoiceLabel(item) + '，二维码和提示语一起换新。', '');
        };
        list.appendChild(button);
      });
      details.appendChild(list);
      altList.appendChild(details);
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
    input.addEventListener('input', function () { input.dataset.testScheme = ''; input.dataset.edited = '1'; renderSyncQr(); });
    var lastSyncUrl = '';
    // 公网地址排第一是后端给的顺序，但页面别依赖它：手机用流量只能走公网那条。
    // B3：后端会标 primary（公网第一、其次唯一推荐的一条）；没有 primary 再退回公网/第一条。
    function primaryAddress(calendars) {
      var list = (calendars || []).filter(function (item) { return item && item.url; });
      var primary = list.filter(function (item) { return item.primary === true; })[0];
      if (primary) return primary;
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
    function scrollToPublicBlock() {
      var card = document.querySelector('[data-setting-anchor="mobile"]');
      if (card) card.scrollIntoView({behavior: 'smooth', block: 'start'});
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
        // 用户手改过地址（比如粘一条公网地址来测）就别被静默刷新冲掉：
        // 想换回自动识别的地址，点「换一个地址」里的那条即可（会自动清掉 edited）。
        if (input.dataset.edited !== '1') {
          input.value = calendar.url;
          try { input.dataset.testScheme = new URL(calendar.url).protocol; } catch (error) { input.dataset.testScheme = ''; }
        }
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
    if (publicButton) publicButton.onclick = scrollToPublicBlock;
    // 主界面「显示订阅二维码」跳过来时静默重取一次地址，地址变了就自动换码。
    window.__nhRefreshSyncInfo = function () { refreshSyncInfo(false); };
    refreshSyncInfo(false);
  })();
  var themeBox = document.createElement('section'); themeBox.className='surface hosting-settings'; themeBox.dataset.settingAnchor='appearance';
  themeBox.innerHTML='<h2 class="section-title">外观</h2>'
    +'<fieldset id="pref-font-scale" class="theme-choice-list"><legend>字号</legend>'
    +'<label class="theme-option"><input name="pref-font-scale" type="radio" value="default"><span class="theme-option-body"><strong>默认</strong><small>正文 15px，原来的大小</small></span></label>'
    +'<label class="theme-option"><input name="pref-font-scale" type="radio" value="large"><span class="theme-option-body"><strong>偏大</strong><small>正文约 19px，按钮和间距一起放大</small></span></label>'
    +'<label class="theme-option"><input name="pref-font-scale" type="radio" value="xlarge"><span class="theme-option-body"><strong>很大</strong><small>正文约 22px，按钮和间距一起放大</small></span></label>'
    +'<p id="pref-font-scale-note" class="setting-note">只影响这一台设备的浏览器</p></fieldset>'
    +'<fieldset id="pref-theme" class="theme-choice-list"><legend>主题</legend>'
    +'<label class="theme-option"><input name="pref-theme" type="radio" value="auto"><span class="theme-option-body"><strong>跟随系统</strong><small>系统切深色就跟着深</small></span></label>'
    +'<label class="theme-option"><input name="pref-theme" type="radio" value="light"><span class="theme-option-body"><strong>浅色</strong><small>固定使用浅色纸面</small></span></label>'
    +'<label class="theme-option"><input name="pref-theme" type="radio" value="dark"><span class="theme-option-body"><strong>深色</strong><small>固定使用深色橄榄</small></span></label>'
    +'<p id="pref-theme-note" class="setting-note">仅保存在此浏览器</p></fieldset>'
    +'<fieldset class="preference-list local-preferences"><legend>界面动效</legend><label class="preference"><input id="pref-motion-enabled" type="checkbox"><span><strong>轻微动效</strong><small id="pref-motion-note"></small></span></label></fieldset>'
    +'<fieldset id="pref-accent" class="theme-choice-list"><legend>强调色</legend>'
    +'<div class="accent-swatches" role="radiogroup" aria-label="强调色">'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="true" data-accent="#5C6B44" data-accent-soft="#E4E8DA" aria-label="鼠尾草" title="鼠尾草" style="background:#5C6B44"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-accent="#8B5E74" data-accent-soft="#EFE1E7" aria-label="梅紫" title="梅紫" style="background:#8B5E74"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-accent="#B0713F" data-accent-soft="#F3E4D6" aria-label="赭橙" title="赭橙" style="background:#B0713F"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-accent="#5E7B8B" data-accent-soft="#DFE7EB" aria-label="黛蓝" title="黛蓝" style="background:#5E7B8B"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-accent="#6E6A5E" data-accent-soft="#E6E4DD" aria-label="石墨" title="石墨" style="background:#6E6A5E"></button>'
    +'</div><p class="setting-note">用在主按钮、选中态与焦点环。</p></fieldset>'
    +'<fieldset id="pref-notice-tint" class="theme-choice-list"><legend>通知颜色</legend>'
    +'<div class="accent-swatches" role="radiogroup" aria-label="通知颜色">'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="true" data-tint="" aria-label="跟随纸面" title="跟随纸面" style="background:var(--paper-raise)"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-tint="#5C6B44" aria-label="鼠尾草" title="鼠尾草" style="background:color-mix(in srgb,#5C6B44 30%,var(--paper-raise))"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-tint="#8B5E74" aria-label="梅紫" title="梅紫" style="background:color-mix(in srgb,#8B5E74 30%,var(--paper-raise))"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-tint="#B0713F" aria-label="赭橙" title="赭橙" style="background:color-mix(in srgb,#B0713F 30%,var(--paper-raise))"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-tint="#5E7B8B" aria-label="黛蓝" title="黛蓝" style="background:color-mix(in srgb,#5E7B8B 30%,var(--paper-raise))"></button>'
    +'<button type="button" class="accent-swatch" role="radio" aria-checked="false" data-tint="#6E6A5E" aria-label="石墨" title="石墨" style="background:color-mix(in srgb,#6E6A5E 30%,var(--paper-raise))"></button>'
    +'</div><p class="setting-note">右上角悬浮通知那层磨砂玻璃的底色，与强调色同一套色；颜色会和纸面调和，玻璃质感不变。只保存在此浏览器。</p></fieldset>';
  root.appendChild(themeBox); initFontScalePreference(); initThemePreference(); initAccentPreference(); initNoticeTintPreference();
  var hostingBox = document.createElement('div'); hostingBox.className='hosting-settings hosting-panel'; hostingBox.dataset.settingAnchor='hosting';
  hostingBox.innerHTML='<h2 class="section-title">托管设置</h2><div id="hosting-status" class="hosting-status" role="status" aria-live="polite">正在读取托管状态…</div><div class="hosting-actions hosting-actions-main">  <button id="hosting-start" class="btn primary" type="button">开始托管</button>  <button id="hosting-stop" class="btn" type="button">结束托管</button>  <span class="setting-note">托盘图标也可用于开始或结束托管。</span></div><p id="hosting-result" role="status" aria-live="polite"></p><h3 class="settings-group-title">自动化选项</h3><div class="settings-rows">  <label class="field">    <span>开始托管前退出电脑版 QQ<small>免得电脑版 QQ 和托管的登录抢同一个号。</small></span>    <input id="pref-quit_qq" type="checkbox">  </label>  <label class="field">    <span>结束托管后恢复电脑版 QQ<small>结束托管时重新启动电脑版 QQ。</small></span>    <input id="pref-restore_qq" type="checkbox">  </label>  <label class="field">    <span>退出群务台时打开电脑版 QQ<small>默认关闭：退出就是安静退出，不额外启动 QQ。托管期间关掉的 QQ 由上一项负责恢复。</small></span>    <input id="pref-quit_restore_qq" type="checkbox">  </label>  <label class="field">    <span>启动群务台时自动开始托管<small>打开群务台就开始接管，按上面的选项执行。</small></span>    <input id="pref-auto_on_start" type="checkbox">  </label>  <label class="field">    <span>开机自动启动群务台<small>开机后自动启动群务台。</small></span>    <input id="pref-autostart" type="checkbox">  </label>  <label class="field">    <span>桌面浮球<small>默认关闭。打开后屏幕右下角出现浮球，显示待处理条数，点开能完成、稍后、忽略。开关状态存在本机，重启后不变。</small></span>    <input id="pref-floatball-enabled" type="checkbox">  </label></div><h3 class="settings-group-title">历史回溯</h3><div class="settings-rows">  <label class="field">    <span>启动时自动回溯最近 N 天<small>只在启动时补收最近几天的消息，点保存不会马上回溯。</small></span>    <input id="pref-catchup-enabled" type="checkbox" disabled>  </label>  <label class="field">    <span>回溯天数<small>按天填，保存时换算成小时。</small></span>    <input id="pref-catchup-days" type="number" disabled min="1" max="365" step="1" value="1" aria-label="启动时自动回溯最近多少天">  </label>  <div class="settings-row-actions">    <button id="pref-catchup-save" class="btn" type="button" disabled>保存回溯设置</button>    <p id="pref-catchup-result" role="status" aria-live="polite"></p>  </div></div><h3 class="settings-group-title" id="llm-settings">AI 摘要（让它替你读消息、写待办）</h3><p class="setting-note">电脑上没装大模型也没关系：挑一家云服务，注册后把它给你的那串「钥匙」粘进来就行，一个月通常花不到一块钱。下面选好服务商，接口地址和模型名已经替你填好了，不用管。</p><div class="settings-rows">  <label class="field">    <span>用哪一家<small id="llm-provider-hint">正在读取…</small></span>    <select id="llm-provider" aria-label="选择 AI 服务商"></select>  </label>  <div class="push-steps" id="llm-steps" hidden>    <ol id="llm-steps-list"></ol>    <a id="llm-help-link" class="push-help" target="_blank" rel="noreferrer" hidden></a>  </div>  <label class="field">    <span>密钥<small>粘一次就行；以后留空表示不改。</small></span>    <input id="llm-key" type="text" autocomplete="off" spellcheck="false" aria-label="密钥" placeholder="还没有填">  </label>  <label class="field">    <span>模型名<small>已经替你填好，一般不用动。</small></span>    <input id="llm-model" type="text" autocomplete="off" spellcheck="false" aria-label="模型名">  </label>  <label class="field" id="llm-endpoint-row" hidden>    <span>接口地址<small>只有选「其它」时才需要填。</small></span>    <input id="llm-endpoint" type="text" autocomplete="off" spellcheck="false" aria-label="接口地址">  </label>  <div class="settings-row-actions">    <button id="llm-save" class="btn primary" type="button">保存并测试</button>    <button id="llm-off" class="btn" type="button">不用 AI</button>    <p id="llm-result" role="status" aria-live="polite"></p>  </div></div><div class="hosting-warning" role="note">  <strong>启用退出选项后，开始托管会关闭电脑版 QQ；结束时可按恢复选项重新启动。</strong></div>';

  var pushPanel = document.createElement('section');
  pushPanel.className = 'surface hosting-settings';
  pushPanel.dataset.settingAnchor = 'push';
  pushPanel.innerHTML = '<h2 class="section-title">推送通道</h2>' + '<fieldset class="preference-list" id="push-settings"><legend>提醒怎么送到手机</legend><p class="setting-note"><strong>不配也能用：</strong>点快捷操作里的「显示订阅二维码」，用手机日历扫一下，到期日程会同步进手机自带日历，到点手机自己响。想在微信里立刻收到提醒，就在下面挑一种，照着 1-2-3 做——每个值去哪里拿，都写在旁边了。填好点下面的「保存」，再点「发一条测试消息」，手机收到就成功了。</p><details class="push-channel" data-channel="wxpusher" open><summary>方式一：微信推送（WxPusher，推荐）</summary><ol class="push-steps"><li>点下面的「打开 WxPusher 后台」，用手机扫码登录。</li><li>点「应用管理」→「创建应用」，类型选「标准推送」，建好后复制那串「应用密钥」贴到下面第一格。</li><li>把应用的二维码发给接收人（一般就是你自己）扫码关注，然后在「用户管理」里复制 UID_ 开头的那串，贴到第二格。</li></ol><p><a class="push-help" data-help="wxpusher" target="_blank" rel="noreferrer">打开 WxPusher 后台，照着做</a></p><label class="preference"><span><strong>应用密钥</strong><small>创建应用以后，页面上那串长得像密码的字符。</small></span><input id="push-wxpusher_key" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher 应用密钥"></label><label class="preference"><span><strong>我的 UID</strong><small>关注公众号后，在「用户管理」里复制。多个用逗号分隔。</small></span><input id="push-wxpusher_uids" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher UID"></label><label class="preference"><span><strong>话题 ID（可选）</strong><small>只有要发给一群人时才填，一个人用就不用管它。</small></span><input id="push-wxpusher_topic_ids" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher 话题 ID"></label></details><details class="push-channel" data-channel="serverchan"><summary>方式二：Server 酱（微信，只需一个值）</summary><ol class="push-steps"><li>点下面的「打开 Server 酱」，用手机扫码登录。</li><li>登录后网页上直接给你一串密码一样的字符，整串复制下来贴到下面。</li></ol><p><a class="push-help" data-help="serverchan" target="_blank" rel="noreferrer">打开 Server 酱（直达密钥页面）</a></p><label class="preference"><span><strong>密钥</strong><small>扫码登录后页面上直接显示的那串。</small></span><input id="push-serverchan_keys" type="text" autocomplete="off" spellcheck="false" aria-label="Server 酱密钥"></label></details><details class="push-channel" data-channel="pushplus"><summary>方式三：PushPlus（微信，只需一个值）</summary><ol class="push-steps"><li>点下面的「打开 PushPlus」，用手机扫码登录。</li><li>进「一对一消息」页面，复制那里的「用户密钥」，贴到下面。</li></ol><p><a class="push-help" data-help="pushplus" target="_blank" rel="noreferrer">打开 PushPlus，照着做</a></p><label class="preference"><span><strong>用户密钥</strong><small>登录后「一对一消息」页面显示的那串。</small></span><input id="push-pushplus_key" type="text" autocomplete="off" spellcheck="false" aria-label="PushPlus 用户密钥"></label></details><details class="push-channel" data-channel="custom"><summary>方式四：企业微信 / 钉钉 / 自己的机器人</summary><ol class="push-steps"><li>在电脑版企业微信里右键要收提醒的群 →「管理聊天信息」。</li><li>右侧点「消息推送」→「自定义消息推送」，填个名字后复制那条地址。</li><li>把地址整段贴到下面（后面的参数别漏）。</li></ol><label class="preference"><span><strong>机器人地址</strong><small>整段粘贴，别漏掉后面的参数。支持钉钉 / 企业微信 / 飞书群机器人；钉钉开了「加签」就把地址和密钥写成「地址|SEC密钥」。</small></span><input id="push-robot-urls" type="text" autocomplete="off" spellcheck="false" aria-label="机器人地址"></label></details><div class="hosting-actions"><button id="push-save" class="btn primary" type="button">保存</button><button id="push-test" class="btn" type="button">发一条测试消息</button><button id="push-clear" class="btn" type="button">清空重填</button></div><p id="push-result" role="status" aria-live="polite"></p></fieldset>';
  root.appendChild(pushPanel);
  root.appendChild(hostingBox);
  var donateBox = document.createElement('section'); donateBox.className='surface hosting-settings'; donateBox.dataset.settingAnchor='support'; donateBox.innerHTML='<h2 class="section-title">支持开发者</h2><p>如果这个工具帮到了你，可以请作者喝杯咖啡。完全自愿，不影响任何功能。</p><div class="donate-qr-row"><img id="donate-qr" alt="微信收款码" style="width:160px;height:160px;background:#fff;border-radius:var(--r-2);border:1px solid var(--line);"><small id="donate-qr-note">微信扫码支付</small></div><p class="setting-note">打赏不构成任何服务承诺，也不影响功能与更新。</p><fieldset class="preference-list" id="community-telemetry-settings"><legend>社区在线数</legend><label class="preference"><input id="pref-community-telemetry" type="checkbox"><span><strong>显示社区在线数</strong><small>打开后会在「关于与支持」里显示现在有多少人在用、今日打开次数。这会向第三方计数服务上报一次匿名打开，不包含任何消息、群号或账号信息；关掉立即生效，不再上报。</small></span></label></fieldset><p id="pref-community-telemetry-result" class="setting-note" role="status" aria-live="polite"></p>'; root.appendChild(donateBox); var donateImage=document.getElementById('donate-qr');donateImage.src='/api/donate/qr.png'; donateImage.onerror=function(){donateImage.hidden=true;document.getElementById('donate-qr-note').textContent='收款码加载失败';};
  // 社区在线数开关：关着时右栏那行显示「未开启」，这里就是用户唯一能打开它的地方。
  var telemetryToggle=document.getElementById('pref-community-telemetry');
  var telemetryResult=document.getElementById('pref-community-telemetry-result');
  if(telemetryToggle){
    api('/api/settings').then(function(v){telemetryToggle.checked=!!(v&&v.community_telemetry);if(telemetryResult)telemetryResult.textContent=telemetryToggle.checked?'已打开':'已关闭，不再上报';}).catch(function(){});
    telemetryToggle.addEventListener('change',function(){
      var enabled=telemetryToggle.checked;
      telemetryToggle.disabled=true;
      api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({community_telemetry:enabled})}).then(function(v){
        if(!v||!v.ok)throw new Error((v&&v.error)||'保存失败');
        if(telemetryResult)telemetryResult.textContent=enabled?'已打开，正在读取在线数…':'已关闭，不再上报';
        // 保存成功立刻重跑右栏那一行，不用刷新页面。
        if(typeof window.initCommunityCounts==='function')window.initCommunityCounts();
      }).catch(function(e){
        // 后端原文直接给用户看（例如「community_telemetry 必须是布尔值」），别吞成「保存失败」。
        telemetryToggle.checked=!enabled;
        if(telemetryResult)telemetryResult.textContent=String((e&&e.message)||e);
      }).then(function(){telemetryToggle.disabled=false;});
    });
  }
   var motionToggle = document.getElementById('pref-motion-enabled');
  motionToggle.checked = !motionPreferencePaused;
  motionToggle.addEventListener('change', function () { setMotionPreference(motionToggle.checked); });
  updateMotionNote();
  // 桌面浮球开关：勾选即存（POST /api/settings {floatball_enabled}），存进本机 .env 并热生效。
  // 契约归后端（webapp.py 的 settings 路由），这里只按字段名读写；不进 preferenceNames 批处理
  // （那套带「保存设置」按钮语义，和勾选即存冲突）。失败走 showFeedback，不再占独立一行。
  var floatballToggle = document.getElementById('pref-floatball-enabled');
  var floatballSaved = false;
  if (floatballToggle) {
    api('/api/settings').then(function (values) {
      floatballSaved = !!(values && values.floatball_enabled);
      floatballToggle.checked = floatballSaved;
    }).catch(function () {});
    floatballToggle.addEventListener('change', function () {
      var enabled = floatballToggle.checked;
      floatballToggle.disabled = true;
      api('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({floatball_enabled: enabled})}).then(function (result) {
        if (!result || !result.ok) throw new Error((result && result.error) || '保存失败');
        floatballSaved = result.floatball_enabled !== undefined ? !!result.floatball_enabled : enabled;
        showFeedback('桌面浮球已' + (floatballSaved ? '打开' : '关闭'), 'success');
      }).catch(function (error) {
        floatballToggle.checked = floatballSaved;
        showFeedback('桌面浮球设置未保存：' + String((error && error.message) || error), 'error', function () {
          floatballToggle.checked = enabled;
          floatballToggle.dispatchEvent(new Event('change'));
        });
      }).then(function () { floatballToggle.disabled = false; });
    });
  }
  // 公网访问：双通道。默认 Cloudflare 快速通道（不用注册、不用域名），Tailscale 作为
  // 「要固定地址」的备选。状态读 /api/tunnel/status：providers.<通道> 是各通道自己的快照，
  // 顶层 state/message/url 表示当前通道（老后端没有 providers 时退回读顶层）。
  // 开通/重试走 /api/tunnel/start（写操作要 confirm:true，带 provider）；关闭走 /api/tunnel/stop。
  // 轮询只在「开通中」跑，切走设置页即停。
  var tunnelTimer = null;
  var tunnelState = {};
  var tunnelProvider = 'cloudflare';
  var tunnelSwitchConfirmTo = null;
  var tunnelSwitchConfirmTimer = null;
  function stopTunnelPolling() { if (tunnelTimer !== null) { window.clearInterval(tunnelTimer); tunnelTimer = null; } }
  function tunnelProviderName(provider) { return provider === 'tailscale' ? 'Tailscale' : 'Cloudflare'; }
  function isTunnelProvider(provider) { return provider === 'cloudflare' || provider === 'tailscale'; }
  // 各通道自己的「开通中」状态名：Cloudflare 是 downloading/starting，Tailscale 沿用它原来那三个。
  function isTunnelBusy(provider, state) {
    var name = String(state || 'idle');
    if (provider === 'tailscale') return name === 'installing' || name === 'waiting_login' || name === 'enabling';
    return name === 'downloading' || name === 'starting';
  }
  function tunnelSnapshot(payload, provider) {
    var providers = payload && payload.providers;
    if (providers && providers[provider]) return providers[provider] || {};
    // 老后端（没有 providers）：顶层字段只代表它自己的通道，别把它当成另一条通道的状态。
    if (!payload || (payload.provider && payload.provider !== provider)) return {};
    return { installed: payload.installed, state: payload.state, message: payload.message, url: payload.url };
  }
  function clearTunnelSwitchConfirm() {
    if (tunnelSwitchConfirmTimer !== null) { window.clearTimeout(tunnelSwitchConfirmTimer); tunnelSwitchConfirmTimer = null; }
    tunnelSwitchConfirmTo = null;
  }
  function tunnelProvidersRow() {
    var row = '<div class="tunnel-providers" role="group" aria-label="公网通道">';
    [['cloudflare', 'Cloudflare（不用注册）'], ['tailscale', 'Tailscale（固定地址）']].forEach(function (item) {
      var active = item[0] === tunnelProvider;
      row += '<button type="button" class="tunnel-provider" id="tunnel-provider-' + item[0] + '" data-provider="' + item[0] + '"'
        + ' aria-pressed="' + (active ? 'true' : 'false') + '">' + item[1] + '</button>';
    });
    return row + '</div><p id="tunnel-switch-note" class="setting-note" role="status" aria-live="polite"></p>';
  }
  function switchTunnelProvider(next) {
    stopTunnelPolling();
    var note = document.getElementById('tunnel-switch-note');
    if (note) note.textContent = '正在切换到 ' + tunnelProviderName(next) + '…';
    api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tunnel_provider: next }) }).then(function (result) {
      if (!result || !result.ok) throw new Error((result && result.error) || '保存失败');
      tunnelProvider = isTunnelProvider(result.tunnel_provider) ? result.tunnel_provider : next;
      return refreshTunnelStatus();
    }).catch(function (error) {
      // 高亮不跳：没切成功就还按原通道重画，别让人以为已经换了。
      renderTunnelCard();
      showFeedback('通道切换没有成功：' + String((error && error.message) || error), 'error');
    });
  }
  function requestTunnelProviderChange(next) {
    if (!isTunnelProvider(next) || next === tunnelProvider) return;
    var snap = tunnelSnapshot(tunnelState, tunnelProvider);
    var readyNow = String(snap.state || 'idle') === 'ready' && !!snap.url;
    if (readyNow && tunnelSwitchConfirmTo !== next) {
      // 已开通时切换会切断手机上正在用的地址，先讲清后果，再点一次才动手。
      var note = document.getElementById('tunnel-switch-note');
      if (note) note.textContent = '切换后当前公网地址会失效，需要重新开通。再点一次「' + tunnelProviderName(next) + '」确认切换。';
      tunnelSwitchConfirmTo = next;
      if (tunnelSwitchConfirmTimer !== null) window.clearTimeout(tunnelSwitchConfirmTimer);
      tunnelSwitchConfirmTimer = window.setTimeout(function () {
        tunnelSwitchConfirmTimer = null;
        tunnelSwitchConfirmTo = null;
        var reset = document.getElementById('tunnel-switch-note');
        if (reset) reset.textContent = '';
      }, 4000);
      return;
    }
    clearTunnelSwitchConfirm();
    switchTunnelProvider(next);
  }
  function refreshTunnelStatus() {
    return api('/api/tunnel/status').then(function (payload) {
      if (!payload || payload.ok === false) throw new Error((payload && payload.error) || '状态读取失败');
      tunnelState = payload;
      if (isTunnelProvider(payload.provider)) tunnelProvider = payload.provider;
      renderTunnelCard();
      if (isTunnelBusy(tunnelProvider, payload.state)) pollTunnel(false);
      return payload;
    });
  }
  function tunnelCopy(text, noteNode) {
    function done(ok) { if (noteNode) noteNode.textContent = ok ? '已复制。' : ''; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
    } else {
      // clipboard API 不可用（http 环境等）：退到老办法选中文本让用户 Ctrl+C。
      var input = document.getElementById('tunnel-url');
      if (input && typeof input.select === 'function') { input.select(); try { document.execCommand('copy'); done(true); } catch (e) { done(false); } }
      else done(false);
    }
  }
  function renderTunnelCard() {
    var card = document.getElementById('tunnel-card');
    if (!card) { stopTunnelPolling(); return; }
    var provider = isTunnelProvider(tunnelProvider) ? tunnelProvider : 'cloudflare';
    var snap = tunnelSnapshot(tunnelState, provider);
    var state = String(snap.state || 'idle');
    var busy = isTunnelBusy(provider, state);
    if (!busy && tunnelTimer !== null) { stopTunnelPolling(); }
    var readyUrl = state === 'ready' ? String(snap.url || '') : '';
    var body;
    if (readyUrl) {
      // 地址 + 复制 + 两段式关闭 + 二维码。Cloudflare 的地址每次重启都变，单独用琥珀小字讲清楚。
      body = '<p class="setting-note">已开通。手机不用装任何客户端，用移动网络在浏览器里直接打开下面这个地址就能用：</p>'
        + '<input id="tunnel-url" class="sync-url tunnel-url" type="url" readonly value="' + readyUrl.replace(/"/g, '&quot;') + '" aria-label="公网地址">'
        + '<div class="tunnel-actions"><button id="tunnel-copy" class="btn" type="button">复制链接</button><button id="tunnel-close" class="btn" type="button">关闭公网</button><span id="tunnel-copy-note" class="setting-note"></span></div>'
        + '<div class="tunnel-qr-row"><img id="tunnel-qr" class="sync-qr" alt="公网地址二维码" hidden><p id="tunnel-qr-message" class="sync-status"></p></div>'
        + (provider === 'cloudflare' ? '<p class="tunnel-warn">这个地址每次重启会变，重启后回来这里重新复制或扫码。</p>' : '');
    } else if (state === 'error') {
      body = '<p class="setting-note tunnel-error">' + String(snap.message || '开通没有成功。').replace(/</g, '&lt;') + '</p>'
        + '<button id="tunnel-retry" class="btn" type="button">重试</button>';
    } else if (state === 'waiting_login') {
      body = '<p class="setting-note">' + String(snap.message || '请在打开的浏览器里登录 Tailscale，登录完会自动继续。') + '</p>';
      var authUrl = safeUrl(snap.auth_url || '');
      if (authUrl) {
        body += '<p><a href="' + authUrl.replace(/"/g, '&quot;') + '" target="_blank" rel="noreferrer">打开 Tailscale 登录页</a></p>';
      }
      body += '<p class="setting-note">登录完成后这里会自动变成公网地址，不用刷新页面。</p>';
    } else if (busy) {
      body = '<p class="setting-note">' + String(snap.message || '正在开通公网入口…') + '</p>'
        + '<button id="tunnel-start" class="btn primary" type="button" disabled>正在开通…</button>';
    } else if (provider === 'cloudflare') {
      body = '<p class="setting-note">第一次会下载约 19 MB 的通道程序（走国内镜像，一般半分钟内），之后不用再下；不用注册、不用域名、手机不用装东西。开通后手机用移动网络在浏览器里直接打开链接就能用。</p>'
        + '<button id="tunnel-start" class="btn primary" type="button">开通公网</button>';
    } else {
      body = '<p class="setting-note">用 Tailscale 开通：要登录一次账号，但地址固定不变，适合长期订阅日历。手机同样不用装任何客户端，浏览器直接打开链接就行。</p>'
        + '<button id="tunnel-start" class="btn primary" type="button">用 Tailscale 开通</button>';
    }
    card.innerHTML = tunnelProvidersRow() + '<div id="tunnel-body">' + body + '</div>';
    Array.prototype.forEach.call(card.querySelectorAll('.tunnel-provider'), function (button) {
      button.onclick = function () { requestTunnelProviderChange(button.getAttribute('data-provider')); };
    });
    var startButton = document.getElementById('tunnel-start');
    if (startButton) startButton.onclick = function () { startTunnel(); };
    var retryButton = document.getElementById('tunnel-retry');
    if (retryButton) retryButton.onclick = function () { startTunnel(); };
    if (readyUrl) bindTunnelReadyActions(readyUrl);
  }
  function bindTunnelReadyActions(url) {
    var copyNote = document.getElementById('tunnel-copy-note');
    var copyButton = document.getElementById('tunnel-copy');
    if (copyButton) copyButton.onclick = function () { tunnelCopy(url, copyNote); };
    // 关闭公网：两段式确认（跟通知删除同一套，4 秒不点第二下自动复原）——
    // 这是把手机上的入口切断，误点会让用户以为服务坏了。
    var closeBtn = document.getElementById('tunnel-close');
    var closeConfirmTimer = null;
    if (closeBtn) {
      closeBtn.onclick = function () {
        if (closeConfirmTimer === null) {
          closeBtn.textContent = '确认关闭？（4 秒内再点一次）';
          closeConfirmTimer = window.setTimeout(function () {
            closeConfirmTimer = null;
            closeBtn.textContent = '关闭公网';
          }, 4000);
          return;
        }
        window.clearTimeout(closeConfirmTimer);
        closeConfirmTimer = null;
        stopTunnelPolling();
        closeBtn.disabled = true;
        closeBtn.textContent = '正在关闭…';
        api('/api/tunnel/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(function (result) {
          if (!result || !result.ok) throw new Error((result && result.error) || '服务端没有确认关闭');
          // 写动作之后缓存必须失效：立刻按未开通重画，页面别再挂着旧公网地址。
          var snapshot = tunnelSnapshot(tunnelState, tunnelProvider) || {};
          var providers = tunnelState.providers || {};
          providers[tunnelProvider] = { installed: !!snapshot.installed, state: 'idle', message: '', url: '' };
          tunnelState = { ok: true, provider: tunnelProvider, state: 'idle', message: '', url: '', auth_url: '', retryable: true, installed: !!snapshot.installed, providers: providers };
          renderTunnelCard();
          refreshTunnelStatus().catch(function () {});
          showFeedback('公网入口已关闭。手机用移动网络打不开了，同一 Wi-Fi 下还能用。', 'success');
        }).catch(function (error) {
          closeBtn.disabled = false;
          closeBtn.textContent = '关闭公网';
          if (copyNote) copyNote.textContent = '关闭没有成功：' + String((error && error.message) || error);
        });
      };
    }
    var qrImage = document.getElementById('tunnel-qr');
    var qrMessage = document.getElementById('tunnel-qr-message');
    if (!qrImage) return;
    qrImage.onload = function () { qrMessage.textContent = ''; };
    qrImage.onerror = function () { qrImage.hidden = true; qrMessage.textContent = '二维码生成失败，可复制链接在手机浏览器打开。'; };
    qrImage.src = '/api/sync/qr.png?text=' + encodeURIComponent(url) + '&token=' + encodeURIComponent(setupToken) + '&t=' + Date.now();
    qrImage.hidden = false;
    qrMessage.textContent = '手机扫码直接打开。';
  }
  function startTunnel() {
    var provider = isTunnelProvider(tunnelProvider) ? tunnelProvider : 'cloudflare';
    var body = document.getElementById('tunnel-body');
    if (body) body.innerHTML = '<p class="setting-note">正在开通公网入口，请稍候…</p>';
    api('/api/tunnel/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: true, provider: provider }) }).then(function (result) {
      if (!result || !result.ok) throw new Error((result && result.error) || '服务端没有接受开通请求');
      pollTunnel(true);
    }).catch(function (error) {
      // 只换卡片正文，顶上那排通道胶囊留着，换个通道还能再试。
      var target = document.getElementById('tunnel-body') || document.getElementById('tunnel-card');
      if (target) target.innerHTML = '<p class="setting-note tunnel-error">开通没有开始：' + String((error && error.message) || error).replace(/</g, '&lt;') + '</p><button id="tunnel-retry" class="btn" type="button">重试</button>';
      var retry = document.getElementById('tunnel-retry');
      if (retry) retry.onclick = function () { startTunnel(); };
    });
  }
  function pollTunnel(immediate) {
    stopTunnelPolling();
    var tick = function () {
      // 切走设置页就停（页面只有设置页有这张卡）。
      if (document.body.dataset.tab !== 'settings') { stopTunnelPolling(); return; }
      api('/api/tunnel/status').then(function (payload) {
        if (!payload || payload.ok === false) throw new Error(payload.error || '状态读取失败');
        tunnelState = payload;
        if (isTunnelProvider(payload.provider)) tunnelProvider = payload.provider;
        renderTunnelCard();
        // 开通中 2 秒后再查（renderTunnelCard 在终态时已经把定时器清掉了）。
        if (isTunnelBusy(tunnelProvider, payload.state)) {
          if (tunnelTimer === null) tunnelTimer = window.setInterval(tick, 2000);
        } else {
          stopTunnelPolling();
        }
      }).catch(function (error) {
        var card = document.getElementById('tunnel-card');
        if (card) card.textContent = '公网状态读取失败：' + String((error && error.message) || error);
        stopTunnelPolling();
      });
    };
    if (immediate) tick(); else tunnelTimer = window.setInterval(tick, 2000);
  }
  api('/api/tunnel/status').then(function (payload) {
    tunnelState = payload || {};
    if (isTunnelProvider(tunnelState.provider)) tunnelProvider = tunnelState.provider;
    renderTunnelCard();
    if (isTunnelBusy(tunnelProvider, tunnelState.state)) pollTunnel(false);
  }).catch(function (error) {
    var card = document.getElementById('tunnel-card');
    if (card) card.textContent = '公网状态读取失败：' + String((error && error.message) || error);
  });
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
  function refreshHosting(){return api('/api/hosting/status').then(function(status){document.getElementById('hosting-status').textContent='托管：'+(status.hosting_active?'进行中':'未开始')+'；NapCat：'+(status.napcat_running?'运行中':'未运行')+'；登录：'+(status.napcat_online?'在线':'未登录')+'；电脑版 QQ：'+(status.user_qq_running?'运行中':'未运行');
    // start/stop 是同一个动作的两个方向：按状态只留一个，别让当前不该做的事也当主按钮摆着。
    var startBtn=document.getElementById('hosting-start'),stopBtn=document.getElementById('hosting-stop');
    if(startBtn)startBtn.hidden=!!status.hosting_active;
    if(stopBtn)stopBtn.hidden=!status.hosting_active;
    return status;}).catch(function(error){setHostingResult('状态读取失败：'+error.message);throw error;});}
  function changeHosting(button,path,desired,pending,success){button.disabled=true;setHostingResult(pending);showFeedback(pending,'loading');function retryStatus(){refreshHosting().then(function(status){if(!!status.hosting_active===desired){setHostingResult(success);showFeedback(success,'success');}else{showFeedback('托管状态尚未确认','error',retryStatus);}},function(error){showFeedback('状态刷新失败：'+error.message,'error',retryStatus);});}api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未确认操作');return refreshHosting().then(function(status){if(!!status.hosting_active!==desired){setHostingResult('服务端回应了，但托管没到预期状态');showFeedback('服务端回应了，但托管没到预期状态','error',retryStatus);return;}setHostingResult(success);showFeedback(success,'success');},function(error){showFeedback('服务端已响应，但状态读取失败：'+error.message,'error',retryStatus);});}).catch(function(error){setHostingResult('托管操作未确认：'+error.message);showFeedback('托管操作未确认：'+error.message,'error',function(){changeHosting(button,path,desired,pending,success);});}).then(function(){button.disabled=false;});}
  document.getElementById('hosting-start').onclick=function(){changeHosting(this,'/api/hosting/start',true,'正在开始托管，请稍候…','托管已开始并确认');};
  document.getElementById('hosting-stop').onclick=function(){changeHosting(this,'/api/hosting/stop',false,'正在结束托管，请稍候…','托管已结束并确认');}; refreshHosting().catch(function(){});
  // 每项是 [发给后端的键名, 输入框元素 id]。两边必须写成一张显式对照表：
  // 早先写成 'push-'+name 拼 id，改元素 id 时静默取不到元素、输入值根本发不出去。
  var pushFields=[['wxpusher_app_token','push-wxpusher_key'],['wxpusher_uids','push-wxpusher_uids'],['wxpusher_topic_ids','push-wxpusher_topic_ids'],['serverchan_keys','push-serverchan_keys'],['pushplus_tokens','push-pushplus_key'],['webhook_urls','push-robot-urls']];
  function pushInput(pair){return document.getElementById(pair[1]);}
  // 通道 id 是内部标识符，给用户看的必须是服务名。
  var PUSH_CHANNEL_NAMES={wxpusher:'WxPusher',serverchan:'Server酱',pushplus:'PushPlus',webhook:'Webhook','qq-bot':'QQ 机器人'};
  function pushChannelName(id){return PUSH_CHANNEL_NAMES[id]||String(id||'未知通道');}
  function applyPushState(state,channels){if(!state)return;pushFields.forEach(function(pair){var input=pushInput(pair);var item=state[pair[0]];if(!input)return;input.placeholder=(item&&item.set)?('已配置：'+item.masked.join('、')+'（留空不修改）'):'还没设置';});var list=(channels||[]).map(pushChannelName);document.getElementById('push-result').textContent='当前推送通道：'+(list.join('、')||'还没设置 · 去设置里加一个推送通道')+'。';}
  function savePushSettings(all){var values={};pushFields.forEach(function(pair){var input=pushInput(pair);if(!input)return;var text=input.value.trim();if(text||all)values[pair[0]]=text;});var resultBox=document.getElementById('push-result');if(!Object.keys(values).length){resultBox.textContent='没有要保存的内容：留空的项不会修改，清空请用「清除全部推送设置」。';return;}resultBox.textContent='正在保存…';api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');pushFields.forEach(function(pair){var input=pushInput(pair);if(input)input.value='';});applyPushState(result.push, result.channels);resultBox.textContent='已保存，当前推送通道：'+(((result.channels||[]).map(pushChannelName)).join('、')||'还没设置 · 去设置里加一个推送通道')+'。';showFeedback('推送设置已保存','success');}).catch(function(error){resultBox.textContent='保存失败：'+error.message;showFeedback('推送设置未保存：'+error.message,'error',function(){savePushSettings(all);});});}
  document.getElementById('push-save').onclick=function(){savePushSettings(false);};
  document.getElementById('push-clear').onclick=function(){if(!window.confirm('确定清空全部推送通道配置吗？清空后「截止提醒」将发不出去。'))return;savePushSettings(true);};
  function applyPushHelp(map){if(!map)return;var links=document.querySelectorAll('a.push-help[data-help]');for(var i=0;i<links.length;i++){var url=safeUrl(map[links[i].getAttribute('data-help')]);if(url){links[i].href=url;links[i].rel='noreferrer';}}}
  function testPush(){var box=document.getElementById('push-result');box.textContent='正在发送测试消息…';return api('/api/settings/test-push',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){var list=result.results||[];if(!list.length){box.textContent=result.error||'还没有配置任何推送通道：先在上面填好并保存。';return;}var text=list.map(function(item){return (item.ok?'收到：':'失败：')+item.channel+(item.ok?'':(' · '+(item.error||'原因不明')));}).join('；');box.textContent=result.ok?('测试消息已发出——'+text):('有的通道没发成功——'+text);if(result.ok)showFeedback('测试消息已发出','success');}).catch(function(error){box.textContent='测试失败：'+error.message;});}
  document.getElementById('push-test').onclick=function(){testPush();};
  var llmProviders=[],llmState=null;
  function llmProvider(id){for(var i=0;i<llmProviders.length;i++){if(llmProviders[i].id===id)return llmProviders[i];}return null;}
  function setLlmBusy(busy){document.getElementById('llm-provider').disabled=busy;document.getElementById('llm-model').disabled=busy;document.getElementById('llm-endpoint').disabled=busy;document.getElementById('llm-save').disabled=busy;document.getElementById('llm-off').disabled=busy;}
  function renderLlmProvider(id){var item=llmProvider(id);if(!item)return;var hint=document.getElementById('llm-provider-hint');hint.textContent=item.hint||'';document.getElementById('llm-model').value=item.model||'';document.getElementById('llm-endpoint').value=item.endpoint||'';document.getElementById('llm-endpoint-row').hidden=(id!=='custom');var list=document.getElementById('llm-steps-list');list.textContent='';var steps=item.key_steps||[];steps.forEach(function(text){list.appendChild(el('li','',text));});document.getElementById('llm-steps').hidden=!steps.length;var link=document.getElementById('llm-help-link');var lu=safeUrl(item.key_url);if(lu){link.href=lu;link.textContent=(item.needs_key?'打开拿钥匙的页面':'打开官网，照着上面做');link.hidden=false;}else{link.hidden=true;}var keyInput=document.getElementById('llm-key');var keyRow=keyInput.closest('.preference');
  // 用户正往里粘钥匙时别清掉。
  if(document.activeElement!==keyInput)keyInput.value='';
  if(!item.needs_key){keyInput.placeholder='这家不用钥匙';if(keyRow)keyRow.hidden=true;}else{keyRow&&(keyRow.hidden=false);keyInput.placeholder=(llmState&&llmState.key_set&&llmState.provider===id)?('已配置：'+llmState.key_masked+'（留空不修改）'):'粘贴你的钥匙';}}
  function applyLlmState(state){if(!state)return;llmState=state;var select=document.getElementById('llm-provider');select.value=state.provider||'';renderLlmProvider(state.provider||'');if(state.model&&document.activeElement!==document.getElementById('llm-model'))document.getElementById('llm-model').value=state.model;if(state.endpoint&&document.activeElement!==document.getElementById('llm-endpoint'))document.getElementById('llm-endpoint').value=state.endpoint;var box=document.getElementById('llm-result');var item=llmProvider(state.provider)||{};if(state.provider==='off'||!state.enabled){box.textContent='现在没有用 AI：群里消息只归档，不生成摘要。想让它替你读消息、写待办，就在上面选一家（有免费额度的那几家不用花钱）。';}else if(item.needs_key&&!state.key_set){box.textContent='还差最后一步——钥匙：'+((item.name||'这家服务')+'要一串密钥。')+' 点上面的链接拿到后粘进「钥匙」框，再点「保存并测试」。';}else{box.textContent='正在使用 '+(state.model||'未设置')+'（'+(item.name||'已选服务商')+'）'+(state.vision?'（能读图片）':'（不能读图片，图片只记录名字）')+'。';}}
  function saveLlm(thenTest){var providerId=document.getElementById('llm-provider').value;var payload={llm_provider:providerId};var key=document.getElementById('llm-key').value.trim();if(key)payload.llm_api_key=key;var model=document.getElementById('llm-model').value.trim();if(model)payload.llm_model=model;if(providerId==='custom')payload.llm_endpoint=document.getElementById('llm-endpoint').value.trim();var box=document.getElementById('llm-result');box.textContent='正在保存…';setLlmBusy(true);return api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');applyLlmState(result.llm);box.textContent='已保存。';showFeedback('AI 接入已保存','success');if(thenTest)return testLlm();}).catch(function(error){box.textContent='保存失败：'+error.message;showFeedback('AI 接入未保存：'+error.message,'error',function(){saveLlm(thenTest);});}).then(function(){setLlmBusy(false);});}
  function testLlm(){var box=document.getElementById('llm-result');box.textContent='正在请求 AI，请稍候。';return api('/api/settings/test-llm',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){if(!result.ok){box.textContent='没连通：'+(result.error||'原因不明');showFeedback('AI 连接测试没通过','error');return;}box.textContent='连通了：'+(result.model||'模型')+' 回了「'+(result.reply||'')+'」（'+(result.seconds||0)+' 秒）。';showFeedback('AI 连接测试通过','success');}).catch(function(error){box.textContent='测试失败：'+error.message;});}
  document.getElementById('llm-provider').onchange=function(){var item=llmProvider(this.value)||{};renderLlmProvider(this.value);document.getElementById('llm-result').textContent=item.hint||'';};
  document.getElementById('llm-save').onclick=function(){saveLlm(true);};
  document.getElementById('llm-off').onclick=function(){document.getElementById('llm-provider').value='off';renderLlmProvider('off');saveLlm(false);};
  // 卡片都建完了，把用户没保存的输入放回去（异步回填之后再按 settingsUnsaved 让位）。
  restoreSettingsInputs(settingsUnsaved);
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
    // 运行信息/纠错提示原来是散在 .settings-body 里的一堆兄弟节点，
    // 既不能定位也不能被搜索命中：各自包成一块带锚点的面板。
    var runtimeBox = el('section', 'surface hosting-settings');
    runtimeBox.dataset.settingAnchor = 'runtime';
    var head = el('div', 'section-head');
    head.appendChild(el('h2', 'section-title', '运行信息'));
    runtimeBox.appendChild(head);
    root.appendChild(runtimeBox);
    var startedAt = formatServiceStartedAt(data.started_at);
    // 实际端口：环境变量优先于 .env，启动器还可能在端口被占时往上找一个。所以这里显示的是
    // 「真正听在哪」+ 它从哪来，不是 .env 里写的那一行。字段缺失（老后端）就整行不显示，
    // 不显示「未知」，也不显示 0。
    var webPort = Number(data.web_port);
    var portRow = null;
    if (isFinite(webPort) && webPort > 0) {
      portRow = String(data.web_port_source || '') === 'env'
        ? ['服务端口', webPort + '（来自环境变量，已覆盖 .env）', '', 'web-port']
        : ['服务端口', String(webPort), '', 'web-port'];
    }
    var rows = [
      ['QQ 监控群', (data.groups || []).join('、') || '还没设置'],
      ['推送通道', (data.channels || []).join('、') || '无'],
      ['截止提醒', data.reminders || '未开启'],
      ['推送预算', data.push_budget || '不限'],
      ['夜间静默', data.quiet_hours || '未设置'],
      ['待办统计', '共 ' + data.stats.total + ' 件，未完成 ' + data.stats.open + ' 件'],
      ['服务时间', startedAt.text, startedAt.title],
      ['微信', '读取中…'],
      ['学习通', '读取中…']
    ];
    // 端口这一行只在这台后端真的报出来时才加：老后端 / 取不到就整行不出现，不留「未知」。
    if (portRow) rows.unshift(portRow);
    var rowValue = {};
    rows.forEach(function (row) {
      var kv = el('div', 'kv');
      if (row[3]) kv.dataset.row = row[3];
      kv.appendChild(el('span', '', row[0]));
      var valueSpan = el('span', '', row[1]);
      if (row[2]) valueSpan.title = row[2];
      rowValue[row[0]] = valueSpan;
      kv.appendChild(valueSpan);
      runtimeBox.appendChild(kv);
    });
    // 版本号不再占顶栏：把 page.html 里服务端替换好的版本徽标节点搬进「运行信息」。
    var versionSlot = document.getElementById('channel-badge-slot');
    if (versionSlot && versionSlot.parentNode !== root) {
      var versionKv = el('div', 'kv');
      versionKv.appendChild(el('span', '', '版本'));
      versionSlot.hidden = false;
      versionKv.appendChild(versionSlot);
      runtimeBox.appendChild(versionKv);
    }
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
      var insightsBox = el('section', 'surface hosting-settings');
      insightsBox.dataset.settingAnchor = 'insights';
      var tipHead = el('div', 'section-head');
      tipHead.appendChild(el('h2', 'section-title', '纠错提示'));
      insightsBox.appendChild(tipHead);
      var tipList = el('ul', 'insight-list');
      insights.forEach(function (text) { tipList.appendChild(el('li', '', text)); });
      insightsBox.appendChild(tipList);
      root.appendChild(insightsBox);
    }
  });
}
/* 「项目地址 / 什么是 agent？」入口：三处提示共用一个原生 dialog。
   Esc 由 dialog 自己处理；点背景或「知道了」关闭。 */
function setupAgentHelp() {
  var dialog = document.getElementById('agent-help-dialog');
  if (!dialog) return;

  function openDialog() {
    if (dialog.open) return;
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    var closeBtn = dialog.querySelector('.agent-help-close');
    if (closeBtn) closeBtn.focus();
  }

  function closeDialog() {
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target.closest ? event.target.closest('.agent-hint-open') : null;
    if (trigger) {
      event.preventDefault();
      openDialog();
      return;
    }
    if (!dialog.open) return;
    if (event.target.closest('.agent-help-close') ||
        event.target.closest('.agent-help-actions button')) closeDialog();
  });

  dialog.addEventListener('click', function (event) {
    if (event.target === dialog) closeDialog();
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', setupAgentHelp);
} else {
  setupAgentHelp();
}
