"""Flask application factory for the bills backend (:5005)."""
import os
import logging

import requests
from flask import Flask, jsonify
from flask_cors import CORS

from sophia.backend import config
from sophia.backend.clients import bills_db, transactions
from sophia.backend.routes import bills, chat, disputes, evidence, fragments, handoff, payments, tools, views
from sophia.backend.services.errors import ServiceError

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _ollama_status():
    """"off" when AI mode is disabled (no probe is made), otherwise "up"/"down"."""
    if not config.AI_MODE_ENABLED:
        return "off"
    try:
        response = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=2)
        return "up" if response.ok else "down"
    except requests.RequestException:
        return "down"


def create_app():
    app = Flask(__name__, template_folder=TEMPLATE_DIR)
    # Release 2 (B5): CORS(app) with no arguments answers every origin with
    # Access-Control-Allow-Origin: *, so any page on the internet could read this
    # backend's responses in a visitor's browser. The only browser origin that
    # legitimately calls it is the bills frontend, which FRONTEND_ORIGIN already
    # names (set per environment in docker-compose.yml and bicep/main.bicep).
    CORS(app, origins=[config.FRONTEND_ORIGIN])
    app.register_blueprint(bills.bp)
    app.register_blueprint(payments.bp)
    app.register_blueprint(disputes.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(chat.bp)
    app.register_blueprint(handoff.bp)
    app.register_blueprint(fragments.bp)
    app.register_blueprint(tools.bp)
    app.register_blueprint(tools.ui)
    app.register_blueprint(evidence.bp)
    app.register_blueprint(evidence.ui)

    @app.after_request
    def security_headers(response):
        """Release 2 (B5). Bills renders server-side HTML fragments for htmx as well
        as JSON, so nosniff matters here, and no response from this backend should
        be cached by a proxy on the way back to the browser."""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ServiceError)
    def handle_service_error(error):
        payload = {"error": error.message}
        if error.code:
            payload["code"] = error.code
        return jsonify(payload), error.status

    @app.get("/health")
    def health():
        try:
            db_health = bills_db.health()
            # bills-db leaves /health open for probes, so reaching it proves only
            # that the service is running. When it says a token is required and
            # ours was not accepted, every other route will refuse us: report that
            # rather than "up".
            db_api = "unauthorized" if db_health.get("auth_required") and not db_health.get("authenticated") else "up"
        except requests.RequestException:
            db_api = "down"
        _rows, transactions_source = transactions.list_transactions()
        ollama = _ollama_status()
        return jsonify(
            {
                "ok": db_api == "up",
                "today": config.DEMO_TODAY.isoformat(),
                "db_api": db_api,
                "transactions_api": transactions_source,
                "ollama": ollama,
                "modes": {
                    "ai": ("enabled" if ollama == "up" else "unavailable") if config.AI_MODE_ENABLED else "disabled",
                    "mcp": "enabled" if config.MCP_ENABLED else "disabled",
                    "rag": "enabled" if config.RAG_ENABLED else "disabled",
                    "multi_agent": "enabled" if config.MULTI_AGENT_ENABLED else "disabled",
                },
            }
        )

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    application = create_app()
    application.run(host="0.0.0.0", port=config.PORT)
