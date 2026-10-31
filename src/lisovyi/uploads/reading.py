"""Текст поради, яку надіслав звір: те саме читання, що й для архіву, плюс межі для звірів."""

import asyncio
import io
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePath

import pypdf

from lisovyi.ingest.parsers import Extracted, NoTextFound, UnsupportedDocument, extract_text

log = logging.getLogger(__name__)

# Найдовший архівний документ — ~800 символів; порада довша за 4000 з'їла б увесь пакет
# контексту, бо перший документ пакета бюджетом не обмежений.
MAX_UPLOAD_CHARS = 4000
# 4000 символів — це кілька сторінок; сотні сторінок скану OCR читав би хвилинами.
MAX_UPLOAD_PAGES = 10
# OCR багатосторінкового скану повільний, але звір не має чекати вічно.
READ_TIMEOUT = 120.0
# Власний пул: після таймауту потік дочитує файл у фоні (зупинити його Python не вміє),
# і такі потоки не мають забивати спільний пул, у якому SearchService будує індекс.
_READERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="upload-read")


class UploadRejected(Exception):
    """Файл звіра не приймається; `reason` — що з ним не так."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TooManyPages(UnsupportedDocument):
    pass


# Причини відмови. Тексти для звірів — у bot/texts.py.
TIMEOUT = "timeout"
UNREADABLE = "unreadable"
EMPTY = "empty"
TOO_LONG = "too_long"


def extract_upload_text(data: bytes, filename: str, transcripts: None) -> Extracted:
    """`extract_text` з межею сторінок PDF, перевіреною до OCR."""
    if PurePath(filename).suffix.lower() == ".pdf":
        try:
            pages = len(pypdf.PdfReader(io.BytesIO(data)).pages)
        except Exception as exc:
            raise UnsupportedDocument(f"{filename}: not a readable PDF ({exc!r})") from exc
        if pages > MAX_UPLOAD_PAGES:
            raise TooManyPages(f"{filename}: {pages} pages, at most {MAX_UPLOAD_PAGES}")
    return extract_text(data, filename, transcripts)


async def read_upload(
    data: bytes,
    filename: str,
    *,
    timeout: float = READ_TIMEOUT,
    extract: Callable[..., Extracted] = extract_upload_text,
) -> Extracted:
    """Текст файлу в окремому потоці: OCR скану не блокує бота.

    Транскриптів для файлів звірів немає, тож скан іде просто в OCR.
    """
    loop = asyncio.get_running_loop()
    try:
        extracted = await asyncio.wait_for(
            loop.run_in_executor(_READERS, extract, data, filename, None), timeout
        )
    except TimeoutError:
        log.warning("upload %r: reading took longer than %.0fs", filename, timeout)
        raise UploadRejected(TIMEOUT) from None
    except NoTextFound:
        raise UploadRejected(EMPTY) from None
    except TooManyPages as exc:
        log.info("upload %r: %s", filename, exc)
        raise UploadRejected(TOO_LONG) from None
    except UnsupportedDocument as exc:
        log.info("upload %r is unreadable: %s", filename, exc)
        raise UploadRejected(UNREADABLE) from None
    if not extracted.text.strip():
        raise UploadRejected(EMPTY)
    if len(extracted.text.strip()) > MAX_UPLOAD_CHARS:
        raise UploadRejected(TOO_LONG)
    return extracted
