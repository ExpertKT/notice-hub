from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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
try:
    from qq_live_digest.webapp import PAGE_HTML
except Exception as exc:
    failures += 1
    print(f"FAIL  PAGE_HTML import: {exc}")
else:
    scripts = re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", PAGE_HTML, re.IGNORECASE | re.DOTALL)
    if not scripts:
        failures += 1
        print("FAIL  PAGE_HTML script extraction: no script blocks")
    elif shutil.which("node") is None:
        print(f"SKIP  PAGE_HTML JavaScript syntax: node unavailable ({len(scripts)} blocks)")
    else:
        for index, script in enumerate(scripts):
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
                handle.write(script)
                js_path = handle.name
            result = subprocess.run(["node", "--check", js_path], text=True, capture_output=True)
            Path(js_path).unlink(missing_ok=True)
            if result.returncode:
                failures += 1
                print(f"FAIL  PAGE_HTML JavaScript syntax block {index}: {result.stdout}{result.stderr}")
            else:
                print(f"PASS  PAGE_HTML JavaScript syntax block {index}")

print(f"=== {failures} syntax failure(s) ===")
raise SystemExit(1 if failures else 0)
