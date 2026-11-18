# 地图 Provider 接入契约

群务台的地图不是绑死某一家底图：底图能力走 `window.nhMapProviders` 注册表，任何第三方
JS 文件都可以在页面加载前后把**自己的底图**注册进来，跟内置的 OSM / 高德 / 百度走同一套
`choose()` / `drawMap()` / `teardownOnline()` 代码路径，没有旁路。

入口：`noticehub/web_static/js/trip.js` 里「底图供应商接口（window.nhMapProviders）」那一段（约 1271 行起）。

## 注册表 API

```js
window.nhMapProviders.register(spec)   // 登记一个底图；成功返回 true
window.nhMapProviders.get(id)          // 取一个；没有这一档返回 null
window.nhMapProviders.list()           // 按登记顺序返回全部 spec
```

`register` 对不合规的 spec（缺 `id`、`init` 不是函数）返回 `false`，不抛错。同一个 `id` 再登记一次会**顶掉**前一份（后登记的生效，顺序不变），这样开发时改一版 provider 刷新页面就够，不用重开标签。

## 怎么接一家新底图（从 register 到能点选）

注册只是第一步——注册进去的档要**在「底图与路线」那排按钮里出现、点得动、选中后真的换过去**，才算接好了。这排按钮和换档逻辑都在 `trip.js`，第三方不需要碰，但要知道自己的 spec 会流经哪几处：

1. **register 进注册表**。`window.nhMapProviders.register(spec)`，`spec` 至少带 `id` / `label` / `credit` / `needsKey` / `init`（字段见下）。注册成功返回 `true`。
2. **按钮上排**。模式条 `.map-modes` 由 `modeBar()` 建（`trip.js:982`）：建栏时先画内置四档，再把注册表里**不在内置 MODES 里**的档各补一个 `<button data-map-mode="…">`（`trip.js:1000-1009`）。**晚于建栏**才 register 的档走 `refreshModes()`（`trip.js:1106`）补按钮——它把注册表里还没有按钮的档追加进去。所以你在页面加载完之后再挂 provider，记得 UI 会在下一次 `refreshModes()`（切档、选地点都会触发）时把按钮补上，不用自己动 DOM。
3. **点按钮 → `choose(id)`**。模式条在 `.map-modes` 上挂了一个冒泡的 click 处理器（`trip.js:1023`），点中 `[data-map-mode]` 就调 `choose(id)`（`trip.js:1857`）。`choose` 先 `loadModeConf()` 拿到这档的 key 配置，再判断 `ready`：`needsKey: false` 的档直接算 ready；`needsKey: true` 且没配 key 的档**不报错**，而是弹填 Key 面板（`openKeyPanel`），填完存进 `map_keys.json` 自动重连。
4. **ready 的档 → `openOnline(id)` → `makeMap` → 你的 `init`**。`openOnline`（`trip.js:1690`）备好投影系数、拆掉旧瓦片、建容器，然后 `makeMap(id, el)`（`trip.js:1415`）对**非内置 id 直接调 `spec.init(el, providerCtx(id))`**——不走 Leaflet / 高德 / 百度那几条内置分支。你的 `init` resolve 出 adapter 后，shell 把路线、POI、「我在哪」全用 adapter 的 `toScreen` 自己画在覆盖层 canvas 上（`drawOverlay`，`trip.js:1600`），**你只管底图本身**。
5. **换档落地**。init resolve、覆盖层挂好后，`choose` 的成功分支把 `mode.id` 设成你的 id、`saveMode()` 写进 localStorage、再 `refreshModes()`——这一步把你的按钮 `aria-pressed` 置 `true`（`trip.js:1130`）、把 `credit` 渲染进 `.map-credit`。整条链任何一步抛错都会落进 `choose` 的 catch，`mode.id` 拨回 `sketch`（自绘），界面上看起来就是「点了没选中」——多数时候是你的 `init`（或它 resolve 之前 shell 调的 `frame` / `layoutOverlay`）抛了，去控制台看原始错误。

一句话：**register 让档进注册表，`modeBar`/`refreshModes` 让它上排，`choose`→`makeMap`→`init` 让它真能切过去**。三段任何一环没走通，档就只是「注册了但点不到 / 点了没反应」。下面这条 node 级回归闸锁的就是这三段都通：

```bash
node tests/js_provider_modebar_probe.js noticehub/web_static/js/trip.js
```

它加载真的 `trip.js`，注册一个假供应商 `probe-tile`，断言 `refreshModes` 后它出现在 `.map-modes` 里、点按钮走完 `choose`→`init`、选中后 `aria-pressed` 变 `true`。改 `modeBar` / `refreshModes` / `choose` / `makeMap` 的注册表对接时跑它，别等浏览器。

## spec（注册时给的字段）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✓ | 稳定字符串，如 `"tileserver"`；内置的三个是 `"osm"` `"amap"` `"baidu"` |
| `label` | ✓ | 模式条按钮上的名字，中文 |
| `credit` | ✓ | **署名**，渲染在 `.map-credit` 里；用到别人数据就照人家许可写清楚 |
| `needsKey` | ✓ | `true` 时没有 key 会弹填 Key 面板而不是直接失败 |
| `init(el, ctx)` | ✓ | 返回 `Promise<adapter>`；`el` 是地图容器 DOM，`ctx` 见下 |

`ctx` 由 shell 按「当前这一档」组装：`{ tile, key, secret, pois, geo, proj: { wgsToGcj, gcjToBd } }`。
`key` / `secret` 取的是这一档自己在设置里填的那份（数据目录 `map_keys.json`，环境变量优先），没填就是空串——`needsKey: true` 的底图靠这个空串判断该弹填 Key 面板。`tile` 对 OSM 类底图有意义，缺省是 `OSM_TILE_DEFAULT`；`pois` 是校区 POI 表；`geo` 是 `campus-geo.json` 解析结果；`proj` 里是现成的坐标系转换函数。

## adapter（`init` resolve 出来的对象）

| 方法 / 字段 | 说明 |
| --- | --- |
| `kind` | 该档坐标系名，`'osm'` / `'amap'` / `'baidu'` 或自定义。取点时用它标 CRS（`'osm'` 记成 `wgs84`），别省。 |
| `toPlan(clientX, clientY) -> {x, y} \| null` | 容器内像素 → 校区平面坐标（米）。取不到返回 `null`。 |
| `fromPlan(x, y) -> {lat, lon} \| null` | 平面坐标 → 这家底图用的经纬度。平面→WGS→（高德 GCJ-02 / 百度 BD-09）这一步由底图自己挑。 |
| `toScreen(x, y) -> {x, y}` | 平面坐标 → 容器内像素。**路线、POI 点、「我在哪」都由 shell 用这个方法自己画**，不用你画标记。 |
| `frame(latlngs)` | 把一组经纬度塞进视野。 |
| `onMove(fn)` | 视野变化时回调（可选）；拖动时会重画覆盖层，不实现就只在切档时重画。 |
| `destroy()` | 收拾干净，切底图时 shell 会调。 |

`setMarkers(points)` 在契约注释里还留着，但**当前不会被调用**——内置三档都实现成空函数。要接自己的底图不必实现它。

## 坐标系约定

- 校区**内部**一切几何（POI 的 `x`/`y`、`trip["line"]`、自绘渲染）都用**平面坐标**（米），
  原点和比例尺在 `campus-geo.json` 的 `projection` 字段里（当前是 lat 31.716299 / lon
  119.815803，0.9 米每像素）。
- 平面 → 快照像素走 `campus-geo.json` 的 `planToSnapshot`（一次旋转+缩放，8.9°），`trip.js`
  里的 `planToSnapshot` / `snapshotToPlan` 会自动从那份 JSON 读，JSON 缺字段就退化成恒等
  并在控制台 warn 一句——**不要在 JS 里复制这几个系数**。
- 平面 → WGS-84 用 `pxToWgs`（先过快照像素，再按 `metersPerPixel` 折算）。
- WGS-84 → GCJ-02 / BD-09 用现成的 `wgsToGcj` / `gcjToBd`（`ctx.proj` 里也有一份）。
  高德吃 GCJ-02，百度吃 BD-09，OSM 吃 WGS-84，别串。

## 例子：一张图片当底图

```js
window.nhMapProviders.register({
  id: "photo",
  label: "平面图",
  credit: "校园平面图 © 某某公司",
  needsKey: false,
  init(el, ctx) {
    const img = document.createElement("img");
    img.src = "campus-photo.jpg";
    img.style.cssText = "width:100%;height:100%;object-fit:contain;";
    el.appendChild(img);
    // 假设这张图正好覆盖 plan [0,0]-[1000,1200]
    const toScreen = (x, y) => ({ x: x / 1000 * el.clientWidth, y: y / 1200 * el.clientHeight });
    return Promise.resolve({
      kind: "image",
      toPlan: (cx, cy) => ({ x: cx / el.clientWidth * 1000, y: cy / el.clientHeight * 1200 }),
      fromPlan: (x, y) => null,          // 图片底图没有经纬度概念，返回 null 就行
      toScreen,
      frame: () => {},
      setMarkers: () => {},
      destroy: () => { img.remove(); },
    });
  },
});
```

注册时机随意：页面加载完之后再挂一个 `<script src="my-provider.js">` 也行，
`trip.js` 会在每次 `choose()` 时重新查注册表。

## 署名与许可

`credit` 是硬要求：底图 shell 会把它渲染出来。用到 OSM 数据就按 ODbL 写清
「© OpenStreetMap contributors」；用到别人扫的图、别人算的高程，照人家许可来。
**不要**把 key 硬编码进 provider 文件——用户的 key 存在服务器数据目录的
`map_keys.json`（不进仓库、不上传第三方），通过 `ctx.key` / `ctx.secret` 传进来。
