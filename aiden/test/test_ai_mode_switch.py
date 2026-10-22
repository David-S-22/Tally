"""AI mode is a real gate: with the switch off no model call is made."""
import pytest

from backend.config import config
from backend.services import ollama_api
from backend.services.ollama_api import AIError


def test_prompt_refuses_before_building_a_client_when_ai_mode_is_off(monkeypatch):
    monkeypatch.delenv("AI_MODE_ENABLED", raising=False)

    def never(*args, **kwargs):
        raise AssertionError("AI mode is off; no client may be built")

    monkeypatch.setattr(ollama_api, "_get_client", never)
    with pytest.raises(AIError) as caught:
        ollama_api.prompt(
            system_prompt="s", user_prompt="u", model="m", temperature=0.0, output_tokens=10
        )
    assert caught.value.code == "ai_disabled"


def test_ai_mode_is_off_when_the_variable_is_absent(monkeypatch):
    """Absent means off: an unconfigured deployment refuses rather than dialling OLLAMA_URL."""
    monkeypatch.delenv("AI_MODE_ENABLED", raising=False)
    assert config.AI_MODE_ENABLED is False


def test_ai_mode_reads_the_variable_when_set(monkeypatch):
    monkeypatch.setenv("AI_MODE_ENABLED", "true")
    assert config.AI_MODE_ENABLED is True
    monkeypatch.setenv("AI_MODE_ENABLED", "false")
    assert config.AI_MODE_ENABLED is False
