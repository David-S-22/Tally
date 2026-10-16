"""Terminal entry point: run a workflow or record a decision without HTTP.

    python ai-services/multi-agent-server/workflow_runner.py --feature bills \
        --task "Draft a dispute letter for bill 12" --evidence evidence.json
    python ai-services/multi-agent-server/workflow_runner.py --decide <id> --decision Accept

Calls the same Coordinator the routes call, so the logs are identical.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from coordinator import Coordinator, WorkflowError
from server import LOG_DIR


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--feature")
    parser.add_argument("--task")
    parser.add_argument("--evidence", help="path to a JSON file holding the evidence list; omit for []")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--decide", metavar="WORKFLOW_ID")
    parser.add_argument("--decision", choices=["Accept", "Partially Accept", "Reject"])
    parser.add_argument("--corrected-output", help="text (or @file.json for a structured object)")
    parser.add_argument("--log-dir", default=LOG_DIR)
    args = parser.parse_args(argv)

    coordinator = Coordinator(args.log_dir)
    try:
        if args.decide:
            if not args.decision:
                parser.error("--decision is required with --decide")
            corrected = args.corrected_output
            if corrected and corrected.startswith("@"):
                with open(corrected[1:], encoding="utf-8") as fh:
                    corrected = json.load(fh)
            record = coordinator.record_decision(args.decide, {"decision": args.decision, "corrected_output": corrected, "decided_by": "terminal"})
        else:
            if not (args.feature and args.task):
                parser.error("--feature and --task are required")
            evidence = []
            if args.evidence:
                with open(args.evidence, encoding="utf-8") as fh:
                    evidence = json.load(fh)
            record = coordinator.run_workflow({
                "feature": args.feature,
                "task": args.task,
                "evidence": evidence,
                "options": {"plan_only": args.plan_only},
            })
    except WorkflowError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in record.items() if not k.startswith("_")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
