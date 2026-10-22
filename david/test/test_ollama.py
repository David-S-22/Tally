import json
import math
from types import SimpleNamespace
from typing import Any
import httpx
import pytest

import pathlib
from backend.savings_service import ollama_service
from backend.savings_service.ollama_service import (
    _environment_flag,
    _format_messages,
    calculate_confidence_category,
    call_ai_to_select_tool,
    execute_mcp_tool,
    fetch_mcp_tools,
    get_ai_json_response,
    get_ai_structured_json,
    get_ai_text_and_calculate_confidence,
    get_ai_text_response,
    load_prompt,
)


class MockLLMState:
    """Captures outgoing request payloads to verify API calls rather than blinding the mock."""
    def __init__(self):
        self.last_request_body: dict[str, Any] | None = None
        self.response_payload: dict[str, Any] = {}
        self.status_code: int = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.last_request_body = json.loads(request.content.decode("utf-8"))
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"error": "LLM API Error"})
        return httpx.Response(200, json=self.response_payload)


@pytest.fixture
def llm_state():
    state = MockLLMState()
    orig_client = ollama_service.client
    ollama_service.client = ollama_service.OpenAI(
        base_url="http://mock-ollama/v1",
        api_key="ollama",
        http_client=httpx.Client(transport=httpx.MockTransport(state.handler)),
    )
    yield state
    ollama_service.client = orig_client


# ============================================================================
# Prompt Loading & Formatting (Pure Functions)
# ============================================================================

def test_load_prompt_with_and_without_extension():
    prompt_txt = load_prompt("classify_prompt.txt")
    prompt_no_ext = load_prompt("classify_prompt")
    assert prompt_txt == prompt_no_ext
    assert "focus" in prompt_txt.lower()


def test_load_prompt_savings_and_search():
    savings = load_prompt("savings_prompt")
    search = load_prompt("search_prompt")
    assert "{today}" in search
    assert "{category_timeframe_feedback}" in search
    assert "{background_preferences}" in search
    assert "{valid_categories}" in search

    assert "Worker output:" in savings
    assert "Insufficient context to generate savings advice." in savings


def test_load_prompt_nonexistent_raises_filenotfound():
    with pytest.raises(FileNotFoundError):
        load_prompt("non_existent_prompt_file_xyz")


def test_format_messages_user_only():
    messages = _format_messages("test prompt")
    assert messages == [{"role": "user", "content": "test prompt"}]


def test_format_messages_with_system_prompt():
    messages = _format_messages("test prompt", system_prompt="system context")
    assert messages == [
        {"role": "system", "content": "system context"},
        {"role": "user", "content": "test prompt"},
    ]


# ============================================================================
# Confidence Calculation (Pure Math)
# ============================================================================

def test_calculate_confidence_category_empty_or_none():
    assert calculate_confidence_category(None) == "Medium"
    assert calculate_confidence_category([]) == "Medium"


def test_calculate_confidence_category_high():
    tokens = [
        SimpleNamespace(logprob=math.log(0.95)),
        SimpleNamespace(logprob=math.log(0.85)),
        SimpleNamespace(logprob=math.log(0.90)),
    ]
    assert calculate_confidence_category(tokens) == "High"


def test_calculate_confidence_category_medium():
    tokens = [
        SimpleNamespace(logprob=math.log(0.60)),
        SimpleNamespace(logprob=math.log(0.50)),
    ]
    assert calculate_confidence_category(tokens) == "Medium"


def test_calculate_confidence_category_low():
    tokens = [
        SimpleNamespace(logprob=math.log(0.20)),
        SimpleNamespace(logprob=math.log(0.30)),
    ]
    assert calculate_confidence_category(tokens) == "Low"


def test_calculate_confidence_category_ignores_none_logprobs():
    tokens = [
        SimpleNamespace(logprob=None),
        SimpleNamespace(logprob=math.log(0.80)),
    ]
    assert calculate_confidence_category(tokens) == "High"

    tokens_all_none = [SimpleNamespace(logprob=None)]
    assert calculate_confidence_category(tokens_all_none) == "Medium"


# ============================================================================
# Text & JSON Responses with Request Payload Verification
# ============================================================================

def test_get_ai_text_response_verifies_request_and_strips(llm_state: MockLLMState):
    llm_state.response_payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "   Save $10 on groceries.   "},
            "finish_reason": "stop",
        }],
    }

    res = get_ai_text_response("How to save?", "llama3.1:8b", 0.7, system_prompt="You are a financial advisor")
    assert res == "Save $10 on groceries."

    req = llm_state.last_request_body
    assert req is not None
    assert req["model"] == "llama3.1:8b"
    assert req["temperature"] == 0.7
    assert req["messages"] == [
        {"role": "system", "content": "You are a financial advisor"},
        {"role": "user", "content": "How to save?"},
    ]


def test_get_ai_text_response_handles_none_content(llm_state: MockLLMState):
    llm_state.response_payload = {
        "id": "chatcmpl-none",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": None},
            "finish_reason": "stop",
        }],
    }
    res = get_ai_text_response("Hello", "llama3.1:8b", 0.5)
    assert res == ""


def test_get_ai_text_and_calculate_confidence_verifies_logprobs_flag(llm_state: MockLLMState):
    llm_state.response_payload = {
        "id": "chatcmpl-2",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "Cut dining costs."},
            "logprobs": {
                "content": [
                    {"token": "Cut", "logprob": math.log(0.92)},
                    {"token": "dining", "logprob": math.log(0.88)},
                ]
            },
            "finish_reason": "stop",
        }],
    }

    text, confidence = get_ai_text_and_calculate_confidence("Suggest advice", "llama3.1:8b", 0.6)
    assert text == "Cut dining costs."
    assert confidence == "High"

    # CRITICAL: Verify logprobs=True was actually requested from Ollama!
    req = llm_state.last_request_body
    assert req is not None
    assert req.get("logprobs") is True


def test_get_ai_json_response_verifies_json_mode_payload(llm_state: MockLLMState):
    expected_data = {"recommendation": "Cook at home", "savings": 40}
    llm_state.response_payload = {
        "id": "chatcmpl-3",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps(expected_data)},
            "finish_reason": "stop",
        }],
    }

    result = get_ai_json_response("Give JSON budget", "llama3.1:8b", 0.0)
    assert result == expected_data

    # CRITICAL: Verify JSON mode response_format was sent!
    req = llm_state.last_request_body
    assert req is not None
    assert req.get("response_format") == {"type": "json_object"}


def test_get_ai_structured_json_verifies_schema_payload(llm_state: MockLLMState):
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
    }
    expected_data = {"status": "ok"}
    llm_state.response_payload = {
        "id": "chatcmpl-4",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": json.dumps(expected_data)},
            "finish_reason": "stop",
        }],
    }

    result = get_ai_structured_json("Validate", schema, "qwen2.5:3b", 0.0)
    assert result == expected_data

    # CRITICAL: Verify grammar-constrained json_schema was sent in response_format!
    req = llm_state.last_request_body
    assert req is not None
    assert req["response_format"]["type"] == "json_schema"
    assert req["response_format"]["json_schema"]["name"] == "StructuredOutput"
    assert req["response_format"]["json_schema"]["schema"] == schema


def test_ai_calls_raise_on_http_error(llm_state: MockLLMState):
    llm_state.status_code = 500
    with pytest.raises(Exception):
        get_ai_text_response("Hello", "model", 0.0)


# ============================================================================
# Tool Calling with Payload Verification
# ============================================================================

def test_call_ai_to_select_tool_verifies_tools_and_tool_choice(llm_state: MockLLMState):
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_transactions",
                "description": "Search transactions",
                "parameters": {"type": "object", "properties": {"category": {"type": "string"}}},
            },
        }
    ]
    llm_state.response_payload = {
        "id": "chatcmpl-5",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_transactions",
                            "arguments": json.dumps({"category": "Dining"}),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }

    tool_name, tool_args = call_ai_to_select_tool("Find dining transactions", tools=tools, model="qwen2.5:3b", temperature=0.0)
    assert tool_name == "search_transactions"
    assert tool_args == {"category": "Dining"}

    # CRITICAL: Verify tools list and required tool_choice were sent!
    req = llm_state.last_request_body
    assert req is not None
    assert req.get("tools") == tools
    assert req.get("tool_choice") == "required"


def test_call_ai_to_select_tool_with_dict_arguments(llm_state: MockLLMState):
    llm_state.response_payload = {
        "id": "chatcmpl-6",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "retrieve_context",
                            "arguments": {"feature": "savings", "k": 3},
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }

    tool_name, tool_args = call_ai_to_select_tool("Retrieve context", tools=[], model="search-model", temperature=0.0)
    assert tool_name == "retrieve_context"
    assert tool_args == {"feature": "savings", "k": 3}


def test_call_ai_to_select_tool_with_malformed_arguments(llm_state: MockLLMState):
    llm_state.response_payload = {
        "id": "chatcmpl-7",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_3",
                        "type": "function",
                        "function": {
                            "name": "search_transactions",
                            "arguments": "{bad-json",
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }

    tool_name, tool_args = call_ai_to_select_tool("Find transactions", tools=[], model="search-model", temperature=0.0)
    assert tool_name == "search_transactions"
    assert tool_args == {}


def test_call_ai_to_select_tool_no_tool_calls_returns_empty(llm_state: MockLLMState):
    llm_state.response_payload = {
        "id": "chatcmpl-8",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "No tool needed",
            },
            "finish_reason": "stop",
        }],
    }

    tool_name, tool_args = call_ai_to_select_tool("Check", tools=[], model="search-model", temperature=0.0)
    assert tool_name == ""
    assert tool_args == {}


def test_call_ai_to_select_tool_accepts_list_of_messages(llm_state: MockLLMState):
    llm_state.response_payload = {
        "id": "chatcmpl-9",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_4",
                        "type": "function",
                        "function": {
                            "name": "search_transactions",
                            "arguments": json.dumps({"limit": 5}),
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }

    messages = [
        {"role": "system", "content": "Assistant"},
        {"role": "user", "content": "List transactions"},
    ]
    tool_name, tool_args = call_ai_to_select_tool(messages, tools=[], model="search-model", temperature=0.0)
    assert tool_name == "search_transactions"
    assert tool_args == {"limit": 5}

    req = llm_state.last_request_body
    assert req["messages"] == messages


# ============================================================================
# MCP Tools Discovery Fallback & Conversion Logic
# ============================================================================

def test_fetch_mcp_tools_returns_empty_when_server_unavailable():
    orig_url = ollama_service.MCP_SERVER_URL
    try:
        ollama_service.MCP_SERVER_URL = "http://localhost:59999/mcp"
        tools = fetch_mcp_tools()
        assert isinstance(tools, list)
        assert tools == []
    finally:
        ollama_service.MCP_SERVER_URL = orig_url


def test_execute_mcp_tool_raises_when_server_unavailable():
    orig_url = ollama_service.MCP_SERVER_URL
    try:
        ollama_service.MCP_SERVER_URL = "http://localhost:59999/mcp"
        with pytest.raises(Exception):
            execute_mcp_tool("unknown_tool", {})
    finally:
        ollama_service.MCP_SERVER_URL = orig_url


def test_fetch_mcp_tools_success():
    """Verifies lines 194-210 conversion of FastMCP tools to OpenAI function format."""
    mock_mcp_tool = SimpleNamespace(
        name="search_transactions",
        description="Search user transactions",
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
    )

    class FakeMCPClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            return [mock_mcp_tool]

    orig_client = ollama_service.Client
    ollama_service.Client = FakeMCPClient
    try:
        tools = fetch_mcp_tools()
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "search_transactions"
        assert tools[0]["function"]["description"] == "Search user transactions"
        assert tools[0]["function"]["parameters"] == {"type": "object", "properties": {"limit": {"type": "integer"}}}
    finally:
        ollama_service.Client = orig_client


def test_execute_mcp_tool_success():
    """Verifies lines 182-192 tool execution through FastMCP Client."""
    class FakeMCPClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def call_tool(self, tool_name, arguments=None):
            return SimpleNamespace(data={"result": f"called {tool_name} with {arguments}"})

    orig_client = ollama_service.Client
    ollama_service.Client = FakeMCPClient
    try:
        data = execute_mcp_tool("my_tool", {"param": 123})
        assert data == {"result": "called my_tool with {'param': 123}"}
    finally:
        ollama_service.Client = orig_client


def test_fetch_mcp_tools_returns_empty_when_mcp_disabled():
    assert fetch_mcp_tools(mcp_enabled=False) == []


def test_execute_mcp_tool_returns_empty_when_mcp_disabled():
    assert execute_mcp_tool("search_transactions", {}, mcp_enabled=False) == []


def test_execute_mcp_tool_returns_empty_when_rag_disabled():
    assert execute_mcp_tool("retrieve_context", {}, rag_enabled=False) == []


@pytest.mark.parametrize(
    "val, expected",
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("TRUE", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        ("other", False),
    ],
)
def test_environment_flag_parsing(val, expected):
    assert _environment_flag("TEST_FLAG", default=True, env={"TEST_FLAG": val}) is expected


def test_compose_and_ci_disable_mcp_and_rag():
    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    compose_path = repo_root / "docker-compose.yml"
    ci_path = repo_root / ".github" / "workflows" / "David-CI.yml"

    compose_text = compose_path.read_text(encoding="utf-8")
    ci_text = ci_path.read_text(encoding="utf-8")

    # savings-backend environment in docker-compose.yml
    savings_section = compose_text.split("savings-backend:", 1)[1].split("depends_on:", 1)[0]
    assert "MCP_ENABLED: ${MCP_ENABLED:-true}" in savings_section
    assert "RAG_ENABLED: ${RAG_ENABLED:-true}" in savings_section

    # David-CI.yml disables MCP and RAG during health check
    assert 'MCP_ENABLED: "false"' in ci_text
    assert 'RAG_ENABLED: "false"' in ci_text

