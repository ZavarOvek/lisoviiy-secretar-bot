"""Розширення питання синонімами. Застосовується тільки до питання, не до документів."""

from collections import Counter
from functools import cache

from lisovyi.search.morph import lemma_set

# Звичайні слова з обох боків: леми з них виводяться під час завантаження.
# Без таблиці «Коли готуватися до холодів?» не знаходить жодного документа про утеплення.
SYNONYMS = {
    "холод": ["утеплювати", "утеплити", "утеплення", "сплячка"],
    "мороз": ["утеплювати", "утеплити", "утеплення"],
    "зима": ["сплячка", "утеплювати", "утеплити"],
    "засинати": ["сплячка", "лягати"],
    "заснути": ["сплячка", "лягати"],
    "спати": ["сплячка"],
    "їсти": ["поживитися", "пожива"],
    "їжа": ["поживитися", "пожива", "їсти"],
    "поїсти": ["поживитися", "пожива", "їсти"],
    "харчі": ["поживитися", "пожива", "їсти"],
    "небезпечно": ["небезпека"],
    "остерігатися": ["небезпека"],
    "боятися": ["небезпека"],
    "дійти": ["стежка", "крок"],
    "дістатися": ["стежка", "крок"],
    "дорога": ["стежка", "крок"],
    "платити": ["внесок"],
    "гроші": ["внесок"],
}


@cache
def lemmatized_synonyms() -> tuple[tuple[frozenset[str], frozenset[str]], ...]:
    """(леми ключа, леми всіх додаткових слів) для кожного запису таблиці."""
    table = []
    for word, extra_words in SYNONYMS.items():
        key = lemma_set(word)
        extra = frozenset().union(*(lemma_set(extra_word) for extra_word in extra_words))
        if not key or not extra:
            raise ValueError(f"synonym entry {word!r} lemmatizes to nothing")
        table.append((key, extra))
    return tuple(table)


def expand(query: Counter[str]) -> Counter[str]:
    """Мішок питання плюс леми синонімів; кожна додана лема має вагу 1."""
    expanded = Counter(query)
    for key, extra in lemmatized_synonyms():
        if key & query.keys():
            for lemma in extra:
                expanded.setdefault(lemma, 1)
    return expanded
