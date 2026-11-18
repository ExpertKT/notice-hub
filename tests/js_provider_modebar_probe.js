'use strict';
/* node-level 回归闸：第三方 register 进来的底图，必须真的出现在「底图与路线」那排按钮里、
 * 且能被选中（refreshModes 补按钮 + 点击走 choose 换档）。
 *
 * 背景：modeBar 只在建栏时遍历一次注册表，晚于建栏 register 的档要靠 refreshModes 补。
 * 这里不打桩复制逻辑，直接加载真的 trip.js 整个 IIFE，用最小假 DOM 让 boot 跑起来，
 * 注册一个假供应商 → nhMap.refreshModes() → 断言按钮在排里、能点、选中后换档生效。
 *
 * 用法：node tests/js_provider_modebar_probe.js <trip.js 绝对路径>
 * 输出：一行 `PASS <name>` 或 `FAIL <name> <原因>`；FAIL 就 exit 1。
 */
var fs = require('fs');
var path = require('path');
var vm = require('vm');

var tripPath = process.argv[2];
if (!tripPath || !fs.existsSync(tripPath)) {
  console.error('usage: node js_provider_modebar_probe.js <trip.js>');
  process.exit(2);
}

var name = 'test_third_party_provider_shows_in_modebar_and_selectable';
function fail(msg) { console.log('FAIL ' + name + ' ' + msg); process.exit(1); }

/* 假 DOM：够让 boot/modeBar/refreshModes/choose/openOnline 这套「碰到就 no-op」的调用走通，
 * 且 createElement 出来的节点能被 appendChild 追踪、querySelector 能按 .class / [attr] 找到。 */
function makeFake(tag) {
  var el = {
    tagName: String(tag || 'div').toUpperCase(),
    style: {}, dataset: {}, hidden: false,
    clientHeight: 600, clientWidth: 800,
    textContent: '', value: '', checked: false, disabled: false,
    className: '',
    attrs: {},
    children: [],
    listeners: {},
    appendChild: function (child) { el.children.push(child); return child; },
    removeChild: function (child) {
      var i = el.children.indexOf(child);
      if (i >= 0) el.children.splice(i, 1);
      return child;
    },
    remove: function () { if (el.parentNode) el.parentNode.removeChild(el); return el; },
    focus: function () { return el; },
    setAttribute: function (k, v) { el.attrs[k] = String(v); return el; },
    getAttribute: function (k) { return Object.prototype.hasOwnProperty.call(el.attrs, k) ? el.attrs[k] : null; },
    addEventListener: function (name, fn) {
      (el.listeners[name] = el.listeners[name] || []).push(fn);
      return el;
    },
    removeEventListener: function () { return el; },
    querySelector: function (sel) { return el.querySelectorAll(sel)[0] || null; },
    querySelectorAll: function (sel) {
      var out = [];
      (function walk(node) {
        (node.children || []).forEach(function (c) {
          if (matches(c, sel)) out.push(c);
          walk(c);
        });
      })(el);
      return out;
    },
    closest: function (sel) { return matches(el, sel) ? el : null; },
    getBoundingClientRect: function () { return { left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 }; },
    getContext: function () { return ctx2d; },
    parentNode: null
  };
  function matches(node, sel) {
    if (sel.charAt(0) === '.') return String(node.className).split(/\s+/).indexOf(sel.slice(1)) >= 0;
    if (sel.charAt(0) === '[') {
      var m = sel.slice(1, -1).split('=');
      var key = m[0];
      var want = m.length > 1 ? m[1].replace(/"/g, '') : null;
      var have = node.attrs ? node.attrs[key] : undefined;
      if (want === null) return have !== undefined;
      return have === want;
    }
    return false;
  }
  function chain() { return el; }
  var ctx2d = {
    measureText: function () { return { width: 10 }; },
    clearRect: chain, fillRect: chain, strokeRect: chain, beginPath: chain, moveTo: chain,
    lineTo: chain, stroke: chain, fill: chain, arc: chain, closePath: chain, save: chain,
    restore: chain, translate: chain, rotate: chain, scale: chain, setLineDash: chain,
    setTransform: chain, fillText: chain, strokeText: chain, drawImage: chain
  };
  el.classList = {
    add: chain, remove: chain,
    toggle: function () { return el; },
    contains: function () { return false; }
  };
  return el;
}

var FAKE_ID = 'probe-tile';
var initCalled = false;
var mapConf = {};   // 没配 key：假供应商 needsKey=false，choose 仍判 ready，可直接选中。

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
  devicePixelRatio: 1,
  NH_CAMPUS_OSM: { origin: { lat: 31.716299, lon: 119.815803 }, metersPerPixel: 0.9 },
  NH_CAMPUS_GEO: { planToSnapshot: { a: 1, b: 0, tx: 0, ty: 0 } },
  nhMap3d: null, nhMap: null, nhMapProviders: null, nhTrip: null
};

// mode.id 预置成 'sketch'，boot 后 choose(假供应商) 才会真的换档。
var store = { nh_map_mode: 'sketch' };
var wrap = makeFake('div');                 // mapWrap() = $('trip-map').parentNode
var tripMap = makeFake('div');
tripMap.parentNode = wrap;

var sandbox = {
  console: console,
  api: function (p) {
    if (p === '/api/agent/map/config') return Promise.resolve(mapConf);
    return Promise.resolve({});
  },
  surfaceShow: function () {},
  surfaceHide: function (el, done) { if (typeof done === 'function') done(); },
  setTimeout: setTimeout, clearTimeout: clearTimeout, setInterval: setInterval, clearInterval: clearInterval,
  Promise: Promise, JSON: JSON, Math: Math, Number: Number, String: String, Boolean: Boolean,
  Object: Object, Array: Array, Date: Date, Error: Error, RegExp: RegExp,
  isFinite: isFinite, isNaN: isNaN, parseFloat: parseFloat, parseInt: parseInt,
  encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent,
  fetch: function () { return Promise.resolve({ ok: false, status: 0, json: function () { return Promise.resolve(null); } }); },
  navigator: { onLine: true },
  localStorage: {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem: function (k, v) { store[k] = String(v); },
    removeItem: function (k) { delete store[k]; }
  },
  document: {
    readyState: 'complete',  // boot() 立即跑，才会真的建出 .map-modes 那排并挂上 click
    documentElement: makeFake('html'),
    body: makeFake('body'),
    head: makeFake('head'),
    getElementById: function (id) { return id === 'trip-map' ? tripMap : makeFake('div'); },
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

// boot() 首行就是 `if (!document.getElementById('trip-stage')) return;`，没有它 boot 直接退，
// modeBar 永远建不出来。这里补一个 trip-stage，让 boot 真跑、bar 真挂上 click。
var tripStage = makeFake('div');
var origGetById = sandbox.document.getElementById;
sandbox.document.getElementById = function (id) {
  if (id === 'trip-stage') return tripStage;
  return origGetById(id);
};

// 抓 choose 的 catch 回退：openOnline 失败会把 mode.id 拨回 'sketch'，这里把原因打出来。
// trip.js 还会调 console.warn/info（loadPlanTf 缺 planToSnapshot 就 warn），sandbox 的 console
// 得补齐这几个方法，不然 `console.warn is not a function` 会把 openOnline 的 promise 打成 reject，
// 让 choose 落进 catch → mode.id 回 'sketch'，看起来就是「点了没选中」。
['warn', 'info', 'debug', 'trace'].forEach(function (fn) {
  if (typeof sandbox.console[fn] !== 'function') {
    sandbox.console[fn] = function () {
      sandbox.console.log('[probe:console.' + fn + '] ' + Array.prototype.slice.call(arguments).map(String).join(' '));
    };
  }
});
var origError = sandbox.console.error;
sandbox.console.error = function () {
  sandbox.console.log('[probe:console.error] ' + Array.prototype.slice.call(arguments).map(String).join(' '));
  return origError.apply(console, arguments);
};

var code = fs.readFileSync(tripPath, 'utf8');
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: path.basename(tripPath) });

var providers = sandboxWindow.nhMapProviders;
if (!providers || !sandboxWindow.nhMap) fail('trip.js 没挂上 window.nhMapProviders / window.nhMap');

// ① 注册一个假供应商（id 不在内置 MODES 里）
var registered = providers.register({
  id: FAKE_ID,
  label: '探针底图',
  credit: 'probe © nobody',
  needsKey: false,
  init: function (el, ctx) {
    initCalled = true;
    return Promise.resolve({
      kind: 'osm',
      toPlan: function () { return { x: 0, y: 0 }; },
      fromPlan: function () { return { lat: 0, lon: 0 }; },
      toScreen: function () { return { x: 0, y: 0 }; },
      frame: function () {}, setMarkers: function () {}, onMove: function () {}, destroy: function () {}
    });
  }
});
if (registered !== true) fail('register() 没返回 true');
if (!providers.get(FAKE_ID)) fail('register 后 get(' + FAKE_ID + ') 为空');
if (providers.list().filter(function (s) { return s.id === FAKE_ID; }).length !== 1) {
  fail('register 后 list() 里没有它');
}

// ② 晚于建栏 register 的档要 refreshModes 补按钮才上排
sandboxWindow.nhMap.refreshModes();

var bar = wrap.querySelector('.map-modes');
if (!bar) fail('modeBar 没建出 .map-modes 那排');
function findBtn() {
  var found = null;
  bar.querySelectorAll('[data-map-mode]').forEach(function (b) {
    if (b.getAttribute('data-map-mode') === FAKE_ID) found = b;
  });
  return found;
}
var button = findBtn();
if (!button) fail('refreshModes 后底图那排里没有 [' + FAKE_ID + '] 的按钮');
if (button.textContent !== '探针底图') fail('按钮文字是 ' + JSON.stringify(button.textContent) + '，应为「探针底图」');

// ③ 能被选中：needsKey=false 判 ready，点按钮走 choose → 换档成功
if (button.getAttribute('aria-pressed') !== 'false') {
  fail('选中前 aria-pressed 应为 false，实际 ' + button.getAttribute('aria-pressed'));
}
// bar 的 click 是挂在 bar 上冒泡处理的，event.target 是按钮、closest('[data-map-mode]') 命中它自己。
var barClick = (bar.listeners && bar.listeners.click) || [];
if (!barClick.length) fail('.map-modes 上没挂 click 处理器，按钮点不动');
sandbox.console.log('[probe] barClick handlers=' + barClick.length + ' busy-guard?');
var evt = { target: button };
barClick.forEach(function (fn) { fn(evt); });
sandbox.console.log('[probe] dispatched, waiting for choose chain');
// 100ms 后再补一次状态，60ms 若还没落地说明链路更长或被 catch 回退了。
setTimeout(function () {
  sandbox.console.log('[probe] t+100ms initCalled=' + initCalled);
}, 100);

// choose 异步落地后 mode.id 换成 FAKE_ID，refreshModes 会把它的 aria-pressed 置 true。
setTimeout(function () {
  if (!initCalled) fail('点按钮后 init 没被调，choose 没走到 openOnline');
  sandboxWindow.nhMap.refreshModes();
  var btn2 = findBtn();
  if (!btn2) fail('二次 refreshModes 后按钮丢了');
  if (btn2.getAttribute('aria-pressed') !== 'true') {
    fail('点击后 [' + FAKE_ID + '] 没被选中（aria-pressed=' + btn2.getAttribute('aria-pressed') + '），换档没生效');
  }
  console.log('PASS ' + name + ' provider=' + FAKE_ID + ' 上排且可选中，initCalled=' + initCalled);
  process.exit(0);
}, 60);
