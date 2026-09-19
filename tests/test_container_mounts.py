from __future__ import annotations

import copy
import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Linux Docker mount/stat identities require POSIX APIs")

SPEC = importlib.util.spec_from_file_location(
    "check_container_mounts", Path(__file__).resolve().parents[1] / "scripts/check_container_mounts.py"
)
assert SPEC and SPEC.loader
mounts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mounts)
CONTAINER = "a" * 64
IMAGE = "sha256:" + "b" * 64
INSPECT_HASH = "c" * 64


def evidence() -> tuple[dict, dict, dict]:
    inspect = {
        "Id": CONTAINER,
        "Image": IMAGE,
        "Config": {"User": "appuser"},
        "HostConfig": {"ReadonlyRootfs": True, "NetworkMode": "none"},
    }
    host_files, container_files = {}, {}
    lines = ["1 0 0:42 / / ro - overlay overlay ro"]
    for number, (target, key) in enumerate(mounts.NETWORK_FILES.items(), start=2):
        source = f"/var/lib/docker/containers/{CONTAINER}/{Path(target).name}"
        inspect[key] = source
        facts = {"device": os.makedev(8, 17), "inode": 1000 + number, "mode": stat.S_IFREG | 0o644}
        host_files[target] = facts | {"path": source}
        container_files[target] = facts | {"path": target}
        # Separate filesystem mounted at /var/lib/docker on the host. Its root
        # inside the filesystem does not equal Docker's absolute host path.
        lines.append(f"{number} 1 8:17 /containers/{CONTAINER}/{Path(target).name} {target} rw - ext4 /dev/sdb1 rw")
    binding = {"schema_version": 1, "container_id": CONTAINER, "image_id": IMAGE, "inspect_sha256": INSPECT_HASH}
    host = binding | {
        "role": "host",
        "files": host_files,
        "mountinfo": "1 0 8:17 / /var/lib/docker rw - ext4 /dev/sdb1 rw\n",
    }
    container = binding | {"role": "container", "files": container_files, "mountinfo": "\n".join(lines) + "\n"}
    return inspect, host, container


def test_separate_host_filesystem_passes_by_file_identity_not_root_path() -> None:
    inspect, host, container = evidence()
    report = mounts.check_bindings(inspect, INSPECT_HASH, host, container, CONTAINER, IMAGE)
    assert report["passed"] is True
    assert set(report["bindings"]) == set(mounts.NETWORK_FILES)
    for target, binding in report["bindings"].items():
        assert binding["source"] == inspect[mounts.NETWORK_FILES[target]]
        assert binding["mount_root"] != binding["source"]


@pytest.mark.parametrize(
    "change",
    [
        "inode",
        "device",
        "mount_device",
        "missing",
        "extra",
        "duplicate_mount",
        "missing_mount",
        "symlink",
        "directory",
        "source",
        "container_path",
    ],
)
def test_conflicting_mount_or_file_facts_fail_closed(change: str) -> None:
    inspect, host, container = evidence()
    target = "/etc/hosts"
    if change == "inode":
        container["files"][target]["inode"] += 1
    elif change == "device":
        container["files"][target]["device"] = os.makedev(8, 18)
    elif change == "mount_device":
        container["mountinfo"] = container["mountinfo"].replace("8:17", "8:18")
    elif change == "missing":
        del host["files"][target]
    elif change == "extra":
        container["files"]["/etc/other"] = copy.deepcopy(container["files"][target])
    elif change == "duplicate_mount":
        container["mountinfo"] += container["mountinfo"].splitlines()[1] + "\n"
    elif change == "missing_mount":
        container["mountinfo"] = "\n".join(
            line for line in container["mountinfo"].splitlines() if " /etc/hosts " not in line
        )
    elif change in {"symlink", "directory"}:
        host["files"][target]["mode"] = (stat.S_IFLNK if change == "symlink" else stat.S_IFDIR) | 0o755
    elif change == "source":
        host["files"][target]["path"] += ".unrelated"
    else:
        container["files"][target]["path"] = "/etc/hostname"
    with pytest.raises(mounts.MountError):
        mounts.check_bindings(inspect, INSPECT_HASH, host, container, CONTAINER, IMAGE)


@pytest.mark.parametrize(
    "change",
    ["inspect_hash", "image", "container", "role", "readonly", "network", "user", "inspect_id", "inspect_image"],
)
def test_all_facts_are_bound_to_exact_inspect_container_and_image(change: str) -> None:
    inspect, host, container = evidence()
    if change == "inspect_hash":
        host["inspect_sha256"] = "d" * 64
    elif change == "image":
        container["image_id"] = "sha256:" + "d" * 64
    elif change == "container":
        host["container_id"] = "d" * 64
    elif change == "role":
        host["role"] = "container"
    elif change == "readonly":
        inspect["HostConfig"]["ReadonlyRootfs"] = False
    elif change == "network":
        inspect["HostConfig"]["NetworkMode"] = "bridge"
    elif change == "user":
        inspect["Config"]["User"] = "root"
    elif change == "inspect_id":
        inspect["Id"] = "d" * 64
    else:
        inspect["Image"] = "sha256:" + "d" * 64
    with pytest.raises(mounts.MountError):
        mounts.check_bindings(inspect, INSPECT_HASH, host, container, CONTAINER, IMAGE)


def test_mountinfo_decodes_kernel_path_escapes_without_suffix_fallback() -> None:
    _, _, container = evidence()
    container["mountinfo"] = container["mountinfo"].replace("/containers/", r"/docker\040data/containers/")
    parsed = mounts.network_mounts(container["mountinfo"])
    assert parsed["/etc/hosts"]["root"].startswith("/docker data/")
    with pytest.raises(mounts.MountError):
        mounts.network_mounts(container["mountinfo"].replace(" /etc/hosts ", " /other/hosts "))


def test_readonly_stat_captures_regular_file_and_rejects_symlink_and_fifo(tmp_path: Path) -> None:
    file = tmp_path / "hosts"
    file.write_bytes(b"public synthetic hosts file")
    result = mounts.regular_file_fact(file)
    assert result["inode"] == file.stat().st_ino
    assert result["device"] == file.stat().st_dev
    link = tmp_path / "link"
    try:
        link.symlink_to(file)
    except OSError:
        pytest.skip("Symlinks are unavailable")
    with pytest.raises(mounts.MountError):
        mounts.regular_file_fact(link)
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(mounts.MountError):
            mounts.regular_file_fact(fifo)


def test_cli_preserves_evidence_and_returns_failure_for_mismatched_inode(tmp_path: Path) -> None:
    inspect, host, container = evidence()
    inspection = tmp_path / "inspect.json"
    inspection.write_text(json.dumps([inspect]))
    binding = mounts.digest(inspection.read_bytes())
    host["inspect_sha256"] = container["inspect_sha256"] = binding
    host_path, container_path = tmp_path / "host.json", tmp_path / "container.json"
    host_path.write_text(json.dumps(host))
    container_path.write_text(json.dumps(container))
    output = tmp_path / "result.json"
    args = [
        "check",
        "--inspect",
        str(inspection),
        "--host-facts",
        str(host_path),
        "--container-facts",
        str(container_path),
        "--container-id",
        CONTAINER,
        "--image-id",
        IMAGE,
        "--output",
        str(output),
    ]
    assert mounts.main(args) == 0
    result = json.loads(output.read_text())
    assert result["inputs"]["host_facts_sha256"] == mounts.digest(host_path.read_bytes())
    container["files"]["/etc/hosts"]["inode"] += 1
    container_path.write_text(json.dumps(container))
    assert mounts.main(args) == 1
    assert json.loads(output.read_text())["passed"] is False
