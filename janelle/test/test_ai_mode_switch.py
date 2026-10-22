"""AI mode is a real gate: with the switch off no model call is made."""
import pytest

from janelle.backend import config
from janelle.backend.services import ollama_service, rag_answer
from janelle.backend.services.chat_service import ChatError
from janelle.backend.services.rag_client import RAGError


def test_create_plan_refuses_before_any_request_when_ai_mode_is_off(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)

    def never(*args, **kwargs):
        raise AssertionError("AI mode is off; no model call may be made")

    monkeypatch.setattr(ollama_service.requests, "post", never)
    with pytest.raises(ChatError) as caught:
        ollama_service.create_plan("spent $9 at a cafe", [])
    assert caught.value.code == "ai_disabled"
    assert caught.value.status == 503


def test_ask_model_refuses_before_any_request_when_ai_mode_is_off(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)

    def never(*args, **kwargs):
        raise AssertionError("AI mode is off; no model call may be made")

    monkeypatch.setattr(rag_answer.requests, "post", never)
    with pytest.raises(RAGError) as caught:
        rag_answer.ask_model("a question", [{"text": "ctx"}], ["Groceries"])
    assert caught.value.code == "ai_disabled"


def test_health_reports_ai_disabled_when_the_switch_is_off(monkeypatch):
    monkeypatch.setattr(config, "AI_MODE_ENABLED", False)
    from janelle.backend import app as backend_app

    application = backend_app.setup_app("http://transactions-db:6001")
    application.config["TESTING"] = True
    body = application.test_client().get("/health").get_json()
    assert body["modes"]["ai"] == "disabled"
