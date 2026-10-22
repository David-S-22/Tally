# AI mode switch (`AI_MODE_ENABLED`) — all five backends

The assessed cloud deployment must have AI mode, MCP, RAG and the Multi-Agent
Server **disabled by default**; enabling them in the cloud is optional and
scored only under Bonus Marks. MCP, RAG and Multi-Agent already had real
switches. AI mode did not: every backend called Ollama unconditionally, and
the only thing stopping it in the cloud was that nothing was listening at
`OLLAMA_URL`.

That is an outage, not a disabled feature. It presents as a timeout or a 500,
which reads as a broken deployment rather than a compliant baseline, and it
cannot be pointed at in the report as evidence that AI is off.
`AI_MODE_ENABLED` makes the refusal explicit and uniform.

## The contract

| | |
|---|---|
| Variable | `AI_MODE_ENABLED` |
| Code default | **off** in all five backends |
| Compose | `${AI_MODE_ENABLED:-true}` — local development is the AI topology |
| Bicep | `enableAi`, default `false`, wired to all five backends |
| Behaviour when off | the call refuses with a safe `ai_disabled` code (HTTP 503); no socket is opened |

With the switch off nothing reaches Ollama — not the chat models, not the
grounded-answer models, not the warm-up calls, and not the `/health` probes.

## Where each backend gates

| Backend | Choke point | Raises |
|---|---|---|
| `sophia` (bills) | `ai/guard.py:run`, `ai/ollama_client.py:chat` | `ModeError("ai_disabled")` → 503 |
| `janelle` (transactions) | `services/ollama_service.py:create_plan`, `services/rag_answer.py:ask_model` | `ChatError` / `RAGError` → 503 |
| `aiden` (anomalies) | `services/ollama_api.py:prompt` | `AIError("ai_disabled")` |
| `ethan` (budgets) | `ai/guard.py:run`, `ai/ollama_client.py:chat` | `ServiceError(503, "ai_disabled")` |
| `david` (savings) | `savings_service/ollama_service.py:_client` | `AIError("ai_disabled")` |

Two details worth knowing before moving a check:

- **`guard.run` refuses before its first attempt, not inside the loop.** Both
  sophia's and ethan's guards catch bare `Exception` and fall back to
  templated text. A gate placed lower would be swallowed and the backend
  would serve canned text as though the model had answered. The point of the
  switch is that a disabled deployment *refuses*.
- **`aiden` treats an absent variable as off.** Its config normally raises
  when a variable is unset, but `AI_MODE_ENABLED` deliberately defaults to
  `False` instead and is not listed in `check_all()`, so a deployment that
  configures nothing refuses rather than dialling whatever `OLLAMA_URL`
  happens to point at.

## Health endpoints

`sophia`, `janelle` and `ethan` report the switch. Before this change
sophia's `"ai"` could only say `enabled` or `unavailable`, and janelle's was
hardcoded to `"enabled"` — neither could ever report a disabled baseline.
With AI mode off, sophia and ethan report `"ollama": "off"` and skip the
probe entirely.

## Deployment

`./deploy.sh` defaults to `containers`: the required baseline, no Ollama VM,
all four switches off. `./deploy.sh all` provisions the Ollama VM for the
optional AI topology, and AI mode **still** needs `enableAi=true` passed
explicitly — deploying the VM does not by itself turn AI on. That separation
is the point: the VM is infrastructure, the switch is the feature.

`bicep/main.bicepparam` states `enableAi`, `enableMcp` and `enableRag`
explicitly so the baseline is readable in one file. Multi-Agent has no Bicep
parameter; its backends default `MULTI_AGENT_ENABLED` off in code.

`cloud-deployment.yml` passes `containers` or `update` explicitly, so the
pipeline never deployed the VM; the old `all` default only applied to a
hand-run `./deploy.sh`.

## Tests

Each backend has a `test_ai_mode_switch.py` asserting the refusal and that no
request is attempted. The suites drive the AI paths behind fakes, so each
`conftest.py` has an autouse `ai_mode_on` fixture that turns the switch on;
the tests that assert the refusal turn it back off.

Run them per module — the five suites share a top-level `backend` package
name and collide when collected together:

```
python -m pytest sophia -q
python -m pytest janelle -q
python -m pytest aiden -q
python -m pytest ethan -q
python -m pytest david -q
```

Bills documents its own slice in
[`sophia/README.md`](../../../sophia/README.md) under "Cloud baseline";
`ethan` and `david` have no README, so this document is their reference.
