import json
import os
from unittest.mock import patch
from datetime import datetime
from flask import Flask
from flask.testing import FlaskClient
import httpx
import pytest
import responses
from responses import matchers

from backend.savings_service.app import setup_app
from backend.savings_service import ollama_service, suggestion_service
from shared.backend import dto

DB_URL = "http://savings-db:6002"
TX_URL = "http://transactions-db:6001"


@pytest.fixture()
def app():
    app = setup_app(DB_URL, TX_URL)
    app.config.update({"TESTING": True})
    yield app


@pytest.fixture()
def client(app: Flask):
    return app.test_client()


@pytest.fixture()
def app_ctx(app: Flask):
    with app.app_context():
        yield


@pytest.fixture(autouse=True)
def mock_ollama_client():
    def handler(request: httpx.Request) -> httpx.Response:
        data = {
            "intent": "focus",
            "category": "Dining",
            "timeframe": "2 weeks",
        }
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 123456789,
                "model": "qwen2.5:3b",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(data),
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    orig_client = ollama_service.client
    ollama_service.client = ollama_service.OpenAI(
        base_url="http://mock-ollama/v1",
        api_key="ollama",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    yield
    ollama_service.client = orig_client


# ============================================================================
# Goal Endpoints (With Request Payload Verification)
# ============================================================================

@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_goals_renders_table(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/goals",
        json=[
            {"id": 1, "name": "Trip to Japan", "cost": 3000, "date": "2026-11-20T00:00:00"},
            {"id": 2, "name": "Emergency Fund", "cost": 5000, "date": "2026-12-31T00:00:00"},
        ],
        status=200,
    )
    response = client.get("/goals")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Trip to Japan" in html
    assert "Emergency Fund" in html
    assert "3000" in html


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_create_goal_valid_json_verifies_db_payload(client: FlaskClient):
    goal_payload = {"name": "New Laptop", "cost": 1500, "date": "2026-10-15T00:00:00"}
    # match verifies that app.py actually transmitted the exact payload to the database!
    responses.add(
        responses.POST,
        f"{DB_URL}/goal",
        match=[matchers.json_params_matcher(goal_payload)],
        json={"id": 3, **goal_payload},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/goals",
        json=[{"id": 3, **goal_payload}],
        status=200,
    )

    response = client.post("/goal", json=goal_payload)
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "goalChanged"
    assert "New Laptop" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_create_goal_valid_form_verifies_db_payload(client: FlaskClient):
    goal_data = {"name": "Vacation", "cost": "800", "date": "2026-10-10T00:00:00"}
    responses.add(
        responses.POST,
        f"{DB_URL}/goal",
        match=[matchers.json_params_matcher(goal_data)],
        json={"id": 4, "name": "Vacation", "cost": 800, "date": "2026-10-10T00:00:00"},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/goals",
        json=[{"id": 4, "name": "Vacation", "cost": 800, "date": "2026-10-10T00:00:00"}],
        status=200,
    )

    response = client.post("/goal", data=goal_data)
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "goalChanged"
    assert "Vacation" in response.get_data(as_text=True)


@pytest.mark.usefixtures("app_ctx")
def test_create_goal_missing_fields_returns_400(client: FlaskClient):
    response = client.post("/goal", json={"name": "Incomplete Goal"})
    assert response.status_code == 400
    assert response.get_json() == {"error": "Missing goal fields"}


@pytest.mark.usefixtures("app_ctx")
def test_create_goal_invalid_date_returns_400(client: FlaskClient):
    response = client.post("/goal", json={"name": "Bad Date", "cost": 500, "date": "invalid-date"})
    assert response.status_code == 400
    assert "error" in response.get_json()


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_goal_by_id_success(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/goal/1",
        json={"id": 1, "name": "Trip to Japan", "cost": 3000, "date": "2026-11-20T00:00:00"},
        status=200,
    )
    response = client.get("/goal/1")
    assert response.status_code == 200
    assert "Trip to Japan" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_goal_by_id_not_found(client: FlaskClient):
    responses.add(responses.GET, f"{DB_URL}/goal/999", status=404)
    response = client.get("/goal/999")
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_edit_goal_renders_edit_template(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/goal/1",
        json={"id": 1, "name": "Trip to Japan", "cost": 3000, "date": "2026-11-20T00:00:00"},
        status=200,
    )
    response = client.get("/goal/1/edit")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Trip to Japan" in html
    assert "<form" in html or "hx-patch" in html or "input" in html


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_edit_goal_not_found(client: FlaskClient):
    responses.add(responses.GET, f"{DB_URL}/goal/999", status=404)
    response = client.get("/goal/999/edit")
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_update_goal_success_verifies_db_payload(client: FlaskClient):
    update_data = {"name": "Updated Trip", "cost": 3500}
    responses.add(
        responses.PATCH,
        f"{DB_URL}/goal/1",
        match=[matchers.json_params_matcher(update_data)],
        json={"id": 1, "name": "Updated Trip", "cost": 3500, "date": "2026-11-25T00:00:00"},
        status=200,
    )
    response = client.patch("/goal/1", json=update_data)
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "goalChanged"
    assert "Updated Trip" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_update_goal_not_found(client: FlaskClient):
    responses.add(responses.PATCH, f"{DB_URL}/goal/999", status=404)
    response = client.patch("/goal/999", json={"name": "Updated Trip"})
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_delete_goal_success(client: FlaskClient):
    responses.add(responses.DELETE, f"{DB_URL}/goal/1", status=204)
    responses.add(responses.GET, f"{DB_URL}/goals", json=[], status=200)

    response = client.delete("/goal/1")
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "goalChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_delete_goal_not_found(client: FlaskClient):
    responses.add(responses.DELETE, f"{DB_URL}/goal/999", status=404)
    response = client.delete("/goal/999")
    assert response.status_code == 404


# ============================================================================
# Suggestion Endpoints (With Request Payload Verification)
# ============================================================================

@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_suggestions_renders_table(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/suggestions",
        json=[
            {"id": 1, "suggestion": "Cut coffee spending", "accepted": True},
            {"id": 2, "suggestion": "Cancel gym subscription", "accepted": False},
        ],
        status=200,
    )
    response = client.get("/suggestions")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Cut coffee spending" in html
    assert "Cancel gym subscription" in html


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_create_suggestion_success_verifies_db_payload(client: FlaskClient):
    sugg_payload = {"suggestion": "Trim dining expenses", "accepted": True}
    responses.add(
        responses.POST,
        f"{DB_URL}/suggestion",
        match=[matchers.json_params_matcher(sugg_payload)],
        json={"id": 10, **sugg_payload},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/suggestions",
        json=[{"id": 10, **sugg_payload}],
        status=200,
    )

    response = client.post("/suggestion", json=sugg_payload)
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "suggestionChanged"
    assert "Trim dining expenses" in response.get_data(as_text=True)


@pytest.mark.usefixtures("app_ctx")
def test_create_suggestion_missing_fields_returns_400(client: FlaskClient):
    response = client.post("/suggestion", json={"suggestion": "Missing accepted field"})
    assert response.status_code == 400
    assert response.get_json() == {"error": "Missing suggestion fields"}


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_suggestion_by_id_success(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/suggestion/1",
        json={"id": 1, "suggestion": "Cut coffee spending", "accepted": True},
        status=200,
    )
    response = client.get("/suggestion/1")
    assert response.status_code == 200
    assert "Cut coffee spending" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_suggestion_by_id_not_found(client: FlaskClient):
    responses.add(responses.GET, f"{DB_URL}/suggestion/999", status=404)
    response = client.get("/suggestion/999")
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_edit_suggestion_renders_edit_template(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/suggestion/1",
        json={"id": 1, "suggestion": "Cut coffee spending", "accepted": True},
        status=200,
    )
    response = client.get("/suggestion/1/edit")
    assert response.status_code == 200
    assert "Cut coffee spending" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_edit_suggestion_not_found(client: FlaskClient):
    responses.add(responses.GET, f"{DB_URL}/suggestion/999", status=404)
    response = client.get("/suggestion/999/edit")
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_update_suggestion_success_verifies_db_payload(client: FlaskClient):
    patch_data = {"accepted": False}
    responses.add(
        responses.PATCH,
        f"{DB_URL}/suggestion/1",
        match=[matchers.json_params_matcher(patch_data)],
        json={"id": 1, "suggestion": "Updated advice", "accepted": False},
        status=200,
    )
    response = client.patch("/suggestion/1", json=patch_data)
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "suggestionChanged"
    assert "Updated advice" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_update_suggestion_not_found(client: FlaskClient):
    responses.add(responses.PATCH, f"{DB_URL}/suggestion/999", status=404)
    response = client.patch("/suggestion/999", json={"accepted": False})
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_delete_suggestion_success_triggers_feedback_refresh(client: FlaskClient):
    responses.add(responses.DELETE, f"{DB_URL}/suggestion/1", status=204)
    responses.add(responses.GET, f"{DB_URL}/suggestions", json=[], status=200)

    response = client.delete("/suggestion/1")
    assert response.status_code == 200
    assert "suggestionChanged" in response.headers.get("HX-Trigger")
    assert "feedbackChanged" in response.headers.get("HX-Trigger")


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_delete_suggestion_not_found(client: FlaskClient):
    responses.add(responses.DELETE, f"{DB_URL}/suggestion/999", status=404)
    response = client.delete("/suggestion/999")
    assert response.status_code == 404


# ============================================================================
# Feedback Endpoints (With Classification & Payload Verification)
# ============================================================================

@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_feedback_renders_table(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/feedbacks",
        json=[
            {"id": 1, "feedback": "Focus on food", "category_id": 80, "timeframe": "2 weeks"},
            {"id": 2, "feedback": "Do not touch gym", "category_id": None, "timeframe": None},
        ],
        status=200,
    )
    response = client.get("/feedback")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Focus on food" in html
    assert "Do not touch gym" in html


@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_missing_text_returns_400(client: FlaskClient):
    response = client.post("/feedback", json={"feedback": "   "})
    assert response.status_code == 400
    assert response.get_json() == {"error": "Missing feedback field"}


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_explicit_category_and_timeframe(client: FlaskClient):
    feedback_payload = {
        "feedback": "Cut dining expenses",
        "category_id": 80,
        "timeframe": "1 month",
        "suggestion_id": 2,
    }
    # Verifies explicit payload is sent without calling classifier
    responses.add(
        responses.POST,
        f"{DB_URL}/feedback",
        match=[matchers.json_params_matcher(feedback_payload)],
        json={"id": 1, **feedback_payload},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/feedbacks",
        json=[{"id": 1, **feedback_payload}],
        status=200,
    )

    response = client.post("/feedback", json=feedback_payload)
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"
    assert "Cut dining expenses" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_with_auto_classification(client: FlaskClient):
    # Verifies that app.py fetched categories, ran classify_feedback, and posted the resulting category_id/timeframe!
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    expected_db_payload = {
        "feedback": "I eat out too much",
        "suggestion_id": None,
        "category_id": 80,
        "timeframe": "2 weeks",
    }
    responses.add(
        responses.POST,
        f"{DB_URL}/feedback",
        match=[matchers.json_params_matcher(expected_db_payload)],
        json={"id": 5, **expected_db_payload},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/feedbacks",
        json=[{"id": 5, **expected_db_payload}],
        status=200,
    )

    response = client.post("/feedback", json={"feedback": "I eat out too much"})
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_with_auto_classification_and_suggestion_id(client: FlaskClient):
    """Verifies that suggestion_id is preserved when feedback undergoes auto-classification."""
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    expected_db_payload = {
        "feedback": "I agree to eat out less",
        "suggestion_id": 42,
        "category_id": 80,
        "timeframe": "2 weeks",
    }
    responses.add(
        responses.POST,
        f"{DB_URL}/feedback",
        match=[matchers.json_params_matcher(expected_db_payload)],
        json={"id": 7, **expected_db_payload},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/feedbacks",
        json=[{"id": 7, **expected_db_payload}],
        status=200,
    )

    response = client.post(
        "/feedback",
        json={"feedback": "I agree to eat out less", "suggestion_id": 42},
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_empty_strings_normalized_to_none_and_classified(client: FlaskClient):
    """Verifies lines 146-150 where empty strings in category_id/timeframe normalize to None and trigger classification."""
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    expected_db_payload = {
        "feedback": "Dining limit",
        "suggestion_id": None,
        "category_id": 80,
        "timeframe": "2 weeks",
    }
    responses.add(
        responses.POST,
        f"{DB_URL}/feedback",
        match=[matchers.json_params_matcher(expected_db_payload)],
        json={"id": 6, **expected_db_payload},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/feedbacks",
        json=[{"id": 6, **expected_db_payload}],
        status=200,
    )

    response = client.post("/feedback", json={"feedback": "Dining limit", "category_id": "", "timeframe": ""})
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_single_feedback_success(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/feedback/1",
        json={"id": 1, "feedback": "Focus on food", "category_id": 80, "timeframe": "2 weeks"},
        status=200,
    )
    response = client.get("/feedback/1")
    assert response.status_code == 200
    assert "Focus on food" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_single_feedback_not_found(client: FlaskClient):
    responses.add(responses.GET, f"{DB_URL}/feedback/999", status=404)
    response = client.get("/feedback/999")
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_edit_feedback_renders_template(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/feedback/1",
        json={"id": 1, "feedback": "Focus on food", "category_id": 80, "timeframe": "2 weeks"},
        status=200,
    )
    response = client.get("/feedback/1/edit")
    assert response.status_code == 200
    assert "Focus on food" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_edit_feedback_not_found(client: FlaskClient):
    responses.add(responses.GET, f"{DB_URL}/feedback/999", status=404)
    response = client.get("/feedback/999/edit")
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_update_feedback_explicit_fields_no_classification(client: FlaskClient):
    expected_patch = {"timeframe": "3 weeks"}
    responses.add(
        responses.PATCH,
        f"{DB_URL}/feedback/1",
        match=[matchers.json_params_matcher(expected_patch)],
        json={"id": 1, "feedback": "Focus on food", "category_id": 80, "timeframe": "3 weeks"},
        status=200,
    )
    response = client.patch("/feedback/1", json=expected_patch)
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"
    assert "Focus on food" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_update_feedback_text_triggers_classification_and_updates_db(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    expected_patch = {
        "feedback": "Dining limit",
        "category_id": 80,
        "timeframe": "2 weeks",
    }
    responses.add(
        responses.PATCH,
        f"{DB_URL}/feedback/1",
        match=[matchers.json_params_matcher(expected_patch)],
        json={"id": 1, "feedback": "Dining limit", "category_id": 80, "timeframe": "2 weeks"},
        status=200,
    )
    response = client.patch("/feedback/1", json={"feedback": "Dining limit"})
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_update_feedback_empty_strings_normalized_to_none(client: FlaskClient):
    """Verifies lines 191-195 empty strings normalized to None on PATCH."""
    expected_patch = {"category_id": None, "timeframe": None}
    responses.add(
        responses.PATCH,
        f"{DB_URL}/feedback/1",
        match=[matchers.json_params_matcher(expected_patch)],
        json={"id": 1, "feedback": "Existing text", "category_id": None, "timeframe": None},
        status=200,
    )
    response = client.patch("/feedback/1", json={"category_id": "", "timeframe": ""})
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_update_feedback_not_found(client: FlaskClient):
    responses.add(responses.PATCH, f"{DB_URL}/feedback/999", status=404)
    response = client.patch("/feedback/999", json={"feedback": "None"})
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_delete_feedback_by_id_success(client: FlaskClient):
    responses.add(responses.DELETE, f"{DB_URL}/feedback/1", status=204)
    responses.add(responses.GET, f"{DB_URL}/feedbacks", json=[], status=200)

    response = client.delete("/feedback/1")
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_delete_feedback_by_id_not_found(client: FlaskClient):
    responses.add(responses.DELETE, f"{DB_URL}/feedback/999", status=404)
    response = client.delete("/feedback/999")
    assert response.status_code == 404


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_delete_feedbacks_by_category_success_verifies_param(client: FlaskClient):
    responses.add(responses.DELETE, f"{DB_URL}/feedbacks?category_id=80", status=204)
    responses.add(responses.GET, f"{DB_URL}/feedbacks", json=[], status=200)

    response = client.delete("/feedbacks?category_id=80")
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "feedbackChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_delete_feedbacks_by_category_invalid_400(client: FlaskClient):
    responses.add(responses.DELETE, f"{DB_URL}/feedbacks?category_id=invalid", json={"error": "Invalid"}, status=400)
    response = client.delete("/feedbacks?category_id=invalid")
    assert response.status_code == 400
    assert response.get_json() == {"error": "Invalid"}


# ============================================================================
# AI Suggestion & Action Endpoints (With Happy Path & Payload Verification)
# ============================================================================

@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_get_ai_suggestion_no_goals(client: FlaskClient):
    responses.add(
        responses.GET,
        f"{DB_URL}/goals?active_only=true&top=3",
        json=[],
        status=200,
    )
    response = client.get("/ai-suggestion")
    assert response.status_code == 200
    assert "don&#39;t have any active savings goals yet" in response.get_data(as_text=True) or "Add a goal" in response.get_data(as_text=True)


@responses.activate
@pytest.mark.parametrize("reviewer, workflow_status", [
    ({"status": "success", "verdict": "approve", "review": "Supported by the spending data."}, 200),
    ({"status": "success", "verdict": "approve_with_changes", "review": "Check your needs first."}, 200),
    ({
        "status": "success", "verdict": "reject", "review": "This may not suit your needs.",
        "risks": ["Transport may cost more."],
        "recommendations": ["Compare alternatives before cancelling."],
    }, 200),
    ({"status": "error", "error": "Reviewer could not complete its review."}, 503),
    (None, 200),
])
def test_get_ai_suggestion_displays_mocked_review_and_keeps_human_actions_enabled(client, reviewer, workflow_status):
    responses.get(
        f"{DB_URL}/goals?active_only=true&top=3",
        json=[{"id": 1, "name": "Emergency Fund", "cost": 1000, "date": "2026-12-31T00:00:00"}],
    )
    responses.get(f"{DB_URL}/feedbacks", json=[])
    responses.get(f"{DB_URL}/suggestions", json=[])
    responses.get(f"{TX_URL}/categories", json=[])
    responses.post("http://multi-agent:5010/workflow", status=workflow_status, json={
        "status": "error" if workflow_status == 503 else "success",
        "worker_result": {
            "status": "success", "output": "Cook dinner at home to save $50 for your Emergency Fund.",
            "confidence": "High",
        },
        "reviewer_result": reviewer,
    })
    with (
        patch.dict(os.environ, {"MULTI_AGENT_URL": "http://multi-agent:5010"}),
        patch.object(suggestion_service, "fetch_mcp_tools", return_value=[]),
        patch.object(suggestion_service, "execute_mcp_tool", side_effect=[
            [{"merchant": "Takeaway", "amount": 50}],
            {"results": [{"text": "Reduce discretionary takeaway spending to support savings goals.", "metadata": {"source": "guide.md"}}]},
        ]),
    ):
        response = client.get("/ai-suggestion")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Cook dinner at home to save $50" in html
    assert "guide.md" in html
    assert "High" in html
    assert "Reviewer:" in html
    if reviewer:
        assert (reviewer.get("review") or reviewer["error"]) in html
        for detail in reviewer.get("risks", []) + reviewer.get("recommendations", []):
            assert detail in html
        assert (reviewer.get("verdict") or reviewer["status"]).replace("_", " ").capitalize() in html
    else:
        assert "Unavailable" in html
    assert 'hx-post="/savings-backend/ai-suggestion/action?accepted=true"' in html
    assert 'hx-post="/savings-backend/ai-suggestion/action?accepted=false"' in html


@pytest.mark.usefixtures("app_ctx")
def test_reviewer_feedback_is_html_escaped(app):
    from flask import render_template

    html = render_template(
        "ai-suggestion.jinja", suggestion="Trim dining spending.", sources="guide.md", confidence="High",
        reviewer={"verdict": "reject", "review": "<script>alert('review')</script>", "risks": ["<b>Risk</b>"], "recommendations": []},
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;Risk&lt;/b&gt;" in html


@pytest.mark.parametrize(
    "invalid_text",
    [
        "",
        "   ",
        "No current AI suggestion available.",
        "Error: Could not generate AI savings suggestion.",
        "You don't have any active savings goals yet.",
        "No transactions found matching your filter.",
        "Insufficient context to generate savings advice.",
    ],
)
@pytest.mark.usefixtures("app_ctx")
def test_action_ai_suggestion_invalid_suggestion_text(client: FlaskClient, invalid_text: str):
    response = client.post(
        "/ai-suggestion/action",
        json={"suggestion": invalid_text, "accepted": True, "feedback": "ok"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "No valid suggestion available to accept or reject."


@pytest.mark.usefixtures("app_ctx")
def test_action_ai_suggestion_missing_decision(client: FlaskClient):
    response = client.post(
        "/ai-suggestion/action",
        json={"suggestion": "Trim dining costs by $50.", "accepted": None, "feedback": "ok"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Missing or invalid decision (accepted/rejected)."


@pytest.mark.usefixtures("app_ctx")
def test_action_ai_suggestion_missing_feedback(client: FlaskClient):
    response = client.post(
        "/ai-suggestion/action",
        json={"suggestion": "Trim dining costs by $50.", "accepted": True, "feedback": "   "},
    )
    assert response.status_code == 400
    assert "Please provide feedback" in response.get_json()["error"]


@responses.activate
@pytest.mark.usefixtures("app_ctx")
@pytest.mark.parametrize("accepted", [True, False])
def test_action_ai_suggestion_saves_human_decision_with_linked_feedback(client: FlaskClient, accepted):
    """
    CRITICAL TEST: Verifies that when saving suggestion decision:
    1. POST /suggestion receives the suggestion text and accepted decision.
    2. POST /feedback receives the EXACT created suggestion_id (42), and classified category/timeframe!
    """
    suggestion_post_payload = {"suggestion": "Trim dining costs by $50.", "accepted": accepted}
    responses.add(
        responses.POST,
        f"{DB_URL}/suggestion",
        match=[matchers.json_params_matcher(suggestion_post_payload)],
        json={"id": 42, **suggestion_post_payload},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    expected_feedback_payload = {
        "feedback": "I agree with trimming dining",
        "suggestion_id": 42,
        "category_id": 80,
        "timeframe": "2 weeks",
    }
    responses.add(
        responses.POST,
        f"{DB_URL}/feedback",
        match=[matchers.json_params_matcher(expected_feedback_payload)],
        json={"id": 100, **expected_feedback_payload},
        status=201,
    )

    response = client.post(
        f"/ai-suggestion/action?accepted={str(accepted).lower()}",
        json={"suggestion": "Trim dining costs by $50.", "feedback": "I agree with trimming dining"},
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Trigger") == "suggestionChanged, feedbackChanged"


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_action_ai_suggestion_fallback_suggestion_id(client: FlaskClient):
    """Verifies lines 279-283 where saved_suggestion is raw JSON dict without id attribute."""
    responses.add(
        responses.POST,
        f"{DB_URL}/suggestion",
        json={"id": 77, "suggestion": "Save money", "accepted": True},
        status=201,
    )
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    expected_feedback_payload = {
        "feedback": "Agreed",
        "suggestion_id": 77,
        "category_id": 80,
        "timeframe": "2 weeks",
    }
    responses.add(
        responses.POST,
        f"{DB_URL}/feedback",
        match=[matchers.json_params_matcher(expected_feedback_payload)],
        json={"id": 101, **expected_feedback_payload},
        status=201,
    )

    response = client.post(
        "/ai-suggestion/action?accepted=true",
        json={"suggestion": "Save money on dining.", "feedback": "Agreed"},
    )
    assert response.status_code == 200


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_action_ai_suggestion_db_error_returns_500(client: FlaskClient):
    responses.add(
        responses.POST,
        f"{DB_URL}/suggestion",
        status=500,
    )
    response = client.post(
        "/ai-suggestion/action?accepted=true",
        json={"suggestion": "Trim dining costs by $50.", "feedback": "I agree"},
    )
    assert response.status_code == 500
    assert "Failed to save suggestion decision" in response.get_json()["error"]


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_action_ai_suggestion_rolls_back_suggestion_when_feedback_save_fails(client: FlaskClient):
    suggestion_url = f"{DB_URL}/suggestion"
    suggestion_delete_url = f"{DB_URL}/suggestion/42"
    feedback_url = f"{DB_URL}/feedback"

    responses.add(
        responses.POST,
        suggestion_url,
        json={"id": 42, "suggestion": "Trim dining costs by $50.", "accepted": True},
        status=201,
    )
    responses.add(responses.GET, f"{TX_URL}/categories", json=[], status=200)
    responses.add(responses.POST, feedback_url, status=500)
    responses.add(responses.DELETE, suggestion_delete_url, status=204)

    response = client.post(
        "/ai-suggestion/action?accepted=true",
        json={"suggestion": "Trim dining costs by $50.", "feedback": "I agree"},
    )

    assert response.status_code == 500
    assert [call.request.url for call in responses.calls] == [
        suggestion_url,
        f"{TX_URL}/categories",
        feedback_url,
        suggestion_delete_url,
    ]


@responses.activate
@pytest.mark.usefixtures("app_ctx")
def test_action_ai_suggestion_rejects_created_suggestion_without_id(client: FlaskClient):
    responses.add(
        responses.POST,
        f"{DB_URL}/suggestion",
        json={"suggestion": "Trim dining costs by $50.", "accepted": True},
        status=201,
    )

    response = client.post(
        "/ai-suggestion/action?accepted=true",
        json={"suggestion": "Trim dining costs by $50.", "feedback": "I agree"},
    )

    assert response.status_code == 500
    assert "did not include an id" in response.get_json()["error"]
