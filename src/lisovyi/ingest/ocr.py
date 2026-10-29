"""OCR сканованих PDF і пошук місць, яким OCR не варто довіряти."""

import bisect
import re
from dataclasses import dataclass

import pypdfium2
import pytesseract
from PIL import Image

# Підібрано на архівних сканах із фоном «береста»: без бінаризації цифри плутаються
# («350» → «850»). Не міняти без вимірювання на цих сканах.
OCR_DPI = 300
BINARIZE_THRESHOLD = 110
TESSERACT_LANG = "ukr"
TESSERACT_CONFIG = "--psm 6"


def _binarize(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    return gray.point([0 if value < BINARIZE_THRESHOLD else 255 for value in range(256)])


def ocr_pdf(data: bytes) -> str:
    """Розпізнає всі сторінки PDF і повертає текст, сторінки розділені переносом рядка."""
    pdf = pypdfium2.PdfDocument(data)
    try:
        pages = []
        for page in pdf:
            bitmap = page.render(scale=OCR_DPI / 72, grayscale=True)
            image = _binarize(bitmap.to_pil())
            pages.append(
                pytesseract.image_to_string(image, lang=TESSERACT_LANG, config=TESSERACT_CONFIG)
            )
        return "\n".join(pages)
    finally:
        pdf.close()


@dataclass(frozen=True)
class ReviewItem:
    line_no: int  # з 1
    line: str
    token: str
    reason: str  # "number" | "digit_like_letter"


_NUMBER = re.compile(r"\d+")
# Одиночна кирилична велика «З» або «О», за якою (через пробіли чи перенос) іде слово.
_DIGIT_LIKE = re.compile(r"(?<!\w)[ЗО](?=\s+[^\W\d_])")
_LIST_MARKER_LINE = re.compile(r"^[ \t]*[–-]$")


def _starts_sentence(before: str) -> bool:
    """Чи починається речення одразу після тексту `before`.

    Початок речення: початок тексту, позиція після `.`/`!`/`?` (через будь-які пробіли й
    переноси) або після маркера списку `–`/`-` на початку рядка. Маркер може стояти й
    окремим рядком: так його виводять PDF-екстрактори. Перенос рядка сам по собі
    початком речення не є, бо OCR переносить рядки посеред речення.
    """
    stripped = before.rstrip()
    if not stripped or stripped[-1] in ".!?":
        return True
    last_line = stripped.rsplit("\n", 1)[-1]
    return bool(_LIST_MARKER_LINE.match(last_line))


def find_review_items(text: str) -> list[ReviewItem]:
    """Місця в OCR-тексті, які варто перепитати: числа й «З»/«О», схожі на 3 і 0."""
    found: list[tuple[int, ReviewItem]] = []
    line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
    lines = text.split("\n")

    def item(pos: int, token: str, reason: str) -> tuple[int, ReviewItem]:
        line_idx = bisect.bisect_right(line_starts, pos) - 1
        return pos, ReviewItem(line_idx + 1, lines[line_idx], token, reason)

    for match in _NUMBER.finditer(text):
        found.append(item(match.start(), match.group(), "number"))
    for match in _DIGIT_LIKE.finditer(text):
        if not _starts_sentence(text[: match.start()]):
            found.append(item(match.start(), match.group(), "digit_like_letter"))
    return [review_item for _, review_item in sorted(found, key=lambda pair: pair[0])]
