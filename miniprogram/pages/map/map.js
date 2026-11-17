const api = require('../../services/api');
const campus = require('../../assets/campus');
const BUILDINGS = campus.buildings.map((building, index) => ({ id: String(index), name: building.n, floors: building.f, center: building.c, rects: building.r }));
Page({
  data: { buildings: BUILDINGS.map(({ id, name, floors }) => ({ id, name, floors })), selected: '0', selectedName: BUILDINGS[0].name, floor: 1, floors: [1, 2, 3, 4, 5], error: '', placeHits: [], placeError: '' },
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
  // 待办里没有地点字段，只按标题里提到的楼名标记，属于文字匹配，不是定位。
  loadPlaces() {
    if (!api.hasConfig()) {
      this.setData({ placeHits: [], placeError: '还没配置连接，待办地点标不出来。点右上角连接设置填地址和令牌。' });
      return;
    }
    if (this.placeCall) this.placeCall.abort();
    const call = api.request('/api/tasks');
    this.placeCall = call;
    call.then(board => {
      if (this.placeCall !== call) return;
      const tasks = ['candidates', 'today', 'week', 'later', 'done']
        .reduce((all, key) => all.concat(Array.isArray(board[key]) ? board[key] : []), []);
      const hits = BUILDINGS.map(building => ({
        id: building.id, name: building.name,
        count: tasks.filter(task => String((task && task.summary) || '').indexOf(building.name) >= 0).length
      })).filter(hit => hit.count);
      this.setData({ placeHits: hits, placeError: '' }, () => this.renderMap());
    }).catch(error => {
      if (this.placeCall !== call) return;
      this.setData({ placeHits: [], placeError: (error.title || '待办地点没读到') + '：' + (error.message || '稍后重试。') + ' 地图本身不受影响。' });
    }).finally(() => {
      if (this.placeCall === call) this.placeCall = null;
    });
  },
  openSettings: api.openSettings,
  selectBuilding(event) {
    const building = BUILDINGS.find(item => item.id === event.currentTarget.dataset.id);
    if (!building) return;
    this.setData({ selected: building.id, selectedName: building.name, floor: 1, floors: Array.from({ length: building.floors }, (_, index) => index + 1) }, () => this.renderMap());
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
        const point = ([x, y]) => [left + x * scale, top + (campus.size_m[1] - y) * scale];
        context.clearRect(0, 0, rect.width, rect.height);
        context.setFillStyle(dark ? '#23231B' : '#EDE7D6'); context.fillRect(0, 0, rect.width, rect.height);
        context.setLineWidth(0.3);
        context.setStrokeStyle(dark ? 'rgba(237,230,210,.28)' : 'rgba(58,58,40,.30)');
        BUILDINGS.forEach(building => {
          context.setFillStyle('#D6C7A4');
          context.setStrokeStyle(building.id === this.data.selected ? accent : (dark ? 'rgba(237,230,210,.28)' : 'rgba(58,58,40,.30)'));
          context.setLineWidth(building.id === this.data.selected ? 0.8 : 0.3);
          building.rects.forEach(([x1, y1, x2, y2]) => {
            const [x, y] = point([x1, y2]);
            const width = (x2 - x1) * scale;
            const height = (y2 - y1) * scale;
            context.fillRect(x, y, width, height); context.strokeRect(x, y, width, height);
          });
        });
        // ponytail: 路线仅展示绘图样式，真实行程需接口提供站点与路网。
        const stops = [BUILDINGS[3], BUILDINGS[0], BUILDINGS[4]];
        context.setStrokeStyle(accent); context.setLineWidth(1.5); context.setLineDash([5, 5]); context.beginPath();
        stops.forEach((building, index) => { const [x, y] = point(building.center); if (index === 0) context.moveTo(x, y); else context.lineTo(x, y); });
        context.stroke(); context.setLineDash([]);
        stops.forEach((building, index) => {
          const [x, y] = point(building.center);
          context.setFillStyle(accent); context.beginPath(); context.arc(x, y, 9, 0, Math.PI * 2); context.fill();
          context.setFillStyle(dark ? '#20201A' : '#F6F2E7'); context.setFontSize(12); context.fillText(String(index + 1), x - 3, y + 4);
          context.font = '12px "Songti SC", "Noto Serif SC", serif';
          context.setFillStyle(dark ? '#EDE6D2' : '#3A3A28'); context.fillText(building.name, x + 13, y + 4);
        });
        // 待办地点：楼名匹配上的楼，中心右上画一个点加件数。
        this.data.placeHits.forEach(hit => {
          const building = BUILDINGS.find(item => item.id === hit.id);
          if (!building) return;
          const [x, y] = point(building.center);
          context.setFillStyle(accent); context.beginPath(); context.arc(x + 26, y - 16, 8, 0, Math.PI * 2); context.fill();
          context.setFillStyle(dark ? '#20201A' : '#F6F2E7'); context.setFontSize(10); context.fillText(String(hit.count), x + 23, y - 12);
        });
        context.font = '12px "Songti SC", "Noto Serif SC", serif';
        context.setFillStyle(dark ? '#EDE6D2' : '#3A3A28');
        context.fillText(`${this.data.selectedName} · ${this.data.floor} 层`, 16, 20);
        context.draw(false, () => this.setData({ error: '' }));
      } catch (_) { this.setData({ error: '地图没有画出来，请点重新绘制；仍失败时重开页面。' }); }
    }).exec();
  }
});
