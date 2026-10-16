"""HTTP client for the shared Multi-Agent Server (Release 2).

Speaks the contract in docs/release-2/shared/adr-multi-agent-server-contract.md:
``POST /workflow`` runs Planner -> Worker -> Reviewer and stops at human review;
``POST /workflow/<id>/decision`` records the human's Accept / Partially Accept /
Reject. Every failure is a ModeError with a safe code, like the MCP client;
the message never carries upstream text.
"""
import logging

import requests

from sophia.backend import config
from sophia.backend.services.errors import ModeError, NotFound

logger = logging.getLogger(__name__)

FEATURE = "bills"
DECISIONS = ("Accept", "Partially Accept", "Reject")
REQUIRED_KEYS = {"workflow_id", "planner_result", "worker_result", "reviewer_result", "human_decision"}


def run_workflow(task, evidence, context=None, options=None):
    """Start a Bills workflow; the record with ``human_decision.status == "pending"``."""
    body = {"feature": FEATURE, "task": task, "evidence": evidence, "context": context or {}, "options": options or {}}
    return _post("/workflow", body, "workflow")


def record_decision(workflow_id, decision, corrected_output=None, reason=None, decided_by=None):
    """Record the human decision; the record with ``human_decision.final_output`` set."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    body = {"decision": decision, "corrected_output": corrected_output, "reason": reason, "decided_by": decided_by}
    return _post(f"/workflow/{workflow_id}/decision", body, "decision")


def _post(path, body, label):
    if not config.MULTI_AGENT_ENABLED:
        raise ModeError("multi_agent_disabled")
    url = config.MULTI_AGENT_SERVER_URL + path
    try:
        response = requests.post(url, json=body, timeout=config.MULTI_AGENT_TIMEOUT_SECONDS)
    except requests.Timeout:
        logger.info("MULTI_AGENT call=%s status=timeout", label)
        raise ModeError("multi_agent_timeout") from None
    except requests.RequestException as exc:
        logger.info("MULTI_AGENT call=%s status=connection_error kind=%s", label, type(exc).__name__)
        raise ModeError("multi_agent_connection") from None

    if response.status_code == 404:
        raise NotFound("workflow not found")
    if response.status_code in (400, 409):
        logger.info("MULTI_AGENT call=%s status=rejected http=%s", label, response.status_code)
        raise ModeError("multi_agent_rejected")
    if response.status_code >= 500:
        logger.info("MULTI_AGENT call=%s status=server_error http=%s", label, response.status_code)
        raise ModeError("multi_agent_error")

    try:
        record = response.json()
    except ValueError:
        raise ModeError("multi_agent_invalid_result") from None
    if not isinstance(record, dict) or not REQUIRED_KEYS <= set(record):
        raise ModeError("multi_agent_invalid_result")
    logger.info("MULTI_AGENT call=%s status=ok workflow_id=%s duration_ms=%s", label, record["workflow_id"], record.get("duration_ms"))
    return record
