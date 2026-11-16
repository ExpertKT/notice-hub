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
  const chat = loadPage('../pages/chat/chat');
  chat.setData({ text: '明天交实验报告' });
  responder = options => options.success({ statusCode: 200, data: { ok: true, say: '已新增待办', session_id: 'session123', changed: true } });
  chat.send(); await turn();
  assert.deepEqual(sent.data, { session_id: '', text: '明天交实验报告' });
  assert.equal(sent.method, 'POST');
  assert.equal(chat.data.sessionId, 'session123');
  assert.equal(chat.data.messages[1].text, '已新增待办');
  assert.equal(chat.data.text, '');
  chat.setData({ text: '问一个问题' });
  responder = options => options.success({ statusCode: 200, data: { ok: false, fallback: true } });
  chat.send(); await turn();
  assert.equal(chat.data.error.title, '助手还没启用');
  assert.equal(chat.data.text, '问一个问题');
  responder = null; chat.send(); chat.stopRequest(); await turn();
  assert.equal(chat.data.sending, false);
  assert.match(chat.data.error.message, /可能已执行/);
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
  const map = loadPage('../pages/map/map');
  let renders = 0;
  map.renderMap = () => { renders += 1; };
  map.selectBuilding({ currentTarget: { dataset: { id: 'library' } } });
  assert.equal(map.data.selectedName, '图书馆');
  assert.deepEqual(map.data.floors, [1, 2, 3]);
  map.selectFloor({ currentTarget: { dataset: { floor: 3 } } });
  assert.equal(map.data.floor, 3);
  map.selectFloor({ currentTarget: { dataset: { floor: 99 } } });
  assert.equal(map.data.floor, 3);
  assert.equal(renders, 2);
  console.log('PASS: project pages, config, HTTP failure classes, headers, abort, board parsing, chat success/fallback/cancel, settings stale reply/malformed/cancel, map selection');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
