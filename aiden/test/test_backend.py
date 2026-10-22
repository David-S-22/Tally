import re
import threading
import time
from typing import Any

import pytest
from datetime import datetime
from types import SimpleNamespace
from pytest import MonkeyPatch, fixture
from flask import Flask
from flask.testing import FlaskClient
from requests import PreparedRequest
from responses import RequestsMock

from backend.app import app
from backend.services import review_queue
from backend.services import anomalies_api
from backend.services import ollama_api
from backend.services import agent_api, multi_agent_client
from backend.services.review_queue import transaction_queue
from backend.helpers import serialise
from database.app import app as dbapp, setup_database
from janelle.database.app import setup_app as setup_transactions
from shared.backend import dto


# Tests
def test_index(client: FlaskClient):
    resp = client.get("/")

    assert resp.status_code == 200
    assert isinstance(resp.json, dict)
    assert resp.json["container"] == "anomalies-backend"


def test_anomalies_database_client_uses_configured_service_timeout(
    monkeypatch: MonkeyPatch,
):
    monkeypatch.setenv("ANOMALIES_DB_URL", "http://mock-database-url/anomalies")
    monkeypatch.setenv("SERVICE_TIMEOUT_SECONDS", "123")
    captured: dict[str, Any] = {}

    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: [],
        )

    monkeypatch.setattr(anomalies_api, "get", get)

    assert anomalies_api.get_all_anomalies() == []
    assert captured["timeout"] == 123


def test_anomalies_database_client_keeps_no_timeout_without_override(
    monkeypatch: MonkeyPatch,
):
    monkeypatch.setenv("ANOMALIES_DB_URL", "http://mock-database-url/anomalies")
    monkeypatch.delenv("SERVICE_TIMEOUT_SECONDS", raising=False)
    captured: dict[str, Any] = {}

    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: [],
        )

    monkeypatch.setattr(anomalies_api, "get", get)

    assert anomalies_api.get_all_anomalies() == []
    assert captured["timeout"] is None


def test_ollama_prompt_requests_logprobs(monkeypatch: MonkeyPatch):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="{}"),
                ),
            ],
        )

    monkeypatch.setattr(
        ollama_api,
        "_get_client",
        lambda: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            ),
        ),
    )

    ollama_api.prompt(
        system_prompt="system",
        user_prompt="user",
        model="test-model",
        temperature=0.2,
        output_tokens=100,
    )

    assert captured["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
    ]
    assert captured["logprobs"] is True
    assert captured["top_logprobs"] == ollama_api._TOP_LOGPROBS
    assert captured["max_tokens"] == 100


def test_ollama_prompt_returns_mean_confidence(monkeypatch: MonkeyPatch):
    import math

    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"is_suspicious": true}',
                },
                "logprobs": {
                    "content": [
                        {
                            "token": "a",
                            "logprob": -0.1,
                            "top_logprobs": [{"token": "b", "logprob": -5.0}],
                        },
                        {
                            "token": "c",
                            "logprob": -0.3,
                            "top_logprobs": [{"token": "d", "logprob": -9.0}],
                        },
                    ],
                },
            }
        ],
    }

    monkeypatch.setattr(
        ollama_api,
        "_get_client",
        lambda: SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kwargs: response),
            ),
        ),
    )

    result = ollama_api.prompt(
        system_prompt="system",
        user_prompt="user",
        model="test-model",
        temperature=0.2,
        output_tokens=100,
    )

    assert result.text == '{"is_suspicious": true}'
    # Each chosen token's probability is exp(logprob); nested top_logprobs are skipped.
    assert result.mean_confidence == (math.exp(-0.1) + math.exp(-0.3)) / 2


def test_ollama_mean_confidence_is_none_without_logprobs():
    assert ollama_api._extract_mean_confidence(SimpleNamespace(output_text="{}")) is None


def test_create_anomaly(client: FlaskClient):
    resp = client.post("/dummy-anomaly")
    assert resp.text.count("data-sort-row") == 1

    resp = client.post("/dummy-anomaly")
    assert resp.text.count("data-sort-row") == 2

    resp = client.post("/dummy-anomaly")
    assert resp.text.count("data-sort-row") == 3

def test_check_transaction_accepts_and_queues(client: FlaskClient, monkeypatch: MonkeyPatch):
    intercept_ollama(monkeypatch, '{"is_suspicious": true, "justification": "Mock response from ollama"}')

    transaction = dto.Transaction(
        id=5,
        amount=9999999,
        merchant="Slim Shady ATMs",
        date=datetime.now(),
        description="we are going to steal your money",
        category_id=0
    )

    resp = client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    assert resp.status_code == 202
    assert resp.json is not None
    assert resp.json["status"] == "queued"
    assert resp.json["transaction_id"] == 5

def test_check_transaction_creates_anomaly_and_persists_it(client: FlaskClient, monkeypatch: MonkeyPatch):
    intercept_ollama(monkeypatch, '{"is_suspicious": true, "justification": "Mock response from ollama"}')

    transaction = dto.Transaction(
        id=42,
        amount=9999999,
        merchant="Slim Shady ATMs",
        date=datetime.now(),
        description="we are going to steal your money",
        category_id=0,
    )

    before = client.get("/anomalies").text.count("data-sort-row")
    resp = client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    assert resp.status_code == 202
    anomalies = client.get("/anomalies").text
    assert anomalies.count("data-sort-row") == before + 1
    assert "Mock response from ollama" in anomalies


def test_check_transaction_no_anomaly_persists_nothing(client: FlaskClient, monkeypatch: MonkeyPatch):
    intercept_ollama(monkeypatch, '{"is_suspicious": false, "justification": "Looks like a routine purchase."}')

    transaction = dto.Transaction(
        id=99,
        amount=42.50,
        merchant="Corner Store",
        date=datetime.now(),
        description="milk and bread",
        category_id=2,
    )

    before = client.get("/anomalies").text.count("data-sort-row")
    resp = client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    assert resp.status_code == 202
    assert client.get("/anomalies").text.count("data-sort-row") == before


def test_create_dummy_anomaly_increases_anomaly_list(client: FlaskClient):
    before = client.get("/anomalies").text.count("data-sort-row")

    client.post("/dummy-anomaly")
    client.post("/dummy-anomaly")

    assert client.get("/anomalies").text.count("data-sort-row") == before + 2


def test_check_transaction_retries_on_invalid_ollama_json(client: FlaskClient, monkeypatch: MonkeyPatch):
    transaction = dto.Transaction(
        id=77,
        amount=5000,
        merchant="Cash Advance",
        date=datetime.now(),
        description="retry test",
        category_id=1,
    )

    before = client.get("/anomalies").text.count("data-sort-row")

    intercept_ollama(monkeypatch, '{not valid json')
    first = client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()
    assert first.status_code == 202
    assert client.get("/anomalies").text.count("data-sort-row") == before

    intercept_ollama(monkeypatch, '{"is_suspicious": true, "justification": "Retry worked"}')
    second = client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    assert second.status_code == 202
    anomalies = client.get("/anomalies").text
    assert anomalies.count("data-sort-row") == before + 1
    assert "Retry worked" in anomalies


def test_check_transaction_invalid_model_json_persists_nothing(client: FlaskClient, monkeypatch: MonkeyPatch):
    intercept_ollama(monkeypatch, '{"is_suspicious": true}')

    transaction = dto.Transaction(
        id=88,
        amount=10,
        merchant="Nope",
        date=datetime.now(),
        description="bad json shape",
        category_id=0,
    )

    before = client.get("/anomalies").text.count("data-sort-row")
    resp = client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    assert resp.status_code == 202
    assert client.get("/anomalies").text.count("data-sort-row") == before


def test_check_transaction_non_bool_is_suspicious_persists_nothing(client: FlaskClient, monkeypatch: MonkeyPatch):
    intercept_ollama(monkeypatch, '{"is_suspicious": "yes", "justification": "not valid"}')

    transaction = dto.Transaction(
        id=89,
        amount=30,
        merchant="Questionable Merchant",
        date=datetime.now(),
        description="bad type",
        category_id=0,
    )

    before = client.get("/anomalies").text.count("data-sort-row")
    resp = client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    assert resp.status_code == 202
    assert client.get("/anomalies").text.count("data-sort-row") == before


def test_check_transaction_persists_exact_anomaly_fields(client: FlaskClient, monkeypatch: MonkeyPatch):
    intercept_ollama(monkeypatch, '{"is_suspicious": true, "justification": "Persisted reason"}')

    transaction = dto.Transaction(
        id=90,
        amount=123456,
        merchant="Large Round Transfer",
        date=datetime.now(),
        description="persist fields",
        category_id=9,
    )

    client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()
    resp = client.get("/anomalies")

    assert resp.status_code == 200
    assert "Persisted reason" in resp.text

    with app.app_context():
        persisted = anomalies_api.get_anomaly_by_transaction_id(90)

    assert persisted is not None
    assert persisted.agent_reason_suspected == "Persisted reason"


def test_check_transaction_persists_mean_confidence_from_logprobs(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    import math

    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": '{"is_suspicious": true, "justification": "Confident finding"}',
                },
                "logprobs": {
                    "content": [
                        {"token": "a", "logprob": -0.02},
                        {"token": "b", "logprob": -0.05},
                    ],
                },
            }
        ],
    }
    intercept_ollama_response(monkeypatch, response)

    transaction = dto.Transaction(
        id=95,
        amount=9999999,
        merchant="Slim Shady ATMs",
        date=datetime.now(),
        description="we are going to steal your money",
        category_id=0,
    )

    client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    with app.app_context():
        persisted = anomalies_api.get_anomaly_by_transaction_id(95)

    assert persisted is not None
    assert persisted.confidence == pytest.approx((math.exp(-0.02) + math.exp(-0.05)) / 2)


def test_check_transaction_confidence_is_none_without_logprobs(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    intercept_ollama(monkeypatch, '{"is_suspicious": true, "justification": "No logprobs"}')

    transaction = dto.Transaction(
        id=96,
        amount=9999999,
        merchant="Slim Shady ATMs",
        date=datetime.now(),
        description="we are going to steal your money",
        category_id=0,
    )

    client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    with app.app_context():
        persisted = anomalies_api.get_anomaly_by_transaction_id(96)

    assert persisted is not None
    assert persisted.confidence is None


def test_review_injects_mcp_examples_into_prompt(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"is_suspicious": false, "justification": "ok"}'))],
        )

    monkeypatch.setattr(
        "backend.services.ollama_api._get_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    intercept_mcp(
        monkeypatch,
        confirmed=[
            {
                "transaction": {"merchant": "Sketchy ATM", "amount": 5000, "description": "cash", "date": "2024-01-01"},
                "anomaly": {"agent_reason_suspected": "large cash withdrawal"},
            }
        ],
        rejected=[
            {
                "transaction": {"merchant": "Coles", "amount": 42, "description": "groceries", "date": "2024-01-02"},
                "anomaly": {"agent_reason_suspected": "flagged in error"},
            }
        ],
    )

    transaction = dto.Transaction(
        id=97,
        amount=10,
        merchant="Coffee",
        date=datetime.now(),
        description="latte",
        category_id=0,
    )

    client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    user_content = captured["messages"][1]["content"]
    assert "Confirmed suspicious examples" in user_content
    assert "Sketchy ATM" in user_content
    assert "Rejected (not suspicious) examples" in user_content
    assert "Coles" in user_content


def test_review_injects_rag_sources_into_prompt_and_persists(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"is_suspicious": true, "justification": "grounded"}'))],
        )

    monkeypatch.setattr(
        "backend.services.ollama_api._get_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    intercept_rag(
        monkeypatch,
        [
            {
                "id": "anomalies_fraud-red-flags.md_0",
                "text": "Charges to ATMs and cash-advance services are higher risk.",
                "metadata": {"source": "fraud-red-flags.md"},
                "distance": 0.31,
            },
            {
                "id": "anomalies_fraud-patterns.md_0",
                "text": "Card testing uses a small charge before a large one.",
                "metadata": {"source": "fraud-patterns.md"},
                "distance": 0.44,
            },
        ],
    )

    transaction = dto.Transaction(
        id=98,
        amount=9999,
        merchant="Sketchy ATM",
        date=datetime.now(),
        description="cash withdrawal",
        category_id=0,
    )

    client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    user_content = captured["messages"][1]["content"]
    assert "Reference guidance" in user_content
    assert "fraud-red-flags.md" in user_content
    assert "higher risk" in user_content

    with app.app_context():
        persisted = anomalies_api.get_anomaly_by_transaction_id(98)

    assert persisted is not None
    assert persisted.sources == ["fraud-red-flags.md", "fraud-patterns.md"]


def test_review_without_rag_persists_empty_sources(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    intercept_ollama(monkeypatch, '{"is_suspicious": true, "justification": "no rag"}')

    transaction = dto.Transaction(
        id=99,
        amount=9999,
        merchant="Sketchy ATM",
        date=datetime.now(),
        description="cash withdrawal",
        category_id=0,
    )

    client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    with app.app_context():
        persisted = anomalies_api.get_anomaly_by_transaction_id(99)

    assert persisted is not None
    assert persisted.sources == []


def test_check_transaction_rejects_invalid_payload(client: FlaskClient):
    resp = client.post("/check-transaction", json={"merchant": "Nope"})

    assert resp.status_code == 500
    assert "Schema mismatch between backend and database" in resp.text


def test_anomaly_alert_returns_alert_when_anomaly_created(client: FlaskClient, monkeypatch: MonkeyPatch):
    intercept_ollama(monkeypatch, '{"is_suspicious": true, "justification": "Long poll reason"}')

    transaction = dto.Transaction(
        id=123,
        amount=9999999,
        merchant="Slim Shady ATMs",
        date=datetime.now(),
        description="we are going to steal your money",
        category_id=0,
    )

    client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    resp = client.get("/anomaly-alert?key=123")

    assert resp.status_code == 200
    assert "<strong>Possible suspicious transaction detected</strong>" in resp.text
    assert "Long poll reason" in resp.text


def test_anomaly_alert_returns_204_when_no_anomaly(client: FlaskClient, monkeypatch: MonkeyPatch):
    intercept_ollama(monkeypatch, '{"is_suspicious": false, "justification": "Looks fine."}')

    transaction = dto.Transaction(
        id=124,
        amount=42.50,
        merchant="Corner Store",
        date=datetime.now(),
        description="milk and bread",
        category_id=2,
    )

    client.post("/check-transaction", json=serialise(transaction))
    transaction_queue.join()

    resp = client.get("/anomaly-alert?key=124")

    assert resp.status_code == 204
    assert resp.data == b""


def test_anomaly_alert_times_out_while_pending(client: FlaskClient, monkeypatch: MonkeyPatch):
    monkeypatch.setattr("backend.app.ANOMALY_WAIT_SECONDS", 0.2)

    resp = client.get("/anomaly-alert?key=999")

    assert resp.status_code == 204
    assert resp.data == b""


def test_anomaly_alert_waits_when_polled_before_enqueue(monkeypatch: MonkeyPatch):
    # Regression: a client that polls for a result *before* the transaction has
    # been enqueued must block until the review completes, rather than treating
    # "not yet pending" as "already reviewed" and returning immediately.
    review_queue.reset()

    sentinel = dto.Anomaly(
        id=1, transaction_id=556, agent_reason_suspected="Raced ahead", is_confirmed_by_user=None)
    monkeypatch.setattr(
        review_queue, "_find_anomaly", lambda key: sentinel if key == 556 else None)

    result = {}

    def poll():
        with app.app_context():
            result["val"] = review_queue.wait_for_result(556, timeout=5)

    poller = threading.Thread(target=poll)
    poller.start()

    # The item is not enqueued yet, so the poll must still be blocking here.
    time.sleep(0.3)
    assert poller.is_alive()
    assert "val" not in result

    # Simulate the transaction being enqueued and then finishing review.
    review_queue._mark_reviewed(556)

    poller.join(timeout=5)
    assert not poller.is_alive()
    assert result["val"] is sentinel


def test_anomaly_alert_requires_key(client: FlaskClient):
    resp = client.get("/anomaly-alert")

    assert resp.status_code == 400


def test_get_anomaly_by_transaction_id_returns_created_anomaly(client: FlaskClient):
    with app.app_context():
        created = anomalies_api.create_anomaly(
            dto.Anomaly(id=0, transaction_id=911, agent_reason_suspected="beans", is_confirmed_by_user=False))

        found = anomalies_api.get_anomaly_by_transaction_id(911)

    assert found is not None
    assert found.id == created.id
    assert found.transaction_id == 911
    assert found.agent_reason_suspected == "beans"


def test_get_anomaly_by_transaction_id_returns_none_when_missing(client: FlaskClient):
    with app.app_context():
        assert anomalies_api.get_anomaly_by_transaction_id(999999) is None


def test_find_anomaly_uses_transaction_lookup(client: FlaskClient):
    with app.app_context():
        created = anomalies_api.create_anomaly(
            dto.Anomaly(id=0, transaction_id=912, agent_reason_suspected="beans", is_confirmed_by_user=False))

        found = review_queue._find_anomaly(912)
        missing = review_queue._find_anomaly(888888)

    assert found is not None
    assert found.id == created.id
    assert missing is None



def test_confirm_anomaly_sets_status(client: FlaskClient):
    with app.app_context():
        created = anomalies_api.create_anomaly(
            dto.Anomaly(id=0, transaction_id=2001, agent_reason_suspected="review me", is_confirmed_by_user=None))

    resp = client.post(f"/anomalies/{created.id}/confirm")

    assert resp.status_code == 200
    assert "Confirmed" in resp.text

    with app.app_context():
        confirmed = anomalies_api.get_anomaly_by_transaction_id(2001)

    assert confirmed is not None
    assert confirmed.is_confirmed_by_user is True


def test_dismiss_anomaly_sets_status(client: FlaskClient):
    with app.app_context():
        created = anomalies_api.create_anomaly(
            dto.Anomaly(id=0, transaction_id=2002, agent_reason_suspected="review me", is_confirmed_by_user=None))

    resp = client.post(f"/anomalies/{created.id}/dismiss")

    assert resp.status_code == 200
    assert "Dismissed" in resp.text

    with app.app_context():
        dismissed = anomalies_api.get_anomaly_by_transaction_id(2002)

    assert dismissed is not None
    assert dismissed.is_confirmed_by_user is False


def test_multi_agent_review_persists_workflow_reference(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "true")
    monkeypatch.setenv("MULTI_AGENT_SERVER_URL", "http://multi-agent")
    monkeypatch.setattr(agent_api, "_build_reviewed_examples_context", lambda: "")
    monkeypatch.setattr(agent_api, "_build_reference_context", lambda _txn: ("guidance", ["fraud.md"]))
    captured = {}

    def create_workflow(body):
        captured.update(body)
        return {
            "status": "success",
            "workflow_id": "workflow-123",
            "worker_result": {
                "status": "success",
                "structured": {
                    "is_suspicious": True,
                    "justification": "A clearly unusual transaction.",
                },
            },
            "reviewer_result": {"status": "success", "verdict": "approve"},
        }

    monkeypatch.setattr(multi_agent_client, "create_workflow", create_workflow)
    transaction = dto.Transaction(
        id=2111,
        amount=9999,
        merchant="Unknown ATM",
        date=datetime.now(),
        description="cash withdrawal",
        category_id=0,
    )

    with app.app_context():
        anomaly = agent_api.review_new_transaction(transaction)
        assert anomaly is not None
        persisted = anomalies_api.create_anomaly(anomaly)
        found = anomalies_api.get_anomaly_by_transaction_id(transaction.id)

    assert captured["feature"] == "anomalies"
    assert captured["evidence"][0]["source"].startswith("transaction:2111:")
    assert persisted.workflow_id == "workflow-123"
    assert found is not None and found.workflow_id == "workflow-123"


def test_multi_agent_client_forwards_backend_token_and_long_timeout(
    monkeypatch: MonkeyPatch,
):
    monkeypatch.setenv("MULTI_AGENT_SERVER_URL", "http://multi-agent")
    monkeypatch.setenv("MULTI_AGENT_TOKEN", "test-token")
    monkeypatch.setenv("MULTI_AGENT_TIMEOUT_SECONDS", "600")
    captured: dict[str, Any] = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"workflow_id": "workflow-123"},
        )

    monkeypatch.setattr(multi_agent_client.requests, "post", post)

    result = multi_agent_client.create_workflow({"feature": "anomalies"})

    assert result["workflow_id"] == "workflow-123"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert captured["timeout"] == 600


def test_multi_agent_creation_timeout_adopts_only_matching_saved_request(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "true")
    monkeypatch.setenv("MULTI_AGENT_SERVER_URL", "http://multi-agent")
    monkeypatch.setattr(agent_api, "_build_reviewed_examples_context", lambda: "")
    monkeypatch.setattr(agent_api, "_build_reference_context", lambda _txn: ("", []))
    submitted = []

    def create_workflow(body):
        submitted.append(body)
        raise multi_agent_client.MultiAgentError("multi_agent_timeout")

    def latest_workflow(feature):
        assert feature == "anomalies"
        return {
            "status": "success",
            "workflow_id": "saved-workflow",
            "evidence": submitted[0]["evidence"],
            "worker_result": {
                "status": "success",
                "structured": {
                    "is_suspicious": False,
                    "justification": "This appears to be a routine purchase.",
                },
            },
            "reviewer_result": {"status": "success", "verdict": "approve"},
        }

    monkeypatch.setattr(multi_agent_client, "create_workflow", create_workflow)
    monkeypatch.setattr(multi_agent_client, "latest_workflow", latest_workflow)
    transaction = dto.Transaction(
        id=2115,
        amount=20,
        merchant="Corner store",
        date=datetime.now(),
        description="groceries",
        category_id=0,
    )

    with app.app_context():
        result = agent_api.review_new_transaction(transaction)

    assert result is None
    assert len(submitted) == 1
    assert submitted[0]["evidence"][0]["source"].startswith("transaction:2115:")


def test_multi_agent_decision_is_recorded_before_local_confirmation(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    with app.app_context():
        anomaly = anomalies_api.create_anomaly(
            dto.Anomaly(
                id=0,
                transaction_id=2112,
                agent_reason_suspected="review me",
                is_confirmed_by_user=None,
                workflow_id="workflow-123",
            )
        )
    captured: dict[str, Any] = {}

    def record_decision(workflow_id, body):
        captured.update(workflow_id=workflow_id, **body)
        return {
            "human_decision": {
                "status": "recorded",
                "decision": "Accept",
            },
        }

    monkeypatch.setattr(multi_agent_client, "record_decision", record_decision)

    response = client.post(
        f"/anomalies/{anomaly.id}/confirm",
        json={"reason": "I verified this transaction."},
    )

    assert response.status_code == 200
    assert captured == {
        "workflow_id": "workflow-123",
        "decision": "Accept",
        "reason": "I verified this transaction.",
    }
    with app.app_context():
        confirmed = anomalies_api.get_anomaly_by_transaction_id(2112)
    assert confirmed is not None and confirmed.is_confirmed_by_user is True


def test_multi_agent_decision_failure_does_not_update_local_status(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    with app.app_context():
        anomaly = anomalies_api.create_anomaly(
            dto.Anomaly(
                id=0,
                transaction_id=2113,
                agent_reason_suspected="review me",
                is_confirmed_by_user=None,
                workflow_id="workflow-123",
            )
        )

    def fail_decision(_workflow_id, _body):
        raise multi_agent_client.MultiAgentError("multi_agent_unavailable")

    monkeypatch.setattr(multi_agent_client, "record_decision", fail_decision)
    response = client.post(
        f"/anomalies/{anomaly.id}/dismiss",
        json={"reason": "I checked the purchase."},
    )

    assert response.status_code == 503
    with app.app_context():
        unchanged = anomalies_api.get_anomaly_by_transaction_id(2113)
    assert unchanged is not None and unchanged.is_confirmed_by_user is None


def test_review_button_only_renders_when_unreviewed(client: FlaskClient):
    with app.app_context():
        unreviewed = anomalies_api.create_anomaly(
            dto.Anomaly(id=0, transaction_id=2003, agent_reason_suspected="pending", is_confirmed_by_user=None))

    rows = client.get("/anomalies").text
    assert f'data-anomaly-id="{unreviewed.id}"' in rows
    assert "onclick=\"openReviewModal(this)\"" in rows

    client.post(f"/anomalies/{unreviewed.id}/confirm")

    rows = client.get("/anomalies").text
    assert f'data-anomaly-id="{unreviewed.id}"' not in rows
    assert '<span class="status-badge status-accepted">Confirmed</span>' in rows


def test_confirm_missing_anomaly_returns_404(client: FlaskClient):
    resp = client.post("/anomalies/999999/confirm")

    assert resp.status_code == 404


def test_anomaly_row_renders_confidence_badge(client: FlaskClient):
    with app.app_context():
        anomalies_api.create_anomaly(
            dto.Anomaly(
                id=0,
                transaction_id=4444,
                agent_reason_suspected="odd",
                is_confirmed_by_user=None,
                confidence=0.92,
            )
        )

    rows = client.get("/anomalies").text

    assert 'data-sort-confidence="0.92"' in rows
    assert '<td class="anomaly-confidence">' in rows
    assert '<td class="anomaly-status">' in rows
    assert '<span class="confidence-indicator confidence-high">High</span>' in rows


def test_anomaly_row_renders_dash_when_confidence_missing(client: FlaskClient):
    with app.app_context():
        anomalies_api.create_anomaly(
            dto.Anomaly(
                id=0,
                transaction_id=4445,
                agent_reason_suspected="odd",
                is_confirmed_by_user=None,
            )
        )

    rows = client.get("/anomalies").text

    assert 'data-sort-confidence=""' in rows
    assert 'aria-label="No confidence score"' in rows


def test_anomaly_row_shows_transaction_date_and_merchant(client: FlaskClient, monkeypatch: MonkeyPatch):
    with app.app_context():
        anomalies_api.create_anomaly(
            dto.Anomaly(id=0, transaction_id=4242, agent_reason_suspected="odd", is_confirmed_by_user=None))

    txn = dto.Transaction(
        id=4242,
        amount=100,
        merchant="Suspicious Merchant Co",
        date=datetime(2025, 1, 15),
        description="x",
        category_id=0,
    )
    monkeypatch.setattr("backend.app.transaction_api.get_all_transactions", lambda: [txn])

    rows = client.get("/anomalies").text

    assert "Suspicious Merchant Co" in rows
    assert "2025-01-15" in rows
    assert "<td>4242</td>" not in rows
    assert 'data-sort-id="' not in rows
    assert 'data-sort-date="2025-01-15T00:00:00"' in rows
    assert 'data-sort-merchant="Suspicious Merchant Co"' in rows
    assert 'data-sort-status="unreviewed"' in rows


def test_review_button_includes_transaction_details_and_anomaly_reason(
    client: FlaskClient, monkeypatch: MonkeyPatch
):
    reason = "A suspicious <script>alert('x')</script> charge."
    with app.app_context():
        anomaly = anomalies_api.create_anomaly(
            dto.Anomaly(
                id=0,
                transaction_id=4243,
                agent_reason_suspected=reason,
                is_confirmed_by_user=None,
                confidence=0.834,
            )
        )

    txn = dto.Transaction(
        id=4243,
        amount=125.5,
        merchant="Corner & Co.",
        date=datetime(2025, 1, 15, 9, 30),
        description="Card payment <script>",
        category_id=80,
    )
    monkeypatch.setattr("backend.app.transaction_api.get_all_transactions", lambda: [txn])

    response = client.get("/anomalies")
    rows = response.text

    assert response.status_code == 200
    assert f'data-anomaly-id="{anomaly.id}"' in rows
    assert 'data-transaction-id' not in rows
    assert 'data-date="2025-01-15T09:30:00"' in rows
    assert 'data-merchant="Corner &amp; Co."' in rows
    assert 'data-amount="125.50"' in rows
    assert 'data-description="Card payment &lt;script&gt;"' in rows
    assert 'data-category="Dining"' in rows
    assert 'data-confidence="83.4"' in rows
    assert 'data-confidence-level="high"' in rows
    assert 'data-reason="A suspicious &lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt; charge."' in rows


# Pytest fixtures
@fixture
def client():
    setup_database(":memory:")
    review_queue.reset()

    with app.test_client() as client:
        yield client

@fixture(autouse=True)
def integrate_services(monkeypatch: MonkeyPatch):
    # In order to get the backend client to send requests
    # to the actual database server, we have to intercept
    # the requests via responses and redirect them to the
    # test_client instance. Same with the transactions db

    transactionsapp = setup_transactions()

    monkeypatch.setenv("ANOMALIES_DB_URL", "http://mock-database-url/anomalies")
    monkeypatch.setenv("TRANSACTIONS_DB_URL", "http://mock-transactions-url")
    monkeypatch.setenv("OLLAMA_MODEL", "billy")
    monkeypatch.setenv("OLLAMA_URL", "http://mock-ollama-url")
    monkeypatch.setenv("MCP_SERVER_URL", "http://mock-mcp-server/mcp")
    monkeypatch.setenv("RAG_ENABLED", "true")
    monkeypatch.setenv("RAG_SERVER_URL", "http://mock-rag-server:5003")
    monkeypatch.setenv("RAG_FEATURE", "anomalies")
    monkeypatch.setenv("RAG_TOP_K", "3")
    monkeypatch.setenv("RAG_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("MULTI_AGENT_ENABLED", "false")

    # fastmcp bypasses the requests-based RequestsMock, so stub the MCP client by
    # default; tests that need reviewed examples override this via intercept_mcp.
    monkeypatch.setattr(
        "backend.services.mcp_client.call_tool",
        lambda name, arguments=None: ([], 0.0),
    )

    dburl = re.compile(r"^http://mock-database-url/anomalies(/.*)?$")
    transactionsurl = re.compile(r"^http://mock-transactions-url/.+$")

    with RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add_callback(RequestsMock.GET, dburl, lambda r: intercept(dbapp, r))
        rsps.add_callback(RequestsMock.POST, dburl, lambda r: intercept(dbapp, r))
        rsps.add_callback(RequestsMock.PATCH, dburl, lambda r: intercept(dbapp, r))
        rsps.add_callback(RequestsMock.DELETE, dburl, lambda r: intercept(dbapp, r))
        rsps.add_callback(RequestsMock.OPTIONS, dburl, lambda r: intercept(dbapp, r))

        rsps.add_callback(RequestsMock.GET, transactionsurl, lambda r: intercept(transactionsapp, r))
        rsps.add_callback(RequestsMock.OPTIONS, transactionsurl, lambda r: intercept(transactionsapp, r))

        yield

def intercept(app_to_inject: Flask, request: PreparedRequest):
    with app_to_inject.test_client() as client:
        resp = client.open(
            path=request.path_url,
            method=request.method,
            headers=dict(request.headers),
            data=request.body)

        return resp.status_code, dict(resp.headers), resp.get_data()

def intercept_ollama(monkeypatch: MonkeyPatch, model_response: str):
    create = lambda **kwargs: SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=model_response),
            ),
        ],
    )
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        ),
    )

    # Monkeypatch ollama api
    monkeypatch.setattr("backend.services.ollama_api._get_client", lambda: fake_client)

def intercept_ollama_response(monkeypatch: MonkeyPatch, response):
    """Intercept Ollama with a full response payload (e.g. including logprobs)."""
    create = lambda **kwargs: response
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        ),
    )

    monkeypatch.setattr("backend.services.ollama_api._get_client", lambda: fake_client)

def intercept_mcp(monkeypatch: MonkeyPatch, confirmed=None, rejected=None):
    """Stub the MCP client so the agent sees reviewed examples without a server."""
    from backend.services.agent_api import (
        _CONFIRMED_ANOMALIES_TOOL,
        _REJECTED_ANOMALIES_TOOL,
    )

    results = {
        _CONFIRMED_ANOMALIES_TOOL: confirmed or [],
        _REJECTED_ANOMALIES_TOOL: rejected or [],
    }

    monkeypatch.setattr(
        "backend.services.mcp_client.call_tool",
        lambda name, arguments=None: (results.get(name, []), 0.0),
    )

def intercept_rag(monkeypatch: MonkeyPatch, results):
    """Stub the RAG client so the agent sees reference guidance without a server."""
    monkeypatch.setattr(
        "backend.services.rag_client.retrieve",
        lambda feature, question, k=3, where=None: list(results),
    )
