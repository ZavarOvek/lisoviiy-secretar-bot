"""Класифікація документа за областю дії (kind + scope) і темою."""

import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass

from lisovyi.domain import GENERAL_SCOPE, DocumentKind, ForestPart

MAP_TITLE = "Карта лісу"
MAP_TOPIC = "karta"

GLOVE_DOCUMENT_TOPICS = {
    "Статут ОСМД": "statut",
    "Правила співжиття в рукавичці": "pravyla",
    "Чергування в рукавичці": "cherhuvannia",
    "Членські внески рукавички": "vnesky",
    "Небезпеки поруч із рукавичкою": "nebezpeky",
    "Стежки до сусідів від рукавички": "stezhky",
    "Чим поживитись поруч із рукавичкою": "yizha",
}

# Маркери частин лісу для правил 3 і 4. Ширші за маркери легенди карти,
# бо поради пишуть про річку різними словами.
SCOPE_PART_MARKERS = {
    ForestPart.KHVOINYI: re.compile(r"хвойн", re.IGNORECASE),
    ForestPart.LYSTIANYI: re.compile(r"листян", re.IGNORECASE),
    ForestPart.RICHKA: re.compile(r"\b(?:біля|при)\s+річ|прибереж", re.IGNORECASE),
}
GENERAL_MARKER = re.compile(r"всіх\s+мешканців\s+лісу", re.IGNORECASE)
# Слова, якими документ називає, для кого він.
ADDRESSEE_MARKER = re.compile(r"\bдля\b|\bстосу\w*|\bзвернення\b", re.IGNORECASE)
SENTENCES_TO_CHECK = 3

# Порядок має значення: перше збіжне ключове слово визначає тему.
# «їстівне» стоїть перед «їсти», щоб «Їстівне й отруйне» не стало «yizha».
TOPIC_KEYWORDS = [
    (re.compile(r"гриб"), "hryby"),
    (re.compile(r"небезпек"), "nebezpeky"),
    (re.compile(r"їстівне"), "yistivne_otruine"),
    (re.compile(r"їсти|поживит"), "yizha"),
    (re.compile(r"утепл"), "uteplennia"),
    (re.compile(r"брод"), "brody"),
    (re.compile(r"жолуд|горіх"), "zholudi_horikhy"),
    (re.compile(r"заблукат"), "oriientuvannia"),
    (re.compile(r"календар"), "kalendar"),
    (re.compile(r"осінній путівник"), "osinnii_putivnyk"),
    (re.compile(r"толок"), "toloka"),
    (re.compile(r"пожеж"), "pozhezha"),
    (re.compile(r"мисливц"), "myslyvtsi"),
]


@dataclass(frozen=True)
class Classification:
    kind: DocumentKind
    scope: str
    topic: str


def classify(title: str, text: str, gloves: Collection[str]) -> Classification | None:
    """Правила з пріоритетом: карта → документ рукавички → текст → назва.

    `gloves` — назви рукавичок з легенди активної карти. None означає, що область дії
    визначити не вдалося.
    """
    # Назва файлу може прийти в NFD (наприклад, з macOS): «й» і «ї» тоді — дві кодові точки,
    # і порівняння з назвами рукавичок та префіксами мовчки не спрацювало б.
    title = unicodedata.normalize("NFC", title)
    text = unicodedata.normalize("NFC", text)
    gloves = {unicodedata.normalize("NFC", glove) for glove in gloves}
    if title == MAP_TITLE:
        return Classification(DocumentKind.MAP, GENERAL_SCOPE, MAP_TOPIC)

    for prefix, topic in GLOVE_DOCUMENT_TOPICS.items():
        rest = title.removeprefix(prefix + " ")
        if rest != title and rest in gloves:
            return Classification(DocumentKind.GLOVE, rest, topic)

    scopes = _scopes_from_text(text)
    if scopes is None:
        scopes = _scopes_from_title(title)
    if len(scopes) != 1:
        # Нічого не знайшли або знайшли кілька областей: вгадувати не беремося,
        # і назва не перебиває неоднозначний текст.
        return None
    scope = scopes.pop()
    kind = DocumentKind.GENERAL if scope == GENERAL_SCOPE else DocumentKind.PART
    return Classification(kind, scope, topic_for(title))


def _scopes_from_text(text: str) -> set[str] | None:
    """Області з першого речення (серед перших трьох після заголовка), яке каже,
    для кого документ, і називає область. None: такого речення немає."""
    _title, _, body = text.strip().partition("\n")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", body.strip()) if s]
    for sentence in sentences[:SENTENCES_TO_CHECK]:
        if ADDRESSEE_MARKER.search(sentence) and (scopes := _scopes_named_in(sentence)):
            return scopes
    return None


def _scopes_named_in(sentence: str) -> set[str]:
    scopes = {part.value for part, marker in SCOPE_PART_MARKERS.items() if marker.search(sentence)}
    if GENERAL_MARKER.search(sentence):
        scopes.add(GENERAL_SCOPE)
    return scopes


def _scopes_from_title(title: str) -> set[str]:
    return {part.value for part, marker in SCOPE_PART_MARKERS.items() if marker.search(title)}


# Звертання, після якого рукавичка в лапках — адресат поради (правило 3 для завантажень).
QUOTED_GLOVE_MARKER = re.compile(
    r"\bдля\b|\bстосу\w*|\bзвернення\b|\bмешкан\w*|\bувага\b|\bшановні\b|\bлюбі\b"
    r"|\bдорогі\b|\bдо\s+відома\b|\bу\s+рукавичці\b",
    re.IGNORECASE,
)
# Тема, якщо з назви файлу не вийшло жодної латинської літери чи цифри.
FALLBACK_TOPIC = "porada"


@dataclass(frozen=True)
class UploadClassification:
    kind: DocumentKind
    scope: str
    topic: str
    # Тема з таблиць (тип документа рукавички, ключове слово назви), а не з транслітерації:
    # лише така тема може точно збігтися з темою наявного документа.
    topic_from_rules: bool


def classify_upload(title: str, text: str, gloves: Collection[str]) -> UploadClassification | None:
    """Місце поради, яку надіслав звір. None — правила не впоралися, питаємо звіра.

    Правила по черзі, перше спрацювання перемагає: «Карта лісу» → назва архівного вигляду →
    рукавичка в лапках у вступі → частина лісу чи весь ліс у вступі → маркери частини в назві.
    Архівний імпорт цим не користується: він і далі йде через `classify`.
    """
    title = unicodedata.normalize("NFC", title)
    text = unicodedata.normalize("NFC", text)
    gloves = [unicodedata.normalize("NFC", glove) for glove in gloves]
    if title == MAP_TITLE:
        return UploadClassification(DocumentKind.MAP, GENERAL_SCOPE, MAP_TOPIC, True)
    for prefix, topic in GLOVE_DOCUMENT_TOPICS.items():
        rest = title.removeprefix(prefix + " ")
        if rest != title and rest in gloves:
            return UploadClassification(DocumentKind.GLOVE, rest, topic, True)

    topic, from_rules = upload_topic(title)
    glove = glove_from_intro(text, gloves)
    if glove is not None:
        return UploadClassification(DocumentKind.GLOVE, glove, topic, from_rules)
    scopes = _scopes_from_intro(text)
    if scopes is None:
        scopes = _scopes_from_title(title)
    if len(scopes) != 1:
        return None
    scope = scopes.pop()
    kind = DocumentKind.GENERAL if scope == GENERAL_SCOPE else DocumentKind.PART
    return UploadClassification(kind, scope, topic, from_rules)


def glove_from_intro(text: str, gloves: Collection[str]) -> str | None:
    """Рукавичка в лапках у першому реченні вступу, яке звертається до когось.

    Речення, де звертання є, а рукавичок у лапках дві чи більше, правило не застосовує:
    вгадувати, котра з них адресат, не беремося.
    """
    for sentence in _intro_sentences(text):
        if not QUOTED_GLOVE_MARKER.search(sentence):
            continue
        named = {glove for glove in gloves if f"«{glove}»" in sentence}
        if named:
            return named.pop() if len(named) == 1 else None
    return None


def _scopes_from_intro(text: str) -> set[str] | None:
    """Правило 4 для завантажень: як `_scopes_from_text` архіву, але зі вступом правила 3."""
    for sentence in _intro_sentences(text):
        if ADDRESSEE_MARKER.search(sentence) and (scopes := _scopes_named_in(sentence)):
            return scopes
    return None


def _intro_sentences(text: str) -> list[str]:
    """Перші речення поради — для правил 3 і 4 завантажень.

    Перший рядок — заголовок лише тоді, коли після нього є ще текст і він не закінчується
    як речення: архівні заголовки крапки не мають, а звір часто пише без заголовка
    («Мешканцям рукавички «Під вербою».» першим рядком). Пробіли й переноси згорнуті: PDF
    буває розриває назву в лапках («Під\n ялиною»).
    """
    stripped = text.strip()
    head, newline, body = stripped.partition("\n")
    heading = bool(newline) and not head.rstrip().endswith((".", "!", "?", "…"))
    flat = " ".join((body if heading else stripped).split())
    return [s for s in re.split(r"(?<=[.!?…])\s+", flat) if s][:SENTENCES_TO_CHECK]


def upload_topic(title: str) -> tuple[str, bool]:
    """Тема поради з назви файлу і чи вона з таблиці ключових слів."""
    topic = _keyword_topic(title)
    if topic is not None:
        return topic, True
    return slugify(title) or FALLBACK_TOPIC, False


def topic_for(title: str) -> str:
    return _keyword_topic(title) or slugify(title)


def _keyword_topic(title: str) -> str | None:
    lowered = title.lower()
    for pattern, topic in TOPIC_KEYWORDS:
        if pattern.search(lowered):
            return topic
    return None


# Транслітерація за постановою КМУ № 55 (2010); на початку слова є/ї/й/ю/я передаються інакше.
# fmt: off
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu",
    "я": "ia", "'": "", "’": "", "ʼ": "",
}
# fmt: on
_TRANSLIT_WORD_START = {"є": "ye", "ї": "yi", "й": "y", "ю": "yu", "я": "ya"}
_APOSTROPHES = "'’ʼ"


def slugify(title: str) -> str:
    out = []
    previous = " "
    for char in title.lower():
        # Після апострофа літера не на початку слова: «пам'ятка» → «pamiatka».
        if char in _TRANSLIT_WORD_START and not (previous.isalpha() or previous in _APOSTROPHES):
            out.append(_TRANSLIT_WORD_START[char])
        else:
            out.append(_TRANSLIT.get(char, char))
        previous = char
    return re.sub(r"[^a-z0-9]+", "_", "".join(out)).strip("_")
