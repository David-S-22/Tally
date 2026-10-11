from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def read(*parts: str) -> str:
    return REPOSITORY_ROOT.joinpath(*parts).read_text(encoding="utf-8")


def test_frontend_proxies_only_to_backend():
    nginx = read("janelle", "frontend", "nginx.conf")

    assert "location = /health" in nginx
    assert '{"ok":true,"container":"transactions-frontend"}' in nginx
    assert "location /transactions-backend/" in nginx
    assert "proxy_pass ${TRANSACTIONS_BACKEND_URL}/;" in nginx
    # Long enough for a bounded agent chat request to finish.
    assert "proxy_read_timeout 200s;" in nginx
    assert "transactions-db" not in nginx


def test_frontend_loads_transaction_rows_with_htmx():
    index = read("janelle", "frontend", "public", "index.html")
    page = read("janelle", "backend", "templates", "transactions_page.jinja")

    assert "htmx.org@2.0.10" in index
    assert 'hx-get="/transactions-backend/ui/transactions/page"' in index
    assert 'id="transactions-table"' in page
    assert 'hx-get="/transactions-backend/ui/transactions?page=1"' in page
    assert 'hx-trigger="load, transactionsChanged from:body"' in page
    assert 'hx-include="#transaction-filters, #transactions-page-size"' in page
    assert 'hx-get="/transactions-backend/ui/chat"' in page


def test_transaction_page_has_filters_toolbar_and_pagination_controls():
    page = read("janelle", "backend", "templates", "transactions_page.jinja")
    table = read("janelle", "backend", "templates", "transactions_table.jinja")

    assert 'id="transaction-filters"' in page
    assert 'hx-get="/transactions-backend/ui/transactions"' in page
    assert 'hx-target="#transactions-table"' in page
    assert 'name="search"' in page
    assert 'name="category_id"' in page
    assert 'name="date_range"' in page
    assert 'hx-get="/transactions-backend/ui/transactions/new"' in page
    assert 'hx-get="/transactions-backend/ui/categories"' in page
    assert 'hx-get="/transactions-backend/ui/categories/new"' not in page

    assert 'name="page_size"' in table
    assert 'id="previous-transactions-page"' in table
    assert 'id="next-transactions-page"' in table
    # Root reload, previous, next, and per-row delete keep filters and page size.
    assert table.count(
        'hx-include="#transaction-filters, #transactions-page-size"'
    ) == 4


def test_compose_configures_backend_image_and_mcp_rag_modes():
    dockerfile = read("janelle", "backend", "Dockerfile")
    compose = read("docker-compose.yml")
    backend = compose.split("\n  transactions-backend:", 1)[1].split(
        "\n  transactions-db:", 1
    )[0]

    assert "COPY janelle/backend ./backend" in dockerfile
    assert "dockerfile: janelle/backend/Dockerfile" in backend
    assert "MCP_ENABLED: ${MCP_ENABLED:-true}" in backend
    assert "RAG_ENABLED: ${RAG_ENABLED:-true}" in backend
    assert '- "host.docker.internal:host-gateway"' in backend


def test_ci_starts_backend_with_mcp_and_rag_disabled():
    workflow = read(".github", "workflows", "janelle-ci.yml")

    assert 'MCP_ENABLED: "false"' in workflow
    assert 'RAG_ENABLED: "false"' in workflow
    assert "--no-deps" in workflow
