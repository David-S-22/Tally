"""Thin CRUD JSON API over the bills SQLite database. No business logic lives here."""
import hmac
import json
import os
from datetime import datetime, timezone

from flask import Flask, g, jsonify, request

from db import get_connection, load_schema, row_to_dict, rows_to_list
from seed import seed

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

CADENCES = {"weekly", "fortnightly", "monthly"}
BILL_TYPES = {"bill", "subscription"}
BILL_STATUSES = {"paid", "due", "overdue"}
BILL_SOURCES = {"manual", "f3_handoff", "f4_handoff", "chat"}
DISPUTE_STATUSES = {"draft", "sent", "resolved"}
CHAT_ROLES = {"user", "assistant"}

BILL_FIELDS = [
    "name", "merchant", "amount_cents", "cadence", "next_billing_date", "type",
    "payment_method", "status", "end_date", "source", "confirmed_at", "created_at",
    "exclude_from_plan",
]
PAYMENT_FIELDS = ["bill_id", "date", "amount_cents"]
DISPUTE_FIELDS = ["bill_id", "reason", "status", "opened_at"]


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _now_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_timestamp():
    return datetime.now(timezone.utc).isoformat()


def _require(data, fields):
    if not isinstance(data, dict):
        raise ApiError("request body must be a JSON object")
    missing = [f for f in fields if data.get(f) in (None, "")]
    if missing:
        raise ApiError(f"missing required fields: {', '.join(missing)}")


def _validate_bill(data, partial):
    if not partial:
        _require(data, ["name", "merchant", "amount_cents", "cadence", "next_billing_date", "type"])
    if "cadence" in data and data["cadence"] not in CADENCES:
        raise ApiError(f"cadence must be one of {sorted(CADENCES)}")
    if "type" in data and data["type"] not in BILL_TYPES:
        raise ApiError(f"type must be one of {sorted(BILL_TYPES)}")
    if "status" in data and data["status"] not in BILL_STATUSES:
        raise ApiError(f"status must be one of {sorted(BILL_STATUSES)}")
    if "source" in data and data["source"] not in BILL_SOURCES:
        raise ApiError(f"source must be one of {sorted(BILL_SOURCES)}")
    if "amount_cents" in data and not isinstance(data["amount_cents"], int):
        raise ApiError("amount_cents must be an integer")
    if "exclude_from_plan" in data and data["exclude_from_plan"] not in (0, 1):
        raise ApiError("exclude_from_plan must be 0 or 1")


def _validate_payment(data, partial):
    if not partial:
        _require(data, PAYMENT_FIELDS)
    if "amount_cents" in data and not isinstance(data["amount_cents"], int):
        raise ApiError("amount_cents must be an integer")


def _validate_dispute(data, partial):
    if not partial:
        _require(data, ["bill_id", "reason"])
    if "status" in data and data["status"] not in DISPUTE_STATUSES:
        raise ApiError(f"status must be one of {sorted(DISPUTE_STATUSES)}")


def create_app(db_path=None, token=None):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("DB_PATH", "./bills.db")

    # Release 2 (B5, cloud endpoint security). bills-db is deployed with external
    # ingress -- the MCP VM sits outside the Container Apps environment and calls it
    # over HTTPS -- so without this the whole bills database is on the public
    # internet with no authentication at all.
    #
    # The token is only enforced once one is configured, which is what keeps
    # docker compose and the test suite working unchanged: there the service is
    # reachable only on the compose network and BILLS_DB_TOKEN is unset. The cloud
    # template always sets it. This mirrors the Multi-Agent Server's rule in
    # ai-services/multi-agent-server/settings.py, including the 32-character floor.
    #
    # flask_cors used to wrap this app with a wildcard policy. Nothing in a browser
    # ever calls bills-db -- only bills-backend does, server to server -- so the
    # right CORS policy here is no CORS headers at all.
    api_token = (os.environ.get("BILLS_DB_TOKEN", "") if token is None else token).strip()
    if api_token and len(api_token) < 32:
        raise ValueError("BILLS_DB_TOKEN must be at least 32 characters")
    app.config["API_TOKEN"] = api_token

    @app.before_request
    def require_token():
        """401 on every route but /health once a token is configured.

        /health stays open so the container platform's probes and `deploy.sh`
        can see the service is up without holding a credential.
        """
        if not api_token or request.path == "/health":
            return None
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {api_token}".encode()):
            return jsonify({"error": "valid bearer token required"}), 401
        return None

    @app.after_request
    def security_headers(response):
        """Same two headers the Multi-Agent Server sets: this API answers with
        financial records, so nothing it returns should be sniffed or cached."""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def db():
        if "db" not in g:
            g.db = get_connection(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exception=None):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.errorhandler(ApiError)
    def handle_api_error(error):
        return jsonify({"error": error.message}), error.status

    @app.get("/health")
    def health():
        """Open, so the platform's probes and deploy.sh need no credential.

        It does report whether the caller presented a valid token. Without that,
        a backend holding the wrong token sees this route answer 200 and reports
        the database as up while every real call it makes is refused -- a green
        health check in front of a service that cannot read a single bill.
        """
        supplied = request.headers.get("Authorization", "")
        authenticated = bool(api_token) and hmac.compare_digest(
            supplied.encode(), f"Bearer {api_token}".encode()
        )
        return jsonify({
            "ok": True,
            "auth_required": bool(api_token),
            "authenticated": authenticated,
        })

    @app.get("/bills")
    def list_bills():
        rows = db().execute("SELECT * FROM bills ORDER BY id").fetchall()
        return jsonify(rows_to_list(rows))

    @app.post("/bills")
    def create_bill():
        data = request.get_json(silent=True) or {}
        _validate_bill(data, partial=False)
        payload = {field: data.get(field) for field in BILL_FIELDS}
        payload["status"] = payload["status"] or "due"
        payload["source"] = payload["source"] or "manual"
        payload["created_at"] = payload["created_at"] or _now_date()
        payload["exclude_from_plan"] = payload["exclude_from_plan"] or 0
        connection = db()
        cursor = connection.execute(
            """
            INSERT INTO bills
                (name, merchant, amount_cents, cadence, next_billing_date, type,
                 payment_method, status, end_date, source, confirmed_at, created_at, exclude_from_plan)
            VALUES (:name, :merchant, :amount_cents, :cadence, :next_billing_date, :type,
                    :payment_method, :status, :end_date, :source, :confirmed_at, :created_at, :exclude_from_plan)
            """,
            payload,
        )
        connection.commit()
        row = connection.execute("SELECT * FROM bills WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify(row_to_dict(row)), 201

    @app.get("/bills/<int:bill_id>")
    def get_bill(bill_id):
        row = db().execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if row is None:
            raise ApiError("bill not found", 404)
        return jsonify(row_to_dict(row))

    @app.put("/bills/<int:bill_id>")
    def update_bill(bill_id):
        data = request.get_json(silent=True) or {}
        _validate_bill(data, partial=True)
        updates = {field: data[field] for field in BILL_FIELDS if field in data}
        if not updates:
            raise ApiError("no updatable fields supplied")
        connection = db()
        existing = connection.execute("SELECT id FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if existing is None:
            raise ApiError("bill not found", 404)
        set_clause = ", ".join(f"{field} = :{field}" for field in updates)
        updates["id"] = bill_id
        connection.execute(f"UPDATE bills SET {set_clause} WHERE id = :id", updates)
        connection.commit()
        row = connection.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
        return jsonify(row_to_dict(row))

    @app.delete("/bills/<int:bill_id>")
    def delete_bill(bill_id):
        connection = db()
        existing = connection.execute("SELECT id FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if existing is None:
            raise ApiError("bill not found", 404)
        connection.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
        connection.commit()
        return jsonify({"deleted": bill_id})

    @app.get("/bills/<int:bill_id>/payments")
    def list_bill_payments(bill_id):
        rows = db().execute(
            "SELECT * FROM payments WHERE bill_id = ? ORDER BY date", (bill_id,)
        ).fetchall()
        return jsonify(rows_to_list(rows))

    @app.get("/payments")
    def list_payments():
        rows = db().execute("SELECT * FROM payments ORDER BY id").fetchall()
        return jsonify(rows_to_list(rows))

    @app.post("/payments")
    def create_payment():
        data = request.get_json(silent=True) or {}
        _validate_payment(data, partial=False)
        connection = db()
        bill = connection.execute("SELECT id FROM bills WHERE id = ?", (data["bill_id"],)).fetchone()
        if bill is None:
            raise ApiError("bill not found", 404)
        cursor = connection.execute(
            "INSERT INTO payments (bill_id, date, amount_cents) VALUES (?, ?, ?)",
            (data["bill_id"], data["date"], data["amount_cents"]),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM payments WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify(row_to_dict(row)), 201

    @app.get("/payments/<int:payment_id>")
    def get_payment(payment_id):
        row = db().execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if row is None:
            raise ApiError("payment not found", 404)
        return jsonify(row_to_dict(row))

    @app.put("/payments/<int:payment_id>")
    def update_payment(payment_id):
        data = request.get_json(silent=True) or {}
        _validate_payment(data, partial=True)
        updates = {field: data[field] for field in PAYMENT_FIELDS if field in data}
        if not updates:
            raise ApiError("no updatable fields supplied")
        connection = db()
        existing = connection.execute("SELECT id FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if existing is None:
            raise ApiError("payment not found", 404)
        set_clause = ", ".join(f"{field} = :{field}" for field in updates)
        updates["id"] = payment_id
        connection.execute(f"UPDATE payments SET {set_clause} WHERE id = :id", updates)
        connection.commit()
        row = connection.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        return jsonify(row_to_dict(row))

    @app.delete("/payments/<int:payment_id>")
    def delete_payment(payment_id):
        connection = db()
        existing = connection.execute("SELECT id FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if existing is None:
            raise ApiError("payment not found", 404)
        connection.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        connection.commit()
        return jsonify({"deleted": payment_id})

    @app.get("/disputes")
    def list_disputes():
        rows = db().execute("SELECT * FROM disputes ORDER BY id").fetchall()
        return jsonify(rows_to_list(rows))

    @app.post("/disputes")
    def create_dispute():
        data = request.get_json(silent=True) or {}
        _validate_dispute(data, partial=False)
        connection = db()
        bill = connection.execute("SELECT id FROM bills WHERE id = ?", (data["bill_id"],)).fetchone()
        if bill is None:
            raise ApiError("bill not found", 404)
        status = data.get("status") or "draft"
        opened_at = data.get("opened_at") or _now_date()
        cursor = connection.execute(
            "INSERT INTO disputes (bill_id, reason, status, opened_at) VALUES (?, ?, ?, ?)",
            (data["bill_id"], data["reason"], status, opened_at),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM disputes WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify(row_to_dict(row)), 201

    @app.get("/disputes/<int:dispute_id>")
    def get_dispute(dispute_id):
        row = db().execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
        if row is None:
            raise ApiError("dispute not found", 404)
        return jsonify(row_to_dict(row))

    @app.put("/disputes/<int:dispute_id>")
    def update_dispute(dispute_id):
        data = request.get_json(silent=True) or {}
        _validate_dispute(data, partial=True)
        updates = {field: data[field] for field in DISPUTE_FIELDS if field in data}
        if not updates:
            raise ApiError("no updatable fields supplied")
        connection = db()
        existing = connection.execute("SELECT id FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
        if existing is None:
            raise ApiError("dispute not found", 404)
        set_clause = ", ".join(f"{field} = :{field}" for field in updates)
        updates["id"] = dispute_id
        connection.execute(f"UPDATE disputes SET {set_clause} WHERE id = :id", updates)
        connection.commit()
        row = connection.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
        return jsonify(row_to_dict(row))

    @app.delete("/disputes/<int:dispute_id>")
    def delete_dispute(dispute_id):
        connection = db()
        existing = connection.execute("SELECT id FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
        if existing is None:
            raise ApiError("dispute not found", 404)
        connection.execute("DELETE FROM disputes WHERE id = ?", (dispute_id,))
        connection.commit()
        return jsonify({"deleted": dispute_id})

    @app.get("/disputes/<int:dispute_id>/drafts")
    def list_dispute_drafts(dispute_id):
        rows = db().execute(
            "SELECT * FROM dispute_drafts WHERE dispute_id = ? ORDER BY version", (dispute_id,)
        ).fetchall()
        return jsonify(rows_to_list(rows))

    @app.post("/disputes/<int:dispute_id>/drafts")
    def create_dispute_draft(dispute_id):
        data = request.get_json(silent=True) or {}
        _require(data, ["letter_text", "steps_json"])
        connection = db()
        dispute = connection.execute("SELECT id FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
        if dispute is None:
            raise ApiError("dispute not found", 404)
        current_max = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM dispute_drafts WHERE dispute_id = ?", (dispute_id,)
        ).fetchone()[0]
        steps_json = data["steps_json"]
        if not isinstance(steps_json, str):
            steps_json = json.dumps(steps_json)
        cursor = connection.execute(
            """
            INSERT INTO dispute_drafts (dispute_id, version, letter_text, steps_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (dispute_id, current_max + 1, data["letter_text"], steps_json, _now_date()),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM dispute_drafts WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify(row_to_dict(row)), 201

    # --- suggestions: AI-proposed changes awaiting approval -----------------
    #
    # The status column is a tiny state machine and the PUT below is its only
    # gate: pending -> applied | rejected | failed, plus applied -> failed
    # (an approve claims the row *before* executing, so a failed execution
    # has to be able to record itself) and failed -> rejected (dismissing a
    # failed card). The transition runs as a guarded UPDATE, so two racing
    # approves cannot both claim one suggestion.
    SUGGESTION_TRANSITIONS = {
        "pending": {"applied", "rejected", "failed"},
        "applied": {"failed"},
        "failed": {"rejected"},
    }

    @app.get("/suggestions")
    def list_suggestions():
        status = request.args.get("status")
        if status:
            rows = db().execute(
                "SELECT * FROM suggestions WHERE status = ? ORDER BY id", (status,)
            ).fetchall()
        else:
            rows = db().execute("SELECT * FROM suggestions ORDER BY id").fetchall()
        return jsonify(rows_to_list(rows))

    @app.post("/suggestions")
    def create_suggestion():
        data = request.get_json(silent=True) or {}
        _require(data, ["op", "entity"])
        if data["op"] not in ("create", "update", "delete"):
            raise ApiError("op must be one of ['create', 'delete', 'update']")
        if data["entity"] not in ("bill", "payment", "dispute"):
            raise ApiError("entity must be one of ['bill', 'dispute', 'payment']")
        payload_json = data.get("payload_json")
        if payload_json is not None and not isinstance(payload_json, str):
            payload_json = json.dumps(payload_json)
        connection = db()
        cursor = connection.execute(
            """
            INSERT INTO suggestions (op, entity, entity_id, payload_json, status, message_id, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (data["op"], data["entity"], data.get("entity_id"), payload_json,
             data.get("message_id"), _now_timestamp()),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM suggestions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify(row_to_dict(row)), 201

    @app.get("/suggestions/<int:suggestion_id>")
    def get_suggestion(suggestion_id):
        row = db().execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        if row is None:
            raise ApiError("suggestion not found", 404)
        return jsonify(row_to_dict(row))

    @app.put("/suggestions/<int:suggestion_id>")
    def update_suggestion(suggestion_id):
        data = request.get_json(silent=True) or {}
        new_status = data.get("status")
        if new_status not in ("applied", "rejected", "failed"):
            raise ApiError("status must be one of ['applied', 'failed', 'rejected']")
        connection = db()
        existing = connection.execute(
            "SELECT status FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone()
        if existing is None:
            raise ApiError("suggestion not found", 404)
        allowed_from = [
            current for current, targets in SUGGESTION_TRANSITIONS.items() if new_status in targets
        ]
        placeholders = ", ".join("?" for _ in allowed_from)
        cursor = connection.execute(
            f"UPDATE suggestions SET status = ?, error = ?, resolved_at = ? "
            f"WHERE id = ? AND status IN ({placeholders})",
            (new_status, data.get("error"), _now_timestamp(), suggestion_id, *allowed_from),
        )
        connection.commit()
        if cursor.rowcount == 0:
            raise ApiError(f"suggestion is already {existing['status']}", 409)
        row = connection.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
        return jsonify(row_to_dict(row))

    @app.get("/chat_messages")
    def list_chat_messages():
        rows = db().execute("SELECT * FROM chat_messages ORDER BY id").fetchall()
        return jsonify(rows_to_list(rows))

    @app.post("/chat_messages")
    def create_chat_message():
        data = request.get_json(silent=True) or {}
        _require(data, ["role", "content"])
        if data["role"] not in CHAT_ROLES:
            raise ApiError(f"role must be one of {sorted(CHAT_ROLES)}")
        op_json = data.get("op_json")
        if op_json is not None and not isinstance(op_json, str):
            op_json = json.dumps(op_json)
        connection = db()
        cursor = connection.execute(
            """
            INSERT INTO chat_messages (role, content, op_json, applied, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (data["role"], data["content"], op_json, int(bool(data.get("applied", False))), _now_timestamp()),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM chat_messages WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return jsonify(row_to_dict(row)), 201

    @app.put("/chat_messages/<int:message_id>")
    def update_chat_message(message_id):
        data = request.get_json(silent=True) or {}
        connection = db()
        existing = connection.execute("SELECT id FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
        if existing is None:
            raise ApiError("chat message not found", 404)
        if "applied" not in data:
            raise ApiError("no updatable fields supplied")
        connection.execute(
            "UPDATE chat_messages SET applied = ? WHERE id = ?",
            (int(bool(data["applied"])), message_id),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
        return jsonify(row_to_dict(row))

    @app.delete("/chat_messages")
    def delete_chat_messages():
        connection = db()
        connection.execute("DELETE FROM chat_messages")
        connection.commit()
        return jsonify({"deleted": "all"})

    return app


if __name__ == "__main__":
    db_path = os.environ.get("DB_PATH", "./bills.db")
    init_connection = get_connection(db_path)
    load_schema(init_connection, SCHEMA_PATH)
    seed(init_connection)
    init_connection.close()
    port = int(os.environ.get("PORT", "6005"))
    application = create_app(db_path)
    application.run(host="0.0.0.0", port=port)
