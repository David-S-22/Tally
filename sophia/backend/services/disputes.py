"""Dispute CRUD and AI-drafted letters, shared by /api/disputes and /ui/disputes; letters cite bank facts read through MCP and policy sentences read through RAG when they are on."""
import re
from datetime import date, timedelta

from sophia.backend import config
from sophia.backend.ai import dispute_prompt, guard
from sophia.backend.ai.schemas import validate_dispute_draft
from sophia.backend.clients import bills_db
from sophia.backend.engine import money
from sophia.backend.engine.status import _day_month
from sophia.backend.services import evidence, tools as tools_service
from sophia.backend.services.evidence import SOURCE_PATTERN
from sophia.backend.services.errors import NotFound, ServiceError

DISPUTE_STATUSES = ("draft", "sent", "resolved")
EVIDENCE_WINDOW_DAYS = 90
EVIDENCE_ROWS = 3
POLICY_ROWS = 3
POLICY_K = 20
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
POLICY_WORDS = re.compile(r"\b(?:due within|late|penalty|fees?|refunds?|disputes?|accepted|business days|notice)\b", re.IGNORECASE)
COMPARE_TOOL = "compare_bill_with_bank_charges"
CONFIRMED_ANOMALIES_TOOL = "get_transactions_with_confirmed_anomalies"
CONFIRMED_NOTE = ", flagged by Spending Alerts and confirmed by you"


def bank_evidence(bill, opened_on):
    """(rows, tools) for a letter, read through MCP: up to EVIDENCE_ROWS of the merchant's charges in the EVIDENCE_WINDOW_DAYS before opened_on, the ones the user confirmed as suspicious first, and the tools that answered; (None, []) when MCP is off or the comparison fails, and a failed anomalies call only drops the confirmed notes."""
    if not config.MCP_ENABLED:
        return None, []
    window = {"bill_id": bill.id, "start_date": (opened_on - timedelta(days=EVIDENCE_WINDOW_DAYS)).isoformat(), "end_date": opened_on.isoformat()}
    try:
        compared, _ms = tools_service.call_allowed_tool(COMPARE_TOOL, window)
    except ServiceError:
        return None, []
    tools = [COMPARE_TOOL]
    flagged = []
    try:
        flagged, _ms = tools_service.call_allowed_tool(CONFIRMED_ANOMALIES_TOOL, {})
        tools.append(CONFIRMED_ANOMALIES_TOOL)
    except ServiceError:
        pass
    confirmed = {item["transaction"]["id"] for item in flagged}
    charges = sorted(compared.get("charges") or [], key=lambda c: c["date"], reverse=True)
    charges = sorted(charges, key=lambda c: c["id"] not in confirmed)
    rows = [
        f"{_day_month(date.fromisoformat(c['date']))} {bill.merchant} {money.format_actual(c['amount_cents'])}{CONFIRMED_NOTE if c['id'] in confirmed else ''}"
        for c in charges[:EVIDENCE_ROWS]
    ]
    return rows, tools


def policy_evidence(reason):
    """Up to POLICY_ROWS whole policy sentences as "<source>: <sentence>" from the non-bill files RAG returns within RAG_LOW, closest chunk first; [] when none qualify (most non-fee reasons sit beyond RAG_LOW), None when MCP or RAG is off or retrieval fails."""
    if not (config.MCP_ENABLED and config.RAG_ENABLED):
        return None
    try:
        found, _ms = evidence.chunks(reason, POLICY_K)
    except ServiceError:
        return None
    sentences = []
    for chunk in sorted(found, key=lambda c: c["distance"]):
        if not chunk["source"] or SOURCE_PATTERN.match(chunk["source"]) or chunk["distance"] > config.RAG_LOW:
            continue
        for line in re.sub(r"[ \t]*\n(?=[a-z])", " ", chunk["text"]).splitlines():
            line = line.strip().removeprefix("- ").lstrip("#").strip()
            if line.endswith(".") and POLICY_WORDS.search(line) and "@" not in line and "$" not in line and "billed on" not in line.lower() and not ISO_DATE.search(line):
                sentences.append(f"{chunk['source']}: {line}")
    return sentences[:POLICY_ROWS]


def steps_json(draft):
    """The JSON blob stored with a draft: its steps, escalation path and the evidence the letter was drawn from."""
    return {"steps": draft["steps"], "escalation": draft["escalation"], "evidence": draft.get("evidence")}


def _opened_on(dispute):
    """The day a dispute was opened, or the demo clock when the row carries none."""
    opened = dispute.get("opened_at")
    return date.fromisoformat(str(opened)[:10]) if opened else config.DEMO_TODAY


def draft_for_bill(bill_row, reason, previous_letter=None, edited_letter=None, feedback=None, opened_on=None):
    bill = bills_db.row_to_bill(bill_row)
    payments = [bills_db.row_to_payment(r) for r in bills_db.list_bill_payments(bill.id)]
    evidence, bank_tools = bank_evidence(bill, opened_on or config.DEMO_TODAY)
    policy = policy_evidence(reason)
    fallback = dispute_prompt.fallback_draft(bill, reason)
    data = guard.run(
        config.DRAFT_MODEL,
        lambda error: dispute_prompt.build(
            bill,
            reason,
            payments=payments,
            evidence=evidence,
            policy=policy,
            previous_letter=previous_letter,
            edited_letter=edited_letter,
            feedback=feedback,
            error=error,
        ),
        validate_dispute_draft,
        fallback,
    )
    data = dispute_prompt.enforce_payment_method_step(data, bill)
    data["evidence"] = {
        "bank": evidence or [],
        "policy": policy or [],
        "tools": bank_tools + ([tools_service.RETRIEVAL_TOOL] if policy is not None else []),
    }
    return data


def list_disputes():
    return bills_db.list_disputes()


def create_dispute(bill_id, reason):
    if not bill_id:
        raise ServiceError("bill_id is required")
    if not reason:
        raise ServiceError("reason is required")
    bill_row = bills_db.get_bill(bill_id)
    if bill_row is None:
        raise NotFound("bill not found")
    dispute = bills_db.create_dispute({"bill_id": bill_id, "reason": reason, "opened_at": config.DEMO_TODAY.isoformat()})
    draft = draft_for_bill(bill_row, reason, opened_on=_opened_on(dispute))
    bills_db.create_dispute_draft(
        dispute["id"],
        {"letter_text": draft["letter_text"], "steps_json": steps_json(draft)},
    )
    dispute["draft"] = draft
    return dispute


def get_dispute(dispute_id):
    dispute = bills_db.get_dispute(dispute_id)
    if dispute is None:
        raise NotFound("dispute not found")
    return dispute


def update_dispute(dispute_id, payload):
    if bills_db.get_dispute(dispute_id) is None:
        raise NotFound("dispute not found")
    if "status" in payload and payload["status"] not in DISPUTE_STATUSES:
        raise ServiceError(f"status must be one of {', '.join(DISPUTE_STATUSES)}")
    return bills_db.update_dispute(dispute_id, payload)


def update_status(dispute_id, status):
    return update_dispute(dispute_id, {"status": status})


def delete_dispute(dispute_id):
    return bills_db.delete_dispute(dispute_id)


def list_drafts(dispute_id):
    return bills_db.list_dispute_drafts(dispute_id)


def regenerate(dispute_id, edited_letter=None, feedback=None):
    dispute = bills_db.get_dispute(dispute_id)
    if dispute is None:
        raise NotFound("dispute not found")
    bill_row = bills_db.get_bill(dispute["bill_id"])
    bill = bills_db.row_to_bill(bill_row)
    if edited_letter and not feedback:
        draft = dispute_prompt.enforce_payment_method_step(
            {
                "letter_text": edited_letter,
                "steps": [],
                "escalation": list(dispute_prompt.ESCALATION_DEFAULT),
                "fallback": False,
            },
            bill,
        )
    else:
        existing_drafts = bills_db.list_dispute_drafts(dispute_id)
        previous_letter = existing_drafts[-1]["letter_text"] if existing_drafts else None
        draft = draft_for_bill(
            bill_row, dispute["reason"], previous_letter=previous_letter, edited_letter=edited_letter, feedback=feedback,
            opened_on=_opened_on(dispute),
        )
    created = bills_db.create_dispute_draft(
        dispute_id,
        {"letter_text": draft["letter_text"], "steps_json": steps_json(draft)},
    )
    created["draft"] = draft
    return created
