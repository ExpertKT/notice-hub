# 日常细活的固定分工

这份文件解决两件事：什么活不该我干；干完拿什么证明干完了。
改了这里的规则，改这一份就够，不要把规则同时写在几个 bot 的说明里再各说一套。

## 一、谁干什么

判断型（要读代码、要比数字、要下结论）只给聪明的：我、攻坚、复核。
执行型（命令已写到能照抄）全部给 Nova。Nova 中文弱，派活用英文，回执英文由我翻。

| 活 | 耗时 | 谁 | 附带的硬要求 |
| --- | --- | --- | --- |
| 跑相关测试模块 | 15–60 秒；`test_webapp` 504 项约 210 秒 | Nova | 连跑两次、两次退出码都贴上 |
| 全量门禁 `--tier full` | 约 700 秒 / 2532 项 | Nova 发起收日志，判定给复核 | `product` 字段与推送前 HEAD 一致才算绿 |
| 截图 1280/390 × 明暗 | 30–60 秒 | Nova | 两张必拍：390 亮、1280 暗 |
| 更新 changelog 未发布节 | 1–2 分钟 | Nova | 一条一句话，带文件 |
| `gen_facts.py` + `render_docs.py` | 2 秒内 | Nova | `facts.json` 现算，别抄旧数 |
| 重渲计划书 PDF | 约 4 秒 | Nova | 打完报字节数（当前 503,321） |
| 同步交付夹 | 1–2 分钟 | Nova | 必带自检 `grep "{{facts." ` 命中 0 |
| 出演示包 `packaging/build.ps1` | 数分钟 | Nova | 系统 3.14 + `PYTHONPATH` 指 venv，包内不得有 `llm.key`/`.env` |
| 冷装验证 | 3–5 分钟 | Nova | 健康接口真连一次，报秒数 |
| 推分支 | 1–2 分钟 | 闸门执行 | 见第三节，推完 `ls-remote` 回读 sha |
| 分支基线整理 | 5–10 分钟 | 我 + 复核 | — |
| 进度落盘 | 20 秒 | Nova | 追加 `.reports/agent-progress.md` |
| 红是真红还是环境 / 说法与代码对不对 / 数字对账 / 降智判断 | 十几分钟 | 复核 | 报告必须带能打假的证据路径 |

## 二、五条已定的规矩

1. 全量仍然每批一次，但不许我盯着跑：交 Nova 后台发起，日志收下来，判定让我或复核做。
2. 浏览器截图只两张是必拍的：390 宽亮色、1280 宽暗色。另外两张（390 暗、1280 亮）留到发版前再补。
3. 这份文件就是固定手段。新规矩进这里，不进聊天记录。
4. 推送不拆开：走 full 还是 bypass、`product` 对不对，是同一个判断，拆开就没人负责。
5. 同一时刻只准一个执行端在跑。门禁、浏览器用例、截图脚本共用 `nh-tmp`，并行就是假红。

## 三、推送判据（照抄，不用问）

在 `F:\nh-agent` 上：

```powershell
git rev-parse HEAD:noticehub
$env:NH_GATE_BYPASS='1'   # 仅当上一行 == gate-records\last-full.json 的 product 字段，且本次只动 docs/tools/tests
git -c "http.https://github.com.proxy=" -c "http.proxy=" push origin agent/unified-shell
git ls-remote origin agent/unified-shell   # 回读 sha，与本地 HEAD 比对
```

只推 `agent/unified-shell` 一个 ref，不推 main，不带 force。

## 四、回执格式

第一行写 `channel ok` 或原始报错。然后：命令原文、退出码、它打出来的那个数（条数/秒数/字节数/sha）。
只写「已完成」「应该没问题」的回执不算回执，退回重做。

同一处卡两次就停手，把卡住的原文贴回来，别在原地换参数硬试。
