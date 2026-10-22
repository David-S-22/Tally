import pathlib
import sys

# Add the package root (aiden/) to sys.path so tests can import the local `database` package
# using absolute imports like `from database.app import app`. Pytest imports test modules as
# top-level modules, so relative imports (e.g. `from .database`) will fail otherwise.
def add_to_path(path: pathlib.Path):
    path_str = str(path)

    if path_str not in sys.path:
        sys.path.insert(0, path_str)

add_to_path(pathlib.Path(__file__).resolve().parent.parent.parent)
add_to_path(pathlib.Path(__file__).resolve().parent.parent)


import pytest


@pytest.fixture(autouse=True)
def ai_mode_on(monkeypatch):
    """Keep AI mode on unless a test switches it off.

    ``AI_MODE_ENABLED`` is absent-means-off so the cloud baseline refuses
    model calls, but the suite drives the prompt path behind a mocked
    transport; the tests that assert the refusal unset it again.
    """
    monkeypatch.setenv("AI_MODE_ENABLED", "true")
