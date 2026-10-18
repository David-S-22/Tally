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
    """An MCP/RAG refusal or failure with a safe code; the message never carries upstream text."""

    def __init__(self, code):
        super().__init__(MODE_MESSAGES[code], status=MODE_STATUSES[code], code=code)
