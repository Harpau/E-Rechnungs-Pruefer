from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter

from app.analyzer import analyze_bytes


def test_pdf_with_embedded_xml_is_supported(ubl_path):
    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_attachment("factur-x.xml", ubl_path.read_bytes())
    writer.write(buffer)

    result = analyze_bytes(
        buffer.getvalue(),
        "hybrid-rechnung.pdf",
        "application/pdf",
        run_official_validation=False,
    )

    assert result["document"]["id"] == "UBL-DEMO-1"
    assert result["source"]["container"]["type"] == "PDF mit eingebetteter XML"
    assert result["source"]["container"]["selected_attachment"] == "factur-x.xml"
    assert result["source"]["attachments"][0]["is_xml"] is True
