/* 今天几趟：左边校园地图，右边一趟一趟的行程。
   数字全来自 /api/agent/plan（服务端用同一套排程算出来的），页面不自己编。
   接口没开时如实说一句，不要留一块转圈的空面板。 */
(function () {
  'use strict';

  var PLAN = '/api/agent/plan';
  var ROUTINES = '/api/agent/routines';
  var SCHEDULE = '/api/agent/schedule';
  var state = { plan: null, route: null, busy: false };

  function $(id) { return document.getElementById(id); }
  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }
  function token(name) {
    return window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  // 拖动转的是这个角（弧度），0 就是默认这张 45° 的等距图。转完存本地，刷新还在。
  var view = { angle: 0 };
  try {
    var savedAngle = parseFloat(localStorage.getItem('nh_map_angle'));
    if (isFinite(savedAngle)) view.angle = savedAngle;
  } catch (e) { view.angle = 0; }

  // 每种楼的footprint和层高（米）。只让图看起来是立起来的，不是施工图。
  var BOX = {
    teach: [76, 44, 18], library: [70, 50, 20], office: [64, 36, 22],
    food: [64, 48, 10], shop: [40, 30, 8], parcel: [34, 26, 6],
    dorm: [58, 30, 15], health: [44, 30, 12], sport: [86, 56, 16],
    hall: [56, 40, 12], gate: [44, 10, 5]
  };
  var NAMED = ['ligonglou', 'boyaju', 'tuwen', 'library', 'admin',
    'canteen1', 'market', 'cainiao', 'dorm2', 'gym'];
  // 3D 那边（js/map3d.js）也用这张表：尺寸和「哪几栋要写名字」只留一份。
  window.nhMapModel = { size: BOX, named: NAMED };

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
    if (state.route) plan = { pois: plan.pois, origin: null, trips: [{ stops: state.route.poi_ids.map(function (id, index) {
      return { place_id: id, place: state.route.order[index] };
    }) }] };
    var host = $('trip-map');
    if (!host) return;
    // 在线底图档自己画（js/map3d.js 这时候不该再往隐藏的画布里画），只重画我们这层。
    if (mode.id !== 'sketch') { drawOverlay(); return; }
    var pois = plan.pois || [];
    if (!pois.length) { host.innerHTML = ''; return; }
    var byId = {};
    pois.forEach(function (p) { byId[p.id] = p; });

    var origin = plan.origin ? byId[plan.origin.id] : null;
    var stops = [];
    (plan.trips || []).forEach(function (trip, ti) {
      (trip.stops || []).forEach(function (stop, si) {
        var poi = byId[stop.place_id];
        if (poi) stops.push({ poi: poi, trip: ti, n: stops.length + 1 });
      });
    });
    var liveTrip = {};
    stops.forEach(function (item) { liveTrip[item.poi.id] = item.trip; });

    var box = host.getBoundingClientRect();
    // 面板藏着（切到别的页签）或正在收合时量到的是 0：拿这个尺寸画出来就是一坨糊的取景，
    // 而且画完再没人叫它（用户报的「3D 地图有时候不显示」）。等 ResizeObserver 叫醒，见 boot。
    if (box.width < 40 || box.height < 40) return;
    var W = Math.max(220, box.width);
    var Hh = Math.max(120, box.height);
    // 对话态把地图压成一条扁带，等距图塞不进去（横向 7:1 怎么缩都是小方块），
    // 换成横向的行程条：还是这一趟的时间顺序，但按一条线读。
    if (box.height < 200) {
      // 扁带上不画 3D：先把上一块的画布和 GL 资源收掉，别留着一个不显示的渲染器占显存。
      if (window.nhMap3d) window.nhMap3d.dispose(host);
      drawStrip(plan, host, byId);
      return;
    }
    // 有 WebGL 就把这张图交给 3D（js/map3d.js），楼是真体块、带光和影子。
    // 没有就往下走这套 SVG：同一份数据，只是画法不同，不能因为显卡不行就没有地图。
    if (window.nhMap3d && window.nhMap3d.draw(host, plan)) {
      drawMeta(plan);
      return;
    }
    // 又窄又高（手机竖屏）或又宽又扁（对话态顶部那条）都按高度铺满，横向裁两头。
    var ratio = W / Hh;
    var narrow = ratio < 2.0 || ratio > 4.0;

    // 视角：楼的四角、路线、编号都跟着这个角转，取景也照转过的位置重算，
    // 不然转完楼会转出画框。
    var cosA = Math.cos(view.angle);
    var sinA = Math.sin(view.angle);
    function flat(x, y) {
      var rx = x * cosA - y * sinA;
      var ry = x * sinA + y * cosA;
      return [(rx - ry) * 0.866, (rx + ry) * 0.5];
    }
    function cornersOf(p) {
      var size = BOX[p.kind] || [52, 34, 12];
      var hw = size[0] / 2; var hd = size[1] / 2;
      return [[p.x - hw, p.y - hd], [p.x + hw, p.y - hd], [p.x + hw, p.y + hd], [p.x - hw, p.y + hd]];
    }

    // 取景：有行程就贴着这一趟画（否则整片校园缩成一堆小方块），没有就画全校。
    var minU = Infinity; var maxU = -Infinity; var minV = Infinity; var maxV = -Infinity;
    var framed = [];
    if (stops.length) {
      stops.forEach(function (item) { framed.push(item.poi); });
      if (origin) framed.push(origin);
    } else {
      framed = pois;
    }
    framed.forEach(function (p) {
      cornersOf(p).forEach(function (corner) {
        var f = flat(corner[0], corner[1]);
        if (f[0] < minU) minU = f[0]; if (f[0] > maxU) maxU = f[0];
        if (f[1] < minV) minV = f[1]; if (f[1] > maxV) maxV = f[1];
      });
    });
    // 边距按像素留：楼名在楼底下一行、编号在楼顶上，各留 40/34 就够；按米留会把图缩得太小。
    var spanU = maxU - minU; var spanV = maxV - minV;
    var roomX = 80; var roomY = 68;
    // 竖屏按高度铺满；对话态那条扁带按宽度铺满（看的是横向有哪几站，纵向裁掉没关系）。
    // 再留 8%：这一趟之外的楼也会画出来，贴到画框边上就被切一半，看着像张破图。
    var S;
    if (ratio < 2.0) S = ((Hh - roomY) / spanV);
    else if (ratio > 4.0) S = ((W - roomX) / spanU);
    else S = Math.min((W - roomX) / spanU, (Hh - roomY) / spanV);
    S *= 0.92;
    var ox = (W - spanU * S) / 2 - minU * S;
    var oy = (Hh - spanV * S) / 2 - minV * S;
    function SX(x, y) { return ox + flat(x, y)[0] * S; }
    function SY(x, y) { return oy + flat(x, y)[1] * S; }

    var out = [];
    out.push('<svg viewBox="0 0 ' + W.toFixed(0) + ' ' + Hh.toFixed(0) + '" preserveAspectRatio="xMidYMid meet" aria-hidden="true">');
    out.push('<rect x="0" y="0" width="' + W.toFixed(0) + '" height="' + Hh.toFixed(0) + '" fill="' + token('--map-ground') + '"/>');

    // 楼的顶面和两个侧面；按 x+y 从小到大画，前面的自然盖住后面的。
    pois.slice().sort(function (p, q) { return flat(p.x, p.y)[1] - flat(q.x, q.y)[1]; }).forEach(function (p) {
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
    function overlaps(box) {
      for (var i = 0; i < taken.length; i += 1) {
        var other = taken[i];
        if (box.x0 < other.x1 && other.x0 < box.x1 && box.y0 < other.y1 && other.y0 < box.y1) return true;
      }
      return false;
    }
    // 编号比楼名重：第几站是读顺序的唯一线索，先把位置占住，楼名绕开。
    stops.forEach(function (item) {
      var p = item.poi;
      var size = BOX[p.kind] || [52, 34, 12];
      var cy = SY(p.x, p.y) - size[2] * 1.6 * S - 11;
      taken.push({ x0: SX(p.x, p.y) - 11, x1: SX(p.x, p.y) + 11, y0: cy - 11, y1: cy + 11 });
    });
    // 楼名挪回画框里再判重叠：窄屏上最靠边的楼名会被裁成半个字，读不出是哪栋。
    function labelFits(x, y, name) {
      var half = (name.length * 11) / 2 + 2;
      if (W < 2 * half + 4) return null;
      x = Math.min(Math.max(x, half + 2), W - half - 2);
      var box = { x0: x - half, x1: x + half, y0: y - 12, y1: y + 2 };
      if (overlaps(box)) return null;
      taken.push(box);
      return x;
    }
    var ordered = pois.slice().sort(function (a, b) {
      var la = liveTrip[a.id] !== undefined ? 0 : 1;
      var lb = liveTrip[b.id] !== undefined ? 0 : 1;
      return la - lb || (a.x + a.y) - (b.x + b.y);
    });
    var labeled = {};
    ordered.forEach(function (p) {
      var live = liveTrip[p.id] !== undefined;
      // 窄屏上只留行程里那几栋：楼名一多就压在楼体和编号上，不如把位置留给路线。
      if (!live && (narrow || NAMED.indexOf(p.id) < 0)) return;
      var size = BOX[p.kind] || [52, 34, 12];
      var x0 = SX(p.x, p.y + size[1] / 2);
      var y0 = SY(p.x, p.y + size[1] / 2) + 13;
      // 这一趟里的楼名不能因为挤就让掉：楼下再错开一行，最多两行。
      var shifts = live ? [0, 13, -13, 26] : [0];
      var placedX = null; var placedY = 0;
      for (var si = 0; si < shifts.length && placedX === null; si += 1) {
        var tryX = labelFits(x0, y0 + shifts[si], p.name);
        if (tryX !== null) { placedX = tryX; placedY = y0 + shifts[si]; }
      }
      if (placedX === null) return;
      labeled[p.id] = true;
      var x = placedX; var y = placedY;
      // 路线从楼名底下穿过去时字被划了一道：单给行程里的楼名垫一块底色，路线让到字后面。
      if (live) {
        var tw = p.name.length * 11;
        out.push('<rect x="' + (x - tw / 2 - 2).toFixed(1) + '" y="' + (y - 10.5).toFixed(1) + '" width="' + (tw + 4).toFixed(1) + '" height="14" rx="3" fill="' + token('--map-ground') + '" opacity=".82"/>');
      }
      out.push('<text x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" text-anchor="middle" font-size="11" fill="' + token(live ? '--map-label' : '--map-label-dim') + '">' + esc(p.name) + '</text>');
    });

    // 楼名放不下时站号一起省掉，不留下没有地点名的数字。
    stops.forEach(function (item) {
      var p = item.poi;
      if (!labeled[p.id]) return;
      var size = BOX[p.kind] || [52, 34, 12];
      var cy = SY(p.x, p.y) - size[2] * 1.6 * S - 11;
      var colour = token(item.trip === 0 ? '--map-road1' : '--map-road2');
      out.push('<circle cx="' + SX(p.x, p.y).toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="9" fill="' + colour + '"/>');
      out.push('<text x="' + SX(p.x, p.y).toFixed(1) + '" y="' + (cy + 3.5).toFixed(1) + '" text-anchor="middle" font-size="11" fill="' + token('--paper-raise') + '">' + esc(item.n) + '</text>');
    });

    out.push('</svg>');
    host.innerHTML = out.join('');
    drawMeta(plan);
  }

  function drawMeta(plan) {
    var meta = $('trip-map-meta');
    if (meta) meta.textContent = (plan.origin ? '起点 ' + plan.origin.name + ' · ' : '') + '横向拖动转视角，双击回正';
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

    if ($('trip-title')) $('trip-title').textContent = state.route ? '这次路线' : '今天几趟';
    var meta = $('trip-meta');
    var counts = plan.counts || {};
    if (meta) {
      var walk = trips.reduce(function (sum, trip) { return sum + (trip.walk_minutes || 0); }, 0);
      meta.textContent = state.route ? state.route.poi_ids.length + ' 站 · ' + state.route.distance_m + ' 米 · 步行 ' + state.route.walk_minutes + ' 分钟' :
        counts.stops ? counts.stops + ' 件 · ' + trips.length + ' 趟 · 走路 ' + walk + ' 分钟' : '空闲';
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

  var SNAPSHOT = 'nh_trip_snapshot';
  function savedPlan() {
    try {
      var saved = JSON.parse(localStorage.getItem(SNAPSHOT));
      if (!saved || !saved.plan || !Array.isArray(saved.plan.trips) || !Array.isArray(saved.plan.pois) || !Number.isFinite(saved.at)) return null;
      return saved;
    } catch (error) { return null; }
  }
  function snapshotNote(text) {
    var note = $('trip-snapshot');
    if (!note) {
      note = document.createElement('p');
      note.id = 'trip-snapshot';
      note.setAttribute('role', 'status');
      $('trip-list').before(note);
    }
    note.textContent = text;
    note.hidden = !text;
  }
  function showSaved() {
    var saved = savedPlan();
    if (!saved) { fail('还没有保存过排法。连上服务打开一次，之后断网也能看。'); return; }
    try {
      render(saved.plan, true);
      snapshotNote('上次的排法 · ' + new Date(saved.at).toLocaleString('zh-CN') + ' 保存。当前只读，连上后点「重试」更新。');
    } catch (error) {
      fail('保存的排法读不出来。连上服务后点「重试」。');
    }
  }
  window.addEventListener('nh:offline', showSaved);

  function renderRoute(route) {
    if (!route || !Array.isArray(route.order) || !Array.isArray(route.poi_ids) ||
        !route.poi_ids.length || route.order.length !== route.poi_ids.length ||
        !Array.isArray(route.legs) || route.legs.length !== route.poi_ids.length - 1 ||
        !route.order.every(function (name) { return typeof name === 'string' && name.length; }) ||
        !route.poi_ids.every(function (id) { return typeof id === 'string' && id.length; }) ||
        !route.legs.every(function (leg) { return leg && typeof leg.start === 'string' && typeof leg.end === 'string' && Number.isFinite(leg.distance_m) && leg.distance_m >= 0 && Number.isFinite(leg.walk_minutes) && leg.walk_minutes >= 0; }) ||
        !Number.isFinite(route.distance_m) || route.distance_m < 0 || !Number.isFinite(route.walk_minutes) || route.walk_minutes < 0) {
      return Promise.reject(new Error('路线结果不完整，请重试这句话。'));
    }
    return (state.plan ? Promise.resolve() : load()).then(function () {
      var pois = state.plan && state.plan.pois || [];
      if (route.poi_ids.some(function (id) {
        return !pois.some(function (poi) { return poi.id === id && Number.isFinite(poi.x) && Number.isFinite(poi.y); });
      })) throw new Error('路线地点不在这张地图中，请重新读取今天排法后再试。');
      state.route = route;
      document.body.classList.add('has-route');
      drawMap(state.plan);
      renderTrips(state.plan);
      var panel = $('trip-route');
      if (!panel) {
        panel = document.createElement('div');
        panel.id = 'trip-route';
        panel.className = 'trip-route';
        panel.setAttribute('role', 'status');
        $('trip-stage').appendChild(panel);
      }
      panel.hidden = false;
      panel.innerHTML = '<b>' + route.order.map(esc).join(' → ') + '</b><span>' +
        esc(route.distance_m) + ' 米 · 步行 ' + esc(route.walk_minutes) + ' 分钟</span><small>' +
        route.legs.map(function (leg) { return esc(leg.start) + ' → ' + esc(leg.end) + '：' + esc(leg.distance_m) + ' 米 / ' + esc(leg.walk_minutes) + ' 分钟'; }).join('；') +
        '</small><small>' + esc(route.basis || '') + '</small>';
      var back = document.createElement('button');
      back.type = 'button';
      back.className = 'btn sm';
      back.textContent = '返回今天排法';
      back.addEventListener('click', function () {
        state.route = null;
        document.body.classList.remove('has-route');
        panel.hidden = true;
        drawMap(state.plan);
        renderTrips(state.plan);
      });
      panel.appendChild(back);
    });
  }

  function render(plan, fromSnapshot) {
    if (fromSnapshot) {
      state.route = null;
      document.body.classList.remove('has-route');
      if ($('trip-route')) $('trip-route').hidden = true;
    }
    state.plan = plan;
    // 手机保留待办概览并限高，地图与行程接在下方。
    document.body.classList.add('has-trip');
    drawMap(plan);
    renderTrips(plan);
    if (!fromSnapshot) {
      snapshotNote('');
      try { localStorage.setItem(SNAPSHOT, JSON.stringify({ at: Date.now(), plan: plan })); }
      catch (error) { snapshotNote('浏览器没能保存这次排法；断网后可能看不到。请检查浏览器存储空间或隐私设置。'); }
    }
  }

  function fail(message) {
    state.plan = null;
    state.route = null;
    document.body.classList.remove('has-route');
    if ($('trip-route')) $('trip-route').hidden = true;
    snapshotNote('');
    if ($('trip-rest')) { $('trip-rest').innerHTML = ''; $('trip-rest').hidden = true; }
    if ($('trip-meta')) $('trip-meta').textContent = '';
    document.body.classList.remove('has-trip');
    var list = $('trip-list');
    if (list) list.innerHTML = '<p class="trip-empty">' + esc(message) + '</p>';
    var host = $('trip-map');
    if (host) host.innerHTML = '';
    if ($('trip-map-meta')) $('trip-map-meta').textContent = '';
    if ($('trip-legend')) $('trip-legend').hidden = true;
    if (list) {
      var retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'btn';
      retry.textContent = '重试读取行程';
      retry.addEventListener('click', function () { retry.disabled = true; load(); });
      list.appendChild(retry);
    }
  }

  function load() {
    if (!document.getElementById('trip-stage')) return Promise.resolve();
    if (navigator.onLine === false) { showSaved(); return Promise.resolve(); }
    return api(PLAN, { timeoutMs: 20000 }).then(function (body) {
      if (body && body.ok && body.plan) render(body.plan);
      else fail('行程接口没返回数据，' + (body && body.error ? body.error : '稍后再试'));
    }, function (error) {
      if (error && error.networkUnavailable || navigator.onLine === false) { showSaved(); return; }
      // Auth/HTTP errors must not reveal a previous session's plan.
      try { localStorage.removeItem(SNAPSHOT); } catch (storageError) {}
      snapshotNote('');
      var message = error && error.message ? error.message : '连不上本机服务';
      // 后端 404 的 body 是英文 'not found'，光靠文案判断会把这句英文直接甩到界面上（实测见过）。
      // api() 现在把状态码挂在 error.status 上，按状态判最稳；老调用方仍给了能匹配的文字就走文字。
      if ((error && error.status === 404) || /404/.test(message)) {
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
        // data-routine-title 是拖动时要挪的那一站：服务端排出来的 stop 里没有 id，只有标题。
        return '<span class="trip-chip" data-routine-id="' + esc(item.id) + '" data-routine-title="' + esc(item.title) + '">' +
          esc(item.title) + '<button type="button" data-routine-remove="' + esc(item.id) + '" aria-label="删掉 ' + esc(item.title) + '">×</button></span>';
      }).join('')
      : '<span class="trip-chip-empty">还没有常做的事。加一条，以后排行程时它按普通一站算。</span>';
  }

  // ---- 常做的事拖进行程 ----
  // 手指短按要还是「插一下」，只有真移动超过 CHIP_DRAG_SLOP 才算拖。
  // 手机上 HTML5 的 dragstart 根本不触发，所以鼠标 / 触屏 / 笔统一走 pointer 事件。
  var CHIP_DRAG_SLOP = 6;

  function chipPressKind(dx, dy) {
    return Math.abs(dx) + Math.abs(dy) < CHIP_DRAG_SLOP ? 'tap' : 'drag';
  }

  // 指针落点是行程里的哪一格：比每行的中线，上半插它前面、下半插它后面，取最近的那行。
  // 横向也要落在那一趟的卡片附近，免得拖到地图上也凭空插一条。
  function routineSlotAt(doc, x, y) {
    var list = doc.getElementById('trip-list');
    if (!list) return null;
    var cards = list.querySelectorAll('.trip-card');
    var best = null;
    for (var ci = 0; ci < cards.length; ci += 1) {
      var rows = cards[ci].querySelectorAll('.trip-row');
      for (var ri = 0; ri < rows.length; ri += 1) {
        var box = rows[ri].getBoundingClientRect();
        if (x < box.left - 24 || x > box.right + 24) continue;
        var mid = box.top + box.height / 2;
        var distance = Math.abs(y - mid);
        if (!best || distance < best.distance) {
          best = { trip: ci, index: y < mid ? ri : ri + 1, distance: distance, row: rows[ri] };
        }
      }
    }
    return best;
  }

  // 把某一站挪到指定位置。常做的事本来就已经被排成普通一站（顺序是服务端最近邻算的），
  // 所以这里是「挪」不是「再加一条」，否则行程里会出现两条一模一样的。
  function moveRoutineStop(plan, title, tripIndex, stopIndex) {
    if (!plan || !Array.isArray(plan.trips)) return false;
    var name = String(title == null ? '' : title).trim();
    if (!name) return false;
    for (var ti = 0; ti < plan.trips.length; ti += 1) {
      var stops = plan.trips[ti].stops || [];
      for (var si = 0; si < stops.length; si += 1) {
        if (String(stops[si].title == null ? '' : stops[si].title).trim() !== name) continue;
        var target = plan.trips[tripIndex];
        if (!target) return false;
        var stop = stops.splice(si, 1)[0];
        var list = target.stops || (target.stops = []);
        // 同一趟里往后挪时，先取出来的那一位已经让后面的下标前移了一位。
        var at = stopIndex;
        if (ti === tripIndex && si < stopIndex) at -= 1;
        list.splice(Math.max(0, Math.min(at, list.length)), 0, stop);
        return true;
      }
    }
    return false;
  }

  function appendRoutineStop(plan, title) {
    if (!plan || !Array.isArray(plan.trips) || !plan.trips.length) return false;
    var last = plan.trips.length - 1;
    return moveRoutineStop(plan, title, last, (plan.trips[last].stops || []).length);
  }

  function orderNote(text) {
    var note = $('trip-order-note');
    if (!note) {
      var list = $('trip-list');
      if (!list || !list.parentNode) return;
      note = document.createElement('p');
      note.id = 'trip-order-note';
      note.className = 'trip-order-note';
      note.setAttribute('role', 'status');
      list.parentNode.insertBefore(note, list);
    }
    note.textContent = text;
    note.hidden = !text;
  }

  // 松手后把这一天的站序写进服务端，刷新后还在。失败了不回滚页面上的顺序——
  // 用户刚摆好，回滚等于「什么都没发生」；改成明说这次没存上。
  function pushPlanOrder(title) {
    if (!state.plan || !Array.isArray(state.plan.trips)) return Promise.resolve(false);
    var order = [];
    state.plan.trips.forEach(function (trip) {
      (trip.stops || []).forEach(function (stop) {
        var name = String((stop && stop.title) == null ? '' : stop.title).trim();
        if (name && order.indexOf(name) < 0) order.push(name);
      });
    });
    return api(PLAN, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'order', date: String(state.plan.date || ''), order: order }),
      timeoutMs: 15000
    }).then(function (body) {
      // 服务端重算过的 plan 才算数：拿它重画，别让页面自己猜存成什么样。
      if (body && body.plan) render(body.plan);
      orderNote('「' + title + '」已经挪好，顺序也存下来了。');
      return true;
    }, function (error) {
      orderNote('「' + title + '」已经挪到这儿了，但顺序没存上（' +
        (error && error.message ? error.message : '连不上服务') + '）：刷新会回到按远近排的走法。');
      return false;
    });
  }

  function dropRoutine(title, slot) {
    if (!state.plan) return Promise.resolve(false);
    var moved = slot
      ? moveRoutineStop(state.plan, title, slot.trip, slot.index)
      : appendRoutineStop(state.plan, title);
    if (!moved) {
      orderNote('「' + title + '」今天没排进行程，先把这一站加进来再摆位置。');
      return Promise.resolve(false);
    }
    renderTrips(state.plan);
    drawMap(state.plan);
    orderNote('「' + title + '」已经挪到这趟行程里，正在把顺序存下来…');
    return pushPlanOrder(title);
  }

  function loadRoutines() {
    return api(ROUTINES, { timeoutMs: 15000 }).then(function (body) {
      renderRoutineChips((body && body.routines) || []);
    }, function (error) {
      var box = $('trip-routine-chips');
      if (!box) return;
      box.innerHTML = '<span class="trip-chip-empty">读取常做的事失败：' + esc(error && error.message ? error.message : '连不上服务') + '</span>';
      var retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'btn';
      retry.textContent = '重试读取';
      retry.addEventListener('click', function () { retry.disabled = true; loadRoutines(); });
      box.appendChild(retry);
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
            (item.name || item.title || '') + ' ' + (item.place || '');
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

  // 手机截图动辄三四 MB，弱网上传要半分钟，模型也读得更慢：先缩到宽 1400 再传。
  // 缩不动（老浏览器 / 图坏了）就返回空串，调用方退回原图。
  function shrinkImage(file) {
    return new Promise(function (resolve) {
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        var data = '';
        try {
          var width = img.naturalWidth || 1400;
          var height = img.naturalHeight || 1000;
          var scale = Math.min(1, 1400 / width);
          var canvas = document.createElement('canvas');
          canvas.width = Math.round(width * scale);
          canvas.height = Math.round(height * scale);
          canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
          data = canvas.toDataURL('image/png');
        } catch (error) {
          data = '';
        }
        URL.revokeObjectURL(url);
        resolve(data);
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        resolve('');
      };
      img.src = url;
    });
  }

  function readAsDataUrl(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () { resolve(reader.result); };
      reader.onerror = function () { reject(new Error('这张图读不出来，换一张试试')); };
      reader.readAsDataURL(file);
    });
  }

  // 课表截图：只读成草稿填进上面的框，存不存由人点保存决定。
  // 本地 35B 读一张课表实测 94.8 秒，所以这条请求单独给 200 秒，别按 30 秒的默认值掐。
  function readScheduleShot() {
    var input = $('trip-schedule-file');
    var msg = $('trip-schedule-msg');
    var shot = $('trip-schedule-shot');
    if (!input) return;
    var file = input.files && input.files[0];
    if (!file) return;
    if (msg) msg.textContent = '正在读图，这一张要几十秒，这个窗先别关。';
    if (shot) shot.disabled = true;
    shrinkImage(file).then(function (shrunk) {
      return shrunk || readAsDataUrl(file);
    }).then(function (dataUrl) {
      return api(SCHEDULE, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image: dataUrl }),
        timeoutMs: 200000
      });
    }).then(function (body) {
      var text = $('trip-schedule-text');
      if (text && body && body.text) text.value = body.text;
      if (msg) {
        var rejected = (body && body.rejected) || [];
        msg.textContent = '认出 ' + ((body && body.count) || 0) + ' 节课，核对一下再点保存。' +
          (rejected.length ? '这几行没认出来：' + rejected.join(' / ') : '');
      }
    }, function (error) {
      if (msg) msg.textContent = (error && error.message)
        ? error.message
        : '读图失败，换张清楚点的截图，或者直接贴文字。';
    }).then(function () {
      if (shot) shot.disabled = false;
      input.value = '';
    });
  }

  // ── 底图模式：自绘 / OSM / 高德 / 百度 ───────────────────────────────
  // 自绘是默认：离线可用、不依赖 key。OSM 用 Leaflet 拉官方瓦片。
  // 高德、百度要 key，配置从后端读（值只从环境变量来，页面里没有字面量）；
  // 没有 key 时按钮可见但禁用，点了给一句话，不是没反应。
  var MAP_CONF = '/api/agent/map/config';
  var POI_SAVE = '/api/agent/campus/poi';
  // （校区路网快照见 js/campus-osm.js：window.NH_CAMPUS_OSM）
  var MODES = [
    { id: 'sketch', label: '自绘' },
    { id: 'osm', label: 'OSM', credit: '© OpenStreetMap contributors' },
    { id: 'amap', label: '高德', credit: '© 高德地图' },
    { id: 'baidu', label: '百度', credit: '© 百度地图' }
  ];
  var mode = { id: 'sketch', conf: null, geo: null, adapter: null, busy: false };
  try {
    var savedMode = localStorage.getItem('nh_map_mode');
    if (savedMode) mode.id = savedMode;
  } catch (e) {}

  function mapWrap() {
    var host = $('trip-map');
    return host && host.parentNode;
  }

  function modeBar() {
    var box = mapWrap();
    if (!box) return null;
    var bar = box.querySelector('.map-modes');
    if (bar) return bar;
    bar = document.createElement('div');
    bar.className = 'map-modes';
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', '底图');
    MODES.forEach(function (m) {
      var button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('data-map-mode', m.id);
      button.textContent = m.label;
      bar.appendChild(button);
    });
    bar.addEventListener('click', function (event) {
      var button = event.target.closest('[data-map-mode]');
      if (!button) return;
      if (button.disabled) {
        // 禁用的按钮不派发 click：用 pointerdown 兜不住，这里靠父节点的点击冒泡也到不了，
        // 所以禁用项不真禁用按钮，改用 aria-disabled + 这里解释一句。
        return;
      }
      choose(button.getAttribute('data-map-mode'));
    });
    box.appendChild(bar);
    return bar;
  }

  function refreshModes() {
    var bar = modeBar();
    var box = mapWrap();
    if (!bar || !box) return;
    var conf = mode.conf || {};
    bar.querySelectorAll('[data-map-mode]').forEach(function (button) {
      var id = button.getAttribute('data-map-mode');
      var ready = id === 'sketch' || id === 'osm' ||
        !!(conf[id] && conf[id].enabled === true && conf[id].key);
      button.disabled = false;
      button.setAttribute('aria-disabled', ready ? 'false' : 'true');
      button.classList.toggle('is-off', !ready);
      button.setAttribute('aria-pressed', id === mode.id ? 'true' : 'false');
      button.title = ready ? (MODES.filter(function (m) { return m.id === id; })[0] || {}).credit || ''
        : '还没配 key，先看自绘或 OSM';
    });
    var credit = box.querySelector('.map-credit');
    var current = MODES.filter(function (m) { return m.id === mode.id; })[0];
    if (current && current.credit) {
      if (!credit) {
        credit = document.createElement('span');
        credit.className = 'map-credit';
        // 挂在 wrap 上，不是模式栏上：模式栏自己是定位元素，署名会被拉到按钮边上。
        box.appendChild(credit);
      }
      credit.textContent = current.credit;
    } else if (credit) {
      credit.remove();
    }
  }

  function note(text) {
    var box = mapWrap();
    if (!box) return;
    var el = box.querySelector('.map-note');
    if (!text) { if (el) el.remove(); return; }
    if (!el) {
      el = document.createElement('p');
      el.className = 'map-note';
      el.setAttribute('role', 'status');
      box.appendChild(el);
    }
    el.textContent = text;
  }

  function loadModeConf() {
    if (mode.conf) return Promise.resolve(mode.conf);
    return api(MAP_CONF, { timeoutMs: 8000 }).then(function (body) {
      mode.conf = body || {};
      return mode.conf;
    }, function () {
      mode.conf = {};
      return mode.conf;
    });
  }

  // 校区经纬度基准：取自 OSM 快照的西北角，跟 campus.py 的米制平面同源。
  // 快照是静态脚本（js/campus-osm.js），没到就等于没有基准，在线档如实说不出来。
  function geo() {
    if (mode.geo) return Promise.resolve(mode.geo);
    var data = window.NH_CAMPUS_OSM;
    if (!data || !data.origin || !Number.isFinite(data.origin.lat)) {
      return Promise.reject(new Error('还缺校区经纬度基准，先看自绘或 OSM'));
    }
    mode.geo = {
      lat: data.origin.lat, lon: data.origin.lon,
      mpp: Number(data.metersPerPixel) || 0.9,
      cal: (data.calibration && data.calibration.osmToPoi) || null
    };
    return Promise.resolve(mode.geo);
  }

  var M_PER_DEG = 111320;
  // campus.py 的像素 → 真实地图的像素（校准）→ 经纬度。
  function pxToWgs(x, y) {
    var g = mode.geo;
    var cal = g.cal;
    var rx = cal && cal.sx ? (x - cal.bx) / cal.sx : x;
    var ry = cal && cal.sy ? (y - cal.by) / cal.sy : y;
    return {
      lat: g.lat - (ry * g.mpp) / M_PER_DEG,
      lon: g.lon + (rx * g.mpp) / (M_PER_DEG * Math.cos(g.lat * Math.PI / 180))
    };
  }
  function wgsToPx(lat, lon) {
    var g = mode.geo;
    var cal = g.cal;
    var rx = ((lon - g.lon) * M_PER_DEG * Math.cos(g.lat * Math.PI / 180)) / g.mpp;
    var ry = ((g.lat - lat) * M_PER_DEG) / g.mpp;
    return {
      x: cal && cal.sx ? rx * cal.sx + cal.bx : rx,
      y: cal && cal.sy ? ry * cal.sy + cal.by : ry
    };
  }

  // 国内三家的坐标：高德是 GCJ-02，百度是 BD-09，OSM 是 WGS-84。
  // 换算是公开的偏移算法，不算测绘数据；不换算会整体偏几百米。
  var GCJ_A = 6378245.0;
  var GCJ_EE = 0.00669342162296594323;
  function outOfChina(lat, lon) {
    return !(lon > 73.66 && lon < 135.05 && lat > 3.86 && lat < 53.55);
  }
  function tLat(x, y) {
    var r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
    r += (20 * Math.sin(y * Math.PI) + 40 * Math.sin(y / 3 * Math.PI)) * 2 / 3;
    r += (160 * Math.sin(y / 12 * Math.PI) + 320 * Math.sin(y * Math.PI / 30)) * 2 / 3;
    return r;
  }
  function tLon(x, y) {
    var r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
    r += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
    r += (20 * Math.sin(x * Math.PI) + 40 * Math.sin(x / 3 * Math.PI)) * 2 / 3;
    r += (150 * Math.sin(x / 12 * Math.PI) + 300 * Math.sin(x / 30 * Math.PI)) * 2 / 3;
    return r;
  }
  function wgsToGcj(lat, lon) {
    if (outOfChina(lat, lon)) return { lat: lat, lon: lon };
    var dLat = tLat(lon - 105, lat - 35);
    var dLon = tLon(lon - 105, lat - 35);
    var rad = lat / 180 * Math.PI;
    var magic = Math.sin(rad);
    magic = 1 - GCJ_EE * magic * magic;
    var sqrtMagic = Math.sqrt(magic);
    dLat = (dLat * 180) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtMagic) * Math.PI);
    dLon = (dLon * 180) / (GCJ_A / sqrtMagic * Math.cos(rad) * Math.PI);
    return { lat: lat + dLat, lon: lon + dLon };
  }
  function gcjToBd(lat, lon) {
    var z = Math.sqrt(lon * lon + lat * lat) + 0.00002 * Math.sin(lat * Math.PI * 3000 / 180);
    var theta = Math.atan2(lat, lon) + 0.000003 * Math.cos(lon * Math.PI * 3000 / 180);
    return { lat: z * Math.sin(theta) + 0.006, lon: z * Math.cos(theta) + 0.0065 };
  }
  function bdToGcj(lat, lon) {
    var x = lon - 0.0065;
    var y = lat - 0.006;
    var z = Math.sqrt(x * x + y * y) - 0.00002 * Math.sin(y * Math.PI * 3000 / 180);
    var theta = Math.atan2(y, x) - 0.000003 * Math.cos(x * Math.PI * 3000 / 180);
    return { lat: z * Math.sin(theta), lon: z * Math.cos(theta) };
  }
  function gcjToWgs(lat, lon) {
    var now = wgsToGcj(lat, lon);
    return { lat: lat - (now.lat - lat), lon: lon - (now.lon - lon) };
  }

  function loadScript(src, ready) {
    return new Promise(function (resolve, reject) {
      if (ready()) { resolve(); return; }
      var script = document.createElement('script');
      var settled = false;
      var timer = window.setTimeout(function () {
        if (settled) return;
        settled = true;
        reject(new Error('在线底图没连上，先用示意地图'));
      }, 15000);
      script.src = src;
      script.async = true;
      script.onload = function () {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        resolve();
      };
      script.onerror = function () {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        reject(new Error('在线底图没连上，先用示意地图'));
      };
      document.head.appendChild(script);
    });
  }

  function loadLeaflet() {
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
    document.head.appendChild(link);
    return loadScript('https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
      function () { return !!window.L; });
  }

  // 脚本进来了不代表图画出来了：瓦片被墙、域名没进白名单，都会静默留一块灰。
  // 所以等第一笔真的画出来；等不到就当这一档连不上，拆掉让 choose 退回示意地图。
  function waitFirst(map, events, message, cleanup) {
    return new Promise(function (resolve, reject) {
      var done = false;
      function hit() { if (!done) { done = true; resolve(); } }
      events.forEach(function (name) { map.on(name, hit); });
      window.setTimeout(function () {
        if (done) return;
        done = true;
        try { cleanup(); } catch (e) {}
        reject(new Error(message));
      }, 6000);
    });
  }

  // 三家的差别只在「怎么建图、怎么把点投到容器坐标、怎么框住一块」，收成同一个壳，
  // 图层（POI、路线、站号）只画一套，各档看起来才是一致的。
  function makeMap(id, el) {
    var conf = mode.conf || {};
    if (id === 'osm') {
      return loadLeaflet().then(function () {
        var map = window.L.map(el, {
          zoomControl: true, attributionControl: true, scrollWheelZoom: true, minZoom: 3, maxZoom: 19
        });
        window.L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
          maxZoom: 19, attribution: '© OpenStreetMap contributors'
        }).addTo(map);
        return waitFirst(map, ['tileload'], 'OSM 瓦片没连上，先用示意地图', function () {
          map.remove();
        }).then(function () { return {
          kind: 'osm',
          project: function (p) {
            var q = map.latLngToContainerPoint(window.L.latLng(p.lat, p.lon));
            return { x: q.x, y: q.y };
          },
          frame: function (pts) {
            var bounds = window.L.latLngBounds(pts.map(function (p) {
              return window.L.latLng(p.lat, p.lon);
            }));
            map.fitBounds(bounds, { padding: [18, 18], animate: false });
          },
          onMove: function (cb) { map.on('move zoom resize', cb); },
          from: function (x, y) {
            var ll = map.containerPointToLatLng([x, y]);
            return { lat: ll.lat, lon: ll.lng };
          },
          destroy: function () { map.remove(); }
          };
        });
      });
    }
    if (id === 'amap') {
      if (!conf.amap || !conf.amap.enabled || !conf.amap.key) throw new Error('还没配 key，先看自绘或 OSM');
      window._AMapSecurityConfig = { securityJsCode: conf.amap.secret || '' };
      return loadScript('https://webapi.amap.com/maps?v=2.0&key=' + encodeURIComponent(conf.amap.key),
        function () { return !!window.AMap; }).then(function () {
        var map = new window.AMap.Map(el, {
          viewMode: '2D', zoom: 16, zooms: [3, 19], showLabel: true, rotateEnable: false, pitchEnable: false
        });
        // 高德 2.0 没有 tilesloaded，'complete' 就是「首屏建完了」。
        // ponytail: key 被域名白名单拒掉时它照样 complete、画面空白，前端分辨不出来；
        // 这种情况看控制台原文（INVALID_USER_DOMAIN 一类），有需求再加像素校验。
        return waitFirst(map, ['complete'], '高德地图没出图，先用示意地图', function () {
          try { map.destroy(); } catch (e) {}
        }).then(function () { return {
          kind: 'amap',
          project: function (p) {
            var q = map.lngLatToContainer(new window.AMap.LngLat(p.lon, p.lat));
            return { x: q.getX(), y: q.getY() };
          },
          frame: function (pts) {
            var lons = pts.map(function (p) { return p.lon; });
            var lats = pts.map(function (p) { return p.lat; });
            map.setBounds(new window.AMap.Bounds(
              [Math.min.apply(null, lons), Math.min.apply(null, lats)],
              [Math.max.apply(null, lons), Math.max.apply(null, lats)]));
          },
          onMove: function (cb) { map.on('mapmove', cb); map.on('zoomend', cb); map.on('moveend', cb); },
          from: function (x, y) {
            var p = map.containerToLngLat(new window.AMap.Pixel(x, y));
            return { lat: p.getLat(), lon: p.getLng() };
          },
          destroy: function () { map.destroy(); }
          };
        });
      });
    }
    if (id === 'baidu') {
      if (!conf.baidu || !conf.baidu.enabled || !conf.baidu.key) throw new Error('还没配 key，先看自绘或 OSM');
      // 走 getscript 而不是 api?type=webgl：后者只是一层 document.write 包装，
      // 异步插进去的脚本里 document.write 是空操作，BMapGL 永远不出现。
      return loadScript('https://api.map.baidu.com/getscript?type=webgl&v=1.0&ak=' +
        encodeURIComponent(conf.baidu.key), function () { return !!window.BMapGL; }).then(function () {
        var map = new window.BMapGL.Map(el, { enableMapClick: false });
        map.centerAndZoom(new window.BMapGL.Point(mode.geo.lon, mode.geo.lat), 16);
        map.enableScrollWheelZoom(true);
        return waitFirst(map, ['tilesloaded'], '百度地图没出图，先用示意地图', function () {
          el.innerHTML = '';
        }).then(function () { return {          kind: 'baidu',
          project: function (p) {
            var q = map.pointToOverlayPixel(new window.BMapGL.Point(p.lon, p.lat));
            return { x: q.x, y: q.y };
          },
          frame: function (pts) {
            map.setViewport(pts.map(function (p) { return new window.BMapGL.Point(p.lon, p.lat); }));
          },
          onMove: function (cb) {
            map.addEventListener('moveend', cb);
            map.addEventListener('zoomend', cb);
            map.addEventListener('moving', cb);
            map.addEventListener('zooming', cb);
          },
          from: function (x, y) {
            var p = map.pixelToPoint(new window.BMapGL.Pixel(x, y));
            return { lat: p.lat, lon: p.lng };
          },
          destroy: function () { el.innerHTML = ''; }
          };
        });
      });
    }
    return Promise.reject(new Error('没有这一档'));
  }

  // 同一把校区坐标，按各档的坐标系换过去再交给 SDK。
  function toMode(px, kind) {
    var wgs = pxToWgs(px.x, px.y);
    if (kind === 'amap') return wgsToGcj(wgs.lat, wgs.lon);
    if (kind === 'baidu') return gcjToBd(wgsToGcj(wgs.lat, wgs.lon).lat, wgsToGcj(wgs.lat, wgs.lon).lon);
    return wgs;
  }
  function fromMode(point, kind) {
    if (kind === 'amap') {
      var wgs = gcjToWgs(point.lat, point.lon);
      return wgsToPx(wgs.lat, wgs.lon);
    }
    if (kind === 'baidu') {
      var gcj = bdToGcj(point.lat, point.lon);
      var back = gcjToWgs(gcj.lat, gcj.lon);
      return wgsToPx(back.lat, back.lon);
    }
    return wgsToPx(point.lat, point.lon);
  }

  var overlay = { canvas: null, ctx: null };

  function layoutOverlay() {
    var box = mapWrap();
    if (!box) return;
    if (!overlay.canvas) {
      overlay.canvas = document.createElement('canvas');
      overlay.canvas.className = 'map-overlay';
      box.appendChild(overlay.canvas);
      overlay.ctx = overlay.canvas.getContext('2d');
    }
    var rect = box.getBoundingClientRect();
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    overlay.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    overlay.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    overlay.canvas.style.width = rect.width + 'px';
    overlay.canvas.style.height = rect.height + 'px';
    overlay.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // 在线底图上只画我们自己的东西：这一趟的路线、经过的楼、站号。
  // 底图本身（路、水、绿地、楼块）交给各档自己画，我们不去复刻。
  function drawOverlay() {
    if (!overlay.ctx || !mode.adapter || !state.plan) return;
    var adapter = mode.adapter;
    var kind = adapter.kind;
    var ctx = overlay.ctx;
    var w = overlay.canvas.width / (Math.min(2, window.devicePixelRatio || 1));
    var h = overlay.canvas.height / (Math.min(2, window.devicePixelRatio || 1));
    ctx.clearRect(0, 0, w, h);
    var pois = state.plan.pois || [];
    if (!pois.length) return;
    var byId = {};
    pois.forEach(function (p) { byId[p.id] = p; });
    var route = [];
    (state.plan.trips || []).forEach(function (trip) {
      (trip.stops || []).forEach(function (stop) {
        var poi = byId[stop.place_id];
        if (poi) route.push(poi);
      });
    });
    var pts = route.map(function (poi) {
      var geoPoint = toMode(poi, kind);
      var at = adapter.project(geoPoint);
      return { poi: poi, x: at.x, y: at.y };
    });
    if (pts.length > 1) {
      ctx.strokeStyle = token('--map-road2') || '#9A7B2E';
      ctx.lineWidth = 3;
      ctx.lineJoin = 'round';
      ctx.beginPath();
      pts.forEach(function (p, i) { if (i) ctx.lineTo(p.x, p.y); else ctx.moveTo(p.x, p.y); });
      ctx.stroke();
    }
    pts.forEach(function (p, i) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 9, 0, Math.PI * 2);
      ctx.fillStyle = token('--map-road1') || '#5C6B44';
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.font = '600 11px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(String(i + 1), p.x, p.y);
    });
    // 站号永远画，楼名让位：楼挨得近时名字会叠成一坨（两栋楼只差几十米），
    // 往上一层层挪，挪不开就不画这栋的名字——站号还在，位置不会丢。
    var taken = [];
    ctx.font = '12px system-ui, sans-serif';
    ctx.fillStyle = token('--map-label') || '#333';
    // 在线底图自带地名，我们的楼名压在别人家底图上没有描边就会糊在一起。
    ctx.strokeStyle = token('--paper') || '#fff';
    ctx.lineWidth = 3;
    ctx.lineJoin = 'round';
    pts.forEach(function (p) {
      var w = ctx.measureText(p.poi.name).width;
      for (var lift = 17; lift <= 17 + 14 * 2; lift += 14) {
        var box = { l: p.x - w / 2 - 2, r: p.x + w / 2 + 2, t: p.y - lift - 8, b: p.y - lift + 8 };
        var clash = taken.some(function (q) {
          return box.l < q.r && q.l < box.r && box.t < q.b && q.t < box.b;
        });
        if (!clash) {
          taken.push(box);
          ctx.strokeText(p.poi.name, p.x, p.y - lift);
          ctx.fillText(p.poi.name, p.x, p.y - lift);
          break;
        }
      }
    });
  }

  function teardownOnline() {
    if (mode.adapter) {
      try { mode.adapter.destroy(); } catch (e) {}
      mode.adapter = null;
    }
    var box = mapWrap();
    if (box) {
      var old = box.querySelector('.map-online');
      if (old) old.remove();
    }
    if (overlay.canvas) { overlay.canvas.remove(); overlay.canvas = null; overlay.ctx = null; }
    var host = $('trip-map');
    if (host) host.style.visibility = '';
  }

  function openOnline(id) {
    var box = mapWrap();
    if (!box) return Promise.reject(new Error('地图面板不在'));
    return geo().then(function () {
      return loadModeConf();
    }).then(function () {
      teardownOnline();
      var el = document.createElement('div');
      el.className = 'map-online';
      box.appendChild(el);
      // 在线档的底图缩放归 SDK 自己（Leaflet/高德/百度各有控件），面板上这套 +/− 只认
      // 自绘的 state（js/map3d.js 的 zoomBox），在这儿点了没反应 —— 收起来，别留死按钮。
      // 回自绘时 render() 会把 zoomBox 重新显示出来。
      var dead = box.querySelector('.map3d-zoom');
      if (dead) dead.hidden = true;
      return makeMap(id, el).then(function (adapter) {
        mode.adapter = adapter;
        var framed = (state.plan && state.plan.pois || []).slice();
        if (state.plan && state.plan.trips && state.plan.trips.length && box.clientHeight < 200) {
          var first = state.plan.trips[0];
          var byId = {};
          (state.plan.pois || []).forEach(function (p) { byId[p.id] = p; });
          var only = (first.stops || []).map(function (s) { return byId[s.place_id]; })
            .filter(function (p) { return p; });
          if (only.length) framed = only;
        }
        if (framed.length) {
          adapter.frame(framed.map(function (p) { return toMode(p, adapter.kind); }));
        }
        layoutOverlay();
        adapter.onMove(function () { drawOverlay(); });
        var host = $('trip-map');
        if (host) host.style.visibility = 'hidden';
        drawOverlay();
        return adapter;
      });
    });
  }

  function saveMode() {
    try { localStorage.setItem('nh_map_mode', mode.id); } catch (e) {}
  }

  function choose(id) {
    if (mode.busy) return;
    loadModeConf().then(function () {
      var conf = mode.conf || {};
      var needsKey = id === 'amap' || id === 'baidu';
      if (needsKey && !(conf[id] && conf[id].enabled && conf[id].key)) {
        note('还没配 key，先看自绘或 OSM');
        return null;
      }
      if (id === 'sketch') {
        teardownOnline();
        mode.id = 'sketch';
        saveMode();
        refreshModes();
        note('');
        if (state.plan) drawMap(state.plan);
        return null;
      }
      mode.busy = true;
      note('正在连' + (MODES.filter(function (m) { return m.id === id; })[0] || {}).label + '…');
      return openOnline(id).then(function () {
        mode.id = id;
        saveMode();
        refreshModes();
        note('');
      }).catch(function (error) {
        teardownOnline();
        mode.id = 'sketch';
        saveMode();
        refreshModes();
        if (state.plan) drawMap(state.plan);
        note((error && error.message) || '在线底图没连上，先用示意地图');
      });
    }).then(function () {
      mode.busy = false;
    }, function () {
      mode.busy = false;
    });
  }

  // 点图取点：自绘档问 3D 要平面坐标，在线档用各家的经纬度再换回来。
  function pickAt(event) {
    if (mode.id === 'sketch') {
      var host = $('trip-map');
      if (window.nhMap3d && window.nhMap3d.pick && host) {
        return window.nhMap3d.pick(host, event.clientX, event.clientY);
      }
      return null;
    }
    if (!mode.adapter) return null;
    var box = mapWrap().getBoundingClientRect();
    var adapter = mode.adapter;
    var point = adapter.from(event.clientX - box.left, event.clientY - box.top);
    if (!point) return null;
    var at = fromMode(point, adapter.kind);
    at.crs = adapter.kind === 'osm' ? 'wgs84' : adapter.kind;
    at.lat = point.lat;
    at.lon = point.lon;
    return at;
  }

  function addPlace(at) {
    var name = window.prompt('这处地点叫什么？', '');
    if (name === null) return;
    name = String(name).trim();
    if (!name) { note('没写名字，没加'); return; }
    api(POI_SAVE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'add', name: name, x: at.x, y: at.y, kind: 'other',
        lat: at.lat, lon: at.lon, crs: at.crs || 'campus' })
    }).then(function (body) {
      if (body && body.plan) render(body.plan);
      stopPicking();
      note('已加「' + name + '」，能当行程目的地了');
      window.setTimeout(function () { note(''); }, 4000);
    }, function (error) {
      note((error && error.message) || '没加上，再试一次');
    });
  }

  // 「＋ 地点」：点了进取点态，在地图上点一下就要名字，存进 campus_poi.json，
  // 下一次排程它就是个能当地的去处（服务端 match_place 一起认）。
  var picking = false;
  var downAt = null;

  function stopPicking() {
    picking = false;
    downAt = null;
    var box = mapWrap();
    if (!box) return;
    box.classList.remove('is-picking');
    var button = box.querySelector('.map-add');
    if (button) {
      button.classList.remove('is-on');
      button.setAttribute('aria-pressed', 'false');
    }
  }

  function addButton() {
    var box = mapWrap();
    if (!box) return null;
    var button = box.querySelector('.map-add');
    if (button) return button;
    button = document.createElement('button');
    button.type = 'button';
    button.className = 'map-add';
    button.textContent = '＋ 地点';
    button.title = '在地图上点一下，给这个地方起个名字';
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', function () {
      if (picking) { stopPicking(); note(''); return; }
      picking = true;
      button.classList.add('is-on');
      button.setAttribute('aria-pressed', 'true');
      box.classList.add('is-picking');
      note('在地图上点一下要加的位置');
    });
    box.appendChild(button);
    return button;
  }

  function bindPicking() {
    var box = mapWrap();
    if (!box || box.dataset.pickBound === '1') return;
    box.dataset.pickBound = '1';
    box.addEventListener('pointerdown', function (event) {
      if (picking) downAt = { x: event.clientX, y: event.clientY };
    }, true);
    box.addEventListener('pointerup', function (event) {
      if (!picking || !downAt) { downAt = null; return; }
      var moved = Math.abs(event.clientX - downAt.x) + Math.abs(event.clientY - downAt.y);
      downAt = null;
      // 拖动地图不算点位置，不然每拖一下都弹一次取名。
      if (moved > 8) return;
      if (event.target.closest && event.target.closest('.map-modes, .map3d-zoom, .map-add, .leaflet-control, .amap-controls')) return;
      var at = pickAt(event);
      if (!at || !Number.isFinite(at.x) || !Number.isFinite(at.y)) {
        note('这个位置取不到坐标，换一处点点');
        return;
      }
      addPlace(at);
    }, true);
  }

  function boot() {
    if (!document.getElementById('trip-stage')) return;
    modeBar();
    refreshModes();
    addButton();
    bindPicking();
    loadModeConf().then(function () {
      refreshModes();
      if (mode.id !== 'sketch') choose(mode.id);
    });
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

      // 按住 chip 拖到行程里松手 = 插到那个位置；轻点一下 = 插到最后一趟末尾。
      var press = null;
      var ghost = null;
      var line = null;
      function rowIndexIn(row) {
        var rows = row.parentNode ? row.parentNode.querySelectorAll('.trip-row') : [];
        return Array.prototype.indexOf.call(rows, row);
      }
      function moveGhost(x, y, text) {
        if (!ghost) {
          ghost = document.createElement('span');
          ghost.className = 'trip-chip is-drag-ghost';
          ghost.textContent = text;
          document.body.appendChild(ghost);
        }
        ghost.style.left = x + 'px';
        ghost.style.top = y + 'px';
      }
      function showLine(slot) {
        if (!line) {
          line = document.createElement('div');
          line.className = 'trip-drop-line';
          line.hidden = true;
          document.body.appendChild(line);
        }
        if (!slot || !slot.row) { line.hidden = true; return; }
        var box = slot.row.getBoundingClientRect();
        line.hidden = false;
        line.style.left = box.left + 'px';
        line.style.width = box.width + 'px';
        line.style.top = (slot.index <= rowIndexIn(slot.row) ? box.top : box.bottom) + 'px';
      }
      function clearChipDrag() {
        if (ghost && ghost.parentNode) ghost.parentNode.removeChild(ghost);
        ghost = null;
        if (line) line.hidden = true;
        document.removeEventListener('pointermove', onChipMove, true);
        document.removeEventListener('pointerup', onChipUp, true);
        document.removeEventListener('pointercancel', onChipUp, true);
      }
      function onChipMove(event) {
        if (!press || event.pointerId !== press.id) return;
        if (!press.dragging) {
          if (chipPressKind(event.clientX - press.x, event.clientY - press.y) !== 'drag') return;
          press.dragging = true;
          chips.classList.add('is-dragging');
        }
        event.preventDefault();
        moveGhost(event.clientX, event.clientY, press.title);
        press.slot = routineSlotAt(document, event.clientX, event.clientY);
        showLine(press.slot);
      }
      function onChipUp(event) {
        if (!press || (event.pointerId !== undefined && event.pointerId !== press.id)) return;
        var done = press;
        press = null;
        chips.classList.remove('is-dragging');
        clearChipDrag();
        dropRoutine(done.title, done.dragging ? done.slot : null);
      }
      chips.addEventListener('pointerdown', function (event) {
        if (event.button) return;
        if (event.target.closest && event.target.closest('[data-routine-remove]')) return;
        var chip = event.target.closest ? event.target.closest('[data-routine-id]') : null;
        if (!chip) return;
        press = {
          id: event.pointerId,
          x: event.clientX,
          y: event.clientY,
          title: chip.getAttribute('data-routine-title') || chip.textContent,
          dragging: false,
          slot: null
        };
        document.addEventListener('pointermove', onChipMove, true);
        document.addEventListener('pointerup', onChipUp, true);
        document.addEventListener('pointercancel', onChipUp, true);
      });
    }
    var scheduleOpen = $('trip-schedule-open');
    if (scheduleOpen) scheduleOpen.addEventListener('click', openSchedule);
    var scheduleSave = $('trip-schedule-save');
    if (scheduleSave) scheduleSave.addEventListener('click', saveSchedule);
    var scheduleShot = $('trip-schedule-shot');
    var scheduleFile = $('trip-schedule-file');
    if (scheduleShot && scheduleFile) {
      scheduleShot.addEventListener('click', function () { scheduleFile.click(); });
      scheduleFile.addEventListener('change', readScheduleShot);
    }

    // 尺寸一变就重画：切页签、收起对话、转屏、拉窗口都算。取景和颜色都是算出来的，不是 CSS 撑的，
    // 尺寸不对时画出来的东西也不会自己变对（以前只盯 resize + dock，中间那一刻量到 0 就废了）。
    var sizeHost = $('trip-map');
    var pending = 0;
    function redrawSoon() {
      if (pending) return;
      pending = window.requestAnimationFrame(function () {
        pending = 0;
        if (state.plan) drawMap(state.plan);
      });
    }
    if (sizeHost && window.ResizeObserver) new window.ResizeObserver(redrawSoon).observe(sizeHost);
    else window.addEventListener('resize', redrawSoon);

    // 拖动转视角，双击回正。转完存本地，下次进来还是这个角。
    // 3D 接管时（host.dataset.map3d）它自己管拖动，这里让开，别一边转一边整张重画。
    if (sizeHost) {
      var drag = null;
      sizeHost.addEventListener('pointerdown', function (event) {
        if (sizeHost.dataset.map3d === '1') return;
        if (event.button !== 0 || !state.plan) return;
        drag = { x: event.clientX, angle: view.angle };
        sizeHost.classList.add('is-dragging');
        if (sizeHost.setPointerCapture) sizeHost.setPointerCapture(event.pointerId);
      });
      sizeHost.addEventListener('pointermove', function (event) {
        if (!drag) return;
        view.angle = drag.angle + (event.clientX - drag.x) * 0.012;
        if (state.plan) drawMap(state.plan);
      });
      function endDrag() {
        if (!drag) return;
        drag = null;
        sizeHost.classList.remove('is-dragging');
        try { localStorage.setItem('nh_map_angle', String(view.angle)); } catch (e) {}
      }
      sizeHost.addEventListener('pointerup', endDrag);
      sizeHost.addEventListener('pointercancel', endDrag);
      sizeHost.addEventListener('dblclick', function () {
        if (sizeHost.dataset.map3d === '1') return;
        view.angle = 0;
        try { localStorage.setItem('nh_map_angle', '0'); } catch (e) {}
        if (state.plan) drawMap(state.plan);
      });
    }
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
    // 唤醒对话 / 收起对话时地图会跟着变高变矮，上面那个 ResizeObserver 已经管了，这里不再猜 60ms。
    // 待办改了行程就得重排：loadTasks 之后会派发这个事件（见 page.html）。
    document.addEventListener('nh:tasks-changed', function () { load(); });
    // type=module 是等解析完才执行的，可能比这页的普通脚本晚一步：3D 一到就补画一次。
    window.addEventListener('nh-map3d-ready', function () { if (state.plan) drawMap(state.plan); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  window.nhTrip = { reload: load, render: render, renderRoute: renderRoute };
})();





