"""Gold-набір наживо: повний шлях AnswerService.answer для кожного питання.

Імпорт архіву в базу (як на старті), SearchService.reload, далі питання по черзі, кожне в
окремому chat_id; для двох реплік обидві йдуть у той самий chat_id, оцінюється друга.
Результат — gold_runs/<UTC-час>.jsonl, наприкінці — зведення kind / evidence / токени.
Зміст відповідей скрипт не оцінює.

Бюджет — ~100–120K токенів з добових 200K: запускати один раз, повторно — лише провалені:

    docker compose run --rm -v "$PWD:/app" bot python scripts/run_gold.py [--only T04,T12]

`--model` підміняє GROQ_MODEL на один прогін і вимикає запасну модель: так видно якість
саме цієї моделі. `--reasoning-effort` і `--max-completion-tokens` так само перекривають
GROQ_REASONING_EFFORT і GROQ_MAX_COMPLETION_TOKENS лише на цей прогін.
"""

import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from lisovyi.__main__ import make_llm
from lisovyi.answer.service import AnswerService, Reply
from lisovyi.config import PROJECT_ROOT, Settings
from lisovyi.db.session import make_engine, make_sessionmaker, run_migrations
from lisovyi.ingest.importer import import_archive
from lisovyi.search.service import SearchService

GOLD = PROJECT_ROOT / "tests" / "fixtures" / "gold_questions.json"
OUT_DIR = PROJECT_ROOT / "gold_runs"
# Скрипту дозволено чекати на 429 довше, ніж звірові в боті.
MAX_WAIT = 70.0

EXPECTED_KIND = {
    "answer": {"answer"},
    "answer_with_general_note": {"answer"},
    "answer_with_caveat": {"answer"},
    "answer_general_only": {"answer"},
    "answer_general_plus_glove_note": {"answer"},
    # Бриф 05: конфлікт частин «Під вербою» — лише відповідь по частинах; звичайна
    # відповідь тут означає, що шлях по частинах не спрацював.
    "conflict_both": {"conflict"},
    # Питання поза gold-набором (--extra): очікуваного kind немає.
    "extra": set(),
    "variants_by_part": {"variants"},
    "ask_glove": {"ask_glove"},
    "not_found": {"not_found"},
    "partial": {"partial"},
}

log = logging.getLogger("run_gold")


async def ask(answers: AnswerService, chat_id: int, turns: list[str]) -> Reply:
    async def notify(text: str) -> None:
        log.info("intermediate message to chat %d: %s", chat_id, text)

    reply = None
    for turn in turns:
        reply = await answers.answer(chat_id, turn, notify)
    assert reply is not None
    return reply


def record(question: dict[str, Any], reply: Reply, settings: Settings) -> dict[str, Any]:
    return {
        "id": question["id"],
        # Модель, що справді відповіла (з останньої спроби); без виклику LLM — налаштована.
        "model": reply.attempts[-1].model if reply.attempts else settings.groq_model,
        "fallback_model": settings.groq_fallback_model or None,
        "reasoning_effort": settings.groq_reasoning_effort,
        "max_completion_tokens": settings.groq_max_completion_tokens,
        "attempts": [asdict(attempt) for attempt in reply.attempts],
        "question": question["turns"][-1],
        "turns": question["turns"],
        "behavior": question["behavior"],
        "mode": str(reply.mode),
        "context": reply.context,
        "raw": reply.raw,
        "reply": reply.text,
        "kind": reply.kind,
        "sources": reply.sources,
        "usage": asdict(reply.usage) if reply.usage else None,
        "retries": reply.retries,
    }


def summarize(rows: list[dict[str, Any]], gold: dict[str, dict[str, Any]]) -> None:
    print()
    print(f"{'id':<4} {'behavior':<31} {'expected':<18} {'kind':<10} {'evidence':>8}  mismatch")
    totals = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0}
    kind_ok = evidence_ok = 0
    for row in rows:
        question = gold[row["id"]]
        expected = EXPECTED_KIND[question["behavior"]]
        evidence = sorted({item["doc"] for item in question["evidence"]})
        found = [doc for doc in evidence if doc in row["sources"]]
        # Для --extra очікуваного kind немає: рядок лише показує, що вийшло.
        kind_match = not expected or row["kind"] in expected
        evidence_match = len(found) == len(evidence)
        kind_ok += kind_match
        evidence_ok += evidence_match
        flags = [
            name for name, ok in [("kind", kind_match), ("evidence", evidence_match)] if not ok
        ]
        print(
            f"{row['id']:<4} {question['behavior']:<31} {'/'.join(sorted(expected)) or '-':<18} "
            f"{row['kind']:<10} {len(found):>3}/{len(evidence):<4}  {', '.join(flags)}"
        )
        for key in totals:
            totals[key] += (row["usage"] or {}).get(key) or 0
    print()
    print(f"kind matches: {kind_ok}/{len(rows)}")
    print(f"evidence fully among sources: {evidence_ok}/{len(rows)}")
    attempts = [attempt for row in rows for attempt in row["attempts"]]
    completions = [a["completion_tokens"] for a in attempts if a["completion_tokens"] is not None]
    truncated = sum(a["finish_reason"] == "length" for a in attempts)
    print(
        f"model calls: {len(attempts)}, finish_reason=length: {truncated}, "
        f"max completion_tokens per call: {max(completions, default=0)}"
    )
    print("tokens: " + " ".join(f"{key.removesuffix('_tokens')}={n}" for key, n in totals.items()))


async def main(
    only: set[str] | None,
    model: str | None,
    reasoning_effort: str | None = None,
    max_completion_tokens: int | None = None,
    extra: list[str] | None = None,
) -> None:
    settings = Settings()
    if model:
        settings = settings.model_copy(update={"groq_model": model, "groq_fallback_model": ""})
    if reasoning_effort:
        settings = settings.model_copy(update={"groq_reasoning_effort": reasoning_effort})
    if max_completion_tokens:
        settings = settings.model_copy(update={"groq_max_completion_tokens": max_completion_tokens})
    if not settings.groq_api_key:
        raise SystemExit("GROQ_API_KEY is not set")
    gold = {item["id"]: item for item in json.loads(GOLD.read_text(encoding="utf-8"))}
    if only:
        unknown = only - gold.keys()
        if unknown:
            raise SystemExit(f"unknown gold ids: {', '.join(sorted(unknown))}")
    questions = [item for item in gold.values() if not only or item["id"] in only]
    for number, text in enumerate(extra or [], start=1):
        item = {"id": f"X{number}", "turns": [text], "behavior": "extra", "evidence": []}
        gold[item["id"]] = item
        questions.append(item)

    db_url = settings.db_url()
    await asyncio.to_thread(run_migrations, db_url)
    engine = make_engine(db_url)
    try:
        sessionmaker = make_sessionmaker(engine)
        async with sessionmaker() as session:
            stats = await import_archive(session, settings.archive_dir, settings.transcripts_dir)
        log.info("archive import: inserted=%d skipped=%d", stats.inserted, stats.skipped)
        search = SearchService(sessionmaker, settings.context_char_budget)
        await search.reload()
    finally:
        await engine.dispose()

    llm = make_llm(settings)
    llm.max_wait = MAX_WAIT
    answers = AnswerService(search, llm, dialogue_ttl=settings.dialogue_ttl_seconds)

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"
    rows = []
    with out.open("w", encoding="utf-8") as file:
        for chat_id, question in enumerate(questions, start=1):
            log.info("%s: %s", question["id"], " → ".join(question["turns"]))
            reply = await ask(answers, chat_id, question["turns"])
            row = record(question, reply, settings)
            rows.append(row)
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
            file.flush()
    print(f"written: {out.relative_to(PROJECT_ROOT)}")
    print(
        f"model: {settings.groq_model}, fallback: {settings.groq_fallback_model or '-'}, "
        f"reasoning_effort: {settings.groq_reasoning_effort}, "
        f"max_completion_tokens: {settings.groq_max_completion_tokens}"
    )
    summarize(rows, gold)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="python scripts/run_gold.py")
    parser.add_argument("--only", help="comma-separated gold ids, e.g. T04,T12")
    parser.add_argument(
        "--extra", action="append", default=[], help="a question outside the gold set (repeatable)"
    )
    parser.add_argument("--model", help="Groq model for this run only; disables the fallback")
    parser.add_argument(
        "--reasoning-effort", choices=["low", "medium", "high"], help="for this run only"
    )
    parser.add_argument(
        "--max-completion-tokens", type=int, help="completion budget for this run only"
    )
    args = parser.parse_args()
    only = {item.strip() for item in args.only.split(",") if item.strip()} if args.only else None
    if args.max_completion_tokens is not None and args.max_completion_tokens <= 0:
        parser.error("--max-completion-tokens must be positive")
    asyncio.run(
        main(only, args.model, args.reasoning_effort, args.max_completion_tokens, args.extra)
    )
