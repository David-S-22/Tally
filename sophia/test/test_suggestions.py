"""The suggestions window: AI-proposed changes wait for approval, and every
outcome — applied, rejected, failed — is on the record for both the user and
the model's next turn."""
import json

import pytest

from sophia.backend.clients import bills_db as bills_db_module
from sophia.backend.engine.money import format_actual as _money
from sophia.backend.services import chat as chat_service
from sophia.backend.services.errors import ServiceError
from conftest import response_text as _text


def _propose(live_client, monkeypatch, response_dict, message="do it"):
    def fake_chat(model, messages, timeout=None):
        return {"message": {"content": json.dumps(response_dict)}}

    monkeypatch.setattr("sophia.backend.ai.guard.chat", fake_chat)
    response = live_client.post("/ui/chat", data={"message": message})
    assert response.status_code == 200
    return response


CREATE_DISNEY = {
    "op": "create", "entity": "bill", "id": None,
    "fields": {"name": "Disney Plus", "merchant": "Disney Plus", "amount": 15.0,
               "cadence": "monthly", "next_billing_date": "2026-09-05",
               "type": "subscription", "payment_method": "card"},
    "question": "none", "say": "I've suggested adding Disney Plus — approve it to save.",
}
DISNEY_MESSAGE = "Add Disney Plus, $15 a month, first charge 5 September, paid by card"
SPOTIFY_MESSAGE = "I cancelled Spotify from September — remove the future payments"


def test_chat_proposal_creates_a_pending_suggestion_not_a_bill(live_client, monkeypatch):
    before = len(bills_db_module.list_bills())
    _propose(live_client, monkeypatch, CREATE_DISNEY, message=DISNEY_MESSAGE)
    assert len(bills_db_module.list_bills()) == before
    pending = bills_db_module.list_suggestions(status="pending")
    assert pending
    latest = pending[-1]
    assert latest["op"] == "create" and latest["entity"] == "bill"
    payload = json.loads(latest["payload_json"])
    assert payload["amount_cents"] == 1500  # dollars canonicalised at proposal time


def test_approve_applies_and_refreshes_the_bills_table(live_client, monkeypatch):
    _propose(live_client, monkeypatch, CREATE_DISNEY, message=DISNEY_MESSAGE)
    suggestion_id = bills_db_module.list_suggestions(status="pending")[-1]["id"]

    response = live_client.post(f"/ui/suggestions/{suggestion_id}/approve")
    assert response.status_code == 200
    body = _text(response)
    # The response carries the refreshed panel plus the bills table out of
    # band — the table shows the new row without any manual refresh. (Coming
    # up and the Calendar are no longer on the page, so they don't ride along.)
    assert 'id="bills-table" hx-swap-oob="true"' in body
    assert 'id="timeline"' not in body and 'id="calendar-card"' not in body
    assert "Disney Plus" in body
    created = [b for b in bills_db_module.list_bills() if b["name"] == "Disney Plus"]
    assert len(created) == 1 and created[0]["source"] == "chat"
    assert bills_db_module.get_suggestion(suggestion_id)["status"] == "applied"
    # ...and the transcript the model reads says exactly what happened.
    outcome = bills_db_module.list_chat_messages()[-1]
    assert "approved and applied" in outcome["content"] and "Disney Plus" in outcome["content"]


def test_reject_changes_nothing_and_tells_the_model(live_client, monkeypatch):
    target = bills_db_module.list_bills()[0]
    _propose(live_client, monkeypatch, {
        "op": "delete", "entity": "bill", "id": target["id"], "fields": None,
        "question": "none", "say": "I've suggested deleting it — approve to remove."})
    suggestion_id = bills_db_module.list_suggestions(status="pending")[-1]["id"]

    response = live_client.post(f"/ui/suggestions/{suggestion_id}/reject")
    assert response.status_code == 200
    assert bills_db_module.get_bill(target["id"]) is not None
    assert bills_db_module.get_suggestion(suggestion_id)["status"] == "rejected"
    # The rejection note is still on the record so the model's next turn knows
    # the change was not made -- but no adapt turn rides the response. That
    # used to be here, and because the stubbed model "just re-proposes", it
    # documented the very loop it caused: the replacement proposal became a new
    # pending row. Asking for an alternative is the Suggest button now.
    recent = [m["content"] for m in bills_db_module.list_chat_messages()[-3:]]
    assert any("rejected by the user" in c and "NOT applied" in c for c in recent)
    assert 'hx-swap-oob="beforeend:#chat-history"' not in _text(response)


def test_approving_a_delete_for_a_missing_bill_fails_honestly(live_client, monkeypatch):
    _propose(live_client, monkeypatch, {
        "op": "delete", "entity": "bill", "id": 99999, "fields": None,
        "question": "none", "say": "I've suggested deleting it."})
    suggestion_id = bills_db_module.list_suggestions(status="pending")[-1]["id"]

    response = live_client.post(f"/ui/suggestions/{suggestion_id}/approve")
    assert response.status_code == 200
    body = _text(response)
    assert "bill not found" in json.loads(response.headers["HX-Trigger"])["toast"]
    row = bills_db_module.get_suggestion(suggestion_id)
    assert row["status"] == "failed" and "bill not found" in row["error"]
    outcome = bills_db_module.list_chat_messages()[-1]
    assert "FAILED" in outcome["content"] and "nothing was changed" in outcome["content"]
    # The failed card stays visible in the panel with its error.
    assert "Couldn't apply" in _text(live_client.get("/ui/suggestions"))


def test_a_suggestion_applies_at_most_once(live_client, monkeypatch):
    """The double-click/double-approve guard: the second approve must not
    create a second bill."""
    _propose(live_client, monkeypatch, CREATE_DISNEY, message=DISNEY_MESSAGE)
    suggestion_id = bills_db_module.list_suggestions(status="pending")[-1]["id"]

    first = live_client.post(f"/ui/suggestions/{suggestion_id}/approve")
    assert first.status_code == 200
    count_after_first = len([b for b in bills_db_module.list_bills() if b["name"] == "Disney Plus"])

    second = live_client.post(f"/ui/suggestions/{suggestion_id}/approve")
    assert second.status_code == 200  # renders current state, applies nothing
    assert "already applied" in json.loads(second.headers["HX-Trigger"])["toast"]
    assert len([b for b in bills_db_module.list_bills() if b["name"] == "Disney Plus"]) == count_after_first


def test_update_suggestion_panel_shows_a_before_after_diff(live_client, monkeypatch):
    # Read the current values rather than assume the seed: earlier tests in
    # this module share the database. Spotify has an amount to diff against
    # and no end date, so both row shapes render.
    spotify = next(b for b in bills_db_module.list_bills() if b["name"] == "Spotify")
    old_cents = spotify["amount_cents"]
    new_cents = old_cents + 200
    _propose(live_client, monkeypatch, {
        "op": "update", "entity": "bill", "id": spotify["id"],
        "fields": {"amount": new_cents / 100, "end_date": "2026-09-16"}, "question": "none",
        "say": "I've suggested the new Spotify price and end date. Approve to save."},
        message=f"Spotify is now ${new_cents / 100:.2f} and I cancelled it from 16 September")
    panel = _text(live_client.get("/ui/suggestions"))
    assert "Update Spotify" in panel
    # A field with a value shows it struck through before the new one...
    assert f'<del class="suggestion-old">{_money(old_cents)}</del> → {_money(new_cents)}' in panel
    # ...and a field that was empty shows only the new value.
    assert "<td>2026-09-16</td>" in panel


def test_chat_reply_card_names_the_change(live_client, monkeypatch):
    """No more blind Confirm buttons: the inline card carries the same titled,
    field-level detail the panel shows."""
    response = _propose(live_client, monkeypatch, CREATE_DISNEY, message=DISNEY_MESSAGE)
    body = _text(response)
    assert "Add bill: Disney Plus" in body
    assert "$15.00" in body
    assert ">Approve<" in body and ">Reject<" in body


def test_api_suggestion_endpoints_roundtrip(live_client, monkeypatch):
    # Flip Rent's current flag (seeded as excluded): proposing the value it
    # already has is a no-op and creates no suggestion.
    rent = next(b for b in bills_db_module.list_bills() if b["id"] == 1)
    flipped = 0 if rent["exclude_from_plan"] else 1
    _propose(live_client, monkeypatch, {
        "op": "update", "entity": "bill", "id": 1,
        "fields": {"exclude_from_plan": flipped}, "question": "none",
        "say": "I've suggested changing whether Rent is in the plan."},
        message="Include Rent in the plan" if flipped == 0 else "Leave Rent out of the plan")
    listed = live_client.get("/api/chat/suggestions", query_string={"status": "pending"}).get_json()
    suggestion_id = listed[-1]["id"]
    applied = live_client.post(f"/api/chat/suggestions/{suggestion_id}/approve")
    assert applied.status_code == 200
    assert live_client.post(f"/api/chat/suggestions/{suggestion_id}/reject").status_code == 409


def test_invalid_enum_value_becomes_a_question_not_a_proposal(live_client, monkeypatch):
    """Live-model case from the field: 'change rent from a bill to none' →
    type="none". That used to become an Approve button that could only fail;
    it must be a rephrase request, with nothing proposed."""
    before_pending = len(bills_db_module.list_suggestions(status="pending"))
    response = _propose(live_client, monkeypatch, {
        "op": "update", "entity": "bill", "id": 1,
        "fields": {"type": "none"}, "question": "none",
        "say": "I've suggested changing the rent from a bill to none."})
    body = _text(response)
    assert "couldn't turn that into a change" in body
    assert "type must be one of" in body
    assert len(bills_db_module.list_suggestions(status="pending")) == before_pending


def test_invalid_cadence_becomes_a_question_not_a_proposal(live_client, monkeypatch):
    before_pending = len(bills_db_module.list_suggestions(status="pending"))
    response = _propose(live_client, monkeypatch, {
        "op": "create", "entity": "bill", "id": None,
        "fields": {"name": "Daily Juice", "amount": 5, "cadence": "daily",
                   "next_billing_date": "2026-09-01", "type": "subscription"},
        "question": "none", "say": "Adding it."})
    assert "cadence must be one of" in _text(response)
    assert len(bills_db_module.list_suggestions(status="pending")) == before_pending


def test_proposal_renders_in_the_panel_only_not_the_chat(live_client, monkeypatch):
    """One surface for one decision: the reply carries a pointer, and the only
    decidable card in the response is the panel's (arriving out of band)."""
    response = _propose(live_client, monkeypatch, CREATE_DISNEY, message=DISNEY_MESSAGE)
    body = _text(response)
    assert "review it in Suggestions below" in body  # the pointer line
    # every decidable card in the response belongs to the panel: one per
    # visible (pending or failed) suggestion, none inline in the chat reply
    visible = len([s for s in bills_db_module.list_suggestions() if s["status"] in ("pending", "failed")])
    panel_part = body[body.index('id="suggestions-panel"'):]
    assert body.count('class="suggestion-card') == visible
    assert panel_part.count('class="suggestion-card') == visible
    assert 'id="suggestions-panel" hx-swap-oob="true"' in body


def test_reply_pointer_names_the_actual_target_when_the_reply_names_no_bill(live_client, monkeypatch):
    """Field case: asked about Netflix, the model emitted Prime Video's id.
    A reply that names Netflix is refused before it gets here; one that names
    no bill is not, so the pointer must name the real target from the
    suggestion row and the mismatch stays visible in the conversation."""
    prime = next(b for b in bills_db_module.list_bills() if b["name"] == "Prime Video")
    response = _propose(live_client, monkeypatch, {
        "op": "update", "entity": "bill", "id": prime["id"],
        "fields": {"end_date": "2026-09-02"}, "question": "none",
        "say": "I've suggested ending it after 2 Sep — approve it to save."},
        message="Cancel Prime Video from September")
    body = _text(response)
    assert "Proposed: <strong>Update Prime Video</strong>" in body
    assert "nothing is saved until you approve" in body


def _propose_then_adapt(monkeypatch, bill_id):
    """First model turn proposes; every later turn answers without an op."""
    calls = []

    def fake_chat(model, messages, timeout=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"message": {"content": json.dumps({
                "op": "update", "entity": "bill", "id": bill_id,
                "fields": {"end_date": "2026-09-16"}, "question": "none",
                "say": "I've suggested ending Spotify — approve to save."})}}
        return {"message": {"content": json.dumps({
            "op": None, "entity": None, "id": None, "fields": None,
            "question": "none", "say": "No problem — what would you like instead?"})}}

    monkeypatch.setattr("sophia.backend.ai.guard.chat", fake_chat)
    return calls


def test_suggest_asks_tally_for_another_option(live_client, monkeypatch):
    """Suggest is the "no, try again" action: it clears the current proposal and
    spends one model turn asking for a different one, appended to the chat out
    of band. This is the behaviour Reject used to have."""
    spotify = next(b for b in bills_db_module.list_bills() if b["name"] == "Spotify")
    calls = _propose_then_adapt(monkeypatch, spotify["id"])
    live_client.post("/ui/chat", data={"message": "end spotify from September"})
    suggestion_id = bills_db_module.list_suggestions(status="pending")[-1]["id"]

    response = live_client.post(f"/ui/suggestions/{suggestion_id}/suggest")
    assert response.status_code == 200
    body = _text(response)
    assert 'hx-swap-oob="beforeend:#chat-history"' in body
    assert "what would you like instead" in body
    assert bills_db_module.get_suggestion(suggestion_id)["status"] == "rejected"
    history_text = " ".join(m["content"] for m in calls[-1])
    assert "rejected by the user" in history_text


def test_reject_is_quiet_and_does_not_spawn_a_replacement(live_client, monkeypatch):
    """The loop that made this necessary: Reject ran an adapt turn, that turn
    proposed again, and _model_turn turned the proposal into a fresh PENDING
    row -- so rejecting produced another card to reject. Three rejects in a row
    left three rejected rows and one still pending. Reject must now be terminal:
    no model turn, no replacement."""
    spotify = next(b for b in bills_db_module.list_bills() if b["name"] == "Spotify")
    calls = _propose_then_adapt(monkeypatch, spotify["id"])
    live_client.post("/ui/chat", data={"message": "end spotify from September"})
    suggestion_id = bills_db_module.list_suggestions(status="pending")[-1]["id"]
    calls_before = len(calls)
    # Earlier tests in this module share the live DB and leave pending rows, so
    # measure the delta rather than asserting the table is empty.
    pending_before = {r["id"] for r in bills_db_module.list_suggestions(status="pending")}

    response = live_client.post(f"/ui/suggestions/{suggestion_id}/reject")
    assert response.status_code == 200
    assert bills_db_module.get_suggestion(suggestion_id)["status"] == "rejected"
    assert len(calls) == calls_before, "Reject must not spend a model turn"
    pending_after = {r["id"] for r in bills_db_module.list_suggestions(status="pending")}
    assert pending_after == pending_before - {suggestion_id}, "no replacement card"
    assert 'hx-swap-oob="beforeend:#chat-history"' not in _text(response)


def test_suggestion_card_offers_all_three_actions(live_client, monkeypatch):
    spotify = next(b for b in bills_db_module.list_bills() if b["name"] == "Spotify")
    _propose_then_adapt(monkeypatch, spotify["id"])
    live_client.post("/ui/chat", data={"message": "end spotify from September"})
    suggestion_id = bills_db_module.list_suggestions(status="pending")[-1]["id"]

    body = _text(live_client.get("/ui/suggestions"))
    for action in ("approve", "reject", "suggest"):
        assert f"/ui/suggestions/{suggestion_id}/{action}" in body, f"{action} button missing"


def test_dismissing_a_failed_suggestion_does_not_adapt(live_client, monkeypatch):
    """Adaptation is for rejections of live proposals; dismissing a failed
    card is just tidying up."""
    calls = []

    def fake_chat(model, messages, timeout=None):
        calls.append(messages)
        return {"message": {"content": json.dumps({
            "op": "delete", "entity": "bill", "id": 99999, "fields": None,
            "question": "none", "say": "I've suggested deleting it."})}}

    monkeypatch.setattr("sophia.backend.ai.guard.chat", fake_chat)
    live_client.post("/ui/chat", data={"message": "delete the ghost"})
    suggestion_id = bills_db_module.list_suggestions(status="pending")[-1]["id"]
    live_client.post(f"/ui/suggestions/{suggestion_id}/approve")  # fails -> failed card
    calls_before = len(calls)

    response = live_client.post(f"/ui/suggestions/{suggestion_id}/reject")  # Dismiss
    assert response.status_code == 200
    assert len(calls) == calls_before  # no model turn
    assert 'hx-swap-oob="beforeend:#chat-history"' not in _text(response)
