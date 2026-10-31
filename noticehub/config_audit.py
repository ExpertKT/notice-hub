"""配置体检：把「实际生效的值 + 它从哪来」列成一张表，设置页直接用。

只做事实汇总，不联网、不起子进程、不写文件。慢的探测（微信存储路径、NapCat 引擎
目录、端口占用）这里一律不碰——设置页随时会调它，宁可少说一句，也不许卡住或弹出
PowerShell 窗口。

来源判定只有一条规则，和 `config.load_env_file` 的 `os.environ.setdefault` 对齐：
环境变量与配置文件的值**不同**才算 `env`（配文件的值填进 `shadowed`）；相同就算
`dotenv`。所以调用方先 `Settings.from_env()` 再调用本模块即可，顺序不影响结果。

安全：`value` / `shadowed` 里不出现 token、API key、Cookie 原文。凭据类字段只报
「已设置 / 未设置」。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable

from .bootstrap.settings import LEGACY_ENV_PREFIX, NEW_ENV_PREFIX, env_get
from .config import Settings, parse_env_file
from .version import APP_CHANNEL

SET = "已设置"
UNSET = "未设置"

#: 推送通道的配置键，用来判断「通道是谁配的」。
_PUSH_ENV_KEYS = (
    "WXPUSHER_APP_TOKEN",
    "WXPUSHER_UIDS",
    "WXPUSHER_TOPIC_IDS",
    "SERVERCHAN_KEYS",
    "PUSHPLUS_TOKENS",
    "WEBHOOKS",
)


def _flag(value: bool) -> str:
    return "开启" if value else "关闭"


def _text(value: Any) -> str:
    return str(value if value is not None else "")


class _EnvFile:
    """配置文件里的值（读一次，不重复解析）。"""

    def __init__(self, env_file: Path | str | None) -> None:
        self._values: dict[str, str] = {}
        if env_file:
            try:
                self._values = {str(k): str(v) for k, v in parse_env_file(env_file).items()}
            except OSError:
                self._values = {}

    def get(self, key: str) -> str:
        return str(self._values.get(key) or "").strip()

    def any(self, suffix: str) -> str:
        """按新旧两套名字取：新名存在就取新名（哪怕为空），否则回退旧名。"""
        new_key = NEW_ENV_PREFIX + suffix
        if new_key in self._values:
            return str(self._values.get(new_key) or "").strip()
        return str(self._values.get(LEGACY_ENV_PREFIX + suffix) or "").strip()


def _source_for(suffix: str, env_file: _EnvFile) -> tuple[str, dict[str, str] | None]:
    """返回（来源, shadowed）。来源只会是 env / dotenv / default。

    ``suffix`` 不带前缀：新旧两套名字都认，新名优先。
    """
    inherited = env_get(suffix).strip()
    from_file = env_file.any(suffix)
    if inherited and from_file and inherited != from_file:
        return "env", {"value": from_file, "source": "dotenv"}
    if inherited and not from_file:
        return "env", None
    if from_file:
        return "dotenv", None
    return "default", None


def _entry(
    key: str,
    label: str,
    value: str,
    source: str,
    *,
    shadowed: dict[str, str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": value,
        "source": source,
        "shadowed": shadowed,
        "note": note,
    }


def _plain(
    env_file: _EnvFile,
    *,
    key: str,
    label: str,
    env_key: str,
    value: str,
    note: str = "",
) -> dict[str, Any]:
    source, shadowed = _source_for(env_key, env_file)
    return _entry(key, label, value, source, shadowed=shadowed, note=note)


def _snapshot_text(snapshot: Any, key: str) -> str:
    """调用方算好的快照值；没有就是空串（快照永远是可选参数）。"""
    if not isinstance(snapshot, dict):
        return ""
    return str(snapshot.get(key) or "").strip()


def _auto(key: str, label: str, value: str, note: str) -> dict[str, Any]:
    return _entry(key, label, value, "auto", note=note)


def _port_item(settings: Settings, env_file: _EnvFile, snapshot: Any) -> dict[str, Any]:
    return _plain(
        env_file,
        key="web_port",
        label="服务端口",
        env_key="WEB_PORT",
        value=str(int(settings.web_port)),
        note="打不开页面时先核对浏览器地址栏的端口；被占用时启动器会临时换到相邻端口。",
    )


def _napcat_item(settings: Settings, env_file: _EnvFile, snapshot: Any) -> dict[str, Any]:
    probed = _snapshot_text(snapshot, "napcat_api_url")
    if probed:
        return _auto(
            "napcat_api_url",
            "NapCat 接口地址",
            probed,
            "这是按本机 NapCat 的配置探测到的地址；连不上就手填一个。",
        )
    value = _text(settings.napcat_api_url).strip()
    source, shadowed = _source_for("NAPCAT_API_URL", env_file)
    note = ""
    if source == "default" and value and value != "http://127.0.0.1:3001":
        # 没在配置里写死、也不是代码默认值：那是程序按本机 NapCat 配置探测出来的。
        source = "auto"
        note = "这是按本机 NapCat 的配置探测到的地址；连不上就手填一个。"
    elif source == "default":
        note = "NapCat 换了端口就改这里；连不上先确认 NapCat 正在运行。"
    return _entry("napcat_api_url", "NapCat 接口地址", value, source, shadowed=shadowed, note=note)


def _wechat_item(settings: Settings, env_file: _EnvFile, snapshot: Any) -> dict[str, Any]:
    located = _snapshot_text(snapshot, "wechat_storage")
    if located:
        return _auto(
            "wechat_storage",
            "微信存储位置",
            located,
            "这是程序自动找到的目录；不对就回设置里手动选。",
        )
    configured = _text(settings.wechat_dir).strip()
    source, shadowed = _source_for("WECHAT_DIR", env_file)
    if configured:
        note = "微信换了存储位置就改这里，否则扫不到新消息。"
    else:
        source = "unset"
        shadowed = None
        note = "没填就自动找；找不到（换过电脑、改过微信存储位置）到设置里手动选目录。"
    return _entry("wechat_storage", "微信存储位置", configured or UNSET, source, shadowed=shadowed, note=note)


def _chaoxing_item(settings: Settings, env_file: _EnvFile, snapshot: Any) -> dict[str, Any]:
    has_cookie = bool(_text(settings.chaoxing_cookie).strip())
    source, shadowed = _source_for("CHAOXING_COOKIE", env_file)
    if source == "default":
        source = "dotenv" if has_cookie else "unset"
    if has_cookie:
        note = "登录信息会过期，抓不到作业时到设置里重新粘一次。"
    else:
        note = "到设置 → 学习通展开「高级：手动粘贴登录凭证」，没配就抓不到课程和作业。"
    # 只报有没有，原文不出现在任何字段里。
    return _entry("chaoxing_cookie", "学习通登录凭证", SET if has_cookie else UNSET, source, shadowed=shadowed, note=note)


def _push_item(settings: Settings, env_file: _EnvFile, snapshot: Any) -> dict[str, Any]:
    channels: Iterable[str] = settings.push_channels()
    names = [str(name) for name in channels]
    if not names:
        return _entry(
            "push_channel",
            "提醒推送通道",
            UNSET,
            "unset",
            note="到设置 → 提醒推送里配一个通道（WxPusher / Server 酱 / PushPlus / 机器人），配完点「发一条测试消息」。",
        )
    source = "default"
    for env_key in _PUSH_ENV_KEYS:
        if env_key == "WEBHOOKS":
            inherited = env_get("WEBHOOKS").strip()
            from_file = env_file.any("WEBHOOKS")
        else:
            inherited = str(os.environ.get(env_key) or "").strip()
            from_file = env_file.get(env_key)
        if inherited and from_file and inherited != from_file:
            source = "env"
            break
        if inherited or from_file:
            source = "dotenv"
    return _entry("push_channel", "提醒推送通道", "、".join(names), source, note="收不到提醒时到设置里点「发一条测试消息」核对。")


def _dir_item(
    settings: Settings, env_file: _EnvFile, snapshot: Any, *, key: str, label: str, env_key: str, value: Path, note: str
) -> dict[str, Any]:
    return _plain(env_file, key=key, label=label, env_key=env_key, value=str(value), note=note)


def _update_item(settings: Settings, env_file: _EnvFile, snapshot: Any) -> dict[str, Any]:
    channel = _text(settings.update_channel).strip() or APP_CHANNEL
    source, shadowed = _source_for("UPDATE_CHANNEL", env_file)
    if source == "default":
        note = "没写就用内置 %s 通道；想收正式版，在同一份配置里把 NOTICEHUB_UPDATE_CHANNEL 写成 stable。" % APP_CHANNEL
    else:
        note = "换通道从下一次检查更新开始生效。"
    return _entry("update_channel", "更新通道", channel, source, shadowed=shadowed, note=note)


def _llm_item(settings: Settings, env_file: _EnvFile, snapshot: Any) -> dict[str, Any]:
    return _plain(
        env_file,
        key="llm_model",
        label="AI 模型",
        env_key="LLM_MODEL",
        value=_text(settings.dashscope_model),
        note="AI 摘要不准就先换个模型试；模型名要和所接服务商一致。",
    )


def _attachments_item(settings: Settings, env_file: _EnvFile, snapshot: Any) -> dict[str, Any]:
    enabled = bool(settings.attachments_enabled)
    return _plain(
        env_file,
        key="attachments_enabled",
        label="附件解析",
        env_key="ATTACHMENTS",
        value=_flag(enabled),
        note="附件内容会一起进摘要；不想读文档就在设置里关掉。" if enabled else "现在只记文件名；要看文档内容就在设置里打开附件解析。",
    )


_BUILDERS: tuple[Callable[[Settings, _EnvFile, Any], dict[str, Any]], ...] = (
    _port_item,
    _napcat_item,
    _wechat_item,
    _chaoxing_item,
    _push_item,
    lambda settings, env_file, snapshot: _dir_item(
        settings,
        env_file,
        snapshot,
        key="data_dir",
        label="数据目录",
        env_key="DATA_DIR",
        value=settings.data_dir,
        note="换目录前先把旧目录整个搬过去，否则历史记录会像丢了。",
    ),
    lambda settings, env_file, snapshot: _dir_item(
        settings,
        env_file,
        snapshot,
        key="log_dir",
        label="日志目录",
        env_key="LOG_DIR",
        value=settings.log_dir,
        note="出问题时把这里的 noticehub.log 发出来。",
    ),
    _update_item,
    _llm_item,
    _attachments_item,
)


def audit_config(settings: Settings, snapshot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """列出 10 项配置的实际值与来源；纯读，无副作用。

    ``snapshot`` 是调用方**已经算好**的少数探测结果，目前只认
    ``wechat_storage``（实际定位到的目录）与 ``napcat_api_url``（探测/写回的地址）：
    有值就按 ``auto`` 报出实际值。函数本身不起子进程、不联网——谁来算这份快照是
    调用方的事，所以它是可选参数，不传时行为与原来一致。

    返回顺序固定（前端按 key 定位，不依赖顺序）。secret 类字段只会是
    ``"已设置"`` / ``"未设置"``。
    """
    env_file = _EnvFile(getattr(settings, "env_file", None))
    return [builder(settings, env_file, snapshot) for builder in _BUILDERS]
