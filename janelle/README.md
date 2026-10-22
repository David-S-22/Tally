# Transactions service

Three-service transaction manager: HTMX frontend, Flask backend, SQLite
database API. Supports transaction and category CRUD, filtering and
pagination, category-correction history, and "Ask Tally", an Ollama-backed
assistant for transaction questions and guarded changes.

## Project structure

| Path | Purpose |
|---|---|
| `frontend/` | nginx serving `public/index.html`; proxies `/transactions-backend/*` to the backend. |
| `backend/` | Flask API, Jinja/HTMX fragments, AI workflow, anomaly notification after a create. |
| `backend/services/` | Planner, trusted transaction operations, MCP and RAG clients, grounded category suggestion, request orchestration, Plan -> Act -> Observe -> Adapt runner. |
| `database/` | Flask-SQLAlchemy API over SQLite: models, validation, filtering, seed data, corrections. |
| `scripts/test/` | Endpoint, live AI workflow, and NFR probes. |
| `test/` | pytest suite (no live services needed). |

## Run

From the repository root:

```powershell
docker compose up --build -d ollama transactions-db transactions-backend transactions-frontend
```

| Service | URL |
|---|---|
| Frontend | `http://localhost:3001` |
| Backend | `http://localhost:5001` |
| Database API | `http://localhost:6001` |
| Ollama | `http://localhost:11434` |

`GET /health` on the backend reports the integration switches:
`{"modes": {"ai": "enabled", "mcp": "enabled", "rag": "enabled"}}`. Turn MCP
or RAG off with `MCP_ENABLED=false` / `RAG_ENABLED=false` in the environment
before `docker compose up`. The backend reaches the shared MCP server
(port `8000`) and RAG server (port `5003`) through `host.docker.internal`.

## API

Transaction and category CRUD at `/transactions` and `/categories` (items
under `/<id>`). Transaction lists accept `search_text`, `merchant`,
`date_from`, `date_to`, `since`, `category_id`, `min_amount`, `max_amount`.
The database API also exposes `POST /transactions/<id>/category-correction`
and `GET /category-corrections`.

HTMX fragments under `/ui/` back the frontend. Each transaction row has a
Delete button (`DELETE /ui/transactions/<id>`) that asks for confirmation
and re-renders the table on the same page with the active filters. The
"Manage categories" screen (`GET /ui/categories`) lists categories with
their transaction counts; Delete (`DELETE /ui/categories/<id>`) is disabled
while a category still has transactions, and the database also refuses to
delete the protected `Uncategorised` category or one referenced by a
correction. Errors are shown inline above the table.

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Plan a read, create, update, or delete request. |
| `POST /chat/category` | Accept or replace a proposed category. |
| `POST /chat/apply` | Apply a server-issued write preview after confirmation. |
| `GET /mcp/tools` | List MCP tools visible to the backend. `503 mcp_disabled`, `502 mcp_unavailable`. |
| `POST /rag/refresh` | Rebuild the `transactions-records` RAG collection; returns `total`, `kinds`, `duration_ms`. `503 rag_disabled`, `502 rag_unavailable`. |

## AI workflow

The planner (`backend/prompts/chat_prompt.txt`, `CHAT_MODEL`) produces one
transaction operation plus optional calculations. Trusted code validates and
grounds the plan before touching data. At most two Plan -> Act -> Observe ->
Adapt iterations. Reads are computed from rows in code. Writes never happen
during planning: the user gets a before/after preview, confirms, and the
confirmed write is matched to server-held request state, version-checked for
updates and deletes, and verified against the database afterwards.

Every response carries `agent` with the request ID, model, status, MCP tool
calls, and a safe stage trace (`AGENT_TRACE_ENABLED`).

### MCP read path

With `MCP_ENABLED=true`, read plans call `search_transactions` on the shared
MCP server (`backend/services/transaction_source.py`) instead of the database.
Plan filters map to tool arguments (`date_from`/`since` -> `start_date`,
`date_to` -> `end_date`, `category_id` -> `category_name`, others by name).
Rows pass the same validation as database rows; results over 500 rows are
truncated. If the server is unreachable, `MCP_FALLBACK_TO_DATABASE=true`
serves the read from the database and records the fallback. Point lookups and
writes always use the database API. `agent.tools` in the response lists each
call with `status` (`succeeded`, `fallback_database`, `failed`,
`skipped_disabled`).

### RAG-grounded category suggestion

When a create has no category in the message and `RAG_ENABLED=true`, the
backend grounds its suggestion before the Release 0 correction vote
(`backend/services/category_grounding.py`, `rag_answer.py`):

1. Retrieve from two collections on the shared RAG server: `transactions-records`
   (backend-owned, one document per transaction `tx-<id>` and per correction
   `corr-<id>`, built by `rag_corpus.py`) and `transactions` (server-owned,
   from `ai-services/rag-server/sources/transactions/categories.md`).
2. Merge by distance, drop anything above `RAG_LOW`. Nothing left means
   `insufficient`; no model call.
3. Ask Ollama (`RAG_MODEL`, temperature 0, `logprobs`) to pick exactly one
   live category name citing `[n]` context lines. Markers outside the context
   or disagreeing with the chosen category's `category_id` are dropped.
4. Confidence: distance band (`high` below `RAG_HIGH` with two or more
   survivors, `medium` below `RAG_MEDIUM`, else `low`), then gated by the
   answer's token probability (`RAG_PROB_HIGH`, `RAG_PROB_MEDIUM`), then one
   step down if uncited or derived from a majority vote over the surviving
   records. No records to vote over means `insufficient`.

`category_selection.grounding` in `POST /chat` and `POST /chat/category`
carries `status` (`grounded`, `insufficient`, `unavailable`), `confidence`,
`citations` (marker, id, collection, source, excerpt, distance, category
metadata), `uncited`, `derived_from_votes`, `model_probability`, `model`,
`thresholds`. A `grounded` result always has at least one citation. Any
`RAGError` degrades to `unavailable` and the Release 0 vote runs. With RAG off
the key is omitted and the Release 0 payload is unchanged.

The Ask Tally card shows a confidence chip and a "Based on:" list of sources
(record rows as `date · merchant · amount · category`, guide rows quoted).
"Accept suggestion" appears only for `high` or `medium` confidence or a
Release 0 correction-vote suggestion; `low` pre-selects the dropdown.

The records collection is refreshed in the background at startup
(`RAG_REFRESH_ON_START`, retried up to ten times), after a confirmed create
(`RAG_REFRESH_AFTER_WRITE`), and on demand via `POST /rag/refresh`. Background
failures are logged and never affect the write.

### Logging

With `AGENT_LOG_ENABLED=true` the backend emits structured JSON records
prefixed `AI_WORKFLOW`: `ai_workflow_stage` and `ai_workflow_complete` per
cycle, `MCP_TOOL` per tool call, `MCP_TOOLS_LISTED` per `GET /mcp/tools`,
`RAG_GROUNDING` per suggestion attempt, and `RAG_REFRESH` per records
refresh. Records carry IDs, statuses, counts, distances, durations, and safe
error codes only. No user text, prompts, excerpts, or model output.

## Data model

- `categories`: case-insensitive unique names, optional `need`/`want`/`saving` type.
- `transactions`: date, merchant, description, amount, category, timestamps.
- `category_corrections`: previous and user-selected category per transaction.

An empty database is seeded with demo data on first start only.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `5001` | Backend port. |
| `TRANSACTIONS_DB_URL` | `http://transactions-db:6001` | Database service. |
| `DATABASE_TIMEOUT_SECONDS` | `20` | Database HTTP timeout. |
| `ANOMALIES_BACKEND_URL` | `http://anomalies-backend:5004` | Anomaly review service. |
| `ANOMALIES_TIMEOUT_SECONDS` | `10` | Anomaly notification timeout. |
| `OLLAMA_URL` | `http://ollama:11434` | Ollama base URL. |
| `CHAT_MODEL` | `qwen2.5:3b` | Planner model. |
| `AI_TIMEOUT_SECONDS` | `90` | Ollama request timeout. |
| `AGENT_MAX_ITERATIONS` | `2` | Iteration limit (1 or 2). |
| `AGENT_TRACE_ENABLED` | `true` | Include stage trace in responses. |
| `AGENT_LOG_ENABLED` | `true` | Emit structured logs. |
| `AGENT_REQUEST_TTL_SECONDS` | `900` | Lifetime of pending category and preview state. |
| `MCP_ENABLED` | `true` | MCP switch. CI sets `false`. |
| `MCP_SERVER_URL` | `http://host.docker.internal:8000/mcp` | Shared MCP server. |
| `MCP_TIMEOUT_SECONDS` | `30` | Per tool call timeout. |
| `MCP_ALLOWED_TOOLS` | `search_transactions` | Tool allow list. |
| `MCP_FALLBACK_TO_DATABASE` | `true` | Serve reads from the database when MCP is unreachable. |
| `MCP_TOOL_SUPPORTS_EXTENDED_FILTERS` | `true` | Send merchant, text, and amount filters to the tool. |
| `RAG_ENABLED` | `true` | RAG switch. CI sets `false`. |
| `RAG_SERVER_URL` | `http://host.docker.internal:5003` | Shared RAG server. |
| `RAG_RECORDS_COLLECTION` | `transactions-records` | Backend-owned records collection. |
| `RAG_GUIDE_COLLECTION` | `transactions` | Server-owned guide collection. |
| `RAG_TOP_K` / `RAG_GUIDE_TOP_K` | `6` / `3` | Results retrieved per collection. |
| `RAG_TIMEOUT_SECONDS` | `15` | Per `/retrieve` and `/refresh` timeout. |
| `RAG_REFRESH_ON_START` | `true` | Refresh records at startup. |
| `RAG_REFRESH_AFTER_WRITE` | `true` | Refresh records after a confirmed create. |
| `RAG_HIGH` / `RAG_MEDIUM` / `RAG_LOW` | `0.6` / `0.9` / `1.2` | Distance bands; above `RAG_LOW` is not used as context. Must satisfy `0 < high < medium < low`. |
| `RAG_MODEL` | `qwen2.5:3b` | Grounded category model. |
| `RAG_LOGPROBS` | `true` | Request token log probabilities and gate confidence on them. |
| `RAG_PROB_HIGH` / `RAG_PROB_MEDIUM` | `0.8` / `0.5` | Minimum answer probability to keep `high` / `medium`. Must satisfy `0 < medium < high <= 1`. |

Invalid threshold combinations log one warning and fall back to defaults.
The database reads `PORT` and `DB_PATH` (Compose: `6001`,
`/app/data/transactions.db`), plus `ANOMALIES_DB_URL` /
`ANOMALIES_TIMEOUT_SECONDS` for best-effort anomaly cleanup when a
transaction is deleted and `SAVINGS_DB_URL` / `SAVINGS_TIMEOUT_SECONDS` for
best-effort savings-feedback cleanup when a category is deleted. The frontend
reads `PORT` (Compose: `3001`).

## Tests and probes

```powershell
python -m pip install -r janelle\backend\requirements.txt -r janelle\database\requirements.txt -r janelle\test\requirements.txt
python -m pytest janelle\test -q
```

With the services running:

```powershell
python janelle\scripts\test\janelle_endpoint_smoke.py      # health, proxy, CRUD, validation
python janelle\scripts\test\janelle_ai_workflow_probe.py   # plan does not write; confirm writes once
python janelle\scripts\test\janelle_nfr_probe.py           # read latency baselines
```

Each probe accepts `--output <path>`. Release 0 evidence lives in
[`docs/release-0/janelle/README.md`](../docs/release-0/janelle/README.md).
