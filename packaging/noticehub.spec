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

# 页面模板与 PWA 资产已外移到 noticehub/web_static/，必须整个目录随包带上。
# webapp 在**导入时**就读 page.html、manifest.webmanifest、icon.svg（模块级常量），
# 少一个就是启动即 FileNotFoundError、/api/health 永远不应答——CI 的托盘冒烟
# 正是这样红过（run 41267574698）。所以这里按目录收，不逐个点名。
datas += [
    (
        os.path.join(SPECPATH, "..", "noticehub", "web_static"),
        "noticehub/web_static",
    ),
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
exe = EXE(pyz, a.scripts, [], [], [], name="noticehub", console=True, exclude_binaries=True, icon=_ICON, manifest=_MANIFEST, contents_directory="_internal")
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="noticehub")
