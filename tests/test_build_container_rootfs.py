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


def test_ldd_reports_all_failures_and_preserves_healthy_dependency_edges() -> None:
    with pytest.raises(rootfs.ElfInspectionError) as caught:
        rootfs.parse_ldd(
            "libmissing1.so => not found\n"
            "libhealthy.so => /usr/lib/libhealthy.so (0x0001)\n"
            "libmissing2.so => not found\n"
            "unexpected diagnostic\n"
        )
    assert caught.value.dependencies == ["/usr/lib/libhealthy.so"]
    for diagnostic in ("libmissing1.so", "libmissing2.so", "unexpected diagnostic"):
        assert diagnostic in str(caught.value)


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


def test_elf_closure_collects_broken_roots_and_finishes_healthy_transitive_inventory(tmp_path: Path) -> None:
    broken_one, broken_two, healthy_root, child, grandchild = [
        f"/usr/lib/{name}.so" for name in ("broken1", "broken2", "healthy", "child", "grandchild")
    ]
    paths = [broken_one, broken_two, healthy_root, child, grandchild]
    build = builder(tmp_path, {path: ["libdemo:amd64"] for path in paths})
    for path in paths:
        put(build.source, path, b"\x7fELF" + path.encode())
    for path in paths[:3]:
        build.copy_path(path)
    visited = []

    def inspect(path: str) -> list[str]:
        visited.append(path)
        if path == broken_one:
            return rootfs.parse_ldd(
                f"libabsent1.so => not found\nlibchild.so => {child} (0x001)\nlibabsent2.so => not found\n"
            )
        if path == broken_two:
            raise rootfs.RootfsError("independent ldd command failure")
        return {healthy_root: [child], child: [grandchild], grandchild: []}[path]

    with pytest.raises(rootfs.RootfsError) as caught:
        build.copy_elf_closure(inspect)
    for detail in (broken_one, broken_two, "libabsent1.so", "libabsent2.so", "independent ldd command failure"):
        assert detail in str(caught.value)
    assert set(visited) == set(paths)
    assert len(visited) == len(paths)
    assert "elf_dependencies" not in build.entries[broken_one]
    assert "elf_dependencies" not in build.entries[broken_two]
    assert build.entries[child]["elf_dependencies"] == [grandchild]
    assert build.entries[grandchild]["elf_dependencies"] == []
    with pytest.raises(rootfs.RootfsError, match="ELF was not inspected"):
        build.write_manifest(lock=tmp_path / "unused-lock", metadata=tmp_path / "unused-metadata")
    assert not (build.output / rootfs.MANIFEST_PATH.lstrip("/")).exists()


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


def ca_configuration_fixture(tmp_path: Path):
    anchor = "/usr/share/ca-certificates/mozilla/example.crt"
    build = builder(tmp_path, {anchor: ["ca-certificates:all"]})
    build.statuses["ca-certificates:all"] = stanza("ca-certificates", "all")
    put(build.source, anchor, b"synthetic public certificate")
    put(build.source, "/etc/ca-certificates.conf", b"# synthetic generated selection\nmozilla/example.crt\n")
    put(
        build.source, "/var/lib/dpkg/info/ca-certificates.postinst", b"# synthetic fixture: /etc/ca-certificates.conf\n"
    )
    return build, anchor


def test_generated_ca_configuration_records_producer_version_and_input_hashes(tmp_path: Path) -> None:
    build, anchor = ca_configuration_fixture(tmp_path)
    build.copy_path("/etc/ca-certificates.conf")
    provenance = build.entries["/etc/ca-certificates.conf"]["provenance"]
    assert provenance["kind"] == "debian-generated"
    assert provenance["packages"] == ["ca-certificates:all"]
    assert provenance["generator_version"] == "1.2-3"
    assert provenance["generator_sha256"] == rootfs.sha256(build.source / "var/lib/dpkg/info/ca-certificates.postinst")
    assert provenance["inputs"] == {anchor: rootfs.sha256(build.source / anchor.lstrip("/"))}


def test_disabled_removed_ca_is_recorded_without_claiming_active_input(tmp_path: Path) -> None:
    build, anchor = ca_configuration_fixture(tmp_path)
    (build.source / "etc/ca-certificates.conf").write_text("mozilla/example.crt\n!mozilla/removed.crt\n")
    build.copy_path("/etc/ca-certificates.conf")
    provenance = build.entries["/etc/ca-certificates.conf"]["provenance"]
    assert provenance["selected"] == [anchor]
    assert provenance["deselected"] == ["/usr/share/ca-certificates/mozilla/removed.crt"]
    assert set(provenance["inputs"]) == {anchor}


@pytest.mark.parametrize(
    "problem",
    [
        "no-producer",
        "no-script",
        "unowned-input",
        "missing-input",
        "escaping-input",
        "duplicate-input",
        "symlink-script",
        "symlink-input",
    ],
)
def test_generated_ca_configuration_does_not_whitelist_unknown_origins(tmp_path: Path, problem: str) -> None:
    build, anchor = ca_configuration_fixture(tmp_path)
    if problem == "no-producer":
        del build.statuses["ca-certificates:all"]
    elif problem == "no-script":
        (build.source / "var/lib/dpkg/info/ca-certificates.postinst").unlink()
    elif problem == "unowned-input":
        build.owners.clear()
    elif problem == "missing-input":
        (build.source / anchor.lstrip("/")).unlink()
    elif problem == "duplicate-input":
        (build.source / "etc/ca-certificates.conf").write_text("mozilla/example.crt\n!mozilla/example.crt\n")
    elif problem in {"symlink-script", "symlink-input"}:
        victim = (
            build.source / "var/lib/dpkg/info/ca-certificates.postinst"
            if problem == "symlink-script"
            else build.source / anchor.lstrip("/")
        )
        external = tmp_path / "unbound"
        external.write_bytes(victim.read_bytes())
        victim.unlink()
        victim.symlink_to(external)
    else:
        (build.source / "etc/ca-certificates.conf").write_text("../../tmp/unknown.crt\n")
    with pytest.raises(rootfs.RootfsError):
        build.copy_path("/etc/ca-certificates.conf")


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


def test_headless_rootfs_removes_only_unused_cpython_gui_payload(tmp_path: Path) -> None:
    build = builder(tmp_path)
    excluded = (
        "tkinter/__init__.py",
        "idlelib/__main__.py",
        "turtledemo/__main__.py",
        "turtle.py",
        "lib-dynload/_tkinter.cpython-314-x86_64-linux-gnu.so",
        "lib-dynload/_tkinter.cpython-314-aarch64-linux-gnu.so",
    )
    retained = (
        "ssl.py",
        "lib-dynload/_ssl.cpython-314-x86_64-linux-gnu.so",
        "lib-dynload/_sqlite3.cpython-314-aarch64-linux-gnu.so",
        "lib-dynload/_ctypes.cpython-314-x86_64-linux-gnu.so",
        "lib-dynload/unrelated_tkinter_named_extension.so",
    )
    for relative in excluded + retained:
        put(build.source, rootfs.PYTHON_STDLIB + "/" + relative)
    build.copy_path(rootfs.PYTHON_STDLIB, recursive=True, exclude=rootfs.skip_cpython)
    for relative in excluded:
        assert not (build.output / rootfs.PYTHON_STDLIB.lstrip("/") / relative).exists()
        assert rootfs.PYTHON_STDLIB + "/" + relative not in build.entries
    for relative in retained:
        assert (build.output / rootfs.PYTHON_STDLIB.lstrip("/") / relative).is_file()
    for directory in ("tkinter", "idlelib", "turtledemo"):
        assert not (build.output / rootfs.PYTHON_STDLIB.lstrip("/") / directory).exists()


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


def test_java_dlopen_fontconfig_root_preserves_library_and_owned_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = "/usr/lib/x86_64-linux-gnu/libfontconfig.so.1.12.0"
    soname = "/usr/lib/x86_64-linux-gnu/libfontconfig.so.1"
    configuration = "/etc/fonts/fonts.conf"
    available = "/usr/share/fontconfig/conf.avail/10-example.conf"
    enabled = "/etc/fonts/conf.d/10-example.conf"
    owners = {
        library: ["libfontconfig1:amd64"],
        soname: ["libfontconfig1:amd64"],
        configuration: ["fontconfig-config:all"],
        available: ["fontconfig-config:all"],
        enabled: ["fontconfig-config:all"],
    }
    build = builder(tmp_path, owners)
    build.statuses["libfontconfig1:amd64"] = stanza("libfontconfig1")
    build.statuses["fontconfig-config:all"] = stanza("fontconfig-config", "all")
    put(build.source, library, b"\x7fELFsynthetic fontconfig")
    (build.source / soname.lstrip("/")).symlink_to("libfontconfig.so.1.12.0")
    put(build.source, configuration, b"synthetic fontconfig configuration")
    put(build.source, available, b"synthetic selected configuration")
    (build.source / "etc/fonts/conf.d").mkdir()
    (build.source / enabled.lstrip("/")).symlink_to(available)
    put(build.source, "/usr/local/lib/.keep")
    original_copy = build.copy_path

    def copy_fontconfig_only(path: str, **kwargs) -> None:
        if path.startswith(("/etc/fonts", "/usr/share/fontconfig", "/usr/lib/x86_64-linux-gnu/libfontconfig")):
            original_copy(path, **kwargs)

    monkeypatch.setattr(build, "copy_path", copy_fontconfig_only)
    build.runtime_roots()
    assert (build.output / soname.lstrip("/")).readlink() == Path("libfontconfig.so.1.12.0")
    assert (build.output / library.lstrip("/")).read_bytes() == b"\x7fELFsynthetic fontconfig"
    assert (build.output / enabled.lstrip("/")).readlink() == Path(available)
    assert build.entries[library]["elf"] is True
    assert build.entries[library]["provenance"]["packages"] == ["libfontconfig1:amd64"]
    assert build.entries[configuration]["provenance"]["packages"] == ["fontconfig-config:all"]
    assert build.entries[available]["provenance"]["packages"] == ["fontconfig-config:all"]


def fontconfig_generated_fixture(tmp_path: Path, name: str = "10-hinting-slight.conf"):
    target = "/usr/share/fontconfig/conf.avail/" + name
    path = "/etc/fonts/conf.d/" + name
    build = builder(tmp_path, {target: ["fontconfig-config:amd64"]})
    build.statuses["fontconfig-config:amd64"] = stanza("fontconfig-config")
    put(build.source, target, b"synthetic package-owned fontconfig template")
    put(build.source, "/var/lib/dpkg/info/fontconfig-config.postinst", b"# synthetic postinst fixture\n")
    (build.source / "etc/fonts/conf.d").mkdir(parents=True)
    (build.source / path.lstrip("/")).symlink_to(target)
    return build, path, target


@pytest.mark.parametrize("name", ["10-hinting-slight.conf", "70-no-bitmaps-except-emoji.conf"])
def test_fontconfig_generated_defaults_bind_package_script_and_exact_template(tmp_path: Path, name: str) -> None:
    build, path, target = fontconfig_generated_fixture(tmp_path, name)
    build.copy_path(path)
    origin = build.entries[path]["provenance"]
    assert origin["kind"] == "debian-generated-symlink"
    assert origin["packages"] == ["fontconfig-config:amd64"]
    assert origin["target"] == target
    assert origin["generator_version"] == "1.2-3"
    assert origin["generator_sha256"] == rootfs.sha256(build.source / "var/lib/dpkg/info/fontconfig-config.postinst")
    assert origin["inputs"] == {target: rootfs.sha256(build.source / target.lstrip("/"))}
    assert (build.output / target.lstrip("/")).read_bytes() == b"synthetic package-owned fontconfig template"


@pytest.mark.parametrize(
    "problem", ["unknown-name", "different-target", "unowned-template", "no-script", "regular-file"]
)
def test_fontconfig_generated_rule_does_not_allow_unknown_payload(tmp_path: Path, problem: str) -> None:
    name = "unapproved.conf" if problem == "unknown-name" else "10-hinting-slight.conf"
    build, path, _ = fontconfig_generated_fixture(tmp_path, name)
    link = build.source / path.lstrip("/")
    if problem == "different-target":
        link.unlink()
        link.symlink_to("/usr/share/fontconfig/conf.avail/10-hinting-full.conf")
    elif problem == "unowned-template":
        build.owners.clear()
    elif problem == "no-script":
        (build.source / "var/lib/dpkg/info/fontconfig-config.postinst").unlink()
    elif problem == "regular-file":
        link.unlink()
        link.write_bytes(b"unapproved file")
    with pytest.raises(rootfs.RootfsError):
        build.copy_path(path)


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
