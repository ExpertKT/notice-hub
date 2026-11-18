/* 锁 providerCtx(id) 的取值契约 —— 高德/百度「判得出 ready、真去连却拿不到 key」的回归闸。
 *
 * 背景：providerCtx 曾写死 key:''，导致配了 key 也弹「还没配 key，先看自绘或 OSM」。
 * 这里不打桩复制逻辑，直接加载真的 trip.js，把浏览器全局换成最小桩，注册一个探针
 * 供应商（id=amap），让真的 boot → loadModeConf → choose → openOnline → makeMap
 * → providerCtx → init 这条链跑起来，把 init 收到的 ctx 抓出来比对。
 *
 * 两个场景：
 *   with-key    配了 key     ⇒ init 被调，ctx.key === conf.amap.key，ctx.secret 同理
 *   without-key 没配 key     ⇒ init 不被调（choose 的 ready 闸拦下），弹填 key 面板
 *                              —— 不再有「拿空 key 去连」的静默失败
 * 用法：node js_provider_ctx_probe.js <trip.js 绝对路径> <with-key|without-key>
 * 输出：一行 `PASS <name>` 或 `FAIL <name> <原因>`；FAIL 就 exit 1。
 */
'use strict';
var fs = require('fs');
var path = require('path');
var vm = require('vm');

var tripPath = process.argv[2];
var scenario = process.argv[3] || 'with-key';
if (!tripPath || !fs.existsSync(tripPath)) {
  console.error('usage: node js_provider_ctx_probe.js <trip.js> <with-key|without-key>');
  process.exit(2);
}

// 假 DOM 元素：所有方法都返回自身（可链式），任意属性可写。够让
// boot/modeBar/refreshModes/openOnline 这套「碰到就 no-op」的调用走通。
function makeFake(tag) {
  var el = {
    tagName: String(tag || 'div').toUpperCase(),
    style: {}, dataset: {}, hidden: false,
    clientHeight: 600, clientWidth: 800,
    textContent: '', value: '', checked: false, disabled: false,
    classList: null,
    appendChild: chain, removeChild: chain, remove: chain, focus: chain,
    setAttribute: chain, getAttribute: function () { return null; },
    addEventListener: chain, removeEventListener: chain,
    querySelector: function () { return makeFake('div'); },
    querySelectorAll: function () { return []; },
    getBoundingClientRect: function () { return { left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 }; },
    getContext: function () { return ctx2d; },
    parentNode: null
  };
  function chain() { return el; }
  var ctx2d = {
    measureText: function () { return { width: 10 }; },
    clearRect: chain, fillRect: chain, strokeRect: chain, beginPath: chain, moveTo: chain,
    lineTo: chain, stroke: chain, fill: chain, arc: chain, closePath: chain, save: chain,
    restore: chain, translate: chain, rotate: chain, scale: chain, setLineDash: chain,
    fillText: chain, strokeText: chain, drawImage: chain
  };
  el.classList = { add: chain, remove: chain, toggle: chain, contains: function () { return false; } };
  el.parentNode = el;   // mapWrap() = host.parentNode，给它一个可用的 wrap
  return el;
}

function run(cfg, done) {
  var captured = null;          // 探针 init 收到的 ctx
  var initCalled = false;
  var keyPanelOpened = false;   // choose 没 ready 时应弹填 key 面板
  var noteCalls = [];

  // 地图配置：with-key 给 amap 配 key/secret；without-key 给空。
  var mapConf = cfg;

  var sandboxWindow = {
    getComputedStyle: function () { return { getPropertyValue: function () { return ''; } }; },
    requestAnimationFrame: function (cb) { return setTimeout(cb, 0); },
    setTimeout: setTimeout, clearTimeout: clearTimeout,
    matchMedia: function () { return { matches: false, addEventListener: function () {}, removeEventListener: function () {} }; },
    ResizeObserver: function () { this.observe = function () {}; this.disconnect = function () {}; },
    MutationObserver: function () { this.observe = function () {}; this.disconnect = function () {}; },
    addEventListener: function () {}, removeEventListener: function () {},
    location: { reload: function () {} },
    navigator: { onLine: true },
    // 让 geo()/loadPlanTf() 不走 fetch、直接有基准
    NH_CAMPUS_OSM: { origin: { lat: 39.9, lon: 116.4 }, metersPerPixel: 0.9 },
    NH_CAMPUS_GEO: { planToSnapshot: { a: 1, b: 0, tx: 0, ty: 0 } },
    nhMap3d: null, nhMap: null, nhMapProviders: null, nhTrip: null
  };

  var store = { nh_map_mode: 'amap' };   // 预置档位，boot→loadMode 就会把 mode.id 设成 amap，choose('amap') 才会跑
  var sandbox = {
    console: console,
    // page.html:3701 的全局 api()：MAP_CONF 返回配置，其它给空壳（plan 为空就画不出，但不影响本探针）。
    api: function (p) {
      if (p === '/api/agent/map/config') return Promise.resolve(mapConf);
      return Promise.resolve({});
    },
    // 路线栏动效（page.html 全局 surfaceShow/surfaceHide），探针里 no-op。
    surfaceShow: function () {},
    surfaceHide: function (el, done) { if (typeof done === 'function') done(); },
    setTimeout: setTimeout, clearTimeout: clearTimeout, setInterval: setInterval, clearInterval: clearInterval,
    Promise: Promise, JSON: JSON, Math: Math, Number: Number, String: String, Boolean: Boolean,
    Object: Object, Array: Array, Date: Date, Error: Error, RegExp: RegExp,
    isFinite: isFinite, isNaN: isNaN, parseFloat: parseFloat, parseInt: parseInt, encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent,
    fetch: function () { return Promise.resolve({ ok: false, status: 0, json: function () { return Promise.resolve(null); } }); },
    navigator: { onLine: true },
    localStorage: {
      getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
      setItem: function (k, v) { store[k] = String(v); },
      removeItem: function (k) { delete store[k]; }
    },
    document: {
      readyState: 'complete',
      documentElement: makeFake('html'),
      body: makeFake('body'),
      head: makeFake('head'),
      getElementById: function () { return makeFake('div'); },
      querySelector: function () { return null; },
      querySelectorAll: function () { return []; },
      createElement: function (tag) { return makeFake(tag); },
      addEventListener: function () {}, removeEventListener: function () {}
    },
    MutationObserver: sandboxWindow.MutationObserver,
    window: sandboxWindow
  };
  sandbox.globalThis = sandbox;
  sandboxWindow.window = sandboxWindow;
  sandboxWindow.document = sandbox.document;
  sandboxWindow.localStorage = sandbox.localStorage;

  var code = fs.readFileSync(tripPath, 'utf8');
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: path.basename(tripPath) });

  // trip.js 的 IIFE 已同步跑完（document.readyState!=='loading' 时已调 boot）。
  // 抢在 boot 的异步链（loadModeConf.then→choose）落地前注册探针，顶掉内置 amap。
  var providers = sandboxWindow.nhMapProviders;
  if (!providers || !sandboxWindow.nhMap) {
    done(new Error('trip.js 没挂上 window.nhMapProviders / window.nhMap'));
    return;
  }
  providers.register({
    id: 'amap', label: '探针高德', credit: 'probe', needsKey: true,
    init: function (el, ctx) {
      initCalled = true;
      captured = ctx;
      return Promise.resolve({
        kind: 'amap',
        toPlan: function () { return { x: 0, y: 0 }; },
        fromPlan: function () { return { lat: 0, lon: 0 }; },
        toScreen: function () { return { x: 0, y: 0 }; },
        frame: function () {}, setMarkers: function () {}, onMove: function () {}, destroy: function () {}
      });
    }
  });
  // 顺手盯一下「没配 key」会不会弹填 key 面板、note 说什么。
  sandbox.document.createElement = function (tag) {
    var el = makeFake(tag);
    var origSet = function () {};
    return el;
  };

  // 给异步链（boot 里的 loadModeConf().then → choose → openOnline → makeMap）留时间落地。
  setTimeout(function () {
    done(null, { captured: captured, initCalled: initCalled, noteCalls: noteCalls, keyPanelOpened: keyPanelOpened });
  }, 60);
}

var KEY = 'AMAP-KEY-123';
var SECRET = 'SEC-456';
var name = scenario === 'with-key' ? 'test_provider_ctx_passes_configured_key' : 'test_provider_ctx_without_key_never_inits';
var cfg = scenario === 'with-key' ? { amap: { enabled: true, key: KEY, secret: SECRET } } : { amap: { enabled: true } };

run(cfg, function (err, r) {
  if (err) { console.log('FAIL ' + name + ' ' + err.message); process.exit(1); }
  if (scenario === 'with-key') {
    var ok = r.initCalled && r.captured && r.captured.key === KEY && r.captured.secret === SECRET;
    if (ok) console.log('PASS ' + name + ' ctx.key=' + r.captured.key);
    else console.log('FAIL ' + name + ' initCalled=' + r.initCalled + ' ctx=' + JSON.stringify(r.captured && { key: r.captured.key, secret: r.captured.secret }));
    process.exit(ok ? 0 : 1);
  } else {
    // 没配 key：choose 的 ready 闸该拦下，init 不该被调（不会拿空 key 去连）。
    var ok = !r.initCalled;
    if (ok) console.log('PASS ' + name + ' initCalled=false');
    else console.log('FAIL ' + name + ' initCalled=true ctx.key=' + (r.captured && JSON.stringify(r.captured.key)));
    process.exit(ok ? 0 : 1);
  }
});
