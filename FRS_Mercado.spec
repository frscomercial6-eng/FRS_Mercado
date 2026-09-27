# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/assets', 'assets'), ('F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/version.txt', '.'), ('F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/EULA.txt', '.'), ('F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/updater_public_keys.json', '.'), ('F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/licensing/trusted_keys.json', 'licensing'), ('C:/Users/User/AppData/Local/Programs/Python/Python311/Lib/site-packages/customtkinter/assets', 'customtkinter/assets')]
binaries = []
hiddenimports = ['hashlib', 'uuid', 'encodings', 'codecs', 'importlib', 'importlib.util', 'pkgutil', 'zipimport', 'site', 'sysconfig', 'altgraph']
hiddenimports += collect_submodules('licensing')
hiddenimports += collect_submodules('encodings')
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('reportlab')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('googleapiclient')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('google.auth')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('httplib2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('requests')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('bcrypt')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cryptography')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('setuptools')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/main.py'],
    pathex=['C:/Users/User/AppData/Local/Programs/Python/Python311/DLLs', 'C:/Users/User/AppData/Local/Programs/Python/Python311/Lib', 'C:/Users/User/AppData/Local/Programs/Python/Python311/Lib/site-packages'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/_runtime_hook_error_logger.py'],
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
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='F:/PROGRAMA/FRS_MERCADO/FRS_MERCADO/_build_support/version_info.txt',
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
