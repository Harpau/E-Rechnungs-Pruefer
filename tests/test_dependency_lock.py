from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "dependency_lock", Path(__file__).resolve().parents[1] / "scripts/dependency_lock.py"
)
assert SPEC and SPEC.loader
lock = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lock)


def environment() -> dict[str, str]:
    return lock.target_environment("windows-release", "3.14.7")


def package(
    name: str, dependencies: tuple[str, ...] = (), version: str = "1.0", extras: tuple[str, ...] = ()
) -> dict[str, object]:
    return {
        "name": name,
        "version": version,
        "requires_python": ">=3.11",
        "requires_dist": list(dependencies),
        "provides_extra": list(extras),
    }


def test_closure_follows_extras_recursively_and_target_markers() -> None:
    packages = [
        package(
            "server", ('watcher[fast]; extra == "standard"', 'windows; sys_platform == "win32"'), extras=("standard",)
        ),
        package("watcher", ('native; extra == "fast"',), extras=("fast",)),
        package("native"),
        package("windows"),
    ]
    lock.verify_closure(["server[standard]>=1"], packages, environment())


@pytest.mark.parametrize("missing", ["watcher", "native", "windows"])
def test_closure_rejects_missing_transitive_or_extra(missing: str) -> None:
    packages = [
        package(
            "server", ('watcher[fast]; extra == "standard"', 'windows; sys_platform == "win32"'), extras=("standard",)
        ),
        package("watcher", ('native; extra == "fast"',), extras=("fast",)),
        package("native"),
        package("windows"),
    ]
    with pytest.raises(lock.LockError, match=missing):
        lock.verify_closure(["server[standard]"], [p for p in packages if p["name"] != missing], environment())


@pytest.mark.parametrize(
    "roots,packages",
    [
        (["demo[missing]"], [package("demo")]),
        (["parent"], [package("parent", ("demo[missing]",)), package("demo")]),
        (["demo[existing,missing]"], [package("demo", extras=("existing",))]),
    ],
)
def test_closure_rejects_undeclared_root_and_transitive_extras(roots: list[str], packages: list[dict]) -> None:
    with pytest.raises(lock.LockError, match="missing"):
        lock.verify_closure(roots, packages, environment())


def test_closure_accepts_declared_empty_and_normalized_extras() -> None:
    lock.verify_closure(["demo[empty,Fast_Mode]"], [package("demo", extras=("empty", "fast-mode"))], environment())


def test_closure_ignores_inactive_undeclared_extra() -> None:
    lock.verify_closure(["demo", 'demo[missing]; sys_platform == "linux"'], [package("demo")], environment())


@pytest.mark.parametrize(
    "packages, roots",
    [
        ([package("server", ("child>=2",)), package("child")], ["server"]),
        ([package("server"), package("unused")], ["server"]),
        ([package("server"), package("server")], ["server"]),
        ([package("server", version="1.0rc1")], ["server"]),
    ],
)
def test_closure_rejects_conflicts_extraneous_duplicates_and_prereleases(packages, roots) -> None:
    with pytest.raises(lock.LockError):
        lock.verify_closure(roots, packages, environment())


def test_closure_checks_python_version_and_older_python_marker() -> None:
    pkg = package("server") | {"requires_python": ">=3.15"}
    with pytest.raises(lock.LockError, match="Python"):
        lock.verify_closure(["server"], [pkg], environment())
    env = environment() | {"python_full_version": "3.11.9", "python_version": "3.11"}
    with pytest.raises(lock.LockError, match="older"):
        lock.verify_closure(["server"], [package("server", ('older; python_version < "3.12"',))], env)


@pytest.mark.parametrize(
    "line",
    [
        "server>=1 --hash=sha256:" + "a" * 64,
        "server==1",
        "server==1; sys_platform == 'win32' --hash=sha256:" + "a" * 64,
        "server==1 --hash=md5:" + "a" * 32,
    ],
)
def test_lock_reader_rejects_ranges_missing_hashes_markers_and_weak_hashes(line: str) -> None:
    with pytest.raises(lock.LockError):
        lock.parse_lock(line)


def test_lock_reader_rejects_normalized_duplicates() -> None:
    with pytest.raises(lock.LockError):
        lock.parse_lock("Demo_Pkg==1 --hash=sha256:" + "a" * 64 + "\ndemo-pkg==1 --hash=sha256:" + "a" * 64)


def make_wheel(tmp_path: Path, filename: str = "demo-1.0-py3-none-any.whl", extras: tuple[str, ...] = ()) -> Path:
    wheel = tmp_path / filename
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.3\nName: demo\nVersion: 1.0\n"
            + "".join(f"Provides-Extra: {extra}\n" for extra in extras),
        )
        archive.writestr("demo-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
    return wheel


def test_wheel_metadata_records_normalized_exported_extras(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path, extras=("Fast_Mode", "empty", "legacy.name"))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    result = lock.read_wheel(wheel, digest, {"py3-none-any"})
    assert result["provides_extra"] == ["empty", "fast-mode", "legacy-name"]


def test_wheel_metadata_rejects_invalid_exported_extra(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path, extras=("not an extra",))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    with pytest.raises(lock.LockError, match="Extra"):
        lock.read_wheel(wheel, digest, {"py3-none-any"})


def test_wheel_verification_rejects_wrong_hash_and_metadata(tmp_path: Path) -> None:
    wheel = make_wheel(tmp_path)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    result = lock.read_wheel(wheel, digest, {"py3-none-any"})
    assert result["name"] == "demo"
    with pytest.raises(lock.LockError, match="SHA-256"):
        lock.read_wheel(wheel, "0" * 64, {"py3-none-any"})
    with pytest.raises(lock.LockError, match="kompatibel"):
        lock.read_wheel(wheel, digest, {"cp314-cp314-win_amd64"})


def test_refresh_report_includes_preinstalled_bootstrap_and_disallows_source_builds() -> None:
    command = lock.resolver_command(Path("report.json"), Path("roots.txt"))
    assert "--ignore-installed" in command
    assert "--only-binary=:all:" in command
    assert "--dry-run" in command


def test_profile_roots_bind_bootstrap_and_dev_inputs(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires=["setuptools>=77", "wheel"]\n'
        '[project]\nname="example"\ndependencies=["server[standard]"]\n'
        '[project.optional-dependencies]\ndev=["pytest", "httpx", "httpx2", "ruff"]\n'
    )
    roots, inputs = lock.profile_inputs(
        tmp_path, "source-release", ["pip==26.2.1", "setuptools==84", "wheel==0.48"], []
    )
    assert {"pip==26.2.1", "setuptools==84", "wheel==0.48", "server[standard]", "ruff"} <= set(roots)
    assert "pyproject.toml" in inputs
    with pytest.raises(lock.LockError, match="Bootstrap"):
        lock.profile_inputs(tmp_path, "source-release", ["pip==26.2.1"], [])


def test_native_target_rejects_os_arch_python_and_free_threading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lock, "host_environment", lambda: environment() | {"sys_platform": "linux"})
    with pytest.raises(lock.LockError, match="Ziel"):
        lock.require_native(environment())


@pytest.mark.parametrize("tag", ["cp314-cp314-linux_x86_64", "cp314t-cp314t-win_amd64", "cp315-cp315-win_amd64"])
def test_offline_tags_cannot_claim_foreign_platform_or_abi(tag: str) -> None:
    with pytest.raises(lock.LockError):
        lock.validate_target_tags({tag}, environment())


def test_target_tags_allow_abi3_and_universal_wheels() -> None:
    lock.validate_target_tags({"cp310-abi3-win_amd64", "py3-none-any", "cp314-cp314-win_amd64"}, environment())


def test_external_inventory_must_match_native_target_and_lock() -> None:
    metadata = {"target": environment(), "packages": [package("demo")]}
    captured = {
        "environment": {
            "python": "3.14.7",
            "implementation": "CPython",
            "sys_platform": "win32",
            "machine": "AMD64",
            "gil_disabled": False,
        },
        "packages": [{"name": "demo", "version": "1.0"}],
    }
    lock.compare_target_inventory(metadata, captured)
    captured["environment"]["python"] = "3.13.15"
    with pytest.raises(lock.LockError, match="Ziel"):
        lock.compare_target_inventory(metadata, captured)


def test_report_rejects_yanked_non_wheel_and_wrong_environment() -> None:
    report = {"version": "1", "environment": environment(), "install": []}
    with pytest.raises(lock.LockError, match="leer"):
        lock.validate_report(report, environment())
    report["environment"] = environment() | {"sys_platform": "linux"}
    with pytest.raises(lock.LockError, match="Ziel"):
        lock.validate_report(report, environment())


def test_inventory_comparison_allows_only_own_project_and_exact_packages() -> None:
    expected = {"demo": ("1.0", "a" * 64)}
    lock.compare_inventory(expected, [{"name": "demo", "version": "1.0"}])
    with pytest.raises(lock.LockError):
        lock.compare_inventory(expected, [{"name": "demo", "version": "2.0"}])


def test_sidecar_schema_is_checked(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("demo==1.0 --hash=sha256:" + "a" * 64 + "\n")
    Path(str(path) + ".metadata.json").write_text(json.dumps({"schema_version": 999}))
    with pytest.raises(lock.LockError, match="Schema"):
        lock.check_lock(path)


def test_native_refresh_and_verify_roundtrip_uses_exact_downloads_without_reresolving(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="example"\ndependencies=["demo"]\n')
    env = lock.target_environment("docker-amd64", "3.14.7")
    monkeypatch.setattr(lock, "host_environment", lambda: env)
    monkeypatch.setattr(lock, "sys_tags", lambda: lock.parse_tag("py3-none-any"))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    downloads = {}
    items = []
    for name in ("demo", "pip"):
        filename = f"{name}-1.0-py3-none-any.whl"
        path = wheels / filename
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(f"{name}-1.0.dist-info/METADATA", f"Metadata-Version: 2.3\nName: {name}\nVersion: 1.0\n")
            archive.writestr(f"{name}-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        url = f"https://files.pythonhosted.org/packages/synthetic/{filename}"
        downloads[url] = path
        items.append(
            {
                "is_yanked": False,
                "is_direct": False,
                "metadata": {"name": name, "version": "1.0"},
                "download_info": {"url": url, "archive_info": {"hashes": {"sha256": digest}}},
            }
        )

    def resolve(command, **kwargs):
        report = Path(command[command.index("--report") + 1])
        report.write_text(json.dumps({"version": "1", "pip_version": "26.2.1", "environment": env, "install": items}))

    monkeypatch.setattr(lock.subprocess, "run", resolve)
    monkeypatch.setattr(lock, "fetch_wheel", lambda url, digest, wheelhouse: downloads[url])
    output = root / "requirements.txt"
    lock.refresh(
        Namespace(
            profile="docker-amd64",
            python_version="3.14.7",
            output=output,
            project_root=root,
            bootstrap=["pip==1.0"],
            require=[],
            wheelhouse=wheels,
        )
    )
    assert set(lock.parse_lock(output.read_text())) == {"demo", "pip"}
    assert lock.check_lock(output, root)["generator"]["pip"] == "26.2.1"

    def forbid_resolver(*args, **kwargs):
        pytest.fail("verify must not invoke pip or any resolver")

    monkeypatch.setattr(lock.subprocess, "run", forbid_resolver)
    lock.verify(
        Namespace(lock=output, project_root=root, wheelhouse=wheels, installed=False, inventory=None, python=None)
    )
    (root / "pyproject.toml").write_text('[project]\nname="example"\ndependencies=["new-input"]\n')
    with pytest.raises(lock.LockError, match="Profileingaben"):
        lock.check_lock(output, root)
