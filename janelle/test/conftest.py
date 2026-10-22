from pytest import MonkeyPatch, fixture

from janelle.backend import config


@fixture(autouse=True)
def ai_mode_on(monkeypatch: MonkeyPatch):
    """Keep AI mode on unless a test switches it off.

    ``AI_MODE_ENABLED`` defaults to false so the cloud baseline refuses model
    calls outright. The unit suite drives the planner and grounding paths
    behind fake transports, so it needs the switch on; the tests that assert
    the refusal turn it back off.
    """
    monkeypatch.setattr(config, "AI_MODE_ENABLED", True)


@fixture(autouse=True)
def mcp_disabled_by_default(monkeypatch: MonkeyPatch):
    """Keep MCP off unless a test switches it on.

    ``MCP_ENABLED`` defaults to true so the deployed backend reaches the
    shared server, but the unit suite must never open a socket. Tests that
    exercise the MCP path re-enable it and install a fake client.
    """
    monkeypatch.setattr(config, "MCP_ENABLED", False)


@fixture(autouse=True)
def rag_disabled_by_default(monkeypatch: MonkeyPatch):
    """Keep RAG off unless a test switches it on, for the same reason.

    With the switch off the Release 0 create flow is byte-identical, so the
    Release 0 suite keeps passing unchanged. Tests that exercise grounding
    re-enable it and install a fake ``rag_client``.
    """
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    monkeypatch.setattr(config, "RAG_REFRESH_AFTER_WRITE", False)
    monkeypatch.setattr(config, "RAG_REFRESH_ON_START", False)
