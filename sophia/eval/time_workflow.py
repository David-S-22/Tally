"""Time the Bills dispute-letter workflow against a live Multi-Agent Server, stage by stage.

    python -m sophia.eval.time_workflow                 # one run, prints per-agent seconds
    python -m sophia.eval.time_workflow --runs 3        # three runs, prints each and the mean

Run it on the demo machine with the server up (``python ai-services/multi-agent-server/server.py``)
and Ollama warm. It sends the real Bills task template with a fixed evidence set, so the numbers
are what the dispute panel will see. Set ``MULTI_AGENT_SERVER_URL`` / ``MULTI_AGENT_TOKEN`` as the
backend would. The decision is always Reject, so nothing is stored anywhere but the server's audit.

Model profiles are a server-side choice (shared); the one that matters for the 20-second target:

    MULTI_AGENT_PLANNER_MODEL=qwen2.5:0.5b MULTI_AGENT_WORKER_MODEL=qwen2.5:3b MULTI_AGENT_REVIEWER_MODEL=qwen2.5:3b

against the default qwen2.5:3b / qwen2.5:3b / llama3.1:8b (43-165 s in Ethan's verified runs).
"""
import argparse
import os
import statistics
import sys
import time
from datetime import date

os.environ.setdefault("MULTI_AGENT_ENABLED", "true")
os.environ.setdefault("MULTI_AGENT_SERVER_URL", "http://127.0.0.1:5010")

from sophia.backend.ai import agent_tasks  # noqa: E402
from sophia.backend.clients import multi_agent  # noqa: E402
from sophia.backend.engine import Bill  # noqa: E402

SPOTIFY = Bill(id=3, name="Spotify", merchant="Spotify AU", amount_cents=1399, cadence="monthly", next_billing_date=date(2026, 9, 16),
               type="subscription", payment_method="card", end_date=None, confirmed_at=None, created_at=None)
EVIDENCE = [
    {"source": "bills-db:bill:3", "summary": "Bill 3 Spotify (Spotify AU): $13.99 monthly, paid by card, next 2026-09-16", "data": {"id": 3}},
    {"source": "mcp:compare_bill_with_bank_charges", "summary": "20 Aug Spotify AU $17.99; 15 Jul Spotify AU $13.99; 8 Jul Spotify AU $13.99", "data": {"bill_id": 3}},
]


def one_run():
    started = time.perf_counter()
    record = multi_agent.run_workflow(agent_tasks.dispute_letter(SPOTIFY, "charged $17.99 instead of $13.99"), EVIDENCE, context={"timing": True})
    total = time.perf_counter() - started
    stages = {role: (record.get(f"{role}_result") or {}) for role in ("planner", "worker", "reviewer")}
    try:
        multi_agent.record_decision(record["workflow_id"], "Reject", reason="timing run", decided_by="time_workflow")
    except Exception:
        pass
    return total, {role: (r.get("duration_ms") or 0) / 1000 for role, r in stages.items()}, {role: r.get("status") for role, r in stages.items()}, record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args(argv)
    health = multi_agent._call("GET", "/health", None, "health")
    print(f"server: ollama={health.get('ollama')} ready={health.get('ready')} models={health.get('models')} missing={health.get('missing_models')}")
    totals = []
    for i in range(args.runs):
        total, seconds, status, record = one_run()
        totals.append(total)
        print(f"run {i + 1}: total {total:5.1f}s | planner {seconds['planner']:5.1f}s {status['planner']} | worker {seconds['worker']:5.1f}s {status['worker']} | reviewer {seconds['reviewer']:5.1f}s {status['reviewer']} | verdict {(record.get('reviewer_result') or {}).get('verdict')} | letter {len((record.get('worker_result') or {}).get('output') or '')} chars")
    if len(totals) > 1:
        print(f"mean {statistics.mean(totals):.1f}s, min {min(totals):.1f}s, max {max(totals):.1f}s")
    print("target: letter ready for human review within 20 s" + (" — met" if totals and max(totals) <= 20 else " — not met on this machine; see the model profile in the docstring"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
