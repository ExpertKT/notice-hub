#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    printf 'This installer supports macOS only.\n' >&2
    exit 1
fi

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
PYTHON="${PYTHON:-python3}"
PYTHON="$(command -v "$PYTHON" || true)"
if [[ -z "$PYTHON" || ! -x "$PYTHON" ]]; then
    printf 'python3 was not found. Install Python 3.10 or newer first.\n' >&2
    exit 1
fi
"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY

"$PYTHON" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"

if [[ ! -e "$ROOT/.env" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
fi
if grep -q '^QQ_DIGEST_WEB_HOST=' "$ROOT/.env"; then
    sed -i '' 's/^QQ_DIGEST_WEB_HOST=.*/QQ_DIGEST_WEB_HOST=127.0.0.1/' "$ROOT/.env"
else
    printf '\nQQ_DIGEST_WEB_HOST=127.0.0.1\n' >> "$ROOT/.env"
fi

mkdir -p "$ROOT/logs"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/com.qqnoticehub.service.plist"
mkdir -p "$PLIST_DIR"
if launchctl list | grep -Fq 'com.qqnoticehub.service'; then
    launchctl unload "$PLIST"
fi
ROOT="$ROOT" \
PLIST_TEMPLATE="$ROOT/packaging/macos/com.qqnoticehub.service.plist" \
PLIST_DEST="$PLIST" \
    "$ROOT/.venv/bin/python" - <<'PY'
import os
from pathlib import Path
from xml.sax.saxutils import escape

root = escape(os.environ["ROOT"])
template = Path(os.environ["PLIST_TEMPLATE"]).read_text(encoding="utf-8")
Path(os.environ["PLIST_DEST"]).write_text(
    template.replace("@PROJECT_ROOT@", root), encoding="utf-8"
)
PY
plutil -lint "$PLIST"
launchctl load "$PLIST"
printf 'Loaded LaunchAgent: %s\n' "$PLIST"
printf 'Open: http://127.0.0.1:8766/\n'
printf 'Logs: %s/logs/launchd.out.log and %s/logs/launchd.err.log\n' "$ROOT" "$ROOT"
