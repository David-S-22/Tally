"""JSON API routes for disputes and their AI-drafted letters."""
from flask import Blueprint, jsonify

from sophia.backend.json_body import json_body
from sophia.backend.services import disputes as disputes_service
from sophia.backend.services import multi_agent as multi_agent_service

bp = Blueprint("disputes", __name__, url_prefix="/api")


@bp.get("/disputes")
def list_disputes():
    return jsonify(disputes_service.list_disputes())


@bp.post("/disputes")
def create_dispute():
    payload = json_body()
    return jsonify(disputes_service.create_dispute(payload.get("bill_id"), payload.get("reason"))), 201


@bp.get("/disputes/<int:dispute_id>")
def get_dispute(dispute_id):
    return jsonify(disputes_service.get_dispute(dispute_id))


@bp.put("/disputes/<int:dispute_id>")
def update_dispute(dispute_id):
    return jsonify(disputes_service.update_dispute(dispute_id, json_body()))


@bp.delete("/disputes/<int:dispute_id>")
def delete_dispute(dispute_id):
    return jsonify(disputes_service.delete_dispute(dispute_id))


@bp.get("/disputes/<int:dispute_id>/drafts")
def list_drafts(dispute_id):
    return jsonify(disputes_service.list_drafts(dispute_id))


@bp.post("/disputes/<int:dispute_id>/regenerate")
def regenerate(dispute_id):
    payload = json_body()
    result = disputes_service.regenerate(dispute_id, edited_letter=payload.get("edited_letter"), feedback=payload.get("feedback"))
    return jsonify(result), 201


@bp.post("/disputes/<int:dispute_id>/workflow")
def start_workflow(dispute_id):
    """Release 2: start Planner -> Worker -> Reviewer for the letter in the background; 202 with the run id to poll."""
    run_id = multi_agent_service.start_dispute_letter(dispute_id)
    return jsonify({"run_id": run_id, "state": "running"}), 202


@bp.get("/workflow-runs/<run_id>")
def workflow_run(run_id):
    """Poll a background run: state, elapsed seconds, the agents' summary once there is one."""
    return jsonify(multi_agent_service.run_status(run_id))


@bp.post("/disputes/<int:dispute_id>/workflow/<workflow_id>/decision")
def decide_workflow(dispute_id, workflow_id):
    """Release 2: record the human review; Accept or Partially Accept stores the final letter as a new draft."""
    payload = json_body()
    return jsonify(
        multi_agent_service.decide_dispute_letter(
            dispute_id, workflow_id, payload.get("decision"),
            corrected_output=payload.get("corrected_output"), reason=payload.get("reason"),
        )
    )
