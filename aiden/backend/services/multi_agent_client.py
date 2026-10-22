"""Client for the shared Multi-Agent Server."""

import re

import requests

from ..config import config


SAFE_MESSAGES = {
    "multi_agent_disabled": "Multi-Agent anomaly detection is disabled.",
    "multi_agent_unavailable": "The Multi-Agent Server is unavailable.",
    "multi_agent_timeout": "The Multi-Agent Server did not respond in time.",
    "multi_agent_busy": "The Multi-Agent Server is busy. Try again shortly.",
    "multi_agent_unauthorised": "The Anomalies backend could not sign in to the Multi-Agent Server.",
    "multi_agent_conflict": "This anomaly review decision was already recorded.",
    "multi_agent_http_error": "The Multi-Agent Server returned an error.",
    "multi_agent_invalid_response": "The Multi-Agent Server returned an invalid response.",
}


class MultiAgentError(Exception):
    def __init__(self, code: str, record: dict | None = None):
        self.code = code
        self.record = record
        self.message = SAFE_MESSAGES.get(
            code,
            "The Multi-Agent Server request failed.",
        )
        super().__init__(self.message)


def _request(method: str, path: str, **options) -> dict:
    headers = options.pop("headers", {})
    if config.MULTI_AGENT_TOKEN:
        headers["Authorization"] = f"Bearer {config.MULTI_AGENT_TOKEN}"
    try:
        response = getattr(requests, method)(
            f"{config.MULTI_AGENT_SERVER_URL}{path}",
            headers=headers,
            timeout=config.MULTI_AGENT_TIMEOUT_SECONDS,
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
            return body
        raise MultiAgentError("multi_agent_unavailable")
    if not 200 <= response.status_code < 300:
        raise MultiAgentError("multi_agent_http_error")
    return body


def create_workflow(body: dict) -> dict:
    return _request("post", "/workflow", json=body)


def get_workflow(workflow_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", workflow_id or ""):
        raise MultiAgentError("multi_agent_invalid_response")
    return _request("get", f"/workflow/{workflow_id}")


def latest_workflow(feature: str) -> dict | None:
    body = _request(
        "get",
        "/workflow/status",
        params={"feature": feature},
    )
    latest = body.get("latest_workflow")
    return latest if isinstance(latest, dict) else None


def record_decision(workflow_id: str, body: dict) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", workflow_id or ""):
        raise MultiAgentError("multi_agent_invalid_response")
    return _request(
        "post",
        f"/workflow/{workflow_id}/decision",
        json=body,
    )
