import pytest

from lisovyi.config import PROJECT_ROOT
from lisovyi.domain import ForestPart
from lisovyi.ingest.forest_map import MapParseError, parse_forest_map, parts_in
from lisovyi.ingest.parsers import extract_text

KH, LY, RI = ForestPart.KHVOINYI, ForestPart.LYSTIANYI, ForestPart.RICHKA

EXPECTED = {
    "Під ялиною": (KH,),
    "У соснах": (KH,),
    "Біля мурашника": (KH,),
    "Під дубом": (LY,),
    "У ліщині": (LY,),
    "Під вербою": (LY, RI),
    "Біля бобрової греблі": (RI,),
    "На узліссі": (),
}

LEGEND = [
    "Під ялиною — хвойний ліс.",
    "У соснах — хвойний ліс.",
    "Біля мурашника — хвойний ліс.",
    "Під дубом — листяний ліс.",
    "У ліщині — листяний ліс.",
    "Під вербою — і листяний ліс, і біля річки водночас.",
    "Біля бобрової греблі — біля річки.",
    "На узліссі — ні хвойний, ні листяний ліс, і до річки далеко.",
]


def _map_text(legend_lines: list[str]) -> str:
    return "\n".join(
        [
            "Карта лісу",
            "Ця карта для всіх мешканців лісу. Частин три: хвойний ліс, листяний ліс і біля річки.",
            "Легенда карти",
            *legend_lines,
            "Дві особливі рукавички",
            "Рукавичка «На узліссі» не належить до жодної з частин: вона ні в хвойному",
            "лісі, ні в листяному, і до річки звідти далеко.",
        ]
    )


def test_archive_map():
    name = "Карта лісу.pdf"
    text = extract_text((PROJECT_ROOT / "data" / "archive" / name).read_bytes(), name, None).text
    forest_map = parse_forest_map(text)
    assert forest_map == EXPECTED
    assert list(forest_map) == list(EXPECTED)


def test_list_marker_on_its_own_line():
    lines = [line for entry in LEGEND for line in ("–", entry)]
    assert parse_forest_map(_map_text(lines)) == EXPECTED


def test_list_marker_on_the_same_line():
    assert parse_forest_map(_map_text([f"– {entry}" for entry in LEGEND])) == EXPECTED


def test_without_list_markers():
    assert parse_forest_map(_map_text(LEGEND)) == EXPECTED


@pytest.mark.parametrize(
    "lines",
    [
        # Перенесений опис не приклеюється до попередньої рукавички.
        LEGEND[:5] + ["– Під вербою — і листяний ліс, і біля", "річки водночас."] + LEGEND[6:],
        # Дефіс замість тире посеред легенди.
        LEGEND[:3] + ["Під дубом - листяний ліс."] + LEGEND[4:],
    ],
)
def test_malformed_line_anywhere_in_legend_is_an_error(lines):
    with pytest.raises(MapParseError, match="not '<glove> — <description>'"):
        parse_forest_map(_map_text(lines))


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("ні хвойний, ні листяний ліс, і до річки далеко.", ()),
        ("ні в хвойному лісі, а біля річки", (RI,)),
        ("хвойний ліс, але ні листяний", (KH,)),
        ("осінні хвойні сосни", (KH,)),
    ],
)
def test_negation(description, expected):
    assert parts_in(description) == expected


def test_missing_legend_is_an_error():
    with pytest.raises(MapParseError, match="Легенда карти"):
        parse_forest_map("Карта лісу\nПід ялиною — хвойний ліс.")


def test_missing_legend_end_is_an_error():
    with pytest.raises(MapParseError, match="Дві особливі"):
        parse_forest_map("Легенда карти\nПід ялиною — хвойний ліс.")


def test_duplicate_glove_is_an_error():
    with pytest.raises(MapParseError, match="twice"):
        parse_forest_map(_map_text(LEGEND + LEGEND[:1]))


def test_legend_line_without_separator_is_an_error():
    with pytest.raises(MapParseError, match="not '<glove> — <description>'"):
        parse_forest_map(_map_text(["Під ялиною хвойний ліс"]))
