# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hidden_imports = (
    collect_submodules("pyhanko")
    + collect_submodules("pyhanko_certvalidator")
    + collect_submodules("asn1crypto")
    + collect_submodules("oscrypto")
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("images", "images")],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Diviloper-PDF-Signer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="images/app_icon.ico",
)
