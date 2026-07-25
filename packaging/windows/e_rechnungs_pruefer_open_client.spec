# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

PROJECT_ROOT = Path(SPECPATH).parents[1]
VERSION = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
VERSION_PARTS = tuple(int(part) for part in VERSION.split("."))
FILE_VERSION = (*VERSION_PARTS, *(0 for _ in range(4 - len(VERSION_PARTS))))[:4]

version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=FILE_VERSION,
        prodvers=FILE_VERSION,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040704B0",
                    [
                        StringStruct("CompanyName", "E-Rechnungs-Pruefer contributors"),
                        StringStruct("FileDescription", "E-Rechnungs-Prüfer öffnen"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", "E-Rechnungs-Pruefer-Oeffnen"),
                        StringStruct("LegalCopyright", "MIT License"),
                        StringStruct("OriginalFilename", "E-Rechnungs-Pruefer-Oeffnen.exe"),
                        StringStruct("ProductName", "E-Rechnungs-Prüfer"),
                        StringStruct("ProductVersion", VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [1031, 1200])]),
    ],
)

analysis = Analysis(
    [str(PROJECT_ROOT / "packaging" / "windows" / "open_client_entrypoint.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "ntsecuritycon",
        "pywintypes",
        "win32api",
        "win32con",
        "win32file",
        "win32net",
        "win32pipe",
        "win32security",
        "win32service",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["fastapi", "jinja2", "lxml", "pypdf", "pystray", "reportlab", "uvicorn"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="E-Rechnungs-Pruefer-Oeffnen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    version=version_info,
)
