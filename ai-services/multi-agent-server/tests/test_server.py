"""Contract tests for the shared Multi-Agent Server."""

import json

import pytest
from agents import Agents
from server import create_app
from settings import Settings

EVIDENCE = [
    {
        "source": "mcp:compare_bill_with_bank_charges",
        "summary": "Bill 12 matched by two $89.00 charges.",
        "data": {"bill_id": 12},
    },
    {"source": "rag:billing", "summary": "Optus Mobile, monthly, $89.00.", "data": {}},
]
REQUEST = {
    "feature": "bills",
    "task": "Draft a dispute letter for bill 12",
    "evidence": EVIDENCE,
}


class FakeModel:
    def generate(self, role, messages, schema, deadline):
        payload = json.loads(messages[1]["content"])
        sources = [item["source"] for item in payload["evidence"]]
        results = {
            "planner": {
                "plan": ["Read supplied evidence", "Produce the requested proposal"],
                "required_evidence": sources,
                "missing_information": [],
                "completion_criteria": ["Supported by evidence"],
            },
            "worker": {
                "output": "Proposal supported by the supplied evidence.",
                "structured": None,
                "evidence_used": sources,
                "limitations": [],
            },
            "reviewer": {
                "verdict": "approve" if sources else "approve_with_changes",
                "risks": [] if sources else ["No supporting evidence"],
                "recommendations": [],
                "review": "Checked the proposal against supplied evidence.",
                "evidence_used": sources,
            },
        }
        return json.dumps(results[role]), "High"

    def health(self):
        return {"ollama": "up", "ready": True, "missing_models": []}


def app_at(directory):
    settings = Settings()
    return create_app(
        log_dir=directory, settings=settings, agent_runner=Agents(settings, FakeModel())
    )


@pytest.fixture
def http(tmp_path):
    return app_at(tmp_path).test_client()


def lines(tmp_path, name):
    from storage import Store

    return Store(tmp_path).export()[name]


def test_health_names_the_service(http):
    body = http.get("/health").get_json()
    assert body["ok"] is True
    assert body["service"] == "multi-agent-server"
    assert set(body["models"]) == {"planner", "worker", "reviewer"}


def test_workflow_runs_all_three_agents_and_waits_for_human(http, tmp_path):
    res = http.post("/workflow", json=REQUEST)
    assert res.status_code == 200
    body = res.get_json()
    assert body["participating_agents"] == [
        "planner_agent",
        "worker_agent",
        "reviewer_agent",
    ]
    assert body["planner_result"]["required_evidence"] == [
        e["source"] for e in EVIDENCE
    ]
    assert body["worker_result"]["evidence_count"] == 2
    assert body["worker_result"]["confidence"] == "High"
    saved = http.get(f"/workflow/{body['workflow_id']}").get_json()
    assert saved["worker_result"]["confidence"] == "High"
    assert body["reviewer_result"]["verdict"] == "approve"
    assert body["human_decision"] == {
        "required": True,
        "status": "pending",
        "allowed_values": ["Accept", "Partially Accept", "Reject"],
    }
    events = [a["event"] for a in lines(tmp_path, "coordination_audit.jsonl")]
    assert events == [
        "workflow_started",
        "agent_started",
        "agent_finished",
        "agent_started",
        "agent_finished",
        "agent_started",
        "agent_finished",
        "handoff",
    ]
    assert (
        lines(tmp_path, "workflow_history.jsonl")[-1]["workflow_id"]
        == body["workflow_id"]
    )


def test_reviewer_flags_missing_evidence(http):
    body = http.post("/workflow", json={**REQUEST, "evidence": []}).get_json()
    assert body["reviewer_result"]["verdict"] == "approve_with_changes"
    assert body["reviewer_result"]["risks"]


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {**REQUEST, "feature": "payroll"},
        {**REQUEST, "task": " "},
        {**REQUEST, "evidence": "not a list"},
        {**REQUEST, "evidence": [{"source": "x"}]},
    ],
)
def test_bad_requests_are_400(http, bad):
    res = http.post("/workflow", json=bad)
    assert res.status_code == 400
    assert res.get_json()["status"] == "error"


def test_accept_records_worker_output_as_final(http, tmp_path):
    wid = http.post("/workflow", json=REQUEST).get_json()["workflow_id"]
    body = http.post(
        f"/workflow/{wid}/decision", json={"decision": "Accept", "decided_by": "sophia"}
    ).get_json()
    hd = body["human_decision"]
    assert hd["status"] == "recorded" and hd["decision"] == "Accept"
    assert hd["final_output"] == body["worker_result"]["output"]
    assert lines(tmp_path, "coordination_audit.jsonl")[-1]["event"] == "human_decision"
    history = lines(tmp_path, "workflow_history.jsonl")
    assert all(h["workflow_id"] == wid for h in history)
    assert history[-1]["human_decision"]["status"] == "recorded"


def test_partially_accept_uses_the_corrected_output(http):
    wid = http.post("/workflow", json=REQUEST).get_json()["workflow_id"]
    body = http.post(
        f"/workflow/{wid}/decision",
        json={
            "decision": "Partially Accept",
            "corrected_output": "Dear Optus, edited.",
            "reason": "Corrected wording",
        },
    ).get_json()
    assert body["human_decision"]["final_output"] == "Dear Optus, edited."


def test_reject_has_no_final_output_and_decision_is_once_only(http):
    wid = http.post("/workflow", json=REQUEST).get_json()["workflow_id"]
    assert (
        http.post(f"/workflow/{wid}/decision", json={"decision": "Reject"}).get_json()[
            "human_decision"
        ]["final_output"]
        is None
    )
    assert (
        http.post(f"/workflow/{wid}/decision", json={"decision": "Accept"}).status_code
        == 409
    )


@pytest.mark.parametrize(
    "body,code",
    [
        ({"decision": "Maybe"}, 400),
        ({"decision": "Partially Accept"}, 400),
        ({"decision": "Reject", "corrected_output": "x"}, 400),
    ],
)
def test_bad_decisions(http, body, code):
    wid = http.post("/workflow", json=REQUEST).get_json()["workflow_id"]
    assert http.post(f"/workflow/{wid}/decision", json=body).status_code == code


def test_unknown_workflow_is_404(http):
    assert http.get("/workflow/nope").status_code == 404
    assert (
        http.post("/workflow/nope/decision", json={"decision": "Accept"}).status_code
        == 404
    )


def test_plan_only_then_reuse_plan(http):
    first = http.post(
        "/workflow", json={**REQUEST, "evidence": [], "options": {"plan_only": True}}
    ).get_json()
    assert (
        first["worker_result"] is None
        and first["human_decision"]["status"] == "awaiting_evidence"
    )
    second = http.post(
        "/workflow", json={**REQUEST, "options": {"reuse_plan": first["workflow_id"]}}
    ).get_json()
    assert second["workflow_id"] == first["workflow_id"]
    assert second["planner_result"] == first["planner_result"]
    assert second["worker_result"]["evidence_count"] == 2
    assert second["human_decision"]["status"] == "pending"
    assert (
        http.post(
            "/workflow",
            json={**REQUEST, "options": {"reuse_plan": first["workflow_id"]}},
        ).status_code
        == 409
    )


def test_status_counts_and_filters(http):
    http.post("/workflow", json=REQUEST)
    http.post(
        "/workflow",
        json={**REQUEST, "feature": "transactions", "task": "Review October spending"},
    )
    body = http.get("/workflow/status").get_json()
    assert body["workflow_history_count"] == 2 and body["pending_decisions"] == 2
    assert body["by_feature"] == {"bills": 1, "transactions": 1}
    assert body["latest_workflow"]["feature"] == "transactions"
    assert (
        http.get("/workflow/status?feature=bills").get_json()["workflow_history_count"]
        == 1
    )


def test_records_survive_a_restart(tmp_path):
    wid = (
        app_at(tmp_path)
        .test_client()
        .post("/workflow", json=REQUEST)
        .get_json()["workflow_id"]
    )
    again = app_at(tmp_path).test_client()
    assert again.get(f"/workflow/{wid}").status_code == 200
    assert (
        again.post(f"/workflow/{wid}/decision", json={"decision": "Accept"}).status_code
        == 200
    )
