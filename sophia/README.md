# Bills List & Dispute Assistant

Owner: Sophia Nguyen, Student 5.

## What this feature does

Tracks recurring bills and subscriptions, projects when they'll next charge,
shows a month-by-month "what to set aside" calendar, tracks paid/due/overdue
status per bill, drafts dispute letters for charges that look wrong, and
answers plain-language questions about the bills through a small chat
assistant ("Ask Tally"). Nothing writes to the database without an explicit
user action: every change the chat proposes becomes a **pending suggestion**,
shown both as a card in the chat reply and in the Suggestions panel beneath
it (with field-level detail — a before/after diff for updates, the row being
removed for deletes). Approving applies it through the same services layer a
manual edit uses and refreshes the table in place; rejecting discards it. The
outcome — applied, rejected, or failed — is written back into the chat
transcript, so the assistant's next turn knows what actually happened and
never reports an unapproved or failed change as done. Disputes are drafts
only — there is no send integration.

## Run it

### Docker Compose (from the repo root)

```
docker compose build bills-frontend bills-backend bills-db
docker compose up -d
```

Then open `http://localhost:3005`. `docker compose down` to stop.

### Local, without Docker

Three processes, each from the repo root so `sophia` resolves as a package:

```
DB_PATH=./bills.db PORT=6005 python sophia/database/app.py
BILLS_DB_API_URL=http://localhost:6005 PORT=5005 python -m sophia.backend.app
```

The frontend is static files; open `sophia/frontend/index.html` directly, or
serve the folder with any static file server, once `nginx.conf`'s proxy
targets are pointed at wherever the backend is actually running (the
Dockerized frontend proxies through nginx; a bare static-file server won't
proxy `/api/` or `/ui/`, so hitting the backend directly on `:5005` is
easiest for local iteration).

Architecture diagrams:
[individual architecture](../docs/architecture/r0-d1-individual-architecture.png),
[compose topology](../docs/architecture/r0-d2-docker-compose-architecture.png),
[dispute loop](../docs/architecture/r0-d3-paoa-dispute-loop.png),
[chat loop](../docs/architecture/r0-d4-paoa-chat-loop.png).

## Ports and env vars

| Service | Port | Key env vars |
|---|---|---|
| `bills-frontend` | 3005 | — (static + nginx proxy) |
| `bills-backend` | 5005 | `PORT`, `BILLS_DB_API_URL` (default `http://bills-db:6005`), `FRONTEND_ORIGIN` (default `http://localhost:3005`), `TRANSACTIONS_DB_API_URL` (optional; unset → stub), `OLLAMA_URL` (default `http://host.docker.internal:11434`), `DRAFT_MODEL` (`llama3.1:8b`), `CHAT_MODEL` (`qwen2.5:3b`), `DEMO_TODAY` (default `2026-10-01`), `AI_TIMEOUT_SECONDS` (default `90`), `OLLAMA_KEEP_ALIVE` (default `30m`), `AI_TEMPERATURE` (default `0.2`), `GROUNDED_TEMPERATURE` (default `0`) |
| `bills-db` | 6005 | `PORT`, `DB_PATH` (default `./bills.db`) |

`DEMO_TODAY` is parsed once in `sophia/backend/config.py`; nothing under
`sophia/backend/engine/` ever calls `date.today()` or `datetime.now()` —
every engine function takes `today` as an explicit argument.

Compose sets `PORT`, `BILLS_DB_API_URL`, `FRONTEND_ORIGIN`, `OLLAMA_URL`, `OLLAMA_KEEP_ALIVE` and the MCP/RAG switches
for the backend — the model and demo-clock values are in-container defaults
from `config.py`. In compose, Bills uses the team's shared `ollama` service
(`OLLAMA_URL=http://ollama:11434`); the first `docker compose up ollama`
pulls three models into its volume (`llama3.1:8b` 4.9 GB, `qwen2.5:3b`
1.9 GB, `qwen2.5:0.5b`), so start it early on demo day.
The `config.py` default (`http://host.docker.internal:11434`) remains the
fallback for running bills-backend without the ollama service, and anyone
running the backend bare on a machine without Docker Desktop should set
`OLLAMA_URL=http://localhost:11434`; the demo clock stays
`DEMO_TODAY=2026-10-01` by default and is overridable per environment. The seed
(`sophia/database/seed.py`) is written relative to 2026-08-20 and moved
`DEMO_DATE_OFFSET_DAYS` (42) forward when it runs, payments staying on their
billing cycle so every cached status holds at the demo clock; after pulling a
seed change, drop and recreate the `bills_data` volume (`docker compose rm -sf
bills-db && docker volume rm <project>_bills_data && docker compose up -d bills-db`).

## The two AI calls

Exactly two, both through `sophia/backend/ai/guard.py`: validate the JSON
response against a hand-written schema (`ai/schemas.py`, no jsonschema/
pydantic), retry once with the validation error appended to the prompt on
failure, and fall back to a templated response (`fallback: true`) if the
second attempt also fails. Neither call ever raises out to the route.

1. **Dispute drafting** (`ai/dispute_prompt.py`, `DRAFT_MODEL`): given the
   bill, its last 6 payments, the reason, and the payment method (or "not
   recorded"), drafts `{letter_text, steps, escalation,
   payment_method_note}`. Code-enforced on top of the model's output
   (`enforce_payment_method_step`): a direct-debit bill's steps must mention
   removing the payment authority; a card bill's steps must mention
   cancelling from the app's account page.
2. **Chat** (`ai/chat_prompt.py`, `CHAT_MODEL`): given a compact bills list
   (id, name, amount, cadence, next date, type) and six few-shot examples
   (two questions, a bill update, a dispute create, a bill create, and an
   under-specified create that asks for the missing details instead),
   classifies the message into `{op, entity, id, fields, question, say}`.
   `question` resolves in code (`total`, `barely_using`, `upcoming`); `op`
   becomes a pending suggestion the user must approve before anything is
   written.
   Kept deliberately short — accuracy degrades with long context.

## Date engine rules

- **Actual vs predicted vs overdue**: an occurrence is `actual` if a payment
  for that bill exists within ±3 days of it, `overdue` if its current cycle
  (`current_cycle_due`) is strictly before today with no matching payment —
  `timeline()`'s forward-only window otherwise drops these entirely, so
  `project()`'s normal walk is topped up with one synthetic occurrence per
  bill for this case — else `predicted`. The `Predicted` tag itself is
  hidden inside the first 30 days of the timeline (an imminent charge reads
  as expected, not speculative); `Overdue` always shows, at the real cents
  amount rather than a rounded estimate, since it's a real unpaid charge.
- **Usual vs extra** (calendar breakdown): a bill already billing before the
  month and still active is "usual" — it contributes `expected_per_month ×
  usual_range` to the month's low/high, plus an `extra_occurrence` line for
  any occurrences beyond that base count. A bill with no prior occurrence
  that bills at least once this month is a "starts" line. A bill whose
  `end_date` falls in this month counts its occurrences as usual and adds a
  zero-cost "ends" note; the month after, it contributes nothing.
- **Status**: `paid` / `due` / `overdue`, derived from the latest occurrence
  at or before `today` and whether a payment covers it — see
  `sophia/backend/engine/status.py` for the exact rules and labels. Status is
  derived on every read; the `status` column in the database is a cache
  refreshed only on write paths (bill create/update/cancel, payment
  create/update/delete), never by a GET. Month
  arithmetic (`add_months`) is computed directly from the anchor date, never
  by repeatedly stepping — this matters for 31st-anchored monthly bills,
  where re-adding a step from an already-clamped date drifts (28 Feb + 1
  month is 31 Mar from the anchor, not 28 Mar from re-adding a month to 28
  Feb).

## Money formatting convention

Integer cents everywhere internally. `format_actual` renders exact amounts
("$1,100.00"); `format_estimate`/`format_estimate_single` round to whole
dollars, half rounding up, and collapse an equal lo/hi range to a single
figure ("$379–415", or "$379" when lo == hi).

## RAG corpus (Bills)

The shared retrieval server (`ai-services/rag-server/`) ingests every folder
under its `sources/` directory into a Chroma collection named after the
folder. Bills' files live in `ai-services/rag-server/sources/billing/`, beside
the two hand-written files that folder already held (`billing_overview.md`,
`billing_policy.pdf`), so the server ingests them into the `billing`
collection. Bills' own files are one Markdown file per bill (name, merchant,
type, cadence, amount, next billing date, stored status, payment method, last
payment, open disputes), generated from the bills database by
`sophia/rag/build_corpus.py` and committed, so the server can ingest at startup
without `bills-db` running. Each file stays under 800 characters so the
server's splitter keeps it as one chunk; the `bill-*.md` files are never
hand-written, and Bills answers use only them, never the two hand-written files.

Regenerate from the repo root whenever the seed or the demo data changes:

```
BILLS_DB_API_URL=http://localhost:6005 python -m sophia.rag.build_corpus   # from a running bills-db
python -m sophia.rag.build_corpus --from-seed                                 # from sophia/database/seed.py
python -m sophia.rag.build_corpus --from-seed --check                         # what Sophia-CI runs
```

Never edit the `bill-*.md` files by hand: the `--check` form exits 1 when the
committed `bill-*.md` files differ from a fresh build (other files in the
folder are not managed), and the `test` job in Sophia-CI runs it.
RAG itself never runs in CI (the rubric has AI Mode, MCP and RAG disabled
during CI/CD): `sophia/rag/test_sources_bills.py`, which loads the folder
through the shared server's loader and checks retrieval order, runs locally
and skips in CI. The backend reaches retrieval only through the MCP tool
`retrieve_context(feature="billing", question, k)` (that wiring is a separate
PR); `sophia/rag/mcp_ragtest.py` is the end-to-end check through the tool.
Decision record: `docs/release-1/sophia/adr-bills-corpus-folder-model.md`.

## MCP and RAG (Bills)

bills-backend reaches the shared MCP server through `clients/mcp_server.py` (a `fastmcp.Client` behind an allow-list; `MCP_SERVER_URL`, default `http://host.docker.internal:8000/mcp`). On Docker Desktop, `host.docker.internal` reaches a server bound to 127.0.0.1; on Linux, `host-gateway` resolves to the bridge address, so a 127.0.0.1-bound MCP server refuses the connection — start it as `FASTMCP_HOST=0.0.0.0 python ai-services/mcp-server/server.py`.
Two tools are called: `search_transactions(merchant=<bill.merchant>)` (Tools card, "Match transactions") and `retrieve_context(feature="billing", question, k)` (Ask with evidence); the `billing` collection also holds two hand-written files, so Bills keeps only its own `bill-*.md` sources. Retrieval is reached only through the MCP tool, so there is no `RAG_SERVER_URL`; the shared server is retrieval-only (team decision 18 Sep; `docs/release-1/sophia/adr-bills-corpus-folder-model.md`), and the grounded answer, citations and confidence category are produced in `services/evidence.py` with `CHAT_MODEL`. Confidence is the L2 distance of the cited chunks: `high` < `RAG_HIGH`, `medium` < `RAG_MEDIUM`, `low` ≤ `RAG_LOW`; when nothing retrieved is within `RAG_LOW` the card says insufficient context and no model is called.
Switches and knobs: `MCP_ENABLED`, `RAG_ENABLED` (code default off; compose `${…:-true}`; CI `"false"`), `MCP_TIMEOUT_SECONDS` (15), `MCP_ALLOWED_TOOLS`, `RAG_TOP_K` (3), `RAG_HIGH`/`RAG_MEDIUM`/`RAG_LOW` (0.8/1.1/1.4), `GROUNDED_TIMEOUT_SECONDS` (20 s per model attempt), `OLLAMA_KEEP_ALIVE` (30m; how long Ollama keeps a model loaded after a call), `AI_TEMPERATURE` (0.2; chat and dispute drafts), `GROUNDED_TEMPERATURE` (0; the grounded answer, so the same question gets the same answer).
Codes: 503 `mcp_disabled`, `rag_disabled`, `mcp_connection`, `mcp_timeout`, `rag_unavailable` (the RAG server failing behind the MCP server); 502 `mcp_tool_error`, `mcp_invalid_result`; 400 `tool_not_allowed`. User copy never names a host, port or upstream text.
Routes: `GET /api/tools`, `POST /api/tools/<name>`, `POST /api/evidence`; fragments `GET /ui/tools`, `POST /ui/tools/search_transactions`, `GET /ui/evidence`, `POST /ui/evidence`.
Start order for a live run: `docker compose up -d`, then `python ai-services/rag-server/server.py`, then `python ai-services/mcp-server/server.py`. Sophia-CI runs with both switches `"false"` and asserts the refusal; it has no `rag` job and its path filter is `ai-services/rag-server/sources/billing/**`; `python sophia/rag/mcp_ragtest.py` is the terminal check of both tools.

## Inbound contracts

See `docs/release-0/sophia/contracts-inbound.md` (first written 22 Aug 2026,
revised through 7 Sep 2026) for the two inbound
handoff endpoints (`POST /api/handoff/recurring`, `POST /api/suggestions`)
and the transactions-service contract this feature assumes.

## Schema adoption

See `docs/release-0/sophia/schema-adoption.md` for the six additive schema items
(`bills.end_date`, `bills.source`, `bills.confirmed_at`, `bills.exclude_from_plan`,
the `chat_messages` table, and the `suggestions` table that holds each AI
proposal until the user decides on it) and why each exists.

## Testing and evidence

```
python -m pytest sophia/test -q --cov=sophia/backend --cov-report=term
```

283 passed; coverage 91% across `sophia/backend` (1698 statements, 155 missed;
measured 7 Sep 2026 on Python 3.13.2 at `aede425`). The same suite is green on
Python 3.12 in Sophia-CI run 145, and the saved output is
`docs/release-0/sophia/evidence/compose/pytest-283-passed.txt`.

Covers the engine (dates, projection, calendar, status, money), the database
API (temp SQLite per test, seed row counts, CRUD round-trips, cascade
delete), the backend routes (monkeypatched `bills_db` client, no network),
the inbound handoff routes and the transactions-service contract (stub
shape, live-path normalisation, non-finite amounts → 422), the services
layer's validation branches, the rule that GETs never write the cached
status column, the AI guard and schemas (mocked HTTP, retry-then-fallback,
direct-debit/card step injection, unreachable-Ollama fallback), the chat
proposal path (a proposal may only promise what can happen; suggestions wait
for approval and apply through the same services layer a manual edit uses),
the agentic loop's collectors, run record and endpoint fingerprint, and the
`/ui/*` HTML fragments and write routes (a real `sophia/database` instance in
a background thread against a temp seeded SQLite file, verbatim-copy and
`$`-formatting assertions).

`docs/release-0/sophia/evidence/ai/` holds raw JSON from real local Ollama calls (not
mocked), re-captured 7 Sep 2026 against the composed stack on the shipping
models — dispute drafts for a direct-debit bill and a card bill plus a
regenerate-with-feedback call (3/3 validated, no fallback), and all four
chat chips (4/4 correct op, with two recorded defects: the Spotify cancel
returns an `end_date` a month late in 5 of 6 repeats, and the GymCo dispute
reply says the draft is done when it is only a pending suggestion — both kept
as recorded),
with an honest note on what tuning changed and what this small sample does
and doesn't demonstrate.
`docs/release-0/sophia/evidence/compose/` holds the `docker compose`
verification runs (22 Aug and 7 Sep) and the curl transcripts of the `/ui/*`
write routes; `docs/release-0/sophia/evidence/ci-summary-2026-09-07.txt`
records the Sophia-CI run counts and the `gh api` commands that reproduce
them.
Diagrams are in `docs/architecture/` and app screenshots in
`docs/release-0/sophia/screenshots/`, each described in
`docs/release-0/sophia/README.md`. Per the spec's repository layout, the
report's evidence lives under `docs/`; this file and
`sophia/agentic_loop/README.md` are the only documentation beside the code.

## Pull requests

All 56 of this feature owner's PRs are merged to `main`, each squash-merged
by the author after a Sophia-CI green run; the ruleset requires no approving
review (verified 7 Sep 2026: every merge commit has one parent). Four
are shared work rather than Bills — the team agentic loop #78, #81, #82 and
the repo-structure chore #85. The other 52 are this feature:

- Scaffold #6, #7 · engine #8 · DB API #13 · backend #10 · frontend #11 ·
  AI #12
- Defect fixes: #14, #15, #16, #17, #18, #31, #73
- Docs, layout, polish: #19, #24, #25, #26, #27, #30, #33, #71, #75
- Requirements split: #34
- Release 0 hardening: #47, #49, #50, #51, #52, #56, #68, #69
- Shared-shell integration: #86, #89, #91, #93, #96, #119
- Ask Tally on `qwen2.5:3b`, chat add-bill, suggestions and the reject → adapt
  loop: #94, #100, #105, #106, #108, #120
- Dark theme, row menu, one-page layout, polish: #101, #103, #109, #117, #118
- Agentic loop, individual extension: #88
- Release 0 evidence refresh: #127, #128

Actions evidence — Sophia-CI run 145 on `main` (3 Sep 2026, 145 runs, 142
successful; the workflow's path filter is `sophia/**`, `shared/**` and the
workflow file, so docs-only merges since then have not triggered it):
<https://github.com/David-S-22/ASD-Spring-2026/actions/runs/33720083787>

## Workflow note

The team's workflow doc names this feature's CI file `student-5.yml`; it's
named `Sophia-CI.yml` here instead, to match the existing `David-CI.yml`
naming convention already in the repo.
