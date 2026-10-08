# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

# SPECPATH 由 PyInstaller 注入，等于本 spec 所在目录；相对解析，不写死盘符。
_ICON = os.path.join(SPECPATH, "..", "assets", "icon.ico")

# pystray 的后端是动态导入（pystray._win32 / pystray._util），静态分析抓不到，必须显式声明。
hiddenimports = ["pystray._win32", "pystray._util"]
datas = []
binaries = []
for package in ("PIL", "pystray"):
    try:
        d, b, h = collect_all(package)
    except ImportError:
        continue
    datas += d
    binaries += b
    hiddenimports += h

# launcher 会 import qq_live_digest.hosting / qq_live_digest.config（与网页端共用同一份托管逻辑），
# 所以 pathex 必须指向仓库根。
a = Analysis(
    ["launcher.py"],
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
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="QQ-Notice-Hub", console=True, icon=_ICON)
