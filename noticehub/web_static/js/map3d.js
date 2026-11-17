/* 校园地图的真 3D 版：three.js 把每栋楼按轮廓挤成体块，打一盏光、落下影子，拖一下转视角。
   只在有 WebGL 的时候接管；模块没到、显卡不支持，返回 false，trip.js 会照旧画它那套 SVG。
   数据只有一份：地点坐标来自 /api/agent/plan，尺寸表在 trip.js 里（window.nhMapModel）。 */
import * as THREE from './three.module.min.js';

var SHAPE = {
  boyaju: 'L', ligonglou: 'L', tuwen: 'H', library: 'L', admin: 'U',
  gym: 'oct', stadium: 'oval'
};
var PITCH = 0.62;
// 站号那个圆点的像素高（.map3d-pin）。楼名要让开它，见下面的 dy。
var PIN_H = 19;
var stores = new WeakMap();

function token(name) {
  return window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function sizeOf(p, model) {
  return model.size[p.kind] || [52, 34, 12];
}
function shapeOf(p) { return SHAPE[p.id] || 'rect'; }
function heightOf(p, model) {
  var shape = shapeOf(p);
  if (shape === 'oval' || p.kind === 'gate') return 0;
  // 真透视下高度按真实比例只有一两层楼厚，远看就是一摊纸片：按示意图口径放大，
  // 跟演示件里那套等距的观感对齐（那是投影自带的竖向放大）。
  return sizeOf(p, model)[2] * 2.4;
}

// 以 (0,0) 为中心的轮廓；跟 SVG 那套是同一组形状，只是这里交给 three 去挤。
function shapePoints(shape, w, d) {
  var hw = w / 2; var hd = d / 2; var t; var m; var c; var i; var pts = [];
  if (shape === 'oval') {
    for (i = 0; i < 24; i += 1) {
      var a = (i / 24) * Math.PI * 2;
      pts.push([Math.cos(a) * hw, Math.sin(a) * hd]);
    }
    return pts;
  }
  if (shape === 'oct') {
    c = Math.min(w, d) * 0.29;
    return [[-hw + c, -hd], [hw - c, -hd], [hw, -hd + c], [hw, hd - c],
      [hw - c, hd], [-hw + c, hd], [-hw, hd - c], [-hw, -hd + c]];
  }
  if (shape === 'L') {
    t = w * 0.44; m = d * 0.44;
    return [[-hw, -hd], [hw, -hd], [hw, -hd + m], [-hw + t, -hd + m],
      [-hw + t, hd], [-hw, hd]];
  }
  if (shape === 'U') {
    t = w * 0.24;
    return [[-hw, -hd + t], [-hw, hd], [hw, hd], [hw, -hd + t],
      [hw - t, -hd + t], [hw - t, -hd], [-hw + t, -hd], [-hw + t, -hd + t]];
  }
  if (shape === 'H') {
    t = w * 0.26; m = d * 0.26;
    return [[-hw, -hd], [-hw + t, -hd], [-hw + t, -m], [hw - t, -m],
      [hw - t, -hd], [hw, -hd], [hw, hd], [hw - t, hd], [hw - t, m],
      [-hw + t, m], [-hw + t, hd], [-hw, hd]];
  }
  return [[-hw, -hd], [hw, -hd], [hw, hd], [-hw, hd]];
}

function planOf(p, model) {
  var size = sizeOf(p, model);
  return shapePoints(shapeOf(p), size[0], size[1]).map(function (q) {
    return [p.x + q[0], p.y + q[1]];
  });
}

var webgl;
function hasWebGL() {
  if (webgl !== undefined) return webgl;
  try {
    var c = document.createElement('canvas');
    webgl = !!(c.getContext('webgl2') || c.getContext('webgl'));
  } catch (e) { webgl = false; }
  return webgl;
}

function killMat(m) {
  if (!m) return;
  if (Array.isArray(m)) { m.forEach(killMat); return; }
  if (typeof m.dispose === 'function') m.dispose();
}
function dispose(host) {
  var old = stores.get(host);
  if (old) {
    old.renderer.dispose();
    old.scene.traverse(function (node) {
      if (node.geometry && typeof node.geometry.dispose === 'function') node.geometry.dispose();
      killMat(node.material);
    });
    stores.delete(host);
  }
  host.innerHTML = '';
  delete host.dataset.map3d;
}

function putLabel(box, text, cls) {
  var el = document.createElement('span');
  el.className = 'map3d-label ' + cls;
  el.textContent = text;
  box.appendChild(el);
  return el;
}

function render(host, plan, opt, box, model) {
  dispose(host);
  var W = box.width; var Hh = box.height;
  var byId = {};
  (plan.pois || []).forEach(function (p) { byId[p.id] = p; });
  var origin = plan.origin ? byId[plan.origin.id] : null;
  var stops = [];
  (plan.trips || []).forEach(function (trip, ti) {
    (trip.stops || []).forEach(function (stop, si) {
      var poi = byId[stop.place_id];
      if (poi) stops.push({ poi: poi, trip: ti, n: stops.length + 1 });
    });
  });
  var liveOf = {};
  stops.forEach(function (item) { liveOf[item.poi.id] = item; });

  var framed = [];
  if (stops.length) {
    stops.forEach(function (item) { framed.push(item.poi); });
    if (origin) framed.push(origin);
  } else {
    framed = (plan.pois || []).slice();
  }
  if (!framed.length) return false;

  var cx = 0; var cz = 0;
  framed.forEach(function (p) { cx += p.x; cz += p.y; });
  cx /= framed.length; cz /= framed.length;
  var reach = 0;
  framed.forEach(function (p) {
    var size = sizeOf(p, model);
    reach = Math.max(reach, Math.hypot(p.x - cx, p.y - cz) + Math.max(size[0], size[1]) / 2);
  });
  reach = Math.max(reach, 40);

  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(34, W / Hh, 1, reach * 60);
  var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.setSize(W, Hh, false);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.domElement.setAttribute('aria-hidden', 'true');
  renderer.domElement.style.display = 'block';
  renderer.domElement.style.width = '100%';
  renderer.domElement.style.height = '100%';
  host.appendChild(renderer.domElement);

  var labelBox = document.createElement('div');
  labelBox.className = 'map3d-labels';
  host.appendChild(labelBox);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x9a9a8a, 1.15));
  var sun = new THREE.DirectionalLight(0xffffff, 1.5);
  sun.position.set(-reach * 0.7, reach * 1.6, reach * 0.5);
  sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  sun.shadow.camera.left = -reach * 1.6;
  sun.shadow.camera.right = reach * 1.6;
  sun.shadow.camera.top = reach * 1.6;
  sun.shadow.camera.bottom = -reach * 1.6;
  sun.shadow.camera.far = reach * 8;
  sun.shadow.bias = -0.0008;
  scene.add(sun);

  var ground = new THREE.Mesh(
    new THREE.PlaneGeometry(reach * 12, reach * 12),
    new THREE.MeshLambertMaterial({ color: new THREE.Color(token('--map-ground')) })
  );
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  // 落到 y = -0.6：操场、门口这些 0 高度的地块跟地面同高会打架闪面。
  ground.position.set(0, -0.6, 0);
  scene.add(ground);

  var topColour = new THREE.Color(token('--map-top'));
  var sideColour = new THREE.Color(token('--map-b'));
  var liveTop = new THREE.Color(token('--map-top-live'));
  var liveSide = new THREE.Color(token('--map-b-live'));
  var edgeColour = new THREE.Color(token('--ink'));

  // 取景用的点：框进来的每栋楼的底角与顶角。投影后按包围盒把镜头拉到刚好装满，
  // 不然按「最远半径」算距离，宽屏上有一半是空地。
  var framedIds = {};
  framed.forEach(function (p) { framedIds[p.id] = true; });
  var fitPts = [];

  (plan.pois || []).forEach(function (p) {
    var pts = planOf(p, model);
    var h = heightOf(p, model);
    var live = liveOf[p.id] !== undefined;
    var shape = new THREE.Shape(pts.map(function (q) {
      // rotateX(-90°) 把平面坐标 (x, y) 变成世界 (x, 0, -y)：所以这里写 -y，
      // 世界 z 才是 +y（y 向南、z 也向南）。写成 +y 会让整张图左右手反掉：
      // x 向东、y 向上、z 向北是个左手系，配 three.js 的右手相机就是一次镜像，
      // 转到哪个视角都掰不回来（楼群南北与卓越大道的走向会跟平面图对不上）。
      return new THREE.Vector2(q[0] - cx, -(q[1] - cz));
    }));
    var geo = h > 0
      ? new THREE.ExtrudeGeometry(shape, { depth: h, bevelEnabled: false })
      : new THREE.ShapeGeometry(shape);
    geo.rotateX(-Math.PI / 2);
    // 顶面一档亮、墙面一档暗；墙面之间靠那盏方向光自己分出明暗，不再另调。
    var mats = [
      new THREE.MeshLambertMaterial({ color: live ? liveTop : topColour }),
      new THREE.MeshLambertMaterial({ color: live ? liveSide : sideColour })
    ];
    var mesh = new THREE.Mesh(geo, mats);
    mesh.castShadow = h > 0;
    mesh.receiveShadow = true;
    // 描一圈边：两张同色系的纸叠在一起，没有边就看不出哪块是楼。
    if (h > 0) {
      mesh.add(new THREE.LineSegments(
        new THREE.EdgesGeometry(geo, 24),
        new THREE.LineBasicMaterial({ color: edgeColour, transparent: true, opacity: 0.34 })
      ));
    }
    scene.add(mesh);

    if (framedIds[p.id]) {
      var size = sizeOf(p, model);
      var hw = size[0] / 2; var hd = size[1] / 2;
      [[-1, -1], [1, -1], [1, 1], [-1, 1]].forEach(function (c) {
        var qx = p.x + c[0] * hw - cx;
        var qz = p.y + c[1] * hd - cz;
        fitPts.push(new THREE.Vector3(qx, 0, qz));
        fitPts.push(new THREE.Vector3(qx, h + 18, qz));
      });
    }
  });

  // 两趟路线，画在地面上一点点，免得跟楼面打架。
  (plan.trips || []).forEach(function (trip, ti) {
    var path = [];
    (trip.stops || []).forEach(function (stop) {
      var poi = byId[stop.place_id];
      if (poi) path.push(poi);
    });
    if (path.length < 2) return;
    var points = path.map(function (p) {
      return new THREE.Vector3(p.x - cx, 1.5, p.y - cz);
    });
    var line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(points),
      new THREE.LineBasicMaterial({ color: new THREE.Color(token(ti === 0 ? '--map-road1' : '--map-road2')) })
    );
    scene.add(line);
  });

  // 楼名和站号用 HTML 贴上去：WebGL 里画字要么糊要么得另做贴图，不如直接叠一层。
  var labels = [];
  (plan.pois || []).forEach(function (p) {
    var live = liveOf[p.id];
    // 窄屏只写跟这一趟有关的和几栋认路的楼，剩下的挤上去只会糊成一团。
    if (!live && W < 620 && model.named.indexOf(p.id) < 0) return;
    var h = heightOf(p, model);
    labels.push({
      el: putLabel(labelBox, p.name, live ? 'is-live' : ''),
      // 跟站号锚同一个点（楼顶上方 22），楼名再用 dy 抬到站号上头：
      // 以前两者都锚在楼顶，站号的框正好盖住楼名，「这一趟去哪栋」就只剩个数字。
      at: new THREE.Vector3(p.x - cx, h + (live ? 22 : 2), p.y - cz),
      weight: live ? 3 : 1,
      group: live ? p.id : null,
      dy: live ? -(PIN_H + 4) : 0
    });
  });
  var pins = stops.map(function (item) {
    var h = heightOf(item.poi, model);
    return {
      el: putLabel(labelBox, String(item.n), 'map3d-pin ' + (item.trip ? 'is-b' : 'is-a')),
      at: new THREE.Vector3(item.poi.x - cx, h + 22, item.poi.y - cz),
      weight: 3,
      group: item.poi.id
    };
  });

  var startAngle = 0;
  try {
    var saved = parseFloat(localStorage.getItem('nh_map_angle'));
    if (isFinite(saved)) startAngle = saved;
  } catch (e) { startAngle = 0; }

  var state = {
    renderer: renderer, scene: scene, camera: camera, labelBox: labelBox,
    labels: labels, pins: pins, host: host, angle: startAngle,
    target: new THREE.Vector3(0, 0, 0), reach: reach, fitPts: fitPts, W: W, Hh: Hh
  };
  stores.set(host, state);
  host.dataset.map3d = '1';
  bind(host);
  paint(state);
  return true;
}

// 需要多远才刚好装下：先在远处按同一方向摆一次相机，把取景点的 NDC 投影算出来，
// NDC 大小跟距离近似成反比，乘回去就是合适的距离。比按包围球拍脑袋准，也不怕转视角。
function distanceFor(state) {
  var far = state.reach * 2.5;
  var yaw = state.angle;
  state.camera.position.set(
    state.target.x + Math.sin(yaw) * Math.cos(PITCH) * far,
    state.target.y + Math.sin(PITCH) * far,
    state.target.z + Math.cos(yaw) * Math.cos(PITCH) * far
  );
  state.camera.lookAt(state.target);
  state.camera.updateMatrixWorld();
  var v = new THREE.Vector3();
  var m = 0;
  state.fitPts.forEach(function (p) {
    v.copy(p).project(state.camera);
    if (Math.abs(v.x) > m) m = Math.abs(v.x);
    if (Math.abs(v.y) > m) m = Math.abs(v.y);
  });
  if (!(m > 0)) return far;
  // 1.1 留一圈呼吸：楼名是贴着楼顶往上写的，装太满字会顶出画框。
  return far * m * 1.1;
}

function paint(state) {
  var dist = distanceFor(state);
  var yaw = state.angle;
  state.camera.position.set(
    state.target.x + Math.sin(yaw) * Math.cos(PITCH) * dist,
    state.target.y + Math.sin(PITCH) * dist,
    state.target.z + Math.cos(yaw) * Math.cos(PITCH) * dist
  );
  state.camera.lookAt(state.target);
  state.renderer.render(state.scene, state.camera);
  place(state, state.labels.concat(state.pins));
}

// 行程楼名和站号一起摆、一起让位，排不下时整组隐藏。
function place(state, items) {
  var v = new THREE.Vector3();
  var taken = [], groups = [], byGroup = {};
  items.slice().sort(function (a, b) { return b.weight - a.weight; }).forEach(function (item) {
    var group = item.group && byGroup[item.group];
    if (!group) { group = []; groups.push(group); if (item.group) byGroup[item.group] = group; }
    v.copy(item.at).project(state.camera);
    group.push({ item: item, x: (v.x * 0.5 + 0.5) * state.W,
      y: (-v.y * 0.5 + 0.5) * state.Hh + (item.dy || 0), z: v.z,
      w: item.w || (item.w = item.el.offsetWidth || 24),
      h: item.h || (item.h = item.el.offsetHeight || 12) });
  });
  groups.forEach(function (group) {
    var shifts = group[0].item.group ? [0, -18, -36, 18, 36, -54, 54] : [0];
    var placed = null;
    shifts.some(function (dy) {
      var boxes = group.map(function (p) { return [p.x - p.w / 2, p.y + dy - p.h, p.x + p.w / 2, p.y + dy]; });
      var fits = boxes.every(function (b, index) {
        return group[index].z <= 1 && b[0] >= 0 && b[2] <= state.W && b[1] >= 0 && b[3] <= state.Hh &&
          !taken.some(function (o) { return b[0] < o[2] && b[2] > o[0] && b[1] < o[3] && b[3] > o[1]; });
      });
      if (!fits) return false;
      placed = dy; taken = taken.concat(boxes); return true;
    });
    group.forEach(function (p) {
      p.item.el.style.transform = 'translate(' + p.x.toFixed(1) + 'px,' + (p.y + (placed || 0)).toFixed(1) + 'px) translate(-50%,-100%)';
      p.item.el.style.display = placed === null ? 'none' : '';
    });
  });
}

function bind(host) {
  if (host.__nhMap3dBound) return;
  host.__nhMap3dBound = true;
  var drag = null;
  host.addEventListener('pointerdown', function (event) {
    var state = stores.get(host);
    if (!state || event.button !== 0) return;
    drag = { x: event.clientX, angle: state.angle };
    host.classList.add('is-dragging');
    try { host.setPointerCapture(event.pointerId); } catch (e) { drag = null; }
  });
  host.addEventListener('pointermove', function (event) {
    var state = stores.get(host);
    if (!drag || !state) return;
    state.angle = drag.angle + (event.clientX - drag.x) * 0.008;
    paint(state);
  });
  function end() {
    var state = stores.get(host);
    if (!drag) return;
    drag = null;
    host.classList.remove('is-dragging');
    if (state) { try { localStorage.setItem('nh_map_angle', String(state.angle)); } catch (e) {} }
  }
  host.addEventListener('pointerup', end);
  host.addEventListener('pointercancel', end);
  host.addEventListener('dblclick', function () {
    var state = stores.get(host);
    if (!state) return;
    state.angle = 0;
    try { localStorage.setItem('nh_map_angle', '0'); } catch (e) {}
    paint(state);
  });
}

window.nhMap3d = {
  draw: function (host, plan, opt) {
    var model = window.nhMapModel;
    if (!host) return false;
    // 到这儿还没 3D，就把上一轮的画布和标记清掉：不留半张旧图，也别把拖动锁在 3D 那边。
    if (!model || !hasWebGL()) { dispose(host); return false; }
    var box = host.getBoundingClientRect();
    if (box.width < 120 || box.height < 120) { dispose(host); return false; }
    try {
      if (!render(host, plan, opt || {}, box, model)) { dispose(host); return false; }
      return true;
    } catch (e) {
      dispose(host);
      return false;
    }
  },
  dispose: dispose
};
window.dispatchEvent(new Event('nh-map3d-ready'));
