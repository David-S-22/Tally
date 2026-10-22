import logging
import threading
import time
import uuid
from datetime import UTC, datetime

from agents import AgentError, Agents
from contracts import (
    DECISION_SCHEMA,
    DECISIONS,
    FEATURES,
    VERSION,
    WorkflowError,
    validate,
    validate_request,
)
from settings import Settings
from storage import Store

logger = logging.getLogger(__name__)
AGENT_ORDER = ["planner_agent", "worker_agent", "reviewer_agent"]


def now_iso():
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Coordinator:
    def __init__(self, log_dir=None, settings=None, agents=None):
        self.settings = settings or Settings()
        self.agents = agents or Agents(self.settings)
        self.store = Store(log_dir or self.settings.data_dir)
        self.gate = threading.Lock()
        self.gate_enabled = self.settings.single_workflow_gate
        for record in self.store.all():
            if record["workflow_state"] == "running":
                record, revision = self.store.get(record["workflow_id"])
                record.update(
                    status="error",
                    workflow_state="failed",
                    error="Workflow interrupted by server restart",
                    code="workflow_interrupted",
                )
                record["human_decision"] = {
                    "required": True,
                    "status": "pending",
                    "allowed_values": ["Reject"],
                }
                self._save(
                    record,
                    revision,
                    self._event(record, "error", code="workflow_interrupted"),
                )

    def _event(self, record, event, **fields):
        return {
            "ts": now_iso(),
            "workflow_id": record["workflow_id"],
            "feature": record["feature"],
            "event": event,
            "request_id": record.get("request_id"),
            **fields,
        }

    def _save(self, record, revision, *events):
        self.store.save(record, events, revision)
        return 0 if revision is None else revision + 1

    def run_workflow(self, body, request_id=None):
        validate_request(body)
        gated = self.gate_enabled
        if gated and not self.gate.acquire(blocking=False):
            raise WorkflowError(
                503, "Workflow capacity reached; retry later", "workflow_busy"
            )
        try:
            return self._run(body, request_id)
        finally:
            if gated:
                self.gate.release()

    def _run(self, body, request_id):
        started = time.monotonic()
        deadline = started + self.settings.workflow_timeout
        reuse = body.get("options", {}).get("reuse_plan")
        if reuse:
            record, revision = self.store.get(reuse)
            if record["workflow_state"] != "awaiting_evidence":
                raise WorkflowError(
                    409, "Workflow is not awaiting evidence", "workflow_conflict"
                )
            if any(
                body.get(key, {}) != record.get(key, {})
                for key in ("feature", "task", "context")
            ):
                raise WorkflowError(
                    409,
                    "Resume must preserve feature, task and context",
                    "workflow_conflict",
                )
            incoming = {e["source"]: e for e in body["evidence"]}
            if any(incoming.get(e["source"]) != e for e in record["evidence"]):
                raise WorkflowError(
                    409,
                    "Resume must retain existing evidence unchanged",
                    "workflow_conflict",
                )
            record.update(
                evidence=body["evidence"],
                workflow_state="running",
                request_id=request_id,
            )
            record["human_decision"] = {"required": True, "status": "not_ready"}
            revision = self._save(
                record,
                revision,
                self._event(
                    record, "workflow_resumed", evidence_count=len(body["evidence"])
                ),
            )
        else:
            record = {
                "contract_version": VERSION,
                "status": "success",
                "workflow_state": "running",
                "workflow_id": str(uuid.uuid4()),
                "feature": body["feature"],
                "task": body["task"],
                "context": body.get("context", {}),
                "evidence": body["evidence"],
                "timestamp": now_iso(),
                "duration_ms": 0,
                "request_id": request_id,
                "participating_agents": list(AGENT_ORDER),
                "planner_result": None,
                "worker_result": None,
                "reviewer_result": None,
                "human_decision": {"required": True, "status": "not_ready"},
                "prompt_version": self.agents.prompt_version,
            }
            revision = self._save(record, None, self._event(record, "workflow_started"))
        roles = ["worker", "reviewer"] if reuse else ["planner", "worker", "reviewer"]
        if body.get("options", {}).get("plan_only"):
            roles = ["planner"]
        for role in roles:
            revision = self._save(
                record,
                revision,
                self._event(record, "agent_started", agent=f"{role}_agent"),
            )
            payload = {key: record[key] for key in ("task", "evidence", "context")}
            if role != "planner":
                payload["planner_result"] = record["planner_result"]
            if role == "reviewer":
                payload["worker_result"] = record["worker_result"]
            step_started = time.monotonic()
            try:
                result = self.agents.run(
                    role,
                    payload,
                    min(deadline, step_started + self.settings.step_timeout),
                )
            except AgentError as exc:
                result = {
                    "status": exc.status,
                    "agent": f"{role}_agent",
                    "model": self.settings.models[role],
                    "code": exc.code,
                    "error": str(exc),
                    "human_approval_required": True,
                }
            except Exception:
                logger.exception(
                    "Unexpected agent failure workflow_id=%s role=%s",
                    record["workflow_id"],
                    role,
                )
                result = {
                    "status": "error",
                    "agent": f"{role}_agent",
                    "code": "agent_failure",
                    "error": "Agent failed unexpectedly",
                    "human_approval_required": True,
                }
            result["duration_ms"] = int((time.monotonic() - step_started) * 1000)
            record[f"{role}_result"] = result
            record["duration_ms"] += result["duration_ms"]
            revision = self._save(
                record,
                revision,
                self._event(
                    record,
                    "agent_finished",
                    agent=f"{role}_agent",
                    status=result["status"],
                    duration_ms=result["duration_ms"],
                    **({"verdict": result["verdict"]} if "verdict" in result else {}),
                ),
            )
            if result["status"] != "success":
                record.update(
                    status="error",
                    workflow_state="failed",
                    error=result["error"],
                    code=result["code"],
                )
                for remaining in roles[roles.index(role) + 1 :]:
                    record[f"{remaining}_result"] = {
                        "status": "skipped",
                        "agent": f"{remaining}_agent",
                        "human_approval_required": True,
                    }
                break
        if record["workflow_state"] == "failed":
            allowed = ["Reject"]
            revision = self._save(
                record,
                revision,
                self._event(record, "error", code=record["code"]),
            )
        elif body.get("options", {}).get("plan_only"):
            record["workflow_state"] = "awaiting_evidence"
            record["human_decision"] = {"required": True, "status": "awaiting_evidence"}
            self._save(record, revision, self._event(record, "awaiting_evidence"))
            return record
        else:
            allowed = list(DECISIONS)
            record["workflow_state"] = "pending"
        record["human_decision"] = {
            "required": True,
            "status": "pending",
            "allowed_values": allowed,
        }
        self._save(
            record,
            revision,
            self._event(
                record,
                "handoff",
                **{
                    "from": "coordinator"
                    if record["workflow_state"] == "failed"
                    else "reviewer_agent",
                    "to": "human_reviewer",
                    "human_approval_required": True,
                },
            ),
        )
        return record

    def record_decision(self, workflow_id, body):
        validate(body, DECISION_SCHEMA)
        record, revision = self.store.get(workflow_id)
        human = record["human_decision"]
        if human["status"] != "pending":
            raise WorkflowError(
                409, "Workflow is not awaiting a decision", "workflow_conflict"
            )
        decision = body["decision"]
        if decision not in human["allowed_values"]:
            raise WorkflowError(
                409, "Failed workflows can only be rejected", "workflow_conflict"
            )
        corrected = body.get("corrected_output")
        reason = body.get("reason") or ""
        if decision == "Partially Accept":
            if (
                not corrected
                or (isinstance(corrected, str) and not corrected.strip())
                or not reason.strip()
            ):
                raise WorkflowError(
                    400, "Partial acceptance requires corrected_output and a reason"
                )
            worker = record["worker_result"]
            if (worker.get("structured") is not None) != isinstance(corrected, dict):
                raise WorkflowError(
                    400,
                    "Corrected output must match the Worker's text/object output type",
                )
        elif corrected is not None:
            raise WorkflowError(
                400, "corrected_output is only allowed for partial acceptance"
            )
        reviewer = record.get("reviewer_result") or {}
        if (
            decision != "Reject"
            and reviewer.get("verdict") != "approve"
            and not reason.strip()
        ):
            raise WorkflowError(400, "Overriding review warnings requires a reason")
        worker = record.get("worker_result") or {}
        final = (
            worker.get("structured")
            if worker.get("structured") is not None
            else worker.get("output")
        )
        if decision == "Partially Accept":
            final = corrected
        elif decision == "Reject":
            final = None
        record["human_decision"] = {
            "required": True,
            "status": "recorded",
            "decision": decision,
            "final_output": final,
            "reason": body.get("reason"),
            "decided_by": body.get("decided_by") or "unspecified",
            "timestamp": now_iso(),
        }
        record["workflow_state"] = "decided"
        self._save(
            record,
            revision,
            self._event(
                record,
                "human_decision",
                decision=decision,
                decided_by=record["human_decision"]["decided_by"],
            ),
        )
        return record

    def get_workflow(self, workflow_id):
        return self.store.get(workflow_id)[0]

    def status(self, feature=None):
        if feature is not None and feature not in FEATURES:
            raise WorkflowError(400, "Unknown feature filter")
        records = self.store.all(feature)
        return {
            "status": "success",
            "contract_version": VERSION,
            "workflow_history_count": len(records),
            "audit_record_count": self.store.event_count(feature),
            "pending_decisions": sum(
                r["human_decision"]["status"] == "pending" for r in records
            ),
            "by_feature": {
                f: sum(r["feature"] == f for r in records)
                for f in FEATURES
                if any(r["feature"] == f for r in records)
            },
            "latest_workflow": records[-1] if records else None,
            "export_mode": "on_demand",
        }
