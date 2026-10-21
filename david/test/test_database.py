from database.models import Suggestion
from typing import List
from typing import List
from database.models import Feedback
from datetime import datetime, timedelta
from dateutil import parser
from flask.testing import FlaskClient
from flask import Flask
from database.app import setup_app, db
import pytest
from database.models import Goal
from dataclasses import asdict
from json import dumps
from database.seed import seed_database_if_empty, get_goals, get_suggestions, get_feedbacks

@pytest.fixture()
def app():
    app = setup_app(":memory:")
    app.config.update({"TESTING": True})
    yield app

@pytest.fixture()
def client(app: Flask):
    return app.test_client()

@pytest.fixture()
def app_ctx(app: Flask):
    with app.app_context():
        yield
    
def setup_goals() -> List[Goal]:
    goal1 = Goal()
    goal1.name = "A single dollary doo"
    goal1.cost = 1
    goal1.date = datetime(2026, 8, 25)
    db.session.add(goal1)

    goal2 = Goal()
    goal2.name = "Large Macas Frozen Coke"
    goal2.cost = 1
    goal2.date = datetime(2026, 8, 25)
    db.session.add(goal2)

    db.session.commit()
    return [goal1, goal2]

def setup_feedback() -> List[Feedback]:
    feedback1 = Feedback()
    feedback1.feedback = "I can't do this :("
    db.session.add(feedback1)

    feedback2 = Feedback()
    feedback2.feedback = "This doesn't make sense"
    db.session.add(feedback2)

    db.session.commit()
    return [feedback1, feedback2]
def setup_suggestions() -> List[Suggestion]:
    suggestion1 = Suggestion()
    suggestion1.suggestion = "I can't do this :("
    suggestion1.accepted = True
    db.session.add(suggestion1)

    suggestion2 = Suggestion()
    suggestion2.suggestion = "I can't do this :("
    suggestion2.accepted = False
    db.session.add(suggestion2)

    db.session.commit()
    return [suggestion1, suggestion2]

@pytest.mark.usefixtures("app_ctx")
def test_get_goals_endpoint_returns_saved_goals(client: FlaskClient):
    expected_goals = setup_goals()
    response = client.get("/goals")
    assert response.status_code == 200
    response_json = response.get_json()
    assert len(response_json) == 2
    assert expected_goals[0].cost == response_json[0]["cost"]
    assert expected_goals[0].name == response_json[0]["name"]
    assert expected_goals[0].date == parser.parse(response_json[0]["date"]).replace(tzinfo=None)

    assert expected_goals[1].cost == response_json[1]["cost"]
    assert expected_goals[1].name == response_json[1]["name"]
    assert expected_goals[1].date == parser.parse(response_json[1]["date"]).replace(tzinfo=None)


@pytest.mark.usefixtures("app_ctx")
def test_get_goals_active_only(client: FlaskClient):
    now = datetime.now()
    past_goal = Goal(name="Past Goal", cost=50, date=now - timedelta(days=10))
    near_goal = Goal(name="Near Goal", cost=150, date=now + timedelta(days=5))
    far_goal = Goal(name="Far Goal", cost=250, date=now + timedelta(days=20))
    db.session.add_all([past_goal, near_goal, far_goal])
    db.session.commit()

    response = client.get("/goals?active_only=true")
    assert response.status_code == 200
    data = response.get_json()
    names = [g["name"] for g in data]
    assert "Past Goal" not in names
    assert names == ["Near Goal", "Far Goal"]


@pytest.mark.usefixtures("app_ctx")
def test_get_goals_top_condition(client: FlaskClient):
    now = datetime.now()
    past_goal = Goal(name="Past Goal", cost=50, date=now - timedelta(days=10))
    near_goal = Goal(name="Near Goal", cost=150, date=now + timedelta(days=5))
    far_goal = Goal(name="Far Goal", cost=250, date=now + timedelta(days=20))
    db.session.add_all([past_goal, near_goal, far_goal])
    db.session.commit()

    response = client.get("/goals?active_only=true&top=1")
    assert response.status_code == 200
    data = response.get_json()
    assert len(data) == 1
    assert data[0]["name"] == "Near Goal"


@pytest.mark.usefixtures("app_ctx")
def test_get_all_feedbacks(client: FlaskClient):
    expected_feedback = setup_feedback()
    response = client.get("/feedbacks")
    assert response.status_code == 200
    response_json = response.get_json()
    assert len(response_json) == 2
    assert expected_feedback[0].feedback == response_json[0]["feedback"]
   
    assert expected_feedback[1].feedback == response_json[1]["feedback"]
  
@pytest.mark.usefixtures("app_ctx")
def test_get_all_suggestions(client: FlaskClient):
    expected_suggestions = setup_suggestions()
    response = client.get("/suggestions")
    assert response.status_code == 200
    response_json = response.get_json()
    assert len(response_json) == 2
    assert expected_suggestions[0].suggestion == response_json[0]["suggestion"]
    assert expected_suggestions[0].accepted == response_json[0]["accepted"]

    assert expected_suggestions[1].suggestion == response_json[1]["suggestion"]
    assert expected_suggestions[1].accepted == response_json[1]["accepted"]

@pytest.mark.usefixtures("app_ctx")
def test_get_specific_goals(client: FlaskClient):
    expected_goals = setup_goals()
    response = client.get("/goal/1")
    assert response.status_code == 200
    response_json = response.get_json()
    assert expected_goals[0].cost == response_json["cost"]
    assert expected_goals[0].name == response_json["name"]
    assert expected_goals[0].date == parser.parse(response_json["date"]).replace(tzinfo=None)

@pytest.mark.usefixtures("app_ctx")
def test_get_specific_suggestion(client: FlaskClient):
    expected_suggestion = setup_suggestions()
    response = client.get("/suggestion/1")
    assert response.status_code == 200
    response_json = response.get_json()
    assert expected_suggestion[0].suggestion == response_json["suggestion"]
    assert expected_suggestion[0].accepted == response_json["accepted"]

@pytest.mark.usefixtures("app_ctx")
def test_get_specific_feedback(client: FlaskClient):
    expected_feedback = setup_feedback()
    response = client.get("/feedback/1")
    assert response.status_code == 200
    response_json = response.get_json()
    assert expected_feedback[0].feedback == response_json["feedback"]


@pytest.mark.usefixtures("app_ctx")
def test_delete_goal(client: FlaskClient):
    goals = setup_goals()
    response = client.delete(f"/goal/{goals[0].id}")
    assert response.status_code == 204
    remaining_goals = db.session.execute(db.select(Goal)).scalars().all()
    assert len(remaining_goals) == 1
    assert remaining_goals[0].id == goals[1].id

@pytest.mark.usefixtures("app_ctx")
def test_delete_suggestion(client: FlaskClient):
    suggestions = setup_suggestions()
    response = client.delete(f"/suggestion/{suggestions[0].id}")
    assert response.status_code == 204
    remaining_suggestions = db.session.execute(db.select(Suggestion)).scalars().all()
    assert len(remaining_suggestions) == 1
    assert remaining_suggestions[0].id == suggestions[1].id

@pytest.mark.usefixtures("app_ctx")
def test_delete_feedback(client: FlaskClient):
    feedbacks = setup_feedback()
    response = client.delete(f"/feedback/{feedbacks[0].id}")
    assert response.status_code == 204
    remaining_feedbacks = db.session.execute(db.select(Feedback)).scalars().all()
    assert len(remaining_feedbacks) == 1
    assert remaining_feedbacks[0].id == feedbacks[1].id

@pytest.mark.usefixtures("app_ctx")
def test_update_goal(client: FlaskClient):
    goals = setup_goals()
    new_name = "two schmickles"
    goals[0].name = new_name
    new_cost = 2
    goals[0].cost = new_cost
    new_date = datetime(2025, 6, 7);
    goals[0].date = new_date
    updated_goal_json = dumps(asdict(goals[0]), default=str)
    headers = {"Content-type": "application/json"}
    response = client.patch(f"/goal/{goals[0].id}", data=updated_goal_json, content_type="application/json")
    assert response.status_code == 200

    updated_goal = db.session.execute(db.select(Goal).where(Goal.id == goals[0].id)).scalar_one()
    assert updated_goal.name == new_name
    assert updated_goal.cost == new_cost
    assert updated_goal.date == new_date

@pytest.mark.usefixtures("app_ctx")
def test_update_suggestion(client: FlaskClient):
    suggestions = setup_suggestions()
    new_suggestion = "two schmickles"
    suggestions[0].suggestion = new_suggestion
    suggestions[0].accepted = False
    updated_suggestion_json = dumps(asdict(suggestions[0]), default=str)
    response = client.patch(f"/suggestion/{suggestions[0].id}", data=updated_suggestion_json, content_type="application/json")
    assert response.status_code == 200

    updated_suggestion = db.session.execute(db.select(Suggestion).where(Suggestion.id == suggestions[0].id)).scalar_one()
    assert updated_suggestion.suggestion == new_suggestion
    assert updated_suggestion.accepted is False

@pytest.mark.usefixtures("app_ctx")
def test_update_suggestion_partial_update(client: FlaskClient):
    suggestions = setup_suggestions()
    response = client.patch(f"/suggestion/{suggestions[0].id}", json={"accepted": False})
    assert response.status_code == 200

    updated_suggestion = db.session.execute(db.select(Suggestion).where(Suggestion.id == suggestions[0].id)).scalar_one()
    assert updated_suggestion.accepted is False
    assert updated_suggestion.suggestion == suggestions[0].suggestion

@pytest.mark.usefixtures("app_ctx")
def test_update_feedback(client: FlaskClient):
    feedbacks = setup_feedback()
    new_feedback = "two schmickles"
    feedbacks[0].feedback = new_feedback
    updated_feedback_json = dumps(asdict(feedbacks[0]), default=str)
    response = client.patch(f"/feedback/{feedbacks[0].id}", data=updated_feedback_json, content_type="application/json")
    assert response.status_code == 200

    updated_feedback = db.session.execute(db.select(Feedback).where(Feedback.id == feedbacks[0].id)).scalar_one()
    assert updated_feedback.feedback == new_feedback

@pytest.mark.usefixtures("app_ctx")
def test_update_goal_partial_update(client: FlaskClient):
    goals = setup_goals()
    new_cost = 5
    goals[1].cost = new_cost
    updated_goal_json = dumps(asdict(goals[1]), default=str)
    response = client.patch(f"/goal/{goals[1].id}", data=updated_goal_json, content_type="application/json")
    assert response.status_code == 200

    updated_goal = db.session.execute(db.select(Goal).where(Goal.id == goals[1].id)).scalar_one()
    assert updated_goal.cost == new_cost
    assert updated_goal.name == goals[1].name
    assert updated_goal.date == goals[1].date

@pytest.mark.usefixtures("app_ctx")
def test_update_goal_rejects_invalid_messages(client: FlaskClient):
    goals = setup_goals()
    response = client.patch(f"/goal/{goals[0].id}", data="", content_type="application/json")
    assert response.status_code == 400

@pytest.mark.usefixtures("app_ctx")
@pytest.mark.parametrize(
    "path",
    [
        "/goal",
        "/suggestion",
        "/feedback",
    ],
)
def test_create_endpoints_reject_malformed_json(client: FlaskClient, path):
    response = client.post(path, data="{not-json", content_type="application/json")
    assert response.status_code == 400

    if path == "/goal":
        assert db.session.execute(db.select(Goal)).scalars().all() == []
    elif path == "/suggestion":
        assert db.session.execute(db.select(Suggestion)).scalars().all() == []
    else:
        assert db.session.execute(db.select(Feedback)).scalars().all() == []

@pytest.mark.usefixtures("app_ctx")
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Invalid cost", "cost": "not-an-int", "date": "2026-12-01T00:00:00"},
        {"name": "Invalid date", "cost": 100, "date": "not-a-date"},
    ],
)
def test_create_goal_rejects_invalid_values_without_persisting(client: FlaskClient, payload):
    response = client.post("/goal", json=payload)
    assert response.status_code == 400
    assert db.session.execute(db.select(Goal)).scalars().all() == []

@pytest.mark.usefixtures("app_ctx")
def test_update_suggestion_rejects_invalid_messages(client: FlaskClient):
    suggestions = setup_suggestions()
    response = client.patch(f"/suggestion/{suggestions[0].id}", data="", content_type="application/json")
    assert response.status_code == 400

@pytest.mark.usefixtures("app_ctx")
def test_update_feedback_rejects_invalid_messages(client: FlaskClient):
    feedbacks = setup_feedback()
    response = client.patch(f"/feedback/{feedbacks[0].id}", data="", content_type="application/json")
    assert response.status_code == 400

@pytest.mark.usefixtures("app_ctx")
def test_update_feedback_invalid_category_is_atomic(client: FlaskClient):
    feedback = setup_feedback()[0]
    original = (feedback.feedback, feedback.category_id, feedback.timeframe)

    response = client.patch(
        f"/feedback/{feedback.id}",
        json={"feedback": "Updated text", "category_id": "invalid"},
    )

    assert response.status_code == 400
    persisted = db.session.get(Feedback, feedback.id)
    assert (persisted.feedback, persisted.category_id, persisted.timeframe) == original

@pytest.mark.usefixtures("app_ctx")
def test_update_goal_invalid_values_are_atomic(client: FlaskClient):
    goal = setup_goals()[0]
    original = (goal.name, goal.cost, goal.date)

    response = client.patch(
        f"/goal/{goal.id}",
        json={"name": "Updated name", "cost": "invalid"},
    )

    assert response.status_code == 400
    persisted = db.session.get(Goal, goal.id)
    assert (persisted.name, persisted.cost, persisted.date) == original

@pytest.mark.usefixtures("app_ctx")
def test_create_goal_endpoint_persists_goal(client: FlaskClient):
    goal_to_create = Goal()
    goal_to_create.cost = 5
    goal_to_create.name = "Borgr"
    goal_to_create.date = datetime(2022, 12, 31)
    updated_goal_json = dumps(asdict(goal_to_create), default=str)
    response = client.post("/goal", content_type="application/json", data=updated_goal_json)
    assert response.status_code == 201

    new_goal = db.session.execute(db.select(Goal)).scalar_one()
    assert new_goal.name == goal_to_create.name
    assert new_goal.cost == goal_to_create.cost
    assert new_goal.date == goal_to_create.date

@pytest.mark.usefixtures("app_ctx")
def test_create_suggestion(client: FlaskClient):
    suggestion_to_create = Suggestion()
    suggestion_to_create.suggestion = "Borgr"
    suggestion_to_create.accepted = True
    updated_suggestion_json = dumps(asdict(suggestion_to_create), default=str)
    response = client.post("/suggestion", content_type="application/json", data=updated_suggestion_json)
    assert response.status_code == 201

    new_suggestion = db.session.execute(db.select(Suggestion)).scalar_one()
    assert new_suggestion.suggestion == suggestion_to_create.suggestion
    assert new_suggestion.accepted == suggestion_to_create.accepted

@pytest.mark.usefixtures("app_ctx")
def test_create_suggestion_missing_accepted(client: FlaskClient):
    response = client.post("/suggestion", content_type="application/json", data=dumps({"suggestion": "Borgr"}))
    assert response.status_code == 400

@pytest.mark.usefixtures("app_ctx")
def test_create_feedback(client: FlaskClient):
    feedback_to_create = Feedback()
    feedback_to_create.feedback = "Borgr"
    updated_feedback_json = dumps(asdict(feedback_to_create), default=str)
    response = client.post("/feedback", content_type="application/json", data=updated_feedback_json)
    assert response.status_code == 201

    new_feedback = db.session.execute(db.select(Feedback)).scalar_one()
    assert new_feedback.feedback == feedback_to_create.feedback

def test_goal_to_dto():
    now = datetime(2026, 8, 25)
    goal = Goal(id=1, name="Goal 1", cost=100, date=now)
    dto_obj = goal.to_dto()
    assert dto_obj.id == 1
    assert dto_obj.name == "Goal 1"
    assert dto_obj.cost == 100
    assert dto_obj.date == now
    assert goal.to_dto() == dto_obj

def test_suggestion_to_dto():
    suggestion = Suggestion(id=2, suggestion="A suggestion", accepted=True)
    dto_obj = suggestion.to_dto()
    assert dto_obj.id == 2
    assert dto_obj.suggestion == "A suggestion"
    assert dto_obj.accepted is True
    assert suggestion.to_dto() == dto_obj

def test_feedback_to_dto():
    feedback = Feedback(id=3, feedback="A feedback")
    dto_obj = feedback.to_dto()
    assert dto_obj.id == 3
    assert dto_obj.feedback == "A feedback"
    assert dto_obj.suggestion_id is None
    assert feedback.to_dto() == dto_obj

    feedback_linked = Feedback(id=4, feedback="Linked feedback", suggestion_id=10)
    dto_obj_linked = feedback_linked.to_dto()
    assert dto_obj_linked.id == 4
    assert dto_obj_linked.feedback == "Linked feedback"
    assert dto_obj_linked.suggestion_id == 10
    assert feedback_linked.to_dto() == dto_obj_linked

@pytest.mark.usefixtures("app_ctx")
def test_seed_database_if_empty():
    test_app = setup_app(":memory:")
    assert len(db.session.execute(db.select(Goal)).scalars().all()) == 0

    expected_goals = get_goals()
    expected_suggestions = get_suggestions()
    expected_feedbacks = get_feedbacks()

    seed_database_if_empty()

    goals = db.session.execute(db.select(Goal)).scalars().all()
    suggestions = db.session.execute(db.select(Suggestion)).scalars().all()
    feedbacks = db.session.execute(db.select(Feedback)).scalars().all()

    assert len(goals) == len(expected_goals)
    assert len(suggestions) == len(expected_suggestions)
    assert len(feedbacks) == len(expected_feedbacks)

    seed_database_if_empty()
    assert len(db.session.execute(db.select(Goal)).scalars().all()) == len(expected_goals)
    assert len(db.session.execute(db.select(Suggestion)).scalars().all()) == len(expected_suggestions)
    assert len(db.session.execute(db.select(Feedback)).scalars().all()) == len(expected_feedbacks)


@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_with_valid_suggestion_id(client: FlaskClient):
    suggestions = setup_suggestions()
    suggestion_id = suggestions[0].id
    response = client.post(
        "/feedback",
        content_type="application/json",
        data=dumps({"feedback": "Linked feedback text", "suggestion_id": suggestion_id}),
    )
    assert response.status_code == 201
    res_json = response.get_json()
    assert res_json["feedback"] == "Linked feedback text"
    assert res_json["suggestion_id"] == suggestion_id

    created_feedback = db.session.execute(db.select(Feedback).where(Feedback.id == res_json["id"])).scalar_one()
    assert created_feedback.feedback == "Linked feedback text"
    assert created_feedback.suggestion_id == suggestion_id
    assert created_feedback.suggestion.id == suggestion_id


@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_with_nonexistent_suggestion_id(client: FlaskClient):
    response = client.post(
        "/feedback",
        content_type="application/json",
        data=dumps({"feedback": "Invalid link", "suggestion_id": 99999}),
    )
    assert response.status_code == 404
    feedbacks = db.session.execute(db.select(Feedback)).scalars().all()
    assert len(feedbacks) == 0


@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_with_invalid_suggestion_id_type(client: FlaskClient):
    response = client.post(
        "/feedback",
        content_type="application/json",
        data=dumps({"feedback": "Invalid link", "suggestion_id": "not-an-int"}),
    )
    assert response.status_code == 400


@pytest.mark.usefixtures("app_ctx")
def test_delete_suggestion_cascades_to_linked_feedback(client: FlaskClient):
    suggestions = setup_suggestions()
    s1, s2 = suggestions[0], suggestions[1]

    # Linked feedback for suggestion 1
    fb_linked1 = Feedback(feedback="Reason for suggestion 1", suggestion_id=s1.id)
    # Linked feedback for suggestion 2
    fb_linked2 = Feedback(feedback="Reason for suggestion 2", suggestion_id=s2.id)
    # General feedback (unlinked)
    fb_general = Feedback(feedback="General preference", suggestion_id=None)

    db.session.add_all([fb_linked1, fb_linked2, fb_general])
    db.session.commit()

    fb1_id = fb_linked1.id
    fb2_id = fb_linked2.id
    fb_gen_id = fb_general.id

    # Delete suggestion 1
    response = client.delete(f"/suggestion/{s1.id}")
    assert response.status_code == 204

    # Verify suggestion 1 is deleted
    assert db.session.get(Suggestion, s1.id) is None

    # Verify linked feedback 1 is cascade deleted
    assert db.session.get(Feedback, fb1_id) is None

    # Verify suggestion 2 and its linked feedback 2 remain intact
    assert db.session.get(Suggestion, s2.id) is not None
    persisted_fb2 = db.session.get(Feedback, fb2_id)
    assert persisted_fb2 is not None
    assert persisted_fb2.suggestion_id == s2.id

    # Verify general feedback remains intact
    persisted_general = db.session.get(Feedback, fb_gen_id)
    assert persisted_general is not None
    assert persisted_general.suggestion_id is None


@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_with_category_and_timeframe(client: FlaskClient):
    response = client.post(
        "/feedback",
        content_type="application/json",
        data=dumps({"feedback": "Cut dining costs", "category_id": 80, "timeframe": "2 weeks"}),
    )
    assert response.status_code == 201
    res = response.get_json()
    assert res["category_id"] == 80
    assert res["timeframe"] == "2 weeks"

    saved = db.session.get(Feedback, res["id"])
    assert saved.category_id == 80
    assert saved.timeframe == "2 weeks"


@pytest.mark.usefixtures("app_ctx")
def test_create_feedback_with_invalid_category_id(client: FlaskClient):
    response = client.post(
        "/feedback",
        content_type="application/json",
        data=dumps({"feedback": "Cut dining costs", "category_id": "not-an-int"}),
    )
    assert response.status_code == 400


@pytest.mark.usefixtures("app_ctx")
def test_update_feedback_partial_category_and_timeframe(client: FlaskClient):
    feedbacks = setup_feedback()
    fb_id = feedbacks[0].id
    orig_text = feedbacks[0].feedback

    # Update only category_id
    response = client.patch(f"/feedback/{fb_id}", json={"category_id": 70})
    assert response.status_code == 200
    res = response.get_json()
    assert res["category_id"] == 70
    assert res["feedback"] == orig_text  # Text must NOT be wiped

    # Update only timeframe
    response = client.patch(f"/feedback/{fb_id}", json={"timeframe": "1 month"})
    assert response.status_code == 200
    res = response.get_json()
    assert res["timeframe"] == "1 month"
    assert res["category_id"] == 70
    assert res["feedback"] == orig_text

    # Update timeframe to empty string normalizes to None
    response = client.patch(f"/feedback/{fb_id}", json={"timeframe": ""})
    assert response.status_code == 200
    res = response.get_json()
    assert res["timeframe"] is None

    # Invalid category_id returns 400
    response = client.patch(f"/feedback/{fb_id}", json={"category_id": "invalid"})
    assert response.status_code == 400


@pytest.mark.usefixtures("app_ctx")
def test_delete_feedbacks_by_category(client: FlaskClient):
    fb1 = Feedback(feedback="Transport focus", category_id=70)
    fb2 = Feedback(feedback="Another transport", category_id=70)
    fb3 = Feedback(feedback="Dining focus", category_id=80)
    db.session.add_all([fb1, fb2, fb3])
    db.session.commit()
    id1, id2, id3 = fb1.id, fb2.id, fb3.id

    # Invalid category_id returns 400
    response = client.delete("/feedbacks?category_id=abc")
    assert response.status_code == 400

    # Valid category_id deletes only matching
    response = client.delete("/feedbacks?category_id=70")
    assert response.status_code == 204

    assert db.session.get(Feedback, id1) is None
    assert db.session.get(Feedback, id2) is None
    assert db.session.get(Feedback, id3) is not None
