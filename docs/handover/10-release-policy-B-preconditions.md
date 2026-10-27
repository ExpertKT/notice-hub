# 10-release-policy-B-preconditions.md — 策略 B 的前提写死

用户要求：**策略 B 的前提写死；做不到就改 A。**

---

## 前提（三条，全部为硬约束）

**P1. 重写清单内文件只加不改。**

清单内文件（`dsh-handoff.md` 第 3 节）：`receiver.py`、`bot.py`、`retry.py`、
`timeutil.py`、`weburl.py`、`qq_digest.py`、`store.py`、`service.py`、`summarizer.py`、
`attachments.py`、`push.py`、`catchup.py`、`config.py`、`main.py`、11 个上游测试。

「只加不改」= 可以新增文件/新增函数/新增列（走既有迁移机制），
**不许改既有函数的签名、不许改既有行为、不许删**。

**P2. 发版只走 Web / 前端 / 打包。**

即：`webapp.py`、`settings_api.py`、`updates_api.py`、`routes.py`、
`web_static/`、`packaging/`、`qq_live_digest/{announce,community,selfcheck,tunnel,ics,updater,update_sources,version}.py`
这些域可以改；**清单内文件不许进发版 diff**。

**P3. 每个发版前跑一次双向 diff，记录两边都改过的文件。**

```powershell
git log --oneline main ^rewrite/clean-room     # clean-room 相对 main 少了什么
git log --oneline rewrite/clean-room ^main     # main 相对 clean-room 少了什么
git diff --name-only main rewrite/clean-room   # 直接列共同改动文件
```

成本：一条命令。不做 = 合并时靠记忆，必漏。

## 一句话结论

**当前能做到，选 B。** 依据：
工作区 19 条改动里，碰清单内文件的只有 `service.py`（S1 事件提取接入待办门槛，
`service.py:982-1062`）—— 这是**唯一一条踩线**的，且它已经落地并测过。

## 那一条例外怎么处理

`service.py` 属清单内文件（P1/P2 都禁改），但它已经改了。三个选项：

| 选项 | 做法 | 代价 |
|---|---|---|
| **a. 接受为既成事实** | 提交时在 commit message 写明「清单内文件改动，合并时需移植」 | 0 |
| b. 回退 S1 | `git checkout -- qq_live_digest/service.py`，S1 延到重写后 | 丢一个已验证的修复 |
| c. 搬出去 | 把 `_merge_extracted_event`/`_event_level_blocks` 挪到新模块，`service.py` 只留调用 | 一次小重构 + 测试跟着动 |

**建议 a**：S1 已经全量测过（1325 项绿），回退是丢功能；搬出去是白做一次重构。
按 commit message 记账，合并时逐个移植正是 `dsh-handoff.md` 第 3 节定的两阶段策略。

## 什么时候必须改 A

命中任意一条，**立即改 A（暂停发版）**，不要硬撑：

1. **clean-room 已经能独立跑通核心链路**（消息进 → 待办出），
   **且** `main` 上连续两个版本都是纯 UI/文案。
2. **发版需要改清单内文件的行为**（不是加列/加函数，而是改语义）——
   说明 P1 已被打破，B 的前提不成立。
3. **G4 验收签字人缺位超过两周** —— 没有验收人，B 的「随时可回退」优势发挥不出来，
   反而让半成品长期并存。
4. **P3 的双向 diff 连续两次发现共同改动文件** —— 说明两边已经开始同向漂移，
   再并行就是双倍维护。

## 合并成本（承接 08-strategy.md）

约 10–16 人日，前提是 G1 契约一次定死。若契约返工，乘 1.5。

---

## 必须由人补的项

1. **「11 个上游测试」已核实**：`git ls-tree -r --name-only upstream/main -- tests/`
   恰好 11 个 `.py`（`tests/__init__.py` + 10 个 `test_*.py`）：
   `test_attachments.py`、`test_catchup.py`、`test_config.py`、`test_push.py`、
   `test_receiver.py`、`test_retry.py`、`test_service.py`、`test_store.py`、
   `test_summarizer.py`、`test_webapp.py`。
   **这 11 个文件在重写范围内**，其余 48 个 `test_*.py` 是新增的。
   注意 `test_webapp.py` 同时在上游清单里（上游就有它），
   但当前 6758 行的版本绝大部分是本仓库新增内容。
2. **G4 验收签字人**：用户已定「项目负责人签字，开发负责人出回归报告不签字」，
   但**项目负责人当前是谁**未指名（B 已退出）。
