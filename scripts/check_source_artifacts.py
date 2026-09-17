#!/usr/bin/env python3
"""Read-only source release inventory, archive-safety and isolated installed-wheel checks."""

from __future__ import annotations

import argparse
import ast
import base64
import csv
import email.parser
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unicodedata
import zipfile
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20_000
RUNTIME_REQUIRED = {
    "app/__init__.py",
    "app/main.py",
    "app/configuration.py",
    "app/http_upload.py",
    "app/report_templates.py",
    "app/server_runtime.py",
    "app/upload_ingress.py",
    "app/processing/__init__.py",
    *(
        f"app/processing/{name}.py"
        for name in (
            "bootstrap",
            "budgets",
            "kosit_runtime",
            "manager",
            "native",
            "operations",
            "posix",
            "protocol",
            "ready",
            "result",
            "supervisor",
            "watchdog",
            "windows",
            "worker",
        )
    ),
    "app/presentation_contract.json",
    "app/templates/index.html",
    "app/templates/report.html",
    "app/static/app.js",
    "app/static/styles.css",
    "app/examples/cii-rechnung-demo.xml",
    "app/examples/ubl-rechnung-demo.xml",
    "app/assets/fonts/NotoSans-Regular.ttf",
    "app/assets/fonts/NotoSans-Bold.ttf",
    "app/assets/fonts/NotoSans-Italic.ttf",
    "app/assets/fonts/NotoSans-BoldItalic.ttf",
    "app/assets/fonts/NotoSansSC-Variable.ttf",
    "app/assets/fonts/OFL-NotoSans.txt",
    "app/assets/fonts/OFL-NotoSansSC.txt",
}
LOCK_PROFILES = {
    "packaging/windows/requirements-release.txt": "windows-release",
    "packaging/python/requirements-source-release.txt": "source-release",
    "packaging/docker/requirements-linux-amd64.txt": "docker-amd64",
    "packaging/docker/requirements-linux-arm64.txt": "docker-arm64",
}
SOURCE_REQUIRED = {
    "VERSION",
    "pyproject.toml",
    "MANIFEST.in",
    "LICENSE",
    "README.md",
    "START_HERE.txt",
    "THIRD_PARTY.md",
    "packaging/kosit/components.lock.json",
    "packaging/windows/components.lock.json",
    "packaging/windows/requirements-build.txt",
    "packaging/windows/entrypoint.py",
    "packaging/windows/service_entrypoint.py",
    "packaging/windows/open_client_entrypoint.py",
    "packaging/windows/e_rechnungs_pruefer.spec",
    "packaging/windows/e_rechnungs_pruefer_service.spec",
    "packaging/windows/e_rechnungs_pruefer_open_client.spec",
    "packaging/windows/installer.iss",
    "packaging/windows/service_installer.iss",
    "scripts/bootstrap_dev.sh",
    "scripts/bootstrap_dev.ps1",
    "scripts/start.sh",
    "scripts/start.bat",
    "scripts/install_kosit.py",
    "scripts/prepare_windows_components.py",
    "scripts/install_inno_setup.ps1",
    "scripts/build_windows.ps1",
    "scripts/build_release.py",
    "scripts/processing_probe.py",
    "scripts/processing_smoke.py",
    "scripts/processing_lifecycle_probe.py",
    "scripts/test_processing_package.py",
    "scripts/test_processing_package.ps1",
    "scripts/check.sh",
    "scripts/check.ps1",
    "scripts/verify_version.py",
    "scripts/dependency_lock.py",
    "scripts/dependency_audit.py",
    "docs/examples/node-red-e-rechnungs-pruefer-flow.json",
    *LOCK_PROFILES,
    *(path + ".metadata.json" for path in LOCK_PROFILES),
}
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    "venv",
    "vendor",
    "runtime",
    "local-data",
    "reports",
    "uploads",
    ".cache",
    ".ssh",
    ".gnupg",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "htmlcov",
}
SENSITIVE_SUFFIXES = {".pdf", ".p12", ".pfx", ".pem", ".key", ".jks", ".keystore"}
DEVICE_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class ArtifactError(ValueError):
    """A release archive is incomplete, inconsistent or unsafe."""


def artifact_names(version: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ArtifactError("Ungültige Releaseversion.")
    return {
        "wheel": f"e_rechnung_pruefer-{version}-py3-none-any.whl",
        "sdist": f"e_rechnung_pruefer-{version}.tar.gz",
        "repository": f"E-Rechnungs-Pruefer-{version}-Codex-GitHub.zip",
        "checksums": f"E-Rechnungs-Pruefer-{version}-SHA256SUMS.txt",
    }


def _safe_name(raw: str) -> str:
    name = raw[:-1] if raw.endswith("/") else raw
    if not name or raw.startswith("/") or "\\" in raw or "//" in raw or ":" in raw:
        raise ArtifactError(f"Unsicherer Pfad im Archiv: {raw!r}")
    for part in name.split("/"):
        if (
            part in {".", ".."}
            or part.endswith((".", " "))
            or any(ord(c) < 32 or ord(c) == 127 for c in part)
            or part.split(".", 1)[0].casefold() in DEVICE_NAMES
        ):
            raise ArtifactError(f"Unsicherer Pfad im Archiv: {raw!r}")
    return name


def _policy(name: str) -> None:
    path = PurePosixPath(name.casefold())
    if (
        FORBIDDEN_PARTS.intersection(path.parts)
        or path.suffix in SENSITIVE_SUFFIXES
        or (path.name.startswith(".env") and path.name != ".env.example")
        or path.name in {".coverage", "coverage.xml", ".ds_store", "thumbs.db"}
    ):
        raise ArtifactError(f"Unzulässiger privater oder generierter Archivinhalt: {name}")
    if path.suffix == ".xml" and not any(
        root in path.parents for root in (PurePosixPath("app/examples"), PurePosixPath("tests/fixtures"))
    ):
        raise ArtifactError(f"Unzulässiger XML-Pfad außerhalb freigegebener Beispiele/Fixtures: {name}")


def _regular(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise ArtifactError(f"Artefakt ist keine reguläre Datei oder ein Link: {path.name}")


def read_archive(path: Path, prefix: str = "") -> dict[str, bytes]:
    _regular(path)
    files: dict[str, bytes] = {}
    seen: dict[str, bool] = {}
    total = 0

    def register(raw: str, directory: bool, size: int) -> str | None:
        nonlocal total
        name = _safe_name(raw)
        if prefix:
            if name == prefix and directory:
                return None
            if not name.startswith(prefix + "/"):
                raise ArtifactError("Archivpfad liegt außerhalb des erwarteten Versionsstamms.")
            name = name[len(prefix) + 1 :]
        _policy(name)
        key = unicodedata.normalize("NFC", name).casefold()
        if key in seen or any(seen.get(parent.as_posix()) is False for parent in PurePosixPath(key).parents):
            raise ArtifactError(f"Doppelter oder kollidierender Archivpfad: {name}")
        if not directory and any(existing.startswith(key + "/") for existing in seen):
            raise ArtifactError(f"Kollidierender Datei-/Verzeichnispfad: {name}")
        seen[key] = directory
        total += size
        if size < 0 or size > MAX_FILE_BYTES or total > MAX_ARCHIVE_BYTES or len(seen) > MAX_MEMBERS:
            raise ArtifactError("Archiv überschreitet die Prüfgrößenbegrenzung.")
        return None if directory else name

    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if not (member.isfile() or member.isdir()):
                    raise ArtifactError("Tar-Link oder Sonderdatei ist nicht erlaubt.")
                name = register(member.name, member.isdir(), member.size)
                if name is not None:
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ArtifactError("Tar-Datei kann nicht gelesen werden.")
                    with stream:
                        content = stream.read(MAX_FILE_BYTES + 1)
                    if len(content) != member.size:
                        raise ArtifactError("Tar-Inhalt widerspricht der Größenangabe.")
                    files[name] = content
    else:
        with zipfile.ZipFile(path) as zip_archive:
            for zip_member in zip_archive.infolist():
                mode = zip_member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or zip_member.flag_bits & 1:
                    raise ArtifactError("ZIP-Link, Sonderdatei oder Verschlüsselung ist nicht erlaubt.")
                name = register(zip_member.filename, zip_member.is_dir(), zip_member.file_size)
                if name is not None:
                    with zip_archive.open(zip_member) as zip_stream:
                        content = zip_stream.read(MAX_FILE_BYTES + 1)
                    if len(content) != zip_member.file_size:
                        raise ArtifactError("ZIP-Inhalt widerspricht der Größenangabe.")
                    files[name] = content
    if not files:
        raise ArtifactError("Archiv enthält keine Dateien.")
    return files


def _require(files: dict[str, bytes], expected: set[str]) -> None:
    missing = expected - files.keys()
    if missing:
        raise ArtifactError(f"Erforderlicher Archivinhalt fehlt: {', '.join(sorted(missing))}")
    empty = [name for name in expected if not files[name]]
    if empty:
        raise ArtifactError(f"Erforderlicher Archivinhalt ist leer: {', '.join(sorted(empty))}")


def _metadata(content: bytes, version: str) -> None:
    metadata = email.parser.BytesParser().parsebytes(content)
    if metadata.get_all("Name") != ["e-rechnung-pruefer"] or metadata.get_all("Version") != [version]:
        raise ArtifactError("Paketname oder Metadata-Version stimmt nicht mit dem Release überein.")


def _app_version(files: dict[str, bytes], version: str) -> None:
    tree = ast.parse(files["app/__init__.py"])
    found = [
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
        and isinstance(node.value, ast.Constant)
    ]
    if found != [version]:
        raise ArtifactError("App-Version stimmt nicht mit dem Release überein.")


def _wheel(files: dict[str, bytes], version: str) -> None:
    info = f"e_rechnung_pruefer-{version}.dist-info"
    _require(files, {f"{info}/METADATA", f"{info}/WHEEL", f"{info}/RECORD"})
    if any(not name.startswith(("app/", info + "/")) for name in files):
        raise ArtifactError("Wheel enthält Dateien außerhalb der Anwendung und ihrer eigenen Metadaten.")
    _metadata(files[f"{info}/METADATA"], version)
    wheel = email.parser.BytesParser().parsebytes(files[f"{info}/WHEEL"])
    if (
        wheel.get_all("Wheel-Version") != ["1.0"]
        or wheel.get_all("Root-Is-Purelib") != ["true"]
        or wheel.get_all("Tag") != ["py3-none-any"]
    ):
        raise ArtifactError("Wheel-Format oder Plattformtag widerspricht dem reinen Python-Artefakt.")
    record_path = f"{info}/RECORD"
    recorded = set()
    for row in csv.reader(io.StringIO(files[record_path].decode("utf-8"), newline="")):
        if len(row) != 3 or row[0] not in files or row[0] in recorded:
            raise ArtifactError("Wheel-RECORD ist unvollständig oder mehrdeutig.")
        name, encoded_hash, size = row
        recorded.add(name)
        if name == record_path:
            valid = encoded_hash == size == ""
        else:
            expected_hash = "sha256=" + base64.urlsafe_b64encode(sha256(files[name]).digest()).rstrip(b"=").decode()
            valid = encoded_hash == expected_hash and size == str(len(files[name]))
        if not valid:
            raise ArtifactError(f"Wheel-RECORD stimmt nicht mit den enthaltenen Bytes überein: {name}")
    if recorded != files.keys():
        raise ArtifactError("Wheel-RECORD erfasst nicht sämtliche enthaltenen Dateien.")


def _source(files: dict[str, bytes], version: str) -> None:
    _require(files, SOURCE_REQUIRED)
    project = tomllib.loads(files["pyproject.toml"].decode("utf-8"))["project"]
    if (
        files["VERSION"].decode("utf-8").strip() != version
        or project.get("version") != version
        or project.get("name") != "e-rechnung-pruefer"
    ):
        raise ArtifactError("Source-Version oder Projektidentität widerspricht dem Release.")
    for path, profile in LOCK_PROFILES.items():
        metadata = json.loads(files[path + ".metadata.json"])
        if (
            metadata.get("schema_version") != 1
            or metadata.get("profile") != profile
            or metadata.get("lock_sha256") != sha256(files[path]).hexdigest()
        ):
            raise ArtifactError(f"Lock und Sidecar sind im Archiv nicht gebunden: {path}")


def check_artifacts(dist: Path, version: str) -> dict[str, Any]:
    names = artifact_names(version)
    if dist.is_symlink() or not dist.is_dir() or {path.name for path in dist.iterdir()} != set(names.values()):
        raise ArtifactError("Artefaktmenge muss exakt Wheel, sdist, Repository-ZIP und SHA256SUMS enthalten.")
    digests = {}
    for name in names.values():
        _regular(dist / name)
        digests[name] = sha256((dist / name).read_bytes()).hexdigest()
    recorded: dict[str, str] = {}
    for line in (dist / names["checksums"]).read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([^/\\]+)", line)
        if match is None or match[2] in recorded:
            raise ArtifactError("Ungültiges oder doppeltes SHA-256-Manifest.")
        recorded[match[2]] = match[1]
    expected = {name: digest for name, digest in digests.items() if name != names["checksums"]}
    if recorded != expected:
        raise ArtifactError("SHA-256-Manifest bindet nicht exakt die drei unveränderten Archive.")
    archives = {
        "wheel": read_archive(dist / names["wheel"]),
        "sdist": read_archive(dist / names["sdist"], f"e_rechnung_pruefer-{version}"),
        "repository": read_archive(dist / names["repository"], f"E-Rechnungs-Pruefer-{version}"),
    }
    for files in archives.values():
        _require(files, RUNTIME_REQUIRED)
        _app_version(files, version)
    _wheel(archives["wheel"], version)
    _require(archives["sdist"], {"PKG-INFO"})
    for name, content in archives["sdist"].items():
        if name == "PKG-INFO" or name.endswith(".egg-info/PKG-INFO"):
            _metadata(content, version)
    for kind in ("sdist", "repository"):
        _source(archives[kind], version)
    app_files = {name: content for name, content in archives["wheel"].items() if name.startswith("app/")}
    for kind in ("sdist", "repository"):
        if {name: content for name, content in archives[kind].items() if name.startswith("app/")} != app_files:
            raise ArtifactError(f"Laufzeitdateien in {kind} und Wheel stimmen nicht bytegenau überein.")
    for path in SOURCE_REQUIRED:
        if archives["sdist"][path] != archives["repository"][path]:
            raise ArtifactError(f"Source-Dateien unterscheiden sich zwischen Archiven: {path}")
    return {
        "version": version,
        "artifacts": digests,
        "archive_file_counts": {kind: len(files) for kind, files in archives.items()},
    }


SMOKE_CODE = r"""
import collections, hashlib, importlib.metadata, importlib.resources, json, os, pathlib, sys
version, source_root = sys.argv[1:]
expected = json.load(sys.stdin)
import app
distribution = importlib.metadata.distribution("e-rechnung-pruefer")
root = pathlib.Path(source_root).resolve()
loaded = pathlib.Path(app.__file__).resolve()
assert not loaded.is_relative_to(root), "Source checkout imported instead of installed wheel"
assert loaded == pathlib.Path(distribution.locate_file("app/__init__.py")).resolve(), "Wrong app distribution"
assert app.__version__ == distribution.version == version, "Installed wheel version mismatch"
origin = distribution.read_text("direct_url.json")
assert not origin or not json.loads(origin).get("dir_info", {}).get("editable"), "Editable project is not a wheel smoke"
for relative, digest in expected.items():
    installed = pathlib.Path(distribution.locate_file(relative)).resolve()
    assert installed.is_relative_to(loaded.parent) and not installed.is_relative_to(root), "Unbound installed path"
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == digest, "Installed bytes differ from candidate wheel: " + relative
from fastapi.testclient import TestClient
from app.main import app as api
from app.processing import manager as controller
native_children = []
real_spawn = controller.spawn_role
def recorded_spawn(role, **kwargs):
    child = real_spawn(role, **kwargs)
    assert child.pid != os.getpid(), "Processing role ran in the HTTP parent"
    record = {"role": role, "pid": child.pid, "reaped": False}
    native_children.append(record)
    real_wait = child.process.wait
    def recorded_wait(*args, **options):
        result = real_wait(*args, **options)
        record["reaped"] = True
        record["returncode"] = result
        return result
    child.process.wait = recorded_wait
    return child
controller.spawn_role = recorded_spawn
syntax = []
with TestClient(api) as client:
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json()["version"] == version
    for expected_syntax, filename in (("CII", "cii-rechnung-demo.xml"), ("UBL", "ubl-rechnung-demo.xml")):
        payload = importlib.resources.files("app").joinpath("examples", filename).read_bytes()
        request = {"files": {"file": (filename, payload, "application/xml")}, "data": {"official": "false"}}
        analysis = client.post("/api/analyze", **request)
        assert analysis.status_code == 200, analysis.text
        body = analysis.json()
        assert body["schema_version"] == 2 and body["capabilities"]["syntax"] == expected_syntax
        assert body["assessment"]["official"]["status"] == "not-requested"
        for endpoint, media in (("/api/report", "text/html"), ("/api/report/pdf", "application/pdf")):
            response = client.post(endpoint, **request)
            assert response.status_code == 200, response.text
            assert response.headers["content-type"].startswith(media)
            assert response.headers["x-einvoice-analysis-schema"] == "2"
            assert response.headers["x-einvoice-syntax"] == expected_syntax
            assert response.content and (media != "application/pdf" or response.content.startswith(b"%PDF-"))
        exported = client.post("/api/xml", files=request["files"])
        assert exported.status_code == 200 and exported.content == payload, "Original XML was changed"
        syntax.append(expected_syntax)
role_counts = dict(collections.Counter(record["role"] for record in native_children))
expected_roles = {"supervisor": 8, "worker": 8}
if sys.platform == "darwin":
    expected_roles["watchdog"] = 8
assert role_counts == expected_roles, "Every HTTP operation must start its actual native roles"
assert all(record["reaped"] for record in native_children), "Native role exit was not confirmed"
assert controller.manager.active_count == 0, "Processing lease was not released after response cleanup"
print(json.dumps({"version": version, "syntax": syntax, "installed_app": str(loaded), "verified_app_files": len(expected),
    "native_processing": {"jobs": 8, "role_counts": role_counts, "all_reaped": True, "leases_remaining": 0,
                          "children": native_children}}))
"""


def wheel_smoke(python: str, wheel: Path, version: str) -> dict[str, Any]:
    files = read_archive(wheel)
    _require(files, RUNTIME_REQUIRED)
    _wheel(files, version)
    expected = {name: sha256(content).hexdigest() for name, content in files.items() if name.startswith("app/")}
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("EINVOICE_", "KOSIT_", "MAX_", "PYTHON")) and key not in {"HOST", "PORT"}
    }
    env["KOSIT_ENABLED"] = "false"
    with tempfile.TemporaryDirectory(prefix="einvoice-wheel-smoke-") as temporary:
        result = subprocess.run(
            [python, "-I", "-c", SMOKE_CODE, version, str(PROJECT_ROOT)],
            cwd=temporary,
            env=env,
            input=json.dumps(expected),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    if result.returncode != 0:
        raise ArtifactError(
            f"Isolierter Wheel-Smoke fehlgeschlagen (Exitcode {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    receipt = json.loads(result.stdout)
    if receipt.get("version") != version or receipt.get("syntax") != ["CII", "UBL"]:
        raise ArtifactError("Wheel-Smoke lieferte keinen vollständigen Nachweis.")
    proof = receipt.get("native_processing")
    if (
        not isinstance(proof, dict)
        or proof.get("jobs") != 8
        or proof.get("all_reaped") is not True
        or proof.get("leases_remaining") != 0
        or proof.get("role_counts")
        not in ({"supervisor": 8, "worker": 8}, {"supervisor": 8, "worker": 8, "watchdog": 8})
    ):
        raise ArtifactError("Wheel-Smoke lieferte keinen vollständigen nativen Prozessnachweis.")
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=PROJECT_ROOT / "dist")
    parser.add_argument("--version")
    parser.add_argument("--wheel-smoke-python")
    args = parser.parse_args(argv)
    try:
        version = args.version or (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        result = check_artifacts(args.dist, version)
        if args.wheel_smoke_python:
            result["wheel_smoke"] = wheel_smoke(
                args.wheel_smoke_python, args.dist / artifact_names(version)["wheel"], version
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        ArtifactError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        SyntaxError,
        zipfile.BadZipFile,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Source-Artefaktprüfung fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
