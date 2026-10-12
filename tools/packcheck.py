from __future__ import annotations

import marshal
import struct
import sys
import zlib
from pathlib import Path

EXPECTED = {
    "webapp": ["/api/napcat/doctor", "napcat_running", "function runRestart()"],
    "napcat_admin": ["ensure_onebot_config", "onebot_config_servers", "diagnose"],
}

def load_pyz(exe: Path):
    data = exe.read_bytes()
    off = data.find(b"PYZ\x00")
    if off < 0 or data[off + 4:off + 8] != b"+\x0e\r\n":
        raise SystemExit("PYZ magic missing or invalid")
    toc_off = struct.unpack("!I", data[off + 8:off + 12])[0]
    toc = marshal.loads(data[off + toc_off:])
    return data, off, toc

def inspect(exe: Path, suffix: str):
    data, off, toc = load_pyz(exe)
    wanted = [entry for entry in toc if str(entry[0]).endswith(suffix)]
    strings, names = [], []
    for _name, (typ, moff, mlen) in wanted:
        blob = data[off + moff:off + moff + mlen]
        obj = marshal.loads(zlib.decompress(blob) if typ == 0 else blob)
        stack = [obj]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                strings.append(item)
            elif isinstance(item, (tuple, list, set, frozenset)):
                stack.extend(item)
            elif isinstance(item, dict):
                stack.extend(item); stack.extend(item.values())
            elif hasattr(item, "co_consts"):
                names.append(str(item.co_name)); stack.extend(item.co_consts)
    return "\n".join(strings + names)

def main() -> int:
    exe = Path(sys.argv[1])
    failures = 0
    data, off, toc = load_pyz(exe)
    print(f"{exe.name}: PYZ at {off}, {len(toc)} entries")
    for suffix, needles in EXPECTED.items():
        hay = inspect(exe, suffix)
        print(f"[{suffix}] chars={len(hay)}")
        for needle in needles:
            ok = needle in hay
            print(("PASS  " if ok else "FAIL  ") + f"[{suffix}] present: {needle}")
            failures += not ok
    print(f"=== {failures} package failure(s) ===")
    return int(bool(failures))

if __name__ == "__main__":
    raise SystemExit(main())
