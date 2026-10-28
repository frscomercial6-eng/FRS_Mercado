# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/main.py'],
    pathex=[],
    binaries=[],
    datas=[('F:/PROGRAMA/FRS_MERCADO/.venv/Lib/site-packages/customtkinter/assets', 'customtkinter/assets'), ('F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/version.txt', '.'), ('F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/assets', 'assets')],
    hiddenimports=['modulo_pdv', 'modulo_login', 'modulo_main', 'modulo_relatorio', 'modulo_config', 'modulo_estoque', 'database_manager', 'app_paths', 'license_manager', 'updater_secure', 'error_notifier', 'customtkinter', 'PIL', 'reportlab', 'cryptography'],
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
    name='FRS_Mercado',
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
    version='F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/version_info',
    icon=['F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/assets/logo.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FRS_Mercado',
)
