from __future__ import annotations

import requests

from .. import config
from ..db_api import ServiceError


def chat(model, messages, timeout=None):
    """POST a chat completion to Ollama; ServiceError (503 ai_disabled) when AI mode is off."""
    if not config.AI_MODE_ENABLED:
        raise ServiceError("AI mode is disabled.", 503, "ai_disabled")
    response = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.2},
        },
        timeout=timeout or config.AI_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()

