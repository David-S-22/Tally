from datetime import datetime
import json
import math
import os
from unittest.mock import patch
from typing import Any
import httpx
import pytest
import requests
import responses
from responses import matchers

from backend.savings_service import suggestion_service, ollama_service
from backend.savings_service.suggestion_service import (
    _aggregate_spending_by_merchant,
    _build_advice_prompt,
    _extract_rag_sources,
    _fetch_filtered_transactions,
    _fetch_rag_guidelines,
    _format_active_goals,
    _format_amount,
    _format_feedback_for_planner,
    _format_feedback_for_search,
    _format_past_suggestions,
    _format_transactions_for_prompt,
    _get_item_field,
    format_planner_prompt,
    generate_advice,
    generate_context_retrieval_args,
    generate_savings_advice,
    generate_savings_suggestion,
    generate_transaction_search_tool_call,
)
from shared.backend import dto

DB_URL = "http://savings-db:6002"
TX_URL = "http://transactions-db:6001"


# ============================================================================
# Test Fixtures: External Isolation Only (Zero Monkeypatching)
# ============================================================================

class MockAIState:
    def __init__(self):
        self.last_prompt: str | None = None
        self.last_request_body: dict[str, Any] | None = None
        self.response_text: str = "Trim dining costs by $40 to reach your goal."
        self.confidence_logprob: float = math.log(0.95)
        self.tool_call_name: str | None = None
        self.tool_call_args: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.last_request_body = body
        messages = body.get("messages", [])
        user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
        self.last_prompt = user_msg

        if "tools" in body and self.tool_call_name is not None:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc-search-1",
                        "type": "function",
                        "function": {
                            "name": self.tool_call_name,
                            "arguments": json.dumps(self.tool_call_args or {})
                            if isinstance(self.tool_call_args, dict)
                            else str(self.tool_call_args),
                        },
                    }
                ],
            }
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": 123456789,
                    "model": "qwen2.5:3b",
                    "choices": [{
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls",
                    }],
                },
            )

        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": 123456789,
                "model": "llama3.1:8b",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": self.response_text,
                    },
                    "logprobs": {
                        "content": [
                            {"token": "Trim", "logprob": self.confidence_logprob},
                        ]
                    },
                    "finish_reason": "stop",
                }],
            },
        )


@pytest.fixture
def ai_state():
    state = MockAIState()
    with patch.object(ollama_service, "client", ollama_service.OpenAI(
        base_url="http://mock-ollama/v1",
        api_key="ollama",
        http_client=httpx.Client(transport=httpx.MockTransport(state.handler)),
    )):
        yield state


class MockMCPState:
    def __init__(self):
        self.transactions: list[dict[str, Any]] = []
        self.unfiltered_transactions: list[dict[str, Any]] | None = None
        self.rag_docs: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []
        self.raise_on_execute: bool = False
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, tool_name: str, arguments: dict[str, Any]):
        self.execute_calls.append((tool_name, arguments))
        if self.raise_on_execute:
            raise RuntimeError("MCP server connection failure")
        if tool_name == "retrieve_context":
            return {"results": self.rag_docs}
        if not arguments and self.unfiltered_transactions is not None:
            return self.unfiltered_transactions
        return self.transactions

    def fetch(self):
        return self.tools


@pytest.fixture
def mcp_state():
    state = MockMCPState()
    with (
        patch.object(suggestion_service, "execute_mcp_tool", side_effect=state.execute),
        patch.object(suggestion_service, "fetch_mcp_tools", side_effect=state.fetch),
        patch.object(ollama_service, "execute_mcp_tool", side_effect=state.execute),
        patch.object(ollama_service, "fetch_mcp_tools", side_effect=state.fetch),
    ):
        yield state


class MockWorkflowState:
    def __init__(self):
        self.response_text = "Trim dining costs by $40 to reach your goal."
        self.last_prompt = None

    def handler(self, request):
        body = json.loads(request.body)
        self.last_prompt = body["evidence"][0]["data"]["prompt"]
        return 200, {}, json.dumps({
            "status": "success",
            "worker_result": {
                "status": "success",
                "output": self.response_text,
                "confidence": "High",
            },
            "reviewer_result": {"status": "success", "verdict": "approve", "review": "Advice is supported by the supplied spending data.", "risks": [], "recommendations": []},
        })


@pytest.fixture
def workflow_state():
    state = MockWorkflowState()
    responses.add_callback(
        responses.POST, "http://localhost:5010/workflow", callback=state.handler,
        content_type="application/json",
    )
    return state


# ============================================================================
# Pure Formatting & Extraction Helpers
# ============================================================================

def test_get_item_field_dict_and_dto():
    d = {"name": "Goal Dict", "val": 123}
    assert _get_item_field(d, "name") == "Goal Dict"
    assert _get_item_field(d, "missing", "default") == "default"

    g = dto.Goal(id=1, name="Goal DTO", cost=500, date=datetime(2026, 12, 1))
    assert _get_item_field(g, "name") == "Goal DTO"
    assert _get_item_field(g, "cost") == 500
    assert _get_item_field(g, "missing", 99) == 99


def test_format_amount():
    assert _format_amount(100) == "$100.00"
    assert _format_amount(45.5) == "$45.50"
    assert _format_amount("20.25") == "$20.25"
    assert _format_amount("non-numeric") == "$non-numeric"
    assert _format_amount(None) == "$None"


def test_format_active_goals():
    goals = [
        dto.Goal(id=1, name="Car", cost=10000, date=datetime(2026, 12, 25)),
        {"name": "Holiday", "cost": 2500, "date": "2026-11-10T00:00:00"},
    ]
    formatted = _format_active_goals(goals)
    assert len(formatted) == 2
    assert formatted[0] == {"goal": "Car", "target_amount": "$10000.00", "deadline": "2026-12-25"}
    assert formatted[1] == {"goal": "Holiday", "target_amount": "$2500.00", "deadline": "2026-11-10"}

    assert _format_active_goals([]) == []
    assert _format_active_goals(None) == []


def test_format_past_suggestions_reverses_and_formats_feedback():
    suggestions = [
        dto.Suggestion(id=1, suggestion="First advice", accepted=True, feedback="Loved it"),
        dto.Suggestion(id=2, suggestion="Second advice", accepted=False),
        dto.Suggestion(id=3, suggestion="", accepted=True),
    ]
    formatted = _format_past_suggestions(suggestions)
    assert len(formatted) == 2
    assert formatted[0] == '[REJECTED] "Second advice"'
    assert formatted[1] == '[ACCEPTED] "First advice" -> User Feedback: "Loved it"'

    assert _format_past_suggestions([]) == []


def test_format_feedback_for_planner():
    category_map = {80: "Dining", 81: "Groceries"}
    feedbacks = [
        dto.Feedback(id=1, feedback="Cut coffee", suggestion_id=10, category_id=80, timeframe="2 weeks"),
        dto.Feedback(id=2, feedback="Don't touch gym"),
    ]
    formatted = _format_feedback_for_planner(feedbacks, category_map)
    assert len(formatted) == 2
    assert formatted[0] == {"preference": "Don't touch gym"}
    assert formatted[1] == {
        "preference": "Cut coffee",
        "on_suggestion_id": 10,
        "category": "Dining",
        "timeframe": "2 weeks",
    }


def test_format_feedback_for_search():
    category_map = {80: "Dining"}
    feedbacks = [
        dto.Feedback(id=1, feedback="General preference without cat/tf"),
        dto.Feedback(id=2, feedback="Limit eating out", category_id=80, timeframe="1 month"),
    ]
    cat_tf, bg = _format_feedback_for_search(feedbacks, category_map)
    assert '1. (Newest) "Limit eating out" [Category: Dining, Timeframe: 1 month]' in cat_tf
    assert '- General preference without cat/tf' in bg

    cat_tf_empty, bg_empty = _format_feedback_for_search([], category_map)
    assert cat_tf_empty == "None"
    assert bg_empty == "None"


def test_format_feedback_skips_empty_text():
    """Verifies that empty or whitespace-only feedback text is skipped in planner and search formatting."""
    category_map = {80: "Dining"}
    feedbacks = [
        dto.Feedback(id=1, feedback=""),
        dto.Feedback(id=2, feedback="   "),
        dto.Feedback(id=3, feedback=None),
        dto.Feedback(id=4, feedback="Valid note", category_id=80),
    ]
    planner_formatted = _format_feedback_for_planner(feedbacks, category_map)
    assert len(planner_formatted) == 1
    assert planner_formatted[0]["preference"] == "Valid note"

    cat_tf, bg = _format_feedback_for_search(feedbacks, category_map)
    assert "Valid note" in cat_tf


@responses.activate
def test_format_planner_prompt():
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    goals = [dto.Goal(id=1, name="Car", cost=5000, date=datetime(2026, 12, 1))]
    prompt_str = format_planner_prompt(goals, [], [], tx_url=TX_URL)
    assert "User Financial Data:" in prompt_str
    assert "active_goals" in prompt_str
    assert "Car" in prompt_str


def test_generate_context_retrieval_args():
    args_with_goal = generate_context_retrieval_args([dto.Goal(id=1, name="Japan Trip", cost=3000, date=datetime(2026, 11, 1))], k=5)
    assert args_with_goal["feature"] == "savings"
    assert args_with_goal["k"] == 5
    assert "Japan Trip" in args_with_goal["question"]

    args_no_goal = generate_context_retrieval_args([], k=3)
    assert args_no_goal["question"] == "advice about how to generate savings advice"
    assert args_no_goal["k"] == 3


def test_format_transactions_for_prompt():
    txs = [
        dto.Transaction(id=1, amount=45.5, merchant="Uber", date=datetime(2026, 9, 1), description="Ride", category_id=70),
    ]
    category_map = {70: "Transport"}
    formatted = _format_transactions_for_prompt(txs, category_map)
    assert len(formatted) == 1
    assert formatted[0] == {
        "date": "2026-09-01",
        "merchant": "Uber",
        "description": "Ride",
        "amount": "$45.50",
        "category": "Transport",
    }


def test_aggregate_spending_by_merchant():
    txs = [
        {"merchant": "Woolies", "amount": 100, "category_id": 81},
        {"merchant": "Woolies", "amount": 50, "category_id": 81},
        {"merchant": "Cafe", "amount": 20, "category_id": 80},
    ]
    category_map = {80: "Dining", 81: "Groceries"}
    aggregated = _aggregate_spending_by_merchant(txs, category_map)
    assert len(aggregated) == 2
    assert aggregated[0]["merchant"] == "Woolies"
    assert aggregated[0]["total_spent"] == "$150.00"
    assert aggregated[0]["transaction_count"] == 2
    assert aggregated[0]["category"] == "Groceries"

    assert aggregated[1]["merchant"] == "Cafe"
    assert aggregated[1]["total_spent"] == "$20.00"
    assert aggregated[1]["transaction_count"] == 1


def test_aggregate_spending_by_merchant_fallback_branches():
    """Verifies non-numeric amount fallback to 0.0 and string category fallback."""
    txs = [
        {"merchant": "Cafe", "amount": "invalid-amount", "category": "Food"},
        {"merchant": None, "amount": 15.0, "category_id": 999},
    ]
    aggregated = _aggregate_spending_by_merchant(txs, category_map={})
    assert len(aggregated) == 2
    cafe_item = next(item for item in aggregated if item["merchant"] == "Cafe")
    assert cafe_item["total_spent"] == "$0.00"
    assert cafe_item["category"] == "Food"

    unknown_item = next(item for item in aggregated if item["merchant"] == "Unknown")
    assert unknown_item["total_spent"] == "$15.00"
    assert unknown_item["category"] == "Uncategorized"


def test_extract_rag_sources():
    docs = [
        {"metadata": {"source": "savings_guide.md"}},
        {"metadata": {"source": "discretionary_budget.md"}},
        {"metadata": {"source": "savings_guide.md"}},
    ]
    sources = _extract_rag_sources(docs)
    assert sources == "savings_guide.md, discretionary_budget.md"
    assert _extract_rag_sources([]) == "savings_advice_guide.md"


def test_build_advice_prompt():
    prompt_with_prev = _build_advice_prompt(
        user_data="UserData",
        spending_summary=[],
        formatted_transactions=[],
        rag_context_blocks="RAGBlocks",
        previous_suggestion="Cut coffee",
    )
    assert 'Previous Suggestion: "Cut coffee"' in prompt_with_prev
    assert "Output 1 or 2" not in prompt_with_prev

    prompt_without_prev = _build_advice_prompt(
        user_data="UserData",
        spending_summary=[],
        formatted_transactions=[],
        rag_context_blocks="RAGBlocks",
        previous_suggestion=None,
    )
    assert "Previous Suggestion:" not in prompt_without_prev

    prompt_without_rag = _build_advice_prompt(
        user_data="UserData",
        spending_summary=[],
        formatted_transactions=[],
        rag_context_blocks=None,
    )
    assert "Retrieved Savings Advice Guidelines" not in prompt_without_rag


def test_advice_tables_preserve_every_transaction_and_calculated_total():
    transactions = [
        {"date": "2026-09-01", "merchant": "Cafe", "description": "Coffee", "amount": "$5.00", "category": "Dining"},
        {"date": "2026-09-01", "merchant": "Cafe", "description": "Coffee", "amount": "$5.00", "category": "Dining"},
        {"date": "2026-09-08", "merchant": "Cafe", "description": "Lunch", "amount": "$15.00", "category": "Dining"},
        {"date": "2026-09-09", "merchant": "Shop", "description": "Groceries", "amount": "$20.00"},
    ]
    merchants = [{"merchant": "Cafe", "total_spent": "$25.00", "transaction_count": 3, "category": "Dining"}]
    prompt = _build_advice_prompt("Goals and preferences", merchants, transactions, "Guidelines with source citations")

    merchant_json = prompt.split("Pre-Calculated Spending Summary by Merchant:\n", 1)[1].split("\n\n", 1)[0]
    transaction_json = prompt.split("Retrieved Transactions:\n", 1)[1].split("\n\n", 1)[0]
    merchant_table = json.loads(merchant_json)
    transaction_table = json.loads(transaction_json)
    restored_transactions = [dict(zip(transaction_table["columns"], row)) for row in transaction_table["rows"]]
    assert restored_transactions == [{**tx, "category": tx.get("category", "")} for tx in transactions]
    assert restored_transactions[0] == restored_transactions[1]
    assert [dict(zip(merchant_table["columns"], row)) for row in merchant_table["rows"]] == merchants
    assert "Goals and preferences" in prompt
    assert "Guidelines with source citations" in prompt


# ============================================================================
# Search Tool & RAG Retrieval
# ============================================================================

def test_generate_transaction_search_tool_call_defaults_when_no_tools(mcp_state: MockMCPState):
    mcp_state.tools = []
    tool_name, tool_args = generate_transaction_search_tool_call([], tx_url=TX_URL)
    assert tool_name == "search_transactions"
    assert tool_args == {}


@responses.activate
def test_generate_transaction_search_tool_call_with_tools_and_category_enum(mcp_state: MockMCPState, ai_state: MockAIState):
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}, {"id": 81, "name": "Groceries", "type": "need"}],
        status=200,
    )
    mcp_state.tools = [
        {
            "type": "function",
            "function": {
                "name": "search_transactions",
                "description": "Search transactions",
                "parameters": {
                    "type": "object",
                    "properties": {"category_name": {"type": "string"}},
                },
            },
        }
    ]
    ai_state.tool_call_name = "search_transactions"
    ai_state.tool_call_args = {
        "category_name": "dining",
        "start_date": "2026-09-01",
        "empty_field": "",
        "none_field": None,
    }
    feedbacks = [dto.Feedback(id=1, feedback="Cut dining", category_id=80, timeframe="2 weeks")]

    tool_name, tool_args = generate_transaction_search_tool_call(feedbacks, tx_url=TX_URL)
    assert tool_name == "search_transactions"
    # Verifies matched_cat normalized case-insensitively to "Dining", and empty/None fields cleaned
    assert tool_args == {"category_name": "Dining", "start_date": "2026-09-01"}

    # Verify category enum was injected into tool definition schema sent to AI
    req = ai_state.last_request_body
    assert req is not None
    props = req["tools"][0]["function"]["parameters"]["properties"]
    assert props["category_name"]["enum"] == ["Dining", "Groceries"]


@responses.activate
def test_generate_transaction_search_tool_call_unmatched_category(mcp_state: MockMCPState, ai_state: MockAIState):
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    mcp_state.tools = [
        {
            "type": "function",
            "function": {
                "name": "query_tx",
                "parameters": {"properties": {}},
            },
        }
    ]
    ai_state.tool_call_name = "query_tx"
    ai_state.tool_call_args = {"category_name": "Entertainment"}

    tool_name, tool_args = generate_transaction_search_tool_call([], tx_url=TX_URL)
    assert tool_name == "query_tx"
    assert tool_args == {"category_name": "Entertainment"}


@responses.activate
def test_generate_transaction_search_tool_call_no_categories(mcp_state: MockMCPState, ai_state: MockAIState):
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[],
        status=200,
    )
    mcp_state.tools = [
        {
            "type": "function",
            "function": {
                "name": "search_transactions",
                "parameters": {"properties": {}},
            },
        }
    ]
    ai_state.tool_call_name = "search_transactions"
    ai_state.tool_call_args = {"limit": 10}

    tool_name, tool_args = generate_transaction_search_tool_call([], tx_url=TX_URL)
    assert tool_name == "search_transactions"
    assert tool_args == {"limit": 10}


def test_fetch_rag_guidelines_insufficient_context_returns_none(mcp_state: MockMCPState):
    # Less than 30 characters of text returns None
    mcp_state.rag_docs = [{"text": "Too short", "metadata": {}}]
    res = _fetch_rag_guidelines([dto.Goal(id=1, name="Goal", cost=100, date=datetime.now())])
    assert res is None


def test_fetch_rag_guidelines_sufficient_context_success(mcp_state: MockMCPState):
    sample_text = "Save money by reviewing all recurring subscriptions and pausing unused services."
    mcp_state.rag_docs = [
        {"text": sample_text, "metadata": {"source": "subscription_tips.md"}},
    ]
    res = _fetch_rag_guidelines([dto.Goal(id=1, name="Goal", cost=100, date=datetime.now())])
    assert res is not None
    context_blocks, sources_str = res
    assert sample_text in context_blocks
    assert sources_str == "subscription_tips.md"


def test_fetch_rag_guidelines_handles_mcp_exception(mcp_state: MockMCPState):
    mcp_state.raise_on_execute = True
    res = _fetch_rag_guidelines([dto.Goal(id=1, name="Goal", cost=100, date=datetime.now())])
    assert res is None


def test_fetch_filtered_transactions_empty_and_filter_messages(mcp_state: MockMCPState):
    # Case 1: Search returns empty, full search also empty -> no transactions yet message
    mcp_state.transactions = []
    txs, error = _fetch_filtered_transactions([], tx_url=TX_URL)
    assert txs is None
    assert "don't have any transactions yet" in error


@responses.activate
def test_fetch_filtered_transactions_retry_category_and_dates(mcp_state: MockMCPState, ai_state: MockAIState):
    """Verifies lines 296-304: filtered search returns empty, unfiltered retry returns transactions with category & dates message."""
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    mcp_state.tools = [
        {
            "type": "function",
            "function": {
                "name": "search_transactions",
                "parameters": {"properties": {"category_name": {"type": "string"}}},
            },
        }
    ]
    ai_state.tool_call_name = "search_transactions"
    ai_state.tool_call_args = {"category_name": "Dining", "start_date": "2026-09-01"}

    # Filtered search returns empty, but unfiltered fallback returns transactions
    mcp_state.transactions = []
    mcp_state.unfiltered_transactions = [{"merchant": "Supermarket", "amount": 50}]

    txs, error = _fetch_filtered_transactions([], tx_url=TX_URL)
    assert txs is None
    assert error == "No transactions found in category 'Dining' within the specified timeframe. Try broadening your feedback or checking other categories."


@responses.activate
def test_fetch_filtered_transactions_retry_generic_filter(mcp_state: MockMCPState, ai_state: MockAIState):
    """Verifies lines 296-304: filter without category or dates generates 'matching your filter' message."""
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[],
        status=200,
    )
    mcp_state.tools = [
        {
            "type": "function",
            "function": {
                "name": "search_transactions",
                "parameters": {"properties": {}},
            },
        }
    ]
    ai_state.tool_call_name = "search_transactions"
    ai_state.tool_call_args = {"min_amount": 500}

    mcp_state.transactions = []
    mcp_state.unfiltered_transactions = [{"merchant": "Supermarket", "amount": 50}]

    txs, error = _fetch_filtered_transactions([], tx_url=TX_URL)
    assert txs is None
    assert error == "No transactions found matching your filter. Try broadening your feedback or checking other categories."


# ============================================================================
# Full End-to-End Advice Generation (Real Pipeline, No Internal Monkeypatching)
# ============================================================================

def test_generate_advice_no_goals():
    advice, sources, confidence, reviewer = generate_advice([], [], [])
    assert advice == "Insufficient context available to generate savings advice."
    assert sources is None
    assert confidence is None


def test_generate_advice_no_transactions(mcp_state: MockMCPState):
    goals = [dto.Goal(id=1, name="Goal", cost=100, date=datetime.now())]
    mcp_state.transactions = []
    advice, sources, confidence, reviewer = generate_advice(goals, [], [])
    assert "don't have any transactions yet" in advice


def test_generate_advice_insufficient_rag_guidelines(mcp_state: MockMCPState):
    goals = [dto.Goal(id=1, name="Goal", cost=100, date=datetime.now())]
    mcp_state.transactions = [{"id": 1, "merchant": "Shop", "amount": 10}]
    mcp_state.rag_docs = [{"text": "Short"}]  # < 30 chars
    advice, sources, confidence, reviewer = generate_advice(goals, [], [])
    assert advice == "Insufficient context available to generate savings advice."


@responses.activate
def test_generate_advice_handles_insufficient_context_from_mocked_server(mcp_state: MockMCPState, workflow_state: MockWorkflowState):
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[],
        status=200,
    )
    goals = [dto.Goal(id=1, name="Emergency", cost=500, date=datetime.now())]
    mcp_state.transactions = [{"id": 1, "merchant": "Cafe", "amount": 15}]
    mcp_state.rag_docs = [{"text": "A comprehensive guideline explaining that users should prioritize emergency funds."}]

    workflow_state.response_text = "Insufficient context to generate savings advice."
    advice, sources, confidence, reviewer = generate_advice(goals, [], [], tx_url=TX_URL)
    assert advice == "Insufficient context available to generate savings advice."
    assert sources is None
    assert confidence is None


@responses.activate
def test_generate_advice_builds_prompt_from_savings_data(mcp_state: MockMCPState, workflow_state: MockWorkflowState):
    """Check the backend's prompt construction with MCP and HTTP calls mocked."""
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )
    goals = [dto.Goal(id=1, name="Japan Trip", cost=2000, date=datetime(2026, 11, 20))]
    suggestions = [dto.Suggestion(id=1, suggestion="Cut coffee spending", accepted=True)]
    feedbacks = [dto.Feedback(id=1, feedback="Limit restaurant meals", category_id=80, timeframe="2 weeks")]

    mcp_state.transactions = [
        {"id": 1, "merchant": "Ramen Bar", "amount": 35.0, "date": "2026-09-01", "category_id": 80},
        {"id": 2, "merchant": "Ramen Bar", "amount": 45.0, "date": "2026-09-05", "category_id": 80},
    ]
    mcp_state.rag_docs = [
        {"text": "Review discretionary dining spending and cook batch meals on weekends.", "metadata": {"source": "dining_tips.md"}},
    ]

    workflow_state.response_text = "Trim restaurant meals at Ramen Bar to save $80 towards your Japan Trip goal."

    advice, sources, confidence, reviewer = generate_advice(
        goals,
        suggestions,
        feedbacks,
        tx_url=TX_URL,
        previous_suggestion="Cut coffee spending",
    )

    assert advice == "Trim restaurant meals at Ramen Bar to save $80 towards your Japan Trip goal."
    assert sources == "dining_tips.md"
    assert confidence == "High"

    prompt_sent = workflow_state.last_prompt
    assert prompt_sent is not None
    assert "Japan Trip" in prompt_sent
    assert "Ramen Bar" in prompt_sent
    assert "$80.00" in prompt_sent
    assert "Previous Suggestion: \"Cut coffee spending\"" in prompt_sent
    assert "dining_tips.md" in prompt_sent or "discretionary dining" in prompt_sent


@responses.activate
def test_generate_advice_joins_multiline_output(mcp_state: MockMCPState, workflow_state: MockWorkflowState):
    responses.add(responses.GET, f"{TX_URL}/categories", json=[], status=200)
    goals = [dto.Goal(id=1, name="Car", cost=1000, date=datetime.now())]
    mcp_state.transactions = [{"id": 1, "merchant": "Gas", "amount": 50}]
    mcp_state.rag_docs = [{"text": "Review vehicle and transport expenses carefully to save on gas."}]

    workflow_state.response_text = "Trim gas expenses by taking public transport.\nThis saves $20 per week."
    advice, sources, confidence, reviewer = generate_advice(goals, [], [], tx_url=TX_URL)
    assert advice == "Trim gas expenses by taking public transport. This saves $20 per week."


# ============================================================================
# Savings advice with mocked external calls
# ============================================================================

@responses.activate
def test_generate_savings_advice_no_active_goals():
    responses.add(
        responses.GET,
        f"{DB_URL}/goals?active_only=true&top=3",
        json=[],
        status=200,
    )
    advice, sources, confidence, reviewer = generate_savings_advice(DB_URL, tx_url=TX_URL)
    assert "don't have any active savings goals yet" in advice
    assert sources is None
    assert confidence is None


@responses.activate
def test_generate_savings_advice_uses_fetched_data_and_mocked_advice(mcp_state: MockMCPState, workflow_state: MockWorkflowState):
    """Check data fetching and advice handling with external services mocked."""
    responses.add(
        responses.GET,
        f"{DB_URL}/goals?active_only=true&top=3",
        json=[{"id": 1, "name": "New Laptop", "cost": 1500, "date": "2026-12-15T00:00:00"}],
        status=200,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/feedbacks",
        json=[{"id": 1, "feedback": "Focus on takeaway food", "category_id": 80, "timeframe": "2 weeks"}],
        status=200,
    )
    responses.add(
        responses.GET,
        f"{DB_URL}/suggestions",
        json=[{"id": 1, "suggestion": "Cut coffee runs", "accepted": True}],
        status=200,
    )
    responses.add(
        responses.GET,
        f"{TX_URL}/categories",
        json=[{"id": 80, "name": "Dining", "type": "want"}],
        status=200,
    )

    mcp_state.transactions = [
        {"id": 1, "merchant": "UberEats", "amount": 60.0, "date": "2026-09-02", "category_id": 80},
    ]
    mcp_state.rag_docs = [
        {"text": "Reduce delivery app spending and prepare home-cooked dinners.", "metadata": {"source": "takeaway_guide.md"}},
    ]

    workflow_state.response_text = "Pause UberEats delivery orders to save $60 for your New Laptop."

    advice, sources, confidence, reviewer = generate_savings_advice(DB_URL, tx_url=TX_URL)
    assert advice == "Pause UberEats delivery orders to save $60 for your New Laptop."
    assert sources == "takeaway_guide.md"
    assert confidence == "High"

    # Verifies that previous_suggestion was automatically forwarded from the latest suggestion
    assert "Previous Suggestion: \"Cut coffee runs\"" in (workflow_state.last_prompt or "")


@responses.activate
def test_generate_savings_advice_handles_empty_ai_response(mcp_state: MockMCPState, workflow_state: MockWorkflowState):
    responses.add(
        responses.GET,
        f"{DB_URL}/goals?active_only=true&top=3",
        json=[{"id": 1, "name": "Emergency", "cost": 500, "date": "2026-12-01T00:00:00"}],
        status=200,
    )
    responses.add(responses.GET, f"{DB_URL}/feedbacks", json=[], status=200)
    responses.add(responses.GET, f"{DB_URL}/suggestions", json=[], status=200)
    responses.add(responses.GET, f"{TX_URL}/categories", json=[], status=200)

    mcp_state.transactions = [{"id": 1, "merchant": "Store", "amount": 10}]
    mcp_state.rag_docs = [{"text": "Save money by creating a detailed monthly spreadsheet."}]

    workflow_state.response_text = ""
    advice, sources, confidence, reviewer = generate_savings_advice(DB_URL, tx_url=TX_URL)
    assert advice == "Insufficient context available to generate savings advice."
    assert sources is None
    assert confidence is None


@responses.activate
def test_generate_savings_advice_empty_advice_fallback():
    """Verifies line 454 fallback when advice returned by generate_advice is empty."""
    responses.add(
        responses.GET,
        f"{DB_URL}/goals?active_only=true&top=3",
        json=[{"id": 1, "name": "Emergency", "cost": 500, "date": "2026-12-01T00:00:00"}],
        status=200,
    )
    responses.add(responses.GET, f"{DB_URL}/feedbacks", json=[], status=200)
    responses.add(responses.GET, f"{DB_URL}/suggestions", json=[], status=200)
    responses.add(responses.GET, f"{TX_URL}/categories", json=[], status=200)

    with patch.object(suggestion_service, "generate_advice", return_value=("", None, None, None)):
        advice, sources, confidence, reviewer = generate_savings_advice(DB_URL, tx_url=TX_URL)
        assert advice == "Error: Could not generate AI savings suggestion (empty response received from AI model)."
        assert sources is None
        assert confidence is None


@responses.activate
def test_generate_savings_advice_exception_fallback():
    """Verifies lines 455-456 exception handler when pipeline encounters an unhandled error."""
    responses.add(
        responses.GET,
        f"{DB_URL}/goals?active_only=true&top=3",
        json=[{"id": 1, "name": "Emergency", "cost": 500, "date": "2026-12-01T00:00:00"}],
        status=200,
    )
    responses.add(responses.GET, f"{DB_URL}/feedbacks", json=[], status=200)
    responses.add(responses.GET, f"{DB_URL}/suggestions", json=[], status=200)
    responses.add(responses.GET, f"{TX_URL}/categories", json=[], status=200)

    with patch.object(suggestion_service, "generate_advice", side_effect=RuntimeError("Unexpected pipeline failure")):
        advice, sources, confidence, reviewer = generate_savings_advice(DB_URL, tx_url=TX_URL)
        assert "Error: Could not generate AI savings suggestion (Unexpected pipeline failure)." in advice
        assert sources is None
        assert confidence is None


@pytest.fixture
def workflow_response(multi_agent_env):
    return {
        "status": "success",
        "worker_result": {
            "status": "success",
            "output": "Trim dining costs to save $40 for your Japan Trip.",
            "confidence": "High",
        },
        "reviewer_result": {"status": "success", "verdict": "approve", "review": "Advice is supported by the supplied spending data.", "risks": [], "recommendations": []},
    }


@pytest.fixture
def multi_agent_env():
    with patch.dict(os.environ, {
        "MULTI_AGENT_URL": "http://multi-agent:5010/",
        "MULTI_AGENT_TOKEN": "",
        "MULTI_AGENT_TIMEOUT": "600",
    }):
        yield


@responses.activate
@pytest.mark.parametrize("confidence", ["High", "Medium", "Low"])
def test_generate_savings_suggestion_sends_evidence_and_reads_mocked_response(workflow_response, confidence):
    """Check the backend request and response parsing; the server is mocked."""
    workflow_response["worker_result"]["confidence"] = confidence
    responses.post(
        "http://multi-agent:5010/workflow", json=workflow_response,
        match=[matchers.request_kwargs_matcher({"timeout": 600.0})],
    )

    advice, level, reviewer = generate_savings_suggestion("Goals and spending evidence", "Write savings advice")

    assert advice == workflow_response["worker_result"]["output"]
    assert level == confidence
    assert reviewer == workflow_response["reviewer_result"]
    request = responses.calls[0].request
    assert "Authorization" not in request.headers
    assert json.loads(request.body) == {
        "contract_version": "1.0",
        "feature": "savings",
        "task": "Write savings advice",
        "evidence": [{
            "source": "savings-context",
            "summary": "Savings goals, preferences, spending and retrieved advice guidelines.",
            "data": {"prompt": "Goals and spending evidence"},
        }],
    }


@responses.activate
def test_generate_savings_suggestion_sends_configured_auth_and_timeout(workflow_response):
    responses.post(
        "http://multi-agent:5010/workflow", json=workflow_response,
        match=[
            matchers.header_matcher({"Authorization": "Bearer test-token"}),
            matchers.request_kwargs_matcher({"timeout": 650.0}),
        ],
    )
    with patch.dict(os.environ, {"MULTI_AGENT_TOKEN": "test-token", "MULTI_AGENT_TIMEOUT": "650"}):
        generate_savings_suggestion("Evidence", "Task")


@responses.activate
@pytest.mark.parametrize("changes", [
    {"worker_result": None},
    {"worker_result": {"status": "timeout"}},
])
def test_generate_savings_suggestion_rejects_response_without_worker_advice(workflow_response, changes):
    workflow_response.update(changes)
    responses.post("http://multi-agent:5010/workflow", json=workflow_response)
    with pytest.raises(ValueError, match="did not return savings advice"):
        generate_savings_suggestion("Evidence", "Task")


@responses.activate
@pytest.mark.parametrize("verdict", ["approve", "approve_with_changes", "reject"])
def test_generate_savings_suggestion_returns_mocked_review_without_enforcing_verdict(workflow_response, verdict):
    workflow_response["reviewer_result"].update({
        "verdict": verdict,
        "review": "Consider whether this spending change suits your needs.",
        "risks": ["This may affect your transport options."],
        "recommendations": ["Check alternatives before cancelling."],
    })
    responses.post("http://multi-agent:5010/workflow", json=workflow_response)

    advice, confidence, reviewer = generate_savings_suggestion("Evidence", "Task")

    assert advice == workflow_response["worker_result"]["output"]
    assert confidence == "High"
    assert reviewer == workflow_response["reviewer_result"]


@responses.activate
@pytest.mark.parametrize("status", ["error", "timeout"])
def test_generate_savings_suggestion_preserves_worker_advice_when_mocked_reviewer_fails(workflow_response, status):
    workflow_response["status"] = "error"
    workflow_response["reviewer_result"] = {"status": status, "error": "Reviewer could not complete its review."}
    responses.post("http://multi-agent:5010/workflow", json=workflow_response, status=503)

    advice, confidence, reviewer = generate_savings_suggestion("Evidence", "Task")

    assert advice == workflow_response["worker_result"]["output"]
    assert confidence == "High"
    assert reviewer == workflow_response["reviewer_result"]


@responses.activate
def test_generate_savings_suggestion_preserves_advice_when_review_is_missing(workflow_response):
    workflow_response["reviewer_result"] = None
    responses.post("http://multi-agent:5010/workflow", json=workflow_response)

    advice, confidence, reviewer = generate_savings_suggestion("Evidence", "Task")

    assert advice == workflow_response["worker_result"]["output"]
    assert confidence == "High"
    assert reviewer == {}


@responses.activate
@pytest.mark.parametrize("confidence", [None, "", "Certain", 0.9])
def test_generate_savings_suggestion_rejects_missing_or_invalid_confidence(workflow_response, confidence):
    workflow_response["worker_result"]["confidence"] = confidence
    responses.post("http://multi-agent:5010/workflow", json=workflow_response)
    with pytest.raises(ValueError, match="invalid confidence"):
        generate_savings_suggestion("Evidence", "Task")


@responses.activate
def test_generate_savings_suggestion_rejects_invalid_advice_in_response(workflow_response):
    workflow_response["worker_result"]["output"] = {"advice": "Unexpected object"}
    responses.post("http://multi-agent:5010/workflow", json=workflow_response)
    with pytest.raises(ValueError, match="invalid savings advice"):
        generate_savings_suggestion("Evidence", "Task")


@responses.activate
@pytest.mark.parametrize("status", [400, 401, 503])
def test_generate_savings_suggestion_propagates_http_errors_without_retrying(status, multi_agent_env):
    responses.post("http://multi-agent:5010/workflow", json={"status": "error"}, status=status)
    with pytest.raises(requests.HTTPError):
        generate_savings_suggestion("Evidence", "Task")
    assert len(responses.calls) == 1


@responses.activate
def test_generate_savings_suggestion_reports_connection_failure_without_network_details(multi_agent_env):
    responses.post(
        "http://multi-agent:5010/workflow",
        body=requests.ConnectionError("HTTPConnectionPool: internal connection details"),
    )
    with pytest.raises(ValueError) as error:
        generate_savings_suggestion("Evidence", "Task")
    assert str(error.value) == "The savings advice service is unavailable. Please try again shortly."
    assert len(responses.calls) == 1


@responses.activate
def test_generate_savings_suggestion_does_not_retry_timed_out_request(multi_agent_env):
    responses.post("http://multi-agent:5010/workflow", body=requests.Timeout("Workflow timed out"))
    with pytest.raises(requests.Timeout):
        generate_savings_suggestion("Evidence", "Task")
    assert len(responses.calls) == 1


@responses.activate
def test_generate_savings_suggestion_rejects_non_json_response(multi_agent_env):
    responses.post("http://multi-agent:5010/workflow", body="Invalid JSON")
    with pytest.raises(ValueError):
        generate_savings_suggestion("Evidence", "Task")
