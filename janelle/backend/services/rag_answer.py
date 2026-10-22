"""Grounded category choice over the two Transactions RAG collections.

The shared RAG server is retrieval-only, so this module owns the grounded
step: retrieve from the records and guide collections, drop weak matches,
ask Ollama to pick one live category name citing numbered context, verify
the answer and its markers, and derive a deterministic confidence band.

Confidence starts from the retrieval distances and is then gated by the
model's own log probabilities for the category-name tokens (Ollama
``logprobs``), so a close match the model was unsure about cannot stay
``high``. When the server does not return log probabilities the gate is
skipped and ``model_probability`` is ``None``.

Every failure that stops grounding raises ``RAGError`` so the caller can
degrade to the Release 0 correction vote.
"""
import math
import numbers
import re

import requests

from .. import config
from . import rag_client
from .rag_client import RAGError


EXCERPT_LIMIT = 200
_MARKER = re.compile(r"\[(\d+)\]")
_CONFIDENCE_ORDER = ("high", "medium", "low")

SYSTEM_PROMPT = (
    "You classify a bank transaction into exactly one category. "
    "Use only the numbered context lines you are given; do not use outside "
    "knowledge. Choose exactly one category name from the provided list and "
    "copy it exactly. Answer on one line as: <Category name> [n][m] where "
    "each [n] is the number of a context line that supports the choice. "
    "If the context does not support any listed category, answer NONE."
)


def grounded_answer(question, categories):
    """Return the grounding dict for ``question`` over the live categories."""
    records = [
        {**item, "collection": config.RAG_RECORDS_COLLECTION}
        for item in rag_client.retrieve(
            config.RAG_RECORDS_COLLECTION,
            question,
            config.RAG_TOP_K,
        )
    ]
    guide = [
        {**item, "collection": config.RAG_GUIDE_COLLECTION}
        for item in rag_client.retrieve(
            config.RAG_GUIDE_COLLECTION,
            question,
            config.RAG_GUIDE_TOP_K,
        )
    ]
    retrieved = len(records) + len(guide)
    survivors = [
        item
        for item in sorted(records + guide, key=lambda item: item["distance"])
        if item["distance"] <= config.RAG_LOW
    ]
    if not survivors:
        return insufficient_result(retrieved, None)

    live = {
        category["name"].casefold(): category
        for category in categories
        if isinstance(category.get("name"), str)
    }
    live_ids = {category["id"] for category in live.values()}
    content, logprobs = ask_model(question, survivors, [
        category["name"] for category in live.values()
    ])
    answer_name, markers = parse_answer(content)
    chosen = live.get(answer_name.casefold()) if answer_name else None
    model_probability = answer_probability(content, logprobs)

    best = survivors[0]["distance"]
    confidence = base_confidence(best, len(survivors))
    uncited = False
    derived_from_votes = False
    cited = []

    if chosen is not None:
        confidence = apply_probability_gate(confidence, model_probability)
        cited = [
            survivors[marker - 1]
            for marker in markers
            if 1 <= marker <= len(survivors)
            and marker_agrees(survivors[marker - 1], chosen["id"])
        ]
        if not cited:
            uncited = True
            cited = [
                item
                for item in survivors
                if item["collection"] == config.RAG_RECORDS_COLLECTION
                and item["metadata"].get("category_id") == chosen["id"]
            ]
            if cited:
                confidence = downgrade(confidence)
            else:
                chosen = None

    if chosen is None:
        voted = majority_vote(survivors, live_ids)
        if voted is None:
            return insufficient_result(retrieved, best)
        category_id, cited = voted
        chosen = next(
            category
            for category in live.values()
            if category["id"] == category_id
        )
        derived_from_votes = True
        uncited = False
        confidence = downgrade(confidence)

    return {
        "status": "grounded",
        "answer": chosen["name"],
        "confidence": confidence,
        "insufficient_context": False,
        "uncited": uncited,
        "derived_from_votes": derived_from_votes,
        "citations": build_citations(cited),
        "retrieved": retrieved,
        "survivors": len(survivors),
        "best_distance": round(best, 4),
        "model_probability": model_probability,
        "model": config.RAG_MODEL,
        "thresholds": thresholds(),
    }


def insufficient_result(retrieved, best):
    return {
        "status": "insufficient",
        "answer": None,
        "confidence": "insufficient",
        "insufficient_context": True,
        "uncited": False,
        "derived_from_votes": False,
        "citations": [],
        "retrieved": retrieved,
        "survivors": 0,
        "best_distance": None if best is None else round(best, 4),
        "model_probability": None,
        "model": config.RAG_MODEL,
        "thresholds": thresholds(),
    }


def thresholds():
    return {
        "insufficient_above": config.RAG_LOW,
        "high_below": config.RAG_HIGH,
        "medium_below": config.RAG_MEDIUM,
        "probability_high_at_least": config.RAG_PROB_HIGH,
        "probability_medium_at_least": config.RAG_PROB_MEDIUM,
    }


def ask_model(question, survivors, category_names):
    """Send the closed-set prompt to Ollama and return the reply text; RAGError("ai_disabled") when AI mode is off."""
    if not config.AI_MODE_ENABLED:
        raise RAGError("ai_disabled")
    context_lines = "\n".join(
        f"[{index}] {excerpt(item['text'])}"
        for index, item in enumerate(survivors, start=1)
    )
    user_prompt = (
        "Categories:\n"
        + "\n".join(f"- {name}" for name in category_names)
        + "\n\nContext:\n"
        + context_lines
        + f"\n\nTransaction to categorise: {question}"
    )
    payload = {
        "model": config.RAG_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0},
    }
    if config.RAG_LOGPROBS:
        payload["logprobs"] = True
    try:
        response = requests.post(
            f"{config.OLLAMA_URL}/api/chat",
            json=payload,
            timeout=config.AI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        content = body["message"]["content"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise RAGError("rag_generation_failed") from None
    if not isinstance(content, str):
        raise RAGError("rag_generation_failed")
    logprobs = body.get("logprobs") if config.RAG_LOGPROBS else None
    return content, logprobs if isinstance(logprobs, list) else None


def answer_probability(content, logprobs):
    """Return the joint probability of the category-name tokens, or None.

    Ollama returns one ``{"token", "logprob"}`` entry per generated token.
    The tokens are walked in order until their concatenation covers the
    answer text before the first ``[`` marker; whitespace-only tokens and the
    markers themselves do not count. A missing or malformed list yields
    ``None`` so the distance-only band is kept.
    """
    if not logprobs:
        return None
    name_end = len(content.split("[", 1)[0].rstrip())
    if name_end == 0:
        return None
    covered = 0
    total = 0.0
    counted = 0
    for item in logprobs:
        if not isinstance(item, dict):
            return None
        token = item.get("token")
        logprob = item.get("logprob")
        if (
            not isinstance(token, str)
            or not isinstance(logprob, numbers.Real)
            or isinstance(logprob, bool)
        ):
            return None
        if covered >= name_end:
            break
        covered += len(token)
        if token.strip():
            total += float(logprob)
            counted += 1
    if counted == 0:
        return None
    return round(min(1.0, math.exp(total)), 4)


def apply_probability_gate(confidence, probability):
    """Downgrade a band the model itself was not confident enough about."""
    if probability is None:
        return confidence
    if confidence == "high" and probability < config.RAG_PROB_HIGH:
        confidence = "medium"
    if confidence == "medium" and probability < config.RAG_PROB_MEDIUM:
        confidence = "low"
    return confidence


def parse_answer(content):
    """Split ``"Fitness [1][3]"`` into ``("Fitness", [1, 3])``."""
    first_line = content.strip().splitlines()[0] if content.strip() else ""
    name = first_line.split("[", 1)[0].strip().strip(".:\"'")
    markers = []
    for match in _MARKER.finditer(content):
        marker = int(match.group(1))
        if marker not in markers:
            markers.append(marker)
    if name.casefold() == "none":
        name = ""
    return name, markers


def marker_agrees(item, category_id):
    cited_id = item["metadata"].get("category_id")
    return cited_id is None or cited_id == category_id


def base_confidence(best, survivor_count):
    if best < config.RAG_HIGH and survivor_count >= 2:
        return "high"
    if best < config.RAG_MEDIUM:
        return "medium"
    return "low"


def downgrade(confidence):
    index = _CONFIDENCE_ORDER.index(confidence)
    return _CONFIDENCE_ORDER[min(index + 1, len(_CONFIDENCE_ORDER) - 1)]


def majority_vote(survivors, live_ids):
    """Vote over surviving records; return ``(category_id, records)``."""
    votes = {}
    for item in survivors:
        if item["collection"] != config.RAG_RECORDS_COLLECTION:
            continue
        category_id = item["metadata"].get("category_id")
        if isinstance(category_id, bool) or category_id not in live_ids:
            continue
        votes.setdefault(category_id, []).append(item)
    if not votes:
        return None
    winner = max(
        votes,
        key=lambda category_id: (
            len(votes[category_id]),
            -min(item["distance"] for item in votes[category_id]),
        ),
    )
    return winner, votes[winner]


def build_citations(items):
    citations = []
    for marker, item in enumerate(items, start=1):
        metadata = item["metadata"]
        citation = {
            "marker": marker,
            "id": item["id"],
            "collection": item["collection"],
            "source": citation_source(item),
            "excerpt": excerpt(item["text"]),
            "distance": round(item["distance"], 4),
        }
        for key in ("category_id", "category", "date", "merchant", "amount"):
            if metadata.get(key) is not None:
                citation[key] = metadata[key]
        citations.append(citation)
    return citations


def citation_source(item):
    metadata = item["metadata"]
    kind = metadata.get("kind")
    if kind == "transaction":
        return f"transaction {metadata.get('transaction_id', item['id'])}"
    if kind == "correction":
        return f"correction {item['id'].split('-', 1)[-1]}"
    source = metadata.get("source")
    return source if isinstance(source, str) and source else item["id"]


def excerpt(text):
    text = " ".join(str(text).split())
    if len(text) <= EXCERPT_LIMIT:
        return text
    return text[: EXCERPT_LIMIT - 1].rstrip() + "…"
