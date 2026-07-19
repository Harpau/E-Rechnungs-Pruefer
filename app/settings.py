from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


for _env_name in (".env", ".env.kosit"):
    _load_env_file(PROJECT_ROOT / _env_name)


def _resolve_path(value: str) -> Path:
    path = Path(value.strip()).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _split_paths(value: str | None) -> tuple[Path, ...]:
    if not value:
        return ()
    # Semicolon is intentionally used on all platforms so Windows drive letters remain intact.
    return tuple(_resolve_path(item) for item in value.split(";") if item.strip())


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _discover_validator_jar() -> Path | None:
    base = PROJECT_ROOT / "vendor" / "kosit" / "validator"
    if not base.is_dir():
        return None
    # Only the standalone artefact is executable with ``java -jar``. The plain
    # validator-<version>.jar is a library and intentionally has no Main-Class.
    candidates = [
        path
        for path in base.rglob("*-standalone.jar")
        if not any(token in path.name.lower() for token in ("sources", "javadoc", "tests"))
    ]
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def _discover_scenarios() -> tuple[Path, ...]:
    base = PROJECT_ROOT / "vendor" / "kosit" / "xrechnung"
    if not base.is_dir():
        return ()
    candidates = [path for path in base.rglob("scenarios.xml") if "src" not in path.parts]
    if not candidates:
        candidates = list(base.rglob("scenarios.xml"))
    if not candidates:
        return ()
    candidates.sort(key=lambda path: (0 if (path.parent / "resources").is_dir() else 1, len(path.parts)))
    return (candidates[0],)


_JAR_FROM_ENV = os.getenv("KOSIT_VALIDATOR_JAR")
_SCENARIOS_FROM_ENV = os.getenv("KOSIT_SCENARIOS")


@dataclass(frozen=True, slots=True)
class Settings:
    max_upload_bytes: int = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    max_technical_rows: int = int(os.getenv("MAX_TECHNICAL_ROWS", "100000"))
    kosit_enabled: bool = _bool_env("KOSIT_ENABLED", True)
    kosit_java_bin: str = os.getenv("KOSIT_JAVA_BIN", "java")
    kosit_validator_jar: Path | None = _resolve_path(_JAR_FROM_ENV) if _JAR_FROM_ENV else _discover_validator_jar()
    kosit_scenarios: tuple[Path, ...] = (
        _split_paths(_SCENARIOS_FROM_ENV) if _SCENARIOS_FROM_ENV else _discover_scenarios()
    )
    kosit_repositories: tuple[Path, ...] = _split_paths(os.getenv("KOSIT_REPOSITORIES"))
    kosit_timeout_seconds: int = int(os.getenv("KOSIT_TIMEOUT_SECONDS", "60"))
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8080"))


settings = Settings()
