"""Сценарій без Telegram і без Groq: звір замінює пораду, пошук і відповідь бачать нову.

Працює з окремою базою з DATABASE_URL і щоразу створює її наново (як тести), тож базу
бота не чіпає:

    set -a; . ./.env; set +a
    docker compose run --rm -v "$PWD:/app" \\
      -e DATABASE_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD@db/lisovyi_smoke" \\
      bot python scripts/upload_smoke.py
"""

import asyncio
import json
import os
import re
import sys

import asyncpg
from sqlalchemy import make_url

from lisovyi.answer.llm import Completion, Usage
from lisovyi.answer.service import AnswerService
from lisovyi.config import PROJECT_ROOT, Settings
from lisovyi.db.session import make_engine, make_sessionmaker, run_migrations
from lisovyi.ingest.classify import classify_upload
from lisovyi.ingest.importer import import_archive
from lisovyi.search.service import SearchService
from lisovyi.uploads.reading import read_upload
from lisovyi.uploads.service import UploadService
from lisovyi.uploads.versions import find_version_candidate

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "uploads" / "гриби_нова_версія.docx"
OLD = "Грибний путівник хвойного лісу.pdf"
QUESTION = "Які гриби біля сосон?"
BEAST = "Тестовий Їжак"


class CitingLLM:
    """Мок LLM: цитує блок пакета з назвою файлу `filename`, Groq не викликається.

    Перше джерело пакета для «Які гриби біля сосон?» — документ рукавички «У соснах»
    (шар рукавички йде першим), тож мок, що цитує [1], автора не показав би ніколи.
    """

    model = "mock"

    def __init__(self, filename: str):
        self.filename = filename

    async def complete(self, messages, notify=None) -> Completion:
        user = messages[-1]["content"]
        number = re.search(rf"\[Джерело (\d+): {re.escape(self.filename)} \|", user)
        sources = [int(number[1])] if number else []
        content = json.dumps({"kind": "answer", "text": "Ось що є в записах.", "sources": sources})
        return Completion("mock", content, "stop", Usage(0, None, 0), 0.0)


def check(condition: bool, what: str) -> None:
    print(f"{'OK  ' if condition else 'FAIL'} {what}")
    if not condition:
        sys.exit(1)


async def recreate(url: str) -> None:
    parsed = make_url(url)
    if parsed.database == os.environ.get("POSTGRES_DB"):
        sys.exit("DATABASE_URL points at the bot's own database; use a disposable one")
    admin = parsed.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(admin.render_as_string(hide_password=False))
    try:
        quoted = '"' + parsed.database.replace('"', '""') + '"'
        await conn.execute(f"DROP DATABASE IF EXISTS {quoted} WITH (FORCE)")
        await conn.execute(f"CREATE DATABASE {quoted}")
    finally:
        await conn.close()


async def main() -> None:
    settings = Settings()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    await recreate(url)
    await asyncio.to_thread(run_migrations, url)
    engine = make_engine(url)
    try:
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session:
            stats = await import_archive(session, settings.archive_dir, settings.transcripts_dir)
        print(f"archive import: inserted={stats.inserted}")
        search = SearchService(sessionmaker)
        await search.reload()
        uploads = UploadService(sessionmaker, search)

        beast = await uploads.register(1, BEAST)
        data = FIXTURE.read_bytes()
        extracted = await read_upload(data, FIXTURE.name)
        place = classify_upload(FIXTURE.stem, extracted.text, search.state.resolver.gloves)
        print(f"place: {place}")
        index = search.state.index
        candidate = find_version_candidate(
            extracted.text, place.scope, place.topic, index.documents, index,
            topic_from_rules=place.topic_from_rules,
        )  # fmt: skip
        check(candidate is not None and candidate.document.filename == OLD, f"candidate is {OLD}")
        saved = await uploads.save(
            beast, extracted, data, FIXTURE.stem, FIXTURE.name, place.scope, place.kind,
            place.topic, candidate.document.document_id,
        )  # fmt: skip
        print(f"saved: {saved}")

        pack = search.search(QUESTION)
        context = [doc.filename for doc in pack.docs]
        print(f"pack for {QUESTION!r}: {context}")
        check(FIXTURE.name in context, "the new version is in the pack")
        check(OLD not in context, "the old guide is not in the pack")

        answers = AnswerService(search, CitingLLM(FIXTURE.name), dialogue_ttl=900)
        reply = await answers.answer(1, QUESTION)
        print("reply:")
        print(reply.text)
        pattern = rf"\(автор: {BEAST}, \d\d\.\d\d\.\d{{4}}\)"
        check(re.search(pattern, reply.text) is not None, f"sources name '(автор: {BEAST}, …)'")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
