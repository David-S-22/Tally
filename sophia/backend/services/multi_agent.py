"""Bills workflows on the shared Multi-Agent Server (Release 2).

Two workflows, both Planner -> Worker -> Reviewer on the server, then a human
decision here:

* dispute letter — evidence from the same MCP/RAG helpers the single-model
  letter uses; Accept or Partially Accept stores the final letter as the next
  dispute draft, Reject stores nothing.
* chat suggestion — the proposal Ask Tally just made, re-derived by the three
  agents from the user's words and the bill rows; Accept applies it through
  the existing suggestions flow, after the same vetting a chat proposal gets.

A workflow takes three sequential model calls (tens of seconds on a GPU,
minutes on CPU), and the server runs one at a time. So a run is started in a
background thread and the UI polls ``run_status`` while the panel shows which
agent is working. Bills' own runs go through a one-worker executor, which
serialises them in front of the server's single slot instead of tripping its
``workflow_busy`` refusal. Nothing is written to the Bills database before the
human decides.
"""
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from sophia.backend import config
from sophia.backend.ai import agent_tasks, dispute_prompt
from sophia.backend.clients import bills_db, multi_agent
from sophia.backend.services import chat as chat_service
from sophia.backend.services import disputes as disputes_service
from sophia.backend.services.errors import MODE_MESSAGES, ModeError, NotFound, ServiceError

logger = logging.getLogger(__name__)

KINDS = ("dispute_letter", "chat_suggestion")
STAGES = ("planner", "worker", "reviewer")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="multi-agent")
_runs = {}
_lock = threading.Lock()


def _submit(fn, *args):
    """Run fn in the background; tests replace this with an inline call."""
    _executor.submit(fn, *args)


# --- starting ----------------------------------------------------------------

def start_dispute_letter(dispute_id):
    """Gather the dispute's evidence now (seconds), run the agents in the background; the run id to poll."""
    if not config.MULTI_AGENT_ENABLED:
        raise ModeError("multi_agent_disabled")
    dispute, bill = _dispute_and_bill(dispute_id)
    opened_on = disputes_service._opened_on(dispute)
    evidence = _dispute_evidence(bill, dispute["reason"], opened_on)
    task = agent_tasks.dispute_letter(bill, dispute["reason"])
    context = {"dispute_id": dispute_id, "bill_id": bill.id, "opened_on": opened_on.isoformat(), "demo_today": config.DEMO_TODAY.isoformat()}
    return _start("dispute_letter", dispute_id, task, evidence, context)


def start_chat_suggestion(suggestion_id, message, preview, bills):
    """Re-derive a pending Ask Tally suggestion with the three agents; the run id, or None when the mode is off."""
    if not config.MULTI_AGENT_ENABLED:
        return None
    task = agent_tasks.chat_suggestion(message, preview)
    evidence = [{"source": "user:message", "summary": message[:1000], "data": {"suggestion_id": suggestion_id}}]
    for bill in bills[:24]:
        evidence.append({
            "source": f"bills-db:bill:{bill.id}",
            "summary": f"Bill {bill.id} {bill.name} ({bill.merchant}): ${bill.amount_cents / 100:.2f} {bill.cadence}, next {bill.next_billing_date}, paid by {bill.payment_method or 'unknown'}"
            + (f", ends {bill.end_date}" if bill.end_date else ""),
            "data": {"id": bill.id},
        })
    context = {"suggestion_id": suggestion_id, "demo_today": config.DEMO_TODAY.isoformat()}
    return _start("chat_suggestion", suggestion_id, task, evidence, context)


def _start(kind, subject_id, task, evidence, context):
    run_id = str(uuid.uuid4())
    run = {
        "run_id": run_id, "kind": kind, "subject_id": subject_id, "state": "running",
        "started_at": time.time(), "finished_at": None, "evidence": evidence, "record": None, "error": None, "workflow_id": None,
    }
    with _lock:
        _runs[run_id] = run
    _submit(_execute, run_id, task, evidence, context)
    return run_id


def _execute(run_id, task, evidence, context):
    run = _runs[run_id]
    try:
        record = multi_agent.run_workflow(task, evidence, context=context)
        with _lock:
            run.update(record=record, workflow_id=record["workflow_id"], state="failed" if multi_agent.failed(record) else "done")
    except ServiceError as error:
        with _lock:
            run.update(state="error", error=error.code or "multi_agent_error")
    except Exception:
        logger.exception("multi-agent run %s crashed", run_id)
        with _lock:
            run.update(state="error", error="multi_agent_error")
    finally:
        with _lock:
            run["finished_at"] = time.time()


# --- polling -----------------------------------------------------------------

def run_status(run_id):
    """What the panel shows while it waits: state, elapsed seconds, the summary when there is a record."""
    run = _runs.get(run_id)
    if run is None:
        raise NotFound("run not found")
    elapsed = int((run["finished_at"] or time.time()) - run["started_at"])
    out = {k: run[k] for k in ("run_id", "kind", "subject_id", "state", "workflow_id", "error")}
    out["elapsed_s"] = elapsed
    out["error_message"] = MODE_MESSAGES.get(run["error"], MODE_MESSAGES["multi_agent_error"]) if run["error"] else None
    out["summary"] = summarise(run["record"]) if run["record"] else None
    return out


def latest_run(kind, subject_id):
    """The most recent run for a dispute or suggestion, or None (lets a reloaded panel pick up a run in flight)."""
    with _lock:
        runs = [r for r in _runs.values() if r["kind"] == kind and r["subject_id"] == subject_id]
    return max(runs, key=lambda r: r["started_at"]) if runs else None


def summarise(record):
    """The three agent results reduced to what the panel shows."""
    planner = record.get("planner_result") or {}
    worker = record.get("worker_result") or {}
    reviewer = record.get("reviewer_result") or {}
    stages = {role: (record.get(f"{role}_result") or {}).get("status", "pending") for role in STAGES}
    return {
        "stages": stages,
        "plan": planner.get("plan") or [],
        "missing_information": planner.get("missing_information") or [],
        "output": worker.get("output") or "",
        "structured": worker.get("structured"),
        "evidence_used": worker.get("evidence_used") or [],
        "limitations": worker.get("limitations") or [],
        "verdict": reviewer.get("verdict"),
        "risks": reviewer.get("risks") or [],
        "recommendations": reviewer.get("recommendations") or [],
        "review": reviewer.get("review") or "",
        "failed": multi_agent.failed(record),
        "failed_agent": next((f"{r}_agent" for r in STAGES if stages[r] in ("error", "timeout")), None),
        "allowed_decisions": (record.get("human_decision") or {}).get("allowed_values") or [],
        "decision": (record.get("human_decision") or {}).get("decision"),
        "duration_ms": record.get("duration_ms"),
    }


# --- deciding ----------------------------------------------------------------

def _check_decision(record, decision, corrected_output, reason):
    """The server's rules, checked here first so the user gets a plain message instead of a 400/409."""
    if decision not in multi_agent.DECISIONS:
        raise ServiceError("decision must be Accept, Partially Accept or Reject")
    if multi_agent.failed(record) and decision != "Reject":
        raise ModeError("multi_agent_failed")
    if decision == "Partially Accept":
        if corrected_output in (None, "", {}):
            raise ServiceError("Partially Accept needs your corrected version")
        if not (reason or "").strip():
            raise ServiceError("Partially Accept needs a short reason")
    verdict = (record.get("reviewer_result") or {}).get("verdict")
    if decision == "Accept" and verdict != "approve" and not (reason or "").strip():
        raise ServiceError("The reviewer did not approve; give a reason to accept anyway")


def decide_dispute_letter(dispute_id, workflow_id, decision, corrected_output=None, reason=None):
    """Record the decision; on Accept or Partially Accept the final letter becomes a new dispute draft."""
    dispute, bill = _dispute_and_bill(dispute_id)
    record = _owned_record(workflow_id, "dispute_letter", dispute_id)
    _check_decision(record, decision, corrected_output, reason)
    if decision != "Reject":
        # The server's final_output is `structured` when the Worker set one, else `output`; a letter
        # must be text. Refuse before the decision is committed (it can be recorded only once).
        candidate = corrected_output if decision == "Partially Accept" else (record.get("worker_result") or {}).get("output")
        if (record.get("worker_result") or {}).get("structured") is not None:
            # The server only accepts a correction of the same type as the original (contract 1.0).
            raise ServiceError("The agents returned a structured object, not a letter; reject it and try again")
        if not isinstance(candidate, str) or not candidate.strip():
            raise ServiceError("There is no letter text to save; reject it or try again")
    record = multi_agent.record_decision(workflow_id, decision, corrected_output=corrected_output, reason=reason, decided_by="bills-ui")
    _decided(workflow_id, record)
    letter = record["human_decision"].get("final_output")
    if isinstance(letter, str) and letter.strip():
        draft = dispute_prompt.enforce_payment_method_step(
            {"letter_text": letter, "steps": [], "escalation": list(dispute_prompt.ESCALATION_DEFAULT), "fallback": False},
            bill,
        )
        draft["evidence"] = _stored_evidence(record)
        created = bills_db.create_dispute_draft(
            dispute_id,
            {"letter_text": draft["letter_text"], "steps_json": {**disputes_service.steps_json(draft), "workflow_id": workflow_id}},
        )
        created["draft"] = draft
        record["draft"] = created
    return record


def decide_chat_suggestion(suggestion_id, workflow_id, decision, corrected_output=None, reason=None):
    """Record the decision; Accept applies the agents' proposal through the suggestions flow, Reject rejects it."""
    row = bills_db.get_suggestion(suggestion_id)
    if row is None:
        raise NotFound("suggestion not found")
    if row["status"] != "pending":
        raise ServiceError(f"This suggestion was already {row['status']}; the agents' decision was not recorded")
    record = _owned_record(workflow_id, "chat_suggestion", suggestion_id)
    _check_decision(record, decision, corrected_output, reason)
    if decision != "Reject":
        proposal = corrected_output if decision == "Partially Accept" else (record.get("worker_result") or {}).get("structured")
        if not isinstance(proposal, dict) or not proposal.get("op") or not proposal.get("entity"):
            raise ServiceError("The agents did not produce an appliable change; reject it or tell Tally the missing details")
        fields, problem = chat_service._vet_proposal(
            {"op": proposal["op"], "entity": proposal["entity"], "id": proposal.get("id"), "fields": proposal.get("fields") or {}}
        )
        if problem:
            raise ServiceError(problem)
        vetted = {"op": proposal["op"], "entity": proposal["entity"], "id": proposal.get("id"), "fields": fields}
    record = multi_agent.record_decision(workflow_id, decision, corrected_output=corrected_output, reason=reason, decided_by="bills-ui")
    _decided(workflow_id, record)
    if decision == "Reject":
        record["applied"] = chat_service.reject_suggestion(suggestion_id)
    else:
        record["applied"] = chat_service.approve_suggestion(suggestion_id, proposal=vetted)
    return record


# --- helpers -----------------------------------------------------------------

KEEP_DECIDED_RUNS = 20


def _owned_record(workflow_id, kind, subject_id):
    """The server's record for this workflow, only if Bills started it for this dispute or suggestion."""
    record = multi_agent.get_workflow(workflow_id)
    key = "dispute_id" if kind == "dispute_letter" else "suggestion_id"
    if record.get("feature") != "bills" or (record.get("context") or {}).get(key) != subject_id:
        raise NotFound("workflow not found for this " + key.split("_")[0])
    return record


def _decided(workflow_id, record):
    """Keep the decided record on its run so the panel stops offering the decision; prune old decided runs."""
    with _lock:
        for run in _runs.values():
            if run["workflow_id"] == workflow_id:
                run["record"] = record
        decided = sorted((r for r in _runs.values() if r["record"] and r["record"].get("human_decision", {}).get("decision")),
                         key=lambda r: r["started_at"])
        for run in decided[:-KEEP_DECIDED_RUNS]:
            _runs.pop(run["run_id"], None)


def _dispute_and_bill(dispute_id):
    dispute = bills_db.get_dispute(dispute_id)
    if dispute is None:
        raise NotFound("dispute not found")
    bill_row = bills_db.get_bill(dispute["bill_id"])
    if bill_row is None:
        raise NotFound("bill not found")
    return dispute, bills_db.row_to_bill(bill_row)


def _dispute_evidence(bill, reason, opened_on):
    """Contract-shaped evidence from the same MCP/RAG helpers the single-model letter uses."""
    rows, tools = disputes_service.bank_evidence(bill, opened_on)
    policy = disputes_service.policy_evidence(reason)
    evidence = [{
        "source": f"bills-db:bill:{bill.id}",
        "summary": f"Bill {bill.id} {bill.name} ({bill.merchant}): ${bill.amount_cents / 100:.2f} {bill.cadence}, paid by {bill.payment_method or 'unknown'}, next {bill.next_billing_date}",
        "data": {"id": bill.id},
    }]
    if rows is not None:
        evidence.append({
            "source": "mcp:" + disputes_service.COMPARE_TOOL,
            "summary": "; ".join(rows) if rows else "No bank charges matched this bill in the last 90 days.",
            "data": {"bill_id": bill.id, "rows": rows, "tools": tools},
        })
    if policy:
        evidence.append({"source": "rag:billing", "summary": " ".join(policy)[:1500], "data": {"rows": policy}})
    return evidence


def _stored_evidence(record):
    """What the dispute panel shows for a workflow letter: the evidence the worker used plus the review."""
    worker = record.get("worker_result") or {}
    reviewer = record.get("reviewer_result") or {}
    evidence = record.get("evidence") or []
    return {
        "bank": [e["summary"] for e in evidence if e["source"].startswith("mcp:")],
        "policy": [e["summary"] for e in evidence if e["source"].startswith("rag:")],
        "tools": worker.get("evidence_used") or [],
        "review": {"verdict": reviewer.get("verdict"), "risks": reviewer.get("risks") or []},
    }
