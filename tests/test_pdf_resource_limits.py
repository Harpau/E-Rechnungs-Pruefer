from __future__ import annotations

import zlib
from dataclasses import replace
from io import BytesIO

import pytest
from pypdf import PdfWriter, get_configuration
from pypdf.filters import FlateDecode
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject

from app.source import PdfResourceLimits, ProcessingLimitError, extract_source


def test_bounded_pdf_decoder_rejects_before_unbounded_materialization(pdf_bytes_factory):
    payload = b"<invoice>" + b"a" * 20000 + b"</invoice>"
    data = pdf_bytes_factory(("factur-x.xml", payload), compress_attachments=True)
    with pytest.raises(ProcessingLimitError):
        extract_source(data, "synthetic.pdf", max_embedded_bytes=1024, resource_limits=PdfResourceLimits())


def test_resource_configuration_is_restored_after_success_and_failure(pdf_bytes_factory):
    before = get_configuration()
    data = pdf_bytes_factory(("factur-x.xml", b"<invoice/>"))
    assert extract_source(data, "invoice.pdf", max_embedded_bytes=1024, resource_limits=PdfResourceLimits()).xml_bytes
    assert get_configuration() is before
    with pytest.raises(ProcessingLimitError):
        extract_source(data, "invoice.pdf", max_embedded_bytes=1, resource_limits=PdfResourceLimits())
    assert get_configuration() is before


def test_null_remaining_budget_never_reaches_decoder(pdf_bytes_factory):
    payload = b"<invoice/>"
    data = pdf_bytes_factory(("factur-x.xml", payload), ("z-other.xml", b""))
    with pytest.raises(ProcessingLimitError):
        extract_source(data, "invoice.pdf", max_embedded_bytes=len(payload), resource_limits=PdfResourceLimits())


def test_exact_attachment_budget_does_not_break_later_page_tree(pdf_bytes_factory):
    payload = b"<invoice/>"
    data = pdf_bytes_factory(("factur-x.xml", payload), compress_attachments=True)
    result = extract_source(data, "invoice.pdf", max_embedded_bytes=len(payload), resource_limits=PdfResourceLimits())
    assert result.xml_bytes == payload
    assert result.container["page_count"] == 1


@pytest.mark.parametrize("field", ["structure_stream_bytes", "page_tree_entries", "page_tree_depth"])
def test_invalid_resource_limits_are_rejected(field):
    with pytest.raises(ValueError):
        replace(PdfResourceLimits(), **{field: 0})


def test_second_decoder_receives_only_remaining_budget(monkeypatch, pdf_bytes_factory):
    first = b"<invoice/>"
    second = b"<extra/>"
    original = FlateDecode.decode
    limits = []

    def decode(*args, **kwargs):
        limits.append(get_configuration().zlib_maximum_output_length)
        return original(*args, **kwargs)

    monkeypatch.setattr(FlateDecode, "decode", decode)
    data = pdf_bytes_factory(("factur-x.xml", first), ("z-extra.xml", second), compress_attachments=True)
    extract_source(
        data, "invoice.pdf", max_embedded_bytes=len(first) + len(second), resource_limits=PdfResourceLimits()
    )
    assert limits == [len(first) + len(second), len(second)]


def test_page_tree_budget_is_enforced_during_flattening():
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=100, height=100)
    writer.add_attachment("factur-x.xml", b"<invoice/>")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ProcessingLimitError):
        extract_source(
            output.getvalue(),
            "invoice.pdf",
            max_embedded_bytes=1024,
            resource_limits=replace(PdfResourceLimits(), page_tree_entries=2),
        )


def test_every_filter_stage_is_checked_even_if_later_stage_would_shrink():
    payload = b"<invoice/>"
    compressed = zlib.compress(payload)
    assert len(compressed) > len(payload)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    attachment = writer.add_attachment("factur-x.xml", compressed.hex().encode() + b">")
    stream = attachment.pdf_object["/EF"]["/F"]
    stream[NameObject("/Filter")] = ArrayObject([NameObject("/ASCIIHexDecode"), NameObject("/FlateDecode")])
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ProcessingLimitError):
        extract_source(
            output.getvalue(), "invoice.pdf", max_embedded_bytes=len(payload), resource_limits=PdfResourceLimits()
        )
    assert (
        extract_source(
            output.getvalue(), "invoice.pdf", max_embedded_bytes=1024, resource_limits=PdfResourceLimits()
        ).xml_bytes
        == payload
    )


def test_page_depth_budget_is_enforced_on_a_small_synthetic_tree():
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_attachment("factur-x.xml", b"<invoice/>")
    root = writer.root_object["/Pages"]
    intermediate = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Pages"),
            NameObject("/Kids"): root["/Kids"],
            NameObject("/Count"): NumberObject(1),
        }
    )
    root[NameObject("/Kids")] = ArrayObject([writer._add_object(intermediate)])
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ProcessingLimitError):
        extract_source(
            output.getvalue(),
            "invoice.pdf",
            max_embedded_bytes=1024,
            resource_limits=replace(PdfResourceLimits(), page_tree_depth=1),
        )
