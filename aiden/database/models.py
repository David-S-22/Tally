from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column
from shared.backend import dto

db = SQLAlchemy()

class Anomaly(db.Model): # type: ignore[name-defined]
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_anomaly_confidence_range",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(unique=True) # One anomaly per transaction
    agent_reason_suspected: Mapped[str] = mapped_column()
    is_confirmed_by_user: Mapped[bool] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(nullable=True)
    sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    workflow_id: Mapped[str] = mapped_column(nullable=True)

    def to_dto(self):
        return dto.Anomaly(
            self.id,
            self.transaction_id,
            self.agent_reason_suspected,
            self.is_confirmed_by_user,
            self.confidence,
            list(self.sources or []),
            self.workflow_id,
        )
