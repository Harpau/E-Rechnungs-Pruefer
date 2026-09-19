from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from io import BytesIO

import pytest
from pypdf import PdfReader

from app.api_models import AnalysisResponse
from app.configuration import Settings
from app.processing.operations import OperationLimits, ProcessingLimitError, execute_operation


def test_operations_import_does_not_load_webserver_or_env():
    script = """
import sys
from app.processing.operations import execute_operation
assert 'app.main' not in sys.modules
assert 'app.settings' not in sys.modules
"""
    subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, MAX_UPLOAD_BYTES="invalid-worker-env-trap"),
        check=True,
        timeout=15,
    )


@pytest.mark.parametrize("fixture", ["cii_path", "ubl_path", "ubl_credit_note_path"])
def test_analysis_operation_serializes_valid_schema_two(request, fixture):
    path = request.getfixturevalue(fixture)
    result = execute_operation("analyze", path.read_bytes(), path.name, app_settings=Settings(), official=False)
    model = AnalysisResponse.model_validate_json(result.body)
    assert model.schema_version == 2
    assert result.media_type == "application/json"
    assert not result.headers


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32"])
def test_export_operation_preserves_original_bytes_without_xml_validation(encoding, pdf_bytes_factory):
    payload = '<?xml version="1.0"?><!DOCTYPE invoice><invoice>\r\n</invoice>'.encode(encoding)
    for data, name in [(payload, "invoice.xml"), (pdf_bytes_factory(("factur-x.xml", payload)), "invoice.pdf")]:
        result = execute_operation("export_xml", data, name, app_settings=Settings())
        assert result.body == payload
        assert result.media_type == "application/xml"
        assert result.headers["Content-Disposition"].endswith('.xml"')


@pytest.mark.parametrize("operation", ["report_html", "report_pdf"])
def test_report_operations_render_complete_schema_two_without_main(operation, ubl_path):
    result = execute_operation(
        operation, ubl_path.read_bytes(), ubl_path.name, app_settings=Settings(), official=False, scope="complete"
    )
    assert result.headers["X-Einvoice-Analysis-Schema"] == "2"
    assert result.headers["X-Einvoice-Report-Scope"] == "complete"
    assert len(result.headers) == 7
    if operation == "report_pdf":
        assert len(PdfReader(BytesIO(result.body)).pages) > 0
    else:
        assert b"<!doctype html>" in result.body.lower()


@pytest.mark.parametrize("operation", ["analyze", "export_xml", "report_html", "report_pdf"])
def test_result_cap_is_explicit_error_never_truncation(operation, cii_path):
    with pytest.raises(ProcessingLimitError):
        execute_operation(
            operation,
            cii_path.read_bytes(),
            cii_path.name,
            app_settings=Settings(),
            official=False,
            limits=OperationLimits(json_bytes=1, html_bytes=1, pdf_bytes=1, xml_bytes=1),
        )


def test_worker_uses_injected_official_validator_only(cii_path):
    calls = []

    def validate(data, filename):
        calls.append((data, filename))
        return {
            "configured": True,
            "problems": [],
            "executed": True,
            "accepted": True,
            "exit_code": 0,
            "summary": "Synthetische Annahme",
            "findings": [],
            "raw_report": None,
        }

    result = execute_operation(
        "analyze",
        cii_path.read_bytes(),
        cii_path.name,
        app_settings=Settings(),
        official_validator=validate,
    )
    assert calls == [(cii_path.read_bytes(), cii_path.name)]
    assert json.loads(result.body)["assessment"]["official"]["status"] == "accepted"


def test_worker_has_no_direct_java_fallback(cii_path):
    with pytest.raises(RuntimeError, match="KoSIT"):
        execute_operation("analyze", cii_path.read_bytes(), cii_path.name, app_settings=Settings())


@pytest.mark.parametrize(("operation", "scope"), [("unknown", "readable"), ("analyze", "bogus")])
def test_operations_fail_closed_on_unknown_operation_or_scope(operation, scope, cii_path):
    with pytest.raises(ValueError):
        execute_operation(operation, cii_path.read_bytes(), cii_path.name, app_settings=Settings(), scope=scope)


@pytest.mark.parametrize("scope", ["readable", "complete"])
def test_worker_html_preserves_existing_template_bytes(monkeypatch, cii_path, scope):
    from app import __version__, main
    from app.analyzer import analyze_bytes
    from app.processing import operations
    from app.report_presentation import build_report_presentation

    instant = datetime(2026, 1, 2, 3, 4, 5).astimezone()
    analysis = analyze_bytes(
        cii_path.read_bytes(), cii_path.name, run_official_validation=False, app_settings=Settings()
    )

    class Clock:
        @staticmethod
        def now():
            return instant

    monkeypatch.setattr(operations, "datetime", Clock)
    monkeypatch.setattr(operations, "analyze_bytes", lambda *args, **kwargs: analysis)
    expected = (
        main.templates.env.get_template("report.html")
        .render(
            analysis=analysis,
            presentation=build_report_presentation(analysis, scope=scope),
            report_scope=scope,
            generated_at=instant.strftime("%d.%m.%Y %H:%M:%S %Z"),
            version=__version__,
        )
        .encode()
    )
    result = execute_operation(
        "report_html",
        cii_path.read_bytes(),
        cii_path.name,
        app_settings=Settings(),
        official=False,
        scope=scope,
    )
    assert result.body == expected
