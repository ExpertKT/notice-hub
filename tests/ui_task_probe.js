(async function () {
  function check(value, message) { if (!value) throw new Error(message); }
  async function wait(test) { var end = Date.now() + 10000; while (!test()) { if (Date.now() > end) throw new Error('等待待办对话超时'); await new Promise(function (r) { setTimeout(r, 30); }); } }
  var raw = '明天下午到图文信息楼资料服务中心交班会材料，带上学生证和打印好的申请表。';
  var task = {id: 991, summary: '交班会材料 图文信息楼', source_text: raw, details: ['带上学生证', '申请表需要打印两份'], category: 'action', source: 'manual', urgent_override: true};
  renderTimeline({today: [task], later: [Object.assign({}, task, {id: 992, summary: '另一件待办'})]});
  await new Promise(function (r) { setTimeout(r, 350); });
  var board = document.getElementById('timeline-board');
  var card = board.querySelector('#task-991');
  check(card && card.getBoundingClientRect().height > 0, '有行程时待办列表被隐藏');
  var fold = card.querySelector('.task-detail'), toggle = fold.querySelector('summary');
  var start = card.getBoundingClientRect(), scroll = board.scrollLeft;
  toggle.click();
  await new Promise(function (r) { requestAnimationFrame(r); });
  check(fold.open, '待办详情没有展开');
  var body = fold.querySelector('.task-detail-body');
  Array.from(card.querySelectorAll('details.raw')).forEach(function (item) { item.open = true; });
  var bounds = body.getBoundingClientRect();
  check(bounds.height <= 181 && body.scrollHeight > body.clientHeight, '长详情没有独立滚动');
  var form = document.getElementById('agent-form').getBoundingClientRect();
  check(bounds.bottom <= form.top + 1, '详情遮住输入栏');
  var controls = Array.from(card.querySelectorAll('button,summary,input,select')).filter(function (item) { return item.getBoundingClientRect().height > 0; });
  var small = controls.filter(function (item) { var b = item.getBoundingClientRect(); return b.width < 39.9 || b.height < 39.9; }).map(function (item) { return item.className + ':' + item.getBoundingClientRect().width + 'x' + item.getBoundingClientRect().height; });
  check(!small.length, '触控目标不足40px：' + small.join(','));
  toggle.click();
  await new Promise(function (r) { requestAnimationFrame(r); });
  check(!fold.open, '待办详情没有收起');
  var after = card.getBoundingClientRect();
  check(Math.abs(after.top - start.top) <= 1 && Math.abs(after.left - start.left) <= 1 && board.scrollLeft === scroll, '收起后列表位置跳动');
  if (window.TASK_SHOT === 'detail') { fold.open = true; return {pass: true, scene: 'detail', controls: controls.length, stable: true, width: innerWidth}; }
  fold.open = true;
  card.querySelector('.task-ask').click();
  await wait(function () { return document.querySelector('.agent-line-bot:not(.agent-wait)') && document.getElementById('agent-form').style.position !== 'absolute'; });
  check(document.body.dataset.tab === 'tasks', '没有进入对话页');
  check(Array.from(document.querySelectorAll('.agent-line-user')).some(function (item) { return item.textContent.includes(raw); }), '对话没有待办原话');
  check(document.querySelector('.agent-subject').textContent.includes(task.summary), '回复没有引用待办');
  check(document.documentElement.scrollWidth <= innerWidth + 1, '页面横向溢出');
  return {pass: true, scene: 'chat', original: raw, subject: task.summary, controls: controls.length, stable: true, width: innerWidth};
})()
