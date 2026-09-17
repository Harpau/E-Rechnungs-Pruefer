from __future__ import annotations

import pytest

from app.configuration import Settings
from app.validators.kosit import KositValidator

ACCEPT = b'<rep:report xmlns:rep="http://www.xoev.de/de/validator/varl/1"><rep:assessment><rep:accept/></rep:assessment></rep:report>'
REJECT = ACCEPT.replace(b"accept", b"reject")


@pytest.mark.parametrize(("report", "exit_code", "accepted"), [(ACCEPT, 9, True), (REJECT, 0, False)])
def test_detached_kosit_evaluation_keeps_varl_authoritative(report, exit_code, accepted):
    validator = KositValidator(Settings())
    result = validator.evaluate_execution(
        {"configured": True, "problems": []},
        returncode=exit_code,
        stdout=b"",
        stderr=b"",
        report_payload=report,
    )
    assert result["accepted"] is accepted
    assert result["executed"] is True
    assert any(item["id"] == "KOSIT-RESULT-MISMATCH" for item in result["findings"])


def test_detached_execution_without_report_remains_technical_failure():
    result = KositValidator(Settings()).evaluate_execution(
        {"configured": True, "problems": []},
        returncode=1,
        stdout=b"",
        stderr=b"Could not create the Java Virtual Machine",
        report_payload=None,
    )
    assert result["executed"] is False
    assert result["accepted"] is None
    assert result["findings"][0]["id"] == "KOSIT-EXEC"


def test_detached_execution_keeps_complete_report_despite_console_overflow():
    result = KositValidator(Settings()).evaluate_execution(
        {"configured": True, "problems": []},
        returncode=0,
        stdout=b"",
        stderr=b"",
        report_payload=ACCEPT,
        console_overflow=True,
    )
    assert result["accepted"] is True
    assert any(item["id"] == "KOSIT-OUTPUT-TRUNCATED" for item in result["findings"])
