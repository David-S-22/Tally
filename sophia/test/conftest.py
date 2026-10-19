"""Shared fixtures and factories for the bills engine test suite."""
from datetime import date

import pytest

from sophia.backend.engine import Bill, Payment

TODAY = date(2026, 8, 20)


@pytest.fixture
def today():
    return TODAY


def make_bill(**overrides):
    """Build a Bill with sensible defaults, overridden per test."""
    defaults = dict(
        id=1,
        name="Test Bill",
        merchant="Test Merchant",
        amount_cents=1000,
        cadence="monthly",
        next_billing_date=date(2026, 9, 1),
        type="bill",
        payment_method="card",
        end_date=None,
        confirmed_at=None,
        created_at=None,
    )
    defaults.update(overrides)
    return Bill(**defaults)


def make_payment(bill_id, payment_date, amount_cents):
    """Build a Payment for the given bill."""
    return Payment(bill_id=bill_id, date=payment_date, amount_cents=amount_cents)


import importlib.util
import os
import sys
import threading
import time
from html import unescape

import requests
from werkzeug.serving import make_server

_DATABASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "database")
if _DATABASE_DIR not in sys.path:
    sys.path.insert(0, _DATABASE_DIR)


def _load_database_app():
    spec = importlib.util.spec_from_file_location("bills_database_app_for_ui_tests", os.path.join(_DATABASE_DIR, "app.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


database_app = _load_database_app()


def response_text(response):
    """Decode HTML entities in a Flask test response body, for verbatim-copy assertions."""
    return unescape(response.get_data(as_text=True))


@pytest.fixture(scope="module")
def live_db_base_url(tmp_path_factory):
    """Run the real sophia/database Flask app in a background thread on a temp seeded SQLite file."""
    db_path = str(tmp_path_factory.mktemp("ui-db") / "bills.db")
    connection = database_app.get_connection(db_path)
    database_app.load_schema(connection, database_app.SCHEMA_PATH)
    database_app.seed(connection, offset_days=0)
    connection.close()

    flask_app = database_app.create_app(db_path=db_path)
    server = make_server("127.0.0.1", 0, flask_app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            requests.get(f"{base_url}/health", timeout=1)
            break
        except requests.RequestException:
            time.sleep(0.05)

    yield base_url
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def live_client(live_db_base_url, monkeypatch):
    """A backend Flask test client pointed at the live_db_base_url database, with DEMO_TODAY fixed."""
    from sophia.backend import app as backend_app_module
    from sophia.backend import config

    monkeypatch.setattr(config, "BILLS_DB_API_URL", live_db_base_url)
    monkeypatch.setattr(config, "DEMO_TODAY", TODAY)
    app = backend_app_module.create_app()
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture(autouse=True)
def no_real_model_warm_up(monkeypatch):
    """Tests never reach a real Ollama: the draft-model warm-up is a no-op unless a test replaces it."""
    from sophia.backend.ai import ollama_client

    monkeypatch.setattr(ollama_client, "warm", lambda model: None)


CI_CORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci_core.txt")


def pytest_configure(config):
    config.addinivalue_line("markers", "extended: outside the CI core set (ci_core.txt); excluded in CI, run with -m extended before a release")


def _core_entries():
    """`file::test` lines from ci_core.txt, comments and blanks dropped. A bare
    function name keeps every parametrized case; `name[id]` keeps one."""
    with open(CI_CORE, encoding="utf-8") as f:
        return {line.split("#", 1)[0].strip() for line in f} - {""}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Mark every test not named in ci_core.txt `extended`, before -m deselects,
    so Sophia-CI's `-m "not extended"` runs the core set and nothing else."""
    core = _core_entries()
    matched, files = set(), set()
    for item in items:
        files.add(item.path.name)
        keys = (f"{item.path.name}::{item.name}", f"{item.path.name}::{getattr(item, 'originalname', item.name)}")
        hit = next((key for key in keys if key in core), None)
        if hit:
            matched.add(hit)
        else:
            item.add_marker(pytest.mark.extended)
    # A renamed or deleted core test must not silently leave CI. Only checked
    # when whole files were collected; a single `file::test` run skips it.
    if not any("::" in arg for arg in config.args):
        stale = sorted(entry for entry in core - matched if entry.split("::", 1)[0] in files)
        if stale:
            raise pytest.UsageError("ci_core.txt names tests that no longer exist: " + ", ".join(stale))
