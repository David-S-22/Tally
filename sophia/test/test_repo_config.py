"""Repo-file checks: the compose block and the workflow carry the MCP/RAG switches the report cites."""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _services():
    """The compose services mapping, parsed rather than string-split."""
    return yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def test_compose_bills_backend_block_carries_the_switches_and_keeps_the_r0_ollama_wiring():
    block = _services()["bills-backend"]
    env = block["environment"]
    assert env["MCP_ENABLED"] == "${MCP_ENABLED:-true}"
    assert env["MCP_SERVER_URL"] == "http://host.docker.internal:8000/mcp"
    assert env["RAG_ENABLED"] == "${RAG_ENABLED:-true}"
    assert env["OLLAMA_URL"] == "http://ollama:11434"
    assert "RAG_SERVER_URL" not in env
    assert "host.docker.internal:host-gateway" in block["extra_hosts"]
    assert block["depends_on"] == ["bills-db", "ollama"]
    assert "network_mode" not in block


def test_sophia_ci_disables_mcp_and_rag_and_asserts_it():
    text = (REPO_ROOT / ".github" / "workflows" / "Sophia-CI.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow[True]
    for event in ("pull_request", "push"):
        assert "docker-compose.yml" in triggers[event]["paths"]
        assert "ai-services/rag-server/sources/billing/**" in triggers[event]["paths"]
    assert "workflow_dispatch" in triggers
    assert workflow["env"] == {"MCP_ENABLED": "false", "RAG_ENABLED": "false"}
    assert set(workflow["jobs"]) >= {"test", "docker-health"}
    assert "rag" not in workflow["jobs"]
    assert "--no-deps" in text
    assert '"mcp":"disabled"' in text and '"rag":"disabled"' in text
    assert '"ai":"unavailable"' in text and '"ollama":"down"' in text
    assert '"code":"mcp_disabled"' in text and "/api/evidence" in text and "/api/tools/retrieve_context" in text
    assert "- run: sleep 5" not in text and "seq 1 12" in text
    assert ": #" not in text
