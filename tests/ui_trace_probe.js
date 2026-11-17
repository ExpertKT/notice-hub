(async function () {
  function check(value, message) { if (!value) throw new Error(message); }
  function wait(check) { return new Promise(function (resolve, reject) {
    var until = Date.now() + 10000;
    function tick() { if (check()) resolve(); else if (Date.now() > until) reject(new Error('等待对话超时')); else setTimeout(tick, 30); }
    tick();
  }); }
  var input = document.getElementById('agent-input');
  input.focus();
  await wait(function () { return document.getElementById('agent-form').style.position !== 'absolute'; });
  for (var round = 0; round < 6; round++) {
    input.value = '把博雅居学生公寓服务中心、立功楼实验教学管理办公室、图文信息楼图书借阅与资料服务中心排顺路';
    document.getElementById('agent-form').requestSubmit();
    await wait(function () { return document.querySelectorAll('.agent-trace').length === round + 1; });
  }
  var log = document.getElementById('agent-log');
  var box = log.querySelector('.agent-trace:last-child');
  var rows = Array.from(box.querySelectorAll('li'));
  var form = document.getElementById('agent-form').getBoundingClientRect();
  check(rows.length === 3, '不是三步过程');
  check(rows.map(function (row) { return row.querySelector('b').textContent; }).join('/') === '读/想/调', '过程标签不全');
  check(box.querySelector('button').getAttribute('aria-expanded') === 'true', '过程没有默认展开');
  check(log.scrollHeight > log.clientHeight, '多轮对话没有形成滚动');
  check(rows[2].getBoundingClientRect().bottom <= form.top + 1, '自动滚动后最后一步被输入栏遮挡');
  check(log.scrollHeight - log.clientHeight - log.scrollTop <= 1, '新回复没有滚到底');
  check(document.documentElement.scrollWidth <= innerWidth + 1, '长站名导致页面横向溢出');
  rows.forEach(function (row) {
    check(row.scrollWidth <= row.clientWidth + 1, '过程文字横向溢出');
    row.scrollIntoView({block: 'nearest'});
    var rect = row.getBoundingClientRect(), bounds = log.getBoundingClientRect();
    check(rect.top >= bounds.top - 1 && rect.bottom <= Math.min(bounds.bottom, form.top) + 1, '过程文字不能完整滚入记录区');
    var hit = document.elementFromPoint(rect.left + rect.width / 2, rect.bottom - 2);
    check(hit && row.contains(hit), '过程被浮层遮住');
  });
  log.scrollTop = 0;
  check(log.scrollTop === 0, '不能滚回历史记录');
  log.scrollTop = log.scrollHeight;
  return {pass: true, steps: rows.length, expanded: true, scrollHeight: log.scrollHeight, clientHeight: log.clientHeight, lastBottom: rows[2].getBoundingClientRect().bottom, formTop: form.top, width: innerWidth};
})()
