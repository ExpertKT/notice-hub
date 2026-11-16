import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
const source = name => readFileSync(new URL('../noticehub/web_static/' + name, import.meta.url), 'utf8');

test('shell is network first; API, mutations and unknown assets never enter cache', async () => {
  const events = {}, saved = new Map(), writes = [];
  let offline = false, reply = new Response('fresh');
  const cache = { addAll: async urls => { assert(urls.includes('/js/three.module.min.js')); },
    put: async (key, value) => { writes.push(key); saved.set(key, value); }, match: async key => saved.get(key) };
  const context = { URL, caches: { open: async () => cache, keys: async () => ['foreign-cache', 'noticehub-shell-1'], delete: async key => { assert.equal(key, 'noticehub-shell-1'); } },
    fetch: async () => { if (offline) throw Error('offline'); return reply.clone(); },
    self: { location: { origin: 'https://test.local' }, clients: { claim() {} }, skipWaiting() {}, addEventListener: (name, fn) => { events[name] = fn; } } };
  vm.runInNewContext(source('js/sw.js'), context);
  for (const name of ['install', 'activate']) { let done; events[name]({ waitUntil: promise => { done = promise; } }); await done; }
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
  for (const id of ['trip-stage', 'trip-list', 'trip-map', 'trip-meta', 'trip-rest', 'trip-snapshot']) nodes[id] = { innerHTML: '', textContent: '', hidden: true };
  let error = null;
  const context = { console, Date, Number, JSON, navigator: { onLine: true }, localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    document: { readyState: 'loading', getElementById: id => nodes[id], body: { classList: { add() {}, remove() {} } }, addEventListener() {} },
    window: { addEventListener: (name, fn) => { events[name] = fn; } }, api: async () => { throw error; } };
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

test('map placement keeps a stop name and its pin together when crowded or out of frame', () => {
  const code = source('js/map3d.js');
  const body = code.slice(code.indexOf('function place(state, items)'), code.indexOf('\nfunction bind(host)'));
  class Vector3 { copy(at) { Object.assign(this, at); return this; } project() { return this; } }
  const context = { THREE: { Vector3 } };
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
  const document = { createElement: tag => new Node(tag), createTextNode: value => { const node = new Node('#text'); node.textContent = value; return node; }, getElementById: () => log };
  const context = { document, localStorage: { setItem: (key, value) => storage.set(key, value) } };
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
  const context = { Date: { now: () => now }, document: { getElementById: () => log, createElement: () => new Node() },
    window: { setTimeout: fn => { next = fn; return 1; }, clearTimeout: () => { cleared++; } }, agentReplyText() {}, rememberAgentLog() {} };
  vm.runInNewContext(page.slice(page.indexOf('function agentWait()'), page.indexOf('var agentPending =')), context);
  const waiting = context.agentWait(), label = waiting.children[0];
  assert.equal(label.textContent, '正在处理 · 0 秒');
  now = 12000; next(); assert.equal(label.textContent, '正在处理 · 12 秒');
  now = 45000; next(); assert.match(label.textContent, /45 秒 · 本机模型较慢，还在跑/);
  now = 151000; next(); assert.match(label.textContent, /151 秒/);
  context.agentWaitDone(waiting); assert.equal(cleared, 1); assert(waiting.removed);
  const submit = page.slice(page.indexOf('function submitAgent('), page.indexOf('function handleAgent('));
  assert.match(submit, /timeoutMs: 300000/);
  assert.match(page, /options.timeoutMs === undefined \? 30000/);
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
