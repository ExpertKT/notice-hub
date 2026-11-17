const api = require('../../services/api');
const { parseBoard } = require('../../services/tasks');
Page({
  data: { sections: [], loading: false, error: null, loaded: false, busyId: 0, actionError: '', brief: '', briefError: '' },
  onShow() {
    if (!api.hasConfig()) {
      this.setData({ sections: [], loaded: false, brief: '', error: { title: '先连接你的群务台', message: '填写电脑端服务地址和访问令牌后，这里会显示真实待办。', settings: true } });
      api.openSettings(); return;
    }
    this.loadTasks();
  },
  onHide() { this.stopRequest(); },
  onUnload() { this.stopRequest(); },
  onPullDownRefresh() { this.loadTasks(); },
  stopRequest() {
    const pending = this.pending;
    this.pending = null;
    if (pending) pending.abort();
    if (this.briefCall) { this.briefCall.abort(); this.briefCall = null; }
    this.setData({ loading: false, busyId: 0 });
    wx.stopPullDownRefresh();
  },
  toggleTask(event) {
    const id = event.currentTarget.dataset.id;
    const action = event.currentTarget.dataset.done ? 'reopen' : 'done';
    if (this.busyId) return;
    this.busyId = id;
    this.setData({ busyId: id, actionError: '' });
    const pending = api.request('/api/tasks/' + id, { method: 'POST', body: { action } });
    this.pending = pending;
    pending.then(reply => {
      if (this.pending !== pending) return;
      if (reply.ok !== true) throw { title: '服务端没有确认', message: '这可能是因为待办已被改动，列表会重新读取。' };
      wx.showToast({ title: action === 'done' ? '已完成' : '已恢复', icon: 'success' });
    }).catch(error => {
      if (this.pending !== pending) return;
      this.setData({ actionError: (error.title || '没有改成') + '：' + (error.message || '请再点一次或刷新列表。') + ' 待办仍按原样保留。' });
    }).finally(() => {
      this.busyId = null;
      this.setData({ busyId: 0 });
      if (this.pending !== pending) return;
      this.pending = null;
      this.loadTasks();
    });
  },
  openSettings: api.openSettings,
  openChat() { wx.switchTab({ url: '/pages/chat/chat' }); },
  loadTasks() {
    if (this.data.loading) return;
    this.setData({ loading: true, error: null, sections: [], loaded: false, brief: '', briefError: '' });
    const pending = api.request('/api/tasks');
    this.pending = pending;
    pending.then(board => {
      if (this.pending !== pending) return;
      this.setData({ sections: parseBoard(board), loaded: true });
    }).catch(error => {
      if (this.pending !== pending) return;
      this.setData({ error: error.title ? error : { title: '待办读取失败', message: error.message, settings: false } });
    }).finally(() => {
      if (this.pending !== pending) return;
      this.pending = null;
      this.setData({ loading: false });
      wx.stopPullDownRefresh();
    });
    const briefCall = api.request('/api/agent/brief');
    this.briefCall = briefCall;
    briefCall.then(reply => {
      if (this.briefCall !== briefCall) return;
      if (typeof reply.sentence !== 'string') throw new Error('今况格式不对');
      this.setData({ brief: reply.sentence });
    }).catch(() => {
      if (this.briefCall === briefCall) this.setData({ briefError: '今况暂时没读到，可点刷新重试。待办列表单独加载。' });
    });
  }
});
