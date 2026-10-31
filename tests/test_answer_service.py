import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from groq_fakes import FakeGroq, completion

from lisovyi.answer import texts
from lisovyi.answer.llm import Completion, LLMBroken, LLMBusy, LLMClient, OutputRejected, Usage
from lisovyi.answer.service import MAX_MESSAGE_CHARS, AnswerService, clean_text, truncate
from lisovyi.domain import DocumentKind, ForestPart
from lisovyi.search import service as search_module
from lisovyi.search.index import Bm25Index, SearchDocument
from lisovyi.search.places import PlaceResolver
from lisovyi.search.service import SearchService

USAGE = Usage(prompt_tokens=1000, cached_tokens=600, completion_tokens=100)


def reply_json(kind: str = "answer", text: str = "Так.", sources: Any = (1,)) -> Completion:
    content = json.dumps({"kind": kind, "text": text, "sources": list(sources)}, ensure_ascii=False)
    return raw_completion(content)


def raw_completion(content: str, finish_reason: str = "stop") -> Completion:
    return Completion("main-model", content, finish_reason, USAGE, 0.5)


class FakeLLM:
    """Мок на рівні LLMClient: черга відповідей або винятків."""

    def __init__(self, *outcomes: Any):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[dict[str, str]], Any]] = []

    async def complete(self, messages, notify=None):
        self.calls.append((messages, notify))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def assert_not_called(self):
        assert self.calls == []

    def question(self, call: int = -1) -> str:
        user = self.calls[call][0][1]["content"]
        return user.split("\n", 1)[0].removeprefix("Питання звіра: ")


def loaded_search(index: Bm25Index, resolver: PlaceResolver) -> SearchService:
    search = SearchService(None)
    search._state = search_module._State(index, resolver)
    return search


@pytest.fixture
def search(archive_search):
    return loaded_search(*archive_search)


def service(search: SearchService, llm: Any) -> AnswerService:
    return AnswerService(search, llm, dialogue_ttl=900)


# --- без LLM


async def test_not_found_does_not_call_the_model(search):
    llm = FakeLLM()
    reply = await service(search, llm).answer(1, "Коли прилітають лелеки?")
    llm.assert_not_called()
    assert (reply.text, reply.kind, reply.keyboard, reply.sources) == (
        texts.NOT_FOUND,
        "not_found",
        [],
        [],
    )


async def test_ask_glove_offers_every_glove_in_map_order(search):
    llm = FakeLLM()
    reply = await service(search, llm).answer(1, "Коли збори мешканців?")
    llm.assert_not_called()
    assert (reply.text, reply.kind) == (texts.ASK_GLOVE, "ask_glove")
    assert reply.keyboard == [
        "Під ялиною",
        "У соснах",
        "Біля мурашника",
        "Під дубом",
        "У ліщині",
        "Під вербою",
        "Біля бобрової греблі",
        "На узліссі",
    ]


async def test_glove_button_answers_the_saved_question(search):
    llm = FakeLLM(reply_json(sources=[1]))
    answers = service(search, llm)
    await answers.answer(1, "Коли збори мешканців?")
    reply = await answers.answer(1, "У соснах")
    assert llm.question() == "Коли збори мешканців? У соснах"
    assert reply.sources == ["Статут ОСМД У соснах.pdf"]


# --- валідація відповіді


async def test_sources_outside_the_pack_are_dropped(search):
    llm = FakeLLM(reply_json(sources=[99, 2, 0, 2, -1, 1]))
    reply = await service(search, llm).answer(1, "Які гриби біля сосон?")
    assert len(llm.calls) == 1
    assert reply.sources == reply.context[:2]
    assert reply.retries == 0


@pytest.mark.parametrize(
    "bad",
    [
        reply_json(sources=[]),
        reply_json(kind="variants", sources=[42]),
        reply_json(kind="partial", sources=[True]),
        raw_completion("{не json"),
        raw_completion('["answer"]'),
        raw_completion('{"kind": "maybe", "text": "Так.", "sources": [1]}'),
        reply_json(text="  **  "),
        raw_completion('{"kind": "answer", "text": "Утеплюйте', finish_reason="length"),
        OutputRejected(),
    ],
    ids=[
        "empty-sources",
        "only-foreign-sources",
        "bool-source",
        "invalid-json",
        "not-an-object",
        "unknown-kind",
        "empty-text",
        "length",
        "rejected-by-groq",
    ],
)
async def test_unusable_answer_is_retried_once(bad, search):
    llm = FakeLLM(bad, reply_json(sources=[1]))
    reply = await service(search, llm).answer(1, "Які гриби біля сосон?")
    assert len(llm.calls) == 2
    assert llm.calls[0][0] == llm.calls[1][0]
    assert (reply.kind, reply.retries) == ("answer", 1)
    assert reply.sources == reply.context[:1]


async def test_second_unusable_answer_gives_the_fallback_text(search, caplog):
    llm = FakeLLM(reply_json(sources=[]), reply_json(sources=[]))
    with caplog.at_level(logging.WARNING):
        reply = await service(search, llm).answer(1, "Які гриби біля сосон?")
    assert len(llm.calls) == 2
    assert (reply.text, reply.kind, reply.sources) == (texts.UNSURE, "unsure", [])
    assert "no usable answer: question='Які гриби біля сосон?'" in caplog.text
    assert '"sources": []' in caplog.text


@pytest.mark.parametrize("kind", ["not_found", "ask_place"])
async def test_not_found_and_ask_place_may_have_no_sources(kind, search):
    llm = FakeLLM(reply_json(kind=kind, text="Нема.", sources=[]))
    reply = await service(search, llm).answer(1, "Які гриби біля сосон?")
    assert len(llm.calls) == 1
    assert (reply.kind, reply.text) == (kind, "Нема.")


async def test_usage_is_summed_over_attempts(search):
    llm = FakeLLM(raw_completion("{"), reply_json())
    reply = await service(search, llm).answer(1, "Які гриби біля сосон?")
    assert reply.usage == USAGE + USAGE


# --- блок джерел

FOREST_MAP = {"Під дубом": ["lystianyi"], "Під ялиною": ["khvoinyi"]}


def _doc(document_id: int, kind, scope: str, title: str, **extra) -> SearchDocument:
    return SearchDocument(
        document_id=document_id,
        filename=f"{title}.txt",
        title=title,
        kind=kind,
        scope=scope,
        topic=f"t{document_id}",
        text="мед мед мед",
        **extra,
    )


@pytest.fixture
def honey_search():
    docs = [
        _doc(1, DocumentKind.GLOVE, "Під дубом", "Порада про мед", author_id=7, author_name="Їжак",
             created_at=datetime(2026, 10, 29, 22, 30, tzinfo=UTC)),
        _doc(2, DocumentKind.PART, "lystianyi", "Мед листяного лісу"),
        _doc(3, DocumentKind.GENERAL, "general", "Календар меду"),
    ]  # fmt: skip
    return loaded_search(Bm25Index.build(docs), PlaceResolver(FOREST_MAP))


async def test_sources_block_is_built_by_the_bot_in_number_order(honey_search):
    llm = FakeLLM(reply_json(text="Мед — у дуплі.", sources=[3, 1]))
    reply = await service(honey_search, llm).answer(1, "Де мед під дубом?")
    assert reply.text == (
        "Мед — у дуплі.\n\nДжерела:\n— Порада про мед (автор: Їжак, 30.10.2026)\n— Календар меду"
    )
    assert reply.sources == ["Порада про мед.txt", "Календар меду.txt"]


async def test_not_found_from_the_model_has_no_sources_block(honey_search):
    llm = FakeLLM(reply_json(kind="not_found", text="Про це записів немає.", sources=[1]))
    reply = await service(honey_search, llm).answer(1, "Де мед під дубом?")
    assert (reply.text, reply.sources) == ("Про це записів немає.", [])


@pytest.mark.parametrize(("kind", "buttons"), [("variants", True), ("ask_place", True),
                                               ("answer", False), ("partial", False)])  # fmt: skip
async def test_glove_buttons_under_variants_and_ask_place(kind, buttons, honey_search):
    llm = FakeLLM(reply_json(kind=kind, sources=[1]))
    reply = await service(honey_search, llm).answer(1, "Де мед під дубом?")
    assert reply.keyboard == (["Під дубом", "Під ялиною"] if buttons else [])


# --- текст


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("У вашій рукавичці «У соснах» горіхи…", "У рукавичці «У соснах» горіхи…"),
        ("Для вашої рукавички це листопад", "Для рукавички це листопад"),
        ("Ваша рукавичка «Під дубом» тепла.", "Рукавичка «Під дубом» тепла."),
        ("Над вашою рукавичкою летить сова.", "Над рукавичкою летить сова."),
        ("Вашу рукавичку видно здалеку.", "Рукавичку видно здалеку."),
        ("Утепліть ваші рукавички мохом.", "Утепліть рукавички мохом."),
        ("Дякуємо за ваші поради і вашу пораду.", "Дякуємо за ваші поради і вашу пораду."),
        ("Ваш дім — ваша фортеця.", "Ваш дім — ваша фортеця."),
    ],
)
def test_clean_text_drops_presumed_possession_of_the_glove(raw, clean):
    assert clean_text(raw) == clean


def test_clean_text_drops_source_mentions():
    raw = "Їжак спить з листопада (джерело 1). Загальне — грудень (джерела 4, 5)."
    assert clean_text(raw) == "Їжак спить з листопада. Загальне — грудень."
    assert clean_text("Гриби (Джерело 2 і 3) ростуть.") == "Гриби ростуть."


def test_clean_text_drops_markdown():
    assert clean_text("**жирне** і __підкреслене__") == "жирне і підкреслене"
    assert clean_text("## Гриби\n  # Ще\n— маслюки #1") == "Гриби\nЩе\n— маслюки #1"


def test_truncate_on_a_sentence_boundary():
    sentence = "Утеплюй рукавичку мохом у жовтні. "
    text = sentence * 200
    cut = truncate(text, 4000)
    assert len(cut) <= 4000
    assert cut.endswith("жовтні.…")
    assert cut[:-1] in text
    assert truncate("Коротко.", 4000) == "Коротко."


def test_truncate_without_sentence_end_cuts_on_a_space():
    assert truncate("слово " * 10, 20) == "слово слово слово…"


async def test_whole_message_stays_within_the_limit(honey_search):
    llm = FakeLLM(reply_json(text="Мед у дуплі. " * 400, sources=[1, 2, 3]))
    reply = await service(honey_search, llm).answer(1, "Де мед під дубом?")
    assert len(reply.text) <= MAX_MESSAGE_CHARS
    assert "у дуплі.…\n\nДжерела:\n" in reply.text


# --- помилки LLM


@pytest.mark.parametrize(
    ("error", "text", "kind"),
    [(LLMBusy(), texts.BUSY, "busy"), (LLMBroken(), texts.BROKEN, "error")],
)
async def test_llm_errors_become_friendly_texts(error, text, kind, search):
    llm = FakeLLM(error)
    reply = await service(search, llm).answer(1, "Які гриби біля сосон?")
    assert (reply.text, reply.kind, reply.sources) == (text, kind, [])


async def test_intermediate_message_is_sent_once_per_question(search):
    class WaitingLLM(FakeLLM):
        async def complete(self, messages, notify=None):
            await notify(texts.WAIT)
            return await super().complete(messages, notify)

    llm = WaitingLLM(reply_json(sources=[]), reply_json())
    sent = []

    async def notify(text: str) -> None:
        sent.append(text)

    reply = await service(search, llm).answer(1, "Які гриби біля сосон?", notify)
    assert len(llm.calls) == 2
    assert sent == [texts.WAIT]
    assert reply.kind == "answer"


async def test_multi_word_glove_button_answers_the_saved_question(search):
    llm = FakeLLM(reply_json(sources=[1]))
    answers = service(search, llm)
    await answers.answer(1, "Коли збори мешканців?")
    await answers.answer(1, "Біля бобрової греблі")
    assert llm.question() == "Коли збори мешканців? Біля бобрової греблі"


# --- пам'ять і черги


async def test_followup_switches_the_place(search):
    llm = FakeLLM(reply_json(), reply_json())
    answers = service(search, llm)
    await answers.answer(1, "Які гриби біля сосон?")
    await answers.answer(1, "А біля дубів?")

    assert llm.question() == "Які гриби? А біля дубів?"
    user = llm.calls[-1][0][1]["content"]
    assert "Місце: Під дубом (листяний ліс)" in user
    assert "сосон" not in user.split("\n", 1)[0]


async def test_messages_of_one_chat_are_handled_in_order(search):
    llm = FakeLLM(reply_json(), reply_json())
    answers = service(search, llm)
    await asyncio.gather(
        answers.answer(1, "Які гриби біля сосон?"), answers.answer(1, "А біля дубів?")
    )
    assert [llm.question(0), llm.question(1)] == [
        "Які гриби біля сосон?",
        "Які гриби? А біля дубів?",
    ]


async def test_two_answers_never_call_the_model_in_parallel(search):
    ok = completion({"kind": "answer", "text": "Так.", "sources": [1]})
    fake = FakeGroq(ok, ok, delay=0.02)
    answers = service(search, LLMClient(fake, "main-model"))
    replies = await asyncio.gather(
        answers.answer(1, "Які гриби біля сосон?"), answers.answer(2, "Коли збирати горіхи?")
    )
    assert [reply.kind for reply in replies] == ["answer", "answer"]
    assert len(fake.calls) == 2
    assert fake.max_active == 1


# --- саме місце без питання (D2)


@pytest.mark.parametrize(
    ("message", "text"),
    [
        (
            "Біля бобрової греблі",
            "Що саме ви хочете дізнатися про рукавичку «Біля бобрової греблі»? Напишіть питання "
            "повністю — наприклад, про гриби, стежки чи внески.",
        ),
        (
            "А в хвойному лісі?",
            "Що саме ви хочете дізнатися про хвойний ліс? Напишіть питання повністю.",
        ),
        (
            "листяний ліс",
            "Що саме ви хочете дізнатися про листяний ліс? Напишіть питання повністю.",
        ),
        (
            "біля річки",
            "Що саме ви хочете дізнатися про тих, хто біля річки? Напишіть питання повністю.",
        ),
        (
            "Під дубом і під ялиною",
            "Що саме ви хочете дізнатися? Напишіть питання повністю й назвіть місце.",
        ),
        (
            "Під вербою біля річки",
            "Що саме ви хочете дізнатися про рукавичку «Під вербою»? Напишіть питання "
            "повністю — наприклад, про гриби, стежки чи внески.",
        ),
        (
            "Під дубом біля річки",
            "Що саме ви хочете дізнатися? Напишіть питання повністю й назвіть місце.",
        ),
    ],
    ids=[
        "glove",
        "khvoinyi",
        "lystianyi",
        "richka",
        "two-gloves",
        "glove-and-its-part",
        "glove-and-foreign-part",
    ],
)
async def test_place_without_a_question_asks_for_it_without_the_model(message, text, search):
    llm = FakeLLM()
    reply = await service(search, llm).answer(1, message)
    llm.assert_not_called()
    assert (reply.text, reply.kind, reply.keyboard, reply.sources) == (text, "ask_question", [], [])


async def test_place_after_ttl_asks_for_the_question(search):
    llm = FakeLLM()
    answers = service(search, llm)
    answers.dialogue.ttl = -1  # будь-яка збережена репліка вже протухла
    await answers.answer(1, "Коли збори мешканців?")
    reply = await answers.answer(1, "У соснах")
    llm.assert_not_called()
    assert reply.kind == "ask_question"
    assert "рукавичку «У соснах»" in reply.text


async def test_place_only_message_is_not_kept_as_the_base(search):
    llm = FakeLLM(reply_json())
    answers = service(search, llm)
    # Якби перше місце стало базою, друге склеїлося б з ним і пішло б у пошук.
    first = await answers.answer(1, "Під дубом")
    second = await answers.answer(1, "У соснах")
    assert (first.kind, second.kind) == ("ask_question", "ask_question")
    llm.assert_not_called()
    await answers.answer(1, "Коли збори мешканців?")
    await answers.answer(1, "Під дубом")
    assert llm.question() == "Коли збори мешканців? Під дубом"


def test_every_forest_part_has_an_ask_question_name():
    assert set(texts.PART_ABOUT) == set(ForestPart)


async def test_attempts_record_the_answering_model_and_finish_reason(search):
    llm = FakeLLM(
        raw_completion('{"kind": "answer", "text": "Утепл', finish_reason="length"),
        reply_json(sources=[1]),
    )
    reply = await service(search, llm).answer(1, "Які гриби біля сосон?")
    assert [(a.model, a.finish_reason, a.completion_tokens) for a in reply.attempts] == [
        ("main-model", "length", 100),
        ("main-model", "stop", 100),
    ]
