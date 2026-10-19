import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from contracts import DECISIONS, FEATURES


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run and review workflows through the shared HTTP server"
    )
    parser.add_argument(
        "--url", default=os.getenv("MULTI_AGENT_SERVER_URL", "http://127.0.0.1:5010")
    )
    parser.add_argument("--timeout", type=float, default=550)
    parser.add_argument("--feature", choices=FEATURES)
    parser.add_argument("--task")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--context", type=Path)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--reuse-plan")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--decide")
    operations.add_argument("--get")
    operations.add_argument("--export")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--decision", choices=DECISIONS)
    parser.add_argument("--corrected-output")
    parser.add_argument("--reason")
    args = parser.parse_args(argv)
    if args.output_dir and not args.export:
        parser.error("--output-dir requires --export")
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
    try:
        body = None
        if args.decide:
            if not args.decision:
                parser.error("--decision is required")
            corrected = args.corrected_output
            if corrected and corrected.startswith("@"):
                corrected = json.loads(Path(corrected[1:]).read_text(encoding="utf-8"))
            path = f"/workflow/{args.decide}/decision"
            body = {
                "decision": args.decision,
                "corrected_output": corrected,
                "reason": args.reason,
                "decided_by": "terminal",
            }
        elif args.get or args.export:
            path = f"/workflow/{args.get or args.export}" + (
                "/export" if args.export else ""
            )
        else:
            if not args.feature or not args.task:
                parser.error("--feature and --task are required")
            path = "/workflow"
            body = {
                "feature": args.feature,
                "task": args.task,
                "evidence": json.loads(args.evidence.read_text(encoding="utf-8"))
                if args.evidence
                else [],
                "context": json.loads(args.context.read_text(encoding="utf-8"))
                if args.context
                else {},
                "options": {
                    "plan_only": args.plan_only,
                    **({"reuse_plan": args.reuse_plan} if args.reuse_plan else {}),
                },
            }
        headers = {"Content-Type": "application/json"}
        token = os.getenv("MULTI_AGENT_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            args.url.rstrip("/") + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.load(exc)
            except ValueError:
                result = {"status": "error", "error": f"HTTP {exc.code}"}
            print(json.dumps(result, indent=2), file=sys.stderr)
            return 1
        if args.output_dir:
            args.output_dir.mkdir(parents=True, exist_ok=False)
            for filename in ("workflow_history.jsonl", "coordination_audit.jsonl"):
                (args.output_dir / filename).write_text(
                    "".join(
                        json.dumps(item, ensure_ascii=True) + "\n"
                        for item in result[filename]
                    ),
                    encoding="utf-8",
                )
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, urllib.error.URLError):
        print(
            "Unable to read input, write export, or contact the server; check paths, URL and server logs.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
