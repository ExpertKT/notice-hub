"""W3 平台替身一键自证：`python tools/e2e/fakes/selftest.py [--only NAME]`。

覆盖五个替身：onebot / wechat / dingtalk / push / chaoxing。
每条都**真起服务或真造目录，再用产品自己的代码去读**，最后关停并确认端口释放/目录删除。
退出码：0 = 全过；1 = 有失败；2 = 环境不满足（写不了临时目录）。

产物落点
--------
默认 `%TEMP%\\nh-e2e-<run-id>\\selftest`；也可 `--root <path>` 指定。
**跑完自己删**（失败也删，除非 `--keep`）。沙箱里 python 写不了 %TEMP% 时，显式指一个仓库外目录：
    set NH_E2E_RUN_DIR=%TEMP%\\nh-e2e-manual
    python tools\\e2e\\fakes\\selftest.py
注意：默认落点会经 `_rundir.ensure_outside_repo()` 校验，指到仓库内（如 `F:\\qq-notice-hub\\.tmp-*`）
会直接报错退出；`--root` 不走这项校验，但也别拿它往仓库里写，产物一样要在同一轮删掉。
跑完确认 `git status --porcelain --untracked-files=all` 里没有残留。
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.e2e.fakes import available, start  # noqa: E402
from tools.e2e.fakes._rundir import default_run_dir  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, ok, detail))
    print("%s %s%s" % ("PASS" if ok else "FAIL", label, ("  :: " + detail) if detail else ""))


def port_closed(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def http_json(url: str, payload: dict | None = None, timeout: float = 15) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                return response.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return response.status, {"_raw": raw}
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        return error.code, {"_raw": raw}


def post_auth(url: str, payload: dict, token: str, timeout: float = 15) -> tuple[int, dict]:
    """带 Bearer 的 POST（OneBot 的那些 action 都要令牌，别用无令牌的 http_json 打）。"""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        return error.code, {"_raw": error.read().decode("utf-8", errors="replace")}


# ---------------------------------------------------------------- onebot
def selftest_onebot(root: Path) -> None:
    fake = start("onebot", token="e2e-token", target_url="", target_token="")
    try:
        check("onebot: 起了随机端口", fake.port is not None, "port=%r" % fake.port)
        status, body = post_auth(fake.base_url + "/get_group_list", {}, token="")
        check("onebot: 无令牌被拒", status == 401, "status=%s" % status)
        status, payload = post_auth(fake.base_url + "/get_group_list", {}, token="e2e-token")
        groups = payload.get("data") or []
        check("onebot: get_group_list 形状", status == 200 and payload.get("retcode") == 0 and len(groups) >= 2,
              json.dumps(groups[:1], ensure_ascii=False))
        status, payload = post_auth(fake.base_url + "/get_login_info", {}, token="e2e-token")
        check("onebot: get_login_info 形状", status == 200 and payload.get("data", {}).get("nickname"),
              json.dumps(payload.get("data"), ensure_ascii=False))
        # 真接收端：起 DigestService 的 OneBot 接收器，经 /__inject 打进真实库
        from qq_live_digest.config import Settings
        from qq_live_digest.service import DigestService

        data_dir = root / "onebot_service"
        settings = Settings(
            group_whitelist=("10001",),
            onebot_enabled=True, onebot_host="127.0.0.1", onebot_port=0, onebot_token="receiver-token",
            window_minutes=1, poll_seconds=3600, min_score=99,
            data_dir=data_dir / "data", log_dir=data_dir / "logs",
        )
        service = DigestService(settings)
        service.start()
        try:
            receiver_url = "http://127.0.0.1:%d/onebot/event" % service.receiver.server.server_address[1]
            injector = start("onebot", token="e2e-token", target_url=receiver_url, target_token="receiver-token")
            try:
                status, body = http_json(injector.base_url + "/__inject",
                                         {"text": "请各班今天18:00前提交材料", "group_id": 10001, "sender": "辅导员"})
                forwarded = body.get("forwarded") and body.get("status") == 200
                check("onebot: /__inject -> 真 /onebot/event 200", bool(forwarded),
                      json.dumps(body, ensure_ascii=False)[:220])
                counts = service.store.counts()
                check("onebot: 入库 1 条", counts.get("messages") == 1, "counts=%s" % counts)
                status, body = post_auth(injector.base_url + "/get_group_msg_history",
                                         {"group_id": 10001}, token="e2e-token")
                check("onebot: 注入的消息也能被历史接口取到",
                      len(body.get("data", {}).get("messages") or []) == 1,
                      "status=%s" % status)
            finally:
                injector.stop()
                time.sleep(0.3)
                check("onebot: 注入端端口已释放", port_closed(injector.port), "port=%s" % injector.port)
        finally:
            service.stop()
    finally:
        fake.stop()
        time.sleep(0.3)
        check("onebot: 端口已释放", port_closed(fake.port), "port=%s" % fake.port)


# ---------------------------------------------------------------- wechat
def selftest_wechat(root: Path) -> None:
    import os

    from qq_live_digest import wechat_receiver as wr

    group = "1234@chatroom"
    ok = start("wechat", mode="ok", root=str(root / "wechat_ok"))
    try:
        os.environ["QQ_DIGEST_WECHAT_DIR"] = ok.env["QQ_DIGEST_WECHAT_DIR"]
        os.environ["QQ_DIGEST_WECHAT_KEY"] = ok.env["QQ_DIGEST_WECHAT_KEY"]
        wr.clear_storage_cache()
        storage = wr.locate_storage()
        check("wechat: locate_storage 找到 db_storage", storage is not None and storage.is_dir(), str(storage))
        key = wr.resolve_key(storage, None)
        check("wechat: 手填 raw key 通过页校验", bool(key) and len(key) == 32)
        records = wr.fetch_messages(storage_dir=ok.env["QQ_DIGEST_WECHAT_DIR"], groups=[group])
        check("wechat: 读出 2 条消息", len(records) == 2,
              json.dumps([r.get("content") for r in records], ensure_ascii=False))
        names = wr.group_names(storage_dir=ok.env["QQ_DIGEST_WECHAT_DIR"])
        check("wechat: 群名解析", names.get(group) == "学院通知群", json.dumps(names, ensure_ascii=False))
    finally:
        ok.stop()
    check("wechat: 停后目录已删", not Path(ok.info["root"]).exists())

    locked = start("wechat", mode="locked", root=str(root / "wechat_locked"))
    try:
        target = Path(locked.info["storage"]) / "message" / "message_0.db"
        raised = ""
        try:
            wr.decrypt_database(target, None, bytes.fromhex(locked.env["QQ_DIGEST_WECHAT_KEY"]))
        except BaseException as error:  # noqa: BLE001
            raised = type(error).__name__
        check("wechat: locked 模式读出真实 PermissionError", raised == "PermissionError", raised)
        # 产品当前把「读不了」归成 bad_manual_key（已知缺陷，后端2 在修）；这里只记录现状
        verdict = ""
        try:
            wr.resolve_key(wr.locate_storage(), None)
        except BaseException as error:  # noqa: BLE001
            verdict = "%s: %s" % (type(error).__name__, str(error)[:60])
        check("wechat: locked 现状=报密钥错（已知缺陷，不改产品）", "QQ_DIGEST_WECHAT_KEY" in verdict, verdict)
    finally:
        locked.stop()
    check("wechat: locked 停后目录已删", not Path(locked.info["root"]).exists())

    missing = start("wechat", mode="missing", root=str(root / "wechat_missing"))
    try:
        wr.clear_storage_cache()
        found = wr.locate_storage(missing.env["QQ_DIGEST_WECHAT_DIR"])
        check("wechat: missing 模式找不到库", found is None, str(found))
    finally:
        missing.stop()


# ---------------------------------------------------------------- dingtalk
def selftest_dingtalk(root: Path) -> None:
    import os

    from qq_live_digest.config import Settings
    from qq_live_digest.dingtalk_receiver import DingTalkLocalReader

    ok = start("dingtalk", root=str(root / "dingtalk_ok"))
    try:
        os.environ["APPDATA"] = ok.env["APPDATA"]
        records: list = []
        reader = DingTalkLocalReader(
            Settings(dingtalk_enabled=True, dingtalk_group_whitelist=("cid_e2e_group_1",)),
            on_message=records.append,
        )
        found = reader._locate_db_dir()
        check("dingtalk: 找到本地库目录", found is not None, str(found))
        groups = reader.list_groups()
        check("dingtalk: list_groups 两个群", len(groups) == 2, json.dumps(groups, ensure_ascii=False))
        count = reader.scan_once()
        check("dingtalk: scan_once 读出 3 条", count == 3 and len(records) == 3,
              json.dumps([r.get("content") for r in records], ensure_ascii=False)[:220])
    finally:
        ok.stop()
    check("dingtalk: 停后目录已删", not Path(ok.info["root"]).exists())

    locked = start("dingtalk", mode="locked", root=str(root / "dingtalk_locked"))
    try:
        os.environ["APPDATA"] = locked.env["APPDATA"]
        reader2 = DingTalkLocalReader(Settings(dingtalk_enabled=True, dingtalk_group_whitelist=("cid_e2e_group_1",)),
                                      on_message=lambda rec: True)
        raised = ""
        try:
            reader2.scan_once()
        except BaseException as error:  # noqa: BLE001
            raised = "%s: %s" % (type(error).__name__, str(error)[:60])
        check("dingtalk: locked 模式真报占用", "PermissionError" in raised, raised)
    finally:
        locked.stop()
    check("dingtalk: locked 停后目录已删", not Path(locked.info["root"]).exists())


# ---------------------------------------------------------------- push
def selftest_push(root: Path) -> None:
    sink = start("push", root=str(root / "push_ok"))
    try:
        status, body = http_json(sink.base_url + "/hook", {"content": "【学院通知】请提交材料", "title": "群务台"})
        check("push: 收包回 200", status == 200 and body == {}, "status=%s" % status)
        http_json(sink.base_url + "/other", {"x": 1})
        status, state = http_json(sink.base_url + "/__state")
        check("push: 计数 2", state.get("count") == 2, json.dumps(state, ensure_ascii=False))
        status, payload = http_json(sink.base_url + "/__requests?limit=1")
        last = (payload.get("items") or [{}])[0].get("body") or {}
        check("push: 最近一条 body 原文", last.get("x") == 1, json.dumps(last, ensure_ascii=False))
        check("push: requests.json 已落盘", Path(sink.info["requests_file"]).exists(),
              sink.info["requests_file"])
    finally:
        sink.stop()
    time.sleep(0.3)
    check("push: 端口已释放", port_closed(sink.port), "port=%s" % sink.port)

    failing = start("push", mode="fail", root=str(root / "push_fail"))
    try:
        status, _ = http_json(failing.base_url + "/hook", {"content": "x"})
        check("push: fail 模式回 500", status == 500, "status=%s" % status)
    finally:
        failing.stop()
    check("push: fail 端口已释放", port_closed(failing.port), "port=%s" % failing.port)

    slow = start("push", mode="slow", delay=1.0, root=str(root / "push_slow"))
    try:
        began = time.time()
        status, _ = http_json(slow.base_url + "/hook", {"content": "slow"})
        elapsed = time.time() - began
        check("push: slow 模式真的慢（>=0.9s）", status == 200 and elapsed >= 0.9, "elapsed=%.2fs" % elapsed)
    finally:
        slow.stop()


# ---------------------------------------------------------------- chaoxing
def selftest_chaoxing(root: Path) -> None:
    """真链路：注入 QQ_DIGEST_CHAOXING_BASE 后，产品的 ChaoxingClient 直接指向替身。"""
    import os

    from qq_live_digest.chaoxing_receiver import (
        CHAOXING_BASE_ENV,
        ChaoxingAuthError,
        ChaoxingClient,
    )

    previous = os.environ.get(CHAOXING_BASE_ENV)

    def use(handle) -> None:
        os.environ[CHAOXING_BASE_ENV] = handle.env[CHAOXING_BASE_ENV]

    ok = start("chaoxing")
    try:
        check("chaoxing: handle.env 带上 base 覆盖",
              ok.env.get(CHAOXING_BASE_ENV) == ok.base_url, str(ok.env.get(CHAOXING_BASE_ENV)))
        check("chaoxing: info.routes 八条齐全", len(ok.info.get("routes") or []) == 8,
              ",".join(ok.info.get("routes") or []))
        use(ok)
        client = ChaoxingClient("e2e=cookie", timeout=5.0)
        check("chaoxing: endpoints 全部指向替身",
              all(url.startswith(ok.base_url) for url in client.endpoints.values()),
              json.dumps({k: v.replace(ok.base_url, "") for k, v in client.endpoints.items()}, ensure_ascii=False))
        client.validate_login()
        check("chaoxing: validate_login 真连替身通过", True, "sso result==1")
        courses = client.my_courses()
        check("chaoxing: my_courses 读到 2 门", len(courses) == 2,
              json.dumps([c.get("course_name") for c in courses], ensure_ascii=False))
        notices = client.notices()
        items = notices if isinstance(notices, list) else (notices or {}).get("notices", {}).get("list")
        check("chaoxing: notices 读到 2 条", len(items or []) == 2)
        works = client.parse_work_list(str(client.work_list()))
        check("chaoxing: work_list 读到 2 条", len(works or []) == 2,
              json.dumps([w.get("title") for w in works or []], ensure_ascii=False))
        hits = ok.impl.hit_count() if ok.impl is not None else -1
        check("chaoxing: 替身真的收到请求", hits >= 4, "hits=%s" % hits)
        # 「逐门课抓作业详情」真链路：course_page（带签名）→ work_list → 相对 detail → 解出作业
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from qq_live_digest.config import Settings

        reader = ChaoxingReader(
            Settings(chaoxing_enabled=True, chaoxing_cookie="e2e=cookie"),
            on_message=lambda record: True,
        )
        first = courses[0]
        records = reader._course_work_records(
            client, first, first["course_id"], first["clazz_id"], first["cpi"], 0.0
        )
        titles = [item.get("title") for item in records]
        has_deadline = bool(records) and "截止时间" in str(records[0].get("content") or "")
        check("chaoxing: 逐门课抓作业解出到期提醒", len(records) == 1 and has_deadline,
              json.dumps(titles, ensure_ascii=False) + " content=" + str((records[0] or {}).get("content") if records else "")[:80])
        paths = [row["path"].split("?")[0] for row in (ok.impl.requests if ok.impl is not None else [])]
        check("chaoxing: 课程详情页与作业详情都落在替身 host",
              "/visit/stucoursemiddle" in paths and "/mooc2/work/detail" in paths,
              ",".join(sorted(set(paths))))
    finally:
        ok.stop()
    time.sleep(0.3)
    check("chaoxing: 端口已释放", port_closed(ok.port), "port=%s" % ok.port)

    expired = start("chaoxing", mode="expired")
    try:
        status = 0
        try:
            urllib.request.urlopen(expired.base_url + "/mooc2-ans/visit/courselistdata", timeout=5)
        except urllib.error.HTTPError as error:
            status = error.code
        check("chaoxing: expired 回 401", status == 401, "status=%s" % status)
        use(expired)
        client2 = ChaoxingClient("e2e=cookie", timeout=5.0)
        began = time.time()
        raised = ""
        try:
            client2.validate_login()
        except ChaoxingAuthError:
            raised = "ChaoxingAuthError"
        except BaseException as error:  # noqa: BLE001
            raised = type(error).__name__
        elapsed = time.time() - began
        check("chaoxing: expired 时客户端判 AuthError 且不卡死",
              raised == "ChaoxingAuthError" and elapsed < 5.0, "%s, %.2fs" % (raised, elapsed))
    finally:
        expired.stop()

    timeout = start("chaoxing", mode="timeout", delay=3.0)
    try:
        use(timeout)
        client3 = ChaoxingClient("e2e=cookie", timeout=1.0)
        began = time.time()
        raised = ""
        try:
            client3.validate_login()
        except BaseException as error:  # noqa: BLE001
            raised = type(error).__name__
        elapsed = time.time() - began
        check("chaoxing: timeout 模式报错不卡死", bool(raised) and elapsed < 5.0,
              "%s, %.2fs" % (raised, elapsed))
    finally:
        timeout.stop()
    check("chaoxing: timeout 端口已释放", port_closed(timeout.port), "port=%s" % timeout.port)

    if previous is None:
        os.environ.pop(CHAOXING_BASE_ENV, None)
    else:
        os.environ[CHAOXING_BASE_ENV] = previous


TESTS = {
    "onebot": selftest_onebot,
    "wechat": selftest_wechat,
    "dingtalk": selftest_dingtalk,
    "push": selftest_push,
    "chaoxing": selftest_chaoxing,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="W3 平台替身自证")
    parser.add_argument("--only", action="append", choices=sorted(TESTS), help="只跑指定替身，可重复")
    parser.add_argument("--root", default="", help="自测落点（默认 %%TEMP%%\\nh-e2e-<run-id>\\selftest）")
    parser.add_argument("--keep", action="store_true", help="跑完不删目录（排障用）")
    args = parser.parse_args(argv)

    print("available:", available())
    if args.root:
        root = Path(args.root)
        root.mkdir(parents=True, exist_ok=True)
        own_root = True
    else:
        try:
            root = default_run_dir("selftest")
        except RuntimeError as error:
            print("ENV_FAIL %s" % error, file=sys.stderr)
            return 2
        own_root = True
    print("self-test root:", root)

    names = args.only or list(TESTS)
    try:
        for name in names:
            print("=" * 16, name, "=" * 16)
            try:
                TESTS[name](root)
            except Exception:  # noqa: BLE001 - 自证脚本要把异常变成 FAIL 而不是崩溃
                check("%s: 未预期异常" % name, False, traceback.format_exc(limit=3).replace("\n", " | ")[:400])
    finally:
        if not args.keep and own_root:
            shutil.rmtree(root, ignore_errors=True)
            print("cleanup -> %s removed=%s" % (root, not root.exists()))

    failed = [label for label, ok, _ in RESULTS if not ok]
    print("-" * 40)
    print("RESULT: %d passed, %d failed" % (len(RESULTS) - len(failed), len(failed)))
    if failed:
        for label in failed:
            print("  FAILED:", label)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
