"""The dispute workflow routes: evidence goes to the shared Multi-Agent Server, the human decision comes back as a draft.

The server side is the real stub coordinator from ai-services/multi-agent-server, driven through
its Flask test client, so these tests check both halves of the contract together.
"""
import json
import os
import sys
from datetime import date
from types import SimpleNamespace

import pytest

from sophia.backend import app as backend_app_module
from sophia.backend import config
from sophia.backend.clients import bills_db as bills_db_module
from sophia.backend.clients import mcp_server, multi_agent, transactions
from sophia.backend.engine import Bill

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "ai-services", "multi-agent-server"))
from server import create_app as create_server  # noqa: E402

SPOTIFY = Bill(id=3, name="Spotify", merchant="Spotify AU", amount_cents=1399, cadence="monthly", next_billing_date=date(2026, 9, 27),
               type="subscription", payment_method="card", end_date=None, confirmed_at=None, created_at=None)
BILL_ROW = {"id": 3, "name": "Spotify", "merchant": "Spotify AU", "amount_cents": 1399, "cadence": "monthly", "next_billing_date": "2026-09-27",
            "type": "subscription", "payment_method": "card", "end_date": None, "confirmed_at": None, "created_at": None}
DISPUTE = {"id": 7, "bill_id": 3, "reason": "charged $17.99 instead of $13.99", "status": "draft", "opened_at": "2026-10-01"}
COMPARE = {"bill": {"id": 3}, "payments": [], "charges": [{"id": 26, "date": "2026-08-20", "amount_cents": 1799, "description": "Monthly", "differs_from_bill_cents": 400}]}


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Bills app with a fake bills-db, MCP answering the comparison, and the stub Multi-Agent Server behind the client."""
    drafts = []
    monkeypatch.setattr(bills_db_module, "health", lambda: {"ok": True})
    monkeypatch.setattr(bills_db_module, "get_dispute", lambda dispute_id: DISPUTE if dispute_id == 7 else None)
    monkeypatch.setattr(bills_db_module, "get_bill", lambda bill_id: BILL_ROW if bill_id == 3 else None)
    monkeypatch.setattr(bills_db_module, "row_to_bill", lambda row: SPOTIFY)
    monkeypatch.setattr(bills_db_module, "create_dispute_draft", lambda dispute_id, payload: drafts.append((dispute_id, payload)) or {"id": 99, "dispute_id": dispute_id, "version": len(drafts), **payload})
    monkeypatch.setattr(transactions, "list_transactions", lambda merchant=None, since=None: ([], "stub"))
    monkeypatch.setattr(mcp_server, "call_tool", lambda name, arguments: (COMPARE if name == "compare_bill_with_bank_charges" else [], 1.0))
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", True)

    server = create_server(log_dir=tmp_path).test_client()
    posted = []

    def post(url, json=None, timeout=None):
        path = url.replace(config.MULTI_AGENT_SERVER_URL, "")
        posted.append((path, json))
        response = server.post(path, json=json)
        return SimpleNamespace(status_code=response.status_code, json=response.get_json)

    monkeypatch.setattr(multi_agent.requests, "post", post)
    app = backend_app_module.create_app()
    app.config["TESTING"] = True
    return SimpleNamespace(client=app.test_client(), posted=posted, drafts=drafts, logs=tmp_path)


def test_start_sends_bank_evidence_and_returns_a_pending_record(world):
    response = world.client.post("/api/disputes/7/workflow")
    assert response.status_code == 201
    body = json.loads(response.data)
    path, sent = world.posted[0]
    assert path == "/workflow"
    assert sent["feature"] == "bills"
    assert sent["task"] == "Draft a dispute letter for bill 3 (Spotify AU, charged $17.99 instead of $13.99)."
    assert [e["source"] for e in sent["evidence"]] == ["mcp:compare_bill_with_bank_charges"]
    assert "Spotify AU" in sent["evidence"][0]["summary"] and "$17.99" in sent["evidence"][0]["summary"]
    assert sent["context"]["bank_facts_only"] is True
    assert body["human_decision"]["status"] == "pending"
    assert body["worker_result"]["evidence_count"] == 1
    assert world.drafts == []  # nothing written before the human decides


def test_accept_stores_the_worker_letter_as_the_next_draft(world):
    wid = json.loads(world.client.post("/api/disputes/7/workflow").data)["workflow_id"]
    response = world.client.post(f"/api/disputes/7/workflow/{wid}/decision", json={"decision": "Accept"})
    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["human_decision"]["decision"] == "Accept"
    (dispute_id, payload), = world.drafts
    assert dispute_id == 7
    assert payload["letter_text"] == body["worker_result"]["output"]
    assert payload["steps_json"]["workflow_id"] == wid
    assert payload["steps_json"]["evidence"]["tools"] == ["mcp:compare_bill_with_bank_charges"]
    assert payload["steps_json"]["evidence"]["review"]["verdict"] == "approve"
    assert any("Cancel the subscription" in s for s in payload["steps_json"]["steps"])  # card rule still enforced
    assert body["draft"]["id"] == 99
    audit = [json.loads(l)["event"] for l in (world.logs / "coordination_audit.jsonl").read_text().splitlines()]
    assert audit[-1] == "human_decision"


def test_partially_accept_stores_the_edited_letter(world):
    wid = json.loads(world.client.post("/api/disputes/7/workflow").data)["workflow_id"]
    body = json.loads(world.client.post(f"/api/disputes/7/workflow/{wid}/decision", json={"decision": "Partially Accept", "corrected_output": "Dear Spotify, edited.", "reason": "removed a claim"}).data)
    assert body["human_decision"]["final_output"] == "Dear Spotify, edited."
    assert world.drafts[0][1]["letter_text"] == "Dear Spotify, edited."


def test_reject_stores_nothing(world):
    wid = json.loads(world.client.post("/api/disputes/7/workflow").data)["workflow_id"]
    body = json.loads(world.client.post(f"/api/disputes/7/workflow/{wid}/decision", json={"decision": "Reject"}).data)
    assert body["human_decision"]["final_output"] is None
    assert world.drafts == [] and "draft" not in body


def test_bad_decisions_are_400_and_a_second_decision_is_409(world):
    wid = json.loads(world.client.post("/api/disputes/7/workflow").data)["workflow_id"]
    assert world.client.post(f"/api/disputes/7/workflow/{wid}/decision", json={"decision": "Maybe"}).status_code == 400
    assert world.client.post(f"/api/disputes/7/workflow/{wid}/decision", json={"decision": "Partially Accept"}).status_code == 400
    assert world.client.post(f"/api/disputes/7/workflow/{wid}/decision", json={"decision": "Accept"}).status_code == 200
    response = world.client.post(f"/api/disputes/7/workflow/{wid}/decision", json={"decision": "Accept"})
    assert response.status_code == 409
    assert json.loads(response.data)["code"] == "multi_agent_rejected"


def test_unknown_dispute_or_workflow_is_404(world):
    assert world.client.post("/api/disputes/8/workflow").status_code == 404
    assert world.client.post("/api/disputes/7/workflow/nope/decision", json={"decision": "Accept"}).status_code == 404


def test_disabled_mode_refuses_with_the_shared_shape(world, monkeypatch):
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    response = world.client.post("/api/disputes/7/workflow")
    assert response.status_code == 503
    assert json.loads(response.data) == {"error": "Multi-Agent mode is disabled.", "code": "multi_agent_disabled"}
    assert world.posted == []


def test_mcp_off_still_runs_with_empty_evidence_and_the_reviewer_flags_it(world, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    body = json.loads(world.client.post("/api/disputes/7/workflow").data)
    assert world.posted[0][1]["evidence"] == []
    assert body["reviewer_result"]["verdict"] == "approve_with_changes"
