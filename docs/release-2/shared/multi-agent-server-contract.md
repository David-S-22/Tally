# Shared Multi-Agent Server HTTP contract 1.0

This implementation contract reconciles the stub with the approved
[server-only implementation plan](../../../ethan/Release2-Multi-Agent-Server-Implementation-Plan.md).
The original proposal is PR #217; its ADR is not present on this branch.
This document records implementation decisions rather than claiming that
the proposed ADR has been merged or approved by other feature owners.

## Boundaries

One host process on port 5010; no Compose service and no feature database/tool
access. Planner -> Worker -> Reviewer is sequential, followed by a separate
human decision request. Feature backends supply evidence and apply accepted
results through their own business validation. Feature integrations and UI are
not included in this shared-service implementation.

All endpoints require bearer authentication when `MULTI_AGENT_TOKEN` is set.
The default binding is `0.0.0.0:5010`; non-loopback binding requires a token.
The server entry point loads the repository-root `.env`, also used by Compose.
This is trusted backend-client access,
not multi-tenant authentication. Feature labels and reviewer names are not
authorization claims. All callers with the token can access all workflows.

## Routes

| Method and path | Result |
| --- | --- |
| `GET /health` | 200 process liveness, `ready`, Ollama status, configured and missing models, contract version, `export_mode: "on_demand"` |
| `POST /workflow` | 200 completed stage sequence or plan-only; 503 saved failed sequence or busy/provider failure |
| `GET /workflow/<id>` | 200 full persisted record, or 404 |
| `POST /workflow/<id>/decision` | 200 recorded decision, 400 invalid decision, 409 invalid transition |
| `GET /workflow/status?feature=<feature>` | 200 counts, latest record and export mode; optional validated feature filter |
| `GET /workflow/<id>/export` | 200 object containing `workflow_history.jsonl` and `coordination_audit.jsonl` arrays for that run |

Responses have `Cache-Control: no-store` and `X-Request-ID`. JSON errors retain
`status: "error"` and `error`, adding safe `code` and request correlation.
Other HTTP errors: 400 invalid JSON/contract, 401 invalid token, 413 oversize,
415 non-JSON input, 503 storage unavailable. A busy response has
`code: "workflow_busy"` and `Retry-After: 5`. Do not automatically retry a
timed-out workflow creation: it may already have been recorded. Retrieve its
state via status/history first. There is no creation idempotency key.

## Request

```json
{
  "contract_version": "1.0",
  "feature": "transactions",
  "task": "Summarise the supplied example without changing data.",
  "evidence": [
    {"source": "example:expense", "summary": "A synthetic grocery expense is AUD 20.", "data": {"amount": 20}}
  ],
  "context": {"constraints": "Do not invent facts or apply changes."},
  "options": {}
}
```

`feature`, `task`, `evidence` are required. `feature` is one of transactions,
savings, anomalies, bills, budgets; it does not select server business logic.
`contract_version` is optional for existing clients and must be `1.0` if sent.
Task limit: 4,000 characters. Evidence: at most 32 items, unique non-blank
source IDs (200 characters), non-blank summaries (4,000 characters), optional
JSON `data`. Context: object, at most 8 KiB. HTTP and validated JSON are limited
to 64 KiB, nesting to 12 levels; NaN/infinity are rejected. JSON serialization
overhead counts toward limits.

Combined input for each agent, including earlier agent outputs, is limited to
16 KiB; exceeding it fails explicitly with `context_limit`, rather than silently
truncating evidence. Keep evidence concise. Role outputs are also bounded by
schemas and generation limits.

Options: `plan_only` (boolean) or `reuse_plan` (workflow ID), not both. Unknown
fields/options are rejected. Plan-only returns `awaiting_evidence`. Resume
must preserve feature/task/context, retain existing evidence unchanged and may
add new sources. It retains the ID and skips Planner; a plan can resume once.

## Results and failures

Records retain the stub's `status`, `workflow_id`, `feature`, `task`,
`timestamp`, `duration_ms`, `participating_agents`, `planner_result`,
`worker_result`, `reviewer_result`, and `human_decision`.
Additional fields include `contract_version`, `workflow_state`, `context`,
`evidence`, `prompt_version`, `request_id`.

Each successful role has `status: "success"`, `agent`, `model`,
`human_approval_required: true`, `duration_ms` and:

| Role | Validated fields |
| --- | --- |
| Planner | `plan`, `required_evidence`, `missing_information`, `completion_criteria` |
| Worker | `output` (text), `structured` (object or null), `evidence_used`, `evidence_count`, `limitations` |
| Reviewer | `verdict` (approve / approve_with_changes / reject), `risks`, `recommendations`, `review`, `evidence_used` |

The server verifies references name supplied source IDs. Missing desired evidence
belongs in Planner `missing_information`, not fabricated source IDs.
The generation schema also limits reference arrays to supplied IDs (or to an
empty array when no sources are supplied); post-generation validation remains
mandatory. These constraints do not restrict missing-information descriptions.
Domain JSON is a proposal, not schema-validated feature data.

Timeouts/errors persist `status: "timeout"` or `"error"` on the failed agent,
a safe `error` and `code`, and mark downstream agents `"skipped"`. The overall
record has `status: "error"` and `workflow_state: "failed"`; only rejection is
allowed. No fallback proposal is fabricated. A negative Reviewer verdict with
valid output is not a provider failure.

One workflow can invoke models at a time. Reads/decisions remain available.
Maximum per-step timeout defaults to 180 seconds and the overall model
deadline to 540 seconds, including one possible schema repair per role.
Caller timeouts should exceed 540 seconds plus network/persistence overhead.
Database lock waits have a separate 10-second bound.

## Human decisions

```json
{
  "decision": "Partially Accept",
  "corrected_output": "Human-corrected output.",
  "reason": "Corrected the wording.",
  "decided_by": "caller-supplied-reviewer"
}
```

- Accept selects Worker `structured` if not null, otherwise `output`.
- Partially Accept requires non-empty corrected output and a reason. Its type
  must match the original text/object output.
- Reject requires no corrected output and yields `final_output: null`.
- Accepting/partially accepting a non-approve Reviewer verdict requires a reason.
- Failed workflows are reject-only; awaiting-evidence/running/decided workflows
  cannot record decisions.
- Decisions commit once, atomically. A repeat returns 409, even if identical.

`human_decision` then contains `required`, `status: "recorded"`, `decision`,
`final_output`, `reason`, `decided_by`, `timestamp`. Identity is audit metadata;
the feature backend is responsible for verifying its human user.

## History, audit and restart

SQLite holds authoritative state, history snapshots and ordered audit events.
JSONL exports keep the existing filenames and latest-snapshot-per-ID semantics;
they are generated explicitly per workflow, not refreshed after state changes.
Intermediate stage snapshots are included. Indexed workflow/sequence queries
avoid rewriting the entire database's history during saves or decisions.
Events include workflow_started/resumed, agent_started/finished, awaiting_evidence,
handoff, human_decision and error. Events contain timestamps, ID, feature and
request correlation, plus outcome/timing where applicable. Full task/evidence
are in history, not ordinary operational logs.

HTTP export reads authoritative state. The CLI's `--export <id> --output-dir <path>`
writes the two JSONL files into a new directory and refuses an existing directory.
Omit `--output-dir` to print the JSON object instead. An export/file-write failure
does not undo a saved workflow or decision; correct the error and request the
export again rather than replaying model work or a decision. Health/status
reports `export_mode: "on_demand"`, not an automatic-export health flag.
Restart marks interrupted running workflows failed and reject-only, and retains
pending/decided workflows. The production entry point locks the data directory
to prevent two coordinators from owning it. Legacy stub history must be archived
and a fresh directory selected; no automatic import or deletion.

## Migration from the stub / PR #217 proposal

Routes and common result fields remain. Intentional differences:

1. Real roles use native Ollama `OLLAMA_URL`, not the proposal's `/v1`
   `OLLAMA_BASE_URL`; default Planner/Worker are 3b, Reviewer 8b.
2. Binds to all interfaces by default; reachable binding requires a token. Clients must send it
   when enabled. Individual feature clients are owner-managed work; the earlier
   Bills-specific integration has been removed from this branch.
3. Strict validation, reason requirements, safe failed-stage handling and
   reject-only failures replace permissive stub behavior.
4. The terminal runner uses HTTP; SQLite replaces JSONL as authoritative state.
5. One active workflow, bounded execution, explicit readiness and export APIs.
6. No per-feature prompt fragments and no `options.use_mcp`.

The earlier stub client's 90-second timeout is insufficient for the default
real-model deadline. Future feature clients must implement the documented
timeout/authentication/reason handling before claiming end-to-end integration.
The Anomalies integration is documented in the
[Anomalies feature guide](../../release-1/anomalies/multi-agent-server.md);
other feature integrations remain owner-managed work.

## Rubric boundary

This service supplies real sequential agents, terminal/HTTP entry points, human
decisions, history and audit evidence. Actual feature UI integration, the separate
agentic-loop review modes, per-student endpoint CI/pre-commit reports, full local
and baseline cloud deployment and the showcase remain group/feature work.
No cloud AI bonus is claimed by this implementation.
