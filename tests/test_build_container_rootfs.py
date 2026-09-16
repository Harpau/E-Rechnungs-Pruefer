from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import struct
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux rootfs helper requires POSIX image/symlink semantics")

SPEC = importlib.util.spec_from_file_location(
    "build_container_rootfs", Path(__file__).resolve().parents[1] / "scripts/build_container_rootfs.py"
)
assert SPEC and SPEC.loader
rootfs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rootfs)


def test_manifest_base_identity_matches_actual_docker_builder() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    first_from = next(
        line.split() for line in dockerfile.read_text().splitlines() if line.strip().upper().startswith("FROM ")
    )
    assert first_from[1] == rootfs.DEFAULT_BASE_IMAGE


def put(root: Path, path: str, content: bytes = b"payload") -> Path:
    result = root / path.lstrip("/")
    result.parent.mkdir(parents=True, exist_ok=True)
    result.write_bytes(content)
    return result


def stanza(name: str, arch: str = "amd64") -> str:
    return (
        f"Package: {name}\nStatus: install ok installed\nArchitecture: {arch}\n"
        "Version: 1.2-3\nSource: upstream (1.2-1)\nDescription: example\n continuation\n"
    )


def builder(tmp_path: Path, owners: dict[str, list[str]] | None = None):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    return rootfs.RootfsBuilder(
        source,
        tmp_path / "output",
        owners=owners or {},
        statuses={"libdemo:amd64": stanza("libdemo")},
        base_image=rootfs.DEFAULT_BASE_IMAGE,
    )


def test_status_preserves_full_source_and_continuation_without_reconstruction() -> None:
    text = stanza("libdemo") + "\n" + stanza("other", "all") + "\n"
    parsed = rootfs.parse_status(text)
    assert parsed["libdemo:amd64"] == stanza("libdemo")
    assert parsed["other:all"] == stanza("other", "all")
    with pytest.raises(rootfs.RootfsError, match="duplicate"):
        rootfs.parse_status(stanza("libdemo") + "\n" + stanza("libdemo"))


def test_ldd_parses_loader_and_rejects_missing_or_unrecognised_results() -> None:
    output = (
        "\tlinux-vdso.so.1 (0x0001)\n"
        "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x0002)\n"
        "\t/lib64/ld-linux-x86-64.so.2 (0x0003)\n"
    )
    assert rootfs.parse_ldd(output) == ["/lib/x86_64-linux-gnu/libc.so.6", "/lib64/ld-linux-x86-64.so.2"]
    for bad in ("libbroken.so => not found", "unrecognised diagnostics", "", "statically linked"):
        with pytest.raises(rootfs.RootfsError):
            rootfs.parse_ldd(bad)


def test_dependency_free_elf_loader_is_proven_from_dynamic_headers(tmp_path: Path) -> None:
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<H", header, 18, 62)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HH", header, 54, 56, 1)
    program = struct.pack("<IIQQQQQQ", 2, 4, 120, 0, 0, 16, 16, 8)
    loader = tmp_path / "ld-linux.so"
    loader.write_bytes(header + program + struct.pack("<qQ", 0, 0))
    assert rootfs.elf_linkage(loader) == (False, None)
    loader.write_bytes(header + program + struct.pack("<qQ", 1, 0))
    with pytest.raises(rootfs.RootfsError, match="Unterminated"):
        rootfs.elf_linkage(loader)


def test_unknown_payload_and_preexisting_destination_fail_closed(tmp_path: Path) -> None:
    build = builder(tmp_path)
    put(build.source, "/usr/lib/libunknown.so")
    with pytest.raises(rootfs.RootfsError, match="provenance"):
        build.copy_path("/usr/lib/libunknown.so")
    with pytest.raises(rootfs.RootfsError, match="empty"):
        rootfs.RootfsBuilder(build.source, build.output, owners={}, statuses={}, base_image=rootfs.DEFAULT_BASE_IMAGE)


def test_absolute_and_merged_usr_symlinks_are_copied_inside_output(tmp_path: Path) -> None:
    owners = {
        "/usr/bin/java": ["libdemo:amd64"],
        "/usr/lib/jvm/bin/java": ["libdemo:amd64"],
        "/usr/lib/libdemo.so.1": ["libdemo:amd64"],
    }
    build = builder(tmp_path, owners)
    put(build.source, "/usr/lib/jvm/bin/java", b"java bytes")
    put(build.source, "/usr/lib/libdemo.so.1", b"library bytes")
    (build.source / "usr/bin").mkdir()
    (build.source / "etc/alternatives").mkdir(parents=True)
    (build.source / "usr/bin/java").symlink_to("/etc/alternatives/java")
    (build.source / "etc/alternatives/java").symlink_to("/usr/lib/jvm/bin/java")
    (build.source / "lib").symlink_to("usr/lib")
    build.copy_path("/usr/bin/java")
    build.copy_path("/lib/libdemo.so.1")
    assert (build.output / "usr/bin/java").readlink() == Path("/etc/alternatives/java")
    assert (build.output / "etc/alternatives/java").readlink() == Path("/usr/lib/jvm/bin/java")
    assert (build.output / "usr/lib/jvm/bin/java").read_bytes() == b"java bytes"
    assert (build.output / "lib").readlink() == Path("usr/lib")
    assert (build.output / "usr/lib/libdemo.so.1").read_bytes() == b"library bytes"


def test_symlink_loop_and_missing_target_are_errors(tmp_path: Path) -> None:
    build = builder(tmp_path)
    (build.source / "loop").symlink_to("/loop")
    (build.source / "missing").symlink_to("/absent")
    for path in ("/loop", "/missing"):
        with pytest.raises(rootfs.RootfsError):
            build.copy_path(path)


def test_elf_closure_inspects_non_executable_files_and_is_transitive(tmp_path: Path) -> None:
    paths = ["/usr/lib/extension.so", "/usr/lib/libfirst.so", "/usr/lib/libsecond.so"]
    build = builder(tmp_path, {path: ["libdemo:amd64"] for path in paths})
    for path in paths:
        put(build.source, path, b"\x7fELF" + path.encode()).chmod(0o644)
    dependencies = {paths[0]: [paths[1]], paths[1]: [paths[2]], paths[2]: []}
    build.copy_path(paths[0])
    seen = []

    def inspect(path: str) -> list[str]:
        seen.append(path)
        return dependencies[path]

    build.copy_elf_closure(inspect)
    assert seen == paths
    assert all(path in build.entries for path in paths)
    assert build.entries[paths[0]]["elf_dependencies"] == [paths[1]]


def test_metadata_keeps_full_status_and_resolves_copyright_owners(tmp_path: Path) -> None:
    owners = {
        "/usr/lib/libdemo.so": ["libdemo:amd64"],
        "/usr/share/doc/libdemo/copyright": ["libdemo:amd64"],
        "/usr/share/doc/common/copyright": ["common:all"],
    }
    build = builder(tmp_path, owners)
    build.statuses["common:all"] = stanza("common", "all")
    put(build.source, "/usr/lib/libdemo.so")
    put(build.source, "/usr/share/doc/common/copyright", b"license")
    (build.source / "usr/share/doc/libdemo").mkdir()
    (build.source / "usr/share/doc/libdemo/copyright").symlink_to("../common/copyright")
    build.copy_path("/usr/lib/libdemo.so")
    build.copy_package_metadata()
    assert build.packages == {"libdemo:amd64", "common:all"}
    assert (build.output / "var/lib/dpkg/status").read_text() == stanza("common", "all") + "\n" + stanza(
        "libdemo"
    ) + "\n"
    assert (build.output / "var/lib/dpkg/status.d/libdemo:amd64").read_text() == stanza("libdemo")


def runtime_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    site = "/opt/runtime/lib/python3.14/site-packages"
    payload = b"demo = True\n"
    put(source, site + "/demo.py", payload)
    metadata_bytes = b"Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n"
    put(source, site + "/demo-1.0.dist-info/METADATA", metadata_bytes)
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
    metadata_digest = base64.urlsafe_b64encode(hashlib.sha256(metadata_bytes).digest()).rstrip(b"=").decode()
    put(
        source,
        site + "/demo-1.0.dist-info/RECORD",
        (
            f"demo.py,sha256={digest},{len(payload)}\n"
            f"demo-1.0.dist-info/METADATA,sha256={metadata_digest},{len(metadata_bytes)}\n"
            "demo-1.0.dist-info/RECORD,,\n"
        ).encode(),
    )
    lock = tmp_path / "lock.txt"
    lock.write_text("demo==1.0 --hash=sha256:" + "a" * 64 + "\n")
    metadata = tmp_path / "lock.metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
                "packages": [{"name": "demo", "version": "1.0", "sha256": "a" * 64, "filename": "demo.whl"}],
            }
        )
    )
    return source, lock, metadata


def test_runtime_inventory_checks_exact_lock_and_record_file_hashes(tmp_path: Path) -> None:
    source, lock, metadata = runtime_fixture(tmp_path)
    provenance = rootfs.runtime_provenance(source, lock, metadata)
    assert provenance["/opt/runtime/lib/python3.14/site-packages/demo.py"]["name"] == "demo"
    put(source, "/opt/runtime/lib/python3.14/site-packages/demo.py", b"tampered")
    with pytest.raises(rootfs.RootfsError, match="RECORD"):
        rootfs.runtime_provenance(source, lock, metadata)


def test_runtime_inventory_rejects_bootstrap_packages_and_changed_lock(tmp_path: Path) -> None:
    source, lock, metadata = runtime_fixture(tmp_path)
    lock.write_text("pip==26.2.1 --hash=sha256:" + "a" * 64 + "\n")
    with pytest.raises(rootfs.RootfsError):
        rootfs.runtime_provenance(source, lock, metadata)


def test_original_wheel_prevents_self_authorizing_rewritten_record(tmp_path: Path) -> None:
    source, lock, metadata = runtime_fixture(tmp_path)
    site = source / "opt/runtime/lib/python3.14/site-packages"
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    wheel = wheelhouse / "demo.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for file in site.rglob("*"):
            if file.is_file():
                archive.write(file, str(file.relative_to(site)))
    lock.write_text("demo==1.0 --hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest() + "\n")
    item = json.loads(metadata.read_text())
    item["lock_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
    item["packages"][0]["sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    metadata.write_text(json.dumps(item))
    rootfs.runtime_provenance(source, lock, metadata, wheelhouse)
    record = site / "demo-1.0.dist-info/RECORD"
    original = (site / "demo.py").read_bytes()
    replacement = b"evil = True\n"
    old_hash = base64.urlsafe_b64encode(hashlib.sha256(original).digest()).rstrip(b"=").decode()
    new_hash = base64.urlsafe_b64encode(hashlib.sha256(replacement).digest()).rstrip(b"=").decode()
    (site / "demo.py").write_bytes(replacement)
    record.write_text(
        record.read_text().replace(old_hash, new_hash).replace(f",{len(original)}\n", f",{len(replacement)}\n")
    )
    with pytest.raises(rootfs.RootfsError, match="original wheel"):
        rootfs.runtime_provenance(source, lock, metadata, wheelhouse)


@pytest.mark.parametrize("relative", ["/opt/runtime/evil.py", "../../../../../../etc/evil.py"])
def test_record_absolute_or_escaping_paths_are_rejected(tmp_path: Path, relative: str) -> None:
    source, lock, metadata = runtime_fixture(tmp_path)
    record = source / "opt/runtime/lib/python3.14/site-packages/demo-1.0.dist-info/RECORD"
    record.write_text(record.read_text() + relative + ",,\n")
    with pytest.raises(rootfs.RootfsError, match="RECORD"):
        rootfs.runtime_provenance(source, lock, metadata)


def test_venv_lib64_link_requires_exact_relative_target(tmp_path: Path) -> None:
    build = builder(tmp_path)
    (build.source / "opt/runtime/lib").mkdir(parents=True)
    (build.source / "opt/runtime/lib64").symlink_to("lib")
    build.copy_path("/opt/runtime/lib64")
    assert (build.output / "opt/runtime/lib64").readlink() == Path("lib")


@pytest.mark.parametrize("interpreter_name", ["python", "python3", "python3.14"])
def test_cpython_314_pi_alias_requires_bound_interpreter_chain(tmp_path: Path, interpreter_name: str) -> None:
    build = builder(tmp_path)
    put(build.source, "/usr/local/bin/python3.14", b"bound CPython interpreter")
    (build.source / "opt/runtime/bin").mkdir(parents=True)
    (build.source / f"opt/runtime/bin/{interpreter_name}").symlink_to("/usr/local/bin/python3.14")
    (build.source / "opt/runtime/bin/𝜋thon").symlink_to(interpreter_name)
    build.copy_path("/opt/runtime/bin/𝜋thon")
    assert (build.output / "opt/runtime/bin/𝜋thon").readlink() == Path(interpreter_name)
    assert build.entries["/opt/runtime/bin/𝜋thon"]["provenance"]["kind"] == "cpython-venv"
    assert (build.output / "usr/local/bin/python3.14").read_bytes() == b"bound CPython interpreter"


@pytest.mark.parametrize("case", ["regular-file", "foreign-target", "redirected-python"])
def test_pi_alias_does_not_authorize_unbound_payload(tmp_path: Path, case: str) -> None:
    build = builder(tmp_path)
    put(build.source, "/usr/local/bin/python3.14", b"bound CPython interpreter")
    put(build.source, "/opt/runtime/bin/foreign", b"unapproved executable")
    (build.source / "opt/runtime/bin/python").symlink_to(
        "foreign" if case == "redirected-python" else "/usr/local/bin/python3.14"
    )
    alias = build.source / "opt/runtime/bin/𝜋thon"
    if case == "regular-file":
        alias.write_bytes(b"unapproved executable")
    else:
        alias.symlink_to("foreign" if case == "foreign-target" else "python")
    with pytest.raises(rootfs.RootfsError):
        build.copy_path("/opt/runtime/bin/𝜋thon")


def test_generated_truststore_requires_real_producer_and_ca_inputs(tmp_path: Path) -> None:
    build = builder(tmp_path)
    put(build.source, "/etc/ssl/certs/java/cacerts")
    with pytest.raises(rootfs.RootfsError, match="producer"):
        build.copy_path("/etc/ssl/certs/java/cacerts")
    build.statuses["ca-certificates-java:all"] = stanza("ca-certificates-java", "all")
    with pytest.raises(rootfs.RootfsError, match="CA inputs"):
        build.copy_path("/etc/ssl/certs/java/cacerts")


def test_cpython_payload_excludes_installer_and_build_payloads() -> None:
    for path in (
        "/usr/local/lib/python3.14/ensurepip/__init__.py",
        "/usr/local/lib/python3.14/site-packages/pip/__init__.py",
        "/usr/local/lib/python3.14/__pycache__/os.cpython-314.pyc",
        "/usr/local/lib/python3.14/config-3.14-x86_64-linux-gnu/libpython3.14.a",
    ):
        assert rootfs.skip_cpython(path)
    assert not rootfs.skip_cpython("/usr/local/lib/python3.14/ssl.py")
    assert not rootfs.skip_cpython("/usr/local/lib/python3.14/lib-dynload/_ssl.cpython-314-x86_64-linux-gnu.so")


def test_only_optional_system_font_roots_may_be_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build = builder(tmp_path)
    put(build.source, "/usr/local/lib/.keep")
    put(build.source, "/usr/lib/.keep")
    copied: list[str] = []
    monkeypatch.setattr(build, "copy_path", lambda path, **kwargs: copied.append(path))
    build.runtime_roots()
    for optional in ("/etc/fonts", "/usr/share/fontconfig", "/usr/share/fonts"):
        assert optional not in copied
    assert "/usr/lib/jvm" in copied
    assert "/usr/share/zoneinfo" in copied
    assert "/etc/ssl" in copied
    put(build.source, "/usr/share/fonts/example.ttf")
    build.runtime_roots()
    assert "/usr/share/fonts" in copied


def test_runtime_ssl_root_excludes_private_keys_and_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    public_paths = ["/etc/ssl/certs/example.pem", "/etc/ssl/certs/java/cacerts"]
    build = builder(tmp_path, {path: ["libdemo:amd64"] for path in public_paths})
    for path in public_paths:
        put(build.source, path, b"public trust data")
    private = put(build.source, "/etc/ssl/private/unapproved.key", b"must never be copied")
    private.parent.chmod(0o700)
    put(build.source, "/usr/local/lib/.keep")
    put(build.source, "/usr/lib/.keep")
    original_copy = build.copy_path

    def copy_ssl_only(path: str, **kwargs) -> None:
        if path == "/etc/ssl" or path.startswith("/etc/ssl/"):
            original_copy(path, **kwargs)

    monkeypatch.setattr(build, "copy_path", copy_ssl_only)
    build.runtime_roots()
    for path in public_paths:
        assert (build.output / path.lstrip("/")).read_bytes() == b"public trust data"
    assert not (build.output / "etc/ssl/private").exists()
    assert all(not path.startswith("/etc/ssl/private") for path in build.entries)


def test_manifest_hashes_files_and_never_labels_unknown_payload_as_cpython(tmp_path: Path) -> None:
    build = builder(tmp_path, {"/usr/lib/libdemo.so": ["libdemo:amd64"]})
    put(build.source, "/usr/lib/libdemo.so", b"library")
    build.copy_path("/usr/lib/libdemo.so")
    item = build.entries["/usr/lib/libdemo.so"]
    assert item["sha256"] == hashlib.sha256(b"library").hexdigest()
    assert item["provenance"]["kind"] == "debian"
    put(build.source, "/usr/local/unapproved/foreign.so", b"unknown")
    with pytest.raises(rootfs.RootfsError, match="provenance"):
        build.copy_path("/usr/local/unapproved/foreign.so")
