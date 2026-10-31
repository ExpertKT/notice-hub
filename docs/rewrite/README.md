# rewrite/clean-room · G0 / G1

状态：主线已签「够接 Web」。签收对象是他们本地的 `rewrite/g1-fix` `b42d688`，不在 `origin`。本分支已吸收到 `cb29a35`（代码段 `1efa41e`，`1.1.0-beta.15`）。`docs/rewrite/baseline/` 不在 `origin`，这边没有可 `--update` 的脚本。

行为基线：`1efa41e`。新包名：`noticehub`。重命名留到最后一个提交，在此之前运行代码仍在 `qq_live_digest`。

主线请先看 [给主线.md](给主线.md)。

| 文件 | 内容 |
|---|---|
| [给主线.md](给主线.md) | 请主线确认的一件事，以及确认前不要做的事 |
| [00-G0.md](00-G0.md) | 基线 SHA、分支、越界清单、已拍板事项 |
| [01-architecture.md](01-architecture.md) | 分层、依赖规则、包名理由 |
| [02-module-map.md](02-module-map.md) | 旧文件到新模块；哪些保留不动 |
| [03-ports.md](03-ports.md) | 仓储、时钟、通知器、抓取器 |
| [04-application.md](04-application.md) | Web 要调用的用例签名，含 beta.15 规则接线 |
| [05-config.md](05-config.md) | 类型化配置与 `from_legacy()` |
| [06-migration.md](06-migration.md) | 并存、切换、最后一次重命名 |
| [07-risks.md](07-risks.md) | 会打断什么，以及发现清单 |

接缝：`noticehub.application` 的用例签名与 `noticehub.ports`。这条线以下归重写方，以上归主线。主线并行改 Web 时只依赖这两处，不读重写方进行中的实现。
