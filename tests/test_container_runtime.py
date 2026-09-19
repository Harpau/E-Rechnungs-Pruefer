from pathlib import Path

import pytest

from scripts.test_container_runtime import font_smoke


@pytest.mark.parametrize(
    ("filename", "text"),
    [("NotoSans-Regular.ttf", "ÄÖÜäöüß € – E-Rechnung"), ("NotoSansSC-Variable.ttf", "中文发票")],
)
def test_real_runtime_unicode_pdf_and_native_image_roundtrip(filename: str, text: str) -> None:
    font = Path(__file__).resolve().parents[1] / "app/assets/fonts" / filename
    result = font_smoke(font, text)
    assert result["unicode_preserved"] is True
    assert result["image_formats"] == ["PNG", "JPEG"]
