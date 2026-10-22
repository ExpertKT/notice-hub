#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python3}"

"$PYTHON" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"

if [[ ! -e "$ROOT/.env" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    sed -i 's/^QQ_DIGEST_WEB_HOST=.*/QQ_DIGEST_WEB_HOST=127.0.0.1/' "$ROOT/.env"
    printf 'Created %s from .env.example with loopback web access\n' "$ROOT/.env"
else
    printf 'Keeping existing %s\n' "$ROOT/.env"
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
ROOT="$ROOT" \
UNIT_TEMPLATE="$ROOT/packaging/linux/notice-hub.service" \
UNIT_DEST="$UNIT_DIR/notice-hub.service" \
    "$ROOT/.venv/bin/python" - <<'PY'
import os
from pathlib import Path

root = os.environ["ROOT"].replace("\\", "\\\\").replace('"', '\\"')
template = Path(os.environ["UNIT_TEMPLATE"]).read_text(encoding="utf-8")
Path(os.environ["UNIT_DEST"]).write_text(
    template.replace("@PROJECT_ROOT@", root), encoding="utf-8"
)
PY

printf 'Installed user unit: %s\n' "$UNIT_DIR/notice-hub.service"
printf 'Enable and start: systemctl --user daemon-reload && systemctl --user enable --now notice-hub.service\n'
printf 'Follow logs: journalctl --user -u notice-hub.service -f\n'
