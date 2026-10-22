import os
import requests
from flask import Flask, abort, jsonify, make_response, render_template, request
from shared.backend import dto
from .classifier_service import classify_feedback
from .helpers import fetch_categories, object_to_hook, try_parse_bool
from .suggestion_service import generate_savings_advice


def setup_app(db_url: str, transactions_db_url: str) -> Flask:
    app = Flask(__name__)
    db_url = db_url.rstrip("/")
    tx_url = transactions_db_url.rstrip("/")

    @app.route("/")
    def home():
        return "<p>Savings Backend</p>", 200

    @app.route("/goals")
    def get_goals():
        resp = requests.get(f"{db_url}/goals")
        resp.raise_for_status()
        goals = resp.json(object_hook=object_to_hook)
        return render_template("goals-table.jinja", goals=goals), 200

    @app.route("/goal", methods=["POST"])
    def create_goal():
        payload = request.get_json(silent=True) or request.form.to_dict()
        try:
            goal = object_to_hook(payload)
            if not isinstance(goal, dto.Goal):
                return jsonify({"error": "Missing goal fields"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 400

        resp = requests.post(f"{db_url}/goal", json=payload)
        resp.raise_for_status()
        return make_response(*get_goals(), {"HX-Trigger": "goalChanged"})

    @app.route("/goal/<int:id>")
    def get_goal(id: int):
        resp = requests.get(f"{db_url}/goal/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        goal = resp.json(object_hook=object_to_hook)
        return render_template("goal-row.jinja", goal=goal), 200

    @app.route("/goal/<int:id>/edit")
    def edit_goal(id: int):
        resp = requests.get(f"{db_url}/goal/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        goal = resp.json(object_hook=object_to_hook)
        return render_template("goal-row-edit.jinja", goal=goal), 200

    @app.route("/goal/<int:id>", methods=["PATCH"])
    def update_goal(id: int):
        payload = request.get_json(silent=True) or request.form.to_dict()
        resp = requests.patch(f"{db_url}/goal/{id}", json=payload)
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        goal = resp.json(object_hook=object_to_hook)
        return make_response(render_template("goal-row.jinja", goal=goal), 200, {"HX-Trigger": "goalChanged"})

    @app.route("/goal/<int:id>", methods=["DELETE"])
    def delete_goal(id: int):
        resp = requests.delete(f"{db_url}/goal/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        return make_response(*get_goals(), {"HX-Trigger": "goalChanged"})

    @app.route("/suggestions")
    def get_suggestions():
        resp = requests.get(f"{db_url}/suggestions")
        resp.raise_for_status()
        suggestions = resp.json(object_hook=object_to_hook)
        return render_template("suggestions-table.jinja", suggestions=suggestions), 200

    @app.route("/suggestion", methods=["POST"])
    def create_suggestion():
        payload = request.get_json(silent=True) or request.form.to_dict()
        suggestion = object_to_hook(payload)
        if not isinstance(suggestion, dto.Suggestion):
            return jsonify({"error": "Missing suggestion fields"}), 400

        resp = requests.post(f"{db_url}/suggestion", json=payload)
        resp.raise_for_status()
        return make_response(*get_suggestions(), {"HX-Trigger": "suggestionChanged"})

    @app.route("/suggestion/<int:id>")
    def get_suggestion(id: int):
        resp = requests.get(f"{db_url}/suggestion/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        suggestion = resp.json(object_hook=object_to_hook)
        return render_template("suggestion-row.jinja", suggestion=suggestion), 200

    @app.route("/suggestion/<int:id>/edit")
    def edit_suggestion(id: int):
        resp = requests.get(f"{db_url}/suggestion/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        suggestion = resp.json(object_hook=object_to_hook)
        return render_template("suggestion-row-edit.jinja", suggestion=suggestion), 200

    @app.route("/suggestion/<int:id>", methods=["PATCH"])
    def update_suggestion(id: int):
        payload = request.get_json(silent=True) or request.form.to_dict()
        resp = requests.patch(f"{db_url}/suggestion/{id}", json=payload)
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        suggestion = resp.json(object_hook=object_to_hook)
        return make_response(render_template("suggestion-row.jinja", suggestion=suggestion), 200, {"HX-Trigger": "suggestionChanged"})

    @app.route("/suggestion/<int:id>", methods=["DELETE"])
    def delete_suggestion(id: int):
        resp = requests.delete(f"{db_url}/suggestion/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        return make_response(*get_suggestions(), {"HX-Trigger": "suggestionChanged, feedbackChanged"})

    @app.route("/feedback")
    def get_feedback():
        resp = requests.get(f"{db_url}/feedbacks")
        resp.raise_for_status()
        feedbacks = resp.json(object_hook=object_to_hook)
        return render_template("feedback-table.jinja", feedbacks=feedbacks), 200

    @app.route("/feedback", methods=["POST"])
    def create_feedback():
        payload = request.get_json(silent=True) or request.form.to_dict()
        feedback = object_to_hook(payload)
        if not isinstance(feedback, dto.Feedback) or not str(feedback.feedback).strip():
            return jsonify({"error": "Missing feedback field"}), 400

        category_id = payload.get("category_id")
        timeframe = payload.get("timeframe")
        if category_id == "":
            category_id = None
        if timeframe == "":
            timeframe = None
        if category_id is None and timeframe is None:
            categories = fetch_categories(tx_url)
            category_id, timeframe = classify_feedback(feedback.feedback, categories)

        feedback_payload = {
            "feedback": str(feedback.feedback).strip(),
            "suggestion_id": feedback.suggestion_id,
            "category_id": category_id,
            "timeframe": timeframe,
        }

        resp = requests.post(f"{db_url}/feedback", json=feedback_payload)
        resp.raise_for_status()
        return make_response(*get_feedback(), {"HX-Trigger": "feedbackChanged"})

    @app.route("/feedback/<int:id>")
    def get_single_feedback(id: int):
        resp = requests.get(f"{db_url}/feedback/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        feedback = resp.json(object_hook=object_to_hook)
        return render_template("feedback-row.jinja", feedback=feedback), 200

    @app.route("/feedback/<int:id>/edit")
    def edit_feedback(id: int):
        resp = requests.get(f"{db_url}/feedback/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        feedback = resp.json(object_hook=object_to_hook)
        return render_template("feedback-row-edit.jinja", feedback=feedback), 200

    @app.route("/feedback/<int:id>", methods=["PATCH"])
    def update_feedback(id: int):
        payload = request.get_json(silent=True) or request.form.to_dict() or {}
        has_feedback = "feedback" in payload
        feedback_text = str(payload.get("feedback", "")).strip() if has_feedback else ""
        category_id = payload.get("category_id")
        timeframe = payload.get("timeframe")

        if category_id == "":
            category_id = None
        if timeframe == "":
            timeframe = None

        if has_feedback and feedback_text and "category_id" not in payload and "timeframe" not in payload:
            categories = fetch_categories(tx_url)
            category_id, timeframe = classify_feedback(feedback_text, categories)

        update_payload = {}
        if has_feedback:
            update_payload["feedback"] = feedback_text
        if "category_id" in payload or (has_feedback and feedback_text):
            update_payload["category_id"] = category_id
        if "timeframe" in payload or (has_feedback and feedback_text):
            update_payload["timeframe"] = timeframe

        resp = requests.patch(f"{db_url}/feedback/{id}", json=update_payload)
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        feedback = resp.json(object_hook=object_to_hook)
        return make_response(render_template("feedback-row.jinja", feedback=feedback), 200, {"HX-Trigger": "feedbackChanged"})

    @app.route("/feedback/<int:id>", methods=["DELETE"])
    def delete_feedback_by_id(id: int):
        resp = requests.delete(f"{db_url}/feedback/{id}")
        if resp.status_code == 404:
            abort(404)
        resp.raise_for_status()
        return make_response(*get_feedback(), {"HX-Trigger": "feedbackChanged"})

    @app.route("/feedbacks", methods=["DELETE"])
    def delete_feedbacks_by_category():
        category_id = request.args.get("category_id")
        params = {}
        if category_id is not None:
            params["category_id"] = category_id
        resp = requests.delete(f"{db_url}/feedbacks", params=params)
        if resp.status_code == 400:
            return jsonify(resp.json()), 400
        resp.raise_for_status()
        return make_response(*get_feedback(), {"HX-Trigger": "feedbackChanged"})

    @app.route("/ai-suggestion")
    def get_ai_suggestion():
        previous_suggestion = request.args.get("suggestion")
        suggestion, sources, confidence, reviewer = generate_savings_advice(
            db_url, tx_url=tx_url, previous_suggestion=previous_suggestion
        )
        return render_template(
            "ai-suggestion.jinja",
            suggestion=suggestion,
            sources=sources,
            confidence=confidence,
            reviewer=reviewer,
        ), 200

    @app.route("/ai-suggestion/action", methods=["POST"])
    def action_ai_suggestion():
        payload = request.get_json(silent=True) or request.form.to_dict()
        suggestion_text = payload.get("suggestion", "").strip() if payload else ""
        feedback_text = payload.get("feedback", "").strip() if payload else ""
        accepted_raw = request.args.get("accepted") if "accepted" in request.args else (payload.get("accepted") if payload else None)
        accepted = try_parse_bool(accepted_raw)

        if (
            not suggestion_text
            or suggestion_text == "No current AI suggestion available."
            or suggestion_text.startswith("Error")
            or suggestion_text.startswith("You don't have")
            or suggestion_text.startswith("No transactions found")
            or suggestion_text.startswith("Insufficient context")
            or suggestion_text.startswith("AI mode is turned off")
        ):
            return jsonify({"error": "No valid suggestion available to accept or reject."}), 400

        if accepted is None:
            return jsonify({"error": "Missing or invalid decision (accepted/rejected)."}), 400

        if not feedback_text:
            return jsonify({"error": "Please provide feedback explaining why you are accepting or rejecting this suggestion."}), 400

        try:
            resp = requests.post(
                f"{db_url}/suggestion",
                json={"suggestion": suggestion_text, "accepted": accepted},
            )
            resp.raise_for_status()
            saved_suggestion = resp.json(object_hook=object_to_hook)
            suggestion_id = (
                getattr(saved_suggestion, "id", None)
                if hasattr(saved_suggestion, "id")
                else resp.json().get("id")
            )
            if suggestion_id is None:
                raise ValueError("Suggestion response did not include an id")

            categories = fetch_categories(tx_url)
            category_id, timeframe = classify_feedback(feedback_text, categories)
            feedback_payload = {
                "feedback": feedback_text,
                "suggestion_id": suggestion_id,
                "category_id": category_id,
                "timeframe": timeframe,
            }

            try:
                feedback_response = requests.post(
                    f"{db_url}/feedback",
                    json=feedback_payload,
                )
                feedback_response.raise_for_status()
            except Exception:
                cleanup_response = requests.delete(f"{db_url}/suggestion/{suggestion_id}")
                cleanup_response.raise_for_status()
                raise
        except Exception as e:
            app.logger.error(f"Error saving suggestion action: {e}")
            return jsonify({"error": f"Failed to save suggestion decision: {str(e)}"}), 500

        return make_response(
            render_template(
                "ai-suggestion.jinja",
                suggestion=None,
                sources=None,
                confidence=None,
                loading=True,
            ),
            200,
            {"HX-Trigger": "suggestionChanged, feedbackChanged"},
        )

    return app

if __name__ == "__main__":
    app = setup_app(
        os.environ.get("DB_URL", "http://localhost:6002"),
        os.environ.get("TRANSACTIONS_DB_URL", "http://localhost:6001"),
    )
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)))
