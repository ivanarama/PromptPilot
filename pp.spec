# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for PromptPilot (cross-platform)
# Build: pyinstaller pp.spec  (build.ps1 on Windows / build.sh on Linux/macOS)

import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

tg_datas, tg_binaries, tg_hiddenimports = collect_all('telegram')
tg_hiddenimports += collect_submodules('telegram')
httpx_datas, httpx_binaries, httpx_hiddenimports = collect_all('httpx')

# Tray GUI deps are Windows-only; skip them on headless Linux/macOS builds.
if sys.platform == 'win32':
    tray_hiddenimports = ['pystray._win32', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'PIL.ImageFont']
else:
    tray_hiddenimports = []

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[] + tg_binaries + httpx_binaries,
    datas=[
        # Bundle the web UI static files
        ('promptpilot/static', 'promptpilot/static'),
    ] + tg_datas + httpx_datas,
    hiddenimports=[
        # uvicorn dynamic imports
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        # click
        'click',
        # pydantic
        'pydantic',
    ] + tg_hiddenimports + httpx_hiddenimports + tray_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='pp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
