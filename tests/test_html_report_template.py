from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "app" / "templates" / "report.html"
client = TestClient(app)


def _report(cii_path: Path, *, scope: str | None = None) -> str:
    data = {"official": "false"}
    if scope is not None:
        data["scope"] = scope
    response = client.post(
        "/api/report",
        files={"file": (cii_path.name, cii_path.read_bytes(), "application/xml")},
        data=data,
    )
    assert response.status_code == 200
    return response.text


def _visible_text(html: str) -> str:
    without_style = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    return " ".join(re.sub(r"<[^>]+>", " ", without_style).split())


def test_readable_html_report_uses_the_shared_header_and_exactly_30_facts(cii_path: Path) -> None:
    html = _report(cii_path)
    text = _visible_text(html)

    assert 'data-report-scope="readable"' in html
    assert "Mit Hinweisen" in text
    assert "Offiziell" in text
    assert "Nicht angefordert" in text
    assert "Intern" in text
    assert "Hinweise" in text
    assert "Verarbeitung" in text
    assert "Vollständig" in text
    assert len(re.findall(r'class="fact"', html)) == 30
    assert "Rechnungsnummer" in text
    assert "Rechnungsart" in text
    assert "Wurzel/Typ-Kompatibilität" in text


def test_readable_html_report_uses_compact_payment_flow_and_bg16_hierarchy(cii_path: Path) -> None:
    html = _report(cii_path)
    text = _visible_text(html)

    assert "Dokument- und Zahlungsfluss" in text
    assert "Dokumentfluss" in text
    assert "Erwarteter Zahlungsfluss" in text
    assert "Dies ist kein Nachweis, dass eine Zahlung tatsächlich erfolgt ist oder erfolgen muss." in text
    assert "Zahlungsanweisungen (BG-16)" in text
    assert 'class="payment-heading payment-section-heading"' in html
    assert 'class="payment-heading payment-item-heading"' in html
    assert 'class="payment-heading payment-detail-heading"' in html
    assert "Dokumentaussteller" not in text
    assert "Erwarteter Zahler" not in text
    assert "Ableitung" not in text


def test_readable_html_report_omits_every_technical_appendix(cii_path: Path) -> None:
    text = _visible_text(_report(cii_path))

    assert "Technischer KoSIT-Bericht" not in text
    assert "Vollständiger technischer Anhang" not in text
    assert "Alle XML-Elemente, Attribute und Namespaces" not in text
    assert "XML-Darstellung" not in text


def test_complete_html_report_adds_every_technical_appendix(cii_path: Path, monkeypatch) -> None:
    original_analyze = main_module._analyze_bytes_limited

    def analyze_with_technical_output(*args, **kwargs):
        analysis = original_analyze(*args, **kwargs)
        analysis["assessment"]["official"]["technical_output"] = "SYNTHETISCHE-HTML-TECHNIKAUSGABE"
        return analysis

    monkeypatch.setattr(main_module, "_analyze_bytes_limited", analyze_with_technical_output)
    readable_text = _visible_text(_report(cii_path))
    html = _report(cii_path, scope="complete")
    text = _visible_text(html)

    assert "SYNTHETISCHE-HTML-TECHNIKAUSGABE" not in readable_text
    assert 'data-report-scope="complete"' in html
    assert "Technische KoSIT-Ausgabe" in text
    assert "SYNTHETISCHE-HTML-TECHNIKAUSGABE" in text
    assert "Vollständiger technischer Anhang" in text
    assert "Alle XML-Elemente, Attribute und Namespaces" in text
    assert "XML-Darstellung" in text


def test_html_report_has_a_light_print_header_and_stable_page_break_rules() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    hero_rule = template.split(".hero {", 1)[1].split("}", 1)[0]

    assert "color:var(--ink)" in hero_rule
    assert "background:var(--soft)" in hero_rule
    assert "break-after:avoid" in template
    assert "thead{display:table-header-group}" in template
    assert "widows:" in template
    assert "orphans:" in template
    assert re.search(r"\.finding\s*\{\s*break-inside:avoid", template)
    assert re.search(r"\.payment-item\s*\{\s*break-inside:avoid", template)


def test_html_report_prioritizes_the_tax_rate_without_a_regular_legend() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    body_rule = template.split("body {", 1)[1].split("}", 1)[0]
    primary_rule = template.split(".line-tax-primary {", 1)[1].split("}", 1)[0]
    secondary_rule = template.rsplit(".line-tax-secondary {", 1)[1].split("}", 1)[0]

    assert '<th style="width:36%">Artikel / Leistung</th>' in template
    assert '<th class="num" style="width:11%">USt.</th>' in template
    assert 'class="num line-tax-cell"' in template
    assert "{% if line_tax.primary_kind == 'empty' %}" in template
    assert '<span class="line-tax-empty">{{ line_tax.primary }}</span>' in template
    assert (
        '<strong class="line-tax-primary line-tax-{{ line_tax.primary_kind }}">{{ line_tax.primary }}</strong>'
    ) in template
    assert '<td class="num"><strong>{{ line.net_amount|money(currency) }}</strong></td>' in template
    assert 'class="line-tax-secondary"' in template
    assert 'class="sr-only">{{ line_tax.accessible_label }}</span>' in template
    assert "font-size:10pt" in body_rule
    assert "font-size" not in primary_rule
    assert "font-weight" not in primary_rule
    assert "font-size:7pt" in secondary_rule
    assert "presentation.tax_breakdown_gaps" in template
    assert "Nicht in der Steueraufschlüsselung enthalten:" in template
    assert "Steuerkategorien:" not in template


def test_html_report_wraps_long_tax_codes_but_not_rates_and_keeps_gap_together() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    rate_rule = template.split(".line-tax-rate {", 1)[1].split("}", 1)[0]
    wrappable_rule = template.split(".line-tax-code,.line-tax-secondary {", 1)[1].split("}", 1)[0]
    gap_rule = template.split(".tax-breakdown-gap {", 1)[1].split("}", 1)[0]

    assert "white-space:nowrap" in rate_rule
    assert "font-size" not in rate_rule
    assert "font-weight" not in rate_rule
    assert "max-width:100%" in wrappable_rule
    assert "overflow-wrap:anywhere" in wrappable_rule
    assert "white-space:normal" in wrappable_rule
    assert "break-inside:avoid" in gap_rule
    assert "page-break-inside:avoid" in gap_rule
    assert "max-width:100%" in gap_rule
    assert "overflow-wrap:anywhere" in gap_rule
