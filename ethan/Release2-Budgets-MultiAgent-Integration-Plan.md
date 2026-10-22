# Release 2 Budgets Multi-Agent Integration Plan

## 1. Purpose

Integrate the shared non-containerised Multi-Agent Server into the Budgets
individual feature (`ethan/`) so that it meets the Release 2 brief and rubric,
while modifying nothing outside the Budgets feature except the minimum shared
wiring listed in section 4.

References: [Release 2 brief and rubric](Release2%20brief%20and%20rubric.docx),
[shared server plan](Release2-Multi-Agent-Server-Implementation-Plan.md),
[HTTP contract](../docs/release-2/shared/multi-agent-server-contract.md),
[feature-owner handoff](../docs/release-2/shared/multi-agent-server-handoff.md).
Reference integration: Savings (commit `80dc97e`).

## 2. Decisions

- On human Accept, create a coach proposal (`proposed`); the user applies it with
  the existing Apply button (two-step).
- Trigger the workflow from both a dedicated "Multi-Agent Review" button on the
  selected budget month and chat adjustment requests.
- The existing Ollama chat path keeps using both MCP and RAG. Transaction
  information comes from MCP; the prompt sent to the Multi-Agent Server is built
  from RAG guidance retrieved using that MCP data.
- Endpoints tested in `Ethan-ci.yml`:
  - `POST /api/budgets/<id>/multi-agent/workflows`
  - `POST /api/multi-agent/workflows/<wid>/decision`

## 3. Flow

Frontend UI -> budgets-backend -> (MCP transactions -> RAG guidance -> prompt/evidence)
-> Multi-Agent Server (`feature: "budgets"`) -> Planner -> Worker -> Reviewer
-> UI shows results -> Human decision (Accept / Partially Accept / Reject)
-> backend records the decision on the server -> validated proposal stored
-> existing Apply flow (`proposal_service`).

## 4. Scope

In scope (`ethan/` and `reports/release2-budgets/`):

- `ethan/backend`
  - `config.py`: `MULTI_AGENT_ENABLED` (default false), `MULTI_AGENT_URL`,
    `MULTI_AGENT_TOKEN`, `MULTI_AGENT_TIMEOUT_SECONDS` (> 550). `/health` reports
    `multi_agent_mode`.
  - `multi_agent_client.py` (same style as `mcp_client.py` / `rag_client.py`):
    bearer token (backend only), maps 401 and `workflow_busy` 503
    (`Retry-After`) to safe errors, never auto-resubmits after a timeout
    (check status first), handles 409 on repeat decisions.
  - `multi_agent_service.py`: evidence builder shared with the Ollama chat path
    (MCP -> RAG), bounded to <= 32 items, unique source IDs, < 16 KiB per agent;
    validates Worker structured output against the existing `update_budget_line`
    schema; Partial Accept requires reason + corrected output of matching type;
    Reject stores nothing; failed workflows are reject-only; overriding a
    non-approve Reviewer verdict requires a reason.
  - `app.py`: the two endpoints above; chat adjustment requests call the same
    service.
- `ethan/frontend`: Multi-Agent Review panel (Planner plan, Worker output,
  Reviewer verdict/risks, source citations, decision controls, non-blocking
  progress for 40-165 s runs); longer proxy timeout in `nginx.conf`.
- `ethan/test` and `.github/workflows/Ethan-ci.yml`: mocked-server tests
  (success, 401, busy, timeout, disabled flag, decision rules); CI runs with
  `MULTI_AGENT_ENABLED=false`.
- `reports/release2-budgets/`: evidence (section 6).

Minimum unavoidable shared edits (as in the Savings integration):

- `docker-compose.yml`: three `MULTI_AGENT_*` env lines on `budgets-backend`.
- `shared/frontend/nginx.conf`: longer `/budgets-backend/` timeouts.

Out of scope: `agentic_loop/`, `ai-services/`, Bicep/cloud IaC, other students'
features, adding the server to Docker Compose.

## 5. Rubric mapping

| # | Criterion | Plan |
|---|-----------|------|
| 1 | Integrated local app + AI services | Budgets CRUD plus AI-Mode, MCP, RAG and Multi-Agent all working locally; server stays a host process |
| 2 | Multi-Agent workflow | UI and terminal runs against the same server; history + audit exports kept as evidence |
| 3 | Pre-commit security | Fix detect-secrets finding (`frontend/public/index.html:8`) and pip-audit finding (flask 3.1.2 -> 3.1.3); locate or add the pre-commit config (none found in repo); re-run Ruff security rules, detect-secrets, pip-audit; save the report |
| 4 | Post-commit endpoint tests | Two Release 2 endpoint tests run by `Ethan-ci.yml`; save workflow evidence |
| 5-6 | Cloud scripts / workflow | Not modified; keep `MULTI_AGENT_ENABLED` false by default so the existing cloud deployment stays valid |
| 7 | Cloud CRUD, AI disabled | Default-off flag; no dependency on `host.docker.internal` in the cloud; `/health` shows `multi_agent_mode: disabled` |
| 8 | Agentic-loop review modes | Provide machine-readable exports and test reports for the shared Multi-Agent and Testing review modes; do not edit `agentic_loop/` |
| 9 | Report evidence | Contribution log, design-flow input, workflow evidence, test and scan reports |
| 10 | Showcase | Short Budgets demo script: UI workflow, human decision, local vs cloud (disabled), scans, CI |

## 6. Evidence to produce (`reports/release2-budgets/`)

- Workflow export (`workflow_history.jsonl`, `coordination_audit.jsonl`) from a
  real UI run, and a terminal run on the same server.
- Pre-commit security scan report (passing).
- CI endpoint test report and workflow run link.
- Local multi-terminal run log; cloud baseline health output.
- Budgets contribution log and showcase script.
- Genuine human decisions only; no automated decisions presented as human review.

## 7. Contract safeguards

- Token only in backend environment configuration; never in frontend, evidence,
  logs or Git.
- Request timeout above 550 s; treat timeouts as unknown outcome.
- Evidence is redacted, concise and treated as untrusted by the server.
- Do not use `options.use_mcp` (not supported); the backend gathers MCP/RAG
  evidence itself.

## 8. Implementation order

1. Config flags, client, service, endpoints (backend) with unit tests.
2. Share the MCP -> RAG evidence builder with the Ollama chat path.
3. Frontend panel, nginx timeout, shared compose/nginx wiring.
4. Pre-commit fixes and scan report.
5. CI workflow update.
6. Live local run (UI + terminal), collect evidence, verify disabled cloud baseline.

## 9. Validation

- `pytest` in `ethan/`, Ruff security rules, detect-secrets, pip-audit.
- Live run with the server as a host process and the Docker Compose Budgets
  containers; confirm Accept creates a `proposed` coach proposal and Apply works.
- Confirm behaviour with `MULTI_AGENT_ENABLED=false`.
