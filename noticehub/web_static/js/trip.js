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
    // 手机上一屏放不下「概览 + 地图 + 行程」，有行程时就不再重复列一遍待办。
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
    if (msg) msg.textContent = '正在读图，本机模型大概一分半，这个窗先别关。';
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





