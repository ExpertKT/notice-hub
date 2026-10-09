"""手机端待办台：一个极小的 HTTP 服务，读取 tasks 表并支持勾选完成。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import shutil
import socket
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import qr as qr_encoder
from . import pairing
from .config import DEFAULT_VISION_MODEL, RUNTIME_ROOT, Settings, split_list, update_env_file
from .ics import render_calendar
from .store import Store, effective_urgent
from .timeutil import iso, now_local, parse_iso

try:
    from . import catchup
except Exception:  # noqa: BLE001
    catchup = None  # type: ignore[assignment]

try:
    from . import inbox
except Exception:  # noqa: BLE001
    inbox = None  # type: ignore[assignment]

try:
    from . import group_suggest
except Exception:  # noqa: BLE001
    group_suggest = None  # type: ignore[assignment]

try:
    from . import hosting
except Exception:  # noqa: BLE001
    hosting = None  # type: ignore[assignment]

try:  # 「一键接入」实现；缺失时向导降级为手工接线而不是整站报错
    from . import napcat_admin
except Exception:  # noqa: BLE001
    napcat_admin = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
MAX_SYNC_QR_CHARS = 512

_JOB_LOCK = threading.Lock()
_JOB_STATE: dict[str, Any] = {
    "active": None,
    "cancel": None,
    "history": {"running": False, "stage": "idle", "done": 0, "total": 0, "note": "", "result": None},
    "classify": {"running": False, "stage": "idle", "done": 0, "total": 0, "note": "", "result": None},
}


def _lan_ipv4() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("8.8.8.8", 80))
        address = str(probe.getsockname()[0])
    if not address or address == "0.0.0.0" or address.startswith("127."):
        raise OSError("无法确定可供手机访问的局域网地址")
    return address


def _is_phone_reachable(address: str) -> bool:
    """环回、链路本地、通配地址手机一定连不上。"""
    return bool(address) and not (address.startswith("127.") or address.startswith("169.254.") or address == "0.0.0.0")


def _lan_hosts() -> list[str]:
    """本机所有可能被手机直连的 IPv4，默认路由地址排第一（最可能是手机用的那个）。

    只靠默认路由探测不够：本机实测拿到的是宽带拨号的运营商地址（手机连不上），
    所以这里把所有网卡地址都列出来，让用户能挑到手机真正能到达的那个。
    """
    hosts: list[str] = []
    try:
        hosts.append(_lan_ipv4())
    except OSError:
        pass
    try:
        hosts.extend(str(info[4][0]) for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    unique: list[str] = []
    for host in hosts:
        if _is_phone_reachable(host) and host not in unique:
            unique.append(host)
    return unique


def _tailscale_calendar_url(port: int) -> str:
    """Tailscale Funnel 暴露到公网的日历地址（手机在流量或任意 Wi-Fi 下都能订阅）。

    直接读 tailscale serve 的配置，确认确实有一条把 /xxx.ics 反代到本机这个端口的公网规则；
    没有装 Tailscale 或没有这条规则时返回空串，调用方退回局域网地址。
    """
    executable = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    if not executable or not Path(executable).is_file():
        return ""
    try:
        completed = subprocess.run([executable, "serve", "status", "--json"], capture_output=True, text=True, timeout=8, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        config = json.loads(completed.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    for target, entry in (config.get("Web") or {}).items():
        if not str(target).endswith(":443"):
            continue
        for route, handler in ((entry or {}).get("Handlers") or {}).items():
            proxy = str((handler or {}).get("Proxy") or "")
            # Proxy 是完整 URL（如 http://127.0.0.1:8766/calendar.ics），必须用 urlsplit 取端口：
            # 直接按 ":" 切最后一段会拿到 "8766/calendar.ics"，端口永远比不上，公网地址就消失了。
            try:
                proxy_port = urllib.parse.urlsplit(proxy).port
            except ValueError:
                continue
            if proxy_port != port or not str(route).lower().endswith(".ics"):
                continue
            return f"https://{str(target).rsplit(':', 1)[0]}{route}"
    return ""


TAILSCALE_FUNNEL_PORTS = (443, 8443, 10000)


def _tailscale_executable() -> str:
    executable = shutil.which("tailscale") or r"C:\Program Files\Tailscale\tailscale.exe"
    return executable if executable and Path(executable).is_file() else ""


def _tailscale_serve_config(executable: str) -> dict[str, Any]:
    try:
        completed = subprocess.run([executable, "serve", "status", "--json"], capture_output=True, text=True, timeout=8, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        config = json.loads(completed.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return config if isinstance(config, dict) else {}


def _tailscale_public_app_url(port: int) -> str:
    """手机 App 用的公网地址：整站（页面 + /api/*）都反代到本机这个端口的那条 Funnel 规则。

    和只认 .ics 的日历地址不同，这里必须有一条 "/" 路由，而且那个端口得在 AllowFunnel 里。
    """
    executable = _tailscale_executable()
    if not executable:
        return ""
    config = _tailscale_serve_config(executable)
    funnel = config.get("AllowFunnel") or {}
    for target, entry in (config.get("Web") or {}).items():
        target = str(target)
        if not funnel.get(target):
            continue
        host, _, port_text = target.rpartition(":")
        if port_text not in {str(value) for value in TAILSCALE_FUNNEL_PORTS}:
            continue
        proxy = str((((entry or {}).get("Handlers") or {}).get("/") or {}).get("Proxy") or "")
        try:
            if urllib.parse.urlsplit(proxy).port != port:
                continue
        except ValueError:
            continue
        return f"https://{host}" + ("" if port_text == "443" else f":{port_text}")
    return ""


def _enable_tailscale_funnel(port: int, public_port: int = 8443) -> dict[str, Any]:
    """电脑上点一下就整站开到公网（Tailscale Funnel，免费，不用买服务器）。"""
    executable = _tailscale_executable()
    if not executable:
        return {"ok": False, "error": "这台电脑上没找到 Tailscale，装了以后再点一次。"}
    if public_port not in TAILSCALE_FUNNEL_PORTS:
        return {"ok": False, "error": "Tailscale 只允许 443、8443、10000 这三个公网端口。"}
    command = [executable, "funnel", "--bg", f"--https={public_port}", f"http://127.0.0.1:{port}"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "error": f"执行 Tailscale 命令失败：{error}"}
    url = _tailscale_public_app_url(port)
    if url:
        return {"ok": True, "url": url}
    detail = (completed.stderr or completed.stdout or "").strip()
    return {"ok": False, "error": detail[:300] or "Tailscale 没有接受这条公网规则。"}


def _pair_help(base: str) -> dict[str, Any]:
    return {
        "base": base,
        "steps": [
            "电脑上这个页面已经在跑了，手机和电脑不用连同一个网络。",
            "手机装好群务台 App，打开后它自己会显示 6 位数字。",
            "在电脑上核对这 6 位数字，点一下「允许」，手机就自动连上了。",
        ],
        "no_public_hint": "还没开通公网入口。装上 Tailscale 后点下面的按钮，手机在任何网络下都能连。",
    }


def _calendar_suffix(token: str) -> str:
    return "?token=" + urllib.parse.quote(token, safe="") if token else ""


def _advertised_calendars(port: int, token: str) -> list[dict[str, Any]]:
    """本服务对外公布的日历订阅地址：公网（Tailscale Funnel）优先，其余是各网卡局域网地址。

    公网地址是唯一「手机用流量也能订阅」的路径；局域网地址排后面，供用户按自己网络情况挑选。
    """
    suffix = _calendar_suffix(token)
    calendars: list[dict[str, Any]] = []
    public = _tailscale_calendar_url(port)
    if public:
        calendars.append({"name": "公网地址", "kind": "public", "url": public + suffix, "hint": "手机用流量或任意 Wi-Fi 都能订阅"})
    for host in _lan_hosts():
        calendars.append({"name": f"局域网 {host}", "kind": "lan", "url": f"http://{host}:{port}/calendar.ics{suffix}", "hint": "手机与电脑在同一个网络时可用"})
    return calendars


def _job_snapshot(kind: str) -> dict[str, Any]:
    with _JOB_LOCK:
        return {key: _JOB_STATE[kind][key] for key in ("running", "stage", "done", "total", "note", "result")}


def _job_progress(kind: str, *values: Any) -> None:
    if len(values) == 4:
        stage, done, total, note = values
    elif len(values) == 2:
        done, total = values
        stage, note = "classifying", "正在判定已回溯消息"
    else:
        stage, done, total, note = "working", 0, 0, "正在处理"
    with _JOB_LOCK:
        state = _JOB_STATE[kind]
        if state["running"]:
            state.update(stage=str(stage or "working"), done=max(0, int(done or 0)), total=max(0, int(total or 0)), note=str(note or ""))


def _run_job(kind: str, worker: Any, cancel_event: threading.Event) -> None:
    try:
        result = worker(lambda *values: _job_progress(kind, *values), cancel_event.is_set)
        if not isinstance(result, dict):
            result = {"ok": False, "error": "后台任务返回格式无效"}
    except Exception as error:  # noqa: BLE001
        LOGGER.exception("后台%s任务失败", kind)
        result = {"ok": False, "error": str(error) or "后台任务失败"}
    with _JOB_LOCK:
        state = _JOB_STATE[kind]
        state.update(
            running=False,
            stage="complete" if result.get("ok") else "error",
            note=("处理完成" if result.get("ok") else str(result.get("error") or "处理失败")),
            result=result,
        )
        if _JOB_STATE["active"] == kind:
            _JOB_STATE["active"] = None
            _JOB_STATE["cancel"] = None


def _start_job(kind: str, worker: Any) -> tuple[bool, str]:
    with _JOB_LOCK:
        active = _JOB_STATE["active"]
        if active:
            return False, str(active)
        cancel_event = threading.Event()
        _JOB_STATE["active"] = kind
        _JOB_STATE["cancel"] = cancel_event
        _JOB_STATE[kind].update(running=True, stage="starting", done=0, total=0, note="正在启动", result=None)
        thread = threading.Thread(target=_run_job, args=(kind, worker, cancel_event), name="web-" + kind, daemon=True)
        try:
            thread.start()
        except Exception as error:  # noqa: BLE001
            _JOB_STATE["active"] = None
            _JOB_STATE["cancel"] = None
            _JOB_STATE[kind].update(running=False, stage="error", note="无法启动后台任务", result={"ok": False, "error": str(error)})
            return False, "无法启动后台任务"
        return True, ""


def _parse_groups(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 500:
        raise ValueError("请至少选择一个群，最多可选择 500 个")
    groups = []
    for item in value:
        group_id = str(item or "").strip()
        if not group_id or len(group_id) > 128:
            raise ValueError("群号无效")
        if group_id not in groups:
            groups.append(group_id)
    return groups


def _parse_day(value: Any, *, end: bool = False) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    day = dt.date.fromisoformat(text)
    if day.isoformat() != text:
        raise ValueError("日期格式应为 YYYY-MM-DD")
    return dt.datetime.combine(day, dt.time(23, 59, 59) if end else dt.time.min)


MANIFEST_JSON = json.dumps(
    {
        "name": "群消息待办",
        "short_name": "群待办",
        "description": "QQ 群通知摘要与个人待办台",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#0b0d12",
        "theme_color": "#0b0d12",
        "orientation": "portrait",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    },
    ensure_ascii=False,
)

ICON_SVG = """<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\">
  <defs>
    <linearGradient id=\"g\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">
      <stop offset=\"0\" stop-color=\"#12695b\"/>
      <stop offset=\"1\" stop-color=\"#173b34\"/>
    </linearGradient>
  </defs>
  <rect width=\"512\" height=\"512\" rx=\"112\" fill=\"#0f2a24\"/>
  <rect x=\"64\" y=\"64\" width=\"384\" height=\"384\" rx=\"96\" fill=\"url(#g)\"/>
  <path d=\"M168 264l58 58 122-142\" fill=\"none\" stroke=\"#fff\" stroke-width=\"36\" stroke-linecap=\"round\" stroke-linejoin=\"round\"/>
</svg>"""

SERVICE_WORKER_JS = """const CACHE = 'qq-digest-shell-v6';
const ASSETS = ['/manifest.webmanifest', '/icon.svg'];
self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  event.respondWith(fetch(event.request).then(response => {
    if (response.ok && (url.pathname === '/' || ASSETS.includes(url.pathname))) {
      const copy = response.clone();
      caches.open(CACHE).then(cache => cache.put(event.request, copy));
    }
    return response;
  }).catch(() => caches.match(event.request).then(cached => cached || caches.match('/'))));
});
"""
CALENDAR_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>月历 - 群消息待办</title>
<style>:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;padding:16px;background:#0a0c12;color:#f3f5fa;font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:680px;margin:auto}header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}h1{font-size:20px;margin:0}.nav{display:flex;gap:6px}button{border:1px solid #303747;background:#171c29;color:#f3f5fa;border-radius:6px;padding:7px 11px;font:inherit;cursor:pointer}.grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}.weekday{text-align:center;color:#9aa4b5;font-size:12px;padding:4px}.day{min-height:76px;padding:6px;background:#141925;border:1px solid #252c3b;border-radius:6px;overflow:hidden}.day.muted{opacity:.42}.day.today{border-color:#5b8cff}.num{font-size:12px;color:#b9c4d8}.event{display:block;margin-top:4px;padding:3px 4px;background:#26385e;color:#dbe7ff;border-radius:4px;font-size:11px;line-height:1.3;overflow-wrap:anywhere}@media(max-width:420px){body{padding:10px}.day{min-height:62px;padding:4px}.event{font-size:10px}.num{font-size:11px}}</style></head>
<body><main><header><h1 id="title">月历</h1><div class="nav"><button id="prev" type="button">上一月</button><button id="next" type="button">下一月</button></div></header><div id="calendar" class="grid"></div></main>
<script>(function(){var KEY='qq_digest_token';var params=new URLSearchParams(location.search);if(params.get('token'))localStorage.setItem(KEY,params.get('token'));var token=localStorage.getItem(KEY)||'';var cursor=new Date();cursor.setDate(1);var tasks=[];var names=['日','一','二','三','四','五','六'];function esc(s){return String(s||'').replace(/[&<>]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]});}function render(){var y=cursor.getFullYear(),m=cursor.getMonth(),root=document.getElementById('calendar');document.getElementById('title').textContent=y+'年'+(m+1)+'月';root.innerHTML='';names.forEach(function(n){var h=document.createElement('div');h.className='weekday';h.textContent=n;root.appendChild(h);});var first=new Date(y,m,1).getDay(),count=new Date(y,m+1,0).getDate(),prevCount=new Date(y,m,0).getDate();for(var i=0;i<42;i++){var d=i-first+1, date=new Date(y,m,d), cell=document.createElement('div');cell.className='day'+(date.getMonth()!==m?' muted':'');if(date.toDateString()===new Date().toDateString())cell.className+=' today';cell.innerHTML='<div class="num">'+date.getDate()+'</div>';tasks.forEach(function(t){if(!t.deadline)return;var raw=String(t.deadline), key=raw.slice(0,10);if(key===date.getFullYear()+'-'+String(date.getMonth()+1).padStart(2,'0')+'-'+String(date.getDate()).padStart(2,'0')){var e=document.createElement('div');e.className='event';e.textContent=t.summary||'未命名任务';cell.appendChild(e);}});root.appendChild(cell);}}document.getElementById('prev').onclick=function(){cursor.setMonth(cursor.getMonth()-1);render();};document.getElementById('next').onclick=function(){cursor.setMonth(cursor.getMonth()+1);render();};fetch('/api/tasks',{headers:{'X-Token':token}}).then(function(r){return r.json();}).then(function(data){tasks=[].concat(data.today||[],data.week||[],data.later||[],data.done||[],data.candidates||[]);render();}).catch(function(){render();});})();</script></body></html>"""
SETUP_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>接入向导</title><style>body{margin:0;padding:18px;background:#0a0c12;color:#f3f5fa;font:15px sans-serif}main{max-width:600px;margin:auto}.card{padding:16px;margin:12px 0;background:#171c29;border:1px solid #303747;border-radius:10px}img{display:block;width:240px;height:240px;object-fit:contain;margin:auto;background:#fff}button{padding:8px 12px;margin:4px;border-radius:7px;border:1px solid #475569;background:#26385e;color:white}label{display:block;padding:10px;border-bottom:1px solid #303747}#groups{margin-top:10px}#go{padding:12px 22px;font-size:16px;background:#2563eb;border:0}#steps div{padding:4px 0;font-size:13px;line-height:1.5}</style></head><body><main><h1>QQ 接入向导</h1><div class="card"><button id="go" type="button">一键接入</button><div id="steps"></div></div><div id="status" class="card">正在检查 QQ 登录状态…</div><div id="qrbox" class="card"><p>用 QQ 主号扫码</p><img id="qr" alt="登录二维码"></div><div id="groupbox" class="card" hidden><div><button id="all" type="button">全选</button><button id="none" type="button">全不选</button></div><div id="groups"></div><button id="save" type="button">保存订阅</button><p id="result"></p><a href="/calendar">打开月历</a></div></main><script>(function(){var selected={},names={},loaded=false;var token=new URLSearchParams(location.search).get('token')||localStorage.getItem('qq_digest_token')||'';function api(path,opt){opt=opt||{};opt.headers=Object.assign({'X-Token':token},opt.headers||{});return fetch(path,opt).then(function(r){return r.json().then(function(x){if(!r.ok)throw Error(x.error||'请求失败');return x;});});}function qrSrc(){return '/api/napcat/qrcode?token='+encodeURIComponent(token)+'&t='+Date.now();}document.getElementById('qr').src=qrSrc();function run(){var go=document.getElementById('go'),box=document.getElementById('steps');go.disabled=true;go.textContent='正在接入…';box.innerHTML='';api('/api/napcat/autosetup',{method:'POST'}).then(function(d){(d.steps||[]).forEach(function(s){var p=document.createElement('div');p.textContent=(s.ok?'✓ ':'✗ ')+s.name+'：'+s.detail;box.appendChild(p);});if(d.qrcode_path){document.getElementById('qr').src=qrSrc();}if(!d.ok){var e=document.createElement('div');e.textContent='未完成：'+(d.error||'');box.appendChild(e);}check();}).catch(function(e){box.textContent='接入失败：'+e.message;}).then(function(){go.disabled=false;go.textContent='一键接入';});}document.getElementById('go').addEventListener('click',run);var lastQrStamp=null;function check(){api('/api/napcat/status').then(function(s){var ok=s.ok&&s.online;document.getElementById('status').textContent=ok?'已登录：'+(s.nickname||'')+'（'+(s.user_id||'')+'）':'未登录，请扫码';document.getElementById('qrbox').hidden=ok;document.getElementById('groupbox').hidden=!ok;if(!ok){var st=String(s.qr_stamp||'');if(st!==lastQrStamp){lastQrStamp=st;document.getElementById('qr').src=qrSrc();}}if(ok&&!loaded)loadGroups();}).catch(function(e){document.getElementById('status').textContent='连接 NapCat 失败：'+e.message;});}function loadGroups(){api('/api/napcat/groups').then(function(d){var root=document.getElementById('groups');root.innerHTML='';d.groups.forEach(function(g){var id=String(g.group_id);names[id]=g.name||'';selected[id]=!!g.selected;var l=document.createElement('label');var c=document.createElement('input');c.type='checkbox';c.value=id;c.checked=!!selected[id];c.onchange=function(){selected[id]=c.checked;};l.appendChild(c);l.appendChild(document.createTextNode(' '+g.name+'（'+id+'）'));root.appendChild(l);});loaded=true;});}document.getElementById('all').onclick=function(){document.querySelectorAll('#groups input').forEach(function(c){c.checked=true;selected[c.value]=true;});};document.getElementById('none').onclick=function(){document.querySelectorAll('#groups input').forEach(function(c){c.checked=false;selected[c.value]=false;});};/* api('/api/subscriptions', ...).catch(function(e){ result.textContent='保存失败'; }) */
document.getElementById('save').onclick=function(){var groups=Object.keys(selected).filter(function(id){return selected[id];});api('/api/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({groups:groups,aliases:groups.reduce(function(a,id){a[id]=names[id]||'';return a;},{})})}).then(function(){document.getElementById('result').textContent='已保存并立即生效';}).catch(function(e){document.getElementById('result').textContent='保存失败：'+e.message;});};setInterval(function(){check();},5000);check();})();</script></body></html>"""
PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0d12">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20512%20512'%3E%3Ccircle%20cx='230.4'%20cy='281.6'%20r='204.8'%20fill='%23173b34'/%3E%3Ccircle%20cx='399.36'%20cy='107.52'%20r='107.52'%20fill='%23FFFFFF'/%3E%3Ccircle%20cx='399.36'%20cy='107.52'%20r='76.8'%20fill='%2312695b'/%3E%3Cpath%20d='M112.64%20307.2%20L184.32%20378.88%20L276.48%20235.52'%20fill='none'%20stroke='%23FFFFFF'%20stroke-width='66.56'%20stroke-linecap='round'%20stroke-linejoin='round'/%3E%3C/svg%3E" type="image/svg+xml">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>群消息待办</title>
<style>
:root{
  --bg:#0a0c12;--bg-top:#111728;--surface:rgba(255,255,255,.055);--surface-2:rgba(255,255,255,.08);
  --line:rgba(255,255,255,.09);--line-strong:rgba(255,255,255,.14);
  --text:#f3f5fa;--muted:#b6c0d0;--dim:#b6c0d0;
  --grad:linear-gradient(135deg,#31bda2,#63bf83);
  --urgent:#ff7078;--action:#f1bb64;--academic:#43c9b0;--info:#b6c0d0;
  --depth-content:8px;--depth-focus:16px;--motion-enter:cubic-bezier(0.2,0.8,0.2,1);--motion-exit:cubic-bezier(0.4,0,1,1);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{width:100%;max-width:100%;overflow-x:clip;background:var(--bg)}
body{width:100%;max-width:100%;margin:0;overflow-x:clip;background:linear-gradient(180deg,var(--bg-top),var(--bg) 340px);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding-bottom:calc(102px + env(safe-area-inset-bottom))}
header{width:100%;min-width:0;padding:calc(12px + env(safe-area-inset-top)) 16px 10px;position:sticky;top:0;background:linear-gradient(180deg,rgba(10,12,18,.98),rgba(10,12,18,.9));backdrop-filter:blur(16px);z-index:5;perspective:1100px;transform-style:preserve-3d}
@media(max-width:899px){header{backdrop-filter:none;-webkit-backdrop-filter:none}}
.hero{width:100%;min-width:0;max-width:100%;border-radius:8px;padding:15px 15px 13px;background:linear-gradient(120deg,rgba(67,201,176,.2),rgba(240,180,77,.13) 58%,rgba(255,255,255,.04));border:1px solid var(--line-strong);box-shadow:0 12px 20px rgba(0,0,0,.16);transform:translateZ(8px);animation:hero-enter 280ms var(--motion-enter) both}
@keyframes hero-enter{from{opacity:.75;transform:translate3d(0,10px,-8px) scale(.985)}to{opacity:1;transform:translate3d(0,0,8px) scale(1)}}
.workflow{display:flex;align-items:center;gap:10px;margin:12px 2px 2px;transform:translateZ(8px)}
.camera-enter{opacity:0;transform:perspective(1100px) translate3d(var(--camera-x,12px),0,-12px) scale(.98)}
.camera-moving{will-change:transform,opacity}
.camera-surface{transition:transform 240ms var(--motion-enter),opacity 240ms var(--motion-enter)}
.workflow button{display:flex;min-height:44px;align-items:baseline;gap:7px;padding:4px 2px;border:0;background:none;color:var(--muted);font:inherit;font-size:13px;white-space:nowrap;cursor:pointer}
.workflow button[aria-current=step]{color:var(--text);font-weight:700}
.flow-index{flex:0 0 auto;font-size:10px;font-variant-numeric:tabular-nums;color:var(--dim)}
.workflow button[aria-current=step] .flow-index{color:#43c9b0}
.flow-link{height:1px;flex:1;min-width:10px;background:linear-gradient(90deg,rgba(67,201,176,.6),rgba(255,255,255,.12))}
.hero-top{display:flex;min-width:0;align-items:flex-start;justify-content:space-between;gap:12px}
.hero h1{min-width:0;margin:0;font-size:24px;line-height:1.25;font-weight:700;letter-spacing:0}
.hero p{margin:6px 0 0;color:var(--muted);font-size:13px;line-height:1.45}
.progress-label{flex:0 0 auto;font-size:12px;font-weight:650;color:#dce5ff;background:rgba(255,255,255,.08);border:1px solid var(--line);border-radius:99px;padding:3px 8px}
.bar{height:4px;border-radius:99px;background:rgba(255,255,255,.1);margin-top:12px;overflow:hidden}
.bar>i{display:block;height:100%;width:100%;transform:scaleX(0);transform-origin:left;background:var(--grad);border-radius:99px;transition:transform 280ms var(--motion-enter)}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.stat{display:none;font-size:12px;color:#d4ddeb;background:rgba(255,255,255,.07);border:1px solid var(--line);border-radius:99px;padding:3px 8px}
.stat.show{display:inline-block}
main{width:100%;min-width:0;padding:4px 16px 30px;perspective:1100px;transform-style:preserve-3d}
.section{width:100%;min-width:0;max-width:100%;margin-top:18px}
.section-head{display:flex;min-width:0;align-items:center;justify-content:space-between;gap:10px;margin:0 0 8px;padding:0 2px}
.section-title{display:flex;align-items:center;gap:7px;margin:0;font-size:18px;line-height:1.35;font-weight:650;color:#dce3ee}
.section-title:before{content:"";width:6px;height:6px;border-radius:50%;background:var(--academic);box-shadow:0 0 0 3px rgba(91,140,255,.12)}
.section.overdue .section-title:before{background:var(--urgent);box-shadow:0 0 0 3px rgba(255,91,99,.12)}
.section.done .section-title:before{background:var(--info);box-shadow:none}
.count-pill{font-size:12px;color:var(--muted);background:rgba(255,255,255,.055);border:1px solid var(--line);border-radius:99px;padding:2px 7px}
ul{width:100%;min-width:0;list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.task{position:relative;display:flex;width:100%;min-width:0;max-width:100%;gap:10px;padding:12px 12px 12px 13px;background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow:hidden;box-shadow:0 8px 16px rgba(0,0,0,.12);transition:transform 180ms var(--motion-enter),opacity 180ms var(--motion-enter)}
.task.is-focused,.task:focus-within{z-index:2;transform:translateZ(var(--depth-focus)) scale(1.015);border-color:rgba(67,201,176,.65)}
.task[aria-busy=true]{opacity:.72}
.task:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--info)}
.task.urgent:before{background:var(--urgent)}
.task.action:before{background:var(--action)}
.task.academic:before{background:var(--academic)}
.task.overdue{background:linear-gradient(90deg,rgba(255,91,99,.13),rgba(255,255,255,.05) 42%);border-color:rgba(255,91,99,.26)}
.task.overdue:before{background:var(--urgent)}
.task.done{opacity:.48}
.task.done .t{text-decoration:line-through}




.candidate-mark{position:relative;flex:0 0 auto;display:grid;place-items:center;width:29px;height:29px;margin:0;border-radius:50%;border:2px solid rgba(180,158,255,.82);background-color:rgba(139,92,246,.18);background-image:radial-gradient(circle at 32% 24%,rgba(255,255,255,.18),transparent 48%);box-shadow:0 0 0 3px rgba(139,92,246,.08),inset 0 1px 0 rgba(255,255,255,.14);color:#e3dcff;font-size:12px;font-weight:750}
.section-head{cursor:default}
details.section>summary{cursor:pointer}


.body{min-width:0;max-width:100%;flex:1}
.card-top{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:6px}
.tag{font-size:12px;font-weight:700;letter-spacing:0;border-radius:99px;padding:2px 7px;border:1px solid var(--line);color:#cbd5e1;background:rgba(255,255,255,.055)}
.tag.urgent{color:#ffdadd;background:rgba(255,112,120,.13);border-color:rgba(255,112,120,.3)}
.tag.action{color:#ffe4b5;background:rgba(241,187,100,.12);border-color:rgba(241,187,100,.26)}
.tag.academic{color:#b5eee4;background:rgba(67,201,176,.13);border-color:rgba(67,201,176,.3)}
.tag.candidate{color:#ffe4b5;background:rgba(241,187,100,.14);border-color:rgba(241,187,100,.3)}
.tag.info{color:#d4ddeb;background:rgba(255,255,255,.06);border-color:var(--line-strong)}
.deadline-chip{display:inline-flex;align-items:center;font-size:12px;color:#ffe0ae;background:rgba(241,187,100,.08);border:1px solid rgba(241,187,100,.2);border-radius:99px;padding:2px 7px}
.deadline-chip.over{color:#ffdadd;background:rgba(255,112,120,.12);border-color:rgba(255,112,120,.24)}
.overdue-chip{font-size:12px;font-weight:700;color:#fff;background:var(--urgent);border-radius:99px;padding:2px 7px}
.snooze-chip{font-size:12px;font-weight:600;color:#d4e2ff;background:rgba(120,150,255,.12);border:1px solid rgba(120,150,255,.24);border-radius:99px;padding:2px 7px}
.duplicate-note{margin-top:7px;font-size:12px;color:var(--dim)}
.t{font-size:15px;font-weight:650;line-height:1.5;letter-spacing:0;overflow-wrap:anywhere;word-break:break-word}
.context{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.ctx{font-size:12px;color:#d0d9e6;background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:6px;padding:3px 6px;overflow-wrap:anywhere}
.meta{display:flex;min-width:0;flex-wrap:wrap;gap:8px;margin-top:8px;font-size:12px;color:var(--muted);overflow-wrap:anywhere}
.group-chip{background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:99px;padding:2px 7px}
.confidence{font-size:12px;color:#b8b0d8;margin-top:6px}
details{margin-top:7px}
summary{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--dim);cursor:pointer;list-style:none;user-select:none}
summary::-webkit-details-marker{display:none}
summary:after{content:"›";font-size:16px;line-height:1;transform:rotate(0);transition:transform .16s ease}
details[open] summary:after{transform:rotate(90deg)}
details p{margin:7px 0 0;font-size:12px;line-height:1.6;color:var(--muted);border-left:2px solid rgba(255,255,255,.1);padding-left:8px;white-space:pre-wrap;overflow-wrap:anywhere}
.detail-list{margin:7px 0 0 18px;padding:0;font-size:12px;line-height:1.55;color:var(--muted)}
.detail-list li{margin:4px 0;overflow-wrap:anywhere}
.actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}
.btn{min-height:44px;border:1px solid var(--line);background:rgba(255,255,255,.06);color:var(--text);font:inherit;font-size:12px;border-radius:7px;padding:7px 10px;cursor:pointer}
.btn.primary{border-color:transparent;background:var(--grad);color:#fff}
.btn.ghost{color:var(--muted)}
.correction{margin-top:10px;padding-top:9px;border-top:1px solid var(--line)}
.correction summary{color:#aebbd4;font-weight:650}
.correction-panel{padding-top:2px}
.correction-hint{margin-top:6px;font-size:11px;line-height:1.45;color:var(--dim)}
.correct-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:8px}
.correct-row{display:flex;min-width:0;align-items:center;gap:7px;margin-top:7px}
.correct-btn{min-width:0;min-height:44px;padding:7px 9px;border:1px solid var(--line);border-radius:9px;background:linear-gradient(180deg,rgba(255,255,255,.09),rgba(255,255,255,.045));color:#dbe3f2;font:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:transform 120ms var(--motion-enter),opacity 120ms var(--motion-enter)}
.correct-btn.primary{border-color:rgba(118,151,255,.36);background:linear-gradient(135deg,rgba(79,124,255,.3),rgba(139,92,246,.2));color:#fff}
.correct-btn.ghost{color:var(--muted);background:rgba(255,255,255,.035)}
.correct-btn:active{transform:scale(.97)}
.correct-select,.correct-date{flex:1;min-width:0;height:44px;padding:6px 8px;border:1px solid var(--line);border-radius:9px;background-color:rgba(255,255,255,.055);color:var(--text);font:inherit;font-size:12px;color-scheme:dark}
.correct-select{appearance:none;padding-right:22px;background-image:linear-gradient(45deg,transparent 50%,#8f9bb2 50%),linear-gradient(135deg,#8f9bb2 50%,transparent 50%);background-position:calc(100% - 13px) 14px,calc(100% - 9px) 14px;background-size:4px 4px,4px 4px;background-repeat:no-repeat}
.task:target{box-shadow:0 0 0 2px rgba(167,139,250,.5)}
.empty{color:var(--dim);font-size:13px;padding:10px 2px}
.tabs{position:fixed;left:50%;bottom:calc(9px + env(safe-area-inset-bottom));display:flex;gap:4px;width:calc(100% - 28px);max-width:440px;padding:6px;transform:translateX(-50%) translateZ(var(--depth-content));border:1px solid rgba(255,255,255,.16);border-radius:25px;background:linear-gradient(180deg,rgba(255,255,255,.14),rgba(255,255,255,.055)),rgba(14,18,29,.74);box-shadow:0 18px 50px rgba(0,0,0,.48),inset 0 1px 0 rgba(255,255,255,.2);backdrop-filter:blur(28px) saturate(180%);-webkit-backdrop-filter:blur(28px) saturate(180%);isolation:isolate;z-index:10}
.tabs:before{content:"";position:absolute;inset:0;border-radius:inherit;background:radial-gradient(circle at 18% -20%,rgba(255,255,255,.24),transparent 42%);pointer-events:none;z-index:0}
.tabs button{position:relative;z-index:2;flex:1;min-width:0;height:54px;padding:5px 2px;border:0;background:transparent;color:rgba(221,228,242,.6);font-family:inherit;font-size:10px;font-weight:600;cursor:pointer;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;transition:transform 120ms var(--motion-enter),opacity 120ms var(--motion-enter)}
.tabs button.active{color:#fff}
.tabs button:active{transform:scale(.98)}
.tabs svg{position:relative;z-index:2;width:21px;height:21px;stroke:currentColor;fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round;transition:stroke-width .18s ease,filter .18s ease}
.tabs button>span:last-child{position:relative;z-index:2;line-height:1.15}
.tabs button.active svg{stroke-width:2.25;filter:drop-shadow(0 0 8px rgba(135,165,255,.55))}
.tabs .dot{position:absolute;z-index:1;inset:5px 4px;border:1px solid transparent;border-radius:18px;background:transparent;transition:background .2s ease,border-color .2s ease,box-shadow .2s ease}
.tabs button.active .dot{border-color:rgba(255,255,255,.15);background:linear-gradient(135deg,rgba(67,201,176,.32),rgba(240,180,77,.24));box-shadow:inset 0 1px 0 rgba(255,255,255,.24),0 8px 22px rgba(67,201,176,.19)}
.notice{padding:12px;background:var(--surface);border:1px solid var(--line);border-radius:8px}
.notice h3{margin:0;font-size:14px;font-weight:650}
.notice p{margin:6px 0 0;font-size:12px;color:var(--muted);white-space:pre-wrap}
.kv{display:flex;justify-content:space-between;gap:12px;padding:11px 0;border-bottom:1px solid var(--line);font-size:13px}
.kv span:last-child{color:var(--muted);text-align:right}
.hosting-settings>.section-title{margin-bottom:14px}
.hosting-warning{margin:0 0 14px;padding:12px 14px;border-left:3px solid #f0b44d;background:rgba(240,180,77,.09);color:#f3dbad;font-size:13px;line-height:1.55}
.preference-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;margin:0;padding:0;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--line)}
.preference-list legend{padding:0 8px;color:var(--muted);font-size:12px}
.preference{display:flex;align-items:center;gap:12px;min-height:66px;margin:0;padding:12px 14px;background:#171b23;color:var(--text);cursor:pointer}
.preference input{width:18px;height:18px;flex:none;accent-color:#31bda2}.preference input[type=number]{width:112px;min-width:72px;height:44px;flex:0 0 112px;padding:8px 10px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink);font:inherit}
.preference span{display:grid;gap:3px;min-width:0}
.preference strong{font-size:13px;line-height:1.35;font-weight:650}
.preference small{font-size:12px;line-height:1.4;color:var(--muted)}
.setting-note{margin:12px 0;color:var(--muted);font-size:12px}
.hosting-status{margin:12px 0;padding:12px 14px;border:1px solid var(--line-strong);border-radius:7px;background:var(--surface);color:var(--text);font-size:13px;line-height:1.5;overflow-wrap:anywhere}
.hosting-actions{display:flex;gap:8px;flex-wrap:wrap}
.hosting-actions .primary{background:var(--grad)}
#hosting-result{min-height:1.5em;margin:8px 0;color:var(--muted);font-size:12px}
@media(max-width:599px){.preference-list{grid-template-columns:minmax(0,1fr)}.preference{min-height:58px}}
.insight-list{margin:6px 0 0;padding:0 0 0 16px;list-style:disc}
.insight-list li{margin:6px 0;font-size:12px;line-height:1.5;color:var(--muted)}
/* Desktop dashboard: keep the operational panels readable on wide screens. */
@media (min-width: 900px){
 body{font-size:16px;line-height:1.6;padding-bottom:24px}
 header{max-width:960px;margin:0 auto;padding-left:24px;padding-right:24px}
 header .hero{padding:22px 24px}
 header .hero h1{font-size:26px}
 main{max-width:960px;margin:0 auto;padding:24px;display:grid;grid-template-columns:minmax(220px,250px) minmax(0,1fr);gap:24px;align-items:start}
 #connect{grid-column:1;grid-row:2 / span 2;position:sticky;top:110px}
 #tab-tasks,#tab-notices,#tab-settings,#calendar-panel{grid-column:2}
 .t{font-size:16px;line-height:1.5}.section-title{font-size:18px;line-height:1.35}
 .dashboard-card{padding:20px;background:rgba(255,255,255,.06);border:1px solid var(--line-strong);border-radius:8px;margin-bottom:16px;transform:translateZ(var(--depth-content))}
  .tabs{position:relative;left:auto;bottom:auto;transform:none;margin:12px auto 0}
}
@media (max-width: 899px){.dashboard-card{margin:14px 0;padding:14px;background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:8px}}
.dashboard-card h2{margin:0 0 12px;font-size:19px}.dashboard-card{transform:translateZ(var(--depth-content));box-shadow:0 10px 18px rgba(0,0,0,.12)}.login-choice{margin:14px 0 0;padding:10px;border:1px solid var(--line);border-radius:7px;color:var(--muted)}.login-choice label{display:inline-flex;min-height:44px;align-items:center;margin-right:12px;padding:4px 0;border:0}.login-choice input[type=text],.login-choice input:not([type]){max-width:180px;padding:7px;border:1px solid var(--line);border-radius:5px;background:rgba(255,255,255,.06);color:var(--text)}.login-note{font-size:12px;line-height:1.55;color:var(--muted);margin:12px 0 4px}.login-note strong{color:#e5edf7}.login-more{margin:4px 0 10px;padding:7px 9px;border-left:2px solid #43c9b0;background:rgba(67,201,176,.06)}.login-more summary{color:#a8dcd1;font-size:12px}.login-more summary:after{margin-left:auto}.login-more p{margin:7px 0 0;font-size:12px;line-height:1.6;color:var(--muted)}.connect-actions{display:flex;gap:8px;flex-wrap:wrap}.connect-actions button{min-height:44px;font:inherit;color:var(--text);background:#2457c6;border:1px solid #6f98ff;border-radius:7px;padding:9px 14px;cursor:pointer}.connect-actions button.secondary{background:rgba(255,255,255,.07);border-color:var(--line)}#qrbox{margin-top:14px}#qrbox img{display:block;width:min(100%,230px);aspect-ratio:1;object-fit:contain;background:#fff}#groups{display:grid;gap:6px;margin-top:12px}#groups label{padding:8px;border-bottom:1px solid var(--line)}#calendar{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:5px}
#calendar-panel{scroll-margin-top:110px}
.action-feedback{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 4px;padding:12px 14px;border:1px solid var(--line-strong);border-left:3px solid #43c9b0;border-radius:7px;background:rgba(67,201,176,.08);font-size:13px;line-height:1.45;color:var(--text)}
.action-feedback[data-state=loading]{border-left-color:#f1bb64;background:rgba(241,187,100,.08)}
.action-feedback[data-state=error]{border-left-color:#ff7078;background:rgba(255,112,120,.08)}
.action-feedback[hidden]{display:none}
.action-feedback button{min-height:44px;flex:none;padding:7px 11px;border:1px solid var(--line-strong);border-radius:6px;background:rgba(255,255,255,.08);color:var(--text);font:inherit;cursor:pointer}
:focus-visible{outline:2px solid #12695b;outline-offset:2px}
button:disabled{cursor:not-allowed;opacity:.55}
button:active{transform:scale(.98);transition:transform 120ms var(--motion-enter)}
.task .btn:active,.task .correct-btn:active,.task .check:active{transform:scale(.98)}
@media(prefers-reduced-motion:reduce){
 :root{--depth-content:0px;--depth-focus:0px;--motion-enter:linear;--motion-exit:linear;scroll-behavior:auto}
 .hero{animation:none!important;transform:translateZ(0)}
 .camera-enter,.task.is-focused,.task:focus-within{transform:none!important;opacity:1!important}
 button:active{transform:none!important}
 .camera-surface,.task,.check,.correct-btn,.tabs button,.bar>i{transition-duration:0ms!important}
 .camera-moving{will-change:auto!important}
}
</style>
<style>
/* 配色阶梯（洁净度）：页面底 #e8ebee 是冷灰蓝、卡片保持纯白，明度差约 12 才分得开；
   三种浅面（页面底/次级面/teal-soft）必须各自差一档，否则会挤成一坨「脏」；
   --line 对白底对比度 3.12:1，达到 WCAG 图形 3:1，边界看得见。 */
:root{--page:#e8ebee;--paper:#fff;--paper-alt:#f2f4f6;--ink:#172722;--muted:#3d4f47;--line:#b0b8bd;--teal:#12695b;--teal-soft:#d6ede7;--red:#a5312d;--red-soft:#ffeeed;--amber:#805411;--amber-soft:#fff3db;--depth-mid:0px;--depth-top:16px;--ease-in:cubic-bezier(.2,.8,.2,1);--ease-out:cubic-bezier(.4,0,1,1)}
html{background:var(--page);color-scheme:light;overflow-x:hidden}
/* body 必须保持透明：底色由上面的 html 提供。body 一旦自己刷底色，被压在 z-index:-1 的流动层
   就会被 body 的不透明背景盖住（画序：根背景 → 负 z 层 → 流内块背景），页面看起来完全静止。 */
body{max-width:100%;padding:0 0 40px;background:transparent;color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
/* 背景流动感：两层「绸缎」柔光（长椭圆），用 transform 沿对角线缓慢漂移并带 ±6° 旋转。
   压在所有内容下方（z-index:-1），卡片与文字始终落在干净的底色上，清晰度不受影响。
   配色只允许用本页自己的两个强调色（teal 18,105,91 / amber 180,140,60）+ 底色 232,235,238。
   上一版混进了蓝 58,120,170 与紫 120,90,180：四个色相互相打架，加上 alpha 高到 .42/.34，
   实测顶部空白带与底色的色差 ΔE 7.1、次级文字对底色的对比度掉到 4.02:1（低于 WCAG 4.5:1）
   —— 这就是「颜色很脏、整体不配合、不方便阅读」。现在只用同色系且 alpha ≤ .24：
   同样位置 ΔE 从 7.1 降到 1 左右，次级文字对底色最坏（两层柔光叠在峰值）≈ 4.7:1，仍达 WCAG AA。
   测试里有这两条守卫，别再放第三、第四个色相，也别把 alpha 抬过 .25。
   radial-gradient 而非斜向 repeating-linear-gradient：后者的斜纹在平铺接缝处会露出竖向
   色阶（实测截图上一条条硬边），柔光没有接缝。高 alpha 会脏，所以可见性主要靠「位移 + 半径」：
   同一组 alpha 下把位移从 ±6% 提到 ±11%，20 秒逐像素差 mean 0.97 -> 1.45、顶部空白带色差
   >=8 的像素占比 36% -> 54%（实测，脚本 实测脚本），再往上抬 alpha 就不值了。 */
body:before,body:after{content:"";position:fixed;inset:-30%;z-index:-1;pointer-events:none;will-change:transform}
body:before{background:radial-gradient(44% 22% at 20% 14%,rgba(18,105,91,.24),rgba(18,105,91,0) 72%),radial-gradient(40% 20% at 48% 82%,rgba(18,105,91,.16),rgba(18,105,91,0) 74%);animation:bg-silk-a 52s cubic-bezier(.37,0,.63,1) infinite}
body:after{background:radial-gradient(38% 18% at 80% 36%,rgba(180,140,60,.16),rgba(180,140,60,0) 74%),radial-gradient(34% 16% at 72% 86%,rgba(180,140,60,.11),rgba(180,140,60,0) 76%);animation:bg-silk-b 68s cubic-bezier(.37,0,.63,1) infinite}
/* 绸缎的运动：长椭圆沿对角线来回走一趟并带旋转，52s / 68s 两个周期互质，所以两层不会同步撞在一起 */
@keyframes bg-silk-a{0%{transform:translate3d(-11%,-7%,0) rotate(-8deg)}50%{transform:translate3d(11%,7%,0) rotate(8deg)}100%{transform:translate3d(-11%,-7%,0) rotate(-8deg)}}
@keyframes bg-silk-b{0%{transform:translate3d(9%,10%,0) rotate(7deg)}50%{transform:translate3d(-9%,-10%,0) rotate(-7deg)}100%{transform:translate3d(9%,10%,0) rotate(7deg)}}
button,input,select{font:inherit}
.masthead,main{width:min(100% - 32px,960px);margin-inline:auto}
.masthead{position:static;top:auto;z-index:auto;padding:20px 0 0;background:transparent;backdrop-filter:none;perspective:1100px;transform-style:preserve-3d}
.masthead-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 0 16px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;min-height:44px}
.brand-mark{display:block;flex:none;width:36px;height:36px}
.brand strong,.brand small{display:block}.brand strong{font-size:16px;line-height:1.25}.brand small{margin-top:2px;color:var(--muted);font-size:12px}
.local-badge{display:flex;align-items:center;gap:8px;min-height:36px;padding:0 12px;border:1px solid var(--line);border-radius:20px;color:#26483d;background:var(--paper);font-size:12px;font-weight:650}
.local-badge i{width:8px;height:8px;border-radius:50%;background:#26805e}
.masthead-tools{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
.host-state{display:flex;align-items:center;gap:8px;min-height:36px;padding:0 12px;border:1px solid var(--line);border-radius:20px;background:var(--paper);color:#26483d;font:inherit;font-size:12px;font-weight:650;cursor:pointer}
.host-state i{width:8px;height:8px;border-radius:50%;background:#9aa8a1}
.host-state[data-state=active] i{background:#26805e;animation:badge-breathe 4s ease-in-out 600ms infinite}
.host-state[data-state=idle] i{background:#c2792a}
.host-state[data-state=offline] i{background:#b4564f}
.host-quick{min-height:36px}
.journey{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:16px;margin-top:16px;padding:16px;border:1px solid #b8cbc1;border-radius:8px;background:linear-gradient(115deg,#eef6f1 0%,#f7f8f4 58%,#f8eee5 100%);box-shadow:0 9px 17px rgba(20,49,39,.1),0 2px 5px rgba(20,49,39,.08)}
.journey[hidden]{display:none}
.journey h2{margin:4px 0 0;font-size:18px;line-height:1.35}
.journey-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:8px;margin:12px 0 0;padding:0;list-style:none}
.journey-step{display:flex;gap:8px;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--paper);color:#33483f;font-size:13px;line-height:1.45}
.journey-step b{flex:none;display:grid;place-items:center;width:20px;height:20px;border-radius:50%;background:#dfe8e2;color:#41604f;font-size:12px}
.journey-step span{min-width:0;overflow-wrap:anywhere}
.journey-step small{display:block;margin-top:2px;color:var(--muted);font-size:12px}
.journey-step[data-state=done]{border-color:#a9cbb9;background:#f2f8f4}
.journey-step[data-state=done] b{background:#26805e;color:#fff}
.journey-step[data-state=current]{border-color:#12695b;background:#eaf5f0;box-shadow:inset 0 0 0 1px rgba(18,105,91,.22)}
.journey-step[data-state=current] b{background:#12695b;color:#fff}
.journey-side{display:flex;flex-direction:column;justify-content:center;min-width:0}
.journey-next{margin:0;color:#34483f;font-size:14px;line-height:1.55}
.journey-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
.journey-target{box-shadow:0 0 0 3px rgba(18,105,91,.45),0 9px 17px rgba(20,49,39,.1)}
.summary{position:relative;margin-top:16px;padding:16px;border:1px solid #b8cbc1;border-radius:8px;background:linear-gradient(118deg,#d9ede6 0%,#e8f1ec 28%,#f7f8f4 58%,#f8ede3 86%,#f4e5d8 100%);box-shadow:0 12px 22px rgba(27,55,44,.12),0 3px 8px rgba(27,55,44,.08);transform:none;animation:summary-enter 280ms var(--ease-in) both}
@keyframes summary-enter{from{opacity:.7;transform:translate3d(0,10px,-8px) scale(.985)}to{opacity:1;transform:none}}
.summary-top,.summary-bottom{display:flex;align-items:center;justify-content:space-between;gap:12px}.eyebrow{display:block;color:#536a5f;font-size:12px;line-height:1.45;font-weight:700;text-transform:uppercase}
.summary h1{margin:4px 0 0;font-size:32px;line-height:1.2;font-weight:760;letter-spacing:-.01em;overflow-wrap:anywhere}.summary p{margin:4px 0 0;color:#34483f;font-size:15px;line-height:1.5}
.progress-label{flex:none;padding:4px 9px;border:1px solid #bdcec5;border-radius:20px;background:rgba(255,255,255,.72);color:#284a3e;font-size:12px;font-weight:700}
.summary-bottom{margin-top:12px;align-items:flex-end}.stats{display:flex;flex-wrap:wrap;gap:8px}.stat{display:none;padding:4px 9px;border:1px solid #ccd8d1;border-radius:18px;background:#fff;color:#33483f;font-size:12px}.stat.show{display:inline-flex}
.bar{width:min(240px,34%);height:6px;margin:0;overflow:hidden;border-radius:6px;background:#d0dbd5}.bar>i{display:block;height:100%;width:100%;transform:scaleX(0);transform-origin:left;background:var(--teal);transition:transform 220ms var(--ease-in)}
.tabs{position:static;display:flex;gap:8px;width:100%;max-width:none;margin:16px 0 0;padding:0;transform:none;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;box-shadow:none;backdrop-filter:none;z-index:auto}
.tabs:before,.tabs .dot{display:none}.tabs button{display:flex;flex:0 0 auto;align-items:center;justify-content:center;flex-direction:row;gap:8px;min-width:112px;min-height:48px;height:48px;padding:0 16px;border:0;border-bottom:3px solid transparent;border-radius:0;background:transparent;color:#42564e;font-size:14px;font-weight:650;transition:transform 120ms var(--ease-in),opacity 120ms var(--ease-in)}
.tabs button.active{border-bottom-color:var(--teal);color:#173e34}.tabs svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;filter:none;transition:none}.tabs button.active svg{stroke-width:2}
main{display:block;padding:16px 0 0;perspective:1100px;transform-style:preserve-3d}.workspace{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;align-items:start}#tab-tasks{display:grid;grid-template-columns:minmax(0,1fr);gap:0;align-items:start}.primary-view{min-width:0}.view-screen{min-width:0;transition:transform 220ms var(--ease-in),opacity 220ms var(--ease-in)}.camera-enter{opacity:0;transform:perspective(1100px) translate3d(var(--camera-x,12px),0,-12px) scale(.98)}.camera-moving{will-change:transform,opacity}
.surface{min-width:0;padding:16px;background:var(--paper);border:1px solid var(--line);border-radius:8px;box-shadow:0 9px 17px rgba(20,49,39,.1),0 2px 5px rgba(20,49,39,.08);transform:translateZ(var(--depth-mid));transform-style:preserve-3d}.side-rail{display:grid;grid-template-columns:minmax(0,1fr);gap:16px;min-width:0}#calendar-panel{grid-column:auto;grid-row:auto;scroll-margin-top:24px}.panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.panel-heading h2{margin:2px 0 0;font-size:19px;line-height:1.35}.panel-index{color:#536a5f;font-size:12px;font-variant-numeric:tabular-nums}
.section{width:100%;max-width:100%;margin:0 0 24px}.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 8px;padding:0 2px}.section-title{margin:0;color:var(--ink);font-size:17px;line-height:1.35;font-weight:680}.section-title:before{content:none}.count-pill{padding:3px 8px;border:1px solid var(--line);border-radius:16px;background:#fff;color:#3f534a;font-size:12px}.section ul{display:grid;gap:8px;margin:0;padding:0;list-style:none}.task{position:relative;display:flex;gap:12px;width:100%;min-width:0;padding:16px;background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 3px 8px rgba(22,48,38,.06);transition:transform 180ms var(--ease-in),opacity 180ms var(--ease-in)}
.task.is-focused,.task:focus-within{z-index:2;transform:translateZ(var(--depth-top)) scale(1.012);border-color:#4b8d78}.task[aria-busy=true]{opacity:.72}.task:before{content:"";position:absolute;inset:0 auto 0 0;width:4px;background:#667c71}.task.urgent:before,.task.overdue:before{background:var(--red)}.task.action:before{background:#ac771e}.task.academic:before{background:#16816d}.task.overdue{background:var(--red-soft);border-color:#d7a7a0}.task.done{opacity:1;background:#f5f7f5}.task.done .t{text-decoration:line-through;color:#42554e}
.check{position:relative;display:grid;place-items:center;flex:0 0 48px;width:48px;min-width:48px;height:48px;min-height:48px;margin:0;padding:0;border:2px solid #5f796d;border-radius:10px;background:#fff;color:var(--teal);cursor:pointer;transition:transform 80ms var(--ease-in),background-color 120ms ease-out,border-color 120ms ease-out,box-shadow 120ms ease-out,color 120ms ease-out}.check:after{content:"";position:absolute;left:18px;top:15px;width:8px;height:13px;border:2px solid transparent;border-top:0;border-left:0;transform:rotate(42deg);transform-origin:center}.task.done .check{border-color:var(--teal);background:var(--teal)}.task.done .check:after{border-color:white}.candidate-mark{display:grid;place-items:center;flex:0 0 32px;height:32px;border:2px solid var(--amber);border-radius:50%;color:var(--amber);font-weight:700}
.body{flex:1;min-width:0}.card-top{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:8px}.tag,.deadline-chip,.overdue-chip,.snooze-chip{display:inline-flex;align-items:center;min-height:24px;padding:2px 8px;border:1px solid var(--line);border-radius:14px;background:#f5f7f5;color:#344b40;font-size:12px;font-weight:650}.tag.urgent,.overdue-chip{border-color:#cb8c83;background:var(--red-soft);color:#802b27}.tag.action,.tag.candidate,.deadline-chip,.deadline-chip.over{border-color:#d5bb87;background:var(--amber-soft);color:#67480f}.tag.academic{border-color:#94bfb1;background:var(--teal-soft);color:#20584a}.tag.info,.snooze-chip{background:#f0f3f1;color:#344b40}.overdue-chip{background:#a5312d;color:#fff}.t{font-size:16px;line-height:1.5;font-weight:680;overflow-wrap:anywhere;word-break:break-word}.context,.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;color:#42554e;font-size:13px}.ctx,.group-chip{padding:4px 8px;border:1px solid #d4ddd8;border-radius:5px;background:#f6f8f6;color:#384d43;font-size:12px;overflow-wrap:anywhere}.duplicate-note,.confidence{margin-top:8px;color:#43574e;font-size:13px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.btn,.correct-btn,.connect-actions button,.action-feedback button{min-height:44px;padding:8px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:#1c382e;font-size:13px;font-weight:650;cursor:pointer}.btn.primary,.correct-btn.primary,.connect-actions .primary,#groupbox .primary{border-color:#145f52;background:#145f52;color:#fff}.btn.ghost,.correct-btn.ghost{background:#f4f7f5;color:#344b40}.btn:active,.correct-btn:active,.check:active,.tabs button:active{transform:scale(.98);transition-duration:100ms}.actions .btn{font-size:13px}details{margin-top:8px}summary{display:flex;align-items:center;min-height:44px;color:#345348;font-size:13px;font-weight:600;cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}summary:after{content:"+";margin-left:7px;font-size:16px}details[open]>summary:after{content:"−"}details p{margin:6px 0 0;color:#42554e;font-size:13px;line-height:1.55;overflow-wrap:anywhere}.detail-list{padding-left:20px;color:#344b40;font-size:13px}.detail-list li{margin:4px 0}.correction{padding-top:8px;border-top:1px solid #d7dfda}.correction-hint,.correction-hint~*{color:#42554e}.correction summary{color:#345348}.correct-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}.correct-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.correct-btn{font-size:12px}.correct-select,.correct-date{min-width:0;min-height:44px;padding:8px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink);font-size:13px;color-scheme:light}.empty{padding:20px 12px;border:1px dashed #aab9b0;border-radius:6px;color:#42554e;font-size:14px}.notice{padding:14px;background:#fff;border:1px solid var(--line);border-radius:7px}.notice h3{margin:0;font-size:16px}.notice p{margin:8px 0 0;color:#42554e;font-size:14px;white-space:pre-wrap}
#connect{position:static;top:auto;grid-column:auto;grid-row:auto;transform:translateZ(var(--depth-mid))}#status{padding:12px;border-left:3px solid #9a6b1c;background:#fff6e5;color:#574111;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.connect-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.login-choice{display:grid;gap:4px;margin:12px 0 0;padding:8px 10px;border:1px solid var(--line);border-radius:6px;color:#344b40}.login-choice legend{padding:0 5px;color:#53685e;font-size:12px}.login-choice label{display:flex;align-items:center;gap:8px;min-height:44px;font-size:13px}.login-choice input[type=radio]{width:18px;height:18px;accent-color:var(--teal)}#uin{width:100%;min-height:44px;padding:8px 10px;border:1px solid #9eafa6;border-radius:5px;background:#fff;color:var(--ink)}.login-note{margin:8px 0 0;color:#42554e;font-size:12px;line-height:1.5}.login-note strong{color:#244d3f}.login-more{margin-top:4px}.login-more summary{min-height:44px;color:#20584a}.login-more p{font-size:12px}#steps,#install-result,#result{margin-top:8px;color:#42554e;font-size:13px;overflow-wrap:anywhere}#qrbox{margin-top:12px;padding:12px;border:1px dashed #b2c0b8;border-radius:6px;background:#f7f9f7}#qrbox p{margin:0;color:#42554e;font-size:13px}#qrbox img{display:block;width:min(100%,200px);height:auto;aspect-ratio:1;object-fit:contain;margin:12px auto 0;background:#fff}#groups{display:grid;gap:4px;margin:12px 0}#groups label{display:flex;align-items:center;min-height:44px;gap:8px;border-bottom:1px solid #e0e6e2;font-size:13px}#groups input{width:18px;height:18px;accent-color:var(--teal)}
.month-controls{display:flex;gap:8px}.month-controls .btn{width:44px;padding:0;font-size:21px}/* 手机端专属的月历折叠按钮：桌面上不显示（桌面有足够宽度直接铺月历） */
.month-controls .calendar-toggle{display:none}.weekday-row{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;margin:0 0 4px;text-align:center;color:#4a6055;font-size:12px;line-height:16px;font-weight:650}#calendar{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;transition:transform 220ms var(--ease-in),opacity 220ms var(--ease-in)}.calendar-day{min-width:0;min-height:58px;line-height:16px;padding:4px;border:1px solid #d4ddd8;border-radius:5px;background:#f6f8f6;color:#263b32;cursor:default}.calendar-day[data-has-events=true]{cursor:pointer}.calendar-day[data-has-events=true]:hover{border-color:#8fb3a5;background:#eef5f1}.calendar-day.is-open{border-color:#12695b;background:#f0fbf8}.calendar-day.today{border-color:#12695b;border-width:2px;background:#eefbf7}.calendar-day.today strong{color:#12695b}.calendar-day strong{font-size:13px;line-height:16px;font-variant-numeric:tabular-nums}.calendar-event{display:-webkit-box;margin-top:3px;padding:3px 4px;border-radius:3px;background:#e5f3ec;color:#1f4738;font-size:11px;line-height:14px;overflow:hidden;overflow-wrap:anywhere;-webkit-line-clamp:1;-webkit-box-orient:vertical}.calendar-more{display:block;margin-top:3px;padding:2px 4px;color:#42554e;font-size:10px;line-height:14px;text-align:right}.calendar-detail{margin-top:10px;padding:10px 12px;border:1px solid #b8cbc1;border-radius:6px;background:#f7faf8;color:#263b32;font-size:13px;line-height:20px;overflow:hidden}.calendar-detail[hidden]{display:none}.calendar-detail h3{margin:0;font-size:14px;line-height:20px}.calendar-detail ul{display:grid;gap:4px;margin:0;padding:0;list-style:none}.calendar-detail li{padding-left:12px;text-indent:-12px;overflow-wrap:anywhere}.calendar-detail-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 6px}.calendar-detail-close{flex:none;min-height:32px;padding:0 10px;border:1px solid #b8cbc1;border-radius:5px;background:#fff;color:#1f4738;font:inherit;font-size:12px;cursor:pointer;transition:border-color 120ms var(--ease-in),background-color 120ms var(--ease-in)}.calendar-detail-close:hover{border-color:#8fb3a5;background:#eef5f1}.calendar-detail time{margin-right:6px;color:#536a5f;font-size:12px;font-variant-numeric:tabular-nums}.calendar-note{margin:8px 0 0;color:#4b5f55;font-size:12px;line-height:16px}@keyframes calendar-turn{from{opacity:0;transform:translateX(var(--turn-x,18px))}to{opacity:1;transform:translateX(0)}}#calendar.calendar-turn{animation:calendar-turn 240ms var(--ease-out) both}.connect-hint{margin:8px 0 0;color:#42554e;font-size:12px;line-height:1.6}.action-feedback{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 16px;padding:12px;border:1px solid #99b9aa;border-left:4px solid var(--teal);border-radius:6px;background:#e5f3ec;color:#1f4738;font-size:15px}.action-feedback[data-state=loading]{border-left-color:#a16d1d;background:#fff3da;color:#60440f}.action-feedback[data-state=error]{border-color:#d3a09a;border-left-color:var(--red);background:#fff0ed;color:#702a26}.action-feedback[hidden]{display:none}.action-feedback button{flex:none}
:focus-visible{outline:2px solid #12695b;outline-offset:2px}button:disabled{cursor:not-allowed;opacity:.6}[hidden]{display:none!important}
 .group-tools{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.group-tools .btn{flex:1 1 140px}.group-search{width:100%;min-height:44px;padding:9px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink)}.group-source{margin:8px 0;color:#344b40;font-size:13px}.group-source strong{display:inline-block;padding:3px 8px;border:1px solid #94bfb1;border-radius:14px;background:var(--teal-soft);color:#20584a;font-size:12px}.group-source[data-source=heuristic] strong{border-color:#d5bb87;background:var(--amber-soft);color:#67480f}.group-category{margin:10px 0}.group-category h3{margin:0 0 6px;color:#344b40;font-size:14px}.group-list{display:grid;gap:4px}#groups .group-row{display:flex;align-items:center;min-width:0;min-height:44px;gap:8px;padding:8px;border:1px solid #d4ddd8;border-radius:6px;background:#fff;color:#263b32;font-size:13px;cursor:pointer}#groups .group-row:focus-within{outline:2px solid #12695b;outline-offset:2px}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.group-row input{flex:0 0 18px;width:18px;height:18px;accent-color:var(--teal)}.group-name{min-width:0;overflow-wrap:anywhere}.group-meta{display:flex;flex:1 1 auto;flex-wrap:wrap;align-items:center;gap:6px;min-width:0}.category-badge,.suggest-badge{display:inline-flex;align-items:center;min-height:24px;padding:2px 7px;border:1px solid #c9d4ce;border-radius:12px;background:#f5f8f6;color:#344b40;font-size:12px}.suggest-badge{border-color:#94bfb1;background:var(--teal-soft);color:#20584a}.suggest-reason{flex-basis:100%;color:#42554e;font-size:12px;line-height:1.4;overflow-wrap:anywhere}.group-source-error,#groups-message{margin:8px 0;color:#42554e;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.group-empty-link{display:inline-flex;align-items:center;min-height:44px;margin:4px 0;padding:8px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:#1c382e;font-size:13px;font-weight:650;text-decoration:none}.other-groups{margin-top:12px;padding-top:8px;border-top:1px solid var(--line)}.other-groups>summary{font-weight:700}.group-category[hidden],.group-row[hidden]{display:none!important}.calendar-day{min-height:56px;padding:5px}
@media(min-width:900px){.masthead,main{width:min(100% - 48px,960px)}.masthead{padding-top:24px}.summary{margin-top:24px;padding:24px 32px}.summary h1{font-size:34px}.tabs{margin-top:24px}.workspace{grid-template-columns:minmax(0,1fr);gap:24px}main{padding-top:24px}.surface{padding:16px}}
@media(min-width:900px) and (max-width:1199px){.workspace{grid-template-columns:minmax(0,1fr)}}
@media(min-width:1024px){.masthead,main{width:min(calc(100% - 64px),1760px);max-width:1760px}.workspace{grid-template-columns:minmax(0,1fr) minmax(320px,560px);gap:24px}.section{margin-bottom:12px}.section-head{margin-bottom:4px}.task{padding:12px}.card-top{margin-bottom:4px}.context,.meta{margin-top:4px}.side-rail{grid-column:2;grid-row:1;grid-template-columns:minmax(0,1fr);align-items:start}#calendar-panel{position:static;width:auto;min-width:0;grid-column:auto;grid-row:auto}}
@media(max-width:899px){.workspace{grid-template-columns:minmax(0,1fr);gap:24px}.side-rail{grid-template-columns:minmax(0,1fr);gap:16px}.summary{margin-top:16px}.masthead{padding-top:12px}.tabs{margin-top:12px}}
@media(max-width:480px){.masthead,main{width:calc(100% - 32px)}.summary{padding:16px}.summary h1{font-size:28px}.summary-bottom{align-items:flex-start;flex-direction:column}.bar{width:100%}.tabs button{flex:1;min-width:0;padding-inline:8px}.workspace{gap:16px}.surface{padding:12px}.task{gap:8px;padding:12px 8px}.task .t{font-size:15px}.connect-actions>*{flex:1}.calendar-day{min-height:46px;padding:3px}.calendar-day strong{font-size:12px}.calendar-event{margin-top:2px;padding:2px 3px;font-size:10px}.calendar-more{font-size:9px}.month-controls .calendar-toggle{display:inline-flex;width:auto;min-width:0;padding:0 12px;font-size:13px}#calendar-panel.calendar-collapsed .weekday-row,#calendar-panel.calendar-collapsed #calendar,#calendar-panel.calendar-collapsed .calendar-note,#calendar-panel.calendar-collapsed #calendar-detail{display:none}.section{margin-bottom:16px}}
.history-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}.history-controls{display:flex;flex-wrap:wrap;gap:8px;align-items:end}.history-controls label,.inbox-filter label{display:grid;gap:4px;min-width:0;color:#344b40;font-size:13px;font-weight:650}.history-controls input,.inbox-filter input,.inbox-filter select{min-height:44px;min-width:0;padding:8px 10px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink);font:inherit}.history-note{margin:8px 0;color:#344b40;font-size:14px;line-height:1.5}.history-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:4px;max-height:180px;overflow:auto;padding:4px;border:1px solid var(--line);border-radius:6px}.history-groups label{display:flex;align-items:center;gap:8px;min-width:0;min-height:44px;padding:6px 8px;border:1px solid #d4ddd8;border-radius:5px;background:#fff;overflow-wrap:anywhere}.history-groups input{width:18px;height:18px;flex:0 0 18px;accent-color:var(--teal)}.history-actions,.inbox-filter,.inbox-filter-tools,.inbox-job-actions{display:flex;flex-wrap:wrap;align-items:end;gap:8px}.inbox-filter{margin:12px 0;padding:12px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.inbox-filter label{flex:1 1 150px}.inbox-filter label.search{flex:2 1 240px}.history-status,.inbox-empty,.inbox-error{margin:8px 0;color:#344b40;line-height:1.5}.history-job{margin-top:12px;padding:12px;border-left:3px solid var(--teal);background:#eff6f2}.history-job progress{display:block;width:100%;height:12px;margin:8px 0;accent-color:var(--teal)}.inbox-counts{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.inbox-counts button{min-height:44px;padding:8px 12px;border:1px solid #aabbb2;border-radius:6px;background:#fff;color:#233b31;font:inherit}.inbox-counts button[aria-pressed=true]{border-color:var(--teal);background:var(--teal-soft);font-weight:700}.inbox-items{display:grid;gap:8px;margin:0;padding:0;list-style:none}.inbox-item{padding:12px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}.inbox-item h3{margin:0;font-size:16px;line-height:1.45}.inbox-meta{display:flex;flex-wrap:wrap;gap:6px 12px;margin:4px 0;color:#42554e;font-size:13px}.inbox-verdict{display:inline-flex;align-items:center;min-height:24px;padding:2px 8px;border:1px solid #9eafa6;border-radius:14px;background:#fff;color:#233b31;font-size:12px;font-weight:700}.inbox-reason{margin:6px 0;color:#344b40;font-size:14px;line-height:1.5}.inbox-content{margin:4px 0;color:var(--ink);font-size:15px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere}.inbox-content summary{min-height:44px;cursor:pointer;color:#20584a;font-weight:650}.inbox-item .btn{margin-top:4px}.inbox-pagination{display:flex;justify-content:center;margin-top:12px}.inbox-pagination .btn{min-width:140px}
@media(max-width:700px){.history-grid{grid-template-columns:minmax(0,1fr)}.history-actions>*{flex:1 1 140px}.inbox-filter-tools>*{flex:1 1 120px}.inbox-item{padding:12px 0}}
.summary>*{position:relative;z-index:1}.summary::after{content:"";position:absolute;inset:-14px;z-index:-1;border-radius:20px;pointer-events:none;background:radial-gradient(58% 62% at 50% 45%,rgba(18,105,91,.3),rgba(18,105,91,.1) 58%,rgba(18,105,91,0) 78%);opacity:.35;transform:scale(.94);will-change:transform,opacity;animation:summary-glow-breathe 4s ease-in-out 280ms infinite}.summary::before{content:"";position:absolute;left:8%;right:8%;bottom:-13px;height:12px;z-index:0;pointer-events:none;border-radius:50%;background:radial-gradient(50% 50% at 50% 50%,rgba(27,55,44,.4),rgba(27,55,44,0) 72%);filter:blur(4px);opacity:.4;transform:scale(.94);animation:summary-shadow-breathe 4s ease-in-out 280ms infinite}.local-badge i{transform:scale(1);opacity:.62;animation:badge-breathe 4s ease-in-out 600ms infinite}/* 呼吸动效只由两个「不含文字」的图层承担：卡片外圈的光晕 + 卡片下方的地面阴影。卡片本体（含全部文字）保持静止。实测（CDP 冻结动画相位 + 截图边缘能量）只要卡片位移，文字层就会被合成器按小数设备像素重采样，edge_mean 从静止的 6.95 掉到 5.78（纯 translateY）甚至 4.63（translateY + scale(1.004)），观感就是「有时糊有时清晰、字微微闪烁」。光晕用 z-index:-1 压在卡片下面、pointer-events:none，动画只动 opacity/transform（合成器属性），不碰任何绘制属性；幅度必须肉眼可见（只改几个百分点等于「动画没掉了」）。 */
@keyframes summary-glow-breathe{0%,100%{opacity:.35;transform:scale(.94)}50%{opacity:1;transform:scale(1.06)}}@keyframes summary-shadow-breathe{0%,100%{opacity:.4;transform:scale(.94)}50%{opacity:.9;transform:scale(1.06)}}@keyframes badge-breathe{0%,100%{transform:scale(1);opacity:.48;box-shadow:0 0 0 0 rgba(18,105,91,.18)}50%{transform:scale(1.12);opacity:1;box-shadow:0 0 0 5px rgba(18,105,91,.12)}}
/* 轮播高度按最坏内容定死：表头 + 3 条(每条最多 2 行) + 「另有 N 件」1 行 + 间距；保证切换时不顶动下方元素，且不裁掉内容 */
.upcoming-carousel{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;height:200px;box-sizing:border-box;overflow:hidden;margin-top:12px;padding:10px 12px;border:1px solid #b8cbc1;border-radius:8px;background:rgba(255,255,255,.72);color:var(--ink)}.upcoming-main{min-width:0}.upcoming-main .eyebrow{font-size:11px}.upcoming-mode{display:inline-flex;gap:4px;margin:0 0 4px}.upcoming-mode-btn{min-height:32px;padding:4px 12px;border:1px solid var(--line);border-radius:999px;background:var(--paper);color:var(--muted);font-size:12px;line-height:18px;font-weight:600;cursor:pointer;transition:background-color 120ms ease-out,border-color 120ms ease-out,color 120ms ease-out}.upcoming-mode-btn:hover{border-color:var(--teal);color:var(--teal)}.upcoming-mode-btn.is-active{background:var(--teal-soft);border-color:var(--teal);color:var(--teal)}.upcoming-mode-btn:focus-visible{outline:2px solid var(--teal);outline-offset:1px}#upcoming-date{margin:2px 0 4px;font-size:16px;line-height:1.35}.upcoming-tasks{display:flex;flex-direction:column;gap:4px;margin:0;padding:0;list-style:none}.upcoming-tasks li{display:-webkit-box;max-width:72ch;color:#34483f;font-size:13px;line-height:1.35;overflow:hidden;overflow-wrap:anywhere;-webkit-box-orient:vertical;-webkit-line-clamp:2}.upcoming-tasks .upcoming-more{color:var(--muted)}#upcoming-date,#upcoming-tasks{transition:opacity 220ms ease-out,transform 220ms ease-out}#upcoming-carousel.is-switching #upcoming-date,#upcoming-carousel.is-switching #upcoming-tasks{opacity:0;transform:translateY(6px)}.upcoming-controls{display:flex;align-items:center;gap:6px;flex:none}.upcoming-count{min-width:38px;color:var(--muted);font-size:12px;text-align:right}.upcoming-controls button{display:grid;place-items:center;flex:0 0 40px;width:40px;min-width:40px;height:40px;min-height:40px;padding:0}.upcoming-controls svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.notice-feed{margin:0;padding:0 0 0 14px;border-left:2px solid var(--line);list-style:none}.notice-feed .notice{position:relative;display:grid;gap:6px;margin:0;padding:0 0 22px 18px;border:0;border-radius:0;background:transparent;box-shadow:none}.notice-feed .notice::before{content:"";position:absolute;left:-21px;top:7px;width:9px;height:9px;border:2px solid var(--paper);border-radius:50%;background:var(--teal)}.notice-meta{display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--muted);font-size:12px}.notice-feed .notice h3{width:max-content;max-width:100%;margin:0;padding:2px 8px;border:1px solid #94bfb1;border-radius:12px;background:var(--teal-soft);color:#20584a;font-size:12px;line-height:1.5}.notice-feed .notice p{max-width:72ch;margin:0;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere}
.hosting-settings{padding:16px;border:1px solid var(--line);border-radius:8px;background:var(--paper);color:var(--ink)}.hosting-settings .hosting-warning{border:1px solid #d5bb87;border-left:3px solid var(--amber);background:var(--amber-soft);color:#67480f}.hosting-settings .preference-list{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:12px 0 0;padding:12px;border:1px solid var(--line);border-radius:6px;background:var(--paper-alt)}.hosting-settings .preference-list legend{padding:0 6px;color:var(--muted)}.hosting-settings .preference{min-height:60px;gap:10px;margin:0;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--paper);color:var(--ink)}.hosting-settings .preference input{flex:0 0 18px;width:18px;height:18px;margin:2px 0 0;accent-color:var(--teal)}.hosting-settings .preference strong{display:block;color:var(--ink)}.hosting-settings .preference small{display:block;margin-top:2px;color:var(--muted);font-size:12px}.hosting-settings .hosting-status{border-color:var(--line);background:var(--paper-alt);color:var(--ink)}.hosting-settings .hosting-actions{display:flex;flex-wrap:wrap;gap:8px}.hosting-settings .hosting-actions .primary{border-color:#145f52;background:#145f52;color:#fff}.hosting-settings .setting-note{color:var(--muted)}
.task{transition:opacity 200ms ease-out,border-color 120ms ease-out,box-shadow 120ms ease-out}.task:not(.done):not([aria-busy=true]):hover,.task:not(.done):not([aria-busy=true]).is-focused,.task:not(.done):not([aria-busy=true]):focus-within{z-index:2;transform:none;border-color:#4b8d78;box-shadow:0 8px 16px rgba(22,48,38,.12)}.task.is-focused,.task:focus-within{transform:none}.btn:not(:disabled),.correct-btn:not(:disabled),.tabs button:not(:disabled){transition:background-color 120ms ease-out,border-color 120ms ease-out,color 120ms ease-out,box-shadow 120ms ease-out,transform 120ms ease-out}.btn:not(:disabled):hover,.correct-btn:not(:disabled):hover,.tabs button:not(:disabled):hover{border-color:#12695b;background-color:var(--teal-soft);color:#173e34}/* 悬停微交互：按钮抬 1px 并落一层浅影，让「可点」有手感；只加在按钮上，不动选项卡（下面已有下划线动画） */
.btn:not(:disabled):hover,.correct-btn:not(:disabled):hover{transform:translateY(-1px);box-shadow:0 4px 10px rgba(20,49,39,.12)}.btn.primary:not(:disabled):hover,.correct-btn.primary:not(:disabled):hover{border-color:#0f554a;background-color:#0f554a;color:#fff}.check:not(:disabled):hover{border-color:var(--teal);background-color:var(--teal-soft);box-shadow:0 0 0 3px rgba(18,105,91,.12)}.task .check:active:not(:disabled){transform:none;background:#d2e9e2;box-shadow:inset 0 0 0 2px rgba(18,105,91,.25)}button:disabled{opacity:.5;cursor:not-allowed}:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid #12695b!important;outline-offset:2px!important}.btn:active:not(:disabled),.correct-btn:active:not(:disabled),.check:active:not(:disabled),.tabs button:active:not(:disabled){transform:scale(.98);transition-duration:80ms}
.task.completion-confirmed .check{border-color:var(--teal);background:var(--teal);color:#fff;box-shadow:0 0 0 3px rgba(18,105,91,.16)}.task.completion-confirmed .check:after{border-color:#fff;animation:check-draw 240ms ease-out both}@keyframes check-draw{from{transform:rotate(42deg) scale(.25);opacity:0}to{transform:rotate(42deg) scale(1);opacity:1}}.task.completing{z-index:5;pointer-events:none;transition:transform 200ms ease-out,opacity 200ms ease-out!important;transform:translateY(-8px) scale(.96)!important;opacity:0!important}
.hosting-settings #push-settings,.hosting-settings #llm-settings{grid-template-columns:minmax(0,1fr)}.hosting-settings .push-channel{margin:8px 0;padding:0 12px;border:1px solid var(--line);border-radius:6px;background:var(--paper)}.hosting-settings .push-channel>summary{padding:10px 2px;font-size:13px;font-weight:650;color:var(--ink)}.hosting-settings .push-channel[open]>summary{margin-bottom:8px;border-bottom:1px solid var(--line)}.hosting-settings .push-steps{margin:8px 0 10px;padding-left:20px;color:var(--muted);font-size:12px;line-height:1.6}.hosting-settings .push-steps li{margin:4px 0}.hosting-settings .push-help{display:inline-block;margin:0 0 8px;color:var(--teal);font-size:12px;font-weight:600}.hosting-settings .preference input[type=text]{flex:1 1 160px;width:auto;min-width:120px;height:38px;margin:0;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink);font:inherit;font-size:13px}.hosting-settings .preference select{flex:0 0 240px;min-width:0;margin-left:auto;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink);font:inherit;font-size:13px}
.hosting-settings .preference-list.local-preferences{grid-template-columns:minmax(0,1fr)}
@media(min-width:1920px){.masthead,main{width:min(calc(100% - 96px),1760px);max-width:1760px}.workspace{grid-template-columns:minmax(0,1fr) minmax(320px,680px)}#tab-tasks{min-height:0}.side-rail{grid-template-columns:minmax(0,1fr)}#calendar-panel{width:auto;min-width:0}.task .t,.notice-feed .notice p,.history-note,.setting-note{max-width:72ch}}
@media(max-width:700px){.upcoming-carousel{height:220px;gap:8px;padding:9px}.upcoming-controls{gap:4px}.upcoming-count{min-width:30px;font-size:11px}.upcoming-controls button{flex-basis:36px;width:36px;min-width:36px;height:36px;min-height:36px}.hosting-settings .preference-list{grid-template-columns:minmax(0,1fr)}}
@media(prefers-reduced-motion:reduce){:root{--depth-mid:0px;--depth-top:0px;scroll-behavior:auto}*,*::before,*::after{animation:none!important;transition-duration:0ms!important}.summary{animation:none!important;transform:none!important}.summary::after,.local-badge i,.task.completion-confirmed .check:after{animation:none!important}.camera-enter,.task.is-focused,.task:focus-within,.surface,.task:hover,.btn:hover,.correct-btn:hover{transform:none!important}.task.completing{opacity:1!important;transform:none!important}.view-screen,.camera-surface,.task,.check,.correct-btn,.tabs button,.bar>i,.btn,.upcoming-carousel{transition-duration:0ms!important}.camera-moving{will-change:auto!important}button:active{transform:none!important}}
html[data-motion=paused] *,html[data-motion=paused] *::before,html[data-motion=paused] *::after{animation:none!important;transition-duration:0ms!important}html[data-motion=paused] .summary{animation:none!important;transform:none!important}html[data-motion=paused] .task:hover,html[data-motion=paused] .task.is-focused,html[data-motion=paused] .task:focus-within,html[data-motion=paused] button:active,html[data-motion=paused] .btn:hover,html[data-motion=paused] .correct-btn:hover{transform:none!important}html[data-motion=paused] .task.completing{opacity:1!important;transform:none!important}
.urgent-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px}.urgent-toggle,.urgent-reset{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:8px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:#344b40;font-size:13px;font-weight:650;transition:background-color 120ms ease-out,border-color 120ms ease-out,color 120ms ease-out,box-shadow 120ms ease-out}
/* 不写死宽度：按钮宽度由文字决定，才能和同排其它按钮（.btn）一致 —— 之前写死 min-width: 88px 让「紧急」两个字的按钮比文字宽出一大截，用户报「字体大小和按钮不符」 */
.pin-toggle{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:8px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:#344b40;font-size:13px;font-weight:650;transition:background-color 120ms ease-out,border-color 120ms ease-out,color 120ms ease-out,box-shadow 120ms ease-out}
.pin-toggle[aria-pressed=true]{border-color:#145f52;background:var(--teal-soft);color:#145f52}
.pinned-panel{margin-bottom:16px}.pinned-list{display:grid;gap:8px;margin:0;padding:0;max-height:280px;overflow:auto;list-style:none}.pinned-resize{display:flex;align-items:center;justify-content:center;gap:6px;width:100%;min-height:26px;margin-top:8px;padding:0;border:1px dashed #b8cbc1;border-radius:6px;background:#f4f7f5;color:#42554e;font:inherit;font-size:11px;cursor:ns-resize;touch-action:none}.pinned-resize:before{content:"";width:28px;height:3px;border-radius:2px;background:#9fb3a9}.pinned-resize:hover{border-color:#8fb3a5;background:#eef5f1}.pinned-resize:focus-visible{outline:2px solid #12695b;outline-offset:2px}.pinned-panel.is-resizing .pinned-resize{border-style:solid;border-color:#12695b}.pinned-item{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 10px;border:1px solid #d4ddd8;border-radius:6px;background:#fff;animation:pinned-enter 200ms ease-out both}.pinned-summary{flex:1 1 100%;min-height:24px;color:#1c382e;font-size:14px;font-weight:650;overflow-wrap:anywhere}.pinned-deadline{flex:1 1 auto;color:#53685e;font-size:12px}.pinned-empty{margin:0;color:#42554e;font-size:13px;line-height:1.5}
@keyframes pinned-enter{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
/* 只有本次渲染里新出现的待办卡片才做入场动画：render() 每次都会重建整个列表，若给 .task 直接挂动画，勾选任意一条都会让所有卡片一起抖 */
.task.is-new{animation:task-enter 220ms ease-out both}
@keyframes task-enter{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}.urgent-toggle[aria-pressed=true]{border-color:#a5312d;background:#fff0ed;color:#702a26}.urgent-mode{color:#53685e;font-size:12px}.task.effective-urgent:before{background:var(--red)}.task.effective-urgent{border-color:#d7a7a0}
.sync-card{margin-bottom:16px}.sync-layout{display:grid;grid-template-columns:minmax(220px,320px) minmax(0,1fr);gap:20px;align-items:center}.sync-qr-wrap{display:grid;place-items:center;min-width:0}.sync-qr{display:block;width:min(100%,320px);height:auto;aspect-ratio:1;object-fit:contain;background:#fff}.sync-qr[hidden]{display:none}.sync-copy{min-width:0}.sync-copy p{margin:8px 0;color:#344b40;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.sync-url{display:block;width:100%;min-height:44px;padding:8px 10px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink);font:13px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.sync-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.sync-status{min-height:24px;margin:8px 0 0;color:#344b40;font-size:13px}.sync-status[data-state=error]{color:#8a302a}.sync-status[data-state=success]{color:#145f52}.sync-alts{margin:8px 0 0;color:#344b40;font-size:13px;line-height:2}.sync-alts[hidden]{display:none}.sync-alt{margin:0 0 0 6px;min-height:34px;padding:4px 10px;font-size:12px}
.pair-steps{margin:0 0 12px;padding-left:20px;color:#344b40;font-size:13px;line-height:1.7}
#pair-pending{margin-top:12px}
#pair-pending .preference{justify-content:space-between}
#pair-pending .preference span{flex:1 1 auto;min-width:0}
#pair-pending .preference .btn{flex:0 0 auto}
button:not(:disabled),a[href],summary,select:not(:disabled),input:not(:disabled),label[for],#groups .group-row,.preference,.history-groups label,.login-choice label{cursor:pointer}input[type=text],input[type=search],input[type=url],input[type=number],input[type=date],input[type=datetime-local],textarea{cursor:text}button:disabled,input:disabled,select:disabled{cursor:not-allowed;opacity:.5}
button:not(:disabled):not(.btn):not(.correct-btn):not(.check):not([role=tab]){transition:background-color 100ms ease-out,border-color 100ms ease-out,color 100ms ease-out,box-shadow 100ms ease-out,transform 100ms ease-out,opacity 100ms ease-out}button:not(:disabled):hover{border-color:#12695b;background-color:var(--teal-soft);color:#173e34}.tabs button[aria-selected=true]:hover{box-shadow:inset 0 -2px var(--teal)}a[href],summary,select:not(:disabled),input:not(:disabled),label[for],#groups .group-row,.preference,.history-groups label,.login-choice label{transition:background-color 100ms ease-out,border-color 100ms ease-out,color 100ms ease-out,box-shadow 100ms ease-out,filter 100ms ease-out,transform 100ms ease-out}a[href]:hover{color:#12695b;text-decoration-line:underline;text-decoration-thickness:2px;text-underline-offset:2px}label[for]:hover{color:#12695b}summary:hover{border-radius:4px;background:var(--teal-soft);color:#173e34}select:not(:disabled):hover,input:not(:disabled):not([type=checkbox]):not([type=radio]):hover{border-color:#12695b;box-shadow:0 0 0 2px rgba(18,105,91,.12)}input[type=checkbox]:not(:disabled):hover,input[type=radio]:not(:disabled):hover{filter:brightness(.82)}#groups .group-row:hover,.preference:hover,.history-groups label:hover,.login-choice label:hover{border-color:#12695b;background:var(--teal-soft);box-shadow:0 0 0 2px rgba(18,105,91,.08)}button:not(:disabled):active{transform:scale(.98);transition-duration:100ms}a[href]:active,summary:active,select:not(:disabled):active,input:not(:disabled):active,label[for]:active,#groups .group-row:active,.preference:active,.history-groups label:active,.login-choice label:active{transform:scale(.98);transition-duration:100ms}select:not(:disabled):active,input:not(:disabled):not([type=checkbox]):not([type=radio]):active{border-color:#12695b;box-shadow:0 0 0 2px rgba(18,105,91,.12)}
.sync-card :focus-visible{outline:2px solid #12695b;outline-offset:2px}
@media(max-width:700px){.sync-layout{grid-template-columns:minmax(0,1fr)}.sync-qr{width:min(100%,280px)}}
/* 右栏新增的三块面板：本周小结 / 快捷操作 / 服务自检。数据全部来自已有接口
   （/api/tasks、/api/napcat/status、/api/hosting/status、/api/sync/info、/api/health），没有新增后端；
   按钮只做页面里本来就能做的事，不放没有实现的摆设。 */
.rail-panel{margin-top:16px}
.rail-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:10px 0 12px}
.rail-stat{padding:10px 8px;border:1px solid var(--line);border-radius:8px;background:var(--paper-alt);text-align:center}
.rail-stat strong{display:block;font-size:24px;line-height:1.15;font-weight:720;color:var(--ink);font-variant-numeric:tabular-nums}
.rail-stat span{font-size:12px;color:var(--muted)}
.rail-bars{display:flex;align-items:flex-end;gap:6px;height:96px;margin:4px 0 8px}
.rail-bar{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;text-align:center;font-size:11px;color:var(--muted)}
.rail-bar-fill{min-height:3px;border-radius:4px 4px 0 0;background:var(--teal-soft);border:1px solid rgba(18,105,91,.28);border-bottom:0}
.rail-bar.is-today .rail-bar-fill{background:var(--teal)}
.rail-bar.is-today{color:var(--ink);font-weight:640}
.rail-bar-value{font-variant-numeric:tabular-nums}
.rail-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:10px 0 8px}
.rail-actions .btn{width:100%;justify-content:center;text-align:center}
.rail-new-form{display:grid;gap:6px;margin:0 0 8px;padding:10px;border:1px dashed var(--line);border-radius:6px;background:var(--paper-alt);overflow:hidden}
.rail-new-form[hidden]{display:none}
.rail-new-form[data-state=entering]{animation:rail-form-enter 220ms var(--ease-in) forwards}
.rail-new-form[data-state=exiting]{animation:rail-form-exit 180ms var(--ease-out) forwards}
.rail-new-label{font-size:12px;font-weight:650;color:var(--muted)}
.rail-new-input{width:100%;min-height:34px;padding:0 8px;border:1px solid var(--line);border-radius:5px;background:var(--paper);color:var(--ink);font:inherit;font-size:13px}
.rail-new-input:focus-visible{outline:2px solid var(--teal);outline-offset:1px}
.rail-new-buttons{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:2px}
.rail-new-buttons .btn{width:100%;justify-content:center;text-align:center}
@keyframes rail-form-enter{from{max-height:0;opacity:0}to{max-height:300px;opacity:1}}
@keyframes rail-form-exit{from{max-height:300px;opacity:1}to{max-height:0;opacity:0}}
@keyframes calendar-detail-in{from{opacity:0;transform:translateY(-12px);max-height:0}to{opacity:1;transform:translateY(0);max-height:var(--detail-h,640px)}}
@keyframes calendar-detail-out{from{opacity:1;transform:translateY(0);max-height:var(--detail-h,640px)}to{opacity:0;transform:translateY(-8px);max-height:0}}
.calendar-detail.is-opening{animation:calendar-detail-in 240ms var(--ease-in) forwards}
.calendar-detail.is-closing{animation:calendar-detail-out 200ms var(--ease-out) forwards}
.health-list{list-style:none;margin:8px 0;padding:0}
.health-row{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:8px;align-items:start;padding:8px 0;border-top:1px solid var(--line)}
.health-row:first-child{border-top:0}
.health-dot{width:9px;height:9px;margin-top:6px;border-radius:50%;background:var(--line)}
.health-row[data-state=ok] .health-dot{background:var(--teal)}
.health-row[data-state=warn] .health-dot{background:var(--amber)}
.health-row[data-state=bad] .health-dot{background:var(--red)}
.health-label{font-weight:620;color:var(--ink)}
.health-hint{grid-column:2 / -1;margin-top:2px;font-size:12px;color:var(--muted);line-height:1.45}
.health-value{font-size:12px;color:var(--muted);text-align:right;white-space:nowrap}
@media(max-width:480px){.rail-bars{height:76px}}
</style>
</head>
<body>
<div id="onboarding" hidden style="position:fixed;inset:0;z-index:1000;background:rgba(10,20,18,.72);display:flex;align-items:center;justify-content:center;padding:20px"><section role="dialog" aria-modal="true" aria-labelledby="onboarding-title" style="max-width:620px;max-height:90vh;overflow:auto;background:var(--paper);color:var(--ink);padding:24px;border-radius:10px;box-shadow:0 16px 40px rgba(0,0,0,.3)"><h2 id="onboarding-title">欢迎使用群务台</h2><p>它把你指定的 QQ 群里的通知挑出来，变成待办和手机日历。先看四步就能用起来。</p><ol><li>点「一键接入」装好引擎</li><li>用手机 QQ 扫码登录</li><li>在设置里选要订阅的群</li><li>把日历订阅地址加到手机日历</li></ol><div role="note" style="padding:12px;margin:12px 0;background:var(--amber-soft);border-left:3px solid var(--amber)"><strong>风险提示</strong><p>NapCat 是第三方 QQ 协议客户端。用它登录主号有被风控甚至封号的风险，建议用小号。群务台只读群消息，不发言、不改群设置、不主动加好友。请遵守 QQ 用户协议，风险自负。</p></div><div role="note"><strong>数据说明</strong><p>消息、待办和附件都存在这台电脑的 SQLite 里，不上传到任何服务器。只有你自己配置了模型或推送通道时，内容才会发给你选的那家服务商。</p></div><p>本软件按 MIT 许可「按原样」提供，不提供任何担保。</p><button id="onboarding-accept" class="btn primary" type="button">我已阅读并理解</button></section></div>
<header class="masthead">
  <div class="masthead-row">
    <a class="brand" id="brand-home" href="/" aria-label="群务台首页：回到顶部并重新显示「开始使用」引导" title="回到顶部；重新显示「开始使用」引导"><svg class="brand-mark" viewBox="0 0 512 512" aria-hidden="true" focusable="false"><circle cx="230.4" cy="281.6" r="204.8" fill="#173b34"/><circle cx="399.36" cy="107.52" r="107.52" fill="#FFFFFF"/><circle cx="399.36" cy="107.52" r="76.8" fill="#12695b"/><path d="M112.64 307.2 L184.32 378.88 L276.48 235.52" fill="none" stroke="#FFFFFF" stroke-width="66.56" stroke-linecap="round" stroke-linejoin="round"/></svg><span><strong>群务台</strong><small>QQ 群消息 · 待办与日历</small></span></a>
    <div class="masthead-tools">
      <span class="local-badge"><i aria-hidden="true"></i> 本地运行</span>
      <button id="host-state" class="host-state" type="button" data-state="unknown" title="点击查看「开始使用」引导"><i aria-hidden="true"></i><span id="host-state-label">检查托管状态…</span></button>
      <button id="host-quick" class="btn primary host-quick" type="button" data-desired="start">开始托管</button>
    </div>
  </div>
  <section class="summary" aria-labelledby="headline">
    <div class="summary-top"><span class="eyebrow">今日概览</span><span class="progress-label" id="progressLabel">0%</span></div>
    <h1 id="headline">加载中…</h1>
    <p id="subline"></p>
    <section id="upcoming-carousel" class="upcoming-carousel" aria-label="今日概览：最近到期与正在进行" aria-live="off" hidden>
      <div class="upcoming-main"><div class="upcoming-mode" role="group" aria-label="今日概览显示内容"><button id="upcoming-mode-due" class="upcoming-mode-btn is-active" type="button" aria-pressed="true">最近到期</button><button id="upcoming-mode-now" class="upcoming-mode-btn" type="button" aria-pressed="false">正在进行</button></div><h2 id="upcoming-date"></h2><ul id="upcoming-tasks" class="upcoming-tasks"></ul></div>
      <div class="upcoming-controls"><span id="upcoming-count" class="upcoming-count"></span><button id="upcoming-prev" class="btn" type="button" aria-label="更早日期" title="更早日期"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"/></svg></button><button id="upcoming-next" class="btn" type="button" aria-label="更晚日期" title="更晚日期"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg></button></div>
    </section>
    <div class="summary-bottom"><div class="stats"><span class="stat" id="stat-open"></span><span class="stat" id="stat-overdue"></span><span class="stat" id="stat-done"></span></div><div class="bar" role="progressbar" aria-label="待办完成进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i id="progress"></i></div></div>
  </section>
  <nav class="tabs" aria-label="主导航" role="tablist">
    <button id="tab-tasks-button" data-tab="tasks" class="active" aria-current="page" role="tab" aria-selected="true" tabindex="0" aria-controls="tab-tasks" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h14v14H5zM8 12l2.5 2.5L16 9"/></svg><span>待办</span></button>
    <button id="tab-notices-button" data-tab="notices" role="tab" aria-selected="false" tabindex="-1" aria-controls="tab-notices" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg><span>最近推送</span></button>
    <button id="tab-inbox-button" data-tab="inbox" role="tab" aria-selected="false" tabindex="-1" aria-controls="tab-inbox" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v16H4zM4 13h4l2 3h4l2-3h4"/></svg><span>收件箱</span></button>
    <button id="tab-settings-button" data-tab="settings" role="tab" aria-selected="false" tabindex="-1" aria-controls="tab-settings" type="button"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="m19 13 2 1-2 4-2-1-2 1v2H9v-2l-2-1-2 1-2-4 2-1v-2l-2-1 2-4 2 1 2-1V4h6v2l2 1 2-1 2 4-2 1z"/></svg><span>设置</span></button>
  </nav>
</header>
<main>
  <section id="journey" class="journey" aria-labelledby="journey-title" hidden>
    <div>
      <span class="eyebrow">开始使用</span>
      <h2 id="journey-title">还差几步，群消息才会变成待办</h2>
      <ol id="journey-steps" class="journey-steps"></ol>
    </div>
    <div class="journey-side">
      <p id="journey-next" class="journey-next" role="status" aria-live="polite"></p>
      <div class="journey-actions">
        <button id="journey-action" class="btn primary" type="button">继续</button>
        <button id="journey-dismiss" class="btn" type="button">暂时收起</button>
      </div>
    </div>
  </section>
  <div id="action-feedback" class="action-feedback" data-state="success" hidden><span id="feedback-message" role="status" aria-live="polite" aria-atomic="true"></span><button id="feedback-retry" type="button" hidden>重试</button></div>
  <div class="workspace">
    <div class="primary-view">
      <section id="tab-tasks" class="view-screen" role="tabpanel" aria-label="待办清单" aria-labelledby="tab-tasks-button"></section>
      <section id="tab-notices" class="view-screen" role="tabpanel" aria-label="最近推送" aria-labelledby="tab-notices-button" hidden></section>
      <section id="tab-inbox" class="view-screen" role="tabpanel" aria-label="回溯收件箱" aria-labelledby="tab-inbox-button" hidden>
        <section class="surface" aria-labelledby="history-title">
          <div class="panel-heading"><div><span class="eyebrow">LOCAL HISTORY</span><h2 id="history-title">回溯聊天记录</h2></div></div>
          <p class="history-note" role="note">只能回溯到 NapCat 本地缓存最早的时间，不代表完整聊天历史；本机实测活跃群的缓存最早到 2026-08-27。按群逐个探测可查看实际边界。</p>
          <div class="history-grid">
            <div><div class="history-controls"><button id="history-all-groups" class="btn" type="button">全选</button><button id="history-clear-groups" class="btn" type="button">清空</button><button id="history-probe" class="btn" type="button">探测最早时间</button></div><p id="history-group-note" class="history-status" role="status" aria-live="polite">正在读取可选群…</p><div id="history-groups" class="history-groups"></div></div>
            <div><div class="history-controls"><label>开始日期<input id="history-since" type="date"></label><label>结束日期<input id="history-until" type="date"></label><label>最多判定条数<input id="history-limit" type="number" min="1" max="20000" step="1" value="1000"></label></div><div class="history-controls" style="margin-top:8px"><button id="history-7" class="btn" type="button">最近 7 天</button><button id="history-30" class="btn" type="button">最近 30 天</button><button id="history-all-dates" class="btn" type="button">全部可用</button></div><p id="history-floor-note" class="history-status" role="status" aria-live="polite">尚未探测缓存底线。</p></div>
          </div>
          <div class="history-actions" style="margin-top:12px"><button id="history-fetch" class="btn primary" type="button">① 回溯聊天记录</button><button id="history-classify" class="btn" type="button">② 提取通知</button></div>
          <div id="history-job" class="history-job" role="status" aria-live="polite" hidden><strong id="history-job-title"></strong><progress id="history-progress" max="1" value="0"></progress><p id="history-job-note"></p></div>
        </section>
        <section class="surface" aria-labelledby="inbox-title">
          <div class="panel-heading"><div><span class="eyebrow">CLASSIFIED MESSAGES</span><h2 id="inbox-title">通知收件箱</h2></div><span class="panel-index" id="inbox-total" hidden></span></div>
          <div class="inbox-counts" role="group" aria-label="按判定筛选" hidden><button type="button" data-inbox-verdict="all" aria-pressed="true" hidden>全部 <span id="count-all"></span></button><button type="button" data-inbox-verdict="notice" aria-pressed="false" hidden>确定通知 <span id="count-notice"></span></button><button type="button" data-inbox-verdict="suspect" aria-pressed="false" hidden>疑似通知 <span id="count-suspect"></span></button><button type="button" data-inbox-verdict="promoted" aria-pressed="false" hidden>已转待办 <span id="count-promoted"></span></button></div>
          <div class="inbox-filter"><label>群筛选<select id="inbox-group"><option value="">全部群</option></select></label><label class="search">正文关键词<input id="inbox-search" type="search" maxlength="200" placeholder="搜索消息正文"></label><div class="inbox-filter-tools"><button id="inbox-search-button" class="btn" type="button">筛选</button><button id="inbox-refresh" class="btn" type="button">刷新</button></div></div>
          <p id="inbox-message" class="inbox-empty" role="status" aria-live="polite">正在读取通知…</p><ul id="inbox-items" class="inbox-items"></ul><div class="inbox-pagination"><button id="inbox-more" class="btn" type="button" hidden>加载更多</button></div>
        </section>
      </section>
      <section id="tab-settings" class="view-screen" role="tabpanel" aria-label="服务设置" aria-labelledby="tab-settings-button" hidden></section>
    </div>
    <section id="calendar-panel" class="surface calendar-panel" aria-labelledby="calendar-title">
       <div class="panel-heading"><div><span class="eyebrow">SCHEDULE</span><h2 id="calendar-title">月历</h2></div><div class="month-controls"><button id="calendar-toggle" class="btn calendar-toggle" type="button" aria-expanded="false" aria-controls="calendar">查看月历</button><button id="cal-prev" class="btn" type="button" aria-label="上一月">‹</button><button id="cal-next" class="btn" type="button" aria-label="下一月">›</button></div></div><div class="weekday-row" aria-hidden="true"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
       <div id="calendar" aria-label="任务月历"></div>
       <p class="calendar-note">每格最多显示 3 条（带截止时刻），标题太长会截断、鼠标悬停看全文；点一下日期格（或键盘回车）看当天全部事项。</p>
       <div id="calendar-detail" class="calendar-detail" role="region" aria-live="polite" aria-label="当天全部事项" hidden></div>
     </section>
     <aside class="side-rail" aria-label="接入与订阅">
      <section id="pinned-panel" class="surface pinned-panel" aria-labelledby="pinned-title">
        <div class="panel-heading"><div><span class="eyebrow">PINNED</span><h2 id="pinned-title">置顶日程</h2></div><span class="panel-index" id="pinned-count" hidden></span></div>
        <p id="pinned-empty" class="pinned-empty" role="status" aria-live="polite">还没有置顶日程。在待办卡片里点「置顶」，就会固定出现在这里。</p>
        <ul id="pinned-list" class="pinned-list" aria-label="置顶日程列表"></ul>
        <button id="pinned-resize" class="pinned-resize" type="button" role="separator" aria-orientation="horizontal" aria-label="拖动调整置顶日程高度；上下方向键微调，双击恢复默认" aria-valuemin="120" aria-valuemax="720" aria-valuenow="280" title="拖动调整高度；双击恢复默认">拖动调整高度</button>
      </section>
      <section id="connect" class="surface connect-panel" aria-labelledby="connect-title">
        <div class="panel-heading"><div><span class="eyebrow">ACCOUNT</span><h2 id="connect-title">QQ 接入</h2></div><span class="panel-index">01</span></div>
        <div id="status" role="status" aria-live="polite">正在检查 QQ 登录状态…</div>
        <div id="install-box" hidden><button id="install-napcat" class="btn" type="button">自动下载并安装 NapCat</button><p>NapCat 会安装到独立数据目录，不会动你平时使用的电脑版 QQ；移除该目录即可卸载。</p><p id="install-result" role="status"></p></div>
        <div class="connect-actions"><button id="auto-setup" class="btn ghost" type="button" title="第一次用点这个：下载安装 NapCat、启动引擎并显示登录二维码">一键接入</button><button id="go" class="btn primary" type="button" aria-label="启动 NapCat 并登录" title="只启动引擎并显示登录二维码；不会退出电脑版 QQ">启动并登录</button><button id="refresh-qr" class="btn" type="button" title="二维码过期或看不清时重新获取一张">刷新二维码</button></div>
        <p class="connect-hint">第一次用：点「一键接入」——它会装好引擎、启动并显示二维码；已经有引擎、只是没登录时点「启动并登录」；二维码过期或看不清就点「刷新二维码」。二维码用手机 QQ 扫，登录成功后下面会出现「订阅群」。</p>
        <fieldset class="login-choice"><legend>登录方式</legend><label><input type="radio" name="login-method" value="qr" checked> 扫码登录（推荐）</label><label><input type="radio" name="login-method" value="uin"> 用指定 QQ 号快速登录</label><input id="uin" inputmode="numeric" pattern="[0-9]*" placeholder="输入 QQ 号" aria-label="QQ 号" disabled></fieldset>
        <p class="login-note"><strong>独立资料目录。</strong>不会改动日常电脑版 QQ；同一 QQ 号不能同时登录两台电脑。建议使用 QQ 小号接收通知。</p>
        <details class="login-more"><summary>账号与设备说明</summary><p>NapCat 使用独立资料目录，不读取或改动平时使用的电脑版 QQ。同一 QQ 号不能同时登录两台电脑；建议使用专用 QQ 小号接收通知。</p></details>
        <div id="steps" role="status" aria-live="polite"></div>
        <div id="qrbox"><p id="qr-note">启动 NapCat 后，这里会显示登录二维码。</p><img id="qr" alt="QQ 登录二维码" hidden></div>
        <section id="groupbox" class="subscription-panel" aria-labelledby="groups-title" hidden>
           <div class="panel-heading"><div><span class="eyebrow">SUBSCRIPTIONS</span><h2 id="groups-title">订阅群</h2></div></div>
           <p id="groups-message" role="status" aria-live="polite">登录后读取群列表。</p><a id="groups-connect-link" class="group-empty-link" href="#connect" hidden>前往 QQ 接入</a>
           <div id="group-source" class="group-source" hidden></div>
           <div id="groupbox-controls" hidden>
             <label class="sr-only" for="group-search">搜索群组</label><input id="group-search" class="group-search" type="search" placeholder="搜索群名或群号" autocomplete="off">
             <div class="group-tools"><button id="select-suggested" class="btn" type="button">全选建议</button><button id="clear-groups" class="btn" type="button">清空</button><button id="retry-groups" class="btn" type="button">重试</button></div>
             <div id="groups"></div>
             <button id="save" class="btn primary" type="button">保存订阅</button><p id="result" role="status" aria-live="polite"></p>
           </div>
         </section>
         <section id="rail-summary" class="surface rail-panel" aria-labelledby="rail-summary-title">
           <div class="panel-heading"><div><span class="eyebrow">WEEKLY</span><h2 id="rail-summary-title">本周小结</h2></div><span class="panel-index">02</span></div>
           <div class="rail-stats">
             <div class="rail-stat"><strong id="rail-stat-open">–</strong><span>待办</span></div>
             <div class="rail-stat"><strong id="rail-stat-done">–</strong><span>已完成</span></div>
             <div class="rail-stat"><strong id="rail-stat-overdue">–</strong><span>逾期</span></div>
           </div>
           <div id="rail-bars" class="rail-bars" role="img" aria-label="本周每天到期的待办条数"></div>
           <p id="rail-summary-note" class="connect-hint">今天有 <span id="rail-bars-today">0</span> 条到期；柱状图数的是本周每天到期的待办条数。</p>
         </section>
         <section id="rail-actions" class="surface rail-panel" aria-labelledby="rail-actions-title">
           <div class="panel-heading"><div><span class="eyebrow">QUICK ACTIONS</span><h2 id="rail-actions-title">快捷操作</h2></div><span class="panel-index">03</span></div>
           <div class="rail-actions">
             <button id="rail-new-task" class="btn primary" type="button" aria-expanded="false" aria-controls="rail-new-form">新建待办</button>
              <button id="rail-copy-today" class="btn" type="button">复制今日清单</button>
             <button id="rail-export-ics" class="btn" type="button">导出日历 .ics</button>
             <button id="rail-show-qr" class="btn" type="button">显示订阅二维码</button>
             <button id="rail-motion" class="btn" type="button" aria-pressed="false">暂停动态效果</button>
             <button id="rail-top" class="btn" type="button">回到顶部</button>
           </div>
           <form id="rail-new-form" class="rail-new-form" hidden>
                <label class="rail-new-label" for="rail-new-summary">待办内容</label>
                <input id="rail-new-summary" class="rail-new-input" type="text" maxlength="200" autocomplete="off" placeholder="例如：周五前交实验报告">
                <label class="rail-new-label" for="rail-new-deadline">截止时间（不填就放在「以后」）</label>
                <input id="rail-new-deadline" class="rail-new-input" type="datetime-local">
                <div class="rail-new-buttons">
                  <button id="rail-new-save" class="btn primary" type="submit">保存</button>
                  <button id="rail-new-cancel" class="btn" type="button">取消</button>
                </div>
              </form>
              <p id="rail-actions-note" class="connect-hint" role="status" aria-live="polite">「复制今日清单」把今天的待办按「时间 · 标题」复制成纯文本，方便贴到微信或备忘录。</p>
         </section>
         <section id="rail-health" class="surface rail-panel" aria-labelledby="rail-health-title">
           <div class="panel-heading"><div><span class="eyebrow">HEALTH</span><h2 id="rail-health-title">服务自检</h2></div><span class="panel-index">04</span></div>
           <p id="rail-health-message" class="connect-hint" role="status" aria-live="polite">正在检测…</p>
           <ul id="rail-health-list" class="health-list"></ul>
           <div class="rail-actions">
             <button id="rail-recheck" class="btn primary" type="button">重新检测</button>
             <button id="rail-settings" class="btn" type="button">打开设置</button>
           </div>
         </section>
      </section>
    </aside>
  </div>
</main>

<script>
var motionPreferencePaused = false;
var reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
try { motionPreferencePaused = localStorage.getItem('nh_motion') === 'paused'; } catch (error) {}
if (motionPreferencePaused) document.documentElement.setAttribute('data-motion', 'paused');
function motionIsPaused() { return motionPreferencePaused || reducedMotionQuery.matches; }
function updateMotionNote() {
  var note = document.getElementById('pref-motion-note');
  if (note) note.textContent = reducedMotionQuery.matches ? '系统减少动态效果设置已生效' : '仅保存在此浏览器';
}
function setMotionPreference(enabled) {
  motionPreferencePaused = !enabled;
  if (motionPreferencePaused) document.documentElement.setAttribute('data-motion', 'paused');
  else document.documentElement.removeAttribute('data-motion');
  var persisted = true;
  try { localStorage.setItem('nh_motion', motionPreferencePaused ? 'paused' : 'on'); }
  catch (error) { persisted = false; }
  updateMotionNote();
  if (!persisted) { var note = document.getElementById('pref-motion-note'); if (note) note.textContent = '无法保存到此浏览器'; }
  refreshUpcomingRotation();
}
reducedMotionQuery.addEventListener('change', function () { updateMotionNote(); refreshUpcomingRotation(); });

// 传输层错误（QQ 未登录 ⇒ NapCat 的 OneBot 端口拒绝连接）在界面上只应显示一句人话。
// 服务端仍按契约在 JSON 的 error 字段里保留原始 socket 文本（方便排查），这里在展示前统一翻译一次，
// 避免「读取群列表失败：<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>」这类消息漏到各个面板。
function friendlyError(raw) {
  var text = String(raw == null ? '' : raw).trim();
  if (/10061|积极拒绝|Connection refused|ECONNREFUSED/i.test(text)) return '连接被拒绝：对方服务没有在运行，或没有监听这个地址。';
  if (/10060|timed out|超时/i.test(text)) return '连接超时：QQ 或 NapCat 没有响应，请稍后重试。';
  if (/Failed to fetch|NetworkError|Load failed|不能连接到远程服务器/i.test(text)) return '本机服务没有响应，请确认 notice-hub 正在运行。';
  return text || '未知错误';
}
var setupToken = new URLSearchParams(location.search).get('token') || localStorage.getItem('qq_digest_token') || '';
function setupApi(path, options) { options = options || {}; options.headers = Object.assign({'X-Token': setupToken}, options.headers || {}); return fetch(path, options).then(function(r){ return r.json().then(function(x){ if(x && typeof x.error === 'string' && x.error) x.error = friendlyError(x.error); if(!r.ok) throw Error(x.error || '请求失败'); return x; }); }); }
function refreshQr() { var image = document.getElementById('qr'); var note = document.getElementById('qr-note'); if (!image) return; image.hidden = true; image.onload = function () { image.hidden = false; if (note) note.hidden = true; }; image.onerror = function () { image.hidden = true; if (note) { note.hidden = false; note.textContent = '二维码暂不可用。请启动 NapCat 后重试。'; } }; image.src = '/api/napcat/qrcode?token=' + encodeURIComponent(setupToken) + '&t=' + Date.now(); }
// 二维码只在 NapCat 换了新图时才重取（qr_stamp 变了）。之前每 5 秒无脑重载一次，图没变也在闪。
var lastQrStamp = null;
function refreshQrIfStale(stamp) { stamp = String(stamp || ''); if (stamp === lastQrStamp) return; lastQrStamp = stamp; refreshQr(); }
var qrWasVisible = false;
function checkSetup() { setupApi('/api/napcat/status').then(function(x){ if (!x.ok) throw Error(x.error || '连接不可用'); var s = document.getElementById('status'); var ok = !!(x.online || x.nickname); s.textContent = ok ? 'QQ 已连接：' + (x.nickname || '在线') : '先登录 QQ 才能读取群列表'; document.getElementById('qrbox').hidden = ok; document.getElementById('install-box').hidden = !!x.napcat_installed; document.getElementById('groupbox').hidden = false; document.getElementById('groups-message').textContent = ok ? '正在读取群列表…' : '先登录 QQ 才能读取群列表。'; document.getElementById('groups-connect-link').hidden=ok; if (!ok) { qrWasVisible = true; sessionStorage.removeItem('qq_login_reloaded'); refreshQrIfStale(x.qr_stamp); return; } if (qrWasVisible && !sessionStorage.getItem('qq_login_reloaded')) { sessionStorage.setItem('qq_login_reloaded','1'); window.location.reload(); return; } loadGroups(); }).catch(function(e){ var raw = String(e && e.message || e); var refused = /10061|积极拒绝|连接被拒绝|QQ 未登录|Failed to fetch|NetworkError|Load failed/.test(raw); var s = document.getElementById('status'); s.textContent = refused ? 'QQ 未登录：NapCat 在运行，但还没有可用的登录状态。请用手机 QQ 扫描下方二维码登录。' : '连接检查失败：' + raw; s.title = raw; document.getElementById('qrbox').hidden = false; document.getElementById('groupbox').hidden = false; document.getElementById('groups-message').textContent = refused ? '先登录 QQ 才能读取群列表。' : '群列表暂不可用：' + raw; document.getElementById('groups-connect-link').hidden=false; }); }
function installNapcat(){var b=document.getElementById('install-napcat');b.disabled=true;document.getElementById('install-result').textContent='下载中 / 解压中，请稍候…';setupApi('/api/napcat/install',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}}).then(function(x){if(!x.ok)throw Error(x.error||'安装失败');document.getElementById('install-result').textContent='已安装，点「启动 NapCat 并登录」继续';checkSetup();}).catch(function(e){document.getElementById('install-result').textContent='安装失败：'+e.message;}).then(function(){b.disabled=false;});}
function groupCategoryName(category) { return ({course:'课程通知',activity:'活动通知',market:'交易群',chat:'聊天群',other:'其它'})[category] || '其它'; }
function addGroupRow(list, group, suggested, selected) { var label=document.createElement('label'); label.className='group-row'; label.dataset.suggested=suggested?'true':'false'; label.dataset.search=(group.name+' '+group.group_id).toLocaleLowerCase(); var input=document.createElement('input'); input.type='checkbox'; input.value=group.group_id; input.checked=selected; input.setAttribute('aria-label','订阅 '+group.name+'，群号 '+group.group_id); label.appendChild(input); var meta=document.createElement('span'); meta.className='group-meta'; meta.appendChild(document.createElement('span')).className='group-name'; meta.lastChild.textContent=group.name+'（'+group.group_id+'）'; meta.appendChild(document.createElement('span')).className='category-badge'; meta.lastChild.textContent=groupCategoryName(group.category); if(suggested){meta.appendChild(document.createElement('span')).className='suggest-badge';meta.lastChild.textContent='建议订阅';} if(group.reason){var reason=document.createElement('span');reason.className='suggest-reason';reason.textContent=group.reason;meta.appendChild(reason);} label.appendChild(meta); list.appendChild(label); }
function renderGroupFilter() { var input=document.getElementById('group-search'),query=input.value.trim().toLocaleLowerCase(),details=document.querySelector('#groups details.other-groups'); if(details) details.open=!!query; var rows=document.querySelectorAll('#groups .group-row'); rows.forEach(function(row){row.hidden=!!query&&row.dataset.search.indexOf(query)<0;}); document.querySelectorAll('#groups .group-category').forEach(function(section){section.hidden=!section.querySelector('.group-row:not([hidden])');}); if(details) details.hidden=!details.querySelector('.group-row:not([hidden])'); }
function loadGroups() { var message=document.getElementById('groups-message'),controls=document.getElementById('groupbox-controls');message.textContent='正在读取群列表…';controls.hidden=true;Promise.all([setupApi('/api/groups/suggest'),setupApi('/api/napcat/groups')]).then(function(results){var suggestion=results[0],persisted=results[1];if(!suggestion.ok)throw Error(suggestion.error||'群组建议暂不可用');if(!Array.isArray(suggestion.groups)||!persisted.ok||!Array.isArray(persisted.groups))throw Error('群组数据格式无效');var saved=Object.create(null);persisted.groups.forEach(function(g){if(g&&g.group_id!==undefined&&g.group_id!==null)saved[String(g.group_id)]=g;});var normalized=suggestion.groups.map(function(g){if(!g||g.group_id===undefined||g.group_id===null)throw Error('群组数据缺少群号');return {group_id:String(g.group_id),name:String(g.name||g.group_id),category:['course','activity','market','chat','other'].indexOf(g.category)>=0?g.category:'other',suggested:g.suggested===true&&['course','activity'].indexOf(g.category)>=0,reason:String(g.reason||'')};});var ids=Object.create(null);normalized.forEach(function(g){ids[g.group_id]=true;});Object.keys(saved).forEach(function(id){if(!ids[id])normalized.push({group_id:id,name:String(saved[id].name||id),category:'other',suggested:false,reason:''});});if(!normalized.length)throw Error('暂未读取到群组，请确认 QQ 已登录后重试。');var root=document.getElementById('groups');root.textContent='';var source=document.getElementById('group-source');source.textContent='';source.hidden=false;source.dataset.source=suggestion.source==='llm'||suggestion.source==='heuristic'?suggestion.source:'unknown';var sourceName=suggestion.source==='llm'?'AI 识别':suggestion.source==='heuristic'?'按关键词识别':'来源未提供';var strong=document.createElement('strong');strong.textContent=sourceName;source.appendChild(strong);if(suggestion.error){var error=document.createElement('span');error.className='group-source-error';error.textContent=' '+String(suggestion.error);source.appendChild(error);}var suggested=normalized.filter(function(g){return g.suggested;});['course','activity'].forEach(function(category){var members=suggested.filter(function(g){return g.category===category;});if(!members.length)return;var section=document.createElement('section');section.className='group-category';var heading=document.createElement('h3');heading.textContent=groupCategoryName(category)+'（'+members.length+'）';section.appendChild(heading);var list=document.createElement('div');list.className='group-list';members.forEach(function(g){var hasSaved=Object.prototype.hasOwnProperty.call(saved,g.group_id)&&Object.prototype.hasOwnProperty.call(saved[g.group_id],'selected');addGroupRow(list,g,true,hasSaved?!!saved[g.group_id].selected:true);});section.appendChild(list);root.appendChild(section);});var others=normalized.filter(function(g){return !g.suggested;});if(others.length){var details=document.createElement('details');details.className='group-category other-groups';var summary=document.createElement('summary');summary.textContent='其它 '+others.length+' 个群（默认不订阅）';details.appendChild(summary);var list=document.createElement('div');list.className='group-list';others.forEach(function(g){var hasSaved=Object.prototype.hasOwnProperty.call(saved,g.group_id)&&Object.prototype.hasOwnProperty.call(saved[g.group_id],'selected');addGroupRow(list,g,false,hasSaved&&!!saved[g.group_id].selected);});details.appendChild(list);root.appendChild(details);}controls.hidden=false;document.getElementById('groupbox').hidden=false;renderGroupFilter();if(!normalized.length){message.textContent='没有读取到群组。先确认 QQ 已登录，再重试。';}else{message.textContent='群组 '+normalized.length+' 个；已订阅状态已载入。';}}).catch(function(e){message.textContent='群列表读取失败：'+e.message;document.getElementById('groups-connect-link').hidden=false;controls.hidden=false;}); }
function runAutoSetup() { var button=document.getElementById('auto-setup'),steps=document.getElementById('steps'),status=document.getElementById('status'); button.disabled=true;steps.textContent='正在检查并配置 NapCat…';setupApi('/api/napcat/autosetup',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}}).then(function(result){var lines=(result.steps||[]).map(function(step){return (step.ok?'✓ ':'! ')+(step.name||'步骤')+(step.detail?'：'+step.detail:'');});steps.textContent=lines.join('；');if(!result.ok)throw new Error(result.error||'一键接入未完成');if(result.waiting_for_login){status.textContent='配置已完成：请用手机 QQ 扫描下面的二维码登录，登录成功后这一页会自己刷新。';checkSetup();refreshQr();}else if(result.restart_required){status.textContent='NapCat 的接入端口没有起来：请点「启动 NapCat 并登录」重试，或稍后再点一次「一键接入」。';steps.textContent+=(steps.textContent?'；':'')+'请重试启动 NapCat。';}else{status.textContent=result.restarted?'NapCat 已重启，请用手机扫描下方二维码重新登录。':'一键接入已完成，请检查登录状态。';checkSetup();refreshQr();}}).catch(function(error){status.textContent='一键接入失败：'+error.message;}).then(function(){button.disabled=false;}); }
function run() { var button=document.getElementById('go'); var chosen=document.querySelector('input[name="login-method"]:checked').value; var uin=chosen==='uin' ? document.getElementById('uin').value.trim() : ''; button.disabled=true; document.getElementById('steps').textContent='正在启动 NapCat…'; setupApi('/api/napcat/launch',{method:'POST',body:JSON.stringify({uin:uin}),headers:{'Content-Type':'application/json'}}).then(function(x){ if(x.already_running){ document.getElementById('steps').textContent='NapCat 已经在运行'; } else if(x.ok){ document.getElementById('steps').textContent='已启动，正在等二维码…'; refreshQr(); } else { throw Error(x.error || '启动失败'); } checkSetup(); }).catch(function(e){document.getElementById('steps').textContent='启动失败：'+e.message;}).then(function(){button.disabled=false;}); }
document.getElementById('auto-setup').onclick=runAutoSetup; document.getElementById('go').onclick=run; document.getElementById('refresh-qr').onclick=refreshQr; document.getElementById('install-napcat').onclick=installNapcat; document.querySelectorAll('input[name="login-method"]').forEach(function(radio){radio.onchange=function(){document.getElementById('uin').disabled=radio.value!=='uin';};});
document.getElementById('group-search').addEventListener('input',function(event){if(!event.isComposing)renderGroupFilter();});document.getElementById('group-search').addEventListener('compositionend',renderGroupFilter);document.getElementById('retry-groups').onclick=loadGroups;document.getElementById('select-suggested').onclick=function(){document.querySelectorAll('#groups .group-row[data-suggested="true"] input').forEach(function(input){input.checked=true;});};document.getElementById('clear-groups').onclick=function(){document.querySelectorAll('#groups input[type="checkbox"]').forEach(function(input){input.checked=false;});};
document.getElementById('save').onclick=function(){var save=document.getElementById('save');var groups=[].slice.call(document.querySelectorAll('#groups input:checked')).map(function(i){return i.value;});if(!groups.length&&!window.confirm('未选择任何群，将清空所有订阅。确认继续？'))return;save.disabled=true;document.getElementById('result').textContent='正在保存…';setupApi('/api/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({groups:groups})}).then(function(result){if(!result.applied)throw Error(result.error||'服务端未保存订阅');document.getElementById('result').textContent='订阅已保存并生效';}).catch(function(e){document.getElementById('result').textContent='保存失败：'+e.message;}).then(function(){save.disabled=false;});}; refreshQr(); checkSetup();
// 未连接时每 5 秒复查登录态；二维码只在 NapCat 换了新图（qr_stamp 变了）时才重取。
// 连上后 #qrbox 会被隐藏，轮询自动停止，避免反复重载群列表覆盖用户已勾选的订阅。
window.setInterval(function () { var box = document.getElementById('qrbox'); if (box && !box.hidden) { checkSetup(); } }, 5000);
var KEY = 'qq_digest_token';
var params = new URLSearchParams(location.search);
if (params.get('token')) localStorage.setItem(KEY, params.get('token'));
var token = localStorage.getItem(KEY) || '';

if ('serviceWorker' in navigator && location.protocol === 'https:') {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () {});
  });
}

function api(path, options) {
  options = options || {};
  options.headers = Object.assign({'X-Token': token}, options.headers || {});
  return fetch(path, options).then(function (response) {
    if (!response.ok) throw new Error(friendlyError('HTTP ' + response.status));
    return response.json();
  }).then(function (body) {
    if (body && typeof body.error === 'string' && body.error) body.error = friendlyError(body.error);
    return body;
  }).catch(function (error) {
    throw new Error(friendlyError((error && error.message) || error));
  });
}

function initOnboarding() { var modal=document.getElementById('onboarding'), button=document.getElementById('onboarding-accept'); if(!modal||!button)return; fetch('/api/onboarding',{cache:'no-store'}).then(function(response){if(!response.ok)throw Error('onboarding unavailable');return response.json();}).then(function(data){if(!data.required)return; modal.hidden=false; button.focus(); button.onclick=function(){button.disabled=true;fetch('/api/onboarding/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(response){if(!response.ok)throw Error('accept failed');modal.hidden=true;}).catch(function(){button.disabled=false;});};}).catch(function(){}); }
initOnboarding();

function showFeedback(message, state, retry) {
  var box = document.getElementById('action-feedback');
  var messageNode = document.getElementById('feedback-message');
  var retryButton = document.getElementById('feedback-retry');
  box.hidden = false;
  box.dataset.state = state;
  messageNode.textContent = message;
  messageNode.setAttribute('aria-live', state === 'error' ? 'assertive' : 'polite');
  retryButton.hidden = typeof retry !== 'function';
  retryButton.disabled = false;
  retryButton.onclick = typeof retry === 'function' ? function () { retryButton.disabled = true; retry(); } : null;
}

function animateSurface(surface, shift) {
  if (!surface || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  surface.style.setProperty('--camera-x', shift + 'px');
  surface.classList.remove('camera-enter');
  surface.classList.add('camera-moving', 'camera-enter');
  var timer = 0;
  function finish(event) {
    if (event && event.target !== surface) return;
    surface.classList.remove('camera-moving', 'camera-enter');
    surface.removeEventListener('transitionend', finish);
    clearTimeout(timer);
  }
  surface.addEventListener('transitionend', finish);
  requestAnimationFrame(function () { requestAnimationFrame(function () { surface.classList.remove('camera-enter'); }); });
  timer = setTimeout(finish, 280);
}

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function actionButton(label, action, cls) {
  var button = el('button', 'btn ' + (cls || ''), label);
  button.onclick = function () { sendAction(button.__task, action, button); };
  return button;
}


var CATEGORY_LABELS = {urgent: '紧急', action: '待办', academic: '学业', info: '通知'};

function correctionButton(label, correction, cls, value) {
  var button = el('button', 'correct-btn ' + (cls || ''), label);
  button.onclick = function () {
    var actual = typeof value === 'function' ? value() : value;
    sendCorrection(button.__task, correction, actual, button);
  };
  return button;
}

function correctionPanel(task) {
  var box = el('details', 'correction');
  box.appendChild(el('summary', '', '纠错'));
  var panel = el('div', 'correction-panel');
  panel.appendChild(el('div', 'correction-hint', '纠正后会记录原判断，用于后续减少误判'));
  var grid = el('div', 'correct-grid');
  var quick = [['不是通知', 'not_notice'], ['不是待办', 'not_task'], ['标记重复', 'duplicate']];
  if (task.category === 'urgent') quick.splice(2, 0, ['不是紧急', 'not_urgent']);
  quick.forEach(function (item) {
    var button = correctionButton(item[0], item[1]);
    button.__task = task;
    grid.appendChild(button);
  });
  panel.appendChild(grid);

  var categoryRow = el('div', 'correct-row');
  var categorySelect = el('select', 'correct-select');
  categorySelect.setAttribute('aria-label', '修改任务分类');
  [['urgent', '紧急'], ['action', '待办'], ['academic', '学业'], ['info', '通知']].forEach(function (item) {
    var option = el('option', '', item[1]);
    option.value = item[0];
    categorySelect.appendChild(option);
  });
  categorySelect.value = CATEGORY_LABELS[task.category] ? task.category : 'info';
  var categoryButton = correctionButton('保存分类', 'category', 'primary', function () { return categorySelect.value; });
  categoryButton.__task = task;
  categoryRow.appendChild(categorySelect);
  categoryRow.appendChild(categoryButton);
  panel.appendChild(categoryRow);

  var deadlineRow = el('div', 'correct-row');
  var deadlineInput = el('input', 'correct-date');
  deadlineInput.type = 'datetime-local';
  deadlineInput.setAttribute('aria-label', '修改截止时间');
  deadlineInput.value = String(task.deadline || '').slice(0, 16);
  var deadlineButton = correctionButton('保存时间', 'deadline', 'primary', function () { return deadlineInput.value; });
  var clearButton = correctionButton('清空时间', 'clear_deadline', 'ghost');
  deadlineButton.__task = task; clearButton.__task = task;
  deadlineRow.appendChild(deadlineInput);
  deadlineRow.appendChild(deadlineButton);
  deadlineRow.appendChild(clearButton);
  panel.appendChild(deadlineRow);
  box.appendChild(panel);
  return box;
}
function taskNode(task) {
  var status = task.status || (task.done ? 'done' : 'open');
  var candidate = status === 'candidate';
  var category = CATEGORY_LABELS[task.category] ? task.category : 'info';
  var overdue = Boolean(task.overdue && !task.done);
  var classes = ['task', category];
  if (task.effective_urgent) classes.push('effective-urgent');
  if (candidate) classes.push('candidate');
  if (task.done) classes.push('done');
  if (overdue) classes.push('overdue');
  var li = el('li', classes.join(' '));
  li.id = 'task-' + task.id;
  if (candidate) {
    var candidateMark = el('span', 'candidate-mark', '?');
    candidateMark.setAttribute('aria-hidden', 'true');
    li.appendChild(candidateMark);
  } else {
    var check = el('button', 'check');
    check.setAttribute('aria-label', task.done ? '恢复待办' : '标记完成');
    check.setAttribute('aria-pressed', task.done ? 'true' : 'false');
    check.title = task.done ? '恢复待办' : '标记完成';
    check.onclick = function () { toggleTask(task, check); };
    li.appendChild(check);
  }
  var body = el('div', 'body');
  var top = el('div', 'card-top');
  top.appendChild(el('span', 'tag ' + category, CATEGORY_LABELS[category]));
  if (candidate) top.appendChild(el('span', 'tag candidate', '待确认'));
  if (task.deadline_text) top.appendChild(el('span', 'deadline-chip' + (overdue ? ' over' : ''), task.deadline_text));
  if (overdue) top.appendChild(el('span', 'overdue-chip', '已逾期'));
  if (task.snoozed) top.appendChild(el('span', 'snooze-chip', '稍后 ' + (task.snooze_text || '')));
  body.appendChild(top);
  var urgentControls = el('div', 'urgent-controls');
  var isUrgent = Boolean(task.effective_urgent);
  var urgentButton = el('button', 'urgent-toggle', '紧急');
  urgentButton.type = 'button';
  urgentButton.setAttribute('aria-pressed', isUrgent ? 'true' : 'false');
  urgentButton.setAttribute('aria-label', '紧急标记');
  urgentButton.title = isUrgent ? '当前生效为紧急；点击切换' : '当前未标记为紧急；点击切换';
  urgentButton.onclick = function () { sendUrgentOverride(task, !isUrgent, urgentButton); };
  urgentControls.appendChild(urgentButton);
  var followsAi = task.urgent_override === null || task.urgent_override === undefined;
  urgentControls.appendChild(el('span', 'urgent-mode', followsAi ? '跟随自动判断' : '用户手动设置'));
  if (!followsAi) {
    var resetUrgent = el('button', 'urgent-reset', '跟随自动判断');
    resetUrgent.type = 'button';
    resetUrgent.onclick = function () { sendUrgentOverride(task, null, resetUrgent); };
    urgentControls.appendChild(resetUrgent);
  }
  var pinButton = el('button', 'pin-toggle', task.pinned ? '已置顶' : '置顶');
  pinButton.type = 'button';
  pinButton.setAttribute('aria-pressed', task.pinned ? 'true' : 'false');
  pinButton.setAttribute('aria-label', (task.pinned ? '取消置顶：' : '置顶：') + (task.summary || task.text || '待办事项'));
  pinButton.onclick = function () { sendPinToggle(task, !task.pinned, pinButton); };
  urgentControls.appendChild(pinButton);
  body.appendChild(urgentControls);
  body.appendChild(el('div', 't', task.summary || task.text || ''));
  if (task.audience || task.condition) {
    var context = el('div', 'context');
    if (task.audience) context.appendChild(el('span', 'ctx', '适用：' + task.audience));
    if (task.condition) context.appendChild(el('span', 'ctx', '条件：' + task.condition));
    body.appendChild(context);
  }
  if (task.groups && task.groups.length) {
    var meta = el('div', 'meta');
    meta.appendChild(el('span', 'group-chip', task.groups.join('、')));
    body.appendChild(meta);
  }
  if (task.duplicate_summary) {
    body.appendChild(el('div', 'duplicate-note', '与「' + task.duplicate_summary + '」重复'));
  }
  if (candidate) {
    body.appendChild(el('div', 'confidence', (task.confidence_text || '') + ' · ' + (task.confidence_reason || '')));
    var actions = el('div', 'actions');
    var confirm = actionButton('确认待办', 'confirm', 'primary');
    var dismiss = actionButton('忽略', 'dismiss', 'ghost');
    var snooze = actionButton('明天提醒', 'snooze', 'ghost');
    confirm.__task = task; dismiss.__task = task; snooze.__task = task;
    actions.appendChild(confirm); actions.appendChild(dismiss); actions.appendChild(snooze);
    body.appendChild(actions);
  }
  if (task.details && task.details.length) {
    var detailBox = el('details');
    detailBox.appendChild(el('summary', '', '详细要求'));
    var detailList = el('ul', 'detail-list');
    task.details.forEach(function (value) { detailList.appendChild(el('li', '', value)); });
    detailBox.appendChild(detailList);
    body.appendChild(detailBox);
  }
  var fullText = String(task.source_text || '').trim();
  var evidenceText = String(task.evidence || '').trim();
  if (fullText || (evidenceText && evidenceText !== task.summary)) {
    var detail = el('details');
    detail.appendChild(el('summary', '', '查看完整原文'));
    detail.appendChild(el('p', '', fullText || evidenceText));
    body.appendChild(detail);
  }
  body.appendChild(correctionPanel(task));
  li.appendChild(body);
  return li;
}

function section(title, tasks, options) {
  tasks = tasks || [];
  options = options || {};
  if (!tasks.length) return null;
  var wrap = el(options.collapsed ? 'details' : 'section', 'section ' + (options.className || ''));
  var head = el(options.collapsed ? 'summary' : 'div', 'section-head');
  head.appendChild(el('h2', 'section-title', title));
  head.appendChild(el('span', 'count-pill', tasks.length + ' 件'));
  wrap.appendChild(head);
  var list = el('ul');
  tasks.forEach(function (task) { list.appendChild(taskNode(task)); });
  wrap.appendChild(list);
  return wrap;
}

function showStat(id, text, show) {
  var node = document.getElementById(id);
  node.textContent = text;
  node.classList.toggle('show', Boolean(show));
}

var calendarPanel = document.getElementById('calendar-panel');
var upcomingGroups = [];
var upcomingIndex = 0;
var upcomingTimer = null;
var upcomingSwitchTimer = null;
var UPCOMING_SWITCH_MS = 220; // 必须与 CSS 里 #upcoming-date,#upcoming-tasks 的 transition 时长一致
var upcomingHovered = false;
var upcomingFocused = false;
function stopUpcomingRotation() {
  if (upcomingTimer !== null) clearInterval(upcomingTimer);
  upcomingTimer = null;
}
// ---- 「最近到期 / 正在进行」切换 ----
// 口径（用户 2026-10-09 修订）：一件事从「收到通知」那一刻起，一直持续到它的截止时间，
// 这中间的任何时刻都算「正在进行」——大多数事情不是只有一天，而是持续几天再到期；
// 通知里写明当天「几点到几点」的（如 13:30-14:55），当天按那个时间段算，因为
// 「13:30 截止」常常只是开始时刻，真正的活动是 13:30-14:55。
var UPCOMING_MODE_KEY = 'qq_digest_overview_mode';
var upcomingMode = 'due';
try { if (window.localStorage.getItem(UPCOMING_MODE_KEY) === 'now') upcomingMode = 'now'; } catch (error) { upcomingMode = 'due'; }
var upcomingDue = [];
var upcomingNow = [];
// 「正在进行」每页最多几条：和「最近到期」一样按页轮换，这样件数再多也不会只显示前三条。
var UPCOMING_PAGE_SIZE = 3;
function nowUpcomingPages() {
  var pages = [];
  for (var index = 0; index < upcomingNow.length; index += UPCOMING_PAGE_SIZE) {
    pages.push({
      mode: 'now',
      page: upcomingNow.slice(index, index + UPCOMING_PAGE_SIZE),
      total: upcomingNow.length,
      pages: Math.ceil(upcomingNow.length / UPCOMING_PAGE_SIZE),
    });
  }
  // 空的时候也留一页，否则面板连切换按钮一起消失（用户报过「切不回最近到期」）。
  if (!pages.length) pages.push({mode: 'now', page: [], total: 0, pages: 1});
  return pages;
}
function parseStamp(value) {
  if (!value) return null;
  var stamp = new Date(String(value));
  return Number.isFinite(stamp.getTime()) ? stamp : null;
}
function parseTimeWindow(task) {
  var text = [task && task.summary, task && task.details, task && task.evidence].filter(Boolean).join(' ');
  if (!text) return null;
  var pattern = /(\\d{1,2})\\s*[:：]\\s*(\\d{2})/g;
  var matches = [];
  var match;
  while ((match = pattern.exec(text)) !== null) {
    var hours = Number(match[1]);
    var minutes = Number(match[2]);
    if (hours > 24 || minutes > 59) continue;
    matches.push({
      minutes: hours * 60 + minutes,
      text: (String(hours).length === 1 ? '0' + hours : String(hours)) + ':' + match[2],
      start: match.index,
      end: match.index + match[0].length,
    });
    if (matches.length >= 2) break;
  }
  if (matches.length < 2) return null;
  // 两个时刻之间必须有范围连接符（- – — ~ ～ 至），否则「9:00 上课 15:00 下课」这类散落的
  // 时刻会被误判成一整段窗口。
  var between = text.slice(matches[0].end, matches[1].start);
  if (!/[-–—~～]|至/.test(between)) return null;
  var start = matches[0].minutes;
  var end = matches[1].minutes;
  // 两个时刻完全相同（例如「23:59 截止 - 23:59 交」这类重复出现的同一时刻）不是一段窗口，
  // 否则会被当成「23:59 到 24:00」在最后一分钟误报「正在进行」。
  if (end === start) return null;
  if (end < start) end = Math.min(24 * 60, end + 24 * 60);
  var day = task && task.deadline ? String(task.deadline).slice(0, 10) : '';
  if (!/^\\d{4}-\\d{2}-\\d{2}$/.test(day)) day = localDateStamp(new Date());
  return {date: day, start: start, end: end, startText: matches[0].text, endText: matches[1].text};
}
function windowBounds(window_, now) {
  var dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return {
    start: new Date(dayStart.getTime() + window_.start * 60000),
    end: new Date(dayStart.getTime() + window_.end * 60000),
  };
}
function ongoingHint(end, now) {
  var hh = ('0' + end.getHours()).slice(-2) + ':' + ('0' + end.getMinutes()).slice(-2);
  var dayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  var endDay = new Date(end.getFullYear(), end.getMonth(), end.getDate());
  var days = Math.round((endDay.getTime() - dayStart.getTime()) / 86400000);
  if (days === 0) return '今天 ' + hh + ' 截止';
  if (days === 1) return '明天 ' + hh + ' 截止';
  return (end.getMonth() + 1) + '月' + end.getDate() + '日 截止';
}
// 返回 {start, end, hint}；既没有截止时间、也没有当天「几点到几点」的事项不算「正在进行」。
function ongoingSpan(task, now) {
  var deadline = parseStamp(task && task.deadline);
  var start = parseStamp(task && task.created_at);
  var end = deadline;
  var hint = '';
  var window_ = parseTimeWindow(task);
  var bounds = window_ && window_.date === localDateStamp(now) ? windowBounds(window_, now) : null;
  if (bounds) {
    if (!start || bounds.start < start) start = bounds.start;
    if (!end || bounds.end > end) end = bounds.end;
    hint = window_.startText + '-' + window_.endText + ' 结束';
  }
  if (!end) return null;
  // 没有开始时间（老数据）时，至少按截止那一天算，别把整条待办从创建那天算起。
  if (!start) start = new Date(end.getFullYear(), end.getMonth(), end.getDate());
  if (now < start || now > end) return null;
  if (!hint) hint = ongoingHint(end, now);
  return {start: start, end: end, hint: hint};
}
function collectUpcomingNow(data) {
  var now = new Date();
  var seen = Object.create(null);
  var found = [];
  (data.today || []).concat(data.week || [], data.later || []).forEach(function (task) {
    if (!task || task.done) return;
    var id = String(task.id);
    if (seen[id]) return;
    seen[id] = true;
    var span = ongoingSpan(task, now);
    if (!span) return;
    found.push({task: task, span: span});
  });
  found.sort(function (a, b) { return (a.span.end - b.span.end) || (Number(a.task.id) - Number(b.task.id)); });
  return found;
}
function renderUpcomingModeSwitch() {
  ['due', 'now'].forEach(function (mode) {
    var button = document.getElementById('upcoming-mode-' + mode);
    if (!button) return;
    var active = upcomingMode === mode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}
function applyUpcomingGroups() {
  var root = document.getElementById('upcoming-carousel');
  stopUpcomingRotation();
  upcomingGroups = upcomingMode === 'now'
    ? nowUpcomingPages()
    : upcomingDue;
  if (upcomingGroups.length) {
    upcomingIndex = Math.min(Math.max(0, upcomingIndex), upcomingGroups.length - 1);
    renderUpcomingSlide();
  } else {
    root.hidden = true;
  }
}
function setUpcomingMode(mode) {
  upcomingMode = mode === 'now' ? 'now' : 'due';
  try { window.localStorage.setItem(UPCOMING_MODE_KEY, upcomingMode); } catch (error) {}
  upcomingIndex = 0;
  renderUpcomingModeSwitch();
  applyUpcomingGroups();
}
function refreshUpcomingRotation() {
  stopUpcomingRotation();
  if (upcomingGroups.length < 2 || document.hidden || upcomingHovered || upcomingFocused || motionIsPaused()) return;
  upcomingTimer = setInterval(function () {
    upcomingIndex = (upcomingIndex + 1) % upcomingGroups.length;
    renderUpcomingSlide(true);
  }, 3000);
}
function renderUpcomingSlide(animate) {
  var root = document.getElementById('upcoming-carousel');
  if (upcomingSwitchTimer !== null) { window.clearTimeout(upcomingSwitchTimer); upcomingSwitchTimer = null; }
  root.classList.remove('is-switching');
  if (!upcomingGroups.length) { root.hidden = true; stopUpcomingRotation(); return; }
  var paint = function () {
    var group = upcomingGroups[upcomingIndex];
    var list = document.getElementById('upcoming-tasks');
    list.textContent = '';
    if (group.mode === 'now') {
      document.getElementById('upcoming-date').textContent = '正在进行的事项';
      document.getElementById('upcoming-count').textContent = group.total + ' 件'
        + (group.pages > 1 ? ' · 第 ' + (upcomingIndex + 1) + '/' + group.pages + ' 页' : '');
      if (!group.page.length) {
        list.appendChild(el('li', 'upcoming-more', '现在没有正在进行的日程'));
      } else {
        group.page.forEach(function (entry) {
          list.appendChild(el('li', '', String(entry.task.summary || entry.task.text || '待办事项') + ' · ' + entry.span.hint));
        });
      }
      document.getElementById('upcoming-prev').hidden = group.pages < 2;
      document.getElementById('upcoming-next').hidden = group.pages < 2;
      root.hidden = false;
      root.classList.remove('is-switching');
      return;
    }
    var date = new Date(group.date + 'T00:00:00');
    var startOfToday = new Date();
    startOfToday.setHours(0, 0, 0, 0);
    var startOfTomorrow = new Date(startOfToday.getTime());
    startOfTomorrow.setDate(startOfTomorrow.getDate() + 1);
    var dateText = date.toLocaleDateString('zh-CN', {month: 'long', day: 'numeric', weekday: 'short'});
    var prefix = group.date === localDateStamp(startOfToday) ? '今天 · '
      : group.date === localDateStamp(startOfTomorrow) ? '明天 · ' : '';
    document.getElementById('upcoming-date').textContent = prefix + dateText;
    document.getElementById('upcoming-count').textContent = group.tasks.length + ' 件';
    group.tasks.slice(0, 3).forEach(function (task) { list.appendChild(el('li', '', String(task.summary || task.text || '待办事项'))); });
    if (group.tasks.length > 3) list.appendChild(el('li', 'upcoming-more', '另有 ' + (group.tasks.length - 3) + ' 件'));
    document.getElementById('upcoming-prev').hidden = upcomingGroups.length < 2;
    document.getElementById('upcoming-next').hidden = upcomingGroups.length < 2;
    root.hidden = false;
    root.classList.remove('is-switching');
  };
  // 顺序必须是：先加 is-switching 淡出 → 等过渡跑完 → 换内容 → 去 is-switching 淡入。
  // 若在加完类的同一帧就换内容并删类，浏览器只绘制最终态，过渡永远不可见（用户报的「硬切」）。
  if (!animate || motionIsPaused()) { paint(); refreshUpcomingRotation(); return; }
  root.classList.add('is-switching');
  upcomingSwitchTimer = window.setTimeout(function () {
    upcomingSwitchTimer = null;
    paint();
    refreshUpcomingRotation();
  }, UPCOMING_SWITCH_MS);
}
function renderUpcoming(data) {
  var root = document.getElementById('upcoming-carousel');
  stopUpcomingRotation();
  // 今天的待办也要参与轮切。旧实现是「今天有到期事项就整块隐藏」（用户报的「没有轮切」）。
  var today = localDateStamp(new Date());
  var byDate = Object.create(null);
  var buckets = (data.today || []).concat(data.week || [], data.later || []);
  buckets.forEach(function (task) {
    if (!task || task.done || !task.deadline) return;
    var deadline = new Date(String(task.deadline));
    if (!Number.isFinite(deadline.getTime())) return;
    var key = localDateStamp(deadline);
    if (key < today) return;
    if (!byDate[key]) byDate[key] = [];
    byDate[key].push(task);
  });
  var previousDate = upcomingMode === 'due' && upcomingGroups[upcomingIndex] ? upcomingGroups[upcomingIndex].date : null;
  upcomingDue = Object.keys(byDate).sort().map(function (date) {
    byDate[date].sort(function (a, b) { return String(a.deadline).localeCompare(String(b.deadline)); });
    return {date: date, tasks: byDate[date]};
  });
  // 到期视图按日期定位；正在进行视图按页轮换，刷新数据时保留当前页，别把轮播拨回第一页。
  var nextIndex = upcomingMode === 'due'
    ? upcomingDue.findIndex(function (group) { return group.date === previousDate; })
    : upcomingIndex;
  upcomingIndex = nextIndex >= 0 ? nextIndex : 0;
  upcomingNow = collectUpcomingNow(data);
  renderUpcomingModeSwitch();
  applyUpcomingGroups();
}
function moveUpcoming(delta) {
  if (upcomingGroups.length < 2) return;
  upcomingIndex = (upcomingIndex + delta + upcomingGroups.length) % upcomingGroups.length;
  renderUpcomingSlide(true);
}
var upcomingRoot = document.getElementById('upcoming-carousel');
upcomingFocused = upcomingRoot.contains(document.activeElement);
upcomingRoot.addEventListener('mouseenter', function () { upcomingHovered = true; stopUpcomingRotation(); });
upcomingRoot.addEventListener('mouseleave', function () { upcomingHovered = false; refreshUpcomingRotation(); });
['upcoming-prev', 'upcoming-next'].forEach(function (id) {
  var button = document.getElementById(id);
  button.addEventListener('focus', function () { upcomingFocused = true; stopUpcomingRotation(); });
  button.addEventListener('blur', function (event) { if (!upcomingRoot.contains(event.relatedTarget)) { upcomingFocused = false; refreshUpcomingRotation(); } });
});
['due', 'now'].forEach(function (mode) {
  var button = document.getElementById('upcoming-mode-' + mode);
  if (!button) return;
  button.addEventListener('click', function () { setUpcomingMode(mode); });
  button.addEventListener('focus', function () { upcomingFocused = true; stopUpcomingRotation(); });
  button.addEventListener('blur', function () { upcomingFocused = false; refreshUpcomingRotation(); });
});
renderUpcomingModeSwitch();
 document.addEventListener('visibilitychange', refreshUpcomingRotation);
 document.getElementById('upcoming-prev').addEventListener('click', function () { moveUpcoming(-1); });
 document.getElementById('upcoming-next').addEventListener('click', function () { moveUpcoming(1); });
function sendPinToggle(task, value, button) {
  var pending = value ? '正在置顶…' : '正在取消置顶…';
  var success = value ? '已置顶' : '已取消置顶';
  var retry = function () { sendPinToggle(task, value, button); };
  return runTaskMutation(task, {task_id: String(task.id), pinned: value}, pending, success, button, retry, '/api/tasks/pin');
}

function renderPinned(data) {
  var list = document.getElementById('pinned-list');
  if (!list) return;
  var empty = document.getElementById('pinned-empty');
  var count = document.getElementById('pinned-count');
  var pinned = data.pinned || [];
  list.textContent = '';
  empty.hidden = pinned.length > 0;
  count.hidden = pinned.length === 0;
  count.textContent = String(pinned.length);
  pinned.forEach(function (task) {
    var summary = task.summary || task.text || '待办事项';
    var li = el('li', 'pinned-item');
    var link = el('a', 'pinned-summary', summary);
    link.href = '#task-' + task.id;
    li.appendChild(link);
    if (task.deadline_text) li.appendChild(el('span', 'pinned-deadline', task.deadline_text));
    var button = el('button', 'pin-toggle', '取消置顶');
    button.type = 'button';
    button.setAttribute('aria-pressed', 'true');
    button.setAttribute('aria-label', '取消置顶：' + summary);
    button.onclick = function () { sendPinToggle(task, false, button); };
    li.appendChild(button);
    list.appendChild(li);
  });
}

var lastRenderedTaskIds = null;
function render(data) {
  var progress = Number(data.progress || 0);
  document.getElementById('headline').textContent = data.headline || '今天没有待办';
  document.getElementById('subline').textContent = data.subline || '';
  document.getElementById('progressLabel').textContent = progress + '%';
  var boundedProgress = Math.max(0, Math.min(100, progress));
  document.getElementById('progress').style.transform = 'scaleX(' + boundedProgress / 100 + ')';
  document.querySelector('[role="progressbar"]').setAttribute('aria-valuenow', boundedProgress);
  var stats = data.stats || {};
  showStat('stat-open', '未完成 ' + (stats.open || 0), (stats.open || 0) > 0);
  showStat('stat-overdue', '已逾期 ' + (stats.overdue || 0), (stats.overdue || 0) > 0);
  showStat('stat-done', '已完成 ' + (stats.done || 0), (stats.done || 0) > 0);
  var today = data.today || [];
  var overdue = today.filter(function (task) { return task.overdue; });
  var dueToday = today.filter(function (task) { return !task.overdue; });
  renderUpcoming(data, dueToday.length);
  renderPinned(data);
  renderRailSummary(data);
  var todayBlock = section('今天', dueToday);
  var overdueBlock = section('已过期', overdue, {className: 'overdue', collapsed: true});
  var weekBlock = section('本周', data.week || []);
  var candidateBlock = section('待确认', data.candidates || []);
  var laterBlock = section('以后', data.later || []);
  var doneBlock = section('已完成', data.done || [], {className: 'done', collapsed: true});
  var taskBlocks = [todayBlock, weekBlock, candidateBlock, laterBlock, doneBlock].filter(Boolean);
  var blocks = [todayBlock, calendarPanel, overdueBlock, weekBlock, candidateBlock, laterBlock, doneBlock].filter(Boolean);
  var root = document.getElementById('tab-tasks');
  // FLIP：重建前先按「文档坐标」记住每张卡片的原位，重建后把被挤动的卡片动画过去，
  // 否则它们是瞬间跳位（用户报的「勾选后下面的待办挤上来没有过渡」）。
  // 只动画「留在同一分组里」的卡片：完成/取消会让卡片换分组（去「已完成」会飞出几千像素）。
  var sectionKey = function (card) {
    var block = card.closest ? card.closest('.section') : null;
    var title = block ? block.querySelector('.section-title') : null;
    return title ? title.textContent.trim() : '';
  };
  var beforeRects = Object.create(null);
  Array.prototype.forEach.call(root.querySelectorAll('li.task'), function (card) {
    var rect = card.getBoundingClientRect();
    beforeRects[card.id] = {left: rect.left + window.scrollX, top: rect.top + window.scrollY, section: sectionKey(card)};
  });
  root.textContent = '';
  if (!taskBlocks.length) root.appendChild(el('div', 'empty', '今天没有需要处理的事项'));
  blocks.forEach(function (block) { root.appendChild(block); });
  if (!motionIsPaused()) {
    Array.prototype.forEach.call(root.querySelectorAll('li.task'), function (card) {
      var before = beforeRects[card.id];
      if (!before || before.section !== sectionKey(card)) return;
      var rect = card.getBoundingClientRect();
      var dx = before.left - (rect.left + window.scrollX);
      var dy = before.top - (rect.top + window.scrollY);
      if (!dx && !dy) return;
      card.style.transition = 'none';
      card.style.transform = 'translate(' + dx + 'px, ' + dy + 'px)';
      void card.offsetWidth;
      card.style.willChange = 'transform';
      card.style.transition = 'transform 240ms cubic-bezier(.2, .8, .2, 1)';
      card.style.transform = '';
      var clearFlip = function () {
        card.style.transition = '';
        card.style.willChange = '';
      };
      card.addEventListener('transitionend', clearFlip, {once: true});
      // 兜底：过渡没被真正启动时也要把内联样式收干净
      window.setTimeout(clearFlip, 280);
    });
  }
  var previousIds = lastRenderedTaskIds;
  var currentIds = {};
  Array.prototype.forEach.call(root.querySelectorAll('li.task'), function (card) {
    var cardId = card.id.replace('task-', '');
    currentIds[cardId] = true;
    if (previousIds && !previousIds[cardId]) card.classList.add('is-new');
  });
  lastRenderedTaskIds = currentIds;
  if (location.hash) {
    var focused = document.querySelector(location.hash);
    if (focused) setTimeout(function () { focused.scrollIntoView({block: 'center'}); }, 40);
  }
}

function syncTasks() {
  return api('/api/tasks').then(function (data) {
    render(data);
    if (window.syncCalendarTasks) window.syncCalendarTasks(data);
    return data;
  });
}

function animateConfirmedTaskCompletion(card) {
  if (!card || !card.isConnected || document.hidden || motionIsPaused()) return Promise.resolve();
  card.classList.add('completion-confirmed');
  return new Promise(function (resolve) { setTimeout(resolve, 240); }).then(function () {
    if (card.isConnected) card.classList.add('completing');
    return new Promise(function (resolve) { setTimeout(resolve, 200); });
  });
}

function runTaskMutation(task, payload, pending, success, button, retry, endpoint) {
  var card = document.getElementById('task-' + task.id);
  var controls = card ? card.querySelectorAll('button') : [];
  var disabledStates = [];
  if (card) {
    card.setAttribute('aria-busy', 'true');
    for (var i = 0; i < controls.length; i += 1) {
      disabledStates.push(controls[i].disabled);
      controls[i].disabled = true;
    }
  }
  showFeedback(pending, 'loading');
  return api(endpoint || '/api/tasks/' + task.id, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  }).then(function (result) {
    if (!result.ok) throw new Error(result.error || '服务端未确认操作');
    var completionAnimation = payload.action === 'done' ? animateConfirmedTaskCompletion(card) : Promise.resolve();
    return completionAnimation.then(function () {
      return syncTasks().then(function () {
        showFeedback(success, 'success');
      }, function (error) {
        function retryRefresh() {
          syncTasks().then(function () { showFeedback(success, 'success'); }, function (refreshError) {
            showFeedback('服务端已确认，刷新仍未完成：' + refreshError.message, 'error', retryRefresh);
          });
        }
        showFeedback('服务端已确认，但待办和月历暂未同步：' + error.message, 'error', retryRefresh);
      });
    });
  }).catch(function (error) {
    showFeedback('操作未完成：' + error.message, 'error', retry);
  }).then(function () {
    if (card) {
      card.removeAttribute('aria-busy');
      for (var j = 0; j < controls.length; j += 1) controls[j].disabled = disabledStates[j];
    }
  });
}

function sendUrgentOverride(task, value, button) {
  var success = value === null ? '已恢复为跟随自动判断' : (value ? '已标记为紧急' : '已取消紧急标记');
  var retry = function () { sendUrgentOverride(task, value, button); };
  return runTaskMutation(task, {task_id: String(task.id), urgent: value}, '正在保存紧急设置…', success, button, retry, '/api/tasks/urgent');
}

function sendAction(task, action, button) {
  var labels = {done: '待办已完成并同步', reopen: '待办已恢复并同步', confirm: '事项已确认并同步', dismiss: '事项已忽略并同步', snooze: '提醒已稍后处理并同步'};
  var retry = function () { sendAction(task, action, button); };
  return runTaskMutation(task, {action: action}, '正在提交待办操作…', labels[action] || '操作已确认并同步', button, retry);
}

function sendCorrection(task, correction, value, button) {
  var retry = function () { sendCorrection(task, correction, value, button); };
  return runTaskMutation(task, {action: 'correct', correction: correction, value: value || ''}, '正在保存纠错…', '纠错已保存并同步', button, retry);
}

function toggleTask(task, button) {
  sendAction(task, task.done ? 'reopen' : 'done', button);
}

function loadTasks() {
  syncTasks().catch(function (error) {
    document.getElementById('headline').textContent = '加载失败';
    document.getElementById('subline').textContent = error.message;
    showFeedback('待办读取失败：' + error.message, 'error', loadTasks);
  });
}

function loadNotices() {
  var root = document.getElementById('tab-notices');
  root.textContent = '';
  api('/api/notices').then(function (data) {
    if (!Array.isArray(data.items)) throw new Error('推送数据格式无效');
    var head = el('div', 'section-head');
    head.appendChild(el('h2', 'section-title', '最近推送'));
    root.appendChild(head);
    if (!data.items.length) {
      root.appendChild(el('div', 'empty', '暂无推送记录。'));
      return;
    }
    var kinds = {urgent: '紧急推送', window: '定时摘要', silent: '静默摘要'};
    var list = el('ol', 'notice-feed');
    data.items.forEach(function (item) {
      var card = el('li', 'notice');
      var kind = String(item.kind || '');
      var title = kinds[kind] || kind || '摘要';
      var rawTime = String(item.created_at || '');
      var date = new Date(rawTime);
      var validTime = Number.isFinite(date.getTime());
      var time = el('time', '', validTime ? date.toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : rawTime || '时间未知');
      if (validTime) time.dateTime = rawTime;
      var meta = el('div', 'notice-meta');
      meta.appendChild(time);
      meta.appendChild(el('span', 'notice-kind', title));
      card.appendChild(meta);
      card.appendChild(el('h3', '', title));
      var body = String(item.body || '').trim();
      if (body) card.appendChild(el('p', '', body));
      list.appendChild(card);
    });
    root.appendChild(list);
  }).catch(function (error) {
    root.textContent = '';
    root.appendChild(el('div', 'empty', '推送读取失败：' + error.message));
  });
}

var historyGroups = [];
var inboxState = {verdict: 'all', groupId: '', q: '', offset: 0, total: 0};

function selectedHistoryGroups() {
  return Array.prototype.slice.call(document.querySelectorAll('#history-groups input:checked')).map(function (input) { return input.value; });
}

function localDateStamp(date) {
  return date.getFullYear() + '-' + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
}

function renderHistoryGroups(groups) {
  historyGroups = groups;
  var root = document.getElementById('history-groups');
  var filter = document.getElementById('inbox-group');
  root.textContent = '';
  filter.textContent = '';
  filter.appendChild(el('option', '', '全部群'));
  filter.firstChild.value = '';
  groups.forEach(function (group) {
    var label = el('label');
    var input = document.createElement('input');
    input.type = 'checkbox';
    input.value = group.group_id;
    input.checked = group.selected === true;
    label.appendChild(input);
    label.appendChild(el('span', '', group.name + ' · ' + group.group_id));
    root.appendChild(label);
    var option = el('option', '', group.name + ' · ' + group.group_id);
    option.value = group.group_id;
    filter.appendChild(option);
  });
  document.getElementById('history-group-note').textContent = groups.length ? '默认勾选当前订阅白名单群，可调整后单独回溯。' : '当前没有可选群。请确认 NapCat 已登录后重试。';
}

function loadHistoryGroups() {
  var note = document.getElementById('history-group-note');
  note.textContent = '正在读取可选群…';
  api('/api/napcat/groups').then(function (data) {
    if (!data.ok || !Array.isArray(data.groups)) throw new Error(data.error || '群列表暂不可用');
    var groups = data.groups.filter(function (group) { return group && group.group_id !== undefined && group.group_id !== null; }).map(function (group) {
      return {group_id: String(group.group_id), name: String(group.name || group.group_id), selected: group.selected === true};
    });
    renderHistoryGroups(groups);
  }).catch(function (error) {
    note.textContent = '读取群列表失败：' + error.message;
  });
}

function historyDates() {
  return {since: document.getElementById('history-since').value, until: document.getElementById('history-until').value};
}

function setHistoryButtonsBusy(busy) {
  document.getElementById('history-fetch').disabled = busy;
  document.getElementById('history-classify').disabled = busy;
}

function historyResultSummary(kind, status) {
  var result = status.result || {};
  if (kind === 'history') {
    var failed = (result.groups || []).filter(function (group) { return group.error; }).length;
    return '扫描 ' + Number(result.scanned || 0) + ' 条，新增 ' + Number(result.inserted || 0) + ' 条' + (failed ? '，' + failed + ' 个群失败' : '') + (result.error ? '；' + result.error : '');
  }
  var counts = result.counts || {};
  return '判定 ' + Number(result.classified || 0) + ' 条：确定通知 ' + Number(counts.notice || 0) + '，疑似 ' + Number(counts.suspect || 0) + '，非通知 ' + Number(counts.noise || 0) + '；来源 ' + String(result.method || '未知');
}

function showHistoryJob(kind, status) {
  var box = document.getElementById('history-job');
  var running = Boolean(status.running);
  box.hidden = false;
  document.getElementById('history-job-title').textContent = kind === 'history' ? (running ? '正在回溯聊天记录' : '回溯任务状态') : (running ? '正在提取通知' : '提取任务状态');
  var progress = document.getElementById('history-progress');
  progress.max = Math.max(1, Number(status.total || 0));
  if (running && Number(status.total || 0) <= 0) progress.removeAttribute('value');
  else progress.value = Math.max(0, Math.min(progress.max, Number(status.done || 0)));
  var note = String(status.note || '等待进度…');
  if (Number(status.total || 0) > 0) note += ' · ' + Number(status.done || 0) + ' / ' + Number(status.total || 0);
  if (!running && status.result) note = historyResultSummary(kind, status);
  document.getElementById('history-job-note').textContent = note;
  setHistoryButtonsBusy(running);
}

function pollHistoryJob(kind) {
  var path = kind === 'history' ? '/api/history/fetch/status' : '/api/inbox/classify/status';
  api(path).then(function (status) {
    showHistoryJob(kind, status);
    if (status.running) {
      window.setTimeout(function () { pollHistoryJob(kind); }, 1000);
    } else if (kind === 'classify' && status.result && status.result.ok) {
      loadInbox(false);
    }
  }).catch(function (error) {
    document.getElementById('history-job').hidden = false;
    document.getElementById('history-job-note').textContent = '任务状态读取失败：' + error.message;
    setHistoryButtonsBusy(false);
  });
}

function startHistoryJob(kind) {
  var groups = selectedHistoryGroups();
  if (!groups.length) {
    document.getElementById('history-group-note').textContent = '请至少选择一个群。';
    return;
  }
  var dates = historyDates();
  if (dates.since && dates.until && dates.since > dates.until) {
    document.getElementById('history-floor-note').textContent = '开始日期不能晚于结束日期。';
    return;
  }
  var payload = {groups: groups, since: dates.since, until: dates.until};
  var path = '/api/history/fetch';
  if (kind === 'classify') {
    var limit = Number(document.getElementById('history-limit').value);
    if (!Number.isInteger(limit) || limit < 1 || limit > 20000) {
      document.getElementById('history-floor-note').textContent = '最多判定条数必须在 1 到 20000 之间。';
      return;
    }
    payload.limit = limit;
    path = '/api/inbox/classify';
  }
  setHistoryButtonsBusy(true);
  var box = document.getElementById('history-job');
  box.hidden = false;
  document.getElementById('history-job-title').textContent = kind === 'history' ? '正在启动回溯任务' : '正在启动提取任务';
  document.getElementById('history-job-note').textContent = '请求已提交，正在等待后台进度…';
  var progress = document.getElementById('history-progress');
  progress.removeAttribute('value');
  api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)}).then(function (result) {
    if (!result.ok || !result.started) throw new Error(result.error || '任务没有启动');
    pollHistoryJob(kind);
  }).catch(function (error) {
    document.getElementById('history-job-note').textContent = '启动失败：' + error.message;
    setHistoryButtonsBusy(false);
  });
}

function probeHistoryFloor() {
  var groups = selectedHistoryGroups();
  var note = document.getElementById('history-floor-note');
  if (!groups.length) { note.textContent = '请至少选择一个群。'; return; }
  if (groups.length > 20) { note.textContent = '为避免长时间占用，请最多选择 20 个群进行探测。'; return; }
  var button = document.getElementById('history-probe');
  button.disabled = true;
  var results = [];
  function next(index) {
    if (index >= groups.length) {
      note.textContent = results.join('；') || '没有探测结果。';
      button.disabled = false;
      return;
    }
    var group = historyGroups.filter(function (item) { return item.group_id === groups[index]; })[0];
    var groupName = group ? group.name : groups[index];
    note.textContent = '正在探测最早可回溯时间… ' + (index + 1) + ' / ' + groups.length + ' · ' + groupName;
    var query = new URLSearchParams({group_id: groups[index]});
    api('/api/history/floor?' + query.toString()).then(function (result) {
      if (!result.ok) throw new Error(result.error || '探测失败');
      results.push(groupName + '：' + (result.floor_ts || '缓存中无消息') + '（扫描 ' + Number(result.total_seen || 0) + ' 条）');
    }).catch(function (error) {
      results.push(groupName + '：探测失败（' + error.message + '）');
    }).then(function () { next(index + 1); });
  }
  next(0);
}

function renderInboxItem(item) {
  var row = el('li', 'inbox-item');
  var verdicts = {notice: '确定通知', suspect: '疑似通知', noise: '非通知'};
  var methods = {llm: 'AI 判定', heuristic: '关键词判定', mixed: 'AI 与关键词判定'};
  var top = el('div', 'inbox-meta');
  top.appendChild(el('time', '', String(item.received_at || '时间未知')));
  top.appendChild(el('span', '', String(item.group_name || item.group_id || '未知群')));
  top.appendChild(el('span', '', String(item.sender_name || '未知发送人')));
  var badge = el('span', 'inbox-verdict', verdicts[item.verdict] || '判定未知');
  top.appendChild(badge);
  row.appendChild(top);
  row.appendChild(el('p', 'inbox-reason', '判定理由：' + String(item.reason || '未提供')));
  row.appendChild(el('p', 'inbox-reason', '来源：' + (methods[item.method] || '来源未提供')));
  var content = String(item.content || '');
  var details = el('details', 'inbox-content');
  details.appendChild(el('summary', '', '查看消息正文'));
  details.appendChild(el('p', '', content));
  row.appendChild(details);
  var promote = el('button', 'btn', item.task_id ? '已转待办' : '转成待办');
  promote.type = 'button';
  promote.disabled = Boolean(item.task_id);
  promote.setAttribute('aria-label', item.task_id ? '已转成待办' : '将这条通知转成待办');
  promote.onclick = function () {
    promote.disabled = true;
    promote.textContent = '正在创建…';
    api('/api/inbox/promote', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({msg_id: item.msg_id, title: content})}).then(function (result) {
      if (!result.ok || !result.task_id) throw new Error(result.error || '待办未创建');
      item.task_id = result.task_id;
      promote.textContent = '已转待办';
      promote.setAttribute('aria-label', '已转成待办');
      syncTasks().then(function () { showFeedback('通知已转成待办并同步到待办页', 'success'); }, function (error) { showFeedback('已转成待办，但待办页刷新失败：' + error.message, 'error', loadTasks); });
      loadInbox(false);
    }).catch(function (error) {
      promote.disabled = false;
      promote.textContent = '重试转待办';
      showFeedback('转换未确认：' + error.message, 'error', function () { promote.click(); });
    });
  };
  row.appendChild(promote);
  return row;
}

function loadInbox(append) {
  var message = document.getElementById('inbox-message');
  var root = document.getElementById('inbox-items');
  if (!append) {
    inboxState.offset = 0;
    root.textContent = '';
    message.textContent = '正在读取通知…';
  }
  var query = new URLSearchParams();
  if (inboxState.verdict !== 'all') query.set('verdict', inboxState.verdict);
  if (inboxState.groupId) query.set('group_id', inboxState.groupId);
  if (inboxState.q) query.set('q', inboxState.q);
  query.set('limit', '50');
  query.set('offset', String(inboxState.offset));
  api('/api/inbox?' + query.toString()).then(function (data) {
    if (!data.ok || !Array.isArray(data.items)) throw new Error(data.error || '收件箱数据格式无效');
    var counts = data.counts || {};
    function positiveCount(value) { var count = Number(value); return Number.isFinite(count) && count > 0 ? count : 0; }
    var filterCounts = {
      all: positiveCount(counts.notice) + positiveCount(counts.suspect) + positiveCount(counts.noise),
      notice: positiveCount(counts.notice),
      suspect: positiveCount(counts.suspect),
      promoted: positiveCount(counts.promoted)
    };
    var countsBar = document.querySelector('.inbox-counts');
    countsBar.hidden = filterCounts.all === 0;
    document.querySelectorAll('[data-inbox-verdict]').forEach(function (button) {
      var verdict = button.getAttribute('data-inbox-verdict');
      var count = filterCounts[verdict] || 0;
      button.querySelector('span').textContent = count ? String(count) : '';
      button.hidden = count === 0;
      button.setAttribute('aria-pressed', verdict === inboxState.verdict ? 'true' : 'false');
    });
    if (inboxState.verdict !== 'all' && !filterCounts[inboxState.verdict]) {
      inboxState.verdict = 'all';
      loadInbox(false);
      return;
    }
    var inboxTotal = positiveCount(data.total);
    var inboxTotalNode = document.getElementById('inbox-total');
    inboxTotalNode.hidden = inboxTotal === 0;
    inboxTotalNode.textContent = inboxTotal ? inboxTotal + ' 条' : '';
    data.items.forEach(function (item) { root.appendChild(renderInboxItem(item)); });
    inboxState.total = Number(data.total || 0);
    inboxState.offset += data.items.length;
    document.getElementById('inbox-more').hidden = inboxState.offset >= inboxState.total || data.items.length === 0;
    if (!inboxState.total) {
      message.textContent = filterCounts.all ? '当前筛选条件下暂无通知。' : '暂无判定结果。';
    } else {
      message.textContent = '显示 ' + inboxState.offset + ' / ' + inboxState.total + ' 条';
    }
  }).catch(function (error) {
    message.textContent = '收件箱读取失败：' + error.message;
    document.getElementById('inbox-more').hidden = true;
  });
}

function loadInboxTab() {
  loadHistoryGroups();
  loadInbox(false);
  api('/api/history/fetch/status').then(function (status) { if (status.running) pollHistoryJob('history'); });
  api('/api/inbox/classify/status').then(function (status) { if (status.running) pollHistoryJob('classify'); });
}

document.getElementById('history-all-groups').onclick = function () { document.querySelectorAll('#history-groups input[type="checkbox"]').forEach(function (input) { input.checked = true; }); };
document.getElementById('history-clear-groups').onclick = function () { document.querySelectorAll('#history-groups input[type="checkbox"]').forEach(function (input) { input.checked = false; }); };
document.getElementById('history-probe').onclick = probeHistoryFloor;
document.getElementById('history-7').onclick = function () { var until = new Date(); var since = new Date(); since.setDate(since.getDate() - 6); document.getElementById('history-since').value = localDateStamp(since); document.getElementById('history-until').value = localDateStamp(until); };
document.getElementById('history-30').onclick = function () { var until = new Date(); var since = new Date(); since.setDate(since.getDate() - 29); document.getElementById('history-since').value = localDateStamp(since); document.getElementById('history-until').value = localDateStamp(until); };
document.getElementById('history-all-dates').onclick = function () { document.getElementById('history-since').value = ''; document.getElementById('history-until').value = ''; };
document.getElementById('history-fetch').onclick = function () { startHistoryJob('history'); };
document.getElementById('history-classify').onclick = function () { startHistoryJob('classify'); };
document.querySelectorAll('[data-inbox-verdict]').forEach(function (button) { button.onclick = function () { inboxState.verdict = button.getAttribute('data-inbox-verdict'); loadInbox(false); }; });
document.getElementById('inbox-group').onchange = function () { inboxState.groupId = this.value; loadInbox(false); };
document.getElementById('inbox-search-button').onclick = function () { inboxState.q = document.getElementById('inbox-search').value.trim(); loadInbox(false); };
document.getElementById('inbox-search').onkeydown = function (event) { if (event.key === 'Enter') { event.preventDefault(); document.getElementById('inbox-search-button').click(); } };
document.getElementById('inbox-refresh').onclick = function () { loadInbox(false); };
document.getElementById('inbox-more').onclick = function () { loadInbox(true); };

var pairTimer = null;
function pairRow(item) {
  var row = el('div', 'preference');
  var text = el('span', '');
  text.appendChild(el('strong', '', item.device || '未知设备'));
  text.appendChild(el('small', '', '配对码 ' + item.code + ' · 已等待 ' + item.waiting_seconds + ' 秒'));
  row.appendChild(text);
  var allow = el('button', 'btn', '允许');
  allow.type = 'button';
  allow.addEventListener('click', function () { decidePair(item.code, true, allow); });
  var deny = el('button', 'btn', '拒绝');
  deny.type = 'button';
  deny.addEventListener('click', function () { decidePair(item.code, false, deny); });
  row.appendChild(allow);
  row.appendChild(deny);
  return row;
}
function setPairState(text, state) {
  var pill = document.getElementById('pair-state');
  if (!pill) return;
  pill.textContent = text;
  pill.dataset.state = state || '';
}
function setPairStatus(text, state) {
  var node = document.getElementById('pair-result');
  if (!node) return;
  node.textContent = text || '';
  node.dataset.state = state || '';
}
function decidePair(code, approved, button) {
  button.disabled = true;
  api('/api/pair/' + (approved ? 'approve' : 'deny'), {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code: code})}).then(function () {
    setPairStatus(approved ? '已允许这台手机，它马上就会连上。' : '已拒绝，手机上会提示重新配对。', approved ? 'ok' : '');
    refreshPair();
  }).catch(function (error) {
    setPairStatus((error && error.message) || '操作失败，请重试。', 'error');
    button.disabled = false;
  });
}
function refreshPair() {
  if (!document.getElementById('pair-pending')) return;
  api('/api/pair/pending', {cache: 'no-store'}).then(function (data) {
    var list = document.getElementById('pair-pending');
    if (!list) return;
    list.textContent = '';
    var items = (data && data.pending) || [];
    if (!items.length) list.appendChild(el('div', 'preference', '现在没有手机在等待配对，手机上打开 App 就会出现。'));
    items.forEach(function (item) { list.appendChild(pairRow(item)); });
    setPairState(items.length ? items.length + ' 台手机等待确认' : '没有等待中的手机', items.length ? 'warn' : 'ok');
  }).catch(function () {
    setPairState('读取失败', 'error');
  });
}
function pairCard() {
  var card = el('section', 'surface hosting-settings');
  card.setAttribute('aria-labelledby', 'pair-title');
  card.innerHTML = '<div class="section-head"><h2 id="pair-title" class="section-title">手机 App</h2><span id="pair-state" class="count-pill">读取中…</span></div>'
    + '<p class="setting-note">最省事：用手机相机扫下面的二维码，手机会直接打开「群务台」App 并自动连好，地址和令牌都不用输。</p>'
    + '<img id="pair-qr" class="sync-qr" alt="手机配对二维码" hidden>'
    + '<p id="pair-qr-msg" class="sync-status" role="status" aria-live="polite"></p>'
    + '<p class="setting-note">扫不动就手动配对：手机上打开 App，屏幕上会出现 6 位数字，在下面点「允许」也一样。</p>'
    + '<ol id="pair-steps" class="pair-steps"></ol>'
    + '<label for="pair-url">手机要用的地址</label>'
    + '<input id="pair-url" class="sync-url" type="text" readonly value="">'
    + '<div class="hosting-actions"><button id="pair-copy" class="btn" type="button">复制地址</button><button id="pair-enable" class="btn" type="button">开通公网入口</button><button id="pair-refresh" class="btn" type="button">刷新</button></div>'
    + '<p id="pair-hint" class="setting-note"></p>'
    + '<div id="pair-pending" class="preference-list"></div>'
    + '<p id="pair-result" class="sync-status" role="status" aria-live="polite"></p>';
  return card;
}
function pollPairInfo() {
  var input = document.getElementById('pair-url');
  if (!input) return;
  api('/api/pair/info', {cache: 'no-store'}).then(function (data) {
    data = data || {};
    input.value = data.base || '';
    var steps = document.getElementById('pair-steps');
    steps.textContent = '';
    (data.steps || []).forEach(function (step) { steps.appendChild(el('li', '', step)); });
    var hint = document.getElementById('pair-hint');
    var qr = document.getElementById('pair-qr');
    var qrMessage = document.getElementById('pair-qr-msg');
    if (data.base) {
      qr.src = '/api/app/qr.png?base=' + encodeURIComponent(data.base) + '&token=' + encodeURIComponent(token);
      qr.hidden = false;
      qrMessage.textContent = '扫码后手机就连上了；不想扫码也可以照着下面三步做。';
    } else {
      qr.hidden = true;
      qrMessage.textContent = '';
    }
    if (data.public_enabled) hint.textContent = '上面的地址在公网上，手机用移动网络也能打开。';
    else if (data.tailscale_ready) hint.textContent = data.no_public_hint || '';
    else hint.textContent = '这台电脑没装 Tailscale，先用局域网地址：手机和电脑连同一个 Wi-Fi 就能用。';
    var enable = document.getElementById('pair-enable');
    enable.disabled = !!data.public_enabled || !data.tailscale_ready;
    enable.textContent = data.public_enabled ? '公网入口已开通' : '开通公网入口';
  }).catch(function (error) {
    setPairStatus((error && error.message) || '读取失败', 'error');
  });
}
function setupPairCard() {
  if (!document.getElementById('pair-url')) return;
  document.getElementById('pair-copy').addEventListener('click', function () {
    var input = document.getElementById('pair-url');
    var text = input.value || '';
    if (!text) return;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { setPairStatus('地址已复制，发到手机上即可。', 'ok'); }, function () { input.select(); });
    } else {
      input.select();
    }
  });
  document.getElementById('pair-enable').addEventListener('click', function () {
    this.disabled = true;
    setPairStatus('正在开通公网入口，请稍等…', '');
    api('/api/pair/public', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}).then(function () {
      setPairStatus('公网入口开通成功，手机现在用移动网络也能连。', 'ok');
      pollPairInfo();
      refreshPair();
    }).catch(function (error) {
      setPairStatus((error && error.message) || '开通失败，请重试。', 'error');
      document.getElementById('pair-enable').disabled = false;
    });
  });
  document.getElementById('pair-refresh').addEventListener('click', function () { pollPairInfo(); refreshPair(); });
  pollPairInfo();
  refreshPair();
  if (pairTimer) window.clearInterval(pairTimer);
  pairTimer = window.setInterval(refreshPair, 4000);
}

function loadSettings() {
  var root = document.getElementById('tab-settings');
  root.textContent = '';
  var syncBox = el('section', 'surface sync-card');
  syncBox.setAttribute('aria-labelledby', 'sync-title');
  syncBox.innerHTML='<div class="section-head"><h2 id="sync-title" class="section-title">iPhone 日历订阅</h2><span id="sync-events" class="count-pill">读取事项数…</span></div><div class="sync-layout"><div class="sync-qr-wrap"><img id="sync-qr" class="sync-qr" alt="iPhone 日历订阅二维码" hidden><p id="sync-qr-message" class="sync-status" role="status" aria-live="polite">正在生成二维码…</p></div><div class="sync-copy"><label for="sync-url">订阅地址</label><input id="sync-url" class="sync-url" type="url" autocomplete="url" spellcheck="false" aria-describedby="sync-events sync-help"><div class="sync-actions"><button id="sync-copy" class="btn" type="button">复制订阅链接</button><button id="sync-test" class="btn" type="button">测试地址</button></div><p id="sync-alts" class="sync-alts" hidden></p><p id="sync-help">日历按 iPhone 的计划刷新，不是实时推送；可在“设置 → 日历 → 账户 → 已订阅的日历”调整刷新频率，也可在日历 App 下拉刷新。手机需能访问此地址（同一 Wi-Fi 或公网地址）。订阅地址包含访问令牌，令牌轮换后需重新订阅。</p><p id="sync-status" class="sync-status" role="status" aria-live="polite"></p></div></div>';
  root.appendChild(syncBox);
  root.appendChild(pairCard());
  setupPairCard();
  (function setupSyncCard() {
    var input = document.getElementById('sync-url');
    var qrImage = document.getElementById('sync-qr');
    var qrMessage = document.getElementById('sync-qr-message');
    var status = document.getElementById('sync-status');
    var events = document.getElementById('sync-events');
    var copyButton = document.getElementById('sync-copy');
    var testButton = document.getElementById('sync-test');
    function setStatus(message, state) { status.textContent = message; status.dataset.state = state || ''; }
    function setQrMessage(message) { qrMessage.textContent = message; }
    function webcalAddress(raw) {
      var value = String(raw || '').trim();
      var parsed = new URL(value);
      if (!parsed.hostname || parsed.username || parsed.password || !['http:', 'https:', 'webcal:'].includes(parsed.protocol)) throw new Error('请输入包含主机名的 HTTP、HTTPS 或 webcal 订阅地址。');
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') input.dataset.testScheme = parsed.protocol;
      else if (!input.dataset.testScheme) input.dataset.testScheme = 'http:';
      return value.replace(/^https?:/i, 'webcal:');
    }
    function renderSyncQr() {
      try {
        var address = webcalAddress(input.value);
        if (input.value !== address) input.value = address;
        qrMessage.textContent = '';
        qrImage.hidden = false;
        qrImage.onload = function () { setQrMessage(''); };
        qrImage.onerror = function () { qrImage.hidden = true; setQrMessage('二维码生成失败；请检查地址长度后重试。'); };
        qrImage.src = '/api/sync/qr.png?text=' + encodeURIComponent(address) + '&token=' + encodeURIComponent(setupToken);
      } catch (error) {
        qrImage.hidden = true;
        qrImage.removeAttribute('src');
        setQrMessage(error.message || '订阅地址无效。');
      }
    }
    var altList = document.getElementById('sync-alts');
    function renderAddressChoices(calendars, current) {
      var others = (calendars || []).filter(function (item) { return item && item.url && item.url !== current; });
      altList.textContent = '';
      if (!others.length) { altList.hidden = true; return; }
      altList.hidden = false;
      altList.appendChild(document.createTextNode('其他可用地址：'));
      others.forEach(function (item) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'btn ghost sync-alt';
        button.textContent = item.name || '备用地址';
        button.title = (item.hint ? item.hint + '；' : '') + item.url;
        button.onclick = function () {
          input.value = item.url;
          try { input.dataset.testScheme = new URL(item.url).protocol; } catch (error) { input.dataset.testScheme = ''; }
          renderSyncQr();
          renderAddressChoices(calendars, item.url);
          setStatus('已切换为' + (item.name || '备用地址') + '，二维码同步更新。', '');
        };
        altList.appendChild(button);
      });
    }
    function manualCopy() {
      input.focus();
      input.select();
      try {
        if (document.execCommand('copy')) { setStatus('订阅链接已复制。', 'success'); return; }
      } catch (error) {}
      setStatus('浏览器不允许复制；地址已选中，请手动复制。', 'error');
    }
    copyButton.onclick = function () {
      try { input.value = webcalAddress(input.value); } catch (error) { setStatus(error.message, 'error'); return; }
      renderSyncQr();
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') { manualCopy(); return; }
      try { navigator.clipboard.writeText(input.value).then(function () { setStatus('订阅链接已复制。', 'success'); }, manualCopy); } catch (error) { manualCopy(); }
    };
    testButton.onclick = function () {
      var requestUrl;
      try {
        input.value = webcalAddress(input.value);
        requestUrl = input.value.replace(/^webcal:/i, input.dataset.testScheme || 'http:');
      } catch (error) { setStatus(error.message, 'error'); return; }
      testButton.disabled = true;
      setStatus('正在让本机服务读取这个地址…', '');
      setupApi('/api/sync/test?url=' + encodeURIComponent(requestUrl)).then(function (data) {
        setStatus('本机服务能读取这个地址，包含 ' + data.events + ' 条事项。手机能不能订阅，请用手机打开同一地址确认。', 'success');
      }).catch(function (error) {
        var raw = String((error && error.message) || error || '').replace(/[。.\\s]+$/, '');
        setStatus('本机服务读不到这个地址：' + raw + '。点上方其他地址可以换一个，或直接用手机打开这个地址确认。', 'error');
      }).then(function () { testButton.disabled = false; });
    };
    input.addEventListener('input', function () { input.dataset.testScheme = ''; renderSyncQr(); });
    fetch('/api/sync/info', {headers: {'X-Token': token}, cache: 'no-store'}).then(function (response) {
      return response.json().then(function (data) { if (!response.ok || !data.ok) throw new Error(data.error || ('HTTP ' + response.status)); return data; });
    }).then(function (data) {
      var calendar = (data.calendars || [])[0];
      var count = Number(data.events);
      if (!calendar || !calendar.url || !Number.isInteger(count) || count < 0) throw new Error('订阅信息格式无效');
      events.textContent = '当前 ' + count + ' 条事项';
      input.value = calendar.url;
      try { input.dataset.testScheme = new URL(calendar.url).protocol; } catch (error) { input.dataset.testScheme = ''; }
      renderSyncQr();
      renderAddressChoices(data.calendars, calendar.url);
      setStatus('订阅地址已生成（' + (calendar.name || '默认地址') + '）。', '');
    }).catch(function (error) {
      events.textContent = '事项数暂不可用';
      setStatus('无法确定局域网订阅地址：' + (error.message || '请粘贴公网地址重试'), 'error');
      renderSyncQr();
    });
  })();
  var hostingBox = document.createElement('div'); hostingBox.className='hosting-settings';
  hostingBox.innerHTML='<h2 class="section-title">托管设置</h2><div class="hosting-warning" role="note"><strong>启用退出选项后，开始托管会关闭电脑版 QQ；结束时可按恢复选项重新启动。</strong></div><fieldset class="preference-list"><legend>自动化选项</legend><label class="preference"><input id="pref-quit_qq" type="checkbox"><span><strong>开始托管前退出电脑版 QQ</strong><small>避免桌面 QQ 与独立登录同时占用账号。</small></span></label><label class="preference"><input id="pref-restore_qq" type="checkbox"><span><strong>结束托管后恢复电脑版 QQ</strong><small>结束托管时重新启动电脑版 QQ。</small></span></label><label class="preference"><input id="pref-auto_on_start" type="checkbox"><span><strong>启动 notice-hub 时自动开始托管</strong><small>启动应用后立即按上述选项接管。</small></span></label><label class="preference"><input id="pref-autostart" type="checkbox"><span><strong>开机自动启动 notice-hub</strong><small>随系统启动此本地待办服务。</small></span></label></fieldset><fieldset class="preference-list"><legend>历史回溯</legend><label class="preference"><input id="pref-catchup-enabled" type="checkbox" disabled><span><strong>启动时自动回溯最近 N 天</strong><small>只影响应用启动时的补采行为，不会立即回溯。</small></span></label><label class="preference"><span><strong>回溯天数</strong><small>保存为现有的小时设置。</small></span><input id="pref-catchup-days" type="number" disabled min="1" max="365" step="1" value="1" aria-label="启动时自动回溯最近多少天"></label><button id="pref-catchup-save" class="btn" type="button" disabled>保存回溯设置</button><p id="pref-catchup-result" role="status" aria-live="polite"></p></fieldset><fieldset class="preference-list" id="llm-settings"><legend>AI 摘要（让它替你读消息、写待办）</legend><p class="setting-note">电脑上没装大模型也没关系：挑一家云服务，注册后把它给你的那串「钥匙」粘进来就行，一个月通常花不到一块钱。下面选好服务商，接口地址和模型名已经替你填好了，不用管。</p><label class="preference"><span><strong>用哪一家</strong><small id="llm-provider-hint">正在读取…</small></span><select id="llm-provider" aria-label="选择 AI 服务商"></select></label><div class="push-steps" id="llm-steps" hidden><ol id="llm-steps-list"></ol><a id="llm-help-link" class="push-help" target="_blank" rel="noreferrer" hidden></a></div><label class="preference"><span><strong>钥匙（API Key）</strong><small>粘一次就行；以后留空表示不改。</small></span><input id="llm-api-key" type="text" autocomplete="off" spellcheck="false" aria-label="API Key" placeholder="还没有填"></label><label class="preference"><span><strong>模型名</strong><small>已经替你填好，一般不用动。</small></span><input id="llm-model" type="text" autocomplete="off" spellcheck="false" aria-label="模型名"></label><label class="preference" id="llm-endpoint-row" hidden><span><strong>接口地址</strong><small>只有选「其它」时才需要填。</small></span><input id="llm-endpoint" type="text" autocomplete="off" spellcheck="false" aria-label="接口地址"></label><div class="hosting-actions"><button id="llm-save" class="btn primary" type="button">保存并测试</button><button id="llm-off" class="btn" type="button">不用 AI</button></div><p id="llm-result" role="status" aria-live="polite"></p></fieldset><fieldset class="preference-list" id="push-settings"><legend>提醒怎么送到手机</legend><p class="setting-note"><strong>不配也能用：</strong>点快捷操作里的「显示订阅二维码」，用手机日历扫一下，到期日程会同步进手机自带日历，到点手机自己响。想在微信里立刻收到提醒，就在下面挑一种，照着 1-2-3 做——每个值去哪里拿，都写在旁边了。</p><details class="push-channel" data-channel="wxpusher" open><summary>方式一：微信推送（WxPusher，推荐）</summary><ol class="push-steps"><li>打开下面的网站，用手机微信扫码登录。</li><li>点「应用管理」→「创建应用」，类型选「标准推送」，建好后复制那串「应用Token」贴到下面第一格。</li><li>把应用的二维码发给接收人（一般就是你自己）扫码关注，然后在「用户管理」里复制 UID_ 开头的那串，贴到第二格。</li><li>点下面的「保存」，再点「发一条测试消息」；手机收到就成功了。</li></ol><p><a class="push-help" data-help="wxpusher" target="_blank" rel="noreferrer">打开 WxPusher 后台，照着做</a></p><label class="preference"><span><strong>应用Token</strong><small>创建应用以后，页面上那串长得像密码的字符。</small></span><input id="push-wxpusher_app_token" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher 应用Token"></label><label class="preference"><span><strong>我的 UID</strong><small>关注公众号后，在「用户管理」里复制。多个用逗号分隔。</small></span><input id="push-wxpusher_uids" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher UID"></label><label class="preference"><span><strong>话题 ID（可选）</strong><small>只有要发给一群人时才填，一个人用就不用管它。</small></span><input id="push-wxpusher_topic_ids" type="text" autocomplete="off" spellcheck="false" aria-label="WxPusher 话题 ID"></label></details><details class="push-channel" data-channel="serverchan"><summary>方式二：Server 酱（微信，只需一个值）</summary><ol class="push-steps"><li>打开下面的网站，用手机微信扫码登录。</li><li>登录后网页上直接给你一串密码一样的字符，整串复制下来。</li><li>贴到下面，点「保存」，再点「发一条测试消息」。</li></ol><p><a class="push-help" data-help="serverchan" target="_blank" rel="noreferrer">打开 Server 酱（直达密钥页面）</a></p><label class="preference"><span><strong>密钥</strong><small>扫码登录后页面上直接显示的那串。</small></span><input id="push-serverchan_keys" type="text" autocomplete="off" spellcheck="false" aria-label="Server 酱密钥"></label></details><details class="push-channel" data-channel="pushplus"><summary>方式三：PushPlus（微信，只需一个值）</summary><ol class="push-steps"><li>打开下面的网站，用手机微信扫码登录。</li><li>进「一对一消息」页面，复制那里的「用户token」。</li><li>贴到下面，点「保存」，再点「发一条测试消息」。</li></ol><p><a class="push-help" data-help="pushplus" target="_blank" rel="noreferrer">打开 PushPlus，照着做</a></p><label class="preference"><span><strong>用户token</strong><small>登录后「一对一消息」页面显示的那串。</small></span><input id="push-pushplus_tokens" type="text" autocomplete="off" spellcheck="false" aria-label="PushPlus 用户token"></label></details><details class="push-channel" data-channel="webhook"><summary>方式四：企业微信 / 钉钉 / 自己的机器人</summary><ol class="push-steps"><li>在电脑版企业微信里右键要收提醒的群 →「管理聊天信息」。</li><li>右侧点「消息推送」→「自定义消息推送」，填个名字后复制那条地址。</li><li>把地址整段贴到下面（后面的参数别漏），点「保存」再点「发一条测试消息」。</li></ol><label class="preference"><span><strong>机器人地址</strong><small>整段粘贴，务必别漏掉后面的参数。</small></span><input id="push-webhook_urls" type="text" autocomplete="off" spellcheck="false" aria-label="机器人地址"></label></details><div class="hosting-actions"><button id="push-save" class="btn primary" type="button">保存</button><button id="push-test" class="btn" type="button">发一条测试消息</button><button id="push-clear" class="btn" type="button">清空重填</button></div><p id="push-result" role="status" aria-live="polite"></p></fieldset><p class="setting-note">托盘图标也可用于开始或结束托管。</p><div id="hosting-status" class="hosting-status" role="status" aria-live="polite">正在读取托管状态…</div><div class="hosting-actions"><button id="hosting-start" class="btn primary" type="button">开始托管</button><button id="hosting-stop" class="btn" type="button">结束托管</button></div><fieldset class="preference-list local-preferences"><legend>界面动效</legend><label class="preference"><input id="pref-motion-enabled" type="checkbox"><span><strong>轻微动效</strong><small id="pref-motion-note"></small></span></label></fieldset><p id="hosting-result" role="status" aria-live="polite"></p>';
  root.appendChild(hostingBox);
  var donateBox = document.createElement('section'); donateBox.className='surface hosting-settings'; donateBox.innerHTML='<h2 class="section-title">支持开发者</h2><p>如果这个工具帮到了你，可以请作者喝杯咖啡。完全自愿，不影响任何功能。</p><div class="donate-qr-row"><img id="donate-qr" src="/api/donate/qr.png" alt="微信收款码" style="width:180px;background:#fff;border-radius:8px;border:1px solid var(--line);"><small id="donate-qr-note">微信扫码支付</small></div><p class="setting-note">打赏不构成任何服务承诺，也不影响功能与更新。</p>'; root.appendChild(donateBox); var donateImage=document.getElementById('donate-qr'); donateImage.onerror=function(){donateImage.hidden=true;document.getElementById('donate-qr-note').textContent='收款码加载失败';};
   var motionToggle = document.getElementById('pref-motion-enabled');
  motionToggle.checked = !motionPreferencePaused;
  motionToggle.addEventListener('change', function () { setMotionPreference(motionToggle.checked); });
  updateMotionNote();
  function setHostingResult(text){document.getElementById('hosting-result').textContent=text;}
  var preferenceNames=['quit_qq','restore_qq','auto_on_start','autostart'];
  var savedPreferences=null;
   var savedCatchup=null;
  function readPreferences(){var values={};preferenceNames.forEach(function(name){values[name]=document.getElementById('pref-'+name).checked;});return values;}
  function setPreferences(values){preferenceNames.forEach(function(name){document.getElementById('pref-'+name).checked=!!values[name];});}
  function setPreferencesBusy(busy){preferenceNames.forEach(function(name){document.getElementById('pref-'+name).disabled=busy;});}
   function setCatchup(values){document.getElementById('pref-catchup-enabled').checked=!!values.catchup_enabled;document.getElementById('pref-catchup-days').value=String(values.catchup_days);}
   function setCatchupBusy(busy){document.getElementById('pref-catchup-enabled').disabled=busy;document.getElementById('pref-catchup-days').disabled=busy;document.getElementById('pref-catchup-save').disabled=busy;}
   function saveCatchup(){var days=Number(document.getElementById('pref-catchup-days').value);if(!Number.isInteger(days)||days<1||days>365){document.getElementById('pref-catchup-result').textContent='回溯天数必须在 1 到 365 之间。';return;}var values={catchup_enabled:document.getElementById('pref-catchup-enabled').checked,catchup_hours:days*24};setCatchupBusy(true);document.getElementById('pref-catchup-result').textContent='正在保存…';api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');savedCatchup={catchup_enabled:values.catchup_enabled,catchup_days:days};document.getElementById('pref-catchup-result').textContent='已保存。仅在下次启动时应用。';showFeedback('启动回溯设置已保存','success');}).catch(function(error){if(savedCatchup)setCatchup(savedCatchup);document.getElementById('pref-catchup-result').textContent='保存失败：'+error.message;showFeedback('启动回溯设置未保存：'+error.message,'error',saveCatchup);}).then(function(){setCatchupBusy(false);});}
   document.getElementById('pref-catchup-save').onclick=saveCatchup;
  function savePreferences(values){setPreferencesBusy(true);showFeedback('正在保存设置…','loading');return api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');savedPreferences=values;setHostingResult('设置已保存');showFeedback('设置已保存','success');}).catch(function(error){if(savedPreferences)setPreferences(savedPreferences);setHostingResult('保存失败：'+error.message);showFeedback('设置未保存：'+error.message,'error',function(){savePreferences(values);});}).then(function(){setPreferencesBusy(false);});}
  setPreferencesBusy(true);
  api('/api/settings').then(function(values){savedPreferences={};preferenceNames.forEach(function(name){savedPreferences[name]=!!values[name];});setPreferences(savedPreferences);var days=Math.min(365,Math.max(1,Math.ceil(Number(values.catchup_hours||24)/24)));savedCatchup={catchup_enabled:!!values.catchup_enabled,catchup_days:days};setCatchup(savedCatchup);setPreferencesBusy(false);setCatchupBusy(false);}).catch(function(error){setHostingResult('设置读取失败：'+error.message);showFeedback('设置读取失败：'+error.message,'error',loadSettings);});
  preferenceNames.forEach(function(name){document.getElementById('pref-'+name).onchange=function(){if(savedPreferences)savePreferences(readPreferences());};});
  function refreshHosting(){return api('/api/hosting/status').then(function(status){document.getElementById('hosting-status').textContent='托管：'+(status.hosting_active?'进行中':'未开始')+'；NapCat：'+(status.napcat_running?'运行中':'未运行')+'；登录：'+(status.napcat_online?'在线':'未登录')+'；电脑版 QQ：'+(status.user_qq_running?'运行中':'未运行');return status;}).catch(function(error){setHostingResult('状态读取失败：'+error.message);throw error;});}
  function changeHosting(button,path,desired,pending,success){button.disabled=true;setHostingResult(pending);showFeedback(pending,'loading');function retryStatus(){refreshHosting().then(function(status){if(!!status.hosting_active===desired){setHostingResult(success);showFeedback(success,'success');}else{showFeedback('托管状态尚未确认','error',retryStatus);}},function(error){showFeedback('状态刷新失败：'+error.message,'error',retryStatus);});}api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未确认操作');return refreshHosting().then(function(status){if(!!status.hosting_active!==desired){setHostingResult('服务端已响应，托管状态尚未达到预期');showFeedback('服务端已响应，托管状态尚未达到预期','error',retryStatus);return;}setHostingResult(success);showFeedback(success,'success');},function(error){showFeedback('服务端已响应，但状态读取失败：'+error.message,'error',retryStatus);});}).catch(function(error){setHostingResult('托管操作未确认：'+error.message);showFeedback('托管操作未确认：'+error.message,'error',function(){changeHosting(button,path,desired,pending,success);});}).then(function(){button.disabled=false;});}
  document.getElementById('hosting-start').onclick=function(){changeHosting(this,'/api/hosting/start',true,'正在开始托管，请稍候…','托管已开始并确认');};
  document.getElementById('hosting-stop').onclick=function(){changeHosting(this,'/api/hosting/stop',false,'正在结束托管，请稍候…','托管已结束并确认');}; refreshHosting().catch(function(){});
  var pushFields=['wxpusher_app_token','wxpusher_uids','wxpusher_topic_ids','serverchan_keys','pushplus_tokens','webhook_urls'];
  function applyPushState(state){if(!state)return;pushFields.forEach(function(name){var input=document.getElementById('push-'+name);var item=state[name];if(!input)return;input.placeholder=(item&&item.set)?('已配置：'+item.masked.join('、')+'（留空不修改）'):'未配置';});var names=[];pushFields.forEach(function(name){if(state[name]&&state[name].set)names.push(name);});document.getElementById('push-result').textContent='当前推送通道：'+(names.join('、')||'未配置')+'。';}
  function savePushSettings(all){var values={};pushFields.forEach(function(name){var input=document.getElementById('push-'+name);if(!input)return;var text=input.value.trim();if(text||all)values[name]=text;});var resultBox=document.getElementById('push-result');if(!Object.keys(values).length){resultBox.textContent='没有要保存的内容：留空的项不会修改，清空请用「清除全部推送设置」。';return;}resultBox.textContent='正在保存…';api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');pushFields.forEach(function(name){var input=document.getElementById('push-'+name);if(input)input.value='';});applyPushState(result.push);resultBox.textContent='已保存，当前推送通道：'+((result.channels||[]).join('、')||'未配置')+'。';showFeedback('推送设置已保存','success');}).catch(function(error){resultBox.textContent='保存失败：'+error.message;showFeedback('推送设置未保存：'+error.message,'error',function(){savePushSettings(all);});});}
  document.getElementById('push-save').onclick=function(){savePushSettings(false);};
  document.getElementById('push-clear').onclick=function(){if(!window.confirm('确定清空全部推送通道配置吗？清空后「截止提醒」将发不出去。'))return;savePushSettings(true);};
  function applyPushHelp(map){if(!map)return;var links=document.querySelectorAll('a.push-help[data-help]');for(var i=0;i<links.length;i++){var url=map[links[i].getAttribute('data-help')];if(url){links[i].href=url;links[i].rel='noreferrer';}}}
  function testPush(){var box=document.getElementById('push-result');box.textContent='正在发送测试消息…';return api('/api/settings/test-push',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){var list=result.results||[];if(!list.length){box.textContent=result.error||'还没有配置任何推送通道：先在上面填好并保存。';return;}var text=list.map(function(item){return (item.ok?'收到：':'失败：')+item.channel+(item.ok?'':(' · '+(item.error||'原因不明')));}).join('；');box.textContent=result.ok?('测试消息已发出——'+text):('有的通道没发成功——'+text);if(result.ok)showFeedback('测试消息已发出','success');}).catch(function(error){box.textContent='测试失败：'+error.message;});}
  document.getElementById('push-test').onclick=function(){testPush();};
  var llmProviders=[],llmState=null;
  function llmProvider(id){for(var i=0;i<llmProviders.length;i++){if(llmProviders[i].id===id)return llmProviders[i];}return null;}
  function setLlmBusy(busy){document.getElementById('llm-provider').disabled=busy;document.getElementById('llm-model').disabled=busy;document.getElementById('llm-endpoint').disabled=busy;document.getElementById('llm-save').disabled=busy;document.getElementById('llm-off').disabled=busy;}
  function renderLlmProvider(id){var item=llmProvider(id);if(!item)return;var hint=document.getElementById('llm-provider-hint');hint.textContent=item.hint||'';document.getElementById('llm-model').value=item.model||'';document.getElementById('llm-endpoint').value=item.endpoint||'';document.getElementById('llm-endpoint-row').hidden=(id!=='custom');var list=document.getElementById('llm-steps-list');list.textContent='';var steps=item.key_steps||[];steps.forEach(function(text){list.appendChild(el('li','',text));});document.getElementById('llm-steps').hidden=!steps.length;var link=document.getElementById('llm-help-link');if(item.key_url){link.href=item.key_url;link.textContent=(item.needs_key?'打开拿钥匙的页面':'打开官网，照着上面做');link.hidden=false;}else{link.hidden=true;}var keyInput=document.getElementById('llm-api-key');keyInput.value='';keyInput.placeholder=(llmState&&llmState.key_set&&llmState.provider===id)?('已配置：'+llmState.key_masked+'（留空不修改）'):(item.needs_key?'粘贴你的钥匙':'这家不用钥匙');}
  function applyLlmState(state){if(!state)return;llmState=state;var select=document.getElementById('llm-provider');select.value=state.provider||'';renderLlmProvider(state.provider||'');if(state.model)document.getElementById('llm-model').value=state.model;if(state.endpoint)document.getElementById('llm-endpoint').value=state.endpoint;var box=document.getElementById('llm-result');var item=llmProvider(state.provider)||{};if(!state.enabled){box.textContent='现在没有用 AI：群里消息只归档，不生成摘要。';}else if(item.needs_key&&!state.key_set){box.textContent='还差一个钥匙：填好以后 AI 摘要才会运行。';}else{box.textContent='正在使用 '+(state.model||'未设置')+(state.vision?'（能读图片）':'（不能读图片，图片只记录名字）')+'。';}}
  function saveLlm(thenTest){var providerId=document.getElementById('llm-provider').value;var payload={llm_provider:providerId};var key=document.getElementById('llm-api-key').value.trim();if(key)payload.llm_api_key=key;var model=document.getElementById('llm-model').value.trim();if(model)payload.llm_model=model;if(providerId==='custom')payload.llm_endpoint=document.getElementById('llm-endpoint').value.trim();var box=document.getElementById('llm-result');box.textContent='正在保存…';setLlmBusy(true);return api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(result){if(!result.ok)throw new Error(result.error||'服务端未保存设置');applyLlmState(result.llm);box.textContent='已保存。';showFeedback('AI 接入已保存','success');if(thenTest)return testLlm();}).catch(function(error){box.textContent='保存失败：'+error.message;showFeedback('AI 接入未保存：'+error.message,'error',function(){saveLlm(thenTest);});}).then(function(){setLlmBusy(false);});}
  function testLlm(){var box=document.getElementById('llm-result');box.textContent='正在问一下 AI，稍等几秒…';return api('/api/settings/test-llm',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){if(!result.ok){box.textContent='没连通：'+(result.error||'原因不明');showFeedback('AI 连接测试没通过','error');return;}box.textContent='连通了：'+(result.model||'模型')+' 回了「'+(result.reply||'')+'」（'+(result.seconds||0)+' 秒）。';showFeedback('AI 连接测试通过','success');}).catch(function(error){box.textContent='测试失败：'+error.message;});}
  document.getElementById('llm-provider').onchange=function(){var item=llmProvider(this.value)||{};renderLlmProvider(this.value);document.getElementById('llm-result').textContent=item.hint||'';};
  document.getElementById('llm-save').onclick=function(){saveLlm(true);};
  document.getElementById('llm-off').onclick=function(){document.getElementById('llm-provider').value='off';renderLlmProvider('off');saveLlm(false);};
  api('/api/meta').then(function (data) {
    llmProviders = data.providers || [];
    var llmSelect = document.getElementById('llm-provider');
    llmSelect.innerHTML = '';
    llmProviders.forEach(function (item) {
      var option = document.createElement('option');
      option.value = item.id;
      option.textContent = item.name;
      llmSelect.appendChild(option);
    });
    applyLlmState(data.llm);
    applyPushHelp(data.push_help);
    applyPushState(data.push);
    var head = el('div', 'section-head');
    head.appendChild(el('h2', 'section-title', '运行信息'));
    root.appendChild(head);
    var rows = [
      ['监控群', (data.groups || []).join('、') || '未配置'],
      ['推送通道', (data.channels || []).join('、') || '无'],
      ['截止提醒', data.reminders || '未开启'],
      ['推送预算', data.push_budget || '不限'],
      ['夜间静默', data.quiet_hours || '未设置'],
      ['待办统计', '共 ' + data.stats.total + ' 件，未完成 ' + data.stats.open + ' 件'],
      ['服务时间', data.started_at || '未知']
    ];
    rows.forEach(function (row) {
      var kv = el('div', 'kv');
      kv.appendChild(el('span', '', row[0]));
      kv.appendChild(el('span', '', row[1]));
      root.appendChild(kv);
    });
    var insights = data.insights || [];
    if (insights.length) {
      var tipHead = el('div', 'section-head');
      tipHead.appendChild(el('h2', 'section-title', '纠错提示'));
      root.appendChild(tipHead);
      var tipList = el('ul', 'insight-list');
      insights.forEach(function (text) { tipList.appendChild(el('li', '', text)); });
      root.appendChild(tipList);
    }
  });
}

(function(){
 var cursor=new Date(); cursor.setDate(1); var calendarTasks=[];
 var MAX_CALENDAR_EVENTS=3; var calendarDetail=document.getElementById('calendar-detail');
 function pad(value){return String(value).padStart(2,'0');}
 function dayKey(y,m,d){return y+'-'+pad(m+1)+'-'+pad(d);}
 function deadlineTime(task){var raw=String(task&&task.deadline||''); return raw.length>=16?raw.slice(11,16):'';}
 function tasksOn(key){return calendarTasks.filter(function(task){return String(task&&task.deadline||'').slice(0,10)===key;});}
 function closeCalendarDetail(){if(!calendarDetail)return; document.querySelectorAll('.calendar-day.is-open').forEach(function(cell){cell.classList.remove('is-open'); cell.setAttribute('aria-expanded','false');}); if(calendarDetail.hidden)return; var finishClose=function(){if(calendarDetail.dataset.closing!=='1')return; delete calendarDetail.dataset.closing; calendarDetail.hidden=true; calendarDetail.textContent=''; calendarDetail.style.removeProperty('--detail-h'); calendarDetail.classList.remove('is-opening','is-closing');}; calendarDetail.style.setProperty('--detail-h',calendarDetail.offsetHeight+'px'); calendarDetail.dataset.closing='1'; calendarDetail.classList.remove('is-opening'); calendarDetail.classList.add('is-closing'); if(getComputedStyle(calendarDetail).animationName==='none'){finishClose(); return;} var onCloseEnd=function(){calendarDetail.removeEventListener('animationend',onCloseEnd); finishClose();}; calendarDetail.addEventListener('animationend',onCloseEnd); setTimeout(onCloseEnd,260);}
 function openCalendarDay(cell,key,day){if(!calendarDetail)return; if(cell.classList.contains('is-open')){closeCalendarDetail(); return;} var items=tasksOn(key); if(!items.length)return; closeCalendarDetail(); cell.classList.add('is-open'); cell.setAttribute('aria-expanded','true'); var heading=document.createElement('h3'); heading.textContent=cursor.getFullYear()+'年'+(cursor.getMonth()+1)+'月'+day+'日 · '+items.length+' 件'; var list=document.createElement('ul'); items.forEach(function(task){var item=document.createElement('li'); var time=deadlineTime(task); if(time){var stamp=document.createElement('time'); stamp.textContent=time; item.appendChild(stamp);} item.appendChild(document.createTextNode(String(task.summary||task.text||'未命名任务'))); list.appendChild(item);}); calendarDetail.textContent=''; var head=document.createElement('div'); head.className='calendar-detail-head'; var close=document.createElement('button'); close.type='button'; close.className='calendar-detail-close'; close.textContent='关闭'; close.setAttribute('aria-label','关闭当天事项'); close.addEventListener('click',function(){closeCalendarDetail(); cell.focus();}); head.appendChild(heading); head.appendChild(close); calendarDetail.appendChild(head); calendarDetail.appendChild(list); calendarDetail.hidden=false; delete calendarDetail.dataset.closing; calendarDetail.classList.remove('is-closing','is-opening'); calendarDetail.style.removeProperty('--detail-h'); void calendarDetail.offsetWidth; calendarDetail.style.setProperty('--detail-h',calendarDetail.offsetHeight+'px'); calendarDetail.classList.add('is-opening'); var onOpenEnd=function(){calendarDetail.removeEventListener('animationend',onOpenEnd); calendarDetail.classList.remove('is-opening'); calendarDetail.style.removeProperty('--detail-h');}; calendarDetail.addEventListener('animationend',onOpenEnd);}
 function renderCalendar(direction){var root=document.getElementById('calendar'); if(!root)return; closeCalendarDetail(); root.textContent=''; var y=cursor.getFullYear(),m=cursor.getMonth(),first=new Date(y,m,1).getDay(),days=new Date(y,m+1,0).getDate(); document.getElementById('calendar-title').textContent='月历 · '+y+'年'+(m+1)+'月'; for(var i=0;i<first;i++)root.appendChild(document.createElement('div')); var now=new Date(),todayKey=dayKey(now.getFullYear(),now.getMonth(),now.getDate()); for(let d=1;d<=days;d++){let key=dayKey(y,m,d); var cell=document.createElement('div'); cell.className='calendar-day'; var items=tasksOn(key); if(key===todayKey)cell.classList.add('today'); var strong=document.createElement('strong'); strong.textContent=d; cell.appendChild(strong); if(items.length>=1){cell.setAttribute('data-has-events','true'); cell.setAttribute('tabindex','0'); cell.setAttribute('aria-expanded','false'); cell.setAttribute('aria-controls','calendar-detail'); cell.setAttribute('aria-label',(m+1)+'月'+d+'日 '+items.length+' 件待办，回车查看全部');} items.slice(0,MAX_CALENDAR_EVENTS).forEach(function(task){var chip=document.createElement('span'); chip.className='calendar-event'; var time=deadlineTime(task); var text=(time?time+' ':'')+String(task.summary||task.text||'未命名任务'); chip.textContent=text; chip.title=text+'（点日期格看当天全部事项）'; cell.appendChild(chip);}); if(items.length>MAX_CALENDAR_EVENTS){var more=document.createElement('span'); more.className='calendar-more'; var rest=items.length-MAX_CALENDAR_EVENTS; more.textContent='+'+rest; more.title='这天还有 '+rest+' 条，点日期格看当天全部'; cell.appendChild(more);} if(items.length){cell.addEventListener('click',function(){openCalendarDay(this,key,d);}); cell.addEventListener('keydown',function(event){if(event.key==='Enter'||event.key===' '){event.preventDefault(); openCalendarDay(this,key,d);} else if(event.key==='Escape'){closeCalendarDetail();}});} root.appendChild(cell);} if(direction){root.classList.remove('calendar-turn'); root.style.setProperty('--turn-x',direction>0?'18px':'-18px'); void root.offsetWidth; root.classList.add('calendar-turn'); window.setTimeout(function(){root.classList.remove('calendar-turn');},320);}}
 window.syncCalendarTasks=function(data){calendarTasks=[].concat(data.today||[],data.week||[],data.later||[],data.done||[],data.candidates||[]);renderCalendar();};
 document.getElementById('cal-prev').onclick=function(){cursor.setMonth(cursor.getMonth()-1);renderCalendar(-1);}; document.getElementById('cal-next').onclick=function(){cursor.setMonth(cursor.getMonth()+1);renderCalendar(1);}; renderCalendar();
  // 手机端（≤480px）默认折起月历：42px 宽的单日格放不下带时刻的日程，先只留一行「查看月历」。
  // 折叠只隐藏面板内部（星期行/网格/说明/当天详情），标题行与按钮始终在 —— 因为 #calendar-panel 会被
  // taskSurface.appendChild(calendarPanel) 整个搬进「待办」标签页，按钮必须在面板里面才不会被落下。
  (function(){var panel=document.getElementById('calendar-panel'), toggle=document.getElementById('calendar-toggle'); if(!panel||!toggle)return;
   function mobile(){return window.matchMedia('(max-width:480px)').matches;}
   function label(){var collapsed=panel.classList.contains('calendar-collapsed'); toggle.textContent=collapsed?'查看月历':'收起月历'; toggle.setAttribute('aria-expanded',collapsed?'false':'true');}
   function setCollapsed(collapsed){if(collapsed){panel.classList.add('calendar-collapsed'); closeCalendarDetail();}else{panel.classList.remove('calendar-collapsed');} label();}
   setCollapsed(mobile());
   toggle.addEventListener('click',function(){setCollapsed(!panel.classList.contains('calendar-collapsed'));});
  })();
})();

document.querySelectorAll('.tabs button[role="tab"]').forEach(function (button) {
  button.onclick = function () {
    var oldButton = document.querySelector('.tabs button.active');
    var oldTab = oldButton ? oldButton.getAttribute('data-tab') : '';
    var tab = button.getAttribute('data-tab');
    var order = ['tasks', 'notices', 'inbox', 'settings'];
    document.querySelectorAll('.tabs button[role="tab"]').forEach(function (other) {
      other.classList.remove('active');
      other.removeAttribute('aria-current');
      other.setAttribute('aria-selected', 'false');
      other.tabIndex = -1;
    });
    button.classList.add('active');
    button.setAttribute('aria-current', 'page');
    button.setAttribute('aria-selected', 'true');
    button.tabIndex = 0;
    order.forEach(function (name) {
      document.getElementById('tab-' + name).hidden = name !== tab;
    });
    if (oldTab !== tab) animateSurface(document.getElementById('tab-' + tab), order.indexOf(tab) > order.indexOf(oldTab) ? 16 : -16);
    if (tab === 'notices') loadNotices();
    if (tab === 'inbox') loadInboxTab();
    if (tab === 'settings') loadSettings();
  };
  button.onkeydown = function (event) {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabs button[role="tab"]'));
    var index = tabs.indexOf(button);
    var next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : -1;
    if (next >= 0) { event.preventDefault(); tabs[next].focus(); tabs[next].click(); }
  };
});

var taskSurface=document.getElementById('tab-tasks');
taskSurface.addEventListener('pointerover',function(event){if(!matchMedia('(hover:hover) and (pointer:fine)').matches)return;var card=event.target.closest('.task');if(card&&taskSurface.contains(card)&&!card.contains(event.relatedTarget))card.classList.add('is-focused');});
taskSurface.addEventListener('pointerout',function(event){var card=event.target.closest('.task');if(card&&(!event.relatedTarget||!card.contains(event.relatedTarget)))card.classList.remove('is-focused');});
taskSurface.appendChild(calendarPanel);
loadTasks();

// 置顶日程面板：手动拖高（用户希望把不要紧的 QQ 接入面板挤下去）。
function initPinnedResize(){
  var panel=document.getElementById('pinned-panel'), list=document.getElementById('pinned-list'), handle=document.getElementById('pinned-resize');
  if(!panel||!list||!handle)return;
  var storeKey='qq_digest_pinned_height', min=120, max=720, fallback=280;
  function value(){return parseInt(handle.getAttribute('aria-valuenow'),10)||fallback;}
  function apply(next,persist){var height=Math.max(min,Math.min(max,Math.round(next))); list.style.maxHeight=height+'px'; handle.setAttribute('aria-valuenow',String(height)); if(persist){try{localStorage.setItem(storeKey,String(height));}catch(error){}}}
  var saved=0; try{saved=parseInt(localStorage.getItem(storeKey)||'0',10)||0;}catch(error){saved=0;}
  apply(saved||fallback,false);
  var dragging=false, startY=0, startHeight=fallback;
  handle.addEventListener('pointerdown',function(event){if(event.pointerType==='mouse'&&event.button!==0)return; dragging=true; startY=event.clientY; startHeight=value(); panel.classList.add('is-resizing'); if(handle.setPointerCapture){try{handle.setPointerCapture(event.pointerId);}catch(error){}} event.preventDefault();});
  handle.addEventListener('pointermove',function(event){if(dragging)apply(startHeight+(event.clientY-startY),false);});
  function finish(event){if(!dragging)return; dragging=false; panel.classList.remove('is-resizing'); apply(value(),true); if(event&&handle.releasePointerCapture&&event.pointerId!==undefined){try{handle.releasePointerCapture(event.pointerId);}catch(error){}}}
  handle.addEventListener('pointerup',finish);
  handle.addEventListener('pointercancel',finish);
  handle.addEventListener('keydown',function(event){var step=event.shiftKey?64:24, next=null; if(event.key==='ArrowDown')next=value()+step; else if(event.key==='ArrowUp')next=value()-step; else if(event.key==='Home')next=min; else if(event.key==='End')next=max; if(next!==null){event.preventDefault(); apply(next,true);}});
  handle.addEventListener('dblclick',function(){apply(fallback,true);});
}

// 托管状态胶囊 + 新手引导：让「启动引擎 → 扫码登录 → 选群 → 订阅日历」这条路径自己找上门。
var journeyCalendarReady=false, journeyCalendarChecked=false;
function journeyDismissed(){try{return localStorage.getItem('qq_digest_journey_hidden')==='1';}catch(error){return false;}}
function applyHostingState(status){
  var pill=document.getElementById('host-state'), label=document.getElementById('host-state-label'), quick=document.getElementById('host-quick');
  if(!pill||!quick)return;
  var reachable=!!(status&&status.ok), active=!!(status&&status.hosting_active), running=!!(status&&status.napcat_running)||active, online=!!(status&&status.napcat_online);
  pill.setAttribute('data-state',reachable?(active?'active':(running?'idle':'offline')):'offline');
  if(label){
    if(!reachable)label.textContent='读不到本机服务';
    else if(active)label.textContent=online?'托管中':'托管已开启，等待扫码';
    else if(running)label.textContent=online?'已登录，未开启托管':'引擎已启动，未登录';
    else label.textContent='未托管';
  }
  quick.setAttribute('data-desired',active?'stop':'start');
  quick.textContent=active?'结束托管':'开始托管';
  quick.setAttribute('aria-label',active?'结束托管：停止引擎并恢复电脑版 QQ':'开始托管：启动引擎（按设置会先退出电脑版 QQ）');
  quick.disabled=!reachable;
}
function startHostingAction(desired,trigger){
  var quick=document.getElementById('host-quick');
  if(quick)quick.disabled=true;
  if(trigger&&trigger!==quick)trigger.disabled=true;
  api('/api/hosting/'+desired,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(function(result){
    if(result&&result.ok===false)throw new Error(result.error||'托管操作没有生效');
    showFeedback(desired==='start'?'已开始托管：引擎已启动，接下来用手机 QQ 扫码登录。':'已结束托管：引擎已停止。','success');
    return fetchHostingStatus();
  }).catch(function(error){
    showFeedback('托管操作失败：'+friendlyError((error&&error.message)||error),'error',function(){startHostingAction(desired,trigger);});
  }).then(function(){if(quick)quick.disabled=false; if(trigger&&trigger!==quick)trigger.disabled=false;});
}
function fetchHostingStatus(){
  return api('/api/hosting/status').then(function(status){
    applyHostingState(status);
    if(!journeyCalendarReady&&!journeyCalendarChecked&&!journeyDismissed()&&status&&status.napcat_online&&Number(status.groups_selected||0)>0){
      journeyCalendarChecked=true;
      api('/api/sync/info').then(function(info){journeyCalendarReady=!!(info&&Array.isArray(info.calendars)&&info.calendars.length); renderJourney(status);}).catch(function(){journeyCalendarChecked=false;});
    }
    renderJourney(status);
    return status;
  }).catch(function(){applyHostingState({ok:false}); renderJourney({ok:false}); return null;});
}
function highlightTarget(target){if(!target)return; target.classList.add('journey-target'); window.setTimeout(function(){target.classList.remove('journey-target');},2400); if(target.scrollIntoView)target.scrollIntoView({block:'center',behavior:'smooth'});}
function showSettingsTab(){var button=document.getElementById('tab-settings-button'); if(button)button.click();}
function renderJourney(status){
  var box=document.getElementById('journey'); if(!box)return;
  if(journeyDismissed()){box.hidden=true; return;}
  var reachable=!!(status&&status.ok);
  var steps=[
    {label:'启动引擎',hint:'让 NapCat 代为收发群消息',done:reachable&&(!!status.hosting_active||!!status.napcat_running)},
    {label:'扫码登录',hint:'用手机 QQ 扫二维码',done:reachable&&!!status.napcat_online},
    {label:'选择订阅群',hint:'勾选要转成待办的群',done:reachable&&Number(status.groups_selected||0)>0},
    {label:'订阅日历',hint:'把 .ics 地址加到手机日历',done:journeyCalendarReady}
  ];
  var current=-1;
  steps.forEach(function(step,index){if(current<0&&!step.done)current=index;});
  if(current<0){box.hidden=true; return;}
  box.hidden=false;
  var list=document.getElementById('journey-steps');
  if(list){
    list.textContent='';
    steps.forEach(function(step,index){
      var item=document.createElement('li');
      item.className='journey-step';
      item.dataset.state=step.done?'done':(index===current?'current':'todo');
      var badge=document.createElement('b'); badge.textContent=step.done?'✓':String(index+1);
      var body=document.createElement('span'); body.appendChild(document.createTextNode(step.label));
      var hint=document.createElement('small'); hint.textContent=step.hint; body.appendChild(hint);
      item.appendChild(badge); item.appendChild(body); list.appendChild(item);
    });
  }
  var titles=['先启动引擎，QQ 消息才能被接手','用手机 QQ 扫码登录','挑出要订阅的群','把日历订阅到手机'];
  var texts=[
    '点顶栏「开始托管」启动引擎（按设置会先退出电脑版 QQ，随时可以结束托管恢复）。',
    '用手机 QQ 扫描右栏「QQ 接入」里的二维码；二维码每 5 秒自动刷新。',
    '在右栏「订阅群」里勾选要变成待办的群，然后点「保存订阅」。',
    '在「设置 → iPhone 日历订阅」里选一个地址，用手机相机扫二维码，或复制链接到手机日历订阅。'
  ];
  var labels=['开始托管','去扫码','去选群','去订阅'];
  var title=document.getElementById('journey-title'); if(title)title.textContent='第 '+(current+1)+' 步 · '+titles[current];
  var next=document.getElementById('journey-next'); if(next)next.textContent=texts[current];
  var action=document.getElementById('journey-action');
  if(action){action.textContent=labels[current]; action.dataset.step=String(current);}
}
function initJourney(){
  var action=document.getElementById('journey-action'), dismiss=document.getElementById('journey-dismiss'), pill=document.getElementById('host-state'), quick=document.getElementById('host-quick');
  if(action){
    action.onclick=function(){
      var step=action.dataset.step;
      if(step==='0'){startHostingAction('start',action); return;}
      if(step==='1'){highlightTarget(document.getElementById('qrbox')); return;}
      if(step==='2'){highlightTarget(document.getElementById('groupbox')); return;}
      if(step==='3'){showSettingsTab(); window.setTimeout(function(){highlightTarget(document.getElementById('sync-url'));},160); return;}
      showSettingsTab();
    };
  }
  if(dismiss)dismiss.onclick=function(){try{localStorage.setItem('qq_digest_journey_hidden','1');}catch(error){} var box=document.getElementById('journey'); if(box)box.hidden=true;};
  if(pill)pill.onclick=function(){try{localStorage.removeItem('qq_digest_journey_hidden');}catch(error){} fetchHostingStatus();};
  if(quick)quick.onclick=function(){startHostingAction(quick.getAttribute('data-desired')==='stop'?'stop':'start',quick);};
  var brand=document.getElementById('brand-home');
  if(brand)brand.addEventListener('click',function(event){
    if(event.metaKey||event.ctrlKey||event.shiftKey||event.altKey||event.button!==0)return;
    event.preventDefault();
    try{localStorage.removeItem('qq_digest_journey_hidden');}catch(error){}
    window.scrollTo({top:0,behavior:motionPreferencePaused?'auto':'smooth'});
    fetchHostingStatus();
    loadTasks();
    showFeedback('已回到顶部，并重新显示「开始使用」引导。','');
  });
}
/* ---- 右栏三块面板：本周小结（P2）/ 快捷操作（P3）/ 服务自检（P4）----
   数据都来自已经存在的接口，按钮只做页面本来就能做的事，没有摆设。 */
var latestTasks = null;

function railCollectTasks(data) {
  var seen = {}, list = [];
  ['today', 'week', 'later', 'done', 'candidates'].forEach(function (key) {
    (data && data[key] || []).forEach(function (task) {
      if (task && task.id && !seen[task.id]) { seen[task.id] = 1; list.push(task); }
    });
  });
  return list;
}

function railParseDate(value) {
  var parsed = new Date(String(value || '').replace(' ', 'T'));
  return isNaN(parsed.getTime()) ? null : parsed;
}

function railAgo(stamp) {
  var minutes = Math.max(0, Math.round((Date.now() - stamp) / 60000));
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return minutes + ' 分钟前';
  if (minutes < 1440) return Math.round(minutes / 60) + ' 小时前';
  return Math.round(minutes / 1440) + ' 天前';
}

function renderRailSummary(data) {
  latestTasks = data || null;
  if (!data || !document.getElementById('rail-summary')) return;
  var stats = data.stats || {};
  [['rail-stat-open', stats.open], ['rail-stat-done', stats.done], ['rail-stat-overdue', stats.overdue]].forEach(function (pair) {
    var node = document.getElementById(pair[0]);
    if (node) node.textContent = pair[1] == null ? '–' : String(pair[1]);
  });
  var today = new Date(); today.setHours(0, 0, 0, 0);
  var monday = new Date(today); monday.setDate(today.getDate() - ((today.getDay() + 6) % 7));
  var counts = [0, 0, 0, 0, 0, 0, 0];
  railCollectTasks(data).forEach(function (task) {
    if (task.done) return;
    var deadline = railParseDate(task.deadline);
    if (!deadline) return;
    var day = new Date(deadline); day.setHours(0, 0, 0, 0);
    var offset = Math.round((day - monday) / 86400000);
    if (offset >= 0 && offset < 7) counts[offset] += 1;
  });
  var todayNode = document.getElementById('rail-bars-today');
  if (todayNode) todayNode.textContent = String(counts[(today.getDay() + 6) % 7]);
  var host = document.getElementById('rail-bars');
  if (!host) return;
  var names = ['一', '二', '三', '四', '五', '六', '日'];
  var max = Math.max.apply(null, counts.concat([1]));
  host.innerHTML = '';
  counts.forEach(function (count, index) {
    var day = new Date(monday); day.setDate(monday.getDate() + index);
    var column = document.createElement('div');
    column.className = 'rail-bar' + (day.getTime() === today.getTime() ? ' is-today' : '');
    column.title = (day.getMonth() + 1) + '月' + day.getDate() + '日 · ' + count + ' 条到期';
    var value = document.createElement('span'); value.className = 'rail-bar-value'; value.textContent = count ? String(count) : '';
    var track = document.createElement('div'); track.className = 'rail-bar-fill'; track.style.height = Math.round((count / max) * 68) + 'px';
    var label = document.createElement('span'); label.textContent = names[index];
    column.appendChild(value); column.appendChild(track); column.appendChild(label);
    host.appendChild(column);
  });
}

function railNote(message, state) {
  var note = document.getElementById('rail-actions-note');
  if (note) { note.textContent = message; note.dataset.state = state || ''; }
}

function railShowNewForm(open) {
  var form = document.getElementById('rail-new-form');
  var toggle = document.getElementById('rail-new-task');
  if (!form) return;
  if (form.dataset.pending) { clearTimeout(Number(form.dataset.pending)); delete form.dataset.pending; }
  if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) {
    form.hidden = false;
    form.dataset.state = 'entering';
    form.dataset.pending = String(setTimeout(function () { form.dataset.state = ''; delete form.dataset.pending; }, 220));
    var field = document.getElementById('rail-new-summary');
    if (field) field.focus();
    return;
  }
  if (form.hidden) { form.dataset.state = ''; return; }
  form.dataset.state = 'exiting';
  if (getComputedStyle(form).animationName === 'none') {
    form.dataset.state = '';
    form.hidden = true;
    form.reset();
    return;
  }
  form.dataset.pending = String(setTimeout(function () {
    form.dataset.state = '';
    form.hidden = true;
    form.reset();
    delete form.dataset.pending;
  }, 180));
}

function railCreateTask() {
  if (!latestTasks) { railNote('还没读到待办列表，请稍后再试。', 'warn'); return; }
  var field = document.getElementById('rail-new-summary');
  var when = document.getElementById('rail-new-deadline');
  var save = document.getElementById('rail-new-save');
  var summary = field ? field.value.trim() : '';
  if (!summary) { railNote('请先填写待办内容。', 'warn'); if (field) field.focus(); return; }
  var deadline = when && when.value ? when.value : '';
  if (save) save.disabled = true;
  railNote('正在保存…', '');
  api('/api/tasks/create', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({summary: summary, deadline: deadline})
  }).then(function (result) {
    if (!result.ok) throw new Error(result.error || '新建待办失败');
    if (field) field.value = '';
    if (when) when.value = '';
    railShowNewForm(false);
    railNote('已新建：' + result.summary + (result.deadline ? '（' + String(result.deadline).slice(5, 16).replace('T', ' ') + ' 截止）' : '（没有截止时间，先放在「以后」）'), 'ok');
    return syncTasks();
  }).catch(function (error) {
    railNote('新建失败：' + error.message, 'error');
  }).then(function () {
    if (save) save.disabled = false;
  });
}

function railCopyToday() {
  var tasks = (latestTasks && latestTasks.today) || [];
  if (!tasks.length) { railNote('今天还没有待办。', 'warn'); return; }
  var text = tasks.map(function (task) {
    var when = task.deadline_text || (task.deadline ? String(task.deadline).slice(5, 16).replace('T', ' ') : '无截止时间');
    return '[' + when + '] ' + (task.summary || '未命名任务');
  }).join('\\n');
  function success() { railNote('已复制 ' + tasks.length + ' 条今日待办。', 'ok'); }
  function fallback() {
    var area = document.createElement('textarea');
    area.value = text; area.setAttribute('readonly', 'readonly');
    area.style.position = 'fixed'; area.style.top = '-1000px';
    document.body.appendChild(area); area.select();
    var copied = false;
    try { copied = document.execCommand('copy'); } catch (error) { copied = false; }
    document.body.removeChild(area);
    if (copied) success();
    else railNote('这个浏览器不让我们写剪贴板，请手动选中待办文字复制。', 'error');
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(success, fallback);
    return;
  }
  fallback();
}

function railExportIcs() {
  var link = document.createElement('a');
  link.href = '/calendar.ics' + (token ? '?token=' + encodeURIComponent(token) : '');
  link.download = 'qq-tasks.ics';
  document.body.appendChild(link); link.click(); document.body.removeChild(link);
  railNote('已开始下载 qq-tasks.ics，双击导入系统日历即可。', 'ok');
}

function railShowSubscription() {
  showSettingsTab();
  var attempts = 0;
  function reveal() {
    var target = document.getElementById('sync-url') || document.getElementById('sync-qr');
    if (target && target.scrollIntoView) {
      target.scrollIntoView({block: 'center', behavior: motionPreferencePaused ? 'auto' : 'smooth'});
      if (target.focus) target.focus({preventScroll: true});
      railNote('已跳到设置里的订阅二维码。', 'ok');
      return;
    }
    if (++attempts < 12) window.setTimeout(reveal, 200);
    else railNote('订阅卡片还没渲染出来，请点上方「设置」查看。', 'warn');
  }
  window.setTimeout(reveal, 120);
}

function railSyncMotionLabel() {
  var button = document.getElementById('rail-motion');
  if (!button) return;
  if (reducedMotionQuery.matches) {
    button.disabled = true;
    button.textContent = '系统已开启减弱动效';
    button.setAttribute('aria-pressed', 'true');
    return;
  }
  button.disabled = false;
  button.textContent = motionPreferencePaused ? '恢复动态效果' : '暂停动态效果';
  button.setAttribute('aria-pressed', motionPreferencePaused ? 'true' : 'false');
}

function railToggleMotion() {
  setMotionPreference(motionPreferencePaused);
  railSyncMotionLabel();
  railNote(motionPreferencePaused ? '已暂停页面动画。' : '已恢复页面动画。', 'ok');
}

function railSoftApi(path) {
  return api(path).catch(function (error) { return {error: (error && error.message) || String(error)}; });
}

function renderRailHealth() {
  var list = document.getElementById('rail-health-list');
  var message = document.getElementById('rail-health-message');
  if (!list) return;
  list.innerHTML = '<li class="health-row" data-state="pending"><span class="health-dot"></span><span class="health-label">正在检测…</span></li>';
  if (message) message.textContent = '正在检测…';
  Promise.all([railSoftApi('/api/napcat/status'), railSoftApi('/api/hosting/status'), railSoftApi('/api/sync/info'), railSoftApi('/api/health')]).then(function (results) {
    var napcat = results[0] || {}, hosting = results[1] || {}, sync = results[2] || {}, health = results[3] || {};
    var latest = 0;
    railCollectTasks(latestTasks).forEach(function (task) {
      var stamp = railParseDate(task.updated_at);
      if (stamp && stamp.getTime() > latest) latest = stamp.getTime();
    });
    var rows = [];
    rows.push(napcat.online
      ? {state: 'ok', label: 'QQ 登录', value: '已登录 · ' + (napcat.nickname || napcat.user_id || ''), hint: ''}
      : {state: 'bad', label: 'QQ 登录', value: '未登录', hint: '点下面「QQ 接入」里的「一键接入」，用手机 QQ 扫码；没登录就收不到消息。'});
    rows.push(hosting.hosting_active
      ? {state: 'ok', label: '引擎托管', value: '托管中 · ' + Number(hosting.groups_selected || 0) + ' 个群', hint: ''}
      : {state: 'warn', label: '引擎托管', value: '未开启', hint: '不开始托管就不会自动收群消息；点「开始托管」并保持 QQ 在线。'});
    var publicAddress = (sync.calendars || []).filter(function (item) { return item.kind === 'public'; })[0];
    rows.push(publicAddress
      ? {state: 'ok', label: '公网日历', value: '可访问 · ' + Number(sync.events || publicAddress.events || 0) + ' 条事项', hint: ''}
      : {state: 'warn', label: '公网日历', value: '只有局域网地址', hint: '手机用流量打不开局域网地址；装有 Tailscale 并开 Funnel 后这里会出现公网地址。'});
    var stale = latest && (Date.now() - latest) >= 12 * 3600000;
    rows.push(latest
      ? {state: stale ? 'warn' : 'ok', label: '最近同步', value: railAgo(latest), hint: stale ? '半天没有更新了，确认 QQ 还在线、托管还开着。' : ''}
      : {state: 'warn', label: '最近同步', value: '暂无记录', hint: '还没有收到群消息，先确认 QQ 登录与托管状态。'});
    rows.push(health.ok
      ? {state: 'ok', label: '本机服务', value: '正常', hint: ''}
      : {state: 'bad', label: '本机服务', value: '没有响应', hint: '重启 QQ-Notice-Hub.exe（托盘图标右键 → 退出，再双击打开）。'});
    var channels = Array.isArray(health.channels) ? health.channels : [];
    rows.push(channels.length
      ? {state: 'ok', label: '消息推送', value: channels.join('、'), hint: ''}
      : {state: 'warn', label: '消息推送', value: '未配置', hint: '截止提醒现在发不出去。到「设置 → 提醒怎么送到手机」里挑一种填好，或者改用手机日历订阅。'});
    list.innerHTML = '';
    rows.forEach(function (row) {
      var item = document.createElement('li');
      item.className = 'health-row';
      item.dataset.state = row.state;
      var dot = document.createElement('span'); dot.className = 'health-dot';
      var label = document.createElement('span'); label.className = 'health-label'; label.textContent = row.label;
      var value = document.createElement('span'); value.className = 'health-value'; value.textContent = row.value;
      item.appendChild(dot); item.appendChild(label); item.appendChild(value);
      if (row.hint) {
        var hint = document.createElement('span'); hint.className = 'health-hint'; hint.textContent = row.hint;
        item.appendChild(hint);
      }
      if (row.label === '消息推送' && row.state !== 'ok') { var settingsButton = document.createElement('button'); settingsButton.type = 'button'; settingsButton.className = 'btn btn-small'; settingsButton.textContent = '去设置'; settingsButton.onclick = showSettingsTab; item.appendChild(settingsButton); }
      list.appendChild(item);
    });
    var bad = rows.filter(function (row) { return row.state !== 'ok'; }).length;
    if (message) message.textContent = bad ? '有 ' + bad + ' 项需要留意，下面标了怎么修。' : '全部正常。';
  });
}

function initRailPanels() {
  var bindings = [['rail-copy-today', railCopyToday], ['rail-export-ics', railExportIcs], ['rail-show-qr', railShowSubscription], ['rail-motion', railToggleMotion], ['rail-recheck', renderRailHealth], ['rail-settings', showSettingsTab]];
  bindings.forEach(function (pair) {
    var node = document.getElementById(pair[0]);
    if (node) node.onclick = pair[1];
  });
  var newTask = document.getElementById('rail-new-task');
  if (newTask) newTask.onclick = function () { railShowNewForm(document.getElementById('rail-new-form').hidden); };
  var newForm = document.getElementById('rail-new-form');
  if (newForm) {
    newForm.onsubmit = function (event) { event.preventDefault(); railCreateTask(); };
    newForm.onkeydown = function (event) {
      if (event.key === 'Escape') { event.preventDefault(); railShowNewForm(false); if (newTask) newTask.focus(); }
    };
  }
  var newCancel = document.getElementById('rail-new-cancel');
  if (newCancel) newCancel.onclick = function () { railShowNewForm(false); if (newTask) newTask.focus(); };
  var top = document.getElementById('rail-top');
  if (top) top.onclick = function () { window.scrollTo({top: 0, behavior: motionPreferencePaused ? 'auto' : 'smooth'}); };
  railSyncMotionLabel();
  renderRailHealth();
}

initPinnedResize();
initJourney();
initRailPanels();
fetchHostingStatus();
window.setInterval(fetchHostingStatus, 10000);
setInterval(loadTasks, 60000);
</script>
</body>
</html>
"""


def _deadline_label(value: dt.datetime | None, now: dt.datetime) -> tuple[str, bool]:
    if not isinstance(value, dt.datetime):
        return "", False
    overdue = value < now
    if value.date() == now.date():
        return (f"今天 {value:%H:%M} 已过" if overdue else f"今天 {value:%H:%M} 截止"), overdue
    if value.date() == now.date() + dt.timedelta(days=1):
        return f"明天 {value:%H:%M} 截止", False
    return f"{value:%m-%d %H:%M} 截止", overdue and value.date() < now.date()


def _confidence_text(task: dict[str, Any]) -> str:
    confidence = float(task.get("confidence") or 0.0)
    if confidence >= 0.75:
        level = "把握较高"
    elif confidence >= 0.55:
        level = "把握中等"
    else:
        level = "把握较低"
    return f"{level} {round(confidence * 100)}%"


def snooze_default_until(settings: Settings, now: dt.datetime) -> str:
    """稍后提醒的默认时间：次日早晨的截止提醒时刻，否则次日同一时间。"""
    target = now + dt.timedelta(days=1)
    if settings.deadline_reminders_enabled:
        try:
            hour, minute = (int(part) for part in str(settings.deadline_morning).split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 7, 30
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return iso(target)


def group_tasks(tasks: list[dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    candidate: list[dict[str, Any]] = []
    today: list[dict[str, Any]] = []
    week: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    horizon = now + dt.timedelta(days=7)
    for task in sorted(tasks, key=lambda item: not effective_urgent(item)):
        status = str(task.get("status") or "open")
        if status in {"dismissed", "expired"}:
            continue
        deadline = parse_iso(task.get("deadline"))
        payload = dict(task)
        payload["effective_urgent"] = effective_urgent(task)
        payload["deadline_text"], payload["overdue"] = _deadline_label(deadline, now)
        snooze_until = parse_iso(payload.get("snooze_until"))
        payload["snoozed"] = bool(isinstance(snooze_until, dt.datetime) and snooze_until > now)
        payload["snooze_text"] = (
            snooze_until.strftime("%m-%d %H:%M") if payload["snoozed"] and snooze_until else ""
        )
        payload["done"] = status == "done"
        if status == "candidate":
            payload["confidence_text"] = _confidence_text(payload)
            detail = str(payload.get("candidate_detail") or "")
            try:
                parsed = json.loads(detail) if detail else {}
            except json.JSONDecodeError:
                parsed = {}
            payload["confidence_reason"] = str(parsed.get("reason") or "需要你确认后再进入正式待办。")
            candidate.append(payload)
            continue
        if payload["done"]:
            done.append(payload)
            continue
        if deadline is None:
            later.append(payload)
        elif deadline.date() <= now.date():
            today.append(payload)
        elif deadline <= horizon:
            week.append(payload)
        else:
            later.append(payload)
    done.sort(key=lambda item: str(item.get("done_at") or ""), reverse=True)
    return {
        "candidates": candidate[:40],
        "today": today[:40],
        "week": week[:40],
        "later": later[:40],
        "done": done[:20],
    }


def overview(tasks: list[dict[str, Any]], grouped: dict[str, Any], now: dt.datetime) -> dict[str, Any]:
    open_tasks = [task for task in tasks if str(task.get("status") or "open") == "open"]
    candidates = grouped.get("candidates") or []
    done_count = sum(1 for task in tasks if str(task.get("status") or "") == "done")
    today = grouped["today"]
    if today:
        nearest = min(
            (parse_iso(task.get("deadline")) for task in today if parse_iso(task.get("deadline"))),
            default=None,
        )
        overdue = sum(1 for task in today if task.get("overdue"))
        if overdue:
            headline = f"今天 {len(today)} 件 · 逾期 {overdue} 件"
        elif nearest:
            headline = f"今天 {len(today)} 件，最近一件 {nearest:%H:%M} 截止"
        else:
            headline = f"今天 {len(today)} 件"
    elif candidates:
        headline = f"{len(candidates)} 条通知待确认"
    elif open_tasks:
        headline = "今天没有到期的事"
    else:
        headline = "所有待办都清空了"
    if any(task.get("overdue") for task in today):
        subline = "逾期事项已收起，可展开后查看"
    elif today:
        subline = "按截止时间从上到下处理"
    elif candidates:
        subline = "确认后才会进入正式待办"
    elif open_tasks:
        subline = "当前没有到期事项，可按自己的节奏推进"
    else:
        subline = "可以休息一下，新的通知会自动汇总"
    actionable = len(open_tasks) + len(candidates) + done_count
    progress = round(done_count * 100 / actionable) if actionable else 0
    return {"headline": headline, "subline": subline, "progress": progress}


# 设置页可写的推送通道键（前端字段名 → .env 变量名）。
_PUSH_SETTING_ENV_KEYS = {
    "wxpusher_app_token": "WXPUSHER_APP_TOKEN",
    "wxpusher_uids": "WXPUSHER_UIDS",
    "wxpusher_topic_ids": "WXPUSHER_TOPIC_IDS",
    "serverchan_keys": "SERVERCHAN_KEYS",
    "pushplus_tokens": "PUSHPLUS_TOKENS",
    "webhook_urls": "QQ_DIGEST_WEBHOOKS",
}
_PUSH_SETTING_LABELS = {
    "wxpusher_app_token": "WxPusher App Token",
    "wxpusher_uids": "WxPusher UID",
    "wxpusher_topic_ids": "WxPusher Topic ID",
    "serverchan_keys": "Server 酱 SendKey",
    "pushplus_tokens": "PushPlus Token",
    "webhook_urls": "Webhook 地址",
}
# 每个通道「去哪里拿值」的官网入口。链接由页面用 JS 填进 <a>，
# 页面本身不写外链（保持离线可用、也避免把地址写死在 HTML 里）。
PUSH_HELP_LINKS = {
    "wxpusher": "https://wxpusher.zjiecode.com/admin/",
    "serverchan": "https://sct.ftqq.com/sendkey",
    "pushplus": "https://www.pushplus.plus/",
}


def _mask_push_secret(value: str) -> str:
    """只暴露尾 4 位，其余打码（短值全码）。"""
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]


def _push_setting_values(payload: dict[str, Any], keys: set[str]) -> tuple[dict[str, str], dict[str, Any]]:
    """校验设置页提交的推送通道配置。

    返回 (写进 .env 的键值, 写回内存 Settings 的字段值)。任何一项非法就整体拒绝，
    调用方拿到 ValueError 后原样返回给页面，不做部分写入。
    """
    updates: dict[str, str] = {}
    applied: dict[str, Any] = {}
    for name in sorted(keys):
        raw = payload.get(name)
        if raw is None or isinstance(raw, (dict, bool, int, float)):
            raise ValueError(f"{_PUSH_SETTING_LABELS[name]}格式不正确")
        items = split_list(raw)
        if name == "wxpusher_app_token":
            if len(items) > 1:
                raise ValueError("WxPusher App Token 只能填一个，不要用逗号或空格分隔")
            value: Any = items[0] if items else ""
        elif name == "wxpusher_topic_ids":
            if any(not item.isdigit() or int(item) <= 0 for item in items):
                raise ValueError("WxPusher Topic ID 只能是正整数")
            value = tuple(int(item) for item in items)
        elif name == "webhook_urls":
            for item in items:
                if not item.lower().startswith(("http://", "https://")):
                    raise ValueError("Webhook 地址必须以 http:// 或 https:// 开头")
            value = items
        else:
            value = items
        if isinstance(value, str):
            updates[_PUSH_SETTING_ENV_KEYS[name]] = value
        else:
            updates[_PUSH_SETTING_ENV_KEYS[name]] = ",".join(str(item) for item in value)
        applied[name] = value
    return updates, applied


def _push_setting_state(settings: Settings) -> dict[str, Any]:
    """回给页面的脱敏状态：只给是否配置、数量与尾 4 位。"""
    state: dict[str, Any] = {}
    for name in _PUSH_SETTING_ENV_KEYS:
        value = getattr(settings, name, None)
        items = [str(item) for item in value] if isinstance(value, (tuple, list)) else ([str(value)] if value else [])
        state[name] = {
            "set": bool(items),
            "count": len(items),
            "masked": [_mask_push_secret(item) for item in items[:3]],
        }
    return state


# 设置页可写的「AI 摘要」键（前端字段名 → .env 变量名）。
_LLM_SETTING_ENV_KEYS = {
    "api_key": "QQ_DIGEST_LLM_API_KEY",
    "endpoint": "QQ_DIGEST_LLM_ENDPOINT",
    "model": "QQ_DIGEST_LLM_MODEL",
}

# 预设服务商：用户只选名字，接口地址与模型名由这里替他填好。
# needs_key 为假表示不用钥匙（本机模型）；vision 为真表示这家能看图片。
LLM_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "name": "DeepSeek（推荐，最便宜）",
        "hint": "按量付费：输入约 ¥1、输出约 ¥4 每百万字，一个月通常不到一元钱；能读图片。",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-flash",
        "vision_model": "deepseek-flash",
        "key_url": "https://platform.deepseek.com/api_keys",
        "key_steps": [
            "打开 platform.deepseek.com，用手机号注册并登录",
            "左侧点「API keys」，再点「创建 API key」",
            "复制弹出的那一串 sk- 开头的字符，粘到下面的「钥匙」里",
        ],
        "needs_key": True,
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM（免费）",
        "hint": "glm-4.7-flash 和能看图的 glm-4.6v-flash 都是 ¥0，长期免费，注册就能用。",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4.7-flash",
        "vision_model": "glm-4.6v-flash",
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "key_steps": [
            "打开 open.bigmodel.cn，用手机号注册并登录",
            "进「API Keys」页面，复制默认那串钥匙",
            "粘到下面；模型名已经替你填好了",
        ],
        "needs_key": True,
    },
    {
        "id": "aliyun_bailian",
        "name": "阿里云百炼（通义千问，送 100 万字）",
        "hint": "新人送 100 万字额度（90 天内有效，仅北京地域），之后约 ¥0.8/百万字；读图能力最好。",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen3.8-flash",
        "vision_model": "qwen3.8-flash",
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "key_steps": [
            "打开阿里云百炼控制台，用支付宝或淘宝账号登录",
            "点右上角「API-KEY」→「创建我的 API-KEY」",
            "复制 sk- 开头的字符，粘到下面",
        ],
        "needs_key": True,
    },
    {
        "id": "siliconflow",
        "name": "硅基流动（免费模型）",
        "hint": "模型广场里有标价 ¥0 的模型（GLM-5.3-Flash），免费还能读图片。",
        "endpoint": "https://api.siliconflow.cn/v1/chat/completions",
        "model": "zai-org/GLM-5.3-Flash",
        "vision_model": "zai-org/GLM-5.3-Flash",
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "key_steps": [
            "打开 cloud.siliconflow.cn，注册并登录",
            "左侧「API 密钥」→「新建 API 密钥」",
            "复制 sk- 开头的字符，粘到下面",
        ],
        "needs_key": True,
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Kimi",
        "hint": "按量付费（约 ¥1.1/百万字输入），中文长文本理解好。",
        "endpoint": "https://api.moonshot.cn/v1/chat/completions",
        "model": "kimi-k2.6",
        "vision_model": "kimi-k2.6",
        "key_url": "https://platform.kimi.com/",
        "key_steps": [
            "打开 platform.kimi.com，注册并登录",
            "进「API Key 管理」，新建一个",
            "复制 sk- 开头的字符，粘到下面",
        ],
        "needs_key": True,
    },
    {
        "id": "ollama",
        "name": "本机跑的 Ollama（免费，但要显卡）",
        "hint": "完全免费、断网也能用，只花电费；要有显卡，速度比云服务慢。",
        "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
        "model": "qwen3:8b",
        "vision_model": "",
        "key_url": "https://ollama.com/download",
        "key_steps": [
            "先装 Ollama（点下面链接），装完在命令行执行 ollama pull qwen3:8b",
            "本机模型不用钥匙，直接点「保存并测试」",
        ],
        "needs_key": False,
    },
    {
        "id": "custom",
        "name": "其它（自己填接口地址）",
        "hint": "任何大模型服务都行，只要它给你的地址以 /v1/chat/completions 结尾。",
        "endpoint": "",
        "model": "",
        "vision_model": "",
        "key_url": "",
        "key_steps": [],
        "needs_key": True,
    },
    {
        "id": "off",
        "name": "不接 AI（只记录原始消息）",
        "hint": "不调用任何模型，群里消息只归档、不生成摘要。",
        "endpoint": "",
        "model": "",
        "vision_model": "",
        "key_url": "",
        "key_steps": [],
        "needs_key": False,
    },
]
_LLM_PROVIDER_BY_ID = {str(item["id"]): item for item in LLM_PROVIDERS}


def _llm_provider(provider_id: str) -> dict[str, Any] | None:
    return _LLM_PROVIDER_BY_ID.get(str(provider_id))


def _llm_provider_id(settings: Settings) -> str:
    """按当前接口地址反推用户选的是哪一家；认不出来就是 custom。"""
    endpoint = str(settings.dashscope_endpoint or "").strip().lower().rstrip("/")
    for item in LLM_PROVIDERS:
        preset = str(item.get("endpoint") or "").strip().lower().rstrip("/")
        if preset and preset == endpoint:
            return str(item["id"])
    return "custom"


def _llm_setting_state(settings: Settings) -> dict[str, Any]:
    """回给页面的脱敏状态：当前选的哪家、模型名、key 只给尾 4 位。"""
    key = str(settings.dashscope_api_key or "")
    return {
        "enabled": bool(settings.llm_enabled),
        "active": bool(settings.llm_active),
        "provider": _llm_provider_id(settings),
        "endpoint": str(settings.dashscope_endpoint or ""),
        "model": str(settings.dashscope_model or ""),
        "vision": bool(settings.vision_active),
        "vision_model": str(settings.vision_model or ""),
        "key_set": bool(key),
        "key_masked": _mask_push_secret(key) if key else "",
        "timeout": int(settings.llm_timeout or 60),
    }


def _llm_setting_updates(payload: dict[str, Any], settings: Settings) -> tuple[dict[str, str], dict[str, Any]]:
    """校验设置页提交的 AI 接入配置，返回 (.env 更新, 写回内存的字段)。"""
    provider_id = str(payload.get("llm_provider") or "").strip()
    provider = _llm_provider(provider_id)
    if provider is None:
        raise ValueError("请先选择一个 AI 服务商")
    raw_key = payload.get("llm_api_key")
    if raw_key is not None and isinstance(raw_key, (dict, bool, int, float)):
        raise ValueError("API Key 格式不正确")
    api_key = str(raw_key or "").strip()
    if provider_id == "off":
        return {"QQ_DIGEST_LLM": "0"}, {"llm_enabled": False}
    if provider.get("needs_key") and not (api_key or str(settings.dashscope_api_key or "").strip()):
        raise ValueError("还没填 API Key：点上面的链接拿到以后粘进来就行")
    endpoint = str(payload.get("llm_endpoint") or provider.get("endpoint") or "").strip()
    if not endpoint.lower().startswith(("http://", "https://")):
        raise ValueError("接口地址要以 http:// 或 https:// 开头")
    model = str(payload.get("llm_model") or provider.get("model") or "").strip()
    if not model:
        raise ValueError("还没填模型名：选好服务商以后它应该自动填上")
    vision_model = str(provider.get("vision_model") or "")
    updates = {
        "QQ_DIGEST_LLM": "1",
        "QQ_DIGEST_LLM_ENDPOINT": endpoint,
        "QQ_DIGEST_LLM_MODEL": model,
        "QQ_DIGEST_VL_MODEL": vision_model,
    }
    applied: dict[str, Any] = {
        "llm_enabled": True,
        "dashscope_endpoint": endpoint,
        "dashscope_model": model,
        "vision_model": vision_model or DEFAULT_VISION_MODEL,
    }
    if api_key:
        updates["QQ_DIGEST_LLM_API_KEY"] = api_key
        applied["dashscope_api_key"] = api_key
    return updates, applied


def _llm_test_error(error: Exception) -> str:
    """把各家服务商的报错翻译成用户看得懂的一句话。"""
    text = str(error) or error.__class__.__name__
    low = text.lower()
    if "dashscope_api_key" in text or "未配置" in text:
        return "还没有填钥匙：把服务商给你的那串 API Key 粘进去再试。"
    if "401" in low or "unauthorized" in low or "invalid api key" in low or "authentication" in low:
        return "钥匙不对或已经失效：回到拿钥匙的那个页面重新复制一次。"
    if "403" in low or "forbidden" in low:
        return "服务商拒绝了这次请求：确认钥匙有没有被停用，或者账号还没实名。"
    if "404" in low or ("model" in low and "not found" in low):
        return "服务商说不认识这个模型名：把模型名改回默认的试试。"
    if "429" in low or "rate limit" in low or "quota" in low or "insufficient" in low:
        return "额度用完或被限流了：换一个钥匙，或者过一会儿再试。"
    if "timed out" in low or "timeout" in low:
        return "等太久没回应：检查一下网络，本机 Ollama 的话确认模型已经下载完。"
    if "connection refused" in low or "10061" in text:
        return "连不上这个地址：本机 Ollama 要先启动，换服务商的话确认地址有没有写对。"
    return text[:200]


class _Handler(BaseHTTPRequestHandler):
    server_version = "qq-tasks/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 与基类签名一致
        LOGGER.debug("web %s", format % args)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect_home(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        location = "/" + ("?" + query if query else "")
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _ics(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        # 页面里的「测试地址」按钮要从浏览器直接 fetch 这个订阅地址（跨源），
        # 没有这个头浏览器会直接拦掉并报 Failed to fetch，让人误以为订阅地址坏了。
        # 地址本身自带令牌，放开读取范围不会多给出任何东西。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _png(self, raw: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _jpeg(self, raw: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(raw)

    def _asset(self, body: str, content_type: str, *, cache: str = "no-store") -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", cache)
        if content_type.startswith("text/javascript"):
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "token", "") or "")
        if not token:
            return True
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if token in params.get("token", []):
            return True
        return self.headers.get("X-Token", "") == token

    def _client_address(self) -> str:
        return str(self.client_address[0] if self.client_address else "")

    def _device_label(self) -> str:
        """给电脑设置页看的一句话设备描述，好让用户核对是不是自己的手机。"""
        agent = str(self.headers.get("User-Agent", "") or "")
        if "Android" in agent:
            name = "安卓手机"
        elif "iPhone" in agent or "iPad" in agent:
            name = "苹果手机"
        elif not agent:
            name = "未知设备"
        else:
            name = agent[:20]
        address = self._client_address()
        return f"{name}（{address}）" if address else name

    def _pair_decision(self, approved: bool) -> None:
        try:
            payload = self._json_body()
        except ValueError as error:
            self._json(400, {"ok": False, "error": str(error)})
            return
        code = str(payload.get("code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            self._json(400, {"ok": False, "error": "配对码必须是 6 位数字"})
            return
        result = pairing.decide(code, approved)
        self._json(200 if result.get("ok") else 400, result)

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as error:
            raise ValueError("请求长度无效") from error
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("JSON 格式无效") from error
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def _napcat(self, action: str) -> dict[str, Any]:
        settings = self.server.settings  # type: ignore[attr-defined]
        url = settings.napcat_api_url.rstrip("/") + "/" + action
        request = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer " + settings.napcat_api_token})
        try:
            with urllib.request.urlopen(request, timeout=settings.http_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") != "ok" or payload.get("retcode", 0) != 0:
                return {"ok": False, "error": str(payload.get("message") or "NapCat request failed")}
            return {"ok": True, "data": payload.get("data") or {}}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def _remember_group_names(self, result: dict[str, Any]) -> None:
        """把 NapCat 报回来的群名存进数据库：界面显示群名靠它，NapCat 掉线时也得显示得出来。"""
        rows = result.get("data") or []
        names = {}
        for row in rows if isinstance(rows, list) else []:
            group_id = str(row.get("group_id", "")).strip()
            name = str(row.get("group_name", "")).strip()
            if group_id and name:
                names[group_id] = name
        if names:
            try:
                self.store.remember_group_names(names)
            except Exception:  # noqa: BLE001
                LOGGER.warning("群名缓存写入失败", exc_info=True)

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/api/health"):
            channels = self.server.settings.push_channels()  # type: ignore[attr-defined]
            self._json(200, {"ok": True, "service": "qq-tasks", "channels": channels})
            return
        if path == "/manifest.webmanifest":
            self._asset(MANIFEST_JSON, "application/manifest+json; charset=utf-8")
            return
        if path == "/icon.svg":
            self._asset(ICON_SVG, "image/svg+xml; charset=utf-8", cache="public, max-age=86400")
            return
        if path == "/sw.js":
            self._asset(SERVICE_WORKER_JS, "text/javascript; charset=utf-8")
            return
        if path == "/api/donate/qr.png":
            image = RUNTIME_ROOT / "assets" / "donate-wechat.jpg"
            if not image.is_file():
                self._json(404, {"ok": False, "error": "donation QR not found"})
                return
            try:
                self._jpeg(image.read_bytes())
            except OSError:
                self._json(404, {"ok": False, "error": "donation QR not found"})
            return
        if path == "/api/onboarding":
            accepted_at = self.store.meta_get("onboarding_accepted_at", "")
            self._json(200, {"required": not bool(accepted_at), "accepted_at": accepted_at})
            return
        if path == "/api/pair/status":
            # 手机在配对批准之前还没有令牌，这个接口必须免鉴权；它只认 code + secret，
            # 别人的码问不出任何东西，令牌也只会发给拿着同一个 secret 的那台手机。
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result = pairing.status((query.get("code") or [""])[0], (query.get("secret") or [""])[0])
            if result is None:
                self._json(404, {"ok": False, "error": "配对码无效或已过期"})
                return
            payload: dict[str, Any] = {"ok": True, "status": result["status"]}
            if result["status"] == "approved":
                payload["token"] = str(getattr(self.server, "token", "") or "")
            self._json(200, payload)
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if path in ("/", "/index.html"):
            self._html(PAGE_HTML)
            return
        if path == "/setup":
            self._redirect_home()
            return
        if path == "/api/napcat/status":
            status = self._napcat("get_status")
            login = self._napcat("get_login_info")
            stamp = napcat_admin.qr_stamp(self.server.settings) if napcat_admin is not None else ""  # type: ignore[attr-defined]
            if not status.get("ok") or not login.get("ok"):
                error = status.get("error") or login.get("error") or "NapCat error"
                boot = napcat_admin.detect_boot() if napcat_admin is not None else None
                self._json(200, {"ok": False, "online": False, "good": False, "user_id": "", "nickname": "", "napcat_installed": bool(boot), "napcat_root": (boot or {}).get("data_dir"), "qr_stamp": stamp, "error": error})
            else:
                sd, ld = status["data"], login["data"]
                boot = napcat_admin.detect_boot() if napcat_admin is not None else None
                self._json(200, {"ok": True, "online": bool(sd.get("online")), "good": bool(sd.get("good")), "user_id": sd.get("user_id") or ld.get("user_id", ""), "nickname": ld.get("nickname", ""), "napcat_installed": bool(boot), "napcat_root": (boot or {}).get("data_dir"), "qr_stamp": stamp, "error": ""})
            return
        if path == "/api/groups/suggest":
            if group_suggest is None:
                self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": "群组建议模块不可用"}); return
            try:
                raw = self._napcat("get_group_list")
                self._remember_group_names(raw)
                groups = raw.get("data", []) if raw.get("ok") else []
                if not raw.get("ok"):
                    self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": raw.get("error", "NapCat error")}); return
                self._json(200, group_suggest.suggest(self.server.settings, groups))  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": str(error)})
            return
        if path == "/api/napcat/groups":
            result = self._napcat("get_group_list")
            self._remember_group_names(result)
            if not result.get("ok"):
                self._json(200, {"ok": False, "groups": [], "error": result.get("error", "NapCat error")})
            else:
                self._json(200, {"ok": True, "groups": [{"group_id": str(g.get("group_id", "")), "name": str(g.get("group_name", "")), "member_count": g.get("member_count", 0), "selected": str(g.get("group_id", "")) in self.server.settings.group_whitelist} for g in result["data"]]})
            return
        if path == "/api/napcat/qrcode":
            raw = None
            if napcat_admin is not None:
                try:
                    raw = napcat_admin.qrcode_bytes(self.server.settings)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    raw = None
            if not raw:
                qr = Path(self.server.settings.napcat_qr_path)  # type: ignore[attr-defined]
                raw = qr.read_bytes() if qr.is_file() else None
            if not raw:
                self._json(404, {"ok": False, "error": "QR code not found"})
                return
            self.send_response(200); self.send_header("Content-Type", "image/png"); self.send_header("Content-Length", str(len(raw))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(raw)
            return
        if path == "/api/sync/info":
            try:
                port = int(self.server.server_address[1])
                token = str(getattr(self.server, "token", "") or "")
                events = sum(1 for line in render_calendar(self.store.list_tasks()).splitlines() if line == "BEGIN:VEVENT")
                calendars = _advertised_calendars(port, token)
            except OSError:
                LOGGER.warning("无法确定 iPhone 日历订阅地址")
                self._json(503, {"ok": False, "error": "无法确定可供手机访问的局域网地址"})
                return
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("读取日历订阅信息失败")
                self._json(500, {"ok": False, "error": str(error) or "读取日历订阅信息失败"})
                return
            if not calendars:
                LOGGER.warning("无法确定 iPhone 日历订阅地址")
                self._json(503, {"ok": False, "error": "无法确定可供手机访问的局域网地址"})
                return
            for item in calendars:
                item["events"] = events
            lan_base = next((item["url"][: item["url"].index("/calendar.ics")] for item in calendars if item["kind"] == "lan"), "")
            self._json(200, {"ok": True, "lan_base": lan_base, "port": port, "events": events, "calendars": calendars})
            return
        if path == "/api/sync/test":
            port = int(self.server.server_address[1])
            token = str(getattr(self.server, "token", "") or "")
            requested = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("url", [""])[0].strip()
            if requested.lower().startswith("webcal:"):
                requested = "http:" + requested[len("webcal:"):]
            parts = urllib.parse.urlsplit(requested)
            allowed = {urllib.parse.urlsplit(item["url"])[:3] for item in _advertised_calendars(port, token)}
            allowed |= {("http", f"127.0.0.1:{port}", "/calendar.ics"), ("http", f"localhost:{port}", "/calendar.ics")}
            if parts[:3] not in allowed:
                self._json(400, {"ok": False, "error": "只能测试本服务公布的日历地址"})
                return
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(urllib.request.Request(requested, headers={"User-Agent": "qq-notice-hub-selftest"}), timeout=10) as response:
                    body = response.read().decode("utf-8", "replace")
            except Exception as error:  # noqa: BLE001
                LOGGER.info("自测日历地址失败：%s", error)
                self._json(502, {"ok": False, "error": f"服务器读取该地址失败：{error}"})
                return
            if not body.startswith("BEGIN:VCALENDAR"):
                self._json(502, {"ok": False, "error": "地址返回的内容不是 iCalendar"})
                return
            count = sum(1 for line in body.splitlines() if line.strip() == "BEGIN:VEVENT")
            self._json(200, {"ok": True, "events": count, "url": requested})
            return
        if path == "/api/sync/qr.png":
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True, max_num_fields=4)
                values = query.get("text", [])
                if len(values) != 1:
                    raise ValueError("二维码内容无效")
                text = values[0]
                if not text or len(text) > MAX_SYNC_QR_CHARS or len(text.encode("utf-8")) > MAX_SYNC_QR_CHARS or any(not char.isprintable() or char.isspace() for char in text):
                    raise ValueError("二维码内容为空、过长或包含无效字符")
                parsed = urllib.parse.urlsplit(text)
                if parsed.scheme.lower() not in {"webcal", "http", "https"} or not parsed.netloc or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("二维码内容必须是带主机名且不含凭据的日历 URL")
                _ = parsed.port
                raw = qr_encoder.png_bytes(text, scale=10, border=4)
            except (ValueError, UnicodeEncodeError) as error:
                self._json(400, {"ok": False, "error": str(error) or "二维码内容无效"})
                return
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("生成日历订阅二维码失败")
                self._json(500, {"ok": False, "error": str(error) or "二维码生成失败"})
                return
            self._png(raw)
            return
        if path == "/api/app/qr.png":
            # 手机相机扫一下就能直接打开 App 并联好，用户不用输地址、也不用抄令牌。
            # 这条地址里带着本机令牌，所以它必须在本页登录之后才拿得到。
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True, max_num_fields=4)
                values = query.get("base", [])
                if len(values) != 1:
                    raise ValueError("手机地址无效")
                base = values[0].strip().rstrip("/")
                parsed = urllib.parse.urlsplit(base)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError("手机地址必须是带主机名、不带凭据的 http 或 https 地址")
                _ = parsed.port
                token = str(getattr(self.server, "token", "") or "")
                if not token:
                    raise ValueError("本机还没有访问令牌")
                text = "noticehub://connect?" + urllib.parse.urlencode({"base": base, "token": token})
                if len(text) > MAX_SYNC_QR_CHARS:
                    raise ValueError("地址太长，生成不了二维码")
                raw = qr_encoder.png_bytes(text, scale=8, border=4)
            except (ValueError, UnicodeEncodeError) as error:
                self._json(400, {"ok": False, "error": str(error) or "二维码内容无效"})
                return
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("生成手机配对二维码失败")
                self._json(500, {"ok": False, "error": str(error) or "二维码生成失败"})
                return
            self._png(raw)
            return
        if path == "/calendar":
            self._redirect_home()
            return
        if path == "/calendar.ics":
            self._ics(render_calendar(self.store.list_tasks()))
            return
        if path == "/api/history/floor":
            group_id = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("group_id") or [""])[0].strip()
            if not group_id or len(group_id) > 128:
                self._json(400, {"ok": False, "floor_ts": None, "total_seen": 0, "error": "群号无效"}); return
            if catchup is None:
                self._json(503, {"ok": False, "floor_ts": None, "total_seen": 0, "error": "回溯模块暂不可用"}); return
            try:
                result = catchup.available_floor(self.server.settings, group_id)  # type: ignore[attr-defined]
                error = str(result.get("error") or "")
                self._json(200, {"ok": not error, "floor_ts": result.get("floor_ts"), "total_seen": int(result.get("total_seen") or 0), "error": error})
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("探测 NapCat 缓存底线失败")
                self._json(200, {"ok": False, "floor_ts": None, "total_seen": 0, "error": str(error) or "探测失败"})
            return
        if path == "/api/history/fetch/status":
            self._json(200, _job_snapshot("history")); return
        if path == "/api/inbox/classify/status":
            self._json(200, _job_snapshot("classify")); return
        if path == "/api/inbox":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            verdict = (query.get("verdict") or [""])[0].strip()
            if verdict not in {"", "notice", "suspect", "noise", "promoted"}:
                self._json(400, {"ok": False, "error": "判定类别无效"}); return
            group_id = (query.get("group_id") or [""])[0].strip()
            q = (query.get("q") or [""])[0].strip()
            try:
                limit = int((query.get("limit") or ["50"])[0])
                offset = int((query.get("offset") or ["0"])[0])
            except ValueError:
                self._json(400, {"ok": False, "error": "分页参数无效"}); return
            if not 1 <= limit <= 100 or not 0 <= offset <= 10000000 or len(group_id) > 128 or len(q) > 200:
                self._json(400, {"ok": False, "error": "筛选或分页参数超出范围"}); return
            try:
                counts = self.store.inbox_counts()
                promoted = True if verdict == "promoted" else None
                selected_verdict = None if verdict in {"", "promoted"} else verdict
                filters = {"verdict": selected_verdict, "group_id": group_id or None, "q": q or None, "promoted": promoted}
                items = self.store.inbox_items(**filters, limit=limit, offset=offset)
                if group_id or q:
                    total = 0
                    count_offset = 0
                    while True:
                        batch = self.store.inbox_items(**filters, limit=1000, offset=count_offset)
                        total += len(batch)
                        if len(batch) < 1000:
                            break
                        count_offset += len(batch)
                elif verdict == "promoted":
                    total = int(counts.get("promoted", 0))
                elif verdict:
                    total = int(counts.get(verdict, 0))
                else:
                    total = sum(int(counts.get(key, 0)) for key in ("notice", "suspect", "noise"))
                self._json(200, {"ok": True, "items": items, "counts": counts, "total": total})
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("读取收件箱失败")
                self._json(200, {"ok": False, "items": [], "counts": {}, "total": 0, "error": str(error) or "收件箱暂不可用"})
            return
        if path == "/api/settings":
            if hosting is None:
                self._json(200, {"ok": False, "error": "托管模块不可用"}); return
            try:
                prefs = hosting.prefs(self.server.settings)  # type: ignore[attr-defined]
                with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
                    prefs.update(self.server.catchup_preferences)  # type: ignore[attr-defined]
                self._json(200, prefs)
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": str(error)})
            return
        if path == "/api/hosting/status":
            if hosting is None:
                self._json(200, {"ok": False, "error": "托管模块不可用"}); return
            try:
                self._json(200, hosting.hosting_status(self.server.settings))  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": str(error)})
            return
        if path == "/api/pair/info":
            settings = self.server.settings  # type: ignore[attr-defined]
            public = _tailscale_public_app_url(settings.web_port)
            lan = [f"http://{host}:{settings.web_port}" for host in _lan_hosts()]
            # 局域网地址也能用（手机和电脑同一个 Wi-Fi 时），所以没开公网也有可复制的地址。
            base = public or (lan[0] if lan else "")
            self._json(
                200,
                {
                    "ok": True,
                    "public_url": public,
                    "public_enabled": bool(public),
                    "tailscale_ready": bool(_tailscale_executable()),
                    "base": base,
                    "lan": lan,
                    "steps": _pair_help(base)["steps"],
                    "no_public_hint": _pair_help(base)["no_public_hint"],
                },
            )
            return
        if path == "/api/pair/pending":
            self._json(200, {"ok": True, "pending": pairing.pending()})
            return
        if path == "/api/tasks":
            now = now_local()
            tasks = self.store.list_tasks()
            # 待办里存的是群号（入库时就存成 messages.group_name，取不到名字时就是群号本身），
            # 界面上要显示群名，所以在出口这一层统一换成名字。
            names = self.store.group_names()
            aliases = self.server.settings.group_aliases  # type: ignore[attr-defined]
            for task in tasks:
                task["groups"] = [
                    self.store.group_label(group_id, names, aliases)
                    for group_id in (task.get("groups") or [])
                ]
            grouped = group_tasks(tasks, now)
            payload = overview(tasks, grouped, now)
            payload["stats"] = self.store.task_stats()
            payload.update(grouped)
            payload["pinned"] = sorted(
                (task for task in tasks if task.get("pinned")),
                key=lambda task: (
                    str(task.get("deadline") or "9999-99-99"),
                    -int(task.get("importance") or 0),
                    -int(task.get("id") or 0),
                ),
            )
            self._json(200, payload)
            return
        if path == "/api/notices":
            items = []
            for digest in self.store.recent_digests(limit=8):
                items.append(
                    {
                        "id": digest.get("id"),
                        "kind": digest.get("kind"),
                        "created_at": digest.get("created_at"),
                        "summary": f"{digest.get('item_count') or 0} 条重点",
                        "body": str(digest.get("body") or "")[:600],
                    }
                )
            self._json(200, {"items": items})
            return
        if path == "/api/insights":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = int((query.get("days") or ["30"])[0])
            except (TypeError, ValueError):
                days = 30
            days = max(1, min(365, days))
            self._json(
                200,
                {
                    "stats": self.store.correction_stats(days=days),
                    "insights": self.store.correction_insights(days=days),
                },
            )
            return
        if path == "/api/meta":
            settings = self.server.settings  # type: ignore[attr-defined]
            manager = getattr(self.server, "meta_provider", None)
            info: dict[str, Any] = {}
            if callable(manager):
                try:
                    info = manager() or {}
                except Exception:  # noqa: BLE001 - 元信息失败不影响页面
                    info = {}
            self._json(
                200,
                {
                    "groups": list(info.get("groups") or []),
                    "channels": list(info.get("channels") or []),
                    "reminders": info.get("reminders") or "",
                    "started_at": info.get("started_at") or "",
                    "insights": list(info.get("insights") or []),
                    "push_budget": info.get("push_budget") or "",
                    "quiet_hours": info.get("quiet_hours") or "",
                    "push": _push_setting_state(settings),
                    "push_help": PUSH_HELP_LINKS,
                    "llm": _llm_setting_state(settings),
                    "providers": LLM_PROVIDERS,
                    "stats": self.store.task_stats(),
                },
            )
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/api/onboarding/accept":
            accepted_at = self.store.meta_get("onboarding_accepted_at", "")
            if not accepted_at:
                accepted_at = iso(now_local())
                self.store.meta_set("onboarding_accepted_at", accepted_at)
            self._json(200, {"ok": True, "accepted_at": accepted_at})
            return
        if path == "/api/pair/start":
            # 手机第一次进门时还没有令牌，这条必须免鉴权。它只发一个 6 位码和 secret，
            # 真正的授权动作是用户在电脑设置页核对数字后点「允许」。
            try:
                result = pairing.start(device=self._device_label(), source=self._client_address())
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            self._json(200, {"ok": True, **result})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        if path == "/api/pair/approve":
            self._pair_decision(True)
            return
        if path == "/api/pair/deny":
            self._pair_decision(False)
            return
        if path == "/api/pair/public":
            settings = self.server.settings  # type: ignore[attr-defined]
            result = _enable_tailscale_funnel(settings.web_port)
            if result.get("ok"):
                self._json(200, {"ok": True, "url": result.get("url", "")})
            else:
                self._json(400, {"ok": False, "error": str(result.get("error") or "开通公网入口失败")})
            return
        if path == "/api/tasks/urgent":
            try:
                payload = self._json_body()
                task_id = payload.get("task_id")
                value = payload.get("urgent")
                if not isinstance(task_id, str) or not task_id.isascii() or not task_id.isdigit() or len(task_id) > 18 or int(task_id) < 1:
                    raise ValueError("task_id 必须是正整数文本")
                if "urgent" not in payload or (value is not None and type(value) is not bool):
                    raise ValueError("urgent 必须是 true、false 或 null")
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            try:
                result = self.store.set_task_urgent(int(task_id), value)
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("保存任务紧急覆盖失败")
                self._json(500, {"ok": False, "error": str(error) or "紧急设置保存失败"})
                return
            self._json(200 if result.get("ok") else 404, result)
            return
        if path == "/api/tasks/pin":
            try:
                payload = self._json_body()
                task_id = payload.get("task_id")
                value = payload.get("pinned")
                if not isinstance(task_id, str) or not task_id.isascii() or not task_id.isdigit() or len(task_id) > 18 or int(task_id) < 1:
                    raise ValueError("task_id 必须是正整数文本")
                if type(value) is not bool:
                    raise ValueError("pinned 必须是 true 或 false")
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            try:
                result = self.store.set_task_pinned(int(task_id), value)
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("保存任务置顶失败")
                self._json(500, {"ok": False, "error": str(error) or "置顶设置保存失败"})
                return
            self._json(200 if result.get("ok") else 404, result)
            return
        if path == "/api/tasks/create":
            try:
                payload = self._json_body()
                summary = payload.get("summary")
                deadline = payload.get("deadline") or ""
                if not isinstance(summary, str):
                    raise ValueError("summary 必须是文本")
                summary = " ".join(summary.split())
                if not summary:
                    raise ValueError("请填写待办内容")
                if len(summary) > 200:
                    raise ValueError("待办内容请控制在 200 字以内")
                if deadline and not isinstance(deadline, str):
                    raise ValueError("deadline 必须是文本")
                stamp = ""
                if deadline:
                    parsed = parse_iso(deadline)
                    if parsed is None:
                        raise ValueError("截止时间格式不对，请重新选择")
                    stamp = iso(parsed)
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
            try:
                # 手动新建的待办用一次性 task_key，避免和 QQ 消息里抽出来的任务互相覆盖。
                task_id = self.store.upsert_task(
                    task_key=f"manual:{secrets.token_hex(8)}",
                    summary=summary,
                    deadline=stamp,
                    source="manual",
                    confidence=1.0,
                    classification_reason="页面手动新建",
                )
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("手动新建待办失败")
                self._json(500, {"ok": False, "error": str(error) or "新建待办失败"})
                return
            if not task_id:
                self._json(500, {"ok": False, "error": "新建待办失败"})
                return
            self._json(200, {"ok": True, "task_id": task_id, "summary": summary, "deadline": stamp})
            return
        if path == "/api/settings/test-llm":
            settings = self.server.settings  # type: ignore[attr-defined]
            started = dt.datetime.now()
            model = str(settings.dashscope_model or "")
            try:
                from .attachments import chat_completion

                reply = chat_completion(
                    settings,
                    [{"role": "user", "content": "只回复三个字：已连通"}],
                    model=model,
                    timeout=min(30, max(10, int(settings.llm_timeout or 60))),
                    max_tokens=16,
                )
            except Exception as error:  # noqa: BLE001 - 任何失败都原样翻译给页面
                self._json(200, {
                    "ok": False,
                    "error": _llm_test_error(error),
                    "model": model,
                    "endpoint": str(settings.dashscope_endpoint or ""),
                })
                return
            self._json(200, {
                "ok": True,
                "model": model,
                "reply": " ".join(str(reply or "").split())[:120],
                "seconds": round((dt.datetime.now() - started).total_seconds(), 1),
            })
            return
        if path == "/api/settings/test-push":
            settings = self.server.settings  # type: ignore[attr-defined]
            try:
                from .push import build_pushers
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": f"推送模块加载失败：{error}", "results": []})
                return
            try:
                pushers = build_pushers(settings)
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": f"推送通道初始化失败：{error}", "results": []})
                return
            if not pushers:
                self._json(200, {"ok": False, "error": "还没有配置任何推送通道", "results": []})
                return
            results: list[dict[str, Any]] = []
            for pusher in pushers:
                try:
                    pusher.send("QQ 群消息台 · 测试消息", "看到这条消息，说明提醒已经能送到你手机上了。")
                except Exception as error:  # noqa: BLE001
                    results.append({
                        "channel": str(getattr(pusher, "name", "通道")),
                        "target": str(getattr(pusher, "target", "")),
                        "ok": False,
                        "error": str(error)[:200] or "发送失败",
                    })
                    continue
                results.append({
                    "channel": str(getattr(pusher, "name", "通道")),
                    "target": str(getattr(pusher, "target", "")),
                    "ok": True,
                    "error": "",
                })
            self._json(200, {
                "ok": all(item["ok"] for item in results),
                "results": results,
            })
            return
        if path == "/api/settings":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("settings must be a JSON object")
            except (ValueError, TypeError, UnicodeDecodeError):
                self._json(400, {"ok": False, "error": "bad json"}); return
            catchup_keys = {key for key in ("catchup_enabled", "catchup_hours") if key in payload}
            host_keys = {"quit_qq", "restore_qq", "auto_on_start", "autostart"}.intersection(payload)
            push_keys = set(_PUSH_SETTING_ENV_KEYS).intersection(payload)
            llm_keys = {"llm_provider", "llm_api_key", "llm_model", "llm_endpoint"}.intersection(payload)
            if catchup_keys and host_keys:
                self._json(400, {"ok": False, "error": "托管设置与回溯设置请分开保存"}); return
            if push_keys and (catchup_keys or host_keys):
                self._json(400, {"ok": False, "error": "推送通道设置请单独保存"}); return
            if llm_keys and (push_keys or catchup_keys or host_keys):
                self._json(400, {"ok": False, "error": "AI 接入设置请单独保存"}); return
            try:
                if llm_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    updates, applied = _llm_setting_updates(payload, settings)
                    result = update_env_file(Path(settings.env_file), updates)
                    if not result.get("ok"):
                        raise ValueError(result.get("error") or ".env 写入失败")
                    # 热生效：保存后立刻用新服务商，不必重启服务。
                    for field_name, field_value in applied.items():
                        setattr(settings, field_name, field_value)
                    self._json(200, {"ok": True, "llm": _llm_setting_state(settings)})
                    return
                if push_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    updates, applied = _push_setting_values(payload, push_keys)
                    result = update_env_file(Path(settings.env_file), updates)
                    if not result.get("ok"):
                        raise ValueError(result.get("error") or ".env 写入失败")
                    # 热生效：同一进程内的 Settings 立刻反映新通道，不必重启。
                    for field_name, field_value in applied.items():
                        setattr(settings, field_name, field_value)
                    self._json(200, {
                        "ok": True,
                        "channels": settings.push_channels(),
                        "push": _push_setting_state(settings),
                    })
                    return
                if catchup_keys:
                    settings = self.server.settings  # type: ignore[attr-defined]
                    if "catchup_enabled" in payload and not isinstance(payload["catchup_enabled"], bool):
                        raise ValueError("catchup_enabled 必须是布尔值")
                    with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
                        current_prefs = dict(self.server.catchup_preferences)  # type: ignore[attr-defined]
                    hours = payload.get("catchup_hours", current_prefs["catchup_hours"])
                    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 8760:
                        raise ValueError("回溯小时数必须在 1 到 8760 之间")
                    env_path = Path(settings.env_file or "")
                    if not str(settings.env_file or ""):
                        raise ValueError("找不到 .env 路径")
                    updates = {}
                    if "catchup_enabled" in payload:
                        updates["QQ_DIGEST_CATCHUP_ENABLED"] = "1" if payload["catchup_enabled"] else "0"
                    if "catchup_hours" in payload:
                        updates["QQ_DIGEST_CATCHUP_HOURS"] = str(hours)
                    result = update_env_file(env_path, updates)
                    if not result.get("ok"):
                        self._json(400, {"ok": False, "error": result.get("error") or ".env 写入失败"}); return
                    with self.server.catchup_preferences_lock:  # type: ignore[attr-defined]
                        saved_prefs = dict(self.server.catchup_preferences)  # type: ignore[attr-defined]
                        if "catchup_enabled" in payload:
                            saved_prefs["catchup_enabled"] = payload["catchup_enabled"]
                        if "catchup_hours" in payload:
                            saved_prefs["catchup_hours"] = hours
                        self.server.catchup_preferences = saved_prefs  # type: ignore[attr-defined]
                    self._json(200, {"ok": True, "updated": result.get("updated"), "added": result.get("added"), **saved_prefs})
                    return
                if hosting is None:
                    self._json(200, {"ok": False, "error": "托管模块不可用"}); return
                result = hosting.save_prefs(self.server.settings, payload)  # type: ignore[attr-defined]
                self._json(200 if result.get("ok") else 400, result)
            except (ValueError, TypeError) as error:
                self._json(400, {"ok": False, "error": str(error)})
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": str(error) or "设置保存失败"})
            return
        if path == "/api/history/fetch":
            try:
                payload = self._json_body()
                groups = _parse_groups(payload.get("groups"))
                since = _parse_day(payload.get("since"))
                until = _parse_day(payload.get("until"), end=True)
                if since and until and since > until:
                    raise ValueError("开始日期不能晚于结束日期")
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)}); return
            if catchup is None:
                self._json(503, {"ok": False, "error": "回溯模块暂不可用"}); return
            settings = self.server.settings  # type: ignore[attr-defined]
            started, active = _start_job("history", lambda progress, cancel: catchup.backfill_range(settings, self.store, groups=groups, since=since, until=until, progress=progress, cancel=cancel))
            if not started:
                self._json(409, {"ok": False, "error": "已有回溯任务在进行" if active == "history" else "已有通知提取任务在进行"}); return
            self._json(200, {"ok": True, "started": True}); return
        if path == "/api/inbox/classify":
            try:
                payload = self._json_body()
                groups = _parse_groups(payload.get("groups"))
                since = _parse_day(payload.get("since"))
                until = _parse_day(payload.get("until"), end=True)
                limit = payload.get("limit", 1000)
                if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20000:
                    raise ValueError("提取条数必须在 1 到 20000 之间")
                if since and until and since > until:
                    raise ValueError("开始日期不能晚于结束日期")
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)}); return
            if inbox is None:
                self._json(503, {"ok": False, "error": "通知判定模块暂不可用"}); return
            settings = self.server.settings  # type: ignore[attr-defined]
            started, active = _start_job("classify", lambda progress, cancel: inbox.classify_messages(settings, self.store, since=since, until=until, groups=groups, limit=limit, progress=progress, cancel=cancel))
            if not started:
                self._json(409, {"ok": False, "error": "已有通知提取任务在进行" if active == "classify" else "已有回溯任务在进行"}); return
            self._json(200, {"ok": True, "started": True}); return
        if path == "/api/inbox/promote":
            try:
                payload = self._json_body()
                msg_id = str(payload.get("msg_id") or "").strip()
                title = payload.get("title")
                due = payload.get("due")
                if not msg_id or len(msg_id) > 256:
                    raise ValueError("消息编号无效")
                if title is not None and (not isinstance(title, str) or len(title) > 500):
                    raise ValueError("待办标题最多 500 个字符")
                if due is not None and due != "" and not isinstance(due, str):
                    raise ValueError("截止时间格式无效")
                if due and parse_iso(due) is None:
                    raise ValueError("截止时间格式无效")
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)}); return
            if inbox is None:
                self._json(503, {"ok": False, "task_id": None, "error": "通知判定模块暂不可用"}); return
            try:
                result = inbox.promote(self.server.settings, self.store, msg_id, title=title, due=due)  # type: ignore[attr-defined]
                self._json(200 if result.get("ok") else 400, {"ok": bool(result.get("ok")), "task_id": result.get("task_id"), "error": str(result.get("error") or "")})
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("通知转待办失败")
                self._json(200, {"ok": False, "task_id": None, "error": str(error) or "转换失败"})
            return
        if path in ("/api/hosting/start", "/api/hosting/stop"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, TypeError):
                self._json(400, {"ok": False, "error": "bad json"}); return
            if hosting is None:
                self._json(200, {"ok": False, "error": "托管模块不可用"}); return
            try:
                result = (hosting.start(self.server.settings, uin=str(payload.get("uin") or "").strip() or None)
                          if path.endswith("/start") else hosting.stop(self.server.settings))  # type: ignore[attr-defined]
                self._json(200, result)
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "error": str(error)})
            return
        if path == "/api/subscriptions":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._json(400, {"ok": False, "error": "bad body"}); return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                groups = [str(x).strip() for x in payload.get("groups", []) if str(x).strip()]
                aliases = {str(k): str(v) for k, v in (payload.get("aliases") or {}).items() if str(k).strip()}
            except (ValueError, TypeError, AttributeError):
                self._json(400, {"ok": False, "error": "bad json"}); return
            settings = self.server.settings  # type: ignore[attr-defined]
            env_path = Path(settings.env_file or Path.cwd() / ".env")
            if not env_path.is_file():
                self._json(400, {"ok": False, "error": ".env not found"}); return
            raw = env_path.read_bytes()
            lines = raw.splitlines(keepends=True)
            updates = {"QQ_DIGEST_GROUPS": ",".join(groups), "QQ_DIGEST_GROUP_ALIASES": ",".join(k + "=" + v for k, v in aliases.items())}
            found = set()
            out = []
            for line in lines:
                text = line.decode("utf-8")
                key = text.split("=", 1)[0].strip() if "=" in text and not text.lstrip().startswith("#") else ""
                if key in updates:
                    newline = "\r\n" if text.endswith("\r\n") else "\n" if text.endswith("\n") else ""
                    out.append((key + "=" + updates[key] + newline).encode("utf-8")); found.add(key)
                else: out.append(line)
            if set(updates) - found:
                self._json(400, {"ok": False, "error": "required .env keys missing"}); return
            env_path.write_bytes(b"".join(out))
            settings.group_whitelist = tuple(groups); settings.group_aliases = aliases
            self._json(200, {"ok": True, "groups": groups, "applied": True}); return
        if path == "/api/napcat/install":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._json(400, {"ok": False, "error": "bad body"}); return
            if napcat_admin is None:
                self._json(200, {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": "一键接入模块不可用"}); return
            try:
                result = napcat_admin.install(self.server.settings)  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                result = {"ok": False, "installed": False, "already_installed": False, "root": None, "version": None, "bytes": 0, "error": str(error)}
            self._json(200, result)
            return
        if path == "/api/napcat/launch":
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                self._json(400, {"ok": False, "error": "bad body"}); return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                uin = str(payload.get("uin") or "").strip() or None
            except (ValueError, TypeError, AttributeError):
                self._json(400, {"ok": False, "error": "bad json"}); return
            if napcat_admin is None:
                self._json(200, {"ok": False, "already_running": False, "pid": None, "command": [], "profile_dir": "", "boot": None, "error": "一键接入模块不可用"}); return
            try:
                result = napcat_admin.launch(self.server.settings, uin=uin)  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("启动 NapCat 失败：%s", error)
                result = {"ok": False, "already_running": False, "pid": None, "command": [], "profile_dir": "", "boot": None, "error": str(error)}
            self._json(200, result)
            return
        if path == "/api/napcat/autosetup":
            if napcat_admin is None:
                self._json(200, {"ok": False, "steps": [], "qrcode_path": None, "uin": None, "applied_via": None, "restart_required": False, "error": "一键接入模块不可用"})
                return
            try:
                result = napcat_admin.auto_setup(self.server.settings)  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                LOGGER.warning("一键接入失败：%s", error)
                result = {"ok": False, "steps": [], "qrcode_path": None, "uin": None, "applied_via": None, "restart_required": False, "error": str(error)}
            self._json(200, result)
            return
        if not path.startswith("/api/tasks/"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            task_id = int(path.rsplit("/", 1)[-1])
        except ValueError:
            self._json(400, {"ok": False, "error": "bad task id"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(400, {"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace") or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "bad json"})
            return
        action = str(payload.get("action") or "").strip().lower()
        if not action and "done" in payload:
            action = "done" if bool(payload.get("done")) else "reopen"
        if action == "correct":
            try:
                changed = self.store.apply_task_correction(
                    task_id,
                    str(payload.get("correction") or ""),
                    value=payload.get("value", ""),
                )
            except ValueError as error:
                self._json(400, {"ok": False, "error": str(error)})
                return
        else:
            if action not in {"confirm", "dismiss", "done", "reopen", "snooze"}:
                self._json(400, {"ok": False, "error": "unsupported action"})
                return
            detail: dict[str, Any] = {}
            if action == "snooze":
                settings = self.server.settings  # type: ignore[attr-defined]
                detail = {"until": snooze_default_until(settings, now_local())}
            changed = self.store.apply_task_action(task_id, action, detail=detail)
        task = self.store.get_task(task_id) if changed else None
        self._json(
            200 if changed else 404,
            {
                "ok": changed,
                "id": task_id,
                "action": action,
                "done": bool(task and task.get("status") == "done"),
                "status": (task or {}).get("status", ""),
            },
        )


class TaskWebServer:
    """待办台的 HTTP 服务，绑定在独立端口上。"""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        *,
        logger: logging.Logger | None = None,
        meta_provider: Any = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.logger = logger or LOGGER
        self.meta_provider = meta_provider
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.token = settings.web_token or store.meta_get("web_token", "")
        if not self.token and settings.web_host not in {"127.0.0.1", "localhost", "::1"}:
            self.token = secrets.token_urlsafe(12)
            store.meta_set("web_token", self.token)

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self) -> bool:
        if self.is_alive:
            return True
        try:
            server = ThreadingHTTPServer((self.settings.web_host, self.settings.web_port), _Handler)
        except OSError as error:
            self.logger.error("待办台启动失败（%s:%d）：%s", self.settings.web_host, self.settings.web_port, error)
            return False
        server.store = self.store  # type: ignore[attr-defined]
        server.settings = self.settings  # type: ignore[attr-defined]
        server.catchup_preferences = {"catchup_enabled": bool(self.settings.catchup_enabled), "catchup_hours": int(self.settings.catchup_hours)}  # type: ignore[attr-defined]
        server.catchup_preferences_lock = threading.Lock()  # type: ignore[attr-defined]
        server.token = self.token  # type: ignore[attr-defined]
        server.meta_provider = self.meta_provider  # type: ignore[attr-defined]
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="tasks-web", daemon=True)
        self.thread.start()
        self.logger.info(
            "待办台已启动：http://%s:%d/ （%s）",
            self.settings.web_host,
            self.settings.web_port,
            "需要 token" if self.token else "未设 token",
        )
        return True

    def stop(self) -> None:
        if self.server is not None:
            try:
                self.server.shutdown()
                if self.thread is not None:
                    self.thread.join(timeout=5)
                self.server.server_close()
            except Exception:  # noqa: BLE001 - 关闭失败不应阻塞退出
                self.logger.exception("待办台关闭失败")
        self.server = None
        self.thread = None

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.web_enabled),
            "alive": self.is_alive,
            "host": self.settings.web_host,
            "port": self.settings.web_port,
            "auth": bool(self.token),
        }
