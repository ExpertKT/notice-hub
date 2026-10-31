"""日历订阅地址的排序和推荐。网卡、隧道探测由调用方传入，这里不读环境。"""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Any, Sequence

# 手机连不到的网段。192.168.56/99 落在 192.168/16 里，必须先判虚拟再判局域网档。
VIRTUAL_NETWORKS = (
    "192.168.56.0/24",   # VirtualBox host-only
    "192.168.99.0/24",   # Docker Toolbox / 老 VirtualBox
    "192.168.122.0/24",  # libvirt
    "172.17.0.0/16",     # Docker 默认桥
    "172.18.0.0/16", "172.19.0.0/16", "172.20.0.0/16", "172.21.0.0/16",
    "172.22.0.0/16", "172.23.0.0/16", "172.24.0.0/16", "172.25.0.0/16",
    "172.26.0.0/16", "172.27.0.0/16", "172.28.0.0/16", "172.29.0.0/16",
    "172.30.0.0/16", "172.31.0.0/16",  # WSL2 默认段
)


def is_virtual_host(host: str) -> bool:
    """明显是虚拟网卡的地址：手机永远连不到，排到最后并给前端置灰。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    for network in VIRTUAL_NETWORKS:
        if ip in ipaddress.ip_network(network):
            return True
    return False


def host_priority(host: str) -> tuple[int, str]:
    """越小越优先：真实 192.168 → 10.x → Tailscale 100.64/10 → 其它 → 虚拟网卡。

    同档再按地址字符串排序，保证同一台机器每次返回同样的顺序。
    """
    if is_virtual_host(host):
        return (9, host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return (5, host)
    if ip in ipaddress.ip_network("192.168.0.0/16"):
        return (0, host)
    if ip in ipaddress.ip_network("10.0.0.0/8"):
        return (1, host)
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return (2, host)
    return (5, host)


def primary_calendar_url(calendars: Sequence[dict[str, Any]]) -> str:
    """唯一推荐地址：有公网用公网；否则取排序后第一条非虚拟；全是虚拟就退回第一条。"""
    for item in calendars:
        if item.get("kind") == "public":
            return str(item.get("url") or "")
    real = [item for item in calendars if not item.get("virtual")]
    chosen = real or list(calendars)
    return str(chosen[0].get("url") or "") if chosen else ""


def _suffix(token: str) -> str:
    return "?token=" + urllib.parse.quote(token, safe="") if token else ""


def assemble_calendars(
    hosts: Sequence[str],
    public_url: str = "",
    *,
    port: int,
    token: str,
) -> list[dict[str, Any]]:
    """把已经探测好的地址收成日历列表。公网在前，局域网按 host_priority 排。"""
    suffix = _suffix(token)
    calendars: list[dict[str, Any]] = []
    if public_url:
        calendars.append({
            "name": "公网地址",
            "kind": "public",
            "url": public_url + suffix,
            "hint": "手机用流量或任意 Wi-Fi 都能订阅",
            "virtual": False,
        })
    for host in sorted(hosts, key=host_priority):
        virtual = is_virtual_host(host)
        calendars.append({
            "name": f"局域网 {host}",
            "kind": "lan",
            "url": f"http://{host}:{port}/calendar.ics{suffix}",
            "hint": "虚拟机/容器网卡，手机连不到" if virtual else "手机与电脑在同一个网络时可用",
            "virtual": virtual,
        })
    return calendars


def describe_calendar_subscription(
    hosts: Sequence[str],
    public_url: str = "",
    *,
    port: int,
    token: str,
    events: int,
) -> dict[str, Any]:
    """GET /api/sync/info 的成功体。空列表由 Web 决定回 503，这里不改状态码。"""
    calendars = assemble_calendars(hosts, public_url, port=port, token=token)
    for item in calendars:
        item["events"] = events
    lan_base = next(
        (item["url"][: item["url"].index("/calendar.ics")] for item in calendars if item["kind"] == "lan"),
        "",
    )
    return {
        "ok": True,
        "primary": primary_calendar_url(calendars),
        "lan_base": lan_base,
        "port": port,
        "events": events,
        "calendars": calendars,
    }


def render_calendar_ics(store: Any, *, now: Any = None) -> str:
    """GET /calendar.ics 的正文。鉴权留在 Web，这里只取全部待办并渲染。"""
    from qq_live_digest.ics import render_calendar

    return render_calendar(store.list_tasks(limit=None), now=now)
