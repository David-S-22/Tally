"""Parse one safe transaction operation from Ollama."""

import json
import math
import re
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

import requests

from .. import config
from .chat_service import ChatError


KEYS = {
    "operation", "transaction_id", "fields", "filters", "calculation",
    "handoff", "reply",
}
PLAN_INTERNAL_KEYS = {"planning_error", "retryable"}
OPERATIONS = {"create", "read", "update", "delete"}
CALCULATIONS = {
    "none", "count", "sum", "average", "largest", "smallest",
}
HANDOFFS = {"none"}
FIELDS = {
    "date", "merchant", "description", "amount", "category", "category_id",
}
FILTERS = {
    "search_text", "date", "dates", "date_from", "date_to", "since", "merchant",
    "category", "category_id", "min_amount", "max_amount",
}
DATE_FILTERS = {"date", "dates", "date_from", "date_to", "since"}
DATE_INTENT = re.compile(
    r"\b(?:"
    r"today|tonight|yesterday|tomorrow|"
    r"(?:this|last|previous|past|next)\s+"
    r"(?:day|week|fortnight|month|quarter|year)s?|"
    r"(?:last|past|next)\s+\d+\s+"
    r"(?:day|week|fortnight|month|quarter|year)s?|"
    r"since|before|after|between|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r")\b|"
    r"\b(?:19|20)\d{2}(?:-\d{1,2}(?:-\d{1,2})?)?\b|"
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
    re.IGNORECASE,
)
LIST_INTENT = re.compile(
    r"\b(?:list|show|find)\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?"
    r"(?:purchases|transactions|expenses)\b",
    re.IGNORECASE,
)
LARGEST_INTENT = re.compile(
    r"\b(?:biggest|largest|highest|most expensive|priciest)\b",
    re.IGNORECASE,
)
SMALLEST_INTENT = re.compile(
    r"\b(?:smallest|cheapest|lowest|least expensive)\b",
    re.IGNORECASE,
)
RANKING_INTENT = re.compile(
    rf"{LARGEST_INTENT.pattern}|{SMALLEST_INTENT.pattern}",
    re.IGNORECASE,
)
AMOUNT_BETWEEN = re.compile(
    r"\bbetween\s+\$?([0-9][0-9,]*(?:\.\d{1,2})?)\s+and\s+"
    r"\$?([0-9][0-9,]*(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
MIN_AMOUNT = re.compile(
    r"\b(at least|more than|greater than|over|above)\s+"
    r"\$?([0-9][0-9,]*(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
MAX_AMOUNT = re.compile(
    r"\b(at most|less than|under|below)\s+"
    r"\$?([0-9][0-9,]*(?:\.\d{1,2})?)\b",
    re.IGNORECASE,
)
CURRENCY_AMOUNT = re.compile(
    r"(?<![a-z0-9])(?:aud\s*)?\$\s*"
    r"(-?[0-9][0-9,]*(?:\.\d{1,2})?)",
    re.IGNORECASE,
)
EXPLICIT_DESCRIPTION = re.compile(
    r"\bdescription\s*(?:is|:|=)\s*"
    r"(?:\"(?P<double>[^\"]+)\"|'(?P<single>[^']+)'|"
    r"(?P<plain>[^,;\n.]+))",
    re.IGNORECASE,
)
REQUESTED_CHANGE = re.compile(
    r"^\s*Requested change:\s*",
    re.IGNORECASE | re.MULTILINE,
)
MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sept": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
DAY_MONTH = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+"
    rf"(?P<month>{MONTH_NAMES})(?:\s+(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
MONTH_DAY = re.compile(
    rf"\b(?P<month>{MONTH_NAMES})\s+"
    rf"(?P<day>\d{{1,2}})(?:st|nd|rd|th)?"
    rf"(?:,?\s+(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
MONTH_ONLY = re.compile(
    rf"\b(?P<month>{MONTH_NAMES})(?:\s+(?P<year>\d{{4}}))?\b",
    re.IGNORECASE,
)
DATE_RANGE_INTENT = re.compile(
    r"\bbetween\b|\bfrom\b.+\b(?:to|through|until)\b",
    re.IGNORECASE,
)
CATEGORY_ALIASES = {
    "Dining": re.compile(
        r"\b(?:eating|dining)\s+out\b|\brestaurants?\b|"
        r"\bcaf(?:e|é)s?\b|\bcoffee\s+shops?\b",
        re.IGNORECASE,
    ),
    "Groceries": re.compile(
        r"\bgrocer(?:y|ies)\b|\bsupermarkets?\b",
        re.IGNORECASE,
    ),
}
FALLBACK = {
    "operation": "read",
    "transaction_id": None,
    "fields": {},
    "filters": {},
    "calculation": "none",
    "handoff": "none",
    "reply": "I could not safely understand that request. No changes were made.",
}


def create_plan(message, categories, previous_observation=None):
    if not config.AI_MODE_ENABLED:
        raise ChatError("AI mode is disabled.", "ai_disabled", 503)
    try:
        response = requests.post(
            f"{config.OLLAMA_URL}/api/chat",
            json={
                "model": config.CHAT_MODEL,
                "messages": build_messages(
                    message,
                    categories,
                    previous_observation,
                ),
                "format": "json",
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=config.AI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = json.loads(response.json()["message"]["content"])
    except requests.RequestException:
        return {
            **FALLBACK,
            "reply": (
                "Tally could not reach the AI service. "
                "No changes were made."
            ),
            "fallback": True,
            "planning_error": "planner_unavailable",
            "retryable": False,
        }
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        return {
            **FALLBACK,
            "fallback": True,
            "planning_error": str(error) or "invalid planner response",
            "retryable": True,
        }

    error = validate_chat_response(data, categories)
    if error is None:
        data = normalize_plan(data, message, categories)
        error = semantic_validation_error(data, message, categories)
    if error is not None:
        return {
            **FALLBACK,
            "fallback": True,
            "planning_error": error,
            "retryable": True,
        }
    return {
        **data,
        "fallback": False,
        "planning_error": None,
        "retryable": False,
    }


def parse_chat(message, transactions, categories):
    previous_observation = None
    result = None
    for _attempt in range(2):
        result = create_plan(message, categories, previous_observation)
        if not result["fallback"] or not result["retryable"]:
            return public_plan(result)
        previous_observation = {
            "status": "failed",
            "error": {
                "code": "invalid_plan",
                "message": result["planning_error"],
            },
        }
    return public_plan(result or {**FALLBACK, "fallback": True})


def public_plan(plan):
    return {
        key: value
        for key, value in plan.items()
        if key not in PLAN_INTERNAL_KEYS
    }


def validate_chat_response(data, categories=()):
    if not isinstance(data, dict):
        return "response must be a JSON object"
    if missing := sorted(KEYS - set(data)):
        return f"missing keys: {', '.join(missing)}"
    if unknown := sorted(set(data) - KEYS):
        return f"unsupported keys: {', '.join(unknown)}"

    operation = data["operation"]
    transaction_id = data["transaction_id"]
    fields = data["fields"]
    filters = data["filters"]
    calculation = data["calculation"]
    if operation not in OPERATIONS:
        return f"operation must be one of {sorted(OPERATIONS)}"
    if transaction_id is not None and not is_positive_id(transaction_id):
        return "transaction_id must be a positive integer or null"
    if not isinstance(fields, dict) or not isinstance(filters, dict):
        return "fields and filters must be JSON objects"
    if unknown := sorted(set(fields) - FIELDS):
        return f"unsupported fields: {', '.join(unknown)}"
    if unknown := sorted(set(filters) - FILTERS):
        return f"unsupported filters: {', '.join(unknown)}"
    if is_invalid_calculation(calculation):
        return (
            "calculation must be count, sum, average, largest, "
            "smallest, none, or a unique list"
        )
    if data["handoff"] not in HANDOFFS:
        return f"handoff must be one of {sorted(HANDOFFS)}"
    if not is_one_line(data["reply"], 300):
        return "reply must be one non-empty line of at most 300 characters"

    for source, keys in (
        (fields, {"date", "merchant", "description", "category"}),
        (
            filters,
            {
                "search_text", "date", "date_from", "date_to", "since", "merchant",
                "category",
            },
        ),
    ):
        if any(
            value is not None and not is_one_line(value)
            for key in keys
            if (value := source.get(key)) is not None
        ):
            return "text fields must be non-empty strings"
    for source, keys in (
        (fields, {"amount"}),
        (filters, {"min_amount", "max_amount"}),
    ):
        if any(
            value is not None and not is_number(value)
            for key in keys
            if (value := source.get(key)) is not None
        ):
            return "amount fields must be finite numbers"
    for source in (fields, filters):
        value = source.get("category_id")
        if value is not None and not is_positive_id(value):
            return "category_id must be a positive integer"
    dates = filters.get("dates")
    if dates is not None and (
        not isinstance(dates, list)
        or not dates
        or len(dates) > 31
        or any(not is_iso_date(value) for value in dates)
        or len(dates) != len(set(dates))
    ):
        return "dates must be a unique list of 1 to 31 ISO dates"
    for key in ("date", "date_from", "date_to", "since"):
        value = filters.get(key)
        if value is not None and not is_iso_date(value):
            return f"{key} must be an ISO date"
    if fields.get("date") is not None and not is_iso_date(fields["date"]):
        return "date must be an ISO date"
    if error := category_validation_error(
        operation,
        fields,
        filters,
        categories,
    ):
        return error
    exact_date_filters = (
        filters.get("date") is not None,
        filters.get("dates") is not None,
    )
    if sum(exact_date_filters) > 1 or (
        any(exact_date_filters)
        and any(
            filters.get(key) is not None
            for key in ("date_from", "date_to", "since")
        )
    ):
        return (
            "date or dates cannot be combined with date_from, date_to, "
            "or since"
        )

    if operation == "create" and transaction_id is not None:
        return "create must not include transaction_id"
    if operation == "update" and (
        not fields or (transaction_id is None and not filters)
    ):
        return "update requires fields and transaction_id or filters"
    if operation == "delete":
        if fields:
            return "delete fields must be empty"
        if transaction_id is None and not filters:
            return "delete requires transaction_id or filters"
    return None


def build_messages(message, categories, previous_observation):
    prompt = (
        Path(__file__).resolve().parent.parent
        / "prompts"
        / "chat_prompt.txt"
    ).read_text(encoding="utf-8").strip()
    today = date.today()
    last_week_end = today - timedelta(days=today.weekday() + 1)
    last_week_start = last_week_end - timedelta(days=6)
    august_year = today.year if today.month >= 8 else today.year - 1
    august_start = date(august_year, 8, 1)
    august_end = date(
        august_year,
        8,
        monthrange(august_year, 8)[1],
    )
    june_year = today.year if (today.month, today.day) >= (6, 10) else today.year - 1
    june_tenth = date(june_year, 6, 10)
    july_year = today.year if (today.month, today.day) >= (7, 15) else today.year - 1
    july_fifteenth = date(july_year, 7, 15)
    context = (
        f"{prompt}\n\nToday: {today.isoformat()}."
        f"\nPrevious calendar week: {last_week_start.isoformat()} "
        f"to {last_week_end.isoformat()}."
        f"\nCategories: {json.dumps(categories, separators=(',', ':'))}"
    )
    examples = [
        (
            "What did I spend at Woolworths in August?",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {
                    "date_from": august_start.isoformat(),
                    "date_to": august_end.isoformat(),
                    "merchant": "Woolworths",
                },
                "calculation": "sum",
                "handoff": "none",
                "reply": "I will total your August Woolworths transactions.",
            },
        ),
        (
            "Show my biggest purchases in August",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {
                    "date_from": august_start.isoformat(),
                    "date_to": august_end.isoformat(),
                },
                "calculation": "largest",
                "handoff": "none",
                "reply": "I will rank your August purchases by amount.",
            },
        ),
        (
            "What is the smallest purchase I made in August?",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {
                    "date_from": august_start.isoformat(),
                    "date_to": august_end.isoformat(),
                },
                "calculation": "smallest",
                "handoff": "none",
                "reply": "I will find your cheapest August purchases.",
            },
        ),
        (
            "How much did eating out cost me last week?",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {
                    "date_from": last_week_start.isoformat(),
                    "date_to": last_week_end.isoformat(),
                    "category": "Dining",
                },
                "calculation": "sum",
                "handoff": "none",
                "reply": "I will total your Dining transactions from last week.",
            },
        ),
        (
            "List all purchases spent on the 10th June",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {"date": june_tenth.isoformat()},
                "calculation": "none",
                "handoff": "none",
                "reply": "I will list every purchase from 10 June.",
            },
        ),
        (
            "List all purchases spent on the 10th June and the 15th July",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {
                    "dates": [
                        june_tenth.isoformat(),
                        july_fifteenth.isoformat(),
                    ],
                },
                "calculation": "none",
                "handoff": "none",
                "reply": "I will list purchases from both requested dates.",
            },
        ),
        (
            "List all purchases spent with Anytime Fitness",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {"search_text": "Anytime Fitness"},
                "calculation": "none",
                "handoff": "none",
                "reply": "I will find merchant or description matches.",
            },
        ),
        (
            "List all purchases over $100",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {"min_amount": 100.01},
                "calculation": "none",
                "handoff": "none",
                "reply": "I will list purchases over $100.",
            },
        ),
        (
            "How many Dining purchases are there?",
            {
                "operation": "read",
                "transaction_id": None,
                "fields": {},
                "filters": {"category": "Dining"},
                "calculation": "count",
                "handoff": "none",
                "reply": "I will count Dining purchases.",
            },
        ),
    ]
    messages = [
        {"role": "system", "content": context},
    ]
    for example_message, example_response in examples:
        messages.extend([
            {"role": "user", "content": example_message},
            {
                "role": "assistant",
                "content": json.dumps(
                    example_response,
                    separators=(",", ":"),
                ),
            },
        ])
    messages.append({"role": "user", "content": message})
    if previous_observation:
        messages.append({
            "role": "user",
            "content": (
                "The previous plan was rejected by trusted application code: "
                f"{json.dumps(previous_observation, separators=(',', ':'))}. "
                "Return corrected JSON only."
            ),
        })
    return messages


def category_validation_error(operation, fields, filters, categories):
    ids = {item["id"] for item in categories}
    names = {item["name"].casefold() for item in categories}
    for source_name, source in (("fields", fields), ("filters", filters)):
        if operation == "create" and source_name == "fields":
            continue
        if source.get("category_id") is not None and source["category_id"] not in ids:
            return "category_id must match an available category"
        if source.get("category") is not None and source["category"].strip().casefold() not in names:
            return "category must match an available category"
    return None


def normalize_plan(data, message, categories):
    if data["operation"] == "read":
        return normalize_read_query(data, message, categories)
    if data["operation"] == "create":
        return normalize_create_plan(data, message, categories)
    return data


def normalize_create_plan(data, message, categories):
    normalized = {
        **data,
        "fields": dict(data["fields"]),
    }
    fields = normalized["fields"]
    for key in tuple(fields):
        if fields[key] is None:
            fields.pop(key)
    for key in ("merchant", "description"):
        value = fields.get(key)
        if value is not None and not text_value_is_grounded(value, message):
            fields.pop(key)
    value = fields.get("date")
    if value is not None and not date_value_is_grounded(value, message):
        fields.pop("date")
    grounded = grounded_dates(message)
    if len(grounded) == 1:
        fields["date"] = next(iter(grounded))

    description = explicit_description(message)
    if description is not None:
        fields["description"] = description

    value = fields.get("amount")
    if value is not None and not amount_value_is_grounded(value, message):
        fields.pop("amount")
    if "amount" not in fields:
        amount = explicit_currency_amount(message)
        if amount is not None:
            fields["amount"] = amount

    category = explicit_category_in_message(message, categories)
    if category is not None:
        fields.pop("category_id", None)
        fields["category"] = category
    elif category := category_in_message(message, categories):
        fields.pop("category_id", None)
        fields["category"] = category
    return normalized


def normalize_read_query(data, message, categories):
    if data["operation"] != "read":
        return data

    normalized = {
        **data,
        "filters": dict(data["filters"]),
    }
    if LIST_INTENT.search(message) and not RANKING_INTENT.search(message):
        normalized["calculation"] = "none"
    normalized["calculation"] = correct_ranking_direction(
        normalized["calculation"],
        message,
    )

    date_filters = extract_date_filters(message)
    if date_filters:
        for key in DATE_FILTERS:
            normalized["filters"].pop(key, None)
        normalized["filters"].update(date_filters)

    amount_filters = extract_amount_filters(message)
    for key in ("min_amount", "max_amount"):
        normalized["filters"].pop(key, None)
    normalized["filters"].update(amount_filters)

    category = category_in_message(message, categories)
    if category is not None:
        normalized["filters"].pop("category_id", None)
        normalized["filters"]["category"] = category
    else:
        remove_unmentioned_category(
            normalized["filters"],
            message,
            categories,
        )
    return normalized


def correct_ranking_direction(calculation, message):
    """Rank from the end of the range the user actually asked for.

    The planner answers "cheapest" with "largest" often enough to matter,
    especially on a replan. The wording is unambiguous, so the message
    wins over the model.
    """
    calculations = (
        [calculation] if isinstance(calculation, str) else list(calculation)
    )
    if not any(item in {"largest", "smallest"} for item in calculations):
        return calculation

    wants_smallest = SMALLEST_INTENT.search(message) is not None
    wants_largest = LARGEST_INTENT.search(message) is not None
    if wants_smallest == wants_largest:
        return calculation

    wanted = "smallest" if wants_smallest else "largest"
    corrected = []
    for item in calculations:
        item = wanted if item in {"largest", "smallest"} else item
        if item not in corrected:
            corrected.append(item)
    return corrected[0] if isinstance(calculation, str) else corrected


def extract_date_filters(message):
    matches = []
    day_spans = []
    for pattern in (DAY_MONTH, MONTH_DAY):
        for match in pattern.finditer(message):
            day_spans.append(match.span())
            month = MONTHS[match.group("month").casefold()]
            day = int(match.group("day"))
            year = match.group("year")
            try:
                value = resolve_date(month, day, year)
            except ValueError:
                continue
            matches.append((match.start(), value))
    if not matches:
        return extract_month_range(message, day_spans)
    dates = []
    for _position, value in sorted(matches):
        iso_date = value.isoformat()
        if iso_date not in dates:
            dates.append(iso_date)
    if not dates:
        return {}
    if len(dates) > 1:
        if DATE_RANGE_INTENT.search(message):
            return {
                "date_from": min(dates),
                "date_to": max(dates),
            }
        return {"dates": dates}

    value = dates[0]
    if re.search(r"\bsince\b", message, re.IGNORECASE):
        return {"since": value}
    if re.search(r"\b(?:after|from)\b", message, re.IGNORECASE):
        return {"date_from": value}
    if re.search(r"\b(?:before|until)\b", message, re.IGNORECASE):
        return {"date_to": value}
    return {"date": value}


def extract_month_range(message, day_spans):
    """Return the first bare month name as a whole-month range.

    A month with no day ("in August") matches neither DAY_MONTH nor
    MONTH_DAY, so without this the period is left to the planner.
    ``day_spans`` are the day-and-month matches already consumed, so
    "10 June" is never also read as the whole of June.
    """
    for match in MONTH_ONLY.finditer(message):
        if any(
            match.start() < end and start < match.end()
            for start, end in day_spans
        ):
            continue
        month = MONTHS[match.group("month").casefold()]
        start = resolve_month_start(month, match.group("year"))
        last_day = monthrange(start.year, month)[1]
        return {
            "date_from": start.isoformat(),
            "date_to": date(start.year, month, last_day).isoformat(),
        }
    return {}


def resolve_month_start(month, year):
    if year is not None:
        return date(int(year), month, 1)
    today = date.today()
    value = date(today.year, month, 1)
    return value if value <= today else date(today.year - 1, month, 1)


def resolve_date(month, day, year):
    if year is not None:
        return date(int(year), month, day)
    today = date.today()
    value = date(today.year, month, day)
    return value if value <= today else date(today.year - 1, month, day)


def extract_amount_filters(message):
    if match := AMOUNT_BETWEEN.search(message):
        first = parse_amount(match.group(1))
        second = parse_amount(match.group(2))
        return {
            "min_amount": min(first, second),
            "max_amount": max(first, second),
        }

    filters = {}
    if match := MIN_AMOUNT.search(message):
        amount = parse_amount(match.group(2))
        filters["min_amount"] = (
            amount
            if match.group(1).casefold() == "at least"
            else round(amount + 0.01, 2)
        )
    if match := MAX_AMOUNT.search(message):
        amount = parse_amount(match.group(2))
        filters["max_amount"] = (
            amount
            if match.group(1).casefold() == "at most"
            else round(amount - 0.01, 2)
        )
    return filters


def parse_amount(value):
    return float(value.replace(",", ""))


def explicit_currency_amount(message):
    for segment in reversed(conversation_segments(message)):
        amounts = {
            parse_amount(match.group(1))
            for match in CURRENCY_AMOUNT.finditer(segment)
        }
        if amounts:
            return next(iter(amounts)) if len(amounts) == 1 else None
    return None


def explicit_description(message):
    matches = list(EXPLICIT_DESCRIPTION.finditer(message))
    if not matches:
        return None
    match = matches[-1]
    value = next(
        (
            group
            for group in (
                match.group("double"),
                match.group("single"),
                match.group("plain"),
            )
            if group is not None
        ),
        "",
    ).strip()
    return value or None


def category_in_message(message, categories):
    for segment in reversed(conversation_segments(message)):
        category_names = {item["name"]: item["name"] for item in categories}
        for name, pattern in CATEGORY_ALIASES.items():
            if name in category_names and pattern.search(segment):
                return name
        for category in sorted(
            categories,
            key=lambda item: len(item["name"]),
            reverse=True,
        ):
            name = re.escape(category["name"])
            patterns = (
                rf"\b{name}\s+(?:purchases?|transactions?|spend|spending|expenses?)\b",
                rf"\b(?:category|on|for|in)\s+{name}\b",
            )
            if any(
                re.search(pattern, segment, re.IGNORECASE)
                for pattern in patterns
            ):
                return category["name"]
    return None


def explicit_category_in_message(message, categories):
    for segment in reversed(conversation_segments(message)):
        for category in sorted(
            categories,
            key=lambda item: len(item["name"]),
            reverse=True,
        ):
            name = re.escape(category["name"])
            patterns = (
                rf"\b(?:category|under|as)\s+(?:the\s+)?{name}"
                rf"(?:\s+category)?\b",
                rf"\b{name}\s+category\b",
                rf"\b(?:in|to)\s+(?:the\s+)?{name}"
                rf"(?=\s*(?:$|[,.!?;]|\bcategory\b))",
            )
            if any(
                re.search(pattern, segment, re.IGNORECASE)
                for pattern in patterns
            ):
                return category["name"]
    return None


def conversation_segments(message):
    return [
        segment.strip()
        for segment in REQUESTED_CHANGE.split(message)
        if segment.strip()
    ]


def remove_unmentioned_category(filters, message, categories):
    category_name = filters.get("category")
    if category_name is None and filters.get("category_id") is not None:
        category_name = next(
            (
                item["name"]
                for item in categories
                if item["id"] == filters["category_id"]
            ),
            None,
        )
    if category_name is None or words_overlap(category_name, message):
        return
    filters.pop("category", None)
    filters.pop("category_id", None)


def words_overlap(value, message):
    words = {
        word
        for word in re.findall(r"[a-z0-9]+", value.casefold())
        if len(word) >= 3
    }
    message_words = set(re.findall(r"[a-z0-9]+", message.casefold()))
    return bool(words & message_words)


def semantic_validation_error(data, message, categories):
    has_date_filter = any(
        data["filters"].get(key) is not None
        for key in DATE_FILTERS
    )
    if has_date_filter and not DATE_INTENT.search(message):
        return (
            "date filters require a date or time period in the user request; "
            "remove date_from, date_to, and since"
        )
    return write_grounding_error(data, message, categories)


def write_grounding_error(data, message, categories):
    if not isinstance(data, dict):
        return "transaction plan must be an object"
    operation = data.get("operation")
    fields = data.get("fields")
    filters = data.get("filters")
    if operation not in {"create", "update", "delete"}:
        return None
    if not isinstance(fields, dict) or not isinstance(filters, dict):
        return None

    if operation in {"create", "update"}:
        for key in ("merchant", "description"):
            value = fields.get(key)
            if (
                value is not None
                and not text_value_is_grounded(value, message)
            ):
                return (
                    f"{key} must be explicitly grounded in the user request"
                )
        if (
            fields.get("amount") is not None
            and not amount_value_is_grounded(fields["amount"], message)
        ):
            return "amount must be explicitly grounded in the user request"
        if (
            fields.get("date") is not None
            and not date_value_is_grounded(fields["date"], message)
        ):
            return "date must be explicitly grounded in the user request"
        if operation == "update" and (
            fields.get("category") is not None
            or fields.get("category_id") is not None
        ):
            planned_category = planned_category_name(fields, categories)
            explicit_category = explicit_category_in_message(
                message,
                categories,
            )
            if (
                planned_category is None
                or explicit_category is None
                or planned_category.casefold()
                != explicit_category.casefold()
            ):
                return (
                    "updated category must be explicitly grounded in the "
                    "user request"
                )

    if operation in {"update", "delete"}:
        for key in ("search_text", "merchant"):
            value = filters.get(key)
            if (
                value is not None
                and not text_value_is_grounded(value, message)
            ):
                return (
                    f"{key} filter must be grounded in the user request"
                )
        expected_amounts = extract_amount_filters(message)
        for key in ("min_amount", "max_amount"):
            if filters.get(key) is not None and (
                key not in expected_amounts
                or not math.isclose(
                    float(filters[key]),
                    float(expected_amounts[key]),
                    abs_tol=0.000001,
                )
            ):
                return (
                    f"{key} filter must be grounded in the user request"
                )
        if (
            filters.get("category") is not None
            or filters.get("category_id") is not None
        ):
            planned_category = planned_category_name(filters, categories)
            requested_category = category_in_message(message, categories)
            if (
                planned_category is None
                or requested_category is None
                or planned_category.casefold()
                != requested_category.casefold()
            ):
                return (
                    "category filter must be grounded in the user request"
                )
        if not date_filters_are_grounded(filters, message):
            return "date filters must match the user request"
    return None


def text_value_is_grounded(value, message):
    value_words = {
        word
        for word in re.findall(r"[^\W_]+", value.casefold())
        if len(word) >= 3
    }
    message_words = set(
        re.findall(r"[^\W_]+", message.casefold())
    )
    if value_words:
        return value_words <= message_words
    return str(value).strip().casefold() in message.casefold()


def amount_value_is_grounded(value, message):
    amounts = []
    for match in re.finditer(
        r"(?<![a-z0-9])(?:aud\s*)?\$?\s*"
        r"(-?[0-9][0-9,]*(?:\.\d{1,2})?)",
        message,
        re.IGNORECASE,
    ):
        try:
            amounts.append(float(match.group(1).replace(",", "")))
        except ValueError:
            continue
    return any(
        math.isclose(float(value), amount, abs_tol=0.000001)
        for amount in amounts
    )


def date_value_is_grounded(value, message):
    if not DATE_INTENT.search(message):
        return False
    expected_dates = grounded_dates(message)
    return value in expected_dates


def grounded_dates(message):
    lowered = message.casefold()
    today = date.today()
    dates = set()
    if re.search(r"\b(?:today|tonight)\b", lowered):
        dates.add(today.isoformat())
    if re.search(r"\byesterday\b", lowered):
        dates.add((today - timedelta(days=1)).isoformat())
    if re.search(r"\btomorrow\b", lowered):
        dates.add((today + timedelta(days=1)).isoformat())
    dates.update(
        re.findall(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b", message)
    )
    extracted = extract_date_filters(message)
    if extracted.get("date"):
        dates.add(extracted["date"])
    dates.update(extracted.get("dates", []))
    return dates


def planned_category_name(source, categories):
    category_name = source.get("category")
    if isinstance(category_name, str):
        return category_name.strip()
    category_id = source.get("category_id")
    return next(
        (
            category["name"]
            for category in categories
            if category["id"] == category_id
        ),
        None,
    )


def date_filters_are_grounded(filters, message):
    planned = {
        key: filters[key]
        for key in DATE_FILTERS
        if filters.get(key) is not None
    }
    if not planned:
        return True
    if not DATE_INTENT.search(message):
        return False
    expected = extract_date_filters(message)
    if not expected:
        return True
    if expected.get("date"):
        value = expected["date"]
        return (
            planned == {"date": value}
            or planned == {"dates": [value]}
            or planned == {
                "date_from": value,
                "date_to": value,
            }
        )
    return planned == expected


def is_iso_date(value):
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def is_invalid_calculation(value):
    if isinstance(value, str):
        return value not in CALCULATIONS
    return (
        not isinstance(value, list)
        or not value
        or len(value) > 4
        or len(value) != len(set(value))
        or any(item not in CALCULATIONS - {"none"} for item in value)
    )


def is_positive_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def is_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def is_one_line(value, maximum=None):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and "\n" not in value
        and "\r" not in value
        and (maximum is None or len(value) <= maximum)
    )
