/* 今天几趟：左边校园地图，右边一趟一趟的行程。
   数字全来自 /api/agent/plan（服务端用同一套排程算出来的），页面不自己编。
   接口没开时如实说一句，不要留一块转圈的空面板。 */
(function () {
  'use strict';

  var PLAN = '/api/agent/plan';
  var ROUTINES = '/api/agent/routines';
  var SCHEDULE = '/api/agent/schedule';
  var state = { plan: null, route: null, busy: false };
  // 输入框原来的提示语：标了位置就还原它，别把例句永久换掉。
  var chatHint = null;

  function $(id) { return document.getElementById(id); }
  function esc(text) {
    return String(text == null ? '' : text).replace(/[&<>"]/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch];
    });
  }
  function token(name) {
    return window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // 路线走向两档：绕楼 = 用后端算好的折线（trip.line，绕开楼体），直线 = 楼与楼直连。
  // 键跟 js/map3d.js 共用，切一次两边都跟着变；存在本地，刷新还在。
  function lineMode() {
    try {
      return localStorage.getItem('nh_map_line') === 'straight' ? 'straight' : 'road';
    } catch (e) {
      return 'road';
    }
  }

  // 一趟要画的点（平面图坐标）。沿路档直接用后端那条线（它含起点），
  // 老数据没有 line 就退回「起点 + 各站」直连，图不能因此空着。
  function tripPoints(trip, origin, byId) {
    if (lineMode() === 'road' && trip.line && trip.line.length >= 2) return trip.line;
    var path = [];
    var firstPlace = trip.stops && trip.stops.length ? byId[trip.stops[0].place_id] : null;
    if (origin && firstPlace && origin.id !== firstPlace.id) path.push([origin.x, origin.y]);
    (trip.stops || []).forEach(function (stop) {
      var poi = byId[stop.place_id];
      if (poi) path.push([poi.x, poi.y]);
    });
    return path;
  }
  // 把每条腿的 stop.road.path 缝成整趟的 trip.line（米制坐标，贴路网绕楼走）。
  // 某条腿 degraded 或没有 path（没接上路网）就退回两端直连——红字那行会标「按直线示意」。
  // 后端只给每站的 stop.road，从不拼 trip.line，所以这两处画线一直退化成站站直连。
  // ponytail: 若后端某天自己给 trip.line，删掉这段客户端缝合
  function buildTripLines(plan) {
    var byId = {};
    (plan.pois || []).forEach(function (p) { byId[p.id] = p; });
    (plan.trips || []).forEach(function (trip) {
      if (trip.line && trip.line.length > 1) return; // 后端已给整趟折线就不动
      var line = [];
      (trip.stops || []).forEach(function (stop) {
        var poi = byId[stop.place_id];
        if (!poi) return;
        var road = stop.road;
        var path = road && road.degraded !== true && road.path && road.path.length > 1 ? road.path : null;
        if (path) {
          path.forEach(function (pt) {
            var last = line[line.length - 1];
            if (!last || last[0] !== pt[0] || last[1] !== pt[1]) line.push([pt[0], pt[1]]);
          });
        } else {
          var last = line[line.length - 1];
          if (!last || last[0] !== poi.x || last[1] !== poi.y) line.push([poi.x, poi.y]);
        }
      });
      if (line.length > 1) trip.line = line;
    });
  }
  // 拖动转的是这个角（弧度），0 就是默认这张 45° 的等距图。转完存本地，刷新还在。
  var view = { angle: 0 };
  try {
    var savedAngle = parseFloat(localStorage.getItem('nh_map_angle'));
    if (isFinite(savedAngle)) view.angle = savedAngle;
  } catch (e) { view.angle = 0; }

  // 每种楼的footprint和层高（米）。只让图看起来是立起来的，不是施工图。
  // 尺寸比早先那版收了一圈：坐标换成 OSM 真值以后，图文信息中心楼、图书馆、武进楼
  // 这几栋本来就挨着三四十米，76×44 的方块会互相压住、把隔壁的楼名盖掉。
  var BOX = {
    teach: [58, 34, 18], library: [56, 38, 20], office: [52, 30, 22],
    food: [54, 40, 10], shop: [36, 26, 8], parcel: [30, 24, 6],
    dorm: [50, 26, 12], health: [40, 28, 12], sport: [86, 56, 16],
    hall: [48, 34, 12], gate: [44, 10, 5]
  };
  // 窄屏上优先写名字的楼。id 必须是 campus-geo.json 里真实存在的键，
  // 以前这里写着 ligonglou（不存在的 id），立功楼于是永远进不了这张「必写」名单。
  var NAMED = ['ligong', 'lide', 'lixing', 'tuwen', 'library', 'boyaju',
    'canteen1', 'gym'];
  // 3D 那边（js/map3d.js）也用这张表：尺寸和「哪几栋要写名字」只留一份。
  window.nhMapModel = { size: BOX, named: NAMED };

  function drawStrip(plan, host, byId) {
    var trips = plan.trips || [];
    if (!trips.length) { host.innerHTML = '<p class="trip-strip-empty">今天还没有要出门的事</p>'; return; }
    var html = trips.map(function (trip, ti) {
      // 同一趟里相邻两站是同一个地方时合成一条「×2」，别画成「第一餐厅 → 第一餐厅」；
      // 只合相邻且地名原文相同的，中间隔了别的站的不合，地名不模糊匹配。只动显示层，趟本身不变。
      var stops = [];
      (trip.stops || []).forEach(function (stop) {
        var last = stops[stops.length - 1];
        if (last && last.place === stop.place) { last.count += 1; return; }
        stops.push({ place: stop.place, arrive: stop.arrive, count: 1 });
      });
      stops = stops.map(function (stop) {
        return '<span class="trip-strip-stop"><b>' + esc(stop.arrive || '') + '</b>' + esc(stop.place || '') +
          (stop.count > 1 ? ' ×' + stop.count : '') + '</span>';
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
    // 面板藏着（切到别的页签）或正在收合时宽度量到的是 0：拿这个尺寸画出来就是一坨糊的取景，
    // 而且画完再没人叫它（用户报的「3D 地图有时候不显示」）。等 ResizeObserver 叫醒，见 boot。
    // 高度不参与这个判断：对话态把画布压扁之后只剩三十几像素，那正是要换行程条的时候，
    // 按 40 卡住它会让扁带上什么都不画（实测 35 高、空白）。
    if (box.width < 40) return;
    var W = Math.max(220, box.width);
    var Hh = Math.max(120, box.height);
    // 对话态把地图压成一条扁带，等距图塞不进去（横向 7:1 怎么缩都是小方块），
    // 换成横向的行程条：还是这一趟的时间顺序，但按一条线读。
    if (box.height < 200) {
      // 扁带上不画 3D：先把上一块的画布和 GL 资源收掉，别留着一个不显示的渲染器占显存。
      if (window.nhMap3d) window.nhMap3d.dispose(host);
      drawStrip(plan, host, byId);
      positionHere(plan);
      return;
    }
    // 有 WebGL 就把这张图交给 3D（js/map3d.js），楼是真体块、带光和影子。
    // 没有就往下走这套 SVG：同一份数据，只是画法不同，不能因为显卡不行就没有地图。
    if (window.nhMap3d && window.nhMap3d.draw(host, plan)) {
      drawMeta(plan);
      // map3d 的画是丢进 rAF 的，camera 这一刻还没就位：等一帧再钉针，
      // 之后的转角/缩放由 boot 里挂的 onPaint 跟着挪。
      window.requestAnimationFrame(function () { positionHere(plan); });
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
      var route = tripPoints(trip, origin, byId);
      if (route.length < 2) return;
      var colour = token(ti === 0 ? '--map-road1' : '--map-road2');
      var d = route.map(function (p, i) {
        return (i ? 'L' : 'M') + SX(p[0], p[1]).toFixed(1) + ' ' + SY(p[0], p[1]).toFixed(1);
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

    // 起点得看得见：只画一条线从楼里出来，分不出人是从哪儿出发的。
    if (origin) {
      var ox = SX(origin.x, origin.y);
      var oy = SY(origin.x, origin.y) - 26;
      out.push('<circle cx="' + ox.toFixed(1) + '" cy="' + oy.toFixed(1) + '" r="4.5" fill="none" stroke="' +
        token('--map-road1') + '" stroke-width="2"/>');
      var startX = labelFits(ox, oy + 21, '起点');
      if (startX !== null) {
        out.push('<text x="' + startX.toFixed(1) + '" y="' + (oy + 21).toFixed(1) +
          '" text-anchor="middle" font-size="11" fill="' + token('--map-label') + '">起点</text>');
      }
    }

    out.push('</svg>');
    host.innerHTML = out.join('');
    drawMeta(plan);
    fillHere();
    positionHere(plan);
  }

  function drawMeta(plan) {
    var meta = $('trip-map-meta');
    if (meta) {
      var lead = '';
      if (plan.origin) {
        lead = '起点 ' + plan.origin.name + (plan.here ? '（你标的）' : '') + ' · ';
      } else {
        lead = '还没标你在哪：点「我在…」选一栋楼，路线按那儿算 · ';
      }
      meta.textContent = lead + '横向拖动转视角，双击回正';
    }
    // 对话框里也要提一句：起点默认按课表最后一节课算，人未必还在那儿。
    var chat = $('agent-input');
    if (chat) {
      if (chatHint === null) chatHint = chat.placeholder;
      chat.placeholder = plan.here ? chatHint : '先说你在哪栋楼（「我在图书馆」），路线就按你那儿算';
    }
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

  // 右侧这一栏一行一站：先时间，再地点，第二行压一行事的标题。
  // 以前只有地点，同楼三件事就排成三个一模一样的「立功楼」，站在那儿看不出要去干嘛；
  // 时间、件数、停留多久、为什么排这一站仍收进 title（栏窄，小字成列看不清）。
  // 这段路画不准才标：degraded=true 表示路网走不通、只能连直线穿楼（reason='straight'）。
  // no_leg（起点没有来处）/ missing_data（缺坐标）是「没有这一段」，不是「画不准」，一律不标——标了等于诬告。
  function degradedBadge(stop) {
    var road = stop.road;
    if (!road || road.degraded !== true) return '';
    // 两个类一起挂：trip-degraded 给版式（nowrap/边框/字号），trip-route-degraded 只改颜色。
    // 少了前一个，这句长中文会在窄栏里折行、把这一站撑高。
    return '<span class="trip-degraded trip-route-degraded">这一段没接上路网，按直线估的距离</span>';
  }

  function rowsOf(trip) {
    return (trip.stops || []).map(function (stop) {
      var where = [stop.place, stop.room].filter(Boolean).join(' · ') || stop.title;
      var sub = stop.place ? stop.title : '';
      var detail = [stop.arrive, stop.title, stop.room, stop.minutes ? '留 ' + stop.minutes + ' 分钟' : '', stop.why]
        .filter(Boolean).join(' · ');
      return '<li class="trip-row" title="' + esc(detail) + '">' +
        '<span class="trip-what"><span class="trip-line">' +
        (stop.arrive ? '<span class="trip-when">' + esc(stop.arrive) + '</span>' : '') +
        '<b>' + esc(where) + '</b>' + degradedBadge(stop) + '</span>' +
        (sub ? '<span class="trip-sub">' + esc(sub) + '</span>' : '') +
        '</span></li>';
    }).join('');
  }

  // 「没说地点」那一段以前把四十多条全铺开，一条一行还带一句原因，手机上是一大坨
  // 滚不到头的字。现在：原因一致时只写在头上（每条都是「没说地点」，重复四十遍
  // 等于没说），前五条先露出来，其余折在「展开全部」后面。
  function restBlock(label, items) {
    var reasons = items.map(function (item) { return String(item.reason || ''); });
    var uniform = reasons.every(function (reason) { return reason === reasons[0]; }) ? reasons[0] : '';
    var head = '<p class="trip-rest-head">' + esc(label) + '（' + items.length + ' 件）' +
      (uniform ? '<span class="trip-rest-why">' + esc(uniform) + '</span>' : '') + '</p>';
    var rows = items.map(function (item) {
      return '<li>' + esc(item.title) +
        (uniform ? '' : '<em>' + esc(item.reason || '') + '</em>') + '</li>';
    });
    if (rows.length <= 5) return head + '<ul class="trip-rest-list">' + rows.join('') + '</ul>';
    return head + '<ul class="trip-rest-list" data-more="1">' + rows.join('') + '</ul>' +
      '<button class="trip-rest-more" type="button" aria-expanded="false" ' +
      'data-label="展开全部 ' + rows.length + ' 条">展开全部 ' + rows.length + ' 条</button>';
  }

  // 没排进今天的每一件，后端都给了 reason_code（trips.py:30-35 明说界面按它分组）。
  // 「日子还没到」和「没认出地点」是两回事，不能共用一个抬头。
  // 抬头标签集合 = 后端真能产出的 code 集合（trips.py 只 _record future_deadline / no_place 两种）。
  // 「塞不下/没时间窗」走 plan.overflow 单独一档「今天塞不下」，从不进这里，别为标签齐全补假 code。
  var REST_LABELS = [
    ['no_place', '没说地点，先放着'],
    ['future_deadline', '日子还没到，不排今天']
  ];
  function restBlocks(items) {
    var byCode = {};
    var others = [];
    items.forEach(function (item) {
      var code = String(item.reason_code || '');
      var known = REST_LABELS.some(function (pair) { return pair[0] === code; });
      if (known) (byCode[code] = byCode[code] || []).push(item);
      else others.push(item);
    });
    var blocks = REST_LABELS.map(function (pair) {
      var rows = byCode[pair[0]];
      return rows && rows.length ? restBlock(pair[1], rows) : '';
    });
    if (others.length) blocks.push(restBlock('另有原因', others));
    return blocks.join('');
  }

  function renderTrips(plan) {
    var list = $('trip-list');
    if (!list) return;
    var trips = plan.trips || [];
    list.innerHTML = trips.map(function (trip, ti) {
      // 抬头只留「第 N 趟 · 哪个区」，时间/件数/走路几分钟收进 title。
      var head = [trip.start_at + '–' + trip.end_at, (trip.stops || []).length + ' 件',
        trip.walk_minutes ? '走路 ' + trip.walk_minutes + ' 分钟' : ''].filter(Boolean).join(' · ');
      return '<article class="trip-card"><div class="trip-card-head">' +
        '<h3 title="' + esc(head) + '">第' + (ti + 1) + '趟 · ' + esc(trip.zone || '') + '</h3></div>' +
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
      if ((plan.unscheduled || []).length) blocks.push(restBlocks(plan.unscheduled));
      if ((plan.overflow || []).length) blocks.push(restBlock('今天塞不下', plan.overflow));
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
        // 先收起再重画：surfaceHide 的回调在进度条真正消失后才跑，
        // 这时重画地图才量得到腾出来的高度，不会按旧尺寸留一道空档。
        surfaceHide(panel, function () {
          drawMap(state.plan);
          renderTrips(state.plan);
        });
      });
      panel.appendChild(back);
      // 内容先填好再进场：surfaceShow 只管淡入上浮，不改高度（高度随内容长）。
      surfaceShow(panel);
    });
  }

  function render(plan, fromSnapshot) {
    if (fromSnapshot) {
      state.route = null;
      document.body.classList.remove('has-route');
      surfaceHide($('trip-route'));
    }
    state.plan = plan;
    buildTripLines(plan);
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
    surfaceHide($('trip-route'));
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

  // 这一站在第几趟里。趟是按地点分区排的（服务端 zone 分组），所以跨趟拖动存不住，
  // 要在松手那一刻就认出来，不能让它「挪好」了刷新又回去。
  function routineTripOf(plan, title) {
    if (!plan || !Array.isArray(plan.trips)) return -1;
    var name = String(title == null ? '' : title).trim();
    for (var ti = 0; ti < plan.trips.length; ti += 1) {
      var stops = plan.trips[ti].stops || [];
      for (var si = 0; si < stops.length; si += 1) {
        if (String(stops[si].title == null ? '' : stops[si].title).trim() === name) return ti;
      }
    }
    return -1;
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
    var groups = [];
    state.plan.trips.forEach(function (trip) {
      var names = [];
      (trip.stops || []).forEach(function (stop) {
        var name = String((stop && stop.title) == null ? '' : stop.title).trim();
        if (!name) return;
        names.push(name);
        if (order.indexOf(name) < 0) order.push(name);
      });
      if (names.length) groups.push(names);
    });
    return api(PLAN, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // groups 是趟边界：只发扁平站序的话，拖到别的趟里的站会被区域重新拆回去。
      body: JSON.stringify({
        action: 'order',
        date: String(state.plan.date || ''),
        order: order,
        groups: groups
      }),
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

  // 摆完之后站序没变（轻点的常做事项本来就在最后一趟的末尾），就别报「已经挪好」：
  // 用户点了没看见任何变化，提示却说好了，等于骗人。
  function planTitles(plan) {
    var trips = plan && Array.isArray(plan.trips) ? plan.trips : [];
    return trips.map(function (trip) {
      return (trip.stops || []).map(function (stop) {
        return String((stop && stop.title) == null ? '' : stop.title).trim();
      }).join('|');
    }).join('/');
  }

  // blocked：落点在别的趟里。跨趟挪的站刷新后会回自己那一区，所以当场拒绝，
  // 不重画、不发 POST —— 宁可说清「这一下不算数」，也别先答应再反悔。
  function dropRoutine(title, slot, blocked) {
    if (!state.plan) return Promise.resolve(false);
    if (blocked) {
      orderNote('「' + title + '」只能在同一趟里调整顺序；跨趟要挪，得去待办里改地点。');
      return Promise.resolve(false);
    }
    var before = planTitles(state.plan);
    var moved = slot
      ? moveRoutineStop(state.plan, title, slot.trip, slot.index)
      : appendRoutineStop(state.plan, title);
    if (!moved) {
      orderNote('「' + title + '」今天没排进行程，先把这一站加进来再摆位置。');
      return Promise.resolve(false);
    }
    if (planTitles(state.plan) === before) {
      orderNote('「' + title + '」已经在那一趟的最后了，没动。');
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
  var MAP_KEYS = '/api/agent/map/keys';
  // 配置里没给瓦片源时的兜底，跟后端 OSM_TILE_DEFAULT 保持一致（真正的值以 conf.osm.tile 为准）。
  var OSM_TILE_DEFAULT = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
  var POI_SAVE = '/api/agent/campus/poi';
  // （校区路网快照见 js/campus-osm.js：window.NH_CAMPUS_OSM）
  var MODES = [
    // 自绘也画 OSM 的路网和楼位，署名跑不掉（ODbL）；虚线是估算位置，写在署名后面当图例。
    { id: 'sketch', label: '自绘',
      credit: '© OpenStreetMap contributors, ODbL 1.0 · 虚线=位置估算' },
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

  // 取一档的名字/署名：先看注册表（第三方 register 进来的），没有才回落内置 MODES，
  // 都没有就返回空对象。refreshModes 的 title 和 credit 都靠它取名——否则第三方注册了
  // 也只在注册表里躺着，界面上点不到、也取不到署名。
  function modeSpec(id) {
    return getProvider(id) || MODES.filter(function (m) { return m.id === id; })[0] || {};
  }

  function modeBar() {
    var box = mapWrap();
    if (!box) return null;
    var bar = box.querySelector('.map-modes');
    if (bar) return bar;
    bar = document.createElement('div');
    bar.className = 'map-modes';
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', '底图与路线');
    MODES.forEach(function (m) {
      var button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('data-map-mode', m.id);
      button.textContent = m.label;
      bar.appendChild(button);
    });
    // 第三方 register 进来的底图也排进这排按钮，否则注册了界面上点不到。
    // 只补不在内置 MODES 里的 id，内置那四个不重复画。名字缺了就用 id 兜底。
    listProviders().forEach(function (spec) {
      if (!spec || !spec.id) return;
      var known = MODES.filter(function (m) { return m.id === spec.id; })[0];
      if (known) return;
      var button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('data-map-mode', spec.id);
      button.textContent = spec.label || spec.id;
      bar.appendChild(button);
    });
    // 路线怎么走，跟底图是两件事，分成单独一组并加一道竖线：
    // 沿路 = 绕楼、贴路的折线，直线 = 楼与楼直连（拿不准的时候能一键切回来看清楚）。
    bar.appendChild(Object.assign(document.createElement('i'), { className: 'map-modes-gap' }));
    [['road', '绕楼'], ['straight', '直线']].forEach(function (item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('data-map-line', item[0]);
      button.textContent = item[1];
      // 别写「沿路」：校园快照里只有外圈和几条干道，楼与楼之间一条人行道都没有，
      // 后端能做的只是绕开楼体（实测绕行 1.2–2.1 倍），写成沿路等于骗人。
      button.title = item[0] === 'road' ? '绕开楼体走（校园路网只有主干道，楼间没有人行道）' : '楼与楼直接连一条线';
      bar.appendChild(button);
    });
    bar.addEventListener('click', function (event) {
      var lineButton = event.target.closest('[data-map-line]');
      if (lineButton) {
        var want = lineButton.getAttribute('data-map-line');
        try { localStorage.setItem('nh_map_line', want); } catch (e) {}
        refreshModes();
        if (state.plan) drawMap(state.plan);
        return;
      }
      var button = event.target.closest('[data-map-mode]');
      if (!button) return;
      if (button.disabled) {
        // 禁用的按钮不派发 click：用 pointerdown 兜不住，这里靠父节点的点击冒泡也到不了，
        // 所以禁用项不真禁用按钮，改用 aria-disabled + 这里解释一句。
        return;
      }
      choose(button.getAttribute('data-map-mode'));
    });
    // 「我在哪」：选一处地点就按它算起点，路线、时刻、米数一起重排。
    // 用原生 select 而不是自绘下拉——手机上系统选择器触控面积够，也不用另写键盘操作。
    bar.appendChild(Object.assign(document.createElement('i'), { className: 'map-modes-gap' }));
    var pick = document.createElement('select');
    pick.id = 'map-here-select';
    pick.className = 'map-here-select';
    pick.title = '你现在在哪栋楼：选了就按这儿算路线';
    pick.setAttribute('aria-label', '我现在在哪');
    pick.addEventListener('change', function () {
      var value = pick.value;
      if (!value) return;
      setHere(value);
    });
    bar.appendChild(pick);
    fillHere();
    box.appendChild(bar);
    // 早先建好的缩放键和「＋地点」也收进来：它们原来浮在画布右上角，挡住半张图。
    ['.map3d-zoom', '.map-add'].forEach(function (sel) {
      var node = box.querySelector(sel);
      if (node) bar.appendChild(node);
    });
    return bar;
  }

  // 地点是后加载的（plan 拿到才有），所以选项每次重画地图时补一遍。
  function fillHere() {
    var pick = $('map-here-select');
    if (!pick) return;
    var plan = state.plan || {};
    var pois = Array.isArray(plan.pois) ? plan.pois : [];
    if (!pois.length) return;
    var here = plan.here || null;
    var current = here ? 'place:' + here.name : '';
    // 选项没变就别重建：重建会把正在展开的手机选择器关掉。
    var signature = current + '|' + pois.map(function (p) { return p.name; }).join(',');
    if (pick.getAttribute('data-signature') === signature) return;
    pick.setAttribute('data-signature', signature);
    var options = ['<option value="">我在…</option>'];
    pois.forEach(function (p) {
      options.push('<option value="place:' + esc(p.name) + '">' + esc(p.name) + '</option>');
    });
    pick.innerHTML = options.join('');
    pick.value = current;
  }

  // 标「我在哪」走的是同一份计划接口：服务端重算完再拿它重画，页面自己不猜。
  function setHere(value) {
    var place = value.replace(/^place:/, '');
    if (!place) return Promise.resolve(false);
    return api(PLAN, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'here', place: place }),
      timeoutMs: 15000
    }).then(function (body) {
      if (body && body.plan) render(body.plan);
      var here = body && body.plan && body.plan.here;
      orderNote(here ? '起点换成「' + here.name + '」，路线按这儿重排了。' : '起点标上了。');
      return true;
    }, function (error) {
      orderNote('位置没标上（' + (error && error.message ? error.message : '连不上服务') + '）：起点还是原来的。');
      return false;
    });
  }

  function refreshModes() {
    var bar = modeBar();
    var box = mapWrap();
    if (!bar || !box) return;
    var conf = mode.conf || {};
    // 晚于建栏才 register 进来的第三方：modeBar 只在建栏时遍历一次注册表，这里补一次，
    // 把注册表里还没有按钮的档追加进去（内置四档 + 已画过的跳过）。否则注册了界面上点不到。
    listProviders().forEach(function (spec) {
      if (!spec || !spec.id) return;
      if (bar.querySelector('[data-map-mode="' + spec.id + '"]')) return;
      if (MODES.some(function (m) { return m.id === spec.id; })) return;
      var button = document.createElement('button');
      button.type = 'button';
      button.setAttribute('data-map-mode', spec.id);
      button.textContent = spec.label || spec.id;
      bar.appendChild(button);
    });
    bar.querySelectorAll('[data-map-mode]').forEach(function (button) {
      var id = button.getAttribute('data-map-mode');
      var ready = id === 'sketch' || id === 'osm' ||
        !!(conf[id] && conf[id].enabled === true && conf[id].key);
      button.disabled = false;
      button.setAttribute('aria-disabled', ready ? 'false' : 'true');
      button.classList.toggle('is-off', !ready);
      button.setAttribute('aria-pressed', id === mode.id ? 'true' : 'false');
      button.title = ready ? modeSpec(id).credit || ''
        : '还没配 key，点一下填 Key（也可先用自绘或 OSM）';
    });
    var currentLine = lineMode();
    bar.querySelectorAll('[data-map-line]').forEach(function (button) {
      button.setAttribute('aria-pressed', button.getAttribute('data-map-line') === currentLine ? 'true' : 'false');
    });
    var credit = box.querySelector('.map-credit');
    var current = modeSpec(mode.id);
    if (current && current.credit) {
      if (!credit) {
        credit = document.createElement('span');
        credit.className = 'map-credit';
        // 署名塞进「校园示意…」那一段里，不自己占一条绝对定位的底边：
        // 挂在 wrap 上要跟图例、说明条抢同一块地方（1280、1100、390 三个宽度都和右侧图例叠成一团），
        // 挂在画布上又会被 map3d 每帧重建子节点时抹掉。
        (box.querySelector('.trip-map-note') || box).appendChild(credit);
      }
      // 前面那个分隔点由 CSS 的 ::before 画，别写进文字里：文本会被拼进段落的可读文字。
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

  // 校区经纬度基准：取自 OSM 快照的西北角，跟 campus-geo.json 是同一套投影
  // （0.9 米/像素、x 向东、y 向南）。快照是静态脚本（js/campus-osm.js），没到就等于
  // 没有基准，在线档如实说不出来。
  // 这里不再走 calibration.osmToPoi：那个换算只有各轴缩放没有旋转，套上去整片北移约 290 米。
  function geo() {
    if (mode.geo) return Promise.resolve(mode.geo);
    var data = window.NH_CAMPUS_OSM;
    if (!data || !data.origin || !Number.isFinite(data.origin.lat)) {
      return Promise.reject(new Error('还缺校区经纬度基准，先看自绘或 OSM'));
    }
    mode.geo = {
      lat: data.origin.lat, lon: data.origin.lon,
      mpp: Number(data.metersPerPixel) || 0.9
    };
    return Promise.resolve(mode.geo);
  }

  var M_PER_DEG = 111320;
  // 快照像素 → 经纬度（campus-geo.json 的 projection 就是这一条式子）。
  function pxToWgs(x, y) {
    var g = mode.geo;
    return {
      lat: g.lat - (y * g.mpp) / M_PER_DEG,
      lon: g.lon + (x * g.mpp) / (M_PER_DEG * Math.cos(g.lat * Math.PI / 180))
    };
  }
  function wgsToPx(lat, lon) {
    var g = mode.geo;
    return {
      x: ((lon - g.lon) * M_PER_DEG * Math.cos(g.lat * Math.PI / 180)) / g.mpp,
      y: ((g.lat - lat) * M_PER_DEG) / g.mpp
    };
  }

  // ── 平面坐标 ⇄ 快照像素 ───────────────────────────────────────────────
  // POI 里存的 x/y 是「平面图坐标」（画示意地图那套），而经纬度基准是挂在
  // 快照像素平面上的。两者差一个带旋转的仿射变换，系数只活在 campus-geo.json
  // 里（planToSnapshot.a/b/tx/ty），这里不复制数值，缺了就按恒等回退。
  var planTf = null;
  var planTfReady = null;

  function loadPlanTf() {
    if (planTfReady) return planTfReady;
    planTfReady = new Promise(function (resolve) {
      var data = window.NH_CAMPUS_GEO;
      if (!data || !data.planToSnapshot) {
        return fetch('js/campus-geo.json')
          .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
          .then(function (json) { resolve(json && json.planToSnapshot ? json : null); })
          .catch(function () { resolve(null); });
      }
      resolve(data);
    }).then(function (data) {
      var t = data && data.planToSnapshot;
      if (t && Number.isFinite(t.a) && Number.isFinite(t.b) && Number.isFinite(t.tx) && Number.isFinite(t.ty)) {
        planTf = t;
      }
      if (!planTf) console.warn('[nhTrip] campus-geo.json 缺 planToSnapshot，平面坐标按快照像素用');
      return planTf;
    });
    return planTfReady;
  }

  function planToSnapshot(x, y) {
    var t = planTf;
    if (!t) return { x: x, y: y };
    return { x: t.a * x - t.b * y + t.tx, y: t.b * x + t.a * y + t.ty };
  }

  function snapshotToPlan(sx, sy) {
    var t = planTf;
    if (!t) return { x: sx, y: sy };
    // [[a,-b],[b,a]] 的逆：det = a²+b²，旋转+缩放永远可逆。
    var det = t.a * t.a + t.b * t.b;
    var dx = sx - t.tx, dy = sy - t.ty;
    return { x: (t.a * dx + t.b * dy) / det, y: (t.a * dy - t.b * dx) / det };
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
      // 4 秒：验收口径是「6 秒内回退并提示」，这里给脚本加载和重绘留出余量。
      // 本机连不上 tile.openstreetmap.org 时，用户不该盯着一块空白的「正在连…」等下去。
      window.setTimeout(function () {
        if (done) return;
        done = true;
        try { cleanup(); } catch (e) {}
        reject(new Error(message));
      }, 4000);
    });
  }

  // ── 底图供应商接口（window.nhMapProviders）────────────────────────────
  // 别人接自己的地图（例如一张 plain-image 校区图、天地图、自建瓦片）：引一段 JS 调
  // nhMapProviders.register({...}) 即可，这里所有换档只认注册表，不写死任何一家。
  // spec = { id, label, credit, needsKey, init(el, ctx) -> Promise<adapter> }
  //   ctx = { tile, key, secret, pois, geo, proj:{ wgsToGcj, gcjToBd } }
  // adapter = { kind,                          // 该档的坐标系名：'osm'|'amap'|'baidu'|…
  //             toPlan(clientX, clientY) -> {x,y},   // 容器像素 → 平面坐标
  //             fromPlan(x, y) -> {lat,lon},         // 平面坐标 → 该档经纬度
  //             toScreen(x, y) -> {x,y},             // 平面坐标 → 容器像素（画路线/楼点用）
  //             frame(latlngs), onMove(cb), destroy() }
  // 注：不列 setMarkers —— 三内置适配器都实现成空函数、shell 也从不调它，
  // POI/「我在哪」由 shell 用 toScreen 自己画在覆盖层上，接入方不必实现。
  // 坐标口径：adapter.fromPlan 返回该档自家的经纬度（OSM=WGS-84，高德=GCJ-02，百度=BD-09），
  // 平面 ⇄ 各家坐标系的换算由宿主 toMode/fromMode 统一做，第三方不用自己管 GCJ/BD 偏移。
  var providers = {};
  var providerOrder = [];

  function registerProvider(spec) {
    if (!spec || !spec.id || typeof spec.init !== 'function') return false;
    if (!providers[spec.id]) providerOrder.push(spec.id);
    providers[spec.id] = spec;
    return true;
  }

  function getProvider(id) {
    return providers[id] || null;
  }

  function listProviders() {
    return providerOrder.map(function (id) { return providers[id]; });
  }

  window.nhMapProviders = {
    register: registerProvider,
    get: getProvider,
    list: listProviders
  };

  // 每档取自己的那份配置：高德/百度认 key/secret，OSM 认 tile。
  // 之前写死 key:''，导致「配了 key 也判得出 ready、真去连却拿不到 key」——
  // 永远弹「还没配 key」。这里按 id 从 mode.conf 里翻自己那档。
  function providerCtx(id) {
    var conf = mode.conf || {};
    var mine = conf[id] || {};
    return {
      tile: mine.tile || (conf.osm && conf.osm.tile) || OSM_TILE_DEFAULT,
      key: mine.key || '',
      secret: mine.secret || '',
      pois: (state.plan && state.plan.pois) || [],
      geo: mode.geo, proj: { wgsToGcj: wgsToGcj, gcjToBd: gcjToBd }
    };
  }

  function makeMap(id, el) {
    var spec = getProvider(id);
    if (!spec) return Promise.reject(new Error('没有这一档'));
    return spec.init(el, providerCtx(id));
  }

  // 各档的 frame 都要先过一次「平面 → 自家经纬度」，收在这里别写三遍。
  function framePlan(adapter, points) {
    var pts = [];
    (points || []).forEach(function (p) {
      var at = adapter.fromPlan(p.x, p.y);
      if (at && Number.isFinite(at.lat) && Number.isFinite(at.lon)) pts.push(at);
    });
    if (pts.length) adapter.frame(pts);
  }

  registerProvider({
    id: 'osm', label: 'OSM', credit: '© OpenStreetMap contributors', needsKey: false,
    init: function (el, ctx) {
      return loadLeaflet().then(function () {
        var map = window.L.map(el, {
          // 缩放用页面自己那套 +/−/⟲（js/map3d.js 的 zoomBox），Leaflet 的关掉，
          // 不然在线档会冒出两套缩放控件。署名控件留着 —— OSM 条款要求显示 attribution。
          zoomControl: false, attributionControl: true, scrollWheelZoom: true, minZoom: 3, maxZoom: 19
        });
        window.L.tileLayer(ctx.tile, {
          maxZoom: 19, attribution: '© OpenStreetMap contributors'
        }).addTo(map);
        return waitFirst(map, ['tileload'], 'OSM 瓦片没连上，先用示意地图', function () {
          map.remove();
        }).then(function () { return {
          kind: 'osm',
          toPlan: function (cx, cy) {
            var ll = map.containerPointToLatLng([cx, cy]);
            return fromMode({ lat: ll.lat, lon: ll.lng }, 'osm');
          },
          fromPlan: function (x, y) { return toMode({ x: x, y: y }, 'osm'); },
          toScreen: function (x, y) {
            var p = toMode({ x: x, y: y }, 'osm');
            var q = map.latLngToContainerPoint(window.L.latLng(p.lat, p.lon));
            return { x: q.x, y: q.y };
          },
          frame: function (pts) {
            var bounds = window.L.latLngBounds(pts.map(function (p) {
              return window.L.latLng(p.lat, p.lon);
            }));
            map.fitBounds(bounds, { padding: [18, 18], animate: false });
          },
          setMarkers: function () {},
          onMove: function (cb) { map.on('move zoom resize', cb); },
          destroy: function () { map.remove(); }
        }; });
      });
    }
  });

  registerProvider({
    id: 'amap', label: '高德', credit: '© 高德地图', needsKey: true,
    init: function (el, ctx) {
      if (!ctx.key) throw new Error('还没配 key，先看自绘或 OSM');
      window._AMapSecurityConfig = { securityJsCode: ctx.secret || '' };
      return loadScript('https://webapi.amap.com/maps?v=2.0&key=' + encodeURIComponent(ctx.key),
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
          toPlan: function (cx, cy) {
            var p = map.containerToLngLat(new window.AMap.Pixel(cx, cy));
            return fromMode({ lat: p.getLat(), lon: p.getLng() }, 'amap');
          },
          fromPlan: function (x, y) { return toMode({ x: x, y: y }, 'amap'); },
          toScreen: function (x, y) {
            var p = toMode({ x: x, y: y }, 'amap');
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
          setMarkers: function () {},
          onMove: function (cb) { map.on('mapmove', cb); map.on('zoomend', cb); map.on('moveend', cb); },
          destroy: function () { map.destroy(); }
        }; });
      });
    }
  });

  registerProvider({
    id: 'baidu', label: '百度', credit: '© 百度地图', needsKey: true,
    init: function (el, ctx) {
      if (!ctx.key) throw new Error('还没配 key，先看自绘或 OSM');
      // 走 getscript 而不是 api?type=webgl：后者只是一层 document.write 包装，
      // 异步插进去的脚本里 document.write 是空操作，BMapGL 永远不出现。
      return loadScript('https://api.map.baidu.com/getscript?type=webgl&v=1.0&ak=' +
        encodeURIComponent(ctx.key), function () { return !!window.BMapGL; }).then(function () {
        var map = new window.BMapGL.Map(el, { enableMapClick: false });
        map.centerAndZoom(new window.BMapGL.Point(mode.geo.lon, mode.geo.lat), 16);
        map.enableScrollWheelZoom(true);
        return waitFirst(map, ['tilesloaded'], '百度地图没出图，先用示意地图', function () {
          el.innerHTML = '';
        }).then(function () { return {
          kind: 'baidu',
          toPlan: function (cx, cy) {
            var p = map.pixelToPoint(new window.BMapGL.Pixel(cx, cy));
            return fromMode({ lat: p.lat, lon: p.lng }, 'baidu');
          },
          fromPlan: function (x, y) { return toMode({ x: x, y: y }, 'baidu'); },
          toScreen: function (x, y) {
            var p = toMode({ x: x, y: y }, 'baidu');
            var q = map.pointToOverlayPixel(new window.BMapGL.Point(p.lon, p.lat));
            return { x: q.x, y: q.y };
          },
          frame: function (pts) {
            map.setViewport(pts.map(function (p) { return new window.BMapGL.Point(p.lon, p.lat); }));
          },
          setMarkers: function () {},
          onMove: function (cb) {
            map.addEventListener('moveend', cb);
            map.addEventListener('zoomend', cb);
            map.addEventListener('moving', cb);
            map.addEventListener('zooming', cb);
          },
          destroy: function () { el.innerHTML = ''; }
        }; });
      });
    }
  });

  // 同一把校区坐标，按各档的坐标系换过去再交给 SDK。
  // 入参是平面坐标（POI x/y），先过 planToSnapshot 变成快照像素，才进经纬度基准。
  function toMode(px, kind) {
    var snap = planToSnapshot(px.x, px.y);
    var wgs = pxToWgs(snap.x, snap.y);
    var gcj = wgsToGcj(wgs.lat, wgs.lon);
    if (kind === 'amap') return gcj;
    if (kind === 'baidu') return gcjToBd(gcj.lat, gcj.lon);
    return wgs;
  }
  // fromMode 是 toMode 的严格逆：GCJ/BD → WGS-84 → 快照像素 → 平面坐标。
  // 加点/取点把点击落回平面，顺序反了地点就会错位到别的楼。
  function fromMode(point, kind) {
    var wgs;
    if (kind === 'amap') {
      wgs = gcjToWgs(point.lat, point.lon);
    } else if (kind === 'baidu') {
      var gcj = bdToGcj(point.lat, point.lon);
      wgs = gcjToWgs(gcj.lat, gcj.lon);
    } else {
      wgs = point;
    }
    var snap = wgsToPx(wgs.lat, wgs.lon);
    return snapshotToPlan(snap.x, snap.y);
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
    var ctx = overlay.ctx;
    var w = overlay.canvas.width / (Math.min(2, window.devicePixelRatio || 1));
    var h = overlay.canvas.height / (Math.min(2, window.devicePixelRatio || 1));
    ctx.clearRect(0, 0, w, h);
    var pois = state.plan.pois || [];
    if (!pois.length) return;
    var byId = {};
    pois.forEach(function (p) { byId[p.id] = p; });
    // 路线走后端给的绕楼折线（trip.line，平面坐标）：只有它贴着路网走。
    // 某趟没有 line（老数据）才退化成站与站直连。
    var segs = [];
    var dots = [];
    (state.plan.trips || []).forEach(function (trip) {
      var order = (trip.stops || []).map(function (s) { return byId[s.place_id]; })
        .filter(function (p) { return p; });
      order.forEach(function (poi) { dots.push(poi); });
      if (trip.line && trip.line.length > 1) segs.push(trip.line);
      else if (order.length > 1) segs.push(order.map(function (p) { return [p.x, p.y]; }));
    });
    ctx.strokeStyle = token('--map-road2') || '#9A7B2E';
    ctx.lineWidth = 3;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    segs.forEach(function (seg) {
      seg.forEach(function (xy, i) {
        var at = adapter.toScreen(xy[0], xy[1]);
        if (i) ctx.lineTo(at.x, at.y); else ctx.moveTo(at.x, at.y);
      });
    });
    ctx.stroke();
    var pts = dots.map(function (poi) {
      var at = adapter.toScreen(poi.x, poi.y);
      return { poi: poi, x: at.x, y: at.y };
    });
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
    positionHere(state.plan);
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
      // 平面→快照的变换系数在 campus-geo.json 里，跟经纬度基准一起先备好，
      // 不然第一笔画下去还没拿到系数，路线会先错一次再被 onMove 纠正。
      return loadPlanTf();
    }).then(function () {
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
          framePlan(adapter, framed);
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

  // ── 配 key 的小面板 ────────────────────────────────────────────────────
  // 点了高德/百度没 key、或 OSM 瓦片连不上时弹出来，填完直接存进数据目录再自动重连，
  // 不再把人晾在「还没配 key」一句提示上（那是原来的死路）。
  var keyPanel = null;
  var keyPanelStyle = false;

  // 面板样式由 trip.js 自己注入：page.html 的界面样式不归地图管，这里只管自己这一小块，
  // 输入框 16px 起（手机上再小会触发整页缩放）、整行铺满、44px 高（指尖点得准）。
  function ensureKeyPanelStyle() {
    if (keyPanelStyle) return;
    keyPanelStyle = true;
    var css = [
      // position:relative + z-index:3：面板要浮在在线底图层 .map-online（absolute inset:0
      // z-index:1）之上，不然 OSM 连不上时这块「换瓦片/取消」恢复面板被画布盖住，
      // 用户看到的就一直是灰底「正在连…」没有出路。工具栏已是 z-index:2，面板在其上。
      '.map-keys{display:flex;position:relative;z-index:3;flex-direction:column;gap:10px;flex:none;width:100%;',
      '  margin:0;padding:12px;border:0;border-top:1px solid var(--line,#ddd);border-radius:0;',
      '  background:var(--paper,#fff);font-size:16px;line-height:1.4;}',
      '.map-keys-sub{margin:0;color:var(--ink-2,#555);}',
      '.map-keys-field{display:flex;flex-direction:column;gap:4px;}',
      '.map-keys-field span{font-size:14px;color:var(--ink-2,#555);}',
      '.map-keys-field input{width:100%;min-height:44px;padding:8px 10px;font-size:16px;',
      '  border:1px solid var(--line,#ccc);border-radius:8px;background:var(--paper,#fff);color:inherit;}',
      '.map-keys-error{margin:0;color:var(--danger,#c0392b);font-size:14px;}',
      '.map-keys-row{display:flex;gap:8px;flex-wrap:wrap;}',
      '.map-keys-row button{min-height:44px;padding:8px 16px;font-size:16px;border-radius:8px;',
      '  border:1px solid var(--line,#ccc);background:var(--paper,#fff);color:inherit;cursor:pointer;}',
      '.map-keys-save{background:var(--accent,#2f6f4f);border-color:var(--accent,#2f6f4f);color:#fff;}',
      '.map-keys-save:disabled{opacity:.6;cursor:default;}',
      '.map-keys-hint{margin:0;font-size:13px;color:var(--ink-3,#888);}'
    ].join('');
    var style = document.createElement('style');
    style.setAttribute('data-nh-trip-keys', '');
    style.textContent = css;
    document.head.appendChild(style);
  }

  function closeKeyPanel() {
    if (keyPanel) { keyPanel.remove(); keyPanel = null; }
  }

  function openKeyPanel(id, subtitle) {
    var box = mapWrap();
    var bar = box && box.querySelector('.map-modes');
    if (!bar) return;
    closeKeyPanel();
    ensureKeyPanelStyle();
    var conf = mode.conf || {};
    var spec = getProvider(id) || {};
    var isOsm = id === 'osm';
    var providerConf = conf[id] || {};
    var panel = document.createElement('form');
    panel.className = 'map-keys';
    panel.setAttribute('aria-label', '配置' + (spec.label || id) + '底图');
    var text = '<p class="map-keys-sub"></p>';
    if (isOsm) {
      text += '<label class="map-keys-field"><span>瓦片地址</span>' +
        '<input name="tile" type="url" inputmode="url" autocomplete="off" spellcheck="false"' +
        ' placeholder="https://…/{z}/{x}/{y}.png"></label>';
    } else {
      text += '<label class="map-keys-field"><span>Key</span>' +
        '<input name="key" type="text" inputmode="text" autocomplete="off" spellcheck="false"' +
        ' placeholder="在' + (spec.label || id) + '开放平台申请的浏览器端 Key"></label>';
      if (id === 'amap') {
        text += '<label class="map-keys-field"><span>安全密钥（安全码）</span>' +
          '<input name="secret" type="text" inputmode="text" autocomplete="off" spellcheck="false"' +
          ' placeholder="控制台里的 jscode，没有可留空"></label>';
      }
    }
    text += '<p class="map-keys-error" role="alert" hidden></p>' +
      '<div class="map-keys-row">' +
      '<button type="submit" class="map-keys-save">保存并连接</button>' +
      '<button type="button" class="map-keys-cancel">取消</button></div>' +
      '<p class="map-keys-hint">Key 只存在这台服务器的数据目录里，不上传、不进日志。</p>';
    panel.innerHTML = text;
    panel.querySelector('.map-keys-sub').textContent = subtitle ||
      ('填上' + (spec.label || id) + '的 Key 就能切到这档底图，也可以先用自绘或 OSM。');
    var tileInput = panel.querySelector('[name=tile]');
    if (tileInput) tileInput.value = providerConf.tile || (conf.osm && conf.osm.tile) || '';
    var keyInput = panel.querySelector('[name=key]');
    if (keyInput) keyInput.value = providerConf.key || '';
    var secretInput = panel.querySelector('[name=secret]');
    if (secretInput) secretInput.value = providerConf.secret || '';
    function fail(message) {
      var el = panel.querySelector('.map-keys-error');
      el.textContent = message;
      el.hidden = false;
    }
    panel.querySelector('.map-keys-cancel').addEventListener('click', function () {
      closeKeyPanel();
      note('');
    });
    panel.addEventListener('submit', function (event) {
      event.preventDefault();
      var payload = { action: 'keys', provider: id };
      if (isOsm) payload.tile = (tileInput.value || '').trim();
      else payload.key = (keyInput.value || '').trim();
      if (secretInput) payload.secret = (secretInput.value || '').trim();
      if (!isOsm && !payload.key) { fail('Key 不能为空'); return; }
      var save = panel.querySelector('.map-keys-save');
      save.disabled = true;
      api(MAP_KEYS, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        timeoutMs: 10000
      }).then(function () {
        mode.conf = null;            // 强制下一次 loadModeConf 重读，别拿旧配置判断
        closeKeyPanel();
        return loadModeConf();
      }).then(function () {
        note('已保存，正在连接' + (spec.label || id) + '…');
        return choose(id);
      }, function (error) {
        save.disabled = false;
        fail((error && error.message) || '没存上，检查网络后再试');
      });
    });
    // 挂到画布外层容器（画布下方当横幅），不进 .map-modes：工具栏只放底图切换，
    // 表单塞进去会把 390 的工具栏从单行撑成两行（53→314）。
    box.appendChild(panel);
    keyPanel = panel;
    var focusInput = isOsm ? tileInput : keyInput;
    if (focusInput) {
      focusInput.focus();
      // 长瓦片地址在窄框里 focus 会把光标落到末尾、把开头的 http 滚出视野，
      // 看着像个坏 URL。复位选区到开头，让它左对齐显示全值。
      try { focusInput.setSelectionRange(0, 0); } catch (_) {}
      // 光标复位只在 layout 后才稳：浏览器 focus 完成后还会在布局/绘制阶段
      // 把字段滚到光标处，覆盖同步那次 setSelectionRange。再在下一帧复位一次。
      requestAnimationFrame(function () {
        try { focusInput.setSelectionRange(0, 0); } catch (_) {}
        focusInput.scrollLeft = 0;
      });
    }
  }

  function choose(id) {
    if (mode.busy) return;
    loadModeConf().then(function () {
      var conf = mode.conf || {};
      var spec = getProvider(id);
      var ready = !spec || !spec.needsKey || !!(conf[id] && conf[id].enabled && conf[id].key);
      if (!ready) {
        // 没配 key 不再是一句死提示：弹面板填，存完自动回来重连这一档。
        note('');
        openKeyPanel(id);
        return null;
      }
      if (id === 'sketch') {
        closeKeyPanel();
        teardownOnline();
        mode.id = 'sketch';
        saveMode();
        refreshModes();
        note('');
        if (state.plan) drawMap(state.plan);
        return null;
      }
      mode.busy = true;
      // 换档的这几秒旧瓦片已经拆掉、新瓦片还没到，署名不能再挂着上一家。
      var staleWrap = mapWrap();
      var staleCredit = staleWrap && staleWrap.querySelector('.map-credit');
      if (staleCredit) staleCredit.remove();
      note('正在连' + (modeSpec(id).label || id) + '…');
      return openOnline(id).then(function () {
        mode.id = id;
        saveMode();
        refreshModes();
        closeKeyPanel();
        note('');
      }).catch(function (error) {
        teardownOnline();
        mode.id = 'sketch';
        saveMode();
        refreshModes();
        if (state.plan) drawMap(state.plan);
        var message = (error && error.message) || '在线底图没连上，先用示意地图';
        note(message);
        // OSM 瓦片连不上时给出路：换个瓦片源就能用，直接把面板递到手上。
        if (id === 'osm' && message.indexOf('瓦片') >= 0) openKeyPanel(id, message + '：填一个能连上的瓦片地址，或先用自绘。');
      });
    }).then(function () {
      mode.busy = false;
    }, function () {
      mode.busy = false;
    });
  }

  // ── 可拖动的「我」标记 ─────────────────────────────────────────────────
  // 起点不再只能靠下拉框选：地图上这枚大头针按住就能拖，松手落到哪儿起点就到哪儿
  // （服务端重算、吸附到最近楼，页面自己不猜坐标）。针是一枚真按钮，键盘方向键也能挪，
  // 拖动时读出最近的楼和距离，落点仍然走同一份计划接口（save_here）。
  // 针挂在地图外层容器上，不进 #trip-map：自绘档每次 drawMap 都会把画布里清空重画，
  // 放进去会被抹掉；挂在容器上也吃不到 map3d 绑在画布上的旋转手势。
  var hereMarker = null;
  var hereDraft = null;     // 拖动/键盘挪动中、还没存回服务端的平面坐标
  var hereSaveTimer = 0;

  function markerHost() { return mapWrap(); }

  // 面板被压成扁带（对话态）时 drawMap 走的是行程条，画布上没有平面可指，
  // 这时候把针收起来，别留一个量不到位置的空壳。
  function boxTallEnough() {
    var host = $('trip-map');
    if (!host) return false;
    return host.getBoundingClientRect().height >= 200;
  }

  function markerVisible(plan) {
    return !!(plan && plan.here && Number.isFinite(plan.here.x) && Number.isFinite(plan.here.y)
      && !state.route && boxTallEnough());
  }

  // 平面坐标 → 外层容器里的像素位置（针的锚点是底尖，所以上移 44、左移 22 居中）。
  function hereScreen(plan) {
    var here = plan.here;
    if (mode.id !== 'sketch') {
      if (!mode.adapter) return null;
      return mode.adapter.toScreen(here.x, here.y);
    }
    var host = $('trip-map');
    if (!window.nhMap3d || !window.nhMap3d.at || !host) return null;
    return window.nhMap3d.at(host, here.x, here.y);
  }

  function ensureHereMarker() {
    if (hereMarker) return hereMarker;
    var box = markerHost();
    if (!box) return null;
    var marker = document.createElement('button');
    marker.type = 'button';
    marker.className = 'map-here-marker';
    marker.title = '按住拖动改起点；方向键微调（Shift 大步）';
    marker.setAttribute('aria-label', '我的位置，拖动可改起点');
    var dot = document.createElement('span');
    dot.className = 'map-here-dot';
    var tip = document.createElement('span');
    tip.className = 'map-here-tip';
    var hint = document.createElement('span');
    hint.className = 'map-here-hint';
    hint.setAttribute('role', 'status');
    hint.setAttribute('aria-live', 'polite');
    marker.appendChild(dot);
    marker.appendChild(tip);
    marker.appendChild(hint);
    marker.hidden = true;
    bindHereDrag(marker, hint);
    box.appendChild(marker);
    hereMarker = marker;
    return marker;
  }

  function positionHere(plan) {
    var marker = ensureHereMarker();
    if (!marker) return;
    if (!markerVisible(plan)) { marker.hidden = true; return; }
    var at = hereScreen(plan);
    if (!at || !Number.isFinite(at.x) || !Number.isFinite(at.y)) { marker.hidden = true; return; }
    marker.hidden = false;
    placeHereMarker(marker, at.x, at.y);
    var hint = marker.querySelector('.map-here-hint');
    if (hint) hint.textContent = hereHintText(plan.here.x, plan.here.y);
  }

  function placeHereMarker(marker, px, py) {
    marker.style.transform = 'translate(' + (px - 22).toFixed(1) + 'px,' + (py - 44).toFixed(1) + 'px)';
  }

  // 当前档位下「一平面单位 = 多少屏幕像素」：拿两个挨着的点投影到屏幕量一下。
  // 键盘挪动要靠它把屏幕位移换回平面坐标。
  function pixelsPerPlanUnit() {
    if (mode.id !== 'sketch') return 1; // 在线档 toPlan 吃的是容器像素，平面单位按 1 走
    var host = $('trip-map');
    if (!window.nhMap3d || !window.nhMap3d.at || !host) return 1;
    var plan = state.plan;
    var base = plan && plan.here ? plan.here : (plan && plan.pois && plan.pois[0]);
    if (!base) return 1;
    var a = window.nhMap3d.at(host, base.x, base.y);
    var b = window.nhMap3d.at(host, base.x + 10, base.y + 10);
    if (!a || !b) return 1;
    var span = Math.hypot(b.x - a.x, b.y - a.y);
    return span > 0.01 ? span / Math.hypot(10, 10) : 1;
  }

  function hereHintText(x, y) {
    var pois = (state.plan && state.plan.pois) || [];
    var best = null;
    var bestDist = Infinity;
    pois.forEach(function (p) {
      if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) return;
      var dist = Math.hypot(p.x - x, p.y - y) * geo().mpp;
      if (dist < bestDist) { bestDist = dist; best = p; }
    });
    if (!best) return '我的位置';
    return '最近：' + best.name + ' · ' + Math.round(bestDist) + ' 米';
  }

  function bindHereDrag(marker, hint) {
    var dragging = false;
    var draft = null;
    marker.addEventListener('pointerdown', function (event) {
      if (event.button !== undefined && event.button !== 0) return;
      dragging = true;
      try { marker.setPointerCapture(event.pointerId); } catch (e) {}
      marker.focus();
      event.preventDefault();
      event.stopPropagation();
    });
    marker.addEventListener('pointermove', function (event) {
      if (!dragging) return;
      var at = pickAt(event);
      if (!at || !Number.isFinite(at.x) || !Number.isFinite(at.y)) return;
      draft = { x: at.x, y: at.y };
      hereDraft = draft;
      var box = markerHost().getBoundingClientRect();
      placeHereMarker(marker, event.clientX - box.left, event.clientY - box.top);
      hint.textContent = hereHintText(at.x, at.y);
      event.stopPropagation();
    });
    function finish(event) {
      if (!dragging) return;
      dragging = false;
      var at = draft;
      draft = null;
      hereDraft = null;
      if (at) dropHere(at.x, at.y);
      if (event && event.pointerId !== undefined) {
        try { marker.releasePointerCapture(event.pointerId); } catch (e) {}
      }
    }
    marker.addEventListener('pointerup', finish);
    marker.addEventListener('pointercancel', finish);
    // 键盘也能挪：方向键 5px、Shift 25px，停 400ms 再存，连按不会一顿一顿打接口。
    marker.addEventListener('keydown', function (event) {
      var step = event.shiftKey ? 25 : 5;
      var dx = 0;
      var dy = 0;
      if (event.key === 'ArrowLeft') dx = -step;
      else if (event.key === 'ArrowRight') dx = step;
      else if (event.key === 'ArrowUp') dy = -step;
      else if (event.key === 'ArrowDown') dy = step;
      else return;
      event.preventDefault();
      nudgeHere(dx, dy, hint);
    });
  }

  function nudgeHere(dx, dy, hint) {
    var base = hereDraft || (state.plan && state.plan.here);
    if (!base || !hereMarker) return;
    var box = markerHost().getBoundingClientRect();
    var from = hereScreen({ here: base });
    var fromX = from ? from.x : base.__px || 0;
    var fromY = from ? from.y : base.__py || 0;
    var next = { x: base.x + dx / pixelsPerPlanUnit(), y: base.y + dy / pixelsPerPlanUnit() };
    hereDraft = next;
    placeHereMarker(hereMarker, fromX + dx, fromY + dy);
    hint.textContent = hereHintText(next.x, next.y);
    window.clearTimeout(hereSaveTimer);
    hereSaveTimer = window.setTimeout(function () {
      var at = hereDraft;
      hereDraft = null;
      if (at) dropHere(at.x, at.y);
    }, 400);
  }

  function dropHere(x, y) {
    return api(PLAN, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'here', x: Math.round(x * 10) / 10, y: Math.round(y * 10) / 10 }),
      timeoutMs: 15000
    }).then(function (body) {
      if (body && body.plan) render(body.plan);
      else if (state.plan) positionHere(state.plan);
    }, function (error) {
      orderNote('起点没挪过去（' + (error && error.message ? error.message : '连不上服务') + '）：先留在原地。');
      if (state.plan) positionHere(state.plan);
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
    var at = adapter.toPlan(event.clientX - box.left, event.clientY - box.top);
    if (!at || !Number.isFinite(at.x) || !Number.isFinite(at.y)) return null;
    var point = adapter.fromPlan(at.x, at.y);
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
  var pickCallback = null;   // 这次进取点态、点下去要交给谁（「＋地点」要名字，外部调用方走自己的回调）

  function stopPicking() {
    picking = false;
    downAt = null;
    pickCallback = null;
    var box = mapWrap();
    if (!box) return;
    box.classList.remove('is-picking');
    var button = box.querySelector('.map-add');
    if (button) {
      button.classList.remove('is-on');
      button.setAttribute('aria-pressed', 'false');
    }
  }

  // 进取点态：点一下地图，把落点交给 callback（「＋地点」要名字，行程区直接拿坐标）。
  // 「＋地点」和 window.nhMap.beginPick 走这一条路，别再各写一套取点。
  function beginPick(callback) {
    var box = mapWrap();
    if (!box) return;
    pickCallback = typeof callback === 'function' ? callback : null;
    picking = true;
    var button = box.querySelector('.map-add');
    if (button) {
      button.classList.add('is-on');
      button.setAttribute('aria-pressed', 'true');
    }
    box.classList.add('is-picking');
    note('在地图上点一下要加的位置');
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
      beginPick(addPlace);
    });
    // 跟缩放键一样搬进工具栏那一排，别压在画布上。
    var bar = box.querySelector('.map-modes');
    (bar || box).appendChild(button);
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
      if (event.target.closest && event.target.closest('.map-modes, .map-keys, .map3d-zoom, .map-add, .leaflet-control, .amap-controls')) return;
      var at = pickAt(event);
      if (!at || !Number.isFinite(at.x) || !Number.isFinite(at.y)) {
        note('这个位置取不到坐标，换一处点点');
        return;
      }
      if (pickCallback) pickCallback(at);
      else addPlace(at);
    }, true);
  }

  function boot() {
    if (!document.getElementById('trip-stage')) return;
    modeBar();
    refreshModes();
    addButton();
    bindPicking();
    // 3D 档转视角/缩放时 map3d 每次重画完会叫一声 onPaint：跟着把针挪回去，
    // 不然相机一动针就和地面脱开了（针的位置是按当时相机投出来的）。
    if (window.nhMap3d) {
      window.nhMap3d.onPaint = function () {
        if (state.plan && mode.id === 'sketch') positionHere(state.plan);
      };
    }
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
        var own = routineTripOf(state.plan, press.title);
        press.blocked = !!(press.slot && own >= 0 && press.slot.trip !== own);
        showLine(press.blocked ? null : press.slot);
      }
      function onChipUp(event) {
        if (!press || (event.pointerId !== undefined && event.pointerId !== press.id)) return;
        var done = press;
        press = null;
        chips.classList.remove('is-dragging');
        clearChipDrag();
        dropRoutine(done.title, done.dragging ? done.slot : null, done.dragging && done.blocked);
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
          slot: null,
          blocked: false
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
    // 「展开全部」按钮是每次重画时生成的新节点，事件只能挂在不会换掉的容器上。
    var restEl = $('trip-rest');
    if (restEl) {
      restEl.addEventListener('click', function (event) {
        var button = event.target.closest && event.target.closest('.trip-rest-more');
        if (!button) return;
        var listEl = button.previousElementSibling;
        if (!listEl) return;
        var open = listEl.classList.toggle('is-open');
        button.textContent = open ? '收起' : button.dataset.label;
        button.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
    }
    // type=module 是等解析完才执行的，可能比这页的普通脚本晚一步：3D 一到就补画一次。
    window.addEventListener('nh-map3d-ready', function () { if (state.plan) drawMap(state.plan); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  window.nhTrip = { reload: load, render: render, renderRoute: renderRoute };

  // 给行程区（和别的板块）复用的地图入口：取点、重画、把某处地点摆到画面中间。
  // 「＋地点」和外部调用方走同一条 beginPick 路径，不再各写一套取点。
  window.nhMap = {
    // 进取点态，点一下地图把落点交给 callback({x,y,lat,lon,crs})。opts 预留。
    beginPick: function (callback) { beginPick(callback); },
    endPick: function () { stopPicking(); },
    redraw: function () { if (state.plan) drawMap(state.plan); },
    // 第三方 register 之后重画底图那排按钮：modeBar 只在建栏时遍历一次注册表，
    // 晚于 boot 注册的档要显式刷一次才出现在界面上。
    refreshModes: function () { refreshModes(); },
    // 把某处地点挪到画面中心：在线档重设取景框，自绘档只平移取景中心（视角不动）。
    focus: function (placeId) {
      var plan = state.plan;
      if (!plan) return;
      var poi = null;
      (plan.pois || []).forEach(function (p) { if (p.id === placeId) poi = p; });
      if (!poi) return;
      if (mode.id !== 'sketch') {
        if (mode.adapter) framePlan(mode.adapter, [poi]);
        return;
      }
      var host = $('trip-map');
      if (window.nhMap3d && window.nhMap3d.focusAt && host) window.nhMap3d.focusAt(host, poi.x, poi.y);
    }
  };
})();





