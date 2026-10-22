import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from sqlalchemy import case, delete as sql_delete, func, select, update as sql_update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.exceptions import BadRequest, HTTPException

from .helpers import (
    category_corrections,
    category_is_in_use,
    category_name_exists,
    filtered_transactions,
)
from .anomalies import delete_anomaly_by_transaction_id
from .models import Category, CategoryCorrection, Transaction, db
from .seed import seed_database_if_empty
from .validation import (
    ApiError,
    parse_query_positive_integer,
    parse_transaction_filters,
    validate_category_payload,
    validate_correction_payload,
    validate_expected_transaction,
    validate_path_identifier,
    validate_transaction_payload,
)


PROTECTED_CATEGORY_NAME = "Uncategorised"
SAVINGS_DB_URL = os.environ.get(
    "SAVINGS_DB_URL",
    "http://savings-db:6002",
).rstrip("/")
SAVINGS_TIMEOUT_SECONDS = float(os.environ.get("SAVINGS_TIMEOUT_SECONDS", "10"))


def database_uri(database_path):
    if database_path == ":memory:":
        return "sqlite:///:memory:"
    path = Path(database_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{path.as_posix()}"


def utc_datetime():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def json_body():
    if not request.is_json:
        raise ApiError(
            "request body must be a JSON object",
            "invalid_json",
            400,
        )
    try:
        payload = request.get_json()
    except (BadRequest, RecursionError) as error:
        raise ApiError("request body contains invalid JSON", "invalid_json", 400) from error
    if not isinstance(payload, dict):
        raise ApiError(
            "request body must be a JSON object",
            "invalid_json",
            400,
        )
    return payload


def require_category(category_id):
    category = db.session.get(Category, category_id)
    if category is None:
        raise ApiError("category not found", "category_not_found", 422)
    return category


def resolve_transaction(transaction_identifier):
    transaction_id = validate_path_identifier(transaction_identifier, "transaction")
    transaction = db.session.get(Transaction, transaction_id)
    if transaction is None:
        raise ApiError("transaction not found", "transaction_not_found", 404)
    return transaction


def resolve_category(category_identifier):
    category_id = validate_path_identifier(category_identifier, "category")
    category = db.session.get(Category, category_id)
    if category is None:
        raise ApiError("category not found", "category_not_found", 404)
    return category


def require_unprotected_category(category):
    if category.name.casefold() == PROTECTED_CATEGORY_NAME.casefold():
        raise ApiError(
            "Uncategorised is a protected system category",
            "protected_category",
            409,
        )


def delete_savings_feedback_by_category_id(category_id):
    """Best-effort removal of savings feedback tied to a deleted category."""
    try:
        requests.delete(
            f"{SAVINGS_DB_URL}/feedbacks",
            params={"category_id": category_id},
            timeout=SAVINGS_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        pass


def apply_transaction_values(transaction, values):
    for field in ("date", "merchant", "description", "amount"):
        if field in values:
            setattr(transaction, field, values[field])
    if "category_id" in values:
        transaction.category = require_category(values["category_id"])
    transaction.updated_at = utc_datetime()


def expected_transaction_conditions(expected):
    return (
        Transaction.id == expected["id"],
        Transaction.updated_at == expected["version"],
    )


def require_matching_expected_id(transaction_id, expected):
    if expected is not None and expected["id"] != transaction_id:
        raise ApiError(
            "transaction changed or no longer exists",
            "stale_preview",
            409,
        )


def transaction_response(transaction, include_version=False):
    payload = asdict(transaction.to_dto())
    if include_version:
        payload["version"] = transaction.updated_at.isoformat()
    return payload


def conditional_update_time(expected):
    now = utc_datetime()
    if expected is not None and now <= expected["version"]:
        return expected["version"] + timedelta(microseconds=1)
    return now


def register_error_handlers(application):
    @application.errorhandler(ApiError)
    def handle_api_error(error):
        db.session.rollback()
        return jsonify(error=error.message, code=error.code), error.status

    @application.errorhandler(IntegrityError)
    def handle_integrity_error(_error):
        db.session.rollback()
        return jsonify(
            error="database constraint conflict",
            code="database_conflict",
        ), 409

    @application.errorhandler(SQLAlchemyError)
    def handle_database_error(error):
        db.session.rollback()
        application.logger.exception("Transactions database operation failed")
        return jsonify(
            error="database unavailable",
            code="database_unavailable",
        ), 503

    @application.errorhandler(HTTPException)
    def handle_http_error(error):
        code = error.name.lower().replace(" ", "_")
        return jsonify(error=error.description, code=code), error.code


def register_routes(application):
    @application.get("/")
    def get_index():
        return jsonify(container="transactions-db")

    @application.get("/health")
    def get_health():
        return jsonify(ok=True, container="transactions-db")

    @application.get("/transactions")
    def get_transactions():
        transactions = filtered_transactions(
            parse_transaction_filters(request.args)
        )
        include_version = request.args.get("_include_version") == "true"
        return jsonify([
            transaction_response(transaction, include_version)
            for transaction in transactions
        ])

    @application.post("/transactions")
    def post_transaction():
        values = validate_transaction_payload(json_body())
        now = utc_datetime()
        category = require_category(values["category_id"])
        suggested_category_id = values.get("suggested_category_id")
        suggested_category = (
            require_category(suggested_category_id)
            if suggested_category_id is not None
            else None
        )
        transaction = Transaction(
            date=values["date"],
            merchant=values["merchant"],
            description=values["description"],
            amount=values["amount"],
            category=category,
            created_at=now,
            updated_at=now,
        )
        db.session.add(transaction)
        db.session.flush()
        if (
            suggested_category is not None
            and suggested_category.id != category.id
        ):
            db.session.add(CategoryCorrection(
                transaction=transaction,
                previous_category=suggested_category,
                user_category=category,
                corrected_at=now.isoformat(),
            ))
        db.session.commit()
        return jsonify(transaction_response(transaction)), 201

    @application.post("/transactions/bulk")
    def post_transactions_bulk():
        payload = json_body()
        rows = payload.get("transactions")
        if not isinstance(rows, list) or not rows:
            raise ApiError(
                "transactions must be a non-empty list",
                "invalid_transactions",
                422,
            )
        validated = [validate_transaction_payload(row) if isinstance(row, dict) else None for row in rows]
        if any(values is None for values in validated):
            raise ApiError(
                "each transaction must be a JSON object",
                "invalid_transactions",
                422,
            )
        categories = {
            category_id
            for category_id in db.session.scalars(select(Category.id))
        }
        for values in validated:
            if values["category_id"] not in categories:
                raise ApiError("category not found", "category_not_found", 422)
        existing = {
            (row.date, row.merchant, row.amount)
            for row in db.session.execute(
                select(Transaction.date, Transaction.merchant, Transaction.amount)
            )
        }
        now = utc_datetime()
        created = 0
        for values in validated:
            key = (values["date"], values["merchant"], values["amount"])
            if key in existing:
                continue
            existing.add(key)
            db.session.add(Transaction(
                date=values["date"],
                merchant=values["merchant"],
                description=values["description"],
                amount=values["amount"],
                category_id=values["category_id"],
                created_at=now,
                updated_at=now,
            ))
            created += 1
        db.session.commit()
        return jsonify(created=created, already_present=len(validated) - created), 201

    @application.get("/transactions/<transaction_id>")
    def get_transaction(transaction_id):
        transaction = resolve_transaction(transaction_id)
        return jsonify(transaction_response(
            transaction,
            request.args.get("_include_version") == "true",
        ))

    @application.patch("/transactions/<transaction_id>")
    def patch_transaction(transaction_id):
        transaction_id = validate_path_identifier(
            transaction_id,
            "transaction",
        )
        values = validate_transaction_payload(json_body(), partial=True)
        expected = validate_expected_transaction(
            request.headers.get("X-Expected-Transaction")
        )
        require_matching_expected_id(transaction_id, expected)
        if expected is not None:
            if "category_id" in values:
                require_category(values["category_id"])
            update_values = {
                **values,
                "updated_at": conditional_update_time(expected),
            }
            result = db.session.execute(
                sql_update(Transaction)
                .where(*expected_transaction_conditions(expected))
                .values(**update_values)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise ApiError(
                    "transaction changed or no longer exists",
                    "stale_preview",
                    409,
                )
            db.session.commit()
            transaction = db.session.get(Transaction, transaction_id)
            return jsonify(transaction_response(transaction))

        transaction = resolve_transaction(transaction_id)
        apply_transaction_values(transaction, values)
        db.session.commit()
        return jsonify(transaction_response(transaction))

    @application.delete("/transactions/<transaction_id>")
    def delete_transaction(transaction_id):
        transaction_id = validate_path_identifier(
            transaction_id,
            "transaction",
        )
        expected = validate_expected_transaction(
            request.headers.get("X-Expected-Transaction")
        )
        require_matching_expected_id(transaction_id, expected)
        if expected is not None:
            result = db.session.execute(
                sql_delete(Transaction)
                .where(*expected_transaction_conditions(expected))
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                raise ApiError(
                    "transaction changed or no longer exists",
                    "stale_preview",
                    409,
                )
            db.session.commit()
            delete_anomaly_by_transaction_id(transaction_id)
            return "", 204

        db.session.delete(resolve_transaction(transaction_id))
        db.session.commit()
        delete_anomaly_by_transaction_id(transaction_id)
        return "", 204

    @application.post("/transactions/<transaction_id>/category-correction")
    def post_category_correction(transaction_id):
        user_category_id = validate_correction_payload(json_body())
        transaction = resolve_transaction(transaction_id)
        user_category = require_category(user_category_id)
        now = utc_datetime()
        correction = CategoryCorrection(
            transaction=transaction,
            previous_category=transaction.category,
            user_category=user_category,
            corrected_at=now.isoformat(),
        )
        transaction.category = user_category
        transaction.updated_at = now
        db.session.add(correction)
        db.session.commit()
        return jsonify(
            transaction=transaction_response(transaction),
            correction=correction.to_dict(),
        ), 201

    @application.get("/categories")
    def get_categories():
        protected_first = case(
            (
                func.casefold(Category.name) == PROTECTED_CATEGORY_NAME.casefold(),
                0,
            ),
            else_=1,
        )
        categories = db.session.scalars(
            select(Category).order_by(protected_first, func.casefold(Category.name))
        ).all()
        return jsonify([category.to_dto() for category in categories])

    @application.post("/categories")
    def post_category():
        values = validate_category_payload(json_body())
        if category_name_exists(values["name"]):
            raise ApiError(
                "category name already exists",
                "category_name_conflict",
                409,
            )
        category = Category(name=values["name"], type=values["type"])
        db.session.add(category)
        db.session.commit()
        return jsonify(category.to_dto()), 201

    @application.get("/categories/<category_id>")
    def get_category(category_id):
        return jsonify(resolve_category(category_id).to_dto())

    @application.patch("/categories/<category_id>")
    def patch_category(category_id):
        category = resolve_category(category_id)
        require_unprotected_category(category)
        values = validate_category_payload(json_body(), partial=True)
        if "name" in values and category_name_exists(
            values["name"],
            excluded_id=category.id,
        ):
            raise ApiError(
                "category name already exists",
                "category_name_conflict",
                409,
            )
        for field in ("name", "type"):
            if field in values:
                setattr(category, field, values[field])
        db.session.commit()
        return jsonify(category.to_dto())

    @application.delete("/categories/<category_id>")
    def delete_category(category_id):
        category = resolve_category(category_id)
        require_unprotected_category(category)
        category_id = category.id
        if category_is_in_use(category_id):
            raise ApiError("category is in use", "category_in_use", 409)
        db.session.delete(category)
        db.session.commit()
        delete_savings_feedback_by_category_id(category_id)
        return "", 204

    @application.get("/category-corrections")
    def get_category_corrections():
        merchant = request.args.get("merchant", "").strip()
        limit = parse_query_positive_integer(
            request.args.get("limit"),
            "limit",
            maximum=100,
        )

        return jsonify([
            correction.to_dict()
            for correction in category_corrections(merchant, limit)
        ])


def setup_app(database_path=None):
    application = Flask(__name__)
    resolved_database_path = database_path or os.environ.get(
        "DB_PATH",
        "./transactions.db",
    )
    application.config["DB_PATH"] = resolved_database_path
    application.config["SQLALCHEMY_DATABASE_URI"] = database_uri(
        resolved_database_path
    )
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(application)
    register_error_handlers(application)
    register_routes(application)

    with application.app_context():
        db.create_all()
        seed_database_if_empty()

    return application
