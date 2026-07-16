# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import importlib.util

from PyInstaller.utils.hooks import collect_all
from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(__file__).resolve().parent


def _resolve_customtkinter_assets():
    spec = importlib.util.find_spec('customtkinter')
    if spec is None:
        return None

    origin = getattr(spec, 'origin', None)
    if not origin:
        return None

    assets_dir = Path(origin).resolve().parent / 'assets'
    if assets_dir.exists() and assets_dir.is_dir():
        return assets_dir
    return None


datas = [('assets', 'assets')]
customtkinter_assets = _resolve_customtkinter_assets()
if customtkinter_assets is not None:
    datas.append((str(customtkinter_assets), 'customtkinter/assets'))

binaries = []
hiddenimports = ['hashlib', 'uuid', 'encodings', 'codecs', 'importlib', 'importlib.util', 'pkgutil', 'zipimport', 'site', 'sysconfig', 'altgraph']
hiddenimports += collect_submodules('encodings')
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('reportlab')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('googleapiclient')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('google_auth_oauthlib')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('google.auth')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('httplib2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('requests')
a = Analysis(
    ['main.py'],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / '_runtime_hook_error_logger.py')],
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
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(ROOT / '_build_support' / 'version_info.txt'),
    icon=[str(ROOT / 'assets' / 'logo.ico')],
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
