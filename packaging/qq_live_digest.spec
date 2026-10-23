# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

# SPECPATH 由 PyInstaller 注入，等于本 spec 所在目录；相对解析，不写死盘符。
_ICON = os.path.join(SPECPATH, "..", "assets", "icon.ico")
_MANIFEST = os.path.join(SPECPATH, "app.manifest")

hiddenimports = [
    "pypdfium2",
]
datas = []
binaries = []
for package in ("PIL", "pypdfium2"):
    try:
        d, b, h = collect_all(package)
    except ImportError:
        continue
    datas += d
    binaries += b
    hiddenimports += h

# 页面模板已外移到 qq_live_digest/web_static/page.html，必须随包带上，
# 否则冻结版 webapp 读不到 PAGE_HTML。
datas += [
    (
        os.path.join(SPECPATH, "..", "qq_live_digest", "web_static", "page.html"),
        "qq_live_digest/web_static",
    )
]

a = Analysis(
    ["../main.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], [], [], name="qq-live-digest", console=True, exclude_binaries=True, icon=_ICON, manifest=_MANIFEST, contents_directory="_internal")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="qq-live-digest")
