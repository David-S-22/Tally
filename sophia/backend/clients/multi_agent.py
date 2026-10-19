"""HTTP client for the shared Multi-Agent Server (Release 2, contract 1.0).

Speaks the contract in docs/release-2/shared/multi-agent-server-contract.md:
``POST /workflow`` runs Planner -> Worker -> Reviewer and stops at human review;
``POST /workflow/<id>/decision`` records Accept / Partially Accept / Reject;
``GET /workflow/<id>`` re-reads a saved record. Every failure is a ModeError
with a safe code, like the MCP client; the message never carries upstream text.

Two server behaviours the caller must expect: only one workflow runs at a time
(503 ``workflow_busy``, retry later), and a workflow whose agent failed comes
back as a saved record with ``status == "error"`` and ``workflow_state ==
"failed"`` (HTTP 503) that only allows Reject. That record is returned, not
raised, so the UI can show what happened.
"""
import logging

import requests

from sophia.backend import config
from sophia.backend.services.errors import ModeError, NotFound

logger = logging.getLogger(__name__)

FEATURE = "bills"
CONTRACT_VERSION = "1.0"
DECISIONS = ("Accept", "Partially Accept", "Reject")
REQUIRED_KEYS = {"workflow_id", "planner_result", "worker_result", "reviewer_result", "human_decision"}


def run_workflow(task, evidence, context=None):
    """Start a Bills workflow; the saved record, pending a human or failed (reject-only)."""
    body = {
        "contract_version": CONTRACT_VERSION,
        "feature": FEATURE,
        "task": task,
        "evidence": evidence,
        "context": context or {},
    }
    return _call("POST", "/workflow", body, "workflow")


def record_decision(workflow_id, decision, corrected_output=None, reason=None, decided_by=None):
    """Record the human decision; the record with ``human_decision.final_output`` set."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    body = {"decision": decision, "corrected_output": corrected_output, "reason": reason, "decided_by": decided_by}
    return _call("POST", f"/workflow/{workflow_id}/decision", body, "decision")


def get_workflow(workflow_id):
    """Re-read a saved record (after a timeout, never resubmit: the run may already exist)."""
    return _call("GET", f"/workflow/{workflow_id}", None, "get")


def failed(record):
    """True when the server saved the run with a failed agent; only Reject is allowed."""
    return record.get("workflow_state") == "failed" or record.get("status") == "error"


def _headers():
    headers = {"Accept": "application/json"}
    if config.MULTI_AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {config.MULTI_AGENT_TOKEN}"
    return headers


def _call(method, path, body, label):
    if not config.MULTI_AGENT_ENABLED:
        raise ModeError("multi_agent_disabled")
    url = config.MULTI_AGENT_SERVER_URL + path
    try:
        response = requests.request(method, url, json=body, headers=_headers(), timeout=config.MULTI_AGENT_TIMEOUT_SECONDS)
    except requests.Timeout:
        logger.info("MULTI_AGENT call=%s status=timeout", label)
        raise ModeError("multi_agent_timeout") from None
    except requests.RequestException as exc:
        logger.info("MULTI_AGENT call=%s status=connection_error kind=%s", label, type(exc).__name__)
        raise ModeError("multi_agent_connection") from None

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise ModeError("multi_agent_invalid_result")
    code = payload.get("code")
    is_record = REQUIRED_KEYS <= set(payload)

    if response.status_code == 404:
        raise NotFound("workflow not found")
    if response.status_code == 401:
        logger.info("MULTI_AGENT call=%s status=unauthorized", label)
        raise ModeError("multi_agent_unauthorized")
    if response.status_code == 503 and code == "workflow_busy":
        logger.info("MULTI_AGENT call=%s status=busy", label)
        raise ModeError("multi_agent_busy")
    if response.status_code in (400, 409, 413, 415):
        logger.info("MULTI_AGENT call=%s status=rejected http=%s code=%s", label, response.status_code, code)
        raise ModeError("multi_agent_rejected")
    if response.status_code >= 500 and not is_record:
        logger.info("MULTI_AGENT call=%s status=server_error http=%s code=%s", label, response.status_code, code)
        raise ModeError("multi_agent_error")
    if not is_record:
        raise ModeError("multi_agent_invalid_result")

    logger.info(
        "MULTI_AGENT call=%s status=%s workflow_id=%s state=%s duration_ms=%s",
        label, payload.get("status"), payload["workflow_id"], payload.get("workflow_state"), payload.get("duration_ms"),
    )
    return payload
