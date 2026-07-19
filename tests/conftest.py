from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
