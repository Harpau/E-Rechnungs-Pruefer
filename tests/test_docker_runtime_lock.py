from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("docker_runtime_lock", ROOT / "scripts/docker_runtime_lock.py")
assert SPEC and SPEC.loader
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def sidecar(path: Path) -> Path:
    return Path(str(path) + ".metadata.json")


def copied_parent(tmp_path: Path, architecture: str = "amd64") -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    original = ROOT / f"packaging/docker/requirements-linux-{architecture}.txt"
    parent = tmp_path / "parent.txt"
    parent.write_bytes(original.read_bytes())
    sidecar(parent).write_bytes(sidecar(original).read_bytes())
    return parent, project


def read_metadata(path: Path) -> dict:
    return json.loads(sidecar(path).read_bytes())


def save_metadata(path: Path, metadata: dict) -> None:
    sidecar(path).write_text(json.dumps(metadata, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def write_derived(parent: Path, project: Path, output: Path) -> dict:
    lock_bytes, metadata = runtime.derive_runtime_lock(parent, project)
    output.write_bytes(lock_bytes)
    save_metadata(output, metadata)
    return metadata


def inventory_for(metadata: dict) -> dict:
    target = metadata["target"]
    packages = [{"name": p["name"], "version": p["version"]} for p in metadata["packages"]]
    return {
        "schema_version": 1,
        "environment": {
            "python": target["python_full_version"],
            "implementation": "CPython",
            "sys_platform": target["sys_platform"],
            "machine": target["platform_machine"],
            "gil_disabled": False,
        },
        "packages": packages,
        "inventory_sha256": runtime.audit.inventory_digest(packages),
        "excluded_editable": None,
    }


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_real_parent_derives_exact_closed_runtime_without_changing_parent(tmp_path: Path, architecture: str) -> None:
    parent, project = copied_parent(tmp_path, architecture)
    parent_bytes, metadata_bytes = parent.read_bytes(), sidecar(parent).read_bytes()
    original = read_metadata(parent)
    output = tmp_path / "runtime.txt"
    derived = write_derived(parent, project, output)

    assert len(derived["packages"]) == 27
    assert derived["packages"] == [p for p in original["packages"] if p["name"] != "pip"]
    assert derived["roots"] == [r for r in original["roots"] if r != "pip==26.2.1"]
    assert derived["target"] == original["target"]
    assert derived["compatible_tags"] == original["compatible_tags"]
    assert derived["parent"]["lock_sha256"] == hashlib.sha256(parent_bytes).hexdigest()
    assert derived["parent"]["metadata_sha256"] == hashlib.sha256(metadata_bytes).hexdigest()
    assert derived["parent"]["generator"] == original["generator"]
    assert derived["derivation"]["script_sha256"] == runtime.lock.source_digest(ROOT / "scripts/docker_runtime_lock.py")
    assert derived["removed"] == [p for p in original["packages"] if p["name"] == "pip"]
    assert "pip" not in runtime.lock.parse_lock(output.read_text())
    runtime.lock.verify_closure(derived["roots"], derived["packages"], derived["target"])
    assert runtime.check_runtime_lock(output, parent, project) == derived
    runtime.verify_runtime_inventory(derived, inventory_for(derived))
    assert (parent.read_bytes(), sidecar(parent).read_bytes()) == (parent_bytes, metadata_bytes)
    assert runtime.derive_runtime_lock(parent, project) == (output.read_bytes(), derived)


@pytest.mark.parametrize("field", ["generator", "inputs", "lock_sha256"])
def test_derivation_rejects_unbound_parent_metadata(tmp_path: Path, field: str) -> None:
    parent, project = copied_parent(tmp_path)
    metadata = read_metadata(parent)
    if field == "generator":
        metadata[field]["script_sha256"] = "0" * 64
    elif field == "inputs":
        metadata[field]["pyproject.toml"] = "0" * 64
    else:
        metadata[field] = "0" * 64
    save_metadata(parent, metadata)
    with pytest.raises(ValueError):
        runtime.derive_runtime_lock(parent, project)


def test_derivation_rejects_changed_project_inputs(tmp_path: Path) -> None:
    parent, project = copied_parent(tmp_path)
    with (project / "pyproject.toml").open("a") as handle:
        handle.write("\n# Different approved project inputs\n")
    with pytest.raises(ValueError):
        runtime.derive_runtime_lock(parent, project)


@pytest.mark.parametrize("profile", ["windows-release", "source-release"])
def test_derivation_rejects_non_docker_parents(tmp_path: Path, profile: str) -> None:
    parent, project = copied_parent(tmp_path)
    metadata = read_metadata(parent)
    metadata["profile"] = profile
    save_metadata(parent, metadata)
    with pytest.raises(ValueError):
        runtime.derive_runtime_lock(parent, project)


@pytest.mark.parametrize("change", ["additional", "bootstrap", "duplicate_bootstrap", "python"])
def test_derivation_rejects_unexpected_profile_scope(tmp_path: Path, change: str) -> None:
    parent, project = copied_parent(tmp_path)
    metadata = read_metadata(parent)
    if change == "additional":
        metadata["additional_requirements"] = ["fastapi>=0.141.1,<1"]
    elif change == "bootstrap":
        metadata["bootstrap"].append("fastapi==0.141.1")
        metadata["roots"], _ = runtime.lock.profile_inputs(
            project, metadata["profile"], metadata["bootstrap"], metadata["additional_requirements"]
        )
    elif change == "duplicate_bootstrap":
        metadata["bootstrap"] *= 2
    else:
        metadata["target"]["python_full_version"] = "3.14.6"
        metadata["target"]["implementation_version"] = "3.14.6"
    save_metadata(parent, metadata)
    with pytest.raises(ValueError):
        runtime.derive_runtime_lock(parent, project)


def test_runtime_closure_rejects_pip_required_transitively(tmp_path: Path) -> None:
    parent, project = copied_parent(tmp_path)
    metadata = read_metadata(parent)
    next(p for p in metadata["packages"] if p["name"] == "fastapi")["requires_dist"].append("pip>=26")
    save_metadata(parent, metadata)
    runtime.lock.check_lock(parent, project)
    with pytest.raises(ValueError, match="pip"):
        runtime.derive_runtime_lock(parent, project)


def test_derivation_cannot_remove_pip_that_is_also_a_project_root(tmp_path: Path) -> None:
    parent, project = copied_parent(tmp_path)
    pyproject = project / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace("dependencies = [", 'dependencies = [\n  "pip==26.2.1",', 1))
    metadata = read_metadata(parent)
    metadata["roots"], metadata["inputs"] = runtime.lock.profile_inputs(
        project, metadata["profile"], metadata["bootstrap"], []
    )
    save_metadata(parent, metadata)
    runtime.lock.check_lock(parent, project)
    with pytest.raises(ValueError, match="[Pp]ip|pip"):
        runtime.derive_runtime_lock(parent, project)


@pytest.mark.parametrize("change", ["package_hash", "wheel_url", "roots", "parent", "script", "extra_key"])
def test_checker_rejects_self_consistent_but_forged_runtime_metadata(tmp_path: Path, change: str) -> None:
    parent, project = copied_parent(tmp_path)
    output = tmp_path / "runtime.txt"
    metadata = write_derived(parent, project, output)
    if change == "package_hash":
        old_hash = metadata["packages"][0]["sha256"]
        metadata["packages"][0]["sha256"] = "0" * 64
        output.write_text(output.read_text().replace(old_hash, "0" * 64))
        metadata["lock_sha256"] = runtime.lock.sha256(output)
    elif change == "wheel_url":
        metadata["packages"][0]["url"] = "https://files.pythonhosted.org/packages/forged.whl"
    elif change == "roots":
        metadata["roots"] = []
    elif change == "parent":
        metadata["parent"]["metadata_sha256"] = "0" * 64
    elif change == "script":
        metadata["derivation"]["script_sha256"] = "0" * 64
    else:
        metadata["ignored_packages"] = ["pip"]
    save_metadata(output, metadata)
    with pytest.raises(ValueError):
        runtime.check_runtime_lock(output, parent, project)


def test_checker_binds_parent_sidecar_bytes_and_rejects_cross_architecture(tmp_path: Path) -> None:
    parent, project = copied_parent(tmp_path)
    output = tmp_path / "runtime.txt"
    write_derived(parent, project, output)
    with sidecar(parent).open("ab") as handle:
        handle.write(b"\n")
    with pytest.raises(ValueError):
        runtime.check_runtime_lock(output, parent, project)
    parent, project = copied_parent(tmp_path, "arm64")
    with pytest.raises(ValueError):
        runtime.check_runtime_lock(output, parent, project)


@pytest.mark.parametrize(
    "change", ["extra", "missing", "version", "duplicate", "digest", "editable", "no_editable_key"]
)
def test_complete_runtime_inventory_rejects_omissions_additions_and_exclusions(tmp_path: Path, change: str) -> None:
    parent, project = copied_parent(tmp_path)
    _, metadata = runtime.derive_runtime_lock(parent, project)
    inventory = inventory_for(metadata)
    if change == "extra":
        inventory["packages"].append({"name": "pip", "version": "26.2.1"})
    elif change == "missing":
        inventory["packages"].pop()
    elif change == "version":
        inventory["packages"][0]["version"] = "0.0.1"
    elif change == "duplicate":
        inventory["packages"].append(copy.deepcopy(inventory["packages"][0]))
    elif change == "editable":
        inventory["excluded_editable"] = {"name": "e-rechnung-pruefer", "version": "2.0.3", "project_root": "/app"}
    elif change == "no_editable_key":
        del inventory["excluded_editable"]
    if change not in {"duplicate", "digest"}:
        inventory["inventory_sha256"] = runtime.audit.inventory_digest(inventory["packages"])
    elif change == "digest":
        inventory["inventory_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        runtime.verify_runtime_inventory(metadata, inventory)


@pytest.mark.parametrize(
    "field,value",
    [
        ("python", "3.14.6"),
        ("machine", "aarch64"),
        ("sys_platform", "win32"),
        ("implementation", "PyPy"),
        ("gil_disabled", True),
    ],
)
def test_runtime_inventory_rejects_wrong_interpreter_architecture_or_abi(
    tmp_path: Path, field: str, value: object
) -> None:
    parent, project = copied_parent(tmp_path)
    _, metadata = runtime.derive_runtime_lock(parent, project)
    inventory = inventory_for(metadata)
    inventory["environment"][field] = value
    with pytest.raises(ValueError):
        runtime.verify_runtime_inventory(metadata, inventory)


def test_cli_derive_check_verify_are_offline_and_do_not_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("Runtime lock commands must neither resolve, download nor install.")

    monkeypatch.setattr(runtime.lock.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(runtime.lock.subprocess, "run", forbidden)
    parent, project = copied_parent(tmp_path)
    output = tmp_path / "runtime.txt"
    common = ["--parent", str(parent), "--project-root", str(project)]
    assert runtime.main(["derive", *common, "--output", str(output)]) == 0
    original_bytes = output.read_bytes(), sidecar(output).read_bytes()
    assert runtime.main(["derive", *common, "--output", str(output)]) == 0
    assert (output.read_bytes(), sidecar(output).read_bytes()) == original_bytes
    assert runtime.main(["check", *common, "--lock", str(output)]) == 0
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps(inventory_for(read_metadata(output))))
    assert runtime.main(["verify", *common, "--lock", str(output), "--inventory", str(inventory)]) == 0
    inventory.write_text("{}")
    assert runtime.main(["verify", *common, "--lock", str(output), "--inventory", str(inventory)]) == 2


@pytest.mark.parametrize("destination", ["parent", "parent_metadata", "existing", "existing_metadata"])
def test_cli_derive_does_not_overwrite_inputs_or_conflicting_outputs(tmp_path: Path, destination: str) -> None:
    parent, project = copied_parent(tmp_path)
    output = tmp_path / "runtime.txt"
    if destination == "parent":
        output = parent
    elif destination == "parent_metadata":
        output = sidecar(parent)
    elif destination == "existing":
        output.write_bytes(b"must remain unchanged")
    else:
        sidecar(output).write_bytes(b"must remain unchanged")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert (
        runtime.main(["derive", "--parent", str(parent), "--project-root", str(project), "--output", str(output)]) == 2
    )
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_cli_check_returns_failure_for_malformed_json(tmp_path: Path) -> None:
    parent, project = copied_parent(tmp_path)
    output = tmp_path / "runtime.txt"
    write_derived(parent, project, output)
    sidecar(output).write_bytes(b"[]")
    assert runtime.main(["check", "--parent", str(parent), "--project-root", str(project), "--lock", str(output)]) == 2


def test_checker_rejects_changed_derivation_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent, project = copied_parent(tmp_path)
    output = tmp_path / "runtime.txt"
    write_derived(parent, project, output)
    original_digest = runtime.lock.source_digest

    def changed_script_digest(path: Path) -> str:
        return "0" * 64 if path == runtime.SCRIPT_PATH else original_digest(path)

    monkeypatch.setattr(runtime.lock, "source_digest", changed_script_digest)
    with pytest.raises(ValueError):
        runtime.check_runtime_lock(output, parent, project)


def test_cli_derive_rejects_symlink_output_even_if_bytes_match(tmp_path: Path) -> None:
    parent, project = copied_parent(tmp_path)
    existing = tmp_path / "existing.txt"
    write_derived(parent, project, existing)
    output = tmp_path / "runtime.txt"
    try:
        output.symlink_to(existing)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host.")
    original = existing.read_bytes()
    assert (
        runtime.main(["derive", "--parent", str(parent), "--project-root", str(project), "--output", str(output)]) == 2
    )
    assert existing.read_bytes() == original
    assert not sidecar(output).exists()


@pytest.mark.parametrize("extra", [[], ["--exclude", "pip"]])
def test_cli_verify_requires_full_inventory_and_offers_no_exclusions(tmp_path: Path, extra: list[str]) -> None:
    parent, project = copied_parent(tmp_path)
    output = tmp_path / "runtime.txt"
    write_derived(parent, project, output)
    arguments = ["verify", "--parent", str(parent), "--project-root", str(project), "--lock", str(output)]
    if extra:
        arguments += ["--inventory", str(tmp_path / "unused.json"), *extra]
    with pytest.raises(SystemExit) as error:
        runtime.main(arguments)
    assert error.value.code == 2
