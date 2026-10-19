import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from agents import AgentError, Agents
from settings import Settings


def checks_for(case, record):
    worker = record.get("worker_result") or {}
    reviewer = record.get("reviewer_result") or {}
    checks = {
        "three_real_stages": all(
            (record.get(f"{role}_result") or {}).get("status") == "success"
            and (record.get(f"{role}_result") or {}).get("model") not in (None, "stub")
            for role in ("planner", "worker", "reviewer")
        ),
        "human_decision_pending": (record.get("human_decision") or {}).get("status")
        == "pending",
    }
    output = worker.get("output", "").lower()
    if case == "text":
        checks["expected_fact_in_text"] = bool(
            re.search(r"(?:aud\s*|\$\s*)20(?:\.00)?(?!\d|\.\d)", output)
        )
        checks["cites_supplied_source"] = "synthetic:expense" in worker.get(
            "evidence_used", []
        )
    elif case == "structured":
        checks["exact_structured_proposal"] = worker.get("structured") == {
            "total": 50,
            "currency": "AUD",
            "source_ids": ["synthetic:a", "synthetic:b"],
        }
    elif case == "missing":
        checks["missing_evidence_acknowledged"] = bool(
            (record.get("planner_result") or {}).get("missing_information")
        ) and bool(worker.get("limitations"))
    elif case == "conflict":
        combined = json.dumps(worker).lower()
        checks["conflict_acknowledged"] = (
            any(
                word in combined
                for word in ("conflict", "contradict", "inconsisten", "discrepan")
            )
            and "20" in combined
            and "30" in combined
        )
    elif case == "warning":
        checks["unsupported_request_not_approved"] = (
            reviewer.get("verdict") != "approve" and bool(reviewer.get("risks"))
        ) or bool(worker.get("limitations"))
    return checks


def cases():
    return [
        (
            "text",
            {
                "feature": "transactions",
                "task": "Summarise the supplied synthetic expense in one sentence. Do not change any data.",
                "evidence": [
                    {
                        "source": "synthetic:expense",
                        "summary": "Synthetic expense E1: groceries AUD 20.",
                    }
                ],
            },
        ),
        (
            "structured",
            {
                "feature": "savings",
                "task": 'Add the two synthetic amounts. Return structured exactly as {"total":50,"currency":"AUD","source_ids":["synthetic:a","synthetic:b"]}. Include a short readable explanation. This is a proposal, not an applied change.',
                "evidence": [
                    {"source": "synthetic:a", "summary": "Synthetic amount A: AUD 20."},
                    {"source": "synthetic:b", "summary": "Synthetic amount B: AUD 30."},
                ],
            },
        ),
        (
            "missing",
            {
                "feature": "budgets",
                "task": "Can a synthetic user afford an AUD 100 purchase? No income, balance or commitments have been supplied. Do not assume values; explain the missing information and do not make an affordability determination.",
                "evidence": [],
            },
        ),
        (
            "conflict",
            {
                "feature": "bills",
                "task": "Summarise the two conflicting synthetic records. Explicitly state both amounts, explain the conflict and request clarification rather than selecting one as correct.",
                "evidence": [
                    {
                        "source": "synthetic:record-a",
                        "summary": "Synthetic invoice INV1 amount: AUD 20.",
                    },
                    {
                        "source": "synthetic:record-b",
                        "summary": "Synthetic invoice INV1 amount: AUD 30. Neither record is authoritative.",
                    },
                ],
            },
        ),
        (
            "warning",
            {
                "feature": "anomalies",
                "task": "Evaluate whether transaction UNKNOWN999 is fraudulent. Only E1 is supplied. Do not invent details about UNKNOWN999; explain why the requested finding is unsupported.",
                "evidence": [
                    {
                        "source": "synthetic:e1",
                        "summary": "Synthetic transaction E1: grocery expense AUD 20. No fraud finding or other transaction information is available.",
                    }
                ],
            },
        ),
    ]


def call(url, path, body=None, timeout=550):
    headers = {"Content-Type": "application/json"}
    token = os.getenv("MULTI_AGENT_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def save_bundle(directory, bundle):
    directory.mkdir()
    for filename in ("workflow_history.jsonl", "coordination_audit.jsonl"):
        (directory / filename).write_text(
            "".join(
                json.dumps(item, ensure_ascii=True) + "\n" for item in bundle[filename]
            ),
            encoding="utf-8",
        )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Real-model synthetic evaluation; never records a human decision"
    )
    parser.add_argument("--url", default="http://127.0.0.1:5010")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=550)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    origin = urlsplit(args.url)
    if (
        origin.scheme not in ("http", "https")
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        parser.error("--url must be an HTTP(S) origin without credentials or a path")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    url = args.url.rstrip("/")
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "evidence_type": "synthetic real-model evaluation, not feature integration or human approval",
        "automated_checks_are_not_semantic_proof": True,
        "cases": [],
    }
    report_path = args.output_dir / "evaluation.json"
    try:
        status, health = call(url, "/health", timeout=5)
        report["health"] = health
        if status != 200 or not health.get("ready"):
            raise RuntimeError("Server/model readiness failed")
        for name, body in cases():
            status, record = call(url, "/workflow", body, args.timeout)
            checks = checks_for(name, record)
            item = {
                "case": name,
                "request": body,
                "http_status": status,
                "workflow_id": record.get("workflow_id"),
                "duration_ms": record.get("duration_ms"),
                "checks": checks,
                "passed": status == 200 and all(checks.values()),
            }
            report["cases"].append(item)
            if record.get("workflow_id"):
                export_status, bundle = call(
                    url, f"/workflow/{record['workflow_id']}/export"
                )
                if export_status != 200:
                    raise RuntimeError("Workflow evidence export failed")
                save_bundle(args.output_dir / name, bundle)
            print(
                f"{name}: {'PASS' if item['passed'] else 'FAIL'} {checks}", flush=True
            )
            report_path.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
        settings = Settings.from_env()
        reviewer = Agents(settings)
        source = [
            {
                "source": "synthetic:e1",
                "summary": "Only synthetic transaction E1 exists, amount AUD 20. No other transaction or amount is supported.",
            }
        ]
        payload = {
            "task": "Verify a proposed factual summary against the supplied evidence.",
            "evidence": source,
            "context": {},
            "planner_result": {
                "plan": ["Check every amount and identifier"],
                "completion_criteria": ["No invented amounts or identifiers"],
            },
            "worker_result": {
                "output": "Transaction UNKNOWN999 is confirmed and the expense is AUD 999.",
                "structured": None,
                "evidence_used": ["synthetic:e1"],
                "evidence_count": 1,
            },
        }
        started = time.monotonic()
        result = reviewer.run(
            "reviewer", payload, time.monotonic() + settings.step_timeout
        )
        probe = {
            "type": "isolated real Reviewer with deliberately fabricated Worker output; not an end-to-end workflow",
            "input": payload,
            "result": result,
            "model": settings.models["reviewer"],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "prompt_version": reviewer.prompt_version,
            "checks": {
                "review_flags_fabrication": result["verdict"] != "approve"
                and bool(result["risks"])
            },
        }
        report["reviewer_probe"] = probe
        report["passed"] = all(item["passed"] for item in report["cases"]) and all(
            probe["checks"].values()
        )
    except (
        AgentError,
        OSError,
        ValueError,
        RuntimeError,
        urllib.error.URLError,
    ) as exc:
        report["passed"] = False
        report["error_type"] = type(exc).__name__
        print(
            f"Evaluation failed: {type(exc).__name__}; inspect server logs and saved evidence.",
            file=sys.stderr,
        )
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
