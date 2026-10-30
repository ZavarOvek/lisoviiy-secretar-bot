"""Розпізнавання місця в питанні: рукавички з Карти лісу і частини лісу."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lisovyi.domain import PART_NAMES, ForestPart
from lisovyi.search.morph import tokens

# Ключі частин фіксовані разом з enum ForestPart. Ключі рукавичок — ні: їх дає карта.
PART_KEYS = {
    ForestPart.KHVOINYI: frozenset({"хвойний"}),
    ForestPart.LYSTIANYI: frozenset({"листяний"}),
    ForestPart.RICHKA: frozenset({"річка", "прибережжя"}),
}


@dataclass(frozen=True)
class Places:
    gloves: list[str]
    parts: list[ForestPart]


class PlaceResolver:
    def __init__(self, forest_map: Mapping[str, Sequence[str]]):
        """`forest_map`: назва рукавички → її частини лісу, як у таблиці forest_map."""
        self._parts_of = {
            glove: tuple(ForestPart(part) for part in parts) for glove, parts in forest_map.items()
        }
        self._glove_keys = {glove: _glove_key(glove) for glove in forest_map}
        self._place_lemmas = frozenset().union(*self._glove_keys.values(), *PART_KEYS.values())
        # Усі слова назв місць: «бобрової» з «Біля бобрової греблі», «ліс» з «хвойний ліс».
        # Ключі теж: «прибережжя» — ключ частини, хоч у назві «біля річки» його немає.
        names = [*forest_map, *PART_NAMES.values()]
        self._name_lemmas = self._place_lemmas.union(
            *(token for name in names for token in tokens(name))
        )

    @property
    def gloves(self) -> list[str]:
        return list(self._parts_of)

    def parts_of(self, glove: str) -> tuple[ForestPart, ...]:
        return self._parts_of[glove]

    def resolve(self, text: str) -> Places:
        """Рукавички й частини, названі в тексті, у порядку першої появи, без повторів."""
        gloves: list[str] = []
        parts: list[ForestPart] = []
        for token in tokens(text):
            if not token:
                continue
            gloves.extend(
                g for g, key in self._glove_keys.items() if token & key and g not in gloves
            )
            parts.extend(p for p, key in PART_KEYS.items() if token & key and p not in parts)
        return Places(gloves, parts)

    def allowed_parts(self, places: Places) -> list[ForestPart]:
        """Названі частини ∪ частини названих рукавичок, у порядку ForestPart."""
        allowed = set(places.parts)
        for glove in places.gloves:
            allowed.update(self._parts_of[glove])
        return [part for part in ForestPart if part in allowed]

    def is_place_token(self, token: frozenset[str]) -> bool:
        return bool(token & self._place_lemmas)

    def is_place_name_token(self, token: frozenset[str]) -> bool:
        """Слово з назви якогось місця, не обов'язково ключове («бобрової», «ліс»)."""
        return bool(token & self._name_lemmas)


def _glove_key(glove: str) -> frozenset[str]:
    """Леми останнього слова назви: «Під ялиною» → {ялина}."""
    words = tokens(glove)
    key = words[-1] if words else frozenset()
    if not key:
        raise ValueError(f"cannot derive a search key from the glove name {glove!r}")
    return key
