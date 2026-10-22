"""AI mode is a real gate: with the switch off guard.run refuses instead of serving the fallback."""
import pytest

from backend import config
from backend.ai import guard
from backend.db_api import ServiceError


def test_guard_run_refuses_before_any_attempt_when_ai_mode_is_off(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)

    def never(*args, **kwargs):
        raise AssertionError("AI mode is off; no model call may be made")

    monkeypatch.setattr(guard, "chat", never)
    with pytest.raises(ServiceError) as caught:
        guard.run("model", lambda error: [], lambda data: None, {"text": "fallback"})
    assert caught.value.code == "ai_disabled"
    assert caught.value.status == 503


def test_guard_run_refuses_rather_than_returning_the_fallback(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)
    with pytest.raises(ServiceError):
        guard.run("model", lambda error: [], lambda data: "always invalid", {"text": "fallback"})


def test_guard_run_still_works_when_ai_mode_is_on(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", True)
    monkeypatch.setattr(guard, "chat", lambda *a, **k: {"message": {"content": '{"ok": true}'}})
    assert guard.run("model", lambda error: [], lambda data: None, {})["fallback"] is False
