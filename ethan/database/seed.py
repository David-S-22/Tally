from __future__ import annotations

from sqlalchemy import func, select

from .models import Budget, BudgetLine, CoachProposal, PlannedEvent, db


SEED_BUDGETS = (
    ("2026-01", 520000, "closed"),
    ("2026-02", 515000, "closed"),
    ("2026-03", 530000, "closed"),
    ("2026-04", 525000, "closed"),
    ("2026-05", 535000, "closed"),
    ("2026-06", 540000, "closed"),
    ("2026-07", 550000, "closed"),
    ("2026-08", 548000, "closed"),
    ("2026-09", 560000, "closed"),
    ("2026-10", 565000, "active"),
)

SEED_LINE_TEMPLATES = (
    (30, "Housing", 115000, 125000),
    (81, "Groceries", 40000, 55000),
    (80, "Dining", 20000, 30000),
    (70, "Transport", 12000, 18000),
    (62, "Utilities", 25000, 32000),
    (60, "Internet", 8000, 9000),
    (61, "Mobile", 5500, 7000),
    (33, "Streaming subscriptions", 3000, 4500),
)

LINES_PER_BUDGET = len(SEED_LINE_TEMPLATES)

CATEGORY_SLOT = {category: slot for slot, (_, category, _, _) in enumerate(SEED_LINE_TEMPLATES)}

DEFAULT_EVENTS = (
    (3, "Weekly grocery shop", "Groceries", 9000, 12000, "user", "confirmed"),
    (14, "Fuel top-up", "Transport", 5000, 7000, "predicted", "planned"),
)

SEED_EVENTS_BY_MONTH = {
    "2026-08": (
        (4, "Weekly grocery shop", "Groceries", 9000, 12000, "user", "confirmed"),
        (9, "Electricity bill", "Utilities", 12000, 14000, "user", "confirmed"),
        (16, "Dinner with friends", "Dining", 6000, 9000, "predicted", "confirmed"),
        (23, "Fuel top-up", "Transport", 5000, 7000, "predicted", "planned"),
    ),
    "2026-09": (
        (5, "Weekly grocery shop", "Groceries", 9000, 12000, "user", "confirmed"),
        (12, "Water bill", "Utilities", 8000, 10000, "user", "confirmed"),
        (19, "Birthday dinner", "Dining", 8000, 12000, "user", "confirmed"),
        (26, "Weekend trip", "Transport", 15000, 22000, "predicted", "planned"),
        (28, "Cinema night", "Dining", 3000, 5000, "predicted", "cancelled"),
    ),
    "2026-10": (
        (24, "Weekly grocery shop", "Groceries", 12000, 16000, "user", "planned"),
        (25, "Dinner with friends", "Dining", 8000, 12000, "user", "planned"),
        (26, "Cinema night", "Dining", 3000, 4500, "predicted", "cancelled"),
        (27, "Weekend trip", "Transport", 15000, 25000, "predicted", "planned"),
        (28, "Electricity bill", "Utilities", 4000, 6000, "user", "confirmed"),
        (30, "Fuel top-up", "Transport", 5000, 7000, "predicted", "planned"),
        (31, "Halloween party supplies", "Dining", 3000, 5000, "predicted", "planned"),
    ),
}

SEED_PROPOSALS_BY_MONTH = {
    "2026-01": (("Housing", 120000, 130000, "accepted"),),
    "2026-02": (("Groceries", 42000, 57000, "rejected"),),
    "2026-03": (("Dining", 21000, 31000, "accepted"),),
    "2026-04": (("Transport", 12500, 18500, "rejected"),),
    "2026-05": (("Utilities", 26000, 33000, "accepted"),),
    "2026-06": (("Internet", 8500, 9500, "accepted"),),
    "2026-07": (("Mobile", 6000, 7500, "rejected"),),
    "2026-08": (("Groceries", 41000, 56000, "accepted"),),
    "2026-09": (
        ("Transport", 15000, 21000, "accepted"),
        ("Dining", 18000, 27000, "rejected"),
    ),
    "2026-10": (
        ("Groceries", 50000, 65000, "proposed"),
        ("Transport", 14000, 20000, "proposed"),
        ("Dining", 18000, 25000, "rejected"),
    ),
}


def _line_id(budget_index: int, slot: int) -> int:
    return (budget_index - 1) * LINES_PER_BUDGET + slot + 1


def seed_database_if_empty():
    try:
        if any(_seed_counts().values()):
            return

        event_id = 0
        proposal_id = 0
        for index, budget_row in enumerate(SEED_BUDGETS, start=1):
            month, declared_income, budget_status = budget_row
            timestamp = f"{month}-01T09:00:00+00:00"
            budget = Budget(
                id=index,
                month=month,
                declared_income=declared_income,
                status=budget_status,
                created_at=timestamp,
                updated_at=timestamp,
            )
            db.session.add(budget)
            db.session.flush()

            for slot, (category_id, category, warn_at, hard_cap) in enumerate(SEED_LINE_TEMPLATES):
                db.session.add(
                    BudgetLine(
                        id=_line_id(index, slot),
                        budget_id=budget.id,
                        category_id=category_id,
                        category=category,
                        warn_at=warn_at,
                        hard_cap=hard_cap,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )

            for day, label, category, est_low, est_high, source, status in SEED_EVENTS_BY_MONTH.get(
                month, DEFAULT_EVENTS
            ):
                event_id += 1
                db.session.add(
                    PlannedEvent(
                        id=event_id,
                        budget_id=budget.id,
                        date=f"{month}-{day:02d}",
                        label=label,
                        category=category,
                        est_low=est_low,
                        est_high=est_high,
                        source=source,
                        status=status,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )

            for category, warn_at, hard_cap, status in SEED_PROPOSALS_BY_MONTH[month]:
                proposal_id += 1
                decided = status in {"accepted", "rejected"}
                db.session.add(
                    CoachProposal(
                        id=proposal_id,
                        budget_id=budget.id,
                        proposal_json={
                            "proposal_type": "threshold_adjustment",
                            "summary": f"Adjust the {category} thresholds for {month}.",
                            "operations": [
                                {
                                    "action": "update_budget_line",
                                    "budget_line_id": _line_id(index, CATEGORY_SLOT[category]),
                                    "category": category,
                                    "fields": {"warn_at": warn_at, "hard_cap": hard_cap},
                                }
                            ],
                        },
                        rationale=f"Seeded proposal for {category.lower()} in {month}.",
                        status=status,
                        rejection_reason="User preferred to keep current budget settings."
                        if status == "rejected"
                        else None,
                        decided_at=timestamp if decided else None,
                        created_at=timestamp,
                    )
                )

        db.session.commit()
    except Exception:
        db.session.rollback()
        raise


def _seed_counts() -> dict[str, int]:
    return {
        "budgets": db.session.scalar(select(func.count(Budget.id))) or 0,
        "budget_lines": db.session.scalar(select(func.count(BudgetLine.id))) or 0,
        "planned_events": db.session.scalar(select(func.count(PlannedEvent.id))) or 0,
        "coach_proposals": db.session.scalar(select(func.count(CoachProposal.id))) or 0,
    }
