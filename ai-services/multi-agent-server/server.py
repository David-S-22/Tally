import hmac
import logging
import sqlite3
import uuid
from pathlib import Path

from agents import Agents
from contracts import VERSION, WorkflowError
from coordinator import Coordinator
from flask import Flask, g, jsonify, request
from dotenv import load_dotenv
from settings import Settings
from storage import InstanceLock
from werkzeug.exceptions import HTTPException

logger = logging.getLogger(__name__)


def create_app(log_dir=None, settings=None, agent_runner=None):
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 96 * 1024
    app.json.ensure_ascii = False
    runner = agent_runner or Agents(settings)
    coordinator = Coordinator(log_dir, settings, runner)
    app.config["coordinator"] = coordinator

    @app.before_request
    def before():
        g.request_id = str(uuid.uuid4())
        if settings.token:
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(
                supplied.encode(), f"Bearer {settings.token}".encode()
            ):
                raise WorkflowError(401, "Valid bearer token required", "unauthorized")
        logger.info(
            "request_id=%s method=%s path=%s",
            g.request_id,
            request.method,
            request.path,
        )

    @app.after_request
    def after(response):
        response.headers["X-Request-ID"] = g.get("request_id", "")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if (
            response.status_code == 503
            and response.is_json
            and response.json.get("code") == "workflow_busy"
        ):
            response.headers["Retry-After"] = "5"
        return response

    @app.errorhandler(WorkflowError)
    def workflow_error(exc):
        return jsonify(
            status="error",
            error=str(exc),
            code=exc.code,
            request_id=g.get("request_id"),
        ), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(
            status="error",
            error=exc.name,
            code="http_error",
            request_id=g.get("request_id"),
        ), exc.code

    @app.errorhandler(sqlite3.Error)
    @app.errorhandler(OSError)
    def persistence_error(exc):
        logger.exception("Persistence failed request_id=%s", g.get("request_id"))
        return jsonify(
            status="error",
            error="Workflow storage is unavailable",
            code="storage_unavailable",
            request_id=g.get("request_id"),
        ), 503

    @app.get("/health")
    def health():
        provider = runner.model.health()
        return jsonify(
            service="multi-agent-server",
            ok=True,
            alive=True,
            contract_version=VERSION,
            models=settings.models,
            mcp="not_used",
            export_mode="on_demand",
            **provider,
        )

    @app.post("/workflow")
    def workflow():
        record = coordinator.run_workflow(request.get_json(), g.request_id)
        return jsonify(record), 503 if record["status"] == "error" else 200

    @app.post("/workflow/<workflow_id>/decision")
    def decision(workflow_id):
        return jsonify(coordinator.record_decision(workflow_id, request.get_json()))

    @app.get("/workflow/status")
    def status():
        return jsonify(coordinator.status(request.args.get("feature")))

    @app.get("/workflow/<workflow_id>")
    def get_workflow(workflow_id):
        return jsonify(coordinator.get_workflow(workflow_id))

    @app.get("/workflow/<workflow_id>/export")
    def export(workflow_id):
        coordinator.get_workflow(workflow_id)
        return jsonify(coordinator.store.export(workflow_id))

    return app


if __name__ == "__main__":
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    configuration = Settings.from_env()
    with InstanceLock(configuration.data_dir):
        create_app(settings=configuration).run(
            host=configuration.host,
            port=configuration.port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
