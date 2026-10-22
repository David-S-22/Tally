"""HTTP client for the bills database API (:6005)."""
from datetime import date

import requests

from sophia.backend import config
from sophia.backend.engine import Bill, Payment
from sophia.backend.services.errors import ServiceError


def _url(path):
    return f"{config.BILLS_DB_API_URL}{path}"


def _headers():
    """Authorization for bills-db when it is configured to require one (cloud),
    and nothing at all when it is not (docker compose, tests). Read per call
    rather than at import so a test can set the token on config."""
    return {"Authorization": f"Bearer {config.BILLS_DB_TOKEN}"} if config.BILLS_DB_TOKEN else {}


def _raise_for_status(response):
    """Raise a ServiceError carrying the bills-db API's own message and status for a 4xx
    response, so a value it rejects comes back as a clean 400/404, never an unhandled 500.
    """
    if 400 <= response.status_code < 500:
        try:
            message = response.json().get("error", "")
        except ValueError:
            message = ""
        if not message or "<" in str(message):
            # A non-JSON body here is Flask's own HTML error page (e.g. a 404
            # for /bills/None when an id never parsed to an int). Rendering a
            # whole HTML document into a toast helps nobody.
            message = f"bills-db returned {response.status_code}"
        raise ServiceError(message, status=response.status_code)
    response.raise_for_status()
    return response


def _parse_date(value):
    return date.fromisoformat(value) if value else None


def row_to_bill(row):
    """Convert a bills-db JSON row into an engine Bill."""
    return Bill(
        id=row["id"],
        name=row["name"],
        merchant=row["merchant"],
        amount_cents=row["amount_cents"],
        cadence=row["cadence"],
        next_billing_date=_parse_date(row["next_billing_date"]),
        type=row["type"],
        payment_method=row.get("payment_method"),
        end_date=_parse_date(row.get("end_date")),
        confirmed_at=_parse_date(row.get("confirmed_at")),
        created_at=_parse_date(row.get("created_at")),
        source=row.get("source") or "manual",
        exclude_from_plan=bool(row.get("exclude_from_plan")),
    )


def row_to_payment(row):
    """Convert a bills-db JSON row into an engine Payment."""
    return Payment(bill_id=row["bill_id"], date=_parse_date(row["date"]), amount_cents=row["amount_cents"])


def list_bills():
    response = requests.get(_url("/bills"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def get_bill(bill_id):
    response = requests.get(_url(f"/bills/{bill_id}"), headers=_headers(), timeout=10)
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    return response.json()


def create_bill(payload):
    response = requests.post(_url("/bills"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def update_bill(bill_id, payload):
    response = requests.put(_url(f"/bills/{bill_id}"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def delete_bill(bill_id):
    response = requests.delete(_url(f"/bills/{bill_id}"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def list_bill_payments(bill_id):
    response = requests.get(_url(f"/bills/{bill_id}/payments"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def list_payments():
    response = requests.get(_url("/payments"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def create_payment(payload):
    response = requests.post(_url("/payments"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def get_payment(payment_id):
    response = requests.get(_url(f"/payments/{payment_id}"), headers=_headers(), timeout=10)
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    return response.json()


def update_payment(payment_id, payload):
    response = requests.put(_url(f"/payments/{payment_id}"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def delete_payment(payment_id):
    response = requests.delete(_url(f"/payments/{payment_id}"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def list_disputes():
    response = requests.get(_url("/disputes"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def create_dispute(payload):
    response = requests.post(_url("/disputes"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def get_dispute(dispute_id):
    response = requests.get(_url(f"/disputes/{dispute_id}"), headers=_headers(), timeout=10)
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    return response.json()


def update_dispute(dispute_id, payload):
    response = requests.put(_url(f"/disputes/{dispute_id}"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def delete_dispute(dispute_id):
    response = requests.delete(_url(f"/disputes/{dispute_id}"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def list_dispute_drafts(dispute_id):
    response = requests.get(_url(f"/disputes/{dispute_id}/drafts"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def create_dispute_draft(dispute_id, payload):
    response = requests.post(_url(f"/disputes/{dispute_id}/drafts"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def list_suggestions(status=None):
    params = {"status": status} if status else None
    response = requests.get(_url("/suggestions"), params=params, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def create_suggestion(payload):
    response = requests.post(_url("/suggestions"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def get_suggestion(suggestion_id):
    response = requests.get(_url(f"/suggestions/{suggestion_id}"), headers=_headers(), timeout=10)
    if response.status_code == 404:
        return None
    _raise_for_status(response)
    return response.json()


def update_suggestion(suggestion_id, payload):
    response = requests.put(_url(f"/suggestions/{suggestion_id}"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def list_chat_messages():
    response = requests.get(_url("/chat_messages"), headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def create_chat_message(payload):
    response = requests.post(_url("/chat_messages"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def update_chat_message(message_id, payload):
    response = requests.put(_url(f"/chat_messages/{message_id}"), json=payload, headers=_headers(), timeout=10)
    _raise_for_status(response)
    return response.json()


def health():
    response = requests.get(_url("/health"), headers=_headers(), timeout=5)
    _raise_for_status(response)
    return response.json()
