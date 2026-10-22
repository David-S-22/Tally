"""Minimal client for the local Ollama chat API."""
import requests

from sophia.backend import config
from sophia.backend.services.errors import ModeError


def chat(model, messages, timeout=None, temperature=None):
    """POST a chat completion request to Ollama and return the parsed JSON response body; temperature defaults to config.AI_TEMPERATURE. Raises ModeError("ai_disabled") when AI mode is off."""
    if not config.AI_MODE_ENABLED:
        raise ModeError("ai_disabled")
    response = requests.post(
        f"{config.OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "format": "json",
            "stream": False,
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {"temperature": config.AI_TEMPERATURE if temperature is None else temperature},
        },
        timeout=timeout or config.AI_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def warm(model):
    """Ask Ollama to load a model without generating (an empty prompt), so the next real call does not wait for it; a no-op when AI mode is off, and failures are ignored."""
    if not config.AI_MODE_ENABLED:
        return None
    try:
        requests.post(f"{config.OLLAMA_URL}/api/generate", json={"model": model, "prompt": "", "keep_alive": config.OLLAMA_KEEP_ALIVE}, timeout=config.AI_TIMEOUT_SECONDS)
    except Exception:
        return None
