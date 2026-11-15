/* 今天几趟：左边校园地图，右边一趟一趟的行程。
   数字全来自 /api/agent/plan（服务端用同一套排程算出来的），页面不自己编。
   接口没开时如实说一句，不要留一块转圈的空面板。 */
(function () {
  'use strict';

  var PLAN = '/api/agent/plan';
  var ROUTINES = '/api/agent/routines';
  var SCHEDULE = '/api/agent/schedule';
  var state = { plan: null, busy: false };

  function $(id) { return document.getElementById(id); }
  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }
  function token(name) {
    return window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  // 等距投影：米 → 画面。西南角是原点，x 向东、y 向南。
  function iso(x, y) { return [(x - y) * 0.866, (x + y) * 0.5]; }

  // 每种楼的footprint和层高（米）。只让图看起来是立起来的，不是施工图。
  var BOX = {
    teach: [76, 44, 18], library: [70, 50, 20], office: [64, 36, 22],
    food: [64, 48, 10], shop: [40, 30, 8], parcel: [34, 26, 6],
    dorm: [58, 30, 15], health: [44, 30, 12], sport: [86, 56, 16],
    hall: [56, 40, 12], gate: [44, 10, 5]
  };
  var NAMED = ['ligonglou', 'boyaju', 'tuwen', 'library', 'admin',
    'canteen1', 'market', 'cainiao', 'dorm2', 'gym'];

  function drawStrip(plan, host, byId) {
    var trips = plan.trips || [];
    if (!trips.length) { host.innerHTML = '<p class="trip-strip-empty">今天还没有要出门的事</p>'; return; }
    var html = trips.map(function (trip, ti) {
      var stops = (trip.stops || []).map(function (stop) {
        return '<span class="trip-strip-stop"><b>' + esc(stop.arrive || '') + '</b>' + esc(stop.place || '') + '</span>';
      }).join('<span class="trip-strip-arrow">→</span>');
      return '<div class="trip-strip-row"><span class="trip-strip-tag" data-trip="' + (ti + 1) + '">第' + (ti + 1) + '趟</span>' +
        strips(stops) + '</div>';
    }).join('');
    host.innerHTML = '<div class="trip-strip">' + html + '</div>';
  }

  function strips(inner) { return '<div class="trip-strip-line">' + inner + '</div>'; }
  function drawMap(plan) {
    var host = $('trip-map');
    if (!host) return;
    var pois = plan.pois || [];
    if (!pois.length) { host.innerHTML = ''; return; }
    var byId = {};
    pois.forEach(function (p) { byId[p.id] = p; });

    var origin = plan.origin ? byId[plan.origin.id] : null;
    var stops = [];
    (plan.trips || []).forEach(function (trip, ti) {
      (trip.stops || []).forEach(function (stop, si) {
        var poi = byId[stop.place_id];
        if (poi) stops.push({ poi: poi, trip: ti, n: si + 1 });
      });
    });
    var liveTrip = {};
    stops.forEach(function (item) { liveTrip[item.poi.id] = item.trip; });

    var box = host.getBoundingClientRect();
    var W = Math.max(220, box.width);
    var Hh = Math.max(120, box.height);
    // 对话态把地图压成一条扁带，等距图塞不进去（横向 7:1 怎么缩都是小方块），
    // 换成横向的行程条：还是这一趟的时间顺序，但按一条线读。
    if (box.height < 200) { drawStrip(plan, host, byId); return; }
    // 又窄又高（手机竖屏）或又宽又扁（对话态顶部那条）都按高度铺满，横向裁两头。
    var ratio = W / Hh;
    var narrow = ratio < 2.0 || ratio > 4.0;

    // 取景：有行程就贴着这一趟画（否则整片校园缩成一堆小方块），没有就画全校。
    var minX, maxX, minY, maxY;
    if (stops.length) {
      var pts = stops.map(function (item) { return item.poi; });
      if (origin) pts.push(origin);
      var pad = narrow ? 120 : 160;
      var xs = pts.map(function (p) { return p.x; });
      var ys = pts.map(function (p) { return p.y; });
      minX = Math.min.apply(null, xs) - pad; maxX = Math.max.apply(null, xs) + pad;
      minY = Math.min.apply(null, ys) - pad; maxY = Math.max.apply(null, ys) + pad;
    } else {
      var allX = pois.map(function (p) { return p.x; });
      var allY = pois.map(function (p) { return p.y; });
      minX = Math.min.apply(null, allX) - 90; maxX = Math.max.apply(null, allX) + 90;
      minY = Math.min.apply(null, allY) - 90; maxY = Math.max.apply(null, allY) + 90;
    }

    var a = iso(minX, minY);
    var b = iso(maxX, maxY);
    var minU = Math.min(a[0], b[0]); var maxU = Math.max(a[0], b[0]);
    var minV = Math.min(a[1], b[1]); var maxV = Math.max(a[1], b[1]);
    // 竖屏按高度铺满；对话态那条扁带按宽度铺满（看的是横向有哪几站，纵向裁掉没关系）。
    var S;
    if (ratio < 2.0) S = (Hh / (maxV - minV)) * 0.9;
    else if (ratio > 4.0) S = (W / (maxU - minU)) * 0.9;
    else S = Math.min(W / (maxU - minU), Hh / (maxV - minV)) * 0.9;
    var ox = (W - (maxU - minU) * S) / 2 - minU * S;
    var oy = (Hh - (maxV - minV) * S) / 2 - minV * S;
    function SX(x, y) { return ox + iso(x, y)[0] * S; }
    function SY(x, y) { return oy + iso(x, y)[1] * S; }

    var out = [];
    out.push('<svg viewBox="0 0 ' + W.toFixed(0) + ' ' + Hh.toFixed(0) + '" preserveAspectRatio="xMidYMid meet" aria-hidden="true">');
    out.push('<rect x="0" y="0" width="' + W.toFixed(0) + '" height="' + Hh.toFixed(0) + '" fill="' + token('--map-ground') + '"/>');

    // 楼的顶面和两个侧面；按 x+y 从小到大画，前面的自然盖住后面的。
    pois.slice().sort(function (p, q) { return (p.x + p.y) - (q.x + q.y); }).forEach(function (p) {
      var size = BOX[p.kind] || [52, 34, 12];
      var hw = size[0] / 2; var hd = size[1] / 2; var hh = size[2] * 1.6;
      var live = liveTrip[p.id] !== undefined;
      var fillTop = token(live ? '--map-top-live' : '--map-top');
      var fillA = token(live ? '--map-a-live' : '--map-a');
      var fillB = token(live ? '--map-b-live' : '--map-b');
      var stroke = token('--map-stroke');
      function T(x, y) { return SX(x, y).toFixed(1) + ',' + (SY(x, y) - hh * S).toFixed(1); }
      function G(x, y) { return SX(x, y).toFixed(1) + ',' + SY(x, y).toFixed(1); }
      out.push('<g stroke="' + stroke + '" stroke-width="1">');
      out.push('<polygon points="' + [T(p.x - hw, p.y - hd), T(p.x + hw, p.y - hd), T(p.x + hw, p.y + hd), T(p.x - hw, p.y + hd)].join(' ') + '" fill="' + fillTop + '"/>');
      out.push('<polygon points="' + [T(p.x - hw, p.y + hd), T(p.x + hw, p.y + hd), G(p.x + hw, p.y + hd), G(p.x - hw, p.y + hd)].join(' ') + '" fill="' + fillB + '"/>');
      out.push('<polygon points="' + [T(p.x + hw, p.y - hd), T(p.x + hw, p.y + hd), G(p.x + hw, p.y + hd), G(p.x + hw, p.y - hd)].join(' ') + '" fill="' + fillA + '"/>');
      out.push('</g>');
    });

    // 路线画在楼之上：这是示意图，被楼挡住就看不出「顺路」这件事。
    (plan.trips || []).forEach(function (trip, ti) {
      var path = [];
      var firstPlace = trip.stops && trip.stops.length ? byId[trip.stops[0].place_id] : null;
      if (origin && firstPlace && origin.id !== firstPlace.id) path.push(origin);
      (trip.stops || []).forEach(function (stop) {
        var poi = byId[stop.place_id];
        if (poi) path.push(poi);
      });
      if (path.length < 2) return;
      var colour = token(ti === 0 ? '--map-road1' : '--map-road2');
      var d = path.map(function (p, i) {
        return (i ? 'L' : 'M') + SX(p.x, p.y).toFixed(1) + ' ' + SY(p.x, p.y).toFixed(1);
      }).join(' ');
      out.push('<path d="' + d + '" fill="none" stroke="' + colour + '" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" opacity=".9"/>');
    });

    // 楼名：在行程里的必画，其余只画主要几栋；后画的避开先画的，
    // 免得食堂、超市、驿站挤在一起糊成一团（画在最上面的那栋优先）。
    var taken = [];
    function fits(x, y, name) {
      var half = (name.length * 11) / 2 + 2;
      var box = { x0: x - half, x1: x + half, y0: y - 12, y1: y + 2 };
      for (var i = 0; i < taken.length; i += 1) {
        var other = taken[i];
        if (box.x0 < other.x1 && other.x0 < box.x1 && box.y0 < other.y1 && other.y0 < box.y1) return false;
      }
      taken.push(box);
      return true;
    }
    var ordered = pois.slice().sort(function (a, b) {
      var la = liveTrip[a.id] !== undefined ? 0 : 1;
      var lb = liveTrip[b.id] !== undefined ? 0 : 1;
      return la - lb || (a.x + a.y) - (b.x + b.y);
    });
    ordered.forEach(function (p) {
      var live = liveTrip[p.id] !== undefined;
      // 窄屏上只留行程里那几栋：楼名一多就压在楼体和编号上，不如把位置留给路线。
      if (!live && (narrow || NAMED.indexOf(p.id) < 0)) return;
      var size = BOX[p.kind] || [52, 34, 12];
      var x = SX(p.x, p.y + size[1] / 2);
      var y = SY(p.x, p.y + size[1] / 2) + 13;
      if (!fits(x, y, p.name)) return;
      out.push('<text x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" text-anchor="middle" font-size="11" fill="' + token(live ? '--map-label' : '--map-label-dim') + '">' + esc(p.name) + '</text>');
    });

    // 站点编号抬到楼顶之上，并且画在楼名之后——编号比楼名重要，不能被名字盖住。
    stops.forEach(function (item) {
      var p = item.poi;
      var size = BOX[p.kind] || [52, 34, 12];
      var cy = SY(p.x, p.y) - size[2] * 1.6 * S - 11;
      var colour = token(item.trip === 0 ? '--map-road1' : '--map-road2');
      out.push('<circle cx="' + SX(p.x, p.y).toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="9" fill="' + colour + '"/>');
      out.push('<text x="' + SX(p.x, p.y).toFixed(1) + '" y="' + (cy + 3.5).toFixed(1) + '" text-anchor="middle" font-size="11" fill="' + token('--paper-raise') + '">' + esc(item.n) + '</text>');
    });

    out.push('</svg>');
    host.innerHTML = out.join('');
    var meta = $('trip-map-meta');
    if (meta) meta.textContent = plan.origin ? '起点 ' + plan.origin.name : '';
    var legend = $('trip-legend');
    if (legend) {
      if ((plan.trips || []).length > 1) {
        legend.hidden = false;
        legend.innerHTML = plan.trips.map(function (trip, ti) {
          return '<span class="trip-legend-item"><i style="background:' + token(ti === 0 ? '--map-road1' : '--map-road2') + '"></i>第' + (ti + 1) + '趟 · ' + esc(trip.zone || '') + '</span>';
        }).join('');
      } else {
        legend.hidden = true;
        legend.innerHTML = '';
      }
    }
  }

  function rowsOf(trip) {
    return (trip.stops || []).map(function (stop) {
      var where = [stop.place, stop.room].filter(Boolean).join(' · ');
      return '<li class="trip-row"><span class="trip-time">' + esc(stop.arrive) + '</span>' +
        '<span class="trip-what"><b>' + esc(stop.title) + '</b>' +
        '<em>' + esc(where) + (stop.minutes ? ' · 留 ' + stop.minutes + ' 分钟' : '') + '</em>' +
        (stop.why ? '<i>' + esc(stop.why) + '</i>' : '') + '</span></li>';
    }).join('');
  }

  function renderTrips(plan) {
    var list = $('trip-list');
    if (!list) return;
    var trips = plan.trips || [];
    list.innerHTML = trips.map(function (trip, ti) {
      return '<article class="trip-card"><div class="trip-card-head">' +
        '<h3>第' + (ti + 1) + '趟 · ' + esc(trip.zone || '') + '</h3>' +
        '<span class="trip-card-meta">' + esc(trip.start_at) + '–' + esc(trip.end_at) +
        ' · ' + (trip.stops || []).length + ' 件 · 走路 ' + trip.walk_minutes + ' 分钟</span></div>' +
        '<ul class="trip-rows">' + rowsOf(trip) + '</ul></article>';
    }).join('') || '<p class="trip-empty">今天没有要出门的事。新的事进来会说一声要不要重排。</p>';

    var meta = $('trip-meta');
    var counts = plan.counts || {};
    if (meta) {
      var walk = trips.reduce(function (sum, trip) { return sum + (trip.walk_minutes || 0); }, 0);
      meta.textContent = counts.stops ? counts.stops + ' 件 · ' + trips.length + ' 趟 · 走路 ' + walk + ' 分钟' : '空闲';
    }

    var rest = $('trip-rest');
    if (rest) {
      var blocks = [];
      if ((plan.warnings || []).length) {
        blocks.push('<div class="trip-warn">' + plan.warnings.map(function (line) { return '<p>' + esc(line) + '</p>'; }).join('') + '</div>');
      }
      if ((plan.unscheduled || []).length) {
        blocks.push('<p class="trip-rest-head">没说地点，先放着</p><ul class="trip-rest-list">' +
          plan.unscheduled.map(function (item) { return '<li>' + esc(item.title) + '<em>' + esc(item.reason || '') + '</em></li>'; }).join('') + '</ul>');
      }
      if ((plan.overflow || []).length) {
        blocks.push('<p class="trip-rest-head">今天塞不下</p><ul class="trip-rest-list">' +
          plan.overflow.map(function (item) { return '<li>' + esc(item.title) + '<em>' + esc(item.reason || '') + '</em></li>'; }).join('') + '</ul>');
      }
      if ((plan.checks || []).length) {
        blocks.push('<ul class="trip-checks">' + plan.checks.map(function (line) { return '<li>' + esc(line) + '</li>'; }).join('') + '</ul>');
      }
      rest.innerHTML = blocks.join('');
      rest.hidden = !blocks.length;
    }
  }

  function render(plan) {
    state.plan = plan;
    // 手机上一屏放不下「概览 + 地图 + 行程」，有行程时就不再重复列一遍待办。
    document.body.classList.add('has-trip');
    drawMap(plan);
    renderTrips(plan);
  }

  function fail(message) {
    document.body.classList.remove('has-trip');
    var list = $('trip-list');
    if (list) list.innerHTML = '<p class="trip-empty">' + esc(message) + '</p>';
    var host = $('trip-map');
    if (host) host.innerHTML = '';
  }

  function load() {
    if (!document.getElementById('trip-stage')) return Promise.resolve();
    return api(PLAN, { timeoutMs: 20000 }).then(function (body) {
      if (body && body.ok && body.plan) render(body.plan);
      else fail('行程接口没返回数据，' + (body && body.error ? body.error : '稍后再试'));
    }, function (error) {
      var message = error && error.message ? error.message : '连不上本机服务';
      if (/404|HTTP 404/.test(message)) {
        message = '这台机器没开行程接口（演示版启动时会打开）';
      }
      fail('读行程失败：' + message);
    });
  }

  function renderRoutineChips(items) {
    var box = $('trip-routine-chips');
    if (!box) return;
    box.innerHTML = items.length
      ? items.map(function (item) {
        return '<span class="trip-chip">' + esc(item.title) + '<button type="button" data-routine-remove="' + esc(item.id) + '" aria-label="删掉 ' + esc(item.title) + '">×</button></span>';
      }).join('')
      : '<span class="trip-chip-empty">还没有常做的事。加一条，以后排行程时它按普通一站算。</span>';
  }

  function loadRoutines() {
    return api(ROUTINES, { timeoutMs: 15000 }).then(function (body) {
      renderRoutineChips((body && body.routines) || []);
    }, function () {
      renderRoutineChips([]);
    });
  }

  function postRoutine(payload) {
    return api(ROUTINES, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      timeoutMs: 20000
    }).then(function (body) {
      if (body && body.plan) render(body.plan);
      renderRoutineChips((body && body.routines) || []);
      return body;
    });
  }

  function openSchedule() {
    var dialog = $('trip-schedule-dialog');
    if (!dialog) return;
    var msg = $('trip-schedule-msg');
    if (msg) msg.textContent = '';
    api(SCHEDULE, { timeoutMs: 15000 }).then(function (body) {
      var text = $('trip-schedule-text');
      var lessons = (body && body.lessons) || [];
      if (text && !text.value) {
        text.value = lessons.map(function (item) {
          var days = ['', '一', '二', '三', '四', '五', '六', '日'];
          return '周' + (days[item.weekday] || '一') + ' ' + item.start + '-' + item.end + ' ' +
            (item.title || '') + ' ' + (item.place || '');
        }).join('\n');
      }
    }, function () {});
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', 'open');
  }

  function saveSchedule() {
    var text = $('trip-schedule-text');
    var msg = $('trip-schedule-msg');
    if (!text) return;
    api(SCHEDULE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text.value }),
      timeoutMs: 20000
    }).then(function (body) {
      if (body && body.plan) render(body.plan);
      var rejected = (body && body.rejected) || [];
      if (msg) {
        msg.textContent = rejected.length
          ? '存了 ' + body.lessons + ' 节课；这几行没认出来：' + rejected.join(' / ')
          : '存了 ' + body.lessons + ' 节课。';
      }
    }, function (error) {
      if (msg) msg.textContent = error && error.message ? error.message : '没存上';
    });
  }

  function boot() {
    if (!document.getElementById('trip-stage')) return;
    // 首屏布局没定下来时量到的地图高度是假的，排完再画一次。
    load().then(function () {
      window.requestAnimationFrame(function () { if (state.plan) drawMap(state.plan); });
      window.setTimeout(function () { if (state.plan) drawMap(state.plan); }, 260);
    });

    var open = $('trip-routine-open');
    if (open) {
      open.addEventListener('click', function () {
        var box = $('trip-routine-box');
        var next = open.getAttribute('aria-expanded') !== 'true';
        open.setAttribute('aria-expanded', next ? 'true' : 'false');
        if (box) box.hidden = !next;
        if (next) loadRoutines();
      });
    }
    var form = $('trip-routine-form');
    if (form) {
      form.addEventListener('submit', function (event) {
        event.preventDefault();
        var input = $('trip-routine-input');
        var value = input ? input.value.trim() : '';
        if (!value) return;
        postRoutine({ action: 'add', title: value }).then(function () {
          if (input) input.value = '';
        }, function (error) {
          var box = $('trip-routine-chips');
          if (box) box.innerHTML = '<span class="trip-chip-empty">' + esc(error && error.message ? error.message : '没加上') + '</span>';
        });
      });
    }
    var chips = $('trip-routine-chips');
    if (chips) {
      chips.addEventListener('click', function (event) {
        var button = event.target.closest('[data-routine-remove]');
        if (!button) return;
        postRoutine({ action: 'remove', id: button.getAttribute('data-routine-remove') });
      });
    }
    var scheduleOpen = $('trip-schedule-open');
    if (scheduleOpen) scheduleOpen.addEventListener('click', openSchedule);
    var scheduleSave = $('trip-schedule-save');
    if (scheduleSave) scheduleSave.addEventListener('click', saveSchedule);

    // 换主题、窗口大小变了都要重画：SVG 里的颜色和取景是算出来的，不是 CSS 撑的。
    window.addEventListener('resize', function () {
      if (state.plan) drawMap(state.plan);
    });
    if (window.matchMedia) {
      var query = window.matchMedia('(prefers-color-scheme: dark)');
      if (query.addEventListener) {
        query.addEventListener('change', function () { if (state.plan) drawMap(state.plan); });
      }
    }
    var root = document.documentElement;
    if (window.MutationObserver) {
      new MutationObserver(function () { if (state.plan) drawMap(state.plan); })
        .observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    }
    // 唤醒对话 / 收起对话时地图要换取景，所以盯一下 dock 的状态。
    var dock = document.getElementById('agent-dock');
    if (dock && window.MutationObserver) {
      new MutationObserver(function () {
        window.setTimeout(function () { if (state.plan) drawMap(state.plan); }, 60);
      }).observe(dock, { attributes: true, attributeFilter: ['data-state'] });
    }
    // 待办改了行程就得重排：loadTasks 之后会派发这个事件（见 page.html）。
    document.addEventListener('nh:tasks-changed', function () { load(); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  window.nhTrip = { reload: load, render: render };
})();





