from __future__ import annotations

import requests

from . import config
from .db_api import ServiceError

CONTRACT_VERSION = "1.0"


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.MULTI_AGENT_TOKEN}"} if config.MULTI_AGENT_TOKEN else {}


def _request(method: str, path: str, payload: dict | None = None, timeout: float | None = None) -> tuple[int, dict]:
    if not config.MULTI_AGENT_ENABLED:
        raise ServiceError("Multi-Agent mode is disabled.", 503, "multi_agent_disabled")
    url = f"{config.MULTI_AGENT_URL}{path}"
    effective_timeout = timeout if timeout is not None else config.MULTI_AGENT_READ_TIMEOUT_SECONDS
    try:
        if method == "GET":
            response = requests.get(url, headers=_headers(), timeout=effective_timeout)
        else:
            response = requests.post(url, json=payload, headers=_headers(), timeout=effective_timeout)
    except requests.Timeout:
        raise ServiceError(
            "The Multi-Agent Server did not respond in time. The workflow may already have been recorded; "
            "check its status before retrying.",
            504,
            "multi_agent_timeout",
        ) from None
    except requests.RequestException:
        raise ServiceError("The Multi-Agent Server is unavailable.", 503, "multi_agent_unavailable") from None

    try:
        body = response.json()
    except (ValueError, RecursionError):
        raise ServiceError("The Multi-Agent Server returned an invalid response.", 502, "multi_agent_invalid_response") from None
    if not isinstance(body, dict):
        raise ServiceError("The Multi-Agent Server returned an invalid response.", 502, "multi_agent_invalid_response")
    return response.status_code, body


def _server_error(status: int, body: dict) -> ServiceError:
    code = body.get("code") if isinstance(body.get("code"), str) else None
    if status == 401:
        return ServiceError("The Multi-Agent Server rejected the backend credentials.", 502, "multi_agent_auth")
    if code == "workflow_busy":
        return ServiceError("The Multi-Agent Server is busy with another workflow. Try again shortly.", 503, "workflow_busy")
    message = body.get("error") if isinstance(body.get("error"), str) and body.get("error") else "The Multi-Agent Server returned an error."
    if status in {400, 404, 409, 413, 415}:
        return ServiceError(message, status, code or "multi_agent_rejected")
    return ServiceError("The Multi-Agent Server could not complete the request.", 502, code or "multi_agent_http_error")


def health() -> dict:
    status, body = _request("GET", "/health")
    if status != 200:
        raise _server_error(status, body)
    return body


def start_workflow(request_body: dict) -> dict:
    status, body = _request(
        "POST",
        "/workflow",
        {"contract_version": CONTRACT_VERSION, **request_body},
        timeout=config.MULTI_AGENT_TIMEOUT_SECONDS,
    )
    if status == 200 or (status == 503 and isinstance(body.get("workflow_id"), str) and body.get("code") != "workflow_busy"):
        return body
    raise _server_error(status, body)


def get_workflow(workflow_id: str) -> dict:
    status, body = _request("GET", f"/workflow/{_workflow_path_id(workflow_id)}")
    if status != 200:
        raise _server_error(status, body)
    return body


def record_decision(workflow_id: str, decision: dict) -> dict:
    status, body = _request("POST", f"/workflow/{_workflow_path_id(workflow_id)}/decision", decision)
    if status != 200:
        raise _server_error(status, body)
    return body


def export_workflow(workflow_id: str) -> dict:
    status, body = _request("GET", f"/workflow/{_workflow_path_id(workflow_id)}/export")
    if status != 200:
        raise _server_error(status, body)
    return body


def _workflow_path_id(workflow_id: object) -> str:
    if not isinstance(workflow_id, str) or not workflow_id.strip() or not all(
        character.isalnum() or character in "-_" for character in workflow_id
    ):
        raise ServiceError("workflow_id is invalid", 422, "invalid_field")
    return workflow_id
