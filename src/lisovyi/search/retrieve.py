"""Пакет контексту для LLM: документи, розкладені за шарами рукавичка → частина → увесь ліс."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from lisovyi.domain import GENERAL_SCOPE, PART_NAMES, DocumentKind, ForestPart
from lisovyi.search.index import Bm25Index, Scored
from lisovyi.search.morph import bag, tokens
from lisovyi.search.places import PlaceResolver
from lisovyi.search.synonyms import expand

DEFAULT_CHAR_BUDGET = 6000
PER_GLOVE = 3
PER_PART = 3
GENERAL_TOP = 3
UNPLACED_TOP = 6
# Режим ask_glove: стільки документів однієї теми з різних рукавичок серед перших ASK_WINDOW.
ASK_WINDOW = 4
ASK_MIN_GLOVES = 3
# Дати для звірів — за київським часом, а не за UTC, у якому їх повертає БД.
DISPLAY_TZ = ZoneInfo("Europe/Kyiv")


class Mode(StrEnum):
    PLACED = "placed"
    UNPLACED = "unplaced"
    ASK_GLOVE = "ask_glove"
    NOT_FOUND = "not_found"


class Layer(StrEnum):
    GLOVE = "glove"
    PART = "part"
    GENERAL = "general"


_LAYER_OF_KIND = {
    DocumentKind.GLOVE: Layer.GLOVE,
    DocumentKind.PART: Layer.PART,
    DocumentKind.GENERAL: Layer.GENERAL,
}


@dataclass(frozen=True)
class ContextDoc:
    document_id: int
    filename: str
    title: str
    kind: DocumentKind
    scope: str
    layer: Layer
    score: float
    text: str
    author_id: int | None
    created_at: datetime | None
    # Ім'я звіра з beasts для заголовка блоку; None для архівних документів.
    author_name: str | None = None


@dataclass(frozen=True)
class ContextPack:
    mode: Mode
    gloves: list[str]
    parts: list[ForestPart]
    allowed_parts: list[ForestPart]
    docs: list[ContextDoc]
    # Частини кожної названої рукавички, для рядка «Місце: …».
    glove_parts: dict[str, tuple[ForestPart, ...]]

    @property
    def chars(self) -> int:
        return sum(len(doc.text) for doc in self.docs)

    def render(self) -> str:
        """Текст для LLM: рядок про місце (якщо назване) і блок на кожен документ."""
        blocks = []
        place = self._place_line()
        if place:
            blocks.append(place)
        for doc in self.docs:
            header = f"[Джерело: {_header_field(doc.filename)} | {_layer_label(doc)}"
            if doc.author_id is not None:
                date = _display_date(doc.created_at) if doc.created_at else "дата невідома"
                author = _header_field(doc.author_name or "невідомий звір")
                header += f" | надіслав: {author}, {date}"
            blocks.append(f"{header}]\n{doc.text.strip()}")
        return "\n\n".join(blocks)

    def _place_line(self) -> str:
        named = []
        for glove in self.gloves:
            parts = self.glove_parts[glove]
            where = ", ".join(PART_NAMES[p] for p in parts) or "не належить до жодної частини лісу"
            named.append(f"{glove} ({where})")
        covered = {part for glove in self.gloves for part in self.glove_parts[glove]}
        named.extend(PART_NAMES[p] for p in self.parts if p not in covered)
        return f"Місце: {'; '.join(named)}" if named else ""


def _display_date(moment: datetime) -> str:
    # Наївний час не можна перевести в Київ без здогадки про його пояс; з БД
    # (timestamptz) він завжди приходить з поясом.
    if moment.tzinfo is None:
        raise ValueError(f"created_at has no time zone: {moment!r}")
    return moment.astimezone(DISPLAY_TZ).strftime("%d.%m.%Y")


_HEADER_UNSAFE = str.maketrans({"[": "(", "]": ")", "|": "/"})


def _header_field(value: str) -> str:
    """Поле заголовка джерела в один рядок і без символів його розмітки.

    Ім'я звіра й назву файлу задають звірі: без цього вони могли б закрити заголовок
    і дописати від себе підроблене «[Джерело: …]».
    """
    return " ".join(value.split()).translate(_HEADER_UNSAFE)


def _layer_label(doc: ContextDoc) -> str:
    if doc.layer == Layer.GLOVE:
        return f"рукавичка «{doc.scope}»"
    if doc.layer == Layer.PART:
        return f"частина лісу: {PART_NAMES[ForestPart(doc.scope)]}"
    return "увесь ліс"


def retrieve(
    question: str,
    index: Bm25Index,
    resolver: PlaceResolver,
    *,
    char_budget: int = DEFAULT_CHAR_BUDGET,
) -> ContextPack:
    places = resolver.resolve(question)
    allowed = resolver.allowed_parts(places)
    scored = [s for s in index.score_all(expand(bag(question))) if s.score > 0]
    # Нічиї розв'язуються за id, щоб пакет був детермінованим.
    scored.sort(key=lambda s: (-s.score, s.document.document_id))

    if places.gloves or places.parts:
        mode = Mode.PLACED
        chosen = _placed(scored, places.gloves, allowed)
    else:
        chosen = [(s, _LAYER_OF_KIND[s.document.kind]) for s in scored[:UNPLACED_TOP]]
        if not chosen:
            mode = Mode.NOT_FOUND
        elif _asks_for_glove([s for s, _ in chosen[:ASK_WINDOW]]):
            mode = Mode.ASK_GLOVE
        else:
            mode = Mode.UNPLACED

    return ContextPack(
        mode=mode,
        gloves=places.gloves,
        parts=places.parts,
        allowed_parts=allowed,
        docs=_fit_budget(chosen, char_budget),
        glove_parts={glove: resolver.parts_of(glove) for glove in places.gloves},
    )


def _placed(
    scored: list[Scored], gloves: list[str], allowed: list[ForestPart]
) -> list[tuple[Scored, Layer]]:
    """Тільки документи названих рукавичок, дозволених частин і всього лісу."""
    by_scope: dict[str, list[Scored]] = defaultdict(list)
    general: list[Scored] = []
    for s in scored:
        if s.document.kind == DocumentKind.GENERAL and s.document.scope == GENERAL_SCOPE:
            general.append(s)
        else:
            by_scope[s.document.scope].append(s)
    chosen = [(s, Layer.GLOVE) for glove in gloves for s in by_scope[glove][:PER_GLOVE]]
    chosen += [(s, Layer.PART) for part in allowed for s in by_scope[part.value][:PER_PART]]
    chosen += [(s, Layer.GENERAL) for s in general[:GENERAL_TOP]]
    return chosen


def _asks_for_glove(top: list[Scored]) -> bool:
    """Чи є серед `top` документи однієї теми з ASK_MIN_GLOVES різних рукавичок."""
    gloves_by_topic: dict[str, set[str]] = defaultdict(set)
    for s in top:
        if s.document.kind == DocumentKind.GLOVE:
            gloves_by_topic[s.document.topic].add(s.document.scope)
    return any(len(gloves) >= ASK_MIN_GLOVES for gloves in gloves_by_topic.values())


def _fit_budget(chosen: Iterable[tuple[Scored, Layer]], char_budget: int) -> list[ContextDoc]:
    """Шари glove → part → general, у шарі за спаданням рахунку. Документ, що не вміщується,
    пропускається; перший документ пакета лишається завжди."""
    layer_order = list(Layer)
    ordered = sorted(
        chosen,
        key=lambda pair: (layer_order.index(pair[1]), -pair[0].score, pair[0].document.document_id),
    )
    docs: list[ContextDoc] = []
    used = 0
    for s, layer in ordered:
        size = len(s.document.text)
        if docs and used + size > char_budget:
            continue
        docs.append(_context_doc(s, layer))
        used += size
    return docs


def _context_doc(s: Scored, layer: Layer) -> ContextDoc:
    d = s.document
    return ContextDoc(
        document_id=d.document_id,
        filename=d.filename,
        title=d.title,
        kind=d.kind,
        scope=d.scope,
        layer=layer,
        score=s.score,
        text=d.text,
        author_id=d.author_id,
        created_at=d.created_at,
        author_name=d.author_name,
    )


def merge_followup(previous: str | None, message: str, resolver: PlaceResolver) -> str:
    """Склеює уточнення місця з попереднім питанням.

    Якщо всі змістовні слова нового повідомлення — назви місць («У соснах»), це уточнення
    попереднього питання. Інакше нове повідомлення — окреме питання.
    """
    if not previous:
        return message
    content = [token for token in tokens(message) if token]
    if content and all(resolver.is_place_token(token) for token in content):
        return f"{previous} {message}"
    return message
