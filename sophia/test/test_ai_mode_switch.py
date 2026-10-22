"""AI mode is a real gate: with the switch off every model call refuses with 503 ai_disabled."""
import pytest

from sophia.backend import config
from sophia.backend.ai import guard, ollama_client
from sophia.backend.services.errors import ModeError


def test_guard_run_refuses_before_any_attempt_when_ai_mode_is_off(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)

    def never(*args, **kwargs):
        raise AssertionError("AI mode is off; no model call may be made")

    monkeypatch.setattr(guard, "chat", never)
    with pytest.raises(ModeError) as caught:
        guard.run("model", lambda error: [], lambda data: None, {"text": "fallback"})
    assert caught.value.code == "ai_disabled"
    assert caught.value.status == 503


def test_guard_run_refuses_rather_than_returning_the_fallback(monkeypatch):
    """A disabled deployment must refuse, not quietly serve canned text as if the model answered."""
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)
    with pytest.raises(ModeError):
        guard.run("model", lambda error: [], lambda data: "always invalid", {"text": "fallback"})


def test_warm_is_a_no_op_when_ai_mode_is_off(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)

    def never(*args, **kwargs):
        raise AssertionError("AI mode is off; no warm-up request may be sent")

    monkeypatch.setattr(ollama_client.requests, "post", never)
    assert ollama_client.warm("model") is None


def test_guard_run_still_works_when_ai_mode_is_on(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", True)
    monkeypatch.setattr(guard, "chat", lambda *a, **k: {"message": {"content": '{"ok": true}'}})
    assert guard.run("model", lambda error: [], lambda data: None, {})["fallback"] is False
