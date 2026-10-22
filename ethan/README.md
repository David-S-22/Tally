# Budget Coach

Owner: Ethan. Budgets feature — `budgets-frontend` (:3006),
`budgets-backend` (:5006, Flask) and `budgets-db` (:6006).

This file currently covers configuration and the AI mode switch only. The
design and release material lives in the plan documents alongside it:
[Release 0](BudgetCoach-Release0-Plan.md),
[Release 1](BudgetCoach-Release1-Implementation-Plan.md),
[AI chat](BudgetCoach-AI-Chat-Implementation-Plan.md),
[database design](BudgetCoach-Database-Design.md),
[Release 2 multi-agent](Release2-Multi-Agent-Server-Implementation-Plan.md).

## AI mode, MCP and RAG switches

| Variable | Code default | Compose | Purpose |
|---|---|---|---|
| `AI_MODE_ENABLED` | `false` | `${AI_MODE_ENABLED:-true}` | Gates every Ollama call. |
| `MCP_ENABLED` | `false` | `${MCP_ENABLED:-true}` | Shared MCP server at `http://host.docker.internal:8000/mcp`. |
| `RAG_ENABLED` | `false` | `${RAG_ENABLED:-true}` | Shared RAG server at `http://host.docker.internal:5003`, `RAG_FEATURE=budgets`. |

All three default **off** in `backend/config.py`, so the required cloud
baseline refuses without any parameter being set. Compose turns them on, so
local development is unchanged.

With `AI_MODE_ENABLED=false`, `ai/guard.py:run` raises
`ServiceError(503, "ai_disabled")` **before its first attempt**, and
`ai/ollama_client.py:chat` raises the same if called directly. The placement
matters: `guard.run` catches bare `Exception` and falls back to templated
text, so a check inside the loop would be swallowed and the backend would
serve canned text as though the model had replied. `/health` reports
`ai_mode` and returns `"ollama": "off"` without probing.

In Bicep the switch is `enableAi` (default `false`), wired alongside
`enableMcp` and `enableRag`. Full cross-backend detail, including why the
switch exists at all:
[AI mode switch](../docs/release-2/shared/ai-mode-switch.md).

## Tests

```
python -m pytest ethan -q
```

Run per module — the five backends share a top-level `backend` package name
and collide when collected together. `test/test_ai_mode_switch.py` asserts
the refusal; `test/conftest.py` has an autouse `ai_mode_on` fixture because
the rest of the suite drives the AI paths behind fakes.
