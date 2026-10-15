"""Pure status-to-row decisions for the service self-check panel."""


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else default


def _setting(settings, key, default=None):
    return getattr(settings, key, default) if settings is not None else default


def feature_items(
    settings,
    *,
    doctor=None,
    napcat=None,
    hosting=None,
    dingtalk=None,
    wechat=None,
    chaoxing=None,
    sync=None,
    push_channels=None,
) -> list[dict]:
    """Return the fixed, ordered feature status rows without doing I/O."""
    if doctor is None:
        qq = ("bad", "自检不可用", "一键接入模块没装上，重启程序；还不行就是安装包不完整。")
    elif _get(doctor, "ok"):
        nickname = _get(napcat, "nickname")
        qq = ("ok", f"已登录 · {nickname}" if nickname else "已接通 · OneBot 在线", "")
    else:
        qq = ("bad", _get(doctor, "broken") or "QQ 接入不通", _get(doctor, "hint") or "")

    if _get(hosting, "hosting_active"):
        hosting_row = ("ok", f"托管中 · {_get(hosting, 'groups_selected') or 0} 个群", "")
    else:
        hosting_row = ("warn", "未开启", "不开始托管就不会自动收群消息；点「开始托管」并保持 QQ 在线。")

    if not _setting(settings, "dingtalk_enabled"):
        dt = ("warn", "未启用", "")
    elif not _setting(settings, "dingtalk_group_whitelist"):
        dt = ("warn", "已启用，但没勾群", "到「设置 → 钉钉」勾选要收的群。没勾群时监听不会启动，点「立即扫描」也不会动。")
    elif _get(dingtalk, "scan_state") == "error":
        dt = ("bad", "上一次扫描失败", _get(dingtalk, "last_error") or "到「设置 → 钉钉」点「立即扫描一次」看具体报错。")
    elif _get(dingtalk, "running"):
        dt = ("ok", f"已连接实时监听 {_get(dingtalk, 'groups') or 0} 个群", "")
    else:
        dt = ("warn", "监听未启动", "到「设置 → 钉钉」检查设置；保存钉钉设置会重新连接监听。")

    if not _setting(settings, "wechat_enabled"):
        wx = ("warn", "未启用", "")
    elif _get(wechat, "available") is False:
        wx = ("warn", "未找到微信数据", "本机没找到微信；确认微信已安装并登录过。")
    elif not _setting(settings, "wechat_group_whitelist"):
        wx = ("warn", "已启用，但没勾群", "到「设置 → 微信」勾选要收的群。没勾群时扫描不会启动，点「立即扫描」也不会动。")
    elif _get(wechat, "scan_state") == "error":
        wx = ("bad", "上一次扫描失败", _get(wechat, "last_error") or "到「设置 → 微信」点「立即扫描一次」看具体报错。")
    elif _get(wechat, "running"):
        wx = ("ok", f"已连接 {_get(wechat, 'groups') or 0} 个群", "")
    else:
        wx = ("warn", "扫描未启动", "到「设置 → 微信」点「立即扫描」。首次要 3–6 分钟扫内存取密钥，之后是几秒。")

    if not _setting(settings, "chaoxing_enabled"):
        cx = ("warn", "未启用", "")
    elif not _setting(settings, "chaoxing_cookie"):
        cx = ("warn", "已启用，但没填 Cookie", "到「设置 → 学习通」粘贴 Cookie；没填时同步不会启动。")
    elif _get(chaoxing, "expired"):
        cx = ("bad", "已过期，需重新粘贴", _get(chaoxing, "banner_text") or "点此重新登录学习通，然后回来粘贴 Cookie。")
    else:
        cx = ("ok", "已配置", f"上次同步：{_get(chaoxing, 'last_sync_at') or '暂无'}")

    if not _setting(settings, "attachments_enabled"):
        attachments = ("warn", "未启用", "")
    else:
        vision = bool(_setting(settings, "vision_active"))
        attachments = (
            "ok",
            f"已启用 · 单文件 ≤{_setting(settings, 'attachment_max_mb', 10)}MB、每小时 ≤{_setting(settings, 'attachment_max_per_hour', 30)} 个",
            f"图片识别：{'开' if vision else '关（当前端点无视觉模型）'}",
        )

    calendars = _get(sync, "calendars") or []
    public_calendar = next((item for item in calendars if _get(item, "kind") == "public"), None)
    if public_calendar is not None:
        count = _get(sync, "events") or _get(public_calendar, "events") or 0
        calendar = ("ok", f"可访问 · {count} 条事项", "")
    else:
        calendar = ("warn", "只有局域网地址", "手机用流量打不开局域网地址；装有 Tailscale 并开 Funnel 后这里会出现公网地址。")

    channels = push_channels if isinstance(push_channels, (list, tuple)) else []
    if channels:
        push = ("ok", "、".join(str(channel) for channel in channels), "")
    else:
        push = ("warn", "未配置", "截止提醒现在发不出去。到「设置 → 提醒怎么送到手机」里挑一种填好，或者改用手机日历订阅。")

    rows = []
    for key, label, result in (
        ("qq", "QQ 登录", qq),
        ("hosting", "引擎托管", hosting_row),
        ("dingtalk", "钉钉", dt),
        ("wechat", "微信", wx),
        ("chaoxing", "学习通 Cookie", cx),
        ("attachments", "附件解析", attachments),
        ("calendar", "公网日历", calendar),
        ("push", "消息推送", push),
    ):
        state, value, hint = result
        rows.append({"key": key, "label": label, "state": state, "value": value, "hint": hint})
    return rows


def check(settings, **kwargs) -> dict:
    return {"ok": True, "items": feature_items(settings, **kwargs)}
