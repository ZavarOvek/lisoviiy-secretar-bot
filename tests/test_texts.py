"""Бот звертається до звірів на «ви» (бриф 04, §3.12)."""

import re

import pytest

from lisovyi.config import PROJECT_ROOT

SOURCES = [
    "src/lisovyi/answer/texts.py",
    "src/lisovyi/bot/texts.py",
    "src/lisovyi/bot/handlers.py",
    "src/lisovyi/bot/uploads.py",
]
INFORMAL = re.compile(
    r"\b(надішли|спробуй|напиши|питаєш|хочеш|маєш|твоя|твоє|тобі)\b", re.IGNORECASE
)


@pytest.mark.parametrize("path", SOURCES)
def test_no_informal_address(path):
    text = (PROJECT_ROOT / path).read_text(encoding="utf-8")
    assert INFORMAL.findall(text) == []


def test_the_check_catches_informal_forms():
    assert INFORMAL.findall("Надішли файл. Що хочеш? Твоя рукавичка.") == [
        "Надішли",
        "хочеш",
        "Твоя",
    ]
