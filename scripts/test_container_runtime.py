#!/usr/bin/env python3
"""Exercise the reduced final runtime without network access or extra tools."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from cpython_security import verify_runtime  # noqa: E402


def require(value: bool, message: str) -> None:
    if not value:
        raise RuntimeError(message)


def font_smoke(font: Path, text: str = "ÄÖÜäöüß € – E-Rechnung") -> dict[str, Any]:
    buffer = BytesIO()
    font_name = "RuntimeSmoke-" + font.stem
    pdfmetrics.registerFont(TTFont(font_name, str(font)))
    canvas = Canvas(buffer)
    canvas.setFont(font_name, 12)
    canvas.drawString(30, 800, text)
    canvas.save()
    extracted = "".join(page.extract_text() for page in PdfReader(BytesIO(buffer.getvalue())).pages)
    require(text in extracted, "Eingebettete Schrift erhält Unicode-Zeichen nicht.")
    # Decode and encode real images through Pillow's native zlib/JPEG libraries.
    for kind in ("PNG", "JPEG"):
        image = BytesIO()
        Image.new("RGB", (16, 16), "white").save(image, kind)
        image.seek(0)
        with Image.open(image) as decoded:
            decoded.load()
            require(decoded.size == (16, 16), "Bildbibliothek liefert falsche Abmessungen.")
    return {"unicode_preserved": True, "font": str(font), "image_formats": ["PNG", "JPEG"]}


def run_smoke() -> dict[str, Any]:
    import grp
    import pwd

    require(os.getuid() == 10001 and os.getgid() == 10001, "Runtime läuft mit falscher Benutzeridentität.")
    require(pwd.getpwuid(10001).pw_name == "appuser", "Benutzerdaten fehlen.")
    require(grp.getgrgid(10001).gr_name == "appuser", "Gruppendaten fehlen.")
    for tool in ("sh", "bash", "apt", "apt-get", "dpkg", "pip", "pip3", "gcc", "javac"):
        require(shutil.which(tool) is None, f"Build-/Administrationswerkzeug verblieben: {tool}")
    for module in ("pip", "ensurepip", "setuptools", "wheel"):
        require(importlib.util.find_spec(module) is None, f"Bootstrap-Paket verblieben: {module}")
    for module in ("ssl", "sqlite3", "bz2", "lzma", "ctypes", "readline", "lxml.etree", "uvloop", "PIL.Image"):
        importlib.import_module(module)
    context = ssl.create_default_context()
    require(context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname, "TLS-Prüfung ist abgeschaltet.")
    certificates = context.cert_store_stats()
    require(certificates["x509_ca"] > 50, "System-Truststore ist unvollständig.")
    addresses = socket.getaddrinfo("localhost", 8080)
    require(bool(addresses), "Lokale Namensauflösung fehlt.")
    berlin = ZoneInfo("Europe/Berlin")
    require(datetime(2026, 1, 1, tzinfo=berlin).utcoffset() == timedelta(hours=1), "Winterzeitzone fehlt.")
    require(datetime(2026, 7, 1, tzinfo=berlin).utcoffset() == timedelta(hours=2), "Sommerzeitzone fehlt.")
    with tempfile.TemporaryDirectory() as temporary:
        Path(temporary, "runtime-smoke.txt").write_text("synthetic", encoding="utf-8")
    java = Path("/usr/bin/java").resolve(strict=True)
    truststore = java.parent.parent / "lib/security/cacerts"
    require(truststore.resolve(strict=True).stat().st_size > 10000, "Java-Truststore fehlt.")
    keytool = java.with_name("keytool")
    result = subprocess.run(
        [str(keytool), "-list", "-cacerts", "-storepass", "changeit"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    require("trustedCertEntry" in result.stdout, "Java kann seinen Truststore nicht lesen.")
    font = PROJECT_ROOT / "app/assets/fonts/NotoSans-Regular.ttf"
    return {
        "passed": True,
        "cpython_security": verify_runtime(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "ca_certificates": certificates,
        "dns_localhost": True,
        "timezone_winter_summer": True,
        "java_truststore_readable": True,
        "rendering": font_smoke(font),
        "rendering_cjk": font_smoke(font.with_name("NotoSansSC-Variable.ttf"), "中文发票"),
        "build_tools_absent": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run_smoke()
    except (OSError, RuntimeError, ValueError, ImportError, subprocess.SubprocessError) as exc:
        report = {"passed": False, "error": str(exc)}
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
