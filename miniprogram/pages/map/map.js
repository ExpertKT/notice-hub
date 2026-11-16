const api = require('../../services/api');
const BUILDINGS = [
  { id: 'study', name: '教学楼', x: 22, y: 160, width: 95, depth: 54, floors: 4 },
  { id: 'library', name: '图书馆', x: 166, y: 138, width: 85, depth: 49, floors: 3 },
  { id: 'hall', name: '活动中心', x: 104, y: 250, width: 110, depth: 48, floors: 2 }
];
function polygon(context, points, color) {
  context.setFillStyle(color);
  context.beginPath();
  points.forEach((point, index) => { if (index === 0) context.moveTo(point[0], point[1]); else context.lineTo(point[0], point[1]); });
  context.closePath(); context.fill();
}
Page({
  data: { buildings: BUILDINGS, selected: 'study', selectedName: '教学楼', floor: 1, floors: [1, 2, 3, 4], error: '' },
  onReady() { this.renderMap(); },
  onShow() { if (this.ready) this.renderMap(); },
  onResize() { this.renderMap(); },
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
        context.clearRect(0, 0, rect.width, rect.height);
        context.save(); context.scale(rect.width / 326, rect.height / 310);
        polygon(context, [[10, 165], [185, 65], [322, 150], [149, 300]], '#e1e9e2');
        BUILDINGS.forEach(building => {
          const { x, y, width, depth, floors } = building;
          const active = building.id === this.data.selected;
          for (let floor = 1; floor <= floors; floor += 1) {
            const bottom = y - (floor - 1) * 19;
            const top = bottom - 17;
            const bright = active && floor === this.data.floor;
            polygon(context, [[x, top], [x + width, top + 25], [x + width, bottom + 25], [x, bottom]], bright ? '#3974c3' : '#b1bdcc');
            polygon(context, [[x + width, top + 25], [x + width + depth, top - 5], [x + width + depth, bottom - 5], [x + width, bottom + 25]], bright ? '#2155a3' : '#8296ae');
            polygon(context, [[x, top], [x + depth, top - 30], [x + width + depth, top - 5], [x + width, top + 25]], bright ? '#93b9ed' : '#dae2ec');
          }
          context.setFillStyle('#172b45'); context.setFontSize(13); context.fillText(building.name, x + 9, y + 47);
        });
        context.restore();
        context.draw(false, () => this.setData({ error: '' }));
      } catch (_) { this.setData({ error: '地图没有画出来，请点重新绘制；仍失败时重开页面。' }); }
    }).exec();
  }
});
