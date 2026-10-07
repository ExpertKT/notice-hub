#!/usr/bin/env python3
"""CodeBuddy smoke check; dry-run unless --live is explicitly supplied."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qq_digest import _CODEBUDDY_SCHEMA, detect_codebuddy_cli, run_codebuddy

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='actually invoke CodeBuddy')
    parser.add_argument('--cli', default='', help='headless CLI script path')
    args = parser.parse_args()
    cli = detect_codebuddy_cli(args.cli)
    command = ['node', cli or '<auto-detect>', '-p', '--tools', '', '--output-format', 'text', '--json-schema', _CODEBUDDY_SCHEMA, 'Reply with exactly OK']
    print('=== COMMAND ===')
    print(json.dumps(command, ensure_ascii=False))
    print('=== SCHEMA ===')
    print(_CODEBUDDY_SCHEMA)
    if args.live:
        print('=== RESPONSE ===')
        print(json.dumps(run_codebuddy('Reply with exactly OK', _CODEBUDDY_SCHEMA, cli=cli), ensure_ascii=False))
    else:
        print('dry-run (use --live to invoke)')
if __name__ == '__main__':
    main()
