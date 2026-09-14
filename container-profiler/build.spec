# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

src_path = Path("src").resolve()
resources = Path("resources")
datas = []
if resources.exists():
    datas.append((str(resources), "resources"))

hiddenimports = [
    "PyQt6.sip",
    "pyqtgraph",
    "docker",
    "pynvml",
    "requests",
    "websocket",
    "urllib3",
]

a = Analysis(
    ["main.py"],
    pathex=[str(src_path)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "tkinter", "PIL"],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ContainerProfiler_v5",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(resources / "icon.ico") if (resources / "icon.ico").exists() else None,
)
