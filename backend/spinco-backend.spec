# -*- mode: python ; coding: utf-8 -*-
"""Phase 11 - PyInstaller spec for the desktop-packaged FastAPI backend.

Rebuild with (from backend/, with .venv active):
    pyinstaller spinco-backend.spec --distpath dist_backend --workpath build --noconfirm

Output is a one-dir bundle at backend/dist_backend/spinco-backend/ containing
spinco-backend.exe plus its dependencies - electron/scripts/build-backend.js
runs this automatically as part of `npm run dist` and copies the result into
Electron's extraResources. Never used by the dev workflow (dev keeps running
`uvicorn app.main:app --reload --port 8001` directly).

collect_all() is used for uvicorn/pymupdf/fitz/argon2/pydantic because each
does non-trivial dynamic imports or ships compiled extensions that plain
static analysis misses - confirmed necessary by trial build+run against a
real Expense Report PDF (OCR fallback and all) rather than assumed.
"""
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['app.main']
hiddenimports += collect_submodules('PIL')
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pymupdf')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('fitz')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('argon2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pydantic')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['run_server.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='spinco-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='spinco-backend',
)
