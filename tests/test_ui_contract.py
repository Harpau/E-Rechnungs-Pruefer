from __future__ import annotations

import re
from pathlib import Path

from app import __version__
from app.component_versions import ANALYSIS_SCHEMA_VERSION
from app.ui_contract import (
    UI_REVISION,
    UI_REVISION_HEADER,
    UI_STATIC_PREFIX,
    calculate_ui_revision,
    is_ui_revision_required,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_ui_revision_is_deterministic_and_sensitive_to_every_contract_input() -> None:
    assets = (
        ("templates/index.html", b"<html></html>"),
        ("static/app.js", b"'use strict';"),
        ("static/styles.css", b"body {}"),
    )
    revision = calculate_ui_revision("2.0.1", 2, assets)

    assert re.fullmatch(r"[0-9a-f]{64}", revision)
    assert revision == calculate_ui_revision("2.0.1", 2, reversed(assets))
    assert revision != calculate_ui_revision("2.0.2", 2, assets)
    assert revision != calculate_ui_revision("2.0.1", 3, assets)

    for index, (name, content) in enumerate(assets):
        changed = list(assets)
        changed[index] = (name, content + b"\n")
        assert revision != calculate_ui_revision("2.0.1", 2, changed)


def test_current_ui_revision_covers_the_shipped_html_javascript_and_css() -> None:
    assets = (
        ("templates/index.html", (PROJECT_ROOT / "app/templates/index.html").read_bytes()),
        ("static/app.js", (PROJECT_ROOT / "app/static/app.js").read_bytes()),
        ("static/styles.css", (PROJECT_ROOT / "app/static/styles.css").read_bytes()),
    )

    assert UI_REVISION == calculate_ui_revision(__version__, ANALYSIS_SCHEMA_VERSION, assets)
    assert UI_STATIC_PREFIX == f"/static/{UI_REVISION}"
    assert UI_REVISION_HEADER == "X-Einvoice-UI-Revision"


def test_only_browser_ui_api_routes_require_the_ui_revision() -> None:
    for path in (
        "/api/analyze",
        "/api/xml",
        "/api/report",
        "/api/report/pdf",
        "/api/examples/cii",
        "/api/examples/ubl",
    ):
        assert is_ui_revision_required(path)

    for path in ("/", "/api/health", "/api/docs", "/api/action", "/static/app.js"):
        assert not is_ui_revision_required(path)
