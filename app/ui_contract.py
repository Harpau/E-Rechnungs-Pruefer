from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
from pathlib import Path
from typing import Final

from . import __version__
from .component_versions import ANALYSIS_SCHEMA_VERSION
from .ui_contract_rules import UI_REVISION_HEADER, is_ui_revision_required

APP_DIR: Final = Path(__file__).resolve().parent
_UI_REVISION_ASSETS: Final = (
    "templates/index.html",
    "static/app.js",
    "static/styles.css",
)


def _update_digest_component(digest, label: str, value: bytes) -> None:
    label_bytes = label.encode("utf-8")
    digest.update(len(label_bytes).to_bytes(8, "big"))
    digest.update(label_bytes)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def calculate_ui_revision(
    application_version: str,
    analysis_schema_version: int,
    assets: Iterable[tuple[str, bytes]],
) -> str:
    """Return a stable revision for the complete browser/server UI contract."""

    normalized_assets = sorted((name, bytes(content)) for name, content in assets)
    names = [name for name, _content in normalized_assets]
    if len(names) != len(set(names)):
        raise ValueError("UI-Assets müssen eindeutige Namen besitzen.")

    digest = sha256()
    _update_digest_component(digest, "application-version", application_version.encode("utf-8"))
    _update_digest_component(digest, "analysis-schema-version", str(analysis_schema_version).encode("ascii"))
    for name, content in normalized_assets:
        _update_digest_component(digest, f"asset:{name}", content)
    return digest.hexdigest()


UI_REVISION: Final = calculate_ui_revision(
    __version__,
    ANALYSIS_SCHEMA_VERSION,
    ((name, (APP_DIR / name).read_bytes()) for name in _UI_REVISION_ASSETS),
)
UI_STATIC_PREFIX: Final = f"/static/{UI_REVISION}"
UI_ENTRY_PATH: Final = f"/?ui={UI_REVISION}"

__all__ = [
    "UI_ENTRY_PATH",
    "UI_REVISION",
    "UI_REVISION_HEADER",
    "UI_STATIC_PREFIX",
    "calculate_ui_revision",
    "is_ui_revision_required",
]
