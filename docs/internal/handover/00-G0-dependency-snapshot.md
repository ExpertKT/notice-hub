# 00-G0-dependency-snapshot.md — 依赖快照（重写前基线）

**状态：待核**（architecture-map 4.4 尚未开工，本文只做快照与差距量化，不含改动）。

生成方式：`F:\nh-tmp\rewrite\dep_graph.py`（AST 扫 `qq_live_digest/*.py` 的 import），
`F:\nh-tmp\rewrite\dep_cycles.py` 做环检测。
机读版：`F:\nh-tmp\rewrite\import-graph.json`；人读版：`F:\nh-tmp\rewrite\import-graph.txt`。
基准：HEAD `ad67e3e` + 工作区未提交改动（R1/R2 已落地）。

---

## 1. 总量

- 包内模块 **49 个**（50 个 `.py` 去掉 `__init__`）
- 有出边的模块 **29 个**，总边数 **109**
- **模块级环：0 个**（2 环 0 对、3 环 0 组）

> 注意：这里「0 环」只说明**模块级** import 不成环。
> `service ↔ webapp` 的耦合不是 import 环，而是
> **service import webapp 取类，webapp 反过来从 `self.server.*` 取 service 的运行时属性**。
> 这是属性级耦合，AST 扫 import 抓不到，见第 3 节。

## 2. 入度最高（改它波及最多）

| 模块 | 上游数 | 上游 |
|---|---|---|
| `config` | 15 | announce, attachments, bot, catchup, dingtalk_receiver, doctor, hosting, push, receiver, replies, service, settings_api, summarizer, webapp, weburl |
| `timeutil` | 13 | attachments, bot, catchup, dingtalk_receiver, event_extract, ics, receiver, replies, search, service, store, summarizer, webapp |
| `messages` | 6 | chaoxing_receiver, host_actions, napcat_admin, tunnel, webapp, wechat_receiver |
| `store` | 5 | catchup, push, replies, service, webapp |
| `win_text` | 5 | host_actions, hosting, napcat_admin, tunnel, webapp |
| `attachments` | 4 | catchup, receiver, service, settings_api |

## 3. 出度最高（它知道太多）

| 模块 | 依赖数 |
|---|---|
| `webapp` | 28 |
| `service` | 15 |
| `settings_api` | 7 |
| `catchup` | 5 |
| `announce` / `attachments` / … | 3 |

## 4. architecture-map 4.4 的量化差距（**待核项**）

`docs/architecture-map.md:364-366` 记的 4.4「把 `service → webapp` 的依赖反过来」。
**该条目标「未经用户授权，仅存档」，从未开工。** 快照实测当前数字：

| 指标 | 评估时（architecture-map:93） | 当前实测 | 变化原因 |
|---|---|---|---|
| `webapp.py` 行数 | 4703 | **3697** | R1 路由表化 + R2 settings 拆分 |
| `service.py:26` import webapp | 有 | **仍有**（`service.py:27 from .webapp import TaskWebServer`） | 未动 |
| `self.server.` 处数 | 87 | **70** | 随 R1/R2 下降，未专项清 |
| `type: ignore[attr-defined]` | 146 | **79** | 同上 |
| 构造后回填属性 | 7 个 | **6 个**（`service.py:396-401`） | 口径微变，仍是「先 None 再赋值」 |

**回填清单（`service.py:396-401`）**：
`reload_chaoxing` / `reload_dingtalk` / `reload_wechat` / `dingtalk_reader_provider` /
`wechat_reader_provider` / `start_scan`（评估时记的「start_scan 写了两次」当前只剩一次）。

**结论：4.4 未开工，但因 R1/R2 已经顺手把它的一半收益拿到了**（行数 −21%、
`type: ignore` −46%、`self.server.` −19%）。真要动 4.4，剩余成本比评估时低。

## 5. 对 G1 契约的三条直接影响

1. **`webapp` 是唯一的枢纽（出度 28）**：`routes.py` / `settings_api.py` / `updates_api.py`
   这三个新模块都在它的下游。重写 `application` 层时，
   **`webapp` 是唯一必须整体替换的入口**，不能分批切。
2. **`config` 与 `timeutil` 是改动的最大刹车**（入度 15 / 13）：
   `ports` 层若定义自己的配置/时间抽象，这两个模块要被所有适配器同时引用，
   **必须最先定签名**。
3. **`store` 入度只有 5，但它是 11 张表的唯一入口**：import 图上看起来很安全，
   实际改动面要看 `store.py` 的方法数而不是入度。这一条不能只靠依赖图判断。
