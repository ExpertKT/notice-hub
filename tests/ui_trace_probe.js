(async function () {
  function check(value, message) { if (!value) throw new Error(message); }
  // label 是等什么，超时要能一眼看出是哪一条没做到，别都报「等待对话超时」。
  function wait(check, label) { return new Promise(function (resolve, reject) {
    var until = Date.now() + 10000;
    function tick() { if (check()) resolve(); else if (Date.now() > until) reject(new Error('等了 10 秒还没做到：' + (typeof label === 'function' ? label() : (label || '对话没出来')))); else setTimeout(tick, 30); }
    tick();
  }); }
  var input = document.getElementById('agent-input');
  input.focus();
  await wait(function () { return document.getElementById('agent-form').style.position !== 'absolute'; }, '面板没有展开成不悬浮的形态');
  // 面板是随内容长高的：等它在连续 300ms 里都不再变再量。只等「两次采样相同」不够 ——
  // 生长是消息到达后异步触发的，连采两帧相同也可能只是还没开始长（390 亮色会量到 61px，
  // 稳定后是 296px）。300ms 覆盖产品里 120/160/240ms 那几档动效。
  function settle(read) { return new Promise(function (resolve) {
    var last = null, stableSince = Date.now(), tries = 0;
    function step() {
      var now = read();
      if (now !== last) { last = now; stableSince = Date.now(); }
      else if (Date.now() - stableSince >= 300) return resolve(now);
      if (++tries > 400) return resolve(now);
      requestAnimationFrame(step);
    }
    step();
  }); }
  for (var round = 0; round < 6; round++) {
    input.value = '把博雅居学生公寓服务中心、立功楼实验教学管理办公室、图文信息楼图书借阅与资料服务中心排顺路';
    document.getElementById('agent-form').requestSubmit();
    await wait(function () { return document.querySelectorAll('.agent-trace').length === round + 1; }, '第 ' + (round + 1) + ' 轮过程没有出现');
  }
  var log = document.getElementById('agent-log');
  await settle(function () { return Math.round(document.getElementById('agent-dock').getBoundingClientRect().height) + ':' + log.clientHeight; });
  var box = log.querySelector('.agent-trace:last-child');
  var rows = Array.from(box.querySelectorAll('li'));
  var form = document.getElementById('agent-form').getBoundingClientRect();
  check(rows.length === 3, '不是三步过程');
  check(rows.map(function (row) { return row.querySelector('b').textContent; }).join('/') === '读/想/调', '过程标签不全');
  check(box.querySelector('button').getAttribute('aria-expanded') === 'true', '过程没有默认展开');
  check(log.scrollHeight > log.clientHeight, '多轮对话没有形成滚动');
  // 这两条得等：面板是随消息到达异步长高的，长完产品自己会滚到底、把末步顶到输入栏上方。
  // 立刻量就是量在长高的半路上（6 次里会红 2 次）。等 10 秒还不成立才是真没做。
  await wait(function () {
    var f = document.getElementById('agent-form').getBoundingClientRect();
    return rows[2].getBoundingClientRect().bottom <= f.top + 1;
  }, '自动滚动没把最后一步停在输入栏上方');
  // 到底的容差是 2px 不是 1px：内容高度是小数（这台机器上 scrollHeight 1673.x），
  // 产品滚到 max 之后浏览器回读只剩 2px 差（实测 scrollTop=1286 可滚=1288），
  // 肉眼就是贴底。按 1px 判会随机红，红的那次是「差 2px 且 10 秒不动」，不是没滚。
  var BOTTOM_SLACK = 2;
  function atBottom() { return log.scrollHeight - log.clientHeight - log.scrollTop <= BOTTOM_SLACK; }
  // 滚到底这一条要连两次都贴底才算：内容还在长的时候量到「刚好贴底」是中间态。
  // 红的时候把 scrollTop / 可滚距离一起报出来，别再让人靠猜（10 秒超时那次只说了「没滚到底」）。
  var bottomTicks = 0;
  await wait(function () {
    if (atBottom()) { bottomTicks += 1; return bottomTicks >= 2; }
    bottomTicks = 0;
    return false;
  }, function () {
    return '新回复没有滚到底（scrollTop=' + Math.round(log.scrollTop)
      + ' 可滚=' + Math.round(log.scrollHeight - log.clientHeight)
      + ' clientHeight=' + log.clientHeight + ' scrollHeight=' + log.scrollHeight
      + ' 底距=' + Math.round(log.scrollHeight - log.clientHeight - log.scrollTop) + '）';
  });
  check(rows[2].getBoundingClientRect().bottom <= document.getElementById('agent-form').getBoundingClientRect().top + 1, '自动滚动后最后一步被输入栏遮挡');
  check(log.scrollHeight - log.clientHeight - log.scrollTop <= BOTTOM_SLACK, '新回复没有滚到底');
  check(document.documentElement.scrollWidth <= innerWidth + 1, '长站名导致页面横向溢出');
  rows.forEach(function (row) {
    check(row.scrollWidth <= row.clientWidth + 1, '过程文字横向溢出');
    // 必须 instant：page.html 的 html 上写了 scroll-behavior:smooth，默认会滑一段，
    // 紧接着量到的是动画中途的位置。必须 start 而不是 nearest/center：nearest 对
    // 「已经露出一半」的行根本不滚，center 会按 scroll-padding 把行抬出上沿。
    row.scrollIntoView({block: 'start', behavior: 'instant'});
    // form 的位置要现量：上面那六个来回里面板跟着内容长高过，开头那一次的 formTop 已经过期，
    // 拿它比会把「最后一行贴着输入栏」误判成被盖住。
    var formNow = document.getElementById('agent-form').getBoundingClientRect();
    var rect = row.getBoundingClientRect(), bounds = log.getBoundingClientRect();
    check(rect.top >= bounds.top - 1 && rect.bottom <= Math.min(bounds.bottom, formNow.top) + 1, '过程文字不能完整滚入记录区'
      + ' top=' + Math.round(rect.top) + ' bottom=' + Math.round(rect.bottom)
      + ' logTop=' + Math.round(bounds.top) + ' logBottom=' + Math.round(bounds.bottom)
      + ' formTop=' + Math.round(formNow.top) + ' rowH=' + Math.round(rect.height)
      + ' logCH=' + log.clientHeight + ' logSH=' + log.scrollHeight
      + ' scrollTop=' + Math.round(log.scrollTop) + ' scrollMax=' + Math.round(log.scrollHeight - log.clientHeight)
      + ' rowIdx=' + rows.indexOf(row));

    var hit = document.elementFromPoint(rect.left + rect.width / 2, rect.bottom - 2);
    check(hit && row.contains(hit), '过程被浮层遮住');
  });
  log.scrollTop = 0;
  check(log.scrollTop === 0, '不能滚回历史记录');
  log.scrollTop = log.scrollHeight;
  return {pass: true, steps: rows.length, expanded: true, scrollHeight: log.scrollHeight, clientHeight: log.clientHeight, lastBottom: rows[2].getBoundingClientRect().bottom, formTop: form.top, width: innerWidth};
})()
