"""Tests for the shared component lock helpers, the installer and the Windows preparation.

All downloads are replaced by copies from a synthetic local "upstream" so the tests
run offline and never touch ``vendor/`` of the repository.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMMIT = "0123456789abcdef0123456789abcdef01234567"
BASE_URL = f"https://raw.githubusercontent.com/example/rules/{COMMIT}/"


def _load_script(name: str, module_name: str):
    path = PROJECT_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve postponed annotations through sys.modules.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


shared = _load_script("kosit_components.py", "kosit_components_under_test")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fileset(files: dict[str, bytes]) -> dict[str, Any]:
    return {
        "kind": "fileset",
        "version": "Factur-X 1.09.2 EXTENDED (Test)",
        "source": "https://github.com/example/rules",
        "tag": "v1.0.0",
        "commit": COMMIT,
        "base_url": BASE_URL,
        "files": [
            {"path": f"upstream/{target}", "target": target, "size": len(data), "sha256": _sha256(data)}
            for target, data in files.items()
        ],
    }


EXTENDED_FILES = {
    "resources/xsd/Factur-X_EXTENDED.xsd": b"<xs:schema/>",
    "resources/xslt/FACTUR-X_EXTENDED.xslt": b"<xsl:stylesheet/>",
    "resources/xslt/FACTUR-X_EXTENDED_codedb.xml": b"<codedb/>",
    "resources/schematron/FACTUR-X_EXTENDED.sch": b"<schema/>",
    "LICENSE": b"Apache License\n",
}


def _jar_bytes(main_class: str | None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        manifest = "Manifest-Version: 1.0\n"
        if main_class:
            manifest += f"Main-Class: {main_class}\n"
        archive.writestr("META-INF/MANIFEST.MF", manifest)
    return buffer.getvalue()


def _config_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("scenarios.xml", "<scenarios/>")
        archive.writestr("resources/default-report.xsl", "<xsl:stylesheet/>")
        archive.writestr("resources/xrechnung-report.xsl", "<xsl:stylesheet/>")
    return buffer.getvalue()


def _java_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("jdk-21-jre/bin/java.exe", b"MZ test")
        archive.writestr("jdk-21-jre/release", "JAVA_VERSION=21")
    return buffer.getvalue()


def _artifact(filename: str, data: bytes) -> dict[str, str]:
    return {
        "version": filename,
        "filename": filename,
        "url": f"https://example.test/{filename}",
        "sha256": _sha256(data),
    }


def _write_upstream(tmp_path: Path, *, with_java: bool = False) -> tuple[Path, dict[str, bytes]]:
    """Create a lock file and the bytes every URL in it should return."""

    jar = _jar_bytes("de.kosit.validationtool.cmd.CommandLineApplication")
    config = _config_zip_bytes()
    components: dict[str, Any] = {
        "validator": _artifact("validator-9.9.9-standalone.jar", jar),
        "xrechnung": _artifact("xrechnung-config.zip", config),
        "facturx_extended": _fileset(EXTENDED_FILES),
    }
    urls = {
        components["validator"]["url"]: jar,
        components["xrechnung"]["url"]: config,
        **{BASE_URL + f"upstream/{target}": data for target, data in EXTENDED_FILES.items()},
    }
    if with_java:
        java = _java_zip_bytes()
        components["java"] = _artifact("java.zip", java)
        urls[components["java"]["url"]] = java
    lock = {
        "schema_version": 2,
        "components": components,
        "standards": {
            "xrechnung": "3.0.2",
            "xrechnung_configuration": "2026-08-31",
            "cen_en16931": "1.3.16",
            "xrechnung_schematron": "2.6.0",
            "facturx_extended": "1.09.2",
            "facturx_extended_source": "example/rules v1.0.0 0123456",
        },
    }
    lock_path = tmp_path / "components.lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return lock_path, urls


def _downloader(urls: dict[str, bytes], *, corrupt: str | None = None) -> Callable[[str, Path], None]:
    def download(url: str, target: Path) -> None:
        if url not in urls:
            raise AssertionError(f"unerwarteter Download: {url}")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = urls[url]
        if corrupt is not None and url.endswith(corrupt):
            # Same length, different bytes: exercises the hash check, not the size check.
            payload = payload[::-1]
        target.write_bytes(payload)

    return download


# --- shared lock validation -------------------------------------------------


def test_fileset_validation_accepts_the_repository_locks() -> None:
    for relative, required in (
        ("packaging/kosit/components.lock.json", ("validator", "xrechnung", "facturx_extended")),
        ("packaging/windows/components.lock.json", ("java", "validator", "xrechnung", "facturx_extended")),
    ):
        payload = shared.load_lock(PROJECT_ROOT / relative, required=required)
        extended = payload["components"]["facturx_extended"]
        assert extended["base_url"] == f"https://raw.githubusercontent.com/fnfempe/France_RFE/{extended['commit']}/"
        assert all(
            entry["path"].startswith(("FNFE_RFE_INVOICE/Factur-X/EXTENDED/", "LICENSE")) for entry in extended["files"]
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"kind": "artifact"}, "Art 'fileset'"),
        ({"source": "http://github.com/example/rules"}, "Unsichere Quelle"),
        ({"commit": "abc123"}, "40-stelligen Commit"),
        ({"base_url": "https://raw.githubusercontent.com/example/rules/main/"}, "Branches oder Tags"),
        ({"base_url": f"https://raw.githubusercontent.com/example/rules/{'f' * 40}/"}, "Branches oder Tags"),
        ({"base_url": f"http://raw.githubusercontent.com/example/rules/{COMMIT}/"}, "Branches oder Tags"),
        ({"files": []}, "keine Dateiliste"),
    ],
)
def test_fileset_validation_rejects_unsafe_component_fields(mutation: dict[str, Any], message: str) -> None:
    component = _fileset(EXTENDED_FILES)
    component.update(mutation)
    with pytest.raises(shared.ComponentLockError, match=message):
        shared.validate_fileset("facturx_extended", component)


@pytest.mark.parametrize(
    ("entry_mutation", "message"),
    [
        ({"target": "/etc/passwd"}, "Unsicherer Pfad"),
        ({"target": "../ausbruch.xsd"}, "Unsicherer Pfad"),
        ({"target": "resources/../../ausbruch.xsd"}, "Unsicherer Pfad"),
        ({"target": "C:/Windows/system32/x.xsd"}, "Unsicherer Pfad"),
        ({"target": "resources\\xsd\\x.xsd"}, "Unsicherer Pfad"),
        ({"target": "resources/con.xsd"}, "Unsicherer Pfad"),
        ({"target": "resources/xsd/"}, "Unsicherer Pfad"),
        ({"path": "/absolute/upstream.xsd"}, "Unsicherer Pfad"),
        ({"path": "upstream/../x.xsd"}, "Unsicherer Pfad"),
        ({"size": 0}, "Ungültige Größe"),
        ({"size": "1629"}, "Ungültige Größe"),
        ({"size": True}, "Ungültige Größe"),
        ({"sha256": "abc"}, "Ungültige SHA-256"),
    ],
)
def test_fileset_validation_rejects_unsafe_file_entries(entry_mutation: dict[str, Any], message: str) -> None:
    component = _fileset(EXTENDED_FILES)
    component["files"][0].update(entry_mutation)
    with pytest.raises(shared.ComponentLockError, match=message):
        shared.validate_fileset("facturx_extended", component)


def test_fileset_validation_rejects_targets_that_collide_on_windows() -> None:
    component = _fileset(EXTENDED_FILES)
    component["files"][1]["target"] = "resources/xsd/factur-x_extended.XSD"
    with pytest.raises(shared.ComponentLockError, match="Doppeltes Ziel"):
        shared.validate_fileset("facturx_extended", component)


def test_load_lock_requires_every_component_and_known_kinds(tmp_path: Path) -> None:
    lock_path, _urls = _write_upstream(tmp_path)
    with pytest.raises(shared.ComponentLockError, match="Komponente java fehlt"):
        shared.load_lock(lock_path, required=("java",))
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload["components"]["validator"]["kind"] = "tarball"
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(shared.ComponentLockError, match="Unbekannte Komponentenart"):
        shared.load_lock(lock_path, required=("validator",))


def test_fetch_fileset_verifies_size_and_hash_and_reuses_a_valid_cache(tmp_path: Path) -> None:
    lock_path, urls = _write_upstream(tmp_path)
    component = json.loads(lock_path.read_text(encoding="utf-8"))["components"]["facturx_extended"]
    cache = tmp_path / "cache"
    calls: list[str] = []

    def counting(url: str, target: Path) -> None:
        calls.append(url)
        _downloader(urls)(url, target)

    first = shared.fetch_fileset(component, tmp_path / "one", user_agent="t", cache_dir=cache, downloader=counting)
    second = shared.fetch_fileset(component, tmp_path / "two", user_agent="t", cache_dir=cache, downloader=counting)
    assert first == second
    assert len(calls) == len(EXTENDED_FILES)
    assert (tmp_path / "two" / "LICENSE").read_bytes() == b"Apache License\n"

    (cache / "facturx-extended" / "LICENSE").write_bytes(b"veraltet")
    shared.fetch_fileset(component, tmp_path / "three", user_agent="t", cache_dir=cache, downloader=counting)
    assert len(calls) == len(EXTENDED_FILES) + 1

    with pytest.raises(shared.ComponentLockError, match="SHA-256"):
        shared.fetch_fileset(
            component, tmp_path / "four", user_agent="t", downloader=_downloader(urls, corrupt="LICENSE")
        )
    assert not (tmp_path / "four" / "LICENSE").exists()


def test_directory_replacement_restores_the_previous_tree_on_rollback(tmp_path: Path) -> None:
    target = tmp_path / "vendor" / "kosit"
    (target / "alt").mkdir(parents=True)
    (target / "alt" / "marker.txt").write_text("alt", encoding="utf-8")
    staged = tmp_path / "stage"
    (staged / "neu").mkdir(parents=True)

    replacement = shared.DirectoryReplacement(target, staged)
    replacement.apply()
    assert (target / "neu").is_dir()
    assert replacement.backup is not None and (replacement.backup / "alt" / "marker.txt").is_file()

    replacement.rollback()
    assert (target / "alt" / "marker.txt").read_text(encoding="utf-8") == "alt"
    assert not list(tmp_path.joinpath("vendor").glob("kosit.previous-*"))

    replacement = shared.DirectoryReplacement(target, tmp_path / "stage2")
    (tmp_path / "stage2").mkdir()
    replacement.apply()
    replacement.finish()
    assert not list(tmp_path.joinpath("vendor").glob("kosit.previous-*"))
    assert not (target / "alt").exists()


# --- install_kosit.py -------------------------------------------------------


@pytest.fixture()
def installer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_script("install_kosit.py", "install_kosit_under_test")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module, "VENDOR_ROOT", tmp_path / "vendor" / "kosit")
    return module


def test_installer_assembles_extended_repository_and_env(
    installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, urls = _write_upstream(tmp_path)
    monkeypatch.setattr(installer, "download", _downloader(urls))

    installer.install(False, lock_file=lock_path)

    vendor = tmp_path / "vendor" / "kosit"
    extended = vendor / "facturx-extended"
    assert (vendor / "validator" / "validator-9.9.9-standalone.jar").is_file()
    assert (vendor / "xrechnung" / "scenarios.xml").is_file()
    for target in EXTENDED_FILES:
        assert (extended / target).read_bytes() == EXTENDED_FILES[target]
    assert (extended / "scenarios.xml").read_bytes() == installer.EXTENDED_SCENARIOS_SOURCE.read_bytes()
    assert (extended / "resources" / "report" / "default-report.xsl").read_text(encoding="utf-8") == "<xsl:stylesheet/>"
    note = (extended / "SOURCE.txt").read_text(encoding="utf-8")
    assert f"Commit: {COMMIT}" in note and "resources/report/default-report.xsl" in note and "scenarios.xml" in note
    env = (tmp_path / ".env.kosit").read_text(encoding="utf-8")
    assert "KOSIT_VALIDATOR_JAR=vendor/kosit/validator/validator-9.9.9-standalone.jar" in env
    assert "KOSIT_SCENARIOS=vendor/kosit/xrechnung/scenarios.xml" in env
    assert "KOSIT_FACTURX_EXTENDED_SCENARIOS=vendor/kosit/facturx-extended/scenarios.xml" in env
    assert "KOSIT_FACTURX_EXTENDED_REPOSITORIES=vendor/kosit/facturx-extended" in env
    assert not (tmp_path / ".env.kosit.tmp").exists()
    assert not list((tmp_path / "vendor").glob("kosit.previous-*"))

    with pytest.raises(installer.InstallError, match="--force"):
        installer.install(False, lock_file=lock_path)


def test_installer_leaves_installation_untouched_when_a_download_is_corrupt(
    installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, urls = _write_upstream(tmp_path)
    vendor = tmp_path / "vendor" / "kosit"
    (vendor / "validator").mkdir(parents=True)
    (vendor / "validator" / "marker.txt").write_text("bisherige Installation", encoding="utf-8")
    (tmp_path / ".env.kosit").write_text("ALT\n", encoding="utf-8")
    monkeypatch.setattr(installer, "download", _downloader(urls, corrupt="FACTUR-X_EXTENDED.xslt"))

    with pytest.raises(installer.InstallError, match="SHA-256"):
        installer.install(True, lock_file=lock_path)

    assert (vendor / "validator" / "marker.txt").read_text(encoding="utf-8") == "bisherige Installation"
    assert not (vendor / "facturx-extended").exists()
    assert (tmp_path / ".env.kosit").read_text(encoding="utf-8") == "ALT\n"
    assert not list((tmp_path / "vendor").glob("kosit.previous-*"))


def test_installer_restores_previous_installation_when_the_replacement_fails(
    installer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, urls = _write_upstream(tmp_path)
    vendor = tmp_path / "vendor" / "kosit"
    (vendor / "validator").mkdir(parents=True)
    (vendor / "validator" / "marker.txt").write_text("bisherige Installation", encoding="utf-8")
    (tmp_path / ".env.kosit").write_text("ALT\n", encoding="utf-8")
    monkeypatch.setattr(installer, "download", _downloader(urls))

    def failing_write_env(*_args: Any, **_kwargs: Any) -> Path:
        raise OSError("Datenträger voll")

    monkeypatch.setattr(installer, "write_env", failing_write_env)

    with pytest.raises(installer.InstallError, match="zurückgenommen"):
        installer.install(True, lock_file=lock_path)

    assert (vendor / "validator" / "marker.txt").read_text(encoding="utf-8") == "bisherige Installation"
    assert not (vendor / "facturx-extended").exists()
    assert (tmp_path / ".env.kosit").read_text(encoding="utf-8") == "ALT\n"
    assert not list((tmp_path / "vendor").glob("kosit.previous-*"))


def test_installer_requires_the_extended_component_and_standards(installer, tmp_path: Path) -> None:
    lock_path, _urls = _write_upstream(tmp_path)
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    del payload["components"]["facturx_extended"]
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(installer.InstallError, match="facturx_extended fehlt"):
        installer.load_lock(lock_path)

    lock_path, _urls = _write_upstream(tmp_path)
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    del payload["standards"]["facturx_extended_source"]
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(installer.InstallError, match="Sperrdatei"):
        installer.load_lock(lock_path)


# --- prepare_windows_components.py ------------------------------------------


@pytest.fixture()
def windows_preparer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = _load_script("prepare_windows_components.py", "prepare_windows_components_under_test")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    return module


def _patch_windows_downloads(module, urls: dict[str, bytes], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def download_locked(component: dict[str, Any], cache_dir: Path) -> Path:
        target = cache_dir / component["filename"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(urls[component["url"]])
        return target

    monkeypatch.setattr(module, "_download_locked", download_locked)
    monkeypatch.setattr(module, "_download_file", _downloader(urls))


def test_windows_preparation_assembles_both_trees(
    windows_preparer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, urls = _write_upstream(tmp_path, with_java=True)
    _patch_windows_downloads(windows_preparer, urls, monkeypatch, tmp_path)

    windows_preparer.prepare(lock_path, tmp_path / "cache")

    assert (tmp_path / "runtime" / "java" / "bin" / "java.exe").is_file()
    kosit = tmp_path / "vendor" / "kosit"
    assert (kosit / "validator" / "validator-9.9.9-standalone.jar").is_file()
    assert (kosit / "xrechnung" / "scenarios.xml").is_file()
    for target in EXTENDED_FILES:
        assert (kosit / "facturx-extended" / target).read_bytes() == EXTENDED_FILES[target]
    assert (kosit / "facturx-extended" / "scenarios.xml").is_file()
    assert (kosit / "facturx-extended" / "resources" / "report" / "default-report.xsl").is_file()
    assert (tmp_path / "cache" / "facturx-extended" / "LICENSE").is_file()
    assert not list((tmp_path / "vendor").glob("kosit.previous-*"))
    assert not list((tmp_path / "runtime").glob("java.previous-*"))


def test_windows_preparation_restores_both_trees_when_the_second_replacement_fails(
    windows_preparer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path, urls = _write_upstream(tmp_path, with_java=True)
    _patch_windows_downloads(windows_preparer, urls, monkeypatch, tmp_path)
    (tmp_path / "runtime" / "java" / "bin").mkdir(parents=True)
    (tmp_path / "runtime" / "java" / "bin" / "java.exe").write_bytes(b"alt")
    (tmp_path / "vendor" / "kosit" / "validator").mkdir(parents=True)
    (tmp_path / "vendor" / "kosit" / "validator" / "marker.txt").write_text("alt", encoding="utf-8")

    original_apply = windows_preparer.shared.DirectoryReplacement.apply

    def failing_apply(self) -> None:
        if self.target.name == "kosit":
            raise OSError("Zugriff verweigert")
        original_apply(self)

    monkeypatch.setattr(windows_preparer.shared.DirectoryReplacement, "apply", failing_apply)

    with pytest.raises(windows_preparer.ComponentError, match="zurückgenommen"):
        windows_preparer.prepare(lock_path, tmp_path / "cache")

    assert (tmp_path / "runtime" / "java" / "bin" / "java.exe").read_bytes() == b"alt"
    assert (tmp_path / "vendor" / "kosit" / "validator" / "marker.txt").read_text(encoding="utf-8") == "alt"
    assert not (tmp_path / "vendor" / "kosit" / "facturx-extended").exists()
    assert not list((tmp_path / "vendor").glob("kosit.previous-*"))
    assert not list((tmp_path / "runtime").glob("java.previous-*"))
    shutil.rmtree(tmp_path / "cache", ignore_errors=True)
