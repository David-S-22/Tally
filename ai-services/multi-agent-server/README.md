# Shared Multi-Agent Server - Release 2

A feature-neutral, host-run HTTP service coordinating real Ollama-backed
Planner -> Worker -> Reviewer -> Human Review. It never reads feature databases,
calls MCP/RAG, executes proposals, or applies feature changes. Feature owners
integrate later through the HTTP contract.

See [the contract](../../docs/release-2/shared/multi-agent-server-contract.md)
and [the implementation plan](../../ethan/Release2-Multi-Agent-Server-Implementation-Plan.md).

## Local setup (PowerShell, repository root)

Requires Python 3.12+ and a running local Ollama runtime. No server container or
cloud deployment is added. The Release 2 brief requires AI services outside
Compose; any existing repository Ollama-in-Compose setup is a separate group
deployment issue, not changed by this shared-server work.

```powershell
py -3 -m venv ai-services\multi-agent-server\.venv
& .\ai-services\multi-agent-server\.venv\Scripts\python.exe -m pip install -r ai-services\multi-agent-server\requirements.txt
ollama pull qwen2.5:3b
ollama pull llama3.1:8b
& .\ai-services\multi-agent-server\.venv\Scripts\python.exe ai-services\multi-agent-server\server.py
```

Ollama must be running before workflows can complete. The server can start
without it, and saved workflows/decisions remain available during model outages.
Requests use the official Ollama Python SDK and native chat/list APIs.
An asynchronous SDK call runs within each synchronous worker request so its
whole network operation can be cancelled at the remaining deadline. A bounded
response stream retains the 256 KiB limit; schemas, source checks and one
repair attempt remain server responsibilities.
Run one server process per data directory using the provided entry point.
Waitress serves HTTP on Windows and Linux; do not use a Flask debug reloader or
multiple WSGI workers. Startup converts interrupted runs into failed,
reject-only records rather than silently rerunning model work.

The default is `http://127.0.0.1:5010`. In another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:5010/health
```

`alive`/`ok` indicate the API is running; `ready` indicates Ollama and all
configured model tags are available. `export_mode: "on_demand"` means JSONL
evidence is generated only when requested, not rewritten during state changes.

## Configure container-to-host access

Feature backends will use `http://host.docker.internal:5010` on Docker Desktop.
For a reachable host interface, explicitly configure binding and a token:

```powershell
$env:MULTI_AGENT_HOST = '0.0.0.0'
$env:MULTI_AGENT_TOKEN = '<at-least-32-random-non-whitespace-characters>'
```

Replace the placeholder with a securely generated secret. Supply the same token
to approved callers via environment configuration, never browser code or Git.
All endpoints require `Authorization: Bearer <token>` when a token is configured.
Restrict Windows Firewall to the required local Docker traffic; do not expose
port 5010 publicly. Plain HTTP bearer tokens are unsuitable on an untrusted
network. A shared token does not isolate features/users; `feature` and
`decided_by` are caller-supplied audit metadata, not verified identity.
Browser CORS is intentionally not enabled.

## Terminal workflow (calls the running HTTP server)

```powershell
py -3 ai-services\multi-agent-server\workflow_runner.py --feature transactions --task 'Explain the supplied synthetic expense without changing any records.' --evidence ai-services\multi-agent-server\example-evidence.json
py -3 ai-services\multi-agent-server\workflow_runner.py --get <workflow-id>
py -3 ai-services\multi-agent-server\workflow_runner.py --decide <workflow-id> --decision Accept --reason 'Verified the evidence and reviewed the recommendation'
py -3 ai-services\multi-agent-server\workflow_runner.py --export <workflow-id>
py -3 ai-services\multi-agent-server\workflow_runner.py --export <workflow-id> --output-dir .\test-evidence\run-1
```

Use the virtual environment's Python if the system Python has no dependencies.
The same generic request works with `transactions`, `savings`, `anomalies`,
`bills`, and `budgets`. This demonstrates reuse, not individual feature integration.
`--url` or `MULTI_AGENT_SERVER_URL` sets the server origin. The CLI reads
`MULTI_AGENT_TOKEN` without exposing it in process arguments. Its default HTTP
timeout is 550 seconds; adjust `--timeout` if the server deadline changes.
Use `--decision 'Partially Accept' --corrected-output 'Edited text' --reason
'Explanation'`; for structured JSON use `--corrected-output '@edited.json'`.
`--context context.json`, `--plan-only`, and `--reuse-plan <id>` are also supported.
Each command exits nonzero for HTTP/input/connection failures.
Without `--output-dir`, export prints the JSON response. With it, the CLI creates
a new directory containing `workflow_history.jsonl` and `coordination_audit.jsonl`.
It refuses an existing directory to avoid overwriting evidence.

## Configuration

| Variable | Default |
| --- | --- |
| `MULTI_AGENT_HOST` | `127.0.0.1` |
| `MULTI_AGENT_PORT` | `5010` |
| `MULTI_AGENT_TOKEN` | Empty on loopback; minimum 32 characters required otherwise |
| `OLLAMA_URL` | `http://localhost:11434` (native API origin, not `/v1`) |
| `MULTI_AGENT_PLANNER_MODEL` | `qwen2.5:3b` |
| `MULTI_AGENT_WORKER_MODEL` | `qwen2.5:3b` |
| `MULTI_AGENT_REVIEWER_MODEL` | `llama3.1:8b` |
| `AGENT_STEP_TIMEOUT_SECONDS` | `180`, including one possible output repair |
| `MULTI_AGENT_WORKFLOW_TIMEOUT_SECONDS` | `540` |
| `MULTI_AGENT_LOG_DIR` | `ai-services/multi-agent-server/logs` |

The HTTP timeout in future feature clients must exceed the workflow deadline
plus overhead (550 seconds initially). Feature owners must implement token
forwarding and the documented decision requirements in their own adapters.
The earlier Bills-specific stub integration has been removed from this branch;
no individual feature integration is included.

## State, audit and evidence

`workflows.db` is the authoritative transactional store. Workflow snapshots and
ordered events commit together. `workflow_history.jsonl` and
`coordination_audit.jsonl` are explicit per-workflow exports; history readers take
the last snapshot per ID. Saves never rewrite full-history files. Indexed export
queries read the requested workflow's snapshots/events from SQLite.
Runtime data stays gitignored. Export a single run via
`GET /workflow/<id>/export` or the terminal command for the report and separate
agentic-loop Multi-Agent Review workstream.

Supply only necessary, pre-redacted evidence. Full bounded workflow evidence
and results are persisted; there is no automatic PII/secret detection or
encryption at rest. Protect the directory with OS permissions and disk
encryption where appropriate. Operational logs record IDs/outcomes, not raw
evidence or model responses. Keep credentials out of all requests.

Legacy stub JSONL is not silently imported: archive it and choose a fresh
`MULTI_AGENT_LOG_DIR` before first startup. Export and retain required report
evidence before cleanup. To clear test data, stop the server, archive the
directory, and explicitly remove only its named database and export files.
There is no automatic expiry.

## Validation

```powershell
py -3 -m pip install pytest
py -3 -m pytest ai-services\multi-agent-server\tests -q
```

Automated tests inject fake provider responses and require no live AI/MCP/RAG
services. The dedicated shared-server CI runs them independently of feature
workflows. They do not replace each student's required two backend endpoint
tests or local pre-commit scans.

Implementation modules and the terminal entry point remain in this directory.
Automated tests live under `tests/`, with import setup in `tests/conftest.py`.
`evaluate.py` is a separate opt-in live-model evaluation tool, not an automated
test collected by pytest. Captured synthetic evaluation outputs are under
`docs/release-2/shared/multi-agent-evidence/`, not beside implementation modules.

For a real smoke run, check `ready=true`, run the example above, inspect all
three role outputs and timings, record a human decision, and export history and
audit evidence. Test quality with representative tasks before relying on an
output; source-ID validation and a Reviewer do not guarantee factual accuracy.

### Repeatable real-model evaluation

With the host server and models running, from the repository root:

```powershell
py -3 ai-services\multi-agent-server\evaluate.py --url http://127.0.0.1:5010 --output-dir .\test-evidence\real-run-1
```

Use a fresh output directory. The evaluator reads `MULTI_AGENT_TOKEN`, saves
synthetic requests, per-workflow JSONL and `evaluation.json`, and exits nonzero
when any check fails. Allow several minutes. Five different scenarios exercise
text, exact structured arithmetic, missing information, conflicting sources and
unsupported requests, labelled with all five feature identifiers. These are
shared-service tasks, not feature integrations. Check model timings, exact totals,
cited sources and the recorded limitations manually as well.

An additional isolated Reviewer probe receives deliberately incorrect Worker
output (AUD 999 and an invented identifier against AUD 20 evidence). This is
clearly labelled as an injected test, not a spontaneous end-to-end model mistake.
The probe calls local Ollama directly; use the same model/environment settings
as the local server. Do not use this runner as a remote-server quality test.

The evaluator never records a human decision. A person must inspect outputs and
use the terminal/API decision path for genuine human-review/report evidence.
Automated semantic checks are only coarse regression gates, not proof that
proposals are correct or safe. Review and redact exported evidence before sharing.
See [the captured evidence and handoff](../../docs/release-2/shared/multi-agent-server-handoff.md)
for validation results, deployment limitations and owner responsibilities.

## Cloud boundary

This implementation is local only. No feature integration, cloud enablement,
cloud infrastructure, or bonus claim is included. The required cloud profile
keeps Multi-Agent disabled. A future bonus deployment needs reachable inference,
TLS, authenticated/private networking, protected durable data and secrets,
deployment automation, and working application evidence.
`host.docker.internal` is a local Docker Desktop access path, not an Azure
service address. No cloud multi-agent URL or VM is configured by this PR.
Cloud enablement belongs in a separate deployment change with a reachable
service URL and appropriate networking; a dedicated or shared VM is an
option, not a requirement of the local baseline.
