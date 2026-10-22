# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\wilso\\webpart\\MTR\\mtr-db-migration\\consolidation\\migrate_app.py'],
    pathex=['C:\\Users\\wilso\\webpart\\MTR\\mtr-db-migration\\consolidation', 'C:\\Users\\wilso\\webpart\\MTR\\mtr-db-migration\\consolidation\\..\\metadata_horizontal'],
    binaries=[],
    datas=[('C:\\Users\\wilso\\webpart\\MTR\\mtr-db-migration\\consolidation\\sql', 'consolidation\\sql'), ('C:\\Users\\wilso\\webpart\\MTR\\mtr-db-migration\\consolidation\\sources.json', 'consolidation'), ('C:\\Users\\wilso\\webpart\\MTR\\mtr-db-migration\\consolidation\\build\\pgbin', 'pgbin')],
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
    name='MtrDbMigration',
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
)
