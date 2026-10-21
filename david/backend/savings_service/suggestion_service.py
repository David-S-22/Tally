from datetime import date
import json
import os
import requests
from typing import Any
from shared.backend import dto
from .helpers import fetch_categories, fetch_feedbacks, fetch_goals, fetch_suggestions
from .ollama_service import (
    MCP_ENABLED,
    MULTI_AGENT_ENABLED,
    RAG_ENABLED,
    call_ai_to_select_tool,
    execute_mcp_tool,
    fetch_mcp_tools,
    get_ai_text_and_calculate_confidence,
    load_prompt,
    planner_model,
    search_model,
)

def _get_item_field(item: dict[str, Any] | Any, field: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(field, default)
    return getattr(item, field, default)

def _format_amount(value: int | float | str | None) -> str:
    """Formats numeric values as currency strings ($X.XX)."""
    try:
        return f"${float(value):.2f}"
    except Exception:
        return f"${value}"

def _format_active_goals(goals: list[dto.Goal] | list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Extracts and formats active goals for the planner prompt."""
    return [
        {
            "goal": str(_get_item_field(goal, "name", "") or ""),
            "target_amount": _format_amount(_get_item_field(goal, "cost", 0)),
            "deadline": str(_get_item_field(goal, "date", "") or "")[:10],
        }
        for goal in (goals or [])
    ]

def _format_past_suggestions(suggestions: list[dto.Suggestion] | list[dict[str, Any]] | None) -> list[str]:
    """Formats past suggestions with their acceptance status and user feedback rationale (newest first)."""
    formatted_suggestions = []
    for suggestion in reversed(suggestions or []):
        text = str(_get_item_field(suggestion, "suggestion", "") or "").strip()
        if not text:
            continue
        is_accepted = bool(_get_item_field(suggestion, "accepted", False))
        feedback_comment = _get_item_field(suggestion, "feedback", None)
        status = "ACCEPTED" if is_accepted else "REJECTED"
        if feedback_comment:
            formatted_suggestions.append(f'[{status}] "{text}" -> User Feedback: "{feedback_comment}"')
        else:
            formatted_suggestions.append(f'[{status}] "{text}"')
    return formatted_suggestions


def _format_feedback_for_planner(
    feedbacks: list[dto.Feedback] | list[dict[str, Any]] | None,
    category_map: dict[int, str],
) -> list[dict[str, Any]]:
    """Formats all user feedback and preferences for the planner prompt (newest first)."""
    preferences = []
    for feedback_item in reversed(feedbacks or []):
        rule_text = str(_get_item_field(feedback_item, "feedback", "") or "").strip()
        if not rule_text:
            continue
        preference: dict[str, Any] = {"preference": rule_text}
        sugg_id = _get_item_field(feedback_item, "suggestion_id")
        if sugg_id is not None:
            preference["on_suggestion_id"] = sugg_id
        category_id = _get_item_field(feedback_item, "category_id")
        if category_id is not None and category_id in category_map:
            preference["category"] = category_map[category_id]
        timeframe = _get_item_field(feedback_item, "timeframe")
        if timeframe:
            preference["timeframe"] = str(timeframe)
        preferences.append(preference)
    return preferences


def _format_feedback_for_search(
    feedbacks: list[dto.Feedback] | list[dict[str, Any]] | None,
    category_map: dict[int, str],
) -> tuple[str, str]:
    """Formats user feedback for the transaction search prompt in a single pass (newest first).

    Returns a tuple of:
      - category_timeframe_feedback: Numbered list of feedbacks that specify a category or timeframe,
        used by the search model to pick query filters (category_name, start_date, end_date).
      - background_preferences: Bulleted list of general constraint feedbacks without category/timeframe.
    """
    category_timeframe_lines: list[str] = []
    background_lines: list[str] = []

    for item in reversed(feedbacks or []):
        text = str(_get_item_field(item, "feedback", "") or "").strip()
        if not text:
            continue

        category_id = _get_item_field(item, "category_id")
        timeframe = _get_item_field(item, "timeframe")

        if category_id is not None or timeframe is not None:
            details = []
            category_name = category_map.get(category_id) if category_id is not None else None
            if category_name:
                details.append(f"Category: {category_name}")
            if timeframe:
                details.append(f"Timeframe: {timeframe}")

            detail_str = f" [{', '.join(details)}]" if details else ""
            index = len(category_timeframe_lines) + 1
            tag = " (Newest)" if index == 1 else ""
            category_timeframe_lines.append(f'{index}.{tag} "{text}"{detail_str}')
        else:
            background_lines.append(f"- {text}")

    cat_tf_str = "\n".join(category_timeframe_lines) if category_timeframe_lines else "None"
    bg_str = "\n".join(background_lines) if background_lines else "None"
    return cat_tf_str, bg_str


def format_planner_prompt(
    goals: list[dto.Goal] | list[dict[str, Any]] | None,
    suggestions: list[dto.Suggestion] | list[dict[str, Any]] | None,
    feedbacks: list[dto.Feedback] | list[dict[str, Any]] | None,
    tx_url: str | None = None,
) -> str:
    """Constructs the structured JSON data block for the 8B planner prompt."""
    categories = fetch_categories(tx_url)
    category_map = {category.id: category.name for category in categories} if categories else {}
    tables: dict[str, Any] = {
        "active_goals": _format_active_goals(goals),
        "recent_suggestions_and_feedback": _format_past_suggestions(suggestions),
        "user_preferences_and_constraints": _format_feedback_for_planner(feedbacks, category_map),
    }
    return f"User Financial Data:\n{json.dumps(tables, separators=(',', ':'))}"


def generate_transaction_search_tool_call(
    feedbacks: list[dto.Feedback] | list[dict[str, Any]] | None,
    tx_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Prompts the search tool model with all available MCP tools to select the transaction querying tool and arguments based on user feedback."""
    tools = fetch_mcp_tools()
    if not tools:
        return "search_transactions", {}

    categories = fetch_categories(tx_url)
    category_names = [category.name for category in categories] if categories else []
    category_map = {category.id: category.name for category in categories} if categories else {}
    today_str = date.today().isoformat()

    category_timeframe_feedback, background_preferences = _format_feedback_for_search(feedbacks, category_map)

    search_prompt = load_prompt("search_prompt.txt").format(
        today=today_str,
        category_timeframe_feedback=category_timeframe_feedback,
        background_preferences=background_preferences,
        valid_categories=", ".join(category_names) if category_names else "None",
    ).strip()

    if category_names:
        for tool_definition in tools:
            props = (
                tool_definition.get("function", {})
                .get("parameters", {})
                .get("properties", {})
            )
            if "category_name" in props:
                props["category_name"]["enum"] = category_names

    system_prompt = (
        "You are a financial data assistant. Select and invoke the tool that queries user transactions. "
        "Do not invoke document retrieval or other tools."
    )

    tool_name, tool_args = call_ai_to_select_tool(
        search_prompt,
        tools=tools,
        model=search_model,
        system_prompt=system_prompt,
        temperature=0.0,
    )

    cat_val = tool_args.get("category_name")
    if cat_val and category_names:
        matched_cat = next(
            (c for c in category_names if c.lower() == str(cat_val).lower()),
            None,
        )
        if matched_cat:
            tool_args["category_name"] = matched_cat

    cleaned_args = {key: value for key, value in tool_args.items() if value is not None and value != ""}
    return tool_name or "search_transactions", cleaned_args


def generate_context_retrieval_args(
    goals: list[dto.Goal] | list[dict[str, Any]] | None = None,
    k: int = 3,
) -> dict[str, str | int]:
    """
    Constructs arguments for the retrieve_context MCP tool targeting savings advice.
    Formulates a targeted question on how to generate savings advice.
    """
    question = "advice about how to generate savings advice"
    if goals:
        goal_name = _get_item_field(goals[0], "name")
        if goal_name:
            question = f"advice about how to generate savings advice towards {goal_name}"
    return {
        "feature": "savings",
        "question": question,
        "k": k,
    }

def _format_transactions_for_prompt(
    transactions: list[dto.Transaction] | list[dict[str, Any]] | None,
    category_map: dict[int, str],
) -> list[dict[str, str]]:
    """Formats and enriches transactions with readable dates, amounts, and category names."""
    formatted = []
    for tx in (transactions or []):
        item = {
            "date": str(_get_item_field(tx, "date", ""))[:10],
            "merchant": str(_get_item_field(tx, "merchant", "") or ""),
            "description": str(_get_item_field(tx, "description", "") or ""),
            "amount": _format_amount(_get_item_field(tx, "amount", 0)),
        }
        category_id = _get_item_field(tx, "category_id")
        if category_id is not None and category_id in category_map:
            item["category"] = category_map[category_id]
        formatted.append(item)
    return formatted

def _aggregate_spending_by_merchant(
    transactions: list[dto.Transaction] | list[dict[str, Any]] | None,
    category_map: dict[int, str],
) -> list[dict[str, Any]]:
    """Aggregates spending totals and transaction counts grouped by merchant."""
    merchants: dict[str, dict[str, Any]] = {}
    for tx in (transactions or []):
        merchant = str(_get_item_field(tx, "merchant") or "Unknown").strip()
        try:
            amount = float(_get_item_field(tx, "amount", 0))
        except (ValueError, TypeError):
            amount = 0.0

        category_id = _get_item_field(tx, "category_id")
        category_name = None
        if category_id is not None and category_id in category_map:
            category_name = category_map[category_id]
        elif _get_item_field(tx, "category") is not None:
            category_name = _get_item_field(tx, "category")

        if merchant not in merchants:
            merchants[merchant] = {
                "merchant": merchant,
                "total_spent_raw": 0.0,
                "transaction_count": 0,
                "categories": set(),
            }
        merchants[merchant]["total_spent_raw"] += amount
        merchants[merchant]["transaction_count"] += 1
        if category_name:
            merchants[merchant]["categories"].add(category_name)

    sorted_merchants = sorted(
        merchants.values(),
        key=lambda m: m["total_spent_raw"],
        reverse=True,
    )

    aggregated = []
    for m in sorted_merchants:
        aggregated.append({
            "merchant": m["merchant"],
            "total_spent": _format_amount(m["total_spent_raw"]),
            "transaction_count": m["transaction_count"],
            "category": ", ".join(sorted(m["categories"])) if m["categories"] else "Uncategorized",
        })
    return aggregated

def _fetch_filtered_transactions(
    feedbacks: list[dto.Feedback] | list[dict[str, Any]] | None,
    tx_url: str | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """
    Executes transaction search tool call derived from user feedbacks.
    Returns (transactions, None) on success, or (None, fallback_message) if no transactions exist.
    """
    if not MCP_ENABLED:
        url = (tx_url or os.environ.get("TRANSACTIONS_DB_URL", "http://localhost:6001")).rstrip("/")
        try:
            response = requests.get(f"{url}/transactions", timeout=5)
            response.raise_for_status()
            transactions = response.json()
            if isinstance(transactions, list) and transactions:
                return transactions, None
        except (requests.RequestException, ValueError):
            pass
        return None, "You don't have any transactions yet. Add transactions using the transactions tab."

    tool_name, search_args = generate_transaction_search_tool_call(feedbacks, tx_url=tx_url)
    transactions = execute_mcp_tool(tool_name, search_args)
    if not transactions:
        if search_args:
            all_transactions = execute_mcp_tool(tool_name, {})
            if all_transactions:
                filter_parts = []
                if "category_name" in search_args:
                    filter_parts.append(f"in category '{search_args['category_name']}'")
                if "start_date" in search_args or "end_date" in search_args:
                    filter_parts.append("within the specified timeframe")
                filter_str = " ".join(filter_parts) if filter_parts else "matching your filter"
                return None, f"No transactions found {filter_str}. Try broadening your feedback or checking other categories."
        return None, "You don't have any transactions yet. Add transactions using the transactions tab."
    return transactions, None


def _extract_rag_sources(rag_docs: list[dict[str, Any]]) -> str:
    """Extracts unique source citations from retrieved RAG documents."""
    sources: list[str] = []
    for doc in rag_docs:
        source_name = doc.get("metadata", {}).get("source")
        if source_name and source_name not in sources:
            sources.append(source_name)
    return ", ".join(sources) if sources else "savings_advice_guide.md"


def _fetch_rag_guidelines(
    goals: list[dto.Goal] | list[dict[str, Any]] | None,
) -> tuple[str, str] | None:
    """
    Retrieves savings advice guidelines from the RAG knowledge base via MCP retrieve_context.
    Returns (context_blocks, sources_str) if sufficient context exists, otherwise None.
    """
    if not MCP_ENABLED or not RAG_ENABLED:
        return None

    rag_args = generate_context_retrieval_args(goals)
    try:
        rag_response = execute_mcp_tool("retrieve_context", rag_args)
    except Exception:
        rag_response = None

    rag_results = rag_response.get("results", []) if isinstance(rag_response, dict) else []
    valid_rag_docs = [doc for doc in rag_results if doc.get("text", "").strip()]

    # Ensure advice is NOT generated if the AI has an insufficient amount of context
    if not valid_rag_docs or sum(len(doc.get("text", "").strip()) for doc in valid_rag_docs) < 30:
        return None

    sources_str = _extract_rag_sources(valid_rag_docs)
    rag_context_blocks = "\n".join([
        f"- [Source: {doc.get('metadata', {}).get('source', 'Unknown')}]: {doc.get('text', '').strip()}"
        for doc in valid_rag_docs
    ])
    return rag_context_blocks, sources_str


def _build_advice_prompt(
    user_data: str,
    spending_summary: list[dict[str, Any]],
    formatted_transactions: list[dict[str, str]],
    rag_context_blocks: str | None,
    previous_suggestion: str | None = None,
) -> str:
    """Keep every transaction in compact tables, with advice rules in the task."""
    previous_block = ""
    if previous_suggestion and str(previous_suggestion).strip():
        previous_block = (
            f"Previous Suggestion: \"{str(previous_suggestion).strip()}\"\n\n"
        )

    merchant_columns = ["merchant", "total_spent", "transaction_count", "category"]
    merchant_table = {
        "columns": merchant_columns,
        "rows": [[merchant.get(column, "") for column in merchant_columns] for merchant in spending_summary],
    }
    transaction_columns = ["date", "merchant", "description", "amount", "category"]
    transaction_table = {
        "columns": transaction_columns,
        "rows": [[tx.get(column, "") for column in transaction_columns] for tx in formatted_transactions],
    }
    prompt = (
        f"{user_data}\n\n"
        f"{previous_block}"
        f"Pre-Calculated Spending Summary by Merchant:\n{json.dumps(merchant_table, separators=(',', ':'))}\n\n"
        f"Retrieved Transactions:\n{json.dumps(transaction_table, separators=(',', ':'))}"
    )
    if rag_context_blocks:
        prompt += f"\n\nRetrieved Savings Advice Guidelines:\n{rag_context_blocks}"
    return prompt


def generate_savings_suggestion(user_prompt: str, system_prompt: str) -> tuple[str, str, dict[str, Any] | None]:
    """Generate savings advice with Ollama directly or through the multi-agent workflow."""
    if not MULTI_AGENT_ENABLED:
        advice, confidence = get_ai_text_and_calculate_confidence(
            user_prompt,
            planner_model,
            0.6,
            system_prompt=system_prompt,
        )
        return advice, confidence, None

    url = os.environ.get("MULTI_AGENT_URL", "http://localhost:5010").rstrip("/")
    token = os.environ.get("MULTI_AGENT_TOKEN", "")
    payload = {
        "contract_version": "1.0",
        "feature": "savings",
        "task": system_prompt,
        "evidence": [{
            "source": "savings-context",
            "summary": "Savings goals, preferences, spending and retrieved advice guidelines.",
            "data": {"prompt": user_prompt},
        }],
    }
    try:
        response = requests.post(
            f"{url}/workflow",
            json=payload,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=float(os.environ.get("MULTI_AGENT_TIMEOUT", "1800")),
        )
    except requests.ConnectionError:
        raise ValueError("The savings advice service is unavailable. Please try again shortly.") from None
    # A reviewer failure can return 503 after the Worker has produced advice.
    if response.status_code != 503:
        response.raise_for_status()
    workflow = response.json()
    worker = workflow.get("worker_result") or {}
    reviewer = workflow.get("reviewer_result") or {}
    if worker.get("status") != "success":
        response.raise_for_status()
        raise ValueError("Multi-agent server did not return savings advice")

    advice = worker.get("output")
    if not isinstance(advice, str):
        raise ValueError("Multi-agent server returned invalid savings advice")
    confidence = worker.get("confidence")
    if confidence not in ("High", "Medium", "Low"):
        raise ValueError("Multi-agent server returned an invalid confidence level")
    return advice, confidence, reviewer


def generate_advice(
    goals: list[dto.Goal] | list[dict[str, Any]] | None,
    suggestions: list[dto.Suggestion] | list[dict[str, Any]] | None,
    feedbacks: list[dto.Feedback] | list[dict[str, Any]] | None,
    tx_url: str | None = None,
    previous_suggestion: str | None = None,
) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
    """Gather transactions and RAG context, then request multi-agent savings advice."""
    if not goals:
        return "Insufficient context available to generate savings advice.", None, None, None

    transactions, tx_error = _fetch_filtered_transactions(feedbacks, tx_url=tx_url)
    if tx_error is not None or not transactions:
        return tx_error or "You don't have any transactions yet. Add transactions using the transactions tab.", None, None, None

    categories = fetch_categories(tx_url)
    category_map = {category.id: category.name for category in categories} if categories else {}
    formatted_transactions = _format_transactions_for_prompt(transactions, category_map)
    spending_summary = _aggregate_spending_by_merchant(transactions, category_map)

    rag_guidelines = _fetch_rag_guidelines(goals)
    if MCP_ENABLED and RAG_ENABLED and not rag_guidelines:
        return "Insufficient context available to generate savings advice.", None, None, None
    rag_context_blocks, sources_str = rag_guidelines if rag_guidelines else (None, None)

    user_data = format_planner_prompt(goals, suggestions, feedbacks, tx_url=tx_url)
    system_prompt = load_prompt("savings_prompt.txt")
    user_prompt = _build_advice_prompt(
        user_data,
        spending_summary,
        formatted_transactions,
        rag_context_blocks,
        previous_suggestion=previous_suggestion,
    )
    raw_advice, confidence_category, reviewer = generate_savings_suggestion(user_prompt, system_prompt)

    if not raw_advice or raw_advice.strip().lower().startswith("insufficient context"):
        return "Insufficient context available to generate savings advice.", None, None, reviewer

    advice_lines = [line.strip() for line in raw_advice.strip().splitlines() if line.strip()]
    advice_paragraph = " ".join(advice_lines)
    return advice_paragraph, sources_str, confidence_category, reviewer


def generate_savings_advice(
    db_url: str,
    tx_url: str | None = None,
    previous_suggestion: str | None = None,
) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
    """Top-level entry point to fetch data and generate personalized savings advice."""
    try:
        goals = fetch_goals(db_url, active_only=True, top=3)
        if not goals:
            return (
                "You don't have any active savings goals yet. "
                "Add a goal in the Savings Goals table to receive personalized, adaptive savings advice!",
                None,
                None,
                None,
            )

        feedbacks = fetch_feedbacks(db_url)
        suggestions = fetch_suggestions(db_url)

        if not previous_suggestion and suggestions:
            latest = suggestions[-1]
            previous_suggestion = _get_item_field(latest, "suggestion", None)

        advice, sources, confidence, reviewer = generate_advice(
            goals, suggestions, feedbacks, tx_url=tx_url, previous_suggestion=previous_suggestion
        )
        if advice:
            return advice, sources, confidence, reviewer
        return "Error: Could not generate AI savings suggestion (empty response received from AI model).", None, None, reviewer
    except Exception as e:
        return f"Error: Could not generate AI savings suggestion ({e}).", None, None, None
