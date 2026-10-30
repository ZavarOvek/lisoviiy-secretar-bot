"""Скільки токенів gpt-oss-120b на Groq дає пакет контексту: символів на токен.

Бере три кейси з retrieval_cases.json з найбільшим `render()`, будує пакети з архіву без БД
і шле кожен одним запитом до Groq. Друкує usage.prompt_tokens. Ключ береться з GROQ_API_KEY
і ніде не друкується.

    docker compose run --rm bot python scripts/measure_context_tokens.py
"""

import json
import math
import sys
import time
import urllib.error
import urllib.request

from lisovyi.config import PROJECT_ROOT, Settings
from lisovyi.search.offline import load_archive
from lisovyi.search.retrieve import merge_followup, retrieve

URL = "https://api.groq.com/openai/v1/chat/completions"
CASES = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_cases.json"
LARGEST = 3
PAUSE_SECONDS = 10
# Якщо Groq не прислав Retry-After: ліміт рахується за хвилину.
RETRY_SECONDS = 60


def largest_packs(char_budget: int) -> list[tuple[str, str]]:
    index, resolver = load_archive(
        PROJECT_ROOT / "data" / "archive", PROJECT_ROOT / "data" / "transcripts"
    )
    rendered = []
    for case in json.loads(CASES.read_text(encoding="utf-8")):
        turns = case["turns"]
        question = merge_followup(turns[0], turns[1], resolver) if len(turns) == 2 else turns[0]
        pack = retrieve(question, index, resolver, char_budget=char_budget)
        rendered.append((case["id"], pack.render()))
    rendered.sort(key=lambda item: len(item[1]), reverse=True)
    return rendered[:LARGEST]


def prompt_tokens(api_key: str, model: str, content: str) -> dict:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_completion_tokens": 32,
            "reasoning_effort": "low",
        }
    ).encode()
    request = urllib.request.Request(
        URL,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # Типовий User-Agent urllib Cloudflare перед Groq може відхилити.
            "User-Agent": "lisovyi-arkhiv/measure-context-tokens",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["usage"]


def measure(api_key: str, model: str, case_id: str, content: str) -> dict:
    """Один запит; на 429 (ліміт токенів за хвилину) — одна повторна спроба після паузи."""
    for attempt in (1, 2):
        try:
            return prompt_tokens(api_key, model, content)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt == 1:
                wait = math.ceil(float(exc.headers.get("Retry-After") or RETRY_SECONDS))
                print(f"{case_id}: rate limited, retrying in {wait} s", file=sys.stderr)
                time.sleep(wait)
                continue
            # Тіло відповіді Groq містить опис помилки, а не ключ.
            sys.exit(f"{case_id}: HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}")
        except urllib.error.URLError as exc:
            sys.exit(f"{case_id}: cannot reach Groq: {exc.reason}")
    raise AssertionError("unreachable")


def main() -> None:
    settings = Settings()
    if not settings.groq_api_key:
        sys.exit("GROQ_API_KEY is not set")
    packs = largest_packs(settings.context_char_budget)

    print(f"model: {settings.groq_model}, char budget: {settings.context_char_budget}")
    print(f"{'case':<6} {'chars':>6} {'prompt_tokens':>14} {'chars/token':>12}")
    for i, (case_id, content) in enumerate(packs):
        if i:
            time.sleep(PAUSE_SECONDS)
        usage = measure(settings.groq_api_key, settings.groq_model, case_id, content)
        tokens = usage["prompt_tokens"]
        print(f"{case_id:<6} {len(content):>6} {tokens:>14} {len(content) / tokens:>12.2f}")


if __name__ == "__main__":
    main()
