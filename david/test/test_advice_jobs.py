"""Submission returns an ID to poll; polling does not repeat model work."""
import os
from unittest.mock import patch

import responses

from backend.savings_service.app import setup_app

JOB_ID = "2d8f120b-d4e2-44a3-84b4-34f99c6d02e5"
BASE = "http://multi-agent:5010"


@responses.activate
def test_submit_then_poll_until_complete():
    client = setup_app("http://savings-db:6002", "http://transactions-db:6001").test_client()
    with patch("backend.savings_service.app.generate_savings_advice", return_value={"job_id": JOB_ID}) as generate:
        submitted = client.post("/ai-suggestion/jobs", data={"suggestion": "Previous advice"})
    generate.assert_called_once_with("http://savings-db:6002", tx_url="http://transactions-db:6001", previous_suggestion="Previous advice", background=True)
    assert f'hx-get="/savings-backend/ai-suggestion/jobs/{JOB_ID}"' in submitted.text
    assert 'hx-trigger="every 5s"' in submitted.text
    assert 'class="btn-refresh" disabled' in submitted.text
    responses.get(BASE + f"/workflow/jobs/{JOB_ID}", json={"feature": "savings", "workflow_state": "running"})
    responses.get(BASE + f"/workflow/jobs/{JOB_ID}", json={
        "feature": "savings", "workflow_state": "pending",
        "context": {"savings_sources": "guide.md"},
        "worker_result": {"status": "success", "output": "Save $20 each week.", "confidence": "High"},
        "reviewer_result": {"status": "success", "verdict": "approve", "review": "Supported by the evidence."},
    })
    with patch.dict(os.environ, {"MULTI_AGENT_URL": BASE}):
        running = client.get(f"/ai-suggestion/jobs/{JOB_ID}")
        done = client.get(f"/ai-suggestion/jobs/{JOB_ID}")
    assert 'hx-trigger="every 5s"' in running.text
    assert 'hx-trigger="every 5s"' not in done.text
    assert all(text in done.text for text in ["Save $20 each week.", "guide.md", "High", "Supported by the evidence."])
    assert 'hx-post="/savings-backend/ai-suggestion/action?accepted=true"' in done.text
    assert all(call.request.method == "GET" for call in responses.calls)


@responses.activate
def test_failed_job_stops_polling_and_disables_actions():
    client = setup_app("http://savings-db:6002", "http://transactions-db:6001").test_client()
    responses.get(BASE + f"/workflow/jobs/{JOB_ID}", json={
        "feature": "savings", "workflow_state": "failed", "worker_result": None,
        "error": "Model deadline exceeded",
    })
    with patch.dict(os.environ, {"MULTI_AGENT_URL": BASE}):
        result = client.get(f"/ai-suggestion/jobs/{JOB_ID}")
    assert "Error:" in result.text
    assert 'hx-trigger="every 5s"' not in result.text
    assert 'hx-post="/savings-backend/ai-suggestion/action?accepted=true"' not in result.text
