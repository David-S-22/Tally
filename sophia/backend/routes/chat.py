"""JSON API routes for the Ask Tally chat assistant. Writes only chat_messages; apply does the rest."""
from flask import Blueprint, jsonify, request

from sophia.backend.clients import bills_db
from sophia.backend.json_body import json_body
from sophia.backend.services import chat as chat_service
from sophia.backend.services import multi_agent as multi_agent_service

bp = Blueprint("chat", __name__, url_prefix="/api/chat")


def _suggestion_title(preview):
    """The Suggestions card title for a proposal, from its stored row, so API callers see what the panel shows."""
    if not preview or not preview.get("suggestion_id"):
        return None
    from sophia.backend.routes.fragments import _suggestion_view

    row = bills_db.get_suggestion(preview["suggestion_id"])
    return _suggestion_view(row)["title"] if row else None


@bp.post("")
def chat():
    payload = json_body()
    result = chat_service.send_message(payload.get("message", ""))
    result["suggestion_title"] = _suggestion_title(result["preview"])
    return jsonify(result)


@bp.post("/apply")
def apply():
    payload = json_body()
    result = chat_service.apply(
        payload.get("op"),
        payload.get("entity"),
        payload.get("id"),
        payload.get("fields"),
        message_id=payload.get("message_id"),
    )
    return jsonify(result)


@bp.get("/history")
def history():
    return jsonify(bills_db.list_chat_messages())


@bp.get("/suggestions")
def list_suggestions():
    return jsonify(bills_db.list_suggestions(request.args.get("status")))


@bp.post("/suggestions/<int:suggestion_id>/approve")
def approve_suggestion(suggestion_id):
    return jsonify(chat_service.approve_suggestion(suggestion_id))


@bp.post("/suggestions/<int:suggestion_id>/reject")
def reject_suggestion(suggestion_id):
    return jsonify(chat_service.reject_suggestion(suggestion_id))


@bp.post("/suggestions/<int:suggestion_id>/workflow/<workflow_id>/decision")
def decide_suggestion_workflow(suggestion_id, workflow_id):
    """Release 2: the human decision on the agents' proposal; Accept applies it, Reject rejects the suggestion."""
    payload = json_body()
    return jsonify(
        multi_agent_service.decide_chat_suggestion(
            suggestion_id, workflow_id, payload.get("decision"),
            corrected_output=payload.get("corrected_output"), reason=payload.get("reason"),
        )
    )
