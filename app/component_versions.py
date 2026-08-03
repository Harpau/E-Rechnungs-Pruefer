from __future__ import annotations

from typing import Final

ANALYSIS_SCHEMA_VERSION: Final = 2

KOSIT_COMPONENT_VERSIONS: Final[dict[str, str]] = {
    "validator": "1.6.2",
    "xrechnung": "3.0.2",
    "xrechnung_configuration": "2026-01-31",
    "cen_en16931": "1.3.15",
    "xrechnung_schematron": "2.5.0",
}


__all__ = ["ANALYSIS_SCHEMA_VERSION", "KOSIT_COMPONENT_VERSIONS"]
