from typing import List, Optional

from flask import abort

from requests import get, patch, post

from shared.backend import dto
from ..config import config
from ..helpers import deserialise_or_abort, serialise


def get_all_anomalies() -> List[dto.Anomaly]:
    resp = get(_url("/"), timeout=config.SERVICE_TIMEOUT_SECONDS)
    resp.raise_for_status()

    return [deserialise_or_abort(dto.Anomaly, item) for item in resp.json()]

def get_anomaly_by_transaction_id(transaction_id: int) -> Optional[dto.Anomaly]:
    resp = get(
        _url(f"/by-transaction/{transaction_id}"),
        timeout=config.SERVICE_TIMEOUT_SECONDS,
    )

    if resp.status_code == 404:
        return None

    resp.raise_for_status()

    return deserialise_or_abort(dto.Anomaly, resp.json())

def get_anomaly(id: int) -> Optional[dto.Anomaly]:
    resp = get(_url(f"/{id}"), timeout=config.SERVICE_TIMEOUT_SECONDS)

    if resp.status_code == 404:
        return None

    resp.raise_for_status()
    return deserialise_or_abort(dto.Anomaly, resp.json())

def create_anomaly(anomaly: dto.Anomaly) -> dto.Anomaly:
    resp = post(
        _url("/"),
        json=serialise(anomaly),
        timeout=config.SERVICE_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    return deserialise_or_abort(dto.Anomaly, resp.json())

def set_confirmation(id: int, is_confirmed_by_user: bool) -> dto.Anomaly:
    resp = patch(
        _url(f"/{id}"),
        json={"is_confirmed_by_user": is_confirmed_by_user},
        timeout=config.SERVICE_TIMEOUT_SECONDS,
    )

    if resp.status_code == 404:
        abort(404)

    resp.raise_for_status()

    return deserialise_or_abort(dto.Anomaly, resp.json())

def _url(path: str) -> str:
    return config.ANOMALIES_DB_URL + path
