import shutil

import pytesseract
import pytest

from lisovyi.config import PROJECT_ROOT
from lisovyi.domain import TextSource
from lisovyi.ingest.ocr import ReviewItem, find_review_items
from lisovyi.ingest.parsers import extract_text


def _tokens(text: str) -> list[tuple[str, str]]:
    return [(item.token, item.reason) for item in find_review_items(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Внесок становить З шишки з дорослого звіра на місяць. Здавати його треба до 1 числа.",
            [("З", "digit_like_letter"), ("1", "number")],
        ),
        ("гриби на 2–3 день", [("2", "number"), ("3", "number")]),
        ("З ранку до вечора.", []),
        ("Внесок — З вербові гілки", [("З", "digit_like_letter")]),
        ("Внесок становить\nЗ шишки", [("З", "digit_like_letter")]),
        ("– З поля не пускати", []),
        ("Отже. З лісу", []),
        # Маркер списку окремим рядком, як його виводять PDF-екстрактори.
        ("Правила\n–\nЗ поля не пускати", []),
        ("Сходимося О пів на сьому", [("О", "digit_like_letter")]),
        ("Внесок становить З.", []),
    ],
)
def test_find_review_items(text, expected):
    assert _tokens(text) == expected


def test_review_items_point_at_their_line():
    assert find_review_items("Перший рядок\nВнесок становить\nЗ шишки, 12 жолудів") == [
        ReviewItem(3, "З шишки, 12 жолудів", "З", "digit_like_letter"),
        ReviewItem(3, "З шишки, 12 жолудів", "12", "number"),
    ]


@pytest.mark.ocr
def test_real_ocr_reads_scan_without_transcript():
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract binary is not installed")
    if "ukr" not in pytesseract.get_languages():
        pytest.skip("tesseract has no 'ukr' language data")
    name = "Стежки до сусідів від рукавички У соснах.pdf"
    data = (PROJECT_ROOT / "data" / "archive" / name).read_bytes()

    result = extract_text(data, name, transcripts=None)

    assert result.source == TextSource.OCR
    assert "кроків" in result.text
    assert "вирубку" in result.text
    assert result.review_items
