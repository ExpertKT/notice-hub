"""用户可见文案的单一来源。

已知重复：``qq_live_digest/web_static/page.html`` 与 ``docs/`` 里仍各有一份同样的
句子。本轮只收 ``qq_live_digest/`` 里的代码副本，页面那份等下一轮改成接口下发再动。

只放最容易走样的几句（用户照着做也不一定成功的那些）；普通 UI 标题、日志不用往
这里搬。
"""

from __future__ import annotations

# 安装 / 拉起
QQ_NOT_FOUND_GUIDE = (
    "未找到电脑版 QQ NT。NapCat Shell 本身不含 QQ.exe，它是挂在官方 QQ 上的外挂引擎，"
    "所以必须先装官方 QQ。三步：① 点「下载官方 QQ」装好官方 QQ NT；② 装完不用打开 QQ；"
    "③ 回到本页点「重新检测」。"
)
NAPCAT_DIR_HINT = (
    "点「自动下载并安装 NapCat」，或在本目录 .env 里把 QQ_DIGEST_NAPCAT_DIR "
    "指向 NapCat 引擎目录（里面有 NapCatWinBootMain.exe 的那个）"
)

# 管理员权限（run_elevated 的返回值）
ADMIN_CANCELLED = "你取消了管理员权限，可以再点一次。"
ADMIN_FAILED = "请求管理员权限失败（{detail}）。"

# 公网入口（Tailscale Funnel）
TAILSCALE_NOT_RUNNING = "没连上 Tailscale（命令跑不起来）：请先打开 Tailscale 并登录，再点一次「开通公网入口」。"
TAILSCALE_NOT_LOGGED_IN = "Tailscale 还没登录：请先打开 Tailscale 用同一个账号登录，再点一次「开通公网入口」。"
FUNNEL_NOT_ENABLED = (
    "Tailscale 账户还没开启 Funnel：去 Tailscale 管理后台的 Access Controls 里允许 Funnel"
    "（免费版也支持），保存后再点一次。"
)

# 数据源
CHAOXING_COOKIE_EXPIRED = (
    "学习通登录已过期。重新登录学习通后，右键托盘图标选「读取学习通 Cookie」，"
    "或在设置里点「立即校验」。"
)
WECHAT_DIR_NOT_FOUND = "没找到微信数据目录（含 db_storage 的文件夹）。"

# 防火墙
FIREWALL_NEEDS_TRAY = "需要托盘程序在运行才能提权"
