const api = require('../../services/api');
Page({
  data: { baseUrl: '', token: '', error: '', checking: false, result: '', sources: [] },
  onLoad() { const config = api.loadConfig(); this.setData({ baseUrl: config.baseUrl || '', token: config.token || '' }); },
  onHide() { this.stopRequest(); },
  onUnload() { this.stopRequest(); },
  updateAddress(event) { this.setData({ baseUrl: event.detail.value, result: '', sources: [] }); },
  updateToken(event) { this.setData({ token: event.detail.value, result: '', sources: [] }); },
  save() {
    try {
      api.saveConfig(this.data.baseUrl, this.data.token);
      this.setData({ error: '', result: '连接信息已保存，还没有检查能否连通。' });
      return true;
    } catch (error) { this.setData({ error: error.message || '没有保存成功，请检查设备存储空间。', result: '' }); return false; }
  },
  openTasks() { if (this.save()) wx.switchTab({ url: '/pages/tasks/tasks' }); },
  openMap() { wx.switchTab({ url: '/pages/map/map' }); },
  stopRequest() {
    const pending = this.pending;
    this.pending = null;
    if (pending) {
      pending.abort();
      this.setData({ checking: false, result: '', error: '连接检查已停止。可以修改输入后重新检查。' });
    }
  },
  check() {
    if (this.data.checking || !this.save()) return;
    this.setData({ checking: true, result: '', sources: [] });
    const pending = api.request('/api/overview', { timeout: 45000 });
    this.pending = pending;
    pending.then(reply => {
      if (this.pending !== pending) return;
      if (reply.ok !== true || !reply.sources || typeof reply.sources !== 'object' || Array.isArray(reply.sources)) throw new Error('总览格式不对，请检查服务地址。');
      const states = { off: '未启用', needs_setup: '待配置', ready: '已就绪', error: '有异常' };
      const sources = Object.keys(reply.sources).map(key => {
        const source = reply.sources[key];
        if (!source || typeof source.label !== 'string' || !states[source.state]) throw new Error('通道状态格式不对。');
        return { key, label: source.label, state: states[source.state], detail: typeof source.detail === 'string' ? source.detail : '' };
      });
      this.setData({ result: '已读到服务总览。各通道状态见下方。', sources });
    }).catch(error => {
      if (this.pending === pending) this.setData({ error: error.title ? error.title + '。' + error.message : error.message, result: '' });
    }).finally(() => {
      if (this.pending !== pending) return;
      this.pending = null;
      this.setData({ checking: false });
    });
  }
});
