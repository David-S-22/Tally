"""Ask Tally answers plain questions from the bills corpus (RAG through MCP); proposals and code-computed questions are untouched, and a missing mode or server falls back to the model's own reply."""
import json

import pytest

from conftest import response_text as _text
from sophia.backend import config
from sophia.backend.clients import bills_db as bills_db_module
from sophia.backend.services import chat as chat_service
from sophia.backend.services import evidence as evidence_service
from sophia.backend.services.errors import ModeError

GROUNDED = {"answer": "Home internet (FibreLink, $79.00) is overdue.", "citations": [{"source": "bill-7-home-internet.md", "bill_id": 7, "title": "Home internet", "distance": 1.083}],
            "confidence": "medium", "insufficient": False, "retrieval": [{"id": "billing_bill-7-home-internet.md_0", "source": "bill-7-home-internet.md", "distance": 1.083}], "fallback": False, "duration_ms": 310.0}
INSUFFICIENT = {"answer": "Tally couldn't find a bill that covers that.", "citations": [], "confidence": "none", "insufficient": True,
                "retrieval": [{"id": "x", "source": "bill-1-rent.md", "distance": 1.62}], "fallback": False, "duration_ms": 290.0}
PLAIN_QUESTION = {"op": None, "entity": None, "id": None, "fields": None, "question": "none", "say": "Let me check."}


@pytest.fixture
def modes_on(monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)


def fake_model(monkeypatch, payload):
    def chat(model, messages, timeout=None, temperature=None):
        return {"message": {"content": json.dumps(payload)}}

    monkeypatch.setattr("sophia.backend.ai.guard.chat", chat)


def fake_evidence(monkeypatch, card=None, error=None):
    questions = []

    def ask(question):
        questions.append(question)
        if error:
            raise error
        return card

    monkeypatch.setattr(evidence_service, "ask", ask)
    return questions


def test_plain_question_is_answered_from_the_corpus_with_chips_and_badge(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    questions = fake_evidence(monkeypatch, GROUNDED)
    body = _text(live_client.post("/ui/chat", data={"message": "Which bill is overdue?"}))
    assert questions == ["Which bill is overdue?"]
    assert "Home internet (FibreLink, $79.00) is overdue." in body and "Let me check." not in body
    assert "bill #7 · Home internet · 1.08" in body and "confidence-badge confidence-medium" in body
    assert bills_db_module.list_chat_messages()[-1]["content"] == GROUNDED["answer"]


def test_insufficient_context_is_the_fixed_line_with_no_chips(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    fake_evidence(monkeypatch, INSUFFICIENT)
    body = _text(live_client.post("/ui/chat", data={"message": "How much is my car insurance?"}))
    assert "couldn't find a bill" in body and "Insufficient context" in body
    assert "citation-chip" not in body


def test_mcp_failure_falls_back_to_the_models_reply(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    fake_evidence(monkeypatch, error=ModeError("mcp_connection"))
    response = live_client.post("/ui/chat", data={"message": "Which bill is overdue?"})
    assert response.status_code == 200
    body = _text(response)
    assert "Let me check." in body and "citation-chip" not in body and "8000" not in body


def test_modes_off_never_reach_the_corpus(live_client, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    fake_model(monkeypatch, PLAIN_QUESTION)
    questions = fake_evidence(monkeypatch, GROUNDED)
    body = _text(live_client.post("/ui/chat", data={"message": "Which bill is overdue?"}))
    assert questions == [] and "Let me check." in body


def test_code_computed_questions_and_proposals_skip_the_corpus(live_client, modes_on, monkeypatch):
    questions = fake_evidence(monkeypatch, GROUNDED)
    fake_model(monkeypatch, dict(PLAIN_QUESTION, question="total"))
    body = _text(live_client.post("/ui/chat", data={"message": "What do my bills add up to?"}))
    assert "ongoing monthly total" in body
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}, "question": "none",
                             "say": "I've suggested ending Spotify after 16 Sep — approve it to save."})
    body = _text(live_client.post("/ui/chat", data={"message": "I cancelled Spotify from September — remove the future payments"}))
    assert "Update Spotify" in body
    assert questions == []


NETFLIX_AS_UPDATE = {"op": "update", "entity": "bill", "id": 5, "fields": {"next_billing_date": "2026-10-14"}, "question": "none",
                     "say": "I've suggested changing the next billing date — approve it to save."}


def test_a_question_naming_a_bill_never_becomes_a_proposal(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, NETFLIX_AS_UPDATE)
    fake_evidence(monkeypatch, dict(GROUNDED, answer="Netflix is next charged on 2026-10-14.", citations=[{"source": "bill-4-netflix.md", "bill_id": 4, "title": "Netflix", "distance": 0.46}], confidence="high"))
    pending_before = len(bills_db_module.list_suggestions(status="pending"))
    body = _text(live_client.post("/ui/chat", data={"message": "When is my Netflix subscription next charged?"}))
    assert "Netflix is next charged on 2026-10-14." in body and "bill #4 · Netflix" in body
    assert "Proposed:" not in body
    assert len(bills_db_module.list_suggestions(status="pending")) == pending_before


def test_a_dropped_proposal_is_never_claimed_when_nothing_else_answers_a_question(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, NETFLIX_AS_UPDATE)
    fake_evidence(monkeypatch, error=ModeError("mcp_connection"))
    pending_before = len(bills_db_module.list_suggestions(status="pending"))
    response = live_client.post("/api/chat", json={"message": "When is Netflix due?"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["reply"] == chat_service.UNANSWERED_REPLY
    assert payload["preview"] is None and payload["route"] == "plain"
    assert len(bills_db_module.list_suggestions(status="pending")) == pending_before
    assert bills_db_module.list_chat_messages()[-1]["content"] == chat_service.UNANSWERED_REPLY


def test_the_chat_panel_shows_the_honest_line_not_the_dropped_proposals_sentence(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, NETFLIX_AS_UPDATE)
    fake_evidence(monkeypatch, error=ModeError("mcp_connection"))
    body = _text(live_client.post("/ui/chat", data={"message": "When is Netflix due?"}))
    assert chat_service.UNANSWERED_REPLY in body
    assert "I've suggested" not in body and "Proposed:" not in body


def test_a_dropped_proposal_is_never_claimed_with_the_modes_off(live_client, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    fake_model(monkeypatch, NETFLIX_AS_UPDATE)
    payload = live_client.post("/api/chat", json={"message": "When is Netflix due?"}).get_json()
    assert payload["reply"] == chat_service.UNANSWERED_REPLY and payload["preview"] is None


def test_a_question_naming_a_bill_is_grounded_even_when_the_model_says_upcoming(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, dict(PLAIN_QUESTION, question="upcoming"))
    questions = fake_evidence(monkeypatch, GROUNDED)
    body = _text(live_client.post("/ui/chat", data={"message": "When is Netflix due?"}))
    assert questions == ["When is Netflix due?"] and "Coming up this week" not in body


@pytest.mark.parametrize("message", ["What's due this week?", "What is coming up in the next two weeks?", "Anything upcoming?"])
def test_a_general_question_about_what_is_due_is_code_computed_whatever_the_model_says(live_client, modes_on, monkeypatch, message):
    fake_model(monkeypatch, PLAIN_QUESTION)
    questions = fake_evidence(monkeypatch, INSUFFICIENT)
    body = _text(live_client.post("/ui/chat", data={"message": message}))
    assert questions == [] and ("Coming up" in body or "Nothing is due" in body)
    assert "couldn't find a bill" not in body


def test_a_general_question_the_model_tags_upcoming_stays_code_computed(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, dict(PLAIN_QUESTION, question="upcoming"))
    questions = fake_evidence(monkeypatch, GROUNDED)
    body = _text(live_client.post("/ui/chat", data={"message": "What's due this week?"}))
    assert questions == [] and ("Coming up" in body or "Nothing is due" in body)


def test_a_question_with_a_change_verb_still_proposes(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 4, "fields": {"end_date": "2026-10-14"}, "question": "none",
                             "say": "I've suggested ending Netflix after 14 Oct — approve it to save."})
    questions = fake_evidence(monkeypatch, GROUNDED)
    body = _text(live_client.post("/ui/chat", data={"message": "Can you cancel Netflix from 14 October?"}))
    assert "Proposed:" in body and "Update Netflix" in body and questions == []


def test_chat_panel_shows_the_rag_mode_badge(live_client, modes_on, monkeypatch):
    body = _text(live_client.get("/ui/chat"))
    assert 'class="mode-badge mode-on">RAG enabled</span>' in body
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    body = _text(live_client.get("/ui/chat"))
    assert 'class="mode-badge mode-off">RAG disabled</span>' in body


def test_api_chat_carries_the_grounded_card(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    fake_evidence(monkeypatch, GROUNDED)
    payload = live_client.post("/api/chat", json={"message": "Which bill is overdue?"}).get_json()
    assert payload["reply"] == GROUNDED["answer"]
    assert payload["grounded"]["confidence"] == "medium"
    assert [c["source"] for c in payload["grounded"]["citations"]] == ["bill-7-home-internet.md"]


COMPARE = {"bill": {"id": 3, "merchant": "Spotify AU", "amount_cents": 1399}, "payments": [],
           "charges": [{"id": 26, "date": "2026-08-20", "amount_cents": 1799, "description": "Monthly subscription", "differs_from_bill_cents": 400},
                       {"id": 22, "date": "2026-07-15", "amount_cents": 1399, "description": "Spotify Premium subscription", "differs_from_bill_cents": 0}]}


def fake_tool(monkeypatch, data=None, error=None):
    from sophia.backend.clients import mcp_server

    calls = []

    def call_tool(name, arguments):
        calls.append((name, arguments))
        if error:
            raise error
        return data, 41.0

    monkeypatch.setattr(mcp_server, "call_tool", call_tool)
    return calls


def test_a_question_about_what_a_bill_charged_is_answered_by_the_compare_tool(live_client, modes_on, monkeypatch):
    from datetime import timedelta

    fake_model(monkeypatch, PLAIN_QUESTION)
    questions = fake_evidence(monkeypatch, GROUNDED)
    calls = fake_tool(monkeypatch, COMPARE)
    pending_before = len(bills_db_module.list_suggestions(status="pending"))
    body = _text(live_client.post("/ui/chat", data={"message": "What has Spotify actually charged me?"}))
    start = (config.DEMO_TODAY - timedelta(days=90)).isoformat()
    assert calls == [("compare_bill_with_bank_charges", {"bill_id": 3, "start_date": start, "end_date": config.DEMO_TODAY.isoformat()})]
    assert questions == [] and "Proposed:" not in body
    assert "Spotify AU charged you two times in the last 90 days" in body and "20 Aug" in body and "$4.00 above" in body
    assert "20 Aug · $17.99 · +$4.00 vs bill" in body and "15 Jul · $13.99" in body
    assert "compare_bill_with_bank_charges" in body and "41.0 ms" in body
    assert len(bills_db_module.list_suggestions(status="pending")) == pending_before


def test_no_bank_charges_is_said_plainly(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    fake_tool(monkeypatch, dict(COMPARE, charges=[]))
    body = _text(live_client.post("/ui/chat", data={"message": "Has Spotify charged me?"}))
    assert "No bank charges from Spotify AU in the last 90 days." in body


def test_next_charge_questions_stay_grounded_and_mcp_failure_falls_back_to_the_corpus(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    questions = fake_evidence(monkeypatch, GROUNDED)
    calls = fake_tool(monkeypatch, COMPARE)
    live_client.post("/ui/chat", data={"message": "When is my Spotify subscription next charged?"})
    assert calls == [] and questions == ["When is my Spotify subscription next charged?"]
    calls = fake_tool(monkeypatch, error=ModeError("mcp_connection"))
    body = _text(live_client.post("/ui/chat", data={"message": "What has Spotify actually charged me?"}))
    assert len(calls) == 1 and "bill #7 · Home internet" in body


def test_tool_answers_never_run_with_mcp_off(live_client, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    fake_model(monkeypatch, PLAIN_QUESTION)
    calls = fake_tool(monkeypatch, COMPARE)
    body = _text(live_client.post("/ui/chat", data={"message": "What has Spotify actually charged me?"}))
    assert calls == [] and "Let me check." in body


def test_a_total_question_without_a_question_mark_is_never_a_change(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 1, "fields": {"amount": None}, "question": "none", "say": "Updating the amount."})
    questions = fake_evidence(monkeypatch, INSUFFICIENT)
    pending_before = len(bills_db_module.list_suggestions(status="pending"))
    body = _text(live_client.post("/ui/chat", data={"message": "What do my bills add up to each month"}))
    assert "ongoing monthly total" in body and "couldn't turn that into a change" not in body and "Proposed:" not in body
    assert questions == [] and len(bills_db_module.list_suggestions(status="pending")) == pending_before


def test_upcoming_answers_use_the_horizon_the_user_asked_for(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    fake_evidence(monkeypatch, INSUFFICIENT)
    two_weeks = _text(live_client.post("/ui/chat", data={"message": "What's coming up in the next two weeks?"}))
    assert "Coming up in the next 14 days:" in two_weeks and "this week" not in two_weeks
    assert " on " in two_weeks and "($" in two_weeks
    month = _text(live_client.post("/ui/chat", data={"message": "What is due next month?"}))
    assert "next 30 days" in month
    week = _text(live_client.post("/ui/chat", data={"message": "What's due this week?"}))
    assert "Coming up this week:" in week


def test_a_due_question_that_mentions_a_total_lists_what_is_due(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    fake_evidence(monkeypatch, INSUFFICIENT)
    body = _text(live_client.post("/ui/chat", data={"message": "What's my total due this week?"}))
    assert "Coming up this week:" in body


@pytest.mark.parametrize("message, span", [
    ("What's due in the next 2 months?", "60 days"),
    ("What's due in the next three weeks?", "21 days"),
    ("What's due in the next ten days?", "10 days"),
    ("What's due in the next 99999999 days?", "180 days"),
    ("What's due in the next 1 day?", "1 day:"),
])
def test_horizons_in_words_months_and_extremes_are_honoured_within_bounds(live_client, modes_on, monkeypatch, message, span):
    fake_model(monkeypatch, PLAIN_QUESTION)
    fake_evidence(monkeypatch, INSUFFICIENT)
    body = _text(live_client.post("/ui/chat", data={"message": message}))
    assert span in body or "Nothing is due" in body


def test_a_will_pay_question_with_a_horizon_and_the_model_tagging_upcoming_honours_the_horizon(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, dict(PLAIN_QUESTION, question="upcoming"))
    fake_evidence(monkeypatch, INSUFFICIENT)
    body = _text(live_client.post("/ui/chat", data={"message": "What will I pay in the next 3 weeks?"}))
    assert "next 21 days" in body and "this week" not in body


def test_a_request_phrased_as_a_can_you_question_without_a_question_mark_is_still_a_proposal(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 1, "fields": {"amount": 22.99}, "question": "none", "say": "Updating."})
    fake_evidence(monkeypatch, INSUFFICIENT)
    body = _text(live_client.post("/ui/chat", data={"message": "Can you edit Netflix to $22.99"}))
    assert "Proposed" in body or "Approve" in body or "Apply" in body


def test_a_barely_using_question_is_answered_in_code_whatever_the_model_tags(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, dict(PLAIN_QUESTION, question="upcoming"))
    questions = fake_evidence(monkeypatch, INSUFFICIENT)
    body = _text(live_client.post("/ui/chat", data={"message": "Which subscriptions am I barely using?"}))
    assert ("has billed" in body or "Everything looks actively used" in body) and "Coming up" not in body and questions == []


def test_an_add_request_missing_the_amount_asks_for_it_instead_of_erroring(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "create", "entity": "bill", "id": None, "fields": {"name": "Gym membership", "merchant": None, "amount": None, "cadence": None, "next_billing_date": None, "type": "subscription"},
                             "question": "none", "say": "Adding a gym membership."})
    pending_before = len(bills_db_module.list_suggestions(status="pending"))
    body = _text(live_client.post("/ui/chat", data={"message": "Add a gym membership"}))
    assert "I just need" in body and "the amount" in body and "must be an amount" not in body
    assert len(bills_db_module.list_suggestions(status="pending")) == pending_before


def test_a_dispute_update_with_a_reason_and_no_status_is_a_new_dispute(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "update", "entity": "dispute", "id": 6, "fields": {"bill_id": 6, "reason": "Never signed up for GymCo"},
                             "question": "none", "say": "I've suggested opening a dispute for GymCo — approve it to save."})
    body = _text(live_client.post("/ui/chat", data={"message": "I want to dispute the GymCo charge, I never signed up for it"}))
    latest = bills_db_module.list_suggestions(status="pending")[-1]
    assert (latest["op"], latest["entity"], latest["entity_id"]) == ("create", "dispute", None)
    assert json.loads(latest["payload_json"]) == {"bill_id": 6, "reason": "I never signed up for it"}
    assert "Update dispute" not in body and "GymCo" in body


def test_an_update_targets_the_one_bill_the_user_named_even_when_the_model_picks_another_id(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 5, "fields": {"amount": 15.99}, "question": "none",
                             "say": "I've suggested changing Spotify to $15.99 a month — approve it to save."})
    body = _text(live_client.post("/ui/chat", data={"message": "Update my Spotify to $15.99 a month"}))
    latest = bills_db_module.list_suggestions(status="pending")[-1]
    assert (latest["op"], latest["entity"], latest["entity_id"]) == ("update", "bill", 3)
    assert json.loads(latest["payload_json"]) == {"amount_cents": 1599}
    assert "Update Spotify" in body and "Which bill did you mean" not in body


def test_an_update_naming_two_bills_still_asks_which(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 5, "fields": {"amount": 15.99}, "question": "none",
                             "say": "I've suggested changing Spotify to $15.99 a month — approve it to save."})
    pending_before = len(bills_db_module.list_suggestions(status="pending"))
    body = _text(live_client.post("/ui/chat", data={"message": "Update my Spotify or Netflix to $15.99 a month"}))
    assert "Which bill did you mean" in body and len(bills_db_module.list_suggestions(status="pending")) == pending_before


@pytest.mark.parametrize("model_reply", [
    {"op": "update", "entity": "bill", "id": 6, "fields": {"disputed": True}, "question": "none", "say": "Marked GymCo as disputed."},
    {"op": None, "entity": None, "id": None, "fields": None, "question": "none", "say": "I can help with that."},
])
def test_a_dispute_request_naming_one_bill_is_built_in_code_whatever_the_model_emits(live_client, modes_on, monkeypatch, model_reply):
    fake_model(monkeypatch, model_reply)
    body = _text(live_client.post("/ui/chat", data={"message": "I want to dispute the GymCo charge, I never signed up for it"}))
    latest = bills_db_module.list_suggestions(status="pending")[-1]
    assert (latest["op"], latest["entity"]) == ("create", "dispute")
    assert json.loads(latest["payload_json"]) == {"bill_id": 6, "reason": "I never signed up for it"}
    assert "Open dispute for GymCo" in body and "cannot be set via chat" not in body


def test_a_dispute_request_without_a_reason_clause_uses_the_whole_message(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    live_client.post("/ui/chat", data={"message": "Dispute my Netflix charge"})
    latest = bills_db_module.list_suggestions(status="pending")[-1]
    assert json.loads(latest["payload_json"]) == {"bill_id": 4, "reason": "Dispute my Netflix charge"}


def test_a_model_upcoming_tag_is_ignored_when_the_question_never_asks_what_is_due(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, dict(PLAIN_QUESTION, question="upcoming"))
    questions = fake_evidence(monkeypatch, GROUNDED)
    body = _text(live_client.post("/ui/chat", data={"message": "Which bill is overdue?"}))
    assert questions == ["Which bill is overdue?"] and "Home internet (FibreLink, $79.00) is overdue." in body
    assert "Coming up" not in body


def test_a_cost_question_about_something_that_is_not_a_bill_is_insufficient_not_a_due_list(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, PLAIN_QUESTION)
    questions = fake_evidence(monkeypatch, INSUFFICIENT)
    body = _text(live_client.post("/ui/chat", data={"message": "What is my car insurance bill cost for this month?"}))
    assert questions == ["What is my car insurance bill cost for this month?"]
    assert "couldn't find a bill" in body and "Coming up" not in body


def test_a_dispute_built_from_the_users_words_gets_a_matching_sentence(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 3, "fields": {"end_date": "2026-09-16"}, "question": "none",
                             "say": "I've suggested ending Spotify after 16 Sep — approve it to save."})
    body = _text(live_client.post("/ui/chat", data={"message": "Dispute my Spotify charge, the price went up without notice (the $17.99 row)."}))
    assert "Open dispute for Spotify" in body
    assert "I've suggested opening a dispute for Spotify" in body and "ending Spotify" not in body


def test_chat_panel_shows_the_demo_date_next_to_the_rag_badge(live_client, modes_on):
    body = _text(live_client.get("/ui/chat"))
    assert 'class="mode-badge today-badge">Today: 20-Aug-2026</span>' in body


def test_an_update_that_repeats_the_bills_current_values_asks_what_to_change(live_client, modes_on, monkeypatch):
    netflix = next(b for b in bills_db_module.list_bills() if b["name"] == "Netflix")
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": netflix["id"], "fields": {"amount": netflix["amount_cents"] / 100, "cadence": netflix["cadence"], "type": netflix["type"]},
                             "question": "none", "say": "I've suggested leaving Netflix as is — approve it to save."})
    pending_before = len(bills_db_module.list_suggestions(status="pending"))
    body = _text(live_client.post("/ui/chat", data={"message": "update my netflix bill (not sure what to put)"}))
    assert "What would you like to change about Netflix?" in body and "Proposed:" not in body
    assert len(bills_db_module.list_suggestions(status="pending")) == pending_before


def test_an_update_to_a_payment_method_the_user_never_said_asks_for_it(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 7, "fields": {"payment_method": "card"}, "question": "none",
                             "say": "I've suggested changing the payment method for Home internet to card — approve it to save."})
    pending_before = len(bills_db_module.list_suggestions(status="pending"))
    body = _text(live_client.post("/ui/chat", data={"message": "update my home internet bill"}))
    assert "the payment method" in body and "Proposed:" not in body
    assert len(bills_db_module.list_suggestions(status="pending")) == pending_before
    fake_model(monkeypatch, {"op": "update", "entity": "bill", "id": 7, "fields": {"payment_method": "card"}, "question": "none",
                             "say": "I've suggested changing the payment method for Home internet to card — approve it to save."})
    body = _text(live_client.post("/ui/chat", data={"message": "switch my home internet to card"}))
    assert "Update Home internet" in body


def test_a_new_bill_drops_a_payment_method_the_user_never_said_but_is_still_proposed(live_client, modes_on, monkeypatch):
    fake_model(monkeypatch, {"op": "create", "entity": "bill", "id": None, "fields": {"name": "Stan", "merchant": "Stan", "amount": 10.0, "cadence": "monthly", "next_billing_date": "2026-10-05", "type": "subscription", "payment_method": "card"},
                             "question": "none", "say": "I've suggested adding Stan at $10 a month from 5 Oct — approve it to save."})
    body = _text(live_client.post("/ui/chat", data={"message": "Add Stan, $10 a month, first charge 5 October"}))
    assert "Add bill: Stan" in body
    payload = json.loads(bills_db_module.list_suggestions(status="pending")[-1]["payload_json"])
    assert "payment_method" not in payload and payload["amount_cents"] == 1000
