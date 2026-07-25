# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules
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

datas = [
    (str(PROJECT_ROOT / "app" / "templates"), "app/templates"),
    (str(PROJECT_ROOT / "app" / "static"), "app/static"),
    (str(PROJECT_ROOT / "app" / "examples"), "app/examples"),
    (str(PROJECT_ROOT / "app" / "assets" / "fonts"), "app/assets/fonts"),
]
for optional_source, destination in (
    (PROJECT_ROOT / "vendor" / "kosit", "vendor/kosit"),
    (PROJECT_ROOT / "runtime" / "java", "runtime/java"),
):
    if optional_source.is_dir():
        datas.append((str(optional_source), destination))

hidden_imports = sorted(
    set(
        collect_submodules("uvicorn")
        + [
            "ntsecuritycon",
            "pywintypes",
            "servicemanager",
            "win32api",
            "win32con",
            "win32event",
            "win32file",
            "win32net",
            "win32pipe",
            "win32security",
            "win32service",
            "win32serviceutil",
            "win32timezone",
        ]
    )
)

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
                        StringStruct("FileDescription", "E-Rechnungs-Prüfer Windows-Dienst"),
                        StringStruct("FileVersion", VERSION),
                        StringStruct("InternalName", "E-Rechnungs-Pruefer-Dienst"),
                        StringStruct("LegalCopyright", "MIT License"),
                        StringStruct("OriginalFilename", "E-Rechnungs-Pruefer-Dienst.exe"),
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
    [str(PROJECT_ROOT / "packaging" / "windows" / "service_entrypoint.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pystray"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="E-Rechnungs-Pruefer-Dienst",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch="x86_64",
    version=version_info,
)

bundle = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="E-Rechnungs-Pruefer-Dienst",
)
