from __future__ import annotations

import json

from .. import config
from ..db_api import ServiceError
from .ollama_client import chat


def run(model, prompt_builder, validator, fallback, timeout=None):
    """Call model up to twice, validating each response.

    Raises ServiceError (503 ai_disabled) before any attempt when AI mode is
    off, so a disabled deployment refuses rather than quietly serving the
    fallback as if the model had answered.
    """
    if not config.AI_MODE_ENABLED:
        raise ServiceError("AI mode is disabled.", 503, "ai_disabled")
    error = None
    for _ in range(2):
        messages = prompt_builder(error)
        try:
            response = chat(model, messages, timeout=timeout)
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

