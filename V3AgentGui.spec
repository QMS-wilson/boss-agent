# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/重构/进销存-本地Agent源码/agent_gui.py'],
    pathex=['D:/重构/进销存-本地Agent源码'],
    binaries=[],
    datas=[('D:/重构/进销存-本地Agent源码/assets/icons/v3agent.ico', 'assets/icons'), ('D:/重构/进销存-本地Agent源码/config.template.json', '.'), ('D:/重构/进销存-本地Agent源码/build/_pkginfo/VERSION.json', '.')],
    hiddenimports=['v3agent', 'v3agent.main', 'v3agent.agent', 'v3agent.ops', 'v3agent.config', 'v3agent.sql', 'agent_daemon', 'agent_gui', 'websockets', 'websockets.asyncio', 'websockets.asyncio.client', 'websockets.legacy', 'websockets.legacy.client', 'pymssql', '_mssql', 'tkinter', 'tkinter.scrolledtext', 'tkinter.messagebox', 'tkinter.ttk'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['D:/重构/进销存-本地Agent源码/scripts/rthook_reap_mei.py'],
    excludes=[],
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
    name='V3AgentGui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:/重构/进销存-本地Agent源码/assets/icons/v3agent.ico'],
)
