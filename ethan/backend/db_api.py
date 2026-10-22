from __future__ import annotations

import requests

from . import config


class ServiceError(Exception):
    def __init__(self, message: str, status: int, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _url(path: str) -> str:
    return f"{config.BUDGETS_DB_URL}{path}"


def _raise_for_status(response: requests.Response) -> requests.Response:
    if 400 <= response.status_code < 500:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        raise ServiceError(
            payload.get("error", response.text or f"budgets-db returned {response.status_code}"),
            response.status_code,
            payload.get("code"),
        )
    response.raise_for_status()
    return response


def _json(response: requests.Response):
    _raise_for_status(response)
    return response.json()


def health():
    response = requests.get(_url("/health"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response)


def reset_database():
    response = requests.post(_url("/admin/reset"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response)


def list_budgets():
    response = requests.get(_url("/budgets"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response)


def create_budget(payload: dict | None):
    response = requests.post(_url("/budgets"), json=payload, timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response), response.status_code


def get_budget(budget_id: str):
    response = requests.get(_url(f"/budgets/{budget_id}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response)


def get_budget_by_month(month: str):
    response = requests.get(_url(f"/budgets/by-month/{month}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response)


def update_budget(budget_id: str, payload: dict | None):
    response = requests.patch(
        _url(f"/budgets/{budget_id}"),
        json=payload,
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response)


def delete_budget(budget_id: str):
    response = requests.delete(_url(f"/budgets/{budget_id}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    _raise_for_status(response)
    return None, response.status_code


def list_budget_lines(budget_id: str):
    response = requests.get(
        _url(f"/budgets/{budget_id}/budget-lines"),
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response)


def create_budget_line(budget_id: str, payload: dict | None):
    response = requests.post(
        _url(f"/budgets/{budget_id}/budget-lines"),
        json=payload,
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response), response.status_code


def get_budget_line(line_id: str):
    response = requests.get(_url(f"/budget-lines/{line_id}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response)


def update_budget_line(line_id: str, payload: dict | None):
    response = requests.patch(
        _url(f"/budget-lines/{line_id}"),
        json=payload,
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response)


def delete_budget_line(line_id: str):
    response = requests.delete(_url(f"/budget-lines/{line_id}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    _raise_for_status(response)
    return None, response.status_code


def list_planned_events(budget_id: str):
    response = requests.get(
        _url(f"/budgets/{budget_id}/planned-events"),
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response)


def create_planned_event(budget_id: str, payload: dict | None):
    response = requests.post(
        _url(f"/budgets/{budget_id}/planned-events"),
        json=payload,
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response), response.status_code


def get_planned_event(event_id: str):
    response = requests.get(_url(f"/planned-events/{event_id}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response)


def update_planned_event(event_id: str, payload: dict | None):
    response = requests.patch(
        _url(f"/planned-events/{event_id}"),
        json=payload,
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response)


def delete_planned_event(event_id: str):
    response = requests.delete(_url(f"/planned-events/{event_id}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    _raise_for_status(response)
    return None, response.status_code


def list_coach_proposals(budget_id: str):
    response = requests.get(
        _url(f"/budgets/{budget_id}/coach-proposals"),
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response)


def create_coach_proposal(budget_id: str, payload: dict | None):
    response = requests.post(
        _url(f"/budgets/{budget_id}/coach-proposals"),
        json=payload,
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response), response.status_code


def get_coach_proposal(proposal_id: str):
    response = requests.get(_url(f"/coach-proposals/{proposal_id}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    return _json(response)


def update_coach_proposal(proposal_id: str, payload: dict | None):
    response = requests.patch(
        _url(f"/coach-proposals/{proposal_id}"),
        json=payload,
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response)


def delete_coach_proposal(proposal_id: str):
    response = requests.delete(_url(f"/coach-proposals/{proposal_id}"), timeout=config.DATABASE_TIMEOUT_SECONDS)
    _raise_for_status(response)
    return None, response.status_code


def list_chat_messages(budget_id: str):
    response = requests.get(
        _url(f"/budgets/{budget_id}/chat-messages"),
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response)


def create_chat_message(budget_id: str, payload: dict | None):
    response = requests.post(
        _url(f"/budgets/{budget_id}/chat-messages"),
        json=payload,
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    return _json(response), response.status_code


def delete_chat_messages(budget_id: str):
    response = requests.delete(
        _url(f"/budgets/{budget_id}/chat-messages"),
        timeout=config.DATABASE_TIMEOUT_SECONDS,
    )
    _raise_for_status(response)
    return None, response.status_code
