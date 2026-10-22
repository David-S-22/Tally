from typing import Any, List

import requests
from flask.testing import FlaskClient
from pytest import fixture

from backend.helpers import deserialise_safe, serialise
from database import reconcile
from database.app import app, setup_database
from database.models import Anomaly, db
from database.seed import SEED_ANOMALIES, seed_database_if_empty
from janelle.database.seed import TRANSACTIONS
from shared.backend import dto


# Tests
def test_index(client: FlaskClient):
    resp = client.get("/")

    assert resp.status_code == 200
    assert isinstance(resp.json, dict)
    assert resp.json["container"] == "anomalies-db"

def test_create_anomaly(client: FlaskClient):
    json = dict(transaction_id=101, agent_reason_suspected="beans", is_confirmed_by_user=False)

    # Check we can create the object
    resp = client.post("/anomalies/", json=json)

    assert resp.status_code == 201
    assert isinstance(create_json := resp.json, dict)

    # Check we can fetch the object we just created
    resp = client.get("/anomalies/" + str(create_json["id"]))

    assert resp.status_code == 200
    assert isinstance(resp.json, dict)
    assert resp.json == create_json

    # Check the expected keys
    assert isinstance(resp.json["id"], int)
    assert isinstance(resp.json["transaction_id"], int)
    assert resp.json["is_confirmed_by_user"] == False
    assert resp.json["agent_reason_suspected"] == "beans"
    # confidence is optional and defaults to null when omitted.
    assert resp.json["confidence"] is None


def test_create_anomaly_persists_confidence(client: FlaskClient):
    json = dict(
        transaction_id=110,
        agent_reason_suspected="beans",
        is_confirmed_by_user=None,
        confidence=0.87,
    )

    resp = client.post("/anomalies/", json=json)

    assert resp.status_code == 201, resp.text
    assert isinstance(create_json := resp.json, dict)
    assert create_json["confidence"] == 0.87

    resp = client.get("/anomalies/" + str(create_json["id"]))

    assert resp.status_code == 200
    assert isinstance(resp.json, dict)
    assert resp.json["confidence"] == 0.87


def test_create_anomaly_defaults_sources_to_empty_list(client: FlaskClient):
    json = dict(transaction_id=120, agent_reason_suspected="beans", is_confirmed_by_user=None)

    resp = client.post("/anomalies/", json=json)

    assert resp.status_code == 201, resp.text
    assert isinstance(create_json := resp.json, dict)
    assert create_json["sources"] == []


def test_create_anomaly_persists_sources(client: FlaskClient):
    json = dict(
        transaction_id=121,
        agent_reason_suspected="beans",
        is_confirmed_by_user=None,
        sources=["fraud-red-flags.md", "fraud-patterns.md"],
    )

    resp = client.post("/anomalies/", json=json)

    assert resp.status_code == 201, resp.text
    assert isinstance(create_json := resp.json, dict)
    assert create_json["sources"] == ["fraud-red-flags.md", "fraud-patterns.md"]

    resp = client.get("/anomalies/" + str(create_json["id"]))

    assert resp.status_code == 200
    assert isinstance(resp.json, dict)
    assert resp.json["sources"] == ["fraud-red-flags.md", "fraud-patterns.md"]


def test_create_anomaly_persists_workflow_id(client: FlaskClient):
    resp = client.post(
        "/anomalies/",
        json={
            "transaction_id": 124,
            "agent_reason_suspected": "review me",
            "workflow_id": "workflow-123",
        },
    )

    assert resp.status_code == 201
    assert resp.json["workflow_id"] == "workflow-123"
    fetched = client.get(f"/anomalies/{resp.json['id']}")
    assert fetched.json["workflow_id"] == "workflow-123"


def test_create_anomaly_deduplicates_sources(client: FlaskClient):
    json = dict(
        transaction_id=122,
        agent_reason_suspected="beans",
        is_confirmed_by_user=None,
        sources=["a.md", "a.md", "b.md"],
    )

    resp = client.post("/anomalies/", json=json)

    assert resp.status_code == 201, resp.text
    assert isinstance(create_json := resp.json, dict)
    assert create_json["sources"] == ["a.md", "b.md"]


def test_create_anomaly_rejects_invalid_sources(client: FlaskClient):
    resp = client.post(
        "/anomalies/",
        json={
            "transaction_id": 123,
            "agent_reason_suspected": "beans",
            "sources": [1, 2, 3],
        },
    )

    assert resp.status_code == 400
    assert isinstance(resp.json, dict)
    assert "Field sources expected try_parse_string_list" in resp.json["description"]


def test_confidence_out_of_range_is_rejected(client: FlaskClient):
    import pytest
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        db.session.add(
            Anomaly(transaction_id=115, agent_reason_suspected="beans", confidence=1.5)
        )

        with pytest.raises(IntegrityError):
            db.session.commit()

        db.session.rollback()

def test_delete_anomaly(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=201, agent_reason_suspected="beans", is_confirmed_by_user=False)
    response = client.delete(f"/anomalies/{anomaly.id}")

    assert response.status_code == 204
    assert client.get(f"/anomalies/{anomaly.id}").status_code == 404

def test_delete_anomaly_by_transaction(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=301, agent_reason_suspected="beans", is_confirmed_by_user=False)
    response = client.delete(f"/anomalies/by-transaction/{anomaly.transaction_id}")

    assert response.status_code == 204
    assert client.get(f"/anomalies/{anomaly.id}").status_code == 404

def test_get_anomaly_by_transaction_returns_anomaly(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=501, agent_reason_suspected="beans", is_confirmed_by_user=False)

    response = client.get(f"/anomalies/by-transaction/{anomaly.transaction_id}")

    assert response.status_code == 200
    assert isinstance(response.json, dict)
    assert response.json["id"] == anomaly.id
    assert response.json["transaction_id"] == 501
    assert response.json["agent_reason_suspected"] == "beans"


def test_get_anomaly_by_transaction_not_found_returns_404(client: FlaskClient):
    response = client.get("/anomalies/by-transaction/999999")

    assert response.status_code == 404
    assert isinstance(response.json, dict)
    assert response.json["code"] == 404
    assert response.json["name"] == "Not Found"


def test_delete_anomaly_that_doesnt_exist(client: FlaskClient):
    by_id = client.delete("/anomalies/999999")
    by_transaction = client.delete("/anomalies/by-transaction/999999")

    assert by_id.status_code == 204
    assert by_transaction.status_code == 204

def test_route_not_found(client: FlaskClient):
    response = client.get("/whatareyoudoinginmyswamp")

    assert response.status_code == 404
    assert isinstance(response.json, dict)
    assert response.json["code"] == 404
    assert response.json["name"] == "Not Found"


def test_get_all_anomalies_returns_empty_list_for_clean_db(client: FlaskClient):
    response = client.get("/anomalies/")
    assert response.status_code == 200
    assert isinstance(response.json, list)

    for anomaly in response.json:
        assert isinstance(anomaly, dict)
        client.delete(f"/anomalies/{anomaly['id']}")

    response = client.get("/anomalies/")
    assert response.status_code == 200
    assert isinstance(response.json, list)
    assert response.json == []


def test_get_anomaly_not_found_returns_404(client: FlaskClient):
    response = client.get("/anomalies/999999")

    assert response.status_code == 404
    assert isinstance(response.json, dict)
    assert response.json["code"] == 404
    assert response.json["name"] == "Not Found"


def test_create_anomaly_missing_required_fields_returns_400(client: FlaskClient):
    response = client.post("/anomalies/", json={"transaction_id": 123})

    assert response.status_code == 400
    assert isinstance(response.json, dict)
    assert response.json["code"] == 400
    assert response.json["description"] == "Missing required field agent_reason_suspected"


def test_create_anomaly_missing_transaction_id_returns_400(client: FlaskClient):
    response = client.post("/anomalies/", json={"agent_reason_suspected": "beans"})

    assert response.status_code == 400
    assert isinstance(response.json, dict)
    assert response.json["code"] == 400
    assert response.json["description"] == "Missing required field transaction_id"


def test_create_anomaly_rejects_invalid_boolean_flag(client: FlaskClient):
    response = client.post(
        "/anomalies/",
        json={
            "transaction_id": 123,
            "agent_reason_suspected": "beans",
            "is_confirmed_by_user": "maybe",
        },
    )

    assert response.status_code == 400
    assert isinstance(response.json, dict)
    assert response.json["code"] == 400
    assert "Field is_confirmed_by_user expected try_parse_bool" in response.json["description"]


def test_create_anomaly_rejects_invalid_json_body(client: FlaskClient):
    response = client.post(
        "/anomalies/",
        data="{bad-json",
        content_type="application/json",
    )

    assert response.status_code == 400
    assert isinstance(response.json, dict)
    assert response.json["code"] == 400


def test_patch_anomaly_updates_confirmation_flag(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=401, agent_reason_suspected="beans", is_confirmed_by_user=None)

    response = client.patch(f"/anomalies/{anomaly.id}", json={"is_confirmed_by_user": True})

    assert response.status_code == 200
    assert isinstance(response.json, dict)
    assert response.json["is_confirmed_by_user"] is True

    updated = client.get(f"/anomalies/{anomaly.id}")
    assert isinstance(updated.json, dict)
    assert updated.json["is_confirmed_by_user"] is True


def test_patch_anomaly_ignores_invalid_update_value(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=402, agent_reason_suspected="beans", is_confirmed_by_user=False)

    response = client.patch(f"/anomalies/{anomaly.id}", json={"is_confirmed_by_user": "not-a-bool"})

    assert response.status_code == 200
    assert isinstance(response.json, dict)
    assert response.json["is_confirmed_by_user"] is False

    updated = client.get(f"/anomalies/{anomaly.id}")
    assert isinstance(updated.json, dict)
    assert updated.json["is_confirmed_by_user"] is False


def test_patch_anomaly_with_null_value_is_ignored(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=403, agent_reason_suspected="beans", is_confirmed_by_user=False)

    response = client.patch(f"/anomalies/{anomaly.id}", json={"is_confirmed_by_user": None})

    assert response.status_code == 200
    assert isinstance(response.json, dict)
    assert response.json["is_confirmed_by_user"] is False


def test_patch_anomaly_with_empty_body_does_not_mutate_record(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=404, agent_reason_suspected="beans", is_confirmed_by_user=False)

    response = client.patch(f"/anomalies/{anomaly.id}", json={})

    assert response.status_code == 200
    assert isinstance(response.json, dict)
    assert response.json["is_confirmed_by_user"] is False
    assert response.json["agent_reason_suspected"] == "beans"


def test_patch_anomaly_with_unrelated_keys_does_not_mutate_state(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=405, agent_reason_suspected="beans", is_confirmed_by_user=False)

    response = client.patch(f"/anomalies/{anomaly.id}", json={"foo": "bar"})

    assert response.status_code == 200
    assert isinstance(response.json, dict)
    assert response.json["is_confirmed_by_user"] is False
    assert response.json["agent_reason_suspected"] == "beans"


def test_delete_by_transaction_repeated_calls_are_idempotent(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=406, agent_reason_suspected="beans", is_confirmed_by_user=False)

    first = client.delete(f"/anomalies/by-transaction/{anomaly.transaction_id}")
    second = client.delete(f"/anomalies/by-transaction/{anomaly.transaction_id}")

    assert first.status_code == 204
    assert second.status_code == 204


# Startup reconciliation of orphaned anomalies
def test_remove_orphaned_anomalies_deletes_only_orphans(client: FlaskClient):
    kept = create_anomaly(client, id=0, transaction_id=701, agent_reason_suspected="beans", is_confirmed_by_user=False)
    orphan = create_anomaly(client, id=0, transaction_id=702, agent_reason_suspected="beans", is_confirmed_by_user=False)

    removed = reconcile.remove_orphaned_anomalies(_all_transaction_ids(client) - {702})

    assert removed == 1
    assert client.get(f"/anomalies/{kept.id}").status_code == 200
    assert client.get(f"/anomalies/{orphan.id}").status_code == 404


def test_remove_orphaned_anomalies_empty_set_removes_all(client: FlaskClient):
    create_anomaly(client, id=0, transaction_id=703, agent_reason_suspected="beans", is_confirmed_by_user=False)

    removed = reconcile.remove_orphaned_anomalies(set())

    assert removed >= 1
    assert client.get("/anomalies/").json == []


def test_remove_orphaned_anomalies_keeps_all_when_all_present(client: FlaskClient):
    anomaly = create_anomaly(client, id=0, transaction_id=704, agent_reason_suspected="beans", is_confirmed_by_user=False)

    removed = reconcile.remove_orphaned_anomalies(_all_transaction_ids(client))

    assert removed == 0
    assert client.get(f"/anomalies/{anomaly.id}").status_code == 200


def test_fetch_transaction_ids_with_retry_returns_ids(monkeypatch):
    monkeypatch.setattr(reconcile.requests, "get", _fake_get([{"id": 1}, {"id": 2}]))

    ids = reconcile.fetch_transaction_ids_with_retry("http://transactions-db:6001", 1, 3, 0)

    assert ids == {1, 2}


def test_fetch_transaction_ids_with_retry_gives_up_when_unreachable(monkeypatch):
    calls = {"count": 0}

    def raising_get(*_args, **_kwargs):
        calls["count"] += 1
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(reconcile.requests, "get", raising_get)

    ids = reconcile.fetch_transaction_ids_with_retry("http://transactions-db:6001", 1, 3, 0)

    assert ids is None
    assert calls["count"] == 3


def test_seed_anomalies_only_references_seeded_transactions(client: FlaskClient):
    seeded_transaction_ids = {transaction[0] for transaction in TRANSACTIONS}
    expected_transaction_ids = {
        transaction_id
        for transaction_id, _, _, _ in SEED_ANOMALIES
        if transaction_id in seeded_transaction_ids
    }

    with app.app_context():
        db.session.query(Anomaly).delete()
        db.session.add(
            Anomaly(
                transaction_id=26,
                agent_reason_suspected="Existing anomaly must remain unchanged.",
                is_confirmed_by_user=None,
            )
        )
        db.session.commit()

    try:
        with app.app_context():
            seeded_count = seed_database_if_empty(seeded_transaction_ids)
        assert seeded_count == len(expected_transaction_ids) - 1
        response = client.get("/anomalies/")
        assert isinstance(anomalies := response.json, list)
        assert (
            {anomaly["transaction_id"] for anomaly in anomalies}
            == expected_transaction_ids
        )
        existing = next(
            anomaly for anomaly in anomalies if anomaly["transaction_id"] == 26
        )
        assert existing["agent_reason_suspected"] == (
            "Existing anomaly must remain unchanged."
        )
        assert existing["confidence"] is None
        assert len(anomalies) == 17
        statuses = [anomaly["is_confirmed_by_user"] for anomaly in anomalies]
        assert any(status is True for status in statuses)
        assert any(status is False for status in statuses)
        assert any(status is None for status in statuses)
        assert all(
            anomaly["transaction_id"] in seeded_transaction_ids
            for anomaly in anomalies
        )
        confidence_by_transaction = {
            anomaly["transaction_id"]: anomaly["confidence"]
            for anomaly in anomalies
        }
        for transaction_id, _, _, confidence in SEED_ANOMALIES:
            if transaction_id in expected_transaction_ids and transaction_id != 26:
                assert confidence_by_transaction[transaction_id] == confidence
        with app.app_context():
            assert seed_database_if_empty(seeded_transaction_ids) == 0
    finally:
        with app.app_context():
            db.session.query(Anomaly).delete()
            db.session.commit()


def test_seed_confidence_scores_are_bounded_and_reflect_review_accuracy():
    assert all(0 <= confidence <= 1 for _, _, _, confidence in SEED_ANOMALIES)

    confirmed_scores = [
        confidence
        for _, _, confirmed, confidence in SEED_ANOMALIES
        if confirmed is True
    ]
    dismissed_scores = [
        confidence
        for _, _, confirmed, confidence in SEED_ANOMALIES
        if confirmed is False
    ]
    unreviewed_scores = [
        confidence
        for _, _, confirmed, confidence in SEED_ANOMALIES
        if confirmed is None
    ]

    assert min(confirmed_scores) > max(unreviewed_scores)
    assert min(unreviewed_scores) > max(dismissed_scores)


# Pytest fixtures & helpers
@fixture
def client():
    setup_database(":memory:")

    with app.test_client() as client:
        yield client


def _fake_get(payload: List[dict]):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def _get(*_args, **_kwargs):
        return _Response()

    return _get


def _all_transaction_ids(client: FlaskClient) -> set:
    response = client.get("/anomalies/")
    assert isinstance(anomalies := response.json, list)
    return {anomaly["transaction_id"] for anomaly in anomalies}


def create_anomaly(client: FlaskClient, **kwargs: Any) -> dto.Anomaly:
    model = dto.Anomaly(**kwargs)
    response = client.post("/anomalies/", json=serialise(model))

    assert response.status_code == 201, response.text
    assert isinstance(data := response.json, dict)

    anomaly = deserialise_safe(dto.Anomaly, data)
    assert anomaly is not None

    return anomaly
