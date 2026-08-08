from __future__ import annotations

from typing import Final

UI_REVISION_HEADER: Final = "X-Einvoice-UI-Revision"
_UI_REVISION_PATHS: Final = frozenset(
    {
        "/api/analyze",
        "/api/xml",
        "/api/report",
        "/api/report/pdf",
    }
)
_UI_REVISION_PREFIXES: Final = ("/api/examples/",)


def is_ui_revision_required(path: str) -> bool:
    """Return whether a browser UI request is bound to the current UI revision."""

    return path in _UI_REVISION_PATHS or path.startswith(_UI_REVISION_PREFIXES)


__all__ = ["UI_REVISION_HEADER", "is_ui_revision_required"]
