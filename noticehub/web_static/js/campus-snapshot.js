/* 校区快照装载器：campus-geo.json 里可选的 "snapshot" 键指向另一份快照脚本
   （如 "js/campus-osm-xxx.js"），换校区只改这一处数据、不改代码。缺省或取值
   非法（不是 js/ 开头、不是 .js 结尾、含 ..）时保留默认那份 js/campus-osm.js，
   行为跟以前完全一样。数据 © OpenStreetMap contributors，ODbL。 */
(function (global) {
  'use strict';
  var DEFAULT_SNAPSHOT = 'js/campus-osm.js';

  // 只放行站内、单段、.js 结尾的路径：挡掉 ../ 穿越、绝对 URL、协议相对地址。
  function validSnapshotPath(value) {
    if (typeof value !== 'string' || !value) return null;
    if (value.indexOf('..') !== -1) return null;
    if (value.slice(0, 3) !== 'js/') return null;
    if (value.slice(-3) !== '.js') return null;
    return value;
  }

  // 读 campus-geo.json 的 snapshot 键（缺省即默认），再按它动态注入脚本。
  // 注入前 window.NH_CAMPUS_OSM 已是默认快照（page.html 先同步加载了默认那份），
  // 脚本 onload 覆盖这个全局，随后 trip.js 开地图时读到的就是新校区的几何。
  function loadCampusSnapshot() {
    return fetch('js/campus-geo.json')
      .then(function (res) { if (!res.ok) throw new Error('HTTP ' + res.status); return res.json(); })
      .then(function (geo) {
        var path = validSnapshotPath(geo && geo.snapshot) || DEFAULT_SNAPSHOT;
        if (path === DEFAULT_SNAPSHOT) return { path: path, swapped: false };
        return new Promise(function (resolve) {
          var el = document.createElement('script');
          el.src = path;
          el.onload = function () { resolve({ path: path, swapped: true }); };
          el.onerror = function () {
            // 自定义快照拉不到就留在默认校区，控制台留一句，别让地图整块空白。
            console.warn('[nhMap] 快照 ' + path + ' 没拉到，仍用默认校区');
            resolve({ path: DEFAULT_SNAPSHOT, swapped: false });
          };
          document.head.appendChild(el);
        });
      })
      .catch(function () { return { path: DEFAULT_SNAPSHOT, swapped: false }; });
  }

  global.nhCampusSnapshot = { validSnapshotPath: validSnapshotPath, loadCampusSnapshot: loadCampusSnapshot };
})(typeof window !== 'undefined' ? window : globalThis);
