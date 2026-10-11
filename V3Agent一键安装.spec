# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/重构/进销存-本地Agent源码/setup_gui/setup_app.py'],
    pathex=['D:/重构/进销存-本地Agent源码'],
    binaries=[('D:/重构/进销存-本地Agent源码/dist/V3AgentGui.exe', '.')],
    datas=[],
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='V3Agent一键安装',
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
    uac_admin=True,
)
