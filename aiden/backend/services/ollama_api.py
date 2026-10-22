import logging
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

from openai import OpenAI

from ..config import config


logger = logging.getLogger(__name__)

# Number of top alternative tokens to request alongside each generated token.
_TOP_LOGPROBS = 1


@dataclass(frozen=True)
class PromptResult:
    """The model's answer plus a confidence signal derived from log probs.

    ``mean_confidence`` is the mean per-token probability of the generated
    answer (each token's probability is ``exp(logprob)``), a value in ``(0, 1]``
    where higher means the model was more certain. It is ``None`` when the model
    server does not return log probabilities.
    """

    text: str
    mean_confidence: Optional[float]


def prompt(*, system_prompt: str, user_prompt: str, model: str, temperature: float, output_tokens: int) -> PromptResult:
    # The client instance is cached between calls, as a singleton instance.

    client = _get_client()

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=output_tokens,
        temperature=temperature,
        logprobs=True,
        top_logprobs=_TOP_LOGPROBS,
    )

    model_text = _extract_response_text(response)
    logger.debug("Model response for %s: %s", model, model_text)
    mean_confidence = _extract_mean_confidence(response)
    logger.debug("Model mean confidence for %s: %s", model, mean_confidence)

    return PromptResult(text=model_text, mean_confidence=mean_confidence)


def _response_payload(response: Any) -> Any:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if isinstance(response, dict):
        return response
    return vars(response)


def _extract_response_text(response: Any) -> str:
    """Return the final answer text, falling back to the raw response payload."""

    choices = getattr(response, "choices", None)
    if isinstance(choices, list) and choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()

    payload = _response_payload(response)
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if (
            isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
        ):
            message = choices[0].get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output = payload.get("output", payload) if isinstance(payload, dict) else payload
    texts: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "text" and isinstance(child, str) and child.strip():
                    texts.append(child.strip())
                else:
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(output)
    return "\n".join(texts)


def _extract_mean_confidence(response: Any) -> Optional[float]:
    """Return the mean per-token probability of the generated answer.

    Chat Completions attaches a ``logprob`` to each output token. Each token's
    probability is ``exp(logprob)``; we average these to gauge how confident the
    model was overall. Nested ``top_logprobs`` entries (alternative tokens) are
    skipped so only the tokens the model actually produced are counted. Returns
    ``None`` when the server does not provide log probabilities.
    """

    probabilities: list[float] = []
    logprob_arrays: list[tuple[str, str, Optional[int]]] = []

    def collect(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "top_logprobs":
                    continue
                child_path = f"{path}.{key}"
                if key == "logprobs":
                    entry_count = len(child) if isinstance(child, list) else None
                    logprob_arrays.append(
                        (child_path, type(child).__name__, entry_count)
                    )
                if (
                    key == "logprob"
                    and isinstance(child, (int, float))
                    and not isinstance(child, bool)
                ):
                    probabilities.append(math.exp(float(child)))
                else:
                    collect(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                collect(child, f"{path}[{index}]")

    collect(_response_payload(response))
    logger.debug(
        "Logprob extraction: fields=%s, generated-token scores=%d",
        logprob_arrays,
        len(probabilities),
    )

    if not probabilities:
        return None

    return sum(probabilities) / len(probabilities)


@lru_cache(maxsize=1)
def _get_client() -> OpenAI:
    return OpenAI(
        base_url=config.OLLAMA_URL,
        api_key="ollama",
        timeout=config.OLLAMA_TIMEOUT_SECONDS,
    )
