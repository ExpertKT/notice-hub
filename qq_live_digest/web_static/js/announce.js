// ---- 公告块：从 page.html 原样拆出（未改逻辑）。依赖 page.html 里的 api()/safeUrl() 与页面 DOM。----
var announcementState = null;
function getDismissedAnnouncements() {
  try { var value = JSON.parse(localStorage.getItem('seen_announce_ids') || '[]'); return Array.isArray(value) ? value.map(String) : []; }
  catch (error) { return []; }
}
function renderAnnouncementBanner() {
  var banner = document.getElementById('announce-banner');
  if (!banner) return;
  banner.textContent = '';
  var items = announcementState && Array.isArray(announcementState.items) ? announcementState.items : [];
  var dismissed = getDismissedAnnouncements();
  var read = getAnnounceRead();
  var item = items.find(function (entry) { return entry && entry.id && !read.includes(String(entry.id)) && !dismissed.includes(String(entry.id)) && !getAnnounceModalSeen().includes(String(entry.id)); });
  if (!item) { banner.hidden = true; return; }
  var level = ['info', 'warning', 'critical'].includes(item.level) ? item.level : 'info';
  banner.dataset.level = level;
  var copy = document.createElement('div'); copy.className = 'announcement-copy';
  var title = document.createElement('p'); title.className = 'announcement-title';
  title.textContent = [item.title, item.date].filter(Boolean).join(' · '); copy.appendChild(title);
  var detail = document.createElement('pre'); detail.className = 'announcement-detail';
  detail.textContent = String(item.body || item.content || item.description || ''); detail.hidden = true;
  if (detail.textContent) copy.appendChild(detail);
  var actions = document.createElement('div'); actions.className = 'announcement-actions';
  if (detail.textContent) {
    var toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'btn'; toggle.textContent = '详情'; toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', function () { detail.hidden = !detail.hidden; toggle.textContent = detail.hidden ? '详情' : '收起'; toggle.setAttribute('aria-expanded', String(!detail.hidden)); });
    actions.appendChild(toggle);
  }
  if (!item.seen_required) {
    var close = document.createElement('button'); close.type = 'button'; close.className = 'btn'; close.textContent = '关闭'; close.setAttribute('aria-label', '关闭公告');
    close.addEventListener('click', function () { var ids = getDismissedAnnouncements(); ids.push(String(item.id)); try { localStorage.setItem('seen_announce_ids', JSON.stringify(Array.from(new Set(ids)).slice(-100))); } catch (error) {} renderAnnouncementBanner(); });
    actions.appendChild(close);
  }
  banner.appendChild(copy); banner.appendChild(actions); banner.hidden = false;
}
function renderAnnouncementList() {
  var list = document.getElementById('announce-list');
  if (!list) return;
  list.textContent = '';
  var items = announcementState && Array.isArray(announcementState.items) ? announcementState.items : [];
  if (!items.length) {
    var note = document.createElement('p'); note.className = 'announcement-muted';
    note.textContent = announcementState ? (announcementState.ok ? '暂无公告。' : '公告暂时不可用。') : '正在读取公告…'; list.appendChild(note); return;
  }
  items.forEach(function (item) {
    var entry = document.createElement('details'); entry.className = 'announcement-entry';
    var summary = document.createElement('summary'); summary.textContent = [item.title, item.date, item.level || 'info'].filter(Boolean).join(' · '); entry.appendChild(summary);
    var body = document.createElement('pre'); body.textContent = String(item.body || item.content || item.description || ''); entry.appendChild(body);
    list.appendChild(entry);
  });
}
// ---- 公告铃铛（吸顶栏未读提醒 + 公告面板）----
// 产品规矩：任何**用户可见**的改动都必须新增一条公告（写进 announcements.json），
// 用户在铃铛里能看到「这版改了什么」；没公告的可见改动不合并。
var ANNOUNCE_READ_KEY = 'nh_ann_read';
// 上次「强制刷新公告」的时刻（毫秒）：铃铛点开最多 30 秒打一次网络。
var announceForcedAt = 0;
function getAnnounceRead() {
  try { var value = JSON.parse(localStorage.getItem(ANNOUNCE_READ_KEY) || '[]'); return Array.isArray(value) ? value.map(String) : []; }
  catch (error) { return []; }
}
function markAnnounceRead(ids) {
  var list = getAnnounceRead();
  (ids || []).forEach(function (id) { if (id != null && id !== '') list.push(String(id)); });
  try { localStorage.setItem(ANNOUNCE_READ_KEY, JSON.stringify(Array.from(new Set(list)).slice(-200))); } catch (error) {}
}
// 面板列所有公告，按日期倒序（同日期保持服务端顺序）。
function announceItems() {
  var items = (announcementState && Array.isArray(announcementState.items) ? announcementState.items : []).filter(function (item) { return item && item.id; });
  return items.slice().sort(function (a, b) { return String(b.date || '').localeCompare(String(a.date || '')); });
}
function announcementsUnreadCount() {
  var read = getAnnounceRead();
  return announceItems().filter(function (item) { return !read.includes(String(item.id)); }).length;
}
function renderAnnounceBell() {
  var badge = document.getElementById('announce-bell-badge');
  var bell = document.getElementById('announce-bell');
  var count = announcementsUnreadCount();
  if (badge) { badge.textContent = count > 99 ? '99+' : String(count); badge.hidden = count <= 0; }
  if (bell) bell.setAttribute('aria-label', count > 0 ? ('公告，' + count + ' 条未读') : '公告');
}
function announcePanelItemNode(item, unread) {
  var wrap = document.createElement('article');
  wrap.className = 'announce-panel-item' + (unread ? ' is-unread' : '');
  var head = document.createElement('div'); head.className = 'announce-panel-item-head';
  var title = document.createElement('h3'); title.className = 'announce-panel-item-title'; title.textContent = String(item.title || '公告');
  head.appendChild(title);
  if (item.date) { var date = document.createElement('span'); date.className = 'announce-panel-item-date'; date.textContent = String(item.date); head.appendChild(date); }
  wrap.appendChild(head);
  var bodyText = String(item.body || item.content || item.description || '');
  if (bodyText) {
    // 长文默认折叠；两三行以内的短公告直接展开，省一次点击。
    var lines = bodyText.split(/\r?\n/).filter(function (line) { return line.trim(); }).length;
    var details = document.createElement('details'); details.className = 'announce-panel-body'; details.open = lines <= 2;
    var summary = document.createElement('summary'); summary.textContent = '正文'; details.appendChild(summary);
    var text = document.createElement('div'); text.className = 'announce-panel-text';
    announceBodyNodes(item).forEach(function (node) { text.appendChild(node); });
    details.appendChild(text); wrap.appendChild(details);
  }
  var url = safeUrl(item.url);
  if (url) {
    var link = document.createElement('a'); link.className = 'announce-panel-link';
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = '查看 Release ↗';
    wrap.appendChild(link);
  }
  return wrap;
}
function renderAnnouncePanel() {
  var list = document.getElementById('announce-panel-list');
  if (!list) return;
  list.textContent = '';
  var items = announceItems();
  var read = getAnnounceRead();
  var unread = 0;
  if (!items.length) {
    var note = document.createElement('p'); note.className = 'announcement-muted';
    note.textContent = announcementState ? (announcementState.ok ? '暂无公告。' : '公告暂时不可用。') : '正在读取公告…';
    list.appendChild(note);
  }
  items.forEach(function (item) {
    var isUnread = !read.includes(String(item.id));
    if (isUnread) unread += 1;
    list.appendChild(announcePanelItemNode(item, isUnread));
  });
  var count = document.getElementById('announce-panel-count');
  if (count) count.textContent = unread ? (unread + ' 条未读') : '';
  var readAll = document.getElementById('announce-panel-read-all');
  if (readAll) readAll.disabled = unread === 0;
  var hint = document.getElementById('announce-panel-hint');
  if (hint) hint.textContent = '共 ' + items.length + ' 条';
}
function announcePanelFocusable(panel) {
  if (!panel || !panel.querySelectorAll) return [];
  return Array.prototype.slice.call(panel.querySelectorAll('button, a[href], summary, [tabindex="0"]')).filter(function (node) { return node && !node.disabled && !node.hidden; });
}
var announcePanelReturnFocus = null;
// B5：焦点在面板外面时，绑在 panel 上的 keydown 收不到 Esc。面板打开期间把监听挂到 document
// 捕获阶段，关闭就解绑，别让页面别处的 Esc 也被我们吞掉。
function setAnnounceKeydown(on) {
  if (!document.addEventListener) return;
  if (on) document.addEventListener('keydown', announcePanelKeydown, true);
  else document.removeEventListener('keydown', announcePanelKeydown, true);
}
function openAnnouncePanel() {
  var panel = document.getElementById('announce-panel');
  var bell = document.getElementById('announce-bell');
  if (!panel) return;
  announcePanelReturnFocus = document.activeElement || bell;
  renderAnnouncePanel();
  // 打开公告栏就是要看最新的：30 秒内只强制拉一次，别每开一次都打一次网络。
  if (Date.now() - announceForcedAt > 30 * 1000) {
    announceForcedAt = Date.now();
    refreshAnnouncements(true);
  }
  panel.hidden = false;
  setAnnounceKeydown(true);
  if (bell) bell.setAttribute('aria-expanded', 'true');
  var close = document.getElementById('announce-panel-close');
  if (close && close.focus) close.focus();
}
function closeAnnouncePanel(restoreFocus) {
  var panel = document.getElementById('announce-panel');
  var bell = document.getElementById('announce-bell');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  setAnnounceKeydown(false);
  if (bell) bell.setAttribute('aria-expanded', 'false');
  if (restoreFocus === false) return;
  var target = announcePanelReturnFocus || bell;
  if (target && target.focus) target.focus();
}
function announcePanelKeydown(event) {
  if (event && event.key === 'Escape') { event.preventDefault(); closeAnnouncePanel(true); return; }
  if (!event || event.key !== 'Tab') return;
  var nodes = announcePanelFocusable(document.getElementById('announce-panel'));
  if (!nodes.length) return;
  var first = nodes[0], last = nodes[nodes.length - 1], active = document.activeElement;
  if (event.shiftKey) { if (active === first || nodes.indexOf(active) < 0) { event.preventDefault(); last.focus(); } }
  else if (active === last || nodes.indexOf(active) < 0) { event.preventDefault(); first.focus(); }
}
function setupAnnounceBell() {
  var bell = document.getElementById('announce-bell');
  var panel = document.getElementById('announce-panel');
  if (!bell || !panel) return;
  bell.addEventListener('click', function () { if (panel.hidden) openAnnouncePanel(); else closeAnnouncePanel(true); });
  var close = document.getElementById('announce-panel-close');
  if (close) close.addEventListener('click', function () { closeAnnouncePanel(true); });
  var readAll = document.getElementById('announce-panel-read-all');
  if (readAll) readAll.addEventListener('click', function () {
    markAnnounceRead(announceItems().map(function (item) { return item.id; }));
    renderAnnounceBell(); renderAnnouncePanel(); renderAnnouncementBanner(); renderAnnounceModal();
  });
  // 键盘监听由 openAnnouncePanel / closeAnnouncePanel 统一挂到 document 上，这里不再绑 panel。
  // 点面板外面关掉；点铃铛本身交给上面的 click 切换，这里跳过，免得关掉又立刻打开。
  document.addEventListener('mousedown', function (event) {
    if (panel.hidden) return;
    var target = event && event.target;
    if (panel.contains && panel.contains(target)) return;
    if (bell.contains && bell.contains(target)) return;
    closeAnnouncePanel(false);
  });
}
// ---- 启动公告弹窗（待办台打开时弹，符合单页自己的视觉）----
var announceModalItems = [];
var announceModalIndex = 0;
function getAnnounceModalSeen() {
  try { var value = JSON.parse(localStorage.getItem('nh_ann_seen') || '[]'); return Array.isArray(value) ? value.map(String) : []; }
  catch (error) { return []; }
}
function markAnnounceModalSeen(id) {
  var ids = getAnnounceModalSeen(); ids.push(String(id));
  try { localStorage.setItem('nh_ann_seen', JSON.stringify(Array.from(new Set(ids)).slice(-100))); } catch (error) {}
}
function announceItemSeen(item) {
  if (!item || !item.id) return true;
  if (item.seen_required) return false;  // critical / sticky：每次启动都弹
  var id = String(item.id);
  return getAnnounceModalSeen().includes(id) || getDismissedAnnouncements().includes(id) || getAnnounceRead().includes(id);
}
function appendAnnounceText(node, text) {
  // 正文来自网络：只用 textContent / createTextNode 拼 DOM，绝不拼 HTML 字符串，
  // 因此它没有任何被解析成标记的机会；**粗体** 之外的内容一律按纯文本显示。
  String(text).split(/\*\*(.+?)\*\*/g).forEach(function (part, index) {
    if (!part) return;
    if (index % 2 === 1) { var strong = document.createElement('strong'); strong.textContent = part; node.appendChild(strong); }
    else { node.appendChild(document.createTextNode(part)); }
  });
}
function announceBodyNodes(item) {
  var nodes = [];
  var list = null;
  String(item.body || item.content || item.description || '').split(/\r?\n/).forEach(function (line) {
    var text = line.trim();
    if (!text) { list = null; return; }
    if (text.slice(0, 2) === '- ' || text.slice(0, 2) === '* ') {
      if (!list) { list = document.createElement('ul'); nodes.push(list); }
      var entry = document.createElement('li'); appendAnnounceText(entry, text.slice(2).trim()); list.appendChild(entry); return;
    }
    list = null;
    var paragraph = document.createElement('p'); appendAnnounceText(paragraph, text); nodes.push(paragraph);
  });
  if (!nodes.length) nodes.push(document.createElement('p'));
  return nodes;
}
function renderAnnounceModal() {
  var modal = document.getElementById('announce-modal');
  if (!modal) return;
  var announcementBanner=document.getElementById('announce-banner');
  var setupWizard=document.getElementById('platform-setup-wizard');
  var onboarding=document.getElementById('onboarding');
  if ((announcementBanner&&!announcementBanner.hidden)||(setupWizard&&!setupWizard.hidden)||(onboarding&&!onboarding.hidden)){modal.hidden=true;return;}
  var items = (announcementState && Array.isArray(announcementState.items) ? announcementState.items : []).filter(function (item) { return item && item.id && !announceItemSeen(item); });
  announceModalItems = items;
  if (!items.length) { modal.hidden = true; return; }
  if (announceModalIndex >= items.length) announceModalIndex = 0;
  var item = items[announceModalIndex];
  modal.dataset.level = ['info', 'warning', 'critical'].includes(item.level) ? item.level : 'info';
  var title = document.getElementById('announce-modal-title');
  if (title) title.textContent = String(item.title || '公告');
  var version = document.getElementById('announce-modal-version');
  if (version) version.textContent = '当前版本 ' + String((announcementState && announcementState.current_version) || '未知');
  var body = document.getElementById('announce-modal-body');
  if (body) { body.textContent = ''; announceBodyNodes(item).forEach(function (node) { body.appendChild(node); }); }
  var link = document.getElementById('announce-modal-link');
  if (link) { var u = safeUrl(item.url); if (u) { link.href = u; link.hidden = false; } else { link.hidden = true; } }
  var prev = document.getElementById('announce-modal-prev');
  var next = document.getElementById('announce-modal-next');
  var count = document.getElementById('announce-modal-count');
  if (prev) { prev.hidden = items.length < 2; prev.disabled = announceModalIndex === 0; }
  if (next) { next.hidden = items.length < 2; next.disabled = announceModalIndex >= items.length - 1; }
  if (count) count.textContent = items.length > 1 ? ((announceModalIndex + 1) + ' / ' + items.length) : '';
  // 公告只在顶部横幅里展示；不再自动弹居中窗口遮挡内容。
}

function setupAnnounceModal() {
  var modal = document.getElementById('announce-modal');
  if (!modal) return;
  var okButton = document.getElementById('announce-modal-ok');
  if (okButton) okButton.addEventListener('click', function () {
    var item = announceModalItems[announceModalIndex];
    if (item) markAnnounceModalSeen(item.id);
    announceModalIndex = 0;
    modal.hidden = true;
    renderAnnouncementBanner();
    renderAnnounceModal();
  });
  var prev = document.getElementById('announce-modal-prev');
  if (prev) prev.addEventListener('click', function () { announceModalIndex = Math.max(0, announceModalIndex - 1); renderAnnounceModal(); });
  var next = document.getElementById('announce-modal-next');
  if (next) next.addEventListener('click', function () { announceModalIndex = Math.min(announceModalItems.length - 1, announceModalIndex + 1); renderAnnounceModal(); });
}
// force=true 走 `?refresh=1`：服务端会真的去拉一次（自己带防抖，连点不会连打 GitHub）。
function refreshAnnouncements(force) {
  api('/api/announcements' + (force ? '?refresh=1' : '')).then(function (data) {
    announcementState = data && typeof data === 'object' ? data : {ok: false, items: []};
    renderAnnouncementBanner(); renderAnnouncementList(); renderAnnounceModal(); renderAnnounceBell(); renderAnnouncePanel();
    if (announcementState.checking) window.setTimeout(refreshAnnouncements, 1500);
  }).catch(function () {
    announcementState = {ok: false, items: []};
    renderAnnouncementBanner(); renderAnnouncementList(); renderAnnounceModal(); renderAnnounceBell(); renderAnnouncePanel();
    // A4：拉取失败别静默——顶部横幅给一条非阻塞提示（下次成功刷新会自动恢复正常公告）。
    var hintBanner = document.getElementById('announce-banner');
    if (hintBanner) { hintBanner.textContent = '公告暂时无法更新，稍后会自动重试。'; hintBanner.hidden = false; }
  });
}
