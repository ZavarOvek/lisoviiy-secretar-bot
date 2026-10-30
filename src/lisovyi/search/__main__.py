"""`python -m lisovyi.search "питання" [--previous "попереднє питання"]`: пакет контексту з БД."""

import argparse
import asyncio

from lisovyi.config import Settings
from lisovyi.db.session import make_engine, make_sessionmaker
from lisovyi.domain import PART_NAMES
from lisovyi.search.retrieve import ContextPack
from lisovyi.search.service import SearchService


async def build_pack(settings: Settings, question: str, previous: str | None) -> ContextPack:
    engine = make_engine(settings.db_url())
    try:
        service = SearchService(make_sessionmaker(engine), settings.context_char_budget)
        await service.reload()
        return service.search(question, previous)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m lisovyi.search")
    parser.add_argument("question")
    parser.add_argument("--previous", default=None, help="попереднє питання звіра")
    args = parser.parse_args()

    pack = asyncio.run(build_pack(Settings(), args.question, args.previous))
    print(f"режим: {pack.mode}")
    print(f"рукавички: {', '.join(pack.gloves) or '—'}")
    print(f"частини: {', '.join(PART_NAMES[p] for p in pack.parts) or '—'}")
    print(f"документів: {len(pack.docs)}, символів: {pack.chars}")
    print()
    print(pack.render())


if __name__ == "__main__":
    main()
