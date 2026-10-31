"""Відповідь звірові: пошук → (за потреби) LLM → текст із джерелами від самого бота."""

import asyncio
import json
import logging
import re
import weakref
from dataclasses import dataclass, field
from typing import Any

from lisovyi.answer import texts
from lisovyi.answer.dialogue import DialogueMemory
from lisovyi.answer.llm import Completion, LLMClient, LLMError, Notify, OutputRejected, Usage
from lisovyi.answer.prompt import (
    KINDS,
    build_conflict_messages,
    build_messages,
    conflict_response_format,
)
from lisovyi.domain import ForestPart
from lisovyi.search.conflicts import PartConflict, detect_part_conflict
from lisovyi.search.places import PlaceResolver, Places
from lisovyi.search.retrieve import ContextDoc, ContextPack, Layer, Mode, display_date
from lisovyi.search.service import SearchService

log = logging.getLogger(__name__)

# Telegram приймає до 4096 символів; запас — на всяк випадок.
MAX_MESSAGE_CHARS = 4000
# Для цих kind відповідь без жодного джерела — не відповідь за архівом.
KINDS_NEEDING_SOURCES = frozenset({"answer", "variants", "partial"})
# Під цими kind бот показує кнопки рукавичок.
KINDS_WITH_GLOVE_BUTTONS = frozenset({"ask_place", "variants"})
# Скільки разів питати модель, якщо відповідь непридатна.
ATTEMPTS = 2


@dataclass(frozen=True)
class Reply:
    text: str
    # Назви рукавичок для кнопок, у порядку карти; порожньо — без кнопок.
    keyboard: list[str]
    # kind моделі, або ask_glove / not_found з пошуку, ask_question (саме місце без питання),
    # conflict (поради частин лісу по пунктах, бриф 05), або unsure / busy / error.
    kind: str
    # Назви файлів джерел, у порядку номерів.
    sources: list[str]
    usage: Usage | None
    # Для прогонів і логів: режим пошуку, файли пакета, сирі відповіді моделі, повтори.
    mode: str = ""
    context: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    retries: int = 0
    # Кожен виклик моделі: хто відповів і чи не обрізано відповідь лімітом токенів.
    attempts: list["Attempt"] = field(default_factory=list)


@dataclass
class _Spent:
    """Виклики моделі, вже витрачені на це питання (шлях по частинах перед звичайним)."""

    usage: Usage | None = None
    raw: list[str] = field(default_factory=list)
    attempts: list["Attempt"] = field(default_factory=list)


@dataclass(frozen=True)
class Attempt:
    model: str
    # stop / length / …; "rejected" — Groq відкинув JSON поза схемою (400).
    finish_reason: str
    completion_tokens: int | None


@dataclass(frozen=True)
class _Parsed:
    kind: str
    text: str
    sources: list[int]


class AnswerService:
    def __init__(self, search: SearchService, llm: LLMClient, *, dialogue_ttl: float):
        self.search = search
        self.llm = llm
        self.dialogue = DialogueMemory(dialogue_ttl)
        # Замок на чат: швидкі повідомлення одного звіра обробляються по черзі.
        self._chat_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    async def answer(self, chat_id: int, message: str, notify: Notify | None = None) -> Reply:
        """`notify` — як надіслати звірові проміжне повідомлення, поки бот чекає на Groq."""
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            resolver = self.search.state.resolver
            question = self.dialogue.merge(chat_id, message, resolver)
            if question is None:
                text = ask_question_text(resolver.resolve(message), resolver)
                return Reply(text, [], "ask_question", [], None)
            pack = self.search.search(question)
            context = [doc.filename for doc in pack.docs]
            if pack.mode == Mode.NOT_FOUND:
                return Reply(texts.NOT_FOUND, [], "not_found", [], None, pack.mode, context)
            if pack.mode == Mode.ASK_GLOVE:
                gloves = self.search.state.resolver.gloves
                return Reply(texts.ASK_GLOVE, gloves, "ask_glove", [], None, pack.mode, context)
            # «Відповім за хвилинку» — не більше одного разу на питання, хоч би скільки
            # викликів моделі воно коштувало.
            notify = _once(notify) if notify is not None else None
            state = self.search.state
            conflict = detect_part_conflict(question, pack, state.index, state.resolver)
            spent = _Spent()
            if conflict is not None:
                reply = await self._ask_by_part(question, pack, conflict, notify, spent)
                if reply is not None:
                    return reply
            return await self._ask_model(question, pack, notify, spent)

    async def _ask_by_part(
        self,
        question: str,
        pack: ContextPack,
        conflict: PartConflict,
        notify: Notify | None,
        spent: "_Spent",
    ) -> Reply | None:
        """Вимога 8: окремий пункт для кожної частини, вступ і завершення — від бота.

        Модель не має де обрати одну пораду. None — відповідь двічі непридатна, і тоді
        питання йде звичайним шляхом; витрачені виклики лишаються в `spent`, щоб звичайна
        відповідь звітувала про всі виклики й токени питання.
        """
        parts = list(conflict.docs)
        messages = build_conflict_messages(question, pack, conflict.glove, parts)
        response_format = conflict_response_format(parts)
        context = [doc.filename for doc in pack.docs]
        usage, raw, attempts = spent.usage, spent.raw, spent.attempts
        for attempt in range(ATTEMPTS):
            try:
                completion = await self.llm.complete(messages, notify, response_format)
            except LLMError as error:
                kind = "busy" if error.reply == texts.BUSY else "error"
                return Reply(
                    error.reply, [], kind, [], usage, pack.mode, context, raw, attempt, attempts
                )
            except OutputRejected as rejected:
                raw.append("")
                attempts.append(Attempt(rejected.model, "rejected", None))
                continue
            raw.append(completion.content)
            attempts.append(
                Attempt(
                    completion.model,
                    completion.finish_reason,
                    completion.usage.completion_tokens if completion.usage else None,
                )
            )
            usage = spent.usage = _add(usage, completion.usage)
            items = parse_by_part(completion, parts, pack)
            log.info(
                "llm by part: model=%s glove=%s valid=%s question=%r",
                completion.model,
                conflict.glove,
                items is not None,
                question,
            )
            if items is not None:
                return self._by_part_reply(conflict, items, pack, usage, raw, attempt, attempts)
        log.warning("unusable answer by part, falling back: question=%r raw=%r", question, raw[-1])
        return None

    def _by_part_reply(
        self,
        conflict: PartConflict,
        items: dict[str, tuple[str, list[int]]],
        pack: ContextPack,
        usage: Usage | None,
        raw: list[str],
        retries: int,
        attempts: list[Attempt],
    ) -> Reply:
        parts = list(conflict.docs)
        intro = texts.CONFLICT_INTRO.format(
            glove=conflict.glove,
            parts=", ".join(f"і до {texts.PART_GENITIVE[part]}" for part in parts),
        )
        lines = [
            f"{texts.PART_LINE[part]}: {drop_part_label(clean_text(items[part.value][0]), part)}"
            for part in parts
        ]
        if "general" in items:
            lines.append(f"{texts.CONFLICT_GENERAL}: {clean_text(items['general'][0])}")
        numbers = sorted({n for _, sources in items.values() for n in sources})
        docs = [pack.docs[n - 1] for n in numbers]
        block = sources_block(docs)
        body = "\n\n".join([intro, "\n".join(lines), texts.CONFLICT_OUTRO])
        return Reply(
            text=truncate(body, MAX_MESSAGE_CHARS - len(block)) + block,
            keyboard=[],
            kind="conflict",
            sources=[doc.filename for doc in docs],
            usage=usage,
            mode=pack.mode,
            context=[doc.filename for doc in pack.docs],
            raw=raw,
            retries=retries,
            attempts=attempts,
        )

    async def _ask_model(
        self, question: str, pack: ContextPack, notify: Notify | None, spent: "_Spent"
    ) -> Reply:
        messages = build_messages(question, pack)
        context = [doc.filename for doc in pack.docs]
        usage, raw, attempts = spent.usage, spent.raw, spent.attempts
        for attempt in range(ATTEMPTS):
            try:
                completion = await self.llm.complete(messages, notify)
            except LLMError as error:
                kind = "busy" if error.reply == texts.BUSY else "error"
                return Reply(
                    error.reply, [], kind, [], usage, pack.mode, context, raw, attempt, attempts
                )
            except OutputRejected as rejected:
                raw.append("")
                attempts.append(Attempt(rejected.model, "rejected", None))
                continue
            raw.append(completion.content)
            attempts.append(
                Attempt(
                    completion.model,
                    completion.finish_reason,
                    completion.usage.completion_tokens if completion.usage else None,
                )
            )
            usage = _add(usage, completion.usage)
            parsed = parse_completion(completion, len(pack.docs))
            _log_call(question, completion, parsed)
            if parsed is not None:
                return self._reply(parsed, pack, usage, raw, attempt, attempts)
        log.warning("no usable answer: question=%r raw=%r", question, raw[-1])
        return Reply(
            texts.UNSURE, [], "unsure", [], usage, pack.mode, context, raw, ATTEMPTS - 1, attempts
        )

    def _reply(
        self,
        parsed: _Parsed,
        pack: ContextPack,
        usage: Usage | None,
        raw: list[str],
        retries: int,
        attempts: list[Attempt],
    ) -> Reply:
        docs = [pack.docs[number - 1] for number in parsed.sources]
        if parsed.kind == "not_found":
            docs = []
        block = sources_block(docs)
        text = clean_text(parsed.text)
        text = truncate(text, MAX_MESSAGE_CHARS - len(block))
        keyboard = (
            self.search.state.resolver.gloves if parsed.kind in KINDS_WITH_GLOVE_BUTTONS else []
        )
        return Reply(
            text=text + block,
            keyboard=keyboard,
            kind=parsed.kind,
            sources=[doc.filename for doc in docs],
            usage=usage,
            mode=pack.mode,
            context=[doc.filename for doc in pack.docs],
            raw=raw,
            retries=retries,
            attempts=attempts,
        )


def ask_question_text(places: Places, resolver: PlaceResolver) -> str:
    """Що відповісти на саме місце без питання: без LLM, тексти з брифу 03b.

    Рукавичка разом із власною частиною лісу («Під вербою біля річки») — це одна рукавичка:
    пошук теж зводить таке місце до неї.
    """
    if len(places.gloves) == 1 and set(places.parts) <= set(resolver.parts_of(places.gloves[0])):
        return texts.ASK_QUESTION_GLOVE.format(glove=places.gloves[0])
    if len(places.parts) == 1 and not places.gloves:
        return texts.ASK_QUESTION_PART.format(part=texts.PART_ABOUT[places.parts[0]])
    return texts.ASK_QUESTION_PLACES


def parse_completion(completion: Completion, pack_size: int) -> _Parsed | None:
    """Розбір JSON моделі; None — відповідь непридатна і варта повторної спроби.

    Номери джерел поза пакетом відкидаються; решта — без повторів, за зростанням.
    """
    if completion.finish_reason == "length":
        return None
    try:
        data: Any = json.loads(completion.content)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind, text, sources = data.get("kind"), data.get("text"), data.get("sources")
    if kind not in KINDS or not isinstance(text, str) or not isinstance(sources, list):
        return None
    numbers = sorted(
        {
            n
            for n in sources
            if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= pack_size
        }
    )
    if not clean_text(text) or (kind in KINDS_NEEDING_SOURCES and not numbers):
        return None
    return _Parsed(kind, text, numbers)


# Мітку частини ставить бот, а модель часто повторює її сама на початку пункту:
# «Листяний ліс: У листяному лісі утеплюйте…». Лише на початку і лише для своєї частини.
_PART_LABEL = {
    ForestPart.KHVOINYI: r"[ув]\s+хвойному\s+лісі",
    ForestPart.LYSTIANYI: r"[ув]\s+листяному\s+лісі",
    ForestPart.RICHKA: r"для\s+тих,\s+хто\s+біля\s+річки|біля\s+річки|коло\s+річки",
}
_PART_LABEL_AT_START = {
    part: re.compile(rf"^(?:{pattern})(?=[\s,:]|$)[\s,:]*", re.IGNORECASE)
    for part, pattern in _PART_LABEL.items()
}


def drop_part_label(text: str, part: ForestPart) -> str:
    """«У листяному лісі гриби…» → «Гриби…» для пункту листяного лісу.

    Порожній залишок — повертається оригінал; посеред тексту нічого не прибирається.
    """
    rest = _PART_LABEL_AT_START[part].sub("", text, count=1)
    if rest == text or not rest.strip():
        return text
    return rest[0].upper() + rest[1:]


def parse_by_part(
    completion: Completion, parts: list[ForestPart], pack: ContextPack
) -> dict[str, tuple[str, list[int]]] | None:
    """Пункти відповіді по частинах: частина (або "general") → (текст, номери джерел).

    None — непридатно: немає пункту якоїсь частини, текст порожній після чистки або пункт
    частини не посилається на жоден документ цієї частини з пакета (інакше модель могла б
    злити обидві поради в одну, а бот усе одно сказав би, що вони різняться). Номери поза
    пакетом відкидаються.
    """
    pack_size = len(pack.docs)
    if completion.finish_reason == "length":
        return None
    try:
        data: Any = json.loads(completion.content)
    except ValueError:
        return None
    entries = data.get("parts") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return None
    allowed = {part.value for part in parts} | {"general"}
    items: dict[str, tuple[str, list[int]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        part, text, sources = entry.get("part"), entry.get("text"), entry.get("sources")
        if part not in allowed or not isinstance(text, str) or not isinstance(sources, list):
            return None
        if part in items or not clean_text(text):
            continue
        numbers = sorted(
            {
                n
                for n in sources
                if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= pack_size
            }
        )
        items[part] = (text, numbers)
    for part in parts:
        if part.value not in items:
            return None
        cited = [pack.docs[n - 1] for n in items[part.value][1]]
        if not any(doc.layer == Layer.PART and doc.scope == part.value for doc in cited):
            return None
    return items


def sources_block(docs: list[ContextDoc]) -> str:
    """«Джерела:» з назвами документів; для документа звіра — ім'я і дата за Києвом."""
    if not docs:
        return ""
    lines = [f"— {_source_line(doc)}" for doc in docs]
    return "\n\n" + texts.SOURCES_HEADER + "\n" + "\n".join(lines)


def _source_line(doc: ContextDoc) -> str:
    if doc.author_id is None:
        return doc.title
    name = doc.author_name or "невідомий звір"
    date = display_date(doc.created_at) if doc.created_at else "дата невідома"
    # «автор:», а не «надіслав(ла)»: бот не знає роду звіра; ім'я не відмінюється.
    return f"{doc.title} (автор: {name}, {date})"


_MARKUP_EMPHASIS = re.compile(r"\*\*|__")
_MARKUP_HEADING = re.compile(r"^[ \t]*#+[ \t]*", re.MULTILINE)


# «у вашій рукавичці»: бот не знає, де живе звір, що питає, а промпт цього модель не відучив.
# «рукавич», а не «рукавичк»: у місцевому відмінку «рукавичці» к чергується з ц.
_PRESUMED_GLOVE = re.compile(r"\b(в)аш(?:а|ої|ій|у|ою|і|их|ими)\s+(?=рукавич)", re.IGNORECASE)
# «(джерело 1)», «(джерела 4, 5)»: джерела показує бот, а не модель.
_SOURCE_MENTION = re.compile(r"\s*\((?:джерело|джерела)\s*[\d,\sі]+\)", re.IGNORECASE)


def clean_text(text: str) -> str:
    """Чистка тексту моделі перед відправкою (повідомлення йде без parse_mode).

    Без `**`, `__` і `#` на початку рядків, без присвійного «ваш…» перед «рукавич…»
    і без «(джерело N)».
    """
    text = _MARKUP_HEADING.sub("", _MARKUP_EMPHASIS.sub("", text))
    text = _SOURCE_MENTION.sub("", text)
    return _drop_presumed_glove(text).strip()


def _drop_presumed_glove(text: str) -> str:
    """«У вашій рукавичці» → «У рукавичці»; «Ваша рукавичка» → «Рукавичка»."""
    out, position = [], 0
    for match in _PRESUMED_GLOVE.finditer(text):
        out.append(text[position : match.start()])
        position = match.end()
        if _starts_sentence(text, match.start()) and position < len(text):
            # «Ваш…» починало речення: велика літера переходить на «рукавич…». Ввічливе
            # «Вашій» посеред речення великої літери не передає.
            out.append(text[position].upper())
            position += 1
    out.append(text[position:])
    return "".join(out)


_BEFORE_SENTENCE = re.compile(r"(?:^|[.!?…]\s+|\n\s*)$")


def _starts_sentence(text: str, position: int) -> bool:
    return _BEFORE_SENTENCE.search(text[:position]) is not None


_SENTENCE_END = re.compile(r"[.!?…](?=\s)")
ELLIPSIS = "…"


def truncate(text: str, limit: int) -> str:
    """Не довше `limit`: обрізання по останній межі речення і «…» в кінці."""
    if len(text) <= limit:
        return text
    head = text[: limit - len(ELLIPSIS)]
    ends = [match.end() for match in _SENTENCE_END.finditer(head + " ")]
    cut = ends[-1] if ends else (head.rfind(" ") if " " in head else len(head))
    return head[:cut].rstrip() + ELLIPSIS


def _once(notify: Notify) -> Notify:
    """Проміжне повідомлення — не більше одного на питання, навіть якщо повторна спроба
    знову впреться в 429."""
    sent = False

    async def notify_once(text: str) -> None:
        nonlocal sent
        if not sent:
            sent = True
            await notify(text)

    return notify_once


def _add(total: Usage | None, usage: Usage | None) -> Usage | None:
    if total is None:
        return usage
    return total if usage is None else total + usage


def _log_call(question: str, completion: Completion, parsed: _Parsed | None) -> None:
    usage = completion.usage
    log.info(
        "llm: model=%s prompt_tokens=%s cached_tokens=%s completion_tokens=%s "
        "kind=%s sources=%s seconds=%.1f question=%r",
        completion.model,
        usage.prompt_tokens if usage else None,
        usage.cached_tokens if usage else None,
        usage.completion_tokens if usage else None,
        parsed.kind if parsed else "invalid",
        parsed.sources if parsed else [],
        completion.seconds,
        question,
    )
