import asyncio
import json
import math
import os
import pathlib
from typing import Any, Literal
from fastmcp import Client
from openai import OpenAI

ConfidenceCategory = Literal["High", "Medium", "Low"]

def _environment_flag(name: str, default: bool = True, env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    val = source.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

# AI mode (direct Ollama calls); off unless switched on, so the cloud baseline refuses.
AI_MODE_ENABLED = _environment_flag("AI_MODE_ENABLED", False)
MCP_ENABLED = _environment_flag("MCP_ENABLED", True)
RAG_ENABLED = _environment_flag("RAG_ENABLED", True)
MULTI_AGENT_ENABLED = _environment_flag("MULTI_AGENT_ENABLED", True)
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")
OLLAMA_API_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "180"))
planner_model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
search_model = os.environ.get("OLLAMA_TOOL_MODEL", "qwen2.5:3b")
classifier_model = os.environ.get("OLLAMA_CLASSIFIER_MODEL", "qwen2.5:3b")

client = OpenAI(base_url=OLLAMA_API_URL, api_key="ollama", timeout=OLLAMA_TIMEOUT)


class AIError(Exception):
    """An AI-mode refusal with a safe code; the message carries no upstream text."""

    def __init__(self, code: str = "ai_disabled", message: str = "AI mode is disabled."):
        self.code = code
        self.message = message
        self.status = 503
        super().__init__(message)


def _client() -> OpenAI:
    """The Ollama-backed client; refuses when AI mode is off so no model call leaves the process."""
    if not AI_MODE_ENABLED:
        raise AIError()
    return client


def load_prompt(prompt_name: str) -> str:
    """Loads a prompt template file from the prompts directory."""
    filename = prompt_name if prompt_name.endswith(".txt") else f"{prompt_name}.txt"
    prompt_path = pathlib.Path(__file__).resolve().parent.parent / "prompts" / filename
    return prompt_path.read_text(encoding="utf-8").strip()


def _format_messages(prompt: str, system_prompt: str | None = None) -> list[dict[str, str]]:
    """Formats prompt and optional system_prompt as standard message dicts."""
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def get_ai_text_response(
    prompt: str,
    model: str,
    temperature: float,
    system_prompt: str | None = None,
) -> str:
    """Sends a prompt to the AI model and returns its plain-text completion response."""
    messages = _format_messages(prompt, system_prompt=system_prompt)
    resp = _client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()


def calculate_confidence_category(
    logprobs_content: list[Any] | None,
) -> ConfidenceCategory:
    """Calculates the confidence category ('High', 'Medium', 'Low') from token log probabilities."""
    if not logprobs_content:
        return "Medium"

    token_probs = [
        math.exp(getattr(token_info, "logprob", -float('inf')))
        for token_info in logprobs_content
        if getattr(token_info, "logprob", None) is not None
    ]

    if not token_probs:
        return "Medium"

    avg_prob = sum(token_probs) / len(token_probs)
    if avg_prob >= 0.70:
        return "High"
    elif avg_prob >= 0.40:
        return "Medium"
    return "Low"


def get_ai_text_and_calculate_confidence(
    prompt: str,
    model: str,
    temperature: float,
    system_prompt: str | None = None,
) -> tuple[str, ConfidenceCategory]:
    """Prompts the AI model to generate text with token logprobs enabled, and calculates its confidence category."""
    messages = _format_messages(prompt, system_prompt=system_prompt)
    resp = _client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        logprobs=True,
    )
    choice = resp.choices[0]
    content = (choice.message.content or "").strip()

    logprobs_obj = getattr(choice, "logprobs", None)
    logprobs_content = getattr(logprobs_obj, "content", None) if logprobs_obj else None
    confidence_category = calculate_confidence_category(logprobs_content)

    return content, confidence_category


def get_ai_json_response(
    prompt: str,
    model: str,
    temperature: float,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Prompts the AI model in JSON mode and parses the response into a Python dictionary."""
    messages = _format_messages(prompt, system_prompt=system_prompt)
    resp = _client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    content = (resp.choices[0].message.content or "").strip()
    return json.loads(content)


def get_ai_structured_json(
    prompt: str,
    schema: dict[str, Any],
    model: str,
    temperature: float,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Prompts the AI model with grammar-constrained JSON schema decoding and returns the parsed structured dictionary."""
    messages = _format_messages(prompt, system_prompt=system_prompt)
    resp = _client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "StructuredOutput",
                "schema": schema,
            },
        },
    )
    content = (resp.choices[0].message.content or "").strip()
    return json.loads(content)


def call_ai_to_select_tool(
    prompt: str | list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    temperature: float,
    system_prompt: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Calls the AI model with a list of available tools, asking the AI to select an appropriate tool and arguments."""
    if isinstance(prompt, list):
        messages = prompt
    else:
        messages = _format_messages(prompt, system_prompt=system_prompt)

    resp = _client().chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        tools=tools,
        tool_choice="required",
    )
    choice = resp.choices[0]
    tool_calls = getattr(choice.message, "tool_calls", None)
    if not tool_calls:
        return "", {}

    tool_call = tool_calls[0]
    tool_name = str(getattr(getattr(tool_call, "function", None), "name", "") or "")
    tool_args_str = getattr(getattr(tool_call, "function", None), "arguments", "{}") or "{}"
    try:
        tool_args = json.loads(tool_args_str) if isinstance(tool_args_str, str) else tool_args_str
    except Exception:
        tool_args = {}

    return tool_name, tool_args


def execute_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    mcp_enabled: bool | None = None,
    rag_enabled: bool | None = None,
) -> dict[str, Any] | list[dict[str, Any]] | Any:
    """Executes a tool on the FastMCP server with the given arguments and returns the result data."""
    is_mcp = MCP_ENABLED if mcp_enabled is None else mcp_enabled
    is_rag = RAG_ENABLED if rag_enabled is None else rag_enabled

    async def _call():
        if not is_mcp:
            return []
        if tool_name == "retrieve_context" and not is_rag:
            return []
        async with Client(MCP_SERVER_URL) as mcp_client:
            res = await mcp_client.call_tool(tool_name, arguments=arguments)
            return res.data

    return asyncio.run(_call())


def fetch_mcp_tools(mcp_enabled: bool | None = None) -> list[dict[str, Any]]:
    """Fetches tool definitions dynamically from the FastMCP server and converts them to OpenAI format."""
    is_mcp = MCP_ENABLED if mcp_enabled is None else mcp_enabled

    async def _fetch():
        if not is_mcp:
            return []
        async with Client(MCP_SERVER_URL) as mcp_client:
            tools = await mcp_client.list_tools()
            return [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description or "",
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]

    try:
        return asyncio.run(_fetch())
    except Exception:
        return []
