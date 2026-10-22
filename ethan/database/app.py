from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.exceptions import BadRequest, HTTPException

from .models import Budget, BudgetLine, CoachProposal, PlannedEvent, db
from .models import ChatMessage
from .seed import seed_database_if_empty


BUDGET_STATUSES = {"draft", "active", "closed"}
PLANNED_EVENT_SOURCES = {"user", "predicted"}
PLANNED_EVENT_STATUSES = {"planned", "confirmed", "cancelled"}
COACH_PROPOSAL_STATUSES = {"proposed", "accepted", "rejected"}
CHAT_MESSAGE_ROLES = {"user", "assistant"}
CHAT_MESSAGE_MODES = {"advice", "clarify", "proposal"}
CHAT_RESPONSE_SOURCES = {"deterministic", "ollama", "mcp", "rag"}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@event.listens_for(Engine, "connect")
def configure_sqlite_connection(database_connection, _connection_record):
    if not isinstance(database_connection, sqlite3.Connection):
        return
    database_connection.execute("PRAGMA foreign_keys = ON")


def _database_uri(db_path: str) -> str:
    if db_path == ":memory:":
        return "sqlite:///:memory:"
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def _now_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_budget_month() -> str:
    return datetime.now().strftime("%Y-%m")


def _derived_budget_status(month: str | None) -> str | None:
    if month is None:
        return None
    current_month = _current_budget_month()
    if month < current_month:
        return "closed"
    if month > current_month:
        return "draft"
    return "active"


def _apply_derived_budget_status(budget: Budget) -> bool:
    derived = _derived_budget_status(budget.month)
    if budget.status == derived:
        return False
    budget.status = derived
    budget.updated_at = _now_timestamp()
    return True


def _sync_budget_statuses() -> bool:
    changed = False
    budgets = db.session.scalars(select(Budget)).all()
    for budget in budgets:
        changed = _apply_derived_budget_status(budget) or changed
    return changed


def _budget_to_dict(budget: Budget) -> dict[str, object | None]:
    return budget.to_dict()


def _json_body() -> dict:
    if not request.is_json:
        raise ApiError("request body must be a JSON object", 400, "invalid_json")
    try:
        payload = request.get_json()
    except (BadRequest, RecursionError) as error:
        raise ApiError("request body contains invalid JSON", 400, "invalid_json") from error
    if not isinstance(payload, dict):
        raise ApiError("request body must be a JSON object", 400, "invalid_json")
    return payload


def _optional_int(data: dict, field: str, values: dict):
    if field not in data:
        return
    value = data[field]
    if value is None:
        values[field] = None
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(f"{field} must be an integer", 422, "invalid_field")
    values[field] = value


def _optional_string(data: dict, field: str, values: dict):
    if field not in data:
        return
    value = data[field]
    if value is None:
        values[field] = None
        return
    if not isinstance(value, str):
        raise ApiError(f"{field} must be a string", 422, "invalid_field")
    values[field] = value.strip() or None


def _optional_enum(data: dict, field: str, allowed: set[str], values: dict):
    _optional_string(data, field, values)
    value = values.get(field)
    if value is not None and value not in allowed:
        raise ApiError(
            f"{field} must be one of {sorted(allowed)}",
            422,
            "invalid_field",
        )


def _optional_json_object(data: dict, field: str, values: dict):
    if field not in data:
        return
    value = data[field]
    if value is None:
        values[field] = None
        return
    if not isinstance(value, dict):
        raise ApiError(f"{field} must be a JSON object", 422, "invalid_field")
    values[field] = value


def _optional_string_list(data: dict, field: str, values: dict):
    if field not in data:
        return
    value = data[field]
    if value is None:
        values[field] = None
        return
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ApiError(f"{field} must be a list of strings", 422, "invalid_field")
    values[field] = [item.strip() for item in value]


def _validate_month(value: str | None):
    if value is None:
        return
    if len(value) != 7 or value[4] != "-" or not value[:4].isdigit() or not value[5:].isdigit():
        raise ApiError("month must use YYYY-MM format", 422, "invalid_field")
    month_number = int(value[5:])
    if month_number < 1 or month_number > 12:
        raise ApiError("month must use YYYY-MM format", 422, "invalid_field")


def _validate_date(value: str | None, field: str):
    if value is None:
        return
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ApiError(f"{field} must use YYYY-MM-DD format", 422, "invalid_field")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ApiError(f"{field} must use YYYY-MM-DD format", 422, "invalid_field") from error


def _month_from_date(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:7]


def _validate_id(identifier: str, field: str = "id") -> int:
    try:
        value = int(identifier)
    except (ValueError, TypeError) as error:
        raise ApiError(f"{field} must be an integer", 422, "invalid_field") from error
    if value < 1:
        raise ApiError(f"{field} must be an integer", 422, "invalid_field")
    return value


def _require_fields(data: dict, fields: list[str]):
    missing = [field for field in fields if data.get(field) in (None, "")]
    if missing:
        raise ApiError(
            f"missing required fields: {', '.join(missing)}",
            400,
            "missing_required_fields",
        )


def _validate_budget_payload(data: dict, partial: bool = False) -> dict:
    if not partial:
        _require_fields(data, ["month"])
    values: dict = {}
    _optional_string(data, "month", values)
    _optional_int(data, "declared_income", values)
    _optional_enum(data, "status", BUDGET_STATUSES, values)
    _validate_month(values.get("month"))
    return values


def _validate_budget_line_payload(data: dict, partial: bool = False) -> dict:
    if not partial:
        _require_fields(data, ["category_id", "category"])
    values: dict = {}
    _optional_int(data, "category_id", values)
    _optional_string(data, "category", values)
    _optional_int(data, "warn_at", values)
    _optional_int(data, "hard_cap", values)
    if ("category_id" in values) != ("category" in values):
        raise ApiError(
            "category_id and category must be supplied together",
            422,
            "invalid_field",
        )
    warn_at = values.get("warn_at")
    hard_cap = values.get("hard_cap")
    if warn_at is not None and hard_cap is not None and warn_at > hard_cap:
        raise ApiError("warn_at must be less than or equal to hard_cap", 422, "invalid_field")
    return values


def _validate_planned_event_payload(data: dict, partial: bool = False) -> dict:
    if not partial:
        _require_fields(data, ["category"])
    values: dict = {}
    _optional_string(data, "date", values)
    _optional_string(data, "label", values)
    _optional_string(data, "category", values)
    _optional_int(data, "est_low", values)
    _optional_int(data, "est_high", values)
    _optional_enum(data, "source", PLANNED_EVENT_SOURCES, values)
    _optional_enum(data, "status", PLANNED_EVENT_STATUSES, values)
    _validate_date(values.get("date"), "date")
    est_low = values.get("est_low")
    est_high = values.get("est_high")
    if est_low is not None and est_high is not None and est_low > est_high:
        raise ApiError("est_low must be less than or equal to est_high", 422, "invalid_field")
    return values


def _validate_coach_proposal_payload(data: dict, partial: bool = False) -> dict:
    if not partial:
        _require_fields(data, ["proposal_json"])
    values: dict = {}
    _optional_json_object(data, "proposal_json", values)
    _optional_string(data, "rationale", values)
    _optional_enum(data, "status", COACH_PROPOSAL_STATUSES, values)
    _optional_string(data, "rejection_reason", values)
    _optional_string(data, "decided_at", values)
    return values


def _validate_chat_message_payload(data: dict) -> dict:
    _require_fields(data, ["role", "content"])
    values: dict = {}
    _optional_enum(data, "role", CHAT_MESSAGE_ROLES, values)
    _optional_string(data, "content", values)
    _optional_enum(data, "mode", CHAT_MESSAGE_MODES, values)
    _optional_enum(data, "response_source", CHAT_RESPONSE_SOURCES, values)
    _optional_json_object(data, "plan_json", values)
    _optional_json_object(data, "observation_json", values)
    _optional_string_list(data, "stage_trace", values)
    _optional_json_object(data, "tool_result_json", values)
    _optional_json_object(data, "grounding_json", values)
    _optional_int(data, "proposal_id", values)
    if values.get("role") is None:
        raise ApiError("role must be user or assistant", 422, "invalid_field")
    if values.get("content") is None:
        raise ApiError("content must not be empty", 422, "invalid_field")
    return values


def _ensure_chat_message_columns():
    rows = db.session.execute(text("PRAGMA table_info(chat_messages)")).fetchall()
    existing = {str(row[1]) for row in rows}
    missing_statements = []
    if "tool_result_json" not in existing:
        missing_statements.append("ALTER TABLE chat_messages ADD COLUMN tool_result_json JSON")
    if "grounding_json" not in existing:
        missing_statements.append("ALTER TABLE chat_messages ADD COLUMN grounding_json JSON")
    for statement in missing_statements:
        db.session.execute(text(statement))
    if missing_statements:
        db.session.commit()


def _chat_messages_sql() -> str:
    row = db.session.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='chat_messages'")
    ).fetchone()
    if row is None or not row[0]:
        return ""
    return str(row[0])


def _migrate_chat_messages_response_source_constraint():
    sql = _chat_messages_sql()
    if not sql or "'mcp'" in sql and "'rag'" in sql:
        return
    if "response_source IN ('deterministic', 'ollama')" not in sql:
        return

    db.session.execute(text(
        """
        CREATE TABLE chat_messages_new (
            id INTEGER NOT NULL PRIMARY KEY,
            budget_id INTEGER NOT NULL,
            proposal_id INTEGER,
            role VARCHAR(16) NOT NULL,
            content TEXT NOT NULL,
            mode VARCHAR(16),
            response_source VARCHAR(16),
            plan_json JSON,
            observation_json JSON,
            stage_trace JSON,
            created_at VARCHAR(40) NOT NULL,
            tool_result_json JSON,
            grounding_json JSON,
            CONSTRAINT ck_chat_messages_role CHECK (role IN ('user', 'assistant')),
            CONSTRAINT ck_chat_messages_mode CHECK (mode IS NULL OR mode IN ('advice', 'clarify', 'proposal')),
            CONSTRAINT ck_chat_messages_response_source CHECK (response_source IS NULL OR response_source IN ('deterministic', 'ollama', 'mcp', 'rag')),
            FOREIGN KEY(budget_id) REFERENCES budgets (id) ON DELETE CASCADE,
            FOREIGN KEY(proposal_id) REFERENCES coach_proposals (id) ON DELETE SET NULL
        )
        """
    ))
    db.session.execute(text(
        """
        INSERT INTO chat_messages_new (
            id, budget_id, proposal_id, role, content, mode, response_source,
            plan_json, observation_json, stage_trace, created_at, tool_result_json, grounding_json
        )
        SELECT
            id, budget_id, proposal_id, role, content, mode, response_source,
            plan_json, observation_json, stage_trace, created_at, tool_result_json, grounding_json
        FROM chat_messages
        """
    ))
    db.session.execute(text("DROP TABLE chat_messages"))
    db.session.execute(text("ALTER TABLE chat_messages_new RENAME TO chat_messages"))
    db.session.commit()


def _get_budget_or_404(budget_id: str) -> Budget:
    budget = db.session.get(Budget, _validate_id(budget_id, "budget_id"))
    if budget is None:
        raise ApiError("budget not found", 404, "budget_not_found")
    return budget


def _get_budget_line_or_404(line_id: str) -> BudgetLine:
    line = db.session.get(BudgetLine, _validate_id(line_id, "budget_line_id"))
    if line is None:
        raise ApiError("budget line not found", 404, "budget_line_not_found")
    return line


def _get_planned_event_or_404(event_id: str) -> PlannedEvent:
    planned_event = db.session.get(PlannedEvent, _validate_id(event_id, "planned_event_id"))
    if planned_event is None:
        raise ApiError("planned event not found", 404, "planned_event_not_found")
    return planned_event


def _get_coach_proposal_or_404(proposal_id: str) -> CoachProposal:
    proposal = db.session.get(CoachProposal, _validate_id(proposal_id, "coach_proposal_id"))
    if proposal is None:
        raise ApiError("coach proposal not found", 404, "coach_proposal_not_found")
    return proposal


def _get_chat_message_budget_or_404(budget_id: str) -> Budget:
    return _get_budget_or_404(budget_id)


def _budget_has_category_id(budget_id: int, category_id: int | None, excluded_line_id: int | None = None) -> bool:
    if category_id is None:
        return False
    statement = select(func.count()).select_from(BudgetLine).where(
        BudgetLine.budget_id == budget_id,
        BudgetLine.category_id == category_id,
    )
    if excluded_line_id is not None:
        statement = statement.where(BudgetLine.id != excluded_line_id)
    return db.session.scalar(statement) > 0


def _budget_has_category_name(budget_id: int, category: str | None, excluded_line_id: int | None = None) -> bool:
    if category is None:
        return False
    statement = select(func.count()).select_from(BudgetLine).where(
        BudgetLine.budget_id == budget_id,
        func.lower(BudgetLine.category) == category.casefold(),
    )
    if excluded_line_id is not None:
        statement = statement.where(BudgetLine.id != excluded_line_id)
    return db.session.scalar(statement) > 0


def _require_existing_budget_line_category(budget_id: int, category: str | None):
    if category is None:
        return
    if not _budget_has_category_name(budget_id, category):
        raise ApiError(
            "category must match an existing budget line for this budget",
            422,
            "budget_line_category_required",
        )


def _planned_events_for_budget_line(line: BudgetLine) -> list[PlannedEvent]:
    if not isinstance(line.category, str) or not line.category.strip():
        return []
    return db.session.scalars(
        select(PlannedEvent).where(
            PlannedEvent.budget_id == line.budget_id,
            func.lower(PlannedEvent.category) == line.category.casefold(),
        )
    ).all()


def _require_date_within_budget_month(budget: Budget, date_value: str | None):
    if date_value is None:
        return
    budget_month = budget.month
    if budget_month is None:
        return
    if _month_from_date(date_value) != budget_month:
        raise ApiError(
            "date must be inside the parent budget month",
            422,
            "planned_event_month_mismatch",
        )


def _create_app(db_path: str, seed_demo_data: bool = True) -> Flask:
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = _database_uri(db_path)
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)

    with application.app_context():
        db.create_all()
        _ensure_chat_message_columns()
        _migrate_chat_messages_response_source_constraint()
        if seed_demo_data:
            seed_database_if_empty()
        if _sync_budget_statuses():
            db.session.commit()

    @application.errorhandler(ApiError)
    def handle_api_error(error: ApiError):
        db.session.rollback()
        return jsonify(error=error.message, code=error.code), error.status

    @application.errorhandler(IntegrityError)
    def handle_integrity_error(_error: IntegrityError):
        db.session.rollback()
        return jsonify(error="database constraint conflict", code="database_conflict"), 409

    @application.errorhandler(SQLAlchemyError)
    def handle_database_error(error: SQLAlchemyError):
        db.session.rollback()
        application.logger.exception("Budgets database operation failed")
        return jsonify(error="database unavailable", code="database_unavailable"), 503

    @application.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        return jsonify(error=error.description, code=error.name.lower().replace(" ", "_")), error.code

    @application.get("/")
    def get_index():
        return jsonify(container="budgets-db")

    @application.get("/health")
    def get_health():
        return jsonify(ok=True)

    @application.get("/budgets")
    def list_budgets():
        if _sync_budget_statuses():
            db.session.commit()
        budgets = db.session.scalars(select(Budget).order_by(Budget.month, Budget.id)).all()
        return jsonify([_budget_to_dict(budget) for budget in budgets])

    @application.post("/budgets")
    def create_budget():
        values = _validate_budget_payload(_json_body())
        now = _now_timestamp()
        budget = Budget(
            month=values.get("month"),
            declared_income=values.get("declared_income"),
            status=values.get("status"),
            created_at=now,
            updated_at=now,
        )
        db.session.add(budget)
        db.session.commit()
        if _sync_budget_statuses():
            db.session.commit()
        return jsonify(_budget_to_dict(budget)), 201

    @application.get("/budgets/by-month/<month>")
    def get_budget_by_month(month: str):
        _validate_month(month)
        if _sync_budget_statuses():
            db.session.commit()
        budget = db.session.scalar(select(Budget).where(Budget.month == month))
        if budget is None:
            raise ApiError("budget not found", 404, "budget_not_found")
        return jsonify(_budget_to_dict(budget))

    @application.get("/budgets/<budget_id>")
    def get_budget(budget_id: str):
        if _sync_budget_statuses():
            db.session.commit()
        return jsonify(_budget_to_dict(_get_budget_or_404(budget_id)))

    @application.patch("/budgets/<budget_id>")
    def patch_budget(budget_id: str):
        budget = _get_budget_or_404(budget_id)
        values = _validate_budget_payload(_json_body(), partial=True)
        if not values:
            raise ApiError("no updatable fields supplied", 400, "missing_required_fields")
        for field, value in values.items():
            setattr(budget, field, value)
        budget.updated_at = _now_timestamp()
        db.session.commit()
        if _sync_budget_statuses():
            db.session.commit()
        return jsonify(_budget_to_dict(budget))

    @application.delete("/budgets/<budget_id>")
    def delete_budget(budget_id: str):
        budget = _get_budget_or_404(budget_id)
        db.session.delete(budget)
        db.session.commit()
        return "", 204

    @application.get("/budgets/<budget_id>/budget-lines")
    def list_budget_lines(budget_id: str):
        budget = _get_budget_or_404(budget_id)
        lines = db.session.scalars(
            select(BudgetLine).where(BudgetLine.budget_id == budget.id).order_by(BudgetLine.category, BudgetLine.id)
        ).all()
        return jsonify([line.to_dict() for line in lines])

    @application.post("/budgets/<budget_id>/budget-lines")
    def create_budget_line(budget_id: str):
        budget = _get_budget_or_404(budget_id)
        values = _validate_budget_line_payload(_json_body())
        if _budget_has_category_id(budget.id, values.get("category_id")):
            raise ApiError(
                "category already exists for this budget",
                409,
                "budget_line_category_conflict",
            )
        now = _now_timestamp()
        line = BudgetLine(
            budget_id=budget.id,
            category_id=values.get("category_id"),
            category=values.get("category"),
            warn_at=values.get("warn_at"),
            hard_cap=values.get("hard_cap"),
            created_at=now,
            updated_at=now,
        )
        db.session.add(line)
        db.session.commit()
        return jsonify(line.to_dict()), 201

    @application.get("/budget-lines/<line_id>")
    def get_budget_line(line_id: str):
        return jsonify(_get_budget_line_or_404(line_id).to_dict())

    @application.patch("/budget-lines/<line_id>")
    def patch_budget_line(line_id: str):
        line = _get_budget_line_or_404(line_id)
        values = _validate_budget_line_payload(_json_body(), partial=True)
        if not values:
            raise ApiError("no updatable fields supplied", 400, "missing_required_fields")
        category_id = values.get("category_id", line.category_id)
        if category_id is not None and _budget_has_category_id(line.budget_id, category_id, excluded_line_id=line.id):
            raise ApiError(
                "category already exists for this budget",
                409,
                "budget_line_category_conflict",
            )
        for field, value in values.items():
            setattr(line, field, value)
        line.updated_at = _now_timestamp()
        db.session.commit()
        return jsonify(line.to_dict())

    @application.delete("/budget-lines/<line_id>")
    def delete_budget_line(line_id: str):
        line = _get_budget_line_or_404(line_id)
        for planned_event in _planned_events_for_budget_line(line):
            db.session.delete(planned_event)
        db.session.delete(line)
        db.session.commit()
        return "", 204

    @application.get("/budgets/<budget_id>/planned-events")
    def list_planned_events(budget_id: str):
        budget = _get_budget_or_404(budget_id)
        planned_events = db.session.scalars(
            select(PlannedEvent).where(PlannedEvent.budget_id == budget.id).order_by(PlannedEvent.date, PlannedEvent.id)
        ).all()
        return jsonify([planned_event.to_dict() for planned_event in planned_events])

    @application.post("/budgets/<budget_id>/planned-events")
    def create_planned_event(budget_id: str):
        budget = _get_budget_or_404(budget_id)
        values = _validate_planned_event_payload(_json_body())
        _require_existing_budget_line_category(budget.id, values.get("category"))
        _require_date_within_budget_month(budget, values.get("date"))
        now = _now_timestamp()
        planned_event = PlannedEvent(
            budget_id=budget.id,
            date=values.get("date"),
            label=values.get("label"),
            category=values.get("category"),
            est_low=values.get("est_low"),
            est_high=values.get("est_high"),
            source=values.get("source"),
            status=values.get("status"),
            created_at=now,
            updated_at=now,
        )
        db.session.add(planned_event)
        db.session.commit()
        return jsonify(planned_event.to_dict()), 201

    @application.get("/planned-events/<event_id>")
    def get_planned_event(event_id: str):
        return jsonify(_get_planned_event_or_404(event_id).to_dict())

    @application.patch("/planned-events/<event_id>")
    def patch_planned_event(event_id: str):
        planned_event = _get_planned_event_or_404(event_id)
        values = _validate_planned_event_payload(_json_body(), partial=True)
        if not values:
            raise ApiError("no updatable fields supplied", 400, "missing_required_fields")
        category = values.get("category", planned_event.category)
        _require_existing_budget_line_category(planned_event.budget_id, category)
        _require_date_within_budget_month(planned_event.budget, values.get("date", planned_event.date))
        for field, value in values.items():
            setattr(planned_event, field, value)
        planned_event.updated_at = _now_timestamp()
        db.session.commit()
        return jsonify(planned_event.to_dict())

    @application.delete("/planned-events/<event_id>")
    def delete_planned_event(event_id: str):
        planned_event = _get_planned_event_or_404(event_id)
        db.session.delete(planned_event)
        db.session.commit()
        return "", 204

    @application.get("/budgets/<budget_id>/coach-proposals")
    def list_coach_proposals(budget_id: str):
        budget = _get_budget_or_404(budget_id)
        proposals = db.session.scalars(
            select(CoachProposal)
            .where(CoachProposal.budget_id == budget.id)
            .order_by(CoachProposal.created_at, CoachProposal.id)
        ).all()
        return jsonify([proposal.to_dict() for proposal in proposals])

    @application.post("/budgets/<budget_id>/coach-proposals")
    def create_coach_proposal(budget_id: str):
        budget = _get_budget_or_404(budget_id)
        values = _validate_coach_proposal_payload(_json_body())
        now = _now_timestamp()
        proposal = CoachProposal(
            budget_id=budget.id,
            proposal_json=values.get("proposal_json"),
            rationale=values.get("rationale"),
            status=values.get("status") or "proposed",
            rejection_reason=values.get("rejection_reason"),
            decided_at=values.get("decided_at"),
            created_at=now,
        )
        db.session.add(proposal)
        db.session.commit()
        return jsonify(proposal.to_dict()), 201

    @application.get("/coach-proposals/<proposal_id>")
    def get_coach_proposal(proposal_id: str):
        return jsonify(_get_coach_proposal_or_404(proposal_id).to_dict())

    @application.patch("/coach-proposals/<proposal_id>")
    def patch_coach_proposal(proposal_id: str):
        proposal = _get_coach_proposal_or_404(proposal_id)
        values = _validate_coach_proposal_payload(_json_body(), partial=True)
        if not values:
            raise ApiError("no updatable fields supplied", 400, "missing_required_fields")
        for field, value in values.items():
            setattr(proposal, field, value)
        if "status" in values and values["status"] in {"accepted", "rejected"} and "decided_at" not in values:
            proposal.decided_at = _now_timestamp()
        db.session.commit()
        return jsonify(proposal.to_dict())

    @application.delete("/coach-proposals/<proposal_id>")
    def delete_coach_proposal(proposal_id: str):
        proposal = _get_coach_proposal_or_404(proposal_id)
        db.session.delete(proposal)
        db.session.commit()
        return "", 204

    @application.get("/budgets/<budget_id>/chat-messages")
    def list_chat_messages(budget_id: str):
        budget = _get_chat_message_budget_or_404(budget_id)
        messages = db.session.scalars(
            select(ChatMessage)
            .where(ChatMessage.budget_id == budget.id)
            .order_by(ChatMessage.created_at, ChatMessage.id)
        ).all()
        return jsonify([message.to_dict() for message in messages])

    @application.post("/budgets/<budget_id>/chat-messages")
    def create_chat_message(budget_id: str):
        budget = _get_chat_message_budget_or_404(budget_id)
        values = _validate_chat_message_payload(_json_body())
        proposal_id = values.get("proposal_id")
        if proposal_id is not None:
            proposal = db.session.get(CoachProposal, proposal_id)
            if proposal is None or proposal.budget_id != budget.id:
                raise ApiError("proposal_id must refer to a coach proposal for this budget", 422, "proposal_not_found")
        message = ChatMessage(
            budget_id=budget.id,
            proposal_id=proposal_id,
            role=values["role"],
            content=values["content"],
            mode=values.get("mode"),
            response_source=values.get("response_source"),
            plan_json=values.get("plan_json"),
            observation_json=values.get("observation_json"),
            stage_trace=values.get("stage_trace"),
            tool_result_json=values.get("tool_result_json"),
            grounding_json=values.get("grounding_json"),
            created_at=_now_timestamp(),
        )
        db.session.add(message)
        db.session.commit()
        return jsonify(message.to_dict()), 201

    @application.delete("/budgets/<budget_id>/chat-messages")
    def delete_chat_messages(budget_id: str):
        budget = _get_chat_message_budget_or_404(budget_id)
        db.session.query(ChatMessage).where(ChatMessage.budget_id == budget.id).delete()
        db.session.commit()
        return "", 204

    @application.post("/admin/reset")
    def reset_database():
        db.session.remove()
        db.drop_all()
        db.create_all()
        if seed_demo_data:
            seed_database_if_empty()
        db.session.commit()
        return jsonify(ok=True)

    return application


def create_app(db_path: str | None = None, seed_demo_data: bool = True) -> Flask:
    return _create_app(
        db_path or os.environ.get("DB_PATH", "./budgets.db"),
        seed_demo_data=seed_demo_data,
    )
