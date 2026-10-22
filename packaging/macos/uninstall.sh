#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    printf 'This uninstaller supports macOS only.\n' >&2
    exit 1
fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--purge" ) ]]; then
    printf 'Usage: bash packaging/macos/uninstall.sh [--purge]\n' >&2
    exit 2
fi

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if [[ -z "$ROOT" || "$ROOT" == "/" ]]; then
    printf 'Refusing to uninstall from an unsafe project path.\n' >&2
    exit 1
fi
PLIST="$HOME/Library/LaunchAgents/com.qqnoticehub.service.plist"
if [[ -f "$PLIST" ]]; then
    if launchctl list | grep -Fq 'com.qqnoticehub.service'; then
        launchctl unload "$PLIST"
    fi
    rm -f "$PLIST"
fi
printf 'Unloaded LaunchAgent and removed %s\n' "$PLIST"

if [[ "${1:-}" == "--purge" ]]; then
    DATA_DIR="$ROOT/data"
    printf 'WARNING: this permanently deletes the local database and task/message data in %s.\n' "$DATA_DIR"
    read -r -p 'Type yes to permanently delete this data: ' answer
    if [[ "$answer" != "yes" ]]; then
        printf 'Data kept.\n'
        exit 0
    fi
    if [[ -L "$DATA_DIR" ]]; then
        printf 'Data directory is a symlink; kept for safety: %s\n' "$DATA_DIR"
    elif [[ -d "$DATA_DIR" ]]; then
        rm -rf -- "$DATA_DIR"
        printf 'Project data directory removed. .env, logs, and .venv were kept.\n'
    else
        printf 'No project data directory found; .env, logs, and .venv were kept.\n'
    fi
fi
