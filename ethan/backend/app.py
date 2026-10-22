from __future__ import annotations

import requests
from flask import Flask, jsonify, request

from . import chat_service, config, db_api, proposal_service, summary_service, transactions_api
from .db_api import ServiceError


def _resolve_transaction_category(categories: list[dict], category_id: object) -> dict:
    if isinstance(category_id, bool) or not isinstance(category_id, int):
        raise ServiceError("category_id must be an integer", 422, "invalid_field")
    for category in categories:
        if category.get("id") == category_id and isinstance(category.get("name"), str):
            return category
    raise ServiceError(
        "category_id must refer to an existing transaction category",
        422,
        "category_not_found",
    )


def _resolve_budget_line_payload(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return payload
    if "category" in payload and "category_id" not in payload:
        raise ServiceError(
            "category_id is required when setting a budget line category",
            422,
            "invalid_field",
        )
    if "category_id" not in payload:
        return payload
    category = _resolve_transaction_category(transactions_api.list_categories(), payload.get("category_id"))
    resolved_payload = dict(payload)
    resolved_payload["category"] = category["name"]
    return resolved_payload


def _ollama_api_base() -> str:
    return config.OLLAMA_URL.removesuffix("/v1")


def _ollama_status() -> str:
    """"off" when AI mode is disabled (no probe is made), otherwise "up"/"down"."""
    if not config.AI_MODE_ENABLED:
        return "off"
    try:
        response = requests.get(f"{_ollama_api_base()}/api/tags", timeout=2)
        response.raise_for_status()
    except requests.RequestException:
        return "down"
    return "up"


def _history_turns(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        return []
    turns: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        content = row.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip():
            continue
        turns.append({"role": role, "content": content.strip()})
    return turns


def _merge_histories(stored_rows: object, request_history: object) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for turn in _history_turns(stored_rows) + _history_turns(request_history):
        key = (turn["role"], turn["content"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(turn)
    return merged[-12:]


def create_app() -> Flask:
    application = Flask(__name__)

    @application.errorhandler(ServiceError)
    def handle_service_error(error: ServiceError):
        return jsonify({"error": error.message, "code": error.code}), error.status

    @application.errorhandler(requests.RequestException)
    def handle_request_error(_error: requests.RequestException):
        return jsonify({
            "error": "budgets database is unavailable",
            "code": "database_unavailable",
        }), 503

    @application.get("/")
    def get_index():
        return jsonify(container="budgets-backend")

    @application.get("/health")
    def get_health():
        try:
            db_health = db_api.health()
            db_status = "up"
        except requests.RequestException:
            db_health = None
            db_status = "down"
        try:
            transactions_categories = transactions_api.list_categories()
            transactions_rows = transactions_api.list_transactions()
            transactions_status = "up"
        except (requests.RequestException, ServiceError):
            transactions_categories = None
            transactions_rows = None
            transactions_status = "down"
        return jsonify(
            {
                "ok": db_status == "up" and transactions_status == "up",
                "db_api": db_status,
                "database_health": db_health,
                "transactions_api": transactions_status,
                "transactions_categories_count": None if transactions_categories is None else len(transactions_categories),
                "transactions_count": None if transactions_rows is None else len(transactions_rows),
                "ollama": _ollama_status(),
                "ai_mode": "enabled" if config.AI_MODE_ENABLED else "disabled",
                "mcp_mode": "enabled" if config.MCP_ENABLED else "disabled",
                "rag_mode": "enabled" if config.RAG_ENABLED else "disabled",
            }
        )

    @application.get("/api/budgets")
    def list_budgets():
        return jsonify(db_api.list_budgets())

    @application.post("/api/budgets")
    def create_budget():
        payload, status = db_api.create_budget(request.get_json(silent=True))
        return jsonify(payload), status

    @application.get("/api/budgets/by-month/<month>")
    def get_budget_by_month(month: str):
        return jsonify(db_api.get_budget_by_month(month))

    @application.get("/api/budgets/<budget_id>")
    def get_budget(budget_id: str):
        return jsonify(db_api.get_budget(budget_id))

    @application.patch("/api/budgets/<budget_id>")
    def patch_budget(budget_id: str):
        return jsonify(db_api.update_budget(budget_id, request.get_json(silent=True)))

    @application.delete("/api/budgets/<budget_id>")
    def delete_budget(budget_id: str):
        _payload, status = db_api.delete_budget(budget_id)
        return "", status

    @application.get("/api/budgets/<budget_id>/snapshot")
    def get_budget_snapshot(budget_id: str):
        budget = db_api.get_budget(budget_id)
        budget_lines = db_api.list_budget_lines(budget_id)
        planned_events = db_api.list_planned_events(budget_id)
        coach_proposals = db_api.list_coach_proposals(budget_id)
        return jsonify(
            {
                "budget": budget,
                "budget_lines": budget_lines,
                "planned_events": planned_events,
                "coach_proposals": coach_proposals,
            }
        )

    @application.get("/api/budgets/<budget_id>/summary")
    def get_budget_summary(budget_id: str):
        return jsonify(summary_service.build_budget_summary(budget_id))

    @application.get("/api/transaction-categories")
    def list_transaction_categories():
        return jsonify(transactions_api.list_categories())

    @application.get("/api/budgets/<budget_id>/budget-lines")
    def list_budget_lines(budget_id: str):
        return jsonify(db_api.list_budget_lines(budget_id))

    @application.post("/api/budgets/<budget_id>/budget-lines")
    def create_budget_line(budget_id: str):
        payload, status = db_api.create_budget_line(
            budget_id,
            _resolve_budget_line_payload(request.get_json(silent=True)),
        )
        return jsonify(payload), status

    @application.get("/api/budget-lines/<line_id>")
    def get_budget_line(line_id: str):
        return jsonify(db_api.get_budget_line(line_id))

    @application.patch("/api/budget-lines/<line_id>")
    def patch_budget_line(line_id: str):
        return jsonify(db_api.update_budget_line(line_id, _resolve_budget_line_payload(request.get_json(silent=True))))

    @application.delete("/api/budget-lines/<line_id>")
    def delete_budget_line(line_id: str):
        _payload, status = db_api.delete_budget_line(line_id)
        return "", status

    @application.get("/api/budgets/<budget_id>/planned-events")
    def list_planned_events(budget_id: str):
        return jsonify(db_api.list_planned_events(budget_id))

    @application.post("/api/budgets/<budget_id>/planned-events")
    def create_planned_event(budget_id: str):
        payload, status = db_api.create_planned_event(budget_id, request.get_json(silent=True))
        return jsonify(payload), status

    @application.get("/api/planned-events/<event_id>")
    def get_planned_event(event_id: str):
        return jsonify(db_api.get_planned_event(event_id))

    @application.patch("/api/planned-events/<event_id>")
    def patch_planned_event(event_id: str):
        return jsonify(db_api.update_planned_event(event_id, request.get_json(silent=True)))

    @application.delete("/api/planned-events/<event_id>")
    def delete_planned_event(event_id: str):
        _payload, status = db_api.delete_planned_event(event_id)
        return "", status

    @application.post("/api/chat")
    def send_chat_message():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ServiceError("request body must be a JSON object", 400, "invalid_json")
        budget_id = payload.get("budget_id")
        if isinstance(budget_id, bool) or not isinstance(budget_id, int):
            raise ServiceError("budget_id must be an integer", 422, "invalid_field")
        budget_id_text = str(budget_id)
        combined_history = _merge_histories(
            db_api.list_chat_messages(budget_id_text),
            payload.get("history"),
        )
        if "integration_mode" in payload:
            result = chat_service.send_message(
                budget_id,
                payload.get("message"),
                combined_history,
                payload.get("integration_mode"),
                payload.get("context"),
                payload.get("skip_deterministic"),
            )
        else:
            result = chat_service.send_message(
                budget_id,
                payload.get("message"),
                combined_history,
                context=payload.get("context"),
                skip_deterministic=payload.get("skip_deterministic"),
            )
        for message_payload in result.get("messages_to_store", []):
            if isinstance(message_payload, dict):
                db_api.create_chat_message(budget_id_text, message_payload)
        return jsonify(result)

    @application.get("/api/budgets/<budget_id>/coach-proposals")
    def list_coach_proposals(budget_id: str):
        return jsonify(db_api.list_coach_proposals(budget_id))

    @application.post("/api/budgets/<budget_id>/coach-proposals")
    def create_coach_proposal(budget_id: str):
        payload, status = db_api.create_coach_proposal(budget_id, request.get_json(silent=True))
        return jsonify(payload), status

    @application.get("/api/coach-proposals/<proposal_id>")
    def get_coach_proposal(proposal_id: str):
        return jsonify(db_api.get_coach_proposal(proposal_id))

    @application.patch("/api/coach-proposals/<proposal_id>")
    def patch_coach_proposal(proposal_id: str):
        return jsonify(db_api.update_coach_proposal(proposal_id, request.get_json(silent=True)))

    @application.post("/api/coach-proposals/<proposal_id>/apply")
    def apply_coach_proposal(proposal_id: str):
        return jsonify(proposal_service.apply(proposal_id))

    @application.get("/api/budgets/<budget_id>/chat-messages")
    def list_chat_messages(budget_id: str):
        return jsonify(db_api.list_chat_messages(budget_id))

    @application.delete("/api/budgets/<budget_id>/chat-messages")
    def delete_chat_messages(budget_id: str):
        _payload, status = db_api.delete_chat_messages(budget_id)
        return "", status
    @application.delete("/api/coach-proposals/<proposal_id>")
    def delete_coach_proposal(proposal_id: str):
        _payload, status = db_api.delete_coach_proposal(proposal_id)
        return "", status

    return application


app = create_app()
