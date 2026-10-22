import json
import logging
import time

import requests
from flask import Flask, jsonify, make_response, render_template, request
from flask.logging import default_handler

from . import config
from .Helpers import (
    database_response_error,
    format_currency,
    format_transaction_date,
    json_object,
    json_response,
    normalize_transaction_id_answer,
    render_categories_page,
    render_category_form,
    render_category_list,
    render_transaction_form,
    render_transaction_page,
    render_transaction_table_from_database,
)
from .services import category_review, mcp_client, rag_client, rag_corpus
from .services.chat_service import ChatError
from .services.transaction_orchestrator import (
    get_preview_request_context,
    log_workflow_event,
    orchestrate_transaction_request,
    run_category_selection,
    run_confirmed_transaction,
)


def mode_error_response(error):
    """Map an MCP or RAG client error onto the ChatError JSON shape.

    A disabled switch is 503 with the disabled code; every other failure is
    502 with the generic unavailable code so no detail leaks to the client.
    AI mode off is its own 503 ai_disabled, whichever client raised it.
    """
    if error.code == "ai_disabled":
        chat_error = ChatError("AI mode is disabled.", "ai_disabled", 503)
        return jsonify(chat_error.to_dict()), chat_error.status
    if isinstance(error, mcp_client.MCPError):
        disabled_code, unavailable_code = "mcp_disabled", "mcp_unavailable"
        unavailable_message = "The MCP server is unavailable."
    else:
        disabled_code, unavailable_code = "rag_disabled", "rag_unavailable"
        unavailable_message = "The RAG server is unavailable."

    if error.code == disabled_code:
        chat_error = ChatError(error.message, disabled_code, 503)
    else:
        chat_error = ChatError(unavailable_message, unavailable_code, 502)
    return jsonify(chat_error.to_dict()), chat_error.status


def setup_app(db_url: str) -> Flask:
    application = Flask(__name__)
    if config.AGENT_LOG_ENABLED:
        application.logger.setLevel(logging.INFO)
        # Background refresh threads log outside any request context, so the
        # module-level workflow logger needs the same level and handler.
        workflow_logger = logging.getLogger("janelle.ai.workflow")
        workflow_logger.setLevel(logging.INFO)
        if not workflow_logger.handlers:
            workflow_logger.addHandler(default_handler)
    db_url = db_url.rstrip("/")
    anomalies_backend_url = config.ANOMALIES_BACKEND_URL.rstrip("/")

    def check_transaction_for_anomalies(transaction):
        """Ask the anomalies backend to review a newly created transaction.

        Runs server-side so the check cannot be bypassed by the client. The
        anomalies backend queues the transaction and returns immediately, so
        this call is quick. Failures are logged and swallowed so they never
        affect transaction creation.
        """
        try:
            response = requests.post(
                f"{anomalies_backend_url}/check-transaction",
                json=transaction,
                timeout=config.ANOMALIES_TIMEOUT_SECONDS,
            )
        except requests.RequestException as error:
            application.logger.warning(
                "Anomaly check request failed: %s",
                error,
            )
            return

        if response.ok:
            return

        application.logger.warning(
            "Anomaly check returned %s: %s",
            response.status_code,
            response.text,
        )

    def created_transaction(response):
        """Return the created row from a 201 response, or None."""
        if response.status_code != 201:
            return None
        try:
            created = response.json()
        except (ValueError, RecursionError):
            return None
        return created if isinstance(created, dict) else None

    def proxy_database(method, path, **options):
        """Forward a JSON request to the database API and relay its reply."""
        response = getattr(requests, method)(
            f"{db_url}{path}",
            timeout=config.DATABASE_TIMEOUT_SECONDS,
            **options,
        )
        return json_response(response)

    def render_chat_error(message, status):
        return render_template(
            "chat_result.jinja",
            result=None,
            error=message,
            success=None,
        ), status

    application.jinja_env.filters["transaction_date"] = (
        format_transaction_date
    )
    application.jinja_env.filters["currency"] = format_currency

    @application.context_processor
    def multi_agent_flag():
        return {"multi_agent_enabled": config.MULTI_AGENT_ENABLED}

    @application.errorhandler(requests.RequestException)
    def handle_database_unavailable(error):
        application.logger.warning(
            "Transactions database request failed: %s",
            error,
        )
        return jsonify({
            "error": "transactions database is unavailable",
            "code": "database_unavailable",
        }), 503

    @application.errorhandler(ChatError)
    def handle_chat_error(error):
        return jsonify(error.to_dict()), error.status

    @application.route("/")
    def get_index():
        return jsonify(container="transactions-backend")

    @application.get("/health")
    def get_health():
        return jsonify(
            ok=True,
            container="transactions-backend",
            modes={
                "ai": "enabled" if config.AI_MODE_ENABLED else "disabled",
                "mcp": "enabled" if config.MCP_ENABLED else "disabled",
                "rag": "enabled" if config.RAG_ENABLED else "disabled",
            },
        )

    @application.get("/mcp/tools")
    def get_mcp_tools():
        started = time.perf_counter()
        try:
            tools = mcp_client.list_tools()
        except mcp_client.MCPError as error:
            log_tools_listed(started, 0, error.code)
            return mode_error_response(error)
        log_tools_listed(started, len(tools), None)
        return jsonify(tools=tools)

    def log_tools_listed(started, count, error):
        """Record one MCP_TOOLS_LISTED workflow event for the diagnostic."""
        if not config.AGENT_LOG_ENABLED:
            return
        log_workflow_event({
            "event": "MCP_TOOLS_LISTED",
            "server": config.MCP_SERVER_URL,
            "tools": count,
            "status": "failed" if error else "succeeded",
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "error": error,
        })

    @application.post("/rag/refresh")
    def refresh_rag_records():
        if not config.RAG_ENABLED:
            return mode_error_response(rag_client.RAGError("rag_disabled"))
        try:
            result = rag_corpus.refresh_records(db_url, "manual")
        except rag_client.RAGError as error:
            return mode_error_response(error)
        return jsonify(
            feature=result["feature"],
            total=result["total"],
            kinds=result["kinds"],
            duration_ms=result["duration_ms"],
        )

    @application.route("/transactions")
    def get_transactions():
        return proxy_database("get", "/transactions", params=request.args)

    @application.route("/ui/transactions")
    def get_transaction_rows():
        return render_transaction_table_from_database(db_url)

    @application.delete("/ui/transactions/<int:transaction_id>")
    def delete_ui_transaction(transaction_id):
        """Delete one row from the table and re-render the current page.

        The table fragment is returned directly so HTMX swaps it in place;
        no ``transactionsChanged`` trigger is fired because that would make
        the fresh table reload itself a second time.
        """
        try:
            response = requests.delete(
                f"{db_url}/transactions/{transaction_id}",
                timeout=config.DATABASE_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            return render_transaction_table_from_database(
                db_url,
                notice="The transaction could not be deleted because the database is unavailable.",
                notice_kind="error",
            )

        if response.status_code >= 400:
            return render_transaction_table_from_database(
                db_url,
                notice=database_response_error(
                    response,
                    "The transaction could not be deleted.",
                ),
                notice_kind="error",
            )

        return render_transaction_table_from_database(
            db_url,
            notice="Transaction deleted.",
        )

    @application.get("/ui/transactions/page")
    def get_transactions_page():
        return render_transaction_page(db_url)

    @application.get("/ui/transactions/new")
    def get_new_transaction_form():
        return render_transaction_form(db_url)

    @application.get("/ui/categories/new")
    def get_new_category_form():
        return render_category_form()

    @application.get("/ui/categories")
    def get_categories_page():
        return render_categories_page(db_url)

    @application.delete("/ui/categories/<int:category_id>")
    def delete_ui_category(category_id):
        """Delete a category and re-render the manage-categories list.

        The database refuses to delete the protected category or one that is
        still referenced by transactions or corrections; that message is
        shown as-is so the user knows why nothing changed.
        """
        try:
            response = requests.delete(
                f"{db_url}/categories/{category_id}",
                timeout=config.DATABASE_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            return render_category_list(
                db_url,
                notice="The category could not be deleted because the database is unavailable.",
                notice_kind="error",
            )

        if response.status_code >= 400:
            return render_category_list(
                db_url,
                notice=database_response_error(
                    response,
                    "The category could not be deleted.",
                ),
                notice_kind="error",
            )

        return render_category_list(db_url, notice="Category deleted.")

    @application.post("/ui/categories")
    def create_ui_category():
        values = request.form.to_dict()
        payload = {
            "name": values.get("name"),
            "type": values.get("type") or None,
        }
        try:
            response = requests.post(
                f"{db_url}/categories",
                json=payload,
                timeout=config.DATABASE_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            return render_category_form(
                "The category could not be saved because the database is unavailable.",
                values,
            )

        if response.status_code >= 400:
            return render_category_form(
                database_response_error(
                    response,
                    "The category could not be saved.",
                ),
                values,
            )

        return render_categories_page(
            db_url,
            notice="Category saved.",
        )

    @application.post("/ui/transactions")
    def create_ui_transaction():
        values = request.form.to_dict()
        try:
            payload = {
                "date": values.get("date"),
                "amount": float(values.get("amount", "")),
                "merchant": values.get("merchant"),
                "description": values.get("description"),
                "category_id": int(values.get("category_id", "")),
            }
        except (TypeError, ValueError):
            return render_transaction_form(
                db_url,
                "Enter a valid amount and category.",
                values,
            )

        try:
            response = requests.post(
                f"{db_url}/transactions",
                json=payload,
                timeout=config.DATABASE_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            return render_transaction_form(
                db_url,
                "The transaction could not be saved because the database is unavailable.",
                values,
            )

        if response.status_code >= 400:
            return render_transaction_form(
                db_url,
                database_response_error(
                    response,
                    "The transaction could not be saved.",
                ),
                values,
            )

        created = created_transaction(response)
        if created is not None:
            check_transaction_for_anomalies(created)

        page = make_response(render_transaction_page(
            db_url,
            notice="Transaction saved.",
        ))
        if created is not None and created.get("id") is not None:
            page.headers["HX-Trigger"] = json.dumps(
                {"transaction-created": created["id"]}
            )
        return page

    @application.route("/transactions", methods=["POST"])
    def create_transaction():
        payload = json_object()
        if "suggested_category_id" in payload:
            raise ChatError(
                "suggested_category_id is only accepted from a confirmed "
                "agent category override",
                "unsupported_fields",
                422,
            )
        response = requests.post(
            f"{db_url}/transactions",
            json=payload,
            timeout=config.DATABASE_TIMEOUT_SECONDS,
        )
        created = created_transaction(response)
        if created is not None:
            check_transaction_for_anomalies(created)
        return json_response(response)

    @application.route("/transactions/<int:transaction_id>")
    def get_transaction(transaction_id):
        return proxy_database("get", f"/transactions/{transaction_id}")

    @application.route(
        "/transactions/<int:transaction_id>",
        methods=["PATCH"],
    )
    def update_transaction(transaction_id):
        return proxy_database(
            "patch",
            f"/transactions/{transaction_id}",
            json=request.get_json(silent=True),
        )

    @application.route(
        "/transactions/<int:transaction_id>",
        methods=["DELETE"],
    )
    def delete_transaction(transaction_id):
        return proxy_database("delete", f"/transactions/{transaction_id}")

    @application.route("/categories")
    def get_categories():
        return proxy_database("get", "/categories")

    @application.route("/categories", methods=["POST"])
    def create_category():
        return proxy_database(
            "post",
            "/categories",
            json=request.get_json(silent=True),
        )

    @application.route("/categories/<int:category_id>")
    def get_category(category_id):
        return proxy_database("get", f"/categories/{category_id}")

    @application.route(
        "/categories/<int:category_id>",
        methods=["PATCH"],
    )
    def update_category(category_id):
        return proxy_database(
            "patch",
            f"/categories/{category_id}",
            json=request.get_json(silent=True),
        )

    @application.route(
        "/categories/<int:category_id>",
        methods=["DELETE"],
    )
    def delete_category(category_id):
        return proxy_database("delete", f"/categories/{category_id}")

    @application.post("/chat")
    def chat():
        payload = json_object()
        unknown = sorted(set(payload) - {"message"})
        if unknown:
            raise ChatError(
                f"unsupported fields: {', '.join(unknown)}",
                "unsupported_fields",
                422,
            )
        return jsonify(orchestrate_transaction_request(
            payload.get("message"),
            db_url,
        ))

    @application.post("/chat/category")
    def select_chat_category():
        return jsonify(run_category_selection(json_object(), db_url))

    @application.post("/chat/apply")
    def apply_chat_preview():
        return jsonify(run_confirmed_transaction(json_object(), db_url))

    @application.get("/ui/chat")
    def get_chat_panel():
        return render_template("chat_panel.jinja")

    @application.post("/ui/chat")
    def post_ui_chat():
        try:
            if "clarification" in request.form:
                original_message = request.form.get("original_message", "").strip()
                clarification = request.form.get("clarification", "").strip()
                if not original_message:
                    raise ChatError(
                        "The original request is missing. Start a new request.",
                        "invalid_message",
                        422,
                    )
                if not clarification:
                    raise ChatError(
                        "Enter an answer before continuing.",
                        "invalid_message",
                        422,
                    )
                if request.form.get("clarification_kind") == "transaction_id":
                    clarification = normalize_transaction_id_answer(
                        clarification
                    )
                separator = (
                    ""
                    if original_message[-1] in ".!?"
                    else "."
                )
                message = (
                    f"{original_message}{separator}\n"
                    f"Additional details: {clarification}"
                )
            elif "adjustment" in request.form:
                adjustment = request.form.get("adjustment", "").strip()
                if not adjustment:
                    raise ChatError(
                        "Enter what you want to change before continuing.",
                        "invalid_message",
                        422,
                    )
                original_message = get_preview_request_context(
                    request.form.get("request_id")
                )
                message = (
                    f"{original_message}\n"
                    f"Requested change: {adjustment}"
                )
            else:
                message = request.form.get("message")
            result = orchestrate_transaction_request(
                message,
                db_url,
            )
            response = make_response(render_template(
                "chat_result.jinja",
                result=result,
                error=None,
                success=None,
                request_context=message,
            ))
            if result.get("agent", {}).get("status") == "complete":
                response.headers["HX-Trigger"] = "transaction-completed"
            return response
        except ChatError as error:
            return render_chat_error(error.message, error.status)
        except requests.RequestException as error:
            application.logger.warning(
                "Transactions database request failed: %s",
                error,
            )
            return render_chat_error(
                "The transactions service is unavailable.",
                503,
            )

    @application.post("/ui/chat/category")
    def select_ui_chat_category():
        try:
            result = run_category_selection(
                {
                    "request_id": request.form.get("request_id"),
                    "category_id": int(
                        request.form.get("category_id", "")
                    ),
                },
                db_url,
            )
            return render_template(
                "chat_result.jinja",
                result=result,
                error=None,
                success=None,
                request_context=request.form.get("request_context"),
            )
        except (TypeError, ValueError):
            return render_chat_error("Choose a valid category.", 422)
        except ChatError as error:
            return render_chat_error(error.message, error.status)

    @application.post("/ui/chat/apply")
    def apply_ui_chat_preview():
        try:
            preview = json.loads(request.form.get("preview", ""))
            payload = {"preview": preview}
            request_id = request.form.get("request_id")
            if request_id:
                payload["request_id"] = request_id
            result = run_confirmed_transaction(payload, db_url)
        except (json.JSONDecodeError, TypeError):
            return render_chat_error(
                "The confirmation preview is invalid.",
                400,
            )
        except ChatError as error:
            return render_chat_error(error.message, error.status)
        except requests.RequestException as error:
            application.logger.warning(
                "Transactions database request failed: %s",
                error,
            )
            return render_chat_error(
                "The transaction change could not be saved.",
                503,
            )

        response = make_response(render_template(
            "chat_result.jinja",
            result=result,
            error=None,
            success=result["reply"],
        ))
        triggers = ["transactionsChanged"]
        if (
            result.get("saved") is True
            and result.get("verified") is True
            and result.get("agent", {}).get("status") == "complete"
        ):
            triggers.append("transaction-completed")
        response.headers["HX-Trigger"] = ", ".join(triggers)
        return response

    @application.get("/ui/chat/clear")
    def clear_ui_chat():
        return ""

    def render_agent_review(run, status=200, error=None, message=None):
        return render_template(
            "agent_review.jinja",
            run=run,
            error=error,
            message=message,
            suggested_id=category_review.suggested_category_id(run),
        ), status

    @application.post("/ui/transactions/<int:transaction_id>/agent-review")
    def start_agent_review(transaction_id):
        if not config.MULTI_AGENT_ENABLED:
            return "<p role=\"alert\">Agent review is turned off.</p>", 404
        try:
            run = category_review.start_run(db_url, transaction_id)
        except ChatError as error:
            return (
                f"<p role=\"alert\">{error.message}</p>",
                error.status,
            )
        return render_agent_review(run)

    @application.get("/ui/agent-review/<run_id>")
    def get_agent_review(run_id):
        run = category_review.get_run(run_id)
        if run is None:
            return "<p role=\"alert\">This review has expired. Start a new one.</p>", 404
        return render_agent_review(run)

    @application.post("/ui/agent-review/<run_id>/decision")
    def decide_agent_review(run_id):
        run = category_review.get_run(run_id)
        if run is None:
            return "<p role=\"alert\">This review has expired. Start a new one.</p>", 404
        try:
            message = category_review.decide(
                run,
                db_url,
                request.form.get("decision"),
                request.form.get("category_id", type=int),
                request.form.get("reason"),
            )
        except ChatError as error:
            return render_agent_review(run, error.status, error=error.message)
        response = make_response(render_agent_review(run, message=message))
        if message.startswith("Category set to"):
            response.headers["HX-Trigger"] = "transactionsChanged"
        return response

    application.config["TRANSACTIONS_DB_URL"] = db_url
    return application


def start_startup_refresh(application, attempts=10, delay_seconds=3):
    if (
        application.testing
        or not config.RAG_ENABLED
        or not config.RAG_REFRESH_ON_START
    ):
        return None
    return rag_corpus.start_background_refresh(
        application.config["TRANSACTIONS_DB_URL"],
        "startup",
        attempts=attempts,
        delay_seconds=delay_seconds,
    )


app = setup_app(config.TRANSACTIONS_DB_URL)
