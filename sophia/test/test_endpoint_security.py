"""bills-db is deployed with external ingress, so these tests are the evidence that
its public routes need a bearer token, that /health stays reachable without one,
and that leaving the token unset keeps docker compose working as before.
"""
import importlib.util
import os
import sys

import pytest

_DATABASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database")
if _DATABASE_DIR not in sys.path:
    sys.path.insert(0, _DATABASE_DIR)


def _load_database_app():
    spec = importlib.util.spec_from_file_location("bills_database_app_for_security_tests", os.path.join(_DATABASE_DIR, "app.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


database_app = _load_database_app()

TOKEN = "t" * 32
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _client(tmp_path, token):
    db_path = str(tmp_path / "bills.db")
    connection = database_app.get_connection(db_path)
    database_app.load_schema(connection, database_app.SCHEMA_PATH)
    database_app.seed(connection, offset_days=0)
    connection.close()
    app = database_app.create_app(db_path=db_path, token=token)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def guarded(tmp_path):
    """bills-db as the cloud deploys it: a token is configured."""
    return _client(tmp_path, TOKEN)


@pytest.fixture
def unguarded(tmp_path):
    """bills-db as docker compose runs it: no token, reachable on the compose network only."""
    return _client(tmp_path, "")


# --- the gate ---------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/bills"),
    ("post", "/bills"),
    ("get", "/payments"),
    ("get", "/disputes"),
    ("get", "/suggestions"),
    ("get", "/chat_messages"),
    ("delete", "/chat_messages"),
])
def test_routes_require_a_token(guarded, method, path):
    assert getattr(guarded, method)(path).status_code == 401


def test_reads_and_writes_succeed_with_the_token(guarded):
    assert guarded.get("/bills", headers=AUTH).status_code == 200
    created = guarded.post("/bills", json={
        "name": "Internet", "merchant": "Telco", "amount_cents": 7900,
        "cadence": "monthly", "next_billing_date": "2026-11-01", "type": "bill",
    }, headers=AUTH)
    assert created.status_code == 201


def test_a_wrong_token_is_refused(guarded):
    response = guarded.get("/bills", headers={"Authorization": "Bearer " + "x" * 32})
    assert response.status_code == 401
    assert response.get_json()["error"] == "valid bearer token required"


def test_a_bare_token_without_the_bearer_scheme_is_refused(guarded):
    assert guarded.get("/bills", headers={"Authorization": TOKEN}).status_code == 401


def test_health_stays_open_so_probes_need_no_credential(guarded):
    response = guarded.get("/health")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_health_reports_whether_the_caller_authenticated(guarded):
    """Without this, a backend holding the wrong token reads /health as 200 and
    reports the database up while every route it actually uses returns 401."""
    anonymous = guarded.get("/health").get_json()
    assert anonymous == {"ok": True, "auth_required": True, "authenticated": False}

    authenticated = guarded.get("/health", headers=AUTH).get_json()
    assert authenticated == {"ok": True, "auth_required": True, "authenticated": True}


def test_health_reports_no_auth_required_when_no_token_is_set(unguarded):
    assert unguarded.get("/health").get_json() == {
        "ok": True, "auth_required": False, "authenticated": False,
    }


def test_backend_reports_an_unusable_token_instead_of_up(monkeypatch):
    """The bug this guards: /health being exempt from the gate made a backend with
    a wrong token look healthy."""
    from sophia.backend import app as backend_app_module
    from sophia.backend.clients import bills_db as bills_db_module
    from sophia.backend.clients import transactions as transactions_module

    monkeypatch.setattr(bills_db_module, "health", lambda: {
        "ok": True, "auth_required": True, "authenticated": False,
    })
    monkeypatch.setattr(transactions_module, "list_transactions", lambda merchant=None, since=None: ([], "stub"))
    app = backend_app_module.create_app()
    app.config["TESTING"] = True
    assert app.test_client().get("/health").get_json()["db_api"] == "unauthorized"


def test_backend_reports_up_when_the_token_is_accepted(monkeypatch):
    from sophia.backend import app as backend_app_module
    from sophia.backend.clients import bills_db as bills_db_module
    from sophia.backend.clients import transactions as transactions_module

    monkeypatch.setattr(bills_db_module, "health", lambda: {
        "ok": True, "auth_required": True, "authenticated": True,
    })
    monkeypatch.setattr(transactions_module, "list_transactions", lambda merchant=None, since=None: ([], "stub"))
    app = backend_app_module.create_app()
    app.config["TESTING"] = True
    assert app.test_client().get("/health").get_json()["db_api"] == "up"


def test_without_a_token_the_api_is_open_as_compose_expects(unguarded):
    assert unguarded.get("/bills").status_code == 200


def test_a_token_shorter_than_32_characters_is_refused_at_startup(tmp_path):
    with pytest.raises(ValueError, match="at least 32 characters"):
        _client(tmp_path, "short")


# --- response headers -------------------------------------------------------

def test_database_responses_carry_the_security_headers(guarded):
    response = guarded.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


def test_database_sends_no_cors_headers(guarded):
    """Only bills-backend calls this API, server to server. The wildcard policy
    flask_cors used to apply here had no caller that needed it."""
    response = guarded.get("/health", headers={"Origin": "https://attacker.example"})
    assert "Access-Control-Allow-Origin" not in response.headers


# --- backend: CORS and headers ----------------------------------------------

@pytest.fixture
def backend_client(monkeypatch):
    from sophia.backend import app as backend_app_module
    from sophia.backend import config

    monkeypatch.setattr(config, "FRONTEND_ORIGIN", "https://shared-frontend.example")
    app = backend_app_module.create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_backend_allows_only_the_configured_frontend_origin(backend_client):
    allowed = backend_client.get("/health", headers={"Origin": "https://shared-frontend.example"})
    assert allowed.headers.get("Access-Control-Allow-Origin") == "https://shared-frontend.example"

    foreign = backend_client.get("/health", headers={"Origin": "https://attacker.example"})
    assert "Access-Control-Allow-Origin" not in foreign.headers


def test_backend_responses_carry_the_security_headers(backend_client):
    response = backend_client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


# --- the client half of the contract ----------------------------------------

def test_client_sends_the_token_when_one_is_configured(monkeypatch):
    from sophia.backend import config
    from sophia.backend.clients import bills_db

    monkeypatch.setattr(config, "BILLS_DB_TOKEN", TOKEN)
    assert bills_db._headers() == AUTH


def test_client_sends_no_header_when_no_token_is_configured(monkeypatch):
    from sophia.backend import config
    from sophia.backend.clients import bills_db

    monkeypatch.setattr(config, "BILLS_DB_TOKEN", "")
    assert bills_db._headers() == {}


def test_every_client_call_passes_the_headers(monkeypatch):
    """A new function added to the client without headers=_headers() would reach
    the cloud bills-db as an unauthenticated request and 401 at runtime."""
    import inspect

    from sophia.backend.clients import bills_db

    source = inspect.getsource(bills_db)
    calls = [line for line in source.splitlines() if "requests." in line and "timeout=" in line]
    assert calls, "expected to find the client's HTTP calls"
    missing = [line.strip() for line in calls if "headers=_headers()" not in line]
    assert missing == []
