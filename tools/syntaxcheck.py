from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
failures = 0
for path in sorted(ROOT.rglob("*.py")):
    if any(part in {".venv", "__pycache__", ".git", "build", "dist"} for part in path.parts):
        continue
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        print(f"PASS  python syntax: {path.relative_to(ROOT)}")
    except Exception as exc:
        failures += 1
        print(f"FAIL  python syntax: {path.relative_to(ROOT)} | {exc}")
for path in sorted((ROOT / "tools").glob("*.ps1")):
    probe = (
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile(" 
        f"'{path}', [ref]$tokens, [ref]$errors); "
        "if ($errors.Count) { $errors | % Message; exit 1 }"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", probe], text=True, capture_output=True)
    if result.returncode:
        failures += 1
        print(f"FAIL  powershell syntax: {path.relative_to(ROOT)} | {result.stdout.strip()} {result.stderr.strip()}")
    else:
        print(f"PASS  powershell syntax: {path.relative_to(ROOT)}")
print(f"=== {failures} syntax failure(s) ===")
raise SystemExit(1 if failures else 0)
