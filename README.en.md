# Лісовий секретар (Forest Secretary)

[Українська](README.md) | English

[![CI](https://github.com/ZavarOvek/lisoviiy-secretar-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ZavarOvek/lisoviiy-secretar-bot/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

This is an English translation of [README.md](README.md). The detailed docs in `docs/` are in Ukrainian only. Texts that the bot shows to users stay in Ukrainian, with an English translation in parentheses.

## Context

This is a Broscorp test assignment for a Junior Python Developer position. It is graded on the quality of the bot and on how completely the requirements were gathered. The client here is a character in Telegram, Mishka-shkriabotushka: a bot that the developer interviewed to collect the requirements. The setting comes from the Ukrainian folk tale "Рукавичка" (The Mitten): a forest with eight mittens that serve as homes, each with three animals living in it. The forest is divided into a coniferous part, a deciduous part and the places by the river, and the document "Карта лісу" (Forest Map) describes this. The animals keep the "Лісовий архів" (Forest Archive), 78 pieces of advice in TXT, DOCX and PDF files (19 of them scans), and the bot answers from these documents and accepts new advice. In the requirements section, [C] means that the requirement was stated by Mishka herself, and [D] means a developer decision that she approved.

## What it is

"Лісовий секретар" is a Telegram bot for the animals of the eight mittens. It answers only from the documents of the Forest Archive and from the advice that the animals send in themselves, and it names the source every time. The answers are written by `openai/gpt-oss-120b` on the free Groq tier. The bot does the search, the choice of place and the list of sources by itself, without an LLM.

## Try it

Examples from the gold set ([docs/gold-questions.md](docs/gold-questions.md)):

- T18, "Які гриби біля сосон?" (Which mushrooms grow near the pines?) By the Forest Map "У соснах" is a coniferous-forest mitten, so the bot answers from "Грибний путівник хвойного лісу".
- T04, "Чим утеплити рукавичку під вербою?" (What should I use to insulate the mitten under the willow?) "Під вербою" belongs to both the deciduous forest and the river. The bot shows both pieces of advice, says that they differ, and does not choose.
- T02, "Коли треба утеплювати рукавичку?" (When should a mitten be insulated?) No place is named, so the bot gives short variants for each part of the forest and buttons with the mittens.
- T19 → T20, "Коли збори мешканців?" (When is the residents' meeting?) and then "У соснах". Every mitten has its own meetings, so the bot asks which one is meant, and the second message clarifies the first question.
- T05, "Коли Ведмідь лягає в сплячку?" (When does the Bear go into hibernation?) The advice of the Bear's mitten (У ліщині) outweighs the general Calendar.
- T22, "Коли прилітають лелеки?" (When do the storks arrive?) The archive has nothing about it, and the bot says so.
- Advice as a file. Send a PDF, DOCX or TXT: the bot asks for your name (only the first time), determines the place or asks you to choose it with buttons, and, if similar advice already exists, asks whether this is its new version.

## Running it

You need Docker and Docker Compose v2.

1. Bot token: in Telegram, write to @BotFather, use the command `/newbot`, and copy the token.
2. Groq key: console.groq.com → API Keys → Create API Key. It is free.
3. Copy the settings and put both values, `TELEGRAM_BOT_TOKEN` and `GROQ_API_KEY`, into `.env`:

   ```bash
   cp .env.example .env
   ```

4. Start it:

   ```bash
   docker compose up --build
   ```

On the first start Docker builds the image (Python and tesseract with the Ukrainian language for OCR of scans), and the bot applies the migrations and imports the 78 archive documents from `data/archive/`. A repeated start skips the files that were already imported. On a fresh clone before submission (the developer's laptop) the build from scratch, `docker compose build --no-cache`, took 1 min 56 s. The base images `python:3.12-slim` and `postgres:17` were already in the Docker cache, so downloading them is not included. From the start of the bot container to readiness it took 23 s.

The bot is ready when the log contains the line `Run polling for bot @…`:

```bash
docker compose logs bot
```

One token can run only one bot. If the same token is already running elsewhere, Telegram delivers each update to only one of them, and the bot will answer every other time.

### `.env` variables

| Variable | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | none | Token from @BotFather. Required. |
| `GROQ_API_KEY` | none | Groq key. Required. |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | The answering model. |
| `GROQ_REASONING_EFFORT` | `low` | Reasoning tokens also count toward the per-minute limit; `medium` did not improve how the instructions are followed (see "Known limitations"). |
| `GROQ_FALLBACK_MODEL` | empty | A second model for the case of a 429. Empty means waiting and retrying with the main model: `openai/gpt-oss-20b` loses caveats more often. |
| `GROQ_MAX_COMPLETION_TOKENS` | `512` | The ceiling for an answer together with reasoning. Groq reserves it in the limit before the answer, so anything extra eats the limit. |
| `DIALOGUE_TTL_SECONDS` | `900` | How long the bot remembers the previous question, so that it can accept "У соснах" as a clarification. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `lisovyi` | The database in the `db` container. It is not exposed outside. |

## Free Groq limits

For `openai/gpt-oss-120b` at the time of development the limits are 8K tokens per minute and 200K per day. In the full gold run before submission (31 questions, 28 model calls) one call cost 3 705 tokens on average: 3 504 prompt and 201 completion.

Groq reserves `prompt + max_completion_tokens` in the per-minute limit before the answer, which is ≈ 3 504 + 512 = 4 016 tokens per question. This is why the answer ceiling is kept at 512 tokens and not higher. That gives ~2 questions per minute (8 000 / 4 016) and ~50–54 per day per key: 200 000 / 4 016 if the daily limit counts the reservation, or 200 000 / 3 705 if it counts the actual tokens.

The bot sends one request to Groq at a time. When the limit is exhausted, the animal sees "Зараз у мене багато питань, відповім за хвилинку 🌿" (I have many questions right now, I will answer in a minute) once, and the question waits: up to 30 seconds in the queue behind the previous one, and then as long as Groq named in its 429 response, if that is no more than 30 seconds. If the wait would be longer, or Groq answered 429 twice in a row, the bot asks the animal to ask again in a minute. In the gold run this happened to one question out of 31 (T23).

## Client requirements

The requirements were agreed with Mishka (the client, see "Context") on 29.10.2026. The original formulations agreed with the client are in the Ukrainian [README.md](README.md); the text below is a translation. [C] means that Mishka stated the requirement herself, [D] that it is a developer decision that she approved or did not object to. The tests are in `tests/`.

1. [C] The bot works in private messages, and the animals of all eight mittens use it.
   **How it was done:** all routers except the group one are assembled under a single "private chats only" filter (`bot/private.py`). In a group the bot answers only to `/start` and `/help` ("Я працюю лише в особистих повідомленнях…", I only work in private messages) and ignores everything else; channels too. The command menu of a group has just those two. Tests: `test_groups.py`.

2. [C] The documents from the "Лісовий архів" folder are in the bot from the first start. How to pick up changes in the archive is up to the developer. [D] A copy of the archive is kept in the repository and is imported idempotently at startup.
   **How it was done:** `data/archive/` is imported on every start (`ingest/importer.py`). An unchanged file is skipped by its sha256. A changed file replaces the active document of the same place and topic, and that can also be an animal's advice that earlier replaced its previous version. Advice from an animal with its own topic is not touched by the import. Tests: `test_importer.py`.

3. [C] The animals send new advice only as PDF, Word or TXT files. They do not send photos. A plain text message is always a question.
   **How it was done:** `bot/uploads.py` accepts the file and `uploads/reading.py` reads it. For photos and other formats the bot explains that it accepts PDF, Word (.docx) and TXT. Any text without `/` counts as a question, except for one case: the bot has just asked for a name. Tests: `test_uploads.py`, `test_reading.py`.

4. [C] The bot answers only from the documents and names the source document every time. For a document from an animal it also names who sent it and when.
   **How it was done:** the model receives only the documents that were found and returns the numbers of those it relied on. The bot itself builds the "Джерела:" (Sources) block from the document titles and, for the advice of animals, adds the author and the date. A number outside the context is discarded. Tests: `test_answer_service.py`.

5. [C] The bot does not need to know who is asking. The animal names the place in the question ("які гриби біля сосон?"). The bot applies the Forest Map.
   **How it was done:** `search/places.py` finds a mitten or a part of the forest in the question using lemmas and synonyms. The bot translates a mitten into parts of the forest by the current Map. Only documents of the named place, its parts and the whole forest get into the search. Tests: `test_morph_places.py`, `test_retrieval_cases.py`.

6. [D] The place is not named:
   - if the answer differs across the parts of the forest, the bot briefly shows the variants for each part with sources and offers to name the place;
   - if the answer is specific to each mitten, the bot first asks which mitten is meant.

   The next message of the animal with a place name is taken as a clarification of the previous question, so a short conversation memory is needed.
   **How it was done:** when the top results contain documents of one topic from three mittens, the bot asks for the mitten with buttons, without calling the model. Otherwise the model gives variants by part, and the bot adds the mitten buttons. If within 15 minutes (`DIALOGUE_TTL_SECONDS`) the animal sends just a place name, the bot takes the previous question, removes the old place from it and adds the new one (`answer/dialogue.py`). Tests: `test_dialogue.py`, `test_answer_service.py`, `test_bot.py`.

7. [C] Advice for a specific place is more important, and the general one can be mentioned additionally. [D, she did not object] The order: mitten → part of the forest → general advice.
   **How it was done:** the context is assembled in layers "mitten → part → whole forest" (`search/retrieve.py`), every document is labelled with its layer, and the system prompt tells the model to keep this order. Tests: `test_retrieve.py`, `test_retrieval_cases.py`.

8. [D] "Під вербою" (Under the Willow), when the advice of the deciduous forest and of the river differs: the bot shows both with sources, says plainly that they differ, and does not choose on its own.
   **How it was done:** a detector (`search/conflicts.py`) decides whether there is a conflict, not the model. Then the model fills a separate item for each part, and the bot writes the introduction "…поради для них різняться" (the advice for them differs) and the phrase "Оберіть ту, що підходить саме вашому місцю." (Choose the one that fits your place.) Tests: `test_conflicts.py`, `test_answer_service.py`.

9. [D] If the documents have no answer, the bot says so honestly, invents nothing, and offers to clarify the question or to send a document. If an answer was found partially, the bot answers that part with a source and says what is missing.
   **How it was done:** if the search finds nothing, the bot replies "не знайшов" (not found) without the model. If there are documents but they have no answer or only a partial one, the model returns the answer kind `not_found` or `partial`. The bot asks again after an unusable answer, and after the second one it says that it cannot answer with confidence. Tests: `test_answer_service.py`.

10. [C] A newer version of the same document for the same place replaces the old one, and the animals see only the new one. Separate advice does not cancel the old one just because of the date; it is ranked by specificity. [D] The method of recognition is a developer decision, and Mishka stressed this directly: the bot classifies the document by type and place, and if such a document already exists, it asks the animal with a "замінити?" (replace?) button. The old version stays in the internal history but disappears from the search.
    **How it was done:** the bot determines the place of the new advice, looks among the advice of the same place for a candidate with the same topic or a similar content (`uploads/versions.py`), and asks "Замінити стару" (Replace the old one) or "Залишити обидві" (Keep both). The replaced advice gets the status `replaced` and disappears from the search. If the bot did not see a candidate, the animal can point to the old advice with the button "Це нова версія іншої поради?" (Is this a new version of other advice?). Tests: `test_versions.py`, `test_upload_service.py`, `test_uploads.py`.

11. [C] Any animal can replace documents, there are no admins ("у лісі всі рівні", everyone in the forest is equal).
    **How it was done:** replacement does not check the author, so an animal can replace both an archive document and someone else's. Tests: `test_upload_service.py` (archive), `test_delete.py` (someone else's, in a chain of replacements).

12. [C] When an animal sends a document for the first time, the bot asks it to give its real name and remembers it. The Telegram name is not suitable for this. A name is not needed for questions. [D] The document waits until the animal gives its name. The name can be changed with a command.
    **How it was done:** the name is stored in the `beasts` table, and the document waits in memory for up to 30 minutes. The name can be changed with the command `/name`. Tests: `test_uploads.py`, `test_upload_service.py`.

13. [D] An animal can delete only a document that it sent itself. Archive documents and those of others can only be replaced. Deletion is soft.
    **How it was done:** `/my` shows only the animal's own active advice with a delete button and a confirmation. Deletion sets the status `deleted`, and if the advice replaced another one, that one comes back. Tests: `test_delete.py`, `test_library.py`.

14. [C] The language of the answers is Ukrainian, the tone is simple and friendly, without technical words.
    **How it was done:** the system prompt requires this (`answer/prompt.py`), and `clean_text` removes markup and mentions of "(джерело N)" (source N) from the answer. All bot texts address the animal formally ("ви"), and a test catches the informal "ти". Tests: `test_texts.py`, `test_answer_service.py`.

15. [D] The Forest Map is not hard-coded. The current version of the document "Карта лісу" is always used, so after a confirmed replacement the bot immediately answers by the new map.
    **How it was done:** the bot parses the legend of the Map from the document (`ingest/forest_map.py`) into the `forest_map` table. An animal can send a new "Карта лісу", and after the confirmation the bot rebuilds the search. Tests: `test_forest_map.py`, `test_upload_service.py`, `test_delete.py`.

16. [D] Viewing documents: a list grouped by mittens, parts of the forest and general advice, with the author and the date. Separately, a "мої документи" (my documents) list with deletion.
    **How it was done:** `/docs` shows buttons for the groups (mittens, parts of the forest, the whole forest) and the list of documents of the group with the author and the date, and `/my` shows the animal's own advice with deletion. Tests: `test_library.py`.

17. [D] The first opening (/start): a greeting, a short note about the archive and the advice from the animals, an example question with a place, and how to send advice (PDF/Word/TXT).
    **How it was done:** the `/start` text is in `bot/handlers.py`. Tests: `test_bot.py`.

18. [D] Notifications: by default the bot never writes first. Those who want can subscribe to new advice and unsubscribe. A message to a subscriber says what changed (new advice or the replacement of which document), for which place, and who sent it. The author does not receive a notification about their own document. The filter by place is postponed.
    **How it was done:** `/subscribe` and `/unsubscribe`. The broadcast (`notify.py`) runs in the background, skips the author, and unsubscribes anyone who blocked the bot. Tests: `test_notify.py`, `test_library.py`.

### Technical decisions that did not need the client

The `.doc` format is not supported: the bot asks to save the file as `.docx`. For photos and other formats it politely explains which formats it accepts. Scanned PDFs from the animals go through the same OCR as the archive ones.

## Contradictions in the archive and how the bot resolves them

The brackets give the numbers of the gold-set questions that check each one ([docs/gold-questions.md](docs/gold-questions.md)).

1. **Insulation** (T01, T02, T21). The Calendar says "у листопаді" (in November), the advice of the coniferous forest says "у жовтні" (in October), and the deciduous one says "коли облетить листя, до першого снігу" (when the leaves fall, before the first snow). For the coniferous forest the bot answers "у жовтні" and may mention the Calendar as general advice.
2. **Hibernation** (T05, T06). The Calendar says "у грудні" (in December), while the Bear (У ліщині) and the Hedgehog (На узліссі) go to sleep in November. The advice of the mitten takes precedence.
3. **The ford** (T07). By the river, the ford above the dam is safe when the dam is open and the water has gone down. For "Біля бобрової греблі" crossing on foot is never allowed, only over the dam. The advice of the mitten wins.
4. **Nuts and acorns** (T09, T11). The summary of the Calendar about acorns and nuts in October contradicts its own text. The bot relies on the separate note: nuts from the end of August, acorns from mid-September to the end of October.
5. **Під вербою** (T04, T12). Mushrooms grow on the 4–5 day after rain (deciduous forest) or the next day (river). For insulation, the advice is oak leaves or reeds on the roots. The bot shows both variants and does not choose (requirement 8).
6. **Rain by the river** (T13). After heavy rain stay away from the bank for two days, while honey mushrooms come the next day after light rain. The answer must contain both facts.
7. **An evening under the oak** (T14). In autumn do not walk under the oaks in the evening because of wild boars, while guests in "Під дубом" are received only in the evening. The bot gives the rule of the mitten, shows the caveat, and says that the advice differs.
8. **Hunters on the clearing** (T15, T27). The route "Під ялиною → до дуба через просіку" has no caveat, while "Під дубом" says that hunters walk the clearing on Saturdays. The bot should add the caveat to the route; in the run before submission the model lost it (see "Known limitations").
9. **The Hare and the felling** (T16). In the pines the bark of young aspens on the felling is meant for the Hare, but at night an eagle owl hunts on the felling. The bot names both facts.
10. **The Magpie's signals** (T17). Three short cries mean hunters, for the whole forest. If the Magpie chatters twice, there is a stranger nearby, and this is only У ліщині. The bot should give the general answer and mention the exception; the general advice sometimes drops out here (see "Known limitations").
11. **"Які гриби біля сосон?"** (T18). "У соснах" has no mushroom note, so through the Forest Map the answer comes from "Грибний путівник хвойного лісу".

Two more were found later, while building the gold set:

- **T10**: nuts in the pines. The note of the deciduous forest does not apply there, and the bot does not apply it.
- **T28**: mushrooms on the forest edge. The mushroom guides of the forest parts do not apply there, because На узліссі does not belong to any part.

## Decisions the requirements do not state directly

The questions T05, T14, T17 and T23 are discussed in the section "Рішення розробника" (Developer decisions) of [docs/gold-questions.md](docs/gold-questions.md).

For the 19 scans in the archive there are hand-verified transcripts in `data/transcripts/`, tied to the sha256 of the file. OCR runs only if the file has changed, and for new scans: on the "birch bark" background it confuses digits ("350 кроків" turns into "850").

The bot asks the animal to confirm the numbers from a new scan with buttons, which is called "poor eyesight" ("поганий зір"). If the numbers are wrong, the bot asks to send a .docx or .txt file.

The place of new advice is first determined by rules (the title, the introductory sentence, a mitten in quotes), and if they do not work, the bot shows buttons. No LLM is involved here.

Deleting advice that replaced another one brings the replaced one back. Deletion does not erase knowledge from the archive.

Requirement 8 is handled by code: a detector decides whether there is a conflict, and the model only fills a separate item for each part. The introduction and the phrase "Оберіть ту, що підходить саме вашому місцю." (Choose the one that fits your place.) are written by the bot.

The whole bot addresses the animals formally ("ви"), because the model kept slipping from the informal "ти" to "ви".

The bot builds the sources. The model returns document numbers and the bot substitutes the titles, so an invented source is impossible.

## How it was verified

The tests with a database work with a separate database from `DATABASE_URL` and create it anew every time:

```bash
set -a; . ./.env; set +a
docker compose run --rm \
  -e DATABASE_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD@db/lisovyi_test" \
  bot pytest -q -rs
```

Before submission 677 tests passed, 0 were skipped, in 12 min 40 s on the developer's laptop. In the tests Telegram is replaced by a recorder of requests and Groq by a fake, and the database is PostgreSQL from the `db` container.

The gold set consists of 31 questions. For each one the expected behavior is recorded, with the documents that must be among the sources and what the answer must and must not say: [docs/gold-questions.md](docs/gold-questions.md). A run against the real Groq:

```bash
docker compose stop bot
docker compose run --rm -v "$PWD:/app" bot python scripts/run_gold.py
docker compose start bot
```

The bot should be stopped so that it does not share the per-minute limit with the run. In the full run before submission the kind of answer matched the expected one in 28 of 31 questions, and all required sources were among the sources of the answer in 26 of 31. It used 103 748 tokens (98 115 prompt and 5 633 completion), ≈52% of the daily limit, in 11 minutes. Discrepancies: T13 and T11 are described in "Known limitations", T23 in "Free Groq limits".

The script checks only the kind of answer and the sources. The content of the answers was checked by hand against the fields "Має сказати" (Must say) and "Не має" (Must not say) of the gold set, and this is how the limitations below were found.

## Known limitations

They were found by hand in live runs of the gold set. They survived two prompt versions and `reasoning_effort=medium`. The next step is a model that follows instructions better, but there is none on the free Groq tier.

- Merging advice for different cases. T08: for crossing over the stones near the willow the model adds the condition of the ford above the dam ("переконайтеся, що бобри відкрили греблю", make sure the beavers have opened the dam).
- Own conclusions. T26: "до першого снігу — це зазвичай листопад" (before the first snow is usually November).
- Inexact verbs and prepositions. T10: "падають" (fall) instead of "збирають" (gather); T27: "на північ" (to the north) instead of "на півночі" (in the north).
- General advice sometimes drops out (T07, T17), although the document is among the sources.
- Generalizing the addressee. To "Що їсти під вербою?" (What can be eaten under the willow?) the model answered "можна їсти комарів і мошки" (you can eat mosquitoes and midges), although in the record this is a treat for the Frog and the Sparrow.
- Small inventions in retelling: "берег, затоплений під час дощу" (a bank flooded during rain) instead of "берег, який заливало" (a bank that used to flood).
- Free Groq limits: ~50–54 questions per day per key and ~2 per minute (see "Free Groq limits").
- OCR of new scans confuses digits. This is why the bot asks to confirm the numbers, but it does not check the text outside the numbers.

The following cases were found by the single full gold run before submission (`reasoning_effort=low`). They were not checked separately with other prompt versions or with `reasoning_effort=medium`:

- A false "not in the records". T13: both needed documents ("Грибний путівник біля річки" and "Небезпеки біля річки") were in the context, and the model answered that there was no information.
- A caveat from another document gets lost. T15: the route "Під ялиною → до дуба" over the clearing came without the Saturday caveat about hunters, although the document "Під дубом" was in the context.
- Variants by part are not always given. T11: to "Коли збирати горіхи?" (When to gather nuts?) the model gave only the advice of the deciduous forest, without variants for the other parts.
- Word errors. T28: "кри́льце на ніжці" (a small wing on the stem) instead of "кільце" (ring); "запитайте у Їжак" (ask the Hedgehog, in the wrong case).

Despite this, the sources are always real, "не знайдено" (not found) and asking for the mitten are deterministic, and advice from another place does not get into the context: 45 of 45 search cases (`tests/test_retrieval_cases.py`).

## What next

The notification filter by place is postponed (requirement 18). It is worth moving to a model that follows instructions better when one appears on an available Groq tier. The names and cases of the forest parts are now described in several modules and could be merged into one table. A template database would make the tests faster: now every test with a database creates it anew and runs the migrations, so a full run takes ~13 minutes.

## Architecture

The `ingest/` module reads PDF, DOCX, TXT and scans, classifies documents by place, and imports the archive. `search/` recognizes the place by the current Map and assembles the BM25 context in layers "mitten → part → whole forest". `answer/` asks Groq with a strict JSON schema, validates the answer, and builds the list of sources. `uploads/` accepts the advice of the animals: the text, the place, a new version or separate advice. `notify.py` sends notifications to subscribers, and `bot/` is a thin aiogram layer. The data lives in PostgreSQL: documents with the statuses `active` / `replaced` / `deleted`, the animals, subscriptions, the Forest Map.

The details, with the path of a question and of advice, and the numbers behind the decisions, are in [docs/architecture.md](docs/architecture.md) (Ukrainian).

How the bot was built stage by stage, with the turning points and the checks: [docs/process.md](docs/process.md) (Ukrainian).
