// 悬浮通知中心：右上角堆叠卡片 + 详情弹窗。
// 设计语汇与全站一致：纸面卡片（--paper-raise / --line / --r-3）、语义色圆点、
// 关动效由全局 [data-motion=paused] 与 prefers-reduced-motion 兜底。
// 其它功能接入：NHNotify.push({id, level, title, body, actionLabel, onOpen, sticky, ttl})
//   level: 'info' | 'ok' | 'warn' | 'bad' | 'critical'（圆点与描边随语义色走；
//          critical 用紧急红 + 光晕，给「关键公告 / 严重故障」这种必须处理的通知）
//   id 相同的推送会去重更新；sticky 通知不自动消失；ttl 毫秒后自动收起。
//   点击卡片（或「查看」按钮）触发 onOpen——通常用来打开详情弹窗。
//   priority:true 插到栈顶（公告这类要压在体检提示之上）；dismissible:false 不显示 ×。
//   用户点 × 会先触发 onDismiss 再撤卡（超时/被挤掉/清空不算——程序性撤除不回调）。
(function () {
  'use strict';

  var stack = null;
  var modal = null;
  var modalTitle = null;
  var modalBody = null;
  var modalActions = null;
  var lastFocused = null;
  var items = new Map(); // id -> {el, timer, paused}
  var MAX_VISIBLE = 3;

  function ensure() {
    if (stack) return;
    stack = document.getElementById('notice-stack');
    modal = document.getElementById('notice-modal');
    if (modal) {
      modalTitle = modal.querySelector('#notice-modal-title');
      modalBody = modal.querySelector('#notice-modal-body');
      modalActions = modal.querySelector('#notice-modal-actions');
      modal.querySelector('.notice-modal-close').addEventListener('click', closeModal);
      // 点遮罩关闭：只有点在卡片外面才算。
      modal.addEventListener('click', function (event) {
        if (event.target === modal) closeModal();
      });
      modal.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          closeModal();
        }
      });
    }
  }

  function levelOf(level) {
    return ['info', 'ok', 'warn', 'bad', 'critical'].indexOf(level) !== -1 ? level : 'info';
  }

  // 栈满时挤掉最旧的一张。sticky 只表示「不自动消失」，不能因此把栈堵死：
  // 全是 sticky（比如公告 + 体检提示 + 关键故障一起挂着）时没有可淘汰项，
  // 之前的 while 循环就会永远转下去，整页卡死。找不到非 sticky 就退而删最旧的。
  function dropOldest() {
    var oldest = null;
    var fallback = null;
    items.forEach(function (record, id) {
      if (!fallback || record.created < fallback.created) fallback = {id: id, created: record.created};
      if (record.sticky) return;
      if (!oldest || record.created < oldest.created) oldest = {id: id, created: record.created};
    });
    var victim = oldest || fallback;
    if (victim) dismiss(victim.id);
  }

  function startTimer(id, ttl) {
    var record = items.get(id);
    if (!record || !ttl) return;
    record.timer = window.setTimeout(function () { dismiss(id); }, ttl);
  }

  function push(options) {
    ensure();
    if (!stack) return null;
    var id = String(options.id || 'notice-' + Date.now());
    var level = levelOf(options.level);
    var sticky = options.sticky === true;
    var existing = items.get(id);
    if (existing) {
      // 同 id 再推：原地更新内容并重新计时，不重复堆卡（priority/dismissible 用首次的）。
      existing.el.dataset.level = level;
      existing.el.querySelector('.notice-title').textContent = options.title || '';
      existing.el.querySelector('.notice-body').textContent = options.body || '';
      existing.el.querySelector('.notice-body').hidden = !options.body;
      existing.el.querySelector('.notice-open').hidden = !options.actionLabel;
      existing.el.querySelector('.notice-open').textContent = options.actionLabel || '';
      existing.onOpen = options.onOpen || null;
      existing.onDismiss = options.onDismiss || null;
      if (existing.timer) window.clearTimeout(existing.timer);
      startTimer(id, sticky ? 0 : (options.ttl || 6000));
      return id;
    }
    // guard 是最后一道保险：淘汰逻辑哪天再出岔子，宁可多挂一张卡，也不能把主线程转死。
    var guard = 0;
    while (items.size >= MAX_VISIBLE && guard < 16) { dropOldest(); guard += 1; }

    var card = document.createElement('article');
    card.className = 'notice-card';
    card.dataset.level = level;
    card.dataset.noticeId = id;   // 探针/测试用它按 id 找卡，不依赖栈顶是谁
    card.dataset.hasOpen = options.actionLabel ? '1' : '';
    var dot = document.createElement('i');
    dot.className = 'notice-dot';
    dot.setAttribute('aria-hidden', 'true');
    card.appendChild(dot);
    var main = document.createElement('div');
    main.className = 'notice-main';
    var title = document.createElement('p');
    title.className = 'notice-title';
    title.textContent = options.title || '';
    main.appendChild(title);
    var body = document.createElement('p');
    body.className = 'notice-body';
    body.textContent = options.body || '';
    body.hidden = !options.body;
    main.appendChild(body);
    card.appendChild(main);
    var side = document.createElement('div');
    side.className = 'notice-side';
    var openButton = document.createElement('button');
    openButton.type = 'button';
    openButton.className = 'notice-open';
    openButton.hidden = !options.actionLabel;
    openButton.textContent = options.actionLabel || '';
    openButton.addEventListener('click', function (event) {
      event.stopPropagation();
      if (typeof options.onOpen === 'function') options.onOpen(id);
    });
    side.appendChild(openButton);
    if (options.dismissible !== false) {
      var close = document.createElement('button');
      close.type = 'button';
      close.className = 'notice-close';
      close.setAttribute('aria-label', '关闭通知');
      close.textContent = '×';
      close.addEventListener('click', function (event) {
        event.stopPropagation();
        dismiss(id, true);   // 只有用户点 × 才算「我关掉了这条」
      });
      side.appendChild(close);
    }
    card.appendChild(side);

    var record = {el: card, timer: null, paused: false, sticky: sticky, created: Date.now(), onOpen: options.onOpen || null, onDismiss: options.onDismiss || null};
    items.set(id, record);
    // 整卡可点（有 onOpen 才有动作语义）：点卡与点「查看」等价。
    if (typeof options.onOpen === 'function') {
      card.classList.add('is-clickable');
      card.setAttribute('role', 'button');
      card.tabIndex = 0;
      card.addEventListener('click', function () { options.onOpen(id); });
      card.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          options.onOpen(id);
        }
      });
    }
    // 悬停暂停自动收起：正在读的时候消失会打断人。
    card.addEventListener('mouseenter', function () {
      if (record.timer) { window.clearTimeout(record.timer); record.timer = null; }
    });
    card.addEventListener('mouseleave', function () { startTimer(id, options.ttl || 0); });
    if (options.priority === true) stack.insertBefore(card, stack.firstChild);
    else stack.appendChild(card);
    startTimer(id, sticky ? 0 : (options.ttl || 6000));
    return id;
  }

  // byUser=true 只在用户点 × 时才传：超时、被更满的栈挤掉、clear() 都不算用户关掉，
  // 不能触发 onDismiss（否则公告会被记成「用户已关闭」，下次启动就不再提醒了）。
  function dismiss(id, byUser) {
    var record = items.get(id);
    if (!record) return;
    if (record.timer) window.clearTimeout(record.timer);
    record.el.remove();
    items.delete(id);
    if (byUser === true && typeof record.onDismiss === 'function') record.onDismiss(id);
  }

  function clear() {
    ensure();
    if (!stack) return;
    Array.from(items.keys()).forEach(function (id) { dismiss(id); });
  }

  function openModal(options) {
    ensure();
    if (!modal) return;
    lastFocused = document.activeElement;
    modalTitle.textContent = options.title || '';
    modalBody.textContent = '';
    if (options.bodyNode) modalBody.appendChild(options.bodyNode);
    else if (options.body) modalBody.textContent = options.body;
    modalActions.textContent = '';
    (options.actions || []).forEach(function (action) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn' + (action.primary ? ' primary' : '');
      button.textContent = action.label;
      button.addEventListener('click', function () {
        var keep = action.keepOpen === true;
        if (typeof action.onClick === 'function') action.onClick();
        if (!keep) closeModal();
      });
      modalActions.appendChild(button);
    });
    modal.hidden = false;
    var focusable = modalActions.querySelector('button') || modal.querySelector('.notice-modal-close');
    if (focusable) focusable.focus();
  }

  function closeModal() {
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    lastFocused = null;
  }

  window.NHNotify = {
    push: push,
    dismiss: dismiss,
    clear: clear,
    openModal: openModal,
    closeModal: closeModal
  };
})();
