"""一键诊断包：``python main.py doctor --bundle``。

用户出问题时把生成的 zip 发给我们，省掉来回问环境。包里只有：

    version.txt / env.sanitized.txt / paths.txt / db.txt
    logs/（最近 2 个日志的末尾各 200 行）
    state.txt（updates/state.json、updates/pending.json、hosting.json）
    napcat.txt / README.txt

两条硬约束：
* 全程只读（唯一的写是那个 zip），不改用户的 ``data/**``。
* 所有文本先过 :func:`sanitize`；``.env`` 只写键名与「已设置/长度」，不写值。
"""

from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import Settings, parse_env_file
from .version import APP_CHANNEL, APP_VERSION

MASK = "***"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_SECRET_WORD = (
    r"(?:token|secret|password|passwd|pwd|cookie|api[_-]?key|apikey|auth|session"
    r"|signature|private[_-]?key|client[_-]?secret)"
)
# key=value / "key": "value" / key: value —— 键名保留，值整段打码。
_KV = re.compile(
    rf'(?i)("?[A-Za-z0-9_.\-]*{_SECRET_WORD}[A-Za-z0-9_.\-]*"?\s*[:=]\s*)'
    r'("[^"\r\n]*"|[^\s,;&"\r\n]+)'
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{6,}")
_HEADER = re.compile(
    r"(?i)^(?P<name>cookie|set-cookie|authorization|x-api-key|proxy-authorization)\s*:\s*(?P<value>.+)$",
    re.MULTILINE,
)
_SECRET_KEY = re.compile(rf"(?i){_SECRET_WORD}")


class DoctorError(RuntimeError):
    """诊断包生成失败（包装成一句能读的话，不抛栈）。"""


def sanitize(text: Any) -> str:
    """把看起来像凭据的值换成 ``***``。

    覆盖三种常见写法：``KEY=value`` / ``"key": "value"``、``Bearer xxx``、
    ``Cookie: …`` 这类整行头。键名一律保留，方便排查「哪一项没配」。
    """
    out = _KV.sub(lambda match: match.group(1) + MASK, str(text))
    out = _BEARER.sub(lambda match: match.group(1) + " " + MASK, out)
    out = _HEADER.sub(lambda match: match.group("name") + ": " + MASK, out)
    return out


def _sanitize_obj(value: Any, depth: int = 0) -> Any:
    """递归打码 JSON：敏感键整值打码，长字符串截断。"""
    if depth > 6:
        return "（层级过深，已省略）"
    if isinstance(value, dict):
        return {
            str(key): (MASK if _SECRET_KEY.search(str(key)) else _sanitize_obj(item, depth + 1))
            for key, item in list(value.items())[:60]
        }
    if isinstance(value, (list, tuple)):
        items = list(value)
        return [_sanitize_obj(item, depth + 1) for item in items[:20]] + (
            [f"（还有 {len(items) - 20} 项）"] if len(items) > 20 else []
        )
    if isinstance(value, str):
        return sanitize(value)[:400]
    return value


def _dump_json(value: Any) -> str:
    return json.dumps(_sanitize_obj(value), ensure_ascii=False, indent=2, default=str)


def _read_text(path: Path, limit: int = 4_000_000) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError as error:
        return f"（读取失败：{error}）"


def _tail_lines(path: Path, count: int = 200) -> str:
    text = _read_text(path)
    lines = text.splitlines()
    return "\n".join(lines[-count:])


def _tree_facts(path: Path) -> str:
    if not path.exists():
        return "不存在"
    if path.is_file():
        return f"文件 {path.stat().st_size} 字节"
    files = total = 0
    for current, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                pass
            if files >= 20_000:
                break
        if files >= 20_000:
            break
    return f"{files} 个文件 / {total} 字节"


def _writable(path: Path) -> str:
    try:
        return "是" if os.access(path, os.W_OK) else "否"
    except OSError:
        return "未知"


def _env_summary(settings: Settings) -> str:
    env_file = Path(settings.env_file) if getattr(settings, "env_file", None) else None
    lines = [f".env：{env_file or '（未指定）'}"]
    if env_file is None or not env_file.is_file():
        lines.append("（没有 .env 文件，全部按默认值运行）")
        return "\n".join(lines) + "\n"
    loaded = parse_env_file(env_file)
    lines.append(f"共 {len(loaded)} 个键；值一律不写，只写「已设置 / 长度」：")
    for key in sorted(loaded):
        value = str(loaded.get(key) or "")
        # 刻意不用 ``键=值`` 的样子：整包出口会统一再过一遍 sanitize，写成等号反而会被打码。
        lines.append(f"  {key}  {'未设置' if not value else f'已设置（长度 {len(value)}）'}")
    return "\n".join(lines) + "\n"


def _version_text(settings: Settings) -> str:
    frozen = bool(getattr(sys, "frozen", False))
    return "\n".join(
        [
            f"APP_VERSION: {APP_VERSION}",
            f"APP_CHANNEL: {APP_CHANNEL}",
            f"update_channel(env): {settings.update_channel or '（未设置，跟随 APP_CHANNEL）'}",
            f"Python: {platform.python_version()} ({platform.python_implementation()})",
            f"OS: {platform.platform()}",
            f"frozen: {'是' if frozen else '否'}",
            f"executable: {sys.executable}",
            f"cwd: {Path.cwd()}",
        ]
    ) + "\n"


def _paths_text(settings: Settings) -> str:
    install_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_ROOT
    data_dir = Path(settings.data_dir)
    log_dir = Path(settings.log_dir)
    updates_dir = data_dir / "updates"
    lines = ["安装根 / 数据 / 日志 / 更新缓存，只读探测：", ""]
    for label, path in (
        ("安装根", install_root),
        ("数据目录", data_dir),
        ("日志目录", log_dir),
        ("更新缓存目录", updates_dir),
    ):
        lines.append(f"{label}：{path}")
        lines.append(f"  存在：{'是' if path.exists() else '否'}；大小：{_tree_facts(path)}；可写：{_writable(path)}")
    return "\n".join(lines) + "\n"


def _db_text(settings: Settings) -> str:
    path = Path(settings.data_dir) / "digest.sqlite3"
    lines = [f"数据库：{path}", f"文件存在：{'是' if path.is_file() else '否'}"]
    if path.is_file():
        lines.append(f"文件大小：{path.stat().st_size} 字节")
    if not path.is_file():
        return "\n".join(lines) + "\n"
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        check = connection.execute("PRAGMA integrity_check").fetchone()
        lines.append(f"integrity_check：{check[0] if check else '（无结果）'}")
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        lines.append(f"表数量：{len(tables)}")
        for table in tables:
            try:
                count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                lines.append(f"  {table}: {count} 行")
            except sqlite3.Error as error:
                lines.append(f"  {table}: 读取失败（{error}）")
    except sqlite3.Error as error:
        lines.append(f"（数据库打不开或已损坏：{error}）")
    finally:
        if connection is not None:
            connection.close()
    return "\n".join(lines) + "\n"


def _logs_sections(settings: Settings) -> dict[str, str]:
    log_dir = Path(settings.log_dir)
    candidates = [path for path in log_dir.glob("noticehub.log*") if path.is_file()] if log_dir.is_dir() else []
    candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    picked = candidates[:2]
    if not picked:
        return {"logs/_no-logs.txt": f"日志目录 {log_dir} 下没有 noticehub.log*。\n"}
    return {
        f"logs/{path.name}": f"源文件：{path}（末尾 200 行）\n\n" + _tail_lines(path, 200) + "\n"
        for path in picked
    }


def _napcat_text(napcat: dict[str, Any]) -> str:
    lines = [
        "NapCat / QQ 托管状态（只读探测，不启动、不停止任何进程）：",
        _dump_json(napcat),
        "",
        "说明：napcat_running / napcat_online 来自本地端口与 NapCat 自身接口；",
        "hosting_active 只反映 data/hosting.json 里我们自己的开关，不代表 QQ 真在运行。",
    ]
    return "\n".join(lines) + "\n"


def _state_text(settings: Settings) -> str:
    data_dir = Path(settings.data_dir)
    lines = []
    for label, path in (
        ("updates/state.json", data_dir / "updates" / "state.json"),
        ("updates/pending.json", data_dir / "updates" / "pending.json"),
        ("hosting.json", data_dir / "hosting.json"),
    ):
        lines.append(f"== {label} ==")
        lines.append(f"路径：{path}；存在：{'是' if path.is_file() else '否'}")
        if path.is_file():
            try:
                lines.append(_dump_json(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError) as error:
                lines.append(f"（读取或解析失败：{error}）")
        lines.append("")
    return "\n".join(lines) + "\n"


def _napcat_probe(settings: Settings) -> dict[str, Any]:
    from . import hosting

    try:
        status = hosting.hosting_status(settings)
    except Exception as error:  # noqa: BLE001 - 探测失败不该让整包失败
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return status if isinstance(status, dict) else {"ok": False, "error": "返回类型异常"}


def _readme_text(bundle_name: str) -> str:
    return (
        "【这是什么】\n"
        f"{bundle_name} 是 notice-hub 的一键诊断包，用于排查「跑不起来 / 推送不通 / 更新异常」。\n\n"
        "【已脱敏】\n"
        "- .env 只有键名与「已设置 / 长度」，不含任何值；\n"
        "- 日志、state 里的 token / cookie / password / api_key / secret / Authorization 一律打码为 ***；\n"
        "- 数据库只导出文件大小、integrity_check 与各表行数，不导出消息内容。\n\n"
        "【怎么发给我们】\n"
        "把这个 zip 直接发到群里或私聊维护者即可，不需要另外造复现步骤。\n"
        "如果还打不开程序，请一并说明：双击图标后的报错原文、当时在做什么操作。\n"
    )


def default_out_dir() -> Path:
    """默认放桌面；没有桌面（或 Mac 中文桌面）就退到系统临时目录。"""
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        return desktop
    return Path(tempfile.gettempdir())


def _resolve_target(out: Path | str | None) -> Path:
    if out is None or str(out).strip() == "":
        return default_out_dir() / f"notice-hub-doctor-{datetime.now():%Y%m%d-%H%M%S}.zip"
    target = Path(str(out)).expanduser()
    if target.suffix.lower() == ".zip":
        return target
    return target / f"notice-hub-doctor-{datetime.now():%Y%m%d-%H%M%S}.zip"


def build_bundle(
    settings: Settings,
    out: Path | str | None = None,
    *,
    napcat: dict[str, Any] | Callable[[Settings], dict[str, Any]] | None = None,
) -> Path:
    """生成诊断包并返回 zip 的绝对路径；失败抛 :class:`DoctorError`。"""
    bundle = _resolve_target(out)
    if bundle.exists() and bundle.is_dir():
        raise DoctorError(f"输出路径是个目录：{bundle}")
    if bundle.parent.exists() and not bundle.parent.is_dir():
        raise DoctorError(f"输出目录不可用（父路径是文件）：{bundle.parent}")

    probe = napcat if napcat is not None else _napcat_probe
    napcat_info = probe(settings) if callable(probe) else probe
    if not isinstance(napcat_info, dict):
        napcat_info = {"ok": False, "error": "探测结果不是字典"}

    sections: dict[str, str] = {
        "version.txt": _version_text(settings),
        "env.sanitized.txt": _env_summary(settings),
        "paths.txt": _paths_text(settings),
        "db.txt": _db_text(settings),
        "state.txt": _state_text(settings),
        "napcat.txt": _napcat_text(napcat_info),
        "README.txt": _readme_text(bundle.name),
    }
    sections.update(_logs_sections(settings))

    try:
        bundle.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in sections.items():
                # 唯一出口：每段都过一遍 sanitize，别指望各段自己记得打码。
                archive.writestr(name, sanitize(text))
    except (OSError, zipfile.BadZipFile) as error:
        try:
            bundle.unlink(missing_ok=True)
        except OSError:
            pass
        raise DoctorError(
            f"生成 {bundle} 失败：{error}（换一个可写目录再试：--bundle <目录>）"
        ) from error
    return bundle.resolve()


__all__ = ["DoctorError", "build_bundle", "default_out_dir", "sanitize"]
