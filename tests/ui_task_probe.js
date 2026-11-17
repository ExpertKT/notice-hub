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
  var raw = '明天下午到图文信息楼资料服务中心交班会材料，带上学生证和打印好的申请表。';
  var task = {id: 991, summary: '交班会材料 图文信息楼', source_text: raw, details: ['带上学生证', '申请表需要打印两份'], category: 'action', source: 'manual', urgent_override: true};
  if (!document.body.classList.contains('has-trip')) window.nhTrip.render({pois: [{id: 'library', name: '图文信息楼', x: 0, y: 0}], trips: [{stops: [{place_id: 'library', place: '图文信息楼', title: task.summary}], walk_minutes: 0}], counts: {stops: 1}});
  renderTimeline({today: [task], later: [Object.assign({}, task, {id: 992, summary: '另一件待办'})]});
  await new Promise(function (r) { setTimeout(r, 350); });
  var board = document.getElementById('timeline-board');
  var card = board.querySelector('#task-991');
  check(card && card.getBoundingClientRect().height > 0, '有行程时待办列表被隐藏');
  var fold = card.querySelector('.task-detail'), toggle = fold.querySelector('summary');
  var start = card.getBoundingClientRect(), scroll = board.scrollLeft;
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
  // 滚进视口可能挪了页面，收起后的比对基线与横向位置按此刻重新取，否则比的是两个滚动状态。
  start = card.getBoundingClientRect();
  scroll = board.scrollLeft;
  var summary = document.querySelector('section.summary'), summaryStyle = getComputedStyle(summary), summaryBox = summary.getBoundingClientRect();
  var summaryInfo = {display: summaryStyle.display, maxHeight: summaryStyle.maxHeight, height: summaryBox.height, clientHeight: summary.clientHeight, scrollHeight: summary.scrollHeight, overflowY: summaryStyle.overflowY};
  if (innerWidth <= 720) {
    check(summaryStyle.display !== 'none' && Math.abs(parseFloat(summaryStyle.maxHeight) - innerHeight * .4) <= 1, '手机待办概览没有按40vh限高');
    check(summary.scrollHeight > summary.clientHeight && summaryStyle.overflowY === 'auto', '展开详情后概览不能内部滚动');
    var beforeScroll = summary.scrollTop; summary.scrollTop = 20;
    check(summary.scrollTop > 0, '概览内部滚动未生效'); summary.scrollTop = beforeScroll;
    var askButton = card.querySelector('.task-ask'), askBox = askButton.getBoundingClientRect(), askHit = document.elementFromPoint(askBox.left + askBox.width / 2, askBox.top + askBox.height / 2);
    check(askBox.top >= 0 && askBox.bottom <= innerHeight && (askHit === askButton || askButton.contains(askHit)), '问智能体入口被遮住');
    var formHit = document.elementFromPoint(form.left + form.width / 2, form.top + form.height / 2);
    check(document.getElementById('agent-form').contains(formHit), '展开待办区遮住输入栏');
  }
  var controls = Array.from(card.querySelectorAll('button,summary,input,select')).filter(function (item) { return item.getBoundingClientRect().height > 0; });
  var small = controls.filter(function (item) { var b = item.getBoundingClientRect(); return b.width < 39.9 || b.height < 39.9; }).map(function (item) { return item.className + ':' + item.getBoundingClientRect().width + 'x' + item.getBoundingClientRect().height; });
  check(!small.length, '触控目标不足40px：' + small.join(','));
  toggle.click();
  await frames(2);
  check(!fold.open, '待办详情没有收起');
  var after = card.getBoundingClientRect();
  check(Math.abs(after.top - start.top) <= 1 && Math.abs(after.left - start.left) <= 1 && board.scrollLeft === scroll, '收起后列表位置跳动');
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
