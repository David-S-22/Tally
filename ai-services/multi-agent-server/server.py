"""Shared Multi-Agent Server: HTTP routes from the Release 2 contract.

Run on the host, never in Docker Compose:

    python ai-services/multi-agent-server/server.py

Contract: docs/release-2/shared/adr-multi-agent-server-contract.md
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))  # local imports, like rag-server

from flask import Flask, jsonify, request

from coordinator import Coordinator, WorkflowError
import agents

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

PORT = int(os.getenv("MULTI_AGENT_PORT", "5010"))
LOG_DIR = os.getenv("MULTI_AGENT_LOG_DIR", os.path.join(os.path.dirname(__file__), "logs"))


def create_app(log_dir=LOG_DIR):
    app = Flask(__name__)
    coordinator = Coordinator(log_dir)
    app.config["coordinator"] = coordinator

    @app.errorhandler(WorkflowError)
    def workflow_error(exc):
        return jsonify({"status": "error", "error": str(exc)}), exc.status

    @app.before_request
    def log_request():
        logger.info("%s %s", request.method, request.path)

    @app.get("/health")
    def health():
        return jsonify({
            "service": "multi-agent-server",
            "ok": True,
            "ollama": "stub",
            "models": {"planner": agents.MODEL, "worker": agents.MODEL, "reviewer": agents.MODEL},
            "mcp": "not_used",
        })

    @app.post("/workflow")
    def workflow():
        return jsonify(coordinator.run_workflow(request.get_json(silent=True)))

    @app.post("/workflow/<workflow_id>/decision")
    def decision(workflow_id):
        return jsonify(coordinator.record_decision(workflow_id, request.get_json(silent=True) or {}))

    @app.get("/workflow/status")
    def status():
        return jsonify(coordinator.status(feature=request.args.get("feature")))

    @app.get("/workflow/<workflow_id>")
    def get_workflow(workflow_id):
        return jsonify(coordinator.get_workflow(workflow_id))

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=PORT)
