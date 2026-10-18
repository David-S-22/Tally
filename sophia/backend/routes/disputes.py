"""JSON API routes for disputes and their AI-drafted letters."""
from flask import Blueprint, jsonify

from sophia.backend.json_body import json_body
from sophia.backend.services import disputes as disputes_service

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
