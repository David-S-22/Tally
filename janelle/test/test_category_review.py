"""Multi-agent category review: evidence, routes and the human decision."""
import json
import logging
import threading
from unittest.mock import Mock

import requests
from pytest import MonkeyPatch, fixture

import janelle.backend.app as backend_app
from janelle.backend import config
from janelle.backend.services import category_review, multi_agent_client
from janelle.backend.services.multi_agent_client import MultiAgentError
from janelle.backend.services.rag_client import RAGError


CATEGORIES = [
    {"id": 1, "name": "Uncategorised", "type": "need"},
    {"id": 80, "name": "Dining", "type": "want"},
    {"id": 81, "name": "Groceries", "type": "need"},
]
UNCATEGORISED = {
    "id": 42, "date": "2026-10-01T10:00:00", "merchant": "Woolworths",
    "description": "Weekly shop", "amount": 20.0, "category_id": 1,
}
HISTORY = {**UNCATEGORISED, "id": 7, "amount": 35.5, "category_id": 81}
CORRECTION = {
    "id": 3, "merchant": "Woolworths", "previous_category_name": "Dining",
    "user_category_name": "Groceries", "corrected_at": "2026-09-01",
}


def workflow(status="success", structured=None, verdict="approve", confidence="High"):
    if structured is None:
        structured = {"category": "groceries", "reason": "history:7 says Groceries"}
    return {
        "workflow_id": "wf-1",
        "status": "success" if status == "success" else "error",
        "evidence": [{"source": "transaction:42", "summary": "row"}],
        "planner_result": {"status": "success", "plan": "Look at history"},
        "worker_result": {
            "status": status, "output": "Groceries", "structured": structured,
            "confidence": confidence,
        },
        "reviewer_result": {"status": status, "verdict": verdict, "risks": []},
        "human_decision": {"status": "pending"},
    }


def decided(decision, final_output):
    return {**workflow(), "human_decision": {
        "status": "recorded", "decision": decision, "final_output": final_output,
    }}


def response_with_json(payload, status=200):
    response = Mock(status_code=status)
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


@fixture
def database(monkeypatch: MonkeyPatch):
    """Serve the database routes the review reads and writes."""
    def install(transaction=UNCATEGORISED, history=(HISTORY,), corrections=(CORRECTION,)):
        def get(url, params=None, **_):
            path = url.split("6001", 1)[1]
            if path == "/categories":
                return response_with_json(CATEGORIES)
            if path == "/transactions":
                return response_with_json(list(history))
            if path == "/category-corrections":
                return response_with_json(list(corrections))
            return response_with_json(transaction)
        monkeypatch.setattr(requests, "get", Mock(side_effect=get))
        post = Mock(return_value=response_with_json({"transaction": {}}, 201))
        monkeypatch.setattr(requests, "post", post)
        return post
    return install


@fixture
def server(monkeypatch: MonkeyPatch):
    """Enable the mode and replace every Multi-Agent Server call."""
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", True)
    monkeypatch.setattr(category_review, "RUNS", {})
    calls = {
        name: Mock() for name in (
            "create_workflow", "get_workflow", "latest_workflow", "record_decision",
        )
    }
    for name, mock in calls.items():
        monkeypatch.setattr(multi_agent_client, name, mock)
    return calls


@fixture
def client():
    with backend_app.app.test_client() as test_client:
        yield test_client


def start(client, transaction_id=42):
    response = client.post(f"/ui/transactions/{transaction_id}/agent-review")
    for run in category_review.RUNS.values():
        run["thread"].join(5)
    return response


def only_run():
    (run,) = category_review.RUNS.values()
    return run


def finished(client, record):
    """Start a run whose workflow finished with ``record`` and return its id."""
    run = only_run()
    run["workflow"], run["status"] = record, "done"
    return run["id"]


# --- evidence ------------------------------------------------------------


def test_request_has_unique_sources_and_names_disabled_rag(database, server):
    database()
    body = category_review.build_request(
        UNCATEGORISED, {c["id"]: c["name"] for c in CATEGORIES},
        CATEGORIES[1:], "http://transactions-db:6001", "run",
    )

    sources = [item["source"] for item in body["evidence"]]
    assert len(sources) == len(set(sources))
    assert {"transaction:42", "categories:list", "history:7", "correction:3"} <= set(sources)
    assert body["evidence"][1]["summary"] == "Valid categories: Dining, Groceries"
    assert body["context"]["unavailable_sources"] == ["rag: disabled"]
    assert body["feature"] == "transactions"


def test_request_marks_failed_rag_as_unavailable(database, server, monkeypatch):
    database()
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(
        category_review.rag_client, "retrieve",
        Mock(side_effect=RAGError("rag_connection")),
    )
    body = category_review.build_request(
        UNCATEGORISED, {c["id"]: c["name"] for c in CATEGORIES},
        CATEGORIES[1:], "http://transactions-db:6001", "run",
    )
    assert body["context"]["unavailable_sources"] == ["rag: unavailable"]


# --- button and start ----------------------------------------------------


def test_button_shows_only_for_uncategorised_rows_when_enabled(client, database, server):
    database(history=(UNCATEGORISED, HISTORY))
    html = client.get("/ui/transactions").text
    assert html.count("Suggest category") == 1
    assert "/ui/transactions/42/agent-review" in html


def test_button_hidden_when_mode_is_off(client, database, server, monkeypatch):
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    database(history=(UNCATEGORISED,))
    assert "Suggest category" not in client.get("/ui/transactions").text
    assert client.post("/ui/transactions/42/agent-review").status_code == 404


def test_start_refuses_a_categorised_row(client, database, server):
    database(transaction=HISTORY)
    response = start(client, 7)
    assert response.status_code == 409
    assert not server["create_workflow"].called


def test_start_refuses_while_another_review_runs(client, database, server):
    database()
    release = threading.Event()
    server["create_workflow"].side_effect = lambda body: release.wait(5) and workflow()
    first = client.post("/ui/transactions/42/agent-review")
    second = client.post("/ui/transactions/42/agent-review")
    release.set()
    only_run()["thread"].join(5)

    assert first.status_code == 200 and "every 3s" in first.text
    assert second.status_code == 409
    assert server["create_workflow"].call_count == 1


def test_busy_server_shows_message_and_records_nothing(client, database, server):
    database()
    server["create_workflow"].side_effect = MultiAgentError("multi_agent_busy")
    start(client)
    html = client.get(f"/ui/agent-review/{only_run()['id']}").text
    assert "busy" in html and "every 3s" not in html
    assert not server["record_decision"].called


def test_timeout_checks_saved_state_once_and_never_resubmits(client, database, server):
    database()
    server["create_workflow"].side_effect = MultiAgentError("multi_agent_timeout")
    server["latest_workflow"].return_value = None
    start(client)
    assert only_run()["status"] == "unknown"
    assert server["create_workflow"].call_count == 1
    assert server["latest_workflow"].call_count == 1


def test_failed_workflow_offers_reject_only(client, database, server):
    database()
    server["create_workflow"].side_effect = MultiAgentError(
        "multi_agent_http_error", record=workflow(status="timeout"),
    )
    start(client)
    html = client.get(f"/ui/agent-review/{only_run()['id']}").text
    assert 'value="Reject"' in html
    assert 'value="Accept"' not in html and "Partially Accept" not in html


def test_panel_shows_confidence_only_when_present(client, database, server):
    database()
    server["create_workflow"].return_value = workflow(confidence=None)
    start(client)
    html = client.get(f"/ui/agent-review/{only_run()['id']}").text
    assert "confidence" not in html
    assert "<strong>Groceries</strong>" in html and 'value="Accept"' in html


def test_agent_run_and_decision_are_logged_without_model_text(client, database, server, caplog):
    database()
    caplog.set_level(logging.INFO)
    server["create_workflow"].return_value = workflow()
    server["record_decision"].return_value = decided("Reject", None)
    start(client)
    client.post(f"/ui/agent-review/{only_run()['id']}/decision", data={"decision": "Reject"})

    events = [line for line in caplog.text.splitlines() if "AGENT_REVIEW_" in line]
    assert [json.loads(line.split("AI_WORKFLOW ", 1)[1])["event"] for line in events] == [
        "AGENT_REVIEW_START", "AGENT_REVIEW_AGENT", "AGENT_REVIEW_AGENT",
        "AGENT_REVIEW_AGENT", "AGENT_REVIEW_COMPLETE", "AGENT_REVIEW_DECISION",
    ]
    assert '"agent":"worker"' in events[2] and '"category":"groceries"' in events[2]
    assert "history:7 says Groceries" not in caplog.text


# --- decisions -------------------------------------------------------------


def test_accept_records_then_applies_the_workers_category(client, database, server):
    post = database()
    server["create_workflow"].return_value = workflow()
    start(client)
    run_id = only_run()["id"]
    server["record_decision"].return_value = decided(
        "Accept", {"category": "Groceries", "reason": "x"},
    )

    response = client.post(
        f"/ui/agent-review/{run_id}/decision", data={"decision": "Accept", "category_id": 81},
    )

    assert response.status_code == 200
    assert response.headers["HX-Trigger"] == "transactionsChanged"
    assert "Category set to Groceries." in response.text
    assert server["record_decision"].call_args.args == (
        "wf-1", {"decision": "Accept", "decided_by": "transactions-ui"},
    )
    assert post.call_args.args[0].endswith("/transactions/42/category-correction")
    assert post.call_args.kwargs["json"] == {"category_id": 81}


def test_accept_of_unapproved_verdict_needs_a_reason(client, database, server):
    database()
    server["create_workflow"].return_value = workflow(verdict="approve_with_changes")
    start(client)
    response = client.post(
        f"/ui/agent-review/{only_run()['id']}/decision", data={"decision": "Accept"},
    )
    assert response.status_code == 422
    assert not server["record_decision"].called


def test_accepting_another_category_is_a_partial_accept_with_a_reason(client, database, server):
    post = database()
    server["create_workflow"].return_value = workflow()
    start(client)
    run_id = only_run()["id"]

    missing = client.post(
        f"/ui/agent-review/{run_id}/decision",
        data={"decision": "Accept", "category_id": 80},
    )
    assert missing.status_code == 422 and not server["record_decision"].called

    server["record_decision"].return_value = decided(
        "Partially Accept", {"category": "Dining", "reason": "Dinner"},
    )
    response = client.post(
        f"/ui/agent-review/{run_id}/decision",
        data={"decision": "Accept", "category_id": 80, "reason": "Dinner"},
    )
    assert response.status_code == 200
    body = server["record_decision"].call_args.args[1]
    assert body["decision"] == "Partially Accept"
    assert body["corrected_output"] == {"category": "Dining", "reason": "Dinner"}
    assert body["reason"] == "Dinner"
    assert post.call_args.kwargs["json"] == {"category_id": 80}


def test_reject_records_and_changes_nothing(client, database, server):
    post = database()
    server["create_workflow"].return_value = workflow()
    start(client)
    server["record_decision"].return_value = decided("Reject", None)
    response = client.post(
        f"/ui/agent-review/{only_run()['id']}/decision", data={"decision": "Reject"},
    )
    assert "Rejected. Nothing changed." in response.text
    assert "HX-Trigger" not in response.headers
    assert not post.called


def test_invalid_final_output_is_not_applied(client, database, server):
    post = database()
    server["create_workflow"].return_value = workflow()
    start(client)
    server["record_decision"].return_value = decided("Accept", {"category": "Uncategorised"})
    response = client.post(
        f"/ui/agent-review/{only_run()['id']}/decision", data={"decision": "Accept"},
    )
    assert "nothing was applied" in response.text
    assert not post.called


def test_row_changed_before_apply_is_not_applied(client, database, server, monkeypatch):
    post = database()
    server["create_workflow"].return_value = workflow()
    start(client)
    database(transaction=HISTORY)  # someone categorised it meanwhile
    server["record_decision"].return_value = decided("Accept", {"category": "Groceries"})
    response = client.post(
        f"/ui/agent-review/{only_run()['id']}/decision", data={"decision": "Accept"},
    )
    assert "transaction changed first" in response.text
    assert not requests.post.called


def test_repeated_decision_reads_the_saved_one(client, database, server):
    database()
    server["create_workflow"].return_value = workflow()
    start(client)
    server["record_decision"].side_effect = MultiAgentError("multi_agent_conflict")
    server["get_workflow"].return_value = decided("Reject", None)
    response = client.post(
        f"/ui/agent-review/{only_run()['id']}/decision", data={"decision": "Reject"},
    )
    assert response.status_code == 200
    assert "Decision: Reject" in response.text
    assert server["get_workflow"].call_args.args == ("wf-1",)


def test_expired_run_is_a_404(client, server):
    assert client.get("/ui/agent-review/nope").status_code == 404
