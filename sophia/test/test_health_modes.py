"""The /health modes block is computed from config plus the existing Ollama probe; no other network call."""
import json
from types import SimpleNamespace

import requests

from sophia.backend import app as backend_app_module
from sophia.backend import config
from sophia.backend.clients import bills_db, transactions


def _client(monkeypatch, mcp, rag, ollama_up):
    """Build a test client with the switches set and the Ollama probe answering or refusing."""
    monkeypatch.setattr(bills_db, "health", lambda: {"ok": True})
    monkeypatch.setattr(transactions, "list_transactions", lambda merchant=None, since=None: ([], "stub"))

    def get(*args, **kwargs):
        if ollama_up:
            return SimpleNamespace(ok=True)
        raise requests.ConnectionError("no network in tests")

    monkeypatch.setattr(backend_app_module.requests, "get", get)
    monkeypatch.setattr(config, "MCP_ENABLED", mcp)
    monkeypatch.setattr(config, "RAG_ENABLED", rag)
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", mcp)
    app = backend_app_module.create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_health_reports_disabled_modes_and_ai_unavailable_when_ollama_is_down(monkeypatch):
    body = json.loads(_client(monkeypatch, False, False, ollama_up=False).get("/health").data)
    assert body["ok"] is True
    assert body["ollama"] == "down"
    assert body["modes"] == {"ai": "unavailable", "mcp": "disabled", "rag": "disabled", "multi_agent": "disabled"}


def test_health_reports_enabled_modes_and_ai_enabled_when_ollama_answers(monkeypatch):
    body = json.loads(_client(monkeypatch, True, True, ollama_up=True).get("/health").data)
    assert body["ollama"] == "up"
    assert body["modes"] == {"ai": "enabled", "mcp": "enabled", "rag": "enabled", "multi_agent": "enabled"}


def test_health_reports_ai_disabled_and_skips_the_probe_when_ai_mode_is_off(monkeypatch):
    """The cloud baseline: AI mode off reports "disabled", not "unavailable", and never probes Ollama."""
    monkeypatch.setattr(bills_db, "health", lambda: {"ok": True})
    monkeypatch.setattr(transactions, "list_transactions", lambda merchant=None, since=None: ([], "stub"))

    def refuse(*args, **kwargs):
        raise AssertionError("AI mode is off; /health must not probe Ollama")

    monkeypatch.setattr(backend_app_module.requests, "get", refuse)
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    monkeypatch.setattr(config, "MULTI_AGENT_ENABLED", False)
    app = backend_app_module.create_app()
    app.config["TESTING"] = True

    body = json.loads(app.test_client().get("/health").data)
    assert body["ollama"] == "off"
    assert body["modes"] == {"ai": "disabled", "mcp": "disabled", "rag": "disabled", "multi_agent": "disabled"}
