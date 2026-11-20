import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
const source = name => readFileSync(new URL('../noticehub/web_static/' + name, import.meta.url), 'utf8');

test('shell is network first; API, mutations and unknown assets never enter cache', async () => {
  const events = {}, saved = new Map(), writes = [], opened = [];
  let offline = false, reply = new Response('fresh');
  const cache = { addAll: async urls => { assert(urls.includes('/js/three.module.min.js')); },
    put: async (key, value) => { writes.push(key); saved.set(key, value); }, match: async key => saved.get(key) };
  const deleted = [];
  const context = { URL, caches: { open: async name => { opened.push(name); return cache; },
    keys: async () => ['foreign-cache', 'noticehub-pwa-shell-v6', 'noticehub-pwa-shell-v7'], delete: async key => { deleted.push(key); } },
    fetch: async () => { if (offline) throw Error('offline'); return reply.clone(); },
    self: { location: { origin: 'https://test.local' }, clients: { claim() {} }, skipWaiting() {}, addEventListener: (name, fn) => { events[name] = fn; } } };
  vm.runInNewContext(source('js/sw.js'), context);
  for (const name of ['install', 'activate']) { let done; events[name]({ waitUntil: promise => { done = promise; } }); await done; }
  // 「开着哪个缓存」和「留下哪个缓存」都得真断言：只写在失败文案里的版本号，把生产代码停在 v4 也照样绿。
  assert.deepEqual(opened, ['noticehub-pwa-shell-v7'], 'install 开的不是当前版本的缓存');
  assert.deepEqual(deleted.sort(), ['foreign-cache', 'noticehub-pwa-shell-v6'],
    'activate 只清旧版本，当前版本的缓存要留着');
  async function request(path, method = 'GET') {
    let result;
    events.fetch({ request: { url: 'https://test.local' + path, method }, respondWith: promise => { result = promise; } });
    return result;
  }
  assert.equal(await request('/api/agent/plan'), undefined);
  assert.equal(await request('/api/tasks', 'POST'), undefined);
  assert.equal(await request('/not-shell.js'), undefined);
  assert.equal(await (await request('/?token=user-supplied')).text(), 'fresh');
  assert.deepEqual(writes, ['/']);
  reply = new Response('newer');
  assert.equal(await (await request('/')).text(), 'newer');
  offline = true;
  assert.equal(await (await request('/?demo=1')).text(), 'newer');
  await assert.rejects(request('/js/map3d.js'), /offline/); // Never return HTML for missing JS.
});

test('install entry needs platform support; dismissed/installed prompts hide it; offline writes are blocked', async () => {
  const events = {}, nodes = {};
  for (const id of ['pwa-install', 'pwa-help', 'pwa-help-text']) nodes[id] = { hidden: true, addEventListener(name, fn) { this[name] = fn; }, showModal() { this.open = true; } };
  let networkCalls = 0;
  const window = { isSecureContext: true, fetch: async () => { networkCalls++; return {}; }, matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener: (name, fn) => { events[name] = fn; } };
  const context = { window, navigator: { userAgent: 'Chrome', platform: 'Linux', onLine: true }, document: { getElementById: id => nodes[id], body: { classList: { contains: () => false } } }, location: new URL('https://test.local/'), URL, Request, Blob };
  vm.runInNewContext(source('js/pwa.js'), context);
  assert.equal(nodes['pwa-install'].hidden, true);
  let prompted = 0;
  events.beforeinstallprompt({ preventDefault() {}, prompt: async () => { prompted++; }, userChoice: Promise.resolve({ outcome: 'dismissed' }) });
  assert.equal(nodes['pwa-install'].hidden, false);
  await nodes['pwa-install'].click();
  assert.equal(prompted, 1);
  assert.equal(nodes['pwa-install'].hidden, true);
  context.navigator.onLine = false;
  await assert.rejects(window.fetch('/api/tasks', { method: 'POST' }), /这次修改没有发送/);
  assert.equal(networkCalls, 0);
  events.appinstalled();
  assert.equal(nodes['pwa-install'].hidden, true);
});

test('last plan survives network failure; auth errors clear it; corrupt storage gives a useful empty state', async () => {
  const nodes = {}, events = {}, storage = new Map();
  for (const id of ['trip-stage', 'trip-list', 'trip-map', 'trip-meta', 'trip-rest', 'trip-snapshot']) nodes[id] = { innerHTML: '', textContent: '', hidden: true, appendChild() {} };
  let error = null;
  const context = { console, Date, Number, JSON, navigator: { onLine: true }, localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    document: { readyState: 'loading', getElementById: id => nodes[id], createElement: () => ({ addEventListener() {} }), body: { classList: { add() {}, remove() {} } }, addEventListener() {} },
    window: { addEventListener: (name, fn) => { events[name] = fn; } }, api: async () => { throw error; },
    surfaceShow(el) { if (el) el.hidden = false; },
    surfaceHide(el, done) { if (el) el.hidden = true; if (done) done(); } };
  vm.runInNewContext(source('js/trip.js'), context);
  context.window.nhTrip.render({ trips: [], pois: [] });
  assert(storage.has('nh_trip_snapshot'));
  error = { networkUnavailable: true };
  await context.window.nhTrip.reload();
  assert.match(nodes['trip-snapshot'].textContent, /上次的排法/);
  error = Error('401');
  await context.window.nhTrip.reload();
  assert.equal(storage.has('nh_trip_snapshot'), false);
  assert.match(nodes['trip-list'].innerHTML, /401/);
  storage.set('nh_trip_snapshot', '{broken');
  context.navigator.onLine = false;
  await context.window.nhTrip.reload();
  assert.match(nodes['trip-list'].innerHTML, /还没有保存过排法/);
});

test('failed plan clears stale map labels and its retry calls the loader; routine failure is not empty data', async () => {
  const nodes = {};
  for (const id of ['trip-map', 'trip-map-meta', 'trip-meta', 'trip-rest', 'trip-list', 'trip-legend', 'trip-routine-chips']) {
    nodes[id] = { textContent: 'stale', hidden: false, innerHTML: '', appendChild(button) { this.button = button; } };
  }
  let loads = 0, fail = true, chips;
  const context = { state: {}, snapshotNote() {}, $: id => nodes[id], esc: text => text,
    load: () => { loads++; }, renderRoutineChips: rows => { chips = rows; }, ROUTINES: '/routines',
    api: async () => { if (fail) throw Error('HTTP 503'); return { routines: ['saved'] }; },
    document: { body: { classList: { remove() {} } }, createElement: () => ({ addEventListener(name, fn) { this[name] = fn; } }) },
    surfaceShow(el) { if (el) el.hidden = false; },
    surfaceHide(el, done) { if (el) el.hidden = true; if (done) done(); } };
  const trip = source('js/trip.js');
  vm.runInNewContext(trip.slice(trip.indexOf('  function fail('), trip.indexOf('  function load()')), context);
  context.fail('HTTP 503');
  assert.equal(nodes['trip-map-meta'].textContent, '');
  assert.equal(nodes['trip-legend'].hidden, true);
  nodes['trip-list'].button.click(); assert.equal(loads, 1);
  vm.runInNewContext(trip.slice(trip.indexOf('  function loadRoutines('), trip.indexOf('  function postRoutine(')), context);
  await context.loadRoutines();
  assert.match(nodes['trip-routine-chips'].innerHTML, /读取常做的事失败.*HTTP 503/);
  assert.equal(chips, undefined);
  fail = false; nodes['trip-routine-chips'].button.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(chips, ['saved']);
});

test('map placement keeps a stop name and its pin together when crowded or out of frame', () => {
  const code = source('js/map3d.js');
  const body = code.slice(code.indexOf('function place(state, items)'), code.indexOf('\nfunction bind(host)'));
  class Vector3 { copy(at) { Object.assign(this, at); return this; } project() { return this; } }
  // place() 会去读「我在…」那枚控件、把它当已占位；平时图上没有这枚控件。
  let picker = null;
  const context = { THREE: { Vector3 }, document: { getElementById: () => picker } };
  vm.runInNewContext(body, context);
  const item = (group, dy, width) => ({ group, dy, weight: 3, at: { x: 0, y: 0, z: 0 }, el: { offsetWidth: width, offsetHeight: 12, style: {} } });
  const name = item('stop', -23, 60), pin = item('stop', 0, 19);
  const blocker = item(null, -23, 180); blocker.weight = 4;
  context.place({ W: 200, Hh: 150 }, [name, pin, blocker]);
  assert.equal(name.el.style.display, '');
  assert.equal(pin.el.style.display, '');
  assert.notEqual(name.el.style.transform, 'translate(100.0px,52.0px) translate(-50%,-100%)');
  context.place({ W: 30, Hh: 20 }, [name, pin]);
  assert.equal(name.el.style.display, 'none');
  assert.equal(pin.el.style.display, 'none');
  // 手机上「我在…」浮在地图上：楼名落在它底下只露出半个字，所以要被顶到它上沿之上。
  // 同一个位置没有控件时是 52.0px（见上面那条 notEqual）。
  picker = { offsetParent: {}, getBoundingClientRect: () => ({ left: 70, top: 40, right: 130, bottom: 52 }) };
  const solo = item('stop', -23, 60);
  context.place({ W: 200, Hh: 150, host: { getBoundingClientRect: () => ({ left: 0, top: 0, right: 200, bottom: 150 }) } }, [solo]);
  assert.equal(solo.el.style.transform, 'translate(100.0px,34.0px) translate(-50%,-100%)');
});

test('assistant replies preserve lines and bold text safely; user text and saved source stay unchanged', () => {
  const page = source('page.html');
  class Node {
    constructor(tag) { this.tag = tag; this.children = []; this.value = ''; }
    set textContent(value) { this.children = []; this.value = value; }
    get textContent() { return this.value + this.children.map(child => child.textContent).join(''); }
    appendChild(child) { this.children.push(child); }
  }
  const log = new Node('log'), storage = new Map();
  log.querySelectorAll = () => log.children;
  log.addEventListener = () => {};
  log.style = {};
  const document = { createElement: tag => new Node(tag), createTextNode: value => { const node = new Node('#text'); node.textContent = value; return node; }, getElementById: () => log };
  const context = { document, MutationObserver: class { observe() {} }, localStorage: { setItem: (key, value) => storage.set(key, value) } };
  vm.runInNewContext(page.slice(page.indexOf('function agentReplyText('), page.indexOf('function paintAgentHistory(')), context);
  const raw = '1166 米\n**第一趟（15:40）：**\n1. **立德楼512** <img src=x onerror=alert(1)>\n2. 图书馆';
  const make = kind => { context.agentLine(kind, raw); const node = log.children.at(-1); node.classList = { contains: name => name === 'agent-line-user' && kind === 'user' }; return node; };
  // agentLine persists immediately, so the mock supplies classList before appending.
  const append = log.appendChild.bind(log);
  log.appendChild = node => { node.classList = { contains: name => node.className.includes(name) }; append(node); };
  const bot = make('bot');
  assert.equal(bot.textContent, raw.replace(/\*\*/g, ''));
  assert.equal(bot.children.filter(node => node.tag === 'strong').length, 2);
  assert(bot.children.every(node => ['strong', '#text'].includes(node.tag)));
  assert.equal(make('user').textContent, raw);
  assert.deepEqual(JSON.parse(storage.get('nh-agent-log')).map(row => row.text), [raw, raw]);
  assert.match(page, /\.agent-line-bot:not\(\.agent-wait\)\{white-space:pre-wrap\}/);
});

test('chat waits five minutes; elapsed wording advances and stops when the reply completes', () => {
  const page = source('page.html');
  let now = 0, next, cleared = 0;
  class Node {
    constructor() { this.children = []; this.attrs = {}; this.classList = { remove() {} }; }
    appendChild(child) { this.children.push(child); }
    setAttribute(key, value) { this.attrs[key] = value; }
    removeAttribute(key) { delete this.attrs[key]; }
    querySelectorAll() { return this.children.map(() => ({ style: {} })); }
    remove() { this.removed = true; }
  }
  const log = new Node();
  log.addEventListener = () => {};
  log.style = {};
  const context = { Date: { now: () => now }, document: { getElementById: () => log, createElement: () => new Node() },
    MutationObserver: class { observe() {} },
    window: { setTimeout: fn => { next = fn; return 1; }, clearTimeout: () => { cleared++; } }, agentReplyText() {}, rememberAgentLog() {} };
  vm.runInNewContext(page.slice(page.indexOf('function keepAgentLogPinned('), page.indexOf('var agentPending =')), context);
  const waiting = context.agentWait(), label = waiting.children[0];
  assert.equal(label.textContent, '正在处理 · 0 秒');
  now = 12000; next(); assert.equal(label.textContent, '正在处理 · 12 秒');
  now = 45000; next(); assert.match(label.textContent, /45 秒 · 还在跑/);
  now = 151000; next(); assert.match(label.textContent, /151 秒/);
  context.agentWaitDone(waiting); assert.equal(cleared, 1); assert(waiting.removed);
  const submit = page.slice(page.indexOf('function submitAgent('), page.indexOf('function handleAgent('));
  assert.match(submit, /timeoutMs: 300000/);
  assert.match(page, /options.timeoutMs === undefined \? 30000/);
});

// 慢机器上回复的最后一步会落在「已经滚到底」之后：内容长高了，滚动位置没跟上，
// 读者得自己往下拨。这条锁住跟着长高贴底，以及「读者往上翻过就不抢位置」。
test('the reply log follows content that grows after the last scroll, unless the reader scrolled up', () => {
  const page = source('page.html');
  const body = page.slice(page.indexOf('function keepAgentLogPinned('), page.indexOf('function paintAgentHistory('));
  const log = { scrollHeight: 400, clientHeight: 100, scrollTop: 300, style: {}, addEventListener(name, fn) { this['on_' + name] = fn; } };
  let grew = null, frame = null, listening = 0;
  const context = {
    MutationObserver: class { constructor(fn) { grew = fn; } observe() { listening++; } },
    requestAnimationFrame: fn => { frame = fn; }
  };
  vm.runInNewContext(body, context);
  context.keepAgentLogPinned(log);
  assert.equal(listening, 1);
  // html 上那句 scroll-behavior:smooth 会继承下来，记录区必须是 instant，否则每贴一次底都是一段动画
  assert.equal(log.style.scrollBehavior, 'auto');
  log.scrollHeight = 520; grew(); frame();
  assert.equal(log.scrollTop, 520, '内容长高后没有贴到底');
  log.scrollTop = 100; log.on_wheel();
  log.scrollHeight = 640; grew(); frame();
  assert.equal(log.scrollTop, 100, '读者往上翻过，内容再长高也不该把他拽回底部');
});

test('external abort remains cancelled rather than timed out', async () => {
  const page = source('page.html');
  const context = { AbortController, token: '', friendlyError: value => value,
    window: { setTimeout: () => 1, clearTimeout() {} },
    fetch: (path, options) => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))) };
  vm.runInNewContext(page.slice(page.indexOf('function api(path, options)'), page.indexOf('<!--AGENT_BRIEF_START-->', page.indexOf('function api(path, options)'))), context);
  const controller = new AbortController();
  const request = context.api('/api/agent/chat', { timeoutMs: 300000, signal: controller.signal });
  controller.abort();
  await assert.rejects(request, error => error.requestCancelled === true && !error.requestTimeout && error.message === '已取消');
});

test('failed chat exposes a one-click retry of the original message and repeated failure stays retryable', async () => {
  const page = source('page.html'), buttons = [], requests = [], subjects = [];
  const pending = { appendChild: button => buttons.push(button), before: node => subjects.push(node.textContent) };
  let fail = true;
  const context = { String, JSON, el: (tag, cls, text) => ({ textContent: text }), wakeAgentDock() {}, agentLine() {}, agentWait: () => pending,
    agentWaitDone() {}, agentTrace() {}, loadTasks() {}, handleAgent() {},
    sessionStorage: { getItem: () => 'session', setItem() {} },
    document: { getElementById: () => ({ scrollHeight: 100 }), createElement: () => ({ addEventListener(name, fn) { this[name] = fn; }, remove() { this.removed = true; } }) },
    api: async (path, options) => { requests.push(JSON.parse(options.body)); if (fail) throw Error('HTTP 503'); return { say: '已恢复' }; } };
  vm.runInNewContext(page.slice(page.indexOf('function submitAgent('), page.indexOf('function handleAgent(')), context);
  const settle = () => new Promise(resolve => setImmediate(resolve));
  context.submitAgent('原来的问题', '交班会材料'); await settle();
  assert.equal(buttons[0].textContent, '重试这句话');
  buttons[0].click(); await settle();
  assert.equal(buttons[0].removed, true);
  assert.equal(buttons[1].textContent, '重试这句话');
  fail = false; buttons[1].click(); await settle();
  assert.equal(requests.length, 3);
  assert(requests.every(row => row.text === '原来的问题' && row.session_id === 'session'));
  assert.deepEqual(subjects, Array(3).fill('关于待办「交班会材料」'));
});

test('chat maps only the last successful route tool result', async () => {
  const page = source('page.html'), mapped = [];
  const first = { poi_ids: ['first'] }, last = { poi_ids: ['last'] };
  const context = { window: { nhTrip: { renderRoute: async route => { mapped.push(route); } } },
    wakeAgentDock() {}, agentLine() {}, agentWait() {}, agentWaitDone() {}, agentTrace() {},
    sessionStorage: { getItem: () => '' }, api: async () => ({ trace: [
      { kind: 'call', tool: 'plan_route', ok: true, result: first },
      { kind: 'call', tool: 'plan_route', ok: true, result: last },
      { kind: 'call', tool: 'plan_route', ok: false, result: { poi_ids: ['failed'] } }
    ] }) };
  vm.runInNewContext(page.slice(page.indexOf('function submitAgent('), page.indexOf('function handleAgent(')), context);
  context.submitAgent('顺路办'); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(mapped, [last]);
});

test('route preview preserves tool order and numbers, rejects unknown places, and restores the day map', async () => {
  const trip = source('js/trip.js'), nodes = {}, classes = new Set();
  let drawn, loads = 0;
  const state = { plan: { pois: [{ id: 'a', x: 0, y: 0 }, { id: 'b', x: 2, y: 3 }], origin: { id: 'a' }, trips: [] } };
  const route = { order: ['乙', '甲'], poi_ids: ['b', 'a'], legs: [{ start: '乙', end: '甲', distance_m: 11.7, walk_minutes: 0.16 }], distance_m: 11.7, walk_minutes: 0.16, basis: '示意坐标' };
  const context = { state, $, Number, Promise, esc: String, load: async () => { loads++; },
    drawMap: plan => { drawn = plan; }, renderTrips() {},
    // page.html 里的浮层帮手是全局的（surfaceShow/Hide 定义在 head 的 inline 脚本里），
    // 这里只切 renderRoute 那一段来跑，所以按 reduce-motion 那条最短路径补两个替身。
    surfaceShow(el) { if (el) el.hidden = false; },
    surfaceHide(el, done) { if (el) el.hidden = true; if (done) done(); },
    document: { body: { classList: { add: name => classes.add(name), remove: name => classes.delete(name) } },
      createElement: () => ({ setAttribute() {}, appendChild(button) { this.button = button; }, addEventListener(name, fn) { this[name] = fn; } }) } };
  function $(id) { return nodes[id]; }
  nodes['trip-stage'] = { appendChild(panel) { nodes[panel.id] = panel; } };
  vm.runInNewContext(trip.slice(trip.indexOf('  function renderRoute('), trip.indexOf('  function render(plan,')), context);
  await context.renderRoute(route);
  assert.equal(loads, 0); assert.equal(state.route, route);
  assert.match(nodes['trip-route'].innerHTML, /乙 → 甲/);
  assert.match(nodes['trip-route'].innerHTML, /11.7 米 · 步行 0.16 分钟/);
  assert.match(nodes['trip-route'].innerHTML, /乙 → 甲：11.7 米 \/ 0.16 分钟/);
  await assert.rejects(context.renderRoute({ ...route, poi_ids: ['missing', 'a'] }), /不在这张地图/);
  await assert.rejects(context.renderRoute({ ...route, legs: [] }), /路线结果不完整/);
  assert.equal(state.route, route);
  nodes['trip-route'].button.click();
  assert.equal(state.route, null); assert.equal(nodes['trip-route'].hidden, true);
  assert.equal(drawn, state.plan); assert.equal(classes.has('has-route'), false);
});

test('chat maps only the last successful route tool result', async () => {
  const page = source('page.html'), mapped = [];
  const first = { poi_ids: ['first'] }, last = { poi_ids: ['last'] };
  const context = { window: { nhTrip: { renderRoute: async route => { mapped.push(route); } } },
    wakeAgentDock() {}, agentLine() {}, agentWait() {}, agentWaitDone() {}, agentTrace() {},
    sessionStorage: { getItem: () => '' }, api: async () => ({ trace: [
      { kind: 'call', tool: 'plan_route', ok: true, result: first },
      { kind: 'call', tool: 'plan_route', ok: true, result: last },
      { kind: 'call', tool: 'plan_route', ok: false, result: { poi_ids: ['failed'] } }
    ] }) };
  vm.runInNewContext(page.slice(page.indexOf('function submitAgent('), page.indexOf('function handleAgent(')), context);
  context.submitAgent('顺路办'); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(mapped, [last]);
});

test('route preview preserves tool order and numbers, rejects unknown places, and restores the day map', async () => {
  const trip = source('js/trip.js'), nodes = {}, classes = new Set();
  let drawn, loads = 0;
  const state = { plan: { pois: [{ id: 'a', x: 0, y: 0 }, { id: 'b', x: 2, y: 3 }], origin: { id: 'a' }, trips: [] } };
  const route = { order: ['乙', '甲'], poi_ids: ['b', 'a'], legs: [{ start: '乙', end: '甲', distance_m: 11.7, walk_minutes: 0.16 }], distance_m: 11.7, walk_minutes: 0.16, basis: '示意坐标' };
  const context = { state, $, Number, Promise, esc: String, load: async () => { loads++; },
    drawMap: plan => { drawn = plan; }, renderTrips() {},
    // page.html 里的浮层帮手是全局的（surfaceShow/Hide 定义在 head 的 inline 脚本里），
    // 这里只切 renderRoute 那一段来跑，所以按 reduce-motion 那条最短路径补两个替身。
    surfaceShow(el) { if (el) el.hidden = false; },
    surfaceHide(el, done) { if (el) el.hidden = true; if (done) done(); },
    document: { body: { classList: { add: name => classes.add(name), remove: name => classes.delete(name) } },
      createElement: () => ({ setAttribute() {}, appendChild(button) { this.button = button; }, addEventListener(name, fn) { this[name] = fn; } }) } };
  function $(id) { return nodes[id]; }
  nodes['trip-stage'] = { appendChild(panel) { nodes[panel.id] = panel; } };
  vm.runInNewContext(trip.slice(trip.indexOf('  function renderRoute('), trip.indexOf('  function render(plan,')), context);
  await context.renderRoute(route);
  assert.equal(loads, 0); assert.equal(state.route, route);
  assert.match(nodes['trip-route'].innerHTML, /乙 → 甲/);
  assert.match(nodes['trip-route'].innerHTML, /11.7 米 · 步行 0.16 分钟/);
  assert.match(nodes['trip-route'].innerHTML, /乙 → 甲：11.7 米 \/ 0.16 分钟/);
  await assert.rejects(context.renderRoute({ ...route, poi_ids: ['missing', 'a'] }), /不在这张地图/);
  await assert.rejects(context.renderRoute({ ...route, legs: [] }), /路线结果不完整/);
  assert.equal(state.route, route);
  nodes['trip-route'].button.click();
  assert.equal(state.route, null); assert.equal(nodes['trip-route'].hidden, true);
  assert.equal(drawn, state.plan); assert.equal(classes.has('has-route'), false);
});

test('bundled manifest and page expose local icons and accessible installation help', () => {
  const manifest = JSON.parse(source('manifest.webmanifest'));
  assert.equal(manifest.name, '群务台');
  assert.equal(manifest.display, 'standalone');
  for (const size of ['192x192', '512x512']) assert(manifest.icons.some(icon => icon.sizes === size));
  const page = source('page.html');
  assert.match(page, /register\('\/js\/sw.js', \{scope: '\/'/);
  assert.match(page, /id="pwa-install"[^>]+hidden/);
  assert.match(page, /safe-area-inset-top/);
});

test('every script the page loads is precached, so offline keeps the map basemap', () => {
  const page = source('page.html');
  const shell = source('js/sw.js').match(/const SHELL = \[([\s\S]*?)\];/)[1]
    .match(/'([^']+)'/g).map(item => item.slice(1, -1));
  // 迟到的模块（map3d.js 在 load 之后才 import）也算「页面会加载的脚本」，断网一样得在手边。
  const loaded = [...page.matchAll(/<script[^>]*\ssrc="([^"]+)"|import\('\.\/(js\/[^']+)'\)/g)]
    .map(match => '/' + (match[1] || match[2]));
  assert(loaded.length > 0, '页面没引用任何脚本？这条检查会变成空转');
  assert.deepEqual(loaded.filter(path => !shell.includes(path)), [], '线上加载的脚本没进离线包');
  for (const path of shell.filter(item => item !== '/')) {
    assert.doesNotThrow(() => readFileSync(new URL('../noticehub/web_static' + path, import.meta.url)), path + ' 进了离线包但磁盘上没有');
  }
});
