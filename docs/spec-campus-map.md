# 校园地图：地点编辑与「我在哪」的约定

这份文件只写三件事：数据放哪、接口长什么样、界面上谁负责。改了这里就是改了契约，前端与后端都得跟着改。

## 1. 数据

| 文件（都在数据目录，不进仓库） | 内容 | 谁写 |
| --- | --- | --- |
| `campus_poi.json` | 用户自己加的地点，数组，`id` 为 `custom_N` | 加点 / 删点 |
| `campus_poi_override.json` | 对内置 27 条地点的改动，`{"<内置 id>": {"name": "…", "x": 1.0, "y": 2.0, "hidden": true}}`，只存被改过的字段 | 改名 / 挪位置 / 隐藏 |
| `campus_here.json` | 我当前在哪：`place_id`、`name`、`at`（隔天作废）；拖动来的点另带 `x`、`y` | 选地点 / 拖标记 |
| `map_keys.json` | 底图 key：`{"amap": {"key": "…", "secret": "…"}, "baidu": {"key": "…"}, "osm": {"tile": "https://…/{z}/{x}/{y}.png"}}` | 设置里填 |

内置点的原始值在 `noticehub/application/campus.py` 的 `DEFAULT_POIS`，**不被覆盖**；override 只在读出时叠上去，所以升级换代码不会丢用户改动，删掉 override 文件就回到出厂。

## 2. 接口

`POST /api/agent/campus/poi`，body 带 `action`：

| action | 必填 | 说明 |
| --- | --- | --- |
| `add` | `name`、`x`、`y` | 加点；`id` 由服务端给 `custom_N` |
| `edit` | `id`、`name` | 改名；内置点写成 override，自定义点直接改 |
| `move` | `id`、`x`、`y` | 挪位置；同上 |
| `hide` / `unhide` | `id` | 只在排程与地图里不出现，文件不删 |
| `remove` | `id` | 只能删自定义点；内置点请用 `hide` |

校验（失败要给出能照做的中文，不是「参数错误」）：名字 1–20 字、不许与可见地点重名、`x`/`y` 必须是有限数且落在 `CAMPUS_BOX` 内、`id` 必须存在。

`POST /api/agent/plan`，`action: "here"`：既可以 `{place: "图书馆"}`，也可以 `{x, y}`。给坐标时服务端在 40 米内找最近地点，找到就用它的名字，找不到就是「自由位置」并用最近一栋楼当说明；两种都存进 `campus_here.json`。

成功一律返回最新的 `plan`（`{"ok": true, "plan": {...}}`），前端拿它整体重画，不自己改本地状态。

## 3. 界面归属

| 位置 | 谁做 | 内容 |
| --- | --- | --- |
| 地图画布 | 地图 bot（`trip.js`） | 「我」标记可拖动；「＋地点」；底图与路线档位 |
| 设置页「校园地图」分区 | 界面 bot（`page.html`） | 地点清单（内置 + 自定义，按区域分组）、改名、移动（进取点态）、隐藏、删除；底图 key 与瓦片源表单 |
| 手机端 | 手机端 bot | 390px 下的清单与表单排布、触控目标 ≥44px |

清单里的每一行都要能看出：名字、区域、来源（OSM / 估算 / 自定义 / 已改）、当前坐标。改动保存后地图要立刻重画，不需要刷新页面。

## 4. 不做的事

- 不做多人共享地图编辑（单机数据目录）。
- 不接外部地图厂商的地名检索，地点只来自内置表 + 用户自加。
- 不在界面上存高德/百度的 key 之外的东西；key 明文存本机，文档里要写明。

## 5. 换一个校区的地图（已完成：快照可换）

底图这一层已经是注册表（`docs/wiki/map-providers.md`），换一家瓦片不用改代码。地点表和校区范围也不再写死在代码里：

1. 地点表：`campus.py` 读 `<data_dir>/campus_poi_seed.json`（与 `campus_poi.json` 同格式，每条自带 `x`/`y`），有它就用它当基线，没有才回落 `DEFAULT_POIS` 那 27 条。换校区只需要放一份 seed，不改代码。
2. 校区范围：`agent_plan._campus_box()` 每次校验都从当前快照的 `campus` 环现算包围盒（`campus_roads._snapshot()`），算不出（环少于 3 点、文件坏了）才回落常量 `CAMPUS_BOX = (0.0, 0.0, 950.0, 1450.0)`。**不要给这个盒子加余量**：环的包围盒按定义已包含环上每一点，外放 2% 会把 `x=-5` 这种明显在墙外的点算成合法（`tests/test_application_agent_plan.py:687 test_move_rules` 锁着这条）。

最后那处硬编码也接好了：`campus-geo.json` 现在可写可选键 `"snapshot": "js/campus-osm-xxx.js"`，`js/campus-snapshot.js` 在启动时读它、动态注入那一份快照脚本（`page.html` 仍先同步加载默认的 `js/campus-osm.js` 打底，自定义那份 `onload` 后覆盖 `window.NH_CAMPUS_OSM`）。取值非法（不是 `js/` 开头、不是 `.js` 结尾、含 `..`）或缺省时维持默认，行为不变；`sw.js` 的预缓存清单只留当前默认那份，自定义快照走运行时注入、不预缓存。

所以现在「别人的地图」能接的既可以是**同一片校区的另一张底图**（底图注册表），也可以是**另一片校区**（换 `snapshot` + 地点 seed）。两者别在材料里混为一谈。
