from __future__ import annotations

import calendar
import re

from . import config, db_api, mcp_client, rag_client, summary_service
from .ai import chat_prompt, guard, grounded_prompt
from .ai.schemas import validate_chat_response, validate_grounded_answer
from .db_api import ServiceError


SUMMARY_TERMS = ("summaris", "overview", "budget situation", "how am i tracking", "how is my budget")
BUDGET_LINE_SUMMARY_TERMS = ("tell me about", "budget line", "budget summary", "budget summarry", "line summary")
SPEND_MOST_TERMS = (
    "where am i spending the most",
    "what am i spending the most",
    "spending the most money",
    "most money on",
    "highest spending",
)
OVERSPENDING_TERMS = ("where am i overspending", "what am i overspending", "over budget the most", "overbudget", "overspending")
WARNING_TERMS = ("closest to warning", "near warning", "warning pressure", "closest to cap", "near cap")
SAVINGS_TERMS = ("save money", "cut back", "reduce spending", "save more", "spend less")
CATEGORY_SPEND_TERMS = ("how much am i spending on", "what am i spend", "spent on")
UNBUDGETED_SPEND_TERMS = (
    "isnt tracked by a budget line",
    "isn't tracked by a budget line",
    "isnt being budgeted",
    "isn't being budgeted",
    "not being budgeted",
    "not tracked by a budget line",
    "not tracking with a budget",
    "not tracking with my budget",
    "not tracking with a budget line",
    "not tracking in my budget",
)
ADJUSTMENT_TERMS = ("adjust", "adjustment", "allocated budget", "increase my budget", "increase the budget", "recommend increasing", "recommend adjusting", "budgetting plan", "budgeting plan")
ADJUSTMENT_SUGGESTION_TERMS = ("suggest", "suggestion", "suggestions", "recommend", "ideas", "idea", "make room")
INCREASE_TERMS = ("increase", "raise", "higher", "extend")
DECREASE_TERMS = ("decrease", "lower", "reduce", "cut")
LOWER_PROPOSAL_TERMS = ("too high", "slightly too high", "bit too high", "lower the suggestion", "reduce the suggestion", "too aggressive")
HIGHER_PROPOSAL_TERMS = ("too low", "increase it further", "increase further", "push it higher", "raise it further", "a bit more", "a little bit", "little bit", "more room")
PROPOSAL_CONTEXT_TERMS = ("not sure if that will be enough", "not sure that will be enough", "will that be enough", "wont be enough", "won't be enough", "not enough", "enough buffer", "can you do it though", "do it though", "extend it a little further", "extend it further")
ADJUSTMENT_FOLLOW_UP_TERMS = ("what can i do then", "what should i do then")
ACKNOWLEDGEMENT_TERMS = ("thanks", "thank you", "awesome thank you", "awesome thanks", "great thank you", "great thanks")
CATEGORY_REMAINING_TERMS = ("free to spend", "left to spend", "remaining on", "remaining for", "how much can i spend", "able to spend more", "spend more in")
AFFORDABILITY_TERMS = (
    "afford",
    "restaurant",
    "dinner",
    "eat out",
    "eating out",
    "buy",
    "purchase",
    "cost",
    "price",
    "affect my budget",
    "impact my budget",
    "will i be over",
    "wont be over",
    "won't be over",
    "be over",
    "over cap",
    "over budget",
    "fit within",
)
FOLLOW_UP_TERMS = ("that", "it", "this", "over", "fit", "affect", "impact", "okay")
RESET_TERMS = ("moving on", "something else", "another question", "different question", "new question")
MCP_TRANSACTION_TERMS = ("transaction", "transactions", "purchase history", "recent purchases", "recent spending", "recent transactions", "matching purchases")
RAG_GUIDANCE_TERMS = ("with sources", "with citations", "grounded", "source-backed", "using rag", "use rag", "budget guidance")
COACHING_CONTEXT_TERMS = (
    "focus on",
    "watch out",
    "under pressure",
    "risky",
    "risk",
    "what does this mean",
    "why is",
    "why am",
    "explain",
)
AMOUNT_PATTERN = re.compile(r"(?<!\d)(?:\$?\s*)(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?:\s*\$)?(?!\d)")
QUANTITY_AMOUNT_PATTERNS = (
    re.compile(
        r"\b(\d+)\s*(?:x|\*)\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?:\s*\$)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d+)\s+(?:more\s+)?\$?\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?:\s*\$|\s+dollars?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(\d+)\s+(?:more\s+)?\$?\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?:\s*\$)?\s+(?:monthly\s+)?(?:[a-z]+\s+){0,3}(subscriptions?|payments?|purchases?)\b",
        re.IGNORECASE,
    ),
)


def _format_cents(value: int | None) -> str:
    cents = 0 if value is None else value
    return f"${cents / 100:,.2f}"


def _format_month_label(value: str | None) -> str:
    if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
        return "this month"
    year = value[:4]
    month = value[5:]
    names = {
        "01": "January",
        "02": "February",
        "03": "March",
        "04": "April",
        "05": "May",
        "06": "June",
        "07": "July",
        "08": "August",
        "09": "September",
        "10": "October",
        "11": "November",
        "12": "December",
    }
    return f"{names.get(month, value)} {year}" if month in names else value


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _normalise_context(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ServiceError("context must be a JSON object", 422, "invalid_field")
    context: dict[str, object] = {}
    ui_action = value.get("ui_action")
    if ui_action is not None:
        if not isinstance(ui_action, str) or not ui_action.strip():
            raise ServiceError("context.ui_action must be a non-empty string", 422, "invalid_field")
        context["ui_action"] = ui_action.strip()
    target_category = value.get("target_category")
    if target_category is not None:
        if not isinstance(target_category, str) or not target_category.strip():
            raise ServiceError("context.target_category must be a non-empty string", 422, "invalid_field")
        context["target_category"] = target_category.strip()
    for key in ("target_budget_line_id", "target_planned_event_id"):
        field = value.get(key)
        if field is not None:
            if isinstance(field, bool) or not isinstance(field, int):
                raise ServiceError(f"context.{key} must be an integer", 422, "invalid_field")
            context[key] = field
    return context


def _normalise_integration_mode(value: object) -> str:
    if value is None:
        return "auto"
    if not isinstance(value, str):
        raise ServiceError("integration_mode must be a string", 422, "invalid_field")
    normalised = value.strip().casefold()
    if normalised in {"", "auto"}:
        return "auto"
    if normalised in {"mcp", "rag"}:
        return normalised
    raise ServiceError('integration_mode must be "auto", "mcp", or "rag"', 422, "invalid_field")


def _normalise_skip_deterministic(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    raise ServiceError("skip_deterministic must be a boolean", 422, "invalid_field")


def _looks_like_mcp_request(message: str) -> bool:
    lowered = message.casefold()
    return _contains_any(lowered, MCP_TRANSACTION_TERMS)


def _looks_like_rag_request(message: str) -> bool:
    lowered = message.casefold()
    return _contains_any(lowered, RAG_GUIDANCE_TERMS)


def _budget_lines(summary: dict) -> list[dict]:
    lines = summary.get("budget_lines")
    if not isinstance(lines, list):
        return []
    return [line for line in lines if isinstance(line, dict)]


def _find_budget_line(summary: dict, line_id: object) -> dict | None:
    if isinstance(line_id, bool) or not isinstance(line_id, int):
        return None
    return next((line for line in _budget_lines(summary) if line.get("id") == line_id), None)


def _planned_events(summary: dict) -> list[dict]:
    events = summary.get("planned_events")
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


def _find_planned_event(summary: dict, event_id: object) -> dict | None:
    if isinstance(event_id, bool) or not isinstance(event_id, int):
        return None
    return next((event for event in _planned_events(summary) if event.get("id") == event_id), None)


def _other_expenses(summary: dict) -> list[dict]:
    transactions = summary.get("transactions")
    if not isinstance(transactions, dict):
        return []
    other_expenses = transactions.get("other_expenses")
    if not isinstance(other_expenses, list):
        return []
    return [expense for expense in other_expenses if isinstance(expense, dict)]


def _month_date_bounds(month: object) -> tuple[str, str] | None:
    if not isinstance(month, str) or len(month) != 7 or month[4] != "-":
        return None
    year_text = month[:4]
    month_text = month[5:]
    if not year_text.isdigit() or not month_text.isdigit():
        return None
    year = int(year_text)
    month_number = int(month_text)
    if month_number < 1 or month_number > 12:
        return None
    last_day = calendar.monthrange(year, month_number)[1]
    return (f"{year:04d}-{month_number:02d}-01", f"{year:04d}-{month_number:02d}-{last_day:02d}")


def _line_projected_high(line: dict) -> int:
    projected = line.get("projected_high_total")
    if isinstance(projected, int):
        return projected
    actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
    planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
    return actual + planned


def _line_pressure_score(line: dict) -> tuple[int, int, int]:
    projected = _line_projected_high(line)
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) and line.get("hard_cap") > 0 else None
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) and line.get("warn_at") > 0 else None
    threshold = hard_cap if hard_cap is not None else warn_at
    if threshold is None:
        return (0, 0, projected)
    overshoot = projected - threshold
    ratio = int((projected / threshold) * 1000)
    return (1 if overshoot >= 0 else 0, overshoot, ratio)


def _top_pressure_lines(summary: dict, limit: int = 3) -> list[dict]:
    return sorted(_budget_lines(summary), key=_line_pressure_score, reverse=True)[:limit]


def _find_amount_cents(text: object) -> int | None:
    if not isinstance(text, str):
        return None
    matches = list(AMOUNT_PATTERN.finditer(text))
    if not matches:
        return None
    raw = matches[-1].group(1).replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        return None
    if amount < 0:
        return None
    return int(round(amount * 100))


def _amount_text_to_cents(raw: str) -> int | None:
    try:
        amount = float(raw.replace(",", ""))
    except ValueError:
        return None
    if amount < 0:
        return None
    return int(round(amount * 100))


def _extract_keyword_target_cents(message: str, keywords: tuple[str, ...]) -> int | None:
    lowered = message.casefold()
    start = max((lowered.rfind(keyword) for keyword in keywords if lowered.rfind(keyword) >= 0), default=-1)
    if start < 0:
        return None
    tail = message[start:start + 100]
    connector_match = re.search(
        r"(?:to|at|of|be)\s+\$?\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)",
        tail,
        re.IGNORECASE,
    )
    if connector_match:
        return _amount_text_to_cents(connector_match.group(1))
    fallback_match = AMOUNT_PATTERN.search(tail)
    if fallback_match:
        return _amount_text_to_cents(fallback_match.group(1))
    return None


def _extract_threshold_targets(message: str) -> dict[str, int]:
    warn_at = _extract_keyword_target_cents(message, ("warning threshold", "warning amount", "warning", "warn"))
    hard_cap = _extract_keyword_target_cents(message, ("hard cap", "cap"))
    targets: dict[str, int] = {}
    if isinstance(warn_at, int):
        targets["warn_at"] = warn_at
    if isinstance(hard_cap, int):
        targets["hard_cap"] = hard_cap
    return targets


def _extract_spend_floor_target_cents(message: str) -> int | None:
    patterns = (
        r"\b(?:spend|budget)\s+(?:at\s+least|up\s+to)\s+\$?\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?:\s*\$)?\b",
        r"\b(?:be\s+able\s+to|allow\s+myself\s+to|let\s+me)\s+spend\s+(?:at\s+least|up\s+to)?\s*\$?\s*(\d+(?:,\d{3})*(?:\.\d{1,2})?)(?:\s*\$)?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return _amount_text_to_cents(match.group(1))
    return None


def _explicit_threshold_proposal(
    line: dict,
    base_warn: int | None,
    base_cap: int | None,
    targets: dict[str, int],
) -> tuple[int, int]:
    projected = _line_projected_high(line)
    target_warn = targets.get("warn_at") if isinstance(targets.get("warn_at"), int) else None
    target_cap = targets.get("hard_cap") if isinstance(targets.get("hard_cap"), int) else None
    step = _proposal_rounding_step(projected, base_warn, base_cap, target_warn, target_cap)
    if target_cap is None:
        baseline_cap = max(base_cap or 0, projected + step, (target_warn or 0) + step)
        recommended_cap = _round_up(baseline_cap, step)
    else:
        recommended_cap = max(target_cap, projected)
    if target_warn is None:
        derived_warn = max(projected, int(recommended_cap * 0.9), base_warn or 0)
        recommended_warn = _round_up(min(recommended_cap - step, derived_warn), step)
    else:
        recommended_warn = target_warn
    if projected > 0:
        recommended_warn = max(recommended_warn, projected)
        recommended_cap = max(recommended_cap, projected + step)
    if recommended_warn >= recommended_cap:
        recommended_warn = max(0, recommended_cap - step)
    return recommended_warn, recommended_cap


def _is_amount_only_message(message: str) -> bool:
    if _find_amount_cents(message) is None:
        return False
    stripped = message.strip().casefold()
    return len(stripped) <= 32 and not _contains_any(stripped, SUMMARY_TERMS + SPEND_MOST_TERMS + WARNING_TERMS + SAVINGS_TERMS)


def _line_aliases(category: str) -> list[str]:
    lowered = category.casefold()
    aliases = [lowered]
    if lowered.endswith("s") and len(lowered) > 3:
        aliases.append(lowered[:-1])
    if lowered == "dining":
        aliases.extend(["restaurant", "restaurants", "dinner", "eat out", "eating out", "lunch", "cafe", "takeaway", "take out"])
    if lowered == "groceries":
        aliases.extend(["grocery", "groceries", "supermarket", "food shop"])
    if lowered == "music subscriptions":
        aliases.extend(["music subscription", "subscription service", "streaming music", "spotify", "apple music"])
    if lowered == "transport":
        aliases.extend(["travel", "bus", "train", "fuel", "petrol", "gas"])
    if lowered == "entertainment":
        aliases.extend(["movie", "movies", "games", "fun"])
    result: list[str] = []
    for alias in aliases:
        if alias not in result:
            result.append(alias)
    return result


def _line_from_context(summary: dict, context: dict) -> dict | None:
    line = _find_budget_line(summary, context.get("target_budget_line_id"))
    if line is not None:
        return line
    target_category = context.get("target_category")
    if isinstance(target_category, str):
        target = target_category.casefold().strip()
        for line in _budget_lines(summary):
            category = line.get("category")
            if isinstance(category, str) and category.casefold().strip() == target:
                return line
    event = _find_planned_event(summary, context.get("target_planned_event_id"))
    if event is not None:
        category = event.get("category")
        if isinstance(category, str) and category.strip():
            for line in _budget_lines(summary):
                line_category = line.get("category")
                if isinstance(line_category, str) and line_category.casefold().strip() == category.casefold().strip():
                    return line
    return None


def _extract_line_from_text(summary: dict, message: str, context: dict | None = None) -> dict | None:
    if isinstance(context, dict):
        contextual = _line_from_context(summary, context)
        if contextual is not None:
            return contextual
    search_text = message.casefold()
    best_line = None
    best_length = -1
    for line in _budget_lines(summary):
        category = line.get("category")
        if not isinstance(category, str) or not category.strip():
            continue
        for alias in _line_aliases(category):
            if alias in search_text and len(alias) > best_length:
                best_line = line
                best_length = len(alias)
    return best_line


def _line_from_recent_context(summary: dict, history: list[dict]) -> dict | None:
    affordability_context = _recent_affordability_context(summary, history)
    if affordability_context is not None and isinstance(affordability_context.get("line"), dict):
        return affordability_context["line"]
    adjustment_context = _recent_adjustment_context(summary, history)
    if adjustment_context is not None and isinstance(adjustment_context.get("line"), dict):
        return adjustment_context["line"]
    return _latest_open_proposal_target_line(summary)


def _follow_up_quantity(message: str) -> int | None:
    lowered = message.casefold().strip()
    if "$" in lowered or "dollar" in lowered:
        return None
    match = re.fullmatch(r"(?:what about|about|make it|try|maybe)?\s*(\d+)", lowered)
    if match:
        return int(match.group(1))
    contextual_match = re.search(
        r"\b(?:buy|purchase|get|afford)\b(?:\s+\w+){0,5}\s+(\d+)\b",
        lowered,
        re.IGNORECASE,
    )
    if contextual_match:
        return int(contextual_match.group(1))
    return None


def _find_quantity_amount_cents(message: str) -> int | None:
    lowered = message.casefold()
    for pattern in QUANTITY_AMOUNT_PATTERNS:
        match = pattern.search(lowered)
        if not match:
            continue
        quantity = int(match.group(1))
        unit_amount = _amount_text_to_cents(match.group(2))
        if unit_amount is not None and quantity > 0:
            return quantity * unit_amount
    return None


def _find_contextual_amount_cents(summary: dict, history: list[dict], message: str) -> int | None:
    quantity_amount_cents = _find_quantity_amount_cents(message)
    if quantity_amount_cents is not None:
        return quantity_amount_cents
    quantity = _follow_up_quantity(message)
    if quantity is not None:
        context = _recent_affordability_context(summary, history)
        if context is not None and isinstance(context.get("amount_cents"), int) and quantity > 1:
            return quantity * context["amount_cents"]
    return _find_amount_cents(message)


def _extract_adjustment_direction(message: str) -> str | None:
    lowered = message.casefold()
    if _contains_any(lowered, DECREASE_TERMS):
        return "decrease"
    if _contains_any(lowered, INCREASE_TERMS):
        return "increase"
    return None


def _extract_proposal_revision(message: str) -> str | None:
    lowered = message.casefold()
    if _contains_any(lowered, LOWER_PROPOSAL_TERMS):
        return "lower"
    if _contains_any(lowered, HIGHER_PROPOSAL_TERMS):
        return "higher"
    return None


def _has_proposal_feedback(message: str) -> bool:
    lowered = message.casefold()
    return (
        _extract_proposal_revision(lowered) is not None
        or bool(_extract_threshold_targets(lowered))
        or _contains_any(lowered, PROPOSAL_CONTEXT_TERMS)
        or ("proposal" in lowered and (_find_amount_cents(lowered) is not None or _extract_adjustment_direction(lowered) is not None))
    )


def _is_acknowledgement(message: str) -> bool:
    lowered = message.casefold().strip()
    if not lowered:
        return False
    return lowered in ACKNOWLEDGEMENT_TERMS


def _looks_like_adjustment_message(summary: dict, message: str) -> bool:
    lowered = message.casefold().strip()
    if not lowered:
        return False
    if _contains_any(lowered, ADJUSTMENT_TERMS):
        return True
    if "accommodate" in lowered or "accomodate" in lowered:
        return True
    if _extract_spend_floor_target_cents(lowered) is not None:
        return True
    if _contains_any(lowered, ADJUSTMENT_SUGGESTION_TERMS) and (
        "budget" in lowered or "budgets" in lowered or "income" in lowered or "spending" in lowered
    ):
        return True
    if "proposal" in lowered and _extract_line_from_text(summary, lowered) is not None and (
        _find_amount_cents(lowered) is not None
        or "extra" in lowered
        or "more" in lowered
        or "spend" in lowered
    ):
        return True
    direction = _extract_adjustment_direction(lowered)
    if direction is not None and (
        "budget" in lowered
        or "budgets" in lowered
        or "warning" in lowered
        or "cap" in lowered
        or _extract_line_from_text(summary, lowered) is not None
    ):
        return True
    return False


def _classify_message(summary: dict, message: str) -> str:
    lowered = message.casefold().strip()
    if not lowered:
        return "other"
    if _contains_any(lowered, SUMMARY_TERMS):
        return "summary"
    if _extract_line_from_text(summary, lowered) is not None and _contains_any(lowered, BUDGET_LINE_SUMMARY_TERMS):
        return "budget-line-summary"
    if _contains_any(lowered, UNBUDGETED_SPEND_TERMS):
        return "unbudgeted-spend"
    if _contains_any(lowered, SPEND_MOST_TERMS):
        return "spend-most"
    if _contains_any(lowered, OVERSPENDING_TERMS):
        return "overspending"
    if _contains_any(lowered, WARNING_TERMS):
        return "warnings"
    if _is_acknowledgement(lowered):
        return "acknowledgement"
    if _looks_like_adjustment_message(summary, lowered):
        return "adjustments"
    if _contains_any(lowered, SAVINGS_TERMS):
        return "savings"
    if _extract_line_from_text(summary, lowered) is not None and _contains_any(lowered, CATEGORY_SPEND_TERMS):
        return "category-spend"
    if _extract_line_from_text(summary, lowered) is not None and _contains_any(lowered, CATEGORY_REMAINING_TERMS):
        return "category-remaining"
    if _contains_any(lowered, AFFORDABILITY_TERMS):
        return "affordability"
    if _is_amount_only_message(lowered):
        return "amount-only"
    if _contains_any(lowered, RESET_TERMS):
        return "reset"
    return "other"


def _contextualised_message(summary: dict, message: str, context: dict) -> str:
    line = _line_from_context(summary, context)
    event = _find_planned_event(summary, context.get("target_planned_event_id"))
    ui_action = context.get("ui_action")
    category = line.get("category") if isinstance(line, dict) and isinstance(line.get("category"), str) else context.get("target_category")
    if not isinstance(ui_action, str):
        return message
    if ui_action == "line-transactions" and isinstance(category, str):
        return f"Show me the most relevant transactions for {category} this month."
    if ui_action == "line-grounded-advice" and isinstance(category, str):
        return f"Using grounded budget guidance with sources, what should I focus on for {category} this month?"
    if ui_action == "line-pressure-explainer" and isinstance(category, str):
        return f"Tell me about the {category} budget line."
    if ui_action == "event-affordability" and isinstance(event, dict):
        label = str(event.get("label") or "this planned event")
        event_category = str(event.get("category") or category or "this category")
        est_high = event.get("est_high") if isinstance(event.get("est_high"), int) else None
        if isinstance(est_high, int):
            return f"Can I still afford {label} in {event_category} if it costs {_format_cents(est_high)} this month?"
        return f"Can I still afford the planned event {label} in {event_category} this month?"
    if ui_action == "event-transactions" and isinstance(event, dict):
        event_category = str(event.get("category") or category or "this category")
        return f"Show me similar recent transactions for {event_category} this month."
    if ui_action == "event-grounded-advice" and isinstance(event, dict):
        label = str(event.get("label") or "this planned event")
        event_category = str(event.get("category") or category or "this category")
        return f"Using grounded budget guidance with sources, what should I watch out for with {label} in {event_category} this month?"
    return message


def _recent_affordability_context(summary: dict, history: list[dict]) -> dict | None:
    line = None
    amount_cents = None
    saw_affordability = False
    for turn in reversed(history[-10:]):
        if turn.get("role") != "user":
            continue
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        kind = _classify_message(summary, content)
        if kind in {"reset", "summary", "unbudgeted-spend", "spend-most", "warnings", "adjustments", "savings", "category-spend", "category-remaining", "acknowledgement"}:
            break
        if kind == "other":
            if saw_affordability or amount_cents is not None or line is not None:
                break
            continue
        if kind == "amount-only" and amount_cents is None:
            amount_cents = _find_contextual_amount_cents(summary, [], content)
            continue
        if kind == "affordability":
            saw_affordability = True
            if line is None:
                line = _extract_line_from_text(summary, content)
            if amount_cents is None:
                amount_cents = _find_contextual_amount_cents(summary, [], content)
    if not saw_affordability and amount_cents is None and line is None:
        return None
    return {"line": line, "amount_cents": amount_cents}


def _recent_adjustment_context(summary: dict, history: list[dict]) -> dict | None:
    line = None
    amount_cents = None
    direction = None
    revision = None
    saw_adjustment = False
    for turn in reversed(history[-10:]):
        if turn.get("role") != "user":
            continue
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        kind = _classify_message(summary, content)
        if kind in {"reset", "summary", "unbudgeted-spend", "spend-most", "warnings", "savings", "category-spend", "category-remaining", "affordability", "acknowledgement"}:
            break
        if kind == "other":
            if saw_adjustment or amount_cents is not None or line is not None or direction is not None or revision is not None:
                break
            continue
        if kind == "amount-only" and saw_adjustment and amount_cents is None:
            amount_cents = _find_contextual_amount_cents(summary, [], content)
            continue
        if kind == "adjustments":
            saw_adjustment = True
            if line is None:
                line = _extract_line_from_text(summary, content)
            if amount_cents is None:
                amount_cents = _find_contextual_amount_cents(summary, [], content)
            if direction is None:
                direction = _extract_adjustment_direction(content)
            if revision is None:
                revision = _extract_proposal_revision(content)
    if not saw_adjustment and amount_cents is None and line is None and direction is None and revision is None:
        return None
    return {"line": line, "amount_cents": amount_cents, "direction": direction, "revision": revision}


def _looks_like_category_only_follow_up(summary: dict, history: list[dict], message: str) -> bool:
    lowered = message.casefold().strip()
    if not lowered or " " not in lowered:
        return False
    if _find_amount_cents(lowered) is not None:
        return False
    if _contains_any(
        lowered,
        SUMMARY_TERMS
        + BUDGET_LINE_SUMMARY_TERMS
        + SPEND_MOST_TERMS
        + OVERSPENDING_TERMS
        + WARNING_TERMS
        + SAVINGS_TERMS
        + CATEGORY_SPEND_TERMS
        + CATEGORY_REMAINING_TERMS
        + AFFORDABILITY_TERMS
        + ADJUSTMENT_TERMS
        + ADJUSTMENT_SUGGESTION_TERMS
        + RESET_TERMS
        + UNBUDGETED_SPEND_TERMS,
    ):
        return False
    return _extract_line_from_text(summary, lowered) is not None and _recent_affordability_context(summary, history) is not None


def _remaining_before(value: int | None, current: int) -> int | None:
    if value is None:
        return None
    return value - current


def _summary_reply(summary: dict) -> dict:
    budget = summary.get("budget") if isinstance(summary.get("budget"), dict) else {}
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    income = totals.get("declared_income") if isinstance(totals.get("declared_income"), int) else budget.get("declared_income")
    actual = totals.get("actual_spend_total") if isinstance(totals.get("actual_spend_total"), int) else 0
    planned = totals.get("planned_est_high_total") if isinstance(totals.get("planned_est_high_total"), int) else 0
    remaining = totals.get("remaining_income_high") if isinstance(totals.get("remaining_income_high"), int) else None
    pressure_parts: list[str] = []
    for line in _top_pressure_lines(summary, limit=2):
        category = str(line.get("category") or "Unnamed")
        projected = _line_projected_high(line)
        hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
        warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
        if hard_cap is not None and projected >= hard_cap:
            pressure_parts.append(f"{category} is over cap at {_format_cents(projected)}")
        elif warn_at is not None and projected >= warn_at:
            pressure_parts.append(f"{category} is at warning pressure with {_format_cents(projected)} projected")
        else:
            pressure_parts.append(f"{category} is projected at {_format_cents(projected)}")
    pressure_text = " ".join(pressure_parts) if pressure_parts else "No budget lines are available yet."
    say = (
        f"For {_format_month_label(budget.get('month'))}, income is {_format_cents(income)}. "
        f"You have spent {_format_cents(actual)} and planned {_format_cents(planned)}, leaving {_format_cents(remaining)} projected. "
        f"{pressure_text}"
    )
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _budget_line_summary_reply(summary: dict, history: list[dict], message: str) -> dict:
    line = _extract_line_from_text(summary, message) or _line_from_recent_context(summary, history)
    if line is None:
        return {
            "mode": "clarify",
            "say": "Tell me which budget line you mean and I will summarise it.",
            "question": "category_needed",
            "proposal": None,
            "fallback": False,
        }
    category = str(line.get("category") or "that budget line")
    actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
    planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
    projected = _line_projected_high(line)
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    if hard_cap is not None and projected >= hard_cap:
        status = f"It is currently {_format_cents(projected - hard_cap)} over the hard cap of {_format_cents(hard_cap)}."
    elif warn_at is not None and projected >= warn_at:
        hard_cap_text = "" if hard_cap is None else f" It has {_format_cents(hard_cap - projected)} left before the hard cap."
        status = f"It is currently in warning range at {_format_cents(projected)} against a warning amount of {_format_cents(warn_at)}.{hard_cap_text}"
    else:
        room_parts: list[str] = []
        if warn_at is not None:
            room_parts.append(f"{_format_cents(warn_at - projected)} before warning")
        if hard_cap is not None:
            room_parts.append(f"{_format_cents(hard_cap - projected)} before hard cap")
        status = (
            "It is currently within its thresholds."
            if not room_parts
            else "It is currently within its thresholds, with " + " and ".join(room_parts) + "."
        )
    say = (
        f"{category} is currently spent {_format_cents(actual)} with {_format_cents(planned)} planned, "
        f"so the projected total is {_format_cents(projected)}. "
        f"The warning amount is {_format_cents(warn_at)} and the hard cap is {_format_cents(hard_cap)}. "
        f"{status}"
    )
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _overspending_reply(summary: dict) -> dict:
    pressured_lines = [
        line for line in _top_pressure_lines(summary, limit=max(3, len(_budget_lines(summary))))
        if _line_pressure_score(line)[0] == 1
    ]
    if not pressured_lines:
        say = "You are not currently projected to be over any budget line thresholds this month."
        return {"mode": "advice", "say": say, "question": None, "proposal": None, "fallback": False}
    line = pressured_lines[0]
    category = str(line.get("category") or "Unnamed")
    projected = _line_projected_high(line)
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    if hard_cap is not None and projected >= hard_cap:
        say = (
            f"You are overspending most in {category}. Its projected total is {_format_cents(projected)}, "
            f"which is {_format_cents(projected - hard_cap)} over the hard cap of {_format_cents(hard_cap)}."
        )
    elif warn_at is not None and projected >= warn_at:
        say = (
            f"{category} is under the most pressure this month. Its projected total is {_format_cents(projected)}, "
            f"which is {_format_cents(projected - warn_at)} over the warning amount of {_format_cents(warn_at)}."
        )
    else:
        say = f"{category} is under the most pressure this month at a projected {_format_cents(projected)}."
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _spend_most_reply(summary: dict) -> dict:
    lines = _budget_lines(summary)
    if not lines:
        say = "There are no budget lines yet, so I cannot rank spending for this month."
        return {"mode": "advice", "say": say, "question": None, "proposal": None, "fallback": False}
    top_line = max(lines, key=lambda line: line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0)
    actual = top_line.get("actual_spend") if isinstance(top_line.get("actual_spend"), int) else 0
    projected = _line_projected_high(top_line)
    category = str(top_line.get("category") or "Unnamed")
    say = (
        f"You are spending the most on {category} this month at {_format_cents(actual)} so far. "
        f"With planned spending included, {category} is projected to reach {_format_cents(projected)}."
    )
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _unbudgeted_spend_reply(summary: dict) -> dict:
    other_expenses = _other_expenses(summary)
    if not other_expenses:
        say = "I cannot see any spending categories outside your current budget lines for this month."
        return {"mode": "advice", "say": say, "question": None, "proposal": None, "fallback": False}
    top_expense = max(other_expenses, key=lambda expense: expense.get("actual_spend") if isinstance(expense.get("actual_spend"), int) else 0)
    category = str(top_expense.get("category") or "Uncategorised")
    actual = top_expense.get("actual_spend") if isinstance(top_expense.get("actual_spend"), int) else 0
    say = f"The biggest spend not currently tracked by a budget line is {category} at {_format_cents(actual)} this month."
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _warning_reply(summary: dict) -> dict:
    candidates = [line for line in _budget_lines(summary) if isinstance(line.get("warn_at"), int)]
    if not candidates:
        say = "No budget lines have warning amounts yet, so I cannot rank which ones are closest."
        return {"mode": "advice", "say": say, "question": None, "proposal": None, "fallback": False}

    def sort_key(line: dict) -> tuple[int, int]:
        projected = _line_projected_high(line)
        warn_at = line.get("warn_at")
        assert isinstance(warn_at, int)
        return (1 if projected >= warn_at else 0, projected - warn_at)

    ranked = sorted(candidates, key=sort_key, reverse=True)[:2]
    parts: list[str] = []
    for line in ranked:
        category = str(line.get("category") or "Unnamed")
        projected = _line_projected_high(line)
        warn_at = line.get("warn_at")
        assert isinstance(warn_at, int)
        if projected >= warn_at:
            parts.append(f"{category} is already above warning, at {_format_cents(projected)} against {_format_cents(warn_at)}")
        else:
            parts.append(f"{category} is closest to warning, with {_format_cents(warn_at - projected)} left before warning")
    say = " ".join(parts)
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _category_remaining_reply(summary: dict, message: str) -> dict:
    line = _extract_line_from_text(summary, message)
    if line is None:
        say = "Tell me which budget line you mean and I will work out how much room is left this month."
        return {"mode": "clarify", "say": say, "question": "category_needed", "proposal": None, "fallback": False}
    category = str(line.get("category") or "that category")
    actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
    planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
    projected = _line_projected_high(line)
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    if hard_cap is not None and projected >= hard_cap:
        say = (
            f"{category} is already over its hard cap. It is spent {_format_cents(actual)} with {_format_cents(planned)} planned, "
            f"so the projected total is {_format_cents(projected)}, which is {_format_cents(projected - hard_cap)} over the cap of {_format_cents(hard_cap)}."
        )
        return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}
    if warn_at is not None and projected >= warn_at:
        cap_room = _remaining_before(hard_cap, projected)
        tail = "" if cap_room is None else f" You still have {_format_cents(cap_room)} before the hard cap."
        say = (
            f"{category} is already in warning range. It is spent {_format_cents(actual)} with {_format_cents(planned)} planned, "
            f"so the projected total is {_format_cents(projected)} against the warning amount of {_format_cents(warn_at)}.{tail}"
        )
        return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}
    warn_room = _remaining_before(warn_at, projected)
    cap_room = _remaining_before(hard_cap, projected)
    room_parts: list[str] = []
    if warn_room is not None:
        room_parts.append(f"{_format_cents(warn_room)} before warning")
    if cap_room is not None:
        room_parts.append(f"{_format_cents(cap_room)} before hard cap")
    room_text = " and ".join(room_parts) if room_parts else "no warning or cap limit set"
    say = (
        f"For {category}, you have spent {_format_cents(actual)} and planned {_format_cents(planned)}, "
        f"so the projected total is {_format_cents(projected)}. That leaves {room_text} this month."
    )
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _category_spend_reply(summary: dict, message: str) -> dict:
    line = _extract_line_from_text(summary, message)
    if line is not None:
        category = str(line.get("category") or "that category")
        actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
        planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
        projected = _line_projected_high(line)
        say = (
            f"You have spent {_format_cents(actual)} on {category} so far this month. "
            f"With {_format_cents(planned)} planned, {category} is projected to reach {_format_cents(projected)}."
        )
        return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}
    other_expenses = _other_expenses(summary)
    lowered = message.casefold()
    for expense in other_expenses:
        category = expense.get("category")
        if isinstance(category, str) and category.casefold() in lowered:
            actual = expense.get("actual_spend") if isinstance(expense.get("actual_spend"), int) else 0
            say = f"You have spent {_format_cents(actual)} on {category} this month, and it is not currently tracked by a budget line."
            return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}
    return {
        "mode": "advice",
        "say": "I cannot see that category in this month's budget lines or unbudgeted spending summary.",
        "question": None,
        "proposal": None,
        "fallback": False,
    }


def _savings_reply(summary: dict) -> dict:
    lines = _top_pressure_lines(summary, limit=2)
    if not lines:
        say = "There are no budget lines yet, so I do not have any targeted saving suggestions for this month."
        return {"mode": "advice", "say": say, "question": None, "proposal": None, "fallback": False}
    suggestions: list[str] = []
    for line in lines:
        category = str(line.get("category") or "Unnamed")
        projected = _line_projected_high(line)
        hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
        warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
        if hard_cap is not None and projected >= hard_cap:
            suggestions.append(f"reduce {category} first, because it is {_format_cents(projected - hard_cap)} over cap")
        elif warn_at is not None and projected >= warn_at:
            suggestions.append(f"watch {category}, because it is already in warning range at {_format_cents(projected)}")
        else:
            actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
            suggestions.append(f"look at {category}, where you have already spent {_format_cents(actual)}")
    say = "The best places to save this month are to " + " and ".join(suggestions) + "."
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _round_up(value: int, step: int = 1000) -> int:
    return ((value + step - 1) // step) * step


def _proposal_rounding_step(*values: int | None) -> int:
    highest = max((value for value in values if isinstance(value, int)), default=0)
    return 5000 if highest >= 50000 else 1000


def _line_has_thresholds(line: dict) -> bool:
    return isinstance(line.get("hard_cap"), int) or isinstance(line.get("warn_at"), int)


def _line_needs_adjustment(line: dict) -> bool:
    projected = _line_projected_high(line)
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    if hard_cap is not None and projected >= hard_cap:
        return True
    if warn_at is not None and projected >= warn_at:
        return True
    threshold = warn_at if warn_at is not None else hard_cap
    return bool(threshold is not None and projected >= int(threshold * 0.85))


def _recommended_increase_thresholds(line: dict, requested_increase: int | None) -> tuple[int, int]:
    projected = _line_projected_high(line)
    current_warn = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    current_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    step = _proposal_rounding_step(projected, current_warn, current_cap, requested_increase)
    current_gap = max((current_cap or 0) - (current_warn or 0), step * 2)
    cap_buffer = max(step * 2, int(projected * 0.08))
    baseline_cap = max(projected + cap_buffer, (current_cap or 0) + step * 2)
    if requested_increase is not None:
        if current_cap is not None:
            baseline_cap = max(baseline_cap, current_cap + requested_increase)
        else:
            baseline_cap = max(baseline_cap, projected + requested_increase)
    recommended_cap = _round_up(baseline_cap, step)
    requested_warn = (current_warn + requested_increase) if requested_increase is not None and current_warn is not None else 0
    warn_target = max(
        requested_warn,
        projected - max(step, cap_buffer // 2),
        int(recommended_cap * 0.88),
        current_warn or 0,
        recommended_cap - current_gap,
    )
    recommended_warn = _round_up(min(recommended_cap, warn_target), step)
    if projected > 0:
        recommended_warn = max(recommended_warn, projected)
        recommended_cap = max(recommended_cap, recommended_warn + step)
    if recommended_warn >= recommended_cap:
        recommended_warn = max(0, recommended_cap - step)
    return recommended_warn, recommended_cap


def _active_coach_proposals(summary: dict) -> list[dict]:
    proposals = summary.get("coach_proposals")
    if not isinstance(proposals, list):
        return []
    return [proposal for proposal in proposals if isinstance(proposal, dict) and proposal.get("status") == "proposed"]


def _proposal_operations(proposal: dict | None) -> list[dict]:
    if not isinstance(proposal, dict):
        return []
    operations = proposal.get("operations")
    if not isinstance(operations, list):
        return []
    return [operation for operation in operations if isinstance(operation, dict)]


def _proposal_line_ids(proposal: dict | None) -> tuple[int, ...]:
    result: list[int] = []
    for operation in _proposal_operations(proposal):
        line_id = operation.get("budget_line_id")
        if isinstance(line_id, int):
            result.append(line_id)
    return tuple(result)


def _proposal_fields_for_line(proposal: dict | None, line_id: int) -> dict | None:
    for operation in _proposal_operations(proposal):
        if operation.get("action") != "update_budget_line" or operation.get("budget_line_id") != line_id:
            continue
        fields = operation.get("fields")
        if isinstance(fields, dict):
            return fields
    return None


def _proposal_target_line(summary: dict, proposal: dict | None) -> dict | None:
    line_ids = _proposal_line_ids(proposal)
    if not line_ids:
        return None
    for line in _budget_lines(summary):
        line_id = line.get("id")
        if isinstance(line_id, int) and line_id in line_ids:
            return line
    return None


def _normalised_proposal(proposal: dict | None) -> dict:
    data = proposal if isinstance(proposal, dict) else {}
    return {
        "proposal_type": data.get("proposal_type"),
        "summary": data.get("summary"),
        "operations": [
            {
                "action": operation.get("action"),
                "budget_line_id": operation.get("budget_line_id"),
                "fields": dict(sorted((operation.get("fields") or {}).items())),
            }
            for operation in _proposal_operations(data)
        ],
    }


def _latest_open_threshold_proposal(summary: dict, line_id: int) -> dict | None:
    for proposal in reversed(_active_coach_proposals(summary)):
        proposal_json = proposal.get("proposal_json")
        if _proposal_fields_for_line(proposal_json, line_id) is not None:
            return proposal
    return None


def _latest_open_proposal_target_line(summary: dict) -> dict | None:
    for proposal in reversed(_active_coach_proposals(summary)):
        line = _proposal_target_line(summary, proposal.get("proposal_json"))
        if line is not None:
            return line
    return None


def _apply_revision_to_thresholds(
    line: dict,
    recommended_warn: int,
    recommended_cap: int,
    revision: str | None,
    summary: dict,
    requested_change: int | None = None,
) -> tuple[int, int]:
    if revision is None:
        return recommended_warn, recommended_cap
    line_id = line.get("id")
    if isinstance(line_id, bool) or not isinstance(line_id, int):
        return recommended_warn, recommended_cap
    current_warn = line.get("warn_at") if isinstance(line.get("warn_at"), int) else 0
    current_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else 0
    projected = _line_projected_high(line)
    latest_proposal = _latest_open_threshold_proposal(summary, line_id)
    latest_fields = _proposal_fields_for_line(latest_proposal.get("proposal_json") if isinstance(latest_proposal, dict) else None, line_id)
    base_warn = latest_fields.get("warn_at") if isinstance(latest_fields, dict) and isinstance(latest_fields.get("warn_at"), int) else recommended_warn
    base_cap = latest_fields.get("hard_cap") if isinstance(latest_fields, dict) and isinstance(latest_fields.get("hard_cap"), int) else recommended_cap
    step = _proposal_rounding_step(projected, current_warn, current_cap, base_warn, base_cap)
    if revision == "lower":
        delta = requested_change if isinstance(requested_change, int) and requested_change > 0 else step
        revised_cap = _round_up(max(projected + step, base_cap - delta, current_cap), step)
        revised_warn = _round_up(max(projected, base_warn - delta, current_warn), step)
        return min(revised_warn, revised_cap - step), revised_cap
    delta = requested_change if isinstance(requested_change, int) and requested_change > 0 else step
    revised_cap = _round_up(max(base_cap + delta, projected + (step * 2)), step)
    revised_warn = _round_up(max(projected, int(revised_cap * 0.88), current_warn), step)
    if revised_warn >= revised_cap:
        revised_warn = revised_cap - step
    return revised_warn, revised_cap


def _proposal_review_say(proposal: dict, preferred_intro: str | None = None) -> str:
    operations = _proposal_operations(proposal)
    if not operations:
        return preferred_intro or "I prepared a proposal for you to review."
    operation = operations[0]
    fields = operation.get("fields") if isinstance(operation.get("fields"), dict) else {}
    category = str(operation.get("category") or "that budget line")
    warn_at = fields.get("warn_at") if isinstance(fields.get("warn_at"), int) else None
    hard_cap = fields.get("hard_cap") if isinstance(fields.get("hard_cap"), int) else None
    if warn_at is not None and hard_cap is not None:
        body = (
            f"move {category}'s warning amount to {_format_cents(warn_at)} "
            f"and hard cap to {_format_cents(hard_cap)}"
        )
    elif warn_at is not None:
        body = f"move {category}'s warning amount to {_format_cents(warn_at)}"
    elif hard_cap is not None:
        body = f"move {category}'s hard cap to {_format_cents(hard_cap)}"
    else:
        body = f"update {category}"
    intro = preferred_intro or "I prepared a proposal"
    return f"{intro} to {body} for your review."


def _proposal_line(summary: dict, operation: dict) -> dict | None:
    line_id = operation.get("budget_line_id")
    category = operation.get("category")
    if isinstance(line_id, int):
        for line in _budget_lines(summary):
            if line.get("id") == line_id:
                return line
    if isinstance(category, str) and category.strip():
        target = category.casefold().strip()
        for line in _budget_lines(summary):
            line_category = line.get("category")
            if isinstance(line_category, str) and line_category.casefold().strip() == target:
                return line
    return None


def _hydrate_proposal(summary: dict, proposal: dict | None) -> dict | None:
    if not isinstance(proposal, dict):
        return proposal
    operations = _proposal_operations(proposal)
    if not operations:
        return proposal
    hydrated_operations: list[dict] = []
    changed = False
    for operation in operations:
        next_operation = dict(operation)
        line = _proposal_line(summary, operation)
        if line is not None:
            line_id = line.get("id")
            category = line.get("category")
            if not isinstance(next_operation.get("budget_line_id"), int) and isinstance(line_id, int):
                next_operation["budget_line_id"] = line_id
                changed = True
            if (not isinstance(next_operation.get("category"), str) or not str(next_operation.get("category")).strip()) and isinstance(category, str) and category.strip():
                next_operation["category"] = category
                changed = True
        hydrated_operations.append(next_operation)
    if not changed:
        return proposal
    hydrated = dict(proposal)
    hydrated["operations"] = hydrated_operations
    if not isinstance(hydrated.get("summary"), str) or not str(hydrated.get("summary")).strip():
        first_category = hydrated_operations[0].get("category")
        if isinstance(first_category, str) and first_category.strip():
            hydrated["summary"] = f"Review {first_category} warning and hard-cap values."
    return hydrated


def _proposal_reply_matches_payload(proposal: dict, say: str) -> bool:
    operations = _proposal_operations(proposal)
    if not operations:
        return True
    operation = operations[0]
    fields = operation.get("fields") if isinstance(operation.get("fields"), dict) else {}
    lowered = say.casefold()
    category = operation.get("category")
    if isinstance(category, str) and category.strip() and category.strip().casefold() not in lowered:
        return False
    warn_at = fields.get("warn_at") if isinstance(fields.get("warn_at"), int) else None
    hard_cap = fields.get("hard_cap") if isinstance(fields.get("hard_cap"), int) else None
    if warn_at is not None and _format_cents(warn_at).casefold() not in lowered:
        return False
    if hard_cap is not None and _format_cents(hard_cap).casefold() not in lowered:
        return False
    return True


def _normalise_proposal_reply(result: dict) -> dict:
    if result.get("mode") != "proposal" or not isinstance(result.get("proposal"), dict):
        return result
    say = str(result.get("say") or "").strip()
    lowered = say.casefold()
    preferred_intro = None
    if any(
        phrase in lowered for phrase in (
            "i have adjusted",
            "i adjusted the budget",
            "i adjusted the dining budget",
            "i adjusted the utilities budget",
            "is now set",
            "has been set",
            "will be set",
            "i updated",
            "i changed",
        )
    ):
        preferred_intro = "I revised the proposal" if "proposal" in lowered or "adjusted" in lowered else "I prepared a proposal"
    elif not _proposal_reply_matches_payload(result["proposal"], say):
        preferred_intro = "I revised the proposal" if any(word in lowered for word in ("revised", "adjusted", "proposal")) else "I prepared a proposal"
    if preferred_intro is not None:
        result = dict(result)
        result["say"] = _proposal_review_say(result["proposal"], preferred_intro)
    return result


def _adjustment_proposal_reply(summary: dict, history: list[dict], message: str) -> dict:
    context = _recent_adjustment_context(summary, history) or {}
    affordability_context = _recent_affordability_context(summary, history) or {}
    line = _extract_line_from_text(summary, message) or context.get("line") or affordability_context.get("line")
    if line is None and _contains_any(message.casefold(), ADJUSTMENT_FOLLOW_UP_TERMS):
        line = (_recent_affordability_context(summary, history) or {}).get("line")
    if line is None:
        line = _latest_open_proposal_target_line(summary)
    if line is None:
        line = next(
            (
                candidate for candidate in _top_pressure_lines(summary, limit=max(3, len(_budget_lines(summary))))
                if _line_has_thresholds(candidate) and _line_needs_adjustment(candidate)
            ),
            None,
        )
    if line is None:
        return {
            "mode": "advice",
            "say": "I do not see a budget line that currently needs a threshold change, so I do not have a safe adjustment proposal to suggest.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }
    line_id = line.get("id")
    if isinstance(line_id, bool) or not isinstance(line_id, int):
        return {
            "mode": "advice",
            "say": "I could not identify a budget line to adjust safely.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }
    if not _line_has_thresholds(line):
        return {
            "mode": "advice",
            "say": "That budget line does not have warning or cap values yet, so I cannot generate a safe threshold proposal for it.",
            "question": None,
            "proposal": None,
            "fallback": False,
        }
    category = str(line.get("category") or "that budget line")
    projected = _line_projected_high(line)
    actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
    planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
    current_warn = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    current_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    latest_proposal = _latest_open_threshold_proposal(summary, line_id)
    latest_fields = _proposal_fields_for_line(latest_proposal.get("proposal_json") if isinstance(latest_proposal, dict) else None, line_id)
    base_warn = latest_fields.get("warn_at") if isinstance(latest_fields, dict) and isinstance(latest_fields.get("warn_at"), int) else current_warn
    base_cap = latest_fields.get("hard_cap") if isinstance(latest_fields, dict) and isinstance(latest_fields.get("hard_cap"), int) else current_cap
    direction = _extract_adjustment_direction(message) or context.get("direction") or "increase"
    explicit_targets = _extract_threshold_targets(message)
    spend_floor_target = _extract_spend_floor_target_cents(message)
    if "hard_cap" not in explicit_targets and isinstance(spend_floor_target, int):
        explicit_targets["hard_cap"] = spend_floor_target
    requested_increase = None if explicit_targets else _find_contextual_amount_cents(summary, history, message)
    if requested_increase is None:
        requested_increase = context.get("amount_cents") or affordability_context.get("amount_cents")
    revision = _extract_proposal_revision(message) or context.get("revision")
    if revision is None and _contains_any(message.casefold(), PROPOSAL_CONTEXT_TERMS):
        revision = "higher"
    if revision is None and "proposal" in message.casefold() and requested_increase is not None:
        revision = "higher" if direction != "decrease" else "lower"
    if revision is None and latest_proposal is not None and direction != "decrease" and requested_increase is not None and (
        "another" in message.casefold() or "more" in message.casefold() or "further" in message.casefold()
    ):
        revision = "higher"
    if direction == "decrease" and revision != "lower" and not (
        "cap" in message.casefold() and (requested_increase is not None or "hard_cap" in explicit_targets)
    ):
        return {
            "mode": "advice",
            "say": (
                f"I would not suggest lowering {category} right now. It is currently spent {_format_cents(actual)} with "
                f"{_format_cents(planned)} planned, so the projected total is {_format_cents(projected)} against "
                f"the current warning amount of {_format_cents(current_warn)} and hard cap of {_format_cents(current_cap)}."
            )[:500],
            "question": None,
            "proposal": None,
            "fallback": False,
        }
    if explicit_targets:
        recommended_warn, recommended_cap = _explicit_threshold_proposal(line, base_warn, base_cap, explicit_targets)
    elif direction == "decrease" and "cap" in message.casefold() and requested_increase is not None:
        step = _proposal_rounding_step(projected, current_warn, current_cap, requested_increase)
        recommended_cap = _round_up(max(projected, current_warn or 0, requested_increase), step)
        recommended_warn = current_warn if isinstance(current_warn, int) and current_warn <= recommended_cap else max(0, recommended_cap - step)
    else:
        recommended_warn, recommended_cap = _recommended_increase_thresholds(line, requested_increase)
    if not explicit_targets:
        recommended_warn, recommended_cap = _apply_revision_to_thresholds(
            line,
            recommended_warn,
            recommended_cap,
            revision,
            summary,
            requested_increase,
        )
    proposal = {
        "proposal_type": "adjust_budget_line_thresholds",
        "summary": f"Review {category} warning and hard-cap values.",
        "operations": [
            {
                "action": "update_budget_line",
                "budget_line_id": line_id,
                "category": category,
                "fields": {
                    "warn_at": recommended_warn,
                    "hard_cap": recommended_cap,
                },
            }
        ],
    }
    if explicit_targets and direction == "decrease" and "hard_cap" in explicit_targets and current_cap is not None and explicit_targets["hard_cap"] < current_cap:
        say = (
            f"I would not normally suggest lowering {category}, but because you asked for it "
            f"{_proposal_review_say(proposal, 'I prepared a proposal')}"
        )
    elif explicit_targets:
        say = _proposal_review_say(proposal, "I revised the proposal" if latest_proposal is not None else "I prepared a proposal")
    elif revision == "lower":
        say = (
            f"I adjusted the suggestion downward for {category}. It is still projected to reach {_format_cents(projected)} this month, "
            f"so I revised the warning amount to {_format_cents(recommended_warn)} and the hard cap to {_format_cents(recommended_cap)}."
        )
    elif revision == "higher":
        say = (
            f"I adjusted the suggestion upward for {category}. It is projected to reach {_format_cents(projected)} this month, "
            f"so I revised the warning amount to {_format_cents(recommended_warn)} and the hard cap to {_format_cents(recommended_cap)}."
        )
    elif requested_increase is not None and current_cap is not None and current_cap + requested_increase < projected:
        say = (
            f"A {_format_cents(requested_increase)} increase would still leave {category} below its projected {_format_cents(projected)} this month. "
            f"It is currently spent {_format_cents(actual)} with {_format_cents(planned)} planned, so I prepared a safer proposal to move "
            f"the warning amount from {_format_cents(current_warn)} to {_format_cents(recommended_warn)} and the hard cap from "
            f"{_format_cents(current_cap)} to {_format_cents(recommended_cap)}."
        )
    elif direction == "decrease" and "cap" in message.casefold() and requested_increase is not None:
        say = (
            f"I would not normally suggest lowering {category}, but because you asked for it I prepared a proposal to move "
            f"the warning amount to {_format_cents(recommended_warn)} and the hard cap to {_format_cents(recommended_cap)} for review."
        )
    else:
        say = (
            f"{category} is projected to reach {_format_cents(projected)} this month against its current warning amount of {_format_cents(current_warn)} "
            f"and hard cap of {_format_cents(current_cap)}. I prepared a proposal to move the warning amount to "
            f"{_format_cents(recommended_warn)} and the hard cap to {_format_cents(recommended_cap)} for you to review."
        )
    return {"mode": "proposal", "say": say[:500], "question": None, "proposal": proposal, "fallback": False}


def _affordability_reply(summary: dict, history: list[dict], message: str) -> dict:
    context = _recent_affordability_context(summary, history) or {}
    line = _extract_line_from_text(summary, message) or context.get("line") or _line_from_recent_context(summary, history)
    amount_cents = _find_contextual_amount_cents(summary, history, message)
    if amount_cents is None:
        amount_cents = context.get("amount_cents")
    if amount_cents is None:
        if line is not None:
            category = str(line.get("category") or "that category")
            actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
            planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
            projected = _line_projected_high(line)
            warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
            hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
            if hard_cap is not None and projected >= hard_cap:
                say = (
                    f"No, your {category} expenses this month are already exceeding your current budget. "
                    f"{category} is spent {_format_cents(actual)} with {_format_cents(planned)} planned, "
                    f"so the projected total is {_format_cents(projected)}, which is {_format_cents(projected - hard_cap)} over the hard cap of {_format_cents(hard_cap)}."
                )
                return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}
            if warn_at is not None and projected >= warn_at:
                if hard_cap is not None:
                    say = (
                        f"Maybe, but {category} is already in warning range this month. "
                        f"Based on your current budget and spending, you still have {_format_cents(hard_cap - projected)} left before reaching the hard cap."
                    )
                else:
                    say = (
                        f"Maybe, but {category} is already in warning range this month at a projected {_format_cents(projected)}."
                    )
                return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}
            limit_value = hard_cap if hard_cap is not None else warn_at
            limit_label = "hard cap" if hard_cap is not None else "warning amount"
            if limit_value is not None:
                say = (
                    f"Yes, based on your current budget and spending you still have {_format_cents(limit_value - projected)} left to spend on {category} this month "
                    f"before reaching the {limit_label}. {category} is currently spent {_format_cents(actual)} with {_format_cents(planned)} planned."
                )
                return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}
        say = "Tell me the rough amount you are considering and I will show how it changes this month's budget."
        return {"mode": "clarify", "say": say, "question": "amount_needed", "proposal": None, "fallback": False}
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    remaining_before = totals.get("remaining_income_high") if isinstance(totals.get("remaining_income_high"), int) else None
    remaining_after = None if remaining_before is None else remaining_before - amount_cents
    if line is None:
        say = (
            f"An extra {_format_cents(amount_cents)} would move your projected remaining income from {_format_cents(remaining_before)} "
            f"to {_format_cents(remaining_after)} for this month. Ask about a specific budget line if you want a warning or cap check as well."
        )
        return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}
    category = str(line.get("category") or "that category")
    actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
    planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
    current_projected = _line_projected_high(line)
    after_spend = current_projected + amount_cents
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    already_over_cap = hard_cap is not None and current_projected >= hard_cap
    if hard_cap is not None and after_spend >= hard_cap:
        headline = f"No, that would put {category} over its hard cap."
    elif warn_at is not None and after_spend >= warn_at:
        if warn_at is not None and current_projected >= warn_at:
            headline = f"Maybe, but {category} is already in warning range and this would add more pressure."
        else:
            headline = f"Maybe, but that would put {category} into warning range."
    else:
        headline = f"Yes, that still fits within {category}."
    line_tail = ""
    if hard_cap is not None and after_spend >= hard_cap:
        over_amount = after_spend - hard_cap
        if already_over_cap:
            line_tail = (
                f" It is already {_format_cents(current_projected - hard_cap)} over the hard cap of {_format_cents(hard_cap)}, "
                f"and this would take it to {_format_cents(over_amount)} over the hard cap of {_format_cents(hard_cap)}."
            )
        else:
            line_tail = f" That would be {_format_cents(over_amount)} over the hard cap of {_format_cents(hard_cap)}."
    elif hard_cap is not None and after_spend < hard_cap:
        line_tail = f" It would leave {_format_cents(hard_cap - after_spend)} before the hard cap."
    elif warn_at is not None and after_spend < warn_at:
        line_tail = f" It would leave {_format_cents(warn_at - after_spend)} before the warning amount."
    say = (
        f"{headline} {category} is currently spent {_format_cents(actual)} with {_format_cents(planned)} planned, "
        f"so adding {_format_cents(amount_cents)} would take the projected total to {_format_cents(after_spend)}. "
        f"Your projected remaining income would move from {_format_cents(remaining_before)} to {_format_cents(remaining_after)}.{line_tail}"
    )
    return {"mode": "advice", "say": say[:500], "question": None, "proposal": None, "fallback": False}


def _guardrail_exact_budget_reply(
    summary: dict,
    history: list[dict],
    message: str,
    kind: str,
    selected_integration: str,
    should_skip_deterministic: bool,
    result: dict | None = None,
) -> tuple[str, dict] | None:
    if should_skip_deterministic:
        return None
    if selected_integration != "auto":
        return None
    if not isinstance(result, dict):
        return None
    proposal_present = result.get("mode") == "proposal" or isinstance(result.get("proposal"), dict)
    if proposal_present and kind != "adjustments" and not _should_use_adjustment_context(summary, history, message):
        fallback = _deterministic_reply(summary, history, message)
        if fallback is not None:
            return "deterministic", fallback
    return None


def _should_use_affordability_context(summary: dict, history: list[dict], message: str) -> bool:
    lowered = message.casefold().strip()
    if _contains_any(
        lowered,
        RESET_TERMS
        + SUMMARY_TERMS
        + BUDGET_LINE_SUMMARY_TERMS
        + SPEND_MOST_TERMS
        + WARNING_TERMS
        + SAVINGS_TERMS
        + ADJUSTMENT_TERMS
        + UNBUDGETED_SPEND_TERMS,
    ):
        return False
    if _extract_line_from_text(summary, message) is not None and _contains_any(lowered, CATEGORY_REMAINING_TERMS + CATEGORY_SPEND_TERMS):
        return False
    if _looks_like_category_only_follow_up(summary, history, message):
        return True
    if _contains_any(lowered, AFFORDABILITY_TERMS):
        return True
    if _extract_line_from_text(summary, message) is not None and _recent_affordability_context(summary, history) is not None:
        return True
    if _is_amount_only_message(lowered):
        return _recent_affordability_context(summary, history) is not None
    if any(term in lowered for term in FOLLOW_UP_TERMS):
        return _recent_affordability_context(summary, history) is not None
    return False


def _should_use_adjustment_context(summary: dict, history: list[dict], message: str) -> bool:
    lowered = message.casefold().strip()
    if _is_acknowledgement(lowered):
        return False
    if _contains_any(lowered, RESET_TERMS + SUMMARY_TERMS + SPEND_MOST_TERMS + WARNING_TERMS + SAVINGS_TERMS + UNBUDGETED_SPEND_TERMS):
        return False
    if _extract_line_from_text(summary, message) is not None and _contains_any(lowered, CATEGORY_REMAINING_TERMS + CATEGORY_SPEND_TERMS):
        return False
    if _looks_like_adjustment_message(summary, lowered):
        return True
    context = _recent_adjustment_context(summary, history)
    if context is not None and (
        _has_proposal_feedback(lowered)
        or _contains_any(lowered, ADJUSTMENT_SUGGESTION_TERMS)
        or _extract_spend_floor_target_cents(lowered) is not None
    ):
        return True
    if _contains_any(lowered, ADJUSTMENT_FOLLOW_UP_TERMS) and (_recent_affordability_context(summary, history) or _latest_open_proposal_target_line(summary)):
        return True
    return _latest_open_proposal_target_line(summary) is not None and (
        _has_proposal_feedback(lowered)
        or _extract_spend_floor_target_cents(lowered) is not None
        or _contains_any(lowered, ADJUSTMENT_SUGGESTION_TERMS)
        or (
            _extract_adjustment_direction(lowered) is not None
            and (
                _find_amount_cents(lowered) is not None
                or "another" in lowered
                or "more" in lowered
            )
        )
    )


def _validated_history(history: object) -> list[dict]:
    if not isinstance(history, list):
        return []
    validated: list[dict] = []
    for turn in history[-12:]:
        if not isinstance(turn, dict):
            continue
        role = turn.get("role")
        content = turn.get("content")
        if role not in {"user", "assistant"}:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        validated.append({"role": role, "content": content.strip()})
    return validated


def _observation_snapshot(summary: dict, budget_id: str, history: list[dict], context: dict | None = None) -> dict:
    budget = summary.get("budget") if isinstance(summary.get("budget"), dict) else {}
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    transactions = summary.get("transactions") if isinstance(summary.get("transactions"), dict) else {}
    target_line = _line_from_context(summary, context or {})
    target_event = _find_planned_event(summary, (context or {}).get("target_planned_event_id"))
    return {
        "budget_id": budget_id,
        "month": budget.get("month"),
        "history_turn_count": len(history),
        "budget_line_count": len(_budget_lines(summary)),
        "planned_event_count": len(summary.get("planned_events")) if isinstance(summary.get("planned_events"), list) else 0,
        "active_proposal_count": len(_active_coach_proposals(summary)),
        "transactions_count": transactions.get("count"),
        "uncategorised_total": transactions.get("uncategorised_total"),
        "remaining_income_high": totals.get("remaining_income_high"),
        "projected_high_total": totals.get("projected_high_total"),
        "ui_action": (context or {}).get("ui_action"),
        "target_category": target_line.get("category") if isinstance(target_line, dict) else (context or {}).get("target_category"),
        "target_planned_event_id": target_event.get("id") if isinstance(target_event, dict) else None,
    }


def _plan_snapshot(summary: dict, message: str, kind: str, context: dict | None = None) -> dict:
    line = _extract_line_from_text(summary, message, context)
    line_id = line.get("id") if isinstance(line, dict) and isinstance(line.get("id"), int) else None
    return {
        "intent": kind,
        "target_budget_line_id": line_id,
        "target_category": line.get("category") if isinstance(line, dict) else None,
        "target_planned_event_id": (context or {}).get("target_planned_event_id"),
        "ui_action": (context or {}).get("ui_action"),
        "requested_amount_cents": _find_amount_cents(message),
        "conversation_mode": "proposal" if kind == "adjustments" else "advice",
        "human_confirmation_required": kind == "adjustments",
        "safety_checks": [
            "budget_must_exist",
            "responses_must_stay_grounded_in_budget_summary",
            "proposal_changes_require_user_approval",
        ],
    }


def _requires_deterministic_exact_answer(summary: dict, history: list[dict], message: str, kind: str) -> bool:
    if kind in {
        "summary",
        "unbudgeted-spend",
        "spend-most",
        "savings",
        "acknowledgement",
        "adjustments",
        "category-spend",
        "category-remaining",
    }:
        return True
    if kind == "amount-only":
        return (
            _recent_adjustment_context(summary, history) is not None
            or _latest_open_proposal_target_line(summary) is not None
        )
    if _should_use_adjustment_context(summary, history, message):
        return True
    return False


def _execution_plan(
    summary: dict,
    history: list[dict],
    message: str,
    kind: str,
    selected_integration: str,
    should_skip_deterministic: bool,
) -> dict:
    uses_computed_budget_facts = (
        kind in {"budget-line-summary", "affordability", "category-spend", "category-remaining"}
        or _should_use_affordability_context(summary, history, message)
    )
    if selected_integration == "mcp" or (selected_integration == "auto" and _looks_like_mcp_request(message)):
        return {
            "path": "mcp",
            "uses_mcp": True,
            "uses_rag": False,
            "uses_deterministic_facts": False,
            "reason": "transaction_evidence",
        }
    if selected_integration == "rag" or (selected_integration == "auto" and _looks_like_rag_request(message)):
        return {
            "path": "rag",
            "uses_mcp": False,
            "uses_rag": True,
            "uses_deterministic_facts": False,
            "reason": "grounded_guidance",
        }
    if not should_skip_deterministic and _requires_deterministic_exact_answer(summary, history, message, kind):
        return {
            "path": "deterministic",
            "uses_mcp": False,
            "uses_rag": False,
            "uses_deterministic_facts": True,
            "reason": "exact_budget_logic",
        }
    return {
        "path": "ollama",
        "uses_mcp": False,
        "uses_rag": False,
        "uses_deterministic_facts": uses_computed_budget_facts,
        "reason": "llm_first_auto",
    }


def _deterministic_reply(summary: dict, history: list[dict], message: str) -> dict | None:
    kind = _classify_message(summary, message)
    if kind == "summary":
        return _summary_reply(summary)
    if kind == "budget-line-summary":
        return _budget_line_summary_reply(summary, history, message)
    if kind == "unbudgeted-spend":
        return _unbudgeted_spend_reply(summary)
    if kind == "spend-most":
        return _spend_most_reply(summary)
    if kind == "overspending":
        return _overspending_reply(summary)
    if kind == "warnings":
        return _warning_reply(summary)
    if kind == "acknowledgement":
        return {"mode": "advice", "say": "Okay.", "question": None, "proposal": None, "fallback": False}
    if kind == "adjustments" or _should_use_adjustment_context(summary, history, message):
        return _adjustment_proposal_reply(summary, history, message)
    if kind == "savings":
        return _savings_reply(summary)
    if kind == "category-spend":
        return _category_spend_reply(summary, message)
    if kind == "category-remaining":
        return _category_remaining_reply(summary, message)
    if kind == "affordability" or _should_use_affordability_context(summary, history, message):
        return _affordability_reply(summary, history, message)
    return None


def _format_currency_from_amount(value: object) -> str:
    if isinstance(value, bool):
        return "$0.00"
    if isinstance(value, (int, float)):
        return f"${float(value):,.2f}"
    return "$0.00"


def _mcp_budget_context(summary: dict, message: str, context: dict | None = None) -> tuple[dict | None, dict]:
    line = _extract_line_from_text(summary, message, context)
    if line is None:
        top_lines = _top_pressure_lines(summary, limit=1)
        line = top_lines[0] if top_lines else None
    budget = summary.get("budget") if isinstance(summary.get("budget"), dict) else {}
    bounds = _month_date_bounds(budget.get("month"))
    arguments: dict[str, object] = {}
    if bounds is not None:
        arguments["start_date"], arguments["end_date"] = bounds
    if isinstance(line, dict) and isinstance(line.get("category"), str) and line.get("category").strip():
        arguments["category_name"] = line.get("category").strip()
    return line, arguments


def _normalise_mcp_tool_result(summary: dict, message: str, context: dict | None = None) -> tuple[str | None, dict]:
    line, arguments = _mcp_budget_context(summary, message, context)
    rows = mcp_client.call_tool("search_transactions", arguments)
    if not isinstance(rows, list):
        raise ServiceError("The MCP server returned an invalid result.", 502, "mcp_invalid_result")
    category = line.get("category") if isinstance(line, dict) else None
    amount_total = 0.0
    preview: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        amount = row.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            amount_total += float(amount)
        if index >= 5:
            continue
        preview.append(
            {
                "date": row.get("date"),
                "merchant": row.get("merchant"),
                "description": row.get("description"),
                "amount": row.get("amount"),
                "category_name": row.get("category_name"),
            }
        )
    tool_result = {
        "tool_name": "search_transactions",
        "arguments": arguments,
        "count": len(rows),
        "scope": category or "the current budget month",
        "total_matched_amount": round(amount_total, 2),
        "result_preview": preview,
    }
    return category, tool_result


def _mcp_reply(summary: dict, message: str, context: dict | None = None) -> dict:
    category, tool_result = _normalise_mcp_tool_result(summary, message, context)
    count = tool_result.get("count", 0)
    amount_total = tool_result.get("total_matched_amount", 0.0)
    if isinstance(amount_total, int):
        amount_total = float(amount_total)
    if count:
        scope = category or "the current budget month"
        reply = (
            f"I used the shared MCP transaction search for {scope} and found {count} matching transaction"
            f"{'' if count == 1 else 's'} totalling {_format_currency_from_amount(amount_total)}."
        )
    else:
        reply = "I used the shared MCP transaction search but could not find matching transactions for this budget context."
    return {
        "mode": "advice",
        "say": reply,
        "question": None,
        "proposal": None,
        "fallback": False,
        "tool_result": tool_result,
        "grounding": None,
    }


def _confidence_for_distance(distance: float | None) -> str:
    if distance is None:
        return "none"
    if distance < config.RAG_HIGH:
        return "high"
    if distance < config.RAG_MEDIUM:
        return "medium"
    if distance <= config.RAG_LOW:
        return "low"
    return "none"


def _grounding_citation(chunk: dict) -> dict:
    metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
    source = str(metadata.get("source") or chunk.get("id") or "retrieved-source")
    first_line = chunk.get("text", "").splitlines()[0].lstrip("# ").strip() if isinstance(chunk.get("text"), str) else source
    return {
        "source": source,
        "label": first_line or source,
        "distance": round(float(chunk.get("distance", 0.0)), 3),
    }


def _line_threshold_state(line: dict, projected: int) -> str:
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    if hard_cap is not None and projected >= hard_cap:
        return "over_hard_cap"
    if warn_at is not None and projected >= warn_at:
        return "warning"
    if hard_cap is not None or warn_at is not None:
        return "within_thresholds"
    return "no_thresholds"


def _line_pressure_fact(line: dict) -> dict:
    projected = _line_projected_high(line)
    actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
    planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    return {
        "budget_line_id": line.get("id") if isinstance(line.get("id"), int) else None,
        "category": line.get("category"),
        "actual_spend_cents": actual,
        "planned_spend_cents": planned,
        "projected_spend_cents": projected,
        "warn_at_cents": warn_at,
        "hard_cap_cents": hard_cap,
        "remaining_before_warning_cents": _remaining_before(warn_at, projected),
        "remaining_before_hard_cap_cents": _remaining_before(hard_cap, projected),
        "primary_pressure_source": "planned" if planned > actual else "actual" if actual > 0 else "none",
        "threshold_state": _line_threshold_state(line, projected),
    }


def _affordability_fact_pack(summary: dict, history: list[dict], message: str, context: dict | None = None) -> dict:
    recent_context = _recent_affordability_context(summary, history) or {}
    line = _extract_line_from_text(summary, message, context) or recent_context.get("line") or _line_from_recent_context(summary, history)
    amount_cents = _find_contextual_amount_cents(summary, history, message)
    if amount_cents is None:
        amount_cents = recent_context.get("amount_cents")
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    remaining_before = totals.get("remaining_income_high") if isinstance(totals.get("remaining_income_high"), int) else None
    remaining_after = None if remaining_before is None or not isinstance(amount_cents, int) else remaining_before - amount_cents

    pack: dict[str, object] = {
        "kind": "affordability",
        "requested_amount_cents": amount_cents,
        "remaining_income_before_cents": remaining_before,
        "remaining_income_after_cents": remaining_after,
        "needs_amount_clarification": amount_cents is None,
        "line": None,
    }
    if not isinstance(line, dict):
        return pack

    current_projected = _line_projected_high(line)
    after_spend = None if not isinstance(amount_cents, int) else current_projected + amount_cents
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    line_pack = _line_pressure_fact(line)
    line_pack["projected_after_spend_cents"] = after_spend
    line_pack["threshold_state_after_spend"] = _line_threshold_state(line, after_spend) if isinstance(after_spend, int) else None
    line_pack["remaining_before_warning_after_spend_cents"] = _remaining_before(warn_at, after_spend) if isinstance(after_spend, int) else None
    line_pack["remaining_before_hard_cap_after_spend_cents"] = _remaining_before(hard_cap, after_spend) if isinstance(after_spend, int) else None
    pack["line"] = line_pack
    return pack


def _pressure_fact_pack(summary: dict, message: str, context: dict | None = None) -> dict | None:
    focus_line = _extract_line_from_text(summary, message, context) or _line_from_context(summary, context or {})
    top_lines = _top_pressure_lines(summary, limit=3)
    if focus_line is None and not top_lines:
        return None
    return {
        "kind": "pressure",
        "focus_line": _line_pressure_fact(focus_line) if isinstance(focus_line, dict) else None,
        "top_lines": [_line_pressure_fact(line) for line in top_lines if isinstance(line, dict)],
    }


def _grounding_payload(retrieved: list[dict]) -> dict | None:
    kept = [item for item in retrieved if item.get("distance") is not None and float(item["distance"]) <= config.RAG_LOW]
    if not kept:
        return None
    citations = [_grounding_citation(chunk) for chunk in kept]
    confidence = _confidence_for_distance(min(citation["distance"] for citation in citations)) if citations else "none"
    return {
        "citations": citations,
        "confidence": confidence,
        "insufficient_context": False,
        "retrieval": [
            {
                "id": item.get("id"),
                "source": (item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("source"),
                "distance": round(float(item.get("distance", 0.0)), 3),
                "text": item.get("text"),
            }
            for item in kept
        ],
    }


def _grounded_focus_line(summary: dict, context: dict | None = None) -> dict | None:
    contextual_line = _line_from_context(summary, context or {})
    if contextual_line is not None:
        return contextual_line
    def score(line: dict) -> tuple[int, int, int]:
        projected = _line_projected_high(line)
        warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) and line.get("warn_at") > 0 else None
        hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) and line.get("hard_cap") > 0 else None
        threshold = warn_at if warn_at is not None else hard_cap
        ratio = int((projected / threshold) * 1000) if threshold else 0
        planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
        return (ratio, planned, projected)

    lines = _budget_lines(summary)
    if not lines:
        return None
    return max(lines, key=score)


def _fallback_grounded_answer(summary: dict, context: dict | None = None) -> str | None:
    line = _grounded_focus_line(summary, context)
    if line is None:
        return None
    category = str(line.get("category") or "that category")
    actual = line.get("actual_spend") if isinstance(line.get("actual_spend"), int) else 0
    planned = line.get("planned_est_high_total") if isinstance(line.get("planned_est_high_total"), int) else 0
    projected = _line_projected_high(line)
    warn_at = line.get("warn_at") if isinstance(line.get("warn_at"), int) else None
    hard_cap = line.get("hard_cap") if isinstance(line.get("hard_cap"), int) else None

    if planned > 0 and actual <= 0:
        base = (
            f"This month, focus on {category}. It is under the most pressure based on week-ahead planned spending: "
            f"{_format_cents(planned)} planned and {_format_cents(projected)} projected in total."
        )
    else:
        base = (
            f"This month, focus on {category}. It is under the most pressure with "
            f"{_format_cents(actual)} already spent and {_format_cents(projected)} projected in total."
        )

    if isinstance(hard_cap, int) and projected > hard_cap:
        threshold = f" That is {_format_cents(projected - hard_cap)} over the hard cap of {_format_cents(hard_cap)}."
    elif isinstance(warn_at, int) and projected >= warn_at:
        threshold = f" That puts it at or above the warning threshold of {_format_cents(warn_at)}."
    elif isinstance(warn_at, int):
        threshold = f" It is the closest category to the warning threshold of {_format_cents(warn_at)}."
    elif isinstance(hard_cap, int):
        threshold = f" It is the closest category to the hard cap of {_format_cents(hard_cap)}."
    else:
        threshold = ""

    return f"{base}{threshold} Keep planned events in mind before increasing spend elsewhere."


def _rag_reply(summary: dict, message: str, context: dict | None = None) -> dict:
    retrieved = rag_client.retrieve(config.RAG_FEATURE, message, config.RAG_TOP_K)
    grounding = _grounding_payload(retrieved)
    if grounding is None:
        return {
            "mode": "advice",
            "say": "I couldn't find enough relevant shared budget guidance to answer that confidently.",
            "question": None,
            "proposal": None,
            "fallback": False,
            "tool_result": None,
            "grounding": {
                "citations": [],
                "confidence": "none",
                "insufficient_context": True,
                "retrieval": [],
            },
        }
    kept = [
        {
            "id": item["id"],
            "text": item["text"],
            "metadata": item.get("metadata", {}),
            "distance": item["distance"],
        }
        for item in retrieved
        if item.get("distance") is not None and float(item["distance"]) <= config.RAG_LOW
    ]
    data = guard.run(
        config.CHAT_MODEL,
        lambda error: grounded_prompt.build(message, summary, kept, error),
        validate_grounded_answer,
        grounded_prompt.FALLBACK,
    )
    by_source = {
        str((item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("source") or item.get("id")): item
        for item in kept
    }
    cited_chunks = [by_source[source] for source in data.get("cited", []) if source in by_source]
    citations = [_grounding_citation(chunk) for chunk in cited_chunks] if cited_chunks else []
    confidence = grounding["confidence"] if not citations else _confidence_for_distance(min(citation["distance"] for citation in citations))
    answer = data.get("answer", "").strip()
    insufficient = bool(data.get("insufficient_context")) or not citations
    if insufficient:
        fallback_answer = _fallback_grounded_answer(summary, context)
        if fallback_answer is not None:
            answer = fallback_answer
            citations = [_grounding_citation(kept[0])]
            confidence = _confidence_for_distance(citations[0]["distance"])
            insufficient = False
    if insufficient:
        answer = "I couldn't ground a reliable answer from the shared budget guidance for that question."
    return {
        "mode": "advice",
        "say": answer,
        "question": None,
        "proposal": None,
        "fallback": bool(data.get("fallback")),
        "tool_result": None,
        "grounding": {
            "citations": citations,
            "confidence": confidence,
            "insufficient_context": insufficient,
            "retrieval": grounding["retrieval"],
        },
    }


def _should_fetch_ollama_mcp_context(summary: dict, message: str, kind: str, context: dict | None = None) -> bool:
    ui_action = str((context or {}).get("ui_action") or "").strip()
    if ui_action in {"line-transactions", "event-transactions"}:
        return True
    lowered = message.casefold()
    if _contains_any(lowered, MCP_TRANSACTION_TERMS):
        return True
    if _extract_line_from_text(summary, message, context) is not None and _contains_any(lowered, COACHING_CONTEXT_TERMS):
        return True
    if kind in {"budget-line-summary", "spend-most", "overspending", "warnings"}:
        return _extract_line_from_text(summary, message, context) is not None or _line_from_context(summary, context or {}) is not None
    return False


def _should_fetch_ollama_rag_context(summary: dict, message: str, kind: str, context: dict | None = None) -> bool:
    ui_action = str((context or {}).get("ui_action") or "").strip()
    if ui_action in {"line-grounded-advice", "event-grounded-advice"}:
        return True
    lowered = message.casefold()
    if _contains_any(lowered, RAG_GUIDANCE_TERMS + COACHING_CONTEXT_TERMS):
        return True
    return kind in {"summary", "budget-line-summary", "spend-most", "overspending", "warnings", "savings"}


def _build_ollama_chat_context(summary: dict, history: list[dict], message: str, kind: str, context: dict | None = None) -> dict:
    chat_context: dict[str, object] = {
        "summary": summary,
        "history": history,
        "context_hints": context or {},
        "computed_budget_context": None,
        "mcp_transactions": None,
        "rag_guidance": None,
    }
    if kind == "affordability" or _should_use_affordability_context(summary, history, message):
        chat_context["computed_budget_context"] = _affordability_fact_pack(summary, history, message, context)
    elif (
        kind in {"summary", "budget-line-summary", "spend-most", "overspending", "warnings", "savings"}
        or (
            _extract_line_from_text(summary, message, context) is not None
            and _contains_any(message.casefold(), COACHING_CONTEXT_TERMS)
        )
        or _line_from_context(summary, context or {}) is not None
    ):
        chat_context["computed_budget_context"] = _pressure_fact_pack(summary, message, context)
    if _should_fetch_ollama_mcp_context(summary, message, kind, context):
        try:
            _category, tool_result = _normalise_mcp_tool_result(summary, message, context)
        except ServiceError:
            tool_result = None
        chat_context["mcp_transactions"] = tool_result
    if _should_fetch_ollama_rag_context(summary, message, kind, context):
        try:
            retrieved = rag_client.retrieve(config.RAG_FEATURE, message, config.RAG_TOP_K)
        except ServiceError:
            retrieved = []
        grounding = _grounding_payload(retrieved)
        if grounding is not None:
            chat_context["rag_guidance"] = grounding
    return chat_context


def send_message(
    budget_id: object,
    message: object,
    history: object = None,
    integration_mode: object = None,
    context: object = None,
    skip_deterministic: object = None,
) -> dict:
    if isinstance(budget_id, bool) or not isinstance(budget_id, int):
        raise ServiceError("budget_id must be an integer", 422, "invalid_field")
    if not isinstance(message, str) or not message.strip():
        raise ServiceError("message is required", 400, "missing_required_fields")

    budget_id_text = str(budget_id)
    trimmed_message = message.strip()
    conversation_history = _validated_history(history)
    selected_integration = _normalise_integration_mode(integration_mode)
    should_skip_deterministic = _normalise_skip_deterministic(skip_deterministic)
    db_api.get_budget(budget_id_text)
    summary = summary_service.build_budget_summary(budget_id_text)
    normalised_context = _normalise_context(context)
    effective_message = _contextualised_message(summary, trimmed_message, normalised_context)
    user_message = {"role": "user", "content": effective_message}
    kind = _classify_message(summary, effective_message)
    observation = _observation_snapshot(summary, budget_id_text, conversation_history, normalised_context)
    execution_plan = _execution_plan(
        summary,
        conversation_history,
        effective_message,
        kind,
        selected_integration,
        should_skip_deterministic,
    )
    plan = _plan_snapshot(summary, effective_message, kind, normalised_context)
    plan["execution_path"] = execution_plan["path"]
    plan["execution_reason"] = execution_plan["reason"]
    plan["uses_mcp"] = execution_plan["uses_mcp"]
    plan["uses_rag"] = execution_plan["uses_rag"]
    plan["uses_deterministic_facts"] = execution_plan["uses_deterministic_facts"]
    response_source = "deterministic"
    if execution_plan["path"] == "mcp":
        response_source = "mcp"
        result = _mcp_reply(summary, effective_message, normalised_context)
    elif execution_plan["path"] == "rag":
        response_source = "rag"
        result = _rag_reply(summary, effective_message, normalised_context)
    elif execution_plan["path"] == "deterministic":
        result = _deterministic_reply(summary, conversation_history, effective_message)
    else:
        result = None
    if result is None:
        response_source = "ollama"
        ollama_context = _build_ollama_chat_context(
            summary,
            conversation_history,
            effective_message,
            kind,
            normalised_context,
        )
        result = guard.run(
            config.CHAT_MODEL,
            lambda error: chat_prompt.build(effective_message, conversation_history, summary, ollama_context, error),
            validate_chat_response,
            chat_prompt.FALLBACK,
        )
        result = dict(result)
        if isinstance(ollama_context.get("mcp_transactions"), dict):
            result["tool_result"] = ollama_context["mcp_transactions"]
        if isinstance(ollama_context.get("rag_guidance"), dict):
            result["grounding"] = ollama_context["rag_guidance"]
        exact_guardrail = _guardrail_exact_budget_reply(
            summary,
            conversation_history,
            effective_message,
            kind,
            selected_integration,
            should_skip_deterministic,
            result,
        )
        if exact_guardrail is not None:
            response_source, result = exact_guardrail
    if isinstance(result, dict) and isinstance(result.get("proposal"), dict):
        result = dict(result)
        result["proposal"] = _hydrate_proposal(summary, result["proposal"])
    result = _normalise_proposal_reply(result)
    stored_proposal = None
    reused_existing_proposal = False
    replaced_proposal_ids: list[int] = []
    if isinstance(result.get("proposal"), dict):
        active_proposals = _active_coach_proposals(summary)
        matching_existing = next(
            (
                proposal for proposal in active_proposals
                if _normalised_proposal(proposal.get("proposal_json")) == _normalised_proposal(result["proposal"])
            ),
            None,
        )
        if matching_existing is not None:
            stored_proposal = matching_existing
            reused_existing_proposal = True
        else:
            target_line_ids = set(_proposal_line_ids(result["proposal"]))
            for proposal in active_proposals:
                proposal_id = proposal.get("id")
                proposal_line_ids = set(_proposal_line_ids(proposal.get("proposal_json")))
                if not isinstance(proposal_id, int) or not target_line_ids.intersection(proposal_line_ids):
                    continue
                db_api.delete_coach_proposal(str(proposal_id))
                replaced_proposal_ids.append(proposal_id)
            stored_proposal, _status = db_api.create_coach_proposal(
                budget_id_text,
                {
                    "proposal_json": result["proposal"],
                    "rationale": result["say"],
                },
            )
    assistant_message = {
        "role": "assistant",
        "content": result["say"],
        "mode": result["mode"],
        "response_source": response_source,
        "tool_result": result.get("tool_result"),
        "grounding": result.get("grounding"),
    }
    return {
        "reply": result["say"],
        "mode": result["mode"],
        "question": result.get("question"),
        "proposal": stored_proposal,
        "fallback": bool(result.get("fallback")),
        "response_source": response_source,
        "tool_result": result.get("tool_result"),
        "grounding": result.get("grounding"),
        "stage_trace": ["observe", "plan", "act", "adapt"],
        "agentic_workflow": {
            "observe": observation,
            "plan": plan,
            "act": {
                "response_source": response_source,
                "used_model": response_source == "ollama",
                "fallback_used": bool(result.get("fallback")),
                "response_mode": result["mode"],
                "mcp_context_used": isinstance(result.get("tool_result"), dict),
                "rag_context_used": isinstance(result.get("grounding"), dict),
            },
            "adapt": {
                "proposal_created": stored_proposal is not None and not reused_existing_proposal,
                "reused_existing_proposal": reused_existing_proposal,
                "replaced_proposal_ids": replaced_proposal_ids,
                "proposal_id": stored_proposal.get("id") if isinstance(stored_proposal, dict) else None,
            },
        },
        "user_message": user_message,
        "assistant_message": assistant_message,
        "messages_to_store": [
            {"role": "user", "content": effective_message},
            {
                "role": "assistant",
                "content": result["say"],
                "mode": result["mode"],
                "response_source": response_source,
                "plan_json": plan,
                "observation_json": observation,
                "stage_trace": ["observe", "plan", "act", "adapt"],
                "tool_result_json": result.get("tool_result"),
                "grounding_json": result.get("grounding"),
                "proposal_id": stored_proposal.get("id") if isinstance(stored_proposal, dict) else None,
            },
        ],
    }
