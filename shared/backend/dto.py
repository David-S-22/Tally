# The following is a list of models used as DTOs between microservices
# They expose the public shape of the data, whilst leaving the internals
# for the database engine to maintain
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# Represents a transaction the user has entered into the application
@dataclass(frozen=True)
class Transaction:
    id: int
    amount: float
    merchant: str
    date: datetime
    description: str
    category_id: int

@dataclass(frozen=True)
class Category:
    id: int
    name: str
    type: Optional[str]

# Represents a transaction an agent has decided may be suspicious. The user can confirm
# whether it is true positive or false positive, which is represented by is_confirmed_by_user.
# confidence is the agent's mean confidence in its finding, derived from the model's token
# log probabilities (log probs). It is None when no confidence signal is available.
# sources lists the filenames of the RAG reference documents the agent used to ground
# its reasoning; it is an empty list when no sources were retrieved.
@dataclass(frozen=True)
class Anomaly:
    id: int
    transaction_id: int
    agent_reason_suspected: str
    is_confirmed_by_user: Optional[bool]
    confidence: Optional[float] = None
    sources: List[str] = field(default_factory=list)
    workflow_id: Optional[str] = None

@dataclass(frozen=True)
class Goal:
    id: int
    name: str
    cost: int
    date: datetime

@dataclass(frozen=True)
class Suggestion:
    id: int
    suggestion: str
    accepted: bool
    feedback: Optional[str] = None

@dataclass(frozen=True)
class Feedback:
    id: int
    feedback: str
    suggestion_id: Optional[int] = None
    category_id: Optional[int] = None
    timeframe: Optional[str] = None
