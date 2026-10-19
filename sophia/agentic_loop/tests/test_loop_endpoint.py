"""Tests for the ollama endpoint fingerprint.

model_inventory() exists because "localhost:11434" does not identify an ollama
instance on a machine that has more than one: a desktop install and the
composed container answer on different addresses with different model sets.
The run record has to say which one answered, so this must never raise and
must never return an empty string.

No socket is opened -- requests.get is replaced in every test.
"""
import pytest

from sophia.agentic_loop import main


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"{self.status} Server Error")

    def json(self):
        return self._payload


def fake_get(payload, status=200, expect=None):
    def _get(url, timeout=None):
        if expect is not None:
            assert url == expect, f"asked for {url}, expected {expect}"
        return FakeResponse(payload, status)
    return _get


def test_the_tag_route_sits_beside_v1_not_under_it(monkeypatch):
    """/api/tags is ollama's own route; hanging it off /v1 would 404."""
    monkeypatch.setattr(main.requests, "get", fake_get(
        {"models": [{"name": "qwen2.5:0.5b"}]},
        expect="http://127.0.0.1:11434/api/tags"))
    assert main.model_inventory("http://127.0.0.1:11434/v1") == "qwen2.5:0.5b"


def test_a_base_url_without_v1_is_handled(monkeypatch):
    monkeypatch.setattr(main.requests, "get", fake_get(
        {"models": [{"name": "llama3.1:8b"}]},
        expect="http://127.0.0.1:11434/api/tags"))
    assert main.model_inventory("http://127.0.0.1:11434") == "llama3.1:8b"


def test_the_models_are_sorted_so_two_runs_compare(monkeypatch):
    monkeypatch.setattr(main.requests, "get", fake_get(
        {"models": [{"name": "qwen2.5:3b"}, {"name": "llama3.1:8b"},
                    {"name": "qwen2.5:0.5b"}]}))
    assert main.model_inventory("http://127.0.0.1:11434/v1") == \
        "llama3.1:8b, qwen2.5:0.5b, qwen2.5:3b"


def test_an_endpoint_serving_nothing_says_so(monkeypatch):
    monkeypatch.setattr(main.requests, "get", fake_get({"models": []}))
    assert main.model_inventory("http://127.0.0.1:11434/v1") == \
        "endpoint served no models"


def test_an_unreachable_endpoint_is_recorded_not_raised(monkeypatch):
    """A dead endpoint must still leave a usable run record behind."""
    def boom(url, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr(main.requests, "get", boom)
    result = main.model_inventory("http://127.0.0.1:11434/v1")
    assert result.startswith("unavailable (")
    assert "connection refused" in result


def test_an_error_status_is_recorded_not_raised(monkeypatch):
    monkeypatch.setattr(main.requests, "get", fake_get({}, status=500))
    assert main.model_inventory("http://127.0.0.1:11434/v1").startswith("unavailable (")


def test_the_default_endpoint_is_pinned_to_ipv4():
    """localhost is ambiguous on Windows; the default must name the address."""
    assert main.DEFAULT_BASE_URL == "http://127.0.0.1:11434/v1"
    assert "localhost" not in main.DEFAULT_BASE_URL
