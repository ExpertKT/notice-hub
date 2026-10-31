# G1 · 配置契约

业务代码不读 `os.environ`，不调用全局 `Settings` 单例。`bootstrap` 在进程启动时构造一份 `Settings`，经参数传入用例、适配器和 infrastructure。

## 类型

`noticehub.bootstrap.Settings` 字段与今天 `qq_live_digest.config.Settings` 一一对应，包括默认值：

- Web `8766`，OneBot `8765`
- `message_retention_days = 30`
- `lab_search / lab_replies / lab_events / lab_rules` 默认关
- `quiet_hours = 23:00-07:00`
- `push_daily_budget = 12`
- `from_legacy` 未给数据目录时用运行根的 `data` / `logs`。直接构造、不走 `from_legacy` 时，数据目录默认是源码根。未冻结时两者相同

方法保持现有语义：`accepts_group`、`group_name`、`lab_enabled`、`push_channels`、`problems`、`llm_active`、`in_quiet_hours`、`describe`。

`problems()` 的五句中文保持原文，doctor 和首启提示在对这几句：

- 缺少 `QQ_BOT_APPID / QQ_BOT_SECRET`，官方机器人无法登录
- 未配置任何推送通道
- `QQ_DIGEST_GROUPS` 未配置
- 已启用 OneBot 但未设置 `QQ_DIGEST_ONEBOT_TOKEN`
- 已启用历史补采但未设置 NapCat API Token

`problems()` 非空不阻止进程启动。服务可以起来，对应功能空转。

`push_channels()` 的名字保持 `qq-bot | wxpusher | serverchan | pushplus | webhook`。

## from_legacy

```text
Settings.from_legacy(env: Mapping[str, str] | None = None, env_file: Path | None = None) -> Settings
```

读取规则与今天的 `Settings.from_env` 相同：

- 传入 `env_file` 时先把该文件装入环境变量（不覆盖已存在的键），无论是否同时传入 `env`。
- 未传 `env_file` 且未传 `env` 时，读 `QQ_DIGEST_ENV`，再否则读运行根下的 `.env`，然后以进程环境变量为来源。
- 传入 `env` 时，字段值只从这份映射读取，不从进程环境变量补。未传 `env_file` 时也不读文件。
- 键名全部保持。包括 `QQ_BOT_APPID`、`QQ_BOT_SECRET`、`QQ_DIGEST_*`、`DASHSCOPE_API_KEY`（作为 `QQ_DIGEST_LLM_API_KEY` 的后备）。
- 布尔、整数、逗号列表、别名 `k=v` 的解析规则不变。
- 未知键不报错。写回 `.env` 时保留未知键和注释以外的原行顺序；并存期写回仍走今天的 `update_env_file`。

并存期：`qq_live_digest.config.Settings.from_env` 转调 `from_legacy`，返回的对象字段布局兼容旧属性访问（`settings.web_port`、`settings.group_whitelist` 等）。主线 G3 再把 `webapp` 里的读取点改成注入对象。

## 注入边界

| 谁 | 可以碰配置的方式 |
|---|---|
| `bootstrap`、CLI | 调用 `from_legacy`，创建目录 |
| application、adapters、infrastructure | 只使用传入的 `Settings` 字段 |
| domain | 不接收整个 Settings。需要的阈值（如最低分、静默时段）由用例作为普通参数传入 |
| Web | G3 前仍读 `self.server.settings`。G3 后从 bootstrap 拿同一份 |

import `noticehub.bootstrap` 或 `noticehub.domain` 时不得读环境变量。读文件只发生在 `from_legacy` 被调用时。

## 写回

`save_subscriptions` 只改 `QQ_DIGEST_GROUPS` 与 `QQ_DIGEST_GROUP_ALIASES`。

设置页九个域的写回留在 `settings_api.py`（主线）。那些函数继续得到同一份 Settings，并在写 `.env` 后热更新该对象。重写方不拆这九个域。

## 版本与通道

`update_channel` 仍是 Settings 字段，对应 `QQ_DIGEST_UPDATE_CHANNEL`。当前版本号不在 Settings 里，只在 `version.py`。
