import json

import requests
import pytest

from backend import chat_service, db_api, mcp_client, proposal_service, rag_client, summary_service, transactions_api
from backend.ai import chat_prompt, grounded_prompt, guard
from backend.app import create_app


def _client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_index():
    resp = _client().get("/")

    assert resp.status_code == 200
    assert isinstance(resp.json, dict)
    assert resp.json["container"] == "budgets-backend"


def test_health_reports_database_up(monkeypatch):
    monkeypatch.setattr(db_api, "health", lambda: {"ok": True})
    monkeypatch.setattr(transactions_api, "list_categories", lambda: [{"id": 80, "name": "Dining"}])
    monkeypatch.setattr(transactions_api, "list_transactions", lambda: [{"id": 1, "amount": 42.5}])
    monkeypatch.setattr("backend.app._ollama_status", lambda: "up")

    resp = _client().get("/health")

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert resp.get_json()["db_api"] == "up"
    assert resp.get_json()["transactions_api"] == "up"
    assert resp.get_json()["transactions_count"] == 1
    assert resp.get_json()["ollama"] == "up"
    assert resp.get_json()["mcp_mode"] in {"enabled", "disabled"}
    assert resp.get_json()["rag_mode"] in {"enabled", "disabled"}


def test_list_budgets(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "list_budgets",
        lambda: [{"id": 1, "month": "2026-09"}],
    )

    resp = _client().get("/api/budgets")

    assert resp.status_code == 200
    assert resp.get_json() == [{"id": 1, "month": "2026-09"}]


def test_list_transaction_categories(monkeypatch):
    monkeypatch.setattr(
        transactions_api,
        "list_categories",
        lambda: [{"id": 80, "name": "Dining", "type": "want"}],
    )

    resp = _client().get("/api/transaction-categories")

    assert resp.status_code == 200
    assert resp.get_json() == [{"id": 80, "name": "Dining", "type": "want"}]


def test_create_budget(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "create_budget",
        lambda payload: ({"id": 1, "month": payload["month"]}, 201),
    )

    resp = _client().post("/api/budgets", json={"month": "2026-09"})

    assert resp.status_code == 201
    assert resp.get_json()["id"] == 1


def test_create_budget_line_resolves_transaction_category(monkeypatch):
    monkeypatch.setattr(
        transactions_api,
        "list_categories",
        lambda: [{"id": 81, "name": "Groceries", "type": "need"}],
    )
    monkeypatch.setattr(
        db_api,
        "create_budget_line",
        lambda budget_id, payload: (
            {
                "id": 1,
                "budget_id": budget_id,
                "category_id": payload["category_id"],
                "category": payload["category"],
            },
            201,
        ),
    )

    resp = _client().post("/api/budgets/b1/budget-lines", json={"category_id": 81, "warn_at": 15000})

    assert resp.status_code == 201
    assert resp.get_json() == {
        "id": 1,
        "budget_id": "b1",
        "category_id": 81,
        "category": "Groceries",
    }


def test_patch_budget_line_requires_category_id_when_setting_category(monkeypatch):
    resp = _client().patch("/api/budget-lines/l1", json={"category": "Groceries"})

    assert resp.status_code == 422
    assert resp.get_json() == {
        "error": "category_id is required when setting a budget line category",
        "code": "invalid_field",
    }


def test_create_planned_event(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "create_planned_event",
        lambda budget_id, payload: (
            {
                "id": 7,
                "budget_id": budget_id,
                "category": payload["category"],
                "est_low": payload["est_low"],
                "est_high": payload["est_high"],
                "status": payload["status"],
            },
            201,
        ),
    )

    resp = _client().post(
        "/api/budgets/1/planned-events",
        json={"category": "Dining", "est_low": 3000, "est_high": 5000, "status": "planned"},
    )

    assert resp.status_code == 201
    assert resp.get_json() == {
        "id": 7,
        "budget_id": "1",
        "category": "Dining",
        "est_low": 3000,
        "est_high": 5000,
        "status": "planned",
    }


def test_send_chat_message(monkeypatch):
    stored_messages = []
    monkeypatch.setattr(db_api, "list_chat_messages", lambda budget_id: [{"role": "assistant", "content": "Earlier stored advice"}])
    monkeypatch.setattr(
        db_api,
        "create_chat_message",
        lambda budget_id, payload: stored_messages.append((budget_id, payload)) or ({"id": len(stored_messages)}, 201),
    )
    monkeypatch.setattr(
        chat_service,
        "send_message",
        lambda budget_id, message, history=None, integration_mode=None, context=None, skip_deterministic=None: {
            "reply": "Dining is projected to reach warning this month.",
            "mode": "advice",
            "question": None,
            "proposal": None,
            "fallback": False,
            "response_source": "deterministic",
            "stage_trace": ["observe", "plan", "act", "adapt"],
            "agentic_workflow": {"observe": {"month": "2026-09"}, "plan": {"intent": "overspending"}},
            "user_message": {"role": "user", "content": message},
            "assistant_message": {"role": "assistant", "content": "Dining is projected to reach warning this month."},
            "messages_to_store": [
                {"role": "user", "content": message},
                {
                    "role": "assistant",
                    "content": "Dining is projected to reach warning this month.",
                    "mode": "advice",
                    "response_source": "deterministic",
                    "plan_json": {"intent": "overspending"},
                    "observation_json": {"month": "2026-09"},
                    "stage_trace": ["observe", "plan", "act", "adapt"],
                },
            ],
        },
    )

    resp = _client().post(
        "/api/chat",
        json={"budget_id": 1, "message": "How is Dining looking?", "history": [{"role": "user", "content": "Earlier"}]},
    )

    assert resp.status_code == 200
    assert resp.get_json()["mode"] == "advice"
    assert resp.get_json()["response_source"] == "deterministic"
    assert resp.get_json()["stage_trace"] == ["observe", "plan", "act", "adapt"]
    assert resp.get_json()["assistant_message"]["content"] == "Dining is projected to reach warning this month."
    assert len(stored_messages) == 2
    assert stored_messages[1][1]["plan_json"] == {"intent": "overspending"}


def test_send_chat_message_passes_integration_mode(monkeypatch):
    seen = {}
    monkeypatch.setattr(db_api, "list_chat_messages", lambda budget_id: [])
    monkeypatch.setattr(db_api, "create_chat_message", lambda budget_id, payload: ({"id": 1}, 201))

    def fake_send_message(budget_id, message, history=None, integration_mode=None, context=None, skip_deterministic=None):
        seen.update(
            {
                "budget_id": budget_id,
                "message": message,
                "history": history,
                "integration_mode": integration_mode,
                "context": context,
                "skip_deterministic": skip_deterministic,
            }
        )
        return {
            "reply": "Shared MCP result ready.",
            "mode": "advice",
            "question": None,
            "proposal": None,
            "fallback": False,
            "response_source": "mcp",
            "tool_result": {"tool_name": "search_transactions", "count": 1},
            "grounding": None,
            "stage_trace": ["observe", "plan", "act", "adapt"],
            "agentic_workflow": {"observe": {}, "plan": {}},
            "user_message": {"role": "user", "content": message},
            "assistant_message": {
                "role": "assistant",
                "content": "Shared MCP result ready.",
                "response_source": "mcp",
                "tool_result": {"tool_name": "search_transactions", "count": 1},
                "grounding": None,
            },
            "messages_to_store": [
                {"role": "user", "content": message},
                {
                    "role": "assistant",
                    "content": "Shared MCP result ready.",
                    "mode": "advice",
                    "response_source": "mcp",
                    "plan_json": {"intent": "mcp"},
                    "observation_json": {"month": "2026-09"},
                    "stage_trace": ["observe", "plan", "act", "adapt"],
                    "tool_result_json": {"tool_name": "search_transactions", "count": 1},
                },
            ],
        }

    monkeypatch.setattr(chat_service, "send_message", fake_send_message)

    resp = _client().post(
        "/api/chat",
        json={"budget_id": 1, "message": "show my recent transactions", "history": [], "integration_mode": "mcp"},
    )

    assert resp.status_code == 200
    assert resp.get_json()["response_source"] == "mcp"
    assert resp.get_json()["tool_result"] == {"tool_name": "search_transactions", "count": 1}
    assert seen["integration_mode"] == "mcp"
    assert seen["context"] is None
    assert seen["skip_deterministic"] is None


def test_send_chat_message_passes_context(monkeypatch):
    seen = {}
    monkeypatch.setattr(db_api, "list_chat_messages", lambda budget_id: [])
    monkeypatch.setattr(db_api, "create_chat_message", lambda budget_id, payload: ({"id": 1}, 201))

    def fake_send_message(budget_id, message, history=None, integration_mode=None, context=None, skip_deterministic=None):
        seen.update(
            {
                "budget_id": budget_id,
                "message": message,
                "history": history,
                "integration_mode": integration_mode,
                "context": context,
                "skip_deterministic": skip_deterministic,
            }
        )
        return {
            "reply": "Budget line summary ready.",
            "mode": "advice",
            "question": None,
            "proposal": None,
            "fallback": False,
            "response_source": "deterministic",
            "tool_result": None,
            "grounding": None,
            "stage_trace": ["observe", "plan", "act", "adapt"],
            "agentic_workflow": {"observe": {}, "plan": {}},
            "user_message": {"role": "user", "content": message},
            "assistant_message": {"role": "assistant", "content": "Budget line summary ready.", "response_source": "deterministic"},
            "messages_to_store": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "Budget line summary ready.", "mode": "advice", "response_source": "deterministic"},
            ],
        }

    monkeypatch.setattr(chat_service, "send_message", fake_send_message)

    resp = _client().post(
        "/api/chat",
        json={
            "budget_id": 1,
            "message": "Tell me about this line",
            "history": [],
            "context": {"ui_action": "line-pressure-explainer", "target_budget_line_id": 3, "target_category": "Dining"},
        },
    )

    assert resp.status_code == 200
    assert seen["integration_mode"] is None
    assert seen["context"] == {"ui_action": "line-pressure-explainer", "target_budget_line_id": 3, "target_category": "Dining"}
    assert seen["skip_deterministic"] is None


def test_send_chat_message_passes_skip_deterministic(monkeypatch):
    seen = {}
    monkeypatch.setattr(db_api, "list_chat_messages", lambda budget_id: [])
    monkeypatch.setattr(db_api, "create_chat_message", lambda budget_id, payload: ({"id": 1}, 201))

    def fake_send_message(budget_id, message, history=None, integration_mode=None, context=None, skip_deterministic=None):
        seen.update(
            {
                "budget_id": budget_id,
                "message": message,
                "history": history,
                "integration_mode": integration_mode,
                "context": context,
                "skip_deterministic": skip_deterministic,
            }
        )
        return {
            "reply": "AI fallback ready.",
            "mode": "advice",
            "question": None,
            "proposal": None,
            "fallback": False,
            "response_source": "ollama",
            "tool_result": None,
            "grounding": None,
            "stage_trace": ["observe", "plan", "act", "adapt"],
            "agentic_workflow": {"observe": {}, "plan": {}},
            "user_message": {"role": "user", "content": message},
            "assistant_message": {"role": "assistant", "content": "AI fallback ready.", "response_source": "ollama"},
            "messages_to_store": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "AI fallback ready.", "mode": "advice", "response_source": "ollama"},
            ],
        }

    monkeypatch.setattr(chat_service, "send_message", fake_send_message)

    resp = _client().post(
        "/api/chat",
        json={"budget_id": 1, "message": "Where am I overspending most?", "history": [], "skip_deterministic": True},
    )

    assert resp.status_code == 200
    assert resp.get_json()["response_source"] == "ollama"
    assert seen["skip_deterministic"] is True

def test_apply_coach_proposal_route(monkeypatch):
    monkeypatch.setattr(
        proposal_service,
        "apply",
        lambda proposal_id: {
            "proposal": {"id": int(proposal_id), "status": "accepted"},
            "applied": [{"id": 3}],
            "stage_trace": ["observe", "plan", "act", "observe", "adapt"],
        },
    )

    resp = _client().post("/api/coach-proposals/7/apply")

    assert resp.status_code == 200
    assert resp.get_json()["proposal"]["status"] == "accepted"
    assert resp.get_json()["applied"] == [{"id": 3}]
    assert resp.get_json()["stage_trace"] == ["observe", "plan", "act", "observe", "adapt"]


def test_chat_history_routes(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "list_chat_messages",
        lambda budget_id: [{"id": 1, "budget_id": int(budget_id), "role": "assistant", "content": "Stored"}],
    )
    monkeypatch.setattr(
        db_api,
        "delete_chat_messages",
        lambda budget_id: (None, 204),
    )

    list_resp = _client().get("/api/budgets/7/chat-messages")
    delete_resp = _client().delete("/api/budgets/7/chat-messages")

    assert list_resp.status_code == 200
    assert list_resp.get_json() == [{"id": 1, "budget_id": 7, "role": "assistant", "content": "Stored"}]
    assert delete_resp.status_code == 204


def test_chat_prompt_examples_use_current_summary_values():
    messages = chat_prompt.build(
        "what should i do then",
        [],
        {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 12000,
                "planned_est_high_total": 0,
                "remaining_income_high": 548000,
            },
            "budget_lines": [
                {
                    "id": 8,
                    "category": "Mobile",
                    "actual_spend": 12000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 12000,
                    "warn_at": 7500,
                    "hard_cap": 10000,
                },
            ],
        },
    )

    content = "\n".join(str(message.get("content") or "") for message in messages)

    assert "Mobile" in content
    assert "$120.00" in content
    assert "$100.00" in content
    assert "Dining" not in content
    assert "$791.00" not in content
    assert "$800.00" not in content


def test_chat_route_returns_json_when_proposal_storage_is_unavailable(monkeypatch):
    monkeypatch.setattr(db_api, "list_chat_messages", lambda _budget_id: [])
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 60100,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79100,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    monkeypatch.setattr(
        db_api,
        "create_coach_proposal",
        lambda _budget_id, _payload: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    resp = _client().post(
        "/api/chat",
        json={"budget_id": 1, "message": "what adjustments should i make to my budgets", "history": []},
    )

    assert resp.status_code == 503
    assert resp.is_json is True
    assert resp.get_json() == {
        "error": "budgets database is unavailable",
        "code": "database_unavailable",
    }


def test_chat_service_rewrites_direct_change_wording_to_proposal_review(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 29000,
                "planned_est_high_total": 0,
                "remaining_income_high": 531000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 10000,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 29000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 40,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "mode": "proposal",
            "say": "I have adjusted the Dining budget. The warning amount is now set to $260.00 and the hard cap is set to $340.00.",
            "question": None,
            "proposal": {
                "proposal_type": "adjust_budget_line_thresholds",
                "operations": [
                    {
                        "action": "update_budget_line",
                        "budget_line_id": 3,
                        "category": "Dining",
                        "fields": {"warn_at": 26000, "hard_cap": 34000},
                    }
                ],
            },
            "fallback": False,
        },
    )

    result = chat_service.send_message(1, "no i want the cap to instead now be 340, with a warning at 260")

    assert result["mode"] == "proposal"
    assert "I have adjusted" not in result["reply"]
    assert "I revised the proposal to move Dining's warning amount to $260.00 and hard cap to $340.00 for your review." == result["reply"]
    assert stored["rationale"] == result["reply"]


def test_chat_service_rewrites_proposal_reply_when_displayed_values_do_not_match_payload(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 24000,
                "planned_est_high_total": 12000,
                "remaining_income_high": 524000,
            },
            "budget_lines": [
                {
                    "id": 7,
                    "category": "Groceries",
                    "actual_spend": 24000,
                    "planned_est_high_total": 12000,
                    "projected_high_total": 36000,
                    "warn_at": 20000,
                    "hard_cap": 25000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 41,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "mode": "proposal",
            "say": (
                "I prepared a proposal to adjust the Groceries category. "
                "The warning threshold will be moved to $110.00 and the hard cap to $150.00 "
                "to reflect your planned spend of $120.00."
            ),
            "question": None,
            "proposal": {
                "proposal_type": "adjust_budget_line_thresholds",
                "operations": [
                    {
                        "action": "update_budget_line",
                        "budget_line_id": 7,
                        "category": "Groceries",
                        "fields": {"warn_at": 1100, "hard_cap": 1500},
                    }
                ],
            },
            "fallback": False,
        },
    )

    result = chat_service.send_message(1, "what should i do about groceries?")

    assert result["mode"] == "proposal"
    assert result["reply"] == "I revised the proposal to move Groceries's warning amount to $11.00 and hard cap to $15.00 for your review."
    assert stored["rationale"] == result["reply"]


def test_chat_service_treats_explicit_groceries_proposal_request_as_adjustment(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 24000,
                "planned_est_high_total": 12000,
                "remaining_income_high": 524000,
            },
            "budget_lines": [
                {
                    "id": 7,
                    "category": "Groceries",
                    "actual_spend": 24000,
                    "planned_est_high_total": 12000,
                    "projected_high_total": 36000,
                    "warn_at": 20000,
                    "hard_cap": 25000,
                },
            ],
            "coach_proposals": [],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 42,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "make a proposal so I can spend an extra $150 in groceries")

    fields = stored["proposal_json"]["operations"][0]["fields"]
    assert result["response_source"] == "deterministic"
    assert result["mode"] == "proposal"
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 7
    assert stored["proposal_json"]["operations"][0]["category"] == "Groceries"
    assert "Groceries" in result["reply"]
    assert "that budget line" not in result["reply"]
    assert "warn_at" in fields
    assert "hard_cap" in fields


def test_chat_service_hydrates_ollama_proposal_category_from_budget_line(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 24000,
                "planned_est_high_total": 12000,
                "remaining_income_high": 524000,
            },
            "budget_lines": [
                {
                    "id": 7,
                    "category": "Groceries",
                    "actual_spend": 24000,
                    "planned_est_high_total": 12000,
                    "projected_high_total": 36000,
                    "warn_at": 20000,
                    "hard_cap": 25000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 43,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "mode": "proposal",
            "say": "I revised the proposal to move that budget line's warning amount to $25.00 for your review.",
            "question": None,
            "proposal": {
                "proposal_type": "adjust_budget_line_thresholds",
                "operations": [
                    {
                        "action": "update_budget_line",
                        "budget_line_id": 7,
                        "fields": {"warn_at": 2500},
                    }
                ],
            },
            "fallback": False,
        },
    )

    result = chat_service.send_message(1, "make a proposal so I can spend an extra $150 in groceries", skip_deterministic=True)

    assert result["response_source"] == "ollama"
    assert stored["proposal_json"]["operations"][0]["category"] == "Groceries"
    assert result["reply"] == "I revised the proposal to move Groceries's warning amount to $25.00 for your review."
    assert "that budget line" not in result["reply"]


def test_chat_service_summarises_budget_with_grounded_data(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {"category": "Dining", "projected_high_total": 79100, "warn_at": 70000, "hard_cap": 85000},
                {"category": "Fitness", "projected_high_total": 37000, "warn_at": 30000, "hard_cap": 45000},
            ],
        },
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "summarise my budget situation")

    assert result["mode"] == "advice"
    assert "September 2026" in result["reply"]
    assert "$5,600.00" in result["reply"]
    assert "$1,071.00" in result["reply"]
    assert "$4,254.00" in result["reply"]
    assert "Dining" in result["reply"]


def test_chat_service_runs_mcp_transaction_lookup(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {"remaining_income_high": 425400},
            "budget_lines": [
                {"id": 3, "category": "Dining", "projected_high_total": 79000, "warn_at": 70000, "hard_cap": 85000}
            ],
        },
    )
    monkeypatch.setattr(
        mcp_client,
        "call_tool",
        lambda name, arguments: [
            {"date": "2026-09-02", "merchant": "Merivale", "description": "Dinner", "amount": 84.5, "category_name": "Dining"},
            {"date": "2026-09-09", "merchant": "The Oaks", "description": "Lunch", "amount": 21.0, "category_name": "Dining"},
        ],
    )

    result = chat_service.send_message(1, "show me recent transactions for dining", integration_mode="mcp")

    assert result["response_source"] == "mcp"
    assert result["tool_result"]["tool_name"] == "search_transactions"
    assert result["tool_result"]["count"] == 2
    assert "shared MCP transaction search" in result["reply"]


def test_chat_service_runs_grounded_rag_reply(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {"remaining_income_high": 425400},
            "budget_lines": [
                {"id": 3, "category": "Dining", "projected_high_total": 79000, "warn_at": 70000, "hard_cap": 85000}
            ],
        },
    )
    monkeypatch.setattr(
        rag_client,
        "retrieve",
        lambda feature, question, k: [
            {"id": "budgets-1", "text": "# Dining guidance\nReduce non-essential dining when pressure is high.", "metadata": {"source": "budgets/budget-guidance.md"}, "distance": 0.42}
        ],
    )
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "answer": "Dining is under the most pressure, so reducing discretionary meals would make the biggest difference.",
            "cited": ["budgets/budget-guidance.md"],
            "insufficient_context": False,
            "fallback": False,
        },
    )

    result = chat_service.send_message(
        1,
        "using grounded budget guidance with sources, what should I focus on this month?",
        integration_mode="rag",
    )

    assert result["response_source"] == "rag"
    assert result["grounding"]["confidence"] == "high"
    assert result["grounding"]["insufficient_context"] is False
    assert result["grounding"]["citations"][0]["source"] == "budgets/budget-guidance.md"


def test_chat_service_contextual_mcp_uses_target_budget_line(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {"remaining_income_high": 534000},
            "budget_lines": [
                {"id": 3, "category": "Dining", "projected_high_total": 19000, "warn_at": 19000, "hard_cap": 24000},
                {"id": 11, "category": "Transport", "projected_high_total": 7000, "warn_at": 7000, "hard_cap": 10000},
            ],
        },
    )
    seen = {}

    def fake_call_tool(name, arguments):
        seen["name"] = name
        seen["arguments"] = arguments
        return [{"date": "2026-09-01", "merchant": "Cafe", "description": "Lunch", "amount": 18.0, "category_name": "Dining"}]

    monkeypatch.setattr(mcp_client, "call_tool", fake_call_tool)

    result = chat_service.send_message(
        1,
        "Show me the evidence",
        integration_mode="mcp",
        context={"ui_action": "line-transactions", "target_budget_line_id": 3, "target_category": "Dining"},
    )

    assert result["response_source"] == "mcp"
    assert seen["name"] == "search_transactions"
    assert seen["arguments"]["category_name"] == "Dining"
    assert "Dining" in result["reply"]


def test_chat_service_contextual_event_affordability_uses_planned_event(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 7000,
                "planned_est_high_total": 19000,
                "remaining_income_high": 534000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 0,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                }
            ],
            "planned_events": [
                {"id": 9, "label": "Weekend trip", "category": "Dining", "est_high": 19000, "status": "planned"}
            ],
            "transactions": {"other_expenses": []},
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Weekend trip would keep Dining within the hard cap, but it would use most of the remaining room this month.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(
        1,
        "Can I still afford this planned event?",
        context={"ui_action": "event-affordability", "target_planned_event_id": 9, "target_category": "Dining"},
    )

    assert result["response_source"] == "ollama"
    assert "Weekend trip" in result["user_message"]["content"]
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Affordability target: Dining" in system_context
    assert "Requested spend: $190.00" in system_context
    assert "Projected spend after request: $380.00" in system_context


def test_chat_service_skip_deterministic_routes_to_ollama(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {"remaining_income_high": 534000},
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 0,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                }
            ],
            "transactions": {"other_expenses": []},
        },
    )
    monkeypatch.setattr(chat_service, "_deterministic_reply", lambda *_args, **_kwargs: pytest.fail("deterministic reply should be skipped"))
    monkeypatch.setattr(rag_client, "retrieve", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "mode": "advice",
            "say": "AI-only test response.",
            "question": None,
            "proposal": None,
            "fallback": False,
        },
    )

    result = chat_service.send_message(1, "Where am I overspending most?", skip_deterministic=True)

    assert result["response_source"] == "ollama"
    assert result["reply"] == "AI-only test response."


def test_chat_service_affordability_train_ticket_prefers_ollama_with_computed_facts(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-10"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-10", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 3000,
                "planned_est_high_total": 0,
                "remaining_income_high": 557000,
            },
            "budget_lines": [
                {
                    "id": 11,
                    "category": "Transport",
                    "actual_spend": 3000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 3000,
                    "warn_at": 1500,
                    "hard_cap": 2500,
                }
            ],
            "transactions": {"other_expenses": []},
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Transport is already over cap, so another $20.00 would add more pressure and take it to $50.00 projected.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "can i afford to buy a $20 train ticket")

    assert result["response_source"] == "ollama"
    assert result["agentic_workflow"]["plan"]["execution_path"] == "ollama"
    assert result["agentic_workflow"]["plan"]["uses_deterministic_facts"] is True
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Affordability target: Transport" in system_context
    assert "Requested spend: $20.00" in system_context
    assert "Projected spend after request: $50.00" in system_context
    assert "Threshold state after request: over_hard_cap" in system_context


def test_chat_service_affordability_follow_up_quantity_reuses_previous_amount(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-10"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-10", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 3000,
                "planned_est_high_total": 0,
                "remaining_income_high": 557000,
            },
            "budget_lines": [
                {
                    "id": 11,
                    "category": "Transport",
                    "actual_spend": 3000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 3000,
                    "warn_at": 1500,
                    "hard_cap": 2500,
                }
            ],
            "transactions": {"other_expenses": []},
        },
    )
    history = [
        {"role": "user", "content": "can i afford to buy a $20 train ticket"},
        {"role": "assistant", "content": "No, that would put Transport over its hard cap."},
    ]
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Two more at that amount would add $40.00, taking Transport to $70.00 projected.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "can i afford to buy 2", history)

    assert result["response_source"] == "ollama"
    assert result["mode"] == "advice"
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Requested spend: $40.00" in system_context
    assert "Projected spend after request: $70.00" in system_context


def test_chat_service_affordability_guardrail_overrides_ollama_when_skip_is_off(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-10"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-10", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 3000,
                "planned_est_high_total": 0,
                "remaining_income_high": 557000,
            },
            "budget_lines": [
                {
                    "id": 11,
                    "category": "Transport",
                    "actual_spend": 3000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 3000,
                    "warn_at": 1500,
                    "hard_cap": 2500,
                }
            ],
            "transactions": {"other_expenses": []},
        },
    )
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "mode": "proposal",
            "say": "I prepared a proposal to raise the hard cap.",
            "question": None,
            "proposal": {
                "proposal_type": "adjust_budget_line_thresholds",
                "operations": [{"action": "update_budget_line", "budget_line_id": 11, "fields": {"hard_cap": 5000}}],
            },
            "fallback": False,
        },
    )

    result = chat_service.send_message(1, "can i afford to buy a $20 train ticket")

    assert result["response_source"] == "deterministic"
    assert result["mode"] == "advice"
    assert result["proposal"] is None
    assert "adding $20.00 would take the projected total to $50.00" in result["reply"]


def test_chat_service_skip_deterministic_bypasses_affordability_guardrail(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-10"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-10", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 3000,
                "planned_est_high_total": 0,
                "remaining_income_high": 557000,
            },
            "budget_lines": [
                {
                    "id": 11,
                    "category": "Transport",
                    "actual_spend": 3000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 3000,
                    "warn_at": 1500,
                    "hard_cap": 2500,
                }
            ],
            "transactions": {"other_expenses": []},
        },
    )
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "mode": "advice",
            "say": "AI affordability response.",
            "question": None,
            "proposal": None,
            "fallback": False,
        },
    )

    result = chat_service.send_message(1, "can i afford to buy a $20 train ticket", skip_deterministic=True)

    assert result["response_source"] == "ollama"
    assert result["reply"] == "AI affordability response."


def test_chat_service_ollama_path_builds_mcp_and_rag_context(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 7000,
                "planned_est_high_total": 19000,
                "remaining_income_high": 534000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 0,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                }
            ],
            "transactions": {"other_expenses": []},
        },
    )
    monkeypatch.setattr(
        mcp_client,
        "call_tool",
        lambda name, arguments: [
            {"date": "2026-09-01", "merchant": "Cafe", "description": "Lunch", "amount": 18.0, "category_name": "Dining"}
        ],
    )
    monkeypatch.setattr(
        rag_client,
        "retrieve",
        lambda feature, question, k: [
            {
                "id": "budgets-1",
                "text": "# Budget Coach guidance\nExplain whether the category pressure comes from actual spend or planned events.",
                "metadata": {"source": "budgets/budget-guidance.md"},
                "distance": 0.42,
            }
        ],
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Dining is under pressure mainly because of planned spending, and the shared guidance suggests keeping that category tight.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "What should I focus on for Dining this month?", skip_deterministic=True)

    assert result["response_source"] == "ollama"
    assert result["tool_result"]["tool_name"] == "search_transactions"
    assert result["tool_result"]["count"] == 1
    assert result["grounding"]["confidence"] == "high"
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Shared MCP transaction evidence" in system_context
    assert "Retrieved guidance" in system_context
    assert "Computed budget facts" in system_context
    assert "Focus line: Dining" in system_context
    assert "Budget Coach guidance" in system_context


def test_chat_service_ollama_affordability_prompt_includes_computed_budget_facts(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-10"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-10", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 3000,
                "planned_est_high_total": 0,
                "remaining_income_high": 557000,
            },
            "budget_lines": [
                {
                    "id": 11,
                    "category": "Transport",
                    "actual_spend": 3000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 3000,
                    "warn_at": 1500,
                    "hard_cap": 2500,
                }
            ],
            "transactions": {"other_expenses": []},
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "AI affordability response.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "can i afford to buy a $20 train ticket", skip_deterministic=True)

    assert result["response_source"] == "ollama"
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Computed budget facts" in system_context
    assert "Affordability target: Transport" in system_context
    assert "Requested spend: $20.00" in system_context
    assert "Remaining income before spend: $5,570.00" in system_context
    assert "Remaining income after spend: $5,550.00" in system_context
    assert "Current threshold state: over_hard_cap" in system_context
    assert "Threshold state after request: over_hard_cap" in system_context


def test_chat_service_uses_grounded_fallback_when_rag_model_punts(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {"remaining_income_high": 534000},
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 0,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                },
                {
                    "id": 11,
                    "category": "Transport",
                    "actual_spend": 7000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 7000,
                    "warn_at": 7000,
                    "hard_cap": 10000,
                },
            ],
        },
    )
    monkeypatch.setattr(
        rag_client,
        "retrieve",
        lambda feature, question, k: [
            {
                "id": "budgets-1",
                "text": "# Budget Coach guidance\nPrioritise the category under the most pressure.",
                "metadata": {"source": "budget-guidance.md"},
                "distance": 0.898,
            }
        ],
    )
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "answer": "I couldn't ground a reliable answer from the retrieved budget guidance right now.",
            "cited": [],
            "insufficient_context": True,
            "fallback": False,
        },
    )

    result = chat_service.send_message(
        1,
        "using grounded budget guidance with sources, what should I focus on this month?",
        integration_mode="rag",
    )

    assert result["response_source"] == "rag"
    assert result["grounding"]["insufficient_context"] is False
    assert result["grounding"]["citations"][0]["source"] == "budget-guidance.md"
    assert "Dining" in result["reply"]
    assert "week-ahead planned spending" in result["reply"]


def test_grounded_prompt_formats_money_and_week_ahead_context():
    messages = grounded_prompt.build(
        "Using grounded budget guidance with sources, what should I focus on this month?",
        {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "actual_spend_total": 0,
                "planned_est_high_total": 19000,
                "remaining_income_high": 541000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 0,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                }
            ],
        },
        [
            {
                "id": "budgets-1",
                "text": "# Budget Coach guidance\nUse planned-event pressure to prioritise categories.",
                "metadata": {"source": "budgets/budget-guidance.md"},
                "distance": 0.42,
            }
        ],
    )

    assert "week-ahead plan or planned spending" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["budget"]["declared_income_display"] == "$5,600.00"
    assert payload["totals"]["planned_est_high_total_display"] == "$190.00"
    assert payload["budget_lines"][0]["actual_spend_display"] == "$0.00"
    assert payload["budget_lines"][0]["projected_high_total_display"] == "$190.00"
    assert payload["budget_lines"][0]["pressure_basis"] == "week-ahead planned spending"


def test_chat_prompt_includes_supplemental_context_sections():
    messages = chat_prompt.build(
        "What should I focus on this month?",
        [{"role": "user", "content": "hello"}],
        {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 7000,
                "planned_est_high_total": 19000,
                "remaining_income_high": 534000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 0,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                }
            ],
            "coach_proposals": [],
        },
        {
            "context_hints": {"ui_action": "line-grounded-advice", "target_category": "Dining"},
            "mcp_transactions": {
                "tool_name": "search_transactions",
                "count": 1,
                "scope": "Dining",
                "total_matched_amount": 18.0,
                "result_preview": [
                    {"date": "2026-09-01", "merchant": "Cafe", "amount": 18.0, "category_name": "Dining"}
                ],
            },
            "rag_guidance": {
                "retrieval": [
                    {
                        "id": "budgets-1",
                        "source": "budgets/budget-guidance.md",
                        "distance": 0.42,
                        "text": "# Budget Coach guidance\nFocus on categories under pressure from planned events first.",
                    }
                ]
            },
        },
    )

    payload = "\n".join(message["content"] for message in messages if message.get("role") == "system")

    assert "Context hints:" in payload
    assert "Shared MCP transaction evidence" in payload
    assert "Retrieved guidance" in payload
    assert "Focus on categories under pressure from planned events first." in payload


def test_chat_service_answers_overspending_from_current_thresholds(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 8,
                    "category": "Mobile",
                    "actual_spend": 12000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 12000,
                    "warn_at": 7500,
                    "hard_cap": 10000,
                },
                {
                    "id": 9,
                    "category": "Groceries",
                    "actual_spend": 12500,
                    "planned_est_high_total": 6000,
                    "projected_high_total": 18500,
                    "warn_at": 22000,
                    "hard_cap": 40000,
                },
            ],
        },
    )
    monkeypatch.setattr(
        guard,
        "run",
        lambda *_args, **_kwargs: {
            "mode": "advice",
            "say": "Mobile is the line under the most pressure right now because it is already over its hard cap.",
            "question": None,
            "proposal": None,
            "fallback": False,
        },
    )

    result = chat_service.send_message(1, "Where am I overspending most?")

    assert result["mode"] == "advice"
    assert result["response_source"] == "ollama"
    assert result["reply"] == "Mobile is the line under the most pressure right now because it is already over its hard cap."
    assert result["agentic_workflow"]["plan"]["execution_path"] == "ollama"
    assert result["agentic_workflow"]["plan"]["execution_reason"] == "llm_first_auto"


def test_chat_service_reuses_recent_amount_for_budget_impact(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
            {"role": "user", "content": "can i afford to go take my partner out to a fancy restaurant for dinner"},
            {"role": "assistant", "content": "Tell me the rough amount you are considering."},
            {"role": "user", "content": "probably at least $80"},
            {"role": "assistant", "content": "Okay."},
        ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [],
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "That extra $80.00 would reduce your projected remaining income from $4,254.00 to $4,174.00 this month.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "how does it affect my budget", history)

    assert result["mode"] == "advice"
    assert result["response_source"] == "ollama"
    assert result["question"] is None
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Requested spend: $80.00" in system_context
    assert "Remaining income before spend: $4,254.00" in system_context
    assert "Remaining income after spend: $4,174.00" in system_context


def test_chat_service_answers_category_only_affordability_from_budget(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 60000,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79000,
                    "warn_at": 70000,
                    "hard_cap": 85000,
                },
            ],
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Dining is already in warning range, but you still have $60.00 before the hard cap if you eat out again this month.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "Can I still afford to eat out this month?")

    assert result["response_source"] == "ollama"
    assert result["mode"] == "advice"
    assert result["question"] is None
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Affordability target: Dining" in system_context
    assert "Requested spend: unknown" in system_context
    assert "Current projected spend: $790.00" in system_context
    assert "Current threshold state: warning" in system_context


def test_chat_service_answers_spend_more_question_from_current_mobile_budget(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 8,
                    "category": "Mobile",
                    "actual_spend": 12000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 12000,
                    "warn_at": 7500,
                    "hard_cap": 10000,
                },
            ],
        },
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "am i able to spend more in mobile")

    assert result["mode"] == "advice"
    assert result["proposal"] is None
    assert "Mobile is already over its hard cap." in result["reply"]


def test_chat_service_recognises_unbudgeted_spend_question_wording(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 20000,
                "planned_est_high_total": 0,
                "remaining_income_high": 540000,
            },
            "budget_lines": [
                {
                    "id": 6,
                    "category": "Music subscriptions",
                    "actual_spend": 20000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 20000,
                    "hard_cap": 17000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "what am I spending the most on that I'm not tracking with a budget")

    assert result["mode"] == "advice"
    assert result["proposal"] is None
    assert result["reply"] == "I cannot see any spending categories outside your current budget lines for this month."


def test_chat_service_keeps_music_subscription_affordability_grounded(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 20000,
                "planned_est_high_total": 0,
                "remaining_income_high": 521000,
            },
            "budget_lines": [
                {
                    "id": 6,
                    "category": "Music subscriptions",
                    "actual_spend": 20000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 20000,
                    "warn_at": 15000,
                    "hard_cap": 17000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Another $50.00 music subscription would take Music subscriptions to $250.00 projected, which is $80.00 over the hard cap.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "can i afford a new monthly music subscription of 50$ a month")

    assert result["mode"] == "advice"
    assert result["response_source"] == "ollama"
    assert result["proposal"] is None
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Affordability target: Music subscriptions" in system_context
    assert "Requested spend: $50.00" in system_context
    assert "Projected spend after request: $250.00" in system_context


def test_chat_service_treats_category_only_follow_up_as_affordability_context(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
        {"role": "user", "content": "can i afford a new monthly music subscription of 50$ a month"},
        {"role": "assistant", "content": "Tell me the rough amount you are considering."},
    ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 20000,
                "planned_est_high_total": 0,
                "remaining_income_high": 521000,
            },
            "budget_lines": [
                {
                    "id": 6,
                    "category": "Music subscriptions",
                    "actual_spend": 20000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 20000,
                    "warn_at": 15000,
                    "hard_cap": 17000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "If you mean Music subscriptions, adding that $50.00 would take the line to $250.00 projected.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "a music subscription", history)

    assert result["mode"] == "advice"
    assert result["response_source"] == "ollama"
    assert result["proposal"] is None
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Affordability target: Music subscriptions" in system_context
    assert "Requested spend: $50.00" in system_context


def test_chat_service_uses_latest_open_proposal_for_increase_it_another_amount(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 20000,
                "planned_est_high_total": 0,
                "remaining_income_high": 521000,
            },
            "budget_lines": [
                {
                    "id": 6,
                    "category": "Music subscriptions",
                    "actual_spend": 20000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 20000,
                    "warn_at": 17000,
                    "hard_cap": 22000,
                },
            ],
            "coach_proposals": [
                {
                    "id": 60,
                    "budget_id": 1,
                    "status": "proposed",
                    "proposal_json": {
                        "proposal_type": "adjust_budget_line_thresholds",
                        "summary": "Review Music subscriptions warning and hard-cap values.",
                        "operations": [
                            {
                                "action": "update_budget_line",
                                "budget_line_id": 6,
                                "category": "Music subscriptions",
                                "fields": {"warn_at": 20000, "hard_cap": 22000},
                            }
                        ],
                    },
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    deleted_ids = []
    stored = {}
    monkeypatch.setattr(db_api, "delete_coach_proposal", lambda proposal_id: deleted_ids.append(int(proposal_id)) or (None, 204))

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 61,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "increase it another 50")

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 61
    assert deleted_ids == [60]
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 6
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 24000, "hard_cap": 27000}


def test_chat_service_multiplies_follow_up_quantity_from_recent_affordability_context(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
        {"role": "user", "content": "can i afford another monthly $50 music subscription?"},
        {"role": "assistant", "content": "Music subscriptions is currently spent $200.00."},
    ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 20000,
                "planned_est_high_total": 0,
                "remaining_income_high": 521000,
            },
            "budget_lines": [
                {
                    "id": 6,
                    "category": "Music subscriptions",
                    "actual_spend": 20000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 20000,
                    "warn_at": 24000,
                    "hard_cap": 27000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Two more would add $100.00, taking Music subscriptions to $300.00 projected.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "what about 2", history)

    assert result["mode"] == "advice"
    assert result["response_source"] == "ollama"
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Requested spend: $100.00" in system_context
    assert "Projected spend after request: $300.00" in system_context


def test_chat_service_multiplies_two_50_music_subscriptions(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
        {"role": "user", "content": "can i afford another monthly $50 music subscription?"},
        {"role": "assistant", "content": "Music subscriptions is currently spent $200.00."},
    ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 20000,
                "planned_est_high_total": 0,
                "remaining_income_high": 521000,
            },
            "budget_lines": [
                {
                    "id": 6,
                    "category": "Music subscriptions",
                    "actual_spend": 20000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 20000,
                    "warn_at": 24000,
                    "hard_cap": 27000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Two $50.00 subscriptions would add $100.00, taking Music subscriptions to $300.00 projected.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "what about 2 $50 music subscriptions", history)

    assert result["mode"] == "advice"
    assert result["response_source"] == "ollama"
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Requested spend: $100.00" in system_context
    assert "Projected spend after request: $300.00" in system_context


def test_chat_service_uses_recent_music_context_for_accommodate_follow_up(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
        {"role": "user", "content": "I want to be able to afford another $100 of music subscriptions per month"},
        {"role": "assistant", "content": "No, that would put Music subscriptions over its hard cap."},
    ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 39000,
                "planned_est_high_total": 0,
                "remaining_income_high": 521000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 19000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                },
                {
                    "id": 6,
                    "category": "Music subscriptions",
                    "actual_spend": 20000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 20000,
                    "warn_at": 24000,
                    "hard_cap": 27000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 70,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "I want to increase my budget to accomodate", history)

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 70
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 6
    assert stored["proposal_json"]["operations"][0]["category"] == "Music subscriptions"
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 34000, "hard_cap": 37000}


def test_chat_service_summarises_music_budget_line_without_stale_affordability(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
        {"role": "user", "content": "can i afford another monthly $50 music subscription?"},
        {"role": "assistant", "content": "Maybe, but that would put Music subscriptions into warning range."},
    ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 20000,
                "planned_est_high_total": 0,
                "remaining_income_high": 521000,
            },
            "budget_lines": [
                {
                    "id": 6,
                    "category": "Music subscriptions",
                    "actual_spend": 20000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 20000,
                    "warn_at": 24000,
                    "hard_cap": 27000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "Music subscriptions is currently at $200.00 projected, which is still within its thresholds but worth watching.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "tell me about my music subscription budget line", history)

    assert result["response_source"] == "ollama"
    assert result["mode"] == "advice"
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Focus line: Music subscriptions at $200.00" in system_context
    assert "Music subscriptions | projected $200.00 | warn $240.00 | cap $270.00 | state=within_threshold" in system_context


def test_chat_service_revises_transport_proposal_for_spend_at_least_target(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
        {"role": "user", "content": "I want to allow myself to spend more on transport"},
        {"role": "assistant", "content": "I revised the proposal to move that budget line's warning amount to $160.00 and hard cap to $180.00 for your review."},
    ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 16000,
                "planned_est_high_total": 0,
                "remaining_income_high": 521000,
            },
            "budget_lines": [
                {
                    "id": 4,
                    "category": "Transport",
                    "actual_spend": 16000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 16000,
                    "warn_at": 10000,
                    "hard_cap": 13000,
                },
            ],
            "coach_proposals": [
                {
                    "id": 80,
                    "budget_id": 1,
                    "status": "proposed",
                    "proposal_json": {
                        "proposal_type": "adjust_budget_line_thresholds",
                        "summary": "Review Transport warning and hard-cap values.",
                        "operations": [
                            {
                                "action": "update_budget_line",
                                "budget_line_id": 4,
                                "category": "Transport",
                                "fields": {"warn_at": 16000, "hard_cap": 18000},
                            }
                        ],
                    },
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    deleted_ids = []
    stored = {}
    monkeypatch.setattr(db_api, "delete_coach_proposal", lambda proposal_id: deleted_ids.append(int(proposal_id)) or (None, 204))

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 81,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "I want to be able to spend at least $200", history)

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 81
    assert deleted_ids == [80]
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 4
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 18000, "hard_cap": 20000}
    assert result["reply"] == "I revised the proposal to move Transport's warning amount to $180.00 and hard cap to $200.00 for your review."


def test_chat_service_switches_topic_to_category_remaining(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
            {"role": "user", "content": "am i able to take my partner out to a nice restaurant for dinner"},
            {"role": "assistant", "content": "How much would that cost?"},
            {"role": "user", "content": "about $90"},
            {"role": "assistant", "content": "It depends on Dining."},
            {"role": "user", "content": "ok moving on then, how much am i free to spend on groceries this month"},
        ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "category": "Dining",
                    "actual_spend": 60000,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79000,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
                {
                    "category": "Groceries",
                    "actual_spend": 12500,
                    "planned_est_high_total": 6000,
                    "projected_high_total": 18500,
                    "warn_at": 22000,
                    "hard_cap": 40000,
                },
            ],
        },
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "ok moving on then, how much am i free to spend on groceries this month", history)

    assert result["mode"] == "advice"
    assert "Groceries" in result["reply"]
    assert "Dining" not in result["reply"]
    assert "$35.00 before warning" in result["reply"]
    assert "$215.00 before hard cap" in result["reply"]


def test_chat_service_answers_spend_most_without_stale_affordability_context(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
            {"role": "user", "content": "am i able to take my partner out to a nice restaurant for dinner"},
            {"role": "assistant", "content": "How much would that cost?"},
            {"role": "user", "content": "about $90"},
        ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "category": "Dining",
                    "actual_spend": 60000,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79000,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
                {
                    "category": "Groceries",
                    "actual_spend": 12500,
                    "planned_est_high_total": 6000,
                    "projected_high_total": 18500,
                    "warn_at": 22000,
                    "hard_cap": 40000,
                },
            ],
        },
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "what am i spending the most money on this month", history)

    assert result["mode"] == "advice"
    assert "Dining" in result["reply"]
    assert "$600.00 so far" in result["reply"]
    assert "Groceries" not in result["reply"]


def test_chat_service_answers_savings_question_from_budget_data(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "category": "Dining",
                    "actual_spend": 60000,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79000,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
                {
                    "category": "Fitness",
                    "actual_spend": 25000,
                    "planned_est_high_total": 12000,
                    "projected_high_total": 37000,
                    "warn_at": 30000,
                    "hard_cap": 45000,
                },
            ],
        },
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "how can i save money")

    assert result["mode"] == "advice"
    assert "Dining" in result["reply"]
    assert "Fitness" in result["reply"]
    assert "save" in result["reply"].casefold()


def test_chat_service_answers_yes_no_follow_up_from_recent_affordability_context(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
            {"role": "user", "content": "am i able to take my partner out to a nice restaurant for dinner"},
            {"role": "assistant", "content": "How much would that cost approximately?"},
            {"role": "user", "content": "about $90"},
            {"role": "assistant", "content": "That would affect Dining."},
        ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "category": "Dining",
                    "actual_spend": 60000,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79000,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
            ],
        },
    )
    captured = {}

    def fake_run(_model, build_messages, _validator, _fallback):
        captured["messages"] = build_messages(None)
        return {
            "mode": "advice",
            "say": "No, another $90.00 would push Dining further over its hard cap this month.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }

    monkeypatch.setattr(guard, "run", fake_run)

    result = chat_service.send_message(1, "oh but i wont be over?", history)

    assert result["mode"] == "advice"
    assert result["response_source"] == "ollama"
    system_context = "\n".join(
        message["content"]
        for message in captured["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert "Affordability target: Dining" in system_context
    assert "Requested spend: $90.00" in system_context


def test_chat_service_creates_reviewable_adjustment_proposal(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 60100,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79100,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 12,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "what adjustments should i make to my budgets")

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 12
    assert stored["proposal_json"]["proposal_type"] == "adjust_budget_line_thresholds"
    assert stored["proposal_json"]["operations"][0]["action"] == "update_budget_line"
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 3
    assert "Dining is projected to reach $791.00" in result["reply"]
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 80000, "hard_cap": 90000}


def test_chat_service_keeps_increase_proposal_safe_against_projected_spend(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 60100,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79100,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 13,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "i would like to increase the dining budget by $200 i think")

    fields = stored["proposal_json"]["operations"][0]["fields"]

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 13
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 3
    assert fields["warn_at"] == 80000
    assert fields["hard_cap"] == 90000
    assert "A $200.00 increase would still leave Dining below its projected $791.00" in result["reply"]


def test_chat_service_uses_recent_adjustment_context_for_follow_up_ideas(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    history = [
        {"role": "user", "content": "where am i spending overbudget the most"},
        {"role": "assistant", "content": "Dining is under the most pressure this month."},
        {"role": "user", "content": "i would like to increase the dining budget by $200 i think"},
        {"role": "assistant", "content": "Okay."},
    ]
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 60100,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79100,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 14,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "hit me with ideas", history)

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 14
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 3
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 80000, "hard_cap": 90000}


def test_chat_service_uses_latest_open_mobile_proposal_for_enough_follow_up(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 8,
                    "category": "Mobile",
                    "actual_spend": 12000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 12000,
                    "warn_at": 7500,
                    "hard_cap": 10000,
                },
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 60100,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79100,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
            ],
            "coach_proposals": [
                {
                    "id": 21,
                    "budget_id": 1,
                    "status": "proposed",
                    "proposal_json": {
                        "proposal_type": "adjust_budget_line_thresholds",
                        "summary": "Review Dining warning and hard-cap values.",
                        "operations": [
                            {
                                "action": "update_budget_line",
                                "budget_line_id": 3,
                                "category": "Dining",
                                "fields": {"warn_at": 80000, "hard_cap": 90000},
                            }
                        ],
                    },
                },
                {
                    "id": 22,
                    "budget_id": 1,
                    "status": "proposed",
                    "proposal_json": {
                        "proposal_type": "adjust_budget_line_thresholds",
                        "summary": "Review Mobile warning and hard-cap values.",
                        "operations": [
                            {
                                "action": "update_budget_line",
                                "budget_line_id": 8,
                                "category": "Mobile",
                                "fields": {"warn_at": 13000, "hard_cap": 14000},
                            }
                        ],
                    },
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    deleted_ids = []
    stored = {}
    monkeypatch.setattr(db_api, "delete_coach_proposal", lambda proposal_id: deleted_ids.append(int(proposal_id)) or (None, 204))

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 23,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "im not sure if that will be enough")

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 23
    assert deleted_ids == [22]
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 8
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 14000, "hard_cap": 15000}
    assert "I adjusted the suggestion upward for Mobile." in result["reply"]


def test_chat_service_increases_mobile_proposal_by_requested_amount(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 8,
                    "category": "Mobile",
                    "actual_spend": 12000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 12000,
                    "warn_at": 7500,
                    "hard_cap": 10000,
                },
            ],
            "coach_proposals": [
                {
                    "id": 22,
                    "budget_id": 1,
                    "status": "proposed",
                    "proposal_json": {
                        "proposal_type": "adjust_budget_line_thresholds",
                        "summary": "Review Mobile warning and hard-cap values.",
                        "operations": [
                            {
                                "action": "update_budget_line",
                                "budget_line_id": 8,
                                "category": "Mobile",
                                "fields": {"warn_at": 13000, "hard_cap": 14000},
                            }
                        ],
                    },
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    deleted_ids = []
    stored = {}
    monkeypatch.setattr(db_api, "delete_coach_proposal", lambda proposal_id: deleted_ids.append(int(proposal_id)) or (None, 204))

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 24,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    history = [
        {"role": "user", "content": "i want to increase my mobile budget"},
        {"role": "assistant", "content": "Mobile is projected to reach $120.00 this month against its current warning amount of $75.00 and hard cap of $100.00."},
    ]
    result = chat_service.send_message(1, "can you increase your mobile proposal by a little bit? maybe $20?", history)

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 24
    assert deleted_ids == [22]
    assert stored["proposal_json"]["operations"][0]["budget_line_id"] == 8
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 15000, "hard_cap": 16000}
    assert "I adjusted the suggestion upward for Mobile." in result["reply"]


def test_chat_service_extends_existing_utilities_proposal(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 26000,
                "planned_est_high_total": 0,
                "remaining_income_high": 503000,
            },
            "budget_lines": [
                {
                    "id": 10,
                    "category": "Utilities",
                    "actual_spend": 26000,
                    "planned_est_high_total": 0,
                    "projected_high_total": 26000,
                    "warn_at": 16000,
                    "hard_cap": 20000,
                },
            ],
            "coach_proposals": [
                {
                    "id": 30,
                    "budget_id": 1,
                    "status": "proposed",
                    "proposal_json": {
                        "proposal_type": "adjust_budget_line_thresholds",
                        "summary": "Review Utilities warning and hard-cap values.",
                        "operations": [
                            {
                                "action": "update_budget_line",
                                "budget_line_id": 10,
                                "category": "Utilities",
                                "fields": {"warn_at": 26000, "hard_cap": 29000},
                            }
                        ],
                    },
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    deleted_ids = []
    stored = {}
    monkeypatch.setattr(db_api, "delete_coach_proposal", lambda proposal_id: deleted_ids.append(int(proposal_id)) or (None, 204))

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 31,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "can you extend it a little further")

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 31
    assert deleted_ids == [30]
    fields = stored["proposal_json"]["operations"][0]["fields"]
    assert fields["warn_at"] > 26000
    assert fields["hard_cap"] > 29000


def test_chat_service_uses_explicit_cap_target_instead_of_stacking_increase(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 29000,
                "planned_est_high_total": 0,
                "remaining_income_high": 531000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 10000,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 29000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                },
            ],
            "coach_proposals": [
                {
                    "id": 41,
                    "budget_id": 1,
                    "status": "proposed",
                    "proposal_json": {
                        "proposal_type": "adjust_budget_line_thresholds",
                        "summary": "Review Dining warning and hard-cap values.",
                        "operations": [
                            {
                                "action": "update_budget_line",
                                "budget_line_id": 3,
                                "category": "Dining",
                                "fields": {"warn_at": 19000, "hard_cap": 31000},
                            }
                        ],
                    },
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    deleted_ids = []
    stored = {}
    monkeypatch.setattr(db_api, "delete_coach_proposal", lambda proposal_id: deleted_ids.append(int(proposal_id)) or (None, 204))

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 42,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "can you increase your proposal to a cap of 350 instead?")

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 42
    assert deleted_ids == [41]
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 32000, "hard_cap": 35000}
    assert result["reply"] == "I revised the proposal to move Dining's warning amount to $320.00 and hard cap to $350.00 for your review."


def test_chat_service_uses_explicit_warn_and_cap_targets(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 29000,
                "planned_est_high_total": 0,
                "remaining_income_high": 531000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 10000,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 29000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                },
            ],
            "coach_proposals": [
                {
                    "id": 42,
                    "budget_id": 1,
                    "status": "proposed",
                    "proposal_json": {
                        "proposal_type": "adjust_budget_line_thresholds",
                        "summary": "Review Dining warning and hard-cap values.",
                        "operations": [
                            {
                                "action": "update_budget_line",
                                "budget_line_id": 3,
                                "category": "Dining",
                                "fields": {"warn_at": 32000, "hard_cap": 35000},
                            }
                        ],
                    },
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    deleted_ids = []
    stored = {}
    monkeypatch.setattr(db_api, "delete_coach_proposal", lambda proposal_id: deleted_ids.append(int(proposal_id)) or (None, 204))

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 43,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "no i want the cap to be $350, warn at $325")

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 43
    assert deleted_ids == [42]
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 32500, "hard_cap": 35000}
    assert result["reply"] == "I revised the proposal to move Dining's warning amount to $325.00 and hard cap to $350.00 for your review."


def test_chat_service_acknowledgement_does_not_spawn_proposal(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 19000,
                "planned_est_high_total": 0,
                "remaining_income_high": 541000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 0,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    monkeypatch.setattr(
        db_api,
        "create_coach_proposal",
        lambda *_args, **_kwargs: pytest.fail("create_coach_proposal should not be called"),
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "awesome thank you")

    assert result["mode"] == "advice"
    assert result["reply"] == "Okay."
    assert result["proposal"] is None


def test_chat_service_creates_requested_lower_cap_proposal(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 19000,
                "planned_est_high_total": 0,
                "remaining_income_high": 541000,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 0,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 19000,
                    "warn_at": 19000,
                    "hard_cap": 24000,
                },
            ],
            "transactions": {"other_expenses": []},
        },
    )
    stored = {}

    def create_coach_proposal(_budget_id, payload):
        stored.update(payload)
        return {
            "id": 32,
            "budget_id": 1,
            "proposal_json": payload["proposal_json"],
            "rationale": payload["rationale"],
            "status": "proposed",
        }, 201

    monkeypatch.setattr(db_api, "create_coach_proposal", create_coach_proposal)
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "make me a suggestion to lower the dining cap to 200")

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 32
    assert stored["proposal_json"]["operations"][0]["fields"] == {"warn_at": 19000, "hard_cap": 20000}
    assert "because you asked for it I prepared a proposal" in result["reply"]

def test_chat_service_reuses_equivalent_open_proposal(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda _budget_id: {"id": 1, "month": "2026-09"})
    existing_proposal = {
        "id": 11,
        "budget_id": 1,
        "status": "proposed",
        "proposal_json": {
            "proposal_type": "adjust_budget_line_thresholds",
            "summary": "Review Dining warning and hard-cap values.",
            "operations": [
                {
                    "action": "update_budget_line",
                    "budget_line_id": 3,
                    "category": "Dining",
                    "fields": {"warn_at": 80000, "hard_cap": 90000},
                }
            ],
        },
        "rationale": "Existing proposal.",
    }
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: {
            "budget": {"id": 1, "month": "2026-09", "declared_income": 560000},
            "totals": {
                "declared_income": 560000,
                "actual_spend_total": 107100,
                "planned_est_high_total": 27500,
                "remaining_income_high": 425400,
            },
            "budget_lines": [
                {
                    "id": 3,
                    "category": "Dining",
                    "actual_spend": 60100,
                    "planned_est_high_total": 19000,
                    "projected_high_total": 79100,
                    "warn_at": 18000,
                    "hard_cap": 24000,
                },
            ],
            "coach_proposals": [existing_proposal],
            "transactions": {"other_expenses": []},
        },
    )
    monkeypatch.setattr(
        db_api,
        "create_coach_proposal",
        lambda *_args, **_kwargs: pytest.fail("create_coach_proposal should not be called"),
    )
    monkeypatch.setattr(guard, "run", lambda *args, **kwargs: pytest.fail("guard should not be called"))

    result = chat_service.send_message(1, "what adjustments should i make to my budgets")

    assert result["mode"] == "proposal"
    assert result["proposal"]["id"] == 11


def test_apply_proposal_updates_budget_line_and_marks_proposal_accepted(monkeypatch):
    snapshots = iter(
        [
            {"budget": {"month": "2026-09"}, "totals": {"projected_high_total": 79100, "remaining_income_high": 425400}, "budget_lines": [{"id": 3}]},
            {"budget": {"month": "2026-09"}, "totals": {"projected_high_total": 79100, "remaining_income_high": 425400}, "budget_lines": [{"id": 3}]},
        ]
    )
    monkeypatch.setattr(
        db_api,
        "get_coach_proposal",
        lambda proposal_id: {
            "id": int(proposal_id),
            "budget_id": 1,
            "status": "proposed",
            "proposal_json": {
                "proposal_type": "adjust_budget_line_thresholds",
                "operations": [
                    {
                        "action": "update_budget_line",
                        "budget_line_id": 3,
                        "fields": {"warn_at": 72000, "hard_cap": 85000},
                    }
                ],
            },
        },
    )
    monkeypatch.setattr(
        db_api,
        "get_budget_line",
        lambda line_id: {"id": int(line_id), "budget_id": 1, "warn_at": 18000, "hard_cap": 24000},
    )
    monkeypatch.setattr(
        db_api,
        "update_budget_line",
        lambda line_id, payload: {"id": int(line_id), **payload},
    )
    monkeypatch.setattr(
        db_api,
        "update_coach_proposal",
        lambda proposal_id, payload: {"id": int(proposal_id), "status": payload["status"]},
    )
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda _budget_id: next(snapshots),
    )

    result = proposal_service.apply(7)

    assert result["proposal"] == {"id": 7, "status": "accepted"}
    assert result["applied"] == [{"id": 3, "warn_at": 72000, "hard_cap": 85000}]
    assert result["stage_trace"] == ["observe", "plan", "act", "observe", "adapt"]
    assert result["agentic_workflow"]["plan"]["operation_count"] == 1
def test_patch_planned_event(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "update_planned_event",
        lambda event_id, payload: {
            "id": int(event_id),
            "status": payload["status"],
        },
    )

    resp = _client().patch("/api/planned-events/7", json={"status": "cancelled"})

    assert resp.status_code == 200
    assert resp.get_json() == {
        "id": 7,
        "status": "cancelled",
    }


def test_budget_snapshot_aggregates_child_collections(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda budget_id: {"id": budget_id, "month": "2026-09"})
    monkeypatch.setattr(db_api, "list_budget_lines", lambda budget_id: [{"id": 1, "budget_id": budget_id}])
    monkeypatch.setattr(db_api, "list_planned_events", lambda budget_id: [{"id": 1, "budget_id": budget_id}])
    monkeypatch.setattr(db_api, "list_coach_proposals", lambda budget_id: [{"id": 1, "budget_id": budget_id}])

    resp = _client().get("/api/budgets/b1/snapshot")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "budget": {"id": "b1", "month": "2026-09"},
        "budget_lines": [{"id": 1, "budget_id": "b1"}],
        "planned_events": [{"id": 1, "budget_id": "b1"}],
        "coach_proposals": [{"id": 1, "budget_id": "b1"}],
    }


def test_budget_summary_route(monkeypatch):
    monkeypatch.setattr(
        summary_service,
        "build_budget_summary",
        lambda budget_id: {
            "budget": {"id": budget_id},
            "totals": {"actual_spend_total": 12345},
        },
    )

    resp = _client().get("/api/budgets/b1/summary")

    assert resp.status_code == 200
    assert resp.get_json() == {
        "budget": {"id": "b1"},
        "totals": {"actual_spend_total": 12345},
    }


def test_service_error_from_database_is_forwarded(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "get_budget",
        lambda _budget_id: (_ for _ in ()).throw(
            db_api.ServiceError("budget not found", 404, "budget_not_found")
        ),
    )

    resp = _client().get("/api/budgets/missing")

    assert resp.status_code == 404
    assert resp.get_json() == {
        "error": "budget not found",
        "code": "budget_not_found",
    }


def test_database_unavailable_returns_503(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "list_budgets",
        lambda: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )

    resp = _client().get("/api/budgets")

    assert resp.status_code == 503
    assert resp.get_json()["code"] == "database_unavailable"


def test_budget_summary_returns_transactions_unavailable(monkeypatch):
    monkeypatch.setattr(db_api, "get_budget", lambda budget_id: {"id": budget_id, "month": "2026-01"})
    monkeypatch.setattr(db_api, "list_budget_lines", lambda _budget_id: [])
    monkeypatch.setattr(db_api, "list_planned_events", lambda _budget_id: [])
    monkeypatch.setattr(db_api, "list_coach_proposals", lambda _budget_id: [])
    monkeypatch.setattr(transactions_api, "list_categories", lambda: [])
    monkeypatch.setattr(
        transactions_api,
        "list_transactions_for_month",
        lambda _month: (_ for _ in ()).throw(
            db_api.ServiceError("transactions API is unavailable", 503, "transactions_unavailable")
        ),
    )

    resp = _client().get("/api/budgets/b1/summary")

    assert resp.status_code == 503
    assert resp.get_json() == {
        "error": "transactions API is unavailable",
        "code": "transactions_unavailable",
    }


def test_budget_summary_calculates_actual_and_planned_totals(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "get_budget",
        lambda _budget_id: {"id": "b1", "month": "2026-09", "declared_income": 30000},
    )
    monkeypatch.setattr(
        db_api,
        "list_budget_lines",
        lambda _budget_id: [
            {"id": 1, "budget_id": "b1", "category_id": 80, "category": "Dining", "warn_at": 10000, "hard_cap": 15000},
            {"id": 2, "budget_id": "b1", "category_id": 81, "category": "Groceries", "warn_at": 12000, "hard_cap": 18000},
        ],
    )
    monkeypatch.setattr(
        db_api,
        "list_planned_events",
        lambda _budget_id: [
            {"id": 1, "budget_id": "b1", "category": "Dining", "est_low": 2000, "est_high": 3000, "status": "planned"},
            {"id": 2, "budget_id": "b1", "category": "Groceries", "est_low": 4000, "est_high": 6000, "status": "confirmed"},
            {"id": 3, "budget_id": "b1", "category": "Dining", "est_low": 9999, "est_high": 9999, "status": "cancelled"},
        ],
    )
    monkeypatch.setattr(db_api, "list_coach_proposals", lambda _budget_id: [])
    monkeypatch.setattr(
        transactions_api,
        "list_categories",
        lambda: [{"id": 80, "name": "Dining"}, {"id": 81, "name": "Groceries"}],
    )
    monkeypatch.setattr(
        transactions_api,
        "list_transactions_for_month",
        lambda _month: [
            {"id": 1, "amount": 42.5, "category_id": 80},
            {"id": 2, "amount": 16.25, "category_id": 80},
            {"id": 3, "amount": 91.0, "category_id": 81},
            {"id": 4, "amount": 11.0, "category_id": 999},
        ],
    )

    summary = summary_service.build_budget_summary("b1")

    assert summary["transactions"] == {
        "count": 4,
        "uncategorised_total": 1100,
        "other_expenses": [],
    }
    assert summary["totals"] == {
        "declared_income": 30000,
        "actual_spend_total": 14975,
        "planned_est_low_total": 6000,
        "planned_est_high_total": 9000,
        "budget_warn_total": 22000,
        "budget_cap_total": 33000,
        "projected_low_total": 20975,
        "projected_high_total": 23975,
        "remaining_income_low": 9025,
        "remaining_income_high": 6025,
    }
    dining_line = next(line for line in summary["budget_lines"] if line["category"] == "Dining")
    assert dining_line["actual_spend"] == 5875
    assert dining_line["planned_est_low_total"] == 2000
    assert dining_line["planned_est_high_total"] == 3000
    assert dining_line["warning_state"] is False
    assert dining_line["cap_state"] is False
    groceries_line = next(line for line in summary["budget_lines"] if line["category"] == "Groceries")
    assert groceries_line["actual_spend"] == 9100
    assert groceries_line["planned_est_low_total"] == 4000
    assert groceries_line["planned_est_high_total"] == 6000
    assert groceries_line["warning_state"] is False
    assert groceries_line["cap_state"] is False


def test_budget_summary_lists_other_expense_categories_without_budget_lines(monkeypatch):
    monkeypatch.setattr(
        db_api,
        "get_budget",
        lambda _budget_id: {"id": "b1", "month": "2026-09", "declared_income": 50000},
    )
    monkeypatch.setattr(
        db_api,
        "list_budget_lines",
        lambda _budget_id: [
            {"id": 1, "budget_id": "b1", "category_id": 80, "category": "Dining", "warn_at": 10000, "hard_cap": 15000},
        ],
    )
    monkeypatch.setattr(db_api, "list_planned_events", lambda _budget_id: [])
    monkeypatch.setattr(db_api, "list_coach_proposals", lambda _budget_id: [])
    monkeypatch.setattr(
        transactions_api,
        "list_categories",
        lambda: [{"id": 80, "name": "Dining"}, {"id": 81, "name": "Groceries"}, {"id": 82, "name": "Fuel"}],
    )
    monkeypatch.setattr(
        transactions_api,
        "list_transactions_for_month",
        lambda _month: [
            {"id": 1, "amount": 25.0, "category_id": 80},
            {"id": 2, "amount": 35.0, "category_id": 81},
            {"id": 3, "amount": 22.5, "category_id": 82},
            {"id": 4, "amount": 5.0, "category_id": 81},
        ],
    )

    summary = summary_service.build_budget_summary("b1")

    assert summary["transactions"] == {
        "count": 4,
        "uncategorised_total": 0,
        "other_expenses": [
            {"category_id": 82, "category": "Fuel", "actual_spend": 2250},
            {"category_id": 81, "category": "Groceries", "actual_spend": 4000},
        ],
    }
