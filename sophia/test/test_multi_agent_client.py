"""The Multi-Agent client maps every failure to a safe ModeError code and never leaks upstream text."""
from types import SimpleNamespace

import pytest
import requests

from sophia.backend import config
from sophia.backend.clients import multi_agent
from sophia.backend.services.errors import ModeError, NotFound

RECORD = {
    "workflow_id": "w1", "status": "success", "workflow_state": "pending", "planner_result": {},
    "worker_result": {"output": "Dear Optus"}, "reviewer_result": {}, "human_decision": {"status": "pending"}, "duration_ms": 5,
}
FAILED = {**RECORD, "status": "error", "workflow_state": "failed", "code": "model_timeout"}


def _server(monkeypatch, status=200, body=RECORD, raises=None, token=""):
    calls = []

    def req(method, url, json=None, headers=None, timeout=None):
        calls.append((method, url, json, headers, timeout))
        if raises:
            raise raises
        return SimpleNamespace(status_code=status, json=lambda: body)

    monkeypatch.setattr(multi_agent.requests, "request", req)
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", True)
    monkeypatch.setattr(config, "MULTI_AGENT_SERVER_URL", "http://ma.test:5010")
    monkeypatch.setattr(config, "MULTI_AGENT_TOKEN", token)
    return calls


def test_disabled_raises_before_any_network(monkeypatch):
    calls = _server(monkeypatch)
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    with pytest.raises(ModeError) as err:
        multi_agent.run_workflow("task", [])
    assert err.value.code == "multi_agent_disabled" and err.value.status == 503
    assert calls == []


def test_run_workflow_posts_the_contract_body_with_the_token(monkeypatch):
    calls = _server(monkeypatch, token="x" * 32)
    record = multi_agent.run_workflow("Draft", [{"source": "s", "summary": "x"}], context={"k": 1})
    assert record == RECORD
    method, url, body, headers, timeout = calls[0]
    assert (method, url) == ("POST", "http://ma.test:5010/workflow")
    assert body == {"contract_version": "1.0", "feature": "bills", "task": "Draft", "evidence": [{"source": "s", "summary": "x"}], "context": {"k": 1}}
    assert headers["Authorization"] == "Bearer " + "x" * 32
    assert timeout == config.MULTI_AGENT_TIMEOUT_SECONDS >= 540


def test_no_token_means_no_authorization_header(monkeypatch):
    calls = _server(monkeypatch)
    multi_agent.get_workflow("w1")
    assert "Authorization" not in calls[0][3]
    assert calls[0][:2] == ("GET", "http://ma.test:5010/workflow/w1")


def test_record_decision_posts_to_the_workflow_and_validates_locally(monkeypatch):
    calls = _server(monkeypatch)
    multi_agent.record_decision("w1", "Partially Accept", corrected_output="edited", reason="r", decided_by="bills-ui")
    assert calls[0][1].endswith("/workflow/w1/decision")
    assert calls[0][2] == {"decision": "Partially Accept", "corrected_output": "edited", "reason": "r", "decided_by": "bills-ui"}
    with pytest.raises(ValueError):
        multi_agent.record_decision("w1", "Maybe")


def test_a_failed_workflow_is_returned_not_raised(monkeypatch):
    _server(monkeypatch, status=503, body=FAILED)
    record = multi_agent.run_workflow("t", [])
    assert multi_agent.failed(record) and record["workflow_id"] == "w1"


@pytest.mark.parametrize("raises,code", [
    (requests.Timeout(), "multi_agent_timeout"),
    (requests.ConnectionError("secret host"), "multi_agent_connection"),
])
def test_transport_failures(monkeypatch, raises, code):
    _server(monkeypatch, raises=raises)
    with pytest.raises(ModeError) as err:
        multi_agent.run_workflow("t", [])
    assert err.value.code == code and "secret" not in err.value.message


@pytest.mark.parametrize("status,body,code", [
    (503, {"status": "error", "code": "workflow_busy", "error": "busy"}, "multi_agent_busy"),
    (401, {"status": "error", "code": "unauthorized"}, "multi_agent_unauthorized"),
    (400, {"status": "error", "error": "upstream detail"}, "multi_agent_rejected"),
    (409, {"status": "error", "error": "upstream detail"}, "multi_agent_rejected"),
    (413, {"status": "error"}, "multi_agent_rejected"),
    (500, {"status": "error", "error": "upstream detail"}, "multi_agent_error"),
    (503, {"status": "error", "code": "storage_unavailable"}, "multi_agent_error"),
    (200, {"workflow_id": "w1"}, "multi_agent_invalid_result"),
])
def test_http_errors(monkeypatch, status, body, code):
    _server(monkeypatch, status=status, body=body)
    with pytest.raises(ModeError) as err:
        multi_agent.run_workflow("t", [])
    assert err.value.code == code and "upstream" not in err.value.message


def test_404_is_not_found(monkeypatch):
    _server(monkeypatch, status=404, body={"status": "error"})
    with pytest.raises(NotFound):
        multi_agent.record_decision("nope", "Accept")
