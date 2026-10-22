# Anomaly Detection Service

An AI-assisted transaction anomaly-detection feature running through
containerised frontend, backend, database, and model components.

## Documentation

- [Backend documentation](backend/README.md) — backend overview, agentic workflow,
  services, and routes.
- [Frontend documentation](frontend/README.md) — frontend overview, user interface,
  backend communication, and nginx configuration.
- [Database documentation](database/README.md) — database overview, routes, and
  conceptual, logical, and physical ERDs.
- [Test documentation](test/README.md) — backend and database testing, including
  service integration with `responses`.

## Directory overview

### `frontend/`

Contains the user-facing anomalies interface. The frontend runs in an nginx
container and serves the static page used to display anomalies, transaction
information, and review controls. HTMX requests refresh anomaly rows and submit
review actions without full page reloads. Nginx proxies
`/anomalies-backend/` requests to the backend container, while environment
variables provide the backend URL. The directory contains the HTML page, nginx
configuration, and Dockerfile required to build and run the frontend
container. Detailed frontend documentation is available in
[`frontend/README.md`](frontend/README.md).

### `backend/`

Contains the main Flask application responsible for coordinating anomaly
detection. It provides routes for listing anomalies, submitting transactions,
receiving review results, and confirming or dismissing findings. HTTP clients
connect the backend to the anomalies and transactions databases. Reviews run
asynchronously so model requests do not block the original request. The
directory includes the agent logic, review queue, Ollama client, templates,
helpers, requirements, and Dockerfile. The agent uses transaction details and
previous user decisions to produce structured findings. Detailed workflow,
services, and routes are documented in
[`backend/README.md`](backend/README.md).

### `database/`

Contains the anomalies persistence service. It is a Flask application backed by
SQLAlchemy and SQLite, exposing a REST API for anomaly CRUD operations. Each
record stores a transaction ID, the agent's reason, an optional confidence
score from `0` to `1`, the RAG source filenames used to ground the finding, and
the user's confirmation status. Transactions and
anomalies are stored in separate databases, so the relationship is represented
by ID rather than a database foreign key. A unique constraint allows at most
one anomaly per transaction. The directory includes the model, routes, parsing
helpers, requirements, and Dockerfile. Database design, routes, and ERDs are
documented in [`database/README.md`](database/README.md).

### `test/`

Contains the automated pytest suite for the backend and database applications.
Database tests use Flask's test client and an in-memory SQLite database to
check persistence, validation, routes, and anomaly operations. Backend tests
exercise the API, asynchronous review queue, agent behaviour, and service
integration. Outbound HTTP requests are intercepted with `responses` and
redirected to Flask test clients, avoiding running containers. Model responses
are controlled with monkeypatching to test valid, invalid, suspicious, and
retry scenarios deterministically. Shared pytest setup and test dependencies
are also stored here. Additional testing details are available in
[`test/README.md`](test/README.md).
