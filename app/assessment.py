from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from .findings import ContractModel, Finding, FindingOrigin, FindingSeverity


class OfficialAssessmentStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_REQUESTED = "not-requested"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    INDETERMINATE = "indeterminate"


class InternalAssessmentStatus(StrEnum):
    CLEAR = "clear"
    ATTENTION = "attention"
    ERRORS = "errors"
    NOT_RUN = "not-run"


class ProcessingAssessmentStatus(StrEnum):
    COMPLETE = "complete"
    LIMITED = "limited"
    INCOMPLETE = "incomplete"


class OfficialReportSource(StrEnum):
    FILE = "file"
    STDOUT = "stdout"
    STDERR = "stderr"
    STDERR_FORMAT_ERROR = "stderr-format-error"


class FindingCounts(ContractModel):
    error: int = Field(default=0, ge=0)
    warning: int = Field(default=0, ge=0)
    info: int = Field(default=0, ge=0)

    @classmethod
    def from_findings(cls, findings: list[Finding]) -> FindingCounts:
        return cls(
            error=sum(item.severity is FindingSeverity.ERROR for item in findings),
            warning=sum(item.severity is FindingSeverity.WARNING for item in findings),
            info=sum(item.severity is FindingSeverity.INFO for item in findings),
        )


class InternalAssessment(ContractModel):
    status: InternalAssessmentStatus = InternalAssessmentStatus.NOT_RUN
    executed: bool = False
    summary: str | None = Field(default=None, max_length=4000)
    scope: str | None = Field(default=None, max_length=4000)
    findings: list[Finding] = Field(default_factory=list)
    counts: FindingCounts = Field(default_factory=FindingCounts)

    @model_validator(mode="after")
    def execution_matches_status(self) -> Self:
        if self.status is InternalAssessmentStatus.NOT_RUN and self.executed:
            raise ValueError("internal status 'not-run' requires executed=false")
        if self.status is not InternalAssessmentStatus.NOT_RUN and not self.executed:
            raise ValueError("an executed internal assessment is required for this status")
        if any(item.origin is not FindingOrigin.INTERNAL for item in self.findings):
            raise ValueError("internal assessment accepts only findings with origin='internal'")
        self.counts = FindingCounts.from_findings(self.findings)
        return self


class OfficialAssessment(ContractModel):
    status: OfficialAssessmentStatus = OfficialAssessmentStatus.NOT_REQUESTED
    requested: bool = False
    configured: bool | None = None
    executed: bool = False
    summary: str | None = Field(default=None, max_length=4000)
    exit_code: int | None = None
    report_source: OfficialReportSource | None = None
    raw_report: str | None = None
    technical_output: str | None = Field(default=None, max_length=2_000_000)
    findings: list[Finding] = Field(default_factory=list)
    counts: FindingCounts = Field(default_factory=FindingCounts)

    @model_validator(mode="after")
    def execution_matches_status(self) -> Self:
        if self.status in {OfficialAssessmentStatus.ACCEPTED, OfficialAssessmentStatus.REJECTED}:
            if not self.requested or not self.executed:
                raise ValueError("accepted or rejected requires requested=true and executed=true")
        elif self.status is OfficialAssessmentStatus.NOT_REQUESTED:
            if self.requested or self.executed:
                raise ValueError("not-requested requires requested=false and executed=false")
        elif self.status in {OfficialAssessmentStatus.UNSUPPORTED, OfficialAssessmentStatus.UNAVAILABLE}:
            if not self.requested or self.executed:
                raise ValueError("unsupported or unavailable requires requested=true and executed=false")
        elif self.status is OfficialAssessmentStatus.INDETERMINATE and not self.requested:
            raise ValueError("indeterminate requires requested=true")
        if any(item.origin is not FindingOrigin.OFFICIAL for item in self.findings):
            raise ValueError("official assessment accepts only findings with origin='official'")
        self.counts = FindingCounts.from_findings(self.findings)
        return self


class ProcessingLimitation(ContractModel):
    code: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4000)
    affected_json_pointer: str | None = Field(default=None, max_length=2000)


class ProcessingAssessment(ContractModel):
    status: ProcessingAssessmentStatus = ProcessingAssessmentStatus.INCOMPLETE
    summary: str | None = Field(default=None, max_length=4000)
    limitations: list[ProcessingLimitation] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    counts: FindingCounts = Field(default_factory=FindingCounts)

    @model_validator(mode="after")
    def findings_match_axis(self) -> Self:
        if any(item.origin is not FindingOrigin.PROCESSING for item in self.findings):
            raise ValueError("processing assessment accepts only findings with origin='processing'")
        self.counts = FindingCounts.from_findings(self.findings)
        return self


class Assessment(ContractModel):
    official: OfficialAssessment = Field(default_factory=OfficialAssessment)
    internal: InternalAssessment = Field(default_factory=InternalAssessment)
    processing: ProcessingAssessment = Field(default_factory=ProcessingAssessment)


__all__ = [
    "Assessment",
    "FindingCounts",
    "InternalAssessment",
    "InternalAssessmentStatus",
    "OfficialAssessment",
    "OfficialAssessmentStatus",
    "OfficialReportSource",
    "ProcessingAssessment",
    "ProcessingAssessmentStatus",
    "ProcessingLimitation",
]
