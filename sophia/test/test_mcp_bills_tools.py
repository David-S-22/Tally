"""The bills-native tools on the shared MCP server, called in-process through fastmcp's client against a faked bills-db and transactions-db; no network."""
import asyncio
import importlib.util
import logging
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

SERVER = Path(__file__).resolve().parents[2] / "ai-services" / "mcp-server" / "server.py"

BILLS = [
    {"id": 3, "name": "Spotify", "merchant": "Spotify AU", "amount_cents": 1399, "cadence": "monthly", "next_billing_date": "2026-09-27", "type": "subscription", "status": "paid"},
    {"id": 6, "name": "GymCo", "merchant": "GymCo", "amount_cents": 2499, "cadence": "monthly", "next_billing_date": "2026-10-15", "type": "subscription", "status": "due"},
    {"id": 7, "name": "Home internet", "merchant": "FibreLink", "amount_cents": 7900, "cadence": "monthly", "next_billing_date": "2026-09-26", "type": "bill", "status": "overdue"},
]
PAYMENTS = {3: [{"id": 11, "bill_id": 3, "date": "2026-07-27", "amount_cents": 1399}, {"id": 12, "bill_id": 3, "date": "2026-08-27", "amount_cents": 1399}, {"id": 13, "bill_id": 3, "date": "2026-09-27", "amount_cents": 1399}], 6: [], 7: []}
TRANSACTIONS = [
    {"id": 26, "date": "Thu, 20 Aug 2026 00:00:00 GMT", "merchant": "Spotify AU", "description": "Monthly subscription", "amount": 17.99, "category_id": 4},
    {"id": 22, "date": "Wed, 15 Jul 2026 00:00:00 GMT", "merchant": "Spotify AU", "description": "Spotify Premium subscription", "amount": 13.99, "category_id": 4},
]


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code, self._payload = status_code, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


@pytest.fixture
def server(monkeypatch):
    spec = importlib.util.spec_from_file_location("mcp_server_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        path = url.split("6005", 1)[1] if "6005" in url else url.split("6001", 1)[1]
        if path == "/bills":
            return FakeResponse(200, BILLS)
        if path == "/transactions":
            return FakeResponse(200, [t for t in TRANSACTIONS if t["merchant"] == params["merchant"]])
        parts = path.strip("/").split("/")
        bill = next((b for b in BILLS if str(b["id"]) == parts[1]), None)
        if bill is None:
            return FakeResponse(404, {"error": "not found"})
        return FakeResponse(200, PAYMENTS[bill["id"]] if len(parts) == 3 else bill)

    monkeypatch.setattr(module.requests, "get", get)
    module.calls = calls
    return module


def call(module, name, arguments):
    async def run():
        async with Client(module.mcp) as client:
            return (await client.call_tool(name, arguments)).data

    return asyncio.run(run())


def test_list_bills_returns_every_row_or_one_type_and_reads_bills_db_with_a_timeout(server):
    assert [b["id"] for b in call(server, "list_bills", {})] == [3, 6, 7]
    assert [b["id"] for b in call(server, "list_bills", {"bill_type": "bill"})] == [7]
    assert server.calls[0][0] == "http://localhost:6005/bills" and server.calls[0][2] == 10
    with pytest.raises(ToolError, match="bill_type"):
        call(server, "list_bills", {"bill_type": "loan"})


def test_get_bill_payments_returns_the_bill_with_its_payments_and_refuses_unknown_ids(server):
    found = call(server, "get_bill_payments", {"bill_id": 3})
    assert found["bill"]["merchant"] == "Spotify AU"
    assert [p["date"] for p in found["payments"]] == ["2026-07-27", "2026-08-27", "2026-09-27"]
    assert call(server, "get_bill_payments", {"bill_id": 6})["payments"] == []
    with pytest.raises(ToolError, match="bill not found"):
        call(server, "get_bill_payments", {"bill_id": 999})
    with pytest.raises(ToolError, match="bill not found"):
        call(server, "get_bill_payments", {"bill_id": 0})


def test_compare_puts_bank_charges_in_cents_beside_the_bill_within_the_window(server):
    result = call(server, "compare_bill_with_bank_charges", {"bill_id": 3, "start_date": "2026-07-03", "end_date": "2026-10-01"})
    assert result["bill"]["id"] == 3
    assert [(c["id"], c["date"], c["amount_cents"], c["differs_from_bill_cents"]) for c in result["charges"]] == [(26, "2026-08-20", 1799, 400), (22, "2026-07-15", 1399, 0)]
    assert [p["date"] for p in result["payments"]] == ["2026-07-27", "2026-08-27", "2026-09-27"]
    transactions_call = next(c for c in server.calls if c[0].endswith("/transactions"))
    assert transactions_call[1] == {"merchant": "Spotify AU", "date_from": "2026-07-03", "date_to": "2026-10-01"}


def test_compare_needs_a_sane_window_and_an_existing_bill(server):
    with pytest.raises(ToolError, match="366"):
        call(server, "compare_bill_with_bank_charges", {"bill_id": 3, "start_date": "2026-10-01", "end_date": "2026-07-03"})
    with pytest.raises(ToolError, match="366"):
        call(server, "compare_bill_with_bank_charges", {"bill_id": 3, "start_date": "2025-01-01", "end_date": "2026-10-01"})
    with pytest.raises(ToolError, match="bill not found"):
        call(server, "compare_bill_with_bank_charges", {"bill_id": 999, "start_date": "2026-07-03", "end_date": "2026-10-01"})
    assert not any(c[0].endswith("/transactions") for c in server.calls)


def test_refusals_are_tool_errors_the_server_logs_without_a_traceback(server):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    fastmcp_logger = logging.getLogger("fastmcp")
    fastmcp_logger.addHandler(handler)
    try:
        for name, arguments, message in (
            ("list_bills", {"bill_type": "loan"}, "bill_type"),
            ("get_bill_payments", {"bill_id": 999}, "bill not found"),
            ("compare_bill_with_bank_charges", {"bill_id": 3, "start_date": "2026-10-01", "end_date": "2026-07-03"}, "366"),
            ("compare_bill_with_bank_charges", {"bill_id": 3, "start_date": "not-a-date", "end_date": "2026-10-01"}, "YYYY-MM-DD"),
            ("compare_bill_with_bank_charges", {"bill_id": 999, "start_date": "2026-07-03", "end_date": "2026-10-01"}, "bill not found"),
        ):
            with pytest.raises(ToolError, match=message):
                call(server, name, arguments)
    finally:
        fastmcp_logger.removeHandler(handler)
    assert len(records) == 5 and not any(r.exc_info for r in records)


def test_the_three_tools_are_registered_read_only_beside_the_existing_four(server):
    async def names():
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
            bills = [t for t in tools if t.name in ("list_bills", "get_bill_payments", "compare_bill_with_bank_charges")]
            assert all(t.meta["fastmcp"]["tags"] == ["bills"] and t.description.startswith("For the Bills feature only.") for t in bills)
            return sorted(t.name for t in tools)

    assert asyncio.run(names()) == sorted(["search_transactions", "retrieve_context", "get_transactions_with_confirmed_anomalies",
                                           "get_transactions_with_rejected_anomalies", "list_bills", "get_bill_payments", "compare_bill_with_bank_charges"])
