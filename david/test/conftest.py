import pathlib
import sys

def add_to_path(path: pathlib.Path):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

root_dir = pathlib.Path(__file__).resolve().parent.parent.parent
david_dir = pathlib.Path(__file__).resolve().parent.parent
add_to_path(root_dir)
add_to_path(david_dir)

import backend.savings_service
import backend.savings_service.app
import backend.savings_service.classifier_service
import backend.savings_service.helpers
import backend.savings_service.ollama_service
import backend.savings_service.suggestion_service

sys.modules["savings_service"] = backend.savings_service
for sub in ["app", "helpers", "ollama_service", "classifier_service", "suggestion_service"]:
    mod = getattr(backend.savings_service, sub)
    sys.modules[f"savings_service.{sub}"] = mod


import pytest


@pytest.fixture(autouse=True)
def ai_mode_on(monkeypatch):
    """Keep AI mode on unless a test switches it off.

    ``AI_MODE_ENABLED`` defaults to false so the cloud baseline refuses model
    calls, but the suite drives the planner, classifier and suggestion paths
    against a stubbed client; the tests that assert the refusal turn it off.
    """
    from backend.savings_service import ollama_service

    monkeypatch.setattr(ollama_service, "AI_MODE_ENABLED", True)
