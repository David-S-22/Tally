"""Runs Planner -> Worker -> Reviewer, holds the human decision, writes the logs.

The coordinator is the only thing that touches the two JSON Lines files
(``workflow_history.jsonl``, ``coordination_audit.jsonl``). Records live in
memory keyed by ``workflow_id`` and are rebuilt from the history file at
start-up, so a restart does not lose pending decisions.
"""
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import agents

FEATURES = ("transactions", "savings", "anomalies", "bills", "budgets")
DECISIONS = ("Accept", "Partially Accept", "Reject")
AGENT_ORDER = ["planner_agent", "worker_agent", "reviewer_agent"]


class WorkflowError(Exception):
    """A request the contract rejects; ``status`` is the HTTP code to return."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Coordinator:
    def __init__(self, log_dir):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.log_dir / "workflow_history.jsonl"
        self.audit_path = self.log_dir / "coordination_audit.jsonl"
        self.records = {}
        for record in self._read(self.history_path):
            self.records[record["workflow_id"]] = record  # last line per id wins

    # --- public API, one method per route ---------------------------------

    def run_workflow(self, body):
        feature, task, evidence, context, options = self._validate_request(body)
        reuse = options.get("reuse_plan")
        if reuse:
            record = self._get(reuse)
            if record["human_decision"]["status"] != "awaiting_evidence":
                raise WorkflowError(409, f"workflow {reuse} already has worker and reviewer results")
            record["_started"] = time.monotonic()
            self._audit(record, "workflow_resumed", evidence_count=len(evidence))
        else:
            record = self._new_record(feature, task)
            self._audit(record, "workflow_started", task=task)
            record["planner_result"] = self._timed(record, agents.plan, task, evidence, context)

        if options.get("plan_only"):
            record["human_decision"] = {"required": True, "status": "awaiting_evidence"}
            self._finish(record)
            return record

        record["evidence"] = evidence
        record["worker_result"] = self._timed(
            record, agents.work, task, evidence, context, record["planner_result"]
        )
        record["reviewer_result"] = self._timed(
            record, agents.review, task, evidence, record["planner_result"], record["worker_result"]
        )
        record["human_decision"] = {"required": True, "status": "pending", "allowed_values": list(DECISIONS)}
        self._audit(record, "handoff", **{"from": "reviewer_agent", "to": "human_reviewer", "human_approval_required": True})
        self._finish(record)
        return record

    def record_decision(self, workflow_id, body):
        record = self._get(workflow_id)
        if record["human_decision"]["status"] != "pending":
            raise WorkflowError(409, f"workflow {workflow_id} is not awaiting a decision")
        decision = body.get("decision")
        if decision not in DECISIONS:
            raise WorkflowError(400, f"decision must be one of {list(DECISIONS)}")
        corrected = body.get("corrected_output")
        if decision == "Partially Accept" and corrected in (None, ""):
            raise WorkflowError(400, "corrected_output is required for Partially Accept")
        if decision == "Reject" and corrected is not None:
            raise WorkflowError(400, "corrected_output must be absent for Reject")

        worker = record["worker_result"]
        final = {
            "Accept": worker.get("structured") if worker.get("structured") is not None else worker.get("output"),
            "Partially Accept": corrected,
            "Reject": None,
        }[decision]
        record["human_decision"] = {
            "required": True,
            "status": "recorded",
            "decision": decision,
            "final_output": final,
            "reason": body.get("reason"),
            "decided_by": body.get("decided_by") or "demo",
            "timestamp": now_iso(),
        }
        self._audit(record, "human_decision", decision=decision, decided_by=record["human_decision"]["decided_by"])
        self._append(self.history_path, record)
        return record

    def get_workflow(self, workflow_id):
        return self._get(workflow_id)

    def status(self, feature=None):
        records = [r for r in self.records.values() if feature in (None, r["feature"])]
        records.sort(key=lambda r: r["timestamp"])
        by_feature = {}
        for r in records:
            by_feature[r["feature"]] = by_feature.get(r["feature"], 0) + 1
        return {
            "status": "success",
            "workflow_history_count": len(records),
            "audit_record_count": sum(1 for a in self._read(self.audit_path) if feature in (None, a["feature"])),
            "pending_decisions": sum(1 for r in records if r["human_decision"]["status"] == "pending"),
            "by_feature": by_feature,
            "latest_workflow": records[-1] if records else None,
        }

    # --- internals ---------------------------------------------------------

    def _validate_request(self, body):
        if not isinstance(body, dict):
            raise WorkflowError(400, "JSON body required")
        feature = body.get("feature")
        if feature not in FEATURES:
            raise WorkflowError(400, f"feature must be one of {list(FEATURES)}")
        task = (body.get("task") or "").strip()
        if not task:
            raise WorkflowError(400, "task is required")
        evidence = body.get("evidence")
        if not isinstance(evidence, list) or any(
            not isinstance(e, dict) or "source" not in e or "summary" not in e for e in evidence
        ):
            raise WorkflowError(400, "evidence must be a list of {source, summary, data?}")
        options = body.get("options") or {}
        return feature, task, evidence, body.get("context") or {}, options

    def _new_record(self, feature, task):
        record = {
            "status": "success",
            "workflow_id": str(uuid.uuid4()),
            "feature": feature,
            "task": task,
            "timestamp": now_iso(),
            "duration_ms": 0,
            "participating_agents": list(AGENT_ORDER),
            "planner_result": None,
            "worker_result": None,
            "reviewer_result": None,
            "human_decision": None,
            "_started": time.monotonic(),
        }
        self.records[record["workflow_id"]] = record
        return record

    def _get(self, workflow_id):
        record = self.records.get(workflow_id)
        if record is None:
            raise WorkflowError(404, f"unknown workflow {workflow_id}")
        return record

    def _timed(self, record, agent_fn, *args):
        started = time.monotonic()
        try:
            result = agent_fn(*args)
        except Exception as exc:  # a model call failing must not lose the workflow
            result = {
                "status": "error",
                "agent": agent_fn.__name__ + "_agent",
                "error": str(exc),
                "human_approval_required": True,
            }
            if agent_fn is agents.work:
                result.update(evidence_count=0, evidence_used=[], output="", structured=None)
        duration = int((time.monotonic() - started) * 1000)
        extra = {}
        if "evidence_count" in result:
            extra["evidence_count"] = result["evidence_count"]
        if "verdict" in result:
            extra["verdict"] = result["verdict"]
        self._audit(record, "agent_finished", agent=result["agent"], status=result["status"], duration_ms=duration, **extra)
        return result

    def _finish(self, record):
        started = record.pop("_started", None)
        if started is not None:
            record["duration_ms"] = int((time.monotonic() - started) * 1000)
        self._append(self.history_path, record)

    def _audit(self, record, event, **fields):
        line = {"ts": now_iso(), "workflow_id": record["workflow_id"], "feature": record["feature"], "event": event}
        line.update(fields)
        self._append(self.audit_path, line)

    @staticmethod
    def _append(path, obj):
        clean = {k: v for k, v in obj.items() if not k.startswith("_")}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(clean) + "\n")

    @staticmethod
    def _read(path):
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
