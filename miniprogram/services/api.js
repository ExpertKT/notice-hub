const CONFIG_KEY = 'noticehub.connection';

function parseConfig(baseUrl, token) {
  const address = String(baseUrl || '').trim().replace(/\/+$/, '');
  const secret = String(token || '').trim();
  if (!/^https?:\/\/(?:\[[a-f\d:]+\]|[a-z\d.-]+)(?::\d{1,5})?$/i.test(address)) {
    throw new Error('服务地址请填 http:// 或 https:// 开头的完整地址，可带端口，不要加路径、查询参数或账号密码。');
  }
  const port = address.match(/:(\d+)$/);
  if (port && (Number(port[1]) < 1 || Number(port[1]) > 65535)) throw new Error('端口请填 1 到 65535。');
  if (!secret || /\s/.test(secret)) throw new Error('请填写访问令牌，令牌中不要带空格或换行。');
  return { baseUrl: address, token: secret };
}

function loadConfig() {
  try { return wx.getStorageSync(CONFIG_KEY) || {}; }
  catch (_) { return {}; }
}

function saveConfig(baseUrl, token) {
  const config = parseConfig(baseUrl, token);
  wx.setStorageSync(CONFIG_KEY, config);
  return config;
}

function hasConfig() {
  const config = loadConfig();
  try { parseConfig(config.baseUrl, config.token); return true; }
  catch (_) { return false; }
}

function openSettings() { wx.navigateTo({ url: '/pages/settings/settings' }); }

function describeError(status) {
  if (status === 401 || status === 403) return { title: '访问令牌未通过', message: '请打开连接设置，核对群务台的访问令牌后再试。', settings: true };
  if (status === 404) return { title: '接口不存在（404）', message: '核对服务地址。对话和今况需要服务端启用 NOTICEHUB_AGENT_SHELL=1。', settings: true };
  if (status >= 500) return { title: '服务处理失败（' + status + '）', message: '请查看电脑端群务台的运行状态，再点重试。', settings: false };
  return { title: '请求未通过（' + status + '）', message: '请核对输入和服务地址，再点重试或修改连接设置。', settings: true };
}

function request(path, options) {
  const opts = options || {};
  let call;
  const promise = new Promise((resolve, reject) => {
    const saved = loadConfig();
    let config;
    try { config = parseConfig(saved.baseUrl, saved.token); }
    catch (_) { reject({ title: '还没配置连接', message: '请先填写服务地址和访问令牌。', settings: true }); return; }
    call = wx.request({
      url: config.baseUrl + path,
      method: opts.method || 'GET',
      data: opts.body,
      header: { 'X-Token': config.token, 'Content-Type': 'application/json' },
      timeout: opts.timeout || 20000,
      success(reply) {
        if (reply.statusCode < 200 || reply.statusCode >= 300) { reject(describeError(reply.statusCode)); return; }
        if (!reply.data || typeof reply.data !== 'object' || Array.isArray(reply.data)) {
          reject({ title: '响应格式不对', message: '服务没有返回 JSON 对象。请核对地址是否指向群务台接口。', settings: true }); return;
        }
        resolve(reply.data);
      },
      fail(error) {
        const cancelled = /abort/.test(error.errMsg || '');
        reject({ title: cancelled ? '请求已停止' : '没有连上服务', message: cancelled ? '没有收到结果；若刚才发送过对话，请先查待办，确认是否已执行。' : '检查电脑端服务、网络和域名配置，再点重试；也可打开连接设置修改地址。', settings: !cancelled });
      }
    });
  });
  promise.abort = () => { if (call) call.abort(); };
  return promise;
}

module.exports = { parseConfig, loadConfig, saveConfig, hasConfig, openSettings, describeError, request };
