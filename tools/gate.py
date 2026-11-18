"""分档门禁：fast / ui / full，结果落 .reports/gate/。

为什么分档：discover 2514 项要 11 分钟，其中六成花在 tests/test_ui_browser.py
（70 个用例、占 3%，每条真浏览器 4–70 秒）。改一行 CSS 也跑全量，等于每轮白等 8 分钟；
而一次全量只能告诉你「哪些用例红了」，分档和基线才让「红项属于哪一类」变得可判。

    python tools/gate.py --tier fast                 # 改了哪块就只跑相关模块
    python tools/gate.py --only board --only feed    # 只跑名字里含这些字的用例
    python tools/gate.py --tier ui                   # test_ui_browser 整模块 + 与上次比新增红项
    python tools/gate.py --tier full                 # discover 全量，推送前必须有一次绿的

每个档都写 .reports/gate/last-<tier>.json（含 sha、耗时、逐条结果）。
ui 档额外写 ui-history.jsonl，并与上一次比对：本次新红的才单独列出来。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def record_dir() -> Path:
    """门禁记录放哪。

    NH_GATE_DIR 可以改：这台机器上 .venv 里的 python 只能写会话工作区，
    往仓库里写会 PermissionError（之前那两个「环境假红」的真身就是这个），
    这时用环境变量把记录指到一个可写目录，推送时带上同一个变量即可。
    """
    override = os.environ.get("NH_GATE_DIR")
    return Path(override) if override else ROOT / ".reports" / "gate"


GATE_DIR = record_dir()


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".nh-gate-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _site_packages() -> str:
    for entry in sys.path:
        if entry and entry.replace("\\", "/").endswith("site-packages"):
            return entry
    return ""


def reexec_if_isolated(argv: list[str]) -> int | None:
    """当前解释器写不了仓库时，提示换一个能写的重跑。

    这台机器上 `.venv\\Scripts\\python.exe` 只能写会话工作区，往仓库里写一律
    PermissionError——`logs/host-actions.log`、仓库根的临时目录、`data/rules.json`
    这些都会假红。注意：被限制的进程拉起的子进程一样受限，所以「自己 exec 自己」
    换不出去；正确的是从外面的 shell 直接用 base python 起：

        $env:PYTHONPATH='<repo>\\.venv\\Lib\\site-packages'
        & 'C:\\...\\Python314\\python.exe' -X utf8 tools\\gate.py --tier full

    这里只负责认出来并说清该敲什么，不再 exec（那样只会变成 11 分钟的假红）。
    显式 NH_GATE_NO_REEXEC=1 可以闭嘴。
    """
    if os.environ.get("NH_GATE_NO_REEXEC") or _writable(ROOT):
        return None
    site = _site_packages()
    sys.stderr.write(
        "当前解释器写不了仓库，门禁会出一堆 PermissionError 假红。改用 base python 起：\n"
        f"  $env:PYTHONPATH='{site}'\n"
        f"  & '{Path(sys.base_prefix) / 'python.exe'}' -X utf8 {Path(__file__).resolve()} --tier full\n")
    return None


# 改了哪块代码，至少要跑哪些模块。没命中就退回「除浏览器外的全部」。
MODULE_MAP = (
    ("noticehub/web_static/", ("test_webapp", "test_web_static_assets", "test_ux_fixes",
                               "test_page_beta11", "test_ui_copy_terms")),
    ("noticehub/application/", ("test_application_trips", "test_application_agent_plan",
                               "test_application_agent_chat", "test_application_campus",
                               "test_application_tasks", "test_routes", "test_webapp")),
    ("noticehub/webapp.py", ("test_webapp", "test_routes", "test_routes_uncovered")),
    ("noticehub/config.py", ("test_config", "test_config_audit")),
    ("noticehub/store.py", ("test_store", "test_store_recovery")),
    ("noticehub/rules.py", ("test_rules", "test_lab")),
    ("docs/", ("test_wiki",)),
)


def _tmp_env() -> dict:
    """用例共用一个仓库外的临时根：这台机器的 Python 写不了用户 %TEMP%。"""
    base = Path(os.environ.get("NH_GATE_TMP", r"F:\qq-notice-hub\nh-tmp"))
    tmp = base / ("gate-tmp-" + str(os.getpid()))
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["TEMP"] = str(tmp)
    env["TMP"] = str(tmp)
    env["NH_REQUIRE_BROWSER"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def product_tree() -> str:
    """`noticehub/` 那棵树的 sha：门禁证明的是「这段产品代码是绿的」。

    只改文档、用例、tools 时树不变，刚跑完的全量记录仍然算数——否则每提交一次
    就得再花 11 分钟重跑一遍同一个产品。
    """
    out = subprocess.run(["git", "rev-parse", "HEAD:noticehub"], cwd=str(ROOT),
                         capture_output=True, text=True)
    return (out.stdout or "").strip()


def _changed_paths() -> list[str]:
    """相对 main 的改动 + 工作区未提交的改动，取并集。"""
    out: list[str] = []
    for cmd in (["git", "diff", "--name-only", "main...HEAD"],
                ["git", "status", "--porcelain"]):
        try:
            raw = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (raw.stdout or "").splitlines():
            line = line.strip()
            if cmd[1] == "status":
                line = line[3:] if len(line) > 3 else ""
            if line:
                out.append(line.replace("\\", "/"))
    return sorted(set(out))


def modules_for_fast() -> list[str]:
    """按改动文件挑模块；挑不出来就除浏览器外全跑（单测很快，浏览器才慢）。"""
    picked: list[str] = []
    for path in _changed_paths():
        for prefix, mods in MODULE_MAP:
            if path.startswith(prefix):
                picked.extend(mods)
        if path.startswith("tests/test_") and path.endswith(".py") and (ROOT / path).exists():
            # 只在文件还在的时候挑：main 到 HEAD 之间删掉的用例模块也出现在 diff 里，
            # 挑进来就是 ModuleNotFoundError，fast 档在干净树上也会红。
            picked.append(Path(path).stem)
    if not picked:
        picked = sorted(p.stem for p in (ROOT / "tests").glob("test_*.py")
                        if p.stem != "test_ui_browser")
    return sorted(set(picked))


class Recorder(unittest.TextTestResult):
    """记下每条用例的结果与耗时，写成 json 而不是从日志里猜。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rows: dict[str, dict] = {}
        self._t0 = time.time()

    def startTest(self, test):  # noqa: N802 - unittest 的接口名
        self._t0 = time.time()
        super().startTest(test)

    def stopTest(self, test):  # noqa: N802
        tid = test.id()
        status, message = "pass", ""
        for bucket, name in ((self.failures, "fail"), (self.errors, "error"),
                            (self.skipped, "skip")):
            for item, text in bucket:
                if item.id() == tid:
                    status, message = name, text.strip().splitlines()[-1][:400]
                    break
            if status != "pass":
                break
        self.rows[tid] = {"status": status, "seconds": round(time.time() - self._t0, 2),
                          "message": message}
        super().stopTest(test)


def _cases(suite):
    """把套件摊平成用例：loadTestsFromNames 会套好几层 TestSuite。"""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for inner in _cases(item):
                yield inner
        else:
            yield item


def _previous_ui_results() -> dict:
    history = GATE_DIR / "ui-history.jsonl"
    if not history.exists():
        return {}
    try:
        last = history.read_text(encoding="utf-8").splitlines()[-1]
        return json.loads(last).get("results", {})
    except (ValueError, IndexError):
        return {}


def run(tier: str, only: tuple[str, ...] = ()) -> int:
    loader = unittest.TestLoader()
    if only:
        # 只跑名字里含这些关键字的用例：改界面时圈一小片，几十秒就有结论。
        names = ["tests." + p.stem for p in sorted((ROOT / "tests").glob("test_*.py"))
                 if any(d in p.stem for d in ("ui_browser", "webapp", "web_static"))]
        picked = [case for case in _cases(loader.loadTestsFromNames(names))
                  if any(pattern.lower() in case.id().lower() for pattern in only)]
        suite = unittest.TestSuite(picked)
        tier = "only:" + ",".join(only)
    elif tier == "ui":
        suite = loader.loadTestsFromNames(["tests.test_ui_browser"])
    elif tier == "full":
        suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    else:
        suite = loader.loadTestsFromNames(["tests." + m for m in modules_for_fast()])

    started = time.time()
    result = unittest.TextTestRunner(verbosity=1, resultclass=Recorder,
                                     stream=sys.stderr).run(suite)
    seconds = round(time.time() - started, 1)

    try:
        GATE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print("记录目录写不了：%s（%s）" % (GATE_DIR, exc))
        print("换一个可写目录再跑：$env:NH_GATE_DIR='<可写目录>'；推送时带同一个变量。")
        return 2

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT), capture_output=True,
                         text=True).stdout.strip()
    reds = {k: v for k, v in result.rows.items() if v["status"] in ("fail", "error")}
    payload = {
        "tier": tier, "sha": sha, "product": product_tree(), "seconds": seconds,
        "count": len(result.rows),
        "exit": 0 if not reds else 1,
        "counts": {"fail": sum(1 for v in result.rows.values() if v["status"] == "fail"),
                   "error": sum(1 for v in result.rows.values() if v["status"] == "error"),
                   "skip": sum(1 for v in result.rows.values() if v["status"] == "skip")},
        "reds": reds, "results": result.rows,
        "modules": sorted({k.split(".")[1] for k in result.rows if k.startswith("tests.")}),
    }
    if tier == "ui":
        previous = _previous_ui_results()
        payload["new_reds"] = {k: v for k, v in reds.items()
                               if previous.get(k, {}).get("status") in ("pass", None)}
        with (GATE_DIR / "ui-history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"sha": sha, "seconds": seconds,
                                 "results": result.rows}, ensure_ascii=False) + "\n")

    out = GATE_DIR / ("last-%s.json" % (re.sub(r"[^0-9A-Za-z]+", "-", tier) or "run"))
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print("tier=%s  %d 项  %.1f 秒  fail=%d error=%d skip=%d"
          % (tier, payload["count"], seconds, payload["counts"]["fail"],
             payload["counts"]["error"], payload["counts"]["skip"]))
    if tier == "ui" and payload.get("new_reds"):
        print("  本次新增红项 %d 条：" % len(payload["new_reds"]))
    for tid, row in reds.items():
        print("  RED %s\n      %s" % (tid, row["message"]))
    print("记录：%s" % out)
    return 0 if not reds else 1


def conventions_gate() -> int:
    """测试约定静态闸：规则 A 命中就别跑用例了。

    这三类坑（用例写仓库 data/、skip 撑绿、MODULE_MAP 漏登记）现在都要等一轮
    11 分钟的全量才暴露，先花 40 毫秒扫一遍，命中就带着文件行号停在这儿。
    """
    script = ROOT / "tools" / "test_conventions.py"
    if not script.exists():
        return 0
    return subprocess.run([sys.executable, "-X", "utf8", str(script)],
                          cwd=str(ROOT)).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分档门禁")
    parser.add_argument("--tier", choices=("fast", "ui", "full"), default="fast")
    parser.add_argument("--only", action="append", default=[], metavar="关键字",
                        help="只跑用例名里含这些关键字的（可重复）")
    args = parser.parse_args(argv)
    swapped = reexec_if_isolated(argv if argv is not None else sys.argv[1:])
    if swapped is not None:
        return swapped
    if conventions_gate():
        print("测试约定闸拦下了本次门禁：先按上面 A 行把 data_dir 指到 tmp。")
        return 1
    env = _tmp_env()
    os.environ.update({k: env[k] for k in ("TEMP", "TMP", "NH_REQUIRE_BROWSER",
                                           "PYTHONIOENCODING")})
    sys.path.insert(0, str(ROOT))
    return run(args.tier, tuple(args.only))


if __name__ == "__main__":
    raise SystemExit(main())