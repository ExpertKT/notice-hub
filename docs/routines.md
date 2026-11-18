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
6. `TEMP`/`TMP` 必须指到**主仓库**下：`$env:TEMP='F:\qq-notice-hub\nh-tmp\t-<谁>'; $env:TMP=$env:TEMP`。`F:\nh-agent\.venv` 是指向主仓库 `.venv` 的 junction，这个 python 只能写 `F:\qq-notice-hub` 之下；指到 `F:\nh-agent\nh-tmp\...` 会在 `tests/__init__.py:16` 的 `tempfile.mkdtemp()` 处报 `FileNotFoundError: No usable temporary directory` 或 `PermissionError [Errno 13]`，整批用例一条都跑不起来（2026-11-19 踩过）。

## 三、推送判据（照抄，不用问）

在 `F:\nh-agent` 上：

```powershell
git rev-parse HEAD:noticehub
$env:NH_GATE_BYPASS='1'   # 仅当上一行 == gate-records\last-full.json 的 product 字段，且本次只动 docs/tools/tests
git -c "http.https://github.com.proxy=" -c "http.proxy=" push origin agent/unified-shell
git ls-remote origin agent/unified-shell   # 回读 sha，与本地 HEAD 比对
```

只推 `agent/unified-shell` 一个 ref，不推 main，不带 force。

## 四、按文件所有权分活（比赛分支这条线）

一个文件同时只归一个人，改前先说，改完立刻报文件:行。目的只有一个：两个 Nova 别在同一个文件上互相覆盖。

| 谁 | 归它的文件 | 不许碰 |
| --- | --- | --- |
| 地图 | `web_static/js/trip.js`、`web_static/js/map3d.js`、`application/agent_plan.py` 里的 `map_config` / keys / `save_poi` / `save_here` | `page.html` 的设置页、`campus_roads.py` |
| 链路 | `application/campus_roads.py`、`application/route_tools.py` 里与路径计算相关的部分 | 任何 `web_static/js/*` |
| 界面 | `page.html`、`web_static/css/*`、设置页新增控件、动效 | `trip.js`、`campus_roads.py` |
| 手机端 | `pwa.js`、manifest、service worker、390px 专项 | 上面三行的文件 |
| 文稿/宣传片 | `docs/**`、`deliverables/**` | 产品代码 |

派活时写清三件事：可复制的验收命令、红线、交什么（文件路径 + 要贴的数）。判断型活（方案选择、根因、对账）不留给自己以外的人；Nova 只执行，不在任务里做取舍。

**改文件前先看一眼有没有别人刚动过它**：

```powershell
(Get-Item <文件>).LastWriteTime; git diff --stat -- <文件>
```

十分钟内被别人动过、而你要改的是同一块（同几行、同名类、同一条规则），停手报我，别并行覆盖。若是别人留下半成品（例如已写好的 CSS keyframes、已导出的公用函数），就在他的成果上接着做、并在注释里点名它，不要另起一套重写。这条是 2026-11-19 01:15 踩出来的：`page.html` 上我和 `界面` 同时改，`界面` 的帮手注释引用了我的 keyframes，才算没撞坏。

「对方模型报错」不等于它停下了。派出去的活只要没收到 `channel ok` 回执，文件仍可能在被改（01:15 `界面` 报 `PI_AI_ERROR`，实际仍在写 `page.html`）。

## 五、回执格式

第一行写 `channel ok` 或原始报错。然后：命令原文、退出码、它打出来的那个数（条数/秒数/字节数/sha）。
只写「已完成」「应该没问题」的回执不算回执，退回重做。

同一处卡两次就停手，把卡住的原文贴回来，别在原地换参数硬试。
