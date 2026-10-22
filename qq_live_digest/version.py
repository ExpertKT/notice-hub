"""应用版本常量。

只放常量，不加任何副作用/依赖：启动器（`packaging/launcher.py`）与网页端
都要 import 它。`updater.check()` 用它和 GitHub 上的 release tag 比较，
网页「设置 → 版本更新」用它显示「当前版本」。

版本号只许往前推。发布时由维护者改这里的值，别在别处硬编码版本字符串。
"""

from __future__ import annotations

APP_VERSION = "1.1.0-beta.6"
APP_CHANNEL = "beta"  # "stable" | "beta"：决定检查 /releases/latest 还是 /releases 列表

__all__ = ["APP_VERSION", "APP_CHANNEL"]


