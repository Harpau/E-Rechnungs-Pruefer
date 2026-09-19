from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.main import app
from scripts import test_container as smoke


@pytest.mark.parametrize(
    "url", ["https://example.test", "http://example.test", "http://127.0.0.1@evil.test", "file:///tmp"]
)
def test_smoke_never_sends_invoices_to_remote_targets(url: str) -> None:
    with pytest.raises(smoke.SmokeError, match="Loopback"):
        smoke.validate_base_url(url)


def test_http_smoke_checks_real_endpoints_through_the_product_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(app) as client:

        def request(url: str, payload: bytes | None = None, content_type: str | None = None):
            response = client.request(
                "GET" if payload is None else "POST",
                url,
                content=payload,
                headers={"Content-Type": content_type} if content_type else {},
            )
            return response.status_code, dict(response.headers), response.content

        monkeypatch.setattr(smoke, "request", request)
        evidence = smoke.run_smoke("http://127.0.0.1:8080", expect_kosit=False)
    assert evidence["passed"] is True
    assert {case["syntax"] for case in evidence["cases"]} == {"cii", "ubl"}
    assert all(case["xml_bytes_preserved"] and case["pdf_pages"] > 0 for case in evidence["cases"])


def test_pdf_check_requires_the_actual_invoice_content() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    output = BytesIO()
    writer.write(output)
    with pytest.raises(smoke.SmokeError, match="Belegnummer"):
        smoke.check_pdf(output.getvalue(), "SYNTHETIC-EXPECTED-ID")


def test_multipart_preserves_the_uploaded_xml_bytes() -> None:
    original = b"<?xml version='1.0'?>\r\n<Invoice>synthetic</Invoice>\r\n"
    content_type, payload = smoke.multipart(original, official=False)
    assert content_type.startswith("multipart/form-data; boundary=")
    assert original in payload
    assert b'name="official"\r\n\r\nfalse\r\n' in payload


def test_xml_export_multipart_has_only_the_file_field() -> None:
    original = b"<Invoice>synthetic-export</Invoice>"
    content_type, payload = smoke.multipart(original, official=None)
    assert content_type.startswith("multipart/form-data; boundary=")
    assert original in payload
    assert b'name="official"' not in payload
    assert payload.count(b"Content-Disposition:") == 1


def test_container_workflow_reads_tmpfs_evidence_and_scans_compiled_java() -> None:
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/docker.yml").read_text()
    # Docker's archive API does not expose tmpfs contents; execute Python in the
    # live shell-free container. Only setup's ordinary /app file uses docker cp.
    copies = [line.strip() for line in workflow.splitlines() if "docker cp" in line]
    assert len(copies) == 1 and ":/app/.env.kosit " in copies[0]
    for filename in ("python-inventory.json", "http-smoke.json", "kosit-smoke.json", "runtime-smoke.json"):
        assert f"Path('/tmp/{filename}').read_text()" in workflow
    assert "trivy rootfs --scanners vuln" in workflow
    assert "trivy fs --scanners vuln" not in workflow
