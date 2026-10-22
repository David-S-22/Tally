import os


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


BUDGETS_DB_URL = os.environ.get("BUDGETS_DB_URL", "http://budgets-db:6006").rstrip("/")
DATABASE_TIMEOUT_SECONDS = float(os.environ.get("DATABASE_TIMEOUT_SECONDS", "10"))
TRANSACTIONS_API_URL = os.environ.get("TRANSACTIONS_API_URL", "http://transactions-backend:5001").rstrip("/")
TRANSACTIONS_TIMEOUT_SECONDS = float(os.environ.get("TRANSACTIONS_TIMEOUT_SECONDS", "10"))
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen2.5:3b")
AI_TIMEOUT_SECONDS = float(os.environ.get("AI_TIMEOUT_SECONDS", "90"))
# AI mode (direct Ollama calls); off unless switched on, like MCP/RAG below.
AI_MODE_ENABLED = _flag("AI_MODE_ENABLED", False)

MCP_ENABLED = _flag("MCP_ENABLED", False)
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://host.docker.internal:8000/mcp").rstrip("/")
MCP_TIMEOUT_SECONDS = float(os.environ.get("MCP_TIMEOUT_SECONDS", "15"))
MCP_ALLOWED_TOOLS = frozenset(
    name.strip()
    for name in os.environ.get("MCP_ALLOWED_TOOLS", "search_transactions,retrieve_context").split(",")
    if name.strip()
)
MULTI_AGENT_ENABLED = _flag("MULTI_AGENT_ENABLED", False)
MULTI_AGENT_URL = os.environ.get("MULTI_AGENT_URL", "http://host.docker.internal:5010").rstrip("/")
MULTI_AGENT_TOKEN = os.environ.get("MULTI_AGENT_TOKEN", "")
MULTI_AGENT_TIMEOUT_SECONDS = float(os.environ.get("MULTI_AGENT_TIMEOUT_SECONDS", "600"))
MULTI_AGENT_READ_TIMEOUT_SECONDS = float(os.environ.get("MULTI_AGENT_READ_TIMEOUT_SECONDS", "15"))
RAG_ENABLED = _flag("RAG_ENABLED", False)
RAG_SERVER_URL = os.environ.get("RAG_SERVER_URL", "http://host.docker.internal:5003").rstrip("/")
RAG_TIMEOUT_SECONDS = float(os.environ.get("RAG_TIMEOUT_SECONDS", "15"))
RAG_FEATURE = os.environ.get("RAG_FEATURE", "budgets").strip() or "budgets"
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "3"))
RAG_HIGH = float(os.environ.get("RAG_HIGH", "0.8"))
RAG_MEDIUM = float(os.environ.get("RAG_MEDIUM", "1.1"))
RAG_LOW = float(os.environ.get("RAG_LOW", "1.4"))
