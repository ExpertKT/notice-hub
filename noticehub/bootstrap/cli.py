"""命令行动作。命令名和参数仍由 main.py 解析。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

from qq_digest import Message
from qq_live_digest.attachments import IMAGE_EXTS, Attachment
from qq_live_digest.config import Settings, ensure_dirs
from qq_live_digest.logging_setup import setup_logging
from qq_live_digest.service import DigestService
from qq_live_digest.summarizer import Digest, finalize_digest, preview_text
from qq_live_digest.timeutil import iso, now_local
from qq_live_digest.weburl import build_web_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV = PROJECT_ROOT / ".env"


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def load_settings(args: argparse.Namespace) -> Settings:
    env_file = Path(args.env).expanduser() if getattr(args, "env", "") else DEFAULT_ENV
    return Settings.from_env(env_file=env_file)


def build_service(settings: Settings, *, console: bool = True) -> DigestService:
    ensure_dirs(settings)
    setup_logging(settings.log_dir, level=logging.INFO, console=console)
    return DigestService(settings)


def command_run(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    ensure_dirs(settings)
    from main import acquire_lock

    lock = acquire_lock(settings.data_dir / "service.lock")
    if lock is None:
        print("已有实例在运行（service.lock 被占用），本次退出。")
        return 3
    try:
        service = build_service(settings)
        for problem in settings.problems():
            logging.getLogger("main").warning("配置提示：%s", problem)
        logging.getLogger("main").info("配置：%s", json.dumps(settings.describe(), ensure_ascii=False))
        service.run_forever(poll_seconds=args.poll)
    finally:
        lock.close()
    return 0


def command_tick(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings)
    start_bot = not args.no_bot
    if start_bot:
        service.start()
    produced = service.tick()
    if start_bot:
        service.stop()
    for digest in produced:
        logging.getLogger("main").info(
            "本轮产出 digest#%s kind=%s items=%d messages=%d",
            digest.id,
            digest.kind,
            len(digest.items),
            digest.message_count,
        )
    return 0


def command_preview(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings, console=False)
    limit = args.limit or settings.max_batch
    records = service.store.unprocessed_messages(limit=limit)
    print(preview_text(settings, records))
    return 0


def command_stats(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings, console=False)
    print(json.dumps(service.status(), ensure_ascii=False, indent=2))
    return 0


def command_show(args: argparse.Namespace) -> int:
    """回看最近几条摘要，以及每条判断所依据的原文句子。"""
    settings = load_settings(args)
    service = build_service(settings, console=False)
    digests = service.store.recent_digests(limit=args.limit or 5)
    if not digests:
        print("还没有已归档的摘要。")
        return 0
    for digest in digests:
        items = digest.get("items") or []
        print(
            f"#{digest['id']} [{digest['kind']}] {digest['window_start']} ~ {digest['window_end']}"
            f" · {digest['item_count']} 条重点 / {digest['message_count']} 条消息"
        )
        for index, item in enumerate(items, start=1):
            summary = str(item.get("summary") or item.get("text") or "")[:60]
            head = f"  {index}. [{item.get('category') or 'info'}] {summary}"
            if item.get("deadline"):
                head += f"（截止 {item['deadline']}）"
            print(head)
            action = str(item.get("action") or "")
            if action and action != summary:
                print(f"     需做：{action}")
            if item.get("group"):
                print(f"     来源：{item['group']}")
            if item.get("evidence"):
                print(f"     依据：{item['evidence']}")
        print()
    return 0


def command_tasks(args: argparse.Namespace) -> int:
    """查看待办清单，并打印手机访问地址。"""
    settings = load_settings(args)
    service = build_service(settings, console=False)
    if args.reset:
        removed = service.store.clear_tasks()
        print(f"已清空 {removed} 条待办。")
    if args.import_existing:
        imported = service.import_existing_tasks(days=args.days)
        print(f"已回填 {imported} 条待办（来自最近 {args.days} 天摘要）。")
    limit = args.limit or 30
    statuses = ("candidate", "open", "done", "dismissed", "expired") if args.all else ("candidate", "open")
    tasks = service.store.list_tasks(statuses=statuses, limit=max(limit, 200))
    stats = service.store.task_stats()
    print(
        f"待办统计：待确认 {stats['candidate']} 条，未完成 {stats['open']} 条，"
        f"已完成 {stats['done']} 条，已忽略 {stats['dismissed']} 条。"
    )
    for task in tasks[:limit]:
        mark = {"done": "x", "candidate": "?", "dismissed": "-", "expired": "!"}.get(
            str(task.get("status") or ""), " "
        )
        deadline = str(task.get("deadline") or "")[:16].replace("T", " ")
        suffix = f"  截止 {deadline}" if deadline else ""
        print(f"  [{mark}] #{task.get('id')} {task.get('summary') or ''}{suffix}")
    token = settings.web_token or service.store.meta_get("web_token", "")
    url = build_web_url(settings, token=token)
    print()
    print(f"待办台：{url}")
    return 0


def command_catchup(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings, console=True)
    inserted = service.run_catchup(force=True)
    print(f"历史补采完成，新增 {inserted} 条消息。")
    return 0


def _check_token(settings: Settings, timeout: int = 15) -> tuple[bool, str]:
    payload = json.dumps({"appId": settings.appid, "clientSecret": settings.secret}).encode("utf-8")
    request = urllib.request.Request(
        "https://bots.qq.com/app/getAppAccessToken",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}: {error.read().decode('utf-8', errors='replace')[:200]}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        return False, f"请求失败: {error}"
    if data.get("access_token"):
        return True, f"获取 access_token 成功（expires_in={data.get('expires_in')}）"
    return False, f"返回异常: {str(data)[:200]}"


def command_doctor(args: argparse.Namespace) -> int:
    configure_console()
    settings = load_settings(args)
    if getattr(args, "bundle", None) is not None:
        from qq_live_digest.doctor import DoctorError, build_bundle

        try:
            path = build_bundle(settings, args.bundle or None)
        except DoctorError as error:
            print(f"诊断包生成失败：{error}")
            return 1
        print(f"诊断包已生成：{path}")
        print("把它发给我们即可；包内已脱敏，不含密钥、Cookie 或消息内容。")
        return 0
    service = build_service(settings)
    ok = True

    print("== 配置 ==")
    print(json.dumps(settings.describe(), ensure_ascii=False, indent=2))
    for problem in settings.problems():
        print(f"  ! {problem}")
    print(f"  env_file: {settings.env_file}")

    print("== 数据库 ==")
    counts = service.store.counts()
    print(f"  {settings.data_dir / 'digest.sqlite3'}")
    print(f"  {json.dumps(counts, ensure_ascii=False)}")

    print("== 推送通道 ==")
    manager = service.push_manager()
    if manager.pushers:
        for pusher in manager.pushers:
            print(f"  - {pusher.describe()} tier={pusher.tier}")
    else:
        ok = False
        print("  ! 未配置任何可用通道")

    print("== QQ 机器人 ==")
    if args.online:
        if not settings.appid or not settings.secret:
            ok = False
            print("  ! 缺少 AppID/AppSecret，跳过在线校验")
        else:
            passed, message = _check_token(settings, settings.http_timeout)
            ok = ok and passed
            print(("  OK " if passed else "  ! ") + message)
    else:
        print("  （默认离线自检；加 --online 会校验 AppID/AppSecret）")

    return 0 if ok else 1


def command_send_test(args: argparse.Namespace) -> int:
    settings = load_settings(args)
    service = build_service(settings)
    started = False
    if settings.official_bot_enabled and settings.appid and settings.secret:
        started = bool(service.bot.start())
        if started:
            ready = service.bot.wait_ready(timeout=args.wait)
            print(f"机器人就绪：{ready}")
    manager = service.push_manager()
    if not manager.pushers:
        print("没有可用推送通道，请先配置 .env")
        if started:
            service.stop()
        return 2
    title = "QQ群通知助手测试"
    stamp = now_local()
    deadline = stamp.replace(hour=18, minute=0, second=0, microsecond=0)

    def card_item(text: str, category: str, summary: str, action: str, due, group: str) -> dict:
        return {
            "message": Message(timestamp=stamp, sender="辅导员", text=text, source=group),
            "category": category,
            "importance": 5 if category == "urgent" else 4,
            "score": 5,
            "summary": summary,
            "action": action,
            "deadline_dt": due,
            "group_name": group,
            "evidence": text,
            "msg_id": "",
        }

    digest = Digest(
        kind="window",
        window_start=stamp,
        window_end=stamp,
        items=[
            card_item(
                "请各班班长今天18:00前提交实验报告",
                "action",
                "提交实验报告",
                "今晚18:00前交给班长",
                deadline,
                "测仪2602班群",
            ),
            card_item(
                "今晚务必完成安全知识答题",
                "urgent",
                "安全知识答题今晚截止",
                "立即完成答题",
                deadline,
                "电气学院2026级群",
            ),
            card_item(
                "【通知】图书馆周末闭馆",
                "info",
                "图书馆周末闭馆",
                "",
                None,
                "电气学院2026级群",
            ),
            card_item(
                "下周一开始选课",
                "academic",
                "下周一开始选课",
                "",
                None,
                "测仪2602班群",
            ),
        ],
        message_count=4,
        groups=["电气学院2026级群", "测仪2602班群"],
    )
    digest = finalize_digest(digest, settings)
    outcomes = manager.send_card(
        title,
        digest.body,
        dedupe_key=f"test:{iso(stamp)}",
        summary=digest.summary,
        html=digest.html,
    )
    failed = 0
    for item in outcomes:
        status = "OK" if item.ok else "失败"
        print(f"  {status} {item.channel}:{item.target} {item.error}")
        failed += 0 if item.ok else 1
    if started:
        service.stop()
    return 0 if failed == 0 else 1


def command_attach_test(args: argparse.Namespace) -> int:
    """解析群文件 / 本地图片并打印摘要，不写库也不推送。"""
    settings = load_settings(args)
    service = build_service(settings, console=True)
    worker = service.attachment_worker
    source = str(getattr(args, "path", "") or "").strip()
    record: dict | None = None
    if source:
        local = Path(source).expanduser()
        if not local.is_file():
            print(f"本地文件不存在：{local}")
            return 2
        attachment = Attachment(
            kind="image" if local.suffix.lower() in IMAGE_EXTS else "file",
            name=local.name,
            group_id=str(args.group or "local"),
            group_name=settings.group_name(str(args.group)) if args.group else "本地测试",
            sender_name="本地文件",
            size=local.stat().st_size,
        )
        record = worker.process_local(attachment, local)
    elif args.group and args.file_id:
        attachment = Attachment(
            kind="file",
            name=str(args.name or "群文件"),
            group_id=str(args.group),
            group_name=settings.group_name(str(args.group)),
            sender_name="群文件",
            file_id=str(args.file_id),
            busid=str(args.busid or ""),
        )
        record = worker.process(attachment)
    else:
        print(
            "用法：attach-test <本地文件路径>"
            " 或 attach-test --group <群号> --file-id <id> [--busid 102] [--name 文件名]"
        )
        return 2
    if not record:
        print("没有生成摘要（内容为空或被限流，详见日志）。")
        return 1
    print("==== 附件摘要 ====")
    print(f"来源：{record['group_name']} · {record['sender_name']}")
    print(record["content"])
    return 0
