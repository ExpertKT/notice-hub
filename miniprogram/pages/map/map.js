const api = require('../../services/api');
const campus = require('../../assets/campus');

// 楼名与别名混在一张表里，长的先比、命中即停：这样「11号楼」不会被「1号楼」抢走。
const ALIASES = campus.places
  .reduce((all, place) => all.concat([place.n].concat(place.a || []).map(alias => ({ alias, id: place.id }))), [])
  .sort((a, b) => b.alias.length - a.alias.length);
// 画在平面上的方块边长（米），只影响观感，不是真实占地。
const SIDE = { dorm: 26, teach: 30, library: 34, food: 22, sport: 48 };

function matchPlace(text) {
  const hay = String(text || '');
  const hit = ALIASES.find(item => hay.indexOf(item.alias) >= 0);
  return hit ? hit.id : '';
}
// canvas 不会折行，楼名超过 8 字截断，其余界面按 CSS 折行。
function shortName(name) { return name && name.length > 8 ? name.slice(0, 7) + '…' : (name || ''); }
function floorsOf(place) { return Array.from({ length: Math.max(1, (place && place.f) || 1) }, (_, index) => index + 1); }

Page({
  data: {
    buildings: campus.places.map(({ id, n, f }) => ({ id, name: n, floors: Math.max(1, f || 1) })),
    selected: campus.places[0].id,
    selectedName: campus.places[0].n,
    floor: 1,
    floors: floorsOf(campus.places[0]),
    error: '',
    placeHits: [],
    placeError: '',
    placeLoaded: false,
    placeLoading: false
  },
  onReady() {
    this.themeListener = () => this.renderMap();
    wx.onThemeChange(this.themeListener);
    this.renderMap();
  },
  onUnload() {
    wx.offThemeChange(this.themeListener);
    if (this.placeCall) { this.placeCall.abort(); this.placeCall = null; }
  },
  onShow() { if (this.ready) this.renderMap(); this.loadPlaces(); },
  onResize() { this.renderMap(); },
  // 待办里没有地点字段，只按标题里提到的楼名或别名标记，属于文字匹配，不是定位。
  loadPlaces() {
    if (!api.hasConfig()) {
      this.setData({ placeHits: [], placeLoaded: false, placeError: '还没配置连接，待办地点标不出来。点右上角连接设置填地址和令牌。' });
      return;
    }
    if (this.placeCall) this.placeCall.abort();
    this.setData({ placeLoading: true, placeError: '' });
    const call = api.request('/api/tasks');
    this.placeCall = call;
    call.then(board => {
      if (this.placeCall !== call) return;
      const tasks = ['candidates', 'today', 'week', 'later', 'done']
        .reduce((all, key) => all.concat(Array.isArray(board[key]) ? board[key] : []), []);
      const counts = {};
      tasks.forEach(task => {
        const id = matchPlace(task && task.summary);
        if (id) counts[id] = (counts[id] || 0) + 1;
      });
      const hits = campus.places
        .filter(place => counts[place.id])
        .map(place => ({ id: place.id, name: place.n, count: counts[place.id] }));
      this.setData({ placeHits: hits, placeError: '', placeLoaded: true }, () => this.renderMap());
    }).catch(error => {
      if (this.placeCall !== call) return;
      this.setData({ placeHits: [], placeLoaded: false, placeError: (error.title || '待办地点没读到') + '：' + (error.message || '稍后重试。') + ' 地图本身不受影响。' });
    }).finally(() => {
      if (this.placeCall === call) this.placeCall = null;
      this.setData({ placeLoading: false });
    });
  },
  openSettings: api.openSettings,
  openChat() { wx.switchTab({ url: '/pages/chat/chat' }); },
  selectBuilding(event) {
    const place = campus.places.find(item => item.id === event.currentTarget.dataset.id);
    if (!place) return;
    this.setData({ selected: place.id, selectedName: place.n, floor: 1, floors: floorsOf(place) }, () => this.renderMap());
  },
  selectFloor(event) {
    const floor = Number(event.currentTarget.dataset.floor);
    if (!this.data.floors.includes(floor)) return;
    this.setData({ floor }, () => this.renderMap());
  },
  renderMap() {
    this.ready = true;
    wx.createSelectorQuery().in(this).select('#campus-map').boundingClientRect(rect => {
      if (!rect || !rect.width || !rect.height) { this.setData({ error: '地图画布没有就绪，请点重新绘制。' }); return; }
      try {
        const context = wx.createCanvasContext('campus-map', this);
        const dark = wx.getAppBaseInfo().theme === 'dark';
        const accent = dark ? '#97A878' : '#7B8B5E';
        const scale = Math.min((rect.width - 32) / campus.size_m[0], (rect.height - 64) / campus.size_m[1]);
        const left = (rect.width - campus.size_m[0] * scale) / 2;
        const top = (rect.height - campus.size_m[1] * scale) / 2;
        const hits = {};
        this.data.placeHits.forEach(hit => { hits[hit.id] = hit.count; });
        context.clearRect(0, 0, rect.width, rect.height);
        context.setFillStyle(dark ? '#23231B' : '#EDE7D6');
        context.fillRect(0, 0, rect.width, rect.height);
        context.font = '12px "Songti SC", "Noto Serif SC", serif';
        campus.places.forEach(place => {
          const side = SIDE[place.k] || 28;
          const x = left + (place.x - side / 2) * scale;
          // 坐标 y 向南，画布 y 向下，所以用总高减去。
          const y = top + (campus.size_m[1] - place.y - side / 2) * scale;
          const width = Math.max(6, side * scale);
          const selected = place.id === this.data.selected;
          context.setFillStyle(selected ? (dark ? '#8A9A6C' : '#9AA87C') : '#D6C7A4');
          context.setStrokeStyle(selected ? accent : (dark ? 'rgba(237,230,210,.28)' : 'rgba(58,58,40,.30)'));
          context.setLineWidth(selected ? 1.6 : 0.6);
          context.fillRect(x, y, width, width);
          context.strokeRect(x, y, width, width);
          // 27 个地点全标楼名会糊成一团，只标选中的和命中待办的那几个。
          if (selected || hits[place.id]) {
            context.setFillStyle(selected ? accent : (dark ? '#EDE6D2' : '#3A3A28'));
            context.setFontSize(selected ? 14 : 13);
            context.fillText(shortName(place.n), x, y + width + 14);
          }
          // 待办地点：楼块右上角画一个点加件数。
          if (hits[place.id]) {
            context.setFillStyle(accent);
            context.beginPath();
            context.arc(x + width, y, 8, 0, Math.PI * 2);
            context.fill();
            context.setFillStyle(dark ? '#20201A' : '#F6F2E7');
            context.setFontSize(10);
            context.fillText(String(hits[place.id]), x + width - 3, y + 4);
            context.setFontSize(12);
          }
        });
        context.setFillStyle(dark ? '#EDE6D2' : '#3A3A28');
        context.setFontSize(14);
        context.fillText(`${shortName(this.data.selectedName)} · ${this.data.floor} 层`, 14, 24);
        context.draw(false, () => this.setData({ error: '' }));
      } catch (_) { this.setData({ error: '地图没有画出来，请点重新绘制；仍失败时重开页面。' }); }
    }).exec();
  }
});
