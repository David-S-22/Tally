from flask import Blueprint, Flask, abort, json, jsonify, request
from pathlib import Path
from sqlalchemy import select, inspect, text
from werkzeug.exceptions import HTTPException

from .models import Anomaly, db
from .helpers import empty, set_mandatory_field, set_optional_field, try_parse_bool, try_parse_float, try_parse_int, try_parse_string_list


app = Flask(__name__)
anomalies = Blueprint("anomalies", __name__)


@app.get("/")
def get_index():
    return jsonify(container="anomalies-db")

@anomalies.get("/")
def get_all_anomalies():
    anomalies = db.session.scalars(select(Anomaly)).all()

    return jsonify([a.to_dto() for a in anomalies])

@anomalies.post("/")
def post_anomaly():
    data = request.get_json() or {}
    anomaly = Anomaly()

    set_mandatory_field(anomaly, data, "transaction_id", try_parse_int)
    set_mandatory_field(anomaly, data, "agent_reason_suspected", str)
    set_optional_field(anomaly, data, "is_confirmed_by_user", try_parse_bool)
    set_optional_field(anomaly, data, "confidence", try_parse_float)
    set_optional_field(anomaly, data, "sources", try_parse_string_list)

    db.session.add(anomaly)
    db.session.commit()
    db.session.refresh(anomaly)

    return jsonify(anomaly.to_dto()), 201

@anomalies.get("/<int:id>")
def get_anomaly(id: int):
    anomaly = db.get_or_404(Anomaly, id)

    return jsonify(anomaly.to_dto())

@anomalies.get("/by-transaction/<int:id>")
def get_anomaly_by_transaction(id: int):
    anomaly = db.session.scalars(
        select(Anomaly).where(Anomaly.transaction_id == id)
    ).first()

    if anomaly is None:
        abort(404)

    return jsonify(anomaly.to_dto())

@anomalies.patch("/<int:id>")
def patch_anomaly(id: int):
    anomaly = db.get_or_404(Anomaly, id)
    data = request.get_json() or {}

    # Only the following fields are permitted to be mutated once created
    parsed = try_parse_bool(data.get("is_confirmed_by_user"))

    if isinstance(parsed, bool):
        anomaly.is_confirmed_by_user = parsed

    if inspect(anomaly, raiseerr=True).modified:
        db.session.commit() # idk about this one

    db.session.refresh(anomaly)

    return jsonify(anomaly.to_dto())

@anomalies.delete("/<int:id>")
def delete_anomaly(id: int):
    deleted_count = db.session.query(Anomaly).where(Anomaly.id == id).delete()

    assert deleted_count in (0, 1)
    db.session.commit()

    return empty()

@anomalies.delete("/by-transaction/<int:id>")
def delete_anomaly_by_id(id: int):
    deleted_count = db.session.query(Anomaly).where(Anomaly.transaction_id == id).delete()

    assert deleted_count in (0, 1)
    db.session.commit()

    return empty()

@app.errorhandler(HTTPException)
def handle_exception(e):
    """Return JSON instead of HTML for HTTP errors."""

    response = e.get_response()
    response.content_type = "application/json"
    response.data = json.dumps({
        "code": e.code,
        "name": e.name,
        "description": e.description,
    })

    return response

app.register_blueprint(anomalies, url_prefix="/anomalies")

def setup_database(db_path: str):
    if "sqlalchemy" in app.extensions:
        return

    if db_path and db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + db_path
        db.init_app(app)
        db.create_all()
        _migrate_add_sources_column()


def _migrate_add_sources_column():
    """Add the ``sources`` column to pre-existing anomaly tables.

    ``db.create_all()`` never alters existing tables, so databases created before
    the sources feature keep their old schema on a persisted volume. This adds
    the column (defaulting to an empty JSON list) when it is missing.
    """
    columns = {column["name"] for column in inspect(db.engine).get_columns("anomaly")}
    if "sources" in columns:
        return

    with db.engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE anomaly ADD COLUMN sources JSON NOT NULL DEFAULT '[]'")
        )
