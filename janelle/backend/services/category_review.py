"""Multi-agent category review for one Uncategorised transaction.

The backend gathers the evidence, runs the shared Planner, Worker and
Reviewer in a background thread, and applies the human decision through the
database's category-correction endpoint. Runs live in memory for
``AGENT_REQUEST_TTL_SECONDS``, like chat previews.
"""
import threading
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4

from .. import config
from . import multi_agent_client, rag_client, rag_corpus, transaction_source
from .category_grounding import build_question
from .chat_service import ChatError, database_request
from .multi_agent_client import MultiAgentError
from .transaction_orchestrator import log_workflow_event


FEATURE = "transactions"
UNCATEGORISED = "uncategorised"
DECIDED_BY = "transactions-ui"
TASK = (
    "Suggest one category for transaction {id} using only the supplied "
    "evidence. Return structured output as {{\"category\": \"<exact name from "
    "categories:list>\", \"reason\": \"<one sentence citing source IDs>\"}}. "
    "Never choose Uncategorised. If the evidence does not support a "
    "category, return structured null and explain why in output."
)

RUNS = {}
_LOCK = threading.Lock()


def is_uncategorised(transaction, category_names):
    name = category_names.get(transaction.get("category_id"))
    return name is None or name.casefold() == UNCATEGORISED


def load_transaction(db_url, transaction_id):
    """Return ``(transaction, category_names, choosable_categories)``."""
    transaction = database_request(
        "get", f"{db_url}/transactions/{transaction_id}", dict,
    )
    categories = database_request("get", f"{db_url}/categories", list)
    names = {item["id"]: item["name"] for item in categories}
    choosable = [
        item for item in categories
        if item["name"].casefold() != UNCATEGORISED
    ]
    return transaction, names, choosable


def build_request(transaction, category_names, choosable, db_url, run_id):
    """Build the ``POST /workflow`` body; missing sources are named, not faked."""
    merchant = transaction["merchant"]
    unavailable = []
    evidence = [
        {
            "source": f"transaction:{transaction['id']}",
            "summary": (
                f"{transaction['date']}; {merchant}; "
                f"{transaction['description']}; AUD {transaction['amount']}; "
                f"current category {category_names.get(transaction.get('category_id'), 'Uncategorised')}"
            ),
        },
        {
            "source": "categories:list",
            # Names only: a 3b model copies integer ids wrongly.
            "summary": "Valid categories: " + ", ".join(
                item["name"] for item in choosable
            ),
            "data": {"categories": [item["name"] for item in choosable]},
        },
    ]

    try:
        rows = transaction_source.query_transactions(
            db_url,
            {"merchant": merchant},
            context={"request_id": run_id, "phase": "agent_review"},
        )
        rows = [
            row for row in rows
            if row["id"] != transaction["id"]
            and not is_uncategorised(row, category_names)
        ]
        for row in rows[:5]:
            evidence.append({
                "source": f"history:{row['id']}",
                "summary": (
                    f"{row['date']}; {row['merchant']}; {row['description']}; "
                    f"AUD {row['amount']}; category "
                    f"{category_names.get(row['category_id'])}"
                ),
            })
    except ChatError:
        unavailable.append("mcp: unavailable")

    try:
        corrections = database_request(
            "get", f"{db_url}/category-corrections", list,
            params={"merchant": merchant, "limit": 3},
        )
        for item in corrections:
            evidence.append({
                "source": f"correction:{item['id']}",
                "summary": (
                    f"{item['merchant']} was moved from "
                    f"{item['previous_category_name']} to "
                    f"{item['user_category_name']} on {item['corrected_at']}"
                ),
            })
    except ChatError:
        unavailable.append("corrections: unavailable")

    try:
        guide = rag_client.retrieve(
            config.RAG_GUIDE_COLLECTION,
            build_question(transaction),
            config.RAG_GUIDE_TOP_K,
        )
        for item in guide:
            evidence.append({
                "source": f"rag:{item['id']}",
                "summary": item["text"][:300],
            })
    except rag_client.RAGError as error:
        state = "disabled" if error.code == "rag_disabled" else "unavailable"
        unavailable.append(f"rag: {state}")

    return {
        "contract_version": "1.0",
        "feature": FEATURE,
        "task": TASK.format(id=transaction["id"]),
        "evidence": evidence,
        "context": {
            "constraints": "Do not invent facts or apply changes.",
            "unavailable_sources": unavailable,
        },
        "options": {},
    }


def start_run(db_url, transaction_id):
    """Validate the row, build the evidence and start the agents."""
    transaction, names, choosable = load_transaction(db_url, transaction_id)
    if not is_uncategorised(transaction, names):
        raise ChatError(
            "This transaction already has a category.", "already_categorised", 409,
        )
    with _LOCK:
        _expire_runs()
        if any(run["status"] == "running" for run in RUNS.values()):
            raise ChatError(
                "Another agent review is running. Try again when it finishes.",
                "review_running",
                409,
            )
        run_id = uuid4().hex
        run = RUNS[run_id] = {
            "id": run_id,
            "transaction": transaction,
            "categories": choosable,
            "category_names": names,
            "status": "running",
            "created": monotonic(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "workflow": None,
            "decision": None,
            "error": None,
        }
    body = build_request(transaction, names, choosable, db_url, run_id)
    run["thread"] = threading.Thread(
        target=_run_workflow, args=(run, body), daemon=True,
    )
    run["thread"].start()
    return run


def _log(event, run, **fields):
    """One AI_WORKFLOW record; safe IDs and statuses only, no model text."""
    if config.AGENT_LOG_ENABLED:
        log_workflow_event({
            "event": event,
            "run_id": run["id"],
            "transaction_id": run["transaction"]["id"],
            **fields,
        })


def _run_workflow(run, body):
    _log(
        "AGENT_REVIEW_START", run,
        evidence=[item["source"] for item in body["evidence"]],
        unavailable=body["context"]["unavailable_sources"],
    )
    started = monotonic()
    try:
        run["workflow"] = multi_agent_client.create_workflow(body)
        run["status"] = "done"
    except MultiAgentError as error:
        if error.record is not None:
            run["workflow"], run["status"] = error.record, "done"
        elif error.code == "multi_agent_timeout":
            _adopt_recorded_workflow(run)
        else:
            run["status"], run["error"] = "failed", error.message
    except Exception:
        run["status"] = "failed"
        run["error"] = "The agent review failed unexpectedly."
    finally:
        workflow = run["workflow"] or {}
        for agent in ("planner", "worker", "reviewer"):
            result = workflow.get(f"{agent}_result")
            if isinstance(result, dict):
                _log(
                    "AGENT_REVIEW_AGENT", run, agent=agent,
                    status=result.get("status"), model=result.get("model"),
                    confidence=result.get("confidence"), verdict=result.get("verdict"),
                    category=(result.get("structured") or {}).get("category"),
                )
        _log(
            "AGENT_REVIEW_COMPLETE", run,
            workflow_id=workflow.get("workflow_id"),
            workflow_status=workflow.get("status"), status=run["status"],
            error=run["error"], duration_ms=round((monotonic() - started) * 1000),
        )


def _adopt_recorded_workflow(run):
    """After a timeout the server may still have saved the run; never resubmit."""
    source = f"transaction:{run['transaction']['id']}"
    try:
        latest = multi_agent_client.latest_workflow(FEATURE)
    except MultiAgentError:
        latest = None
    if (
        isinstance(latest, dict)
        and str(latest.get("timestamp", "")) >= run["started_at"]
        and any(
            item.get("source") == source
            for item in latest.get("evidence", [])
            if isinstance(item, dict)
        )
    ):
        run["workflow"], run["status"] = latest, "done"
    else:
        run["status"] = "unknown"
        run["error"] = (
            "The agents did not answer in time. Check the Multi-Agent Server "
            "before trying again."
        )


def get_run(run_id):
    with _LOCK:
        _expire_runs()
        return RUNS.get(run_id)


def _expire_runs():
    cutoff = monotonic() - config.AGENT_REQUEST_TTL_SECONDS
    for run_id in [k for k, v in RUNS.items() if v["created"] < cutoff]:
        del RUNS[run_id]


def suggestion(run):
    """Return the Worker's ``structured`` dict, or None."""
    worker = (run.get("workflow") or {}).get("worker_result") or {}
    structured = worker.get("structured")
    return structured if isinstance(structured, dict) else None


def _category_id(run, name):
    """Map a category name from the agents to a choosable id, or None."""
    if not isinstance(name, str):
        return None
    for item in run["categories"]:
        if item["name"].strip().casefold() == name.strip().casefold():
            return item["id"]
    return None


def suggested_category_id(run):
    """Return the Worker's category id when it names a choosable category."""
    return _category_id(run, (suggestion(run) or {}).get("category"))


def workflow_failed(run):
    return (run.get("workflow") or {}).get("status") == "error"


def verdict(run):
    reviewer = (run.get("workflow") or {}).get("reviewer_result") or {}
    return reviewer.get("verdict")


def decide(run, db_url, decision, category_id, reason):
    """Record the human decision and apply an accepted category."""
    message = _decide(run, db_url, decision, category_id, reason)
    _log(
        "AGENT_REVIEW_DECISION", run,
        decision=(run["decision"] or {}).get("decision"),
        applied=message.startswith("Category set to"),
    )
    return message


def _decide(run, db_url, decision, category_id, reason):
    if run["status"] != "done" or run["decision"] is not None:
        raise ChatError("This review has already been decided.", "decided", 409)
    reason = (reason or "").strip()
    # One Accept button: a different dropdown choice is a Partially Accept.
    if decision == "Accept" and category_id not in (None, suggested_category_id(run)):
        decision = "Partially Accept"
    body = {"decision": decision, "decided_by": DECIDED_BY}
    if reason:
        body["reason"] = reason

    if decision == "Accept":
        if workflow_failed(run) or suggested_category_id(run) is None:
            raise ChatError("There is no suggestion to accept.", "no_suggestion", 422)
        if verdict(run) != "approve" and not reason:
            raise ChatError(
                "Give a reason to accept a suggestion the Reviewer did not approve.",
                "reason_required",
                422,
            )
    elif decision == "Partially Accept":
        if workflow_failed(run) or suggestion(run) is None:
            raise ChatError("There is no suggestion to correct.", "no_suggestion", 422)
        if not reason:
            raise ChatError("Give a reason for your correction.", "reason_required", 422)
        if category_id not in {item["id"] for item in run["categories"]}:
            raise ChatError("Choose a valid category.", "invalid_category", 422)
        body["corrected_output"] = {
            "category": run["category_names"][category_id], "reason": reason,
        }
    elif decision != "Reject":
        raise ChatError("Choose a decision.", "invalid_decision", 422)

    workflow_id = run["workflow"]["workflow_id"]
    try:
        record = multi_agent_client.record_decision(workflow_id, body)
    except MultiAgentError as error:
        if error.code != "multi_agent_conflict":
            raise ChatError(error.message, error.code, 502) from None
        record = multi_agent_client.get_workflow(workflow_id)
    human = record.get("human_decision") or {}
    run["decision"] = human
    if human.get("decision") == "Reject":
        return "Rejected. Nothing changed."

    final = human.get("final_output")
    final_id = _category_id(run, final.get("category")) if isinstance(final, dict) else None
    if final_id is None:
        return (
            "The decision was recorded, but it does not name a valid "
            "category, so nothing was applied."
        )
    transaction, names, _ = load_transaction(db_url, run["transaction"]["id"])
    if not is_uncategorised(transaction, names):
        return (
            "The decision was recorded, but the transaction changed first, "
            "so nothing was applied."
        )
    database_request(
        "post",
        f"{db_url}/transactions/{transaction['id']}/category-correction",
        dict,
        json={"category_id": final_id},
    )
    if config.RAG_ENABLED and config.RAG_REFRESH_AFTER_WRITE:
        rag_corpus.start_background_refresh(db_url, "agent_review")
    return f"Category set to {names[final_id]}."
