# Savings

Owner: David. Savings feature — `savings-frontend` (:3002),
`savings-backend` (:5002, Flask) and `savings-db` (:6002).

This file currently covers configuration and the AI mode switch only.

## AI mode and the shared services

| Variable | Code default | Compose | Purpose |
|---|---|---|---|
| `AI_MODE_ENABLED` | `false` | `${AI_MODE_ENABLED:-true}` | Gates every Ollama call. |
| `MCP_ENABLED` | `true` | `${MCP_ENABLED:-true}` | Shared MCP server at `http://host.docker.internal:8000/mcp`. |
| `RAG_ENABLED` | `true` | `${RAG_ENABLED:-true}` | Shared RAG server. |
| `MULTI_AGENT_URL` | `http://host.docker.internal:5010` | from repo-root `.env` | Shared Multi-Agent Server. |
| `OLLAMA_MODEL` / `OLLAMA_CLASSIFIER_MODEL` | `llama3.1:8b` / `qwen2.5:3b` | same | Planner and classifier models. |

`AI_MODE_ENABLED` defaults **off** in
`backend/savings_service/ollama_service.py`, so the required cloud baseline
refuses model calls without any parameter being set. Compose turns it on, so
local development is unchanged.

Every model call goes through one choke point, `_client()`, which raises
`AIError("ai_disabled")` (status 503) when the switch is off — so none of the
five `chat.completions.create` call sites can reach the network. In Bicep the
switch is `enableAi` (default `false`).

Note that `MCP_ENABLED` and `RAG_ENABLED` default to **true** in code here,
unlike `AI_MODE_ENABLED`. The cloud baseline is still correct because Bicep
sets them `false` explicitly, but the safety depends on that parameter rather
than on the code default. Worth aligning if the three are meant to behave the
same way.

Full cross-backend detail:
[AI mode switch](../docs/release-2/shared/ai-mode-switch.md).

## Tests

```
python -m pytest david -q
```

Run per module — the five backends share a top-level `backend` package name
and collide when collected together. `test/test_ai_mode_switch.py` asserts
the refusal; `test/conftest.py` has an autouse `ai_mode_on` fixture because
the rest of the suite drives the AI paths behind a stubbed client.
