# Shared terminal agentic loop (Plan → Act → Observe → Adapt)

A terminal review workflow for the group application. Each run picks a
review target, collects evidence, sends it through two local LLMs, and ends
with a human step that accepts, rejects or edits the finding. Every run
writes the run record the reports require, as `reports/report.json`,
`report.md` and `run-view.md`.

The loop is two Python files. `main.py` holds the loop and the collectors,
and `record.py` writes the run record. Each mode is one `collect` function
and three prompt files.

## Modes

| Mode | OBSERVE evidence | Asserts, one PASS/FAIL line each |
|---|---|---|
| Architecture | The `docker-compose.yml` services, ports and dependency edges, the student directories, the required directories, and the shared `index.html`. Read from files, so nothing needs to be running | None. The models describe what they see |
| MCP validation | Live and read-only. Lists the tools and the `search_transactions` schema, then calls the tool unfiltered, filtered by the first row's merchant and month, and with a merchant that cannot exist | The tool is registered with `start_date`, `end_date` and `merchant`; the result is a list of rows with `id`, `date`, `merchant` and `amount`; every row matches the filters and the impossible merchant returns 0 rows; the filtered call takes under 3000 ms |
| RAG validation | Live and read-only. `GET /health`, then a question built from one real `transactions-records` chunk, then `POST /retrieve` with `k=3` on `transactions-records` and `transactions` for that question and for a fixed unanswerable one | Both collections are present; the seeded question scores under `RAG_INSUFFICIENT_ABOVE`, has a `category_id` and returns the seed's merchant as the best chunk; the unanswerable question scores above the threshold; the guide's best chunk has `doc_type` markdown |
| Multi-Agent Workflow Review | Live and read-only. `GET /health`, then the latest workflow from `GET /workflow/status`, or the one named by `MULTI_AGENT_WORKFLOW_ID`, then its `GET /workflow/<id>/export`. When `MULTI_AGENT_EXPORT_DIR` is set, the mode reads a committed folder holding `workflow_history.jsonl` and `coordination_audit.jsonl` instead, and needs no server | Planner, Worker and Reviewer all report `success` and ran in that order; every `evidence_used` entry names a supplied source; a human decision is recorded, with a reason when someone accepted a non-approve verdict; the audit events are in order and `ts` never goes backwards. The mode also reports the verdict, the risk and recommendation counts, the snapshot count and each role's confidence label, with no PASS or FAIL |
| Testing Review | Runs `pre-commit run --all-files` at review time. For each student, runs `gh run list` for their `*-ci.yml` on `main` and counts the `def test_endpoint_` functions in their test folder | The Ruff security, detect-secrets and pip-audit hooks each Passed; each student's latest run is `success` and they have at least 2 endpoint tests |
| Cloud Deployment Review, release decision | Runs `gh run list` for `cloud-deployment.yml` on `main` and for each student CI workflow at that commit; reads `enableMcp`, `enableRag` and `deployOllamaVm` from `bicep/`; sends `GET` to `CLOUD_APP_URL`, and to `CLOUD_APP_URL` followed by `CLOUD_CRUD_PATH`, with one retry for a cold start | The deployment is `success`; CI is `success` at the deployed commit; all three flags are `false` and `bicep/` defines no Multi-Agent app; both live probes return 2xx. The ADAPT accept or reject is the human release decision |

The validation probes seed themselves from live data, so they keep working
when the seed data changes. Neither calls `/refresh` or writes anything. When
a server is unreachable, the collector returns `(False, reason)`, the run
records the OBSERVE failure and continues. When a single `/retrieve` fails,
the mode records it on its own line as `retrieve failed: HTTP <code>` and
continues.

## Stages

- PLAN. The run chooses the target and loads the prompts from `prompts/<family>/`.
- OBSERVE. The mode's collector gathers the evidence.
- ACT. The implementation model writes a finding, and the review model critiques it.
- ADAPT. The human accepts, rejects or edits the finding, and the run records the decision.

## Run

```
pip install -r agentic_loop/requirements.txt
python -m agentic_loop.main
```

Run it from the repository root. Real model output needs `ollama` up, which
`docker compose up -d ollama` starts. The validation modes also need the
shared servers on the host. Start the MCP server on 8000 with
`python ai-services/mcp-server/server.py` and the RAG server on 5003 with
`python ai-services/rag-server/server.py`, with `transactions-db` running
and the Transactions backend started once so `transactions-records` is
populated. The run records anything missing as a failure, not a crash.

The Multi-Agent, Testing and Cloud modes have their own prerequisites. The Multi-Agent mode needs
the server running on 5010, started with
`python ai-services/multi-agent-server/server.py`, and `MULTI_AGENT_TOKEN`
in the root `.env`, or `MULTI_AGENT_EXPORT_DIR` set to a committed export
folder. The Testing mode needs `.pre-commit-config.yaml` at the repository
root and the GitHub CLI (`gh`) logged in. The Cloud mode needs `gh` and
`CLOUD_APP_URL` for the live probe.

Git ignores `reports/`. The committed samples live in
`docs/release-0/agentic-loop/` for Architecture, `docs/release-1/agentic-loop/`
for MCP and RAG, and `docs/release-2/agentic-loop/` for Multi-Agent, Testing and Cloud
Deployment.

## Environment variables

The loop loads these from an optional root `.env`. The defaults suit a host
terminal.

| Variable | Default | Meaning |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | The ollama OpenAI-compatible endpoint |
| `OLLAMA_MODEL` | `qwen2.5:0.5b` | Implementation model |
| `OLLAMA_REVIEW_MODEL` | `llama3.1:8b` | Review model |
| `MCP_SERVER_URL` | `http://localhost:8000/mcp` | Shared MCP server |
| `RAG_SERVER_URL` | `http://localhost:5003` | Shared RAG server |
| `RAG_INSUFFICIENT_ABOVE` | `1.2` | A best distance above this is insufficient. The Transactions backend uses the same value |
| `LOOP_HTTP_TIMEOUT` | `30` | Seconds per MCP, RAG, Multi-Agent or cloud call |
| `MULTI_AGENT_SERVER_URL` | `http://127.0.0.1:5010` | Shared Multi-Agent Server, the same variable `workflow_runner.py` reads |
| `MULTI_AGENT_TOKEN` | from the root `.env` | Bearer token. Required because the server binds `0.0.0.0` |
| `MULTI_AGENT_FEATURE` | all | Feature filter for the latest workflow |
| `MULTI_AGENT_WORKFLOW_ID` | latest | Review this workflow instead of the latest |
| `MULTI_AGENT_EXPORT_DIR` | unset | Review a committed export folder instead of calling the server |
| `CLOUD_APP_URL` | unset | Deployed shared frontend, the Bicep `applicationUrl` output |
| `CLOUD_CRUD_PATH` | `/transactions-backend/transactions` | Read-only CRUD probe path |
| `LOOP_COMMAND_TIMEOUT` | `300` | Seconds for `pre-commit` and for each `gh` call |

## Adding a mode

1. Add `prompts/<family>/implementation/system_prompt.txt`,
   `implementation/task_prompt.txt` and `review/review_prompt.txt`.
2. Write a collect function in `main.py` that returns `(ok, evidence_text)`.
3. Add one entry to `MODES`. The menu numbers itself.

The MCP and RAG modes are the worked example. The engine has not changed since
they were added.
