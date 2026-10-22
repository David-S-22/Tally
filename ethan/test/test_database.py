import pytest

from database import app as database_app
from database.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database_app, "_current_budget_month", lambda: "2026-10")
    db_path = str(tmp_path / "budgets.db")
    app = create_app(db_path, seed_demo_data=False)
    app.config["TESTING"] = True
    return app.test_client()


def test_index(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert isinstance(resp.json, dict)
    assert resp.json["container"] == "budgets-db"


def test_budget_crud_round_trip(client):
    created = client.post(
        "/budgets",
        json={
            "month": "2026-09",
            "declared_income": 500000,
            "status": "draft",
        },
    )
    assert created.status_code == 201
    budget = created.get_json()
    assert isinstance(budget["id"], int)
    assert budget["month"] == "2026-09"
    assert budget["declared_income"] == 500000
    assert budget["status"] == "closed"

    fetched = client.get(f"/budgets/{budget['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["id"] == budget["id"]

    by_month = client.get("/budgets/by-month/2026-09")
    assert by_month.status_code == 200
    assert by_month.get_json()["id"] == budget["id"]

    updated = client.patch(
        f"/budgets/{budget['id']}",
        json={"status": "active", "declared_income": 550000},
    )
    assert updated.status_code == 200
    assert updated.get_json()["status"] == "closed"
    assert updated.get_json()["declared_income"] == 550000


def test_budget_statuses_follow_current_month(client):
    september = client.post("/budgets", json={"month": "2026-09", "declared_income": 500000}).get_json()
    october = client.post("/budgets", json={"month": "2026-10", "declared_income": 510000}).get_json()
    november = client.post("/budgets", json={"month": "2026-11", "declared_income": 520000}).get_json()

    assert september["status"] == "closed"
    assert october["status"] == "active"
    assert november["status"] == "draft"

    listed = client.get("/budgets")
    assert listed.status_code == 200
    by_month = {budget["month"]: budget["status"] for budget in listed.get_json()}
    assert by_month["2026-09"] == "closed"
    assert by_month["2026-10"] == "active"
    assert by_month["2026-11"] == "draft"


def test_seeded_budgets_recompute_statuses_from_current_month(tmp_path, monkeypatch):
    monkeypatch.setattr(database_app, "_current_budget_month", lambda: "2026-10")
    db_path = str(tmp_path / "seeded-budgets.db")
    app = create_app(db_path, seed_demo_data=True)
    app.config["TESTING"] = True
    seeded_client = app.test_client()

    listed = seeded_client.get("/budgets")

    assert listed.status_code == 200
    by_month = {budget["month"]: budget["status"] for budget in listed.get_json()}
    assert by_month["2026-09"] == "closed"
    assert by_month["2026-10"] == "active"


def test_budget_line_round_trip_and_unique_category_per_budget(client):
    budget = client.post("/budgets", json={"month": "2026-10"}).get_json()

    created = client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category_id": 81, "category": "Groceries", "warn_at": 15000, "hard_cap": 20000},
    )
    assert created.status_code == 201
    line = created.get_json()
    assert isinstance(line["id"], int)
    assert line["category_id"] == 81
    assert line["category"] == "Groceries"

    fetched = client.get(f"/budget-lines/{line['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["budget_id"] == budget["id"]

    duplicate = client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category_id": 81, "category": "Groceries"},
    )
    assert duplicate.status_code == 409

    updated = client.patch(
        f"/budget-lines/{line['id']}",
        json={"warn_at": 17500, "hard_cap": 22500},
    )
    assert updated.status_code == 200
    assert updated.get_json()["warn_at"] == 17500
    assert updated.get_json()["hard_cap"] == 22500


def test_deleting_budget_line_removes_matching_planned_events(client):
    budget = client.post("/budgets", json={"month": "2026-10"}).get_json()
    line = client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category_id": 81, "category": "Groceries", "warn_at": 15000, "hard_cap": 20000},
    ).get_json()
    matching_event = client.post(
        f"/budgets/{budget['id']}/planned-events",
        json={
            "date": "2026-10-04",
            "label": "Weekly shop",
            "category": "Groceries",
            "est_low": 4000,
            "est_high": 6000,
            "source": "user",
            "status": "planned",
        },
    ).get_json()
    other_line = client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category_id": 80, "category": "Dining", "warn_at": 12000, "hard_cap": 18000},
    ).get_json()
    other_event = client.post(
        f"/budgets/{budget['id']}/planned-events",
        json={
            "date": "2026-10-10",
            "label": "Dinner out",
            "category": "Dining",
            "est_low": 5000,
            "est_high": 7000,
            "source": "user",
            "status": "planned",
        },
    ).get_json()

    deleted = client.delete(f"/budget-lines/{line['id']}")

    assert deleted.status_code == 204
    assert client.get(f"/budget-lines/{line['id']}").status_code == 404
    assert client.get(f"/planned-events/{matching_event['id']}").status_code == 404
    assert client.get(f"/planned-events/{other_event['id']}").status_code == 200
    assert client.get(f"/budget-lines/{other_line['id']}").status_code == 200


def test_planned_event_requires_matching_budget_line_category(client):
    budget = client.post("/budgets", json={"month": "2026-11"}).get_json()
    client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category_id": 80, "category": "Dining", "warn_at": 8000, "hard_cap": 12000},
    )

    invalid = client.post(
        f"/budgets/{budget['id']}/planned-events",
        json={
            "date": "2026-11-04",
            "label": "Movie night",
            "category": "Entertainment",
            "est_low": 3000,
            "est_high": 5000,
            "source": "user",
            "status": "planned",
        },
    )
    assert invalid.status_code == 422

    created = client.post(
        f"/budgets/{budget['id']}/planned-events",
        json={
            "date": "2026-11-04",
            "label": "Dinner out",
            "category": "Dining",
            "est_low": 4000,
            "est_high": 6000,
            "source": "user",
            "status": "planned",
        },
    )
    assert created.status_code == 201
    planned_event = created.get_json()
    assert isinstance(planned_event["id"], int)
    assert planned_event["category"] == "Dining"

    updated = client.patch(
        f"/planned-events/{planned_event['id']}",
        json={"status": "confirmed"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["status"] == "confirmed"


def test_planned_event_date_must_stay_inside_budget_month(client):
    budget = client.post("/budgets", json={"month": "2026-11"}).get_json()
    client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category_id": 80, "category": "Dining", "warn_at": 8000, "hard_cap": 12000},
    )

    invalid_create = client.post(
        f"/budgets/{budget['id']}/planned-events",
        json={
            "date": "2026-12-04",
            "label": "Dinner out",
            "category": "Dining",
            "est_low": 4000,
            "est_high": 6000,
            "source": "user",
            "status": "planned",
        },
    )
    assert invalid_create.status_code == 422
    assert invalid_create.get_json()["code"] == "planned_event_month_mismatch"

    created = client.post(
        f"/budgets/{budget['id']}/planned-events",
        json={
            "date": "2026-11-04",
            "label": "Dinner out",
            "category": "Dining",
            "est_low": 4000,
            "est_high": 6000,
            "source": "user",
            "status": "planned",
        },
    )
    planned_event = created.get_json()

    invalid_update = client.patch(
        f"/planned-events/{planned_event['id']}",
        json={"date": "2026-10-30"},
    )
    assert invalid_update.status_code == 422
    assert invalid_update.get_json()["code"] == "planned_event_month_mismatch"


def test_coach_proposal_round_trip(client):
    budget = client.post("/budgets", json={"month": "2026-12"}).get_json()

    created = client.post(
        f"/budgets/{budget['id']}/coach-proposals",
        json={
            "proposal_json": {
                "proposal_type": "chat_edit",
                "operations": [
                    {
                        "action": "update_budget_line",
                        "fields": {"warn_at": 15000, "hard_cap": 20000},
                    }
                ],
            },
            "rationale": "Adjusted to reflect recent spending.",
        },
    )
    assert created.status_code == 201
    proposal = created.get_json()
    assert isinstance(proposal["id"], int)
    assert proposal["status"] == "proposed"
    assert proposal["proposal_json"]["proposal_type"] == "chat_edit"

    updated = client.patch(
        f"/coach-proposals/{proposal['id']}",
        json={"status": "rejected", "rejection_reason": "Too aggressive"},
    )
    assert updated.status_code == 200
    assert updated.get_json()["status"] == "rejected"
    assert updated.get_json()["rejection_reason"] == "Too aggressive"
    assert updated.get_json()["decided_at"] is not None


def test_chat_message_round_trip_and_reset(client):
    budget = client.post("/budgets", json={"month": "2026-12"}).get_json()
    proposal = client.post(
        f"/budgets/{budget['id']}/coach-proposals",
        json={"proposal_json": {"proposal_type": "chat_edit", "operations": []}},
    ).get_json()

    created_user = client.post(
        f"/budgets/{budget['id']}/chat-messages",
        json={"role": "user", "content": "How is Dining looking?"},
    )
    assert created_user.status_code == 201

    created_assistant = client.post(
        f"/budgets/{budget['id']}/chat-messages",
        json={
            "role": "assistant",
            "content": "Dining is under pressure this month.",
            "mode": "advice",
            "response_source": "rag",
            "plan_json": {"intent": "overspending"},
            "observation_json": {"month": "2026-12"},
            "stage_trace": ["observe", "plan", "act", "adapt"],
            "tool_result_json": {"tool_name": "search_transactions", "count": 2},
            "grounding_json": {
                "citations": [{"label": "Budget policy", "source": "budgets/policy.md", "distance": 0.4}],
                "confidence": "high",
                "insufficient_context": False,
            },
            "proposal_id": proposal["id"],
        },
    )
    assert created_assistant.status_code == 201

    listed = client.get(f"/budgets/{budget['id']}/chat-messages")
    assert listed.status_code == 200
    messages = listed.get_json()
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["proposal_id"] == proposal["id"]
    assert messages[1]["response_source"] == "rag"
    assert messages[1]["stage_trace"] == ["observe", "plan", "act", "adapt"]
    assert messages[1]["tool_result_json"] == {"tool_name": "search_transactions", "count": 2}
    assert messages[1]["grounding_json"]["confidence"] == "high"

    deleted = client.delete(f"/budgets/{budget['id']}/chat-messages")
    assert deleted.status_code == 204
    assert client.get(f"/budgets/{budget['id']}/chat-messages").get_json() == []


def test_deleting_budget_cascades_to_child_records(client):
    budget = client.post("/budgets", json={"month": "2027-01"}).get_json()
    line = client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category_id": 81, "category": "Groceries"},
    ).get_json()
    planned_event = client.post(
        f"/budgets/{budget['id']}/planned-events",
        json={"category": "Groceries", "status": "planned"},
    ).get_json()
    proposal = client.post(
        f"/budgets/{budget['id']}/coach-proposals",
        json={"proposal_json": {"proposal_type": "coach"}},
    ).get_json()
    client.post(
        f"/budgets/{budget['id']}/chat-messages",
        json={"role": "assistant", "content": "Stored history", "mode": "advice"},
    ).get_json()

    deleted = client.delete(f"/budgets/{budget['id']}")
    assert deleted.status_code == 204

    assert client.get(f"/budgets/{budget['id']}").status_code == 404
    assert client.get(f"/budget-lines/{line['id']}").status_code == 404
    assert client.get(f"/planned-events/{planned_event['id']}").status_code == 404
    assert client.get(f"/coach-proposals/{proposal['id']}").status_code == 404
    assert client.get(f"/budgets/{budget['id']}/chat-messages").status_code == 404


def test_startup_seeds_at_least_ten_rows_per_table(tmp_path):
    db_path = str(tmp_path / "seeded.db")
    client = create_app(db_path).test_client()

    budgets = client.get("/budgets").get_json()
    assert len(budgets) >= 10

    total_budget_lines = 0
    total_planned_events = 0
    total_coach_proposals = 0
    for budget in budgets:
        total_budget_lines += len(client.get(f"/budgets/{budget['id']}/budget-lines").get_json())
        total_planned_events += len(client.get(f"/budgets/{budget['id']}/planned-events").get_json())
        total_coach_proposals += len(client.get(f"/budgets/{budget['id']}/coach-proposals").get_json())

    assert total_budget_lines >= 10
    assert total_planned_events >= 10
    assert total_coach_proposals >= 10


def test_startup_seed_skips_database_that_already_contains_data(tmp_path):
    db_path = str(tmp_path / "existing.db")
    first_client = create_app(db_path, seed_demo_data=False).test_client()
    created_budget = first_client.post("/budgets", json={"month": "2027-02"}).get_json()

    seeded_client = create_app(db_path).test_client()
    budgets = seeded_client.get("/budgets").get_json()
    assert budgets == [seeded_client.get(f"/budgets/{created_budget['id']}").get_json()]
    assert seeded_client.get(f"/budgets/{created_budget['id']}/budget-lines").get_json() == []
    assert seeded_client.get(f"/budgets/{created_budget['id']}/planned-events").get_json() == []
    assert seeded_client.get(f"/budgets/{created_budget['id']}/coach-proposals").get_json() == []


def test_budget_line_requires_category_id_and_category_together(client):
    budget = client.post("/budgets", json={"month": "2027-03"}).get_json()

    missing_name = client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category_id": 81},
    )
    assert missing_name.status_code == 400

    missing_id = client.post(
        f"/budgets/{budget['id']}/budget-lines",
        json={"category": "Groceries"},
    )
    assert missing_id.status_code == 400


def test_seeded_rows_use_simple_integer_ids(tmp_path):
    db_path = str(tmp_path / "seeded-ids.db")
    client = create_app(db_path).test_client()

    budgets = client.get("/budgets").get_json()
    assert [budget["id"] for budget in budgets] == list(range(1, len(budgets) + 1))

    first_budget = budgets[0]
    budget_lines = client.get(f"/budgets/{first_budget['id']}/budget-lines").get_json()
    planned_events = client.get(f"/budgets/{first_budget['id']}/planned-events").get_json()
    coach_proposals = client.get(f"/budgets/{first_budget['id']}/coach-proposals").get_json()

    assert sorted(line["id"] for line in budget_lines) == list(range(1, len(budget_lines) + 1))
    assert planned_events[0]["id"] == 1
    assert coach_proposals[0]["id"] == 1


def test_admin_reset_restores_seed_data(tmp_path):
    app = create_app(str(tmp_path / "budgets.db"))
    app.config["TESTING"] = True
    client = app.test_client()
    seeded = client.get("/budgets").get_json()
    assert seeded
    for budget in seeded:
        assert client.delete(f"/budgets/{budget['id']}").status_code == 204
    assert client.get("/budgets").get_json() == []

    resp = client.post("/admin/reset")

    assert resp.status_code == 200
    assert len(client.get("/budgets").get_json()) == len(seeded)
