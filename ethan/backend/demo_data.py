from __future__ import annotations

from decimal import Decimal
from email.utils import parsedate_to_datetime
from datetime import timezone

import requests

from . import config, db_api
from .db_api import ServiceError

TRANSACTIONS = {
    "2026-08": [
        (1, "Harbourview Realty", "Rent payment", "1100.00", 30),
        (2, "FibreLink", "Internet plan", "79.00", 60),
        (3, "Telco One", "Mobile plan", "49.00", 61),
        (4, "Netflix", "Netflix.com subscription", "20.99", 33),
        (4, "Woolworths", "Weekly groceries", "98.00", 81),
        (8, "Coles", "Weekly groceries", "77.50", 81),
        (15, "Aldi", "Groceries", "64.50", 81),
        (22, "Woolworths", "Weekly groceries", "70.00", 81),
        (3, "Cafe Sorella", "Brunch", "32.00", 80),
        (14, "Thai Orchid", "Dinner", "58.00", 80),
        (24, "Mr Wong", "Dinner", "50.00", 80),
        (5, "Transport card", "Opal top-up", "38.50", 70),
        (18, "Shell", "Fuel", "51.50", 70),
        (7, "AGL Energy", "Electricity", "130.00", 62),
        (19, "Sydney Water", "Water usage", "80.00", 62),
        (11, "Spotify", "Spotify Premium", "12.99", 32),
        (12, "Anytime Fitness Ultimo", "Direct debit membership fee", "17.50", 31),
    ],
    "2026-09": [
        (1, "Harbourview Realty", "Rent payment", "1100.00", 30),
        (2, "FibreLink", "Internet plan", "79.00", 60),
        (3, "Telco One", "Mobile plan", "49.00", 61),
        (4, "Netflix", "Netflix.com subscription", "20.99", 33),
        (3, "Woolworths", "Weekly groceries", "120.40", 81),
        (9, "Coles", "Weekly groceries", "98.60", 81),
        (14, "Aldi", "Groceries", "87.00", 81),
        (20, "Woolworths", "Weekly groceries", "112.20", 81),
        (27, "Harris Farm", "Fresh produce", "46.80", 81),
        (2, "Cafe Sorella", "Brunch", "45.00", 80),
        (6, "Thai Orchid", "Dinner", "68.50", 80),
        (11, "Gelato Messina", "Dessert", "22.00", 80),
        (19, "Bills Surry Hills", "Birthday dinner", "59.50", 80),
        (23, "Uber Eats", "Takeaway", "39.00", 80),
        (29, "Mr Wong", "Dinner", "41.00", 80),
        (4, "Transport card", "Opal top-up", "38.50", 70),
        (10, "Shell", "Fuel", "65.00", 70),
        (17, "Transport card", "Opal top-up", "38.50", 70),
        (26, "Uber", "Weekend trip rides", "50.00", 70),
        (8, "AGL Energy", "Electricity", "140.00", 62),
        (13, "Sydney Water", "Water usage", "90.00", 62),
        (11, "Spotify", "Spotify Premium", "12.99", 32),
        (12, "Anytime Fitness Ultimo", "Direct debit membership fee", "17.50", 31),
    ],
    "2026-10": [
        (1, "Harbourview Realty", "Rent payment", "1100.00", 30),
        (2, "FibreLink", "Internet plan", "79.00", 60),
        (3, "Telco One", "Mobile plan", "49.00", 61),
        (4, "Netflix", "Netflix.com subscription", "20.99", 33),
        (3, "Woolworths", "Weekly groceries", "142.30", 81),
        (7, "Coles", "Weekly groceries", "96.85", 81),
        (10, "Aldi", "Groceries", "74.20", 81),
        (14, "Woolworths", "Weekly groceries", "131.45", 81),
        (17, "Coles", "Groceries", "58.90", 81),
        (19, "Harris Farm", "Fresh produce", "78.30", 81),
        (2, "Cafe Sorella", "Brunch", "18.50", 80),
        (4, "Thai Orchid", "Dinner", "62.00", 80),
        (9, "Mr Wong", "Dinner", "48.50", 80),
        (12, "Gelato Messina", "Dessert", "14.00", 80),
        (15, "Uber Eats", "Takeaway", "36.50", 80),
        (18, "Bills Surry Hills", "Brunch", "31.00", 80),
        (21, "Subway", "Lunch", "23.50", 80),
        (3, "Transport card", "Opal top-up", "38.50", 70),
        (8, "Shell", "Fuel", "54.00", 70),
        (14, "Transport card", "Opal top-up", "38.50", 70),
        (6, "AGL Energy", "Electricity", "168.00", 62),
        (13, "Sydney Water", "Water usage", "96.00", 62),
        (11, "Spotify", "Spotify Premium", "12.99", 32),
        (12, "Anytime Fitness Ultimo", "Direct debit membership fee", "17.50", 31),
        (20, "ATM", "Cash withdrawal", "100.00", 1),
    ],
}


def _transactions_url() -> str:
    return (config.TRANSACTIONS_DB_URL or config.TRANSACTIONS_API_URL) + "/transactions"


def _request(method: str, **kwargs):
    try:
        response = requests.request(
            method,
            _transactions_url(),
            timeout=config.TRANSACTIONS_TIMEOUT_SECONDS,
            **kwargs,
        )
        response.raise_for_status()
        return response
    except requests.RequestException as error:
        raise ServiceError("transactions API is unavailable", 503, "transactions_unavailable") from error


def _iso_date(value: str) -> str:
    if len(value) >= 10 and value[4] == "-":
        return value[:10]
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return value


def _key(date: str, merchant: str, amount: object) -> tuple[str, str, Decimal]:
    return _iso_date(date), merchant, Decimal(str(amount))


def seed_transactions() -> dict[str, int]:
    existing = {
        _key(str(row.get("date", "")), str(row.get("merchant", "")), row.get("amount", 0))
        for row in _request("GET").json()
    }
    created = 0
    skipped = 0
    for month in sorted(TRANSACTIONS):
        for day, merchant, description, amount, category_id in TRANSACTIONS[month]:
            date = f"{month}-{day:02d}"
            key = _key(date, merchant, amount)
            if key in existing:
                skipped += 1
                continue
            _request(
                "POST",
                json={
                    "date": date,
                    "merchant": merchant,
                    "description": description,
                    "amount": float(amount),
                    "category_id": category_id,
                },
            )
            existing.add(key)
            created += 1
    return {"created": created, "already_present": skipped}


def seed_demo_data(skip_budgets_reset: bool, skip_transactions: bool) -> dict:
    result: dict = {"budgets_reset": False, "transactions": None}
    if not skip_budgets_reset:
        db_api.reset_database()
        result["budgets_reset"] = True
    if not skip_transactions:
        result["transactions"] = seed_transactions()
    return result
