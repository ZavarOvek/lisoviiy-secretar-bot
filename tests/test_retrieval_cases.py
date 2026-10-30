import json
from pathlib import Path

import pytest

from lisovyi.search.retrieve import DEFAULT_CHAR_BUDGET, merge_followup, retrieve

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "retrieval_cases.json").read_text(encoding="utf-8")
)


def test_case_count():
    assert len(CASES) == 45


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_retrieval_case(case, archive_search):
    index, resolver = archive_search
    turns = case["turns"]
    question = merge_followup(turns[0], turns[1], resolver) if len(turns) == 2 else turns[0]

    pack = retrieve(question, index, resolver, char_budget=DEFAULT_CHAR_BUDGET)

    filenames = {doc.filename for doc in pack.docs}
    assert set(pack.gloves) == set(case["gloves"])
    if case["parts"] is not None:
        assert {part.value for part in pack.parts} == set(case["parts"])
    assert pack.mode == case["mode"]
    assert set(case["must_retrieve"]) <= filenames
    assert set(case["must_not_retrieve"]).isdisjoint(filenames)
    if len(pack.docs) > 1:
        assert pack.chars <= DEFAULT_CHAR_BUDGET
