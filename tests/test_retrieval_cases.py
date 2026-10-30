import json
from pathlib import Path

import pytest

from lisovyi.config import PROJECT_ROOT
from lisovyi.ingest.importer import import_archive
from lisovyi.search.retrieve import DEFAULT_CHAR_BUDGET, ContextPack, merge_followup, retrieve
from lisovyi.search.service import SearchService

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "retrieval_cases.json").read_text(encoding="utf-8")
)


def _mismatches(case: dict, pack: ContextPack) -> list[str]:
    filenames = {doc.filename for doc in pack.docs}
    problems = []
    if set(pack.gloves) != set(case["gloves"]):
        problems.append(f"gloves {pack.gloves}")
    if case["parts"] is not None and {p.value for p in pack.parts} != set(case["parts"]):
        problems.append(f"parts {pack.parts}")
    if pack.mode != case["mode"]:
        problems.append(f"mode {pack.mode}")
    if missing := set(case["must_retrieve"]) - filenames:
        problems.append(f"missing {sorted(missing)}")
    if forbidden := set(case["must_not_retrieve"]) & filenames:
        problems.append(f"forbidden {sorted(forbidden)}")
    if len(pack.docs) > 1 and pack.chars > DEFAULT_CHAR_BUDGET:
        problems.append(f"{pack.chars} chars")
    return problems


def test_case_count():
    assert len(CASES) == 45


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_retrieval_case(case, archive_search):
    index, resolver = archive_search
    turns = case["turns"]
    question = merge_followup(turns[0], turns[1], resolver) if len(turns) == 2 else turns[0]

    pack = retrieve(question, index, resolver, char_budget=DEFAULT_CHAR_BUDGET)

    assert _mismatches(case, pack) == []


@pytest.mark.db
async def test_retrieval_cases_through_the_database(sessionmaker):
    """Ті самі 45 кейсів через робочий шлях: імпорт у БД → SearchService.reload → search."""
    async with sessionmaker() as session:
        await import_archive(
            session, PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
        )
    service = SearchService(sessionmaker, DEFAULT_CHAR_BUDGET)
    await service.reload()

    failures = {}
    for case in CASES:
        *previous, message = case["turns"]
        pack = service.search(message, previous[0] if previous else None)
        if problems := _mismatches(case, pack):
            failures[case["id"]] = problems
    assert failures == {}
