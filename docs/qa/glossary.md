# 术语表：一个产品，三个名字

以后所有人（人、Bot、AI）写文档、提 issue、发公告、改代码前，先看这张表。

## 一、名字怎么用

| 场景 | 用什么 | 例子 |
|------|--------|------|
| 面向用户的中文文案 | 群务台 | 页面标题、公告、文档正文 |
| 仓库 / GitHub 项目 / 发行包文件名 | notice-hub | `ExpertKT/notice-hub`、`QQ-Notice-Hub.zip` |
| Python 包 / 代码模块 | qq_live_digest | `qq_live_digest.webapp` |
| 计划任务 / 进程名 / 日志文件 | 历史遗留，不改 | `QQ-Live-Digest`、`qq-live-digest.log` |

## 二、三条硬规则

1. **命令、路径、URL、发行包名一律跟现实一致**，不为了「统一」去改。改了就是 bug：用户复制粘贴执行会失败，自动更新会找不到包。
2. **新写的对外文案一律用「群务台」**，不再出现「notice-hub 支持…」这种把仓库名当产品名的句子。
3. **历史遗留的进程名 / 任务名 / 日志名保留**。要改必须单独一次改动 + 迁移脚本（否则老用户机器上残留旧任务名，新代码又按新名字找，看护和自启会双份或失效），不能顺手改。

## 三、改名前自查

先跑：

```text
grep -rn "notice-hub" docs/ README.md
```

（Windows PowerShell：`Select-String -Path README.md,docs\*.md,docs\**\*.md -Pattern 'notice-hub'`）

然后逐条判断属于哪一类：

- **仓库名（保留）**：GitHub URL、`git clone` 地址、badge、`QQ-Notice-Hub.zip` / `QQ-Notice-Hub.exe` / `notice-hub-android-*.apk`、示例目录路径、代码路径里的包名。
- **产品名（改群务台）**：句子里在讲「这个软件能干什么」，例如「notice-hub 只连接 OneBot」「直接用 notice-hub 页面点」。
- **字面量（保留，改了会坏）**：OneBot 实例名 `qq-notice-hub`、计划任务名 `QQ-Live-Digest`、日志文件 `qq-live-digest.log`、桌面快捷方式名。这些是程序写死或用户机器上已存在的字符串，必须两边一致。

判断不出来的，别猜，先问再改。
