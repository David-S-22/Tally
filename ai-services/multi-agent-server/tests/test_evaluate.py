import copy
import json
import os
from unittest.mock import patch

import evaluate
import pytest
from evaluate import checks_for


def successful():
    return {
        "planner_result": {
            "status": "success",
            "model": "real",
            "missing_information": ["No balance"],
        },
        "worker_result": {
            "status": "success",
            "model": "real",
            "output": "AUD 20.",
            "evidence_used": ["synthetic:expense"],
            "limitations": ["No balance"],
        },
        "reviewer_result": {"status": "success", "model": "real"},
        "human_decision": {"status": "pending"},
    }


def test_text_checks_fail_for_incorrect_fact_and_missing_source():
    record = successful()
    assert all(checks_for("text", record).values())
    record["worker_result"]["output"] = "AUD 999."
    record["worker_result"]["evidence_used"] = []
    checks = checks_for("text", record)
    assert not checks["expected_fact_in_text"] and not checks["cites_supplied_source"]

    for output in ("AUD 120.", "AUD 200.", "AUD 20.01."):
        record["worker_result"]["output"] = output
        assert not checks_for("text", record)["expected_fact_in_text"]


def test_structured_checks_verify_exact_total_and_sources():
    record = successful()
    record["worker_result"]["structured"] = {
        "total": 50,
        "currency": "AUD",
        "source_ids": ["synthetic:a", "synthetic:b"],
    }
    assert all(checks_for("structured", record).values())
    wrong = copy.deepcopy(record)
    wrong["worker_result"]["structured"]["total"] = 51
    assert not checks_for("structured", wrong)["exact_structured_proposal"]


def test_missing_conflict_and_warning_checks_detect_omissions():
    record = successful()
    assert all(checks_for("missing", record).values())
    assert not checks_for("conflict", record)["conflict_acknowledged"]
    record["worker_result"]["output"] = "Conflicting amounts: AUD 20 and AUD 30."
    assert all(checks_for("conflict", record).values())
    record["worker_result"]["limitations"] = []
    record["reviewer_result"].update(verdict="approve", risks=[])
    assert not checks_for("warning", record)["unsupported_request_not_approved"]


def test_runner_exports_and_reports_checks_without_human_decisions(
    monkeypatch, tmp_path
):
    calls = []
    record = successful()
    record["workflow_id"] = "synthetic-workflow"

    def call(url, path, body=None, timeout=550):
        calls.append(path)
        if path == "/health":
            return 200, {"ready": True}
        if path.endswith("/export"):
            return 200, {
                "workflow_history.jsonl": [record],
                "coordination_audit.jsonl": [{"event": "handoff"}],
            }
        return 200, record

    class Reviewer:
        prompt_version = "test"

        def __init__(self, settings):
            pass

        def run(self, role, payload, deadline):
            assert role == "reviewer"
            assert "999" in payload["worker_result"]["output"]
            return {"verdict": "reject", "risks": ["Unsupported amount and identifier"]}

    monkeypatch.setattr(evaluate, "call", call)
    monkeypatch.setattr(evaluate, "cases", lambda: [("text", {"task": "synthetic"})])
    monkeypatch.setattr(evaluate, "Agents", Reviewer)
    output = tmp_path / "new-evidence"
    with patch.dict(os.environ, {"MULTI_AGENT_TOKEN": "a" * 32}):
        assert evaluate.main(["--output-dir", str(output)]) == 0
    report = json.loads((output / "evaluation.json").read_text())
    assert (
        report["passed"]
        and report["reviewer_probe"]["checks"]["review_flags_fabrication"]
    )
    assert (
        json.loads((output / "text" / "workflow_history.jsonl").read_text()) == record
    )
    assert not any("decision" in path for path in calls)


def test_runner_saves_readiness_failure_and_refuses_overwriting(monkeypatch, tmp_path):
    monkeypatch.setattr(
        evaluate, "call", lambda *args, **kwargs: (401, {"ready": False})
    )
    output = tmp_path / "failure"
    assert evaluate.main(["--output-dir", str(output)]) == 1
    report = json.loads((output / "evaluation.json").read_text())
    assert not report["passed"] and report["error_type"] == "RuntimeError"
    with pytest.raises(FileExistsError):
        evaluate.main(["--output-dir", str(output)])
