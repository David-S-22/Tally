# Multi-Agent Server (Release 2) — stub

The shared Planner → Worker → Reviewer → Human Review server, as specified in
[the contract ADR](../../docs/release-2/shared/adr-multi-agent-server-contract.md).
This is the **stub**: every route, both log files, the human decision and the
`plan_only` / `reuse_plan` options are real; the three agents return canned,
deterministic results with `"model": "stub"` and make no model calls.

It exists so the five feature backends, the agentic-loop Multi-Agent Review
mode and the UI can be built and tested now. Replacing the stub with the real
agents means editing the three function bodies in `agents.py` and nothing
else.

## Run

```bash
pip install -r ai-services/multi-agent-server/requirements.txt
python ai-services/multi-agent-server/server.py            # http://127.0.0.1:5010
python -m pytest ai-services/multi-agent-server -q         # 18 tests
```

Runs on the host, never in Docker Compose (brief: non-containerised).
Containers reach it at `http://host.docker.internal:5010`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MULTI_AGENT_PORT` | `5010` | Listen port (5004, the Lab 10 port, is Anomalies here) |
| `MULTI_AGENT_LOG_DIR` | `ai-services/multi-agent-server/logs` | Where the two `.jsonl` files go; gitignored |

## Try it

```bash
# HTTP
curl -s -X POST localhost:5010/workflow -H 'content-type: application/json' \
  -d '{"feature":"bills","task":"Draft a dispute letter for bill 12","evidence":[{"source":"mcp:compare_bill_with_bank_charges","summary":"Bill 12 matched by two $89.00 charges.","data":{"bill_id":12}}]}'
curl -s -X POST localhost:5010/workflow/<workflow_id>/decision -H 'content-type: application/json' \
  -d '{"decision":"Partially Accept","corrected_output":"Dear Optus, ...","decided_by":"sophia"}'
curl -s localhost:5010/workflow/status?feature=bills

# Terminal (same coordinator, same logs)
python ai-services/multi-agent-server/workflow_runner.py --feature bills --task "Draft a dispute letter for bill 12" --evidence evidence.json
python ai-services/multi-agent-server/workflow_runner.py --decide <workflow_id> --decision Accept
```

## Files and what each one is for

- `server.py` — the Flask app and the five HTTP routes (`/health`, `POST /workflow`, `POST /workflow/<id>/decision`, `GET /workflow/<id>`, `GET /workflow/status`). It only parses requests and turns `WorkflowError` into the right status code; no workflow logic lives here. `create_app(log_dir)` is a factory so tests can point it at a temp folder.
- `coordinator.py` — the workflow itself. Validates a request against the contract, runs the three agents in order (timing each one), holds the record until the human decides, works out `final_output` for Accept / Partially Accept / Reject, and is the only code that writes `workflow_history.jsonl` and `coordination_audit.jsonl`. Reloads records from the history file on start so a restart keeps pending decisions.
- `agents.py` — the Planner, Worker and Reviewer as three plain functions with the signatures the real implementation keeps. In the stub they build their results from the task and evidence without a model. This is the file Ethan replaces the bodies of.
- `workflow_runner.py` — the terminal entry point the brief asks for ("callable from the terminal"). Runs a workflow or records a decision by calling the coordinator directly, so terminal and HTTP runs produce identical logs. Handy for capturing report samples.
- `test_server.py` — contract tests through the Flask test client: every route, every error code in the ADR, the audit event sequence, the three decisions, `plan_only`/`reuse_plan`, status filtering and restart recovery. Each test gets its own temp log folder.
- `requirements.txt` — Flask only.
- `logs/` — where the two JSON Lines files are written at run time. Only `.gitkeep` is committed; sample runs for the report go under `docs/release-2/<name>/multi-agent/` instead.

## What the real implementation adds

1. In `agents.py`: Ollama calls (planner and worker on `qwen2.5:0.5b`, reviewer on `llama3.1:8b`) using prompts from `prompts/multi_agent/`, `AGENT_STEP_TIMEOUT_SECONDS`, and `"status": "timeout"` results. The coordinator already records an `error` result and continues to human review if an agent raises.
2. In `server.py` `/health`: a real Ollama probe instead of `"stub"`.
3. Optional, per the ADR's open points: `options.use_mcp`.
