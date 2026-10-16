"""The Multi-Agent client maps every failure to a safe ModeError code and never leaks upstream text."""
from types import SimpleNamespace

import pytest
import requests

from sophia.backend import config
from sophia.backend.clients import multi_agent
from sophia.backend.services.errors import ModeError, NotFound

RECORD = {
    "workflow_id": "w1", "planner_result": {}, "worker_result": {"output": "Dear Optus"},
    "reviewer_result": {}, "human_decision": {"status": "pending"}, "duration_ms": 5,
}


def _post(monkeypatch, status=200, body=RECORD, raises=None):
    calls = []

    def post(url, json=None, timeout=None):
        calls.append((url, json, timeout))
        if raises:
            raise raises
        return SimpleNamespace(status_code=status, json=lambda: body)

    monkeypatch.setattr(multi_agent.requests, "post", post)
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", True)
    monkeypatch.setattr(config, "MULTI_AGENT_SERVER_URL", "http://ma.test:5010")
    return calls


def test_disabled_raises_before_any_network(monkeypatch):
    calls = _post(monkeypatch)
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    with pytest.raises(ModeError) as err:
        multi_agent.run_workflow("task", [])
    assert err.value.code == "multi_agent_disabled" and err.value.status == 503
    assert calls == []


def test_run_workflow_posts_the_contract_body(monkeypatch):
    calls = _post(monkeypatch)
    record = multi_agent.run_workflow("Draft", [{"source": "s", "summary": "x"}], context={"k": 1})
    assert record == RECORD
    url, body, timeout = calls[0]
    assert url == "http://ma.test:5010/workflow"
    assert body == {"feature": "bills", "task": "Draft", "evidence": [{"source": "s", "summary": "x"}], "context": {"k": 1}, "options": {}}
    assert timeout == config.MULTI_AGENT_TIMEOUT_SECONDS


def test_record_decision_posts_to_the_workflow_and_validates_locally(monkeypatch):
    calls = _post(monkeypatch)
    multi_agent.record_decision("w1", "Partially Accept", corrected_output="edited", decided_by="bills-ui")
    url, body, _ = calls[0]
    assert url == "http://ma.test:5010/workflow/w1/decision"
    assert body["decision"] == "Partially Accept" and body["corrected_output"] == "edited"
    with pytest.raises(ValueError):
        multi_agent.record_decision("w1", "Maybe")


@pytest.mark.parametrize("raises,code", [
    (requests.Timeout(), "multi_agent_timeout"),
    (requests.ConnectionError("secret host"), "multi_agent_connection"),
])
def test_transport_failures(monkeypatch, raises, code):
    _post(monkeypatch, raises=raises)
    with pytest.raises(ModeError) as err:
        multi_agent.run_workflow("t", [])
    assert err.value.code == code
    assert "secret" not in err.value.message


@pytest.mark.parametrize("status,code", [(400, "multi_agent_rejected"), (409, "multi_agent_rejected"), (500, "multi_agent_error"), (503, "multi_agent_error")])
def test_http_errors(monkeypatch, status, code):
    _post(monkeypatch, status=status, body={"status": "error", "error": "upstream detail"})
    with pytest.raises(ModeError) as err:
        multi_agent.run_workflow("t", [])
    assert err.value.code == code
    assert "upstream" not in err.value.message


def test_404_is_not_found(monkeypatch):
    _post(monkeypatch, status=404, body={})
    with pytest.raises(NotFound):
        multi_agent.record_decision("nope", "Accept")


def test_malformed_record_is_invalid_result(monkeypatch):
    _post(monkeypatch, body={"workflow_id": "w1"})
    with pytest.raises(ModeError) as err:
        multi_agent.run_workflow("t", [])
    assert err.value.code == "multi_agent_invalid_result"
