"""Decode bytes emitted by Windows console tools (netstat, powershell, tasklist).

Why this exists
---------------
On a Chinese Windows those tools write **GBK (cp936)**, but `subprocess(...,
text=True)` without an explicit `encoding` decodes with whatever Python thinks the
current encoding is. That is usually fine, but as soon as UTF-8 mode is on
(`PYTHONUTF8=1`, or `-X utf8`, or Python 3.15+ defaults) the same call raises
`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbb ...`, and the user sees
something like `NapCat 安装失败：'utf-8' codec can't decode byte 0xbb in position 2`.

So we take **bytes** and decode them ourselves, trying UTF-8 first and falling back
to the ANSI code page. `errors="replace"` is used only as a last resort: a U+FFFD in
a path would be worse than a hard failure, but never crashing the whole
install/start flow over one stray byte is worse still — callers already validate what
they parsed.

Pure stdlib on purpose (README red line: no new third-party dependencies).
"""

from __future__ import annotations

import locale
import sys

# Tried in order. UTF-8 first because Node/Electron tooling (NapCat, tailscale) and
# PowerShell 7 both emit UTF-8; the ANSI code page catches classic netstat/powershell.
def _candidate_encodings() -> list[str]:
    candidates = ["utf-8"]
    for name in (locale.getpreferredencoding(False), sys.getfilesystemencoding()):
        if name and name.lower() not in {c.lower() for c in candidates}:
            candidates.append(name)
    # Chinese Windows reports cp936/mbcs; make sure it is always tried.
    for name in ("mbcs", "cp936", "gbk"):
        if name.lower() not in {c.lower() for c in candidates}:
            candidates.append(name)
    return candidates


def decode_console_output(raw: bytes | str | None) -> str:
    """Decode Windows console output without ever raising UnicodeDecodeError."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        # Already decoded by the caller (or by a text= call that somehow succeeded).
        return raw
    for encoding in _candidate_encodings():
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    # Every candidate failed (mixed-encoding garbage). Keep the ASCII structure —
    # callers here only ever regex numbers, ports and JSON punctuation out of this.
    return raw.decode("utf-8", errors="replace")


__all__ = ["decode_console_output"]
