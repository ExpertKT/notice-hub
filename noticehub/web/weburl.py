"""给用户点的网页地址：局域网 IP 探测、基址选择、令牌与待办锚点。"""

from __future__ import annotations

import socket
from typing import Any
from urllib.parse import urlencode

# 借用这个公网地址只为了问内核「默认出口走哪块网卡」，不真发包。
_ROUTE_PROBE = ("223.5.5.5", 80)

# 绑成这些地址时浏览器是打不开的，得换成真实的局域网地址。
_UNREACHABLE_HOSTS = {"0.0.0.0", "::", ""}


def _trim_trailing_slashes(text: str) -> str:
    return text.rstrip("/")


def _compose_base(settings: Any, base_url: str) -> str:
    """优先用显式基址；没有就按 host:port 现拼，并把不可达的 host 换成局域网地址。"""
    explicit = _trim_trailing_slashes(str(base_url or "").strip())
    if explicit:
        return explicit
    configured = _trim_trailing_slashes(str(getattr(settings, "web_base_url", "") or "").strip())
    if configured:
        return configured
    host = str(getattr(settings, "web_host", "") or "").strip()
    if host in _UNREACHABLE_HOSTS:
        host = detect_lan_ip()
    return "http://%s:%s" % (host, getattr(settings, "web_port", 8766))


def detect_lan_ip() -> str:
    """本机在默认出口上的地址；探测不成一律回落回环地址。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(_ROUTE_PROBE)
        return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def build_web_url(
    settings: Any,
    *,
    token: str = "",
    task_id: int | None = None,
    base_url: str = "",
) -> str:
    """拼一条可直接点开的地址：基址永远以 `/` 收尾，令牌进查询串，任务进片段。"""
    url = _compose_base(settings, base_url) + "/"
    if token:
        url += "?" + urlencode({"token": str(token)})
    if task_id is not None:
        url += "#task-%d" % int(task_id)
    return url
