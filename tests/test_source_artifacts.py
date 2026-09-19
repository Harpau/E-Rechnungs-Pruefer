from __future__ import annotations

import base64
import csv
import importlib.util
import io
import json
import stat
import subprocess
import tarfile
import zipfile
from hashlib import sha256
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_source_artifacts.py"
VERSION = "0.0.0"


@pytest.fixture
def module():
    spec = importlib.util.spec_from_file_location("source_artifacts_under_test", SCRIPT)
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def sample_contents(module):
    runtime = {name: b"synthetic resource" for name in module.RUNTIME_REQUIRED}
    runtime["app/__init__.py"] = f'__version__ = "{VERSION}"\n'.encode()
    source = runtime | {name: b"synthetic source" for name in module.SOURCE_REQUIRED}
    source["VERSION"] = (VERSION + "\n").encode()
    source["pyproject.toml"] = f'[project]\nname="e-rechnung-pruefer"\nversion="{VERSION}"\n'.encode()
    for path, profile in module.LOCK_PROFILES.items():
        source[path] = b"synthetic==1.0 --hash=sha256:" + b"a" * 64 + b"\n"
        source[path + ".metadata.json"] = json.dumps(
            {"schema_version": 1, "profile": profile, "lock_sha256": sha256(source[path]).hexdigest()}
        ).encode()
    return runtime, source


def write_release(module, tmp_path, change=None):
    runtime, source = sample_contents(module)
    info = f"Metadata-Version: 2.4\nName: e-rechnung-pruefer\nVersion: {VERSION}\n".encode()
    wheel_files = runtime | {f"e_rechnung_pruefer-{VERSION}.dist-info/METADATA": info}
    wheel_files[f"e_rechnung_pruefer-{VERSION}.dist-info/WHEEL"] = (
        b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    sdist_files = source | {"PKG-INFO": info}
    repo_files = dict(source)
    groups = {"wheel": wheel_files, "sdist": sdist_files, "repository": repo_files}
    if change:
        change(groups)
    record_name = f"e_rechnung_pruefer-{VERSION}.dist-info/RECORD"
    record = io.StringIO(newline="")
    writer = csv.writer(record)
    for name, content in wheel_files.items():
        digest = base64.urlsafe_b64encode(sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([name, "sha256=" + digest, str(len(content))])
    writer.writerow([record_name, "", ""])
    wheel_files[record_name] = record.getvalue().encode()
    dist = tmp_path / "dist"
    dist.mkdir()
    names = module.artifact_names(VERSION)
    for kind in ("wheel", "repository"):
        prefix = "" if kind == "wheel" else f"E-Rechnungs-Pruefer-{VERSION}/"
        with zipfile.ZipFile(dist / names[kind], "w") as archive:
            for name, content in groups[kind].items():
                archive.writestr(prefix + name, content)
    with tarfile.open(dist / names["sdist"], "w:gz") as archive:
        for name, content in groups["sdist"].items():
            member = tarfile.TarInfo(f"e_rechnung_pruefer-{VERSION}/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    rewrite_sums(module, dist)
    return dist


def rewrite_sums(module, dist):
    names = module.artifact_names(VERSION)
    (dist / names["checksums"]).write_text(
        "".join(
            f"{sha256((dist / names[kind]).read_bytes()).hexdigest()}  {names[kind]}\n"
            for kind in ("wheel", "sdist", "repository")
        ),
        encoding="utf-8",
    )


def test_complete_archive_set_and_resources_are_verified_without_writes(module, tmp_path):
    dist = write_release(module, tmp_path)
    before = {file.name: sha256(file.read_bytes()).hexdigest() for file in dist.iterdir()}
    result = module.check_artifacts(dist, VERSION)
    assert result["version"] == VERSION
    assert len(result["artifacts"]) == 4
    assert before == {file.name: sha256(file.read_bytes()).hexdigest() for file in dist.iterdir()}


@pytest.mark.parametrize(
    "relative",
    [
        ".git/config",
        ".venv/secret",
        "vendor/kosit/a.jar",
        "runtime/java/x",
        ".env.private",
        "local-data/evidence",
        "reports/result.html",
        "uploads/invoice.pdf",
        "keys/cert.PFX",
        "invoice.xml",
    ],
)
@pytest.mark.parametrize("kind", ["wheel", "sdist", "repository"])
def test_sensitive_files_are_rejected_in_every_archive(module, tmp_path, relative, kind):
    dist = write_release(module, tmp_path, lambda groups: groups[kind].update({relative: b"forbidden synthetic data"}))
    with pytest.raises(module.ArtifactError, match="Unzulässig"):
        module.check_artifacts(dist, VERSION)


@pytest.mark.parametrize(
    "relative",
    ["../outside", "/absolute", "C:/drive", "dir\\escape", "dir/./alias", "dir//alias", "dir/file.", "CON.txt"],
)
def test_unsafe_member_names_are_not_normalized_into_safe_paths(module, tmp_path, relative):
    dist = write_release(module, tmp_path, lambda groups: groups["wheel"].update({relative: b"synthetic"}))
    with pytest.raises(module.ArtifactError, match="Pfad"):
        module.check_artifacts(dist, VERSION)


@pytest.mark.parametrize("kind", ["wheel", "sdist", "repository"])
def test_missing_runtime_resource_is_not_hidden_by_other_archives(module, tmp_path, kind):
    dist = write_release(module, tmp_path, lambda groups: groups[kind].pop("app/assets/fonts/NotoSans-Regular.ttf"))
    with pytest.raises(module.ArtifactError, match="fehlt"):
        module.check_artifacts(dist, VERSION)


def test_isolation_bootstrap_cannot_be_missing_from_all_runtime_archives(module, tmp_path):
    def omit(groups):
        for files in groups.values():
            files.pop("app/processing/bootstrap.py", None)

    dist = write_release(module, tmp_path, omit)
    with pytest.raises(module.ArtifactError, match="fehlt"):
        module.check_artifacts(dist, VERSION)


@pytest.mark.parametrize("kind", ["sdist", "repository"])
def test_source_archives_require_lock_sidecars_and_setup_entrypoints(module, tmp_path, kind):
    path = "packaging/docker/requirements-linux-arm64.txt.metadata.json"
    dist = write_release(module, tmp_path, lambda groups: groups[kind].pop(path))
    with pytest.raises(module.ArtifactError, match="fehlt"):
        module.check_artifacts(dist, VERSION)


def test_archive_metadata_version_must_match_candidate(module, tmp_path):
    path = f"e_rechnung_pruefer-{VERSION}.dist-info/METADATA"
    dist = write_release(
        module, tmp_path, lambda groups: groups["wheel"].update({path: b"Name: e-rechnung-pruefer\nVersion: 9.9.9\n"})
    )
    with pytest.raises(module.ArtifactError, match="Version"):
        module.check_artifacts(dist, VERSION)


def test_source_lock_sidecar_must_bind_embedded_lock(module, tmp_path):
    path = "packaging/windows/requirements-release.txt"
    dist = write_release(module, tmp_path, lambda groups: groups["sdist"].update({path: b"altered lock"}))
    with pytest.raises(module.ArtifactError, match="Lock"):
        module.check_artifacts(dist, VERSION)


def test_archives_must_agree_on_runtime_bytes(module, tmp_path):
    dist = write_release(module, tmp_path, lambda groups: groups["wheel"].update({"app/static/app.js": b"different"}))
    with pytest.raises(module.ArtifactError, match="Laufzeit"):
        module.check_artifacts(dist, VERSION)


def test_checksums_and_exact_outer_asset_inventory(module, tmp_path):
    dist = write_release(module, tmp_path)
    names = module.artifact_names(VERSION)
    with (dist / names["wheel"]).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(module.ArtifactError, match="SHA-256"):
        module.check_artifacts(dist, VERSION)
    rewrite_sums(module, dist)
    (dist / "private-evidence.json").write_text("synthetic", encoding="utf-8")
    with pytest.raises(module.ArtifactError, match="Artefaktmenge"):
        module.check_artifacts(dist, VERSION)


def test_duplicate_zip_members_and_symlinks_fail_closed(module, tmp_path):
    dist = write_release(module, tmp_path)
    names = module.artifact_names(VERSION)
    with zipfile.ZipFile(dist / names["wheel"], "a") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "app/__init__.py")
    rewrite_sums(module, dist)
    with pytest.raises(module.ArtifactError, match="Link"):
        module.check_artifacts(dist, VERSION)


def test_tar_hardlinks_are_rejected_without_extraction(module, tmp_path):
    dist = write_release(module, tmp_path)
    path = dist / module.artifact_names(VERSION)["sdist"]
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(f"e_rechnung_pruefer-{VERSION}/hardlink")
        member.type = tarfile.LNKTYPE
        member.linkname = "/outside"
        archive.addfile(member)
    rewrite_sums(module, dist)
    with pytest.raises(module.ArtifactError, match="Link"):
        module.check_artifacts(dist, VERSION)


def test_smoke_uses_isolated_interpreter_and_an_empty_directory(module, tmp_path, monkeypatch):
    dist = write_release(module, tmp_path)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert list(Path(kwargs["cwd"]).iterdir()) == []
        assert json.loads(kwargs["input"])["app/__init__.py"]
        receipt = {
            "version": VERSION,
            "syntax": ["CII", "UBL"],
            "native_processing": {
                "jobs": 8,
                "role_counts": {"supervisor": 8, "worker": 8},
                "all_reaped": True,
                "leases_remaining": 0,
            },
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(receipt), "")

    monkeypatch.setattr(module.subprocess, "run", run)
    result = module.wheel_smoke("synthetic-python", dist / module.artifact_names(VERSION)["wheel"], VERSION)
    assert result["version"] == VERSION
    assert calls[0][0][:3] == ["synthetic-python", "-I", "-c"]
    assert calls[0][1]["env"]["KOSIT_ENABLED"] == "false"


@pytest.mark.parametrize(
    "proof",
    [None, {"jobs": 8, "role_counts": {"supervisor": 8, "worker": 8}, "all_reaped": False, "leases_remaining": 0}],
)
def test_wheel_smoke_cannot_succeed_without_native_child_end_proof(module, tmp_path, monkeypatch, proof):
    dist = write_release(module, tmp_path)
    receipt = {"version": VERSION, "syntax": ["CII", "UBL"], "native_processing": proof}
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, json.dumps(receipt), ""),
    )
    with pytest.raises(module.ArtifactError, match="Prozess"):
        module.wheel_smoke("synthetic-python", dist / module.artifact_names(VERSION)["wheel"], VERSION)


def test_wheel_metadata_and_unexpected_importable_top_level_are_rejected(module, tmp_path):
    dist = write_release(
        module, tmp_path, lambda groups: groups["wheel"].update({"surprise.pth": b"import unexpected\n"})
    )
    with pytest.raises(module.ArtifactError, match="Wheel"):
        module.check_artifacts(dist, VERSION)


def test_wheel_requires_internal_format_metadata(module, tmp_path):
    path = f"e_rechnung_pruefer-{VERSION}.dist-info/WHEEL"
    dist = write_release(module, tmp_path, lambda groups: groups["wheel"].pop(path))
    with pytest.raises(module.ArtifactError, match="fehlt"):
        module.check_artifacts(dist, VERSION)


def test_case_colliding_archive_names_are_rejected(module, tmp_path):
    dist = write_release(module, tmp_path, lambda groups: groups["wheel"].update({"app/MAIN.py": b"collision"}))
    with pytest.raises(module.ArtifactError, match="kollidierender"):
        module.check_artifacts(dist, VERSION)


def test_runtime_version_in_source_cannot_disagree_with_metadata(module, tmp_path):
    dist = write_release(
        module, tmp_path, lambda groups: groups["sdist"].update({"app/__init__.py": b'__version__ = "9.9.9"\n'})
    )
    with pytest.raises(module.ArtifactError, match="Version"):
        module.check_artifacts(dist, VERSION)


def test_wheel_smoke_failure_cannot_produce_a_success_receipt(module, tmp_path, monkeypatch):
    dist = write_release(module, tmp_path)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "source checkout imported"),
    )
    with pytest.raises(module.ArtifactError, match="source checkout"):
        module.wheel_smoke("synthetic-python", dist / module.artifact_names(VERSION)["wheel"], VERSION)
