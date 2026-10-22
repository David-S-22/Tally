import os
from random import choice, randint

from flask import Flask, abort, jsonify, render_template, request

from shared.backend import dto
from .config import config
from .helpers import deserialise_or_abort, empty
from .services import (
    anomalies_api,
    multi_agent_client,
    ollama_api,
    review_queue,
    transaction_api,
)


app = Flask(__name__)
review_queue.start_worker(app)


@app.errorhandler(multi_agent_client.MultiAgentError)
def handle_multi_agent_error(error: multi_agent_client.MultiAgentError):
    status = {
        "multi_agent_timeout": 504,
        "multi_agent_busy": 503,
        "multi_agent_unavailable": 503,
        "multi_agent_conflict": 409,
        "multi_agent_invalid_response": 502,
    }.get(error.code, 502)
    return jsonify(code=error.code, error=error.message), status


# How long the /anomaly-alert endpoint long-polls for a new anomaly before
# returning empty so the client can re-poll.
ANOMALY_WAIT_SECONDS = float(os.environ.get("ANOMALY_WAIT_SECONDS", "180"))


@app.get("/")
def get_index():
    return jsonify(container="anomalies-backend")

@app.get("/fact")
def get_fact():
    return ollama_api.prompt(
        system_prompt="You are a helpful assistant",
        user_prompt="tell me a random fact",
        model=config.OLLAMA_MODEL,
        temperature=0.5,
        output_tokens=500).text

@app.get("/anomalies")
def get_anomaly_rows():
    anomalies = anomalies_api.get_all_anomalies()
    transactions = {t.id: t for t in transaction_api.get_all_transactions()}
    categories = {category.id: category.name for category in transaction_api.get_all_categories()}

    return render_template(
        "anomalies.jinja",
        anomalies=anomalies,
        transactions=transactions,
        categories=categories,
    )

@app.post("/check-transaction")
def check_transaction():
    data = request.get_json() or {}
    transaction = deserialise_or_abort(dto.Transaction, data)

    review_queue.enqueue(transaction)
    app.logger.info("Queued transaction %s for anomaly review", transaction.id)

    return jsonify(status="queued", transaction_id=transaction.id), 202

@app.get("/anomaly-alert")
def wait_for_anomaly_alert():
    """Wait for a specific queued transaction to finish review, then report it.

    The client passes the `key` (the transaction id returned by
    /check-transaction). Blocks for up to ANOMALY_WAIT_SECONDS until that item
    has been reviewed. Returns the alert HTML if the transaction was flagged as
    anomalous, otherwise 204 (no anomaly, still processing, or unknown key) so
    the client can re-poll.
    """
    key = request.args.get("key", type=int)
    if key is None:
        abort(400, "missing 'key' query parameter")

    anomaly = review_queue.wait_for_result(key, timeout=ANOMALY_WAIT_SECONDS)

    if anomaly is None:
        return "", 204

    return render_template("alert.jinja", anomaly=anomaly)

@app.post("/anomalies/<int:id>/confirm")
def confirm_anomaly(id: int):
    _record_review_decision(id, True)
    anomalies_api.set_confirmation(id, True)
    return get_anomaly_rows()

@app.post("/anomalies/<int:id>/dismiss")
def dismiss_anomaly(id: int):
    _record_review_decision(id, False)
    anomalies_api.set_confirmation(id, False)
    return get_anomaly_rows()


def _record_review_decision(id: int, confirmed: bool) -> None:
    anomaly = anomalies_api.get_anomaly(id)
    if anomaly is None:
        abort(404)
    if anomaly.workflow_id is None:
        return

    data = request.get_json(silent=True) or {}
    reason = data.get("reason") if isinstance(data, dict) else None
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        abort(400, "A review reason of 1 to 4000 characters is required")

    decision = "Accept" if confirmed else "Reject"
    body = {"decision": decision, "reason": reason.strip()}
    try:
        result = multi_agent_client.record_decision(anomaly.workflow_id, body)
    except multi_agent_client.MultiAgentError as error:
        if error.code not in {"multi_agent_timeout", "multi_agent_conflict"}:
            _raise_multi_agent_error(error)
        try:
            workflow = multi_agent_client.get_workflow(anomaly.workflow_id)
        except multi_agent_client.MultiAgentError:
            _raise_multi_agent_error(error)
        human = workflow.get("human_decision")
        if not (
            isinstance(human, dict)
            and human.get("status") == "recorded"
            and human.get("decision") == decision
            and human.get("reason") == reason.strip()
        ):
            _raise_multi_agent_error(error)
        result = workflow

    human = result.get("human_decision")
    if not (
        isinstance(human, dict)
        and human.get("status") == "recorded"
        and human.get("decision") == decision
    ):
        _raise_multi_agent_error(
            multi_agent_client.MultiAgentError("multi_agent_invalid_response")
        )


def _raise_multi_agent_error(error: multi_agent_client.MultiAgentError) -> None:
    raise error


@app.post("/dummy-anomaly")
def create_dummy_anomaly():
    anomaly = dto.Anomaly(
        id=0,
        transaction_id=randint(1, 1_000_000_000),
        agent_reason_suspected="hello",
        is_confirmed_by_user=choice((True, False, None)),
    )

    anomalies_api.create_anomaly(anomaly)
    return get_anomaly_rows()
