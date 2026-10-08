"""手机端待办台：一个极小的 HTTP 服务，读取 tasks 表并支持勾选完成。"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import socket
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import qr as qr_encoder
from .config import Settings, update_env_file
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
SETUP_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>接入向导</title><style>body{margin:0;padding:18px;background:#0a0c12;color:#f3f5fa;font:15px sans-serif}main{max-width:600px;margin:auto}.card{padding:16px;margin:12px 0;background:#171c29;border:1px solid #303747;border-radius:10px}img{display:block;width:240px;height:240px;object-fit:contain;margin:auto;background:#fff}button{padding:8px 12px;margin:4px;border-radius:7px;border:1px solid #475569;background:#26385e;color:white}label{display:block;padding:10px;border-bottom:1px solid #303747}#groups{margin-top:10px}#go{padding:12px 22px;font-size:16px;background:#2563eb;border:0}#steps div{padding:4px 0;font-size:13px;line-height:1.5}</style></head><body><main><h1>QQ 接入向导</h1><div class="card"><button id="go" type="button">一键接入</button><div id="steps"></div></div><div id="status" class="card">正在检查 QQ 登录状态…</div><div id="qrbox" class="card"><p>用 QQ 主号扫码</p><img id="qr" alt="登录二维码"></div><div id="groupbox" class="card" hidden><div><button id="all" type="button">全选</button><button id="none" type="button">全不选</button></div><div id="groups"></div><button id="save" type="button">保存订阅</button><p id="result"></p><a href="/calendar">打开月历</a></div></main><script>(function(){var selected={},names={},loaded=false;var token=new URLSearchParams(location.search).get('token')||localStorage.getItem('qq_digest_token')||'';function api(path,opt){opt=opt||{};opt.headers=Object.assign({'X-Token':token},opt.headers||{});return fetch(path,opt).then(function(r){return r.json().then(function(x){if(!r.ok)throw Error(x.error||'请求失败');return x;});});}function qrSrc(){return '/api/napcat/qrcode?token='+encodeURIComponent(token)+'&t='+Date.now();}document.getElementById('qr').src=qrSrc();function run(){var go=document.getElementById('go'),box=document.getElementById('steps');go.disabled=true;go.textContent='正在接入…';box.innerHTML='';api('/api/napcat/autosetup',{method:'POST'}).then(function(d){(d.steps||[]).forEach(function(s){var p=document.createElement('div');p.textContent=(s.ok?'✓ ':'✗ ')+s.name+'：'+s.detail;box.appendChild(p);});if(d.qrcode_path){document.getElementById('qr').src=qrSrc();}if(!d.ok){var e=document.createElement('div');e.textContent='未完成：'+(d.error||'');box.appendChild(e);}check();}).catch(function(e){box.textContent='接入失败：'+e.message;}).then(function(){go.disabled=false;go.textContent='一键接入';});}document.getElementById('go').addEventListener('click',run);function check(){api('/api/napcat/status').then(function(s){var ok=s.ok&&s.online;document.getElementById('status').textContent=ok?'已登录：'+(s.nickname||'')+'（'+(s.user_id||'')+'）':'未登录，请扫码';document.getElementById('qrbox').hidden=ok;document.getElementById('groupbox').hidden=!ok;if(ok&&!loaded)loadGroups();}).catch(function(e){document.getElementById('status').textContent='连接 NapCat 失败：'+e.message;});}function loadGroups(){api('/api/napcat/groups').then(function(d){var root=document.getElementById('groups');root.innerHTML='';d.groups.forEach(function(g){var id=String(g.group_id);names[id]=g.name||'';selected[id]=!!g.selected;var l=document.createElement('label');var c=document.createElement('input');c.type='checkbox';c.value=id;c.checked=!!selected[id];c.onchange=function(){selected[id]=c.checked;};l.appendChild(c);l.appendChild(document.createTextNode(' '+g.name+'（'+id+'）'));root.appendChild(l);});loaded=true;});}document.getElementById('all').onclick=function(){document.querySelectorAll('#groups input').forEach(function(c){c.checked=true;selected[c.value]=true;});};document.getElementById('none').onclick=function(){document.querySelectorAll('#groups input').forEach(function(c){c.checked=false;selected[c.value]=false;});};/* api('/api/subscriptions', ...).catch(function(e){ result.textContent='保存失败'; }) */
document.getElementById('save').onclick=function(){var groups=Object.keys(selected).filter(function(id){return selected[id];});api('/api/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({groups:groups,aliases:groups.reduce(function(a,id){a[id]=names[id]||'';return a;},{})})}).then(function(){document.getElementById('result').textContent='已保存并立即生效';}).catch(function(e){document.getElementById('result').textContent='保存失败：'+e.message;});};setInterval(function(){document.getElementById('qr').src='/api/napcat/qrcode?t='+Date.now();check();},5000);check();})();</script></body></html>"""
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
.dashboard-card h2{margin:0 0 12px;font-size:19px}.dashboard-card{transform:translateZ(var(--depth-content));box-shadow:0 10px 18px rgba(0,0,0,.12)}.login-choice{margin:14px 0 0;padding:10px;border:1px solid var(--line);border-radius:7px;color:var(--muted)}.login-choice label{display:inline-flex;min-height:44px;align-items:center;margin-right:12px;padding:4px 0;border:0}.login-choice input[type=text],.login-choice input:not([type]){max-width:180px;padding:7px;border:1px solid var(--line);border-radius:5px;background:rgba(255,255,255,.06);color:var(--text)}.login-note{font-size:12px;line-height:1.55;color:var(--muted);margin:12px 0 4px}.login-note strong{color:#e5edf7}.login-more{margin:4px 0 10px;padding:7px 9px;border-left:2px solid #43c9b0;background:rgba(67,201,176,.06)}.login-more summary{color:#a8dcd1;font-size:12px}.login-more summary:after{margin-left:auto}.login-more p{margin:7px 0 0;font-size:12px;line-height:1.6;color:var(--muted)}.connect-actions{display:flex;gap:8px;flex-wrap:wrap}.connect-actions button{min-height:44px;font:inherit;color:var(--text);background:#2457c6;border:1px solid #6f98ff;border-radius:7px;padding:9px 14px;cursor:pointer}.connect-actions button.secondary{background:rgba(255,255,255,.07);border-color:var(--line)}#qrbox{margin-top:14px}#qrbox img{display:block;width:min(100%,230px);aspect-ratio:1;object-fit:contain;background:#fff}#groups{display:grid;gap:6px;margin-top:12px}#groups label{padding:8px;border-bottom:1px solid var(--line)}#calendar{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:5px}.calendar-day{min-height:72px;padding:7px;background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:5px}.calendar-day strong{font-size:14px}.calendar-event{display:-webkit-box;margin-top:4px;font-size:11px;line-height:1.35;color:#bcd6ff;overflow:hidden;text-overflow:ellipsis;overflow-wrap:anywhere;-webkit-line-clamp:2;-webkit-box-orient:vertical}
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
:root{--page:#edf2ef;--paper:#fff;--paper-alt:#f5f8f6;--ink:#172722;--muted:#42554e;--line:#c9d4ce;--teal:#12695b;--teal-soft:#e1f1ec;--red:#a5312d;--red-soft:#fff0ed;--amber:#805411;--amber-soft:#fff4dd;--depth-mid:0px;--depth-top:16px;--ease-in:cubic-bezier(.2,.8,.2,1);--ease-out:cubic-bezier(.4,0,1,1)}
html{background:var(--page);color-scheme:light;overflow-x:hidden}
body{max-width:100%;padding:0 0 40px;background:var(--page);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
button,input,select{font:inherit}
.masthead,main{width:min(100% - 32px,960px);margin-inline:auto}
.masthead{position:static;top:auto;z-index:auto;padding:20px 0 0;background:transparent;backdrop-filter:none;perspective:1100px;transform-style:preserve-3d}
.masthead-row{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:0 0 16px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;color:var(--ink);text-decoration:none;min-height:44px}
.brand-mark{display:block;flex:none;width:36px;height:36px}
.brand strong,.brand small{display:block}.brand strong{font-size:16px;line-height:1.25}.brand small{margin-top:2px;color:var(--muted);font-size:12px}
.local-badge{display:flex;align-items:center;gap:8px;min-height:36px;padding:0 12px;border:1px solid var(--line);border-radius:20px;color:#26483d;background:var(--paper);font-size:12px;font-weight:650}
.local-badge i{width:8px;height:8px;border-radius:50%;background:#26805e}
.summary{position:relative;margin-top:16px;padding:16px;border:1px solid #b8cbc1;border-radius:8px;background:linear-gradient(115deg,#e5f2ec 0%,#f7f8f4 57%,#f8eee5 100%);box-shadow:0 12px 22px rgba(27,55,44,.12),0 3px 8px rgba(27,55,44,.08);transform:none;animation:summary-enter 280ms var(--ease-in) both}
@keyframes summary-enter{from{opacity:.7;transform:translate3d(0,10px,-8px) scale(.985)}to{opacity:1;transform:none}}
.summary-top,.summary-bottom{display:flex;align-items:center;justify-content:space-between;gap:12px}.eyebrow{display:block;color:#536a5f;font-size:12px;line-height:1.45;font-weight:700;text-transform:uppercase}
.summary h1{margin:4px 0 0;font-size:26px;line-height:1.25;font-weight:720;overflow-wrap:anywhere}.summary p{margin:4px 0 0;color:#34483f;font-size:15px;line-height:1.5}
.progress-label{flex:none;padding:4px 9px;border:1px solid #bdcec5;border-radius:20px;background:rgba(255,255,255,.72);color:#284a3e;font-size:12px;font-weight:700}
.summary-bottom{margin-top:12px;align-items:flex-end}.stats{display:flex;flex-wrap:wrap;gap:8px}.stat{display:none;padding:4px 9px;border:1px solid #ccd8d1;border-radius:18px;background:#fff;color:#33483f;font-size:12px}.stat.show{display:inline-flex}
.bar{width:min(240px,34%);height:6px;margin:0;overflow:hidden;border-radius:6px;background:#d0dbd5}.bar>i{display:block;height:100%;width:100%;transform:scaleX(0);transform-origin:left;background:var(--teal);transition:transform 220ms var(--ease-in)}
.tabs{position:static;display:flex;gap:8px;width:100%;max-width:none;margin:16px 0 0;padding:0;transform:none;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent;box-shadow:none;backdrop-filter:none;z-index:auto}
.tabs:before,.tabs .dot{display:none}.tabs button{display:flex;flex:0 0 auto;align-items:center;justify-content:center;flex-direction:row;gap:8px;min-width:112px;min-height:48px;height:48px;padding:0 16px;border:0;border-bottom:3px solid transparent;border-radius:0;background:transparent;color:#42564e;font-size:14px;font-weight:650;transition:transform 120ms var(--ease-in),opacity 120ms var(--ease-in)}
.tabs button.active{border-bottom-color:var(--teal);color:#173e34}.tabs svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;filter:none;transition:none}.tabs button.active svg{stroke-width:2}
main{display:block;padding:16px 0 0;perspective:1100px;transform-style:preserve-3d}.workspace{display:grid;grid-template-columns:minmax(0,1fr);gap:24px;align-items:start}#tab-tasks{display:grid;grid-template-columns:minmax(0,1fr);gap:0;align-items:start}.primary-view{min-width:0}.view-screen{min-width:0;transition:transform 220ms var(--ease-in),opacity 220ms var(--ease-in)}.camera-enter{opacity:0;transform:perspective(1100px) translate3d(var(--camera-x,12px),0,-12px) scale(.98)}.camera-moving{will-change:transform,opacity}
.surface{min-width:0;padding:16px;background:var(--paper);border:1px solid var(--line);border-radius:8px;box-shadow:0 9px 17px rgba(20,49,39,.1),0 2px 5px rgba(20,49,39,.08);transform:translateZ(var(--depth-mid));transform-style:preserve-3d}.side-rail{display:grid;grid-template-columns:minmax(0,1fr);gap:16px;min-width:0}#calendar-panel{grid-column:auto;grid-row:auto;scroll-margin-top:24px}.panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.panel-heading h2{margin:2px 0 0;font-size:19px;line-height:1.35}.panel-index{color:#536a5f;font-size:12px;font-variant-numeric:tabular-nums}
.section{width:100%;max-width:100%;margin:0 0 24px}.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 8px;padding:0 2px}.section-title{margin:0;color:var(--ink);font-size:18px;line-height:1.35;font-weight:700}.section-title:before{content:none}.count-pill{padding:3px 8px;border:1px solid var(--line);border-radius:16px;background:#fff;color:#3f534a;font-size:12px}.section ul{display:grid;gap:8px;margin:0;padding:0;list-style:none}.task{position:relative;display:flex;gap:12px;width:100%;min-width:0;padding:16px;background:#fff;border:1px solid var(--line);border-radius:8px;box-shadow:0 3px 8px rgba(22,48,38,.06);transition:transform 180ms var(--ease-in),opacity 180ms var(--ease-in)}
.task.is-focused,.task:focus-within{z-index:2;transform:translateZ(var(--depth-top)) scale(1.012);border-color:#4b8d78}.task[aria-busy=true]{opacity:.72}.task:before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:#667c71}.task.urgent:before,.task.overdue:before{background:var(--red)}.task.action:before{background:#ac771e}.task.academic:before{background:#16816d}.task.overdue{background:var(--red-soft);border-color:#d7a7a0}.task.done{opacity:1;background:#f5f7f5}.task.done .t{text-decoration:line-through;color:#42554e}
.check{position:relative;display:grid;place-items:center;flex:0 0 48px;width:48px;min-width:48px;height:48px;min-height:48px;margin:0;padding:0;border:2px solid #5f796d;border-radius:10px;background:#fff;color:var(--teal);cursor:pointer;transition:transform 80ms var(--ease-in),background-color 120ms ease-out,border-color 120ms ease-out,box-shadow 120ms ease-out,color 120ms ease-out}.check:after{content:"";position:absolute;left:18px;top:15px;width:8px;height:13px;border:2px solid transparent;border-top:0;border-left:0;transform:rotate(42deg);transform-origin:center}.task.done .check{border-color:var(--teal);background:var(--teal)}.task.done .check:after{border-color:white}.candidate-mark{display:grid;place-items:center;flex:0 0 32px;height:32px;border:2px solid var(--amber);border-radius:50%;color:var(--amber);font-weight:700}
.body{flex:1;min-width:0}.card-top{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:8px}.tag,.deadline-chip,.overdue-chip,.snooze-chip{display:inline-flex;align-items:center;min-height:24px;padding:2px 8px;border:1px solid var(--line);border-radius:14px;background:#f5f7f5;color:#344b40;font-size:12px;font-weight:650}.tag.urgent,.overdue-chip{border-color:#cb8c83;background:var(--red-soft);color:#802b27}.tag.action,.tag.candidate,.deadline-chip,.deadline-chip.over{border-color:#d5bb87;background:var(--amber-soft);color:#67480f}.tag.academic{border-color:#94bfb1;background:var(--teal-soft);color:#20584a}.tag.info,.snooze-chip{background:#f0f3f1;color:#344b40}.overdue-chip{background:#a5312d;color:#fff}.t{font-size:16px;line-height:1.5;font-weight:680;overflow-wrap:anywhere;word-break:break-word}.context,.meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;color:#42554e;font-size:13px}.ctx,.group-chip{padding:4px 8px;border:1px solid #d4ddd8;border-radius:5px;background:#f6f8f6;color:#384d43;font-size:12px;overflow-wrap:anywhere}.duplicate-note,.confidence{margin-top:8px;color:#43574e;font-size:13px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.btn,.correct-btn,.connect-actions button,.action-feedback button{min-height:44px;padding:8px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:#1c382e;font-size:13px;font-weight:650;cursor:pointer}.btn.primary,.correct-btn.primary,.connect-actions .primary,#groupbox .primary{border-color:#145f52;background:#145f52;color:#fff}.btn.ghost,.correct-btn.ghost{background:#f4f7f5;color:#344b40}.btn:active,.correct-btn:active,.check:active,.tabs button:active{transform:scale(.98);transition-duration:100ms}.actions .btn{font-size:13px}details{margin-top:8px}summary{display:flex;align-items:center;min-height:44px;color:#345348;font-size:13px;font-weight:600;cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}summary:after{content:"+";margin-left:7px;font-size:16px}details[open]>summary:after{content:"−"}details p{margin:6px 0 0;color:#42554e;font-size:13px;line-height:1.55;overflow-wrap:anywhere}.detail-list{padding-left:20px;color:#344b40;font-size:13px}.detail-list li{margin:4px 0}.correction{padding-top:8px;border-top:1px solid #d7dfda}.correction-hint,.correction-hint~*{color:#42554e}.correction summary{color:#345348}.correct-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}.correct-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.correct-btn{font-size:12px}.correct-select,.correct-date{min-width:0;min-height:44px;padding:8px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink);font-size:13px;color-scheme:light}.empty{padding:20px 12px;border:1px dashed #aab9b0;border-radius:6px;color:#42554e;font-size:14px}.notice{padding:14px;background:#fff;border:1px solid var(--line);border-radius:7px}.notice h3{margin:0;font-size:16px}.notice p{margin:8px 0 0;color:#42554e;font-size:14px;white-space:pre-wrap}
#connect{position:static;top:auto;grid-column:auto;grid-row:auto;transform:translateZ(var(--depth-mid))}#status{padding:12px;border-left:3px solid #9a6b1c;background:#fff6e5;color:#574111;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.connect-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.login-choice{display:grid;gap:4px;margin:12px 0 0;padding:8px 10px;border:1px solid var(--line);border-radius:6px;color:#344b40}.login-choice legend{padding:0 5px;color:#53685e;font-size:12px}.login-choice label{display:flex;align-items:center;gap:8px;min-height:44px;font-size:13px}.login-choice input[type=radio]{width:18px;height:18px;accent-color:var(--teal)}#uin{width:100%;min-height:44px;padding:8px 10px;border:1px solid #9eafa6;border-radius:5px;background:#fff;color:var(--ink)}.login-note{margin:8px 0 0;color:#42554e;font-size:12px;line-height:1.5}.login-note strong{color:#244d3f}.login-more{margin-top:4px}.login-more summary{min-height:44px;color:#20584a}.login-more p{font-size:12px}#steps,#install-result,#result{margin-top:8px;color:#42554e;font-size:13px;overflow-wrap:anywhere}#qrbox{margin-top:12px;padding:12px;border:1px dashed #b2c0b8;border-radius:6px;background:#f7f9f7}#qrbox p{margin:0;color:#42554e;font-size:13px}#qrbox img{display:block;width:min(100%,200px);height:auto;aspect-ratio:1;object-fit:contain;margin:12px auto 0;background:#fff}#groups{display:grid;gap:4px;margin:12px 0}#groups label{display:flex;align-items:center;min-height:44px;gap:8px;border-bottom:1px solid #e0e6e2;font-size:13px}#groups input{width:18px;height:18px;accent-color:var(--teal)}
.month-controls{display:flex;gap:8px}.month-controls .btn{width:44px;padding:0;font-size:21px}.weekday-row{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;margin:0 0 4px;text-align:center;color:#4a6055;font-size:12px;font-weight:650}#calendar{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;transition:transform 220ms var(--ease-in),opacity 220ms var(--ease-in)}.calendar-day{min-width:0;min-height:58px;padding:4px;border:1px solid #d4ddd8;border-radius:5px;background:#f6f8f6;color:#263b32}.calendar-day strong{font-size:13px;font-variant-numeric:tabular-nums}.calendar-event{display:-webkit-box;margin-top:4px;color:#20584a;font-size:12px;line-height:1.25;overflow:hidden;overflow-wrap:anywhere;-webkit-line-clamp:2;-webkit-box-orient:vertical}.calendar-note{margin:8px 0 0;color:#4b5f55;font-size:12px}.action-feedback{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 16px;padding:12px;border:1px solid #99b9aa;border-left:4px solid var(--teal);border-radius:6px;background:#e5f3ec;color:#1f4738;font-size:15px}.action-feedback[data-state=loading]{border-left-color:#a16d1d;background:#fff3da;color:#60440f}.action-feedback[data-state=error]{border-color:#d3a09a;border-left-color:var(--red);background:#fff0ed;color:#702a26}.action-feedback[hidden]{display:none}.action-feedback button{flex:none}
:focus-visible{outline:2px solid #12695b;outline-offset:2px}button:disabled{cursor:not-allowed;opacity:.6}[hidden]{display:none!important}
 .group-tools{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.group-tools .btn{flex:1 1 140px}.group-search{width:100%;min-height:44px;padding:9px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink)}.group-source{margin:8px 0;color:#344b40;font-size:13px}.group-source strong{display:inline-block;padding:3px 8px;border:1px solid #94bfb1;border-radius:14px;background:var(--teal-soft);color:#20584a;font-size:12px}.group-source[data-source=heuristic] strong{border-color:#d5bb87;background:var(--amber-soft);color:#67480f}.group-category{margin:10px 0}.group-category h3{margin:0 0 6px;color:#344b40;font-size:14px}.group-list{display:grid;gap:4px}#groups .group-row{display:flex;align-items:center;min-width:0;min-height:44px;gap:8px;padding:8px;border:1px solid #d4ddd8;border-radius:6px;background:#fff;color:#263b32;font-size:13px;cursor:pointer}#groups .group-row:focus-within{outline:2px solid #12695b;outline-offset:2px}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.group-row input{flex:0 0 18px;width:18px;height:18px;accent-color:var(--teal)}.group-name{min-width:0;overflow-wrap:anywhere}.group-meta{display:flex;flex:1 1 auto;flex-wrap:wrap;align-items:center;gap:6px;min-width:0}.category-badge,.suggest-badge{display:inline-flex;align-items:center;min-height:24px;padding:2px 7px;border:1px solid #c9d4ce;border-radius:12px;background:#f5f8f6;color:#344b40;font-size:12px}.suggest-badge{border-color:#94bfb1;background:var(--teal-soft);color:#20584a}.suggest-reason{flex-basis:100%;color:#42554e;font-size:12px;line-height:1.4;overflow-wrap:anywhere}.group-source-error,#groups-message{margin:8px 0;color:#42554e;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.group-empty-link{display:inline-flex;align-items:center;min-height:44px;margin:4px 0;padding:8px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:#1c382e;font-size:13px;font-weight:650;text-decoration:none}.other-groups{margin-top:12px;padding-top:8px;border-top:1px solid var(--line)}.other-groups>summary{font-weight:700}.group-category[hidden],.group-row[hidden]{display:none!important}.calendar-day{min-height:56px;padding:5px}
@media(min-width:900px){.masthead,main{width:min(100% - 48px,960px)}.masthead{padding-top:24px}.summary{margin-top:24px;padding:16px 24px}.summary h1{font-size:28px}.tabs{margin-top:24px}.workspace{grid-template-columns:minmax(0,1fr);gap:24px}main{padding-top:24px}.surface{padding:16px}}
@media(min-width:900px) and (max-width:1199px){.workspace{grid-template-columns:minmax(0,1fr)}}
@media(min-width:1024px){.masthead,main{width:min(calc(100% - 64px),1760px);max-width:1760px}.workspace{grid-template-columns:minmax(0,1fr) minmax(320px,560px);gap:24px}.section{margin-bottom:12px}.section-head{margin-bottom:4px}.task{padding:12px}.card-top{margin-bottom:4px}.context,.meta{margin-top:4px}.side-rail{grid-column:2;grid-row:1;grid-template-columns:minmax(0,1fr);align-items:start}#calendar-panel{position:static;width:auto;min-width:0;grid-column:auto;grid-row:auto}}
@media(max-width:899px){.workspace{grid-template-columns:minmax(0,1fr);gap:24px}.side-rail{grid-template-columns:minmax(0,1fr);gap:16px}.summary{margin-top:16px}.masthead{padding-top:12px}.tabs{margin-top:12px}}
@media(max-width:480px){.masthead,main{width:calc(100% - 32px)}.summary{padding:16px}.summary h1{font-size:24px}.summary-bottom{align-items:flex-start;flex-direction:column}.bar{width:100%}.tabs button{flex:1;min-width:0;padding-inline:8px}.workspace{gap:16px}.surface{padding:12px}.task{gap:8px;padding:12px 8px}.task .t{font-size:15px}.connect-actions>*{flex:1}.calendar-day{min-height:50px;padding:4px}.calendar-event{font-size:12px}.section{margin-bottom:16px}}
.history-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}.history-controls{display:flex;flex-wrap:wrap;gap:8px;align-items:end}.history-controls label,.inbox-filter label{display:grid;gap:4px;min-width:0;color:#344b40;font-size:13px;font-weight:650}.history-controls input,.inbox-filter input,.inbox-filter select{min-height:44px;min-width:0;padding:8px 10px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink);font:inherit}.history-note{margin:8px 0;color:#344b40;font-size:14px;line-height:1.5}.history-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:4px;max-height:180px;overflow:auto;padding:4px;border:1px solid var(--line);border-radius:6px}.history-groups label{display:flex;align-items:center;gap:8px;min-width:0;min-height:44px;padding:6px 8px;border:1px solid #d4ddd8;border-radius:5px;background:#fff;overflow-wrap:anywhere}.history-groups input{width:18px;height:18px;flex:0 0 18px;accent-color:var(--teal)}.history-actions,.inbox-filter,.inbox-filter-tools,.inbox-job-actions{display:flex;flex-wrap:wrap;align-items:end;gap:8px}.inbox-filter{margin:12px 0;padding:12px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.inbox-filter label{flex:1 1 150px}.inbox-filter label.search{flex:2 1 240px}.history-status,.inbox-empty,.inbox-error{margin:8px 0;color:#344b40;line-height:1.5}.history-job{margin-top:12px;padding:12px;border-left:3px solid var(--teal);background:#eff6f2}.history-job progress{display:block;width:100%;height:12px;margin:8px 0;accent-color:var(--teal)}.inbox-counts{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.inbox-counts button{min-height:44px;padding:8px 12px;border:1px solid #aabbb2;border-radius:6px;background:#fff;color:#233b31;font:inherit}.inbox-counts button[aria-pressed=true]{border-color:var(--teal);background:var(--teal-soft);font-weight:700}.inbox-items{display:grid;gap:8px;margin:0;padding:0;list-style:none}.inbox-item{padding:12px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}.inbox-item h3{margin:0;font-size:16px;line-height:1.45}.inbox-meta{display:flex;flex-wrap:wrap;gap:6px 12px;margin:4px 0;color:#42554e;font-size:13px}.inbox-verdict{display:inline-flex;align-items:center;min-height:24px;padding:2px 8px;border:1px solid #9eafa6;border-radius:14px;background:#fff;color:#233b31;font-size:12px;font-weight:700}.inbox-reason{margin:6px 0;color:#344b40;font-size:14px;line-height:1.5}.inbox-content{margin:4px 0;color:var(--ink);font-size:15px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere}.inbox-content summary{min-height:44px;cursor:pointer;color:#20584a;font-weight:650}.inbox-item .btn{margin-top:4px}.inbox-pagination{display:flex;justify-content:center;margin-top:12px}.inbox-pagination .btn{min-width:140px}
@media(max-width:700px){.history-grid{grid-template-columns:minmax(0,1fr)}.history-actions>*{flex:1 1 140px}.inbox-filter-tools>*{flex:1 1 120px}.inbox-item{padding:12px 0}}
.summary>*{position:relative;z-index:1}.summary::after{content:"";position:absolute;inset:-14px;z-index:-1;border-radius:20px;pointer-events:none;background:radial-gradient(58% 62% at 50% 45%,rgba(18,105,91,.3),rgba(18,105,91,.1) 58%,rgba(18,105,91,0) 78%);opacity:.35;transform:scale(.94);will-change:transform,opacity;animation:summary-glow-breathe 4s ease-in-out 280ms infinite}.summary::before{content:"";position:absolute;left:8%;right:8%;bottom:-13px;height:12px;z-index:0;pointer-events:none;border-radius:50%;background:radial-gradient(50% 50% at 50% 50%,rgba(27,55,44,.4),rgba(27,55,44,0) 72%);filter:blur(4px);opacity:.4;transform:scale(.94);animation:summary-shadow-breathe 4s ease-in-out 280ms infinite}.local-badge i{transform:scale(1);opacity:.62;animation:badge-breathe 4s ease-in-out 600ms infinite}/* 呼吸动效只由两个「不含文字」的图层承担：卡片外圈的光晕 + 卡片下方的地面阴影。卡片本体（含全部文字）保持静止。实测（CDP 冻结动画相位 + 截图边缘能量）只要卡片位移，文字层就会被合成器按小数设备像素重采样，edge_mean 从静止的 6.95 掉到 5.78（纯 translateY）甚至 4.63（translateY + scale(1.004)），观感就是「有时糊有时清晰、字微微闪烁」。光晕用 z-index:-1 压在卡片下面、pointer-events:none，动画只动 opacity/transform（合成器属性），不碰任何绘制属性；幅度必须肉眼可见（只改几个百分点等于「动画没掉了」）。 */
@keyframes summary-glow-breathe{0%,100%{opacity:.35;transform:scale(.94)}50%{opacity:1;transform:scale(1.06)}}@keyframes summary-shadow-breathe{0%,100%{opacity:.4;transform:scale(.94)}50%{opacity:.9;transform:scale(1.06)}}@keyframes badge-breathe{0%,100%{transform:scale(1);opacity:.48;box-shadow:0 0 0 0 rgba(18,105,91,.18)}50%{transform:scale(1.12);opacity:1;box-shadow:0 0 0 5px rgba(18,105,91,.12)}}
/* 轮播高度按最坏内容定死：表头 + 3 条(每条最多 2 行) + 「另有 N 件」1 行 + 间距；保证切换时不顶动下方元素，且不裁掉内容 */
.upcoming-carousel{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;height:200px;box-sizing:border-box;overflow:hidden;margin-top:12px;padding:10px 12px;border:1px solid #b8cbc1;border-radius:8px;background:rgba(255,255,255,.72);color:var(--ink)}.upcoming-main{min-width:0}.upcoming-main .eyebrow{font-size:11px}#upcoming-date{margin:2px 0 4px;font-size:16px;line-height:1.35}.upcoming-tasks{display:flex;flex-direction:column;gap:4px;margin:0;padding:0;list-style:none}.upcoming-tasks li{display:-webkit-box;max-width:72ch;color:#34483f;font-size:13px;line-height:1.35;overflow:hidden;overflow-wrap:anywhere;-webkit-box-orient:vertical;-webkit-line-clamp:2}.upcoming-tasks .upcoming-more{color:var(--muted)}#upcoming-date,#upcoming-tasks{transition:opacity 220ms ease-out,transform 220ms ease-out}#upcoming-carousel.is-switching #upcoming-date,#upcoming-carousel.is-switching #upcoming-tasks{opacity:0;transform:translateY(6px)}.upcoming-controls{display:flex;align-items:center;gap:6px;flex:none}.upcoming-count{min-width:38px;color:var(--muted);font-size:12px;text-align:right}.upcoming-controls button{display:grid;place-items:center;flex:0 0 40px;width:40px;min-width:40px;height:40px;min-height:40px;padding:0}.upcoming-controls svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.notice-feed{margin:0;padding:0 0 0 14px;border-left:2px solid var(--line);list-style:none}.notice-feed .notice{position:relative;display:grid;gap:6px;margin:0;padding:0 0 22px 18px;border:0;border-radius:0;background:transparent;box-shadow:none}.notice-feed .notice::before{content:"";position:absolute;left:-21px;top:7px;width:9px;height:9px;border:2px solid var(--paper);border-radius:50%;background:var(--teal)}.notice-meta{display:flex;align-items:center;justify-content:space-between;gap:12px;color:var(--muted);font-size:12px}.notice-feed .notice h3{width:max-content;max-width:100%;margin:0;padding:2px 8px;border:1px solid #94bfb1;border-radius:12px;background:var(--teal-soft);color:#20584a;font-size:12px;line-height:1.5}.notice-feed .notice p{max-width:72ch;margin:0;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere}
.hosting-settings{padding:16px;border:1px solid var(--line);border-radius:8px;background:var(--paper);color:var(--ink)}.hosting-settings .hosting-warning{border:1px solid #d5bb87;border-left:3px solid var(--amber);background:var(--amber-soft);color:#67480f}.hosting-settings .preference-list{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:12px 0 0;padding:12px;border:1px solid var(--line);border-radius:6px;background:var(--paper-alt)}.hosting-settings .preference-list legend{padding:0 6px;color:var(--muted)}.hosting-settings .preference{min-height:60px;gap:10px;margin:0;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--paper);color:var(--ink)}.hosting-settings .preference input{flex:0 0 18px;width:18px;height:18px;margin:2px 0 0;accent-color:var(--teal)}.hosting-settings .preference strong{display:block;color:var(--ink)}.hosting-settings .preference small{display:block;margin-top:2px;color:var(--muted);font-size:12px}.hosting-settings .hosting-status{border-color:var(--line);background:var(--paper-alt);color:var(--ink)}.hosting-settings .hosting-actions{display:flex;flex-wrap:wrap;gap:8px}.hosting-settings .hosting-actions .primary{border-color:#145f52;background:#145f52;color:#fff}.hosting-settings .setting-note{color:var(--muted)}
.task{transition:opacity 200ms ease-out,border-color 120ms ease-out,box-shadow 120ms ease-out}.task:not(.done):not([aria-busy=true]):hover,.task:not(.done):not([aria-busy=true]).is-focused,.task:not(.done):not([aria-busy=true]):focus-within{z-index:2;transform:none;border-color:#4b8d78;box-shadow:0 8px 16px rgba(22,48,38,.12)}.task.is-focused,.task:focus-within{transform:none}.btn:not(:disabled),.correct-btn:not(:disabled),.tabs button:not(:disabled){transition:background-color 120ms ease-out,border-color 120ms ease-out,color 120ms ease-out,box-shadow 120ms ease-out}.btn:not(:disabled):hover,.correct-btn:not(:disabled):hover,.tabs button:not(:disabled):hover{border-color:#12695b;background-color:var(--teal-soft);color:#173e34}.btn.primary:not(:disabled):hover,.correct-btn.primary:not(:disabled):hover{border-color:#0f554a;background-color:#0f554a;color:#fff}.check:not(:disabled):hover{border-color:var(--teal);background-color:var(--teal-soft);box-shadow:0 0 0 3px rgba(18,105,91,.12)}.task .check:active:not(:disabled){transform:none;background:#d2e9e2;box-shadow:inset 0 0 0 2px rgba(18,105,91,.25)}button:disabled{opacity:.5;cursor:not-allowed}:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid #12695b!important;outline-offset:2px!important}.btn:active:not(:disabled),.correct-btn:active:not(:disabled),.check:active:not(:disabled),.tabs button:active:not(:disabled){transform:scale(.98);transition-duration:80ms}
.task.completion-confirmed .check{border-color:var(--teal);background:var(--teal);color:#fff;box-shadow:0 0 0 3px rgba(18,105,91,.16)}.task.completion-confirmed .check:after{border-color:#fff;animation:check-draw 240ms ease-out both}@keyframes check-draw{from{transform:rotate(42deg) scale(.25);opacity:0}to{transform:rotate(42deg) scale(1);opacity:1}}.task.completing{z-index:5;pointer-events:none;transition:transform 200ms ease-out,opacity 200ms ease-out!important;transform:translateY(-8px) scale(.96)!important;opacity:0!important}
.hosting-settings .preference-list.local-preferences{grid-template-columns:minmax(0,1fr)}
@media(min-width:1920px){.masthead,main{width:min(calc(100% - 96px),1760px);max-width:1760px}.workspace{grid-template-columns:minmax(0,1fr) minmax(320px,680px)}#tab-tasks{min-height:0}.side-rail{grid-template-columns:minmax(0,1fr)}#calendar-panel{width:auto;min-width:0}.task .t,.notice-feed .notice p,.history-note,.setting-note{max-width:72ch}}
@media(max-width:700px){.upcoming-carousel{height:220px;gap:8px;padding:9px}.upcoming-controls{gap:4px}.upcoming-count{min-width:30px;font-size:11px}.upcoming-controls button{flex-basis:36px;width:36px;min-width:36px;height:36px;min-height:36px}.hosting-settings .preference-list{grid-template-columns:minmax(0,1fr)}}
@media(prefers-reduced-motion:reduce){:root{--depth-mid:0px;--depth-top:0px;scroll-behavior:auto}*,*::before,*::after{animation:none!important;transition-duration:0ms!important}.summary{animation:none!important;transform:none!important}.summary::after,.local-badge i,.task.completion-confirmed .check:after{animation:none!important}.camera-enter,.task.is-focused,.task:focus-within,.surface,.task:hover{transform:none!important}.task.completing{opacity:1!important;transform:none!important}.view-screen,.camera-surface,.task,.check,.correct-btn,.tabs button,.bar>i,.btn,.upcoming-carousel{transition-duration:0ms!important}.camera-moving{will-change:auto!important}button:active{transform:none!important}}
html[data-motion=paused] *,html[data-motion=paused] *::before,html[data-motion=paused] *::after{animation:none!important;transition-duration:0ms!important}html[data-motion=paused] .summary{animation:none!important;transform:none!important}html[data-motion=paused] .task:hover,html[data-motion=paused] .task.is-focused,html[data-motion=paused] .task:focus-within,html[data-motion=paused] button:active{transform:none!important}html[data-motion=paused] .task.completing{opacity:1!important;transform:none!important}
.urgent-controls{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px}.urgent-toggle,.urgent-reset{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:8px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:#344b40;font-size:13px;font-weight:650;transition:background-color 120ms ease-out,border-color 120ms ease-out,color 120ms ease-out,box-shadow 120ms ease-out}
/* 不写死宽度：按钮宽度由文字决定，才能和同排其它按钮（.btn）一致 —— 之前写死 min-width: 88px 让「紧急」两个字的按钮比文字宽出一大截，用户报「字体大小和按钮不符」 */
.pin-toggle{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:8px 12px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:#344b40;font-size:13px;font-weight:650;transition:background-color 120ms ease-out,border-color 120ms ease-out,color 120ms ease-out,box-shadow 120ms ease-out}
.pin-toggle[aria-pressed=true]{border-color:#145f52;background:var(--teal-soft);color:#145f52}
.pinned-panel{margin-bottom:16px}.pinned-list{display:grid;gap:8px;margin:0;padding:0;max-height:280px;overflow:auto;list-style:none}.pinned-item{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 10px;border:1px solid #d4ddd8;border-radius:6px;background:#fff;animation:pinned-enter 200ms ease-out both}.pinned-summary{flex:1 1 100%;min-height:24px;color:#1c382e;font-size:14px;font-weight:650;overflow-wrap:anywhere}.pinned-deadline{flex:1 1 auto;color:#53685e;font-size:12px}.pinned-empty{margin:0;color:#42554e;font-size:13px;line-height:1.5}
@keyframes pinned-enter{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
/* 只有本次渲染里新出现的待办卡片才做入场动画：render() 每次都会重建整个列表，若给 .task 直接挂动画，勾选任意一条都会让所有卡片一起抖 */
.task.is-new{animation:task-enter 220ms ease-out both}
@keyframes task-enter{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}.urgent-toggle[aria-pressed=true]{border-color:#a5312d;background:#fff0ed;color:#702a26}.urgent-mode{color:#53685e;font-size:12px}.task.effective-urgent:before{background:var(--red)}.task.effective-urgent{border-color:#d7a7a0}
.sync-card{margin-bottom:16px}.sync-layout{display:grid;grid-template-columns:minmax(220px,320px) minmax(0,1fr);gap:20px;align-items:center}.sync-qr-wrap{display:grid;place-items:center;min-width:0}.sync-qr{display:block;width:min(100%,320px);height:auto;aspect-ratio:1;object-fit:contain;background:#fff}.sync-qr[hidden]{display:none}.sync-copy{min-width:0}.sync-copy p{margin:8px 0;color:#344b40;font-size:13px;line-height:1.5;overflow-wrap:anywhere}.sync-url{display:block;width:100%;min-height:44px;padding:8px 10px;border:1px solid #9eafa6;border-radius:6px;background:#fff;color:var(--ink);font:13px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.sync-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.sync-status{min-height:24px;margin:8px 0 0;color:#344b40;font-size:13px}.sync-status[data-state=error]{color:#8a302a}.sync-status[data-state=success]{color:#145f52}
button:not(:disabled),a[href],summary,select:not(:disabled),input:not(:disabled),label[for],#groups .group-row,.preference,.history-groups label,.login-choice label{cursor:pointer}input[type=text],input[type=search],input[type=url],input[type=number],input[type=date],input[type=datetime-local],textarea{cursor:text}button:disabled,input:disabled,select:disabled{cursor:not-allowed;opacity:.5}
button:not(:disabled):not(.btn):not(.correct-btn):not(.check):not([role=tab]){transition:background-color 100ms ease-out,border-color 100ms ease-out,color 100ms ease-out,box-shadow 100ms ease-out,transform 100ms ease-out,opacity 100ms ease-out}button:not(:disabled):hover{border-color:#12695b;background-color:var(--teal-soft);color:#173e34}.tabs button[aria-selected=true]:hover{box-shadow:inset 0 -2px var(--teal)}a[href],summary,select:not(:disabled),input:not(:disabled),label[for],#groups .group-row,.preference,.history-groups label,.login-choice label{transition:background-color 100ms ease-out,border-color 100ms ease-out,color 100ms ease-out,box-shadow 100ms ease-out,filter 100ms ease-out,transform 100ms ease-out}a[href]:hover{color:#12695b;text-decoration-line:underline;text-decoration-thickness:2px;text-underline-offset:2px}label[for]:hover{color:#12695b}summary:hover{border-radius:4px;background:var(--teal-soft);color:#173e34}select:not(:disabled):hover,input:not(:disabled):not([type=checkbox]):not([type=radio]):hover{border-color:#12695b;box-shadow:0 0 0 2px rgba(18,105,91,.12)}input[type=checkbox]:not(:disabled):hover,input[type=radio]:not(:disabled):hover{filter:brightness(.82)}#groups .group-row:hover,.preference:hover,.history-groups label:hover,.login-choice label:hover{border-color:#12695b;background:var(--teal-soft);box-shadow:0 0 0 2px rgba(18,105,91,.08)}button:not(:disabled):active{transform:scale(.98);transition-duration:100ms}a[href]:active,summary:active,select:not(:disabled):active,input:not(:disabled):active,label[for]:active,#groups .group-row:active,.preference:active,.history-groups label:active,.login-choice label:active{transform:scale(.98);transition-duration:100ms}select:not(:disabled):active,input:not(:disabled):not([type=checkbox]):not([type=radio]):active{border-color:#12695b;box-shadow:0 0 0 2px rgba(18,105,91,.12)}
.sync-card :focus-visible{outline:2px solid #12695b;outline-offset:2px}
@media(max-width:700px){.sync-layout{grid-template-columns:minmax(0,1fr)}.sync-qr{width:min(100%,280px)}}
</style>
</head>
<body>
<header class="masthead">
  <div class="masthead-row">
    <a class="brand" href="/" aria-label="群务台首页"><svg class="brand-mark" viewBox="0 0 512 512" aria-hidden="true" focusable="false"><circle cx="230.4" cy="281.6" r="204.8" fill="#173b34"/><circle cx="399.36" cy="107.52" r="107.52" fill="#FFFFFF"/><circle cx="399.36" cy="107.52" r="76.8" fill="#12695b"/><path d="M112.64 307.2 L184.32 378.88 L276.48 235.52" fill="none" stroke="#FFFFFF" stroke-width="66.56" stroke-linecap="round" stroke-linejoin="round"/></svg><span><strong>群务台</strong><small>QQ 群消息 · 待办与日历</small></span></a>
    <span class="local-badge"><i aria-hidden="true"></i> 本地运行</span>
  </div>
  <section class="summary" aria-labelledby="headline">
    <div class="summary-top"><span class="eyebrow">今日概览</span><span class="progress-label" id="progressLabel">0%</span></div>
    <h1 id="headline">加载中…</h1>
    <p id="subline"></p>
    <section id="upcoming-carousel" class="upcoming-carousel" aria-label="最近到期事项" aria-live="off" hidden>
      <div class="upcoming-main"><span class="eyebrow">最近到期</span><h2 id="upcoming-date"></h2><ul id="upcoming-tasks" class="upcoming-tasks"></ul></div>
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
       <div class="panel-heading"><div><span class="eyebrow">SCHEDULE</span><h2 id="calendar-title">月历</h2></div><div class="month-controls"><button id="cal-prev" class="btn" type="button" aria-label="上一月">‹</button><button id="cal-next" class="btn" type="button" aria-label="下一月">›</button></div></div><div class="weekday-row" aria-hidden="true"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
       <div id="calendar" aria-label="任务月历" role="grid"></div>
       <p class="calendar-note">日期内显示有截止时间的事项</p>
     </section>
     <aside class="side-rail" aria-label="接入与订阅">
      <section id="pinned-panel" class="surface pinned-panel" aria-labelledby="pinned-title">
        <div class="panel-heading"><div><span class="eyebrow">PINNED</span><h2 id="pinned-title">置顶日程</h2></div><span class="panel-index" id="pinned-count" hidden></span></div>
        <p id="pinned-empty" class="pinned-empty" role="status" aria-live="polite">还没有置顶日程。在待办卡片里点「置顶」，就会固定出现在这里。</p>
        <ul id="pinned-list" class="pinned-list" aria-label="置顶日程列表"></ul>
      </section>
      <section id="connect" class="surface connect-panel" aria-labelledby="connect-title">
        <div class="panel-heading"><div><span class="eyebrow">ACCOUNT</span><h2 id="connect-title">QQ 接入</h2></div><span class="panel-index">01</span></div>
        <div id="status" role="status" aria-live="polite">正在检查 QQ 登录状态…</div>
        <div id="install-box" hidden><button id="install-napcat" class="btn" type="button">自动下载并安装 NapCat</button><p>NapCat 会安装到独立数据目录，不会动你平时使用的电脑版 QQ；移除该目录即可卸载。</p><p id="install-result" role="status"></p></div>
        <div class="connect-actions"><button id="auto-setup" class="btn ghost" type="button">一键接入</button><button id="go" class="btn primary" type="button" aria-label="启动 NapCat 并登录">启动并登录</button><button id="refresh-qr" class="btn" type="button">刷新二维码</button></div>
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
  if (/10061|积极拒绝|Connection refused|ECONNREFUSED/i.test(text)) return 'QQ 未登录：NapCat 没连上（连接被拒绝）。请用手机 QQ 扫描下方二维码登录。';
  if (/10060|timed out|超时/i.test(text)) return '连接超时：QQ 或 NapCat 没有响应，请稍后重试。';
  if (/Failed to fetch|NetworkError|Load failed|不能连接到远程服务器/i.test(text)) return '本机服务没有响应，请确认 notice-hub 正在运行。';
  return text || '未知错误';
}
var setupToken = new URLSearchParams(location.search).get('token') || localStorage.getItem('qq_digest_token') || '';
function setupApi(path, options) { options = options || {}; options.headers = Object.assign({'X-Token': setupToken}, options.headers || {}); return fetch(path, options).then(function(r){ return r.json().then(function(x){ if(x && typeof x.error === 'string' && x.error) x.error = friendlyError(x.error); if(!r.ok) throw Error(x.error || '请求失败'); return x; }); }); }
function refreshQr() { var image = document.getElementById('qr'); var note = document.getElementById('qr-note'); if (!image) return; image.hidden = true; image.onload = function () { image.hidden = false; if (note) note.hidden = true; }; image.onerror = function () { image.hidden = true; if (note) { note.hidden = false; note.textContent = '二维码暂不可用。请启动 NapCat 后重试。'; } }; image.src = '/api/napcat/qrcode?token=' + encodeURIComponent(setupToken) + '&t=' + Date.now(); }
function checkSetup() { setupApi('/api/napcat/status').then(function(x){ if (!x.ok) throw Error(x.error || '连接不可用'); var s = document.getElementById('status'); var ok = !!(x.online || x.nickname); s.textContent = ok ? 'QQ 已连接：' + (x.nickname || '在线') : '先登录 QQ 才能读取群列表'; document.getElementById('qrbox').hidden = ok; document.getElementById('install-box').hidden = !!x.napcat_installed; document.getElementById('groupbox').hidden = false; document.getElementById('groups-message').textContent = ok ? '正在读取群列表…' : '先登录 QQ 才能读取群列表。'; document.getElementById('groups-connect-link').hidden=ok; if(ok) loadGroups(); }).catch(function(e){ var raw = String(e && e.message || e); var refused = /10061|积极拒绝|连接被拒绝|QQ 未登录|Failed to fetch|NetworkError|Load failed/.test(raw); var s = document.getElementById('status'); s.textContent = refused ? 'QQ 未登录：NapCat 在运行，但还没有可用的登录状态。请用手机 QQ 扫描下方二维码登录（二维码每 5 秒自动刷新）。' : '连接检查失败：' + raw; s.title = raw; document.getElementById('qrbox').hidden = false; document.getElementById('groupbox').hidden = false; document.getElementById('groups-message').textContent = refused ? '先登录 QQ 才能读取群列表。' : '群列表暂不可用：' + raw; document.getElementById('groups-connect-link').hidden=false; }); }
function installNapcat(){var b=document.getElementById('install-napcat');b.disabled=true;document.getElementById('install-result').textContent='下载中 / 解压中，请稍候…';setupApi('/api/napcat/install',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}}).then(function(x){if(!x.ok)throw Error(x.error||'安装失败');document.getElementById('install-result').textContent='已安装，点「启动 NapCat 并登录」继续';checkSetup();}).catch(function(e){document.getElementById('install-result').textContent='安装失败：'+e.message;}).then(function(){b.disabled=false;});}
function groupCategoryName(category) { return ({course:'课程通知',activity:'活动通知',market:'交易群',chat:'聊天群',other:'其它'})[category] || '其它'; }
function addGroupRow(list, group, suggested, selected) { var label=document.createElement('label'); label.className='group-row'; label.dataset.suggested=suggested?'true':'false'; label.dataset.search=(group.name+' '+group.group_id).toLocaleLowerCase(); var input=document.createElement('input'); input.type='checkbox'; input.value=group.group_id; input.checked=selected; input.setAttribute('aria-label','订阅 '+group.name+'，群号 '+group.group_id); label.appendChild(input); var meta=document.createElement('span'); meta.className='group-meta'; meta.appendChild(document.createElement('span')).className='group-name'; meta.lastChild.textContent=group.name+'（'+group.group_id+'）'; meta.appendChild(document.createElement('span')).className='category-badge'; meta.lastChild.textContent=groupCategoryName(group.category); if(suggested){meta.appendChild(document.createElement('span')).className='suggest-badge';meta.lastChild.textContent='建议订阅';} if(group.reason){var reason=document.createElement('span');reason.className='suggest-reason';reason.textContent=group.reason;meta.appendChild(reason);} label.appendChild(meta); list.appendChild(label); }
function renderGroupFilter() { var input=document.getElementById('group-search'),query=input.value.trim().toLocaleLowerCase(),details=document.querySelector('#groups details.other-groups'); if(details) details.open=!!query; var rows=document.querySelectorAll('#groups .group-row'); rows.forEach(function(row){row.hidden=!!query&&row.dataset.search.indexOf(query)<0;}); document.querySelectorAll('#groups .group-category').forEach(function(section){section.hidden=!section.querySelector('.group-row:not([hidden])');}); if(details) details.hidden=!details.querySelector('.group-row:not([hidden])'); }
function loadGroups() { var message=document.getElementById('groups-message'),controls=document.getElementById('groupbox-controls');message.textContent='正在读取群列表…';controls.hidden=true;Promise.all([setupApi('/api/groups/suggest'),setupApi('/api/napcat/groups')]).then(function(results){var suggestion=results[0],persisted=results[1];if(!suggestion.ok)throw Error(suggestion.error||'群组建议暂不可用');if(!Array.isArray(suggestion.groups)||!persisted.ok||!Array.isArray(persisted.groups))throw Error('群组数据格式无效');var saved=Object.create(null);persisted.groups.forEach(function(g){if(g&&g.group_id!==undefined&&g.group_id!==null)saved[String(g.group_id)]=g;});var normalized=suggestion.groups.map(function(g){if(!g||g.group_id===undefined||g.group_id===null)throw Error('群组数据缺少群号');return {group_id:String(g.group_id),name:String(g.name||g.group_id),category:['course','activity','market','chat','other'].indexOf(g.category)>=0?g.category:'other',suggested:g.suggested===true&&['course','activity'].indexOf(g.category)>=0,reason:String(g.reason||'')};});var ids=Object.create(null);normalized.forEach(function(g){ids[g.group_id]=true;});Object.keys(saved).forEach(function(id){if(!ids[id])normalized.push({group_id:id,name:String(saved[id].name||id),category:'other',suggested:false,reason:''});});if(!normalized.length)throw Error('暂未读取到群组，请确认 QQ 已登录后重试。');var root=document.getElementById('groups');root.textContent='';var source=document.getElementById('group-source');source.textContent='';source.hidden=false;source.dataset.source=suggestion.source==='llm'||suggestion.source==='heuristic'?suggestion.source:'unknown';var sourceName=suggestion.source==='llm'?'AI 识别':suggestion.source==='heuristic'?'按关键词识别':'来源未提供';var strong=document.createElement('strong');strong.textContent=sourceName;source.appendChild(strong);if(suggestion.error){var error=document.createElement('span');error.className='group-source-error';error.textContent=' '+String(suggestion.error);source.appendChild(error);}var suggested=normalized.filter(function(g){return g.suggested;});['course','activity'].forEach(function(category){var members=suggested.filter(function(g){return g.category===category;});if(!members.length)return;var section=document.createElement('section');section.className='group-category';var heading=document.createElement('h3');heading.textContent=groupCategoryName(category)+'（'+members.length+'）';section.appendChild(heading);var list=document.createElement('div');list.className='group-list';members.forEach(function(g){var hasSaved=Object.prototype.hasOwnProperty.call(saved,g.group_id)&&Object.prototype.hasOwnProperty.call(saved[g.group_id],'selected');addGroupRow(list,g,true,hasSaved?!!saved[g.group_id].selected:true);});section.appendChild(list);root.appendChild(section);});var others=normalized.filter(function(g){return !g.suggested;});if(others.length){var details=document.createElement('details');details.className='group-category other-groups';var summary=document.createElement('summary');summary.textContent='其它 '+others.length+' 个群（默认不订阅）';details.appendChild(summary);var list=document.createElement('div');list.className='group-list';others.forEach(function(g){var hasSaved=Object.prototype.hasOwnProperty.call(saved,g.group_id)&&Object.prototype.hasOwnProperty.call(saved[g.group_id],'selected');addGroupRow(list,g,false,hasSaved&&!!saved[g.group_id].selected);});details.appendChild(list);root.appendChild(details);}controls.hidden=false;document.getElementById('groupbox').hidden=false;renderGroupFilter();if(!normalized.length){message.textContent='没有读取到群组。先确认 QQ 已登录，再重试。';}else{message.textContent='群组 '+normalized.length+' 个；已订阅状态已载入。';}}).catch(function(e){message.textContent='群列表读取失败：'+e.message;document.getElementById('groups-connect-link').hidden=false;controls.hidden=false;}); }
function runAutoSetup() { var button=document.getElementById('auto-setup'),steps=document.getElementById('steps'),status=document.getElementById('status'); button.disabled=true;steps.textContent='正在检查并配置 NapCat…';setupApi('/api/napcat/autosetup',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}}).then(function(result){var lines=(result.steps||[]).map(function(step){return (step.ok?'✓ ':'! ')+(step.name||'步骤')+(step.detail?'：'+step.detail:'');});steps.textContent=lines.join('；');if(!result.ok)throw new Error(result.error||'一键接入未完成');if(result.restart_required){status.textContent='接入设置已更新，需要重启 notice-hub 后生效。';steps.textContent+=(steps.textContent?'；':'')+'请重启后重新检查 QQ 接入。';}else{status.textContent='一键接入已完成，请检查登录状态。';checkSetup();refreshQr();}}).catch(function(error){status.textContent='一键接入失败：'+error.message;}).then(function(){button.disabled=false;}); }
function run() { var button=document.getElementById('go'); var chosen=document.querySelector('input[name="login-method"]:checked').value; var uin=chosen==='uin' ? document.getElementById('uin').value.trim() : ''; button.disabled=true; document.getElementById('steps').textContent='正在启动 NapCat…'; setupApi('/api/napcat/launch',{method:'POST',body:JSON.stringify({uin:uin}),headers:{'Content-Type':'application/json'}}).then(function(x){ if(x.already_running){ document.getElementById('steps').textContent='NapCat 已经在运行'; } else if(x.ok){ document.getElementById('steps').textContent='已启动，正在等二维码…'; refreshQr(); } else { throw Error(x.error || '启动失败'); } checkSetup(); }).catch(function(e){document.getElementById('steps').textContent='启动失败：'+e.message;}).then(function(){button.disabled=false;}); }
document.getElementById('auto-setup').onclick=runAutoSetup; document.getElementById('go').onclick=run; document.getElementById('refresh-qr').onclick=refreshQr; document.getElementById('install-napcat').onclick=installNapcat; document.querySelectorAll('input[name="login-method"]').forEach(function(radio){radio.onchange=function(){document.getElementById('uin').disabled=radio.value!=='uin';};});
document.getElementById('group-search').addEventListener('input',function(event){if(!event.isComposing)renderGroupFilter();});document.getElementById('group-search').addEventListener('compositionend',renderGroupFilter);document.getElementById('retry-groups').onclick=loadGroups;document.getElementById('select-suggested').onclick=function(){document.querySelectorAll('#groups .group-row[data-suggested="true"] input').forEach(function(input){input.checked=true;});};document.getElementById('clear-groups').onclick=function(){document.querySelectorAll('#groups input[type="checkbox"]').forEach(function(input){input.checked=false;});};
document.getElementById('save').onclick=function(){var save=document.getElementById('save');var groups=[].slice.call(document.querySelectorAll('#groups input:checked')).map(function(i){return i.value;});if(!groups.length&&!window.confirm('未选择任何群，将清空所有订阅。确认继续？'))return;save.disabled=true;document.getElementById('result').textContent='正在保存…';setupApi('/api/subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({groups:groups})}).then(function(result){if(!result.applied)throw Error(result.error||'服务端未保存订阅');document.getElementById('result').textContent='订阅已保存并生效';}).catch(function(e){document.getElementById('result').textContent='保存失败：'+e.message;}).then(function(){save.disabled=false;});}; refreshQr(); checkSetup();
// 未连接时每 5 秒重取二维码并复查登录态：扫码用的二维码必须是最新的，否则用户扫到过期二维码会一直登录失败。
// 连上后 #qrbox 会被隐藏，轮询自动停止，避免反复重载群列表覆盖用户已勾选的订阅。
window.setInterval(function () { var box = document.getElementById('qrbox'); if (box && !box.hidden) { refreshQr(); checkSetup(); } }, 5000);
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
    var date = new Date(group.date + 'T00:00:00');
    var dayAfterToday = new Date();
    dayAfterToday.setHours(0, 0, 0, 0);
    dayAfterToday.setDate(dayAfterToday.getDate() + 1);
    var dateText = date.toLocaleDateString('zh-CN', {month: 'long', day: 'numeric', weekday: 'short'});
    document.getElementById('upcoming-date').textContent = group.date === localDateStamp(dayAfterToday) ? '明天 · ' + dateText : dateText;
    document.getElementById('upcoming-count').textContent = group.tasks.length + ' 件';
    var list = document.getElementById('upcoming-tasks');
    list.textContent = '';
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
function renderUpcoming(data, dueTodayCount) {
  var root = document.getElementById('upcoming-carousel');
  stopUpcomingRotation();
  if (dueTodayCount > 0) { upcomingGroups = []; root.hidden = true; return; }
  var today = localDateStamp(new Date());
  var byDate = Object.create(null);
  (data.week || []).concat(data.later || []).forEach(function (task) {
    if (!task || task.done || !task.deadline) return;
    var deadline = new Date(String(task.deadline));
    if (!Number.isFinite(deadline.getTime())) return;
    var key = localDateStamp(deadline);
    if (key <= today) return;
    if (!byDate[key]) byDate[key] = [];
    byDate[key].push(task);
  });
  var currentDate = upcomingGroups[upcomingIndex] && upcomingGroups[upcomingIndex].date;
  upcomingGroups = Object.keys(byDate).sort().map(function (date) {
    byDate[date].sort(function (a, b) { return String(a.deadline).localeCompare(String(b.deadline)); });
    return {date: date, tasks: byDate[date]};
  });
  upcomingIndex = Math.max(0, upcomingGroups.findIndex(function (group) { return group.date === currentDate; }));
  if (upcomingGroups.length) renderUpcomingSlide();
  else root.hidden = true;
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
  var todayBlock = section('今天', dueToday);
  var overdueBlock = section('已过期', overdue, {className: 'overdue', collapsed: true});
  var weekBlock = section('本周', data.week || []);
  var candidateBlock = section('待确认', data.candidates || []);
  var laterBlock = section('以后', data.later || []);
  var doneBlock = section('已完成', data.done || [], {className: 'done', collapsed: true});
  var taskBlocks = [todayBlock, weekBlock, candidateBlock, laterBlock, doneBlock].filter(Boolean);
  var blocks = [todayBlock, calendarPanel, overdueBlock, weekBlock, candidateBlock, laterBlock, doneBlock].filter(Boolean);
  var root = document.getElementById('tab-tasks');
  root.textContent = '';
  if (!taskBlocks.length) root.appendChild(el('div', 'empty', '今天没有需要处理的事项'));
  blocks.forEach(function (block) { root.appendChild(block); });
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

function loadSettings() {
  var root = document.getElementById('tab-settings');
  root.textContent = '';
  var syncBox = el('section', 'surface sync-card');
  syncBox.setAttribute('aria-labelledby', 'sync-title');
  syncBox.innerHTML='<div class="section-head"><h2 id="sync-title" class="section-title">iPhone 日历订阅</h2><span id="sync-events" class="count-pill">读取事项数…</span></div><div class="sync-layout"><div class="sync-qr-wrap"><img id="sync-qr" class="sync-qr" alt="iPhone 日历订阅二维码" hidden><p id="sync-qr-message" class="sync-status" role="status" aria-live="polite">正在生成二维码…</p></div><div class="sync-copy"><label for="sync-url">订阅地址</label><input id="sync-url" class="sync-url" type="url" autocomplete="url" spellcheck="false" aria-describedby="sync-events sync-help"><div class="sync-actions"><button id="sync-copy" class="btn" type="button">复制订阅链接</button><button id="sync-test" class="btn" type="button">测试地址</button></div><p id="sync-help">日历按 iPhone 的计划刷新，不是实时推送；可在“设置 → 日历 → 账户 → 已订阅的日历”调整刷新频率，也可在日历 App 下拉刷新。手机需能访问此地址（同一 Wi-Fi 或公网地址）。订阅地址包含访问令牌，令牌轮换后需重新订阅。</p><p id="sync-status" class="sync-status" role="status" aria-live="polite"></p></div></div>';
  root.appendChild(syncBox);
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
      setStatus('正在读取日历地址…', '');
      fetch(requestUrl, {method: 'GET', cache: 'no-store'}).then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        if (!(response.headers.get('Content-Type') || '').toLowerCase().includes('text/calendar')) throw new Error('地址没有返回 text/calendar');
        return response.text();
      }).then(function (body) {
        var calendarLines = body.split('\\n').map(function (line) { return line.trim(); });
        if (calendarLines[0] !== 'BEGIN:VCALENDAR') throw new Error('地址返回内容不是 iCalendar');
        var count = calendarLines.filter(function (line) { return line === 'BEGIN:VEVENT'; }).length;
        setStatus('地址可读取，包含 ' + count + ' 条事项。', 'success');
      }).catch(function (error) {
        setStatus('无法读取此地址：' + (error.message || '网络或跨域请求失败'), 'error');
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
      renderSyncQr();
      setStatus('订阅地址已生成。', '');
    }).catch(function (error) {
      events.textContent = '事项数暂不可用';
      setStatus('无法确定局域网订阅地址：' + (error.message || '请粘贴公网地址重试'), 'error');
      renderSyncQr();
    });
  })();
  var hostingBox = document.createElement('div'); hostingBox.className='hosting-settings';
  hostingBox.innerHTML='<h2 class="section-title">托管设置</h2><div class="hosting-warning" role="note"><strong>启用退出选项后，开始托管会关闭电脑版 QQ；结束时可按恢复选项重新启动。</strong></div><fieldset class="preference-list"><legend>自动化选项</legend><label class="preference"><input id="pref-quit_qq" type="checkbox"><span><strong>开始托管前退出电脑版 QQ</strong><small>避免桌面 QQ 与独立登录同时占用账号。</small></span></label><label class="preference"><input id="pref-restore_qq" type="checkbox"><span><strong>结束托管后恢复电脑版 QQ</strong><small>结束托管时重新启动电脑版 QQ。</small></span></label><label class="preference"><input id="pref-auto_on_start" type="checkbox"><span><strong>启动 notice-hub 时自动开始托管</strong><small>启动应用后立即按上述选项接管。</small></span></label><label class="preference"><input id="pref-autostart" type="checkbox"><span><strong>开机自动启动 notice-hub</strong><small>随系统启动此本地待办服务。</small></span></label></fieldset><fieldset class="preference-list"><legend>历史回溯</legend><label class="preference"><input id="pref-catchup-enabled" type="checkbox" disabled><span><strong>启动时自动回溯最近 N 天</strong><small>只影响应用启动时的补采行为，不会立即回溯。</small></span></label><label class="preference"><span><strong>回溯天数</strong><small>保存为现有的小时设置。</small></span><input id="pref-catchup-days" type="number" disabled min="1" max="365" step="1" value="1" aria-label="启动时自动回溯最近多少天"></label><button id="pref-catchup-save" class="btn" type="button" disabled>保存回溯设置</button><p id="pref-catchup-result" role="status" aria-live="polite"></p></fieldset><p class="setting-note">托盘图标也可用于开始或结束托管。</p><div id="hosting-status" class="hosting-status" role="status" aria-live="polite">正在读取托管状态…</div><div class="hosting-actions"><button id="hosting-start" class="btn primary" type="button">开始托管</button><button id="hosting-stop" class="btn" type="button">结束托管</button></div><fieldset class="preference-list local-preferences"><legend>界面动效</legend><label class="preference"><input id="pref-motion-enabled" type="checkbox"><span><strong>轻微动效</strong><small id="pref-motion-note"></small></span></label></fieldset><p id="hosting-result" role="status" aria-live="polite"></p>';
  root.appendChild(hostingBox);
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
  api('/api/meta').then(function (data) {
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
 function renderCalendar(direction){var root=document.getElementById('calendar'); if(!root)return; root.textContent=''; var y=cursor.getFullYear(),m=cursor.getMonth(),first=new Date(y,m,1).getDay(),days=new Date(y,m+1,0).getDate(); document.getElementById('calendar-title').textContent='月历 · '+y+'年'+(m+1)+'月'; for(var i=0;i<first;i++)root.appendChild(document.createElement('div')); for(var d=1;d<=days;d++){var cell=document.createElement('div');cell.className='calendar-day';var strong=document.createElement('strong');strong.textContent=d;cell.appendChild(strong);calendarTasks.forEach(function(t){if(String(t.deadline||'').slice(0,10)===y+'-'+String(m+1).padStart(2,'0')+'-'+String(d).padStart(2,'0')){var e=document.createElement('span');e.className='calendar-event';e.textContent=t.summary||'未命名任务';cell.appendChild(e);}});root.appendChild(cell);}if(direction)animateSurface(root,direction*12);}
 window.syncCalendarTasks=function(data){calendarTasks=[].concat(data.today||[],data.week||[],data.later||[],data.done||[],data.candidates||[]);renderCalendar();};
 document.getElementById('cal-prev').onclick=function(){cursor.setMonth(cursor.getMonth()-1);renderCalendar(-1);}; document.getElementById('cal-next').onclick=function(){cursor.setMonth(cursor.getMonth()+1);renderCalendar(1);}; renderCalendar();
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
        self.end_headers()
        self.wfile.write(raw)

    def _png(self, raw: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
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

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/health", "/api/health"):
            self._json(200, {"ok": True, "service": "qq-tasks"})
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
            if not status.get("ok") or not login.get("ok"):
                error = status.get("error") or login.get("error") or "NapCat error"
                boot = napcat_admin.detect_boot() if napcat_admin is not None else None
                self._json(200, {"ok": False, "online": False, "good": False, "user_id": "", "nickname": "", "napcat_installed": bool(boot), "napcat_root": (boot or {}).get("data_dir"), "error": error})
            else:
                sd, ld = status["data"], login["data"]
                boot = napcat_admin.detect_boot() if napcat_admin is not None else None
                self._json(200, {"ok": True, "online": bool(sd.get("online")), "good": bool(sd.get("good")), "user_id": sd.get("user_id") or ld.get("user_id", ""), "nickname": ld.get("nickname", ""), "napcat_installed": bool(boot), "napcat_root": (boot or {}).get("data_dir"), "error": ""})
            return
        if path == "/api/groups/suggest":
            if group_suggest is None:
                self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": "群组建议模块不可用"}); return
            try:
                raw = self._napcat("get_group_list")
                groups = raw.get("data", []) if raw.get("ok") else []
                if not raw.get("ok"):
                    self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": raw.get("error", "NapCat error")}); return
                self._json(200, group_suggest.suggest(self.server.settings, groups))  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001
                self._json(200, {"ok": False, "source": "heuristic", "groups": [], "counts": {}, "error": str(error)})
            return
        if path == "/api/napcat/groups":
            result = self._napcat("get_group_list")
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
                lan_base = f"http://{_lan_ipv4()}:{self.server.server_address[1]}"
                token = str(getattr(self.server, "token", "") or "")
                calendar_url = lan_base + "/calendar.ics"
                if token:
                    calendar_url += "?token=" + urllib.parse.quote(token, safe="")
                events = sum(1 for line in render_calendar(self.store.list_tasks()).splitlines() if line == "BEGIN:VEVENT")
            except OSError:
                LOGGER.warning("无法确定 iPhone 日历订阅地址")
                self._json(503, {"ok": False, "error": "无法确定可供手机访问的局域网地址"})
                return
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("读取日历订阅信息失败")
                self._json(500, {"ok": False, "error": str(error) or "读取日历订阅信息失败"})
                return
            self._json(200, {"ok": True, "lan_base": lan_base, "port": self.server.server_address[1], "events": events, "calendars": [{"name": "QQ 任务日历", "url": calendar_url, "events": events}]})
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
        if path == "/api/tasks":
            now = now_local()
            tasks = self.store.list_tasks()
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
                    "stats": self.store.task_stats(),
                },
            )
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        if not self._authorized():
            self._json(401, {"ok": False, "error": "invalid token"})
            return
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
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
            if catchup_keys and host_keys:
                self._json(400, {"ok": False, "error": "托管设置与回溯设置请分开保存"}); return
            try:
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
