/* 校园地图的真 3D 版：three.js 把每栋楼按轮廓挤成体块，打一盏光、落下影子，拖一下转视角。
   只在有 WebGL 的时候接管；模块没到、显卡不支持，返回 false，trip.js 会照旧画它那套 SVG。
   数据只有一份：地点坐标来自 /api/agent/plan，尺寸表在 trip.js 里（window.nhMapModel）。 */
import * as THREE from './three.module.min.js';

// 键是地点表里的 id（campus.py）。写成不存在的 id 等于没写：运动场原来写成 stadium，
// 表里的 id 是 field，结果跑道中央立了一栋方楼。
var SHAPE = {
  boyaju: 'L', ligong: 'L', tuwen: 'H', library: 'L',
  gym: 'oct', field: 'oval'
};
var PITCH = 0.62;
// 站号那个圆点的像素高（.map3d-pin）。楼名要让开它，见下面的 dy。
var PIN_H = 19;
var stores = new WeakMap();
// 已经画着的容器：底图数据晚到、或者换主题时，按这张表把图重画一遍。
var hosts = [];
// 缩放：1 就是「刚好装下全校那圈楼」，往两边各留三档左右。
// 再小看不清楼，再大楼名一张嘴就没几栋了。重画（拉窗口、切页签）时保留。
var ZOOM_MIN = 0.55;
var ZOOM_MAX = 2.6;
var lastZoom = 1;
var lastAngle = null;

// ── 底图 ────────────────────────────────────────────────────────────
// 底图的几何来自 js/campus-osm.js 那份 OSM 快照（© OpenStreetMap contributors,
// ODbL 1.0）：校园边界、道路、绿地、水面、跑道。跟楼块同一套像素平面
// （0.9 米/像素、原点校区西北角、x 向东、y 向南），y 一律写正向，只有 toShape()
// 里那一次取反。
// 水面分两档：面积大的那片是校区外的湖，铺在校园底色**下面**（只露出校区之外的部分）；
// 小的当校内水塘，铺在校园底色**上面**。Overpass 把 multipolygon 的每个环单独给了一条，
// 逐条画会把整片宿舍区盖成蓝色——「左边直接变成水上乐园」就是这么来的。
var BIG_WATER = 20000;    // px²：超过这个当湖，压在校园底色底下
var TRACK_MIN = 20000;    // px²：运动场的跑道环（22209）比各个球场大一档

function osmGeo() { return window.NH_CAMPUS_OSM || null; }

// 路线走向哪一档：road = 后端给的绕楼折线（trip.line），straight = 楼与楼直连。
// 跟 trip.js 里那个开关共用同一个 localStorage 键，两边同时生效。
export function lineMode() {
  try {
    return localStorage.getItem('nh_map_line') === 'straight' ? 'straight' : 'road';
  } catch (e) {
    return 'road';
  }
}

// 一趟要画的折线（平面图坐标）。没有 line（老数据、纯直线档）就退回站与站直连。
function lineOf(trip, byId) {
  var line = trip && trip.line;
  if (lineMode() === 'road' && line && line.length >= 2) {
    return line.map(function (p) { return [p[0], p[1]]; });
  }
  var out = [];
  (trip && trip.stops ? trip.stops : []).forEach(function (stop) {
    var poi = byId && byId[stop.place_id];
    if (poi) out.push([poi.x, poi.y]);
  });
  return out;
}

function ringArea(pts) {
  var sum = 0;
  for (var i = 0; i < pts.length; i += 1) {
    var a = pts[i]; var b = pts[(i + 1) % pts.length];
    sum += a[0] * b[1] - b[0] * a[1];
  }
  return Math.abs(sum / 2);
}

// 点在不在这块面里（射线法）。路网要按它裁：91 条路的 bbox 一直伸到校区外几百米，
// 不裁就是几条大马路从湖面上横穿过去。
function inRing(pts, x, y) {
  var hit = false;
  for (var i = 0, j = pts.length - 1; i < pts.length; j = i, i += 1) {
    var a = pts[i]; var b = pts[j];
    if ((a[1] > y) !== (b[1] > y) &&
      x < ((b[0] - a[0]) * (y - a[1])) / (b[1] - a[1]) + a[0]) hit = !hit;
  }
  return hit;
}

// 一条路按「点在校内」切成几段，只留校内的那些（两段以上才画得出一条线）。
function clipRoad(ring, pts) {
  var runs = []; var run = [];
  pts.forEach(function (q) {
    if (inRing(ring, q[0], q[1])) { run.push(q); return; }
    if (run.length > 1) runs.push(run);
    run = [];
  });
  if (run.length > 1) runs.push(run);
  return runs;
}

// 道路按等级分三档：主干（双线）、支路（单线）、步行（更细更淡）。
var ROAD_WIDTH = {
  primary: 17, secondary: 15, tertiary: 13, trunk: 17,
  residential: 9, unclassified: 9, living_street: 8, service: 7,
  pedestrian: 8, footway: 5, cycleway: 5, path: 4, steps: 4, track: 6
};

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

// 平面点 (x 东, y 南) → three 的 Shape。Shape 在 XY 平面上，rotateX(-90°) 之后
// (x, y) 落到世界 (x, 0, -y)：所以这里喂 -y，世界 z 才等于 +y（南）。
// 全文件只有这一处取反，别处一律按 x 东、y 南正向写。
function toShape(points) {
  return new THREE.Shape(points.map(function (q) {
    return new THREE.Vector2(q[0], -q[1]);
  }));
}

// 平铺在地面上的一块：绿地、水面、跑道都是它。
function flatMesh(points, colour, holePoints) {
  var shape = toShape(points);
  if (holePoints) shape.holes.push(toShape(holePoints));
  var geo = new THREE.ShapeGeometry(shape);
  geo.rotateX(-Math.PI / 2);
  var mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({
    color: new THREE.Color(colour), side: THREE.DoubleSide
  }));
  mesh.receiveShadow = true;
  return mesh;
}

// 按折线铺一条带宽度的路面。linewidth 在 WebGL 里基本被忽略，道路只能自己拼三角形。
function ribbon(points, width, h) {
  var pos = [];
  for (var i = 0; i < points.length - 1; i += 1) {
    var ax = points[i][0]; var ay = points[i][1];
    var bx = points[i + 1][0]; var by = points[i + 1][1];
    var len = Math.hypot(bx - ax, by - ay);
    if (!len) continue;
    var nx = (-(by - ay) / len) * (width / 2);
    var ny = ((bx - ax) / len) * (width / 2);
    pos.push(ax + nx, h, ay + ny, ax - nx, h, ay - ny, bx + nx, h, by + ny);
    pos.push(bx + nx, h, by + ny, ax - nx, h, ay - ny, bx - nx, h, by - ny);
  }
  var geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.computeVertexNormals();
  return new THREE.Mesh(geo, new THREE.MeshLambertMaterial({
    color: new THREE.Color(token('--map-path')), side: THREE.DoubleSide
  }));
}

// 路线上的箭头：每段中点摆一个，方向是「从这一站到下一站」。
function arrowHead(from, to, colour, h, size) {
  var dx = to[0] - from[0]; var dz = to[1] - from[1];
  var len = Math.hypot(dx, dz) || 1;
  dx /= len; dz /= len;
  var mx = (from[0] + to[0]) / 2; var mz = (from[1] + to[1]) / 2;
  var px = -dz; var pz = dx;
  var geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute([
    mx + dx * size, h, mz + dz * size,
    mx - dx * size * 0.5 + px * size * 0.6, h, mz - dz * size * 0.5 + pz * size * 0.6,
    mx - dx * size * 0.5 - px * size * 0.6, h, mz - dz * size * 0.5 - pz * size * 0.6
  ], 3));
  geo.computeVertexNormals();
  return new THREE.Mesh(geo, new THREE.MeshBasicMaterial({
    color: new THREE.Color(colour), side: THREE.DoubleSide
  }));
}

function loopLine(points, h, colour, opacity) {
  var pts = points.map(function (q) { return new THREE.Vector3(q[0], h, q[1]); });
  return new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({ color: new THREE.Color(colour), transparent: opacity < 1, opacity: opacity }));
}

// 底图整块：边界、道路、绿地、水面、跑道，全部来自 OSM 快照。坐标系已经挪到相机中心
// (cx, cz)。高度按 -2 米上下错开：地面在 -3，楼从 0 起，谁都压不着谁，也不会闪面。
function drawBasemap(scene, cx, cz) {
  var data = osmGeo();
  if (!data) return;
  var ring = (data.campus && data.campus[0]) || [];
  if (ring.length < 3) return;
  var place = placer(scene, cx, cz);
  // 湖先铺在底下，校园底色盖住校内那一块
  data.water.forEach(function (w) {
    if (ringArea(w.pts) >= BIG_WATER) place(flatMesh(w.pts, token('--map-water'), null), -2.8);
  });
  place(flatMesh(ring, token('--map-campus'), null), -2.6);
  data.green.forEach(function (g) {
    place(flatMesh(g.pts, token('--map-green'), null), -2.5);
  });
  data.sport.forEach(function (s) {
    // 跑道和球场分开上色：跑道是砖红的一圈，球场就是块草地
    var track = s.kind === 'track' && ringArea(s.pts) >= TRACK_MIN;
    place(flatMesh(s.pts, token(track ? '--map-track' : '--map-green'), null), track ? -2.46 : -2.52);
  });
  data.water.forEach(function (w) {
    if (ringArea(w.pts) < BIG_WATER) place(flatMesh(w.pts, token('--map-water'), null), -2.4);
  });
  data.roads.forEach(function (road) {
    clipRoad(ring, road.pts).forEach(function (run) {
      drawRoad(scene, cx, cz, run, road.kind);
    });
  });
  place(loopLine(ring, 0, token('--map-edge'), 0.9), -1.8);
}

function placer(scene, cx, cz) {
  return function (mesh, h) {
    mesh.geometry.translate(-cx, h, -cz);
    scene.add(mesh);
    return mesh;
  };
}

// 一条路：主干先铺一道深色宽边再铺亮色窄芯，远处看就是「双线」；
// 支路和步道只铺一条，步道再细一档、淡一档。路口用圆补角，不然直角处会缺个口子。
function roadMesh(points, width, colour, opacity) {
  var mesh = ribbon(points, width, 0);
  mesh.material.color.set(colour);
  mesh.material.transparent = opacity < 1;
  mesh.material.opacity = opacity;
  mesh.material.depthWrite = false;
  return mesh;
}

function drawRoad(scene, cx, cz, points, kind) {
  var width = ROAD_WIDTH[kind] || 6;
  var main = width >= 13;
  var place = placer(scene, cx, cz);
  if (main) {
    place(roadMesh(points, width + 4, token('--map-road-line'), 1), -1.95);
    place(roadMesh(points, width, token('--map-road-fill'), 1), -1.9);
  } else {
    // 支路铺浅色（跟主干一样的路面色）：--map-path 跟校园底色只差一点点，铺上去等于没画；
    // --map-road-line 是描边用的土色，压在上面也糊。最窄的步道再透一点，免得盖过楼体。
    place(roadMesh(points, width, token('--map-road-fill'), width <= 5 ? 0.86 : 1), -2);
  }
  points.slice(1, -1).forEach(function (q) {
    var geo = new THREE.CircleGeometry(width / 2 + (main ? 2 : 0), 10);
    geo.rotateX(-Math.PI / 2);
    geo.translate(q[0], 0, q[1]);
    var disc = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({
      color: new THREE.Color(token('--map-road-fill')),
      side: THREE.DoubleSide,
      transparent: !main,
      opacity: main ? 1 : (width <= 5 ? 0.72 : 1)
    }));
    disc.material.depthWrite = main;
    place(disc, main ? -1.96 : -2.01);
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
  // 退回 SVG 时别把按钮留在那儿：那套图只认拖动，按了没反应比没有更糟。
  var box = host.parentNode && host.parentNode.querySelector('.map3d-zoom');
  if (box) box.hidden = true;
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
  // 「窄」说的是小画布，不是窄窗口：桌面右栏只有 400 宽却有 430 高，楼名照样摆得开。
  var compact = W < 420 && Hh < 300;
  var byId = {};
  (plan.pois || []).forEach(function (p) { byId[p.id] = p; });
  var stops = [];
  (plan.trips || []).forEach(function (trip, ti) {
    (trip.stops || []).forEach(function (stop, si) {
      var poi = byId[stop.place_id];
      if (poi) stops.push({ poi: poi, trip: ti, n: stops.length + 1 });
    });
  });
  var liveOf = {};
  stops.forEach(function (item) { liveOf[item.poi.id] = item; });

  // 取景按画出来的全部楼算，不是只按这一趟经过的那几栋：只框行程楼的话，边上的
  // 立功楼、立言楼照样画出来，却顶在面板右界外面被切掉半边。
  // （高度 <200px 的窄带走不到这里：trip.js 在那一档直接换行程条，不叫 3D。）
  var framed = (plan.pois || []).slice();
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
  // 落到 y = -3：底图那几层铺在 -2.4…-1.8，楼从 0 起，谁也不跟谁同高闪面。
  ground.position.set(0, -3, 0);
  scene.add(ground);
  drawBasemap(scene, cx, cz);

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
    // 运动场不立块：底图上那条 OSM 跑道就是它，再压一块同尺寸的方片只会把跑道盖住。
    if (h > 0) {
      var shape = toShape(pts.map(function (q) { return [q[0] - cx, q[1] - cz]; }));
      var geo = new THREE.ExtrudeGeometry(shape, { depth: h, bevelEnabled: false });
      geo.rotateX(-Math.PI / 2);
      // 顶面一档亮、墙面一档暗；墙面之间靠那盏方向光自己分出明暗，不再另调。
      var mats = [
        new THREE.MeshLambertMaterial({ color: live ? liveTop : topColour }),
        new THREE.MeshLambertMaterial({ color: live ? liveSide : sideColour })
      ];
      var mesh = new THREE.Mesh(geo, mats);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      // 描一圈边：两张同色系的纸叠在一起，没有边就看不出哪块是楼。
      // 位置是估算的（estimated / unknown）描虚线，别让人以为二十栋都是测绘值。
      var guess = p.src === 'estimated' || p.src === 'unknown';
      var edge = new THREE.LineSegments(
        new THREE.EdgesGeometry(geo, 24),
        guess
          ? new THREE.LineDashedMaterial({
            color: edgeColour, transparent: true, opacity: 0.55, dashSize: 7, gapSize: 5
          })
          : new THREE.LineBasicMaterial({ color: edgeColour, transparent: true, opacity: 0.34 })
      );
      if (guess) edge.computeLineDistances();
      mesh.add(edge);
      scene.add(mesh);
    }

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

  // 路线：一条带子铺在路上，中间摆箭头，站号在 pin 里（1、2、3…），一眼能看出先后。
  // 带子铺在 -0.4：压住底图的路面，又还在楼体（0 起）下面，像画在路面上的一笔。
  // 走向取后端的 `trip.line`（绕楼、贴路）；切到「直线」档就直接连楼与楼。
  (plan.trips || []).forEach(function (trip, ti) {
    var flat = lineOf(trip, byId);
    if (flat.length < 2) return;
    var colour = token(ti === 0 ? '--map-road1' : '--map-road2');
    // 12 个绘制单位：整校塞进两百来像素的面板时，7 个单位只有一像素宽，看不出走了哪条路。
    var band = ribbon(flat, 12, 0);
    band.material.color.set(colour);
    band.geometry.translate(-cx, -0.4, -cz);
    band.material.depthWrite = false;
    scene.add(band);
    for (var i = 0; i < flat.length - 1; i += 1) {
      var arrow = arrowHead(flat[i], flat[i + 1], colour, 0, 16);
      arrow.geometry.translate(-cx, -0.35, -cz);
      scene.add(arrow);
    }
  });

  // 楼名和站号用 HTML 贴上去：WebGL 里画字要么糊要么得另做贴图，不如直接叠一层。
  var labels = [];
  var namedShown = 0;
  (plan.pois || []).forEach(function (p) {
    var live = liveOf[p.id];
    var named = model.named.indexOf(p.id) >= 0;
    // 窄屏只写跟这一趟有关的和几栋认路的楼，剩下的挤上去只会糊成一团。
    if (!live && Hh < 300 && W < 620 && !named) return;
    // 手机宽度再收一层：路线 + 最多 4 栋认路的楼，其余靠点（canvas 只有两百来像素高）。
    if (!live && compact && namedShown >= 4) return;
    if (named && !live) namedShown += 1;
    var size = sizeOf(p, model);
    var h = heightOf(p, model);
    labels.push({
      el: putLabel(labelBox, p.name, live ? 'is-live' : ''),
      // 锚在楼顶靠镜头那一边，不是楼顶正中：正中那个点投影下来落在顶面里面，
      // 楼一大，楼名的底边就压住楼顶轮廓线（390 下的立行楼就是这样）。+y 是朝镜头。
      // 站号也放在这条边上，楼名再用 dy 抬到站号上头：
      // 以前两者都锚在楼顶，站号的框正好盖住楼名，「这一趟去哪栋」就只剩个数字。
      at: new THREE.Vector3(p.x - cx, h + (live ? 22 : 2), p.y + size[1] / 2 - cz),
      // 摆不下的按这个顺序丢：先丢认路的楼，再丢自建地点，最后才是这一趟要去的楼。
      weight: live ? 3 : ((p.zone === '自定义' || p.custom) ? 2 : 1),
      // 三层：1 = 路线上的楼（任何缩放级别都写）、2 = 认路的几栋、3 = 其余。
      // 缩远了只留第 1 层，不然全校名字叠在几十个像素里。
      tier: live ? 1 : (model.named.indexOf(p.id) >= 0 ? 2 : 3),
      group: live ? p.id : null,
      dy: live ? -(PIN_H + 4) : -3
    });
  });
  var pins = stops.map(function (item) {
    var h = heightOf(item.poi, model);
    return {
      el: putLabel(labelBox, String(item.n), 'map3d-pin ' + (item.trip ? 'is-b' : 'is-a')),
      at: new THREE.Vector3(item.poi.x - cx, h + 22,
        item.poi.y + sizeOf(item.poi, model)[1] / 2 - cz),
      weight: 3,
      tier: 1,
      group: item.poi.id
    };
  });

  // 宽度必须现在量：等 paint() 把某些楼名 display:none 藏掉再量，offsetWidth 就是 0，
  // 包围盒当 24px 算，「图文信息中心楼」和「图书馆」就会叠在一起（390 下就是这样糊的）。
  labels.concat(pins).forEach(function (item) {
    item.w = item.el.offsetWidth || 24;
    item.h = item.el.offsetHeight || 12;
  });

  var startAngle = 0;
  try {
    var saved = parseFloat(localStorage.getItem('nh_map_angle'));
    if (isFinite(saved)) startAngle = saved;
  } catch (e) { startAngle = 0; }
  // 拖过之后以内存里的为准：localStorage 只在松手时写，中途重画会跳回上一次的角度。
  if (lastAngle !== null) startAngle = lastAngle;

  var state = {
    renderer: renderer, scene: scene, camera: camera, labelBox: labelBox,
    labels: labels, pins: pins, host: host, angle: startAngle, zoom: lastZoom,
    plan: plan, opt: opt, model: model, cx: cx, cz: cz,
    // 手机宽度上按钮栏（上）、缩放键与加地点（右）、注脚（下）都压在画布上，
    // 留出这几条安全区，楼名不许钻到控件底下。
    pad: compact ? { l: 0, r: 44, t: 78, b: 8 } : { l: 0, r: 0, t: 0, b: 0 },
    compact: compact,
    target: new THREE.Vector3(0, 0, 0), reach: reach, fitPts: fitPts, W: W, Hh: Hh
  };
  stores.set(host, state);
  if (hosts.indexOf(host) < 0) hosts.push(host);
  host.dataset.map3d = '1';
  bind(host);
  zoomBox(host);
  paint(state);
  return true;
}

// 把相机摆到 target 外 dist 米、按当前转角朝向校园中心。
function aim(state, dist) {
  var yaw = state.angle;
  state.camera.position.set(
    state.target.x + Math.sin(yaw) * Math.cos(PITCH) * dist,
    state.target.y + Math.sin(PITCH) * dist,
    state.target.z + Math.cos(yaw) * Math.cos(PITCH) * dist
  );
  state.camera.lookAt(state.target);
  state.camera.updateMatrixWorld();
}

// 需要多远才刚好装下：按同一方向摆相机，把取景点的 NDC 投影算出来，乘回去就是距离。
// 一轮不够——透视下近处的楼长得比远处快，按远处量出的尺寸缩回来，近端会顶出画框
// （东侧那栋被面板右界切掉半边就是这么来的）。拿同一个式子迭代到收敛，装的就是真的那圈。
function distanceFor(state) {
  var dist = state.reach * 2.5;
  var v = new THREE.Vector3();
  for (var pass = 0; pass < 3; pass += 1) {
    aim(state, dist);
    var m = 0;
    state.fitPts.forEach(function (p) {
      v.copy(p).project(state.camera);
      if (Math.abs(v.x) > m) m = Math.abs(v.x);
      if (Math.abs(v.y) > m) m = Math.abs(v.y);
    });
    if (!(m > 0)) return dist;
    // 1.1 留一圈呼吸：楼名是贴着楼顶往上写的，装太满字会顶出画框。
    dist *= m * 1.1;
  }
  return dist;
}

function paint(state) {
  // 「刚好装下」那圈距离是基准，缩放是在它上面乘除：拉到哪儿、窗口多大都还是同一张图。
  aim(state, distanceFor(state) / state.zoom);
  state.renderer.render(state.scene, state.camera);
  // 缩放分层：远只写路线上的楼，中加认路的几栋，近才全写。名字本身是 HTML，永远不糊。
  // 默认那一档（zoom=1）把楼名全写出来；只有往远处缩才减，不然一进来就少一半名字。
  var tier = state.zoom < 0.7 ? 1 : (state.zoom < 1.0 ? 2 : 3);
  var items = [];
  state.labels.concat(state.pins).forEach(function (item) {
    var show = item.tier <= tier;
    if (item.shown !== show) { item.el.style.display = show ? '' : 'none'; item.shown = show; }
    if (show) items.push(item);
  });
  place(state, items);
}

// 缩放的唯一入口：滚轮、双指、+/− 按钮都走这里，上下限只写一份。
function zoomTo(state, next) {
  var value = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, next));
  if (Math.abs(value - state.zoom) < 0.001) return;
  state.zoom = value;
  lastZoom = value;
  paint(state);
}

// 行程楼名和站号一起摆、一起让位，排不下时整组隐藏。
function place(state, items) {
  var v = new THREE.Vector3();
  var taken = [], groups = [], byGroup = {};
  var pad = state.pad || { l: 0, r: 0, t: 0, b: 0 };
  // 手机上站号徽标和楼名挨得太近就读成「图书馆3」：要 4px 缝，桌面 2px 就够。
  var gap = state.compact ? 4 : 2;
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
    // 不带站号的楼名排不下时也允许上下错开：宿舍片 6/7/8/9/10/11 号楼本来就在一排，
    // 只试 dy=0 的话后半个名字会被整块藏掉，一排楼只剩前三个名字（1280 下就是这样）。
    var shifts = group[0].item.group ? [0, -18, -36, 18, 36, -54, 54] : [0, -14, 14, -28, 28];
    var placed = null;
    shifts.some(function (dy) {
      var boxes = group.map(function (p) { return [p.x - p.w / 2, p.y + dy - p.h, p.x + p.w / 2, p.y + dy]; });
      var fits = boxes.every(function (b, index) {
        // 留 2px 缝：紧贴着的两块（b[0] 正好等于 o[2]）不算相交，但字挤在一起看着就是
        // 一个词（390 下的「图书馆汤湖体育馆」），所以按间隔判，排不下就整块藏掉。
        return group[index].z <= 1 && b[0] >= 0 && b[2] <= state.W && b[1] >= 0 && b[3] <= state.Hh &&
          b[0] >= pad.l && b[2] <= state.W - pad.r && b[1] >= pad.t && b[3] <= state.Hh - pad.b &&
          !taken.some(function (o) {
            return b[0] < o[2] + gap && b[2] > o[0] - gap && b[1] < o[3] + gap && b[3] > o[1] - gap;
          });
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

// 面板上的缩放按钮挂在 .trip-map-wrap 上，不是挂在 #trip-map 里：
// host.innerHTML 每次重画都会被清空，挂里面按钮就跟着没了。
function zoomBox(host) {
  var wrap = host.parentNode;
  if (!wrap) return null;
  var box = wrap.querySelector('.map3d-zoom');
  if (!box) {
    box = document.createElement('div');
    box.className = 'map3d-zoom';
    box.innerHTML =
      '<button type="button" data-map-zoom="in" aria-label="放大" title="放大">+</button>' +
      '<button type="button" data-map-zoom="out" aria-label="缩小" title="缩小">−</button>' +
      '<button type="button" data-map-zoom="reset" aria-label="复位" title="复位（正北、放大到刚好装下）">⟲</button>';
    box.addEventListener('click', function (event) {
      var button = event.target.closest('[data-map-zoom]');
      var state = stores.get(host);
      if (!button || !state) return;
      var what = button.getAttribute('data-map-zoom');
      if (what === 'reset') {
        state.angle = 0;
        lastAngle = 0;
        try { localStorage.setItem('nh_map_angle', '0'); } catch (e) {}
        zoomTo(state, 1);
        return;
      }
      zoomTo(state, state.zoom * (what === 'in' ? 1.35 : 1 / 1.35));
    });
    wrap.appendChild(box);
  }
  box.hidden = false;
  return box;
}

// 点一下：把离手指最近的那个楼名亮两秒（窄屏没写的楼就靠这个看）。
function peek(state, clientX, clientY) {
  var rect = state.host.getBoundingClientRect();
  var x = clientX - rect.left, y = clientY - rect.top;
  var v = new THREE.Vector3();
  var best = null, bestD = 44;
  state.labels.forEach(function (item) {
    v.copy(item.at).project(state.camera);
    var px = (v.x * 0.5 + 0.5) * state.W;
    var py = (-v.y * 0.5 + 0.5) * state.Hh + (item.dy || 0);
    var d = Math.hypot(px - x, py - y);
    if (d < bestD) { bestD = d; best = item; }
  });
  if (!best) return;
  best.el.style.display = '';
  best.el.style.transform = 'translate(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px) translate(-50%,-100%)';
  best.shown = true;
  clearTimeout(state.peekTimer);
  state.peekTimer = setTimeout(function () { paint(state); }, 2200);
}

function bind(host) {
  if (host.__nhMap3dBound) return;
  host.__nhMap3dBound = true;
  var drag = null;
  // 双指：记下按下去时两指的距离和那会儿的缩放，移动时按比例乘上去。
  var fingers = {};
  var pinch = null;
  function spread() {
    var ids = Object.keys(fingers);
    if (ids.length < 2) return 0;
    return Math.hypot(fingers[ids[0]].x - fingers[ids[1]].x, fingers[ids[0]].y - fingers[ids[1]].y);
  }
  host.addEventListener('pointerdown', function (event) {
    var state = stores.get(host);
    if (!state) return;
    fingers[event.pointerId] = { x: event.clientX, y: event.clientY };
    if (Object.keys(fingers).length === 2) {
      drag = null;
      host.classList.remove('is-dragging');
      pinch = { start: spread(), zoom: state.zoom };
      return;
    }
    if (event.button !== 0 || pinch) return;
    drag = { x: event.clientX, angle: state.angle };
    host.classList.add('is-dragging');
    try { host.setPointerCapture(event.pointerId); } catch (e) { drag = null; }
  });
  host.addEventListener('pointermove', function (event) {
    var state = stores.get(host);
    if (!state) return;
    if (fingers[event.pointerId]) { fingers[event.pointerId] = { x: event.clientX, y: event.clientY }; }
    if (pinch) {
      var now = spread();
      if (pinch.start > 0 && now > 0) zoomTo(state, pinch.zoom * (now / pinch.start));
      return;
    }
    if (!drag) return;
    if (Math.abs(event.clientX - drag.x) > 6) drag.moved = true;
    state.angle = drag.angle + (event.clientX - drag.x) * 0.008;
    lastAngle = state.angle;
    paint(state);
  });
  function end(event) {
    var state = stores.get(host);
    if (event && event.pointerId !== undefined) delete fingers[event.pointerId];
    if (pinch && Object.keys(fingers).length < 2) pinch = null;
    if (!drag) return;
    var tap = !drag.moved && event && event.pointerId !== undefined;
    drag = null;
    host.classList.remove('is-dragging');
    if (state) {
      try { localStorage.setItem('nh_map_angle', String(state.angle)); } catch (e) {}
      // 窄屏只写几栋楼的名字，剩下的点一下把最近那个名字亮出来两秒。
      if (tap) peek(state, event.clientX, event.clientY);
    }
  }
  host.addEventListener('pointerup', end);
  host.addEventListener('pointercancel', end);
  // 滚轮缩放；触控板双指捏合走的是 ctrl + wheel，同一个式子就够。
  host.addEventListener('wheel', function (event) {
    var state = stores.get(host);
    if (!state) return;
    event.preventDefault();
    var delta = event.deltaY || 0;
    if (event.deltaMode === 1) delta *= 16;
    else if (event.deltaMode === 2) delta *= state.Hh;
    zoomTo(state, state.zoom * Math.exp(-delta * 0.0018));
  }, { passive: false });
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
  dispose: dispose,
  // 屏幕上点一下，要知道落到校园平面的哪个点（自定义地点用）。
  // 射线打地面 y=0，再还原成 campus.py 的像素坐标（画的时候减过 cx/cz，这里加回来）。
  pick: function (host, clientX, clientY) {
    var state = stores.get(host);
    if (!state || !state.camera || !THREE.Raycaster) return null;
    var box = host.getBoundingClientRect();
    if (!box.width || !box.height) return null;
    var ndc = new THREE.Vector2(
      ((clientX - box.left) / box.width) * 2 - 1,
      -((clientY - box.top) / box.height) * 2 + 1
    );
    var ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, state.camera);
    var hit = new THREE.Vector3();
    if (!ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), hit)) return null;
    return { x: hit.x + (state.cx || 0), y: hit.z + (state.cz || 0) };
  }
};
window.dispatchEvent(new Event('nh-map3d-ready'));
