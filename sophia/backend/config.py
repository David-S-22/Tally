"""Environment configuration for the bills backend, read once at import time."""
import os
from datetime import datetime

PORT = int(os.environ.get("PORT", "5005"))
BILLS_DB_API_URL = os.environ.get("BILLS_DB_API_URL", "http://bills-db:6005")
TRANSACTIONS_DB_API_URL = os.environ.get("TRANSACTIONS_DB_API_URL") or None
FRONTEND_ORIGIN = os.environ.get("FRONTEND_ORIGIN", "http://localhost:3005")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
DRAFT_MODEL = os.environ.get("DRAFT_MODEL", "llama3.1:8b")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen2.5:3b")
AI_TIMEOUT_SECONDS = int(os.environ.get("AI_TIMEOUT_SECONDS", "90"))
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
AI_TEMPERATURE = float(os.environ.get("AI_TEMPERATURE", "0.2"))
GROUNDED_TEMPERATURE = float(os.environ.get("GROUNDED_TEMPERATURE", "0"))
DEMO_TODAY = datetime.strptime(os.environ.get("DEMO_TODAY", "2026-10-01")[:10], "%Y-%m-%d").date()


def _flag(name, default):
    """Read a boolean switch; 1/true/yes/on (any case) is on, anything else is off."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


RAG_DEFAULT_THRESHOLDS = (0.8, 1.1, 1.4)


def _rag_thresholds(env):
    """Return (RAG_HIGH, RAG_MEDIUM, RAG_LOW) from env; the defaults unless 0 < high < medium < low."""
    try:
        high = float(env.get("RAG_HIGH", RAG_DEFAULT_THRESHOLDS[0]))
        medium = float(env.get("RAG_MEDIUM", RAG_DEFAULT_THRESHOLDS[1]))
        low = float(env.get("RAG_LOW", RAG_DEFAULT_THRESHOLDS[2]))
    except (TypeError, ValueError):
        return RAG_DEFAULT_THRESHOLDS
    if 0 < high < medium < low:
        return (high, medium, low)
    return RAG_DEFAULT_THRESHOLDS


MCP_ENABLED = _flag("MCP_ENABLED", False)
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://host.docker.internal:8000/mcp").rstrip("/")
MCP_TIMEOUT_SECONDS = float(os.environ.get("MCP_TIMEOUT_SECONDS", "15"))
MCP_ALLOWED_TOOLS = frozenset(
    name.strip()
    for name in os.environ.get("MCP_ALLOWED_TOOLS", "retrieve_context,search_transactions,list_bills,get_bill_payments,compare_bill_with_bank_charges,get_transactions_with_confirmed_anomalies").split(",")
    if name.strip()
)
RAG_ENABLED = _flag("RAG_ENABLED", False)
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "3"))
RAG_HIGH, RAG_MEDIUM, RAG_LOW = _rag_thresholds(os.environ)
GROUNDED_TIMEOUT_SECONDS = float(os.environ.get("GROUNDED_TIMEOUT_SECONDS", "20"))
