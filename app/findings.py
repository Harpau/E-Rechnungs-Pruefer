from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    """Base class for the public schema-2 contract.

    Public contract objects are deliberately closed. Parser or validator data
    must be mapped intentionally instead of leaking accidental dictionary keys
    into the API.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class FindingOrigin(StrEnum):
    INTERNAL = "internal"
    OFFICIAL = "official"
    PROCESSING = "processing"


class FindingRuleClass(StrEnum):
    CORE_PRECHECK = "core_precheck"
    PROFILE_PRECHECK = "profile_precheck"
    PLAUSIBILITY = "plausibility"
    PROCESSING = "processing"
    OFFICIAL = "official"


class FindingSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class OccurrenceScope(StrEnum):
    DOCUMENT = "document"
    PROFILE = "profile"
    PARTY = "party"
    PERIOD = "period"
    REFERENCE = "reference"
    LINE = "line"
    ALLOWANCE_CHARGE = "allowance-charge"
    TAX = "tax"
    TOTAL = "total"
    PAYMENT = "payment"
    SOURCE = "source"
    TECHNICAL = "technical"
    RUNTIME = "runtime"


class EvidenceDataType(StrEnum):
    TEXT = "text"
    CODE = "code"
    DATE = "date"
    DATETIME = "datetime"
    DECIMAL = "decimal"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    IDENTIFIER = "identifier"
    COUNT = "count"


class FindingRule(ContractModel):
    id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=500)
    message: str = Field(min_length=1, max_length=4000)
    source: str | None = Field(default=None, max_length=500)
    reference: str | None = Field(default=None, max_length=1000)
    profile: str | None = Field(default=None, max_length=1000)
    version: str | None = Field(default=None, max_length=200)


class SemanticReference(ContractModel):
    id: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=500)


class FindingOccurrence(ContractModel):
    scope: OccurrenceScope
    index: int | None = Field(default=None, ge=0)
    identifier: str | None = Field(default=None, max_length=1000)
    json_pointer: str | None = Field(default=None, max_length=2000)


class XmlLocation(ContractModel):
    path: str | None = Field(default=None, min_length=1, max_length=4000)
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_known_coordinate(self) -> Self:
        if self.path is None and self.line is None and self.column is None:
            raise ValueError("xml_location requires path, line or column")
        return self


class FindingEvidence(ContractModel):
    value: str = Field(max_length=4000)
    data_type: EvidenceDataType = EvidenceDataType.TEXT
    unit: str | None = Field(default=None, max_length=100)


class Finding(ContractModel):
    origin: FindingOrigin
    rule_class: FindingRuleClass
    severity: FindingSeverity
    rule: FindingRule
    semantic_references: list[SemanticReference] = Field(default_factory=list)
    occurrence: FindingOccurrence | None = None
    xml_location: XmlLocation | None = None
    actual: FindingEvidence | None = None
    expected: FindingEvidence | None = None


__all__ = [
    "ContractModel",
    "EvidenceDataType",
    "Finding",
    "FindingEvidence",
    "FindingOccurrence",
    "FindingOrigin",
    "FindingRule",
    "FindingRuleClass",
    "FindingSeverity",
    "OccurrenceScope",
    "SemanticReference",
    "XmlLocation",
]
