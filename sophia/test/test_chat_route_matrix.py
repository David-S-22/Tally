"""Every phrasing in sophia/eval/chat_queries.yaml must take its route whatever the classifier returns: the matrix drives each query through the chat with a set of adversarial model outputs."""
import json
from pathlib import Path

import pytest
import yaml

from sophia.backend import config
from sophia.backend.clients import bills_db as bills_db_module
from sophia.backend.clients import mcp_server
from sophia.backend.routes.fragments import _suggestion_view
from sophia.backend.services import evidence as evidence_service

CORPUS = yaml.safe_load((Path(__file__).resolve().parents[1] / "eval" / "chat_queries.yaml").read_text(encoding="utf-8"))
GROUNDED = {"answer": "Grounded answer from the corpus.", "citations": [{"source": "bill-7-home-internet.md", "bill_id": 7, "title": "Home internet", "distance": 1.0}],
            "confidence": "medium", "insufficient": False, "retrieval": [], "fallback": False, "duration_ms": 1.0}
INSUFFICIENT = {"answer": "Tally couldn't find a bill that covers that.", "citations": [], "confidence": "none", "insufficient": True, "retrieval": [], "fallback": False, "duration_ms": 1.0}
COMPARE = {"bill": {"id": 3, "merchant": "Spotify AU", "amount_cents": 1399}, "payments": [],
           "charges": [{"id": 26, "date": "2026-08-20", "amount_cents": 1799, "description": "x", "differs_from_bill_cents": 400}]}

ADVERSARIAL = {
    "plain": {"op": None, "entity": None, "id": None, "fields": None, "question": "none", "say": "Let me check."},
    "tag_upcoming": {"op": None, "entity": None, "id": None, "fields": None, "question": "upcoming", "say": "Here."},
    "tag_total": {"op": None, "entity": None, "id": None, "fields": None, "question": "total", "say": "Here."},
    "wrong_update": {"op": "update", "entity": "bill", "id": 5, "fields": {"next_billing_date": "2026-10-14"}, "question": "none", "say": "Updated."},
    "invented_field": {"op": "update", "entity": "bill", "id": 6, "fields": {"disputed": True}, "question": "none", "say": "Marked."},
    "empty_update": {"op": "update", "entity": "bill", "id": 4, "fields": {}, "question": "none", "say": "I've suggested updating your Netflix bill - approve it to provide the amount."},
}

FAITHFUL = {
    "Update my Spotify to $15.99 a month": {"op": "update", "entity": "bill", "id": 5, "fields": {"amount": 15.99}, "question": "none", "say": "I've suggested changing Spotify to $15.99 a month."},
    "I cancelled Spotify from October - remove the future payments": {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-10-15"}, "question": "none", "say": "I've suggested ending Spotify after 15 Oct."},
    "Cancel Netflix from October": {"op": "update", "entity": "bill", "id": 5, "fields": {"end_date": "2026-10-02"}, "question": "none", "say": "I've suggested ending Netflix from 2 October"},
    "Can you cancel Netflix from 14 October?": {"op": "update", "entity": "bill", "id": 4, "fields": {"end_date": "2026-10-14"}, "question": "none", "say": "I've suggested ending Netflix after 14 Oct."},
    "Add a Disney+ subscription for 17.99 monthly starting 5th September": {"op": "create", "entity": "bill", "id": None, "fields": {"name": "Disney+", "merchant": "Disney+", "amount": 17.99, "cadence": "monthly", "next_billing_date": "2026-09-05", "type": "subscription"}, "question": "none", "say": "I've suggested adding Disney+."},
    "Add Disney Plus, $15 a month, first charge 5 September, paid by card": {"op": "create", "entity": "bill", "id": None, "fields": {"name": "Disney Plus", "merchant": "Disney Plus", "amount": 15.0, "cadence": "monthly", "next_billing_date": "2026-09-05", "type": "subscription", "payment_method": "card"}, "question": "none", "say": "I've suggested adding Disney Plus."},
    "Add a gym membership": {"op": "create", "entity": "bill", "id": None, "fields": {"name": "Gym membership", "amount": None, "cadence": None, "next_billing_date": None, "type": "subscription"}, "question": "none", "say": "Adding."},
    "Add Netflix": {"op": None, "entity": None, "id": None, "fields": None, "question": "none", "say": "Happy to add Netflix - how much is it, how often does it bill, and when is the next charge?"},
    "update my netflix bill (not sure what to put)": {"op": "update", "entity": "bill", "id": 4, "fields": {"amount": None, "cadence": None}, "question": "none", "say": "I've suggested updating your Netflix bill - approve it to provide the amount, billing frequency, and next date."},
    "Draft a note to dispute my GymCo charge": {"op": "create", "entity": "dispute", "id": None, "fields": {"bill_id": 6, "reason": "Charged after I cancelled"}, "question": "none", "say": "I've suggested opening a dispute for GymCo."},
}


def cases():
    for entry in CORPUS:
        message = entry["message"]
        variants = {"faithful": FAITHFUL[message]} if message in FAITHFUL else ADVERSARIAL
        for name, reply in variants.items():
            # The faithful variant per phrasing is the essential check and runs
            # everywhere. The adversarial variants (six model misreadings per
            # phrasing, 216 cases, ~17 s under coverage) are the extended sweep:
            # `python -m pytest sophia/test -m extended` before a release or
            # after touching chat routing; CI runs `-m "not extended"`.
            marks = [] if name == "faithful" else [pytest.mark.extended]
            yield pytest.param(entry, reply, id=f"{message[:42]} | {name}", marks=marks)


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(evidence_service, "ask", lambda question: INSUFFICIENT if "insurance" in question.lower() else GROUNDED)
    monkeypatch.setattr(mcp_server, "call_tool", lambda name, arguments: (COMPARE, 5.0))


@pytest.mark.parametrize("entry,model_reply", list(cases()))
def test_every_corpus_phrasing_keeps_its_route(live_client, harness, monkeypatch, entry, model_reply):
    monkeypatch.setattr("sophia.backend.ai.guard.chat", lambda model, messages, timeout=None, temperature=None: {"message": {"content": json.dumps(model_reply)}})
    payload = live_client.post("/api/chat", json={"message": entry["message"]}).get_json()
    expect = entry.get("expect") or {}
    assert payload["route"] == entry["route"], payload["reply"]
    for text in expect.get("contains", []):
        assert text in payload["reply"], payload["reply"]
    for text in expect.get("not", []):
        assert text not in payload["reply"], payload["reply"]
    if "insufficient" in expect:
        assert payload["grounded"]["insufficient"] is expect["insufficient"]
    if "title" in expect:
        row = bills_db_module.get_suggestion(payload["preview"]["suggestion_id"])
        assert _suggestion_view(row)["title"] == expect["title"]
