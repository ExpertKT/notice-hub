(async function () {
  function check(value, message) { if (!value) throw new Error(message); }
  function wait(check, timeout) { return new Promise(function (resolve, reject) {
    var until = Date.now() + timeout;
    function tick() { if (check()) resolve(); else if (Date.now() > until) reject(new Error('等待路线超时')); else setTimeout(tick, 50); }
    tick();
  }); }
  ['onboarding', 'journey', 'platform-setup-wizard', 'update-guide', 'announce-modal', 'announce-panel'].forEach(function (id) {
    var node = document.getElementById(id); if (node) { node.hidden = true; node.style.display = 'none'; }
  });
  await wait(function () { return window.nhTrip && window.nhMap3d && document.querySelector('#trip-map canvas, #trip-map svg'); }, 15000);
  var baseline = await api('/api/agent/plan');
  var drawn, reply;
  var draw = window.nhMap3d.draw;
  window.nhMap3d.draw = function (host, plan) { drawn = plan; return draw.call(this, host, plan); };
  var request = window.fetch;
  window.fetch = async function () {
    var response = await request.apply(this, arguments);
    if (String(arguments[0]).indexOf('/api/agent/chat') >= 0) reply = await response.clone().json();
    return response;
  };
  try {
    var input = document.getElementById('agent-input');
    input.focus();
    input.value = '已有两件事在博雅居、立功楼，新第三件在图文信息楼，顺路把第三件办了';
    document.getElementById('agent-form').requestSubmit();
    await wait(function () { return reply && document.getElementById('trip-route') && drawn; }, 30000);
    var step = reply.trace.filter(function (step) { return step.tool === 'plan_route' && step.ok; }).pop();
    check(step && step.result, '缺少真实工具返回');
    var route = step.result, panel = document.getElementById('trip-route');
    check(JSON.stringify(drawn.trips[0].stops.map(function (stop) { return stop.place_id; })) === JSON.stringify(route.poi_ids), '地图站序与工具不同');
    check(JSON.stringify(drawn.trips[0].stops.map(function (stop) { return stop.place; })) === JSON.stringify(route.order), '地图站名与工具不同');
    check(drawn.origin === null, '路线不应接入今天排法的起点');
    check(panel.textContent.includes(route.distance_m + ' 米 · 步行 ' + route.walk_minutes + ' 分钟'), '总距离或步行时间被改变');
    route.legs.forEach(function (leg) { check(panel.textContent.includes(leg.start + ' → ' + leg.end + '：' + leg.distance_m + ' 米 / ' + leg.walk_minutes + ' 分钟'), '分段数值被改变'); });
    check(document.getElementById('trip-map').getBoundingClientRect().height >= 200, '路线被压成行程条');
    check(document.querySelector('#trip-map canvas, #trip-map svg'), '没有绘制地图');
    check(document.querySelector('.agent-trace-head').getAttribute('aria-expanded') === 'true', '过程被折叠');
    check(document.documentElement.scrollWidth <= innerWidth + 1, '页面横向溢出');
    var failure = null;
    try { await window.nhTrip.renderRoute(Object.assign({}, route, { poi_ids: route.poi_ids.map(function () { return 'missing'; }) })); }
    catch (error) { failure = error; }
    check(failure && /不在这张地图/.test(failure.message), '未知地点未拒绝');
    check(JSON.stringify(drawn.trips[0].stops.map(function (stop) { return stop.place_id; })) === JSON.stringify(route.poi_ids), '失败覆盖了正确路线');
    if (new URLSearchParams(location.search).has('restore')) {
      panel.querySelector('button').click();
      await wait(function () { return !document.body.classList.contains('has-route'); }, 3000);
      check(panel.hidden, '返回排法后路线摘要未隐藏');
      check(document.getElementById('trip-meta').textContent.includes(baseline.plan.counts.stops + ' 件'), '返回排法未恢复原件数');
      check(document.getElementById('trip-map').textContent.includes(baseline.plan.trips[0].stops[0].place), '返回排法未恢复原站点');
    }
    await wait(function () { return document.getElementById('agent-form').style.position !== 'absolute'; }, 3000);
    document.getElementById('agent-log').scrollTop = document.getElementById('agent-log').scrollHeight;
    var last = document.querySelector('.agent-trace:last-child li:last-child');
    check(last && last.getBoundingClientRect().bottom <= document.getElementById('agent-form').getBoundingClientRect().top + 1, '最后一步被输入栏遮挡');
    return { pass: true, route: route, stops: drawn.trips[0].stops.map(function (stop) { return stop.place_id; }), width: innerWidth };
  } finally { window.fetch = request; window.nhMap3d.draw = draw; }
})()
