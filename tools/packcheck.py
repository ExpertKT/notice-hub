"""Inspect the bundled PYZ archive without importing application code."""
from __future__ import annotations
import marshal
import pathlib
import struct
import sys
import types


def walk(code: types.CodeType, names: set[str], strings: set[str]) -> None:
    names.add(code.co_name)
    for value in code.co_consts:
        if isinstance(value, str):
            strings.add(value)
        elif isinstance(value, types.CodeType):
            walk(value, names, strings)


def main(path: str) -> int:
    data = pathlib.Path(path).read_bytes()
    marker = data.find(b"PYZ\x00")
    if marker < 0:
        print("FAIL PYZ magic not found")
        return 1
    toc_off = struct.unpack("!I", data[marker + 8:marker + 12])[0]
    toc = marshal.loads(data[marker + toc_off:])
    names: set[str] = set()
    strings: set[str] = set()
    for entry in toc.values():
        if isinstance(entry, tuple):
            offset = entry[-1]
        else:
            offset = entry
        walk(marshal.loads(data[marker + offset:]), names, strings)
    required_names = {"main", "run", "_handle_request"}
    required_markers = {"/api/health", "/api/ics", "/api/subscriptions"}
    print(f"PYZ marker={marker} toc_entries={len(toc)} code_names={len(names)} strings={len(strings)}")
    missing_names = sorted(required_names - names)
    missing_markers = sorted(required_markers - strings)
    print(f"required function names: {sorted(required_names)} missing={missing_names}")
    print(f"required page/API markers: {sorted(required_markers)} missing={missing_markers}")
    return int(bool(missing_names or missing_markers))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
