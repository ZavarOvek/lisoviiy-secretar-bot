"""Сценарій без Telegram: звір замінює архівну пораду, потім видаляє свою — архівна повертається.

Як і upload_smoke.py, працює з окремою базою з DATABASE_URL і щоразу створює її наново:

    set -a; . ./.env; set +a
    docker compose run --rm -v "$PWD:/app" \\
      -e DATABASE_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD@db/lisovyi_smoke" \\
      bot python scripts/delete_smoke.py
"""

import asyncio
import hashlib
import os
import sys

from upload_smoke import check, recreate

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
BEAST = "Тестова Білочка"


def pack_files(search: SearchService) -> list[str]:
    files = [doc.filename for doc in search.search(QUESTION).docs]
    print(f"pack for {QUESTION!r}: {files}")
    return files


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

        squirrel = await uploads.register(1, BEAST)
        data = FIXTURE.read_bytes()
        extracted = await read_upload(data, FIXTURE.name)
        place = classify_upload(FIXTURE.stem, extracted.text, search.state.resolver.gloves)
        index = search.state.index
        candidate = find_version_candidate(
            extracted.text, place.scope, place.topic, index.documents, index,
            topic_from_rules=place.topic_from_rules,
        )  # fmt: skip
        check(candidate is not None and candidate.document.filename == OLD, f"candidate is {OLD}")
        saved = await uploads.save(
            squirrel, extracted, hashlib.sha256(data).hexdigest(), FIXTURE.stem, FIXTURE.name,
            place.scope, place.kind, place.topic, candidate.document.document_id,
        )  # fmt: skip
        print(f"saved: {saved}")
        files = pack_files(search)
        check(FIXTURE.name in files and OLD not in files, "search sees the beast's version")

        deleted = await uploads.delete(squirrel, saved.id)
        print(f"deleted: {deleted}")
        check(deleted.restored_title == "Грибний путівник хвойного лісу", "the guide is restored")
        files = pack_files(search)
        check(OLD in files and FIXTURE.name not in files, "search sees the archive guide again")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
