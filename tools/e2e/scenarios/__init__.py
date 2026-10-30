"""剧本加载器：把本目录下所有 `*.py` 导入一遍，`@scenario` 自己完成登记。"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import harness


def load() -> list[harness.Scenario]:
    package_dir = Path(__file__).resolve().parent
    for module in sorted(pkgutil.iter_modules([str(package_dir)])):
        if module.name.startswith("_"):
            continue
        importlib.import_module(f"scenarios.{module.name}")
    return harness.get_scenarios()


__all__ = ["load"]
