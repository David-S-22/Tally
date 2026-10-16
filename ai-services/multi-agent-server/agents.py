"""The three agents. Stub implementation: deterministic, no model calls.

Each function has the signature the real implementation keeps; only the
bodies change when Ethan wires in Ollama (planner and worker on
``qwen2.5:0.5b``, reviewer on ``llama3.1:8b``). Every result carries the
fields the contract promises (docs/release-2/shared/adr-multi-agent-server-contract.md,
"HTTP contract") so clients built against the stub keep working.
"""

MODEL = "stub"


def plan(task, evidence, context):
    """Planner: turn the task into steps and name the evidence it needs."""
    sources = [item["source"] for item in evidence]
    return {
        "status": "success",
        "agent": "planner_agent",
        "model": MODEL,
        "plan": [
            f"Read the evidence: {', '.join(sources) or 'none supplied'}",
            f"Produce the output for: {task}",
            "List every claim the evidence does not support",
        ],
        "required_evidence": sources,
        "human_approval_required": True,
    }


def work(task, evidence, context, plan_result):
    """Worker: produce the output from the plan and the evidence only."""
    summaries = [item["summary"] for item in evidence]
    output = f"[stub] {task}\n" + "\n".join(f"- {line}" for line in summaries)
    return {
        "status": "success",
        "agent": "worker_agent",
        "model": MODEL,
        "evidence_count": len(evidence),
        "evidence_used": [item["source"] for item in evidence],
        "output": output,
        "structured": None,
        "human_approval_required": True,
    }


def review(task, evidence, plan_result, worker_result):
    """Reviewer: check the worker output against the plan and the evidence."""
    risks = []
    if worker_result["evidence_count"] == 0:
        risks.append("No evidence was supplied; the output cannot be verified.")
    return {
        "status": "success",
        "agent": "reviewer_agent",
        "model": MODEL,
        "verdict": "approve_with_changes" if risks else "approve",
        "risks": risks,
        "recommendations": ["Human to confirm before use."] if risks else [],
        "review": "[stub] Output follows the plan and cites only the supplied evidence.",
        "human_approval_required": True,
    }
