"""Чи окремі в Groq ліміти токенів на хвилину для різних моделей.

1. Запит на ~2K токенів до GROQ_MODEL, з відповіді — x-ratelimit-remaining-tokens.
2. Одразу малий запит до openai/gpt-oss-20b, його x-ratelimit-remaining-tokens.
3. Якщо в другої моделі залишок ≈ її повний ліміт мінус її власний малий запит — бакети
   окремі, і GROQ_FALLBACK_MODEL має сенс. Скрипт нічого не вмикає.

Ключ береться з GROQ_API_KEY і ніде не друкується.

    docker compose run --rm bot python scripts/check_rate_buckets.py
"""

import asyncio
import sys

from groq import AsyncGroq

from lisovyi.config import PROJECT_ROOT, Settings
from lisovyi.search.offline import load_archive
from lisovyi.search.retrieve import retrieve

SMALL_MODEL = "openai/gpt-oss-20b"
# ~2K токенів при ~2,5 символа на токен (вимірювання брифу 02).
BIG_CHARS = 5000
QUESTION = "Чим утеплити рукавичку під вербою?"
MAX_COMPLETION = 64


def big_prompt() -> str:
    index, resolver = load_archive(
        PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
    )
    rendered = retrieve(QUESTION, index, resolver).render()
    if len(rendered) < BIG_CHARS:
        sys.exit(f"pack for {QUESTION!r} is only {len(rendered)} chars, need {BIG_CHARS}")
    return f"Перекажи одним словом.\n\n{rendered[:BIG_CHARS]}"


async def probe(client: AsyncGroq, model: str, content: str) -> dict[str, int]:
    raw = await client.chat.completions.with_raw_response.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_completion_tokens=MAX_COMPLETION,
        reasoning_effort="low",
    )
    usage = (await raw.parse()).usage
    return {
        "limit": int(raw.headers["x-ratelimit-limit-tokens"]),
        "remaining": int(raw.headers["x-ratelimit-remaining-tokens"]),
        "prompt_tokens": usage.prompt_tokens,
        "total_tokens": usage.total_tokens,
        # Groq резервує в TPM prompt + max_completion_tokens ще до відповіді.
        "reserved": usage.prompt_tokens + MAX_COMPLETION,
    }


async def main() -> None:
    settings = Settings()
    if not settings.groq_api_key:
        sys.exit("GROQ_API_KEY is not set")
    if settings.groq_model == SMALL_MODEL:
        sys.exit(f"GROQ_MODEL is already {SMALL_MODEL}: nothing to compare")
    client = AsyncGroq(api_key=settings.groq_api_key, max_retries=0, timeout=60)

    big = await probe(client, settings.groq_model, big_prompt())
    small = await probe(client, SMALL_MODEL, "Скажи «так».")

    header = f"{'model':<24} {'limit':>7} {'remaining':>10} {'prompt':>7} {'total':>7}"
    print(f"{header} {'limit - remaining':>18}")
    for model, row in [(settings.groq_model, big), (SMALL_MODEL, small)]:
        print(
            f"{model:<24} {row['limit']:>7} {row['remaining']:>10} "
            f"{row['prompt_tokens']:>7} {row['total_tokens']:>7} "
            f"{row['limit'] - row['remaining']:>18}"
        )

    # Свій запит у залишку вже є: Groq списує prompt + max_completion_tokens наперед.
    if_separate = small["limit"] - small["reserved"]
    if_shared = big["remaining"] - small["reserved"]
    print()
    print(f"{SMALL_MODEL} remaining if buckets are separate: ~{if_separate}")
    print(f"{SMALL_MODEL} remaining if the bucket is shared:  ~{if_shared}")
    separate = abs(small["remaining"] - if_separate) < abs(small["remaining"] - if_shared)
    print(
        "verdict: SEPARATE buckets — a fallback model gets its own TPM"
        if separate
        else "verdict: SHARED bucket — a fallback model does not help with 429"
    )


if __name__ == "__main__":
    asyncio.run(main())
