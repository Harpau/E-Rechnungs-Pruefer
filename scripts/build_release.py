#!/usr/bin/env python3
"""Build wheel, source distribution and a sanitized Codex/GitHub repository ZIP."""

from __future__ import annotations

import shutil
import stat
import subprocess
import sys
import zipfile
from hashlib import sha256
from pathlib import Path, PurePosixPath

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from verify_version import verify  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = PROJECT_ROOT / "dist"
EXCLUDED_PARTS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    ".cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "local-data",
    "reports",
    "runtime",
    "vendor",
    "venv",
}
EXCLUDED_NAMES = {
    ".coverage",
    ".env",
    ".env.kosit",
    "coverage.xml",
    "Thumbs.db",
    ".DS_Store",
}
SAFE_XML_ROOTS = {
    PurePosixPath("app/examples"),
    PurePosixPath("tests/fixtures"),
}
SENSITIVE_SUFFIXES = {".pdf", ".p12", ".pfx", ".pem", ".key"}


def _is_below(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path == parent or parent in path.parents


def should_include(relative: Path) -> bool:
    posix = PurePosixPath(relative.as_posix())
    if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in posix.parts):
        return False
    if relative.name in EXCLUDED_NAMES:
        return False
    if relative.suffix.lower() in SENSITIVE_SUFFIXES:
        return False
    if relative.suffix.lower() == ".xml" and not any(_is_below(posix, root) for root in SAFE_XML_ROOTS):
        return False
    return True


def repository_files() -> list[Path]:
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(PROJECT_ROOT)
        if should_include(relative):
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(PROJECT_ROOT).as_posix())


def create_repository_zip(version: str) -> Path:
    output = DIST_DIR / f"E-Rechnungs-Pruefer-{version}-Codex-GitHub.zip"
    root_name = f"E-Rechnungs-Pruefer-{version}"
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in repository_files():
            relative = path.relative_to(PROJECT_ROOT)
            archive_name = PurePosixPath(root_name) / PurePosixPath(relative.as_posix())
            info = zipfile.ZipInfo.from_file(path, archive_name.as_posix())
            mode = path.stat().st_mode
            if mode & stat.S_IXUSR:
                info.external_attr = (0o100755 & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return output


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(*command: str) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def main() -> int:
    version = verify()
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
    DIST_DIR.mkdir(parents=True)

    run(sys.executable, "-m", "build", "--outdir", str(DIST_DIR))
    distributions = sorted(DIST_DIR.glob("*.whl")) + sorted(DIST_DIR.glob("*.tar.gz"))
    run(sys.executable, "-m", "twine", "check", *(str(path) for path in distributions))

    repository_zip = create_repository_zip(version)
    artefacts = [repository_zip, *distributions]
    checksum_path = DIST_DIR / f"E-Rechnungs-Pruefer-{version}-SHA256SUMS.txt"
    checksum_path.write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in artefacts),
        encoding="utf-8",
    )

    print("\nErzeugte Artefakte:")
    for path in [*artefacts, checksum_path]:
        print(f"- {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
