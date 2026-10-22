"""AI mode is a real gate: with the switch off no model call leaves the process."""
import pytest

from backend.savings_service import ollama_service
from backend.savings_service.ollama_service import AIError


def test_client_refuses_when_ai_mode_is_off(monkeypatch):
    monkeypatch.setattr(ollama_service, "AI_MODE_ENABLED", False)
    with pytest.raises(AIError) as caught:
        ollama_service._client()
    assert caught.value.code == "ai_disabled"
    assert caught.value.status == 503


@pytest.mark.parametrize(
    "call",
    [
        lambda: ollama_service.get_ai_text_response(prompt="p", model="m", temperature=0.0),
        lambda: ollama_service.get_ai_json_response(prompt="p", model="m", temperature=0.0),
    ],
)
def test_entry_points_refuse_when_ai_mode_is_off(monkeypatch, call):
    monkeypatch.setattr(ollama_service, "AI_MODE_ENABLED", False)

    class Exploding:
        def __getattr__(self, name):
            raise AssertionError("AI mode is off; the client must not be touched")

    monkeypatch.setattr(ollama_service, "client", Exploding())
    with pytest.raises(AIError):
        call()


def test_client_is_returned_when_ai_mode_is_on(monkeypatch):
    monkeypatch.setattr(ollama_service, "AI_MODE_ENABLED", True)
    sentinel = object()
    monkeypatch.setattr(ollama_service, "client", sentinel)
    assert ollama_service._client() is sentinel
