"""Environment configuration for the transactions backend."""
import logging
import os


def _environment_flag(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


PORT = int(os.environ.get("PORT", "5001"))
TRANSACTIONS_DB_URL = os.environ.get(
    "TRANSACTIONS_DB_URL",
    "http://transactions-db:6001",
).rstrip("/")
DATABASE_TIMEOUT_SECONDS = float(os.environ.get("DATABASE_TIMEOUT_SECONDS", "20"))
ANOMALIES_BACKEND_URL = os.environ.get(
    "ANOMALIES_BACKEND_URL",
    "http://anomalies-backend:5004",
).rstrip("/")
ANOMALIES_TIMEOUT_SECONDS = float(
    os.environ.get("ANOMALIES_TIMEOUT_SECONDS", "10")
)
OLLAMA_URL = os.environ.get(
    "OLLAMA_URL",
    "http://ollama:11434",
).rstrip("/")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen2.5:3b")
AGENT_MAX_ITERATIONS = min(
    2,
    max(
        1,
        int(os.environ.get("AGENT_MAX_ITERATIONS", "2")),
    ),
)
AGENT_TRACE_ENABLED = _environment_flag("AGENT_TRACE_ENABLED", True)
AGENT_LOG_ENABLED = _environment_flag("AGENT_LOG_ENABLED", True)
AGENT_REQUEST_TTL_SECONDS = max(
    1,
    int(os.environ.get("AGENT_REQUEST_TTL_SECONDS", "900")),
)
AI_TIMEOUT_SECONDS = float(os.environ.get("AI_TIMEOUT_SECONDS", "90"))

# MCP mode
MCP_ENABLED = _environment_flag("MCP_ENABLED", True)
MCP_SERVER_URL = os.environ.get(
    "MCP_SERVER_URL",
    "http://host.docker.internal:8000/mcp",
).rstrip("/")
MCP_TIMEOUT_SECONDS = float(os.environ.get("MCP_TIMEOUT_SECONDS", "30"))
MCP_ALLOWED_TOOLS = frozenset(
    name.strip()
    for name in os.environ.get(
        "MCP_ALLOWED_TOOLS",
        "search_transactions",
    ).split(",")
    if name.strip()
)
MCP_FALLBACK_TO_DATABASE = _environment_flag("MCP_FALLBACK_TO_DATABASE", True)
MCP_TOOL_SUPPORTS_EXTENDED_FILTERS = _environment_flag(
    "MCP_TOOL_SUPPORTS_EXTENDED_FILTERS",
    True,
)

# RAG mode
RAG_ENABLED = _environment_flag("RAG_ENABLED", True)
RAG_SERVER_URL = os.environ.get(
    "RAG_SERVER_URL",
    "http://host.docker.internal:5003",
).rstrip("/")
RAG_RECORDS_COLLECTION = os.environ.get(
    "RAG_RECORDS_COLLECTION",
    "transactions-records",
)
RAG_GUIDE_COLLECTION = os.environ.get("RAG_GUIDE_COLLECTION", "transactions")
RAG_TOP_K = max(1, int(os.environ.get("RAG_TOP_K", "6")))
RAG_GUIDE_TOP_K = max(1, int(os.environ.get("RAG_GUIDE_TOP_K", "3")))
RAG_TIMEOUT_SECONDS = float(os.environ.get("RAG_TIMEOUT_SECONDS", "15"))
RAG_REFRESH_ON_START = _environment_flag("RAG_REFRESH_ON_START", True)
RAG_REFRESH_AFTER_WRITE = _environment_flag("RAG_REFRESH_AFTER_WRITE", True)
RAG_MODEL = os.environ.get("RAG_MODEL", "qwen2.5:3b")

# Multi-Agent mode
MULTI_AGENT_ENABLED = _environment_flag("MULTI_AGENT_ENABLED", False)
MULTI_AGENT_SERVER_URL = os.environ.get(
    "MULTI_AGENT_SERVER_URL",
    "http://host.docker.internal:5010",
).rstrip("/")
MULTI_AGENT_TOKEN = os.environ.get("MULTI_AGENT_TOKEN", "")
MULTI_AGENT_TIMEOUT_SECONDS = float(
    os.environ.get("MULTI_AGENT_TIMEOUT_SECONDS", "600")
)

_RAG_THRESHOLD_DEFAULTS = (0.6, 0.9, 1.2)


def _rag_thresholds():
    """Return the (high, medium, low) retrieval distance thresholds.

    A best distance below RAG_HIGH is high confidence, below RAG_MEDIUM is
    medium, and up to RAG_LOW is low. Documents farther than RAG_LOW are not
    used as context at all. The values must satisfy 0 < high < medium < low;
    an invalid or unparseable combination falls back to the defaults with one
    warning rather than failing startup.
    """
    try:
        high = float(os.environ.get(
            "RAG_HIGH",
            str(_RAG_THRESHOLD_DEFAULTS[0]),
        ))
        medium = float(os.environ.get(
            "RAG_MEDIUM",
            str(_RAG_THRESHOLD_DEFAULTS[1]),
        ))
        low = float(os.environ.get(
            "RAG_LOW",
            str(_RAG_THRESHOLD_DEFAULTS[2]),
        ))
    except ValueError:
        high = medium = low = None

    if high is not None and 0 < high < medium < low:
        return high, medium, low

    logging.getLogger(__name__).warning(
        "Invalid RAG distance thresholds; using defaults "
        "RAG_HIGH=%s RAG_MEDIUM=%s RAG_LOW=%s",
        *_RAG_THRESHOLD_DEFAULTS,
    )
    return _RAG_THRESHOLD_DEFAULTS


RAG_HIGH, RAG_MEDIUM, RAG_LOW = _rag_thresholds()

# Model log-probability gate on the grounded category answer.
RAG_LOGPROBS = _environment_flag("RAG_LOGPROBS", True)
_RAG_PROBABILITY_DEFAULTS = (0.8, 0.5)


def _rag_probability_thresholds():
    """Return the (high, medium) minimum answer probabilities.

    Ollama returns per-token log probabilities when asked; the probability of
    the category-name tokens must be at least RAG_PROB_HIGH to keep a high
    band and at least RAG_PROB_MEDIUM to keep a medium band, otherwise the
    band is downgraded one step. Values must satisfy 0 < medium < high <= 1.
    """
    try:
        high = float(os.environ.get(
            "RAG_PROB_HIGH",
            str(_RAG_PROBABILITY_DEFAULTS[0]),
        ))
        medium = float(os.environ.get(
            "RAG_PROB_MEDIUM",
            str(_RAG_PROBABILITY_DEFAULTS[1]),
        ))
    except ValueError:
        high = medium = None

    if high is not None and 0 < medium < high <= 1:
        return high, medium

    logging.getLogger(__name__).warning(
        "Invalid RAG probability thresholds; using defaults "
        "RAG_PROB_HIGH=%s RAG_PROB_MEDIUM=%s",
        *_RAG_PROBABILITY_DEFAULTS,
    )
    return _RAG_PROBABILITY_DEFAULTS


RAG_PROB_HIGH, RAG_PROB_MEDIUM = _rag_probability_thresholds()
