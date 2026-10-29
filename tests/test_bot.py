import pytest

from lisovyi.bot.handlers import documents_word, start_text


def test_start_text():
    assert start_text(78) == "Лісовий архів скоро відкриється. У архіві зараз 78 документів."


@pytest.mark.parametrize(
    ("n", "word"),
    [
        (0, "документів"),
        (1, "документ"),
        (2, "документи"),
        (4, "документи"),
        (5, "документів"),
        (11, "документів"),
        (14, "документів"),
        (21, "документ"),
        (22, "документи"),
        (111, "документів"),
    ],
)
def test_documents_word(n, word):
    assert documents_word(n) == word
