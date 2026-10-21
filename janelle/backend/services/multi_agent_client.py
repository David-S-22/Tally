"""Small client for the shared Multi-Agent Server.

Every failure is raised as ``MultiAgentError`` with a safe code. The token,
the task and the evidence are never logged. A 503 that carries a saved failed
workflow keeps that record on the error so the caller can show it.
"""
import requests

from .. import config


SAFE_MESSAGES = {
    "multi_agent_disabled": "Agent review is turned off.",
    "multi_agent_unavailable": "The agent server is not running.",
    "multi_agent_timeout": "The agents did not answer in time.",
    "multi_agent_busy": (
        "The agents are busy with another review. Try again shortly."
    ),
    "multi_agent_unauthorised": (
        "The transactions backend could not sign in to the agent server."
    ),
    "multi_agent_conflict": "This decision was already recorded.",
    "multi_agent_http_error": "The agent server returned an error.",
    "multi_agent_invalid_response": (
        "The agent server returned an invalid response."
    ),
}


class MultiAgentError(Exception):
    def __init__(self, code, record=None):
        self.code = code
        self.record = record
        self.message = SAFE_MESSAGES.get(code, "The agent request failed.")
        super().__init__(self.message)


def _request(method, path, timeout, **options):
    if not config.MULTI_AGENT_ENABLED:
        raise MultiAgentError("multi_agent_disabled")
    headers = {}
    if config.MULTI_AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {config.MULTI_AGENT_TOKEN}"
    try:
        response = getattr(requests, method)(
            f"{config.MULTI_AGENT_SERVER_URL}{path}",
            headers=headers,
            timeout=timeout,
            **options,
        )
    except requests.Timeout:
        raise MultiAgentError("multi_agent_timeout") from None
    except requests.RequestException:
        raise MultiAgentError("multi_agent_unavailable") from None

    try:
        body = response.json()
    except (ValueError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise MultiAgentError("multi_agent_invalid_response")

    if response.status_code == 401:
        raise MultiAgentError("multi_agent_unauthorised")
    if response.status_code == 409:
        raise MultiAgentError("multi_agent_conflict")
    if response.status_code == 503:
        if body.get("code") == "workflow_busy":
            raise MultiAgentError("multi_agent_busy")
        if "workflow_id" in body:
            raise MultiAgentError("multi_agent_http_error", record=body)
        raise MultiAgentError("multi_agent_unavailable")
    if not 200 <= response.status_code < 300:
        raise MultiAgentError("multi_agent_http_error")
    return body


def create_workflow(body):
    """Run Planner, Worker and Reviewer; blocks until they finish."""
    return _request(
        "post",
        "/workflow",
        config.MULTI_AGENT_TIMEOUT_SECONDS,
        json=body,
    )


def get_workflow(workflow_id):
    return _request(
        "get",
        f"/workflow/{workflow_id}",
        config.DATABASE_TIMEOUT_SECONDS,
    )


def latest_workflow(feature):
    """Return the newest saved record for ``feature``, or None."""
    body = _request(
        "get",
        "/workflow/status",
        config.DATABASE_TIMEOUT_SECONDS,
        params={"feature": feature},
    )
    return body.get("latest_workflow")


def record_decision(workflow_id, body):
    return _request(
        "post",
        f"/workflow/{workflow_id}/decision",
        config.DATABASE_TIMEOUT_SECONDS,
        json=body,
    )
