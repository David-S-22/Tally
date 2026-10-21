"""Exception types the service layer raises; each route protocol translates them."""

MODE_MESSAGES = {
    "mcp_disabled": "MCP mode is disabled.",
    "rag_disabled": "RAG mode is disabled.",
    "mcp_connection": "The MCP server is unavailable.",
    "mcp_timeout": "The MCP server did not respond in time.",
    "mcp_tool_error": "The MCP tool reported an error.",
    "mcp_invalid_result": "The MCP tool returned an invalid result.",
    "tool_not_allowed": "The MCP tool is not allowed.",
    "rag_unavailable": "The RAG server is unavailable.",
    "multi_agent_disabled": "Multi-Agent mode is disabled.",
    "multi_agent_connection": "The Multi-Agent server is unavailable.",
    "multi_agent_timeout": "The Multi-Agent server did not respond in time.",
    "multi_agent_error": "The Multi-Agent server reported an error.",
    "multi_agent_invalid_result": "The Multi-Agent server returned an invalid result.",
    "multi_agent_rejected": "The Multi-Agent server rejected the request.",
    "multi_agent_busy": "The Multi-Agent server is running another workflow; try again shortly.",
    "multi_agent_unauthorized": "The Multi-Agent server refused the token.",
    "multi_agent_failed": "The Multi-Agent workflow failed; only Reject is available.",
}

MODE_STATUSES = {
    "mcp_disabled": 503,
    "rag_disabled": 503,
    "mcp_connection": 503,
    "mcp_timeout": 503,
    "mcp_tool_error": 502,
    "mcp_invalid_result": 502,
    "tool_not_allowed": 400,
    "rag_unavailable": 503,
    "multi_agent_disabled": 503,
    "multi_agent_connection": 503,
    "multi_agent_timeout": 503,
    "multi_agent_error": 502,
    "multi_agent_invalid_result": 502,
    "multi_agent_rejected": 409,
    "multi_agent_busy": 503,
    "multi_agent_unauthorized": 503,
    "multi_agent_failed": 409,
}


class ServiceError(Exception):
    """A service-layer failure with an HTTP status and, for mode failures, a safe code."""

    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class NotFound(ServiceError):
    def __init__(self, message="not found"):
        super().__init__(message, status=404)


class ModeError(ServiceError):
    """An MCP/RAG/Multi-Agent refusal or failure with a safe code; the message never carries upstream text."""

    def __init__(self, code):
        super().__init__(MODE_MESSAGES[code], status=MODE_STATUSES[code], code=code)
