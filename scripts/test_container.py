#!/usr/bin/env python3
"""Exercise a running product over loopback HTTP with repository demo invoices."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SmokeError(RuntimeError):
    """The running image did not demonstrate the required HTTP behavior."""


def validate_base_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SmokeError("Der HTTP-Smoke benötigt eine direkte Loopback-URL.")
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        raise SmokeError("Der lokale HTTP-Smoke folgt keinen Weiterleitungen.")


def request(
    url: str, payload: bytes | None = None, content_type: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": content_type} if content_type else {}
    req = urllib.request.Request(url, data=payload, headers=headers)
    # No proxy or redirect may turn the local synthetic test into a remote upload.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=180) as response:
        return response.status, {key.lower(): value for key, value in response.headers.items()}, response.read()


def multipart(xml: bytes, *, official: bool | None) -> tuple[str, bytes]:
    boundary = f"einvoice-smoke-{uuid4().hex}"
    options = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="official"\r\n\r\n{str(official).lower()}\r\n'
        if official is not None
        else ""
    )
    payload = (
        (
            options + f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic-demo.xml"\r\n'
            "Content-Type: application/xml\r\n\r\n"
        ).encode()
        + xml
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return f"multipart/form-data; boundary={boundary}", payload


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def check_pdf(payload: bytes, document_id: str) -> int:
    _require(payload.startswith(b"%PDF-") and payload.rstrip().endswith(b"%%EOF"), "PDF ist unvollständig.")
    pdf = PdfReader(BytesIO(payload))
    text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    _require(bool(pdf.pages) and document_id in text, "PDF enthält nicht die synthetische Belegnummer.")
    return len(pdf.pages)


def _health(base_url: str) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while True:
        try:
            status, _, payload = request(f"{base_url}/api/health")
            _require(status == 200, "Healthcheck liefert nicht HTTP 200.")
            health = json.loads(payload)
            _require(health.get("status") == "ok", "Healthcheck ist nicht erfolgreich.")
            return health
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise SmokeError("Anwendung ist nach 60 Sekunden nicht erreichbar.") from None
            time.sleep(1)


def run_smoke(base_url: str, *, expect_kosit: bool) -> dict[str, Any]:
    base_url = validate_base_url(base_url)
    health = _health(base_url)
    version = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    _require(health.get("version") == version and health.get("analysis_schema_version") == 2, "Falsche Produktversion.")
    if expect_kosit:
        _require(health["kosit"]["configured"] is True, "Gebundene KoSIT-Komponenten sind nicht konfiguriert.")
    evidence: dict[str, Any] = {"schema_version": 1, "passed": False, "version": version, "health": health, "cases": []}
    for syntax in ("cii", "ubl"):
        xml = (PROJECT_ROOT / "app" / "examples" / f"{syntax}-rechnung-demo.xml").read_bytes()
        content_type, payload = multipart(xml, official=expect_kosit)
        status, _, body = request(f"{base_url}/api/analyze", payload, content_type)
        _require(status == 200, f"{syntax}: Analyse liefert nicht HTTP 200.")
        analysis = json.loads(body)
        document_id = f"{syntax.upper()}-DEMO-1"
        _require(
            analysis.get("schema_version") == 2
            and analysis["document"]["id"] == document_id
            and analysis["assessment"]["processing"]["status"] == "complete",
            f"{syntax}: Analyse ist unvollständig oder gehört zu einem anderen Dokument.",
        )
        official = analysis["assessment"]["official"]
        if expect_kosit:
            _require(
                official["executed"] is True and official["status"] == "accepted", f"{syntax}: KoSIT akzeptiert nicht."
            )
        else:
            _require(official["status"] == "not-requested", f"{syntax}: Unangeforderte KoSIT-Prüfung.")
        xml_type, xml_payload = multipart(xml, official=None)
        status, headers, exported = request(f"{base_url}/api/xml", xml_payload, xml_type)
        _require(status == 200 and exported == xml, f"{syntax}: XML-Export ist nicht byteidentisch.")
        _require("application/xml" in headers.get("content-type", ""), f"{syntax}: Falscher XML-Medientyp.")
        pdf_type, pdf_payload = multipart(xml, official=False)
        status, headers, pdf = request(f"{base_url}/api/report/pdf", pdf_payload, pdf_type)
        _require(
            status == 200 and headers.get("content-type") == "application/pdf", f"{syntax}: PDF-Export fehlgeschlagen."
        )
        pages = check_pdf(pdf, document_id)
        evidence["cases"].append(
            {
                "syntax": syntax,
                "passed": True,
                "official_status": official["status"],
                "xml_bytes_preserved": True,
                "xml_sha256": hashlib.sha256(xml).hexdigest(),
                "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                "pdf_pages": pages,
            }
        )
    evidence["passed"] = True
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--expect-kosit", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise SmokeError(f"Nachweisziel existiert bereits: {args.output}")
    try:
        evidence = run_smoke(args.base_url, expect_kosit=args.expect_kosit)
    except (SmokeError, OSError, ValueError, KeyError) as exc:
        evidence = {"schema_version": 1, "passed": False, "error": str(exc)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n")
    print(f"HTTP-Smoke: {'bestanden' if evidence['passed'] else 'fehlgeschlagen'} ({args.output})")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
