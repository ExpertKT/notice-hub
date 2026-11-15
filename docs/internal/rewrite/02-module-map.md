# G1 · 模块映射

「替换」= G2 用新模块承担行为，旧文件先留作适配层，切换完成后再删。「保留」= 不重写内部，最多改调用点，且调用点切换归主线（G3）。

## 替换（上游派生）

| 现文件 | 去向 | 差异要点 |
|---|---|---|
| `receiver.py`、`bot.py` | `adapters/qq` | 只产出 `MessageEvent`。去重和入库不在适配器内 |
| `qq_digest.py` | `domain` 的消息解析 | 解析结果是领域类型，不负责落盘 |
| `timeutil.py` | `domain` 的纯时间函数 + `ports.Clock` | 现时钟与格式化拆开。`now_local()` 的调用点改走 Clock |
| `retry.py` | `domain` 或 `infrastructure` 的小策略对象 | 退避表由调用方传入，不读全局配置 |
| `weburl.py` | `domain` 的 URL 判定 | 保持现有允许的 scheme 与凭据拒绝规则 |
| `summarizer.py` | `application` 编排 + `domain` 资格规则 | 摘要拼装不直接碰 Store |
| `event_extract.py` | 算法保留，调用收进 application | 该文件是自研。不换规则，只改调用方不再是 `service` 私有方法 |
| `rules.py` | 纯 `parse` / `match` 留在领域可调用的纯函数；读写 `rules.json` 走 port | 自研。命中后建待办的副作用今天在 `service._apply_rules`，改由用例承担 |
| `store.py` | `ports` 仓储 + `infrastructure/sqlite` | 表、列、迁移语义见 `03-ports.md` |
| `service.py` | `application` 用例 | 去掉对 `webapp` 的 import。启动 HTTP 留在 bootstrap |
| `push.py` | `ports.Notifier` + `infrastructure` HTTP | 通道名与去重键算法保持 |
| `catchup.py` | application 用例 + `ports.GroupHistory` | NapCat HTTP 留在 infrastructure |
| `attachments.py` | application 编排 + infrastructure 解析器 | 解析 Excel/PDF/图片的行为保持；限额仍来自 Settings |
| `config.py` | `bootstrap.Settings` + `from_legacy()` | 见 `05-config.md`。旧 `Settings.from_env` 在并存期转调 `from_legacy` |
| `main.py` | `bootstrap` CLI | 子命令集合保持：`run tick preview send-test doctor stats show tasks catchup attach-test` |

## 保留（主线或自研，重写方不改文件）

`webapp.py`、`routes.py`、`settings_api.py`、`updates_api.py`、`web_static/**`、`packaging/**`、`android/**`。

`wechat_receiver.py`、`dingtalk_receiver.py`、`chaoxing_receiver.py`、`napcat_admin.py`、`hosting.py`、`updater.py`、`update_sources.py`、`announce.py`、`tunnel.py`、`community.py`、`browser_cookie.py`、`inbox.py`、`lab.py`、`pairing.py`、`selfcheck.py`、`floatball_*.py`、`version.py`。

`rules.py` 与 `event_extract.py` 的算法保留。允许 G2 把「谁调用它们」从 `service.py` 挪到用例，不允许改命中语义。

## 测试

上游 `tests/` 共 11 个路径：`tests/__init__.py` 加下面 10 个 `test_*.py`。其中这 9 个按行为规格重写，不放宽断言：

`tests/test_attachments.py`、`test_catchup.py`、`test_config.py`、`test_push.py`、`test_receiver.py`、`test_retry.py`、`test_service.py`、`test_store.py`、`test_summarizer.py`。

`tests/test_webapp.py` 也在上游清单里，但当前文件约 96% 是主线后写的页面与路由断言。重写方不改这个文件。上游那一小段接收/页面骨架断言若仍在，G4 自查时单列，由主线决定留在 Web 测试还是改成行为断言。其余 `tests/test_*.py`（含 `test_routes.py`、`test_rules.py`、`test_calendar_public.py`）归主线，必须继续通过。

## beta.13 必须落在新用例里的行为

这些今天写在 `service.py`，替换 `service.py` 时要原样保住：

1. 只有新插入的消息才跑规则。`insert_message` 因重复返回 false 时不跑。
2. `lab_enabled("rules")` 为 false 时不读规则文件。
3. 命中动作的 `payload` 可覆盖 `summary` / `category` / `importance`。缺省：摘要取正文首行最多 60 字，空则 `(规则命中)`；分类 `action`；重要度 `3`。摘要最终截断到 200 字。
4. `task_key = rule:<rule_id>:<msg_id>`。重复投递不建第二条。
5. 群名非空时 `groups=[group_name]`。`source="rule"`，`status="candidate"`，`classification_reason` 为 `规则 <id> 命中`。
6. 规则读取、匹配、建待办任一异常：记日志，`on_message` 仍返回插入结果。
7. `_sync_tasks` 先 `_merge_extracted_event` 再判断资格。`morning` / `evening` 摘要不建任务。含「链路自测」的不建。建任务异常不取消本批推送。
