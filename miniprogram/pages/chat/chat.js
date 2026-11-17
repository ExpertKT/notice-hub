const api = require('../../services/api');

// 服务端 trace 有 read（读了待办）、plan（打算怎么做）、call（真调了工具）三类。
function formatSteps(trace) {
  if (!Array.isArray(trace)) return [];
  return trace.map(step => {
    if (!step || typeof step !== 'object') return '';
    if (step.kind === 'call') {
      const detail = typeof step.detail === 'string' && step.detail ? ' · ' + step.detail : '';
      return (step.tool || '工具') + (step.ok ? ' · 已执行' : ' · 未完成') + detail;
    }
    return typeof step.text === 'string' ? step.text : '';
  }).filter(Boolean);
}

Page({
  data: { text: '', messages: [], sending: false, waitedText: '', error: null, sessionId: '' },
  onShow() {
    const config = api.loadConfig();
    const connection = JSON.stringify(config);
    if (this.connection && connection !== this.connection) this.setData({ messages: [], sessionId: '', error: null });
    this.connection = connection;
    if (!api.hasConfig()) this.setData({ error: { title: '还没配置连接', message: '打开连接设置，填好服务地址和访问令牌。', settings: true } });
  },
  onHide() { this.stopRequest(); },
  onUnload() { this.stopRequest(); },
  openSettings: api.openSettings,
  openTasks() { wx.switchTab({ url: '/pages/tasks/tasks' }); },
  updateText(event) { this.setData({ text: event.detail.value }); },
  stopWait() {
    clearInterval(this.waitTimer);
    this.waitTimer = null;
    this.setData({ waitedText: '' });
  },
  stopRequest() {
    this.stopWait();
    const pending = this.pending;
    this.pending = null;
    if (pending) {
      pending.abort();
      this.setData({ sending: false, error: { title: '已停止等待', message: '服务端可能已执行。请先去待办页刷新核对，再决定是否重发。', settings: false } });
    }
  },
  send() {
    const text = this.data.text.trim();
    if (this.data.sending || !text) return;
    if (text.length > 500) { this.setData({ error: { title: '这句话太长了', message: '请缩到 500 字以内再发。', settings: false } }); return; }
    const messages = this.data.messages.concat([{ role: 'user', text }]).slice(-39);
    this.setData({ sending: true, waitedText: '', error: null, messages });
    let waited = 0;
    this.waitTimer = setInterval(() => {
      waited += 1;
      this.setData({ waitedText: '助手正在回复 · ' + waited + ' 秒' + (waited >= 45 ? ' · 还在跑' : '') });
    }, 1000);
    // ponytail: 页面内保留最近 40 条；需要跨启动历史时再接 GET /api/agent/chat。
    const pending = api.request('/api/agent/chat', { method: 'POST', body: { session_id: this.data.sessionId, text }, timeout: 300000 });
    this.pending = pending;
    pending.then(reply => {
      if (this.pending !== pending) return;
      if (reply.ok !== true || typeof reply.say !== 'string' || !reply.say.trim() || typeof reply.session_id !== 'string') {
        throw { title: reply.fallback ? '助手还没启用' : '助手没有返回可用结果', message: reply.fallback ? '请在电脑端检查模型配置，然后再试。' : '请先去待办页刷新核对，确认这句话是否已执行，再决定是否重发。', settings: false };
      }
      this.setData({ messages: messages.concat([{ role: 'assistant', text: reply.say, steps: formatSteps(reply.trace) }]).slice(-40), sessionId: reply.session_id, text: '' });
    }).catch(error => {
      if (this.pending !== pending) return;
      this.setData({ error: { title: error.title || '发送失败', message: (error.message || '没有收到回复。') + ' 输入已保留；先查待办，避免重复提交。', settings: Boolean(error.settings) } });
    }).finally(() => {
      if (this.pending !== pending) return;
      this.stopWait();
      this.pending = null;
      this.setData({ sending: false });
    });
  }
});
