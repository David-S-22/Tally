import sqlite3
from datetime import datetime
from email.utils import parsedate_to_datetime
from unittest.mock import Mock

from pytest import fixture, mark, raises
from requests import RequestException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

import janelle.database.anomalies as database_anomalies
import janelle.database.app as database_app
import janelle.database.seed as database_seed
from janelle.backend.services.chat_service import expected_transaction_header
from janelle.database.app import setup_app
from janelle.database.models import CategoryCorrection, db


SEED_TRANSACTION_COUNT = 34
MISSING_CATEGORY_ID = 9999
TRANSACTION_FIELDS = {"id", "date", "merchant", "description", "amount", "category_id"}


@fixture
def database_client(tmp_path):
    database_path = tmp_path / "data" / "transactions.db"
    application = setup_app(str(database_path))
    application.config["TESTING"] = True

    with application.test_client() as client:
        yield client, database_path


@fixture(autouse=True)
def anomaly_cleanup_calls(monkeypatch):
    """Record anomaly cleanup calls and prevent real network requests."""
    calls = []
    monkeypatch.setattr(
        database_app,
        "delete_anomaly_by_transaction_id",
        calls.append,
    )
    return calls


@fixture(autouse=True)
def feedback_cleanup_calls(monkeypatch):
    """Record savings feedback cleanup calls and prevent real network requests."""
    calls = []

    def record(url, *_args, **kwargs):
        assert url.endswith("/feedbacks")
        calls.append(kwargs["params"]["category_id"])
        return Mock(status_code=204)

    monkeypatch.setattr(database_app.requests, "delete", record)
    return calls


def get_connection(database_path):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "casefold",
        1,
        lambda value: value.casefold() if isinstance(value, str) else value,
        deterministic=True,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def query(database_path, sql, *parameters):
    connection = get_connection(database_path)
    try:
        return [dict(row) for row in connection.execute(sql, parameters).fetchall()]
    finally:
        connection.close()


def correction_rows(database_path, transaction_id):
    return query(
        database_path,
        """
        SELECT previous_category_id, user_category_id
        FROM category_corrections
        WHERE transaction_id = ?
        """,
        transaction_id,
    )


def transaction_insert_sql(transaction_id, category_id):
    return f"""
        INSERT INTO transactions (
            id, date, merchant, description, amount, category_id,
            created_at, updated_at
        )
        VALUES (
            {transaction_id}, '2026-08-31', 'Raw insert', 'Raw insert', 1.00,
            {category_id}, '2026-08-31T00:00:00+00:00', '2026-08-31T00:00:00+00:00'
        )
    """


def response_datetime(value):
    return parsedate_to_datetime(value).replace(tzinfo=None)


def transaction_payload(**overrides):
    payload = {
        "date": "2026-08-31T14:30:00",
        "merchant": "Atomic Cafe",
        "description": "Team lunch",
        "amount": 24.5,
        "category_id": 80,
    }
    payload.update(overrides)
    return payload


def create_transaction(client, **overrides):
    response = client.post("/transactions", json=transaction_payload(**overrides))
    assert response.status_code == 201
    return response.get_json()


def versioned_header(client, transaction_id):
    versioned = client.get(
        f"/transactions/{transaction_id}?_include_version=true"
    ).get_json()
    return {"X-Expected-Transaction": expected_transaction_header(versioned)}


def test_index_and_health_identify_database(database_client):
    client, _database_path = database_client

    assert client.get("/").get_json() == {"container": "transactions-db"}
    assert client.get("/health").get_json() == {
        "ok": True,
        "container": "transactions-db",
    }


def test_setup_creates_schema_indexes_and_enforces_foreign_keys(database_client):
    _client, database_path = database_client
    connection = get_connection(database_path)
    try:
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index')"
        ).fetchall()
        tables = {row["name"] for row in objects if row["type"] == "table"}
        indexes = {row["name"] for row in objects if row["type"] == "index"}
        transaction_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(transactions)")
        }

        assert tables == {"categories", "transactions", "category_corrections"}
        assert {
            "idx_transactions_date",
            "idx_transactions_merchant_normalized",
            "idx_transactions_category_id",
            "idx_category_corrections_transaction_id",
            "idx_category_corrections_corrected_at",
            "uq_categories_name_normalized",
        } <= indexes
        assert transaction_columns == TRANSACTION_FIELDS | {"created_at", "updated_at"}
        with raises(sqlite3.IntegrityError):
            connection.execute(transaction_insert_sql(999999, MISSING_CATEGORY_ID))
    finally:
        connection.close()


def test_transactions_return_seeded_public_contract_in_newest_first_order(
    database_client,
):
    client, _database_path = database_client

    response = client.get("/transactions")

    assert response.status_code == 200
    transactions = response.get_json()
    assert len(transactions) == SEED_TRANSACTION_COUNT
    assert all(set(row) == TRANSACTION_FIELDS for row in transactions)
    assert all(isinstance(row["category_id"], int) for row in transactions)
    dates = [response_datetime(row["date"]) for row in transactions]
    assert dates == sorted(dates, reverse=True)
    assert transactions[0]["merchant"] == "Merivale"
    assert dates[0] == datetime(2026, 8, 30)


def test_startup_does_not_reseed_a_populated_database(database_client):
    client, database_path = database_client
    seeded_id = client.get("/transactions").get_json()[0]["id"]
    assert client.delete(f"/transactions/{seeded_id}").status_code == 204
    created = create_transaction(client, merchant="User row")

    setup_app(str(database_path))

    assert client.get(f"/transactions/{seeded_id}").status_code == 404
    assert client.get(
        f"/transactions/{created['id']}"
    ).get_json()["merchant"] == "User row"
    assert len(client.get("/transactions").get_json()) == SEED_TRANSACTION_COUNT


def test_startup_seed_rolls_back_when_any_seed_row_is_invalid(tmp_path, monkeypatch):
    database_path = tmp_path / "transactions.db"
    invalid_row = (
        999,
        "2026-09-01",
        "Invalid seed row",
        "Missing category",
        "10.00",
        MISSING_CATEGORY_ID,
        "2026-09-01T00:00:00+00:00",
        "2026-09-01T00:00:00+00:00",
    )
    monkeypatch.setattr(
        database_seed,
        "TRANSACTIONS",
        database_seed.TRANSACTIONS + (invalid_row,),
    )

    with raises(IntegrityError):
        setup_app(str(database_path))

    for table in ("categories", "transactions", "category_corrections"):
        assert query(database_path, f"SELECT COUNT(*) AS count FROM {table}") == [
            {"count": 0}
        ]


def test_transaction_crud_round_trip(database_client):
    client, _database_path = database_client

    created = create_transaction(client, date="2026-08-31T14:30:00+10:00")
    assert isinstance(created["id"], int)
    assert set(created) == TRANSACTION_FIELDS
    assert created["amount"] == 24.5
    assert created["category_id"] == 80
    # Offset-aware dates are normalised to naive UTC.
    assert response_datetime(created["date"]) == datetime(2026, 8, 31, 4, 30)

    assert client.get(f"/transactions/{created['id']}").get_json() == created

    update_response = client.patch(
        f"/transactions/{created['id']}",
        json={"merchant": "Atomic Coffee", "amount": 27.5},
    )
    assert update_response.status_code == 200
    assert update_response.get_json() == {
        **created,
        "merchant": "Atomic Coffee",
        "amount": 27.5,
    }

    assert client.delete(f"/transactions/{created['id']}").status_code == 204
    assert client.get(f"/transactions/{created['id']}").status_code == 404


def test_conditional_update_applies_matching_version_and_rejects_stale_one(
    database_client,
):
    client, _database_path = database_client
    created = create_transaction(client)
    header = versioned_header(client, created["id"])

    fresh = client.patch(
        f"/transactions/{created['id']}",
        json={"amount": 30},
        headers=header,
    )
    assert fresh.status_code == 200
    assert fresh.get_json()["amount"] == 30

    stale = client.patch(
        f"/transactions/{created['id']}",
        json={"amount": 40},
        headers=header,
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "stale_preview"
    assert client.get(f"/transactions/{created['id']}").get_json()["amount"] == 30


def test_conditional_delete_rejects_stale_version_then_deletes_and_cleans_up(
    database_client,
    anomaly_cleanup_calls,
):
    client, _database_path = database_client
    created = create_transaction(client)
    stale_header = versioned_header(client, created["id"])
    assert client.patch(
        f"/transactions/{created['id']}",
        json={"description": "Changed after preview"},
    ).status_code == 200

    stale = client.delete(f"/transactions/{created['id']}", headers=stale_header)
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "stale_preview"
    assert client.get(f"/transactions/{created['id']}").status_code == 200
    assert anomaly_cleanup_calls == []

    fresh = client.delete(
        f"/transactions/{created['id']}",
        headers=versioned_header(client, created["id"]),
    )
    assert fresh.status_code == 204
    assert anomaly_cleanup_calls == [created["id"]]


def test_delete_transaction_triggers_anomaly_cleanup_only_when_a_row_is_deleted(
    database_client,
    anomaly_cleanup_calls,
):
    client, _database_path = database_client
    created = create_transaction(client)

    assert client.delete(f"/transactions/{created['id']}").status_code == 204
    assert client.delete("/transactions/999999").status_code == 404
    assert anomaly_cleanup_calls == [created["id"]]


def test_anomaly_cleanup_targets_anomalies_database_and_swallows_request_errors(
    monkeypatch,
):
    delete = Mock(return_value=Mock(status_code=204))
    monkeypatch.setattr(database_anomalies.requests, "delete", delete)

    database_anomalies.delete_anomaly_by_transaction_id(42)

    delete.assert_called_once_with(
        f"{database_anomalies.ANOMALIES_DB_URL}/by-transaction/42",
        timeout=database_anomalies.ANOMALIES_TIMEOUT_SECONDS,
    )

    monkeypatch.setattr(
        database_anomalies.requests,
        "delete",
        Mock(side_effect=RequestException("boom")),
    )
    database_anomalies.delete_anomaly_by_transaction_id(42)


def test_delete_category_triggers_feedback_cleanup_and_swallows_request_errors(
    database_client,
    feedback_cleanup_calls,
    monkeypatch,
):
    client, _database_path = database_client
    first = client.post(
        "/categories", json={"name": "Cleanup one", "type": "want"}
    ).get_json()
    second = client.post(
        "/categories", json={"name": "Cleanup two", "type": "want"}
    ).get_json()

    assert client.delete(f"/categories/{first['id']}").status_code == 204
    assert client.delete(f"/categories/{MISSING_CATEGORY_ID}").status_code == 404
    assert feedback_cleanup_calls == [first["id"]]

    monkeypatch.setattr(
        database_app.requests,
        "delete",
        Mock(side_effect=RequestException("savings db offline")),
    )
    assert client.delete(f"/categories/{second['id']}").status_code == 204
    assert client.get(f"/categories/{second['id']}").status_code == 404


def test_transaction_create_records_ai_category_override_atomically(database_client):
    client, database_path = database_client

    created = create_transaction(client, category_id=81, suggested_category_id=80)

    assert created["category_id"] == 81
    assert "suggested_category_id" not in created
    assert correction_rows(database_path, created["id"]) == [
        {"previous_category_id": 80, "user_category_id": 81}
    ]


def test_transaction_create_does_not_record_accepted_ai_suggestion(database_client):
    client, database_path = database_client

    created = create_transaction(client, suggested_category_id=80)

    assert correction_rows(database_path, created["id"]) == []


def test_transaction_create_rejects_invalid_ai_suggestion_without_insert(
    database_client,
):
    client, _database_path = database_client

    response = client.post(
        "/transactions",
        json=transaction_payload(
            merchant="Invalid AI suggestion",
            suggested_category_id=MISSING_CATEGORY_ID,
        ),
    )

    assert response.status_code == 422
    assert response.get_json()["code"] == "category_not_found"
    assert client.get("/transactions?merchant=Invalid AI suggestion").get_json() == []


def test_failed_ai_correction_insert_rolls_back_transaction(
    database_client,
    monkeypatch,
):
    client, _database_path = database_client
    original_add = db.session.add

    def fail_correction_add(instance):
        if isinstance(instance, CategoryCorrection):
            raise SQLAlchemyError("correction insert failed")
        return original_add(instance)

    monkeypatch.setattr(db.session, "add", fail_correction_add)

    response = client.post(
        "/transactions",
        json=transaction_payload(
            merchant="Rolled back",
            category_id=81,
            suggested_category_id=80,
        ),
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == "database_unavailable"
    assert client.get("/transactions?merchant=Rolled back").get_json() == []


def test_unknown_fields_are_rejected_on_create_and_update(database_client):
    client, _database_path = database_client
    created = create_transaction(client)

    create_response = client.post(
        "/transactions",
        json=transaction_payload(amount_cents=999),
    )
    update_response = client.patch(
        f"/transactions/{created['id']}",
        json={"suggested_category_id": 81},
    )

    for response in (create_response, update_response):
        assert response.status_code == 422
        assert response.get_json()["code"] == "unsupported_fields"


@mark.parametrize(
    ("payload", "code"),
    [
        (transaction_payload(date="31-08-2026"), "invalid_date"),
        (transaction_payload(amount=12.345), "invalid_amount"),
        (transaction_payload(category_id=MISSING_CATEGORY_ID), "category_not_found"),
    ],
)
def test_transaction_create_rejects_invalid_values(database_client, payload, code):
    client, _database_path = database_client

    response = client.post("/transactions", json=payload)

    assert response.status_code == 422
    assert response.get_json()["code"] == code


def test_text_fields_reject_unpaired_unicode_surrogates(database_client):
    client, _database_path = database_client

    response = client.post("/transactions", json=transaction_payload(merchant="\ud800"))

    assert response.status_code == 422
    assert response.get_json()["code"] == "invalid_merchant"


def test_transaction_routes_return_json_error_for_malformed_or_nested_json(
    database_client,
):
    client, _database_path = database_client

    for body in ("{", ("[" * 5000) + "0" + ("]" * 5000)):
        response = client.post(
            "/transactions",
            data=body,
            content_type="application/json",
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_json"


def test_transaction_filters_work_alone_and_in_combination(database_client):
    client, _database_path = database_client
    create_transaction(client, merchant="Spotify AU Family", category_id=70)
    create_transaction(client, merchant="CAFÉ Central", category_id=70)

    spotify = client.get("/transactions?merchant=spotify au").get_json()
    assert len(spotify) == 3
    assert all(row["merchant"] == "Spotify AU" for row in spotify)

    unicode_merchant = client.get(
        "/transactions", query_string={"merchant": "café central"}
    ).get_json()
    assert [row["merchant"] for row in unicode_merchant] == ["CAFÉ Central"]

    search = client.get("/transactions?search_text=premium").get_json()
    assert len(search) == 2
    assert all("premium" in row["description"].lower() for row in search)

    date_range = client.get(
        "/transactions?date_from=2026-08-20&date_to=2026-08-26"
    ).get_json()
    assert date_range
    assert all(
        datetime(2026, 8, 20)
        <= response_datetime(row["date"])
        <= datetime(2026, 8, 26, 23, 59, 59, 999999)
        for row in date_range
    )

    since = client.get("/transactions?since=2026-08-25").get_json()
    assert since
    assert all(
        response_datetime(row["date"]) >= datetime(2026, 8, 25) for row in since
    )

    dining = client.get("/transactions?category_id=80").get_json()
    assert len(dining) == 5
    assert all(row["category_id"] == 80 for row in dining)

    amount_range = client.get(
        "/transactions?min_amount=20&max_amount=50"
    ).get_json()
    assert amount_range
    assert all(20 <= row["amount"] <= 50 for row in amount_range)

    combined = client.get(
        "/transactions",
        query_string={
            "merchant": "MERIVALE",
            "category_id": 80,
            "date_from": "2026-08-10",
            "date_to": "2026-08-31",
            "min_amount": "50",
            "max_amount": "90",
        },
    ).get_json()
    assert len(combined) == 1
    assert combined[0]["merchant"] == "Merivale"
    assert response_datetime(combined[0]["date"]) == datetime(2026, 8, 30)


def test_transaction_filter_by_category_name(database_client):
    client, _database_path = database_client

    dining = client.get("/transactions?category_name=dining").get_json()
    assert len(dining) == 5
    assert all(row["category_id"] == 80 for row in dining)

    merivale_dining = client.get(
        "/transactions",
        query_string={
            "category_name": "Dining",
            "merchant": "merivale",
            "date_from": "2026-08-10",
            "date_to": "2026-08-31",
        },
    ).get_json()
    assert len(merivale_dining) == 2
    assert all(row["merchant"] == "Merivale" for row in merivale_dining)

    assert client.get("/transactions?category_name=NonExistent").get_json() == []


def test_date_only_filter_includes_the_entire_day(database_client):
    client, _database_client = database_client
    created = create_transaction(
        client,
        date="2026-09-01T23:59:59.999999",
        merchant="Late purchase",
    )

    whole_day = client.get(
        "/transactions",
        query_string={
            "merchant": "Late purchase",
            "date_from": "2026-09-01",
            "date_to": "2026-09-01",
        },
    ).get_json()
    before_purchase = client.get(
        "/transactions",
        query_string={"merchant": "Late purchase", "date_to": "2026-09-01T23:59:59"},
    ).get_json()

    assert [row["id"] for row in whole_day] == [created["id"]]
    assert before_purchase == []


@mark.parametrize(
    "url",
    [
        "/transactions?date_from=not-a-date",
        "/transactions?date_from=2026-09-01&date_to=2026-08-01",
        "/transactions?category_id=abc",
    ],
)
def test_transaction_filters_reject_invalid_query_values(database_client, url):
    client, _database_path = database_client

    response = client.get(url)

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_query"


def test_category_crud_round_trip_and_case_insensitive_uniqueness(database_client):
    client, _database_path = database_client

    create_response = client.post(
        "/categories",
        json={"name": "Education", "type": "saving"},
    )
    assert create_response.status_code == 201
    created = create_response.get_json()
    assert client.get(f"/categories/{created['id']}").get_json() == created

    update_response = client.patch(
        f"/categories/{created['id']}",
        json={"name": "Learning", "type": "want"},
    )
    assert update_response.status_code == 200
    assert update_response.get_json() == {
        "id": created["id"],
        "name": "Learning",
        "type": "want",
    }

    conflict = client.post("/categories", json={"name": "learning", "type": "want"})
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "category_name_conflict"

    assert client.delete(f"/categories/{created['id']}").status_code == 204
    assert client.get(f"/categories/{created['id']}").status_code == 404

    assert client.post(
        "/categories", json={"name": "CAFÉ", "type": "want"}
    ).status_code == 201
    unicode_conflict = client.post(
        "/categories", json={"name": "café", "type": "want"}
    )
    assert unicode_conflict.status_code == 409
    assert unicode_conflict.get_json()["code"] == "category_name_conflict"


def test_categories_reject_invalid_type_and_unsafe_deletion(database_client):
    client, _database_path = database_client

    invalid_type = client.post("/categories", json={"name": "Invalid", "type": "other"})
    assert invalid_type.status_code == 422
    assert invalid_type.get_json()["code"] == "invalid_category_type"

    protected_category_id = 1
    protected_delete = client.delete(f"/categories/{protected_category_id}")
    assert protected_delete.status_code == 409
    assert protected_delete.get_json()["code"] == "protected_category"

    protected_patch = client.patch(
        f"/categories/{protected_category_id}",
        json={"name": "Other"},
    )
    assert protected_patch.status_code == 409
    assert protected_patch.get_json()["code"] == "protected_category"

    in_use = client.delete("/categories/80")
    assert in_use.status_code == 409
    assert in_use.get_json()["code"] == "category_in_use"


def test_database_constraint_conflicts_return_409(database_client):
    client, database_path = database_client
    category = client.post(
        "/categories",
        json={"name": "Concurrent category", "type": "want"},
    ).get_json()
    connection = get_connection(database_path)
    try:
        connection.execute(
            f"""
            CREATE TRIGGER reference_category_before_delete
            BEFORE DELETE ON categories
            WHEN OLD.id = {category["id"]}
            BEGIN
                {transaction_insert_sql(999999, "OLD.id")};
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    response = client.delete(f"/categories/{category['id']}")

    assert response.status_code == 409
    assert response.get_json()["code"] == "database_conflict"
    assert client.get(f"/categories/{category['id']}").status_code == 200


def test_category_correction_records_and_applies_atomically(database_client):
    client, database_path = database_client
    created = create_transaction(client)
    correction_url = f"/transactions/{created['id']}/category-correction"

    rejected = client.post(correction_url, json={"category_id": MISSING_CATEGORY_ID})
    assert rejected.status_code == 422
    assert rejected.get_json()["code"] == "category_not_found"

    response = client.post(correction_url, json={"category_id": 81})
    assert response.status_code == 201
    result = response.get_json()
    assert result["transaction"] == {**created, "category_id": 81}
    assert result["correction"]["previous_category_id"] == 80
    assert result["correction"]["previous_category_name"] == "Dining"
    assert result["correction"]["user_category_id"] == 81
    assert result["correction"]["user_category_name"] == "Groceries"
    assert client.get(f"/transactions/{created['id']}").get_json()["category_id"] == 81
    assert correction_rows(database_path, created["id"]) == [
        {"previous_category_id": 80, "user_category_id": 81}
    ]

    assert client.delete(f"/transactions/{created['id']}").status_code == 204
    assert correction_rows(database_path, created["id"]) == []


def test_category_corrections_can_be_filtered_by_merchant_and_limit(database_client):
    client, _database_path = database_client

    response = client.get("/category-corrections?merchant=merivale&limit=1")

    assert response.status_code == 200
    corrections = response.get_json()
    assert len(corrections) == 1
    assert corrections[0]["merchant"] == "Merivale"
    assert corrections[0]["previous_category_name"] == "Uncategorised"
    assert corrections[0]["user_category_name"] == "Dining"


@mark.parametrize(
    ("method", "url", "payload", "status"),
    [
        ("post", "/transactions", transaction_payload(category_id=2**63), 422),
        ("get", f"/transactions/{'9' * 5000}", None, 404),
    ],
)
def test_numeric_overflow_respects_error_contract(
    database_client,
    method,
    url,
    payload,
    status,
):
    client, _database_path = database_client

    response = getattr(client, method)(url, json=payload)

    assert response.status_code == status
    assert response.is_json


def test_transactions_report_unavailable_database(database_client):
    client, database_path = database_client
    with client.application.app_context():
        db.session.remove()
        db.engine.dispose()
    database_path.unlink()
    database_path.mkdir()

    response = client.get("/transactions")

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "database unavailable",
        "code": "database_unavailable",
    }


def test_bulk_transactions_skip_existing_and_validate(database_client):
    client, _database_path = database_client
    rows = [
        transaction_payload(date="2026-08-01", merchant="Bulk One", amount=10),
        transaction_payload(date="2026-08-02", merchant="Bulk Two", amount=20.5),
    ]

    first = client.post("/transactions/bulk", json={"transactions": rows})
    second = client.post("/transactions/bulk", json={"transactions": rows})

    assert first.status_code == 201
    assert first.get_json() == {"created": 2, "already_present": 0}
    assert second.get_json() == {"created": 0, "already_present": 2}
    merchants = [row["merchant"] for row in client.get("/transactions").get_json()]
    assert merchants.count("Bulk One") == 1

    unknown_category = client.post(
        "/transactions/bulk",
        json={"transactions": [transaction_payload(category_id=MISSING_CATEGORY_ID)]},
    )
    assert unknown_category.status_code == 422
    assert client.post("/transactions/bulk", json={"transactions": []}).status_code == 422
