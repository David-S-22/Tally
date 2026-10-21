"""Shared terminal agentic loop: Plan -> Act -> Observe -> Adapt.

This file runs the whole loop, record.py writes the
run record the reports cite. Modes collect evidence from the repository
(architecture), from the shared MCP, RAG and Multi-Agent servers, live and
read-only (mcp, rag, multi_agent_review), or from pre-commit, the GitHub CLI
and the deployed app (testing_review, cloud_deployment_review). To add a
review mode, add prompt files under prompts/<your-family>/ and one entry to
MODES below.
"""

import asyncio
import calendar
import json
import os
import re
import subprocess
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from fastmcp import Client
from openai import OpenAI

from .record import RunRecord

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"


# --- prompts and models ----------------------------------------------------

def read_prompt(family: str, name: str) -> str:
    return (PROMPTS_DIR / family / name).read_text(encoding="utf-8").strip()


def call_model(system_prompt: str, user_prompt: str, *, review: bool = False):
    """One chat call to the local ollama service. Returns (output, error)."""
    if review:
        model = os.getenv("OLLAMA_REVIEW_MODEL", "llama3.1:8b")
    else:
        model = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
    client = OpenAI(base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                    api_key="ollama", timeout=180.0)
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            max_tokens=300,
            temperature=0.1,
        )
        output = (response.choices[0].message.content or "").strip()
        return output or "No response generated.", None
    except Exception as exc:
        return None, f"Model call failed ({model}): {exc}"


# --- evidence collection (OBSERVE) -----------------------------------------

def _host_port(entry):
    if isinstance(entry, dict):          # long-form "published: 3005"
        return entry.get("published")
    head = str(entry).split(":", 1)[0]   # short-form "3005:80"
    return head if head.isdigit() else None


def collect_architecture():
    """Compose topology and repo layout, read from files - nothing must be running."""
    compose_path = REPO_ROOT / "docker-compose.yml"
    if not compose_path.exists():
        return False, "docker-compose.yml not found at the repository root."
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}

    services, edges = [], []
    for name, spec in (compose.get("services") or {}).items():
        spec = spec or {}
        host_ports = [p for p in map(_host_port, spec.get("ports") or []) if p]
        services.append(f"{name}:{host_ports[0]}" if host_ports else name)
        deps = spec.get("depends_on") or []
        for dep in (deps.keys() if isinstance(deps, dict) else deps):
            edges.append(f"{name}->{dep}")

    # a student directory is any root directory with a backend/ ("shared" also
    # has one - shared/backend/dto.py - so it is excluded by name)
    student_dirs = sorted(p.name for p in REPO_ROOT.iterdir()
                          if p.name != "shared" and (p / "backend").is_dir())
    required = [".github/workflows", "docs", "shared", "ai-services", "scripts"]
    missing = [d for d in required if not (REPO_ROOT / d).is_dir()]
    shared_index = (REPO_ROOT / "shared" / "frontend" / "public" / "index.html").exists()

    return True, (
        f"Compose services (host port): {', '.join(sorted(services))}. "
        f"Dependency edges: {', '.join(sorted(edges)) or 'none'}. "
        f"Student directories: {', '.join(student_dirs) or 'none'}. "
        f"Missing required directories: {', '.join(missing) or 'none'}. "
        f"Shared frontend index.html present: {shared_index}."
    )


def http_timeout():
    return float(os.getenv("LOOP_HTTP_TIMEOUT", "30"))


def parse_row_date(value):
    """Rows come back RFC 2822 ("Wed, 15 Jul 2026 00:00:00 GMT") or ISO."""
    try:
        return parsedate_to_datetime(str(value)).date()
    except (TypeError, ValueError):
        return datetime.fromisoformat(str(value)[:10]).date()


def month_window(day):
    """First and last day of the month containing `day`, as ISO strings."""
    last_day = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1).isoformat(), day.replace(day=last_day).isoformat()


# -- MCP validation ---------------------------------------------------------

def rows_from_tool_result(result):
    data = result.data if result.data is not None else result.structured_content
    if isinstance(data, dict) and isinstance(data.get("result"), list):
        data = data["result"]
    return data if isinstance(data, list) else []


async def timed_search(client, args):
    """Call search_transactions; return (rows, duration in ms)."""
    started = time.perf_counter()
    result = await client.call_tool("search_transactions", args)
    return rows_from_tool_result(result), (time.perf_counter() - started) * 1000


def filters_from_row(row):
    """Filters that should match `row`: its merchant and its calendar month."""
    start_date, end_date = month_window(parse_row_date(row.get("date")))
    return {"start_date": start_date, "end_date": end_date, "merchant": row.get("merchant")}


async def probe_mcp_server(url, impossible_merchant):
    """One session: list tools, an unfiltered call, a filtered call derived
    from the first row, and a call for a merchant that cannot exist."""
    async with Client(url, timeout=http_timeout()) as client:
        tools = await client.list_tools()
        all_rows, all_ms = await timed_search(client, {})

        seed_args, seed_rows, seed_ms = None, [], None
        if all_rows and isinstance(all_rows[0], dict):
            seed_args = filters_from_row(all_rows[0])
            seed_rows, seed_ms = await timed_search(client, seed_args)

        none_rows, _ = await timed_search(client, {"merchant": impossible_merchant})

    return {"tools": tools, "all_rows": all_rows, "all_ms": all_ms,
            "seed_args": seed_args, "seed_rows": seed_rows, "seed_ms": seed_ms,
            "none_rows": none_rows}


def describe_filtered_call(seed_args, rows, ms, total_rows, row_keys):
    """Evidence sentence for the seeded, filtered search_transactions call."""
    shape_ok = bool(rows) and all(isinstance(row, dict) and row_keys <= row.keys() for row in rows)
    merchants_ok = bool(rows) and all(row.get("merchant") == seed_args["merchant"] for row in rows)
    try:
        dates_ok = bool(rows) and all(
            seed_args["start_date"] <= parse_row_date(row.get("date")).isoformat() <= seed_args["end_date"]
            for row in rows)
    except (TypeError, ValueError):
        dates_ok = False
    sample_row = {key: rows[0].get(key) for key in sorted(row_keys)} if rows else None

    return (f"Filtered call (seeded from the first row) with arguments {seed_args} returned "
            f"{len(rows)} rows in {ms:.0f} ms. "
            f"Result is a list of rows with id, date, merchant, amount: {'PASS' if shape_ok else 'FAIL'}. "
            f"Every row has merchant '{seed_args['merchant']}': {merchants_ok}. "
            f"Every row dated within {seed_args['start_date']}..{seed_args['end_date']}: {dates_ok}. "
            f"Filtered count not above unfiltered count: {len(rows) <= total_rows}. "
            f"Sample row: {sample_row}.")


def collect_mcp():
    """MCP tool contract: search_transactions is registered, exposes the filter
    parameters, returns well-formed rows, and honours the filters both ways."""
    url = os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")
    row_keys = {"id", "date", "merchant", "amount"}
    filter_params = ("start_date", "end_date", "merchant")
    impossible_merchant = "Quantum Llama Rentals"

    try:
        probe = asyncio.run(probe_mcp_server(url, impossible_merchant))
    except Exception as exc:
        return False, f"MCP server unreachable or tool call failed at {url}: {type(exc).__name__}"

    tool_names = [tool.name for tool in probe["tools"]]
    search_tool = None
    for tool in probe["tools"]:
        if tool.name == "search_transactions":
            search_tool = tool
    schema = (getattr(search_tool, "input_schema", None) or {}) if search_tool else {}
    schema_params = set((schema.get("properties") or {}).keys())
    missing_params = [name for name in filter_params if name not in schema_params]

    evidence = [
        f"MCP server: {url}. Registered tools: {', '.join(tool_names) or 'none'}. "
        f"search_transactions registered: {search_tool is not None}. "
        f"Filter parameters in its schema: {', '.join(sorted(schema_params)) or 'none'}; "
        f"missing: {', '.join(missing_params) or 'none'}. "
        f"Unfiltered call returned {len(probe['all_rows'])} rows in {probe['all_ms']:.0f} ms."
    ]

    if probe["seed_args"] is None:
        evidence.append("No rows available, so the filtered call was skipped: "
                        "result shape FAIL, filters honoured FAIL.")
    else:
        evidence.append(describe_filtered_call(
            probe["seed_args"], probe["seed_rows"], probe["seed_ms"],
            total_rows=len(probe["all_rows"]), row_keys=row_keys))

    none_rows = probe["none_rows"]
    evidence.append(f"Call with merchant '{impossible_merchant}' returned {len(none_rows)} rows "
                    f"(expected 0): {'FAIL' if none_rows else 'PASS'}.")

    return True, " ".join(evidence)


# -- RAG validation ---------------------------------------------------------

def post_retrieve(url, collection, question, k=3, where=None):
    """POST /retrieve; returns (results, ms) or raises with the failure text."""
    body = {"feature": collection, "question": question, "k": k}
    if where:
        body["where"] = where
    started = time.perf_counter()
    response = requests.post(f"{url}/retrieve", json=body, timeout=http_timeout())
    ms = (time.perf_counter() - started) * 1000
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code} in {ms:.0f} ms")
    results = (response.json() or {}).get("results") or []
    return results, ms


def seed_merchant_from_corpus(url, records_collection):
    """Merchant of the best transaction chunk for a generic query, used as the
    seeded question; None when the collection holds no transaction chunks."""
    results, _ = post_retrieve(url, records_collection, "transaction", k=1,
                               where={"kind": "transaction"})
    if not results:
        return None
    return (results[0].get("metadata") or {}).get("merchant") or None


def describe_retrieve(url, label, collection, question, threshold, expect_merchant=None):
    try:
        results, ms = post_retrieve(url, collection, question)
    except requests.RequestException as exc:
        return f"[{collection}/{label}] retrieve failed: {type(exc).__name__}."
    except RuntimeError as exc:
        return f"[{collection}/{label}] retrieve failed: {exc}."
    if not results:
        return f"[{collection}/{label}] 0 results in {ms:.0f} ms."

    best_chunk = min(results, key=lambda chunk: chunk.get("distance", float("inf")))
    distance = best_chunk.get("distance")
    metadata = best_chunk.get("metadata") or {}
    under_threshold = distance is not None and distance <= threshold

    parts = [f"[{collection}/{label}] {len(results)} results",
             f"best distance {distance:.3f}",
             f"under_threshold: {under_threshold}",
             f"doc_type: {metadata.get('doc_type')}",
             f"kind: {metadata.get('kind')}",
             f"category_id: {metadata.get('category_id')}"]
    if expect_merchant:
        parts.append(f"best chunk merchant '{metadata.get('merchant')}' matches seed: "
                     f"{metadata.get('merchant') == expect_merchant}")
    parts.append(f"{ms:.0f} ms")
    return "; ".join(parts) + "."


def collect_rag():
    """RAG corpus contract: collections present, a seeded question grounds on
    its own record, an unanswerable one is insufficient, the guide is markdown."""
    url = os.getenv("RAG_SERVER_URL", "http://localhost:5003").rstrip("/")
    threshold = float(os.getenv("RAG_INSUFFICIENT_ABOVE", "1.2"))
    records, guide = "transactions-records", "transactions"
    unrelated_question = "Quantum Llama Rentals equipment hire"   # negative control: expects insufficient

    try:
        health = requests.get(f"{url}/health", timeout=http_timeout()).json()
    except Exception as exc:
        return False, f"RAG server unreachable at {url}: {type(exc).__name__}"
    collections = health.get("collections") or []
    evidence = [
        f"RAG server: {url}. Insufficient threshold (distance above): {threshold}. "
        f"Collections: {', '.join(collections) or 'none'}. "
        f"{guide} present: {guide in collections}. "
        f"{records} present: {records in collections}."
    ]

    try:
        seed_merchant = seed_merchant_from_corpus(url, records)
    except (requests.RequestException, RuntimeError) as exc:
        seed_merchant = None
        evidence.append(f"Seed lookup on {records} failed: {exc}.")
    if seed_merchant:
        evidence.append(f"Question (seeded from the corpus): '{seed_merchant}'.")
        evidence.append(describe_retrieve(url, "seeded", records, seed_merchant, threshold,
                                          expect_merchant=seed_merchant))
        evidence.append(describe_retrieve(url, "seeded", guide, seed_merchant, threshold))
    else:
        evidence.append(f"No transaction chunk found in {records}; seeded question skipped.")

    evidence.append(f"Question (unrelated, fixed): '{unrelated_question}'.")
    evidence.append(describe_retrieve(url, "unrelated", records, unrelated_question, threshold))
    evidence.append(describe_retrieve(url, "unrelated", guide, unrelated_question, threshold))

    return True, " ".join(evidence)


# -- Multi-Agent workflow review --------------------------------------------

# the audit log of one completed, decided workflow, in the order the server writes it
AUDIT_SEQUENCE = ["workflow_started",
                  "agent_started", "agent_finished",     # planner
                  "agent_started", "agent_finished",     # worker
                  "agent_started", "agent_finished",     # reviewer
                  "handoff", "human_decision"]
ROLES = ("planner", "worker", "reviewer")


def multi_agent_get(path):
    """GET one route of the shared Multi-Agent Server as JSON. The bearer
    token comes from MULTI_AGENT_TOKEN, which the root .env provides."""
    url = os.getenv("MULTI_AGENT_SERVER_URL", "http://127.0.0.1:5010").rstrip("/")
    token = os.getenv("MULTI_AGENT_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = requests.get(url + path, headers=headers, timeout=http_timeout())
    if response.status_code == 401:
        raise RuntimeError("401 from the Multi-Agent Server: MULTI_AGENT_TOKEN missing or wrong in .env")
    response.raise_for_status()
    return response.json()


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_export(folder):
    """A committed export folder. Returns the last history snapshot, which is
    the final state of the workflow, and the audit events."""
    history = read_jsonl(folder / "workflow_history.jsonl")
    events = read_jsonl(folder / "coordination_audit.jsonl")
    return history[-1], events


def audit_in_order(events):
    """Check the audit events follow AUDIT_SEQUENCE and their timestamps never
    go backwards. Returns (ok, detail)."""
    names = [event.get("event") for event in events]
    if names != AUDIT_SEQUENCE:
        return False, f"sequence {names}"
    stamps = [event.get("ts") or "" for event in events]
    for earlier, later in zip(stamps, stamps[1:]):
        if later < earlier:
            return False, f"timestamp {later} before {earlier}"
    return True, f"{len(events)} events in order"


def load_workflow():
    """The workflow under review and its audit events, from a committed export
    when MULTI_AGENT_EXPORT_DIR is set, otherwise from the running server.
    Returns (health line, record, events, history snapshot count)."""
    export_dir = os.getenv("MULTI_AGENT_EXPORT_DIR")
    if export_dir:
        record, events = read_export(Path(export_dir))
        return f"Server health: skipped, offline export {export_dir}.", record, events, "n/a (offline export)"

    health = multi_agent_get("/health")
    health_line = (f"Server alive: {health.get('status')}; ready: {health.get('ready')}; "
                   f"contract version: {health.get('contract_version')}.")

    workflow_id = os.getenv("MULTI_AGENT_WORKFLOW_ID")
    feature = os.getenv("MULTI_AGENT_FEATURE")
    if workflow_id:
        record = multi_agent_get(f"/workflow/{workflow_id}")
    else:
        query = f"?feature={feature}" if feature else ""
        record = multi_agent_get(f"/workflow/status{query}").get("latest_workflow")
        if not record:
            raise RuntimeError(f"no workflow on the server for feature {feature or 'any'}")

    export = multi_agent_get(f"/workflow/{record['workflow_id']}/export")
    events = export.get("coordination_audit.jsonl") or []
    history_count = len(export.get("workflow_history.jsonl") or [])
    return health_line, record, events, history_count


def collect_multi_agent():
    """One completed Planner, Worker, Reviewer and Human Review run, read-only."""
    try:
        health_line, record, events, history_count = load_workflow()
    except (requests.RequestException, RuntimeError, OSError, ValueError, KeyError) as exc:
        return False, f"Multi-Agent evidence unavailable: {exc}"

    results = {role: record.get(f"{role}_result") or {} for role in ROLES}
    statuses = {role: results[role].get("status") for role in ROLES}
    all_succeeded = all(status == "success" for status in statuses.values())
    in_order = record.get("participating_agents") == [f"{role}_agent" for role in ROLES]

    supplied_sources = {item.get("source") for item in record.get("evidence") or []}
    worker_used = results["worker"].get("evidence_used") or []
    reviewer_used = results["reviewer"].get("evidence_used") or []
    references_ok = set(worker_used) <= supplied_sources and set(reviewer_used) <= supplied_sources

    reviewer = results["reviewer"]
    decision = record.get("human_decision") or {}
    accepted = decision.get("decision") in ("Accept", "Partially Accept")
    # the server requires a reason when a human accepts a verdict the reviewer did not approve
    reason_missing = accepted and reviewer.get("verdict") != "approve" and not decision.get("reason")
    decision_ok = decision.get("status") == "recorded" and not reason_missing

    order_ok, order_detail = audit_in_order(events)
    confidence = {role: results[role].get("confidence") for role in ROLES}
    worker_output = str(results["worker"].get("output") or "")[:300]

    return True, " ".join([
        health_line,
        f"Workflow {record.get('workflow_id')} for feature {record.get('feature')}, "
        f"state {record.get('workflow_state')}.",
        f"Role statuses: {statuses}; agents in Planner, Worker, Reviewer order: {in_order}: "
        f"{'PASS' if all_succeeded and in_order else 'FAIL'}.",
        f"Supplied sources: {sorted(supplied_sources)}; worker used {worker_used}; "
        f"reviewer used {reviewer_used}; every reference names a supplied source: "
        f"{'PASS' if references_ok else 'FAIL'}.",
        f"Reviewer verdict: {reviewer.get('verdict')}; risks: {len(reviewer.get('risks') or [])}; "
        f"recommendations: {len(reviewer.get('recommendations') or [])}.",
        f"Human decision status: {decision.get('status')}; decision: {decision.get('decision')}; "
        f"decided_by: {decision.get('decided_by')}; reason: {decision.get('reason')}: "
        f"{'PASS' if decision_ok else 'FAIL (set MULTI_AGENT_WORKFLOW_ID to a decided run)'}.",
        f"Audit log: {order_detail}: {'PASS' if order_ok else 'FAIL'}.",
        f"History snapshots: {history_count}; total duration_ms: {record.get('duration_ms')}.",
        f"Confidence: {confidence}.",
        f"Worker output (first 300 chars): {worker_output!r}.",
    ])


# -- Testing review ---------------------------------------------------------

STUDENT_WORKFLOWS = {
    "aiden": ("aiden-ci.yml", "aiden/test"),
    "david": ("David-CI.yml", "david/test"),
    "ethan": ("Ethan-ci.yml", "ethan/test"),
    "janelle": ("janelle-ci.yml", "janelle/test"),
    "sophia": ("Sophia-CI.yml", "sophia/test"),
}
SECURITY_HOOKS = ("ruff", "detect-secrets", "pip-audit")


def run_command(args):
    """Run a host tool from the repository root. Returns (exit code, output).
    An exit code of None means the tool could not run, and output says why."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, cwd=REPO_ROOT,  # noqa: S603 fixed argv, no shell
                              timeout=float(os.getenv("LOOP_COMMAND_TIMEOUT", "300")))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{args[0]}: {type(exc).__name__}"
    return done.returncode, done.stdout + done.stderr


def parse_precommit(output):
    """Read `pre-commit run` output into {hook name: (result, finding lines)}.
    A hook line looks like `detect-secrets.........Failed`; the lines under a
    failed hook are its findings, except the `- hook id:` style metadata."""
    hooks, current = {}, None
    for line in output.splitlines():
        match = re.match(r"^(\S.*?)\.{3,}.*?(Passed|Failed|Skipped)$", line)
        if match:
            current = match.group(1)
            hooks[current] = (match.group(2), [])
        elif current and line.strip() and not line.startswith("- "):
            hooks[current][1].append(line.strip())
    return hooks


def latest_run(workflow, commit=None):
    """The latest GitHub Actions run of a workflow, on main or at one commit,
    through the gh CLI. Returns (run, error)."""
    where = ["--commit", commit] if commit else ["--branch", "main"]
    code, output = run_command(["gh", "run", "list", "--workflow", workflow, *where,
                                "--limit", "1", "--json", "conclusion,headSha,createdAt,url"])
    if code != 0:
        last_line = output.strip().splitlines()[-1] if output.strip() else f"gh exit {code}"
        return None, last_line
    runs = json.loads(output or "[]")
    if not runs:
        return None, "no runs found"
    return runs[0], None


def describe_run(label, workflow, run, error):
    """One evidence sentence for a (run, error) pair from latest_run."""
    if error:
        return f"{label}: {workflow} run lookup failed ({error}): FAIL."
    passed = run["conclusion"] == "success"
    return (f"{label}: {workflow} run {run['conclusion']} at {run['headSha'][:7]} on "
            f"{run['createdAt'][:10]}: {'PASS' if passed else 'FAIL'}.")


def count_endpoint_tests(folder):
    # shortcut: counts test_endpoint_* in the tree, not in the run's pytest log;
    # switch to `gh run view --log` once workflows run pytest -v
    return sum(path.read_text(encoding="utf-8").count("def test_endpoint_")
               for path in (REPO_ROOT / folder).glob("test_*.py"))


def collect_testing():
    """Run the pre-commit security scans now, then read each student's latest
    CI run from GitHub and count their endpoint tests in the tree."""
    if not (REPO_ROOT / ".pre-commit-config.yaml").exists():
        return False, ".pre-commit-config.yaml not found at the repository root."
    code, output = run_command(["pre-commit", "run", "--all-files"])
    if code is None:
        return False, f"pre-commit could not run: {output}"

    hooks = parse_precommit(output)
    evidence = []
    for wanted in SECURITY_HOOKS:
        name = next((hook for hook in hooks if wanted in hook.lower()), None)
        if name is None:
            evidence.append(f"Scan {wanted}: not configured: FAIL.")
            continue
        result, findings = hooks[name]
        first_finding = f"; first: {findings[0]}" if findings else ""
        evidence.append(f"Scan {wanted} ({name}): {result}, {len(findings)} finding lines"
                        f"{first_finding}: {'PASS' if result == 'Passed' else 'FAIL'}.")

    for student, (workflow, folder) in STUDENT_WORKFLOWS.items():
        run, error = latest_run(workflow)
        evidence.append(describe_run(f"CI {student}", workflow, run, error))
        count = count_endpoint_tests(folder)
        evidence.append(f"Endpoint tests {student}: {count} test_endpoint_ functions in {folder} "
                        f"(need 2): {'PASS' if count >= 2 else 'FAIL'}.")
    return True, " ".join(evidence)


# -- Cloud deployment review ------------------------------------------------

def bicep_params():
    """Read {name: value} from the `param` lines of bicep/main.bicep, then let
    bicep/main.bicepparam override them, as a deployment does."""
    params = {}
    for name in ("main.bicep", "main.bicepparam"):
        for line in (REPO_ROOT / "bicep" / name).read_text(encoding="utf-8").splitlines():
            match = re.match(r"^param\s+(\w+)(?:\s+\w+)?\s*=\s*(.+?)\s*$", line)
            if match:
                params[match.group(1)] = match.group(2).strip("'")
    return params


def describe_get(label, url):
    """One evidence sentence for a GET of the deployed app: status, time, and
    the row count when the body is a JSON list."""
    attempts = 2   # minReplicas is 0, so a cold start can outlast the first timeout
    for attempt in range(attempts):
        started = time.perf_counter()
        try:
            response = requests.get(url, timeout=http_timeout())
        except requests.Timeout:
            continue
        except requests.RequestException as exc:
            return f"{label} GET {url}: {type(exc).__name__}: FAIL."
        ms = (time.perf_counter() - started) * 1000
        try:
            body = response.json()
        except ValueError:
            body = None
        rows = f", JSON list of {len(body)} rows" if isinstance(body, list) else ""
        return (f"{label} GET {url}: HTTP {response.status_code} in {ms:.0f} ms{rows}: "
                f"{'PASS' if response.ok else 'FAIL'}.")
    return f"{label} GET {url}: timed out {attempts} times: FAIL."


def collect_cloud_deployment():
    """The latest cloud-deployment.yml run and CI at its commit, the AI-off
    baseline in bicep/, and one read-only probe of the deployed app."""
    deployment, error = latest_run("cloud-deployment.yml")
    evidence = [describe_run("Deployment", "cloud-deployment.yml", deployment, error)]
    if deployment:
        for student, (workflow, _folder) in STUDENT_WORKFLOWS.items():
            run, error = latest_run(workflow, deployment["headSha"])
            label = f"CI {student} at deployed commit"
            if error == "no runs found":   # path-filtered workflows skip commits outside their folder
                run, error = latest_run(workflow)
                label = f"CI {student} (latest on main; none at deployed commit)"
            evidence.append(describe_run(label, workflow, run, error))

    params = bicep_params()
    flags = {name: params.get(name) for name in ("enableMcp", "enableRag", "deployOllamaVm")}
    flags_off = all(value == "false" for value in flags.values())
    bicep_text = " ".join(path.read_text(encoding="utf-8").lower()
                          for path in (REPO_ROOT / "bicep").rglob("*.bicep*"))
    multi_agent_deployed = "multi-agent" in bicep_text or "5010" in bicep_text
    evidence.append(f"Baseline AI flags {flags} (expected false): {'PASS' if flags_off else 'FAIL'}. "
                    f"Multi-Agent app in bicep/: {multi_agent_deployed}: "
                    f"{'FAIL' if multi_agent_deployed else 'PASS'}.")

    app_url = os.getenv("CLOUD_APP_URL", "").rstrip("/")
    if not app_url:
        evidence.append("Live frontend: CLOUD_APP_URL not set: FAIL. Live CRUD: CLOUD_APP_URL not set: FAIL.")
    else:
        crud_path = os.getenv("CLOUD_CRUD_PATH", "/transactions-backend/transactions")
        evidence.append(describe_get("Live frontend", app_url))
        evidence.append(describe_get("Live CRUD", app_url + crud_path))
    return True, " ".join(evidence)


MODES = {
    "architecture": ("Architecture", "architecture", collect_architecture),
    "mcp": ("MCP validation", "mcp", collect_mcp),
    "rag": ("RAG validation", "rag", collect_rag),
    "multi_agent_review": ("Multi-Agent Workflow Review", "multi_agent_review", collect_multi_agent),
    "testing_review": ("Testing Review", "testing_review", collect_testing),
    "cloud_deployment_review": ("Cloud Deployment Review (release decision)",
                                "cloud_deployment_review", collect_cloud_deployment),
}


# --- the loop --------------------------------------------------------------

def stage(record, label, step, message):
    print(f"[{label}][{step}] {message}")
    record.stage(step, message)


def ask(prompt_text):
    try:
        return input(prompt_text).strip()
    except EOFError:                     # piped input ran out / Ctrl+D
        return None


def adapt(record, label, finding):
    """ADAPT: the human accepts, rejects, or edits the finding; recorded."""
    stage(record, label, "ADAPT", "Human review of the finding")
    print("-" * 70)
    print("FINDING UNDER REVIEW:")
    print(finding)
    print("-" * 70)
    while True:
        answer = ask("Accept, reject, or edit this finding? [a/r/e]: ")
        if answer is None:
            record.decision("abandoned", "input closed before a decision")
            return "abandoned", None
        choice = answer.lower()
        if choice in ("a", "accept"):
            record.decision("accepted", None)
            return "accepted", None
        if choice in ("r", "reject"):
            reason = ask("Why is it rejected? (recorded): ") or None
            record.decision("rejected", reason)
            return "rejected", reason
        if choice in ("e", "edit"):
            edit = ask("Enter the corrected finding: ") or ""
            record.decision("edited", edit)
            return "edited", edit
        print("Please answer a, r, or e.")


def run_review(key, record):
    label, family, collect = MODES[key]
    record.start_mode(key, label)
    stage(record, label, "PLAN", f"Review target: {label}; prompts: prompts/{family}/")

    stage(record, label, "OBSERVE", "Collecting evidence")
    ok, evidence = collect()
    record.set(evidence=evidence)
    if not ok:
        stage(record, label, "OBSERVE", "Failed")
        record.end_mode()
        return f"OBSERVE FAILED: {evidence}"
    stage(record, label, "OBSERVE", "Complete")

    system_prompt = read_prompt(family, "implementation/system_prompt.txt")
    task_prompt = read_prompt(family, "implementation/task_prompt.txt")
    stage(record, label, "ACT", "Running implementation model")
    output, err = call_model(system_prompt, f"{task_prompt}\n\nEvidence:\n{evidence}")
    if err:
        stage(record, label, "ACT", "Failed")
        record.set(implementation_output=err)
        record.end_mode()
        return f"MODEL FAILED: {err}"
    record.set(implementation_output=output)

    review_system = read_prompt(family, "review/review_prompt.txt")
    stage(record, label, "ACT", "Running review model")
    review, review_err = call_model(
        review_system, f"Implementation finding:\n{output}\n\nEvidence:\n{evidence}", review=True)
    # unlike ACT above, a review failure is not fatal: the error text is shown
    # as the review and the human still gets to decide
    if review_err:
        review = review_err
    record.set(review_output=review)

    finding = f"{output}\n\nREVIEW MODEL: {review}"
    decision, edit = adapt(record, label, finding)
    record.end_mode()
    final = edit if decision == "edited" else finding
    return f"OBSERVE: {evidence}\n\nFINDING: {final}\n\nHUMAN DECISION: {decision}"


def main():
    load_dotenv(REPO_ROOT / ".env")      # optional file; a missing one is ignored
    reports_dir = REPO_ROOT / "reports"
    record = RunRecord(reports_dir)
    keys = list(MODES)

    print("AGENTIC LOOP - shared review workflow (Plan -> Act -> Observe -> Adapt)")
    print(f"Run record: {reports_dir.relative_to(REPO_ROOT).as_posix()}")
    while True:
        print()
        print("=" * 70)
        for number, key in enumerate(keys, start=1):
            print(f"{number} - {MODES[key][0]}")
        print("0 - Exit")
        print("=" * 70)
        choice = ask("Choose a review target: ")
        if choice is None or choice == "0":
            print("Loop closed.")
            break
        if choice.isdigit() and 1 <= int(choice) <= len(keys):
            print()
            print(run_review(keys[int(choice) - 1], record))
        else:
            print(f"Choose 0-{len(keys)}.")


if __name__ == "__main__":
    main()
