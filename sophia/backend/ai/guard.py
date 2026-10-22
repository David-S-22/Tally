"""Wraps an Ollama call with validation, one retry, and a safe fallback."""
import json

from sophia.backend import config
from sophia.backend.ai.ollama_client import chat
from sophia.backend.services.errors import ModeError


def run(model, prompt_builder, validator, fallback, timeout=None, temperature=None):
    """Call model via prompt_builder(error) up to twice, validating each response.

    prompt_builder(error) returns a list of chat messages; error is None on the
    first attempt and the previous failure's message on the retry. Returns the
    validated response dict with fallback=False, or a copy of fallback with
    fallback=True if both attempts fail. temperature is passed to chat only
    when it is not None. Raises ModeError("ai_disabled") before any attempt
    when AI mode is off, so a disabled deployment refuses rather than falling
    back to canned text.
    """
    if not config.AI_MODE_ENABLED:
        raise ModeError("ai_disabled")
    chat_kwargs = {"timeout": timeout}
    if temperature is not None:
        chat_kwargs["temperature"] = temperature
    error = None
    for _ in range(2):
        messages = prompt_builder(error)
        try:
            response = chat(model, messages, **chat_kwargs)
            content = response.get("message", {}).get("content", "")
            data = json.loads(content)
        except Exception as exc:
            error = str(exc)
            continue
        error = validator(data)
        if error is None:
            result = dict(data)
            result["fallback"] = False
            return result
    result = dict(fallback)
    result["fallback"] = True
    return result
