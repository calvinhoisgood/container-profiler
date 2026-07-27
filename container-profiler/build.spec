# -*- mode: python ; coding: utf-8 -*-
import sys
import os

# Define src path
src_path = os.path.abspath('src')

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[src_path],
    binaries=[],
    # FORCE copy the profiler package source to the bundle root
    datas=[
        ('resources', 'resources'),
        ('src/profiler', 'profiler')
    ],
    hiddenimports=[
        'PyQt6.sip',
        'pyqtgraph',
        'docker',
        'pynvml',
        'construct',
        'requests', 
        'websocket',
        'urllib3',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'tkinter', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ContainerProfiler_v4',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False, 
    # icon='resources/icon.ico', # Icon removed as it is missing
)
