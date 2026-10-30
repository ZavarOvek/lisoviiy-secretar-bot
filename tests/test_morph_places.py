import pytest

from lisovyi.domain import ForestPart
from lisovyi.search.morph import bag, lemma_set, normalize, tokens
from lisovyi.search.places import PlaceResolver

FOREST_MAP = {
    "Під ялиною": ["khvoinyi"],
    "Під дубом": ["lystianyi"],
    "Під вербою": ["lystianyi", "richka"],
    "На узліссі": [],
}


@pytest.fixture(scope="module")
def resolver():
    return PlaceResolver(FOREST_MAP)


# --- морфологія


@pytest.mark.parametrize(
    ("word", "lemma"),
    [("соснах", "сосна"), ("дуба", "дуб"), ("ліщину", "ліщина"), ("греблі", "гребля")],
)
def test_lemma_set_contains_the_right_lemma(word, lemma):
    assert lemma in lemma_set(word)


def test_adjective_is_not_its_noun():
    assert "дуб" not in lemma_set("дубове")


@pytest.mark.parametrize("word", ["біля", "в", "із", "ми", "він", "коли", "це", "треба", "можна"])
def test_function_and_stop_words_are_dropped(word):
    assert lemma_set(word) == frozenset()


def test_normalize():
    assert normalize("Пам’ять І памʼять") == "пам'ять і пам'ять"


def test_bag_counts_each_lemma_of_a_token_once():
    assert bag("сосна сосна")["сосна"] == 2
    assert all(count == 1 for count in bag("соснах").values())
    assert tokens("біля дуба") == [frozenset(), lemma_set("дуба")]


# --- резолвер


def test_gloves_in_order_of_appearance():
    resolver = PlaceResolver({"Під ялиною": ["khvoinyi"], "Під дубом": ["lystianyi"]})
    places = resolver.resolve("Як дійти від ялини до дуба?")
    assert places.gloves == ["Під ялиною", "Під дубом"]
    assert resolver.resolve("Як дійти від дуба до ялини?").gloves == ["Під дубом", "Під ялиною"]


def test_adjective_does_not_name_a_glove(resolver):
    assert resolver.resolve("Насипте дубового листя").gloves == []


def test_new_glove_in_the_map_is_recognised_without_code_changes():
    resolver = PlaceResolver({**FOREST_MAP, "Біля старого клена": ["lystianyi"]})
    places = resolver.resolve("Що робити біля клена?")
    assert places.gloves == ["Біля старого клена"]
    assert resolver.allowed_parts(places) == [ForestPart.LYSTIANYI]


def test_part_without_glove(resolver):
    places = resolver.resolve("Які ягоди є в хвойному лісі?")
    assert (places.gloves, places.parts) == ([], [ForestPart.KHVOINYI])


def test_allowed_parts_union(resolver):
    places = resolver.resolve("Як дійти від ялини до верби?")
    assert resolver.allowed_parts(places) == [
        ForestPart.KHVOINYI,
        ForestPart.LYSTIANYI,
        ForestPart.RICHKA,
    ]
    assert resolver.allowed_parts(resolver.resolve("Що на узліссі?")) == []


def test_glove_name_without_a_key_is_an_error():
    with pytest.raises(ValueError, match="search key"):
        PlaceResolver({"Під нами": []})
