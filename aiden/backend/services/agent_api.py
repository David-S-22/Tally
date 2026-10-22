from datetime import datetime
from json import JSONDecodeError
from typing import Optional
from dataclasses import dataclass
from uuid import uuid4

from flask import current_app

from shared.backend import dto
from ..config import config
from .ollama_api import prompt
from . import mcp_client
from . import multi_agent_client
from . import rag_client
from ..helpers import serialise

_CONFIRMED_ANOMALIES_TOOL = "get_transactions_with_confirmed_anomalies"
_REJECTED_ANOMALIES_TOOL = "get_transactions_with_rejected_anomalies"

_detect_system_prompt = """

You are a skeptical transaction anomaly-detection agent.
Your task is to decide whether a single financial transaction looks suspicious based ONLY on the information provided in the transaction.
Everything you flag is shown directly to the user for review. Because the user sees your findings, err on the side of caution: only flag a transaction when there is a clear, defensible reason, and keep your explanation accurate and easy for a person to verify. Avoid flooding the user with weak or speculative flags.
Today's date is {0}. Use this information when evaluating the transaction.

You have these fields:

- id: the unique transaction identifier
- amount: the transaction amount
- merchant: the merchant name
- description: the transaction description
- date: the transaction timestamp

Consider which properties of the transaction could indicate an anomaly:
- unusually large or round amounts (e.g. very high values, or suspiciously round numbers)
- cash-like, high-risk, generic, or unfamiliar merchant names (e.g. ATMs, cash transfers, gift cards, crypto, vague names)
- unusual or inconsistent date/time information (e.g. odd hours, future dates)
- combinations of the above that together look anomalous

Analyze the transaction using only the supplied properties and make a final determination. If your explanation describes a genuinely unusual, risky, or noteworthy characteristic, set "is_suspicious": true. Never describe something as unusual or risky while also marking it not suspicious — that is a contradiction.

Guidance:
- A large amount is a legitimate reason to flag a transaction.
- A cash-like, generic, or unfamiliar merchant is a legitimate reason to flag a transaction.
- Only mark a transaction not suspicious when nothing about the supplied fields stands out as unusual.
- Do not claim fraud as a fact and do not invent missing context; describe only what the supplied fields show.
- Keep the explanation concise and factual, and between 30 and 50 words long.

Before evaluating the transaction, review the reviewed-example transactions
provided in the user message:
- "Confirmed suspicious examples" are transactions the user agreed were suspicious.
- "Rejected (not suspicious) examples" are transactions the user decided were not suspicious.
Use the confirmed examples as positive examples and the rejected examples as
negative examples. Use this feedback to align your judgement with the user's,
but still evaluate the current transaction on its own merits.

The user message may also include a "Reference guidance" section containing
excerpts from fraud/anomaly reference documents retrieved for this transaction.
Ground your reasoning in that guidance where it applies, but never fabricate
details it does not support.

Return ONLY valid JSON matching this schema:

{{
  "is_suspicious": boolean,
  "justification": string
}}

Always populate "justification" with a concise explanation of your decision, between 30 and 50 words long — even when "is_suspicious" is false, briefly state why the transaction looks legitimate. Never leave it empty.

Do not return Markdown, code fences, commentary, or any additional fields.
"""

_detect_user_prompt = """
{1}{2}
Review the following transaction. Determine whether this transaction is suspicious according to your instructions.

{0}
"""

class CouldNotParseAgentResponseException(Exception):
    pass

@dataclass(frozen=True)
class ReviewFinding:
    is_suspicious: bool
    justification: str
    confidence: Optional[float] = None
    workflow_id: Optional[str] = None

def review_new_transaction(
    transaction: dto.Transaction,
) -> Optional[dto.Anomaly]:
    iteration = 1
    serialised = serialise(transaction)
    detect_system_prompt = _detect_system_prompt.format(datetime.now().strftime("%Y-%m-%d"))
    examples_context = _build_reviewed_examples_context()
    reference_context, reference_sources = _build_reference_context(transaction)
    detect_user_prompt = _detect_user_prompt.format(serialised, examples_context, reference_context)
    review_finding: Optional[ReviewFinding] = None

    current_app.logger.info("Scan new transaction %s", serialised)
    current_app.logger.info(
        "Full anomaly prompt for transaction %s:\n"
        "----- SYSTEM PROMPT -----\n%s\n"
        "----- USER PROMPT (includes MCP-retrieved examples and RAG reference guidance) -----\n%s",
        transaction.id,
        detect_system_prompt,
        detect_user_prompt,
    )

    if config.MULTI_AGENT_ENABLED:
        review_finding = _review_with_multi_agent(
            transaction,
            examples_context,
            reference_context,
            reference_sources,
        )
        if not review_finding.is_suspicious:
            current_app.logger.info(
                "Not classified suspicious, no anomaly. Justification: %s",
                review_finding.justification,
            )
            return None
        return dto.Anomaly(
            id=0,
            transaction_id=transaction.id,
            agent_reason_suspected=review_finding.justification,
            is_confirmed_by_user=None,
            confidence=review_finding.confidence,
            sources=reference_sources,
            workflow_id=review_finding.workflow_id,
        )

    while iteration < 5:
        temperature = 0.2 * iteration # increase as it gets iterated
        result = prompt(
            system_prompt=detect_system_prompt,
            user_prompt=detect_user_prompt,
            model=config.OLLAMA_MODEL,
            temperature=temperature,
            output_tokens=500)

        if (candidate := parse_review_finding(result.text)) is None:
            current_app.logger.info("Response is not acceptable json %s", result.text)
            iteration += 1
            continue

        review_finding = ReviewFinding(
            candidate.is_suspicious,
            candidate.justification,
            result.mean_confidence,
        )
        current_app.logger.info("Response was formatted into finding %s", review_finding)
        break

    if review_finding is None:
        raise CouldNotParseAgentResponseException()

    if not review_finding.is_suspicious:
        current_app.logger.info("Not classified suspicious, no anomaly. Justification: %s", review_finding.justification)
        return None

    return dto.Anomaly(
        id=0,
        transaction_id=transaction.id,
        agent_reason_suspected=review_finding.justification,
        is_confirmed_by_user=None,
        confidence=review_finding.confidence,
        sources=reference_sources,
    )


def _review_with_multi_agent(
    transaction: dto.Transaction,
    examples_context: str,
    reference_context: str,
    reference_sources: list[str],
) -> ReviewFinding:
    source = f"transaction:{transaction.id}:{uuid4().hex}"
    evidence = [{
        "source": source,
        "summary": (
            f"Transaction {transaction.id}: {transaction.date}; "
            f"{transaction.merchant}; {transaction.description}; "
            f"amount {transaction.amount}."
        ),
        "data": serialise(transaction),
    }]
    if examples_context.strip():
        evidence.append({
            "source": "reviewed-examples",
            "summary": examples_context.strip()[:4000],
        })
    if reference_context.strip():
        evidence.append({
            "source": "rag:reference-guidance",
            "summary": reference_context.strip()[:4000],
            "data": {"sources": reference_sources},
        })

    workflow_request = {
        "contract_version": "1.0",
        "feature": "anomalies",
        "task": (
            "Assess whether the supplied transaction is suspicious using only "
            "the evidence. Return structured output with exactly these fields: "
            '{"is_suspicious": boolean, "justification": string}. Keep the '
            "justification factual, concise, and 30 to 50 words. Do not claim "
            "fraud as fact or invent missing context. Use confirmed and "
            "rejected examples as positive and negative guidance."
        ),
        "evidence": evidence,
        "context": {
            "constraints": (
                "Do not take action on the transaction. The user will review "
                "the finding."
            ),
        },
        "options": {},
    }
    try:
        workflow = multi_agent_client.create_workflow(workflow_request)
    except multi_agent_client.MultiAgentError as error:
        if error.code != "multi_agent_timeout":
            raise
        try:
            latest = multi_agent_client.latest_workflow("anomalies")
        except multi_agent_client.MultiAgentError:
            latest = None
        latest_evidence = latest.get("evidence") if latest else None
        if (
            latest
            and isinstance(latest_evidence, list)
            and any(
                item.get("source") == source
                for item in latest_evidence
                if isinstance(item, dict)
            )
        ):
            workflow = latest
        else:
            raise

    workflow_id = workflow.get("workflow_id")
    worker = workflow.get("worker_result")
    if (
        workflow.get("status") != "success"
        or not isinstance(workflow_id, str)
        or not workflow_id.strip()
        or not isinstance(worker, dict)
        or worker.get("status") != "success"
        or not isinstance(workflow.get("reviewer_result"), dict)
        or workflow["reviewer_result"].get("status") != "success"
    ):
        raise multi_agent_client.MultiAgentError(
            "multi_agent_invalid_response",
        )

    structured = worker.get("structured")
    if (
        isinstance(structured, dict)
        and isinstance(structured.get("is_suspicious"), bool)
        and isinstance(structured.get("justification"), str)
    ):
        finding_data = structured
    else:
        output = worker.get("output")
        if not isinstance(output, str):
            raise multi_agent_client.MultiAgentError(
                "multi_agent_invalid_response",
            )
        try:
            finding_data = current_app.json.loads(output)
        except (ValueError, RecursionError):
            finding_data = None
    if (
        not isinstance(finding_data, dict)
        or not isinstance(finding_data.get("is_suspicious"), bool)
        or not isinstance(finding_data.get("justification"), str)
        or not finding_data["justification"].strip()
    ):
        raise multi_agent_client.MultiAgentError(
            "multi_agent_invalid_response",
        )

    return ReviewFinding(
        is_suspicious=finding_data["is_suspicious"],
        justification=finding_data["justification"].strip(),
        workflow_id=workflow_id,
    )


def _build_reference_context(transaction: dto.Transaction) -> tuple[str, list[str]]:
    """Retrieve RAG reference guidance for a transaction.

    Returns a formatted prompt section and the de-duplicated list of source
    filenames the guidance came from (closest first). If RAG is disabled or the
    server is unavailable the review still proceeds without guidance, so a
    transient RAG failure never blocks anomaly detection.
    """

    question = _reference_question(transaction)

    try:
        results = rag_client.retrieve(config.RAG_FEATURE, question, config.RAG_TOP_K)
    except rag_client.RAGError as error:
        current_app.logger.warning(
            "RAG reference retrieval unavailable, continuing without guidance: %s",
            error.code,
        )
        return "", []

    if not results:
        return "", []

    sources: list[str] = []
    lines = ["Reference guidance (excerpts from fraud/anomaly reference documents):"]
    for index, item in enumerate(results, start=1):
        source = item["metadata"].get("source")
        if isinstance(source, str) and source and source not in sources:
            sources.append(source)
        lines.append(f"[{index}] ({source or 'unknown source'}) {_excerpt(item['text'])}")

    return "\n".join(lines) + "\n", sources


def _reference_question(transaction: dto.Transaction) -> str:
    return (
        f"{transaction.merchant} {transaction.description} "
        f"amount {transaction.amount}"
    ).strip()


def _excerpt(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"

def _build_reviewed_examples_context() -> str:
    """Fetch reviewed examples from the MCP server and format them for the prompt.

    Mirrors the other services' approach of calling the shared MCP server through
    ``fastmcp`` directly. If the server is unavailable the review still proceeds
    without examples, so a transient MCP failure never blocks anomaly detection.
    """

    confirmed = _fetch_examples(_CONFIRMED_ANOMALIES_TOOL)
    rejected = _fetch_examples(_REJECTED_ANOMALIES_TOOL)

    if not confirmed and not rejected:
        return ""

    sections = [
        _format_examples_section(
            "Confirmed suspicious examples (the user agreed these were suspicious):",
            confirmed,
        ),
        _format_examples_section(
            "Rejected (not suspicious) examples (the user decided these were fine):",
            rejected,
        ),
    ]
    return "\n".join(section for section in sections if section) + "\n"


def _fetch_examples(tool_name: str) -> list[dict]:
    try:
        data, _duration_ms = mcp_client.call_tool(tool_name)
    except mcp_client.MCPError as error:
        current_app.logger.warning(
            "MCP tool %s unavailable, continuing without examples: %s",
            tool_name,
            error.code,
        )
        return []

    return data if isinstance(data, list) else []


def _format_examples_section(heading: str, examples: list[dict]) -> str:
    if not examples:
        return ""

    lines = [heading]
    for example in examples:
        transaction = example.get("transaction", {}) if isinstance(example, dict) else {}
        anomaly = example.get("anomaly", {}) if isinstance(example, dict) else {}
        lines.append(
            "- amount={amount}, merchant={merchant!r}, description={description!r}, "
            "date={date}; reason={reason!r}".format(
                amount=transaction.get("amount"),
                merchant=transaction.get("merchant"),
                description=transaction.get("description"),
                date=transaction.get("date"),
                reason=anomaly.get("agent_reason_suspected"),
            )
        )
    return "\n".join(lines)


def parse_review_finding(model_response: str) -> Optional[ReviewFinding]:
    """Determines if the model response was a valid format"""

    try:
        data = current_app.json.loads(model_response)
    except JSONDecodeError:
        return None

    if (
        isinstance(data, dict) and
        isinstance(is_suspicious := data.get("is_suspicious"), bool) and
        isinstance(justification := data.get("justification"), str)
    ):
        return ReviewFinding(is_suspicious, justification)

    return None
