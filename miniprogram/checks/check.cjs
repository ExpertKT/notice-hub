const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
let saved = {};
let responder;
let sent;
global.wx = {
  getStorageSync: () => saved,
  setStorageSync: (_, value) => { saved = value; },
  request: options => { sent = options; if (responder) responder(options); return { abort: () => options.fail({ errMsg: 'request:fail abort' }) }; }
};
const api = require('../services/api');
const { parseBoard } = require('../services/tasks');
function loadPage(file) {
  let page;
  global.Page = value => { page = value; };
  require(file);
  page.data = JSON.parse(JSON.stringify(page.data));
  page.setData = (update, callback) => { Object.assign(page.data, update); if (callback) callback(); };
  return page;
}
const turn = () => new Promise(resolve => setImmediate(resolve));
async function run() {
  const app = JSON.parse(fs.readFileSync(path.join(root, 'app.json'), 'utf8'));
  for (const page of app.pages) for (const ext of ['js', 'json', 'wxml', 'wxss']) assert.ok(fs.existsSync(path.join(root, page + '.' + ext)), page + '.' + ext);
  assert.equal(app.tabBar.list.length, 3);
  assert.equal(app.darkmode, true);
  const themes = JSON.parse(fs.readFileSync(path.join(root, app.themeLocation), 'utf8'));
  assert.equal(themes.light.page, '#EDE7D6');
  assert.equal(themes.dark.page, '#23231B');
  const asset = fs.readFileSync(path.join(root, 'assets/campus.js'), 'utf8');
  // 资产只允许注释 + 一个纯数据 module.exports，防止有人往里写逻辑。
  assert.match(asset, /抄自服务端 noticehub\/application\/campus\.py/);
  const original = asset.slice(asset.indexOf('module.exports = ') + 'module.exports = '.length).replace(/;\s*$/, '');
  assert.doesNotMatch(original, /require\(|function/);
  const campusData = require('../assets/campus');
  assert.deepEqual(campusData, new Function('return (' + original + ');')());
  // Git 会转换行尾，按解析后的数据验哈希，避免 CRLF/LF 造成假失败。
  assert.equal(require('node:crypto').createHash('sha256').update(JSON.stringify(campusData)).digest('hex'), 'ee81e3563caa6f08e5d05d428532fa320a283cb31b67334d2cc1e81ecff7ffc2');
  assert.deepEqual(campusData.size_m, [787, 1048]);
  // 地点表必须与服务端 campus.py 的 27 条 POI 对齐（楼名 + 别名都算）。
  assert.equal(campusData.places.length, 27);
  assert.deepEqual(campusData.places.map(place => place.n).slice(0, 3), ['1号楼', '2号楼', '3号楼']);
  assert.deepEqual(campusData.places.find(place => place.id === 'gym').a, ['润湖体育馆', '体育馆']);
  assert.equal(api.hasConfig(), false);
  await assert.rejects(api.request('/api/tasks'), error => error.settings && /配置/.test(error.title));
  for (const address of ['', 'file://host', 'https://host/api', 'https://user:secret@host', 'https://host?q=1', 'http://host:70000']) assert.throws(() => api.parseConfig(address, 'token'));
  assert.throws(() => api.parseConfig('https://host', 'bad\ntoken'));
  assert.deepEqual(api.saveConfig(' https://host/ ', ' token '), { baseUrl: 'https://host', token: 'token' });
  assert.equal(api.hasConfig(), true);
  responder = options => options.success({ statusCode: 200, data: { today: [] } });
  await api.request('/api/tasks');
  assert.equal(sent.header['X-Token'], 'token');
  assert.equal(sent.url, 'https://host/api/tasks');
  assert.equal(sent.timeout, 20000);
  for (const status of [400, 401, 403, 404, 429, 500, 503]) {
    responder = options => options.success({ statusCode: status, data: {} });
    await assert.rejects(api.request('/api/tasks'), error => error.title === api.describeError(status).title);
  }
  responder = options => options.fail({ errMsg: 'request:fail timeout' });
  await assert.rejects(api.request('/api/tasks'), error => error.title === '没有连上服务');
  responder = options => options.success({ statusCode: 200, data: '<html>login</html>' });
  await assert.rejects(api.request('/api/tasks'), error => error.title === '响应格式不对');
  responder = null;
  const pending = api.request('/api/tasks'); pending.abort();
  await assert.rejects(pending, error => error.title === '请求已停止');
  const board = { candidates: [], today: [{ id: 1, summary: '实验报告', deadline: '2026-11-17T15:00', groups: ['课程群'] }], week: [], later: [], done: [] };
  const sections = parseBoard(board);
  assert.equal(sections[0].items[0].deadline, '2026-11-17 15:00');
  assert.equal(sections[0].items[0].groups, '课程群');
  assert.throws(() => parseBoard({}));
  assert.throws(() => parseBoard({ ...board, today: [null] }));
  const chatSource = fs.readFileSync(path.join(root, 'pages/chat/chat.js'), 'utf8');
  assert.match(chatSource, /timeout: 300000/);
  assert.match(fs.readFileSync(path.join(root, 'pages/chat/chat.wxml'), 'utf8'), /{{waitedText \|\| '助手正在回复，可以停止等待。'}}/);
  const readme = fs.readFileSync(path.join(root, 'README.md'), 'utf8');
  assert.match(readme, /对话 300 秒/);
  assert.doesNotMatch(readme, /对话 90 秒/);
  const chat = loadPage('../pages/chat/chat');
  chat.useExample();
  assert.equal(chat.data.text, '明天下午三点交实验报告');
  const nativeSet = global.setInterval, nativeClear = global.clearInterval;
  const timers = new Map();
  let timerId = 0;
  global.setInterval = (callback, delay) => { assert.equal(delay, 1000); timers.set(++timerId, callback); return timerId; };
  global.clearInterval = id => timers.delete(id);
  const cleared = () => { assert.equal(timers.size, 0); assert.equal(chat.waitTimer, null); assert.equal(chat.data.waitedText, ''); };
  try {
    chat.setData({ text: '明天交实验报告' });
    responder = options => options.success({ statusCode: 200, data: { ok: true, say: '已新增待办', session_id: 'session123', changed: true, trace: [{ kind: 'read', text: '读了 2 件待办' }, { kind: 'plan', text: '打算新建一件' }, { kind: 'call', tool: 'create_task', ok: true, detail: '已新建「实验报告」' }, { kind: 'call', tool: 'day_plan', ok: false, detail: '没有空档' }, null] } });
    chat.send(); await turn();
    assert.deepEqual(sent.data, { session_id: '', text: '明天交实验报告' });
    assert.equal(sent.method, 'POST');
    assert.equal(sent.timeout, 300000);
    assert.equal(chat.data.sessionId, 'session123');
    assert.equal(chat.data.messages[1].text, '已新增待办');
    assert.deepEqual(chat.data.messages[1].steps, ['读了 2 件待办', '打算新建一件', 'create_task · 已执行 · 已新建「实验报告」', 'day_plan · 未完成 · 没有空档']);
    assert.equal(chat.data.messages[0].steps, undefined);
    assert.equal(chat.data.text, '');
    cleared();
    chat.setData({ text: '问一个问题' });
    responder = options => options.success({ statusCode: 200, data: { ok: false, fallback: true } });
    chat.send(); await turn();
    assert.equal(chat.data.error.title, '助手还没启用');
    assert.equal(chat.data.text, '问一个问题');
    cleared();
    responder = null; chat.send();
    const tick = timers.get(chat.waitTimer);
    tick(); assert.equal(chat.data.waitedText, '助手正在回复 · 1 秒');
    for (let n = 1; n < 44; n += 1) tick();
    assert.equal(chat.data.waitedText, '助手正在回复 · 44 秒');
    tick(); assert.equal(chat.data.waitedText, '助手正在回复 · 45 秒 · 还在跑');
    chat.send(); assert.equal(timers.size, 1);
    chat.stopRequest(); cleared();
    // 旧请求的 finally 不能清掉紧接着发起的新请求计时器。
    chat.send(); const currentTimer = chat.waitTimer;
    await turn(); assert.equal(chat.waitTimer, currentTimer); assert.equal(timers.size, 1);
    chat.onHide(); cleared(); await turn();
    assert.equal(chat.data.sending, false);
    assert.match(chat.data.error.message, /可能已执行/);
    chat.send(); chat.onUnload(); cleared(); await turn();
    responder = options => options.fail({ errMsg: 'request:fail timeout' });
    chat.send(); await turn(); cleared();
    assert.equal(chat.data.error.title, '没有连上服务');
  } finally { global.setInterval = nativeSet; global.clearInterval = nativeClear; }
  const settings = loadPage('../pages/settings/settings');
  settings.setData({ baseUrl: 'https://host', token: 'token' });
  responder = null;
  settings.check();
  const oldRequest = sent;
  settings.stopRequest();
  responder = options => options.success({ statusCode: 200, data: { ok: true, sources: { qq: { label: 'QQ', state: 'ready' } } } });
  settings.check();
  oldRequest.success({ statusCode: 200, data: { ok: true, sources: { old: { label: '过期结果', state: 'error' } } } });
  await turn();
  assert.equal(settings.data.sources[0].label, 'QQ');
  assert.equal(settings.data.checking, false);
  responder = options => options.success({ statusCode: 200, data: { ok: true, sources: { qq: { label: 'QQ', state: 'bogus' } } } });
  settings.check(); await turn();
  assert.match(settings.data.error, /格式不对/);
  assert.equal(settings.data.result, '');
  responder = null; settings.check(); settings.onUnload(); await turn();
  assert.equal(settings.data.checking, false);
  assert.match(settings.data.error, /已停止/);
  const tasks = loadPage('../pages/tasks/tasks');
  const requests = [];
  const toasts = [];
  wx.stopPullDownRefresh = () => {};
  wx.showToast = options => toasts.push(options.title);
  wx.switchTab = () => {};
  const taskBoard = { candidates: [], today: [{ id: 1, summary: '图书馆还书', deadline: '2026-11-17T15:00' }], week: [], later: [], done: [{ id: 2, summary: '已办完的事' }] };
  responder = options => {
    requests.push(options);
    if (/\/api\/agent\/brief$/.test(options.url)) return options.success({ statusCode: 200, data: { sentence: '今天还有 1 件' } });
    if (options.method === 'POST') return options.success({ statusCode: 200, data: { ok: true, id: 1, action: 'done', done: true, status: 'done' } });
    return options.success({ statusCode: 200, data: taskBoard });
  };
  tasks.onShow();
  for (let n = 0; n < 4; n += 1) await turn();
  assert.equal(tasks.data.sections[0].items[0].summary, '图书馆还书');
  assert.equal(tasks.data.brief, '今天还有 1 件');
  assert.equal(tasks.data.loading, false);
  // 两次连点只发一次写请求。
  requests.length = 0;
  tasks.toggleTask({ currentTarget: { dataset: { id: 1, done: false } } });
  tasks.toggleTask({ currentTarget: { dataset: { id: 1, done: false } } });
  for (let n = 0; n < 4; n += 1) await turn();
  const posts = requests.filter(item => item.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, 'https://host/api/tasks/1');
  assert.deepEqual(posts[0].data, { action: 'done' });
  assert.equal(posts[0].header['X-Token'], 'token');
  assert.equal(toasts.pop(), '已完成');
  assert.equal(tasks.data.busyId, 0);
  assert.equal(tasks.data.actionError, '');
  assert.ok(requests.some(item => item.method === 'GET' && /\/api\/tasks$/.test(item.url)));
  // 服务端没确认：给出下一步，列表仍重读。
  requests.length = 0;
  responder = options => {
    requests.push(options);
    if (options.method === 'POST') return options.success({ statusCode: 200, data: { ok: false } });
    return options.success({ statusCode: 200, data: taskBoard });
  };
  tasks.toggleTask({ currentTarget: { dataset: { id: 2, done: true } } });
  for (let n = 0; n < 4; n += 1) await turn();
  const reopen = requests.filter(item => item.method === 'POST');
  assert.equal(reopen.length, 1);
  assert.deepEqual(reopen[0].data, { action: 'reopen' });
  assert.equal(reopen[0].url, 'https://host/api/tasks/2');
  assert.match(tasks.data.actionError, /服务端没有确认/);
  assert.match(tasks.data.actionError, /仍按原样保留/);
  assert.equal(tasks.data.busyId, 0);
  const map = loadPage('../pages/map/map');
  const campus = require('../assets/campus');
  const calls = [];
  const context = new Proxy({}, { get: (target, name) => name in target ? target[name] : (...args) => calls.push([name, ...args]) });
  wx.createSelectorQuery = () => ({ in() { return this; }, select() { return this; }, boundingClientRect(callback) { callback({ width: 326, height: 310 }); return this; }, exec() {} });
  wx.createCanvasContext = () => context;
  for (const theme of ['light', 'dark']) {
    calls.length = 0;
    wx.getAppBaseInfo = () => ({ theme });
    map.renderMap();
    assert.ok(calls.some(call => call[0] === 'setFillStyle' && call[1] === '#D6C7A4'));
    assert.ok(calls.some(call => call[0] === 'setStrokeStyle' && call[1] === (theme === 'dark' ? '#97A878' : '#7B8B5E')));
    assert.ok(calls.some(call => call[0] === 'setFillStyle' && call[1] === (theme === 'dark' ? '#8A9A6C' : '#9AA87C')));
    // 路线是编的，已删掉；画布上不该再有虚线或序号圆点。
    assert.equal(calls.filter(call => call[0] === 'setLineDash').length, 0);
    assert.equal(calls.filter(call => call[0] === 'arc').length, 0);
    assert.match(context.font, /Songti SC/);
    const shapes = calls.filter(call => call[0] === 'strokeRect');
    assert.equal(shapes.length, 27);
    const scale = Math.min((326 - 32) / 787, (310 - 64) / 1048);
    const left = (326 - 787 * scale) / 2;
    const top = (310 - 1048 * scale) / 2;
    const first = campus.places[0];
    assert.equal(shapes[0][1], left + (first.x - 13) * scale);
    assert.equal(shapes[0][2], top + (1048 - first.y - 13) * scale);
    for (const shape of shapes) assert.ok(shape.slice(1).every(Number.isFinite) && shape[3] > 0 && shape[4] > 0);
    // 只标选中的地点和命中待办的地点；27 个全标会糊成一团。
    const labels = calls.filter(call => call[0] === 'fillText').map(call => call[1]);
    assert.deepEqual(labels, [first.n, first.n + ' · 1 层']);
  }
  responder = options => options.success({ statusCode: 200, data: { candidates: [], today: [{ id: 1, summary: '3号楼 405 交实验报告' }, { id: 2, summary: '周三前把 图书馆 三楼的书还了' }], week: [{ id: 3, summary: '11号楼 楼下取快递' }], later: [{ id: 4, summary: '去润湖体育馆体检' }], done: [{ id: 5, summary: '1号楼 送水' }, { id: 6, summary: '把课程表发到群里' }] } });
  map.loadPlaces(); await turn();
  // 「11号楼」不能算成「1号楼」；「润湖体育馆」是别名，要落到「汤湖体育馆」。
  assert.deepEqual(map.data.placeHits.map(hit => [hit.name, hit.count]), [['1号楼', 1], ['3号楼', 1], ['11号楼', 1], ['图书馆', 1], ['汤湖体育馆', 1]]);
  assert.equal(map.data.placeError, '');
  calls.length = 0;
  map.renderMap();
  assert.equal(calls.filter(call => call[0] === 'arc').length, 5);
  // 命中待办的五个地点都带上了楼名。
  const hitLabels = calls.filter(call => call[0] === 'fillText').map(call => call[1]);
  for (const name of ['1号楼', '3号楼', '11号楼', '图书馆', '汤湖体育馆']) assert.ok(hitLabels.includes(name), name);
  // canvas 不折行：超长楼名截到 7 字加省略号。
  calls.length = 0;
  map.setData({ selectedName: '一二三四五六七八九十' });
  map.renderMap();
  assert.ok(calls.some(call => call[0] === 'fillText' && call[1] === '一二三四五六七… · 1 层'));
  map.setData({ selectedName: map.data.buildings[0].name });
  responder = options => options.success({ statusCode: 200, data: { candidates: [], today: [{ id: 9, summary: '洗衣服' }], week: [], later: [], done: [] } });
  map.loadPlaces(); await turn();
  assert.deepEqual(map.data.placeHits, []);
  assert.equal(map.data.placeLoaded, true);
  assert.equal(map.data.placeError, '');
  assert.equal(map.data.placeLoading, false);
  responder = options => options.fail({ errMsg: 'request:fail' });
  map.loadPlaces(); await turn();
  assert.deepEqual(map.data.placeHits, []);
  assert.match(map.data.placeError, /没有连上服务/);
  assert.match(map.data.placeError, /地图本身不受影响/);
  responder = null;
  let listener;
  wx.onThemeChange = callback => { listener = callback; };
  wx.offThemeChange = callback => { assert.equal(callback, listener); listener = null; };
  let renders = 0;
  map.renderMap = () => { renders += 1; };
  map.onReady(); listener(); map.onUnload();
  assert.equal(listener, null);
  assert.equal(renders, 2);
  renders = 0;
  map.selectBuilding({ currentTarget: { dataset: { id: 'gym' } } });
  assert.equal(map.data.selectedName, '汤湖体育馆');
  assert.deepEqual(map.data.floors, [1, 2, 3]);
  map.selectFloor({ currentTarget: { dataset: { floor: 3 } } });
  assert.equal(map.data.floor, 3);
  map.selectFloor({ currentTarget: { dataset: { floor: 99 } } });
  assert.equal(map.data.floor, 3);
  // 运动场没有楼层，至少留一层可选，不能是空列表。
  map.selectBuilding({ currentTarget: { dataset: { id: 'field' } } });
  assert.deepEqual(map.data.floors, [1]);
  assert.equal(renders, 3);
  // 界面约束：四页都有深色块；可点控件 ≥ 88rpx；空态/加载态/长文本规则在位。
  for (const page of app.pages) {
    const wxss = fs.readFileSync(path.join(root, page + '.wxss'), 'utf8');
    assert.match(wxss, /prefers-color-scheme: dark/, page + '.wxss 缺深色');
    // WXML 里不带花括号的 wx:if 是字符串字面量，恒为真：错误面板、骨架、空态会一起显示。
    assert.doesNotMatch(fs.readFileSync(path.join(root, page + '.wxml'), 'utf8'), /wx:if="[^{]/, page + '.wxml 的 wx:if 少了 {{ }}');
  }
  const appWxss = fs.readFileSync(path.join(root, 'app.wxss'), 'utf8');
  assert.match(appWxss, /button \{[^}]*min-height: 88rpx/);
  assert.match(appWxss, /\.small \{[^}]*min-width: 88rpx; min-height: 88rpx;/);
  const tasksWxml = fs.readFileSync(path.join(root, 'pages/tasks/tasks.wxml'), 'utf8');
  assert.match(tasksWxml, /class="skeleton"/);
  assert.match(tasksWxml, /去对话说一句/);
  assert.match(fs.readFileSync(path.join(root, 'pages/tasks/tasks.wxss'), 'utf8'), /\.skeleton-line/);
  assert.match(fs.readFileSync(path.join(root, 'pages/chat/chat.wxml'), 'utf8'), /bindtap="useExample"/);
  assert.match(fs.readFileSync(path.join(root, 'pages/chat/chat.wxss'), 'utf8'), /overflow-wrap: break-word/);
  assert.match(fs.readFileSync(path.join(root, 'pages/map/map.wxss'), 'utf8'), /overflow-wrap: break-word/);
  assert.match(fs.readFileSync(path.join(root, 'pages/map/map.wxml'), 'utf8'), /bindtap="loadPlaces"/);
  const doc = fs.readFileSync(path.resolve(root, '../docs/contest/小程序说明.md'), 'utf8');
  assert.match(doc, /mp\.weixin\.qq\.com/);
  console.log('PASS: project pages, config, HTTP failure classes, headers, abort, board parsing, chat success/fallback/cancel/300s/wait timer cleanup/plan trace, tasks board + done-reopen guard, settings stale reply/malformed/cancel, 27 real campus places with alias priority, map selection/place markers/empty state/label thinning/canvas truncation, dark mode per page, 88rpx targets, skeleton and example CTA, wx:if braces');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
