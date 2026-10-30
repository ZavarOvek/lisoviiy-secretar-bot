"""Розбір легенди «Карти лісу»: які рукавички є в лісі й до яких частин вони належать.

Список рукавичок і відповідність «рукавичка → частини» беруться тільки з документа,
у коді їх немає. Код знає лише словник частин (`ForestPart`) і маркери, якими їх названо.
"""

import re
import unicodedata

from lisovyi.domain import ForestPart

LEGEND_START = "Легенда карти"
LEGEND_END = "Дві особливі"

PART_MARKERS = {
    ForestPart.KHVOINYI: re.compile(r"хвойн", re.IGNORECASE),
    ForestPart.LYSTIANYI: re.compile(r"листян", re.IGNORECASE),
    ForestPart.RICHKA: re.compile(r"біля\s+річ", re.IGNORECASE),
}
# Заперечення «ні» перед маркером, з одним словом між ними або без нього.
_NEGATION_BEFORE = re.compile(r"\bні\s+(?:\S+\s+)?$", re.IGNORECASE)
_LIST_MARKER = re.compile(r"^[–-]\s*")
_ENTRY = re.compile(r"^(?P<glove>\S.*?)\s+—\s+(?P<description>.+)$")


class MapParseError(ValueError):
    pass


def parse_forest_map(text: str) -> dict[str, tuple[ForestPart, ...]]:
    """Рукавичка → частини лісу (у порядку `ForestPart`), у порядку легенди."""
    forest_map: dict[str, tuple[ForestPart, ...]] = {}
    # Назви рукавичок стають scope документів, а classify() порівнює їх у NFC.
    text = unicodedata.normalize("NFC", text)
    for raw in _legend_lines(text):
        line = _LIST_MARKER.sub("", raw.strip())
        if not line:  # порожній рядок або маркер «–» окремим рядком
            continue
        # Кожен рядок легенди — окремий запис. Рядок іншого вигляду (перенесений опис,
        # дефіс замість тире) не вгадуємо: він міг би тихо змінити частини рукавички.
        match = _ENTRY.match(line)
        if not match:
            raise MapParseError(f"legend line is not '<glove> — <description>': {line!r}")
        glove = match["glove"]
        if glove in forest_map:
            raise MapParseError(f"glove {glove!r} appears twice in the legend")
        forest_map[glove] = parts_in(match["description"])
    return forest_map


def _legend_lines(text: str) -> list[str]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == LEGEND_START), None)
    if start is None:
        raise MapParseError(f"no line {LEGEND_START!r} in the map")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith(LEGEND_END)),
        None,
    )
    if end is None:
        raise MapParseError(f"no line starting with {LEGEND_END!r} after the legend")
    return lines[start + 1 : end]


def parts_in(description: str) -> tuple[ForestPart, ...]:
    """Частини, названі в описі без заперечення «ні»."""
    return tuple(
        part
        for part, marker in PART_MARKERS.items()
        if any(
            not _NEGATION_BEFORE.search(description[: match.start()])
            for match in marker.finditer(description)
        )
    )
