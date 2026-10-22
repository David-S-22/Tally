import os
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
def ai_mode_on():
    """Switch AI mode on for the duration of each test, then put it back.

    AI mode is off by default so the cloud baseline makes no model calls, but
    the suite drives the planner, classifier and suggestion paths against a
    stubbed client. ai_mode_enabled() reads the environment per call, so
    setting the variable here is enough; the original value is restored
    afterwards so nothing leaks out of the test run.
    """
    previous = os.environ.get("AI_MODE_ENABLED")
    os.environ["AI_MODE_ENABLED"] = "true"
    try:
        yield
    finally:
        if previous is None:
            del os.environ["AI_MODE_ENABLED"]
        else:
            os.environ["AI_MODE_ENABLED"] = previous
