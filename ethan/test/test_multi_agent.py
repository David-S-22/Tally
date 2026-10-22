import pytest
import requests

from backend import chat_service, config, db_api, mcp_client, multi_agent_client, multi_agent_service, rag_client, summary_service
from backend.app import create_app


def _client():
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


LINES = [
    {"id": 7, "budget_id": 1, "category": "Dining", "warn_at": 20000, "hard_cap": 30000},
    {"id": 8, "budget_id": 1, "category": "Groceries", "warn_at": 40000, "hard_cap": 60000},
]
SUMMARY = {
    "budget": {"id": 1, "month": "2026-10", "declared_income": 500000},
    "budget_lines": [
        {**LINES[0], "actual_spend": 25000, "planned_est_high_total": 5000, "projected_high_total": 30000},
        {**LINES[1], "actual_spend": 10000, "planned_est_high_total": 0, "projected_high_total": 10000},
    ],
    "planned_events": [],
    "coach_proposals": [],
    "transactions": {"count": 2, "uncategorised_total": 0, "other_expenses": []},
    "totals": {"declared_income": 500000, "projected_high_total": 40000, "remaining_income_high": 460000},
}
PROPOSAL = {
    "proposal_type": "threshold_adjustment",
    "summary": "Raise the Dining thresholds.",
    "operations": [
        {"action": "update_budget_line", "budget_line_id": 7, "fields": {"warn_at": 27000, "hard_cap": 36000}}
    ],
}


def _record(structured=PROPOSAL, decision=None, status="success", verdict="approve_with_changes"):
    return {
        "status": status,
        "workflow_id": "wf-1",
        "workflow_state": "awaiting_decision",
        "feature": "budgets",
        "context": {"budget_id": 1, "month": "2026-10"},
        "planner_result": {"status": "success", "agent": "planner_agent", "model": "m", "plan": "plan",
                           "required_evidence": ["budgets:summary"], "missing_information": [],
                           "completion_criteria": ["c"], "unlisted_field": "hidden"},
        "worker_result": {"status": "success", "agent": "worker_agent", "model": "m", "output": "Raise Dining.",
                          "structured": structured, "evidence_used": ["budgets:summary"], "limitations": [],
                          "confidence": "Medium"},
        "reviewer_result": {"status": "success", "agent": "reviewer_agent", "model": "m", "verdict": verdict,
                            "risks": ["r"], "recommendations": ["x"], "review": "ok", "evidence_used": []},
        "human_decision": decision or {"required": True, "status": "pending"},
    }


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", True)
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(db_api, "get_budget", lambda budget_id: SUMMARY["budget"])
    monkeypatch.setattr(db_api, "list_budget_lines", lambda budget_id: LINES)
    monkeypatch.setattr(summary_service, "build_budget_summary", lambda budget_id: SUMMARY)
    monkeypatch.setattr(
        mcp_client,
        "call_tool",
        lambda name, arguments=None: [
            {"date": "2026-10-02", "merchant": "Cafe", "description": "Lunch", "amount": 25.0, "category_name": "Dining"}
        ],
    )
    monkeypatch.setattr(
        rag_client,
        "retrieve",
        lambda feature, question, k=3, where=None: [
            {"id": "c1", "text": "# Thresholds\nRaise caps gradually.", "metadata": {"source": "budget-guide"}, "distance": 0.5}
        ],
    )


def test_health_reports_multi_agent_mode_disabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    monkeypatch.setattr(db_api, "health", lambda: {"ok": True})
    monkeypatch.setattr("backend.app.transactions_api.list_categories", lambda: [])
    monkeypatch.setattr("backend.app.transactions_api.list_transactions", lambda: [])
    monkeypatch.setattr("backend.app._ollama_status", lambda: "up")

    assert _client().get("/health").get_json()["multi_agent_mode"] == "disabled"


def test_start_workflow_builds_mcp_then_rag_evidence_and_returns_candidate(enabled, monkeypatch):
    sent = {}

    def fake_start(body):
        sent.update(body)
        return _record()

    monkeypatch.setattr(multi_agent_client, "start_workflow", fake_start)

    resp = _client().post("/api/budgets/1/multi-agent/workflows", json={"message": "Adjust dining"})

    assert resp.status_code == 200
    assert sent["feature"] == "budgets"
    sources = [item["source"] for item in sent["evidence"]]
    assert sources == ["budgets:summary", "budgets:transactions", "rag:budget-guide"]
    assert "rag:budget-guide" in sent["task"]
    assert sent["context"]["budget_id"] == 1
    body = resp.get_json()
    assert body["proposal_candidate"]["operations"][0]["category"] == "Dining"
    assert body["integration"]["mcp_used"] is True and body["integration"]["rag_used"] is True
    assert "unlisted_field" not in body["planner_result"]


def test_start_workflow_rag_query_uses_mcp_merchants(enabled, monkeypatch):
    queries = []
    monkeypatch.setattr(
        rag_client,
        "retrieve",
        lambda feature, question, k=3, where=None: queries.append(question) or [],
    )
    monkeypatch.setattr(multi_agent_client, "start_workflow", lambda body: _record(structured=None))

    resp = _client().post("/api/budgets/1/multi-agent/workflows", json={})

    assert resp.status_code == 200
    assert "Cafe" in queries[0]
    assert resp.get_json()["integration"]["rag_used"] is False


def test_start_workflow_degrades_when_mcp_and_rag_unavailable(enabled, monkeypatch):
    def unavailable(*args, **kwargs):
        raise db_api.ServiceError("disabled", 503, "mcp_disabled")

    monkeypatch.setattr(mcp_client, "call_tool", unavailable)
    monkeypatch.setattr(rag_client, "retrieve", unavailable)
    sent = {}
    monkeypatch.setattr(multi_agent_client, "start_workflow", lambda body: sent.update(body) or _record(structured=None))

    resp = _client().post("/api/budgets/1/multi-agent/workflows", json={})

    assert resp.status_code == 200
    assert [item["source"] for item in sent["evidence"]] == ["budgets:summary"]
    assert resp.get_json()["integration"]["mcp_skipped_reason"] == "mcp_disabled"


def test_start_workflow_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    monkeypatch.setattr(db_api, "get_budget", lambda budget_id: SUMMARY["budget"])

    resp = _client().post("/api/budgets/1/multi-agent/workflows", json={})

    assert resp.status_code == 503
    assert resp.get_json()["code"] == "multi_agent_disabled"


def test_start_workflow_requires_budget_lines(enabled, monkeypatch):
    monkeypatch.setattr(summary_service, "build_budget_summary", lambda budget_id: {**SUMMARY, "budget_lines": []})

    resp = _client().post("/api/budgets/1/multi-agent/workflows", json={})

    assert resp.status_code == 422
    assert resp.get_json()["code"] == "no_budget_lines"


def test_task_names_target_line_and_validator_rejects_implausible_cap(enabled):
    from backend import multi_agent_service as service

    task = service._task(SUMMARY, [], None)
    assert "budget_line_id 7 (Dining)" in task and "30000 cents" in task

    implausible = {**PROPOSAL, "operations": [
        {"action": "update_budget_line", "budget_line_id": 7, "fields": {"warn_at": 1500, "hard_cap": 2500}}
    ]}
    candidate, error = service.validate_proposal(implausible, LINES)
    assert candidate is None and "half and triple" in error


def test_missing_structured_output_explains_advice_only(enabled, monkeypatch):
    monkeypatch.setattr(multi_agent_client, "start_workflow", lambda body: _record(structured=None))

    body = _client().post("/api/budgets/1/multi-agent/workflows", json={}).get_json()

    assert body["proposal_candidate"] is None
    assert "advice only" in body["proposal_candidate_error"]


def test_failed_workflow_has_no_candidate(enabled, monkeypatch):
    failed = _record(structured=None, status="error")
    failed["worker_result"] = {"status": "error", "code": "model_timeout", "error": "Model deadline exceeded"}
    monkeypatch.setattr(multi_agent_client, "start_workflow", lambda body: failed)

    body = _client().post("/api/budgets/1/multi-agent/workflows", json={}).get_json()

    assert body["status"] == "error"
    assert body["proposal_candidate"] is None


def _server_response(status, body):
    response = requests.Response()
    response.status_code = status
    response._content = __import__("json").dumps(body).encode()
    return response


def test_client_sends_bearer_token_and_returns_saved_failed_workflow(enabled, monkeypatch):
    monkeypatch.setattr(config, "MULTI_AGENT_TOKEN", "t" * 40)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        return _server_response(503, {"status": "error", "workflow_id": "wf-1", "code": "x"})

    monkeypatch.setattr(requests, "post", fake_post)

    record = multi_agent_client.start_workflow({"feature": "budgets"})

    assert record["workflow_id"] == "wf-1"
    assert calls[0][1] == {"Authorization": "Bearer " + "t" * 40}
    assert calls[0][2] == config.MULTI_AGENT_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("response", "exception", "status", "code"),
    [
        (_server_response(503, {"status": "error", "code": "workflow_busy", "error": "busy"}), None, 503, "workflow_busy"),
        (_server_response(401, {"status": "error", "code": "unauthorized"}), None, 502, "multi_agent_auth"),
        (_server_response(409, {"status": "error", "code": "conflict", "error": "Decision exists"}), None, 409, "conflict"),
        (None, requests.Timeout(), 504, "multi_agent_timeout"),
        (None, requests.ConnectionError(), 503, "multi_agent_unavailable"),
    ],
)
def test_client_maps_server_failures(enabled, monkeypatch, response, exception, status, code):
    def fake_post(*args, **kwargs):
        if exception is not None:
            raise exception
        return response

    monkeypatch.setattr(requests, "post", fake_post)

    with pytest.raises(db_api.ServiceError) as error:
        multi_agent_client.start_workflow({"feature": "budgets"})

    assert (error.value.status, error.value.code) == (status, code)


def _decision_env(monkeypatch, record=None):
    record = record or _record()
    recorded = []
    created = []
    monkeypatch.setattr(multi_agent_client, "get_workflow", lambda workflow_id: record)

    def fake_record(workflow_id, payload):
        recorded.append(payload)
        return {**record, "human_decision": {"status": "recorded", "decision": payload["decision"]}}

    monkeypatch.setattr(multi_agent_client, "record_decision", fake_record)
    monkeypatch.setattr(
        db_api,
        "create_coach_proposal",
        lambda budget_id, payload: created.append((budget_id, payload)) or ({"id": 3, "status": "proposed", **payload}, 201),
    )
    return recorded, created


def test_accept_records_decision_and_creates_proposed_coach_proposal(enabled, monkeypatch):
    recorded, created = _decision_env(monkeypatch)

    resp = _client().post(
        "/api/multi-agent/workflows/wf-1/decision",
        json={"decision": "Accept", "reason": "Looks right", "decided_by": "Ethan"},
    )

    assert resp.status_code == 200
    assert recorded == [{"decision": "Accept", "decided_by": "Ethan", "reason": "Looks right"}]
    assert created[0][0] == "1"
    assert created[0][1]["proposal_json"]["operations"][0]["fields"] == {"warn_at": 27000, "hard_cap": 36000}
    assert resp.get_json()["proposal"]["status"] == "proposed"


def test_partial_accept_uses_corrected_proposal(enabled, monkeypatch):
    recorded, created = _decision_env(monkeypatch)
    corrected = {**PROPOSAL, "operations": [{"action": "update_budget_line", "budget_line_id": 7, "fields": {"hard_cap": 33000}}]}

    resp = _client().post(
        "/api/multi-agent/workflows/wf-1/decision",
        json={"decision": "Partially Accept", "corrected_output": corrected, "reason": "Lower cap"},
    )

    assert resp.status_code == 200
    assert recorded[0]["corrected_output"] == corrected
    assert created[0][1]["proposal_json"]["operations"][0]["fields"] == {"hard_cap": 33000}


def test_reject_records_decision_without_creating_proposal(enabled, monkeypatch):
    recorded, created = _decision_env(monkeypatch)

    resp = _client().post("/api/multi-agent/workflows/wf-1/decision", json={"decision": "Reject", "reason": "No"})

    assert resp.status_code == 200
    assert recorded[0]["decision"] == "Reject"
    assert created == []
    assert resp.get_json()["proposal"] is None


@pytest.mark.parametrize(
    "operations",
    [
        [{"action": "update_budget_line", "budget_line_id": 99, "fields": {"warn_at": 1000}}],
        [{"action": "update_budget_line", "budget_line_id": 7, "fields": {"warn_at": 40000, "hard_cap": 30000}}],
        [{"action": "delete_budget_line", "budget_line_id": 7, "fields": {"warn_at": 1000}}],
        [{"action": "update_budget_line", "budget_line_id": 7, "fields": {"warn_at": 10.5}}],
    ],
)
def test_accept_rejects_invalid_proposal_before_recording(enabled, monkeypatch, operations):
    recorded, created = _decision_env(monkeypatch, _record(structured={**PROPOSAL, "operations": operations}))

    resp = _client().post("/api/multi-agent/workflows/wf-1/decision", json={"decision": "Accept"})

    assert resp.status_code == 422
    assert resp.get_json()["code"] == "invalid_proposal"
    assert recorded == [] and created == []


def test_decision_rejects_unknown_decision_and_repeat(enabled, monkeypatch):
    _decision_env(monkeypatch)
    assert _client().post("/api/multi-agent/workflows/wf-1/decision", json={"decision": "Maybe"}).status_code == 422

    _decision_env(monkeypatch, _record(decision={"status": "recorded", "decision": "Reject"}))
    resp = _client().post("/api/multi-agent/workflows/wf-1/decision", json={"decision": "Reject"})
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "decision_recorded"


def test_decision_without_structured_output_records_advice_only(enabled, monkeypatch):
    recorded, created = _decision_env(monkeypatch, _record(structured=None))

    resp = _client().post("/api/multi-agent/workflows/wf-1/decision", json={"decision": "Accept"})

    assert resp.status_code == 200
    assert len(recorded) == 1 and created == []


def test_invalid_workflow_id_is_rejected(enabled):
    resp = _client().get("/api/multi-agent/workflows/..%2Fadmin")

    assert resp.status_code in {404, 422}


def test_chat_multi_agent_mode_returns_review_without_storing_proposal(enabled, monkeypatch):
    monkeypatch.setattr(multi_agent_client, "start_workflow", lambda body: _record())
    monkeypatch.setattr(db_api, "list_chat_messages", lambda budget_id: [])
    monkeypatch.setattr(db_api, "create_chat_message", lambda budget_id, payload: ({}, 201))

    resp = _client().post(
        "/api/chat",
        json={"budget_id": 1, "message": "Adjust my dining budget", "integration_mode": "multi_agent"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["response_source"] == "multi_agent"
    assert body["multi_agent"]["workflow_id"] == "wf-1"
    assert body["proposal"] is None


def test_chat_adjustment_with_ollama_path_routes_to_multi_agent_when_enabled(enabled, monkeypatch):
    called = []
    monkeypatch.setattr(multi_agent_client, "start_workflow", lambda body: called.append(body) or _record())
    monkeypatch.setattr(db_api, "list_chat_messages", lambda budget_id: [])
    monkeypatch.setattr(db_api, "create_chat_message", lambda budget_id, payload: ({}, 201))

    resp = _client().post(
        "/api/chat",
        json={"budget_id": 1, "message": "Please adjust my dining budget to $350", "skip_deterministic": True},
    )

    assert resp.status_code == 200
    assert resp.get_json()["response_source"] == "multi_agent"
    assert called


def test_chat_adjustment_stays_off_multi_agent_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    selected = chat_service._execution_plan(SUMMARY, [], "Please adjust my dining budget", "adjustments", "auto", True)

    assert selected["path"] != "multi_agent"
    assert multi_agent_service.FEATURE == "budgets"
