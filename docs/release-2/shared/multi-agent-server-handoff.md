# Shared Multi-Agent Server: feature-owner handoff

This covers the local shared tool only. No feature routes, UI, databases,
Compose services or cloud infrastructure are added. The earlier Bills-specific
integration and its configuration have been removed from this branch. Use the
[setup guide](../../../ai-services/multi-agent-server/README.md) and
[versioned HTTP contract](multi-agent-server-contract.md) together.

## Deployment and caller requirements

Run `server.py` as one host Python process using Flask's built-in threaded
development server and a persistent, protected `MULTI_AGENT_LOG_DIR`. Debug
mode and the reloader are disabled to avoid extra processes against the data
directory. This is suitable for a small local project/demo, not production.
Keep AI/MCP/RAG host-run as required by the brief. The existing Compose Ollama
service is a separate topology issue:
successful inference against it is not proof of the required non-containerised
whole-application deployment.

Docker Desktop callers use `http://host.docker.internal:5010`; a host binding
reachable from Docker requires `MULTI_AGENT_HOST=0.0.0.0` and a random token of
at least 32 non-whitespace characters. Forward that token only from trusted
backend environment configuration as a bearer authorization header. Never put
it in frontend code, request evidence, screenshots or Git. Restrict firewall
access; use TLS/private networking beyond the trusted local machine. Linux
Docker may need the `host.docker.internal:host-gateway` mapping.

After starting the host server, a generic container can verify the same access
without modifying any feature. With `MULTI_AGENT_TOKEN` already set in the
PowerShell environment:

```powershell
docker run --rm --add-host host.docker.internal:host-gateway -e MULTI_AGENT_TOKEN python:3.12-slim python -c 'import os,json,urllib.request; r=urllib.request.Request("http://host.docker.internal:5010/health",headers={"Authorization":"Bearer "+os.environ["MULTI_AGENT_TOKEN"]}); print(json.load(urllib.request.urlopen(r,timeout=15)))'
```

Expect `ready: True` and no missing models. A call without the token must receive
401. This checks networking/authentication only, not feature integration.

Use a workflow request timeout above the configured overall deadline:
550 seconds is the initial minimum recommendation for a 540-second workflow.
Allow more overhead on slow machines. The Anomalies adapter uses a 600-second
timeout and backend-only token forwarding; other future feature adapters must
implement the same documented requirements.
Only one model workflow runs at a time; handle `workflow_busy`/503 and its
`Retry-After` header without blocking other UI interaction.

## Integration responsibilities

1. Authenticate/authorize the application user in the feature backend. The
   shared token is not tenant isolation, and caller-supplied `decided_by` is
   not verified identity.
2. Retrieve bounded evidence using the feature's own database/MCP/RAG code.
   Supply unique stable source IDs and concise summaries; redact sensitive
   data. Evidence is persisted. No tool retrieval happens inside this server.
3. Submit generic task/context/evidence. Store the returned workflow ID.
   Display Planner, Worker, Reviewer, risks, limitations and source citations.
   Reviewer approval is not human approval or a correctness guarantee.
4. Let the person select Accept, Partially Accept or Reject. Corrections need
   a reason and must match the original text/object type. Overriding a
   non-approve verdict needs a reason. Failed workflows permit rejection only.
5. Use the recorded `human_decision.final_output`, not an unchecked Worker
   response. Validate accepted JSON against the feature's own domain schema
   and current business rules before any explicitly authorized mutation.
   Apply normal authorization, concurrency and duplicate-application controls;
   a saved server decision does not itself apply anything to feature data.
6. Treat network timeouts as an unknown outcome. Do not blindly resubmit a
   workflow or decision. Retrieve saved state/status first; there is no
   workflow-creation idempotency key. Repeated decisions return 409. A failed
   export is safe to retry and does not invalidate an already saved decision.

Use the HTTP export or CLI `--export <id> --output-dir <new-directory>` for
history and ordered audit events. Keep exports outside Git unless they contain
only intentionally reviewed synthetic evidence. No expiry or encryption at
rest is provided; protect, retain and remove data according to group policy.

## Release 2 assessment boundary

The shared service supplies sequential real agents, explicit human-decision
mechanics, terminal/HTTP entry points and auditable persistence. Synthetic
server scenarios across five feature labels are not five integrated features.
Automated decision-path exercises are not genuine human review.

Feature owners still need UI-driven evidence of their actual workflows and
human decisions, and their required backend endpoint tests. The separate
agentic-loop review modes, pre-commit security scans/reports, full local
non-containerised AI topology, AI-disabled cloud baseline, report and showcase
remain group work. No cloud bonus is claimed here.

## Captured validation

The [synthetic evidence directory on the companion branch](https://github.com/David-S-22/Tally/tree/ethan/MultiAgentEvidence/docs/release-2/shared/multi-agent-evidence) retains requests,
full stage snapshots, ordered audit events and machine-readable results.
No application financial records or credentials are included.
The files are separated from implementation PR #219 on `ethan/MultiAgentEvidence`
for an evidence-only PR. The branch links become available after publication.

The first run, `2026-10-18-local`, intentionally remains available even though
its overall result failed. Four scenarios passed; with no supplied evidence,
the Planner put desired income/balance/commitment descriptions into
`required_evidence` instead of `missing_information`. Post-generation validation
blocked it after one repair, skipped Worker/Reviewer and allowed rejection only.
The isolated real Reviewer also flagged the injected AUD 999/invented-ID error.

The fix constrains generation-time reference arrays to supplied source IDs
(or an empty array with no sources), keeps runtime validation and clarifies
the Planner prompt. Tests check that only citation fields are constrained,
without accidentally constraining other fields or mutating shared schemas.

The focused suite passes 60 tests on Windows Python 3.14 and in a fresh-dependency
Linux Python 3.12 container with the repository mounted read-only. This verifies
the interpreter/platform contract; it is not native Windows Python 3.12
installation evidence, a live-model Linux deployment, or a completed remote
GitHub Actions run. Ruff checks and formatting also pass locally.

The corrected run is [2026-10-18-verified/evaluation.json](https://github.com/David-S-22/Tally/blob/ethan/MultiAgentEvidence/docs/release-2/shared/multi-agent-evidence/2026-10-18-verified/evaluation.json).
All five automated scenario gates and the isolated real Reviewer probe passed.
Planner/Worker used `qwen2.5:3b`, Reviewer used `llama3.1:8b`; completed workflows
took approximately 43-165 seconds. Each scenario directory contains history/audit
exports taken before decisions, preserving the pending handoff.

| Scenario | Verified automated gate |
| --- | --- |
| Text / transactions label | Expected AUD 20 fact and supplied source citation |
| Structured / savings label | Exact AUD 50 object and the two expected source IDs |
| Missing / budgets label | Planner missing information and Worker limitations |
| Conflict / bills label | Both AUD 20/AUD 30 records and an explicit conflict |
| Unsupported / anomalies label | Worker limitations or a non-approve verdict with risks |
| Isolated Reviewer | Risks identify the injected AUD 999 and invented transaction identifier |

[deployment.json](https://github.com/David-S-22/Tally/blob/ethan/MultiAgentEvidence/docs/release-2/shared/multi-agent-evidence/2026-10-18-verified/deployment.json)
records successful authenticated Docker-to-host health and saved-workflow
retrieval, unauthenticated 401, actual server restarts retaining pending and
recorded decisions, terminal retrieval, all three automated decision paths
and duplicate-decision 409. The `text-decision`, `structured-decision` and
`missing-decision` directories contain the later decision snapshots. Their
reasons and identities explicitly identify automated synthetic tests; they
must not be presented as genuine human-review assessment evidence.

These passing gates are deliberately narrower than full task-quality approval.
All five real Reviewer verdicts were `approve_with_changes`. For example, the
structured proposal had the correct total but lacked the requested readable
explanation; the Reviewer flagged that. Some other Reviewer objections were
subjective or overly strict. Human inspection remains necessary even when
automated checks pass, and a Reviewer warning is not itself proof of a defect.

The host server used Waitress for these historical captured runs; the current
entry point uses Flask's built-in threaded development server. The host server
used the existing local Ollama container for these runs. The
ephemeral Python containers were test runners/callers, not a deployed server.
This establishes compatibility, not compliance with the brief's fully
non-containerised AI topology. All temporary host servers and test containers
were stopped; existing application containers were left untouched.

## PR review follow-up

The Azure `host.docker.internal` setting is absent from the current branch;
Bicep and Compose match the baseline without a multi-agent deployment.
That hostname is local-only, not an Azure endpoint. Optional cloud hosting
requires a separate deployment change, a real reachable URL, protected
networking/TLS, authentication and persistent state. A VM is one option;
the required cloud baseline keeps multi-agent disabled.

Removed `logs/.gitkeep`: startup creates the configured data directory itself.
Existing runtime data was not deleted.

Ollama transport now uses the official Python SDK (`AsyncClient.chat` and
`list`) rather than custom HTTP request/envelope handling. A small response
stream wrapper preserves the size cap, and an asynchronous timeout bounds the
whole operation, including response reads. Existing role orchestration,
generation options, schema/source validation and failure semantics remain.
The earlier saved evaluation bundles describe the pre-SDK implementation;
they have not been rewritten to imply that they were rerun with the SDK.

Automated tests are now separated under
`ai-services/multi-agent-server/tests/`; shared pytest import setup lives in
`tests/conftest.py`. CI and the README target that directory explicitly.
Implementation/CLI modules remain outside the test directory, the opt-in live
evaluator remains `evaluate.py`, and saved evidence is reviewed separately on
the companion evidence branch.
