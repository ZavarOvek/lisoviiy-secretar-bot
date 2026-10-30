import pytest

from lisovyi.answer.dialogue import DialogueMemory
from lisovyi.search.retrieve import is_place_followup, retrieve, strip_places


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def resolver(archive_search):
    return archive_search[1]


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def memory(clock):
    return DialogueMemory(ttl=900, clock=clock)


# --- strip_places


@pytest.mark.parametrize(
    ("question", "stripped"),
    [
        ("Які гриби біля сосон?", "Які гриби?"),
        ("Коли йти по гриби під вербою?", "Коли йти по гриби?"),
        ("Чим утеплити рукавичку біля бобрової греблі?", "Чим утеплити рукавичку?"),
        ("Що росте у хвойному лісі?", "Що росте?"),
        ("Як дійти від ялини до дуба?", "Як дійти?"),
        (
            "Скільки внесок у ліщині і що буде, якщо не заплатити?",
            "Скільки внесок і що буде, якщо не заплатити?",
        ),
        ("Які гриби, біля сосон?", "Які гриби?"),
        ("Коли збори мешканців?", "Коли збори мешканців?"),
        ("Під Вербою", ""),
    ],
)
def test_strip_places_removes_the_place_with_its_preposition(question, stripped, resolver):
    assert strip_places(question, resolver) == stripped


def test_strip_places_keeps_other_words_intact(resolver):
    # Регістр і апострофи решти питання не чіпаються.
    assert strip_places("Пам’ять Ведмедя біля сосон", resolver) == "Пам’ять Ведмедя"


@pytest.mark.parametrize(
    ("message", "followup"),
    [
        ("А біля дубів?", True),
        ("У соснах", True),
        ("біля річки", True),
        ("А під вербою?", True),
        ("Під дубом", True),
        ("Біля бобрової греблі", True),
        ("А в листяному лісі?", True),
        ("на прибережжі", True),
        ("Бобрової", False),
        ("Чи можна в лісі?", False),
        ("Які гриби біля дубів?", False),
        ("а де?", False),
        ("", False),
    ],
)
def test_is_place_followup(message, followup, resolver):
    assert is_place_followup(message, resolver) is followup


# --- пам'ять


def test_followup_within_ttl_is_merged(memory, clock, resolver):
    assert memory.merge(1, "Коли збори мешканців?", resolver) == "Коли збори мешканців?"
    clock.now += 899
    assert memory.merge(1, "У соснах", resolver) == "Коли збори мешканців? У соснах"


def test_place_after_ttl_has_nothing_to_clarify(memory, clock, resolver):
    memory.merge(1, "Коли збори мешканців?", resolver)
    clock.now += 901
    assert memory.merge(1, "У соснах", resolver) is None


def test_followup_switches_the_place_instead_of_adding_it(memory, resolver, archive_search):
    index, _ = archive_search
    memory.merge(1, "Які гриби біля сосон?", resolver)

    question = memory.merge(1, "А біля дубів?", resolver)

    assert question == "Які гриби? А біля дубів?"
    assert "сосон" not in question
    assert retrieve(question, index, resolver).gloves == ["Під дубом"]


def test_second_followup_rests_on_the_same_base(memory, clock, resolver):
    memory.merge(1, "Які гриби біля сосон?", resolver)
    memory.merge(1, "А біля дубів?", resolver)
    clock.now += 600
    assert memory.merge(1, "А під вербою?", resolver) == "Які гриби? А під вербою?"
    # Уточнення продовжує розмову: TTL рахується від останньої репліки.
    clock.now += 600
    assert memory.merge(1, "У ліщині", resolver) == "Які гриби? У ліщині"


def test_new_full_question_replaces_the_base(memory, resolver):
    memory.merge(1, "Які гриби біля сосон?", resolver)
    assert memory.merge(1, "Коли збори мешканців?", resolver) == "Коли збори мешканців?"
    assert memory.merge(1, "У соснах", resolver) == "Коли збори мешканців? У соснах"


def test_chats_do_not_share_memory(memory, resolver):
    memory.merge(1, "Коли збори мешканців?", resolver)
    assert memory.merge(2, "У соснах", resolver) is None


def test_expired_chats_are_forgotten(memory, clock, resolver):
    memory.merge(1, "Коли збори мешканців?", resolver)
    clock.now += 901
    memory.merge(2, "Які гриби біля сосон?", resolver)
    assert list(memory._turns) == [2]


@pytest.mark.parametrize("glove", ["Біля бобрової греблі", "Біля мурашника", "На узліссі"])
def test_every_glove_button_is_a_followup(glove, memory, resolver):
    # Натиск кнопки — назва рукавички як є, зокрема з кількох змістовних слів.
    assert resolver.gloves, "archive map is empty"
    memory.merge(1, "Коли збори мешканців?", resolver)
    assert memory.merge(1, glove, resolver) == f"Коли збори мешканців? {glove}"


def test_all_archive_gloves_are_followups(resolver):
    assert [g for g in resolver.gloves if not is_place_followup(g, resolver)] == []


def test_place_without_a_question_does_not_become_the_base(memory, resolver):
    # Порожня пам'ять — як після рестарту.
    assert memory.merge(1, "Під дубом", resolver) is None
    assert memory.merge(1, "У соснах", resolver) is None
    assert memory._turns == {}
    assert memory.merge(1, "Коли збори мешканців?", resolver) == "Коли збори мешканців?"
    assert memory.merge(1, "Під дубом", resolver) == "Коли збори мешканців? Під дубом"
