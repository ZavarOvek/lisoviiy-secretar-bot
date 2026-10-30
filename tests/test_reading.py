import time

import pytest

from lisovyi.domain import TextSource
from lisovyi.ingest.parsers import Extracted, NoTextFound, UnsupportedDocument
from lisovyi.uploads.reading import (
    EMPTY,
    MAX_UPLOAD_CHARS,
    TIMEOUT,
    TOO_LONG,
    UNREADABLE,
    UploadRejected,
    read_upload,
)


async def _rejected(data: bytes, filename: str, **kwargs) -> str:
    with pytest.raises(UploadRejected) as caught:
        await read_upload(data, filename, **kwargs)
    return caught.value.reason


async def test_text_file_is_read():
    extracted = await read_upload("Ягоди збирайте зранку.".encode(), "порада.txt")
    assert (extracted.text, extracted.source) == ("Ягоди збирайте зранку.", TextSource.TEXT)


async def test_empty_text_is_rejected():
    assert await _rejected(b"  \n\t \n", "порожня.txt") == EMPTY


async def test_scan_without_any_text_is_rejected_as_empty():
    def extract(data, filename, transcripts):
        raise NoTextFound("OCR found no text")

    assert await _rejected(b"%PDF", "скан.pdf", extract=extract) == EMPTY


async def test_unreadable_file_is_rejected():
    assert await _rejected(b"not a docx", "порада.docx") == UNREADABLE

    def extract(data, filename, transcripts):
        raise UnsupportedDocument("broken")

    assert await _rejected(b"%PDF", "a.pdf", extract=extract) == UNREADABLE


async def test_length_limit():
    assert len((await read_upload(("а" * MAX_UPLOAD_CHARS).encode(), "a.txt")).text) == 4000
    assert await _rejected(("а" * (MAX_UPLOAD_CHARS + 1)).encode(), "a.txt") == TOO_LONG
    # Порожні рядки на краях не рахуються.
    assert await read_upload(("а" * MAX_UPLOAD_CHARS + "\n\n").encode(), "a.txt")


async def test_slow_reading_times_out():
    def extract(data, filename, transcripts):
        time.sleep(0.3)
        return Extracted("пізно", TextSource.OCR)

    assert await _rejected(b"%PDF", "скан.pdf", timeout=0.05, extract=extract) == TIMEOUT


async def test_reading_runs_off_the_event_loop_thread():
    import threading

    threads = []

    def extract(data, filename, transcripts):
        threads.append(threading.current_thread())
        assert transcripts is None
        return Extracted("текст", TextSource.TEXT)

    await read_upload(b"x", "a.txt", extract=extract)
    assert threads[0] is not threading.main_thread()
