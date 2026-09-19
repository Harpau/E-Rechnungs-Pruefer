from __future__ import annotations

import copy
import subprocess
import sys

import pytest

from app.processing.result import OperationLimits, validate_result_metadata


def _metadata():
    return {"status_code": 200, "media_type": "application/json", "headers": {}, "body_size": 2}


def test_parent_metadata_import_has_no_parser_renderer_or_configuration_side_effects():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from app.processing.result import validate_result_metadata
assert not {'app.settings', 'app.main', 'pypdf', 'reportlab', 'jinja2', 'lxml'} & set(sys.modules)
""",
        ],
        check=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status_code", True),
        ("status_code", 201),
        ("body_size", True),
        ("body_size", 0),
        ("body_size", 129 * 1024 * 1024),
        ("body_size", "2"),
        ("media_type", "text/html"),
        ("headers", {"Set-Cookie": "secret"}),
        ("unknown", "extra"),
    ],
)
def test_parent_metadata_rejects_invalid_envelope(key, value):
    metadata = _metadata()
    metadata[key] = value
    with pytest.raises(ValueError):
        validate_result_metadata(metadata, operation="analyze")


@pytest.mark.parametrize(
    "filename", ["../invoice.xml", "bad\r\nheader.xml", ".hidden.xml", "secret.txt", "a" * 121 + ".xml"]
)
def test_xml_result_download_name_is_not_arbitrary(filename):
    metadata = {
        **_metadata(),
        "media_type": "application/xml",
        "headers": {"Content-Disposition": f'attachment; filename="{filename}"'},
    }
    with pytest.raises(ValueError):
        validate_result_metadata(metadata, operation="export_xml")


def test_parent_metadata_detaches_validated_header_mapping():
    metadata = _metadata()
    validated = validate_result_metadata(metadata, operation="analyze")
    metadata["headers"]["Set-Cookie"] = "secret"
    assert validated["headers"] == {}


def test_parent_binds_scope_and_checks_every_report_status():
    metadata = {
        "status_code": 200,
        "body_size": 10,
        "media_type": "text/html",
        "headers": {
            "Content-Disposition": 'inline; filename="E-Rechnungs-Pruefbericht.html"',
            "X-Einvoice-Analysis-Schema": "2",
            "X-Einvoice-Syntax": "UBL",
            "X-Einvoice-Conformity-Status": "not-requested",
            "X-Einvoice-Internal-Status": "clear",
            "X-Einvoice-Processing-Status": "complete",
            "X-Einvoice-Report-Scope": "readable",
        },
    }
    assert validate_result_metadata(metadata, operation="report_html", expected_scope="readable") == metadata
    with pytest.raises(ValueError):
        validate_result_metadata(metadata, operation="report_html", expected_scope="complete")
    for key in metadata["headers"]:
        modified = copy.deepcopy(metadata)
        modified["headers"][key] = "untrusted"
        with pytest.raises(ValueError):
            validate_result_metadata(modified, operation="report_html")


@pytest.mark.parametrize("value", [True, 0, -1, 2**31])
def test_invalid_operation_budget(value):
    with pytest.raises(ValueError):
        OperationLimits(json_bytes=value)
