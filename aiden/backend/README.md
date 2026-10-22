# Anomalies Backend

## Overview

The backend is a Flask application that provides the anomaly-detection API. It
coordinates transaction retrieval, asynchronous reviews, model requests, and
anomaly persistence. The application starts a daemon review worker when it is
initialised and returns JSON for API responses or rendered Jinja fragments for
the frontend.

Transactions are submitted for review as `dto.Transaction` objects. The backend
does not store transaction records itself; it retrieves transactions from the
transactions database and sends detected anomalies to the anomalies database.
Configuration such as service URLs, the model name, and polling timeouts is
read from environment variables in `config.py`. Values are resolved lazily, so
reading one raises a `RuntimeError` if the variable is unset or invalid;
`config.check_all()` validates every variable up front (it runs on startup).
The Compose deployment sets `MCP_SERVER_URL` to the host machine's MCP server
at `http://host.docker.internal:8000/mcp`; the MCP server is not containerised.
It also sets `RAG_SERVER_URL` to the shared, retrieve-only RAG server at
`http://host.docker.internal:5003` and `RAG_FEATURE` to `anomalies`; RAG can be
disabled with `RAG_ENABLED=false`. The RAG server is likewise not containerised.

## Agentic workflow

1. The frontend sends a transaction to `POST /check-transaction`.
2. The backend validates and deserialises the request into a transaction DTO.
3. The transaction is placed on the in-process review queue, and the endpoint
   immediately returns `HTTP 202 (Accepted)` with the transaction ID.
4. A background worker passes the transaction to the agent.
5. The agent calls the MCP server's
   `get_transactions_with_confirmed_anomalies` and
   `get_transactions_with_rejected_anomalies` tools through the `fastmcp` client
   to retrieve reviewed examples, and retrieves fraud/anomaly reference guidance
   for the transaction from the shared RAG server (`services/rag_client.py`).
   Both are embedded into the prompt before classifying the transaction.
6. The model must return JSON containing `is_suspicious` and `justification`.
   Invalid responses are retried with increasing temperature, up to four
   attempts.
7. If the transaction is suspicious, the resulting anomaly is written to the
   anomalies database, including the filenames of the RAG documents used as
   `sources`. Otherwise, no anomaly is created.
8. The worker marks the transaction as complete. The frontend can poll
   `GET /anomaly-alert?key=<transaction-id>` for a rendered alert fragment.

The agent is instructed to be cautious, avoid claiming fraud as fact, use only
the supplied transaction information, and incorporate the user's previous
confirmation or dismissal decisions.

## Plan–Act–Observe–Adapt workflow

The anomaly review implements a **Plan → Act → Observe → Adapt** loop whose
cycle spans *across* reviews: the user's decisions on one finding shape how the
agent judges the next transaction.

- **Plan** — Before classifying the transaction, `agent_api` calls the shared
  MCP server's `get_transactions_with_confirmed_anomalies` and
  `get_transactions_with_rejected_anomalies` tools through the `fastmcp` client
  (`services/mcp_client.py`), the same way the other services talk to the MCP
  server. These return reviewed transactions and their anomaly records as
  positive and negative examples, which are formatted and embedded into the
  user prompt. The agent also retrieves fraud/anomaly reference guidance for the
  transaction from the shared retrieve-only RAG server
  (`services/rag_client.py`), and embeds the closest excerpts as a "Reference
  guidance" section. The plan for judging the current transaction is therefore
  shaped by the accumulated human feedback and the reference material. If the
  MCP or RAG server is unavailable the review still proceeds without that input.
- **Act** — The agent sends the system and user prompts to the model through
  `ollama_api.prompt`, requesting a JSON finding with `is_suspicious` and
  `justification`. A valid, suspicious finding is converted to an anomaly DTO —
  along with the model's mean confidence score derived from token log
  probabilities and the filenames of the RAG documents used as `sources` — and
  persisted so it can be shown to the user. (Malformed responses are simply
  retried with a higher temperature, up to four attempts — a robustness detail,
  not part of the adaptation loop.)
- **Observe** — The persisted finding is surfaced to the user, who reviews it
  and records the ground truth by **confirming** it as genuinely suspicious
  (true positive) or **dismissing** it as a false positive. This human review
  is the observation of how well the agent's judgement matched reality.
- **Adapt** — Those confirmed/denied decisions are exactly the reviewed findings
  returned by the MCP tools on the *next* review. The agent treats transactions
  similar to confirmed findings as more likely suspicious, and avoids
  re-flagging transactions similar to denied ones. The loop then returns to Plan
  for the next transaction, with the agent's behaviour adapted to the user's
  accumulated feedback.

```mermaid
flowchart TD
    A[Transaction enqueued for review] --> B

    subgraph Plan
        B[Call MCP reviewed-example tools<br/>via fastmcp client] --> C[Retrieve confirmed/rejected examples]
        C --> RAG[Retrieve RAG reference guidance<br/>via rag_client]
        RAG --> D[Construct constrained prompt<br/>transaction fields + examples + guidance]
    end

    subgraph Act
        D --> E[Prompt Ollama for JSON finding]
        E --> F{Suspicious?}
        F -- No --> G[No anomaly created]
        F -- Yes --> H[Create + persist anomaly DTO]
    end

    subgraph Observe
        H --> I[User reviews the finding]
        I --> J{Confirm or dismiss?}
        J -- Confirm --> K[Marked CONFIRMED<br/>true positive]
        J -- Dismiss --> L[Marked DENIED<br/>false positive]
    end

    subgraph Adapt
        K --> M[Reviewed findings available<br/>through MCP tools]
        L --> M
    end

    M -. MCP results for next review .-> B
```

## Services

### `services/agent_api.py`

Implements the anomaly-detection agent. It fetches reviewed examples from the
MCP server through `services/mcp_client.py`, retrieves fraud/anomaly reference
guidance from the RAG server through `services/rag_client.py`, formats both into
the prompt, parses model JSON, validates the expected response fields, captures
the model's mean confidence (from token log probabilities), and converts
suspicious findings into anomaly DTOs (recording the RAG source filenames used).

### `services/rag_client.py`

Small HTTP client for the shared, retrieve-only RAG server. It exposes
`retrieve` and `health`, applies `RAG_TIMEOUT_SECONDS`, validates the response
shape, and maps every failure to a safe `RAGError` code so a RAG outage degrades
gracefully and never blocks anomaly detection. Retrieval is skipped when
`RAG_ENABLED` is false.

### `services/mcp_client.py`

Thin `fastmcp` client for the shared MCP server, mirroring the other services'
clients. It exposes `list_tools` and `call_tool`, runs the async client from
synchronous code, applies `MCP_TIMEOUT_SECONDS`, and maps every failure to a
safe `MCPError` code so server details never leak.

### `ai-services/mcp-server/server.py`

Provides transaction search plus
`get_transactions_with_confirmed_anomalies` and
`get_transactions_with_rejected_anomalies`. The reviewed-example tools join
anomaly records from the anomalies database to their corresponding
transactions.

### `services/review_queue.py`

Provides the in-process queue and daemon worker. It tracks pending and
completed transaction IDs, runs reviews outside the request thread, persists
findings, and allows callers to wait for a specific transaction's result.

### `services/anomalies_api.py`

HTTP client for the anomalies database. It retrieves all anomalies, looks up an
anomaly by transaction ID, creates anomalies, and updates user confirmation
status.

### `services/transaction_api.py`

HTTP client for the transactions database. It retrieves transaction records
used for anomaly listings and agent context.

### `services/ollama_api.py`

OpenAI-compatible client wrapper for the model server. It caches one client
instance and sends system and user prompts through Ollama's Chat Completions API
with the configured model, temperature, token limit, and timeout. It requests
token log probabilities and returns a `PromptResult` with the response `text`
and the `mean_confidence` (the arithmetic mean of each chosen token's
probability, `exp(logprob)`), or `None` when the server does not return log
probabilities. Set `OLLAMA_LOG_LEVEL=DEBUG` to log the response's
log-probability field locations, their shape, and the number of token scores
extracted when investigating missing confidence values, for example with
`docker compose logs -f anomalies-backend`.

### `helpers.py`

Contains request DTO deserialisation, DTO serialisation, and environment
variable helpers used by the application and integration clients.

## Routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/` | Returns the backend health response. |
| `GET` | `/fact` | Requests and returns a random fact from the configured model. |
| `GET` | `/anomalies` | Retrieves anomalies and transactions, then renders the anomaly list. |
| `POST` | `/check-transaction` | Validates and queues a transaction for asynchronous review; returns `202`. |
| `GET` | `/anomaly-alert?key=<id>` | Waits for a queued review and returns an alert fragment when an anomaly exists, otherwise `204`. |
| `POST` | `/anomalies/<id>/confirm` | Marks an anomaly as confirmed and returns the refreshed anomaly list. |
| `POST` | `/anomalies/<id>/dismiss` | Marks an anomaly as dismissed and returns the refreshed anomaly list. |
| `POST` | `/dummy-anomaly` | Creates a random test anomaly and returns the refreshed anomaly list. |
