from __future__ import annotations

import zlib
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import NameObject

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PdfBytesFactory = Callable[..., bytes]


@pytest.fixture()
def pdf_bytes_factory() -> PdfBytesFactory:
    def build(
        *attachments: tuple[str, bytes],
        password: str | None = None,
        compress_attachments: bool = False,
    ) -> bytes:
        buffer = BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        for name, payload in attachments:
            embedded_file = writer.add_attachment(name, payload)
            if compress_attachments:
                stream = embedded_file.pdf_object["/EF"]["/F"]
                stream.set_data(zlib.compress(payload))
                stream[NameObject("/Filter")] = NameObject("/FlateDecode")
        if password is not None:
            writer.encrypt(password)
        writer.write(buffer)
        return buffer.getvalue()

    return build


@pytest.fixture()
def cii_path() -> Path:
    return PROJECT_ROOT / "app" / "examples" / "cii-rechnung-demo.xml"


@pytest.fixture()
def ubl_path() -> Path:
    return PROJECT_ROOT / "app" / "examples" / "ubl-rechnung-demo.xml"


@pytest.fixture()
def cii_category_o_path() -> Path:
    return PROJECT_ROOT / "tests" / "fixtures" / "cii-category-o.xml"


@pytest.fixture()
def cii_category_g_mismatch_path() -> Path:
    return PROJECT_ROOT / "tests" / "fixtures" / "cii-category-g-mismatch.xml"


@pytest.fixture()
def ubl_category_o_path() -> Path:
    return PROJECT_ROOT / "tests" / "fixtures" / "ubl-category-o.xml"
