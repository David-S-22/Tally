"""The Bills workflows against the real shared Multi-Agent Server coordinator (fake model), end to end.

The server side is ai-services/multi-agent-server with its own validation, SQLite store and
decision rules; only the Ollama call is faked. The Bills side runs on the live seeded database.
Runs execute inline instead of in the background thread, so the polling block is checked in both
its states by looking at the status before and after.
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from conftest import response_text as _text
from sophia.backend import config
from sophia.backend.clients import bills_db, mcp_server, multi_agent
from sophia.backend.services import multi_agent as multi_agent_service

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "ai-services", "multi-agent-server"))
from agents import Agents  # noqa: E402
from server import create_app as create_server  # noqa: E402
from settings import Settings  # noqa: E402

LETTER = "Dear Spotify AU,\n\nOn 20 Aug 2026 my card was charged $17.99 for my Spotify subscription, which is $13.99 a month. Please refund the $4.00 difference.\n\n[Your name]"
COMPARE = {"bill": {"id": 3}, "payments": [], "charges": [{"id": 26, "date": "2026-08-20", "amount_cents": 1799, "description": "Monthly", "differs_from_bill_cents": 400}]}


class BillsModel:
    """What the three roles return for the two Bills task templates."""

    def __init__(self, verdict="approve", structured=None, fail_role=None):
        self.verdict, self.structured, self.fail_role = verdict, structured, fail_role
        self.structured_on_letters = False  # a worker that drifts into JSON on a letter task

    def generate(self, role, messages, schema, deadline):
        if role == self.fail_role:
            raise ValueError("model exploded")
        payload = json.loads(messages[1]["content"])
        sources = [item["source"] for item in payload["evidence"]]
        letter_task = payload["task"].startswith("Draft a dispute letter")
        results = {
            "planner": {"plan": ["Read the evidence", "Produce the output"], "required_evidence": sources, "missing_information": [], "completion_criteria": ["Supported by evidence"]},
            "worker": {"output": LETTER if letter_task else "Ends Spotify after 16 Sep.", "structured": self.structured if (self.structured_on_letters or not letter_task) else None, "evidence_used": sources, "limitations": []},
            "reviewer": {"verdict": self.verdict, "risks": [] if self.verdict == "approve" else ["The refund timeframe is not in the evidence"], "recommendations": [], "review": "Checked.", "evidence_used": sources},
        }
        return json.dumps(results[role]), "High"  # (content, confidence) since #222

    def health(self):
        return {"ollama": "up", "ready": True, "missing_models": []}


@pytest.fixture
def world(live_client, monkeypatch, tmp_path):
    """Bills app on the live DB, MCP answering the bank comparison, the real server coordinator behind the client."""
    state = SimpleNamespace(client=live_client, model=BillsModel(), posted=[])
    settings = Settings(host="127.0.0.1")  # #222 made the default 0.0.0.0, which demands a token
    server = create_server(log_dir=tmp_path, settings=settings, agent_runner=Agents(settings, state.model)).test_client()

    def request(method, url, json=None, headers=None, timeout=None):
        path = url.replace(config.MULTI_AGENT_SERVER_URL, "")
        state.posted.append((method, path, json))
        response = server.open(path, method=method, json=json, headers=headers)
        return SimpleNamespace(status_code=response.status_code, json=response.get_json)

    monkeypatch.setattr(multi_agent.requests, "request", request)
    monkeypatch.setattr(multi_agent_service, "_submit", lambda fn, *args: fn(*args))
    monkeypatch.setattr(multi_agent_service, "_runs", {})
    monkeypatch.setattr(mcp_server, "call_tool", lambda name, arguments: (COMPARE if name == "compare_bill_with_bank_charges" else [], 1.0))
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", True)
    monkeypatch.setattr(config, "MULTI_AGENT_TOKEN", "")
    return state


def _start_letter(world, dispute_id=1):
    response = world.client.post(f"/api/disputes/{dispute_id}/workflow")
    assert response.status_code == 202
    run = json.loads(response.data)
    status = json.loads(world.client.get(f"/api/workflow-runs/{run['run_id']}").data)
    return status


# --- dispute letter ------------------------------------------------------------

def test_letter_workflow_sends_bills_evidence_and_waits_for_the_human(world):
    before = len(bills_db.list_dispute_drafts(1))
    status = _start_letter(world)
    method, path, sent = world.posted[0]
    assert (method, path) == ("POST", "/workflow")
    assert sent["feature"] == "bills" and sent["contract_version"] == "1.0"
    assert sent["task"].startswith("Draft a dispute letter for bill 2")
    assert "Planner:" in sent["task"] and "Worker:" in sent["task"] and "Reviewer:" in sent["task"]
    assert [e["source"] for e in sent["evidence"]] == ["bills-db:bill:2", "mcp:compare_bill_with_bank_charges"]
    assert status["state"] == "done" and status["workflow_id"]
    assert status["summary"]["stages"] == {"planner": "success", "worker": "success", "reviewer": "success"}
    assert status["summary"]["output"] == LETTER and status["summary"]["verdict"] == "approve"
    assert status["summary"]["allowed_decisions"] == ["Accept", "Partially Accept", "Reject"]
    assert len(bills_db.list_dispute_drafts(1)) == before  # nothing written before the human decides


def test_accept_stores_the_letter_as_the_next_draft_with_the_review(world):
    status = _start_letter(world)
    wid = status["workflow_id"]
    before = len(bills_db.list_dispute_drafts(1))
    response = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Accept"})
    assert response.status_code == 200
    body = json.loads(response.data)
    assert body["human_decision"]["decision"] == "Accept" and body["human_decision"]["final_output"] == LETTER
    drafts = bills_db.list_dispute_drafts(1)
    assert len(drafts) == before + 1
    stored = json.loads(drafts[-1]["steps_json"])
    assert drafts[-1]["letter_text"] == LETTER
    assert stored["workflow_id"] == wid
    assert stored["evidence"]["review"] == {"verdict": "approve", "risks": []}
    assert stored["evidence"]["bank"] and stored["evidence"]["tools"] == ["bills-db:bill:2", "mcp:compare_bill_with_bank_charges"]
    assert any("direct-debit" in s.lower() or "cancel" in s.lower() for s in stored["steps"])  # payment-method rule still enforced


def test_partially_accept_needs_a_reason_and_stores_the_edit(world):
    wid = _start_letter(world)["workflow_id"]
    missing = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Partially Accept", "corrected_output": "Dear Spotify, edited."})
    assert missing.status_code == 400 and "reason" in json.loads(missing.data)["error"]
    ok = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Partially Accept", "corrected_output": "Dear Spotify, edited.", "reason": "shorter"})
    assert ok.status_code == 200
    assert bills_db.list_dispute_drafts(1)[-1]["letter_text"] == "Dear Spotify, edited."


def test_accepting_against_the_reviewer_needs_a_reason(world):
    world.model.verdict = "approve_with_changes"
    wid = _start_letter(world)["workflow_id"]
    refused = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Accept"})
    assert refused.status_code == 400 and "reviewer" in json.loads(refused.data)["error"]
    assert world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Accept", "reason": "the timeframe is fine"}).status_code == 200


def test_reject_stores_nothing_and_a_second_decision_is_409(world):
    wid = _start_letter(world)["workflow_id"]
    before = len(bills_db.list_dispute_drafts(1))
    assert world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Reject"}).status_code == 200
    assert len(bills_db.list_dispute_drafts(1)) == before
    again = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Accept"})
    assert again.status_code == 409 and json.loads(again.data)["code"] == "multi_agent_rejected"


def test_a_failed_agent_leaves_only_reject(world):
    world.model.fail_role = "worker"
    status = _start_letter(world)
    assert status["state"] == "failed"
    assert status["summary"]["failed"] and status["summary"]["failed_agent"] == "worker_agent"
    assert status["summary"]["stages"]["reviewer"] == "skipped"
    wid = status["workflow_id"]
    refused = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Accept", "reason": "x"})
    assert refused.status_code == 409 and json.loads(refused.data)["code"] == "multi_agent_failed"
    assert world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Reject"}).status_code == 200


def test_disabled_mode_refuses_with_the_shared_shape(world, monkeypatch):
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    response = world.client.post("/api/disputes/1/workflow")
    assert response.status_code == 503
    assert json.loads(response.data) == {"error": "Multi-Agent mode is disabled.", "code": "multi_agent_disabled"}
    assert world.posted == []


def test_unknown_dispute_or_run_is_404(world):
    assert world.client.post("/api/disputes/999/workflow").status_code == 404
    assert world.client.get("/api/workflow-runs/nope").status_code == 404


# --- the HTMX review block -------------------------------------------------------

def test_ui_start_renders_the_block_and_the_panel_picks_it_up_on_reload(world, monkeypatch):
    # Hold the run in flight: the block must poll itself and name the stages.
    monkeypatch.setattr(multi_agent_service, "_submit", lambda fn, *args: None)
    response = world.client.post("/ui/disputes/1/workflow")
    assert response.status_code == 202
    html = _text(response)
    assert 'hx-trigger="every 2s"' in html and "/ui/workflow-runs/" in html
    assert "planner" in html and "reviewer" in html and "human" in html
    panel = _text(world.client.get("/ui/disputes?dispute_id=1"))
    assert 'id="agent-review-dispute_letter-1"' in panel and "Draft with agents" in panel


def test_ui_block_shows_the_draft_and_the_decision_saves_a_version(world):
    run_id = json.loads(world.client.post("/api/disputes/1/workflow").data)["run_id"]
    html = _text(world.client.get(f"/ui/workflow-runs/{run_id}"))
    assert 'hx-trigger="every 2s"' not in html
    assert "Dear Spotify AU" in html and "Reviewer: approve" in html
    assert 'value="Accept"' in html and 'value="Partially Accept"' in html and 'value="Reject"' in html
    # The editable draft sits outside the <form>; it must join it, or the edit never posts.
    form_id = f"agent-decision-{run_id}"
    assert f'<form class="agent-decision" id="{form_id}"' in html
    assert f'name="corrected_output" form="{form_id}"' in html
    wid = multi_agent_service.run_status(run_id)["workflow_id"]
    before = len(bills_db.list_dispute_drafts(1))
    response = world.client.post(f"/ui/disputes/1/workflow/{wid}/decision", data={"decision": "Partially Accept", "corrected_output": "Dear Spotify, trimmed.", "reason": "shorter"})
    assert response.status_code == 200
    assert "Dear Spotify, trimmed." in _text(response)
    assert 'id="dispute-list" hx-swap-oob="true"' in _text(response)
    assert "saved as a new version" in response.headers["HX-Trigger"]
    assert len(bills_db.list_dispute_drafts(1)) == before + 1
    assert "Drafted by the agents" in _text(world.client.get("/ui/disputes?dispute_id=1"))


# --- chat suggestion ---------------------------------------------------------------

def test_chat_suggestion_workflow_applies_the_agents_change_on_accept(world):
    world.model.structured = {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}}
    bills = [bills_db.row_to_bill(r) for r in bills_db.list_bills()]
    row = bills_db.create_suggestion({"op": "update", "entity": "bill", "entity_id": 3, "payload_json": json.dumps({"end_date": "2026-09-16"}), "message_id": None})
    run_id = multi_agent_service.start_chat_suggestion(row["id"], "I cancelled Spotify from 16 September", {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}}, bills)
    sent = world.posted[0][2]
    assert sent["task"].startswith("The user asked Tally to change their bills data")
    assert sent["evidence"][0]["source"] == "user:message" and any(e["source"] == "bills-db:bill:3" for e in sent["evidence"])
    status = multi_agent_service.run_status(run_id)
    assert status["summary"]["structured"]["fields"] == {"end_date": "2026-09-16"}
    panel = _text(world.client.get("/ui/suggestions"))
    assert 'id="agent-review-chat_suggestion-' in panel and "Ends Spotify after 16 Sep." in panel
    response = world.client.post(f"/ui/suggestions/{row['id']}/workflow/{status['workflow_id']}/decision", data={"decision": "Accept"})
    assert response.status_code == 200
    assert "change saved" in response.headers["HX-Trigger"]
    assert bills_db.get_suggestion(row["id"])["status"] == "applied"
    assert bills_db.get_bill(3)["end_date"] == "2026-09-16"
    assert 'id="bills-table" hx-swap-oob="true"' in _text(response)


def test_chat_suggestion_without_an_appliable_change_can_only_be_rejected(world):
    world.model.structured = None
    row = bills_db.create_suggestion({"op": "update", "entity": "bill", "entity_id": 3, "payload_json": "{}", "message_id": None})
    run_id = multi_agent_service.start_chat_suggestion(row["id"], "change spotify", {"op": "update", "entity": "bill", "id": 3, "fields": {}}, [])
    wid = multi_agent_service.run_status(run_id)["workflow_id"]
    refused = world.client.post(f"/api/chat/suggestions/{row['id']}/workflow/{wid}/decision", json={"decision": "Accept"})
    assert refused.status_code == 400 and "appliable" in json.loads(refused.data)["error"]
    assert world.client.post(f"/api/chat/suggestions/{row['id']}/workflow/{wid}/decision", json={"decision": "Reject"}).status_code == 200
    assert bills_db.get_suggestion(row["id"])["status"] == "rejected"


def test_chat_hook_starts_a_run_for_a_fresh_proposal_and_never_breaks_the_reply(world, monkeypatch):
    from sophia.backend.services import chat as chat_service

    started = []
    monkeypatch.setattr(multi_agent_service, "start_chat_suggestion", lambda *a: started.append(a) or "run-1")
    preview = {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}}
    assert chat_service._review_with_agents(7, "cancel spotify", preview, []) == "run-1"
    assert started[0][0] == 7
    monkeypatch.setattr(multi_agent_service, "start_chat_suggestion", lambda *a: 1 / 0)
    assert chat_service._review_with_agents(7, "cancel spotify", preview, []) is None


def test_a_chat_proposal_through_ui_chat_starts_the_agents_with_the_bills_as_evidence(world, monkeypatch):
    # The real path: /ui/chat hands the database rows on, not Bill objects.
    # The module shares one live database: an earlier test may already have ended Spotify on
    # 16 Sep, and a proposal that changes nothing never becomes a suggestion. Reset it first.
    bills_db.update_bill(3, {"end_date": None})
    world.model.structured = {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}}
    proposal = {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}, "question": "none", "say": "I've suggested ending Spotify after 16 September."}
    monkeypatch.setattr("sophia.backend.ai.guard.chat", lambda model, messages, timeout=None: {"message": {"content": json.dumps(proposal)}})
    assert world.client.post("/ui/chat", data={"message": "I cancelled Spotify from 16 September"}).status_code == 200
    started = [body for method, path, body in world.posted if (method, path) == ("POST", "/workflow")]
    assert len(started) == 1
    assert any(e["source"] == "bills-db:bill:3" and "Spotify" in e["summary"] for e in started[0]["evidence"])
    assert 'id="agent-review-chat_suggestion-' in _text(world.client.get("/ui/suggestions"))


# --- review fixes: runs that never started, decisions that cannot be applied ------------

def test_an_errored_run_hands_the_suggestion_back_to_the_r1_card(world, monkeypatch):
    """Server down (or busy, or timed out): the R1 Approve/Reject buttons must come back, not an empty block."""
    def down(method, url, json=None, headers=None, timeout=None):
        raise __import__("requests").ConnectionError("no server")

    monkeypatch.setattr(multi_agent.requests, "request", down)
    row = bills_db.create_suggestion({"op": "update", "entity": "bill", "entity_id": 3, "payload_json": json.dumps({"end_date": "2026-09-16"}), "message_id": None})
    run_id = multi_agent_service.start_chat_suggestion(row["id"], "cancel spotify", {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}}, [])
    assert multi_agent_service.run_status(run_id)["state"] == "error"
    panel = _text(world.client.get("/ui/suggestions"))
    assert f"/ui/suggestions/{row['id']}/approve" in panel and f"/ui/suggestions/{row['id']}/reject" in panel
    assert 'id="agent-review-chat_suggestion-' not in panel
    bills_db.update_suggestion(row["id"], {"status": "rejected"})


def test_a_dispute_accept_without_letter_text_is_refused_before_the_server_commits(world):
    world.model.structured, world.model.structured_on_letters = {"op": "update"}, True
    status = _start_letter(world)
    wid = status["workflow_id"]
    before = len(bills_db.list_dispute_drafts(1))
    refused = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Accept"})
    assert refused.status_code == 400 and "structured" in json.loads(refused.data)["error"]
    assert not any(path == f"/workflow/{wid}/decision" for _, path, _ in world.posted)  # not committed
    assert len(bills_db.list_dispute_drafts(1)) == before
    partial = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Partially Accept", "corrected_output": "Dear Spotify, my own words.", "reason": "wrote it myself"})
    assert partial.status_code == 400  # the server would refuse a text correction of an object anyway (contract 1.0)
    ok = world.client.post(f"/api/disputes/1/workflow/{wid}/decision", json={"decision": "Reject", "reason": "not a letter"})
    assert ok.status_code == 200 and len(bills_db.list_dispute_drafts(1)) == before


def test_a_resolved_suggestion_cannot_take_an_agent_decision(world):
    world.model.structured = {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}}
    row = bills_db.create_suggestion({"op": "update", "entity": "bill", "entity_id": 3, "payload_json": "{}", "message_id": None})
    run_id = multi_agent_service.start_chat_suggestion(row["id"], "cancel spotify", {"op": "update", "entity": "bill", "id": 3, "fields": {}}, [])
    wid = multi_agent_service.run_status(run_id)["workflow_id"]
    bills_db.update_suggestion(row["id"], {"status": "rejected"})  # resolved elsewhere (the R1 card, another tab)
    refused = world.client.post(f"/api/chat/suggestions/{row['id']}/workflow/{wid}/decision", json={"decision": "Accept"})
    assert refused.status_code == 400 and "already rejected" in json.loads(refused.data)["error"]
    assert not any(path == f"/workflow/{wid}/decision" for _, path, _ in world.posted)
