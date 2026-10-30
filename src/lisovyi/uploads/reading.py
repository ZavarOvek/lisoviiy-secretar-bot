"""Текст поради, яку надіслав звір: те саме читання, що й для архіву, плюс межі для звірів."""

import asyncio
import logging
from collections.abc import Callable

from lisovyi.ingest.parsers import Extracted, NoTextFound, UnsupportedDocument, extract_text

log = logging.getLogger(__name__)

# Найдовший архівний документ — ~800 символів; порада довша за 4000 з'їла б увесь пакет
# контексту, бо перший документ пакета бюджетом не обмежений.
MAX_UPLOAD_CHARS = 4000
# OCR багатосторінкового скану повільний, але звір не має чекати вічно.
READ_TIMEOUT = 120.0


class UploadRejected(Exception):
    """Файл звіра не приймається; `reason` — що з ним не так."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Причини відмови. Тексти для звірів — у bot/texts.py.
TIMEOUT = "timeout"
UNREADABLE = "unreadable"
EMPTY = "empty"
TOO_LONG = "too_long"


async def read_upload(
    data: bytes,
    filename: str,
    *,
    timeout: float = READ_TIMEOUT,
    extract: Callable[..., Extracted] = extract_text,
) -> Extracted:
    """Текст файлу в окремому потоці: OCR скану не блокує бота.

    Транскриптів для файлів звірів немає, тож скан іде просто в OCR.
    Після таймауту потік дочитує файл у фоні: зупинити його Python не вміє, але результат
    уже нікому не потрібен.
    """
    try:
        extracted = await asyncio.wait_for(
            asyncio.to_thread(extract, data, filename, None), timeout
        )
    except TimeoutError:
        log.warning("upload %r: reading took longer than %.0fs", filename, timeout)
        raise UploadRejected(TIMEOUT) from None
    except NoTextFound:
        raise UploadRejected(EMPTY) from None
    except UnsupportedDocument as exc:
        log.info("upload %r is unreadable: %s", filename, exc)
        raise UploadRejected(UNREADABLE) from None
    if not extracted.text.strip():
        raise UploadRejected(EMPTY)
    if len(extracted.text.strip()) > MAX_UPLOAD_CHARS:
        raise UploadRejected(TOO_LONG)
    return extracted
