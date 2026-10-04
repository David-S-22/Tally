"""Chat resolution and apply, shared by /api/chat(/apply) and /ui/chat(/apply).

send_message() only ever calls bills_db.create_chat_message - it never
touches bills, payments, or disputes directly. apply() is the only path
that writes those, always through the same CRUD calls a manual edit uses.
"""
import json
import re
import threading

import requests
from datetime import date, timedelta
from decimal import Decimal

from sophia.backend import config
from sophia.backend.ai import chat_prompt, guard, ollama_client
from sophia.backend.ai.schemas import validate_chat_response
from sophia.backend.clients import bills_db, transactions
from sophia.backend.engine import BARELY_USING_THRESHOLD, money
from sophia.backend.engine.calendar import month_breakdown
from sophia.backend.engine.dates import expected_per_month
from sophia.backend.engine.projection import project
from sophia.backend.engine.status import _day_month
from sophia.backend.services import bills as bills_service
from sophia.backend.services import disputes as disputes_service
from sophia.backend.services import evidence as evidence_service
from sophia.backend.services import payments as payments_service
from sophia.backend.services import tools as tools_service
from sophia.backend.services.errors import ModeError, NotFound, ServiceError

# The model is asked for the real column names, but a small model drifts, and
# it drifts predictably: it says "amount" in dollars where the column is
# amount_cents, and "next" where the column is next_billing_date. Those two are
# translated here rather than widened into the whitelist, so the whitelist keeps
# doing its job -- a genuinely invented field still fails loudly.
#
# Deliberately narrow. Mapping every plausible synonym would turn a strict
# allowlist into a guessing game, and a wrong guess writes bad data silently.
CHAT_FIELD_ALIASES = {
    "bill": {
        "amount": "amount_cents",
        "next": "next_billing_date",
        "next_date": "next_billing_date",
        "next_billing": "next_billing_date",
    },
    "payment": {"amount": "amount_cents"},
}

# Values the model states in dollars; stored in cents.
DOLLAR_VALUED_ALIASES = {"amount"}

BILL_FIELD_WHITELIST = {
    "bill": {
        "name", "merchant", "amount_cents", "cadence", "next_billing_date", "type",
        "payment_method", "end_date", "exclude_from_plan",
    },
    "payment": {"bill_id", "date", "amount_cents"},
    "dispute": {"bill_id", "reason", "status"},
}

# Keys the model may not send raw, even though the whitelist accepts them as
# alias *targets*. The prompt asks for dollars under "amount" and the one
# conversion lives in _normalise_chat_fields; a model that emits amount_cents
# itself is emitting a unit nothing verified ("amount_cents": 15 for a $15
# bill stores 15 cents, silently). Refusing the raw key makes that drift fail
# loudly instead.
RAW_KEY_BLOCKLIST = {"amount_cents"}

_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def _count_word(n):
    return _COUNT_WORDS.get(n, str(n))


def _recent_history():
    rows = bills_db.list_chat_messages()
    return [{"role": r["role"], "content": r["content"], "op_json": r.get("op_json")} for r in rows[-10:]]


def _stated_text(message, history):
    """The user's own words since the last proposal: the only text a new proposal may draw its values from."""
    said = [message or ""]
    for row in reversed(history):
        if row["role"] == "assistant" and row.get("op_json"):
            break
        if row["role"] == "user":
            said.append(row["content"])
    return " ".join(said)


def _answer_total(named=None):
    """Answer the "what do my bills add up to" question with both figures, over every bill or only the named rows.

    There are two defensible totals and they do not match. The table header
    shows the ongoing monthly rate -- every bill scaled to a month -- while this
    answer is about the calendar month actually in view, where a five-Monday
    month or a bill that starts mid-month changes the number. Quoting only the
    second put "around $1,695" two inches from a header reading $1,731.95, and
    from the user's chair that is the app disagreeing with itself. Naming both,
    and what each measures, costs one clause.
    """
    today = config.DEMO_TODAY
    bills = [bills_db.row_to_bill(r) for r in (named or bills_db.list_bills())]
    payments = [bills_db.row_to_payment(r) for r in bills_db.list_payments()]
    breakdown = month_breakdown(bills, payments, today.year, today.month, today)
    monthly_rate = sum(b.amount_cents * expected_per_month(b.cadence) for b in bills)
    scope = " and ".join(b.name for b in bills) if named else "all bills"
    return (
        f"{today.strftime('%B')} is set to cost around "
        f"{money.format_estimate_single(breakdown.total_high_cents)}{' for ' + scope if named else ''}. "
        f"Your ongoing monthly total across {scope} is {money.format_actual(monthly_rate)}."
    )


def _answer_barely_using():
    """A subscription counts as barely used once it has been billed at least
    BARELY_USING_THRESHOLD times since confirmed_at (or created_at when confirmed_at
    is null) - it keeps charging even though the user hasn't re-confirmed they still
    want it. Transaction history for the merchant since that date is supporting
    evidence only, never a second threshold that could exclude a flagged subscription.
    """
    bill_rows = bills_db.list_bills()
    payment_rows = bills_db.list_payments()
    sentences = []
    for row in bill_rows:
        if row["type"] != "subscription":
            continue
        since = row.get("confirmed_at") or row.get("created_at")
        count = sum(1 for p in payment_rows if p["bill_id"] == row["id"] and (since is None or p["date"] >= since))
        if count < BARELY_USING_THRESHOLD:
            continue
        transaction_rows, _source = transactions.list_transactions(merchant=row["merchant"], since=since)
        amount = money.format_actual(row["amount_cents"])
        sentence = (
            f"{row['name']} has billed {_count_word(count)} times since you last "
            f"confirmed you're using it — worth a look at {amount}/month."
        )
        if not transaction_rows:
            sentence += " No recent activity for that merchant either."
        sentences.append(sentence)
    if not sentences:
        return "Everything looks actively used — nothing has billed repeatedly since you last confirmed it."
    return " ".join(sentences)


WORD_NUMBERS = {word: number for number, word in _COUNT_WORDS.items()}
COUNT = r"(\d+|" + "|".join(WORD_NUMBERS) + ")"
DAYS_AHEAD = re.compile(r"\b" + COUNT + r" (day|week|month)s?\b", re.I)
NAMED_HORIZON = re.compile(r"(?<!each )(?<!every )(?<!per )\b(next week|fortnight|month)\b", re.I)
NAMED_HORIZON_DAYS = {"next week": 7, "fortnight": 14, "month": 30}
UNIT_DAYS = {"day": 1, "week": 7, "month": 30}
MAX_HORIZON_DAYS = 180


def _horizon_days(message):
    """How many days ahead a what's-due question looks: a count of days, weeks or months, the next week, a fortnight or month, else a week."""
    counted = DAYS_AHEAD.search(message or "")
    named = NAMED_HORIZON.search(message or "")
    if counted:
        number = counted.group(1).lower()
        days = (int(number) if number.isdigit() else WORD_NUMBERS[number]) * UNIT_DAYS[counted.group(2).lower()]
    else:
        days = NAMED_HORIZON_DAYS[named.group(1).lower()] if named else 7
    return min(max(days, 1), MAX_HORIZON_DAYS)


def _answer_upcoming(days=7, named=None):
    """Every bill occurrence, or only the named rows', from today for the next days, soonest first, as one sentence."""
    today = config.DEMO_TODAY
    bills = [bills_db.row_to_bill(r) for r in (named or bills_db.list_bills())]
    occurrences = sorted((occ for bill in bills for occ in project(bill, today, today + timedelta(days=days))), key=lambda occ: occ.date)
    span = f"the next {days} day{'s' if days != 1 else ''}"
    if not occurrences:
        return f"Nothing is due in {span}."
    items = ", ".join(f"{occ.name} on {_day_month(occ.date)} ({money.format_actual(occ.amount_cents)})" for occ in occurrences)
    return f"Coming up {'this week' if days == 7 else 'in ' + span}: {items}."


def _resolve_question(question, message=""):
    if question == "total":
        return _answer_total()
    if question == "barely_using":
        return _answer_barely_using()
    if question == "upcoming":
        return _answer_upcoming(_horizon_days(message))
    return None


def _build_preview(data):
    """The proposal in a classifier reply, or None for no op or a read; a dispute update that carries a reason and no status is a new dispute, whatever id the model put on it."""
    op, entity = data.get("op"), data.get("entity")
    if not op or not entity or op == "read":
        return None
    fields = {key: value for key, value in (data.get("fields") or {}).items() if value is not None}
    entity_id = data.get("id")
    if entity == "dispute" and op == "update" and "reason" in fields and "status" not in fields:
        op, entity_id = "create", None
        fields.setdefault("bill_id", data.get("id"))
    return {"op": op, "entity": entity, "id": entity_id, "fields": fields}


# What a bill create must carry before it is worth proposing. merchant is
# excluded: _normalise_chat_fields falls back to the name, honestly.
CREATE_REQUIRED = ["name", "amount_cents", "cadence", "next_billing_date", "type"]

# Enum fields vetted at proposal time, mirroring the DB API's validation. A
# value outside these used to become an Approve button that could only fail
# ("change rent from a bill to none" → type="none" → a doomed suggestion);
# now it becomes a rephrase request before anything is proposed.
ENUM_FIELDS = {
    "cadence": {"weekly", "fortnightly", "monthly"},
    "type": {"bill", "subscription"},
    "status": {"draft", "sent", "resolved"},  # dispute status — the only whitelisted "status"
}

METHOD_PHRASES = {"card": ("card",), "direct_debit": ("direct debit", "direct-debit", "debit"), "bpay": ("bpay",)}
TYPE_PHRASES = {"bill": ("bill",), "subscription": ("subscription", "sub", "subs")}

MISSING_FIELD_QUESTIONS = {
    "payment_method": "the payment method",
    "name": "what the bill is called",
    "amount_cents": "the amount (in dollars)",
    "cadence": "how often it bills (weekly, fortnightly or monthly)",
    "next_billing_date": "the next billing date",
    "type": "whether it's a bill or a subscription",
    "end_date": "the end date",
}

CREATE_NEEDS_REPLY = "Happy to add that — I just need {wants}. I won't guess details you haven't given me."

UNANSWERED_REPLY = "Tally couldn't answer that just now — nothing was changed."

CADENCE_PHRASES = {
    "monthly": ("monthly", "a month", "per month", "each month", "every month", "/month", "/mo"),
    "weekly": ("weekly", "a week", "per week", "each week", "every week", "/week", "/wk"),
    "fortnightly": (
        "fortnightly", "a fortnight", "per fortnight", "each fortnight", "every fortnight",
        "every two weeks", "every 2 weeks",
    ),
}

MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)

MONTH_WORDS = "|".join(MONTH_NAMES + ("sept",) + tuple(m[:3] for m in MONTH_NAMES))

DATE_SPAN = re.compile(
    rf"\d{{4}}-\d{{2}}-\d{{2}}|(?<!\d)\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?(?!\d)"
    rf"|(?<![\d$.])\d{{1,2}}(?:st|nd|rd|th)?(?: of)? (?:{MONTH_WORDS})(?![a-z])"
    rf"|(?<![a-z])(?:{MONTH_WORDS}) \d{{1,2}}(?!\d)"
)


_ADD_VERB = re.compile(r"\b(add|adds|adding|added|new bill|new subscription)\b", re.I)

CHANGE_VERB = re.compile(
    r"\b(add(?:s|ing|ed)?(?! up)|create|pause|draft|log|edit|cancel|cancels|cancelled|cancelling|end|ends|ending|stop|stops|remove|removes|change|changes"
    r"|update|updates|set|rename|delete|dispute|record|mark|move|exclude|include|raise|lower|increase|decrease|switch)\b",
    re.I,
)


DUE_WORDS = re.compile(r"\b(due|upcoming|coming up|scheduled)\b", re.I)
PAY_WORDS = re.compile(r"\b(pay|paying|owe|spend|spending)\b", re.I)


def _asks_what_is_due(message):
    """True for a question about what is due: a due word, or a time horizon together with a pay word."""
    text = message or ""
    return bool(DUE_WORDS.search(text) or ((DAYS_AHEAD.search(text) or NAMED_HORIZON.search(text)) and PAY_WORDS.search(text)))


QUESTION_START = re.compile(r"^(?:please\s+)?(?:(?:tell|show) me\s+|check\s+|list\s+)?(whats|what|which|when|how|why|is|are|was|were|does|did|has|have|any)\b", re.I)
TOTAL_WORDS = re.compile(r"\b(add(?:s|ing|ed)? up|total|altogether|combined|sum|spend|spending)\b", re.I)
BARELY_WORDS = re.compile(r"\b(barely|hardly|rarely|never|not) (?:really |ever )?(using|used|use|touch)\b|\b(unused|underused)\b", re.I)


def _is_plain_question(message):
    """True for a message that asks rather than instructs, so the model may not turn it into a proposal."""
    text = (message or "").strip()
    asking = text.endswith("?") or QUESTION_START.match(text) or TOTAL_WORDS.search(text)
    return bool(asking) and not CHANGE_VERB.search(text)


def _bills_named(text, bills):
    """The bill rows whose name or merchant the text mentions as a whole word."""
    return [row for row in bills if _mentions(text, row["name"]) or _mentions(text, row["merchant"])]


CHARGE_WORDS = re.compile(r"\b(charged?|charges|charging|debited|took|taken|actually (paid|pay)|bank)\b", re.I)
FUTURE_WORDS = re.compile(r"\b(will|next)\b", re.I)
CHARGE_WINDOW_DAYS = 90
COMPARE_TOOL = "compare_bill_with_bank_charges"


def _asks_what_was_charged(message, named):
    """True for a plain question about what one named bill's merchant charged, as opposed to what is due or coming next."""
    return len(named) == 1 and bool(CHARGE_WORDS.search(message)) and not FUTURE_WORDS.search(message) and not DUE_WORDS.search(message)


def _contradicts_an_update(preview, say):
    """True when the sentence promises a NEW bill but the op edits an existing one.

    Observed on the 3b model, 3 Sep: say "I've suggested adding Disney Plus at
    $15 a month from 5 Sep", op {"op": "update", "id": 6} -- id 6 being GymCo,
    with no name field. The user reads a new subscription; Approve rewrites an
    old one's amount, date and payment method.

    Both halves are required so the cancel beat is untouched: "I've suggested
    ending Spotify after 1 Oct" names its target, so it passes even though a
    reply like "added an end date" would trip the verb alone.
    """
    if preview["op"] != "update" or preview["entity"] != "bill":
        return False
    if not _ADD_VERB.search(say or ""):
        return False
    target = _bill_name(preview.get("id"))
    return bool(target) and target.lower() not in (say or "").lower()


def _mentions(text, name):
    """True when name appears in text as a whole word or phrase, ignoring case."""
    return bool(name) and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.I) is not None


DISPUTE_VERB = re.compile(r"\bdisput(e|es|ed|ing)\b", re.I)
REASON_SPLIT = re.compile(r",\s+|\s+because\s+|\s+-\s+|:\s+|\s+as\s+", re.I)


def _dispute_from_words(message, bills):
    """A create-dispute proposal built from the user's own words when they say dispute and name exactly one bill; the reason is the clause after the first comma, because or dash, else the whole message."""
    named = _bills_named(message, bills)
    if not DISPUTE_VERB.search(message or "") or len(named) != 1:
        return None
    parts = REASON_SPLIT.split(message.strip(), maxsplit=1)
    reason = parts[1].strip() if len(parts) == 2 and parts[1].strip() else message.strip()
    return {"op": "create", "entity": "dispute", "id": None, "fields": {"bill_id": named[0]["id"], "reason": reason}}


def _retarget_to_named_bill(preview, message, bills):
    """Point a bill update or delete at the one bill the user's own message names, since a small model sometimes says Spotify and emits Prime Video's id."""
    named = _bills_named(message, bills)
    if preview["entity"] == "bill" and preview["op"] in ("update", "delete") and len(named) == 1:
        preview["id"] = named[0]["id"]
    return preview


def _names_a_different_bill(preview, say):
    """Return (target_name, other_name) when an update's reply names another bill but not its own target, else None."""
    if preview["op"] != "update" or preview["entity"] != "bill" or not say:
        return None
    rows = bills_db.list_bills()
    target = next((row["name"] for row in rows if row["id"] == preview.get("id")), None)
    if not target or _mentions(say, target):
        return None
    other = next((row["name"] for row in rows if row["id"] != preview.get("id") and _mentions(say, row["name"])), None)
    return (target, other) if other else None


def _has_phrase(text, phrases):
    """True when any phrase appears in text with no letters glued to either end."""
    return any(re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", text) for phrase in phrases)


def _date_stated(text, days, value, need_day):
    """True when the text gives the month of an ISO date, and its day when need_day."""
    when = date.fromisoformat(str(value))
    name = MONTH_NAMES[when.month - 1]
    words = (name, name[:3]) + (("sept",) if when.month == 9 else ())
    slashed = re.findall(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", text)
    month_given = (
        _has_phrase(text, words)
        or when.isoformat() in text
        or any(int(d) == when.day and int(m) == when.month for d, m in slashed)
    )
    day_given = any(int(n) == when.day for n in days)
    return month_given and (day_given or not need_day)


def _ungrounded_fields(entity, op, fields, stated):
    """Names of the proposed values the user's own words (stated) do not support.

    Amounts are read from the text with its date spans removed, and days only
    from those spans, so a stated date cannot ground a made-up amount or the
    other way round.
    """
    if op not in ("create", "update") or entity != "bill":
        return []
    text = stated.lower().replace(",", "")
    amounts = {Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", DATE_SPAN.sub(" ", text))}
    days = re.findall(r"\d+", " ".join(DATE_SPAN.findall(text)))
    ungrounded = []
    if "amount_cents" in fields and Decimal(fields["amount_cents"]) / 100 not in amounts:
        ungrounded.append("amount_cents")
    cadence = fields.get("cadence")
    if op == "create" and cadence and not _has_phrase(text, CADENCE_PHRASES.get(cadence, ())):
        ungrounded.append("cadence")
    for key in ("next_billing_date", "end_date"):
        need_day = op == "create" and key == "next_billing_date"
        if fields.get(key) and not _date_stated(text, days, fields[key], need_day):
            ungrounded.append(key)
    if op == "update" and fields.get("payment_method") and not _has_phrase(text, METHOD_PHRASES.get(fields["payment_method"], ())):
        ungrounded.append("payment_method")
    if op == "update" and fields.get("type") and not _has_phrase(text, TYPE_PHRASES.get(fields["type"], ())):
        ungrounded.append("type")
    return ungrounded


def _changed_fields(bill_id, fields):
    """The proposed fields that differ from the bill's stored values; a field that repeats what is already saved is not a change."""
    try:
        row = bills_db.get_bill(bill_id) if bill_id else None
    except (ServiceError, requests.RequestException):
        row = None
    if not row:
        return fields
    current = dict(row, amount=row["amount_cents"] / 100)
    return {key: value for key, value in fields.items() if str(current.get(key)) != str(value)}


def _vet_proposal(preview, say="", stated=None):
    """Vet a proposal *before* it reaches the user.

    Returns (canonical_fields, None) when the proposal is appliable — fields
    normalised onto real columns, dollars already in cents — or (None, reply)
    when it is not: the reply asks for what's missing instead of presenting
    an approve button that can only fail. This is the deterministic backstop
    beneath the prompt's "ask, don't invent" instruction: even if the model
    ignores it and emits an under-specified create, no appliable proposal
    exists until the user has supplied the details.

    With stated (the user's own words since the last proposal) the amount,
    cadence and dates must also appear in what the user said; stated=None
    skips that check.
    """
    if preview["op"] == "update" and preview["entity"] == "bill":
        preview["fields"] = _changed_fields(preview.get("id"), preview.get("fields") or {})
    if preview["op"] == "update" and not preview.get("fields"):
        target = (_bill_name(preview.get("id")) if preview["entity"] == "bill" else None) or "that"
        return None, f"What would you like to change about {target}? Tell me the new amount, date or payment method and I'll propose it."
    if _contradicts_an_update(preview, say):
        return None, (
            "I need to be clearer about that one — I can add a new bill, or change an "
            "existing one, and that came out as both. Which did you mean?"
        )
    wrong_target = _names_a_different_bill(preview, say)
    if wrong_target:
        target, other = wrong_target
        return None, f"That change would apply to {target}, not {other}. Which bill did you mean?"
    try:
        fields = _normalise_chat_fields(preview["entity"], preview["op"], preview["fields"] or {})
        allowed = BILL_FIELD_WHITELIST[preview["entity"]]
        for key in fields:
            if key not in allowed:
                raise ServiceError(f"field '{key}' cannot be set via chat")
        for key in ("next_billing_date", "end_date", "date"):
            if fields.get(key):
                date.fromisoformat(str(fields[key]))
        for key, allowed_values in ENUM_FIELDS.items():
            if key in fields and fields[key] not in allowed_values:
                raise ServiceError(f"{key} must be one of {sorted(allowed_values)}")
    except ServiceError as error:
        return None, f"Tally couldn't turn that into a change ({error.message}) — try rephrasing."
    except ValueError:
        return None, "Tally needs dates as a real calendar date (like 5 September) — try rephrasing."
    if preview["entity"] == "bill" and preview["op"] == "create":
        missing = [f for f in CREATE_REQUIRED if not fields.get(f)]
        if missing:
            wants = ", ".join(MISSING_FIELD_QUESTIONS[f] for f in missing)
            return None, CREATE_NEEDS_REPLY.format(wants=wants)
    if preview["op"] == "create" and stated is not None and fields.get("payment_method") and not _has_phrase(stated.lower(), METHOD_PHRASES.get(fields["payment_method"], ())):
        fields.pop("payment_method")
    ungrounded = [] if stated is None else _ungrounded_fields(preview["entity"], preview["op"], fields, stated)
    if ungrounded:
        wants = ", ".join(MISSING_FIELD_QUESTIONS[f] for f in ungrounded)
        if preview["op"] == "create":
            return None, CREATE_NEEDS_REPLY.format(wants=wants)
        target = (_bill_name(preview.get("id")) if preview["entity"] == "bill" else None) or "that"
        return None, f"I'd need you to state {wants} before I change {target}. I won't guess details you haven't given me."
    return fields, None


def send_message(message):
    if not message:
        raise ServiceError("message is required")
    history = _recent_history()
    bills_db.create_chat_message({"role": "user", "content": message})
    return _model_turn(message, history, stated=_stated_text(message, history))


# The Observe→Adapt half of the loop. Sent to the model as the current turn
# (never stored as a user message): the transcript it reads already ends with
# the "[suggestion #N rejected …]" outcome note, so this just tells it what a
# good next move looks like.
ADAPT_NUDGE = (
    "The user rejected your last suggestion — it was NOT applied. Adapt: ask briefly what "
    "they'd like instead, or propose a corrected change if their earlier messages make the "
    "fix obvious. Never repeat the rejected proposal unchanged."
)

ADAPT_FALLBACK = {
    "op": None, "entity": None, "id": None, "fields": None, "question": "none",
    "say": "Noted — I won't make that change. Tell me what you'd like instead.",
}


def adapt_after_rejection():
    """One extra model turn straight after a rejection, so the loop closes
    without waiting for the user to speak: the model sees the rejection note
    in its history and either asks what to change or proposes a corrected
    suggestion (which lands as a fresh pending row via the same vetting)."""
    history = _recent_history()
    return _model_turn(ADAPT_NUDGE, history, fallback=ADAPT_FALLBACK, stated=_stated_text("", history), grounded=False)


def _grounded_answer(message):
    """The bills-corpus answer card for a plain question, or None when a mode is off or the MCP call fails, so the chat never breaks on retrieval."""
    if not (config.MCP_ENABLED and config.RAG_ENABLED):
        return None
    try:
        return evidence_service.ask(message)
    except (ModeError, ServiceError):
        return None


def _bank_charges(bill):
    """Answer what a bill's merchant charged over the last CHARGE_WINDOW_DAYS from the MCP compare tool: (sentence, facts for the chips), or None when MCP is off or fails."""
    if not config.MCP_ENABLED:
        return None
    today = config.DEMO_TODAY
    arguments = {"bill_id": bill["id"], "start_date": (today - timedelta(days=CHARGE_WINDOW_DAYS)).isoformat(), "end_date": today.isoformat()}
    try:
        data, duration_ms = tools_service.call_allowed_tool(COMPARE_TOOL, arguments)
    except ServiceError:
        return None
    charges = data.get("charges") or []
    rows = []
    for charge in charges:
        when = _day_month(date.fromisoformat(charge["date"]))
        delta = charge.get("differs_from_bill_cents") or 0
        note = f"{'+' if delta > 0 else '-'}{money.format_actual(abs(delta))} vs bill" if delta else ""
        rows.append({"date": when, "amount": money.format_actual(charge["amount_cents"]), "note": note, "delta": delta})
    merchant = bill["merchant"]
    if not rows:
        sentence = f"No bank charges from {merchant} in the last {CHARGE_WINDOW_DAYS} days."
    else:
        sentence = f"{merchant} charged you {_count_word(len(rows))} time{'' if len(rows) == 1 else 's'} in the last {CHARGE_WINDOW_DAYS} days"
        odd = next((r for r in rows if r["delta"]), None)
        if odd:
            direction = "above" if odd["delta"] > 0 else "below"
            sentence += f"; the {odd['date']} charge was {money.format_actual(abs(odd['delta']))} {direction} your {money.format_actual(bill['amount_cents'])} bill."
        else:
            sentence += f", each at your {money.format_actual(bill['amount_cents'])} bill amount."
    return {"sentence": sentence, "tool": COMPARE_TOOL, "rows": rows, "duration_ms": duration_ms}


def _warm_in_background(model):
    """Start loading a model on a daemon thread; a dispute proposal uses it so Approve does not wait for the draft model to swap in."""
    threading.Thread(target=ollama_client.warm, args=(model,), daemon=True).start()


def _model_turn(model_message, history, fallback=None, stated=None, grounded=True):
    """One classifier turn; a plain question (no proposal, no code-computed answer) is then answered from the bills corpus when grounded."""
    bills = bills_db.list_bills()
    data = guard.run(
        config.CHAT_MODEL,
        lambda error: chat_prompt.build(model_message, history, bills=bills, error=error),
        validate_chat_response,
        fallback or chat_prompt.FALLBACK,
    )

    agrees = {"upcoming": _asks_what_is_due(model_message), "total": bool(TOTAL_WORDS.search(model_message)), "barely_using": bool(BARELY_WORDS.search(model_message))}
    if data.get("question") in agrees and not agrees[data.get("question")]:
        data["question"] = "none"
    answered = _resolve_question(data.get("question"), model_message)
    route = data.get("question") if answered else "plain"
    reply = answered or data.get("say", "")
    asks = grounded and _is_plain_question(model_message)
    from_words = None if asks else _dispute_from_words(model_message, bills)
    preview = None if asks else (from_words or _build_preview(data))
    if from_words:
        reply = f"I've suggested opening a dispute for {_bill_name(from_words['fields']['bill_id'])} — approve it to draft the letter."
    if preview:
        preview = _retarget_to_named_bill(preview, model_message, bills)
    named = _bills_named(model_message, bills) if asks else []
    about_a_bill = len(named) == 1
    card = None
    facts = _bank_charges(named[0]) if asks and _asks_what_was_charged(model_message, named) else None
    if facts:
        route = "tool"
        reply = facts["sentence"]
    elif asks and BARELY_WORDS.search(model_message):
        reply = _answer_barely_using()
        route = "barely_using"
    elif asks and not about_a_bill and _asks_what_is_due(model_message):
        reply = _answer_upcoming(_horizon_days(model_message), named)
        route = "upcoming"
    elif asks and not about_a_bill and TOTAL_WORDS.search(model_message):
        reply = _answer_total(named)
        route = "total"
    elif asks and not preview and (data.get("question") in (None, "none") or about_a_bill):
        card = _grounded_answer(model_message)
        if card:
            reply = card["answer"]
            route = "grounded"
    if not preview and not asks and CHANGE_VERB.search(model_message or ""):
        route = "ask_back"
    if asks and route == "plain" and _build_preview(data) is not None:
        reply = UNANSWERED_REPLY
    canonical_fields = None
    if preview:
        canonical_fields, reply_override = _vet_proposal(preview, reply, stated=stated)
        if reply_override:
            reply = reply_override
            preview = None
            route = "ask_back"

    if preview:
        route = "proposal"
    assistant_row = bills_db.create_chat_message(
        {"role": "assistant", "content": reply, "op_json": json.dumps(preview) if preview else None}
    )
    if preview:
        preview["message_id"] = assistant_row["id"]
        # The proposal becomes a pending suggestion the moment it exists, so
        # the same row backs the chat card and the Suggestions panel, and
        # approving in either place resolves both.
        suggestion = bills_db.create_suggestion(
            {
                "op": preview["op"],
                "entity": preview["entity"],
                "entity_id": preview.get("id"),
                "payload_json": json.dumps(canonical_fields or {}),
                "message_id": assistant_row["id"],
            }
        )
        preview["suggestion_id"] = suggestion["id"]
        if (preview["op"], preview["entity"]) == ("create", "dispute"):
            try:
                _warm_in_background(config.DRAFT_MODEL)
            except Exception:
                pass
    return {
        "reply": reply,
        "op": preview["op"] if preview else None,
        "preview": preview,
        "fallback": bool(data.get("fallback", False)) and card is None and facts is None,
        "grounded": card,
        "tool": facts,
        "route": route,
    }


def _normalise_chat_fields(entity, op, fields):
    """Translate the model's field names onto the real columns.

    Runs before the whitelist check, so an alias is accepted and anything still
    unrecognised afterwards is rejected exactly as before.
    """
    aliases = CHAT_FIELD_ALIASES.get(entity, {})
    out = {}
    for key, value in fields.items():
        if key in RAW_KEY_BLOCKLIST:
            raise ServiceError(f"field '{key}' cannot be set via chat — state the amount in dollars")
        target = aliases.get(key, key)
        if key in DOLLAR_VALUED_ALIASES:
            try:
                value = money.parse_dollars_to_cents(value)
            except ValueError:
                raise ServiceError(f"'{key}' must be an amount, got {value!r}")
        if target in out and out[target] != value:
            raise ServiceError(f"conflicting values for '{target}'")
        out[target] = value

    # A bill needs a merchant and the request rarely names one separately --
    # "add Disney Plus" gives the name and nothing else. Falling back to the
    # name keeps the row valid and honest: it says what the user actually told
    # us rather than inventing a trading entity.
    if entity == "bill" and op == "create" and not out.get("merchant") and out.get("name"):
        out["merchant"] = out["name"]
    return out


def _execute(op, entity, entity_id, clean_fields):
    """Perform an already-normalised, whitelisted change.

    Bill and payment writes go through the services layer — the same
    validation and status-sync a manual edit gets — never the raw DB client.
    The DB API checks enums and integer types but not date *format*: a chat
    update that stored next_billing_date="early September" used to 500 every
    bills read from then on. services/bills._clean_payload is what refuses it.
    """
    if entity == "bill" and op == "update":
        return bills_service.update_bill(entity_id, clean_fields)
    if entity == "bill" and op == "create":
        return bills_service.create_bill({**clean_fields, "source": "chat"})
    if entity == "bill" and op == "delete":
        return bills_service.delete_bill(entity_id)
    if entity == "payment" and op == "create":
        return payments_service.create_payment(clean_fields)
    if entity == "payment" and op == "delete":
        return payments_service.delete_payment(entity_id)
    if entity == "dispute" and op == "update":
        return bills_db.update_dispute(entity_id, clean_fields)
    if entity == "dispute" and op == "create":
        bill_row = bills_db.get_bill(clean_fields.get("bill_id"))
        if bill_row is None:
            raise NotFound("bill not found")
        reason = clean_fields.get("reason", "")
        result = bills_db.create_dispute({"bill_id": bill_row["id"], "reason": reason, "opened_at": config.DEMO_TODAY.isoformat()})
        draft = disputes_service.draft_for_bill(bill_row, reason)
        bills_db.create_dispute_draft(
            result["id"],
            {"letter_text": draft["letter_text"], "steps_json": disputes_service.steps_json(draft)},
        )
        result["draft"] = draft
        return result
    raise ServiceError(f"unsupported op '{op}' for entity '{entity}'")


def _bill_name(entity_id):
    row = bills_db.get_bill(entity_id) if entity_id else None
    return row["name"] if row else None


def _change_summary(op, entity, entity_id, fields, bill_name=None):
    """One honest sentence describing a change, for the chat transcript the
    model reads back. Names beat ids: 'deleted bill 4' tells the model less
    than 'deleted Netflix'."""
    label = f"{entity} {entity_id}" if entity_id else entity
    if entity == "bill" and bill_name:
        label = f"bill '{bill_name}'"
    elif entity == "bill" and fields.get("name"):
        label = f"bill '{fields['name']}'"
    if op == "create":
        detail = ", ".join(f"{k}={v}" for k, v in fields.items() if k != "name")
        return f"added {label}" + (f" ({detail})" if detail else "")
    if op == "update":
        detail = ", ".join(f"{k} → {v}" for k, v in fields.items())
        return f"updated {label}" + (f" ({detail})" if detail else "")
    return f"deleted {label}"


def _record_outcome(content, applied=False):
    bills_db.create_chat_message({"role": "assistant", "content": content, "applied": applied})


def apply(op, entity, entity_id, fields, message_id=None):
    """Direct apply for /api/chat/apply and /ui/chat/apply (raw model-shaped
    fields, dollars under 'amount'). The suggestions flow uses approve_suggestion
    below; both record an honest outcome the model sees on its next turn."""
    fields = fields or {}
    if entity not in BILL_FIELD_WHITELIST:
        raise ServiceError("unknown entity")
    fields = _normalise_chat_fields(entity, op, fields)
    allowed = BILL_FIELD_WHITELIST[entity]
    for key in fields:
        if key not in allowed:
            raise ServiceError(f"field '{key}' cannot be set via chat")
    clean_fields = dict(fields)

    bill_name = _bill_name(entity_id) if entity == "bill" and op in ("update", "delete") else None
    try:
        result = _execute(op, entity, entity_id, clean_fields)
    except ServiceError as error:
        # A failed change used to vanish from the transcript: the user saw an
        # error fragment, the model saw nothing, and its next turn happily
        # repeated that the change was made. The failure is now on the record.
        _record_outcome(f"[change failed: {error.message} — nothing was changed]")
        raise

    if message_id:
        bills_db.update_chat_message(message_id, {"applied": 1})
    summary = _change_summary(op, entity, entity_id, clean_fields, bill_name)
    _record_outcome(f"[change applied: {summary}]", applied=True)
    return result


def approve_suggestion(suggestion_id):
    """Apply a pending suggestion. Claims the row first (an atomic
    pending→applied transition in the DB), so two racing approves — or a
    double-clicked button — cannot both execute the change."""
    row = bills_db.get_suggestion(suggestion_id)
    if row is None:
        raise NotFound("suggestion not found")
    bills_db.update_suggestion(suggestion_id, {"status": "applied"})

    fields = json.loads(row["payload_json"]) if row["payload_json"] else {}
    bill_name = _bill_name(row["entity_id"]) if row["entity"] == "bill" and row["op"] in ("update", "delete") else None
    try:
        result = _execute(row["op"], row["entity"], row["entity_id"], fields)
    except ServiceError as error:
        bills_db.update_suggestion(suggestion_id, {"status": "failed", "error": error.message})
        _record_outcome(
            f"[suggestion #{suggestion_id} FAILED to apply: {error.message} — nothing was changed]"
        )
        raise
    if row["message_id"]:
        bills_db.update_chat_message(row["message_id"], {"applied": 1})
    summary = _change_summary(row["op"], row["entity"], row["entity_id"], fields, bill_name)
    _record_outcome(f"[suggestion #{suggestion_id} approved and applied: {summary}]", applied=True)
    return result


def reject_suggestion(suggestion_id):
    """Reject a pending suggestion (or dismiss a failed one). Recorded in the
    transcript so the model's next turn knows the change was NOT made."""
    row = bills_db.get_suggestion(suggestion_id)
    if row is None:
        raise NotFound("suggestion not found")
    updated = bills_db.update_suggestion(suggestion_id, {"status": "rejected"})
    fields = json.loads(row["payload_json"]) if row["payload_json"] else {}
    bill_name = _bill_name(row["entity_id"]) if row["entity"] == "bill" else None
    summary = _change_summary(row["op"], row["entity"], row["entity_id"], fields, bill_name)
    _record_outcome(f"[suggestion #{suggestion_id} rejected by the user — NOT applied: {summary}]")
    return updated
