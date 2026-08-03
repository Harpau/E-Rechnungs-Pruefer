from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table
from starlette.datastructures import UploadFile

import app.pdf_report as pdf_report_module
from app.pdf_report import render_pdf_report


def _finding(
    *,
    origin: str = "internal",
    title: str = "Zahlungsangaben prüfen",
    message: str = "Synthetische Prüfmeldung.",
) -> dict:
    return {
        "origin": origin,
        "rule_class": "core_precheck" if origin == "internal" else origin,
        "severity": "warning",
        "rule": {
            "id": "PAY-TEST-1",
            "title": title,
            "message": message,
            "source": "Synthetischer Test",
            "reference": "BR-TEST-1",
            "profile": None,
            "version": "1",
        },
        "semantic_references": [{"id": "BG-16", "label": "Zahlungsanweisungen"}],
        "occurrence": {
            "scope": "payment",
            "index": 0,
            "identifier": "58",
            "json_pointer": "/payment/instructions/0",
        },
        "xml_location": {
            "path": "/Invoice/PaymentMeans[1]",
            "line": 42,
            "column": 7,
        },
        "actual": {"value": "keine Angabe", "data_type": "text", "unit": None},
        "expected": {"value": "vollständige Angabe", "data_type": "text", "unit": None},
    }


def _party(name: str) -> dict:
    return {
        "legal_name": name,
        "trading_name": f"{name} Handel",
        "additional_legal_information": "GmbH",
        "identifiers": [
            {
                "kind": "legal-registration",
                "identifier": {"value": "SYNTH-4711", "scheme_id": "HRB"},
            }
        ],
        "tax_identifiers": [
            {
                "kind": "vat",
                "identifier": {"value": "DE000000000", "scheme_id": "VA"},
            }
        ],
        "electronic_address": {"value": "leitweg-synthetisch", "scheme_id": "0204"},
        "postal_address": {
            "line1": "Musterweg 1",
            "line2": "Gebäude 2",
            "line3": "Etage 3",
            "postcode": "10115",
            "city": "Berlin",
            "subdivision": "DE-BE",
            "country": {"value": "DE", "label": "Deutschland", "list_id": "ISO3166-1"},
        },
        "contact": {
            "name": "Synthetischer Kontakt",
            "department": "Testabteilung",
            "phone": "+49 30 000000",
            "email": "synthetisch@example.invalid",
        },
    }


def _schema2_analysis() -> dict:
    return {
        "schema_version": 2,
        "document": {
            "id": "SYNTH-INV-1",
            "issue_date": "2026-07-22",
            "type": {
                "status": "known",
                "code": {"value": "380", "label": "Rechnung", "list_id": "UNCL1001"},
                "family": "invoice",
                "base_polarity": "debit",
                "settlement_relevance": "relevant",
                "self_billing": False,
                "ubl_root": "invoice",
                "root_compatibility": "compatible",
                "registry_version": "2026-07",
            },
            "tax_point_date": "2026-07-22",
            "tax_point_date_code": None,
            "document_currency": {"value": "EUR", "label": "Euro", "list_id": "ISO4217"},
            "vat_accounting_currency": None,
            "buyer_reference": "LEITWEG-TEST",
            "notes": [
                {
                    "text": "Synthetischer Rechnungshinweis",
                    "subject_code": {
                        "value": "AAI",
                        "label": "Allgemeine Information",
                        "list_id": "UNCL4451",
                    },
                }
            ],
        },
        "profile": {
            "id": "urn:cen.eu:en16931:2017",
            "name": "EN 16931",
            "business_process_id": "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0",
        },
        "capabilities": {
            "syntax": "UBL",
            "syntax_version": "2.1",
            "format_name": "OASIS UBL 2.1 Invoice",
            "document_type_recognition": "recognized",
            "rendering": "full",
            "internal_checks": "partial",
            "official_validation": "bundled",
        },
        "parties": {
            "seller": _party("Synthetischer Lieferant"),
            "buyer": _party("Synthetischer Käufer"),
            "payee": None,
            "invoice_recipient": None,
            "seller_tax_representative": None,
            "delivery_recipient": _party("Synthetischer Warenempfänger"),
        },
        "roles": {
            "issuer": "seller",
            "document_recipient": "buyer",
            "creditor": "seller",
            "debtor": "buyer",
            "expected_payer": "buyer",
            "expected_recipient": "seller",
            "expected_payment_direction": "debtor-to-creditor",
            "derivation": "derived",
        },
        "periods": {
            "invoice": {
                "start_date": "2026-07-01",
                "end_date": "2026-07-31",
                "description": "Abrechnungsmonat Juli",
            },
            "delivery": None,
        },
        "delivery": {
            "actual_date": "2026-07-20",
            "location": {
                "id": {"value": "4000001000005", "scheme_id": "GLN"},
                "postal_address": {
                    "line1": "Lieferweg 1",
                    "line2": None,
                    "line3": None,
                    "postcode": "12345",
                    "city": "Lieferstadt",
                    "subdivision": None,
                    "country": {
                        "value": "DE",
                        "label": "Deutschland",
                        "list_id": "ISO3166-1",
                    },
                },
            },
        },
        "references": {
            "buyer_order": {
                "id": {"value": "BEST-1", "scheme_id": None},
                "issue_date": None,
                "description": None,
            },
            "seller_order": None,
            "contract": None,
            "tender": None,
            "project": None,
            "buyer_accounting_reference": "KST-42",
            "invoiced_object": None,
            "preceding_invoices": [],
            "supporting_documents": [
                {
                    "id": {"value": "ANLAGE-1", "scheme_id": None},
                    "type": {"value": "916", "label": "Anlage", "list_id": None},
                    "name": "Synthetische Anlage",
                    "description": "Nur Testdaten",
                    "attachment_filename": "anlage.txt",
                    "attachment_mime_type": "text/plain",
                    "embedded": True,
                    "external_uri": None,
                }
            ],
            "despatch_advice": None,
            "receiving_advice": None,
        },
        "lines": [
            {
                "id": "1",
                "notes": ["Synthetischer Positionshinweis"],
                "item": {
                    "name": "Synthetische Leistung",
                    "description": "Leistung ausschließlich für Regressionstests",
                    "seller_identifier": {"value": "SELL-1", "scheme_id": None},
                    "buyer_identifier": {"value": "BUY-1", "scheme_id": None},
                    "standard_identifier": {"value": "04000000000001", "scheme_id": "0160"},
                    "classifications": [
                        {
                            "code": "TEST",
                            "name": "Testklassifikation",
                            "scheme_id": "SYNTH",
                            "scheme_version": "1",
                        }
                    ],
                    "properties": [{"name": "Farbe", "value": "Blau"}],
                    "origin_country": {
                        "value": "DE",
                        "label": "Deutschland",
                        "list_id": "ISO3166-1",
                    },
                },
                "quantity": {
                    "value": "1",
                    "unit": {"value": "C62", "label": "Stück", "list_id": "UNECERec20"},
                },
                "period": None,
                "order_line_reference": "1",
                "accounting_reference": "KST-42",
                "object_identifier": None,
                "price": {
                    "net": {"value": "100.00", "currency": "EUR"},
                    "base_quantity": {
                        "value": "1",
                        "unit": {"value": "C62", "label": "Stück", "list_id": "UNECERec20"},
                    },
                    "gross": {"value": "110.00", "currency": "EUR"},
                    "discount": {
                        "amount": {"value": "10.00", "currency": "EUR"},
                        "percentage": "9.09",
                    },
                },
                "allowances_charges": [],
                "tax_type": {"value": "VAT", "label": None, "list_id": None},
                "tax_category": {
                    "value": "S",
                    "label": "Standardsteuersatz",
                    "list_id": None,
                },
                "tax_rate_percent": "19",
                "net_amount": {"value": "100.00", "currency": "EUR"},
            }
        ],
        "allowances_charges": [
            {
                "kind": "allowance",
                "indicator_raw": "false",
                "amount": {"value": "5.00", "currency": "EUR"},
                "base_amount": {"value": "100.00", "currency": "EUR"},
                "percentage": "5",
                "reason_text": "Synthetischer Nachlass",
                "reason_code": {"value": "95", "label": None, "list_id": None},
                "tax_category": {
                    "value": "S",
                    "label": "Standardsteuersatz",
                    "list_id": None,
                },
                "tax_rate_percent": "19",
            }
        ],
        "tax": {
            "breakdown": [
                {
                    "tax_type": {"value": "VAT", "label": None, "list_id": None},
                    "category": {
                        "value": "S",
                        "label": "Standardsteuersatz",
                        "list_id": None,
                    },
                    "rate_percent": "19",
                    "taxable_amount": {"value": "100.00", "currency": "EUR"},
                    "tax_amount": {"value": "19.00", "currency": "EUR"},
                    "exemption": None,
                }
            ],
            "totals": {
                "document_currency": {"value": "19.00", "currency": "EUR"},
                "vat_accounting_currency": None,
            },
        },
        "totals": {
            "line_net_total": {"value": "100.00", "currency": "EUR"},
            "allowance_total": {"value": "0.00", "currency": "EUR"},
            "charge_total": {"value": "0.00", "currency": "EUR"},
            "tax_exclusive_total": {"value": "100.00", "currency": "EUR"},
            "tax_inclusive_total": {"value": "119.00", "currency": "EUR"},
            "prepaid_total": {"value": "0.00", "currency": "EUR"},
            "rounding": {"value": "0.00", "currency": "EUR"},
            "payable": {"value": "119.00", "currency": "EUR"},
        },
        "payment": {
            "due_date": "2026-08-05",
            "reference": "SYNTH-INV-1",
            "terms": [
                {
                    "description": "Zahlbar innerhalb von 14 Tagen.",
                    "due_date": "2026-08-05",
                    "partial_payment": None,
                }
            ],
            "instructions": [
                {
                    "means": {"value": "58", "label": "SEPA-Überweisung", "list_id": "UNCL4461"},
                    "instruction_note": "Synthetische Überweisung",
                    "payment_id": "SYNTH-INV-1",
                    "credit_transfers": [
                        {
                            "account_id": {
                                "value": "DE89370400440532013000",
                                "scheme_id": "IBAN",
                            },
                            "account_name": "Synthetischer Lieferant",
                            "service_provider_id": {
                                "value": "COBADEFFXXX",
                                "scheme_id": "BIC",
                            },
                        }
                    ],
                    "payment_card": {
                        "masked_account_identifier": "•••• 1234",
                        "holder_name": "Synthetische Karteninhaberin",
                    },
                    "direct_debit": {
                        "mandate_reference": "MANDAT-1",
                        "creditor_id": {"value": "GLAEUBIGER-1", "scheme_id": "SEPA"},
                        "debited_account_id": {"value": "BELASTUNG-1", "scheme_id": "LOCAL"},
                    },
                }
            ],
        },
        "assessment": {
            "official": {
                "status": "not-requested",
                "requested": False,
                "configured": True,
                "executed": False,
                "summary": "Offizielle Prüfung wurde nicht angefordert.",
                "exit_code": None,
                "report_source": None,
                "raw_report": None,
                "technical_output": None,
                "findings": [],
                "counts": {"error": 0, "warning": 0, "info": 0},
            },
            "internal": {
                "status": "attention",
                "executed": True,
                "summary": "Eine synthetische Auffälligkeit.",
                "scope": "Interne Vorabprüfung",
                "findings": [_finding()],
                "counts": {"error": 0, "warning": 1, "info": 0},
            },
            "processing": {
                "status": "complete",
                "summary": "Verarbeitung vollständig.",
                "limitations": [],
                "findings": [],
                "counts": {"error": 0, "warning": 0, "info": 0},
            },
        },
        "source": {
            "upload": {
                "filename": "synthetische-rechnung.xml",
                "media_type": "application/xml",
                "size_bytes": 1234,
                "sha256": "a" * 64,
            },
            "invoice_xml": {
                "filename": "synthetische-rechnung.xml",
                "media_type": "application/xml",
                "size_bytes": 1234,
                "sha256": "b" * 64,
            },
            "container": {
                "kind": "xml",
                "page_count": None,
                "selected_attachment": None,
                "attachment_count": 0,
            },
            "attachments": [],
        },
        "technical": {
            "root_element": "Invoice",
            "root_namespace": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
            "field_count": 2,
            "truncated": False,
            "fields": [
                {
                    "kind": "element",
                    "path": "/Invoice/ID[1]",
                    "name": "ID",
                    "namespace": None,
                    "value": "SYNTH-INV-1",
                },
                {
                    "kind": "attribute",
                    "path": "/Invoice/Line[1]/@unitCode",
                    "name": "unitCode",
                    "namespace": None,
                    "value": "C62",
                },
            ],
            "source_xml": "<Invoice><ID>SYNTH-INV-1</ID></Invoice>",
            "pretty_xml": "<Invoice>\n  <ID>SYNTH-INV-1</ID>\n</Invoice>",
        },
        "runtime": {
            "generated_at": "2026-07-22T10:00:00+02:00",
            "duration_ms": "12.5",
            "application_version": "test",
        },
    }


def _pdf_text(payload: bytes) -> tuple[PdfReader, str]:
    document = PdfReader(BytesIO(payload))
    return document, "\n".join(page.extract_text() or "" for page in document.pages)


def _without_layout_whitespace(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _render(
    analysis: dict,
    *,
    scope: str = "readable",
    presentation: dict | None = None,
) -> tuple[bytes, PdfReader, str]:
    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
        scope=scope,
        presentation=presentation,
    )
    document, text = _pdf_text(payload)
    return payload, document, text


def test_pdf_requires_schema_two_and_does_not_render_legacy_keys():
    with pytest.raises(ValueError, match="Schema 2"):
        render_pdf_report({}, generated_at="22.07.2026", version="test")

    analysis = _schema2_analysis()
    analysis["validation"] = {"status": "LEGACY-STATUS-DARF-NICHT-ERSCHEINEN"}
    analysis["seller"] = {"name": "LEGACY-VERKÄUFER-DARF-NICHT-ERSCHEINEN"}
    analysis["taxes"] = [{"category_code": "LEGACY-STEUER"}]

    _, _, text = _render(analysis)

    assert "LEGACY-STATUS-DARF-NICHT-ERSCHEINEN" not in text
    assert "LEGACY-VERKÄUFER-DARF-NICHT-ERSCHEINEN" not in text
    assert "LEGACY-STEUER" not in text


def test_pdf_readable_scope_uses_localized_statuses_shared_facts_and_compact_payment_flow():
    from app.report_presentation import build_report_presentation

    analysis = _schema2_analysis()
    presentation = build_report_presentation(analysis)
    _, _, text = _render(analysis, presentation=presentation)

    assert presentation["scope"] == "readable"
    assert len(presentation["header_facts"]) == 30
    assert presentation["overall_status"]["label"] in text
    assert "Interne Vorabprüfung" in text
    assert "Offizielle Prüfung" in text
    assert "Verarbeitung" in text
    for axis in presentation["axes"]:
        assert axis["label"] in text
    for fact in presentation["header_facts"]:
        assert _without_layout_whitespace(fact["label"]) in _without_layout_whitespace(text)
    assert "Zahlungsanweisungen (BG-16)" in text
    assert "XML-Pfad" in text
    assert "/Invoice/PaymentMeans[1]" in text
    assert "Ort" not in text
    assert "Ausstehender Betrag (BT-115)" in text
    assert "Dokumentfluss" in text
    assert presentation["payment_flow"]["document_flow"] in text
    assert "Erwarteter Zahlungsfluss" in text
    assert presentation["payment_flow"]["expected_payment_flow"] in text
    assert presentation["payment_flow"]["note"] in text
    assert "Ausstellerrolle" not in text
    assert "Dokumentempfängerrolle" not in text
    assert "Gläubigerrolle" not in text
    assert "Schuldnerrolle" not in text
    assert "Erwartete zahlende Rolle" not in text
    assert "Erwartete empfangende Rolle" not in text
    assert "Tatsächliches Lieferdatum (BT-72)" in text
    assert "Kennung des Lieferorts (BT-71)" in text
    assert "4000001000005 (GLN)" in text
    assert "Lieferweg 1" in text
    assert "Registerkennung" in text
    assert ("AAI – Allgemeine Information (Liste: UNCL4451): Synthetischer Rechnungshinweis") in text
    assert "Kern-Vorprüfung" in text
    assert "Datentyp: Text" in text
    for raw_value in (
        "not-requested",
        "attention",
        "complete",
        "known",
        "full",
        "core_precheck",
    ):
        assert raw_value not in text


def test_pdf_does_not_repeat_a_code_already_contained_in_its_label():
    analysis = _schema2_analysis()
    analysis["payment"]["instructions"][0]["means"]["label"] = "58 – SEPA-Überweisung"

    _, _, text = _render(analysis)

    assert "58 – SEPA-Überweisung" in text
    assert "58 - 58 – SEPA-Überweisung" not in text


def test_pdf_defaults_to_readable_scope_without_technical_attachments():
    analysis = _schema2_analysis()
    analysis["assessment"]["official"].update(
        {
            "technical_output": "TECHNISCHE-AUSGABE-NUR-COMPLETE",
            "raw_report": "OFFIZIELLER-ROHBERICHT-NUR-COMPLETE",
        }
    )
    analysis["technical"]["fields"][0]["value"] = "TECHNISCHES-FELD-NUR-COMPLETE"
    analysis["technical"]["source_xml"] = "<COMPLETE-XML-MARKER/>"

    _, _, text = _render(analysis)

    assert "Prüfbericht" in text
    assert "Zahlungsangaben prüfen" in text
    assert "Technischer Anhang" not in text
    assert "Technische Ausgabe der offiziellen Prüfung" not in text
    assert "Technischer offizieller Bericht" not in text
    assert "TECHNISCHE-AUSGABE-NUR-COMPLETE" not in text
    assert "OFFIZIELLER-ROHBERICHT-NUR-COMPLETE" not in text
    assert "TECHNISCHES-FELD-NUR-COMPLETE" not in text
    assert "COMPLETE-XML-MARKER" not in text


def test_pdf_complete_scope_adds_bounded_technical_attachments():
    analysis = _schema2_analysis()
    analysis["assessment"]["official"].update(
        {
            "technical_output": "TECHNISCHE-AUSGABE-NUR-COMPLETE",
            "raw_report": "OFFIZIELLER-ROHBERICHT-NUR-COMPLETE",
        }
    )
    analysis["technical"]["fields"][0]["value"] = "TECHNISCHES-FELD-NUR-COMPLETE"
    analysis["technical"]["source_xml"] = "<COMPLETE-XML-MARKER/>"

    _, _, text = _render(analysis, scope="complete")

    assert "Prüfbericht" in text
    assert "Technischer Anhang" in text
    assert "Technische Ausgabe der offiziellen Prüfung" in text
    assert "Technischer offizieller Bericht" in text
    assert "TECHNISCHE-AUSGABE-NUR-COMPLETE" in text
    assert "OFFIZIELLER-ROHBERICHT-NUR-COMPLETE" in text
    assert "TECHNISCHES-FELD-NUR-COMPLETE" in _without_layout_whitespace(text)
    assert "COMPLETE-XML-MARKER" in text


def test_pdf_rejects_unknown_scope():
    with pytest.raises(ValueError, match="Berichtsumfang"):
        _render(_schema2_analysis(), scope="alles")


def test_pdf_readable_sections_follow_shared_report_order():
    _, _, text = _render(_schema2_analysis())
    headings = [
        "Übersicht",
        "Parteien",
        "Nachlässe und Zuschläge",
        "Rechnungspositionen",
        "Umsatzsteuer und Summen",
        "Zahlung",
        "Referenzen und Lieferung",
        "Hinweise und Quelle",
        "Prüfbericht",
    ]

    positions = []
    for heading in headings:
        match = re.search(rf"(?m)^{re.escape(heading)}(?: \(|$)", text)
        assert match is not None
        positions.append(match.start())

    assert positions == sorted(positions)


def test_pdf_tax_display_keeps_nested_code_basis_and_exemption_details():
    analysis = _schema2_analysis()
    analysis["tax"]["breakdown"][0].update(
        {
            "category": {
                "value": "O",
                "label": "Nicht der Umsatzsteuer unterliegend",
                "list_id": None,
            },
            "rate_percent": None,
            "taxable_amount": {"value": "495.00", "currency": "EUR"},
            "tax_amount": {"value": "0.00", "currency": "EUR"},
            "exemption": {
                "reasons": ["Leistung nicht im Inland steuerbar gemäß § 3a Abs. 2 UStG"],
                "reason_code": {"value": "VATEX-EU-O", "label": None, "list_id": None},
            },
        }
    )

    _, _, text = _render(analysis)

    assert "Kategoriecode (Original)" in text
    assert "O – Nicht der Umsatzsteuer unterliegend" in text
    assert "495,00 EUR" in text
    assert "Befreiungsgrund" in text
    assert "Leistung nicht im Inland steuerbar gemäß § 3a Abs. 2 UStG" in text
    assert "VATEX-EU-O" in text


def test_pdf_line_tax_category_and_rate_use_separate_full_width_detail_rows():
    analysis = _schema2_analysis()
    preparation = pdf_report_module._PdfPreparation(lines_total=1, lines_rendered=1)
    story = []

    pdf_report_module._render_lines(
        story,
        analysis,
        pdf_report_module._styles(),
        preparation,
    )

    tables = [flowable for flowable in story if isinstance(flowable, Table)]
    assert len(tables) == 1
    table = tables[0]
    rows = [tuple(cell.getPlainText() for cell in row) for row in table._cellvalues]

    category_index = rows.index(("Steuerkategorie", "S – Standardsteuersatz"))
    rate_index = rows.index(("Steuersatz", "19 %"))
    assert rate_index == category_index + 1
    assert all(label != "USt." for label, _ in rows)
    assert len(table._colWidths) == 2
    assert table._colWidths[1] >= 100 * mm


def test_pdf_escapes_untrusted_text_and_discloses_technical_limits(monkeypatch):
    analysis = _schema2_analysis()
    finding = _finding(
        title="Prüfung ÄÖÜ äöü ß € <nicht fett>",
        message="Unvertrauenswürdiger Text: <b>sichtbar & unverändert</b>.",
    )
    finding["actual"]["value"] = "<script>alert('nein')</script>"
    analysis["assessment"]["internal"]["findings"].append(finding)
    analysis["technical"]["fields"] = [
        {
            "kind": "element",
            "path": f"/Test[{index}]",
            "name": "Test",
            "namespace": None,
            "value": f"Wert <{index}> & Diagnose",
        }
        for index in range(8)
    ]
    analysis["technical"]["source_xml"] = "<Invoice>" + ("Ä & <Test>" * 100) + "</Invoice>"
    monkeypatch.setattr(pdf_report_module, "PDF_TECHNICAL_ROW_LIMIT", 3)
    monkeypatch.setattr(pdf_report_module, "PDF_TECHNICAL_CHARACTER_LIMIT", 120)
    monkeypatch.setattr(pdf_report_module, "PDF_RAW_XML_CHARACTER_LIMIT", 80)

    payload, document, text = _render(analysis, scope="complete")

    assert payload.startswith(b"%PDF-")
    assert len(document.pages) > 1
    assert "Prüfung ÄÖÜ äöü ß € <nicht fett>" in text
    assert "Unvertrauenswürdiger Text: <b>sichtbar & unverändert</b>." in text
    assert "<script>alert('nein')</script>" in text
    assert "Dargestellte technische Einträge: 3 von 8" in text
    assert "Mindestens ein technischer Bereich wurde im PDF gekürzt." in text
    assert "/Test[7]" not in text


def test_pdf_masks_card_identifier_even_if_schema_input_is_not_pre_masked():
    analysis = _schema2_analysis()
    analysis["payment"]["instructions"][0]["payment_card"]["masked_account_identifier"] = "4111111111111234"

    _, _, text = _render(analysis)

    assert "4111111111111234" not in text
    assert "•••• 1234" in text


def test_pdf_limits_long_untrusted_line_and_finding_text():
    analysis = _schema2_analysis()
    long_value = "<fremdes-markup>&" + ("SehrLangesWortOhneTrennzeichen" * 750) + "-TABELLEN-ENDE"
    analysis["lines"][0]["item"]["description"] = long_value
    analysis["assessment"]["internal"]["findings"].append(
        _finding(title="Sehr lange synthetische Prüfmeldung", message=long_value)
    )
    analysis["technical"]["fields"] = []
    analysis["technical"]["source_xml"] = ""

    _, document, text = _render(analysis)

    assert len(document.pages) > 1
    assert "<fremdes-markup>&" in text
    assert "TABELLEN-ENDE" not in text
    assert "PDF-Darstellung gekürzt" in text


def test_pdf_embeds_noto_for_latin_greek_cyrillic_and_cjk_with_visible_fallback():
    analysis = _schema2_analysis()
    analysis["document"]["notes"] = [
        {
            "text": "Łódź · Ελληνικά · Україна · 東京 · 你好 · Emoji 😀 · NUL \x00 Ende",
            "subject_code": None,
        }
    ]

    _, _, text = _render(analysis)

    assert "Łódź" in text
    assert "Ελληνικά" in text
    assert "Україна" in text
    assert "東京" in text
    assert "你好" in text
    assert "[U+1F600]" in text
    assert "[U+0000]" in text
    assert "\x00" not in text
    assert "�" not in text


def test_pdf_preparation_is_deterministic_budgeted_and_non_mutating():
    analysis = _schema2_analysis()
    base_line = deepcopy(analysis["lines"][0])
    base_line["item"]["description"] = "X" * 5_000
    analysis["lines"] = [deepcopy(base_line) for _ in range(300)]
    base_finding = _finding(message="Y" * 5_000)
    analysis["assessment"]["internal"]["findings"] = [deepcopy(base_finding) for _ in range(300)]
    analysis["document"]["notes"] = [{"text": f"Hinweis {index}", "subject_code": None} for index in range(75)]
    analysis["technical"] = {
        "root_element": "Invoice",
        "root_namespace": None,
        "field_count": 0,
        "truncated": False,
        "fields": [],
        "source_xml": "",
        "pretty_xml": "",
    }
    original = deepcopy(analysis)

    first, first_limits = pdf_report_module._prepare_analysis_for_pdf(analysis)
    second, second_limits = pdf_report_module._prepare_analysis_for_pdf(analysis)

    assert analysis == original
    assert first == second
    assert first_limits == second_limits
    assert len(first["lines"]) == first_limits.lines_rendered
    assert len(first["lines"]) <= 250
    assert len(first["assessment"]["internal"]["findings"]) == first_limits.findings_rendered
    assert first_limits.findings_rendered <= 250
    assert len(first["document"]["notes"]) == 50
    assert len(first["lines"][0]["item"]["description"]) == 4_000
    assert first["lines"][0]["item"]["description"].endswith("[...]")
    assert first_limits.lines_total == 300
    assert first_limits.findings_total == 300
    assert first_limits.notes_total == 75
    assert first_limits.notes_rendered == 50
    assert first_limits.total_truncated is True


def test_pdf_reserves_schema_two_core_status_and_totals_before_large_lines(monkeypatch):
    analysis = _schema2_analysis()
    original = deepcopy(analysis)
    base_line = deepcopy(analysis["lines"][0])
    for key in ("name", "description"):
        base_line["item"][key] = f"{key}:" + ("X" * 5_000)
    analysis["lines"] = [deepcopy(base_line) for _ in range(300)]
    analysis["assessment"]["internal"]["findings"] = [
        _finding(title=f"Core-Reservierung {index}", message="Y" * 5_000) for index in range(300)
    ]

    prepared, limits = pdf_report_module._prepare_analysis_for_pdf(analysis, scope="complete")

    assert prepared["document"]["id"] == original["document"]["id"]
    assert prepared["capabilities"]["syntax"] == original["capabilities"]["syntax"]
    assert prepared["assessment"]["internal"]["status"] == "attention"
    assert prepared["assessment"]["official"]["status"] == "not-requested"
    assert prepared["assessment"]["processing"]["status"] == "complete"
    assert prepared["totals"] == original["totals"]
    assert len(prepared["lines"]) == limits.lines_rendered
    assert limits.lines_rendered < limits.lines_total
    assert limits.total_truncated is True

    monkeypatch.setattr(pdf_report_module, "PDF_PAGE_LIMIT", 1)
    _, _, text = _render(analysis)

    assert original["document"]["id"] in text
    assert original["capabilities"]["syntax"] in text
    assert "Interne Vorabprüfung" in text
    assert "Offizielle Prüfung" in text
    assert "Verarbeitung" in text
    assert "119,00 EUR" in text


def test_pdf_bounds_control_expansion_while_normalizing():
    analysis = _schema2_analysis()
    analysis["document"]["notes"] = [{"text": "\t\x00" * 500_000, "subject_code": None}]
    analysis["technical"]["fields"] = []
    analysis["technical"]["source_xml"] = "\t" * 100_000

    prepared, limits = pdf_report_module._prepare_analysis_for_pdf(analysis, scope="complete")
    note = prepared["document"]["notes"][0]["text"]
    source_xml = prepared["technical"]["source_xml"]

    assert len(note) == pdf_report_module.PDF_SCALAR_CHARACTER_LIMIT
    assert note.startswith("[U+0009][U+0000]")
    assert note.endswith("[...]")
    assert "\t" not in note
    assert "\x00" not in note
    assert len(source_xml) <= pdf_report_module.PDF_RAW_XML_CHARACTER_LIMIT
    assert source_xml.startswith("[U+0009]")
    assert source_xml.endswith("[...]")
    assert limits.scalar_truncated is True
    assert limits.original_xml_limited is True


def test_pdf_limits_one_hundred_thousand_newlines_before_story():
    analysis = _schema2_analysis()
    analysis["technical"]["fields"] = []
    analysis["technical"]["source_xml"] = "XML\n" * 100_000

    prepared, limits = pdf_report_module._prepare_analysis_for_pdf(analysis, scope="complete")
    prepared_xml = prepared["technical"]["source_xml"]
    payload, document, text = _render(analysis, scope="complete")

    assert prepared_xml.count("\n") <= pdf_report_module.PDF_TECHNICAL_NEWLINE_LIMIT
    assert limits.original_xml_limited is True
    assert payload.startswith(b"%PDF-")
    assert len(document.pages) <= pdf_report_module.PDF_PAGE_LIMIT
    assert "Mindestens ein technischer Bereich wurde im PDF gekürzt." in text


def test_pdf_official_raw_report_starts_on_fresh_page_and_flows_across_pages():
    analysis = _schema2_analysis()
    raw_report = "OFFICIAL-BEGIN\n" + "\n".join(
        f'<rep:item id="validation-{index:04d}" value="synthetic"/>' for index in range(720)
    )
    raw_report += "\nOFFICIAL-END"
    analysis["assessment"]["official"].update(
        {
            "status": "accepted",
            "requested": True,
            "configured": True,
            "executed": True,
            "summary": "Synthetische offizielle Prüfung",
            "raw_report": raw_report,
        }
    )

    payload, _, _ = _render(analysis, scope="complete")
    document = PdfReader(BytesIO(payload))
    page_texts = [page.extract_text() or "" for page in document.pages]
    heading_page = next(index for index, text in enumerate(page_texts) if "Technischer offizieller Bericht" in text)
    end_page = next(index for index, text in enumerate(page_texts) if "OFFICIAL-END" in text)

    assert heading_page > 0
    assert "OFFICIAL-BEGIN" in page_texts[heading_page]
    assert "Interne Vorabprüfung" not in page_texts[heading_page]
    assert end_page > heading_page


def test_pdf_technical_text_chunks_are_splittable_and_prefer_safe_boundaries():
    token = "TOKEN_BLEIBT_GANZ"
    value = f"12345678901 {token} tail"

    chunks = list(pdf_report_module._iter_text_chunks(value, chunk_size=20))
    story = []
    pdf_report_module._append_text_chunks(
        story,
        value,
        pdf_report_module._styles(),
        chunk_size=20,
    )

    assert "".join(chunks) == value
    assert all(chunk[-1].isspace() for chunk in chunks[:-1])
    assert sum(token in chunk for chunk in chunks) == 1
    assert all(isinstance(flowable, Paragraph) for flowable in story[::2])
    assert all(isinstance(flowable, Spacer) for flowable in story[1::2])


def test_pdf_page_guard_returns_valid_compact_schema_two_replacement(monkeypatch):
    monkeypatch.setattr(pdf_report_module, "PDF_PAGE_LIMIT", 1)

    payload, document, text = _render(_schema2_analysis())

    assert payload.startswith(b"%PDF-")
    assert payload.rstrip().endswith(b"%%EOF")
    assert len(document.pages) == 1
    assert "Kompakter Ersatz-Prüfbericht" in text
    assert "Sicherheitsgrenze von maximal 1 Seite" in text
    assert "Ausstehender Betrag (BT-115)" in text


def test_pdf_endpoint_delegates_schema_two_analysis_and_renderer_to_threadpool(monkeypatch):
    import app.main as main_module

    delegated: list[object] = []
    analysis = _schema2_analysis()

    async def fake_run_in_threadpool(function, *args, **kwargs):
        delegated.append(function)
        return analysis if function is main_module._analyze_bytes_limited else b"%PDF-test\n%%EOF"

    monkeypatch.setattr(main_module, "run_in_threadpool", fake_run_in_threadpool)

    response = asyncio.run(
        main_module.pdf_report(
            file=UploadFile(BytesIO(b"<xml/>"), filename="rechnung.xml"),
            official=False,
            scope="readable",
        )
    )

    assert bytes(response.body) == b"%PDF-test\n%%EOF"
    assert delegated == [
        main_module._analyze_bytes_limited,
        main_module._render_pdf_report_limited,
    ]


def test_limited_analysis_allows_only_two_concurrent_jobs(monkeypatch):
    import app.main as main_module

    active = 0
    maximum_active = 0
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def fake_analyze(*_args, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
            if active == 2:
                both_started.set()
        assert release.wait(timeout=1)
        with lock:
            active -= 1
        return {"schema_version": 2}

    monkeypatch.setattr(main_module, "analyze_bytes", fake_analyze)
    monkeypatch.setattr(main_module, "_analysis_slots", threading.BoundedSemaphore(2))
    with ThreadPoolExecutor(max_workers=5) as executor:
        active_futures = [
            executor.submit(
                main_module._analyze_bytes_limited,
                b"<xml/>",
                "rechnung.xml",
                "application/xml",
                run_official_validation=False,
            )
            for _ in range(2)
        ]
        assert both_started.wait(timeout=1)
        overflow_futures = [
            executor.submit(
                main_module._analyze_bytes_limited,
                b"<xml/>",
                "rechnung.xml",
                "application/xml",
                run_official_validation=False,
            )
            for _ in range(3)
        ]
        for future in overflow_futures:
            with pytest.raises(main_module._AnalysisCapacityError):
                future.result(timeout=1)
        release.set()
        results = [future.result(timeout=1) for future in active_futures]

    assert results == [{"schema_version": 2}] * 2
    assert maximum_active == 2


def test_pdf_limited_renderer_allows_only_two_concurrent_jobs(monkeypatch):
    import app.main as main_module
    from app.report_presentation import build_report_presentation

    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def fake_render(*_args, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return b"%PDF-test\n%%EOF"

    monkeypatch.setattr(main_module, "render_pdf_report", fake_render)
    monkeypatch.setattr(main_module, "_pdf_render_slots", threading.BoundedSemaphore(2))
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(
                main_module._render_pdf_report_limited,
                _schema2_analysis(),
                generated_at="22.07.2026 10:00:00 CEST",
                version="test",
                scope="readable",
                presentation=build_report_presentation(_schema2_analysis()),
            )
            for _ in range(5)
        ]
        payloads = [future.result() for future in futures]

    assert payloads == [b"%PDF-test\n%%EOF"] * 5
    assert maximum_active == 2


def test_pdf_font_assets_are_pinned_and_licensed():
    font_directory = Path(pdf_report_module.__file__).resolve().parent / "assets" / "fonts"
    expected_hashes = {
        "NotoSans-Regular.ttf": "f5f552c8c5edb61fe6efb824baf4d4de47b1a8689ab4925ff43f7bd6a4ebece5",
        "NotoSans-Bold.ttf": "3a08a47daa00cade516425c15c57615aef2fd418ec9811a7b9f465088f92cc05",
        "NotoSans-Italic.ttf": "126522ae1bb9cd92120287fc47dfc74ef981e73931d93e52c565fb7e09b2d74a",
        "NotoSans-BoldItalic.ttf": "2e34b41a4b9c234b1be7dff6d06cba18811ecb694b41350873edf0ec16a0f0fa",
        "NotoSansSC-Variable.ttf": "a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da",
    }

    for filename, expected in expected_hashes.items():
        assert hashlib.sha256((font_directory / filename).read_bytes()).hexdigest() == expected
    for filename in ("OFL-NotoSans.txt", "OFL-NotoSansSC.txt"):
        license_text = (font_directory / filename).read_text(encoding="utf-8")
        assert "SIL OPEN FONT LICENSE Version 1.1" in license_text
