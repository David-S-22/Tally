"""Dispute letters cite bank facts and policy sentences read through MCP and RAG; either off or down leaves the letter as it was, and the panel says so."""
import json
from datetime import date

from conftest import response_text as _text
from sophia.backend import config
from sophia.backend.ai import dispute_prompt
from sophia.backend.clients import bills_db as bills_db_module
from sophia.backend.clients import mcp_server
from sophia.backend.engine import Bill
from sophia.backend.services import disputes
from sophia.backend.services.errors import ModeError

SPOTIFY = Bill(id=3, name="Spotify", merchant="Spotify AU", amount_cents=1399, cadence="monthly", next_billing_date=date(2026, 9, 27),
               type="subscription", payment_method="card", end_date=None, confirmed_at=None, created_at=None)
COMPARE = {"bill": {"id": 3, "merchant": "Spotify AU", "amount_cents": 1399}, "payments": [],
           "charges": [{"id": 26, "date": "2026-08-20", "amount_cents": 1799, "description": "Monthly subscription", "differs_from_bill_cents": 400},
                       {"id": 22, "date": "2026-07-15", "amount_cents": 1399, "description": "Spotify Premium subscription", "differs_from_bill_cents": 0},
                       {"id": 15, "date": "2026-07-08", "amount_cents": 1399, "description": "Spotify Premium subscription", "differs_from_bill_cents": 0}]}
FLAGGED = [{"transaction": {"id": 22, "merchant": "Spotify AU", "amount": 13.99}, "anomaly": {"id": 10, "transaction_id": 22, "is_confirmed_by_user": True}},
           {"transaction": {"id": 1, "merchant": "Harbourview Realty", "amount": 1100.0}, "anomaly": {"id": 1, "transaction_id": 1, "is_confirmed_by_user": True}}]


def fake_tools(monkeypatch, error=None, anomalies_error=None):
    calls = []

    def call_tool(name, arguments):
        calls.append((name, arguments))
        if error:
            raise error
        if anomalies_error and name == "get_transactions_with_confirmed_anomalies":
            raise anomalies_error
        return ({"compare_bill_with_bank_charges": COMPARE, "get_transactions_with_confirmed_anomalies": FLAGGED}[name], 12.0)

    monkeypatch.setattr(mcp_server, "call_tool", call_tool)
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    return calls


def fake_draft(monkeypatch):
    prompts = []

    def chat(model, messages, timeout=None, temperature=None):
        prompts.append(messages)
        return {"message": {"content": json.dumps({"letter_text": "x" * 100, "steps": ["Step one", "Step two"], "escalation": ["Merchant support"], "payment_method_note": None})}}

    monkeypatch.setattr("sophia.backend.ai.guard.chat", chat)
    return prompts


def test_bank_evidence_lists_the_merchants_charges_confirmed_ones_first(monkeypatch):
    calls = fake_tools(monkeypatch)
    rows, tools = disputes.bank_evidence(SPOTIFY, date(2026, 9, 26))
    assert rows == ["15 Jul Spotify AU $13.99, flagged by Spending Alerts and confirmed by you", "20 Aug Spotify AU $17.99", "8 Jul Spotify AU $13.99"]
    assert tools == ["compare_bill_with_bank_charges", "get_transactions_with_confirmed_anomalies"]
    assert calls == [("compare_bill_with_bank_charges", {"bill_id": 3, "start_date": "2026-06-28", "end_date": "2026-09-26"}),
                     ("get_transactions_with_confirmed_anomalies", {})]


def test_bank_evidence_is_none_with_no_tools_when_mcp_is_off_or_the_comparison_fails(monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    assert disputes.bank_evidence(SPOTIFY, date(2026, 9, 26)) == (None, [])
    fake_tools(monkeypatch, error=ModeError("mcp_connection"))
    assert disputes.bank_evidence(SPOTIFY, date(2026, 9, 26)) == (None, [])


def test_bank_evidence_keeps_the_comparison_rows_when_only_the_anomalies_tool_fails(monkeypatch):
    fake_tools(monkeypatch, anomalies_error=ModeError("mcp_tool_error"))
    rows, tools = disputes.bank_evidence(SPOTIFY, date(2026, 9, 26))
    assert rows == ["20 Aug Spotify AU $17.99", "15 Jul Spotify AU $13.99", "8 Jul Spotify AU $13.99"]
    assert not any("flagged by Spending Alerts" in row for row in rows)
    assert tools == ["compare_bill_with_bank_charges"]


def test_draft_prompt_carries_the_bank_facts_only_when_there_are_some(monkeypatch):
    fake_tools(monkeypatch)
    prompts = fake_draft(monkeypatch)
    monkeypatch.setattr(bills_db_module, "list_bill_payments", lambda bill_id: [])
    row = {"id": 3, "name": "Spotify", "merchant": "Spotify AU", "amount_cents": 1399, "cadence": "monthly", "next_billing_date": "2026-09-27", "type": "subscription", "payment_method": "card"}
    disputes.draft_for_bill(row, "Charged twice in July", opened_on=date(2026, 9, 26))
    user_text = prompts[0][1]["content"]
    assert "Bank statement facts" in user_text and "15 Jul Spotify AU $13.99, flagged by Spending Alerts and confirmed by you" in user_text
    assert "cite them exactly" in user_text.lower() or "cite these" in user_text.lower()
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    disputes.draft_for_bill(row, "Charged twice in July", opened_on=date(2026, 9, 26))
    assert "Bank statement" not in prompts[1][1]["content"]


def test_dispute_panel_says_when_bank_evidence_is_unavailable(live_client, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    body = _text(live_client.get("/ui/disputes"))
    assert "Bank evidence and policy facts unavailable: MCP mode is disabled." in body
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    body = _text(live_client.get("/ui/disputes"))
    assert "Policy facts unavailable: RAG mode is disabled." in body and "MCP mode is disabled" not in body
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    body = _text(live_client.get("/ui/disputes"))
    assert "evidence-note" not in body and "unavailable:" not in body


def test_creating_a_dispute_through_the_ui_uses_the_disputes_opened_date(live_client, monkeypatch):
    calls = fake_tools(monkeypatch)
    fake_draft(monkeypatch)
    response = live_client.post("/ui/disputes", data={"bill_id": "3", "reason": "Charged twice in July"})
    assert response.status_code == 201
    compare = next(c for c in calls if c[0] == "compare_bill_with_bank_charges")
    assert compare[1]["bill_id"] == 3 and compare[1]["end_date"] >= compare[1]["start_date"]


PDF_TEXT = (
    "Payment Methods Guide\nAccepted Payment Methods\nDirect debit and credit cards (Visa, Mastercard, Amex) are accepted for all utility \n"
    "bills and subscription payments.\nBank transfers take 2-3 business days to clear.\nLate Fee Notice\nLate payments incur a ten dollar penalty fee.\n"
    "For any billing disputes or inquiries, contact customer support at \nsupport@example.com."
)
OVERVIEW_TEXT = (
    "# Billing and Subscriptions Guide\n\n## Recurring Subscriptions\n"
    "- Spotify from Spotify AU is a monthly subscription of $13.99. It was billed on 2026-08-16 and its status is paid.\n"
    "- Netflix is a monthly subscription of $20.99. It is billed on 2026-09-02 and its status is due.\n"
    "- GymCo is a monthly subscription of $24.99. It is billed on 2026-09-03 and its status is due.\n\n## Utility Bills\n"
    "- Home internet from FibreLink is a monthly bill of $79.00. It was billed on 2026-08-15 and its status is overdue.\n"
    "- Electricity from Sparkwell Energy is a monthly bill of $142.00. It is billed on 2026-09-10 and its status is paid.\n\n"
    "## Payment Policies and Schedules\n"
    "- Full payment for utility bills is due within 14 calendar days from the invoice issue date.\n"
    "- Accounts with payments overdue beyond 30 days incur a 5% late penalty fee.\n"
    "- Direct debit and major credit cards are accepted for automatic settlements."
)
PDF_SENTENCES = [
    "billing_policy.pdf: Direct debit and credit cards (Visa, Mastercard, Amex) are accepted for all utility bills and subscription payments.",
    "billing_policy.pdf: Bank transfers take 2-3 business days to clear.",
    "billing_policy.pdf: Late payments incur a ten dollar penalty fee.",
]
OVERVIEW_SENTENCES = [
    "billing_overview.md: Full payment for utility bills is due within 14 calendar days from the invoice issue date.",
    "billing_overview.md: Accounts with payments overdue beyond 30 days incur a 5% late penalty fee.",
    "billing_overview.md: Direct debit and major credit cards are accepted for automatic settlements.",
]
BILL_TEXT = "- Spotify is late and was billed on 2026-09-01 with a penalty fee."
ROW = {"id": 3, "name": "Spotify", "merchant": "Spotify AU", "amount_cents": 1399, "cadence": "monthly", "next_billing_date": "2026-09-27", "type": "subscription", "payment_method": "card"}


def chunk(source, text, distance):
    return {"id": source, "text": text, "metadata": {"source": source, "doc_type": "md", "page": 1}, "distance": distance}


def fake_retrieve(monkeypatch, results, error=None):
    calls = []

    def call_tool(name, arguments):
        calls.append((name, arguments))
        if error:
            raise error
        return {"results": results}, 5.0

    monkeypatch.setattr(mcp_server, "call_tool", call_tool)
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    return calls


def test_policy_evidence_keeps_only_whole_policy_sentences_closest_chunk_first(monkeypatch):
    calls = fake_retrieve(monkeypatch, [chunk("billing_overview.md", OVERVIEW_TEXT, 1.33), chunk("bill-7-spotify.md", BILL_TEXT, 0.5), chunk("billing_policy.pdf", PDF_TEXT, 1.03)])
    assert disputes.policy_evidence("Charged twice") == PDF_SENTENCES
    assert calls == [("retrieve_context", {"feature": "billing", "question": "Charged twice", "k": disputes.POLICY_K})]


def test_policy_evidence_never_carries_headings_fragments_contacts_bill_facts_or_bill_chunks(monkeypatch):
    fake_retrieve(monkeypatch, [chunk("billing_policy.pdf", PDF_TEXT, 1.03), chunk("billing_overview.md", OVERVIEW_TEXT, 1.33), chunk("bill-7-spotify.md", BILL_TEXT, 0.5)])
    monkeypatch.setattr(disputes, "POLICY_ROWS", 99)
    rows = disputes.policy_evidence("x")
    assert rows == PDF_SENTENCES + OVERVIEW_SENTENCES
    assert all(row.endswith(".") for row in rows)
    text = " ".join(rows)
    assert "support@example.com" not in text and "billed on" not in text.lower() and "Spotify" not in text and "Netflix" not in text


def test_policy_evidence_rejects_bill_facts_worded_without_billed_on(monkeypatch):
    lines = "- Billed on 2026-08-16, the Spotify late fee was 5.\n- The late fee was $5.\n- Payment was due on 2026-08-16 with a late fee.\n- Your feedback on the related template was calculated."
    fake_retrieve(monkeypatch, [chunk("billing_overview.md", lines, 1.0)])
    assert disputes.policy_evidence("x") == []


def test_policy_evidence_ignores_chunks_beyond_rag_low_and_is_empty_not_none(monkeypatch):
    fake_retrieve(monkeypatch, [chunk("billing_policy.pdf", PDF_TEXT, config.RAG_LOW + 0.01), chunk("bill-7-spotify.md", BILL_TEXT, 0.4)])
    assert disputes.policy_evidence("x") == []


def test_policy_evidence_is_none_when_rag_or_mcp_is_off_or_the_tool_fails(monkeypatch):
    calls = fake_retrieve(monkeypatch, [chunk("billing_policy.pdf", PDF_TEXT, 1.0)])
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    assert disputes.policy_evidence("x") is None
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    assert disputes.policy_evidence("x") is None
    assert calls == []
    fake_retrieve(monkeypatch, [], error=ModeError("rag_unavailable"))
    assert disputes.policy_evidence("x") is None


def test_policy_evidence_is_none_for_a_malformed_payload_and_the_letter_still_drafts(monkeypatch):
    prompts = fake_draft(monkeypatch)
    monkeypatch.setattr(bills_db_module, "list_bill_payments", lambda bill_id: [])
    monkeypatch.setattr(disputes, "bank_evidence", lambda bill, opened_on: (None, []))
    good = chunk("billing_policy.pdf", PDF_TEXT, 1.0)
    payloads = [{"results": "x"}, {"results": [{"id": "a", "text": "t", "distance": 1.0}]}, {"results": [{**good, "text": None}]},
                {"results": [{**good, "distance": None}]}, {}, [good], {"results": [{**good, "metadata": {}}]}]
    for payload in payloads:
        monkeypatch.setattr(mcp_server, "call_tool", lambda name, arguments, payload=payload: (payload, 1.0))
        monkeypatch.setattr(config, "MCP_ENABLED", True)
        monkeypatch.setattr(config, "RAG_ENABLED", True)
        assert not disputes.policy_evidence("x")
        assert disputes.draft_for_bill(ROW, "Charged twice", opened_on=date(2026, 9, 26))["letter_text"]
    assert all("Policy notes" not in p[1]["content"] for p in prompts)


def test_draft_still_drafts_without_policy_notes_when_retrieval_is_down(monkeypatch):
    fake_retrieve(monkeypatch, [], error=ModeError("mcp_tool_error"))
    prompts = fake_draft(monkeypatch)
    monkeypatch.setattr(bills_db_module, "list_bill_payments", lambda bill_id: [])
    assert disputes.draft_for_bill(ROW, "Charged twice", opened_on=date(2026, 9, 26))["letter_text"]
    assert "Policy notes" not in prompts[0][1]["content"] and "Bank statement" not in prompts[0][1]["content"]


def test_prompt_carries_policy_notes_only_when_there_are_some():
    plain = dispute_prompt.build(SPOTIFY, "Charged twice")
    assert dispute_prompt.build(SPOTIFY, "Charged twice", evidence=None, policy=None) == plain
    assert dispute_prompt.build(SPOTIFY, "Charged twice", evidence=[], policy=[]) == plain
    text = dispute_prompt.build(SPOTIFY, "Charged twice", evidence=["15 Jul Spotify AU $13.99"], policy=["p.pdf: A.", "o.md: B."])[1]["content"]
    assert "Policy notes from the user's billing guide (quote only these; never use them to compute amounts or dates; never present them as the merchant's terms): p.pdf: A. o.md: B." in text
    assert "Bank statement facts" in text


def test_draft_prompt_policy_line_follows_the_modes(monkeypatch):
    fake_tools(monkeypatch)
    prompts = fake_draft(monkeypatch)
    monkeypatch.setattr(bills_db_module, "list_bill_payments", lambda bill_id: [])
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(disputes, "policy_evidence", lambda reason: ["billing_policy.pdf: Late payments incur a ten dollar penalty fee."])
    disputes.draft_for_bill(ROW, "Charged twice", opened_on=date(2026, 9, 26))
    assert "Policy notes from the user's billing guide" in prompts[0][1]["content"] and "Bank statement facts" in prompts[0][1]["content"]


def test_draft_prompt_with_mcp_and_rag_off_is_the_pre_policy_prompt(monkeypatch):
    prompts = fake_draft(monkeypatch)
    monkeypatch.setattr(bills_db_module, "list_bill_payments", lambda bill_id: [])
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    disputes.draft_for_bill(ROW, "Charged twice", opened_on=date(2026, 9, 26))
    assert prompts[0] == dispute_prompt.build(bills_db_module.row_to_bill(ROW), "Charged twice", payments=[])
    assert prompts[0][1]["content"] == "Bill: Spotify (Spotify AU), amount $13.99, cadence: monthly.\nPayment method: card.\nNo payment history on file.\nReason for dispute: Charged twice."


def fake_all_tools(monkeypatch):
    def call_tool(name, arguments):
        if name == "retrieve_context":
            return {"results": [{"id": "billing_billing_policy.pdf_0", "text": PDF_TEXT, "metadata": {"source": "billing_policy.pdf", "doc_type": "pdf", "page": 1}, "distance": 1.03}]}, 9.0
        return ({"compare_bill_with_bank_charges": COMPARE, "get_transactions_with_confirmed_anomalies": FLAGGED}[name], 12.0)

    monkeypatch.setattr(mcp_server, "call_tool", call_tool)
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)


def test_draft_carries_the_evidence_it_used(monkeypatch):
    fake_all_tools(monkeypatch)
    fake_draft(monkeypatch)
    monkeypatch.setattr(bills_db_module, "list_bill_payments", lambda bill_id: [])
    row = {"id": 3, "name": "Spotify", "merchant": "Spotify AU", "amount_cents": 1399, "cadence": "monthly", "next_billing_date": "2026-09-27", "type": "subscription", "payment_method": "card"}
    draft = disputes.draft_for_bill(row, "Late fee added", opened_on=date(2026, 9, 26))
    assert draft["evidence"]["bank"][0] == "15 Jul Spotify AU $13.99, flagged by Spending Alerts and confirmed by you"
    assert draft["evidence"]["policy"] and all(p.startswith("billing_policy.pdf: ") for p in draft["evidence"]["policy"])
    assert draft["evidence"]["tools"] == ["compare_bill_with_bank_charges", "get_transactions_with_confirmed_anomalies", "retrieve_context"]
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    assert disputes.draft_for_bill(row, "Late fee added", opened_on=date(2026, 9, 26))["evidence"] == {"bank": [], "policy": [], "tools": []}


def test_dispute_panel_shows_the_evidence_and_tools_under_the_letter(live_client, monkeypatch):
    fake_all_tools(monkeypatch)
    fake_draft(monkeypatch)
    body = _text(live_client.post("/ui/disputes", data={"bill_id": "3", "reason": "Late fee added"}))
    assert 'class="evidence-used"' in body
    assert "15 Jul Spotify AU $13.99, flagged by Spending Alerts and confirmed by you" in body and "20 Aug Spotify AU $17.99" in body
    assert "billing_policy.pdf" in body and "compare_bill_with_bank_charges" in body and "retrieve_context" in body
    latest = bills_db_module.list_dispute_drafts(bills_db_module.list_disputes()[-1]["id"])[-1]
    assert "evidence" in json.loads(latest["steps_json"])


def test_draft_keeps_its_bank_facts_when_only_the_anomalies_tool_fails(monkeypatch):
    fake_tools(monkeypatch, anomalies_error=ModeError("mcp_tool_error"))
    fake_draft(monkeypatch)
    monkeypatch.setattr(bills_db_module, "list_bill_payments", lambda bill_id: [])
    draft = disputes.draft_for_bill(ROW, "Late fee added", opened_on=date(2026, 9, 26))
    assert draft["evidence"]["bank"]
    assert "compare_bill_with_bank_charges" in draft["evidence"]["tools"]
    assert "get_transactions_with_confirmed_anomalies" not in draft["evidence"]["tools"]


def test_dispute_panel_keeps_the_bank_evidence_when_only_the_anomalies_tool_fails(live_client, monkeypatch):
    fake_tools(monkeypatch, anomalies_error=ModeError("mcp_tool_error"))
    fake_draft(monkeypatch)
    body = _text(live_client.post("/ui/disputes", data={"bill_id": "3", "reason": "Late fee added"}))
    assert 'class="evidence-used"' in body
    assert "20 Aug Spotify AU $17.99" in body and "compare_bill_with_bank_charges" in body
    assert "get_transactions_with_confirmed_anomalies" not in body


def test_seeded_drafts_and_mcp_off_drafts_show_no_evidence_block(live_client, monkeypatch):
    assert 'class="evidence-used"' not in _text(live_client.get("/ui/disputes?dispute_id=1"))
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    fake_draft(monkeypatch)
    body = _text(live_client.post("/ui/disputes", data={"bill_id": "7", "reason": "Speed downgrade"}))
    assert 'class="evidence-used"' not in body and "MCP mode is disabled" in body


def test_with_bank_facts_the_recorded_payments_are_labelled_as_tallys_and_only_bank_facts_may_be_cited():
    from datetime import date as _date

    from sophia.backend.ai import dispute_prompt
    from sophia.backend.engine import Payment

    payments = [Payment(bill_id=3, date=_date(2026, 9, 27), amount_cents=1399)]
    with_bank = dispute_prompt.build(SPOTIFY, "Price went up", payments=payments, evidence=["20 Aug Spotify AU $17.99"])[1]["content"]
    assert "Payments you recorded in Tally (your own records, not bank data): 2026-09-27: $13.99" in with_bank
    assert "Cite dates and amounts only from the bank statement facts" in with_bank
    assert "Last payments:" not in with_bank
    without = dispute_prompt.build(SPOTIFY, "Price went up", payments=payments)[1]["content"]
    assert "Last payments: 2026-09-27: $13.99" in without and "recorded in Tally" not in without


def test_a_new_dispute_is_opened_on_the_demo_date_not_the_databases_clock(live_client, monkeypatch):
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    fake_draft(monkeypatch)
    live_client.post("/ui/disputes", data={"bill_id": "7", "reason": "Speed downgrade"})
    assert bills_db_module.list_disputes()[-1]["opened_at"] == config.DEMO_TODAY.isoformat()
    fake_model = lambda model, messages, timeout=None, temperature=None: {"message": {"content": json.dumps({"op": "create", "entity": "dispute", "id": None, "fields": {"bill_id": 7, "reason": "Speed downgrade"}, "question": "none", "say": "Opening."})}}
    monkeypatch.setattr("sophia.backend.ai.guard.chat", fake_model)
    live_client.post("/ui/chat", data={"message": "Draft a note to dispute my Home internet charge"})
    sid = bills_db_module.list_suggestions(status="pending")[-1]["id"]
    monkeypatch.setattr("sophia.backend.ai.guard.chat", lambda model, messages, timeout=None, temperature=None: {"message": {"content": json.dumps({"letter_text": "x" * 100, "steps": ["Step one", "Step two"], "escalation": ["Merchant support"], "payment_method_note": None})}})
    live_client.post(f"/ui/suggestions/{sid}/approve")
    assert bills_db_module.list_disputes()[-1]["opened_at"] == config.DEMO_TODAY.isoformat()


def test_approving_a_dispute_proposal_opens_the_panel_with_its_evidence_and_switches_to_disputes(live_client, monkeypatch):
    fake_all_tools(monkeypatch)
    fake_draft(monkeypatch)
    monkeypatch.setattr("sophia.backend.ai.guard.chat", lambda model, messages, timeout=None, temperature=None: {"message": {"content": json.dumps(
        {"op": "create", "entity": "dispute", "id": None, "fields": {"bill_id": 3, "reason": "Late fee added"}, "question": "none", "say": "Opening."})}})
    live_client.post("/ui/chat", data={"message": "Draft a note to dispute my Spotify charge"})
    sid = bills_db_module.list_suggestions(status="pending")[-1]["id"]
    fake_draft(monkeypatch)
    response = live_client.post(f"/ui/suggestions/{sid}/approve")
    body = _text(response)
    assert response.status_code == 200
    assert 'id="dispute-panel"' in body and 'hx-swap-oob="true"' in body and 'class="evidence-used"' in body
    assert "15 Jul Spotify AU $13.99, flagged by Spending Alerts and confirmed by you" in body and "compare_bill_with_bank_charges" in body
    assert 'id="dispute-list" hx-swap-oob="true"' in body
    trigger = json.loads(response.headers["HX-Trigger"])
    assert trigger["switchTab"] == "disputes" and trigger["toast"] == "Done — change saved."


def test_approving_a_bill_update_does_not_open_the_dispute_panel(live_client, monkeypatch):
    monkeypatch.setattr("sophia.backend.ai.guard.chat", lambda model, messages, timeout=None, temperature=None: {"message": {"content": json.dumps(
        {"op": "update", "entity": "bill", "id": 3, "fields": {"amount": 15.99}, "question": "none", "say": "I've suggested changing Spotify to $15.99 a month."})}})
    live_client.post("/ui/chat", data={"message": "Update my Spotify to $15.99 a month"})
    sid = bills_db_module.list_suggestions(status="pending")[-1]["id"]
    response = live_client.post(f"/ui/suggestions/{sid}/approve")
    assert response.status_code == 200 and 'id="dispute-panel"' not in _text(response)
    assert "switchTab" not in json.loads(response.headers["HX-Trigger"])
