"""Async submission returns before model execution and persists pollable results."""
import threading

import pytest
from agents import Agents
from server import create_app
from settings import Settings
from test_server import FakeModel, REQUEST


class BlockingModel(FakeModel):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def generate(self, role, messages, schema, deadline):
        self.calls.append(role)
        if role == "planner":
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("Test did not release planner")
        return super().generate(role, messages, schema, deadline)


@pytest.mark.parametrize("feature", ["bills", "savings", "transactions", "budgets", "anomalies"])
def test_async_job_survives_submission_connection_closing_and_is_read_only_to_poll(tmp_path, feature):
    model = BlockingModel()
    settings = Settings(host="127.0.0.1")
    app = create_app(log_dir=tmp_path, settings=settings, agent_runner=Agents(settings, model))
    coordinator = app.config["coordinator"]
    try:
        with app.test_client() as client:
            response = client.post("/workflow/jobs", json={**REQUEST, "feature": feature})
            assert response.status_code == 202
            location = response.headers["Location"]
        assert model.entered.wait(2)
        with app.test_client() as polling_client:
            for _ in range(3):
                record = polling_client.get(location).get_json()
                assert record["workflow_state"] == "running"
            assert model.calls == ["planner"]
            busy = polling_client.post("/workflow/jobs", json=REQUEST)
            assert busy.status_code == 503
            assert busy.get_json()["code"] == "workflow_busy"
            assert busy.headers["Retry-After"] == "5"
            # Existing synchronous clients observe the same capacity protection.
            assert polling_client.post("/workflow", json=REQUEST).status_code == 503
        model.release.set()
        assert coordinator.gate.acquire(timeout=3)
        coordinator.gate.release()
        with app.test_client() as client:
            result = client.get(location).get_json()
        assert result["workflow_state"] == "pending"
        assert result["worker_result"]["status"] == "success"
        assert result["reviewer_result"]["verdict"] == "approve"
        assert model.calls == ["planner", "worker", "reviewer"]
        # A fresh process reads completed state from SQLite without running models.
        restarted = create_app(log_dir=tmp_path, settings=settings, agent_runner=Agents(settings, FakeModel()))
        assert restarted.test_client().get(location).get_json() == result
    finally:
        model.release.set()
        assert coordinator.gate.acquire(timeout=5)
        coordinator.gate.release()


def test_async_jobs_require_authentication_and_validate_before_start(tmp_path):
    settings = Settings(token="a" * 32)
    app = create_app(log_dir=tmp_path, settings=settings, agent_runner=Agents(settings, FakeModel()))
    client = app.test_client()
    assert client.post("/workflow/jobs", json=REQUEST).status_code == 401
    headers = {"Authorization": "Bearer " + settings.token}
    assert client.post("/workflow/jobs", json={}, headers=headers).status_code == 400
    assert client.get("/workflow/jobs/00000000-0000-0000-0000-000000000000", headers=headers).status_code == 404
    assert app.config["coordinator"].store.all() == []


def test_thread_start_failure_is_saved_and_releases_capacity(tmp_path, monkeypatch):
    from unittest.mock import patch
    settings = Settings(host="127.0.0.1")
    app = create_app(log_dir=tmp_path, settings=settings, agent_runner=Agents(settings, FakeModel()))
    with patch("coordinator.threading.Thread.start", side_effect=OSError("cannot start thread")):
        response = app.test_client().post("/workflow/jobs", json=REQUEST)
    assert response.status_code == 503
    coordinator = app.config["coordinator"]
    records = coordinator.store.all()
    assert len(records) == 1
    assert records[0]["workflow_state"] == "failed"
    assert records[0]["code"] == "workflow_failure"
    assert not coordinator.gate.locked()
