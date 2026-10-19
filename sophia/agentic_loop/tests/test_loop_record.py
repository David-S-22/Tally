"""Tests for the agentic loop's run record.

The record is the artefact the Release 0 report cites, so the thing worth
testing is that a run always leaves all three files behind and that the writer
does not become the reason a run dies.
"""
import json

import pytest

from sophia.agentic_loop.record import RunRecord


@pytest.fixture
def record(tmp_path):
    return RunRecord(tmp_path / "reports")


def complete_mode(record, key="architecture", label="Architecture",
                  decision="accepted", note=None):
    record.start_mode(key, label)
    record.stage("PLAN", f"Review target: {label}")
    record.stage("OBSERVE", "Collecting evidence")
    record.set(evidence="Compose defines 15 services.")
    record.stage("ACT", "Running implementation model")
    record.set(implementation_output="Finding text.", review_output="Review text.")
    record.stage("ADAPT", "Human review of the finding")
    record.decision(decision, note)
    record.end_mode()


def test_a_completed_mode_writes_all_three_files(record):
    complete_mode(record)
    written = sorted(p.name for p in record.reports_dir.iterdir())
    assert written == ["report.json", "report.md", "run-view.md"]


def test_report_json_carries_the_stages_and_the_decision(record):
    complete_mode(record, decision="rejected", note="anomalies-db was not running")
    report = json.loads((record.reports_dir / "report.json").read_text(encoding="utf-8"))
    assert report["started"]
    assert len(report["modes"]) == 1
    mode = report["modes"][0]
    assert mode["mode"] == "architecture"
    assert [s["stage"] for s in mode["stages"]] == ["PLAN", "OBSERVE", "ACT", "ADAPT"]
    assert mode["decision"] == "rejected"
    assert mode["decision_note"] == "anomalies-db was not running"


def test_report_md_shows_the_decision_and_its_note(record):
    complete_mode(record, decision="edited", note="corrected to 14 services")
    text = (record.reports_dir / "report.md").read_text(encoding="utf-8")
    assert "## Architecture" in text
    assert "Human decision (ADAPT): edited - corrected to 14 services" in text


def test_run_view_shows_one_line_per_mode_with_its_stage_path(record):
    complete_mode(record)
    complete_mode(record, key="devops", label="DevOps", decision="abandoned")
    view = (record.reports_dir / "run-view.md").read_text(encoding="utf-8")
    assert "[Architecture] PLAN -> OBSERVE -> ACT -> ADAPT => accepted" in view
    assert "[DevOps] PLAN -> OBSERVE -> ACT -> ADAPT => abandoned" in view


def test_each_completed_mode_rewrites_the_record(record):
    complete_mode(record)
    after_one = json.loads((record.reports_dir / "report.json").read_text(encoding="utf-8"))
    assert len(after_one["modes"]) == 1
    complete_mode(record, key="database", label="Database")
    after_two = json.loads((record.reports_dir / "report.json").read_text(encoding="utf-8"))
    assert [m["mode"] for m in after_two["modes"]] == ["architecture", "database"]


def test_a_traceback_is_recorded_and_rendered(record):
    record.start_mode("endpoints", "Endpoints")
    record.stage("ACT", "Running implementation model")
    record.set(implementation_output="Model call failed.",
               traceback="Traceback (most recent call last):\n  ConnectionError")
    record.end_mode()
    text = (record.reports_dir / "report.md").read_text(encoding="utf-8")
    assert "- Traceback:" in text
    assert "ConnectionError" in text


def test_no_traceback_section_when_there_was_no_failure(record):
    complete_mode(record)
    assert "- Traceback:" not in (record.reports_dir / "report.md").read_text(encoding="utf-8")


def test_a_mode_with_unexpected_fields_still_writes(record):
    """A mode added later may carry keys this writer has never seen."""
    record.start_mode("future-mode", "Future Mode")
    record.stage("OBSERVE", "Collecting evidence")
    record.set(evidence="something", severity="high", extra={"nested": True})
    record.decision("accepted", None)
    record.end_mode()
    report = json.loads((record.reports_dir / "report.json").read_text(encoding="utf-8"))
    assert report["modes"][0]["severity"] == "high"
    assert "[Future Mode] OBSERVE => accepted" in \
        (record.reports_dir / "run-view.md").read_text(encoding="utf-8")


def test_a_mode_missing_the_usual_fields_still_writes(record):
    """Nothing but start_mode and end_mode -- the record must not raise."""
    record.start_mode("bare", "Bare")
    record.end_mode()
    text = (record.reports_dir / "report.md").read_text(encoding="utf-8")
    assert "## Bare" in text
    assert "Human decision (ADAPT): None" in text


def test_the_reports_directory_is_created_on_demand(tmp_path):
    nested = tmp_path / "does" / "not" / "exist"
    record = RunRecord(nested)
    complete_mode(record)
    assert (nested / "report.json").is_file()


def test_the_environment_reaches_all_three_files(record):
    """Which ollama answered is the point of the field, so it must survive
    into every artefact -- the JSON the report cites and the two readables."""
    record.set_environment(endpoint="http://127.0.0.1:11434/v1",
                           models_served="llama3.1:8b, qwen2.5:0.5b")
    complete_mode(record)

    report = json.loads((record.reports_dir / "report.json").read_text(encoding="utf-8"))
    assert report["environment"]["endpoint"] == "http://127.0.0.1:11434/v1"
    assert report["environment"]["models_served"] == "llama3.1:8b, qwen2.5:0.5b"

    assert "Endpoint: http://127.0.0.1:11434/v1" in \
        (record.reports_dir / "report.md").read_text(encoding="utf-8")
    assert "Models served: llama3.1:8b, qwen2.5:0.5b" in \
        (record.reports_dir / "run-view.md").read_text(encoding="utf-8")


def test_a_run_without_an_environment_still_writes(record):
    """set_environment is optional; its absence must not add an empty heading."""
    complete_mode(record)
    report = json.loads((record.reports_dir / "report.json").read_text(encoding="utf-8"))
    assert report["environment"] == {}
    assert "## Environment" not in \
        (record.reports_dir / "report.md").read_text(encoding="utf-8")
