# Tests

## Backend

Backend tests exercise the Flask routes, asynchronous review queue, anomaly
agent, and integrations with the anomalies and transactions databases. The
`client` fixture creates a test client for the backend and resets the
in-memory anomalies database and review queue before each test.

The model server is mocked with `monkeypatch`. `intercept_ollama()` replaces
the cached Ollama client with a small fake client whose `responses.create()`
method returns a controlled model response. This allows tests to cover
suspicious findings, non-suspicious transactions, invalid JSON, and retry
behaviour without contacting a real model server.

### Using `responses` to integrate service tests

The backend normally sends HTTP requests to the anomalies and transactions
database services using the `requests` library. The `integrate_services`
fixture uses `responses.RequestsMock` to intercept those outgoing requests and
route them to Flask test clients for the actual database applications:

```python
with RequestsMock(assert_all_requests_are_fired=False) as rsps:
    rsps.add_callback(
        RequestsMock.GET,
        dburl,
        lambda request: intercept(dbapp, request),
    )
    rsps.add_callback(
        RequestsMock.GET,
        transactionsurl,
        lambda request: intercept(transactionsapp, request),
    )
    yield
```

Callbacks are registered for the HTTP methods used by each integration. The
`intercept()` helper copies the intercepted request path, method, headers, and
body into a Flask `test_client()` request, then returns its status code,
headers, and response body to `responses`. As a result, the backend exercises
its real HTTP clients and the database applications handle the requests,
without requiring running containers or external services.

This approach provides an integration-style test across the backend,
anomalies database, and transactions database while keeping the test suite
local and deterministic.
Shared Multi-Agent workflow submission and human-decision calls are replaced
with deterministic responses in focused tests. These cover bearer-token and
timeout configuration, workflow-ID persistence, the `anomalies` feature
label, decision propagation, and the rule that a failed shared decision does
not update local review status.

## Database

Database tests use the database Flask application's `test_client()` directly.
The `client` fixture calls `setup_database(":memory:")`, so each test operates
against an isolated in-memory SQLite database. The tests cover the health
route, anomaly creation, retrieval, updates, deletion, lookup by transaction,
validation errors, duplicate transaction handling, JSON error responses, and
seeded confidence scores and their relationship to reviewed finding accuracy.

These tests do not use `responses` because the database application is the
service being tested. Calling its Flask test client directly verifies the
routes, SQLAlchemy persistence, and SQLite behaviour without making an HTTP
request to another service.
