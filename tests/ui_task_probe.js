(async function () {
  function check(value, message) { if (!value) throw new Error(message); }
  async function wait(test) { var end = Date.now() + 10000; while (!test()) { if (Date.now() > end) throw new Error('等待待办对话超时'); await new Promise(function (r) { setTimeout(r, 30); }); } }
  function frames(count) { return new Promise(function (resolve) { var left = count; (function step() { if (left <= 0) { resolve(); return; } left -= 1; requestAnimationFrame(step); })(); }); }
  // 等两个坐标连续两帧都不动再读：详情撑开、输入栏贴底重排都有过渡帧，
  // 读到半途的坐标就会把位置断言随机打红（gate-17ab4a3 轮 1 就是这么红的）。
  async function stable(read, tries) {
    var previous = null;
    for (var i = 0; i < (tries || 30); i += 1) {
      await frames(2);
      var now = read();
      if (previous && Math.abs(now.detail.bottom - previous.detail.bottom) <= 0.5 && Math.abs(now.detail.top - previous.detail.top) <= 0.5 && Math.abs(now.form.top - previous.form.top) <= 0.5) return now;
      previous = now;
    }
    return previous;
  }
  // 收起之后也还有一两帧重排（概览上的数字是异步刷回来的），读到半途就会把「跳位」断言随机打红：
  // 先等动画全停（页面上还有 running 的 animation 就继续等），再要求坐标连续三次读数不动，才用同一套 1 像素容差比。
  // 三次而不是两次：机器忙的时候两帧之间可能夹着一帧没跑动的过渡（gate-73b67f2 轮 2 的横 0/-1 就是这么来的）。
  async function quiet() {
    var end = Date.now() + 3000;
    while (Date.now() < end) {
      var running = document.getAnimations ? document.getAnimations().filter(function (item) { return item.playState === 'running'; }) : [];
      if (!running.length) return;
      await frames(2);
    }
  }
  async function settled(read, tries) {
    var previous = read(), same = 0;
    for (var i = 0; i < (tries || 30); i += 1) {
      await frames(2);
      var now = read();
      if (Math.abs(now.top - previous.top) <= 0.5 && Math.abs(now.left - previous.left) <= 0.5) {
        same += 1;
        if (same >= 2) return now;
      } else { same = 0; }
      previous = now;
    }
    return previous;
  }
  // 坐标不在这里编：/api/agent/plan 给的 pois 就是产品自己那份地点表。
  var answer = await (await fetch('/api/agent/plan')).json();
  var pois = (answer && answer.plan && answer.plan.pois) || [];
  check(pois.length > 0, '/api/agent/plan 没给地点');
  var spot = pois.find(function (p) { return p.id === 'tuwen'; }) || pois[0];
  var raw = '明天下午到' + spot.name + '交班会材料，带上学生证和打印好的申请表。';
  var task = {id: 991, summary: '交班会材料 ' + spot.name, source_text: raw, details: ['带上学生证', '申请表需要打印两份'], category: 'action', source: 'manual', urgent_override: true};
  if (!document.body.classList.contains('has-trip')) window.nhTrip.render({pois: pois, trips: [{stops: [{place_id: spot.id, place: spot.name, title: task.summary}], walk_minutes: 0}], counts: {stops: 1}});
  renderTimeline({today: [task], later: [Object.assign({}, task, {id: 992, summary: '另一件待办'})]});
  await new Promise(function (r) { setTimeout(r, 350); });
  var board = document.getElementById('timeline-board');
  var card = board.querySelector('#task-991');
  check(card && card.getBoundingClientRect().height > 0, '有行程时待办列表被隐藏');
  var fold = card.querySelector('.task-detail'), toggle = fold.querySelector('summary');
  // 基线要落在动画之后：看板换列表是 FLIP 过渡（名义 360ms），忙的机器上更长，
  // 上面那个固定 350ms 只能保证「开始画了」，不能保证画完。
  // 量到中间帧就会把「收起后跳位」随机打红（gate-298e175-r2 的横 -4/-15 就是这么来的）。
  async function pinned(read, tries) {
    var previous = null, same = 0;
    for (var i = 0; i < (tries || 60); i += 1) {
      await frames(2);
      var now = read();
      if (previous && Math.abs(now.top - previous.top) <= 0.5 && Math.abs(now.left - previous.left) <= 0.5 && Math.abs(now.width - previous.width) <= 0.5 && now.scroll === previous.scroll) {
        same += 1;
        if (same >= 2) return now;
      } else { same = 0; }
      previous = now;
    }
    throw new Error('待办列表一直没停稳，量不到基线');
  }
  // 卡片在列表里的相对位置：概览上的数字是异步刷新的，页面整体会挪几像素，
  // 那不属于「收起后跳位」，所以基线取相对看板，不取相对整页。
  // 卡片与看板同一帧取，别隔一帧取（隔一帧又是一次中间态）。
  var startRel = await (async function () { await quiet(); return pinned(function () {
    var cardBox = card.getBoundingClientRect(), boardBox = board.getBoundingClientRect();
    return {top: cardBox.top - boardBox.top, left: cardBox.left - boardBox.left, width: cardBox.width, scroll: board.scrollLeft, absTop: cardBox.top + window.scrollY};
  }); })();
  var scroll = startRel.scroll, startAbsTop = startRel.absTop;
  toggle.click();
  await frames(2);
  check(fold.open, '待办详情没有展开');
  var body = fold.querySelector('.task-detail-body');
  Array.from(card.querySelectorAll('details.raw')).forEach(function (item) { item.open = true; });
  body.scrollIntoView({block: 'nearest'});
  var sitting = await stable(function () {
    return {detail: body.getBoundingClientRect(), form: document.getElementById('agent-form').getBoundingClientRect()};
  });
  var bounds = sitting.detail;
  check(bounds.height <= 181 && body.scrollHeight > body.clientHeight, '长详情没有独立滚动');
  var form = sitting.form;
  check(bounds.bottom <= form.top + 1, '详情遮住输入栏');
  // 滚进视口会挪页面：收起后的比对用滚动无关的绝对纵坐标（startAbsTop），
  // 别拿展开后的卡片位置当基线——那是两个不同的布局状态。
  var summary = document.querySelector('section.summary'), summaryStyle = getComputedStyle(summary), summaryBox = summary.getBoundingClientRect();
  var summaryInfo = {display: summaryStyle.display, maxHeight: summaryStyle.maxHeight, height: summaryBox.height, clientHeight: summary.clientHeight, scrollHeight: summary.scrollHeight, overflowY: summaryStyle.overflowY};
  if (innerWidth <= 720) {
    // 手机概览现在是「三格数字 + 明细」一行（用户嫌概览吃掉半屏、地图被顶成一条缝），
    // 40vh 限高那条契约作废；新契约：一行放得下、待办列表照样在页面上、明细点开能滚能关。
    check(summaryStyle.display !== 'none', '手机待办概览被隐藏');
    var strip = document.querySelector('.ov-strip');
    check(strip && strip.getBoundingClientRect().height <= 120, '手机概览没收回一行，还占着 ' + (strip ? Math.round(strip.getBoundingClientRect().height) : '没有') + 'px');
    check(card.getBoundingClientRect().height > 0, '手机概览把待办列表顶没了');
    var moreButton = document.getElementById('ov-more'), ovDetail = document.getElementById('ov-detail');
    check(moreButton && moreButton.getBoundingClientRect().height >= 39.9, '明细入口没有可点的触控区');
    var askButton = card.querySelector('.task-ask'), askBox = askButton.getBoundingClientRect(), askHit = document.elementFromPoint(askBox.left + askBox.width / 2, askBox.top + askBox.height / 2);
    check(askBox.top >= 0 && askBox.bottom <= innerHeight && (askHit === askButton || askButton.contains(askHit)), '问智能体入口被遮住');
    var formHit = document.elementFromPoint(form.left + form.width / 2, form.top + form.height / 2);
    check(document.getElementById('agent-form').contains(formHit), '展开待办区遮住输入栏');
  }
  // 明细浮层的开关放到「收起不跳位」之后测：它会临时挂 body[data-ov-open]，
  // 改变 #overview 的 animation/transform，插在位置比对前面会把基线打歪。
  if (innerWidth <= 720) {
    var ovMore = document.getElementById('ov-more'), ovPanel = document.getElementById('ov-detail');
    ovMore.click();
    await frames(2);
    check(getComputedStyle(ovPanel).display !== 'none' && ovPanel.getBoundingClientRect().height > 0, '明细没打开');
    if (ovPanel.scrollHeight > ovPanel.clientHeight) {
      ovPanel.scrollTop = 60;
      check(ovPanel.scrollTop > 0, '明细不能内部滚动');
      ovPanel.scrollTop = 0;
    }
    document.getElementById('ov-close').click();
    await frames(2);
    check(getComputedStyle(ovPanel).display === 'none', '明细关不上');
  }
  var controls = Array.from(card.querySelectorAll('button,summary,input,select')).filter(function (item) { return item.getBoundingClientRect().height > 0; });
  var small = controls.filter(function (item) { var b = item.getBoundingClientRect(); return b.width < 39.9 || b.height < 39.9; }).map(function (item) { return item.className + ':' + item.getBoundingClientRect().width + 'x' + item.getBoundingClientRect().height; });
  check(!small.length, '触控目标不足40px：' + small.join(','));
  toggle.click();
  await frames(2);
  check(!fold.open, '待办详情没有收起');
  await quiet();
  var afterRel = await settled(function () {
    var cardBox = card.getBoundingClientRect(), boardBox = board.getBoundingClientRect();
    return {top: cardBox.top - boardBox.top, left: cardBox.left - boardBox.left};
  });
  var after = card.getBoundingClientRect();
  check(Math.abs(afterRel.top - startRel.top) <= 1 && Math.abs(afterRel.left - startRel.left) <= 1 && board.scrollLeft === scroll, '收起后列表位置跳动：看板内纵 ' + Math.round(afterRel.top) + '/' + Math.round(startRel.top) + '，横 ' + Math.round(afterRel.left) + '/' + Math.round(startRel.left) + '，横向滚动 ' + board.scrollLeft + '/' + scroll + '，整页纵 ' + Math.round(after.top + window.scrollY) + '/' + Math.round(startAbsTop));
  if (window.TASK_SHOT === 'detail') { fold.open = true; return {pass: true, scene: 'detail', hasTrip: document.body.classList.contains('has-trip'), summary: summaryInfo, controls: controls.length, stable: true, width: innerWidth}; }
  fold.open = true;
  card.querySelector('.task-ask').click();
  await wait(function () { return document.querySelector('.agent-line-bot:not(.agent-wait)') && document.getElementById('agent-form').style.position !== 'absolute'; });
  check(document.body.dataset.tab === 'tasks', '没有进入对话页');
  check(Array.from(document.querySelectorAll('.agent-line-user')).some(function (item) { return item.textContent.includes(raw); }), '对话没有待办原话');
  check(document.querySelector('.agent-subject').textContent.includes(task.summary), '回复没有引用待办');
  check(document.documentElement.scrollWidth <= innerWidth + 1, '页面横向溢出');
  return {pass: true, scene: 'chat', original: raw, subject: task.summary, controls: controls.length, stable: true, width: innerWidth};
})()
