"""Морфологія для пошуку: слово → множина можливих лем."""

import re
import unicodedata
from collections import Counter
from functools import cache, lru_cache

import pymorphy3

# Службові частини мови: слово відкидається, якщо всі його розбори такі.
FUNCTION_POS = frozenset({"PREP", "CONJ", "PRCL", "INTJ", "NPRO"})
# З цим списком прототип пошуку пройшов усі кейси; міняти лише з вимірюванням на них.
# «можна» pymorphy3 зводить до «можний», тому працює саме друга лема.
# fmt: off
STOP_LEMMAS = frozenset({
    "коли", "як", "що", "де", "чи", "можна", "можний", "треба", "який", "якийсь", "бути",
    "мати", "нас", "свій", "цей", "той", "весь", "такий", "там", "тут", "ще", "вже", "а",
    "і", "й", "та", "це",
})
# fmt: on

_WORD = re.compile(r"[а-яіїєґ']+")


@cache
def analyzer() -> pymorphy3.MorphAnalyzer:
    """Один аналізатор на процес: словник вантажиться ~0,1 с."""
    return pymorphy3.MorphAnalyzer(lang="uk")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower().replace("’", "'").replace("ʼ", "'")


@lru_cache(maxsize=65536)
def lemma_set(word: str) -> frozenset[str]:
    """Нормальні форми з усіх розборів слова; порожня множина для службових і стоп-слів.

    Усі розбори, бо перший для української ненадійний: «соснах» → «сосни», «дуба» → «дуба».
    """
    word = normalize(word).strip("'")
    if not word:
        return frozenset()
    parses = analyzer().parse(word)
    if all(parse.tag.POS in FUNCTION_POS for parse in parses):
        return frozenset()
    lemmas = frozenset(parse.normal_form for parse in parses)
    if lemmas & STOP_LEMMAS:
        return frozenset()
    return lemmas


def tokens(text: str) -> list[frozenset[str]]:
    """Множини лем слів тексту по порядку; службові слова дають порожні множини."""
    return [lemma_set(word) for word in _WORD.findall(normalize(text))]


def bag(text: str) -> Counter[str]:
    """Мішок лем: кожна лема кожного токена рахується один раз."""
    counter: Counter[str] = Counter()
    for token in tokens(text):
        counter.update(token)
    return counter
