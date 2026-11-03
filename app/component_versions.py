from __future__ import annotations

from typing import Final

ANALYSIS_SCHEMA_VERSION: Final = 2

KOSIT_COMPONENT_VERSIONS: Final[dict[str, str]] = {
    "validator": "1.6.3",
    "xrechnung": "3.0.2",
    "xrechnung_configuration": "2026-08-31",
    "cen_en16931": "1.3.16",
    "xrechnung_schematron": "2.6.0",
    "facturx_extended": "1.09.2",
    "facturx_extended_source": "fnfempe/France_RFE v1.4.0.03 a63e6b5",
}


__all__ = ["ANALYSIS_SCHEMA_VERSION", "KOSIT_COMPONENT_VERSIONS"]
