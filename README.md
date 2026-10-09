# KnowledgeLake

A personal context hub on Telegram. Dump topics, tasks and notes in plain language; ask
"what should I focus on?" and get a ranked, explained answer.

```
Telegram ─► handlers ─► router ─► classifier (M5) ─► rules fast-path │ Groq │ NVIDIA │ rules fallback
                           │
                           ├─► add_item ──► extractor (M6) ─► Item + tags (Neon Postgres)
                           ├─► retrieve ──► ranker (M7): focus · goals · urgency · energy · staleness
                           └─► view_queue · list_tags · mark_done · search · update/get_context · help
```

## Setup

1. Copy `.env.example` to `.env` and fill in `DATABASE_URL` (Neon), `TELEGRAM_BOT_TOKEN`,
   `GROQ_API_KEY` and optionally `NVIDIA_API_KEY` / `NVIDIA_MODEL`.
2. Pick a mode: `KL_MODE=polling` for local runs, `KL_MODE=webhook` + `WEBHOOK_URL` for Cloud Run.
   Only **one** instance per bot token can poll at a time.

## Run

**Docker (recommended)**

```bash
docker compose up -d --build      # build & start
docker compose logs -f app        # watch logs
curl localhost:8080/health        # {"status":"ok",...,"module":"m7"}
curl localhost:8080/ready         # checks the database
docker compose down               # stop
```

Or pull the published image: `docker pull jackdiva/knowledgelake:latest`.

**Locally without Docker**

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows (use bin/activate on Linux/macOS)
pip install -e ".[dev]"
uvicorn app.main:app --port 8080
```

## Using the bot

Just type naturally:

| You say | What happens |
|---|---|
| `learn kafka consumer groups this weekend` | saved as an item (type, tags, time hint extracted) |
| `remind me to email Priya by Friday, quick one` | saved as a low-energy action due Friday |
| `what should I focus on?` / `what's next` | top 5 open items, ranked, with reasons |
| `what should I study for kafka` | ranked picks filtered to kafka |
| `I'm focusing on GCP this month` | sets current focus (boosts matching items) |
| `my goal is pass the AWS SA exam` | adds a goal |
| `feeling tired today` | energy = low (favours low-effort items) |
| `done with kafka` / `done #12` | marks done (asks which one if ambiguous) |
| `find anything about dbt` | searches open + done items |
| `show my queue` · `show my tags` · `what are my goals` | lists |
| `done with terraform, what's next?` | several intents in one message |

Commands work even if every LLM is down: `/add` `/queue` `/focus [topic]` `/done <#id|title>`
`/search <words>` `/tags` `/context` `/setfocus <topic>` `/goal <goal>` `/energy <low|medium|high>` `/help`.

## Debug endpoints

Open locally; when `WEBHOOK_SECRET` is set, send it as the `X-Debug-Token` header.

```bash
curl -X POST localhost:8080/debug/classify -H "Content-Type: application/json" -H "X-Debug-Token: <secret>" \
     -d '{"message": "done with terraform, whats next?"}'
curl -X POST localhost:8080/debug/extract  -H "Content-Type: application/json" -H "X-Debug-Token: <secret>" \
     -d '{"message": "remind me to email Priya by Friday"}'
curl -X POST localhost:8080/debug/message  -H "Content-Type: application/json" -H "X-Debug-Token: <secret>" \
     -d '{"message": "what should I focus on?", "telegram_id": 1}'   # full flow, writes to the DB
```

## Tests

```bash
pytest                                # ~300 offline tests (SQLite + fake LLM, no network)
KL_LIVE_TESTS=1 pytest -m live -s     # optional smoke tests against the real Groq/NVIDIA keys in .env
```

## Reliability notes

- If Groq fails, NVIDIA is tried, then deterministic rules, so every message gets a reply.
- Read timeouts and 4xx errors fail over to the next provider immediately; 429/5xx/connect errors are retried once.
- Each intent runs in its own savepoint; duplicate Telegram updates are ignored.
- Every message and classification is logged to `raw_messages` / `intent_logs` for later evaluation.

## Publishing a new image

```bash
docker build -t jackdiva/knowledgelake:m7 -t jackdiva/knowledgelake:latest .
docker push jackdiva/knowledgelake:m7 && docker push jackdiva/knowledgelake:latest
```
