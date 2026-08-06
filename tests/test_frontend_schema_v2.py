from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "app" / "static" / "app.js"
STYLES_PATH = PROJECT_ROOT / "app" / "static" / "styles.css"
TEMPLATE_PATH = PROJECT_ROOT / "app" / "templates" / "index.html"
NODE_TEST_PATH = PROJECT_ROOT / "tests" / "frontend" / "test_app_schema_v2.mjs"


def _script() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _styles() -> str:
    return STYLES_PATH.read_text(encoding="utf-8")


def _template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _css_rule(styles: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", styles)
    assert match is not None, f"CSS-Regel für {selector!r} fehlt"
    return match.group("body")


def test_loading_indicator_avoids_animated_backdrop_compositing() -> None:
    styles = _styles()
    template = _template()

    loading_rule = _css_rule(styles, ".upload-card.is-loading")
    marker_rule = _css_rule(styles, ".progress-marker")
    progress_markup = template.split('<div id="progress"', 1)[1].split('<div id="error-box"', 1)[0]
    progress_tag = progress_markup.split(">", 1)[0]

    assert "backdrop-filter: none;" in loading_rule
    assert "animation:" not in marker_rule
    assert "@keyframes spin" not in styles
    assert ".spinner" not in styles

    assert 'class="progress-marker"' in progress_markup
    assert 'class="spinner"' not in progress_markup
    assert 'role="status"' in progress_tag
    assert 'aria-live="polite"' in progress_tag
    assert 'aria-atomic="true"' in progress_tag
    marker_tag = re.search(r'<span\b[^>]*class="progress-marker"[^>]*>', progress_markup)
    assert marker_tag is not None
    assert 'aria-hidden="true"' in marker_tag.group()


def test_browser_renderer_uses_only_schema_two_roots_and_nested_models() -> None:
    script = _script()

    for root in (
        "document",
        "profile",
        "capabilities",
        "parties",
        "roles",
        "periods",
        "delivery",
        "references",
        "lines",
        "allowances_charges",
        "tax",
        "totals",
        "payment",
        "assessment",
        "source",
        "technical",
        "runtime",
    ):
        assert f"data.{root}" in script or f"data.{root}?" in script

    assert "data.schema_version !== 2" in script
    assert "data.validation" not in script
    assert "data.seller" not in script
    assert "data.buyer" not in script
    assert "data.taxes" not in script
    assert "data.header_allowances_charges" not in script
    assert "payment.means" not in script
    assert "technical?.rows" not in script
    assert "technical?.raw_xml" not in script
    assert "due_payable_amount" not in script
    assert "additional_documents" not in script


def test_browser_renderer_keeps_assessment_axes_and_locations_semantically_separate() -> None:
    script = _script()

    assert "Offizielle Konformitätsprüfung" in script
    assert "Interne Prüfung" in script
    assert "renderAxisFindings('Verarbeitung'" in script
    assert "item.semantic_references" in script
    assert "reference.label" in script
    assert "`${reference.label} (${reference.id})`" in script
    assert "present(xmlLocation.path)" in script
    assert "`XML-Pfad: ${xmlLocation.path}`" in script
    assert "Ort:" not in script
    assert "item.location" not in script


def test_browser_renderer_uses_neutral_payment_semantics_and_masks_cards() -> None:
    script = _script()

    assert "Ausstehender Betrag (BT-115)" in script
    assert "Dokument- und Zahlungsfluss" in script
    assert "Erwarteter Zahlungsfluss" in script
    assert "Zahlungsanweisungen (BG-16)" in script
    assert "kein Nachweis, dass eine Zahlung tatsächlich erfolgt ist oder erfolgen muss" in script
    assert "instruction.payment_card.masked_account_identifier" in script
    assert "maskCardIdentifier(" in script
    assert "card_account" not in script
    assert "Kartennummer/Konto" not in script


def test_browser_renderer_limits_entry_facts_and_groups_subsection_headings() -> None:
    script = _script()
    styles = _styles()

    assert "Typregister-Version" not in script
    assert '<h3 class="subsection-heading">' in script
    assert ".subsection-heading" in styles
    assert "margin: 28px 0 5px" in styles
    assert '<h3 class="payment-heading payment-section-heading">' in script
    assert '<h4 class="payment-heading payment-item-heading">' in script
    assert '<h5 class="payment-heading payment-detail-heading">' in script
    assert ".subsection-heading + .subsection-heading" not in styles
    assert "#payment-section .detail-row dt { font-size: .78rem; }" in styles
    assert ".role-semantics-note { margin: 2px 0 0; font-size: .70rem; line-height: 1.4; }" in styles
    assert ".payment-heading + .detail-list > .detail-row:first-child" in styles


def test_browser_header_uses_the_exact_document_type_instead_of_repeating_the_family() -> None:
    script = _script()
    styles = _styles()
    template = _template()

    assert 'id="document-type-summary"' in template
    assert 'id="document-kind"' not in template
    assert "Rechnungsart · ${codeDisplay(type.code" in script
    assert "Rechnungsart · ${type.code.value} – Unbekannter Dokumenttyp" in script
    assert "Rechnungsart · Nicht angegeben" in script
    assert "$('#document-type-summary').textContent" in script

    responsive_header = styles.split("@media (max-width: 1360px)", 1)[1].split("@media", 1)[0]
    assert ".summary-card { grid-template-columns: minmax(0, 1fr) auto; }" in responsive_header
    assert (
        ".summary-counts { grid-column: 1 / -1; grid-template-columns: repeat(3, minmax(0, 1fr)); }"
        in responsive_header
    )


def test_browser_header_status_badge_keeps_every_label_visible() -> None:
    styles = _styles()

    badge = styles.split(".status-badge {", 1)[1].split("}", 1)[0]
    assert "flex: 0 0 auto;" in badge
    assert "inline-size: max-content;" in badge
    assert "min-inline-size: 10rem;" in badge
    assert "white-space: nowrap;" in badge

    assert "container-name: invoice-result;" in styles
    assert "container-type: inline-size;" in styles
    responsive_container = styles.split("@container invoice-result (max-width: 1420px)", 1)[1].split("@media", 1)[0]
    assert ".summary-card { grid-template-columns: minmax(0, 1fr) auto; }" in responsive_container
    assert (
        ".summary-counts { grid-column: 1 / -1; grid-template-columns: repeat(3, minmax(0, 1fr)); }"
        in responsive_container
    )

    mobile = styles.split("@media (max-width: 760px)", 1)[1].split("@media", 1)[0]
    assert ".status-badge { min-width: 62px" not in mobile
    assert ".summary-card { grid-template-columns: minmax(0, 1fr);" in mobile
    assert ".summary-count strong, .summary-count span { display: block; overflow-wrap: anywhere; }" in styles


def test_browser_report_actions_use_explicit_scopes_and_wait_for_the_report() -> None:
    script = _script()
    template = _template()

    assert 'id="download-complete-html-button"' in template
    assert ">Vollständiger Bericht</button>" in template
    assert "async function fetchHtmlReport(scope = 'readable')" in script
    assert "form.append('scope', scope);" in script
    assert "fetchHtmlReport('readable')" in script
    assert "fetchHtmlReport('complete')" in script
    assert "download-complete-html-button" in script
    assert "printWindow.addEventListener('load'" in script
    assert "printWindow.addEventListener('afterprint'" in script
    assert "}, 1200);" not in script


def test_direct_browser_print_shows_the_readable_report_scope() -> None:
    styles = _styles()
    print_styles = styles.split("@media print", 1)[1].split("[hidden]", 1)[0]

    assert "#invoice-panel, #validation-panel { display: block !important; }" in print_styles
    assert "#technical-panel, #raw-panel, #official-report-details { display: none !important; }" in print_styles
    assert ".summary-card" in print_styles
    assert "background: #fff" in print_styles


def test_browser_renderer_has_labels_for_every_document_family() -> None:
    script = _script()

    for family in (
        "invoice",
        "credit-note",
        "correction",
        "debit-note",
        "prepayment-invoice",
        "payment-request",
        "pro-forma",
        "information",
        "claim",
        "other",
        "unknown",
    ):
        assert f"'{family}':" in script or f"{family}:" in script


def test_browser_renderer_contracts_in_real_node_runtime() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js ist lokal nicht installiert")

    completed = subprocess.run(
        [node, "--test", str(NODE_TEST_PATH)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, f"{completed.stdout}\n{completed.stderr}"
