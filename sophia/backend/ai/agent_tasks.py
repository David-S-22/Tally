"""Bills task templates for the shared Multi-Agent Server (Release 2).

The server's Planner, Worker and Reviewer prompts are shared and generic
(prompts/multi_agent/); they treat the ``task`` as the assignment and the
``context`` as untrusted data. So everything Bills-specific that the three
agents must honour is written here, in the task text, under one heading per
role. Keep each template well under the server's 4,000-character task limit and
tell the Worker how long its output may be: the workflow runs three sequential
model calls, so output length is the lever on latency.

Two workflows:

* ``dispute_letter`` — text output. The Worker drafts the letter the user will
  send; Accept or Partially Accept stores it as the next dispute draft.
* ``chat_suggestion`` — structured output ``{op, entity, id, fields}`` in the
  shape Ask Tally already vets and applies; Accept applies it through the
  existing suggestions flow.
"""

BANK_FACTS_RULE = (
    "Cite dates and amounts only from the bank statement facts in the evidence (source ids starting "
    "with 'mcp:'). Payments the user recorded in Tally are their own records, never bank charges. "
    "Never invent account numbers, reference numbers, phone numbers, emails, dates or amounts. "
    "Never include legal threats. Never claim to send anything: this is a draft the user sends themselves."
)

DISPUTE_LETTER = """Draft a dispute letter for bill {bill_id} ({name}, {merchant}, ${amount}, {cadence}). Reason for the dispute: {reason}.

Planner: list the facts the letter must state (the bill, the charge or charges being disputed with their dates and amounts, what the user asks for) and name which supplied evidence source proves each one. If the evidence does not show the charge the reason refers to, say so in missing_information instead of planning around it.

Worker: write the letter the user will send to {merchant}. Plain English, 90 to 180 words, addressed "Dear {merchant}", one paragraph per fact, ending with the request (a refund or a corrected charge) and the user's name left as [Your name]. {bank_facts_rule} Output the letter as plain text in `output`; set `structured` to null.

Reviewer: check every date, amount and claim in the letter against the evidence. Flag as a risk any sentence the evidence does not support, any promised timeframe, and any contact detail. Verdict approve only when every claim is supported."""

CHAT_SUGGESTION = """The user asked Tally to change their bills data. Their words: "{message}". Tally's first reading of the request: {preview}.

Planner: decide which supplied evidence proves the bill or payment the user refers to and whether the user's words state every value the change needs (an amount in dollars, a date as YYYY-MM-DD, a cadence of weekly, fortnightly, monthly, quarterly or yearly, a payment method of card or direct_debit). Anything the user did not state goes in missing_information; never guess a value.

Worker: propose the change as a JSON object in `structured` with exactly these keys: "op" (create, update or delete), "entity" (bill, payment or dispute), "id" (the existing bill, payment or dispute id from the evidence, or null for create) and "fields" (only the columns that change: name, merchant, amount, cadence, next_billing_date, type, payment_method, end_date for a bill; date, amount for a payment; bill_id, reason for a dispute). Amounts are dollars as numbers. Use only ids that appear in the evidence. In `output`, one sentence (under 25 words) telling the user what the change does. If a needed value is missing, set `structured` to null and say in `output` what to ask the user.

Reviewer: confirm the id exists in the evidence, every field value appears in the user's words or the evidence, and the op matches what the user asked. A field the user never stated is a risk. Verdict approve only when the proposal can be applied exactly as the user asked."""


def dispute_letter(bill, reason):
    return DISPUTE_LETTER.format(
        bill_id=bill.id,
        name=bill.name,
        merchant=bill.merchant,
        amount=f"{bill.amount_cents / 100:.2f}",
        cadence=bill.cadence,
        reason=reason,
        bank_facts_rule=BANK_FACTS_RULE,
    )


def chat_suggestion(message, preview):
    summary = "none" if not preview else (
        f"{preview.get('op')} {preview.get('entity')}"
        + (f" {preview.get('id')}" if preview.get("id") is not None else "")
        + (f" with {preview.get('fields')}" if preview.get("fields") else "")
    )
    return CHAT_SUGGESTION.format(message=message.replace('"', "'")[:600], preview=summary[:400])
