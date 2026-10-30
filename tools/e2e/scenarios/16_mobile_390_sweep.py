"""390 窄屏巡场：四个 tab、设置卡片、收件箱两段删除，每屏都不许横向溢出、不许被吸顶栏盖住。

为什么要这条：窄屏溢出与吸顶栏遮挡在历史上漏到过用户手上（按钮看得见、点不到 / 页面能左右拖）。

依赖：run.py 需要为 `fakes:onebot` 注入
`QQ_DIGEST_ONEBOT_ENABLED=1`、`QQ_DIGEST_ONEBOT_PORT=<随机>`、`QQ_DIGEST_ONEBOT_TOKEN=<随机>`，
剧本据此直接 POST 一条 OneBot v11 群消息事件（不需要真 NapCat）。

复现：
    python tools/e2e/run.py --only 16_mobile_390_sweep
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from harness import scenario

GROUP_ID = "10001"
MESSAGES = [
    "明天上午九点在图书馆门口集合，带好学生证。",
    "周五下午三点体育馆排练，提前十分钟到。",
    "今晚八点线上班会，腾讯会议号 123-456-789。",
]

LAYOUT_JS = """(function(){
  var de=document.scrollingElement||document.documentElement;
  var overflow=Math.round(de.scrollWidth-innerWidth);
  var bad=[];
  function label0(el){return (el.id?'#'+el.id:el.tagName.toLowerCase())+'['+(el.getAttribute('aria-label')||el.textContent||'').trim().slice(0,18)+']';}
  function fixedAncestor(el){
    for(var n=el;n&&n!==document.documentElement;n=n.parentElement){
      var p=getComputedStyle(n).position;
      if(p==='fixed'||p==='sticky')return n;
    }
    return null;
  }
  function desc(hit){
    var hs=getComputedStyle(hit), hr=hit.getBoundingClientRect();
    return (hit.id?'#'+hit.id:hit.tagName.toLowerCase()+'.'+String(hit.className||'').split(' ')[0])
      +' pos='+hs.position+' z='+hs.zIndex
      +' '+Math.round(hr.left)+','+Math.round(hr.top)+' '+Math.round(hr.width)+'x'+Math.round(hr.height);
  }
  function probe(el){
    var r=el.getBoundingClientRect();
    var x=Math.min(Math.max(r.left+r.width/2,1),innerWidth-1);
    var y=Math.min(Math.max(r.top+r.height/2,1),innerHeight-1);
    var hit=document.elementFromPoint(x,y);
    if(!hit)return {kind:'null'};
    if(el===hit||el.contains(hit)||hit.contains(el))return null;
    return {kind:'hit',hit:hit};
  }
  var nodes=document.querySelectorAll('button,a,input,select,textarea,[role="tab"]');
  Array.prototype.forEach.call(nodes,function(el){
    var s=getComputedStyle(el);
    if(s.display==='none'||s.visibility==='hidden'||parseFloat(s.opacity||'1')<0.05)return;
    if(el.disabled)return;
    // 祖先被 hidden/display:none 收起来的（比如关掉的首访向导）不属于这一屏。
    if(el.checkVisibility && !el.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return;
    var r=el.getBoundingClientRect();
    // 完全不在这一屏：纵向落在首屏之外属于正常滚动，不算问题。
    if(r.bottom<0||r.top>innerHeight||r.right<0||r.left>innerWidth)return;
    var name=label0(el);
    // ① 横向溢出被裁
    if(r.left<-1||r.right>innerWidth+1){bad.push(name+' 横向被裁 left='+Math.round(r.left)+' right='+Math.round(r.right)+' vw='+innerWidth);return;}
    // ② 尺寸为 0 / 不可点：只算「有名字、本该能点」的控件，纯隐藏 input 不算。
    if(r.width<4||r.height<4){
      if((el.getAttribute('aria-label')||el.getAttribute('title')||(el.textContent||'').trim())){
        bad.push(name+' 尺寸为 0（'+Math.round(r.width)+'x'+Math.round(r.height)+'）');
      }
      return;
    }
    if(s.pointerEvents==='none'&&el.tagName!=='INPUT'&&el.tagName!=='SELECT'&&el.tagName!=='TEXTAREA'){bad.push(name+' pointer-events:none（点不到）');return;}
    // ③ 被盖住：普通元素盖住=不可点；固定元素（顶栏/底栏/浮球）盖住要滚一下再看，滚开就不算
    var c=probe(el);
    if(!c)return;
    if(c.kind==='null'){bad.push(name+' 点在视口内但 elementFromPoint 返回 null（被透明层整块盖住）');return;}
    if(!fixedAncestor(c.hit)){bad.push(name+' 被普通元素盖住：'+desc(c.hit)+' scrollY='+Math.round(scrollY));return;}
    var before=scrollY;
    el.scrollIntoView({block:'center',inline:'nearest',behavior:'instant'});
    var c2=probe(el);
    window.scrollTo(0,before);
    if(!c2)return;                                   // 滚一下就能点到：正常滚动，不算问题
    if(c2.kind==='null'){bad.push(name+' 滚动后 elementFromPoint 返回 null');return;}
    bad.push(name+' 被固定元素 '+desc(c2.hit)+' 盖住且滚不到');
  });
  return {overflow:overflow, inner:innerWidth, bad:bad.slice(0,8)};
})()"""


def _env(app) -> dict[str, str]:
    path = Path(app.install.install_dir) / ".env"
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _inject_group_messages(app) -> None:
    """往实例的 OneBot 接收端推 3 条群消息（走产品自己的入库路径）。"""
    env = _env(app)
    if env.get("QQ_DIGEST_ONEBOT_ENABLED", "0") not in ("1", "true", "True"):
        raise AssertionError(
            "冷装实例没有启用 OneBot 接收：run.py 需要为 fakes:onebot 注入 "
            "QQ_DIGEST_ONEBOT_ENABLED=1 / _PORT / _TOKEN（剧本不带私货去改 .env）"
        )
    port = int(env.get("QQ_DIGEST_ONEBOT_PORT", "0") or 0)
    token = env.get("QQ_DIGEST_ONEBOT_TOKEN", "")
    if port <= 0:
        raise AssertionError("冷装实例的 .env 没有可用的 QQ_DIGEST_ONEBOT_PORT")

    subscribed = app.api_post("/api/subscriptions", {"groups": [GROUP_ID]})
    if not subscribed.get("ok"):
        raise AssertionError(f"订阅群没设上，消息会被白名单丢掉：{subscribed}")

    for index, text in enumerate(MESSAGES, start=1):
        event = {
            "post_type": "message", "message_type": "group",
            "time": 1761800000 + index, "self_id": 111,
            "user_id": 2001, "group_id": int(GROUP_ID), "message_id": 9000 + index,
            "raw_message": text, "message": [{"type": "text", "data": {"text": text}}],
            "sender": {"user_id": 2001, "nickname": "辅导员", "card": "辅导员"},
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/onebot/event",
            data=json.dumps(event, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + token} if token else {})},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
        if not body.get("inserted"):
            raise AssertionError(f"第 {index} 条群消息没入库：{body}")


def _classify(app) -> None:
    """把刚注入的消息过一遍通知判定，收件箱才会有行。"""
    started = app.api_post("/api/inbox/classify", {"groups": [GROUP_ID], "limit": 50})
    if not started.get("ok"):
        raise AssertionError(f"通知判定没启动：{started}")
    deadline = time.monotonic() + 60
    status = {}
    while time.monotonic() < deadline:
        status = app.api_get("/api/inbox/classify/status")
        if not status.get("running"):
            if status.get("error"):
                raise AssertionError(f"通知判定失败：{status.get('error')}")
            return
        time.sleep(0.3)
    raise AssertionError(f"通知判定 60s 内没跑完，最后一次状态：{status}")


def _dismiss_wizard(app) -> None:
    visible = app.page.evaluate(
        "(function(){var w=document.getElementById('platform-setup-wizard');return !!(w&&!w.hidden);})()"
    )
    if visible:
        app.click("#platform-setup-wizard .platform-setup-later")
        app.wait_for("#platform-setup-wizard", state="hidden", timeout=10)


def _assert_layout(app, label: str) -> None:
    # 每次量之前先回到页顶：页面底部/吸顶栏的滚动态不是这一屏要验的版式，
    # 而且「滚出视口的残条压在吸顶栏下」是正常现象，不该算遮挡。
    app.page.evaluate(
        "(function(){var de=document.scrollingElement||document.documentElement;"
        "if(de&&de.scrollTo){de.scrollTo({top:0,left:0,behavior:'instant'});}else{window.scrollTo(0,0);}})()"
    )
    app.settle(0.25)
    result = app.page.evaluate(LAYOUT_JS) or {}
    overflow = int(result.get("overflow") or 0)
    if overflow > 1:
        raise AssertionError(f"{label}：390 宽度出现横向溢出 {overflow}px（scrollWidth > innerWidth）")
    bad = result.get("bad") or []
    if bad:
        raise AssertionError(f"{label}：有控件横向被裁/尺寸为 0/被盖住且滚不到：{' | '.join(bad)}")


@scenario("16_mobile_390_sweep", viewports=("mobile",), tags=("fakes:onebot", "mobile"))
def run(app) -> None:
    """390×844 下逐屏巡场：四个 tab + 设置卡片 + 收件箱两段删除。"""
    app.goto("/")
    _dismiss_wizard(app)
    _inject_group_messages(app)
    _classify(app)

    for tab, name in (
        ("#tab-tasks-button", "待办"),
        ("#tab-notices-button", "通知"),
        ("#tab-inbox-button", "收件箱"),
        ("#tab-settings-button", "设置"),
    ):
        app.click(tab)
        app.wait_for(tab, state="visible", timeout=10)
        app.settle(0.3)
        _assert_layout(app, f"「{name}」tab")
        app.shot(f"tab-{name}")

    # 设置卡片（微信）在窄屏也不能溢出
    app.wait_for("#wechat-settings-card", timeout=15)
    _assert_layout(app, "设置页·微信卡片")

    # 收件箱：三条 → 两段删除 → 两条，每步都验布局
    app.click("#tab-inbox-button")
    app.wait_for("#inbox-items .inbox-item", timeout=15)
    app.assert_count("#inbox-items .inbox-item", 3)
    _assert_layout(app, "收件箱·3 条")
    app.shot("inbox-3-rows")

    delete = '#inbox-items .inbox-item:first-child button[aria-label="删除这条通知"]'
    app.click(delete)
    app.assert_text(delete, "确认删除？（4 秒内再点一次）")
    _assert_layout(app, "收件箱·删除确认态")
    app.shot("inbox-delete-confirm")

    app.click(delete)
    deadline_text = app.text(delete) if app.count(delete) else ""
    assert "确认删除" not in deadline_text, f"第二次点击后仍停在确认态：{deadline_text!r}"
    app.wait_for("#inbox-items .inbox-item", timeout=15)
    app.assert_count("#inbox-items .inbox-item", 2)
    _assert_layout(app, "收件箱·删除后 2 条")
    app.shot("inbox-2-rows")

    app.assert_no_console_errors()
    app.assert_no_failed_requests()
