# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# services/ is bundled as DATA (below), so PyInstaller never analyzes its
# imports statically — every third-party lib it lazily imports must be
# collected explicitly here or the frozen exe raises "No module named X"
# only when that code path runs at runtime.
_collect = [
    'playwright',   # facebook_card browser automation (bundles the node driver)
    'requests',
    'certifi',      # TLS CA bundle for requests
    'dotenv',
]

datas = [
    ('services', 'services'),
    ('agent_core.py', '.'),
    ('config.py', '.'),
    ('prosperidadelogo.ico', '.'),
]
binaries = []
hiddenimports = [
    'websockets',
    'websockets.asyncio',
    'websockets.asyncio.client',
    'websockets.asyncio.connection',
    'websockets.asyncio.messages',
    'websockets.client',
    'websockets.exceptions',
    'websockets.frames',
    'websockets.headers',
    'websockets.http11',
    'websockets.protocol',
    'websockets.streams',
    'websockets.sync',
    'websockets.sync.client',
    'websockets.uri',
    'tkinter',
    'tkinter.ttk',
    'asyncio',
    'concurrent.futures',
    'queue',
    'threading',
    'uuid',
    'services.facebook_card',
    'services.facebook_link',
    'services.facebook_scan',
    'services.adspower',
    'services.manager_api',
    'requests.adapters',
    'requests.auth',
    'requests.exceptions',
    'urllib3',
    'urllib3.util.retry',
    'json',
    'pathlib',
    'traceback',
]

for _pkg in _collect:
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ['agent_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Server-only packages — this client is a thin WS client and never
        # runs the Flask app or touches the DB directly.
        'flask',
        'flask_sqlalchemy',
        'flask_login',
        'flask_sock',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Card Client',
    icon=['prosperidadelogo.ico'],
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX can corrupt native extensions (playwright driver)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
