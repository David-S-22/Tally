from flask import abort
from flask import jsonify
from flask import request
from .models import Goal, Feedback, Suggestion, db
from .helpers import try_parse_bool
from .seed import seed_database_if_empty
from flask import Flask
import os
import datetime
from pathlib import Path

def setup_app(database_path) -> Flask:
    app = Flask(__name__)
    if database_path and database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{database_path}"
    db.init_app(app)
    with app.app_context():
        db.create_all()
    
    @app.route("/")
    def home():
        return "<p>Savings Database</p>", 200

    @app.route("/goals")
    def get_goals():
        query = db.select(Goal)
        active_only = request.args.get("active_only")
        if active_only and active_only.lower() in ("true", "1"):
            today = datetime.datetime.now()
            query = query.where(Goal.date >= today).order_by(Goal.date.asc())

        top = request.args.get("top") or request.args.get("limit")
        if top:
            try:
                query = query.limit(int(top))
            except (ValueError, TypeError):
                pass

        goals = db.session.execute(query).scalars().all()
        return jsonify([goal.to_dto() for goal in goals])

    @app.route("/suggestions")
    def get_suggestions():
        suggestions = db.session.execute(db.select(Suggestion)).scalars().all()
        return jsonify([suggestion.to_dto() for suggestion in suggestions])

    @app.route("/feedbacks")
    def get_feedbacks():
        feedbacks = db.session.execute(db.select(Feedback)).scalars().all()
        return jsonify([feedback.to_dto() for feedback in feedbacks])

    @app.route("/goal/<int:id>")
    def get_goal(id: int):
        return jsonify(db.get_or_404(Goal, id).to_dto())

    @app.route("/suggestion/<int:id>")
    def get_suggestion(id: int):
        return jsonify(db.get_or_404(Suggestion, id).to_dto())

    @app.route("/feedback/<int:id>")
    def get_feedback(id: int):
        return jsonify(db.get_or_404(Feedback, id).to_dto())

    @app.route("/goal", methods=["POST"])
    def add_goal():
        payload = request.get_json(silent=True) or {}

        if "name" not in payload:
            return jsonify({"error": "Missing goal field: name"}), 400
        if "cost" not in payload:
            return jsonify({"error": "Missing goal field: amount"}), 400
        if "date" not in payload:
            return jsonify({"error": "Missing goal field: date"}), 400

        try:
            cost = int(payload["cost"])
            goal_date = datetime.datetime.fromisoformat(payload["date"])
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid goal cost or date"}), 400

        goal = Goal(name=payload["name"], cost=cost, date=goal_date)

        db.session.add(goal)
        db.session.commit()
        return jsonify(goal.to_dto()), 201


    @app.route("/suggestion", methods=["POST"])
    def add_suggestion():
        payload = request.get_json(silent=True) or {}

        if not payload or "suggestion" not in payload:
            return jsonify({"error": "Missing suggestion field"}), 400

        accepted_val = try_parse_bool(payload.get("accepted"))
        if accepted_val is None:
            return jsonify({"error": "Missing accepted field"}), 400

        suggestion = Suggestion(
            suggestion=payload["suggestion"],
            accepted=accepted_val,
        )
        db.session.add(suggestion)
        db.session.commit()
        return jsonify(suggestion.to_dto()), 201


    @app.route("/feedback", methods=["POST"])
    def add_feedback():
        payload = request.get_json(silent=True) or {}

        if "feedback" not in payload:
            return jsonify({"error": "Missing feedback field"}), 400

        suggestion_id = payload.get("suggestion_id")
        if suggestion_id is not None:
            try:
                suggestion_id = int(suggestion_id)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid suggestion_id"}), 400
            suggestion = db.session.get(Suggestion, suggestion_id)
            if not suggestion:
                return abort(404)

        category_id = payload.get("category_id")
        if category_id is not None:
            try:
                category_id = int(category_id)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid category_id"}), 400

        timeframe = payload.get("timeframe")
        if timeframe is not None:
            timeframe = str(timeframe).strip() or None

        feedback = Feedback(
            feedback=payload["feedback"],
            suggestion_id=suggestion_id,
            category_id=category_id,
            timeframe=timeframe,
        )
        db.session.add(feedback)
        db.session.commit()
        return jsonify(feedback.to_dto()), 201


    @app.route("/feedback/<int:id>", methods=["PATCH"])
    def update_feedback(id: int):
        feedback_to_update = db.session.get(Feedback, id)
        if not feedback_to_update:
            return abort(404)

        updated_feedback = request.get_json() or {}
        if "feedback" not in updated_feedback and "category_id" not in updated_feedback and "timeframe" not in updated_feedback:
            return jsonify({"error": "Missing feedback field"}), 400

        new_feedback = updated_feedback.get("feedback", feedback_to_update.feedback)

        new_category_id = feedback_to_update.category_id
        if "category_id" in updated_feedback:
            raw_category = updated_feedback["category_id"]
            if raw_category is not None:
                try:
                    new_category_id = int(raw_category)
                except (ValueError, TypeError):
                    return jsonify({"error": "Invalid category_id"}), 400
            else:
                new_category_id = None

        new_timeframe = feedback_to_update.timeframe
        if "timeframe" in updated_feedback:
            raw_timeframe = updated_feedback["timeframe"]
            new_timeframe = str(raw_timeframe).strip() or None if raw_timeframe is not None else None

        feedback_to_update.feedback = new_feedback
        feedback_to_update.category_id = new_category_id
        feedback_to_update.timeframe = new_timeframe
        db.session.commit()
        return jsonify(feedback_to_update.to_dto()), 200


    @app.route("/suggestion/<int:id>", methods=["PATCH"])
    def update_suggestion(id: int):
        suggestion_to_update = db.session.get(Suggestion, id)
        if not suggestion_to_update:
            return abort(404)

        updated_suggestion = request.get_json() or {}
        if "suggestion" not in updated_suggestion and "accepted" not in updated_suggestion:
            return jsonify({"error": "Missing suggestion or accepted field"}), 400

        if "suggestion" in updated_suggestion:
            suggestion_to_update.suggestion = updated_suggestion["suggestion"]

        if "accepted" in updated_suggestion:
            accepted_val = try_parse_bool(updated_suggestion["accepted"])
            if accepted_val is None:
                return jsonify({"error": "Invalid accepted field"}), 400
            suggestion_to_update.accepted = accepted_val

        db.session.commit()
        return jsonify(suggestion_to_update.to_dto()), 200


    @app.route("/goal/<int:id>", methods=["PATCH"])
    def update_goal(id: int):
        goal_to_update = db.session.get(Goal, id)
        if not goal_to_update:
            return abort(404)

        updated_goal = request.get_json(silent=True) or {}
        if "name" not in updated_goal and "amount" not in updated_goal and "date" not in updated_goal and "cost" not in updated_goal:
            return jsonify({"error": "No valid fields provided"}), 400

        new_cost = goal_to_update.cost
        if "cost" in updated_goal:
            try:
                new_cost = int(updated_goal["cost"])
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid goal cost"}), 400

        new_date = goal_to_update.date
        if "date" in updated_goal:
            try:
                new_date = datetime.datetime.fromisoformat(updated_goal["date"])
            except (TypeError, ValueError):
                return jsonify({"error": "Invalid goal date"}), 400

        if "name" in updated_goal:
            goal_to_update.name = updated_goal["name"]
        goal_to_update.cost = new_cost
        goal_to_update.date = new_date

        db.session.commit()
        return jsonify(goal_to_update.to_dto()), 200


    @app.route("/goal/<int:id>", methods=["DELETE"])
    def delete_goal(id: int):
        goal = db.session.get(Goal, id)
        if goal is None:
            return abort(404)

        db.session.delete(goal)
        db.session.commit()
        return "", 204


    @app.route("/suggestion/<int:id>", methods=["DELETE"])
    def delete_suggestion(id: int):
        suggestion = db.session.get(Suggestion, id)
        if suggestion is None:
            return abort(404)

        db.session.delete(suggestion)
        db.session.commit()
        return "", 204


    @app.route("/feedback/<int:id>", methods=["DELETE"])
    def delete_feedback_by_id(id: int):
        feedback = db.session.get(Feedback, id)
        if feedback is None:
            return abort(404)

        db.session.delete(feedback)
        db.session.commit()
        return "", 204


    @app.route("/feedbacks", methods=["DELETE"])
    def delete_feedbacks_by_category():
        category_id = request.args.get("category_id")
        if category_id is not None:
            try:
                cat_id_int = int(category_id)
            except (ValueError, TypeError):
                return jsonify({"error": "Invalid category_id"}), 400
            db.session.execute(
                db.delete(Feedback).where(Feedback.category_id == cat_id_int)
            )
            db.session.commit()
            return "", 204
        return jsonify({"error": "No filter provided"}), 400

    return app

if __name__ == "__main__":
    db_path = os.environ.get("DB_PATH", "savings.db")
    app = setup_app(db_path)
    with app.app_context():
        seed_database_if_empty()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 6002)))
