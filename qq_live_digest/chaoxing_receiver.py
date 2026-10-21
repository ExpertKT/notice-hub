"""Read-only Chaoxing client and response probe (experimental)."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import re
import time
import threading
from typing import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, unquote, urlparse
from urllib.request import Request, urlopen

POLL_INTERVAL_SECONDS = 1800
PROBE_DELAY_SECONDS = 1.0
YEAR_UNKNOWN_ROLLFORWARD_DAYS = 180
USER_AGENT = "notice-hub-chaoxing/0.1 (read-only)"
# Matches "07-01 18:51", "2026-07-01 18:51", "2026年7月1日 18:51" and "2026/7/1 18:51".
DEADLINE_PATTERN = re.compile(
    r"(?<!\d)(?:(\d{4})\s*[年./-]\s*)?(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?\s+(\d{1,2}):(\d{2})(?!\d)"
)
ENDPOINTS = {
    "courses": "https://i.chaoxing.com/base",
    "course_list": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
    "notices": "https://notice.chaoxing.com/pc/notice/getNoticeList",
    "work_list": "https://mooc1.chaoxing.com/mooc2/work/list",
    "lessons": "https://kb.chaoxing.com/pc/curriculum/getMyLessons",
}


class ChaoxingError(RuntimeError):
    """Base error for expected client failures."""


class ChaoxingHTTPError(ChaoxingError):
    """Non-success HTTP response."""


class ChaoxingAuthError(ChaoxingError):
    """Cookie is absent or the server returned a login page."""


class ChaoxingClient:
    def __init__(self, cookie: str, *, timeout: float = 15.0, logger=None) -> None:
        self.cookie = str(cookie or "").strip()
        self.timeout = float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        self.logger = logger or logging.getLogger(__name__)

    def _headers(self) -> dict[str, str]:
        return {"Cookie": self.cookie, "User-Agent": USER_AGENT, "Accept": "*/*"}

    def _get(self, url: str, params: dict[str, Any] | None = None) -> tuple[int, bytes, str]:
        if params:
            url = url + ("&" if "?" in url else "?") + urlencode(params)
        request = Request(url, headers=self._headers(), method="GET")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                geturl = getattr(response, "geturl", None)
                final_url = str(geturl() if callable(geturl) else url)
                body = response.read()
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise ChaoxingAuthError("Chaoxing login session is invalid or expired") from exc
            raise ChaoxingHTTPError(f"Chaoxing HTTP {exc.code} for {url}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ChaoxingError(f"Chaoxing request failed for {url}: {exc}") from exc
        text = body.decode("utf-8", errors="replace")
        low = text.lower()
        final = urlparse(final_url)
        login_redirect = final.hostname in {"passport2.chaoxing.com", "passport.chaoxing.com"} or "login" in final.path.lower()
        login_body = any(marker in low for marker in ("fanyalogin", "请输入验证码", "请登录", "统一身份认证", "账号密码登录"))
        if status in (401, 403) or login_redirect or login_body:
            raise ChaoxingAuthError("Chaoxing login session is invalid or expired")
        if status < 200 or status >= 300:
            raise ChaoxingHTTPError(f"Chaoxing HTTP {status} for {url}")
        return status, body, text

    @staticmethod
    def _json_or_text(text: str) -> Any:
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text

    @staticmethod
    def _courses_from_html(text: str) -> list[dict[str, str]]:
        links = re.findall(r"/visit/stucoursemiddle\?courseid=(\d+)&clazzid=(\d+)&cpi=(\d+)", text)
        names = re.findall(r'class="course-name[^\"]*"[^>]*?title="([^\"]*)"', text)
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, (course_id, clazz_id, cpi) in enumerate(links):
            if course_id in seen:
                continue
            seen.add(course_id)
            name = html.unescape(names[len(result)]).strip() if len(result) < len(names) else course_id
            result.append({"course_id": course_id, "course_name": name, "clazz_id": clazz_id, "cpi": cpi})
        return result

    def validate_login(self) -> None:
        """Probe the lightweight home page; _get raises when it redirects to login."""
        if not self.cookie:
            raise ChaoxingAuthError("Chaoxing cookie is not configured")
        self._get(ENDPOINTS["courses"])

    def my_courses(self) -> list[dict[str, str]]:
        fid_match = re.search(r"(?:^|;\s*)fid=([^;]+)", self.cookie, re.I)
        fid = fid_match.group(1).strip() if fid_match else ""
        if fid:
            text = self._get(ENDPOINTS["course_list"], {"courseType": "1", "courseFid": fid})[2]
        else:
            base = self._get(ENDPOINTS["courses"])[2]
            fid_match = re.search(r"(?:fid[=\"']|courseFid[=\"'])(\d+)", base, re.I)
            if not fid_match:
                raise ChaoxingError("Chaoxing course family id (fid) is unavailable")
            text = self._get(ENDPOINTS["course_list"], {"courseType": "1", "courseFid": fid_match.group(1)})[2]
        result = self._courses_from_html(text)
        if not result and "暂无课程" not in text:
            raise ChaoxingError("Chaoxing course response shape is not recognized")
        return result

    def notices(self, course_id: str | None = None) -> Any:
        params = {"courseId": course_id} if course_id else None
        payload = self._json_or_text(self._get(ENDPOINTS["notices"], params)[2])
        if isinstance(payload, dict) and isinstance(payload.get("notices"), dict):
            return payload["notices"].get("list", [])
        return payload

    @staticmethod
    def extract_work_signatures(text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for key in ("workEnc", "openc", "enc"):
            match = re.search(rf'(?:id|name)=["\']{key}["\'][^>]*value=["\']([^"\']+)', text, re.I)
            if not match:
                match = re.search(rf'value=["\']([^"\']+)["\'][^>]*(?:id|name)=["\']{key}["\']', text, re.I)
            if match:
                values[key] = html.unescape(match.group(1))
        return values

    @staticmethod
    def parse_work_list(text: str) -> list[dict[str, str]]:
        if "没有此页面访问权限" in text or "无权限" in text:
            raise ChaoxingError("Chaoxing work list access denied; course signatures are missing")
        rows = []
        pattern = r'<li[^>]*onclick=["\']goTask\(this\);["\'][^>]*data=["\']([^"\']+)["\'][\s\S]*?<p class=["\']overHidden2 fl["\'][^>]*>([^<]+)</p>[\s\S]*?<p class=["\']status fl["\'][^>]*>([^<]*)</p>'
        for url, title, status in re.findall(pattern, text, re.I):
            rows.append({"url": html.unescape(unquote(url)), "title": html.unescape(title).strip(), "status": html.unescape(status).strip()})
        return rows

    @staticmethod
    def parse_work_window(text: str) -> tuple[str, str] | None:
        plain = re.sub(r"<[^>]+>", " ", html.unescape(text))
        match = re.search(r"作答时间[:：]\s*(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})\s*至\s*(\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})", plain)
        return match.groups() if match else None

    def work_list(self, course_id: str | None = None, *, clazz_id: str | None = None, cpi: str | None = None, signatures: dict[str, str] | None = None) -> Any:
        if not (course_id and clazz_id and cpi and signatures):
            return self._json_or_text(self._get(ENDPOINTS["work_list"], {"courseId": course_id} if course_id else None)[2])
        params = {"courseId": course_id, "classId": clazz_id, "cpi": cpi, "ut": "s", "openc": signatures["openc"], "enc": signatures["workEnc"], "t": str(int(time.time() * 1000)), "stuenc": signatures["enc"], "isdisplaytable": "2"}
        return self.parse_work_list(self._get(ENDPOINTS["work_list"], params)[2])

    def work_detail(self, url: str) -> str:
        if not url.startswith(("https://", "http://")):
            raise ValueError("work detail URL must be absolute HTTP(S)")
        return self._get(url)[2]

    def deadline_overview(self) -> list[dict[str, str]]:
        """Conservative overview; fields depend on the live site response shape."""
        works = self.work_list()
        items = works if isinstance(works, list) else works.get("data", []) if isinstance(works, dict) else []
        result: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("workUrl") or item.get("jobUrl") or "")
            if not url:
                continue
            detail = self.work_detail(urljoin(ENDPOINTS["work_list"], url))
            result.append({"title": str(item.get("title") or item.get("name") or ""), "deadline": parse_deadline(detail) or "", "url": url})
        return result


def parse_deadline(fragment: str, *, year: int | None = None, now: dt.datetime | None = None) -> str | None:
    """Parse a deadline from caller-provided HTML/text.

    When the text has no year, this year is assumed unless that date is already more
    than YEAR_UNKNOWN_ROLLFORWARD_DAYS in the past (then next year is used, so a
    "12-30" deadline read in January is not dated a year late).
    """
    text = html.unescape(fragment)
    marked = re.search(r"(?:结束时间|截止时间|截至|deadline)\s*[:：]?\s*" + DEADLINE_PATTERN.pattern, text, re.I)
    match = DEADLINE_PATTERN.search(marked.group(0)) if marked else DEADLINE_PATTERN.search(text)
    if not match:
        return None
    text_year, month, day, hour, minute = match.groups()
    reference = now or dt.datetime.now()
    resolved = int(text_year) if text_year else int(year or reference.year)
    if not text_year and not year:
        candidate = dt.datetime(resolved, int(month), int(day), int(hour), int(minute))
        if (reference - candidate) > dt.timedelta(days=YEAR_UNKNOWN_ROLLFORWARD_DAYS):
            resolved += 1
    return f"{resolved:04d}-{int(month):02d}-{int(day):02d} {int(hour):02d}:{int(minute):02d}"


def to_record(course_id: str, course_name: str, notice_id: str, title: str, content: str, *, sender_id: str = "", sender_name: str = "超星", ts: str | None = None, received_at: str | None = None, event: str = "NOTICE_CREATE") -> dict[str, str]:
    now = received_at or dt.datetime.now().astimezone().isoformat()
    msg_id = f"chaoxing:{notice_id}" if notice_id.startswith("work:") else f"chaoxing:{course_id}:{notice_id}"
    return {"msg_id": msg_id, "source": "chaoxing", "event": event, "group_id": str(course_id), "group_name": str(course_name), "sender_id": str(sender_id), "sender_name": str(sender_name), "ts": ts or now, "received_at": now, "content": "\n".join(part for part in (title.strip(), content.strip()) if part)}


def chaoxing_status(settings, store=None) -> dict[str, Any]:
    """Return credential-free sync status shared with the local web UI."""
    lock = getattr(settings, "chaoxing_status_lock", None)
    if lock:
        with lock:
            state = dict(getattr(settings, "chaoxing_status", {}) or {})
    else:
        state = dict(getattr(settings, "chaoxing_status", {}) or {})
    if not state and store is not None and callable(getattr(store, "meta_get", None)):
        try:
            state = json.loads(store.meta_get("chaoxing_status", "{}"))
        except (ValueError, TypeError):
            state = {}
    expired = bool(state.get("expired"))
    return {
        "ok": bool(state.get("ok", False)),
        "expired": expired,
        "last_sync_at": str(state.get("last_sync_at") or ""),
        "last_error": str(state.get("last_error") or ""),
        "has_cookie": bool(settings.chaoxing_cookie),
        "banner_text": "学习通登录已过期。重新登录学习通后，右键托盘图标选「读取学习通 Cookie」，或在设置里点「立即校验」。" if expired else "",
        "login_url": "https://passport2.chaoxing.com/login" if expired else "",
    }


class ChaoxingReader:
    """Low-frequency, read-only notice poller; payload parsing stays conservative."""
    def __init__(self, settings, on_message: Callable[[dict[str, Any]], bool], logger=None, store=None) -> None:
        self.settings, self.on_message, self.logger, self.store = settings, on_message, logger or logging.getLogger(__name__), store
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        if not hasattr(settings, "chaoxing_status_lock"):
            settings.chaoxing_status_lock = threading.Lock()
        if not hasattr(settings, "chaoxing_status"):
            saved = {}
            if store is not None and callable(getattr(store, "meta_get", None)):
                try: saved = json.loads(store.meta_get("chaoxing_status", "{}"))
                except (ValueError, TypeError): pass
            settings.chaoxing_status = {"ok": False, "expired": False, "last_sync_at": "", "last_error": "", **(saved if isinstance(saved, dict) else {})}
        self._last_logged_error = None

    def _set_status(self, *, ok: bool, expired: bool = False, error: str = "", synced_at: str | None = None) -> None:
        lock = self.settings.chaoxing_status_lock
        with lock:
            status = dict(self.settings.chaoxing_status)
            status.update({"ok": ok, "expired": expired, "last_error": error})
            if synced_at is not None:
                status["last_sync_at"] = synced_at
            self.settings.chaoxing_status = status
        if self.store is not None and callable(getattr(self.store, "meta_set", None)):
            try: self.store.meta_set("chaoxing_status", json.dumps(status, ensure_ascii=False))
            except Exception: self.logger.debug("学习通状态持久化失败", exc_info=True)

    def _record_failure(self, exc: Exception) -> None:
        expired = isinstance(exc, ChaoxingAuthError)
        message = "Cookie 已过期（需重新登录）" if expired else str(exc)
        cookie = str(self.settings.chaoxing_cookie or "")
        if cookie:
            message = message.replace(cookie, "<redacted>")
        self._set_status(ok=False, expired=expired, error=message)
        key = (type(exc).__name__, message)
        if key != self._last_logged_error:
            if expired:
                self.logger.warning("学习通 Cookie 已过期，需重新登录并更新 Cookie")
            else:
                self.logger.warning("学习通读取失败：%s", message)
            self._last_logged_error = key

    def start(self) -> bool:
        if self.thread and self.thread.is_alive(): return True
        self.stop_event.clear(); self.thread = threading.Thread(target=self._run, name="chaoxing-reader", daemon=True); self.thread.start(); return True

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive(): self.thread.join(timeout=max(0.0, timeout))

    def _work_records(self, client: ChaoxingClient, courses: list[dict[str, str]], baseline: bool) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for course in courses:
            cid, clazz, cpi = (str(course.get(k) or "") for k in ("course_id", "clazz_id", "cpi"))
            if not cid or (self.settings.chaoxing_group_whitelist and cid not in self.settings.chaoxing_group_whitelist) or not clazz or not cpi:
                continue
            try:
                page = client._get(f"https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid={cid}&clazzid={clazz}&cpi={cpi}&ismooc2=1&v=2")[2]
                sig = client.extract_work_signatures(page)
                if set(sig) != {"workEnc", "openc", "enc"}:
                    raise ChaoxingError("Chaoxing work signatures are missing")
                for item in client.work_list(cid, clazz_id=clazz, cpi=cpi, signatures=sig):
                    status = item["status"]
                    if not any(word in status for word in ("未提交", "待提交", "未交", "进行中")):
                        continue
                    time.sleep(1.0)
                    window = client.parse_work_window(client.work_detail(item["url"]))
                    if not window: continue
                    deadline = parse_deadline(window[1], now=dt.datetime.now())
                    if not deadline or dt.datetime.strptime(deadline, "%Y-%m-%d %H:%M") <= dt.datetime.now(): continue
                    work_id = (re.search(r"[?&]workId=([^&]+)", item["url"]) or ["", item["url"]])[1]
                    records.append(to_record(cid, course.get("course_name") or cid, f"work:{cid}:{work_id}", item["title"], f"未提交作业\n截止时间：{deadline}", event="WORK_DUE"))
                    if len(records) >= 10: return records
            except ChaoxingAuthError:
                raise
            except Exception as exc:
                self.logger.warning("学习通作业读取失败，跳过课程 %s：%s", cid, exc)
        return records

    def _run(self) -> None:
        state = self.store.chaoxing_sync_state() if self.store else {"notice_ids": []}
        baseline = not bool(state.get("notice_ids"))
        while not self.stop_event.is_set():
            try:
                client = ChaoxingClient(self.settings.chaoxing_cookie, timeout=self.settings.http_timeout, logger=self.logger)
                notices = client.notices()
                items = notices if isinstance(notices, list) else (notices.get("data") or []) if isinstance(notices, dict) else []
                try:
                    course_rows = client.my_courses()
                except ChaoxingAuthError:
                    raise
                except Exception as exc:
                    self.logger.warning("学习通课程列表读取失败，通知按正文尝试匹配：%s", exc)
                    course_rows = []
                course_rows = [row for row in course_rows if isinstance(row, dict)]
                course_names = {str(row.get("course_id")): str(row.get("course_name") or row.get("course_id")) for row in course_rows if row.get("course_id") is not None}
                name_to_id = {name: course_id for course_id, name in course_names.items()}
                seen = set(str(x) for x in state.get("notice_ids", []))
                baseline_pushed = 0
                for item in items:
                    if not isinstance(item, dict): continue
                    notice_id = str(item.get("uuid") or item.get("idCode") or item.get("id") or "")
                    content = str(item.get("content") or "")
                    attachments = item.get("attachment")
                    if isinstance(attachments, str):
                        try: attachments = json.loads(attachments)
                        except (TypeError, ValueError): attachments = []
                    if isinstance(attachments, dict): attachments = [attachments]
                    course_id = ""
                    detail_url = ""
                    for attachment in attachments if isinstance(attachments, list) else []:
                        if not isinstance(attachment, dict): continue
                        raw = attachment.get("att_web", attachment)
                        if isinstance(raw, str):
                            try: raw = json.loads(raw)
                            except (TypeError, ValueError): raw = {}
                        if isinstance(raw, dict):
                            course_id = str(raw.get("courseId") or raw.get("courseid") or course_id)
                            detail_url = str(raw.get("url") or detail_url)
                    match = re.search(r"课程名称：([^：\n]+)", content)
                    course_name = course_names.get(course_id, "")
                    if not course_id and match:
                        course_name = match.group(1).strip()
                        course_id = name_to_id.get(course_name, "")
                    if not course_name: course_name = match.group(1).strip() if match else "未知课程"
                    if not notice_id or notice_id in seen: continue
                    if self.settings.chaoxing_group_whitelist and course_id not in self.settings.chaoxing_group_whitelist: continue
                    deadline = parse_deadline(content)
                    body = content + (("\n截止时间：" + deadline) if deadline and deadline not in content else "")
                    should_push = not baseline
                    if baseline and deadline:
                        try:
                            should_push = dt.datetime.strptime(deadline, "%Y-%m-%d %H:%M") > dt.datetime.now()
                        except ValueError:
                            should_push = False
                    if should_push and (not baseline or baseline_pushed < 10):
                        record = to_record(course_id or "unknown", course_name, notice_id, str(item.get("title") or ""), body, ts=str(item.get("sendTime") or item.get("insertTime") or ""))
                        if detail_url: record["content"] += "\n详情：" + detail_url
                        self.on_message(record)
                        if baseline: baseline_pushed += 1
                    seen.add(notice_id)
                for record in self._work_records(client, course_rows, baseline):
                    self.on_message(record)
                if baseline:
                    self.logger.info("学习通通知已建立基线：%d 条", len(seen))
                    baseline = False
                state["notice_ids"] = list(seen)[-1000:]
                if self.store: self.store.save_chaoxing_sync_state(state)
                self._set_status(ok=True, expired=False, error="", synced_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
                self._last_logged_error = None
            except Exception as exc:
                self._record_failure(exc)
            self.stop_event.wait(max(1800, int(self.settings.chaoxing_scan_interval_seconds)))


def _probe(cookie: str, out_dir: Path, logger: logging.Logger) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    client = ChaoxingClient(cookie, logger=logger)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for index, (name, url) in enumerate(ENDPOINTS.items()):
        if index:
            time.sleep(PROBE_DELAY_SECONDS)
        try:
            status, body, text = client._get(url)
            path = out_dir / f"{stamp}-{name}.bin"
            path.write_bytes(body)
            safe = re.sub(r"cookie\s*[:=].{0,120}", "cookie: <redacted>", text[:200], flags=re.I)
            print(f"{name}: HTTP {status}, {len(body)} bytes, {safe!r}")
        except ChaoxingError as exc:
            print(f"{name}: ERROR {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe read-only Chaoxing endpoints and save raw responses")
    parser.add_argument("--cookie-file", required=True, help="UTF-8 file containing a manually copied Cookie header")
    parser.add_argument("--out", required=True, type=Path, help="directory for raw response samples")
    args = parser.parse_args()
    cookie = Path(args.cookie_file).read_text(encoding="utf-8").strip()
    _probe(cookie, args.out, logging.getLogger(__name__))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
