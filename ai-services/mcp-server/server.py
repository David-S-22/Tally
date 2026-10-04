import logging
import os
import sys
from datetime import datetime

import requests
from dateutil import parser
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger("mcp-server")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.addHandler(_handler)


class RequestLoggingMiddleware(Middleware):
    """Log every incoming MCP request as it arrives."""

    async def on_message(self, context: MiddlewareContext, call_next):
        logger.info("Request received: %s", context.method)
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = context.message
        logger.info(
            "Tool call: %s args=%s",
            getattr(message, "name", "<unknown>"),
            getattr(message, "arguments", {}),
        )
        return await call_next(context)


mcp = FastMCP("Transactions")
mcp.add_middleware(RequestLoggingMiddleware())

TRANSACTIONS_DB_URL = os.getenv("TRANSACTIONS_DB_URL", "http://localhost:6001")
RAG_SERVER_URL = os.getenv("RAG_SERVER_URL", "http://localhost:5003")
ANOMALIES_DB_URL = os.getenv("ANOMALIES_DB_URL", "http://localhost:6004/anomalies")


@mcp.resource("docs://readme", mime_type="text/markdown")
def readme() -> str:
    return "Fetch transactions using requirements with the 'search_transactions' tool"

@mcp.tool()
def search_transactions(
    start_date: str | None = None,
    end_date: str | None = None,
    category_name: str | None = None,
    merchant: str | None = None,
    search_text: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> list[dict]:
    """Search and filter transactions by date range, category, merchant, text and amount.

    Args:
        start_date: Earliest transaction date (inclusive). If omitted, searches from earliest transaction.
        end_date: Latest transaction date (inclusive). If omitted, searches up to current date.
        category_name: Name of category to filter by (case-insensitive). If omitted, returns all categories.
        merchant: Merchant name to filter by (case-insensitive exact match). If omitted, returns all merchants.
        search_text: Free-text search across merchant and description (case-insensitive substring). If omitted, no text filter.
        min_amount: Minimum transaction amount (inclusive). If omitted, no lower bound.
        max_amount: Maximum transaction amount (inclusive). If omitted, no upper bound.
    """
    start_dt = parser.parse(start_date) if start_date else None
    end_dt = parser.parse(end_date) if end_date else None

    if start_dt and not end_dt:
        end_dt = datetime.now()

    if start_dt and end_dt and start_dt > end_dt:
        raise ValueError("start_date must not be after end_date")

    min_value = _parse_amount(min_amount, "min_amount")
    max_value = _parse_amount(max_amount, "max_amount")

    if min_value is not None and max_value is not None and min_value > max_value:
        raise ValueError("min_amount must not exceed max_amount")

    params = {}
    if start_dt:
        params["date_from"] = start_dt.date().isoformat()
    if end_dt:
        params["date_to"] = end_dt.date().isoformat()

    if category_name:
        params["category_name"] = category_name

    for name, value in (("merchant", merchant), ("search_text", search_text)):
        text = str(value).strip() if value is not None else ""
        if text:
            params[name] = text

    if min_value is not None:
        params["min_amount"] = min_value
    if max_value is not None:
        params["max_amount"] = max_value

    resp = requests.get(f"{TRANSACTIONS_DB_URL.rstrip('/')}/transactions", params=params)
    resp.raise_for_status()
    return resp.json()


def _parse_amount(value, name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be numeric") from None


@mcp.tool()
def retrieve_context(feature: str, question: str, k: int = 3) -> dict:
    """Return the documents in a feature's corpus that are closest to the question.

    Args:
        feature: Which feature's corpus to search, for example 'bills' or 'savings'.
        question: The question to find supporting documents for.
        k: How many documents to return.
    """
    resp = requests.post(
        f"{RAG_SERVER_URL.rstrip('/')}/retrieve",
        json={"feature": feature, "question": question, "k": k},
    )
    resp.raise_for_status()
    return resp.json()

def _get_transactions_with_anomaly_status(is_confirmed: bool) -> list[dict]:
    anomalies_response = requests.get(ANOMALIES_DB_URL.rstrip("/"))
    anomalies_response.raise_for_status()
    anomalies = [
        anomaly for anomaly in anomalies_response.json()
        if anomaly.get("is_confirmed_by_user") is is_confirmed
    ]

    transactions_response = requests.get(
        f"{TRANSACTIONS_DB_URL.rstrip('/')}/transactions")
    transactions_response.raise_for_status()
    transactions_by_id = {
        transaction["id"]: transaction
        for transaction in transactions_response.json()
    }

    return [
        {
            "transaction": transactions_by_id[anomaly["transaction_id"]],
            "anomaly": anomaly,
        }
        for anomaly in anomalies
        if anomaly["transaction_id"] in transactions_by_id
    ]

@mcp.tool()
def get_transactions_with_confirmed_anomalies() -> list[dict]:
    """Return transactions whose anomalies the user confirmed as suspicious."""
    return _get_transactions_with_anomaly_status(True)

@mcp.tool()
def get_transactions_with_rejected_anomalies() -> list[dict]:
    """Return transactions whose anomalies the user rejected as not suspicious."""
    return _get_transactions_with_anomaly_status(False)


BILLS_DB_URL = os.getenv("BILLS_DB_URL", "http://localhost:6005")


def _bills_db(path: str):
    """GET one bills-db path as JSON; 404 becomes ToolError("bill not found"), a refusal the server logs in one line."""
    resp = requests.get(f"{BILLS_DB_URL.rstrip('/')}{path}", timeout=10)
    if resp.status_code == 404:
        raise ToolError("bill not found")
    resp.raise_for_status()
    return resp.json()


def _charge(row: dict, bill_amount_cents: int) -> dict:
    """One transactions-db row as a bank charge in integer cents, with its difference from the bill."""
    amount_cents = round(float(row["amount"]) * 100)
    return {
        "id": row["id"],
        "date": parser.parse(row["date"]).date().isoformat(),
        "amount_cents": amount_cents,
        "description": row["description"],
        "differs_from_bill_cents": amount_cents - bill_amount_cents,
    }


@mcp.tool(tags={"bills"})
def list_bills(bill_type: str | None = None) -> list[dict]:
    """For the Bills feature only. List the user's bills from the Bills database (read-only): id, name, merchant, amount_cents, cadence, next_billing_date, status, type, payment_method, end_date.

    Args:
        bill_type: 'bill' or 'subscription' to return only that type; omit for every bill.
    """
    if bill_type not in (None, "bill", "subscription"):
        raise ToolError("bill_type must be 'bill' or 'subscription'")
    return [bill for bill in _bills_db("/bills") if bill_type in (None, bill["type"])]


@mcp.tool(tags={"bills"})
def get_bill_payments(bill_id: int) -> dict:
    """For the Bills feature only. Return {"bill", "payments": [{id, bill_id, date, amount_cents}]} for one bill, payments oldest first (read-only).

    Args:
        bill_id: The bill's id from list_bills.
    """
    return {"bill": _bills_db(f"/bills/{bill_id}"), "payments": _bills_db(f"/bills/{bill_id}/payments")}


@mcp.tool(tags={"bills"})
def compare_bill_with_bank_charges(bill_id: int, start_date: str, end_date: str) -> dict:
    """For the Bills feature only. Return {"bill", "payments", "charges"} for one bill between two dates: the payments recorded in Bills and the bank charges from the bill's merchant, each charge in cents with differs_from_bill_cents. Nothing is matched, summed or written; the caller supplies the dates.

    Args:
        bill_id: The bill's id from list_bills.
        start_date: First day to include, YYYY-MM-DD.
        end_date: Last day to include, YYYY-MM-DD, on or after start_date and at most 366 days later.
    """
    try:
        start, end = parser.isoparse(start_date).date().isoformat(), parser.isoparse(end_date).date().isoformat()
    except ValueError:
        raise ToolError("start_date and end_date must be YYYY-MM-DD") from None
    if not 0 <= (parser.isoparse(end) - parser.isoparse(start)).days <= 366:
        raise ToolError("end_date must be on or after start_date and at most 366 days later")
    found = get_bill_payments(bill_id)
    bill = found["bill"]
    resp = requests.get(f"{TRANSACTIONS_DB_URL.rstrip('/')}/transactions", params={"merchant": bill["merchant"], "date_from": start, "date_to": end}, timeout=10)
    resp.raise_for_status()
    return {
        "bill": bill,
        "payments": [p for p in found["payments"] if start <= p["date"] <= end],
        "charges": [_charge(row, bill["amount_cents"]) for row in resp.json()],
    }


if __name__ == "__main__":
    mcp.run(transport="http", port=8000)
