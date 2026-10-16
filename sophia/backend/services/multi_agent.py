"""The Bills dispute-letter workflow on the shared Multi-Agent Server.

Two steps, matching the contract: ``start`` gathers the dispute's evidence
through the existing MCP/RAG helpers and asks the server for a plan, a
drafted letter and a review; ``decide`` forwards the human's decision and, when
there is a final letter, stores it as the next dispute draft. Nothing is
written to the Bills database before the decision.
"""
from sophia.backend.ai import dispute_prompt
from sophia.backend.clients import bills_db, multi_agent
from sophia.backend.services import disputes as disputes_service
from sophia.backend.services.errors import NotFound, ServiceError

TASK = "Draft a dispute letter for bill {bill_id} ({merchant}, {reason})."


def start(dispute_id):
    """Run Planner -> Worker -> Reviewer for a dispute; the server's record, awaiting the human."""
    dispute, bill = _dispute_and_bill(dispute_id)
    opened_on = disputes_service._opened_on(dispute)
    evidence = _evidence(bill, dispute["reason"], opened_on)
    task = TASK.format(bill_id=bill.id, merchant=bill.merchant, reason=dispute["reason"])
    context = {"dispute_id": dispute_id, "opened_on": opened_on.isoformat(), "bank_facts_only": True}
    return multi_agent.run_workflow(task, evidence, context=context)


def decide(dispute_id, workflow_id, decision, corrected_output=None, reason=None):
    """Record the human decision; on Accept or Partially Accept the final letter becomes a new draft."""
    dispute, bill = _dispute_and_bill(dispute_id)
    if decision == "Partially Accept" and not (corrected_output or "").strip():
        raise ServiceError("corrected_output is required for Partially Accept")
    record = multi_agent.record_decision(workflow_id, decision, corrected_output=corrected_output, reason=reason, decided_by="bills-ui")
    letter = record["human_decision"].get("final_output")
    if letter:
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


def _dispute_and_bill(dispute_id):
    dispute = bills_db.get_dispute(dispute_id)
    if dispute is None:
        raise NotFound("dispute not found")
    bill_row = bills_db.get_bill(dispute["bill_id"])
    if bill_row is None:
        raise NotFound("bill not found")
    return dispute, bills_db.row_to_bill(bill_row)


def _evidence(bill, reason, opened_on):
    """Contract-shaped evidence from the same MCP/RAG helpers the single-model letter uses."""
    rows, tools = disputes_service.bank_evidence(bill, opened_on)
    policy = disputes_service.policy_evidence(reason)
    evidence = []
    if rows is not None:
        evidence.append({
            "source": "mcp:" + disputes_service.COMPARE_TOOL,
            "summary": "; ".join(rows) if rows else "No bank charges matched this bill in the last 90 days.",
            "data": {"bill_id": bill.id, "rows": rows, "tools": tools},
        })
    if policy:
        evidence.append({"source": "rag:billing", "summary": " ".join(policy), "data": {"rows": policy}})
    return evidence


def _stored_evidence(record):
    """What the dispute panel shows for a workflow letter: the evidence the worker used plus the review."""
    worker = record.get("worker_result") or {}
    reviewer = record.get("reviewer_result") or {}
    bank = [e["summary"] for e in record.get("evidence", []) if e["source"].startswith("mcp:")]
    policy = [e["summary"] for e in record.get("evidence", []) if e["source"].startswith("rag:")]
    return {
        "bank": bank,
        "policy": policy,
        "tools": worker.get("evidence_used", []),
        "review": {"verdict": reviewer.get("verdict"), "risks": reviewer.get("risks", [])},
    }
