"""E2E 骨架对外入口。

剧本这样写（别的模块只依赖这三个名字）：

    from harness import scenario, cold_install

    @scenario("inbox/delete", viewports=("desktop", "mobile"), tags=("fakes:none",))
    def run(app):
        app.goto("/")
        ...

冷装由 run.py 统一做（一个场景一份全新实例，或 --reuse 复用同一个），剧本只拿 `app`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .app import VIEWPORTS, App
from .install import ColdInstallError, Install, cold_install
from .report import CaseResult, RunReport

__all__ = [
    "App",
    "CaseResult",
    "ColdInstallError",
    "Install",
    "RunReport",
    "VIEWPORTS",
    "Scenario",
    "cold_install",
    "get_scenarios",
    "scenario",
]


@dataclass
class Scenario:
    name: str
    func: Callable[[App], None]
    viewports: tuple[str, ...] = ("desktop",)
    tags: tuple[str, ...] = field(default_factory=tuple)
    doc: str = ""

    def matches(self, needles: list[str]) -> bool:
        """空过滤 = 全选；否则名字或任一 tag 里含该子串。"""
        if not needles:
            return True
        haystacks = [self.name, *self.tags]
        return any(needle in hay for needle in needles for hay in haystacks)


_REGISTRY: dict[str, Scenario] = {}


def scenario(name: str, *, viewports: tuple[str, ...] = ("desktop",), tags: tuple[str, ...] = ()):
    """把一个函数登记成剧本。name 同时用于 --only 过滤与报告。"""
    for viewport in viewports:
        if viewport not in VIEWPORTS:
            raise ValueError(f"剧本 {name} 声明了未知视口 {viewport}")

    def decorator(func: Callable[[App], None]) -> Callable[[App], None]:
        if name in _REGISTRY:
            raise ValueError(f"剧本重名：{name}")
        _REGISTRY[name] = Scenario(
            name=name,
            func=func,
            viewports=tuple(viewports),
            tags=tuple(tags),
            doc=(func.__doc__ or "").strip(),
        )
        return func

    return decorator


def get_scenarios() -> list[Scenario]:
    return sorted(_REGISTRY.values(), key=lambda item: item.name)
