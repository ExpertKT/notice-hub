"""结果汇总 + 报告：终端表格、report.json、report.md，失败截图按场景落盘。"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from .paths import temp_root


def report_root() -> Path:
    # 绝不用 tempfile.gettempdir()：本机它会解析到仓库根，报告会污染工作区。
    return temp_root() / "nh-e2e-report"


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def safe_name(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", name).strip("_") or "scenario"


@dataclass
class CaseResult:
    scenario: str
    viewport: str
    ok: bool
    seconds: float
    error: str = ""
    screenshots: list[str] = field(default_factory=list)
    out_dir: str = ""
    # 本用例收尾断言到的失败请求条数（0 = 干净；断言真跑过，不能看不出来）。
    failed_requests: int = 0

    @property
    def title(self) -> str:
        return f"{self.scenario}[{self.viewport}]"


@dataclass
class RunReport:
    run_id: str
    results: list[CaseResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    zip_path: str = ""
    keep: bool = False
    reuse: bool = False
    mode: str = "zip"  # zip = 解包安装包；source = 最小源码树 + 系统 python
    root: Path | None = None

    @property
    def dir(self) -> Path:
        return self.root if self.root is not None else report_root() / self.run_id

    @property
    def failed(self) -> list[CaseResult]:
        return [item for item in self.results if not item.ok]

    def case_dir(self, scenario: str, viewport: str) -> Path:
        return self.dir / f"{safe_name(scenario)}--{viewport}"

    def add(self, result: CaseResult) -> None:
        self.results.append(result)

    # ---------------------------------------------------------------- 输出
    def write(self) -> tuple[Path, Path]:
        self.finished_at = self.finished_at or time.time()
        self.dir.mkdir(parents=True, exist_ok=True)
        json_path = self.dir / "report.json"
        md_path = self.dir / "report.md"
        json_path.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "started_at": _stamp(self.started_at),
                    "finished_at": _stamp(self.finished_at),
                    "duration_seconds": round(self.finished_at - self.started_at, 1),
                    "zip": self.zip_path,
                    "mode": self.mode,
                    "keep": self.keep,
                    "reuse": self.reuse,
                    "total": len(self.results),
                    "passed": len(self.results) - len(self.failed),
                    "failed": len(self.failed),
                    "results": [
                        {
                            "scenario": item.scenario,
                            "viewport": item.viewport,
                            "ok": item.ok,
                            "seconds": round(item.seconds, 2),
                            "error": item.error,
                            "screenshots": item.screenshots,
                            "dir": item.out_dir,
                            "failed_requests": item.failed_requests,
                            "repro": f"python tools/e2e/run.py --only {item.scenario}",
                        }
                        for item in self.results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, md_path

    def to_markdown(self) -> str:
        passed = len(self.results) - len(self.failed)
        lines = [
            f"# E2E 报告 {self.run_id}",
            "",
            f"- 模式：**{self.mode}**（zip = 安装包解包；source = 源码树）",
            f"- 包：`{self.zip_path or '(默认)'}`",
            f"- 开始：{_stamp(self.started_at)}　结束：{_stamp(self.finished_at)}",
            f"- 结果：**{passed}/{len(self.results)} 通过**，失败 {len(self.failed)}",
            "",
            "| 场景 | 视口 | 结论 | 失败请求(断言到) | 耗时(s) |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in self.results:
            lines.append(
                f"| {item.scenario} | {item.viewport} | {'通过' if item.ok else '失败'} | {item.failed_requests} | {item.seconds:.1f} |"
            )
        failures = self.failed
        if failures:
            lines += ["", "## 失败详情", ""]
            for item in failures:
                lines += [
                    f"### {item.title}",
                    "",
                    "```",
                    item.error,
                    "```",
                    "",
                    f"- 复现：`python tools/e2e/run.py --only {item.scenario}`",
                    f"- 截图目录：`{item.out_dir}`",
                    "",
                ]
                for shot in item.screenshots:
                    lines.append(f"![{Path(shot).stem}]({Path(shot).name})")
                lines.append("")
        shots = [shot for item in self.results for shot in item.screenshots]
        if shots:
            lines += ["", "## 截图", ""]
            for item in self.results:
                for shot in item.screenshots:
                    lines.append(f"- `{shot}`")
        return "\n".join(lines) + "\n"

    def print_table(self) -> None:
        print()
        print(f"{'场景':<28}{'视口':<9}{'结论':<6}{'失败请求':>9}{'耗时':>8}")
        print("-" * 63)
        for item in self.results:
            mark = "通过" if item.ok else "失败"
            print(f"{item.scenario:<28}{item.viewport:<9}{mark:<6}{item.failed_requests:>9}{item.seconds:>7.1f}s")
        print("-" * 63)
        passed = len(self.results) - len(self.failed)
        print(f"合计 {len(self.results)} 项：通过 {passed}，失败 {len(self.failed)}")
        if self.failed:
            print()
            print("失败详情：")
            for item in self.failed:
                print(f"  ✗ {item.title}")
                for line in item.error.splitlines()[:8]:
                    print(f"    {line}")
                print(f"    截图：{item.out_dir}")
                print(f"    复现：python tools/e2e/run.py --only {item.scenario}")
        print()
        print(f"报告目录：{self.dir}")


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
