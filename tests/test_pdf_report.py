from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.platypus import Paragraph, Spacer
from starlette.datastructures import UploadFile

import app.main as main_module
import app.pdf_report as pdf_report_module
from app.analyzer import analyze_bytes
from app.pdf_report import render_pdf_report


def _analyze(path: Path) -> dict:
    return analyze_bytes(
        path.read_bytes(),
        path.name,
        "application/xml",
        run_official_validation=False,
    )


def _pdf_text(payload: bytes) -> tuple[PdfReader, str]:
    document = PdfReader(BytesIO(payload))
    return document, "\n".join(page.extract_text() or "" for page in document.pages)


def test_pdf_tax_display_keeps_code_basis_and_exemption_details(cii_category_o_path):
    analysis = _analyze(cii_category_o_path)
    analysis["taxes"][0]["exemption_reason_code"] = "VATEX-EU-O"

    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
    )
    _, text = _pdf_text(payload)

    assert "Kategoriecode (Original)" in text
    assert "O – Nicht der Umsatzsteuer unterliegend" in text
    assert "Nettobetrag dieser Steuerkategorie" in text
    assert "495,00 EUR" in text
    assert "Befreiungsgrund" in text
    assert "Leistung nicht im Inland steuerbar gemäß § 3a Abs. 2 UStG" in text
    assert "Befreiungsgrundcode" in text
    assert "VATEX-EU-O" in text


def test_pdf_escapes_untrusted_text_and_discloses_technical_limits(cii_path, monkeypatch):
    analysis = _analyze(cii_path)
    finding = {
        "id": "PDF-TEST-001",
        "severity": "warning",
        "title": "Prüfung ÄÖÜ äöü ß € <nicht fett>",
        "message": "Unvertrauenswürdiger Text: <b>sichtbar & unverändert</b>.",
        "location": "/Test[1]",
        "actual": "<script>alert('nein')</script>",
        "expected": "Nur Text",
        "source": "Synthetischer PDF-Test",
    }
    analysis["validation"]["findings"].append(finding)
    analysis["technical"]["rows"] = [
        {"kind": "element", "path": f"/Test[{index}]", "value": f"Wert <{index}> & Diagnose"} for index in range(8)
    ]
    analysis["technical"]["original_xml"] = "<Invoice>" + ("Ä & <Test>" * 100) + "</Invoice>"
    monkeypatch.setattr(pdf_report_module, "PDF_TECHNICAL_ROW_LIMIT", 3)
    monkeypatch.setattr(pdf_report_module, "PDF_TECHNICAL_CHARACTER_LIMIT", 120)
    monkeypatch.setattr(pdf_report_module, "PDF_RAW_XML_CHARACTER_LIMIT", 80)

    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
    )
    document, text = _pdf_text(payload)

    assert payload.startswith(b"%PDF-")
    assert len(document.pages) > 1
    assert "Prüfung ÄÖÜ äöü ß € <nicht fett>" in text
    assert "Unvertrauenswürdiger Text: <b>sichtbar & unverändert</b>." in text
    assert "<script>alert('nein')</script>" in text
    assert "Dargestellte technische Einträge: 3 von 8" in text
    assert "Mindestens ein technischer Bereich wurde im PDF gekürzt." in text
    assert "Das vollständige Original" in text
    assert "/api/xml" in text
    assert "/Test[7]" not in text


def test_pdf_limits_very_long_untrusted_table_and_finding_text(cii_path):
    analysis = _analyze(cii_path)
    long_value = "<fremdes-markup>&" + ("SehrLangesWortOhneTrennzeichen" * 750) + "-TABELLEN-ENDE"
    analysis["lines"][0]["description"] = long_value
    analysis["validation"]["findings"].append(
        {
            "id": "PDF-LONG-001",
            "severity": "warning",
            "title": "Sehr lange synthetische Prüfmeldung",
            "message": long_value,
            "location": "/Synthetischer/Testpfad",
            "actual": long_value,
            "expected": "Vollständig paginierter Text",
            "source": "Synthetischer PDF-Test",
        }
    )
    analysis["technical"]["rows"] = []
    analysis["technical"]["original_xml"] = ""

    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
    )
    document, text = _pdf_text(payload)

    assert len(document.pages) > 1
    assert "<fremdes-markup>&" in text
    assert "TABELLEN-ENDE" not in text
    assert "PDF-Darstellung gekürzt" in text
    assert "PDF-LONG-001" in text


def test_pdf_embeds_noto_for_latin_greek_cyrillic_and_cjk_with_visible_fallback(cii_path):
    analysis = _analyze(cii_path)
    analysis["document"]["notes"] = ["Łódź · Ελληνικά · Україна · 東京 · 你好 · Emoji 😀 · NUL \x00 Ende"]

    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
    )
    _, text = _pdf_text(payload)

    assert "Łódź" in text
    assert "Ελληνικά" in text
    assert "Україна" in text
    assert "東京" in text
    assert "你好" in text
    assert "[U+1F600]" in text
    assert "[U+0000]" in text
    assert "\x00" not in text
    assert "�" not in text


def test_pdf_prepares_deterministic_list_scalar_and_total_budgets(cii_path):
    analysis = _analyze(cii_path)
    base_line = deepcopy(analysis["lines"][0])
    base_line["description"] = "X" * 5_000
    analysis["lines"] = [deepcopy(base_line) for _ in range(300)]
    base_finding = {
        "id": "PDF-BUDGET",
        "severity": "warning",
        "title": "Budget",
        "message": "Y" * 5_000,
        "location": "/Test",
        "actual": "Ist",
        "expected": "Soll",
        "source": "Test",
    }
    analysis["validation"]["findings"] = [deepcopy(base_finding) for _ in range(300)]
    analysis["document"]["notes"] = [f"Hinweis {index}" for index in range(75)]
    analysis["technical"] = {"rows": [], "original_xml": "", "truncated": False}

    first, first_limits = pdf_report_module._prepare_analysis_for_pdf(analysis)
    second, second_limits = pdf_report_module._prepare_analysis_for_pdf(analysis)

    assert first == second
    assert first_limits == second_limits
    assert len(first["lines"]) == first_limits.lines_rendered
    assert len(first["lines"]) <= 250
    assert len(first["validation"]["findings"]) == first_limits.findings_rendered
    assert len(first["validation"]["findings"]) <= 250
    assert len(first["document"]["notes"]) == 50
    assert len(first["lines"][0]["description"]) == 4_000
    assert first["lines"][0]["description"].endswith("[...]")
    assert first_limits.lines_total == 300
    assert first_limits.lines_rendered <= 250
    assert first_limits.findings_total == 300
    assert first_limits.findings_rendered <= 250
    assert first_limits.notes_total == 75
    assert first_limits.notes_rendered == 50
    assert first_limits.total_truncated is True


def test_pdf_reserves_core_status_and_totals_before_large_lines(cii_path, monkeypatch):
    analysis = _analyze(cii_path)
    original_document = deepcopy(analysis["document"])
    original_validation = deepcopy(analysis["validation"])
    original_totals = deepcopy(analysis["totals"])
    base_line = deepcopy(analysis["lines"][0])
    for key in (
        "name",
        "description",
        "seller_item_id",
        "buyer_item_id",
        "standard_item_id",
        "order_line_reference",
        "accounting_cost",
    ):
        base_line[key] = f"{key}:" + ("X" * 5_000)
    analysis["lines"] = [deepcopy(base_line) for _ in range(300)]
    analysis["validation"]["findings"] = [
        {
            "id": f"PDF-CORE-{index}",
            "severity": "warning",
            "title": "Core-Reservierung",
            "message": "Y" * 5_000,
        }
        for index in range(300)
    ]
    analysis["technical"] = {"rows": [], "original_xml": "", "truncated": False}

    prepared, limits = pdf_report_module._prepare_analysis_for_pdf(analysis)

    assert prepared["document"]["id"] == original_document["id"]
    assert prepared["document"]["syntax"] == original_document["syntax"]
    assert prepared["validation"]["status"] == original_validation["status"]
    assert prepared["validation"]["counts"] == {
        key: str(original_validation["counts"][key]) for key in ("error", "warning", "info")
    }
    assert prepared["validation"]["official"]["configured"] is original_validation["official"]["configured"]
    assert prepared["validation"]["official"]["executed"] is original_validation["official"]["executed"]
    assert prepared["validation"]["official"]["accepted"] is original_validation["official"]["accepted"]
    assert prepared["totals"] == {key: None if value is None else str(value) for key, value in original_totals.items()}
    assert len(prepared["lines"]) == limits.lines_rendered
    assert limits.lines_rendered < limits.lines_total
    assert all(any(value not in (None, "", [], {}) for value in line.values()) for line in prepared["lines"])
    assert limits.total_truncated is True

    monkeypatch.setattr(pdf_report_module, "PDF_PAGE_LIMIT", 1)
    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
    )
    _, text = _pdf_text(payload)

    assert original_document["id"] in text
    assert original_document["syntax"] in text
    assert original_validation["status"] in text
    assert "KoSIT konfiguriert" in text
    assert "KoSIT ausgeführt" in text
    assert "13.820,42 EUR" in text


def test_pdf_bounds_control_expansion_while_normalizing(cii_path):
    analysis = _analyze(cii_path)
    analysis["document"]["notes"] = [("\t\x00" * 500_000)]
    analysis["technical"]["rows"] = []
    analysis["technical"]["original_xml"] = "\t" * 100_000

    prepared, limits = pdf_report_module._prepare_analysis_for_pdf(analysis)
    note = prepared["document"]["notes"][0]
    original_xml = prepared["technical"]["original_xml"]

    assert len(note) == pdf_report_module.PDF_SCALAR_CHARACTER_LIMIT
    assert note.startswith("[U+0009][U+0000]")
    assert note.endswith("[...]")
    assert "\t" not in note
    assert "\x00" not in note
    assert len(original_xml) <= pdf_report_module.PDF_RAW_XML_CHARACTER_LIMIT
    assert original_xml.startswith("[U+0009]")
    assert original_xml.endswith("[...]")
    assert limits.scalar_truncated is True
    assert limits.original_xml_limited is True


def test_pdf_limits_one_hundred_thousand_newlines_before_story(cii_path):
    analysis = _analyze(cii_path)
    analysis["technical"]["rows"] = []
    analysis["technical"]["original_xml"] = "XML\n" * 100_000

    prepared, limits = pdf_report_module._prepare_analysis_for_pdf(analysis)
    prepared_xml = prepared["technical"]["original_xml"]
    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
    )
    document, text = _pdf_text(payload)

    assert prepared_xml.count("\n") <= pdf_report_module.PDF_TECHNICAL_NEWLINE_LIMIT
    assert limits.original_xml_limited is True
    assert payload.startswith(b"%PDF-")
    assert len(document.pages) <= pdf_report_module.PDF_PAGE_LIMIT
    assert "Mindestens ein technischer Bereich wurde im PDF gekürzt." in text


def test_pdf_kosit_report_starts_on_fresh_page_and_flows_across_pages(cii_path):
    analysis = _analyze(cii_path)
    raw_report = "KOSIT-BEGIN\n" + "\n".join(
        f'<rep:item id="validation-{index:04d}" value="synthetic"/>' for index in range(720)
    )
    raw_report += "\nKOSIT-END"
    analysis["validation"]["official"].update(
        {
            "configured": True,
            "executed": True,
            "accepted": True,
            "summary": "Synthetische KoSIT-Prüfung",
            "raw_report": raw_report,
        }
    )

    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
    )
    document = PdfReader(BytesIO(payload))
    page_texts = [page.extract_text() or "" for page in document.pages]
    heading_page = next(index for index, text in enumerate(page_texts) if "Technischer KoSIT-Bericht" in text)
    end_page = next(index for index, text in enumerate(page_texts) if "KOSIT-END" in text)

    assert heading_page > 0
    assert "KOSIT-BEGIN" in page_texts[heading_page]
    assert "Gesamtstatus" not in page_texts[heading_page]
    assert end_page > heading_page


def test_pdf_technical_text_chunks_are_splittable_and_prefer_safe_boundaries():
    token = "TOKEN_BLEIBT_GANZ"
    value = f"12345678901 {token} tail"

    chunks = list(pdf_report_module._iter_text_chunks(value, chunk_size=20))
    story = []
    pdf_report_module._append_text_chunks(story, value, pdf_report_module._styles(), chunk_size=20)

    assert "".join(chunks) == value
    assert all(chunk[-1].isspace() for chunk in chunks[:-1])
    assert sum(token in chunk for chunk in chunks) == 1
    assert all(isinstance(flowable, Paragraph) for flowable in story[::2])
    assert all(isinstance(flowable, Spacer) for flowable in story[1::2])


def test_pdf_page_guard_returns_valid_compact_replacement(cii_path, monkeypatch):
    analysis = _analyze(cii_path)
    monkeypatch.setattr(pdf_report_module, "PDF_PAGE_LIMIT", 1)

    payload = render_pdf_report(
        analysis,
        generated_at="22.07.2026 10:00:00 CEST",
        version="test",
    )
    document, text = _pdf_text(payload)

    assert payload.startswith(b"%PDF-")
    assert payload.rstrip().endswith(b"%%EOF")
    assert len(document.pages) == 1
    assert "Kompakter Ersatz-Prüfbericht" in text
    assert "Sicherheitsgrenze von maximal 1 Seite" in text
    assert "vollständigen analysierten Daten" in text


def test_pdf_endpoint_delegates_analysis_and_limited_renderer_to_threadpool(monkeypatch):
    delegated: list[object] = []
    analysis = {
        "document": {"syntax": "CII"},
        "validation": {
            "status": "ok",
            "official": {"configured": False, "executed": False, "accepted": None},
        },
    }

    async def fake_run_in_threadpool(function, *args, **kwargs):
        delegated.append(function)
        return analysis if function is main_module._analyze_bytes_limited else b"%PDF-test\n%%EOF"

    monkeypatch.setattr(main_module, "run_in_threadpool", fake_run_in_threadpool)

    response = asyncio.run(
        main_module.pdf_report(file=UploadFile(BytesIO(b"<xml/>"), filename="rechnung.xml"), official=False)
    )

    assert bytes(response.body) == b"%PDF-test\n%%EOF"
    assert delegated == [main_module._analyze_bytes_limited, main_module._render_pdf_report_limited]


def test_limited_analysis_allows_only_two_concurrent_jobs(monkeypatch):
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
        return {"ok": True}

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

    assert results == [{"ok": True}] * 2
    assert maximum_active == 2


def test_pdf_limited_renderer_allows_only_two_concurrent_jobs(monkeypatch):
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
                {},
                generated_at="22.07.2026 10:00:00 CEST",
                version="test",
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
