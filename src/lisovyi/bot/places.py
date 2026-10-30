"""Кнопки місць: рукавички з карти, частини лісу й увесь ліс.

callback_data несе не назву й не позицію, а хеш назви. Карта змінюється, а старі кнопки
лишаються в історії чатів: хеш або знайде те саме місце, або голосно не знайде жодного.
Позиція ж тихо вказала б на іншу рукавичку, а довга назва не влізла б у 64 байти.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from lisovyi.domain import GENERAL_SCOPE, PART_NAMES, DocumentKind, ForestPart

PLACE_PREFIX = "place:"
HASH_CHARS = 10
WHOLE_FOREST = "Увесь ліс"
BUTTONS_PER_ROW = 2


@dataclass(frozen=True)
class Place:
    # Текст кнопки; з нього ж рахується хеш.
    label: str
    kind: DocumentKind
    scope: str

    @property
    def key(self) -> str:
        return place_hash(self.label)


def place_hash(label: str) -> str:
    return hashlib.sha1(label.encode("utf-8")).hexdigest()[:HASH_CHARS]


def glove_places(gloves: Sequence[str]) -> list[Place]:
    return [Place(glove, DocumentKind.GLOVE, glove) for glove in gloves]


def all_places(gloves: Sequence[str]) -> list[Place]:
    """Рукавички в порядку карти, далі три частини лісу, далі весь ліс."""
    parts = [
        Place(name[:1].upper() + name[1:], DocumentKind.PART, part.value)
        for part, name in PART_NAMES.items()
    ]
    return [*glove_places(gloves), *parts, Place(WHOLE_FOREST, DocumentKind.GENERAL, GENERAL_SCOPE)]


_PART_FOR_WHOM = {
    ForestPart.KHVOINYI: "хвойного лісу",
    ForestPart.LYSTIANYI: "листяного лісу",
    ForestPart.RICHKA: "тих, хто біля річки",
}


def human_place(kind: DocumentKind, scope: str) -> str:
    """Місце людською мовою після «для»: рукавички «…», хвойного лісу, усього лісу."""
    if kind == DocumentKind.GLOVE:
        return f"рукавички «{scope}»"
    if kind == DocumentKind.PART:
        return _PART_FOR_WHOM[ForestPart(scope)]
    return "усього лісу"


def find_place(key: str, places: Sequence[Place]) -> Place | None:
    return next((place for place in places if place.key == key), None)


def place_keyboard(
    places: Sequence[Place], prefix: str = PLACE_PREFIX
) -> InlineKeyboardMarkup | None:
    """Кнопка на кожне місце, по дві в ряд; callback_data — `prefix` + хеш назви."""
    if not places:
        return None
    buttons = [
        InlineKeyboardButton(text=place.label, callback_data=prefix + place.key) for place in places
    ]
    rows = [buttons[i : i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(inline_keyboard=rows)
