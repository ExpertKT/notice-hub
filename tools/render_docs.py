"""把 ``docs/contest/*.md`` 里的 ``{{facts.xxx}}`` 换成 facts.json 里的值。

源文档写占位，``docs/contest/rendered/`` 里是发给评委的成品。占位名走点号：
``{{facts.trip_meters}}``、``{{facts.code_lines.python}}``、``{{facts.routes.methods.GET}}``。

    .venv\\Scripts\\python.exe -X utf8 tools/render_docs.py           # 写成品
    .venv\\Scripts\\python.exe -X utf8 tools/render_docs.py --check   # 只校验，返回非零=不一致

``--check`` 会红在三件事上：占位解析不出来（写错了名、或者那个值是 null）、成品缺了、
成品跟「源文档 + 当前 facts」对不上（也就是改了文档或 facts 却没重渲染）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DOCS = ROOT / "docs" / "contest"
FACTS = DOCS / "facts.json"
OUT_DIR = DOCS / "rendered"
RENDERED_SUFFIX = ".md"

PLACEHOLDER = re.compile(r"\{\{facts\.([A-Za-z0-9_.]+)\}\}")


class FactError(KeyError):
    """占位解析不出来：名字写错，或者这条事实是 null（带 why）。"""


def load_facts(path: Path = FACTS) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve(facts: dict, dotted: str):
    """按点号取值。叶子是 ``{"value": ...}`` 就拿 value，裸标量原样返回。"""
    node = facts
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise FactError("%s 里没有这一项" % dotted)
        node = node[part]
    if isinstance(node, dict):
        if "value" not in node:
            raise FactError("%s 不是一条事实（没有 value）" % dotted)
        if node["value"] is None:
            raise FactError("%s 没有值：%s" % (dotted, node.get("why") or "未说明"))
        return node["value"]
    return node


def format_value(value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return repr(round(value, 6))
    return str(value)


def render_text(text: str, facts: dict) -> str:
    def replace(match: re.Match) -> str:
        return format_value(resolve(facts, match.group(1)))

    return PLACEHOLDER.sub(replace, text)


def placeholders(text: str) -> list[str]:
    return PLACEHOLDER.findall(text)


def source_docs(docs: Path = DOCS) -> dict[str, str]:
    """``{文件名: 源文档正文}``。rendered/ 里的成品不算源。"""
    found = {}
    for path in sorted(docs.glob("*" + RENDERED_SUFFIX)):
        if path.parent != docs:
            continue
        found[path.name] = path.read_text(encoding="utf-8")
    return found


def read_rendered(docs: Path = DOCS, out_dir: Path | None = None) -> dict[str, str]:
    folder = out_dir or (docs / "rendered")
    if not folder.is_dir():
        return {}
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(folder.glob("*" + RENDERED_SUFFIX))}


def problems_for(sources: dict[str, str], rendered: dict[str, str], facts: dict) -> list[str]:
    """比对「源文档 + facts」和成品。纯函数，测试直接拿它演一遍漂移。"""
    problems: list[str] = []
    for name, text in sources.items():
        names = placeholders(text)
        if not names:
            # 没有占位的文档不需要成品；有的话也不能是旧内容。
            if name in rendered and rendered[name] != text:
                problems.append("%s：文档里没有占位，成品却跟源文档不一样" % name)
            continue
        try:
            expected = render_text(text, facts)
        except FactError as error:
            problems.append("%s：%s" % (name, error))
            continue
        if name not in rendered:
            problems.append("%s：缺成品 docs/contest/rendered/%s" % (name, name))
            continue
        if "{{facts." in rendered[name]:
            problems.append("%s：成品里还有没替换的 {{facts.…}}" % name)
        if rendered[name] != expected:
            problems.append("%s：成品与「源文档 + facts.json」对不上，重跑 render_docs" % name)
    return problems


def render_all(docs: Path = DOCS, out_dir: Path | None = None, facts: dict | None = None) -> list[str]:
    """写成品，返回写过的文件名。"""
    folder = out_dir or (docs / "rendered")
    data = facts if facts is not None else load_facts(docs / "facts.json")
    written = []
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in source_docs(docs).items():
        if not placeholders(text):
            continue
        (folder / name).write_text(render_text(text, data), encoding="utf-8", newline="\n")
        written.append(name)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="渲染 docs/contest 的 {{facts.*}} 占位")
    parser.add_argument("--check", action="store_true", help="只校验，不写盘；不一致返回 1")
    parser.add_argument("--docs", type=Path, default=DOCS, help="文档目录")
    parser.add_argument("--out", type=Path, default=None, help="成品目录，默认 <docs>/rendered")
    args = parser.parse_args(argv)

    facts = load_facts(args.docs / "facts.json")
    if args.check:
        problems = problems_for(source_docs(args.docs), read_rendered(args.docs, args.out), facts)
        for item in problems:
            print("✗ %s" % item)
        if problems:
            print("共 %d 处不一致" % len(problems))
            return 1
        print("占位与 facts.json 一致，成品没有旧数")
        return 0

    written = render_all(args.docs, args.out, facts)
    print("渲染了 %d 份：%s" % (len(written), "、".join(written) or "（没有文档用到占位）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
