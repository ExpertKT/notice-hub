"""测试约定静态闸：三类要等 11 分钟全量门禁才暴露的坑，先在这里抓。

    python tools/test_conventions.py            # 人话清单；规则 A 命中就 exit 1
    python tools/test_conventions.py --verbose  # 跳过用例的完整清单（默认只打计数和没写理由的）
    python tools/test_conventions.py --json     # 机器可读

三条规则：

A（阻断）用例把数据落进仓库 `data/`。`noticehub/config.py` 的 `data_dir` 默认就是
  `<仓库>/data`，用例手拼 `Settings(...)` 不传 `data_dir` 会写坏开发机真数据，
  撞上同机实例还会 PermissionError 假红。
B（只警告）`self.skipTest(` / `@unittest.skip*` / `assertTrue(True)`：这仓库断言即契约，
  跳过和空断言会让门禁假绿。
C（只警告）`noticehub/` 下没有出现在 `tools/gate.py` `MODULE_MAP` 里的顶层模块/子包：
  改了这些代码，fast 档挑不到相关用例，只能退回全量。

为什么 A 的 Settings 那条要限定形状：树里现成就有上百处 `Settings()` 不带 `data_dir`
（`test_config.py` 只读属性那种），全拦等于门禁常红。真正会落盘的是「拼了 Settings、又拿它
去 `.data_dir` / 开 store/service」——只认这个形状。模块级 `SETTINGS = Settings()` 的 body
取整个文件，因为它在哪个函数里落盘都算这一个 settings 的事。
ponytail: 跨文件的 factory（模块级 settings 传给别的模块用）仍看不到；真踩到就做跨文件赋值追踪，别加白名单。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".venv", "build", "dist", "node_modules"}

# —— 规则 A：仓库 data 路径 / 没注入 data_dir 的 Settings ——

_REPO_DATA_PATTERNS = (
    ("仓库 data 路径（<root> / \"data\"）",
     re.compile(r"\b(?:PROJECT_ROOT|REPO_ROOT|PROJECT_DIR|repo_root|project_root|_?ROOT)\s*/\s*[\"']data[\"']")),
    ("仓库 data 路径（Path(__file__) … / \"data\"）",
     re.compile(r"Path\(\s*__file__\s*\)[^\n]{0,120}[\"']data[\"']")),
    ("仓库 data 路径（os.path.join）",
     re.compile(r"os\.path\.join\([^\n]{0,120}(?:__file__|PROJECT_ROOT|REPO_ROOT|\bROOT)[^\n]{0,120}[\"']data[\"']")),
)
# 「仓库 data 指回去」的断言是哨兵，不是违规：用例本来就该有一条比对。
_GUARD = re.compile(r"\bassert(?:NotEqual|NotIn|False|IsNot)\b")

_SETTINGS_CALL = re.compile(r"(?<![\w.])(?:_Settings|Settings)\(")
_SETTINGS_NAME = re.compile(r"(\w+)\s*(?:\.\w+\s*)?=\s*$")
# 会从 settings 推路径的入口。带显式 store= 的那几个不算（store 由用例自己指到 tmp）。
_PATH_ENTRIES = ("build_service", "create_app")
_STORE_ENTRIES = ("DigestService", "TaskWebServer", "WebApp")
_DEF_LINE = re.compile(r"^(\s*)(?:async\s+)?def\s")
_QUOTED = re.compile(r"[\"'][^\"'\n]*[\"']")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _call_arguments(text: str, open_paren: int) -> str:
    """从 `(` 开始配对括号，返回括号里的原文。配不上就返回到文件尾。"""
    depth = 0
    for index in range(open_paren, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:index]
    return text[open_paren + 1:]


def _enclosing_def(lines: list[str], line_number: int) -> tuple[int, int] | None:
    """第 line_number 行（1 起）所在的函数体，返回 (起, 止) 行下标；模块级返回 None。"""
    index = line_number - 1
    target = _indent(lines[index])
    start = None
    for i in range(index - 1, -1, -1):
        match = _DEF_LINE.match(lines[i])
        if match and len(match.group(1)) < target:
            start = i
            break
    if start is None:
        return None
    def_indent = len(_DEF_LINE.match(lines[start]).group(1))
    for end in range(start + 1, len(lines)):
        if lines[end].strip() and _indent(lines[end]) <= def_indent:
            return start, end
    return start, len(lines)


def _statement_text(lines: list[str], index: int) -> str:
    """index 这一行所属的整条语句（跨行的括号往里收）。"""
    start = index
    while start > 0 and lines[start - 1].rstrip().endswith(("(", ",")):
        start -= 1
    return "\n".join(lines[start:index + 1])


def _settings_reaches_data(text: str, name: str) -> bool:
    """这个 settings 变量有没有被当落盘路径用。

    `build_service` / `create_app` 自己按 settings 建 store，算；带 `store=` 的服务类不算
    ——store 由用例指到 tmp，settings 只当配置读。`settings.data_dir = ...` 是补丁，不算。
    """
    quoted = re.escape(name)
    for match in re.finditer(r"\b%s\.(?:data_dir|log_dir)\b" % quoted, text):
        if not re.match(r"\s*=(?!=)", text[match.end():]):
            return True
    for entry in _PATH_ENTRIES:
        if re.search(r"\b%s\(\s*%s\b" % (entry, quoted), text):
            return True
    for entry in _STORE_ENTRIES:
        for match in re.finditer(r"\b%s\(\s*%s\b" % (entry, quoted), text):
            if "store=" not in _call_arguments(text, match.start() + len(entry)):
                return True
    return False


def scan_a(path: Path, text: str) -> list[dict]:
    lines = text.splitlines()
    hits: list[dict] = []

    def add(line: int, message: str) -> None:
        if all(hit["line"] != line or hit["message"] != message for hit in hits):
            hits.append({"file": path, "line": line, "message": message})

    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            continue
        for reason, pattern in _REPO_DATA_PATTERNS:
            if not pattern.search(line):
                continue
            if not _GUARD.search(_statement_text(lines, number - 1)):
                add(number, reason + "；换成 tmp 目录")
            break

    for match in _SETTINGS_CALL.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if "#" in text[line_start:match.start()]:
            continue
        if "data_dir" in _call_arguments(text, match.end() - 1):
            continue
        line_number = _line_of(text, match.start())
        block = _enclosing_def(lines, line_number)
        # 模块级的 `SETTINGS = Settings()` 没有所在函数，就拿整个文件当 body：
        # 它在哪个函数里落盘都算这一个 settings 的事。
        body = text if block is None else "\n".join(lines[block[0]:block[1]])
        assigned = _SETTINGS_NAME.search(lines[line_number - 1][:match.start() - line_start])
        if assigned is None:
            # 内联用法：`build_service(Settings(...))` 这类，看同一行是不是那条按 settings
            # 建 store 的入口（服务类的 store 是位置参数，不在此列）。
            if not re.search(r"\b(?:%s)\s*\(" % "|".join(_PATH_ENTRIES),
                             lines[line_number - 1]):
                continue
            add(line_number, "Settings(...) 没传 data_dir，又直接喂给按它建 store 的入口，"
                             "会写进仓库 data/")
            continue
        name = assigned.group(1).rsplit(".", 1)[-1]
        if _settings_reaches_data(body, name):
            add(line_number, "%s 没传 data_dir，又被当落盘路径用（.data_dir / 建 service），"
                             "会写进仓库 data/" % name)
    return hits


# —— 规则 B：跳过用例 / 空断言 ——

_SKIP_PATTERNS = (
    re.compile(r"self\.skipTest\("),
    re.compile(r"@unittest\.skip"),
    re.compile(r"@skip(?:If|Unless)?\("),
    re.compile(r"assertTrue\(\s*True\s*\)"),
)


def scan_b(path: Path, text: str) -> list[dict]:
    hits: list[dict] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not any(pattern.search(line) for pattern in _SKIP_PATTERNS):
            continue
        # 理由可以是同一行的字符串，也可以写在下一行的多行装饰器里。
        reason = _QUOTED.search(line) or _QUOTED.search(
            text.splitlines()[number] if number < len(text.splitlines()) else "")
        hits.append({"file": path, "line": number, "reasoned": bool(reason),
                     "message": line.strip()[:120]})
    return hits


# —— 规则 C：noticehub/ 顶层模块有没有进 MODULE_MAP ——

def module_map() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """直接借 tools/gate.py 的表，别抄一份。"""
    tools_dir = str(ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import gate  # noqa: PLC0415 - 只在需要时借表
    except Exception:  # pragma: no cover - gate.py 被别人改坏时不该连带这里
        return ()
    return tuple(getattr(gate, "MODULE_MAP", ()))


def scan_c(root: Path, mapping) -> list[dict]:
    base = root / "noticehub"
    if not base.is_dir():
        return []
    hits: list[dict] = []
    for entry in sorted(base.iterdir()):
        if entry.name in SKIP_DIRS or (entry.is_file() and entry.suffix != ".py"):
            continue
        rel = "noticehub/" + entry.name + ("/" if entry.is_dir() else "")
        if any(prefix == rel.rstrip("/") or prefix.startswith(rel) for prefix, _ in mapping):
            continue
        hits.append({"file": entry, "line": 0,
                     "message": "%s 没进 MODULE_MAP，改这块时 fast 档挑不到用例" % rel})
    return hits


# —— 入口 ——

def collect(root: Path = ROOT) -> dict[str, list[dict]]:
    rule_a: list[dict] = []
    rule_b: list[dict] = []
    for path in sorted((root / "tests").rglob("*.py")):
        if SKIP_DIRS & set(path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rule_a.extend(scan_a(path, text))
        rule_b.extend(scan_b(path, text))
    return {"rule_a": rule_a, "rule_b": rule_b, "rule_c": scan_c(root, module_map())}


def _where(hit: dict, root: Path) -> str:
    try:
        shown = hit["file"].resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        shown = str(hit["file"])
    return "%s:%d" % (shown, hit["line"]) if hit["line"] else shown


def report(found: dict[str, list[dict]], root: Path, verbose: bool = False) -> int:
    rule_a, rule_b, rule_c = found["rule_a"], found["rule_b"], found["rule_c"]
    for hit in rule_a:
        print("A %s: %s" % (_where(hit, root), hit["message"]))
    if rule_a:
        print("规则 A 命中 %d 处：用例会把数据落进仓库 data/，先改成传 tmp 的 data_dir。"
              % len(rule_a))

    unreasoned = [hit for hit in rule_b if not hit["reasoned"]]
    print("规则 B：%d 处跳过/空断言，%s"
          % (len(rule_b), ("其中 %d 处没写理由" % len(unreasoned)) if unreasoned else "都写了理由"))
    for hit in (rule_b if verbose else unreasoned):
        print("B %s: %s" % (_where(hit, root), hit["message"]))

    if rule_c:
        print("规则 C：%d 个顶层模块没进 MODULE_MAP%s"
              % (len(rule_c), "" if verbose else "（清单要 --verbose）"))
        if verbose:
            for hit in rule_c:
                print("C %s: %s" % (_where(hit, root), hit["message"]))
    return 1 if rule_a else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测试约定静态闸")
    parser.add_argument("--json", action="store_true", help="输出机器可读清单")
    parser.add_argument("--verbose", action="store_true", help="打印跳过用例的完整清单")
    parser.add_argument("--root", default=str(ROOT), help="仓库根（默认本脚本上一级）")
    args = parser.parse_args(argv)
    root = Path(args.root)
    found = collect(root)
    if args.json:
        payload = {key: [{"file": _where({**hit, "line": 0}, root), "line": hit["line"],
                          "message": hit["message"]} for hit in hits]
                   for key, hits in found.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 1 if found["rule_a"] else 0
    return report(found, root, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
