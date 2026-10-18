# Release 2 Shared Multi-Agent Server Implementation Plan

## 1. Purpose

This plan defines how to take the current Release 2 Multi-Agent Server stub to
a complete, reusable shared service. It is the implementation reference for
work on the shared server and its server-side tests, documentation, and
deployment support.

The design recommendations below were approved on 18 October 2026. Use these
decisions during implementation; optional cloud enablement and individual
feature integrations remain outside this work.

The server must implement the Release 2 Planner → Worker → Reviewer → Human
Review workflow, run locally as a non-containerised process, be callable from
the terminal and over HTTP, and expose a stable contract that all five feature
backends can integrate with independently.

## 2. Scope boundary

This work is dedicated to the **shared Multi-Agent Server**.

In scope:

- Real Planner, Worker, and Reviewer behavior using the shared local AI
  runtime.
- Generic HTTP and terminal interfaces, workflow validation, persistence,
  history, auditability, failure handling, health reporting, security, tests,
  setup documentation, and deployment/run instructions for the shared server.
- A stable, feature-neutral contract and examples for future feature-owner
  integrations.
- Server evidence that the shared agent workflow can be exercised with each
  supported feature identifier without integrating or modifying those
  features.

Out of scope:

- Integrating Transactions, Anomalies, Savings, Bills, or Budgets UIs/backends
  with the server. Feature owners will do that independently later.
- Adding feature-specific prompts, business rules, database access, or data
  writes to the shared server.
- Changing `ethan/` Budgets application code or treating the server as a
  Budgets feature.
- Adding the Multi-Agent Server to Docker Compose. The brief requires this and
  the other local AI services to remain non-containerised.
- Enabling Multi-Agent in the required cloud baseline. Cloud enablement is
  optional bonus work and must be kept separately gated.

The shared service may support the five stable feature identifiers
(`transactions`, `anomalies`, `savings`, `bills`, and `budgets`) as contract
metadata. It must not contain per-feature orchestration or assume that a
feature has already integrated it.

## 3. Release 2 requirements this plan addresses

The work must support the following brief and rubric outcomes:

1. One shared, non-containerised local server coordinates Planner, Worker,
   Reviewer, and Human Review.
2. A workflow can be run from both the terminal and the integrated
   application path. For this server-only phase, the HTTP contract is made
   ready for feature backends; individual feature UI/backend wiring is deferred.
3. Human approval, correction/partial acceptance, or rejection is explicit
   and recorded.
4. Workflow history and coordination/audit logs show the work and decision.
5. The agentic-loop Multi-Agent Review mode can consume machine-readable server
   evidence. Implementing that review mode itself remains a separate shared
   agentic-loop workstream.
6. The full local application can run with this server as a separate host
   process and the existing frontend/backend/database containers.
7. The standard cloud application remains operational with AI-Mode, MCP, RAG,
   and Multi-Agent disabled by default. Cloud Multi-Agent deployment is an
   optional bonus extension only.

The complete assessment also requires pre-commit security scans, post-commit
tests for two endpoints per student, other agentic-loop review modes, cloud
deployment, a report, and a showcase. Those are not server implementation
tasks, but this service must provide reliable interfaces and evidence that
allow the group to complete them.

## 4. Starting baseline (before implementation)

The current `ai-services/multi-agent-server/` implementation already provides
a useful contract prototype:

- Flask routes for health, starting workflows, recording decisions, getting a
  workflow, and viewing status.
- A coordinator that runs three agent functions, supports `plan_only` and
  `reuse_plan`, and writes `workflow_history.jsonl` and
  `coordination_audit.jsonl`.
- A terminal runner using the same coordinator.
- Contract tests for routes, decisions, logging, filtering, and restart
  recovery.
- Bills-specific client wiring on the current branch.

The agents in `agents.py` currently return deterministic `[stub]` output and
make no model calls. The server's health route reports a stub model. This plan
replaces those placeholders while preserving a stable HTTP contract.

## 5. Target design

### 5.1 Responsibilities and boundaries

- **Feature backend (future integration):** authenticates/validates the
  user's feature request, gathers permitted feature evidence, calls the
  shared server, presents results, and applies any accepted change through
  that feature's own services.
- **Multi-Agent Server:** validates the generic workflow request, coordinates
  agent stages, invokes configured models, stores workflow state and audit
  events, and records the human decision.
- **Planner:** turns the task and supplied evidence into a bounded, structured
  plan, listing evidence sources and unanswered questions.
- **Worker:** performs the requested task using the plan and supplied
  evidence; it must distinguish evidence-backed statements from uncertainty.
- **Reviewer:** checks the Worker result against the plan and evidence,
  identifies unsupported claims and risks, and makes recommendations. Its
  verdict is advisory; it never replaces the required human decision.
- **Human reviewer:** explicitly accepts, partially accepts with corrections,
  or rejects. The server records the decision and final output; it does not
  write to a feature database.

The service must be generic: it receives task, context, and evidence from a
caller. It must not fetch data directly from MCP, RAG, feature databases, or
feature APIs. A feature may gather MCP/RAG evidence in its own backend and
send the bounded evidence to the server.

### 5.2 Model integration

- Use the existing shared Ollama runtime; do not introduce another model
  server or move AI runtime into Docker Compose.
- Configure planner, worker, and reviewer model names independently, with
  initial defaults of `qwen2.5:3b` for Planner and Worker and `llama3.1:8b`
  for Reviewer. Validate quality and runtime with real smoke tests before
  finalising these defaults. Read model names, Ollama
  URL, request timeout, and generation settings from environment variables.
- Keep role prompts in `prompts/multi_agent/`, external to Python code.
- Require structured JSON output for each role and validate it against
  explicit schemas before passing it to the next agent.
- Allow at most one bounded repair attempt for malformed model output, within
  the workflow deadline. An unsuccessful repair is a recorded failure.
- Treat evidence as untrusted data, not instructions. Evidence source IDs must
  be unique; all returned evidence references must identify supplied sources.
- Pass only the inputs each role needs. In particular, the Reviewer receives
  the plan, supplied evidence, and Worker result; it does not silently acquire
  additional tools or data.
- Bound task, context, evidence count, evidence text, output size, retries,
  and per-step runtime. Report validation, timeout, model-unavailable, and
  malformed-output failures explicitly.
- Never present stubbed or failed model output as a successful AI result.
  Keep any deterministic test double available only through dependency
  injection or an explicit test configuration, not as a production fallback.
- Record model/provider identifiers and per-agent timings as workflow
  metadata, without recording credentials or hidden chain-of-thought.

### 5.3 Generic workflow contract

Freeze and document a versioned HTTP contract before feature owners integrate.
The existing route names can be retained where practical:

- `GET /health` — service readiness and configured provider/model status.
- `POST /workflow` — validate request, run Planner → Worker → Reviewer, and
  return a workflow ID and pending human decision.
- `GET /workflow/<workflow_id>` — return a caller-authorized workflow record.
- `POST /workflow/<workflow_id>/decision` — record one permitted human
  decision and final output.
- `GET /workflow/status` — return operational counts and pending decision
  information without exposing unrelated workflow content.

The request contract should define:

- `feature`: one of the five known feature identifiers.
- `task`: non-empty, bounded instruction from the caller.
- `evidence`: a bounded list of `{source, summary, data?}` entries, with
  documented size and serialization constraints.
- `context`: optional bounded, non-secret metadata.
- `options`: only documented, validated options such as plan-only/resume.

The response contract should define stable fields for workflow ID, status,
timestamps, durations, each agent's validated result, evidence references,
human decision state, and safe error codes. Use a contract version so later
changes remain compatible with independently developed feature clients.
Document success and failure HTTP statuses, idempotency expectations, and
decision rules. Do not make the endpoint responsible for saving feature
outputs.

Preserve the current routes and existing response fields wherever possible.
Support readable Worker output plus optional structured JSON; validate the
common envelope here and leave domain-specific validation to feature backends.
Initially keep execution synchronous with a documented overall deadline.
Do not introduce a queue or asynchronous job protocol unless measured runtime
demonstrates the need.

Plan resumption must preserve the original feature, task, and constraints and
only allow documented evidence additions to an awaiting-evidence workflow.
Reject unknown options and incompatible resume requests.

### 5.4 Human decision semantics

- Always require human review before a workflow result is treated as final.
- Accept uses the validated Worker output; Partially Accept requires a
  non-empty corrected output and a reason; Reject produces no final output.
- A decision can be recorded only once. Repeated or out-of-order decisions
  return a clear conflict response.
- Do not allow acceptance of a workflow whose Planner/Worker/Reviewer failed
  or whose result failed schema validation. Permit rejection and retain the
  error record for review.
- Record decision value, timestamp, reason if supplied, and caller-provided
  reviewer identity. Make clear that caller identity is audit metadata unless
  an authentication mechanism verifies it.
- Distinguish a technically successful negative review from an agent failure.
  A human may override a negative Reviewer verdict only with a recorded reason.

### 5.5 Persistence, history, and audit

- Preserve workflow state across server restarts, including pending human
  decisions.
- Keep the workflow history and coordination/audit logs required by the brief.
  Each event should include workflow ID, feature, timestamp, stage/event,
  outcome, duration where relevant, and correlation data.
- Record lifecycle events such as start, agent start/finish/failure, handoff,
  resumed plan, human decision, and workflow failure.
- Use local SQLite as the authoritative store for workflow state and ordered
  audit events. Transactions must protect decision recording and concurrent
  updates. Retain `workflow_history.jsonl` and `coordination_audit.jsonl` as
  reproducible, explicit per-workflow exports, not competing authoritative
  stores. State changes must not rewrite full-history JSONL files.
- Change the normal terminal runner to call the HTTP server. It must not run
  an independent coordinator against the same persistence files.
- Validate persisted records during startup and surface corrupt state
  explicitly; do not silently discard pending workflows.
- Redact secrets and avoid persisting unnecessary personal/financial data.
  Document log location, retention, export, and cleanup for test/report runs.
- Retain bounded evidence snapshots, validated role results, model/prompt and
  contract versions, timings, and human decisions. Operational logs contain
  IDs and outcomes rather than complete evidence bodies. Cleanup is explicit,
  not silent deletion of assessment evidence.
- Store reproducible sample evidence under `docs/release-2/` rather than
  committing developer runtime logs.

### 5.6 Security and reliability

- Validate request types, allowed feature IDs, field sizes, JSON depth and
  decision transitions before invoking a model.
- Avoid exposing raw provider exceptions or internal paths in HTTP responses;
  return safe error codes and log diagnostic details locally.
- Configure service host and port explicitly. For Docker-to-host local access,
  document the required host binding and firewall considerations; do not
  expose an unauthenticated development service to untrusted networks.
- Bind to loopback by default. Require configured bearer-token authentication
  for non-loopback operation; keep tokens in environment configuration and out
  of browser code. Document that tokens over HTTP are not safe on untrusted
  networks and do not provide per-feature or per-human identity isolation.
- Do not enable browser CORS by default. Backend-to-server HTTP is the
  supported application integration path; CORS is not an access-control
  substitute.
- Keep secrets out of source control, prompts, HTTP responses, and audit logs.
- Set explicit connect/read timeouts, maximum request duration, and bounded
  retries. Ensure a failed agent stage leaves a recoverable, auditable record.
- Add request correlation IDs and structured logs suitable for a terminal
  multi-service demo.
- Initially admit one active model workflow and return a documented retryable
  busy response at capacity. Keep workflow reads and human decisions available
  independently of model capacity.
- Define a future authenticated deployment profile before any optional cloud
  bonus deployment. The baseline cloud deployment must leave this service
  disabled and must not point to a nonexistent local host service.

### 5.7 Deployment and operation

Required local deployment:

- Install the server's minimal dependencies in an isolated Python
  environment.
- Start the server with a documented command and configuration; it remains
  a host process, not a Compose service.
- Ensure the local AI runtime and selected models are available before running
  model-backed workflow tests.
- Provide health/readiness checks, startup instructions, stop/restart
  behavior, log locations, and troubleshooting for port, model, and
  persistence issues.
- Distinguish process liveness from readiness to start model workflows.
  Ollama/model unavailability must not prevent access to saved workflows or
  eligible human decisions.
- Demonstrate HTTP use and terminal use against the same server and persisted
  workflow history.

Cloud baseline:

- Keep the Multi-Agent Server disabled in the required cloud deployment.
- Preserve a safe disabled configuration and health reporting in any
  supported backend configuration, without making cloud backends depend on
  `host.docker.internal`.

Optional cloud bonus:

- Treat cloud server deployment as a separate, gated extension after baseline
  cloud CRUD deployment works.
- Before enabling it, provide deployable infrastructure, private/authenticated
  service-to-service access, secret management, TLS, protected persistence,
  logging/retention, and validation evidence. Do not claim cloud Multi-Agent
  bonus based only on a local server or a configuration flag.

## 6. Implementation phases and completion gates

### Phase 1 — Contract and scope freeze

- Write/locate the shared Release 2 contract/ADR in `docs/release-2/shared/`.
- Resolve discrepancies between the contract, current tests, README, and
  branch behavior.
- Freeze request/response schemas, statuses, decisions, failure semantics,
  feature identifiers, and persistence/audit events.
- Preserve compatibility with the existing Bills client wherever possible.
  Document any necessary migration for its owner rather than changing
  individual feature code within this work.

**Gate:** contract examples and schema tests pass; feature owners can build
clients against the documented interface without needing server changes for
their individual business logic.

### Phase 2 — Real role agents

- Add external Planner, Worker, and Reviewer prompts.
- Add a shared Ollama client with per-role model configuration, bounded
  timeouts, and structured-output validation.
- Replace stub behavior with role implementations and safe, explicit failure
  results.
- Add unit tests using a fake model client for valid output, malformed JSON,
  schema violations, timeouts, unavailable models, unsupported claims, and
  evidence-free tasks.

**Gate:** each role has independently tested inputs/outputs; the full sequence
uses Planner output in Worker and Reviewer inputs; no error path returns a
success-shaped fabricated answer.

### Phase 3 — Harden coordinator and state

- Implement the decision rules and failed-stage behavior above.
- Make history/audit persistence and state transitions safe for concurrent
  requests and process restarts.
- Add validation for request limits, plan reuse, duplicate decisions, unknown
  IDs, corrupt state, and log-write failures.
- Ensure logs contain useful coordination evidence without sensitive content.

**Gate:** automated tests prove ordered stages, exactly-once decisions,
recovery, audit completeness, and safe failure behavior.

### Phase 4 — Generic HTTP and terminal experience

- Keep the server framework thin and use the same coordinator for HTTP and
  terminal workflows.
- Add a documented contract version, stable error responses, request
  correlation, readiness checks, and operational status.
- Update the terminal runner to support representative requests and human
  decisions, including corrected output, without bypassing validation.
- Provide generic sample requests for each of the five feature identifiers;
  do not add feature-specific service calls or data mutations.

**Gate:** all supported feature IDs pass the same generic workflow contract
tests via HTTP and terminal paths.

### Phase 5 — Future-client readiness and documentation

- Publish integration guidance and examples for a backend client: configure
  service URL/timeouts, submit task/evidence, persist workflow ID, poll/fetch
  status, record human decision, map errors, and keep feature writes in the
  owning backend.
- Document configuration, local setup, model requirements, server lifecycle,
  health checks, logs, security limitations, and failure recovery.
- Mark the Bills integration as one existing example only; do not expand the
  server work into other feature implementations.
- Provide a machine-readable report/evidence format consumable by the separate
  agentic-loop Multi-Agent Review mode.

**Gate:** a feature owner can implement their adapter from docs and examples
without modifying the shared server contract or importing server internals.

### Phase 6 — Integrated local server demonstration and evidence

- Run the full Docker Compose application while running AI-Mode, MCP, RAG,
  Multi-Agent Server, and the agentic loop as separate host processes, as
  required by the brief.
- Demonstrate model-backed Planner → Worker → Reviewer and a human
  Accept/Partially Accept/Reject decision from the terminal.
- Demonstrate the HTTP contract with a generic client and show how a backend
  can reach the host-run service. Individual feature UI/backend integrations
  remain for their owners.
- Capture workflow history, coordination/audit logs, health output, and
  agentic-loop review evidence for the technical report.

**Gate:** a clean-machine setup following the README can run the service,
complete workflows, survive restart with pending decisions, and reproduce the
documented evidence.

### Phase 7 — Cloud baseline and optional bonus boundary

- Verify standard cloud deployment keeps the Multi-Agent capability off and
  leaves core frontend/backend/database/CRUD behavior operational.
- Confirm cloud configuration does not claim Multi-Agent availability unless
  an actual reachable service has been deployed.
- If and only if the group chooses the bonus, implement a separately gated,
  secured cloud deployment and provide successful workflow demonstration and
  evidence.

**Gate:** baseline cloud settings are demonstrably disabled. Bonus status is
claimed only after secure, working cloud operation is demonstrated.

## 7. Test and evidence matrix

| Area | Required evidence |
| --- | --- |
| Role agents | Unit tests with fake model responses; schema, timeout, unavailable-model, and malformed-output cases |
| Coordinator | Ordered Planner → Worker → Reviewer flow; evidence flow; failures; human decision rules; one-decision-only behavior |
| Persistence/audit | Restart recovery, concurrent write safety, complete event sequence, corrupt-log/write failure handling |
| HTTP API | Contract tests for each route, feature identifier, validation error, status code, safe error body, and request limits |
| Terminal runner | Workflow and decision commands produce the same validated history and audit events as HTTP |
| Model-backed local smoke test | A real local Ollama run for all three roles, with model names, timings, and final human decision recorded |
| Feature-neutrality | Same generic workflow contract exercised with each of the five supported feature IDs and representative caller-provided evidence |
| Security | Local pre-commit Ruff security rules, detect-secrets, and pip-audit (or approved equivalents); results retained |
| Shared review | Multi-Agent Review mode processes actual workflow evidence and emits a review plus validation log; agentic-loop code remains its own workstream |
| Deployment | Reproducible host-process setup, health check, full local application topology, clean restart, and documented logs |
| Cloud baseline | Evidence AI-Mode, MCP, RAG and Multi-Agent are disabled while core cloud CRUD remains operational |

## 8. Definition of done for the shared server

The server-only work is complete when:

1. All three roles call configured models and return schema-validated,
   distinct role results grounded in supplied evidence.
2. The coordinator enforces Planner → Worker → Reviewer → Human Review with
   explicit, recorded decision semantics and no automatic final approval.
3. The versioned contract, terminal runner, health/status behavior, error
   handling, persistence, and audit trail are documented and tested.
4. The service runs locally as a non-containerised process alongside the
   Compose application and works for generic requests from every feature
   identifier.
5. Feature owners can integrate through the documented API without changing
   the shared service for feature-specific rules or persistence.
6. The local demonstration and evidence are reproducible, and the required
   cloud baseline remains disabled for AI services.
7. No Budgets-specific application work or other individual feature
   integration has been included in this shared-server scope.

## 9. Development tracking

Use this plan as the shared-server checklist during implementation. Update its
phase gates and evidence matrix only when Release 2 requirements or the shared
server contract materially change. Record implementation outcomes and
test/evidence locations in the technical report; do not use this plan as a
substitute for runtime evidence.

### Local implementation progress - 18 October 2026

The initial non-cloud implementation now includes:

- A documented contract at
  `docs/release-2/shared/multi-agent-server-contract.md`, reconciled against
  the proposed ADR in PR #217 without modifying that separate PR.
- Real sequential Ollama roles, external prompts, schema validation, bounded
  repair, evidence-reference checks and explicit failure records.
- SQLite workflow/history/audit transactions, atomic decisions, JSONL exports,
  interrupted-run recovery and single-process data-directory locking.
- HTTP-based terminal access, strict request/decision validation, loopback
  defaults, non-loopback bearer-token requirements, workload admission and
  liveness/readiness reporting.
- Independent shared-server CI and automated contract/runtime tests using
  fake model responses, plus a successful real three-role Ollama run through
  HTTP with a terminal-recorded correction and evidence export.
- Setup, configuration, security limitations and feature-owner migration
  guidance in the shared server README. No feature code, Compose topology,
  cloud infrastructure or feature workflows were modified.

The real smoke run used synthetic evidence and completed the three stages in
approximately 86 seconds on the current machine. This is an observation, not
a performance guarantee or proof of model quality across all domain tasks.

Outstanding group/operational gates remain:

- Feature-owner acknowledgement of stricter authentication, reason and timeout
  requirements before integrating; the existing Bills stub client is unchanged.
- Genuine person-led review and feature-owner deployment/integration evidence;
  automated synthetic decision exercises are not human approval.
- Full multi-terminal, non-containerised local AI deployment evidence. The
  existing Compose configuration still contains Ollama; that separate group
  topology needs reconciliation with the brief without expanding this work
  into deployment changes.
- Agentic-loop Multi-Agent Review implementation and report/showcase evidence
  produced by that separate workstream.
- Required pre-commit security tooling/reports and per-student endpoint CI
  evidence; shared-server CI is not a substitute.
- Baseline cloud validation and any optional cloud bonus work, both outside
  this non-cloud implementation.

### Shared-server validation and handoff follow-up - 18 October 2026

Completed within the shared-server-only scope:

- Removed automatic full-history JSONL regeneration from state/decision writes.
  SQLite remains authoritative; indexed HTTP export and terminal
  `--export <id> --output-dir <new-directory>` produce the same named evidence
  files explicitly without overwriting an existing directory.
- Added an opt-in real-model evaluator and deterministic evaluator tests.
  It runs different text, exact structured arithmetic, missing-evidence,
  conflicting-source and unsupported-request scenarios across all five
  metadata labels. It never records a human decision.
- Retained the initial failed evaluation rather than concealing it.
  Its empty-evidence Planner invented reference names for desired information;
  runtime validation correctly failed the workflow and prevented acceptance.
  Fixed the root cause by constraining generation-time source-reference
  arrays, clarifying the Planner prompt and preserving post-generation checks.
- Repeated the complete real-model evaluation after the fix: all five
  automated scenario gates and an isolated real Reviewer probe passed.
  The probe deliberately injects an incorrect Worker amount/identifier and
  is not represented as an end-to-end model-generated mistake.
- Verified 60 tests on Windows Python 3.14 and Linux Python 3.12 using an
  ephemeral container and read-only code mount, plus local Ruff checks.
  This is not a native Windows Python 3.12 setup or completed remote CI run.
- Verified host-run Waitress, authenticated Docker-to-host health/retrieval,
  unauthenticated 401, restart retention of pending and recorded decisions,
  terminal access, Accept/Partially Accept/Reject mechanics and repeat-decision
  409. Decisions are explicitly labelled automated synthetic exercises,
  not real human approval. Temporary test servers/containers were stopped.
- Saved synthetic requests, full stage snapshots, ordered events, model/prompt
  metadata, timings and result/deployment checks under
  `docs/release-2/shared/multi-agent-evidence/`. Checked the JSON/JSONL shapes
  and credential/local-identity markers before retaining the evidence.
- Added the owner handoff at
  `docs/release-2/shared/multi-agent-server-handoff.md`, including token forwarding,
  timeout/error/unknown-outcome handling, source IDs, human reasons/overrides
  and feature-side validation before applying accepted proposals.

The corrected evaluation's workflows took approximately 43-165 seconds.
All real Reviewer verdicts were `approve_with_changes`; correct numeric gates
do not imply complete task compliance or reliable Reviewer judgments. The
structured example's readable explanation remained deficient and was flagged.
Real person-led review, actual feature integrations and the remaining group
assessment work above are still required.

Inference used the existing local Ollama container; the server itself ran on
the host. Full host-run AI/MCP/RAG deployment remains a separate group gate.
No feature code (including `ethan/budgets`), Compose topology or cloud
infrastructure was changed, and all changes remain unstaged/uncommitted.
