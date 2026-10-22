from typing import List

from requests import get

from shared.backend import dto
from ..config import config
from ..helpers import deserialise_or_abort


def get_all_transactions() -> List[dto.Transaction]:
    resp = get(_url("/transactions"), timeout=config.SERVICE_TIMEOUT_SECONDS)
    resp.raise_for_status()

    return [deserialise_or_abort(dto.Transaction, item) for item in resp.json()]


def get_all_categories() -> List[dto.Category]:
    resp = get(_url("/categories"), timeout=config.SERVICE_TIMEOUT_SECONDS)
    resp.raise_for_status()

    return [deserialise_or_abort(dto.Category, item) for item in resp.json()]


def _url(path: str) -> str:
    return config.TRANSACTIONS_DB_URL + path
