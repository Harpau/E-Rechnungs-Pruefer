from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from typing import Any

import pytest

import app.source as source_module
from app.analyzer import analyze_bytes
from app.settings import settings
from app.source import extract_source
from app.xml_utils import InvoiceInputError


def test_pdf_with_embedded_xml_is_supported(ubl_path, pdf_bytes_factory):
    payload = ubl_path.read_bytes()
    pdf = pdf_bytes_factory(("factur-x.xml", payload))

    result = analyze_bytes(
        pdf,
        "hybrid-rechnung.pdf",
        "application/pdf",
        run_official_validation=False,
    )

    assert result["document"]["id"] == "UBL-DEMO-1"
    assert result["source"]["size"] == len(pdf)
    assert result["source"]["sha256"] == sha256(pdf).hexdigest()
    assert result["source"]["xml_size"] == len(payload)
    assert result["source"]["xml_sha256"] == sha256(payload).hexdigest()
    assert result["source"]["container"] == {
        "type": "PDF mit eingebetteter XML",
        "page_count": 1,
        "selected_attachment": "factur-x.xml",
        "attachment_count": 1,
    }
    assert result["source"]["attachments"] == [
        {
            "name": "factur-x.xml",
            "size": len(payload),
            "sha256": sha256(payload).hexdigest(),
            "is_xml": True,
        }
    ]


def test_extracted_pdf_xml_preserves_original_bytes_and_metadata(ubl_path, pdf_bytes_factory):
    payload = ubl_path.read_bytes()
    note = b"Synthetic attachment without invoice data."
    pdf = pdf_bytes_factory(
        ("notes.txt", note),
        ("FACTUR-X.XML", payload),
    )

    source = extract_source(pdf, "../hybrid-rechnung.pdf", "application/pdf")

    assert source.xml_bytes == payload
    assert source.xml_filename == "FACTUR-X.XML"
    assert source.original_filename == "hybrid-rechnung.pdf"
    assert source.original_media_type == "application/pdf"
    assert source.original_size == len(pdf)
    assert source.original_sha256 == sha256(pdf).hexdigest()
    assert source.container == {
        "type": "PDF mit eingebetteter XML",
        "page_count": 1,
        "selected_attachment": "FACTUR-X.XML",
        "attachment_count": 2,
    }
    attachments = {row["name"]: row for row in source.attachments}
    assert attachments == {
        "notes.txt": {
            "name": "notes.txt",
            "size": len(note),
            "sha256": sha256(note).hexdigest(),
            "is_xml": False,
        },
        "FACTUR-X.XML": {
            "name": "FACTUR-X.XML",
            "size": len(payload),
            "sha256": sha256(payload).hexdigest(),
            "is_xml": True,
        },
    }


def test_visual_pdf_without_attachments_is_rejected(pdf_bytes_factory):
    pdf = pdf_bytes_factory()

    with pytest.raises(InvoiceInputError, match="keine erkennbare eingebettete XML-Rechnung"):
        extract_source(pdf, "sichtrechnung.pdf", "application/pdf")


def test_pdf_with_only_non_xml_attachment_is_rejected(pdf_bytes_factory):
    pdf = pdf_bytes_factory(("hinweis.txt", b"Nur ein synthetischer Hinweis."))

    with pytest.raises(InvoiceInputError, match="reine Sicht-PDF"):
        extract_source(pdf, "sichtrechnung-mit-anlage.pdf", "application/pdf")


def test_password_protected_pdf_is_rejected(ubl_path, pdf_bytes_factory):
    pdf = pdf_bytes_factory(("invoice.xml", ubl_path.read_bytes()), password="secret")

    with pytest.raises(InvoiceInputError, match="Kennwortgeschützte PDF-Dateien"):
        extract_source(pdf, "geschuetzt.pdf", "application/pdf")


def test_pdf_encrypted_with_empty_password_is_supported(ubl_path, pdf_bytes_factory):
    payload = ubl_path.read_bytes()
    pdf = pdf_bytes_factory(("invoice.xml", payload), password="")

    source = extract_source(pdf, "leer-verschluesselt.pdf", "application/pdf")

    assert source.xml_bytes == payload
    assert source.xml_filename == "invoice.xml"


def test_known_attachment_name_priority_is_case_insensitive(ubl_path, pdf_bytes_factory):
    preferred = ubl_path.read_bytes()
    pdf = pdf_bytes_factory(
        ("custom.xml", b"<synthetic-generic-invoice />"),
        ("XRECHNUNG.XML", b"<synthetic-xrechnung-invoice />"),
        ("FACTUR-X.XML", preferred),
    )

    source = extract_source(pdf, "mehrere-anlagen.pdf", "application/pdf")

    assert source.xml_filename == "FACTUR-X.XML"
    assert source.xml_bytes == preferred
    assert source.container["attachment_count"] == 3


def test_same_named_candidates_with_different_content_are_rejected(pdf_bytes_factory):
    pdf = pdf_bytes_factory(
        ("invoice.xml", b"<first-synthetic-invoice />"),
        ("invoice.xml", b"<second-synthetic-invoice />"),
    )

    with pytest.raises(InvoiceInputError, match="mehrere gleichnamige XML-Rechnungskandidaten"):
        extract_source(pdf, "mehrdeutig.pdf", "application/pdf")


def test_same_named_candidates_with_identical_content_are_not_ambiguous(pdf_bytes_factory):
    payload = b"<synthetic-invoice />"
    pdf = pdf_bytes_factory(
        ("invoice.xml", payload),
        ("invoice.xml", payload),
    )

    source = extract_source(pdf, "duplikat.pdf", "application/pdf")

    assert source.xml_bytes == payload
    assert [row["name"] for row in source.attachments] == ["invoice.xml (1)", "invoice.xml (2)"]


def test_malformed_preferred_xml_does_not_fall_back_to_valid_candidate(ubl_path, pdf_bytes_factory):
    pdf = pdf_bytes_factory(
        ("invoice.xml", ubl_path.read_bytes()),
        ("factur-x.xml", b"<Invoice>"),
    )

    with pytest.raises(InvoiceInputError, match="nicht wohlgeformt"):
        analyze_bytes(
            pdf,
            "fehlerhafte-bevorzugte-xml.pdf",
            "application/pdf",
            run_official_validation=False,
        )


def test_non_xml_preferred_candidate_does_not_fall_back_to_valid_candidate(ubl_path, pdf_bytes_factory):
    pdf = pdf_bytes_factory(
        ("invoice.xml", ubl_path.read_bytes()),
        ("factur-x.xml", b"not xml"),
    )

    with pytest.raises(InvoiceInputError, match="bevorzugte eingebettete Rechnungskandidat"):
        extract_source(pdf, "nicht-xml-bevorzugt.pdf", "application/pdf")


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE Invoice SYSTEM "synthetic.dtd"><Invoice />',
        b'<Invoice><!ENTITY synthetic "value"></Invoice>',
    ],
    ids=["doctype", "entity"],
)
def test_forbidden_xml_declarations_in_pdf_are_rejected(payload, pdf_bytes_factory):
    pdf = pdf_bytes_factory(("invoice.xml", payload))

    with pytest.raises(InvoiceInputError, match="DTD- oder ENTITY-Deklarationen"):
        analyze_bytes(
            pdf,
            "unsichere-xml.pdf",
            "application/pdf",
            run_official_validation=False,
        )


def test_damaged_pdf_is_reported_as_input_error():
    damaged_pdf = b"%PDF-1.7\nsynthetic but damaged"

    with pytest.raises(InvoiceInputError, match="PDF-Datei konnte nicht gelesen werden"):
        extract_source(damaged_pdf, "beschaedigt.pdf", "application/pdf")


def test_attachment_access_error_is_reported_as_input_error(monkeypatch):
    class BrokenAttachmentReader:
        is_encrypted = False

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        @property
        def attachment_list(self):
            raise RuntimeError("synthetisch beschädigter Anhangsbaum")

    monkeypatch.setattr(source_module, "PdfReader", BrokenAttachmentReader)

    with pytest.raises(InvoiceInputError, match="Eingebettete PDF-Dateien konnten nicht ausgelesen werden"):
        extract_source(b"%PDF-1.7\n", "defekter-anhang.pdf", "application/pdf")


def test_page_access_error_is_reported_as_input_error(monkeypatch):
    class SyntheticEmbeddedFile:
        name = "invoice.xml"
        alternative_name = None
        size = None
        content = b"<synthetic-invoice />"

    class BrokenPageReader:
        is_encrypted = False
        attachment_list = [SyntheticEmbeddedFile()]

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        @property
        def pages(self):
            raise RuntimeError("synthetisch beschädigter Seitenbaum")

    monkeypatch.setattr(source_module, "PdfReader", BrokenPageReader)

    with pytest.raises(InvoiceInputError, match="Eingebettete PDF-Dateien konnten nicht ausgelesen werden"):
        extract_source(b"%PDF-1.7\n", "defekte-seiten.pdf", "application/pdf")


def test_alternative_attachment_name_is_used_once(monkeypatch):
    class SyntheticEmbeddedFile:
        name = "name-tree-key"
        alternative_name = "invoice.xml"
        size = None
        content = b"<synthetic-invoice />"

    class SyntheticReader:
        is_encrypted = False
        attachment_list = [SyntheticEmbeddedFile()]
        pages = [object()]

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    monkeypatch.setattr(source_module, "PdfReader", SyntheticReader)

    source = extract_source(b"%PDF-1.7\n", "alternativer-name.pdf", "application/pdf")

    assert source.xml_filename == "invoice.xml"
    assert source.container["attachment_count"] == 1
    assert [row["name"] for row in source.attachments] == ["invoice.xml"]


def test_declared_attachment_size_is_checked_before_content(monkeypatch):
    class OversizedEmbeddedFile:
        name = "invoice.xml"
        alternative_name = None
        size = 11

        @property
        def content(self):
            raise AssertionError("content must not be decoded")

    class SyntheticReader:
        is_encrypted = False
        attachment_list = [OversizedEmbeddedFile()]
        pages = [object()]

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

    monkeypatch.setattr(source_module, "PdfReader", SyntheticReader)

    with pytest.raises(InvoiceInputError, match="XML-Datei überschreitet"):
        extract_source(
            b"%PDF-1.7\n",
            "deklarierte-groesse.pdf",
            "application/pdf",
            max_embedded_bytes=10,
        )


def test_pdf_attachment_count_is_limited(monkeypatch, pdf_bytes_factory):
    monkeypatch.setattr(source_module, "MAX_PDF_ATTACHMENTS", 2)
    pdf = pdf_bytes_factory(
        ("one.txt", b"one"),
        ("two.txt", b"two"),
        ("invoice.xml", b"<Invoice />"),
    )

    with pytest.raises(InvoiceInputError, match="mehr als 2 eingebettete Dateien"):
        extract_source(pdf, "zu-viele-anlagen.pdf", "application/pdf")


def test_single_embedded_xml_must_fit_size_budget(pdf_bytes_factory):
    payload = b"<Invoice>" + (b"x" * 64) + b"</Invoice>"
    pdf = pdf_bytes_factory(("invoice.xml", payload))

    with pytest.raises(InvoiceInputError, match="XML-Datei überschreitet"):
        extract_source(
            pdf,
            "zu-grosse-xml.pdf",
            "application/pdf",
            max_embedded_bytes=len(payload) - 1,
        )


def test_single_non_xml_attachment_must_fit_size_budget(pdf_bytes_factory):
    pdf = pdf_bytes_factory(
        ("anlage.bin", b"x" * 11),
        ("invoice.xml", b"<Invoice />"),
    )

    with pytest.raises(InvoiceInputError, match="PDF-Datei überschreitet"):
        extract_source(
            pdf,
            "zu-grosse-anlage.pdf",
            "application/pdf",
            max_embedded_bytes=10,
        )


def test_decoded_attachments_share_cumulative_size_budget(pdf_bytes_factory):
    pdf = pdf_bytes_factory(
        ("anlage.bin", b"123456"),
        ("invoice.xml", b"<x />"),
    )

    with pytest.raises(InvoiceInputError, match="zusammen die zulässige Größenbegrenzung"):
        extract_source(
            pdf,
            "zu-viele-anlagen.pdf",
            "application/pdf",
            max_embedded_bytes=10,
        )


def test_decoded_attachments_may_exactly_fill_size_budget(pdf_bytes_factory):
    first = b"12345"
    selected = b"<x />"
    pdf = pdf_bytes_factory(
        ("anlage.bin", first),
        ("invoice.xml", selected),
    )

    source = extract_source(
        pdf,
        "passende-anlagen.pdf",
        "application/pdf",
        max_embedded_bytes=len(first) + len(selected),
    )

    assert source.xml_bytes == selected


def test_analysis_rejects_compressed_xml_larger_than_upload_budget(pdf_bytes_factory):
    payload = b"<Invoice>" + (b"x" * 200_000) + b"</Invoice>"
    pdf = pdf_bytes_factory(("invoice.xml", payload), compress_attachments=True)
    assert len(pdf) < len(payload)
    limit = (len(pdf) + len(payload)) // 2

    with pytest.raises(InvoiceInputError, match="XML-Datei überschreitet"):
        analyze_bytes(
            pdf,
            "komprimierte-xml.pdf",
            "application/pdf",
            run_official_validation=False,
            app_settings=replace(settings, max_upload_bytes=limit),
        )


def test_unsupported_input_is_rejected():
    with pytest.raises(InvoiceInputError, match="Unterstützt werden XML-Rechnungen sowie PDF-Dateien"):
        extract_source(b"synthetic unsupported input", "rechnung.bin", "application/octet-stream")
