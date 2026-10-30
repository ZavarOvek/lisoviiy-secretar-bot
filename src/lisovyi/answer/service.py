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
from lisovyi.answer.prompt import KINDS, build_messages
from lisovyi.search.retrieve import ContextDoc, ContextPack, Mode, display_date
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
    # kind моделі, або ask_glove / not_found з пошуку, або unsure / busy / error.
    kind: str
    # Назви файлів джерел, у порядку номерів.
    sources: list[str]
    usage: Usage | None
    # Для прогонів і логів: режим пошуку, файли пакета, сирі відповіді моделі, повтори.
    mode: str = ""
    context: list[str] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    retries: int = 0


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
            question = self.dialogue.merge(chat_id, message, self.search.state.resolver)
            pack = self.search.search(question)
            context = [doc.filename for doc in pack.docs]
            if pack.mode == Mode.NOT_FOUND:
                return Reply(texts.NOT_FOUND, [], "not_found", [], None, pack.mode, context)
            if pack.mode == Mode.ASK_GLOVE:
                gloves = self.search.state.resolver.gloves
                return Reply(texts.ASK_GLOVE, gloves, "ask_glove", [], None, pack.mode, context)
            return await self._ask_model(question, pack, notify)

    async def _ask_model(self, question: str, pack: ContextPack, notify: Notify | None) -> Reply:
        messages = build_messages(question, pack)
        context = [doc.filename for doc in pack.docs]
        usage: Usage | None = None
        raw: list[str] = []
        for attempt in range(ATTEMPTS):
            try:
                completion = await self.llm.complete(messages, notify)
            except LLMError as error:
                kind = "busy" if error.reply == texts.BUSY else "error"
                return Reply(error.reply, [], kind, [], usage, pack.mode, context, raw, attempt)
            except OutputRejected:
                raw.append("")
                continue
            raw.append(completion.content)
            usage = _add(usage, completion.usage)
            parsed = parse_completion(completion, len(pack.docs))
            _log_call(question, completion, parsed)
            if parsed is not None:
                return self._reply(parsed, pack, usage, raw, attempt)
        log.warning("no usable answer: question=%r raw=%r", question, raw[-1])
        return Reply(texts.UNSURE, [], "unsure", [], usage, pack.mode, context, raw, ATTEMPTS - 1)

    def _reply(
        self, parsed: _Parsed, pack: ContextPack, usage: Usage | None, raw: list[str], retries: int
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
        )


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


def clean_text(text: str) -> str:
    """Без `**`, `__` і `#` на початку рядків: повідомлення йде без parse_mode."""
    return _MARKUP_HEADING.sub("", _MARKUP_EMPHASIS.sub("", text)).strip()


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
