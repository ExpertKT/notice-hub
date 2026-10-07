# -*- mode: python ; coding: utf-8 -*-
a = Analysis(["launcher.py"], pathex=["."], binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="QQ-Notice-Hub", console=True)
