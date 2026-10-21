import asyncio
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import ollama
import pytest
from agents import AgentError, Agents, Ollama
from contracts import FEATURES, ROLE_SCHEMAS
from server import create_app
from settings import Settings
from storage import InstanceLock, Store
from test_server import REQUEST, FakeModel, app_at
from workflow_runner import main


@pytest.mark.parametrize("probabilities, expected", [
    ([0.95, 0.85], "High"),
    ([0.70], "High"),
    ([0.90, 0.10], "Medium"),
    ([0.40], "Medium"),
    ([0.20, 0.30], "Low"),
    ([], None),
])
def test_confidence_is_calculated_from_provider_logprobs(probabilities, expected):
    response = ollama.ChatResponse(
        message={"role": "assistant", "content": "{}"},
        logprobs=[{"token": "example", "logprob": math.log(p)} for p in probabilities],
    )
    with patch("ollama.AsyncClient.chat", return_value=response) as chat:
        content, confidence = Ollama(Settings()).generate("worker", [], {}, time.monotonic() + 2)
    assert content == "{}"
    assert confidence == expected
    assert chat.call_args.kwargs["logprobs"] is True


@pytest.mark.parametrize("feature", FEATURES)
def test_every_feature_uses_same_contract(tmp_path, feature):
    client = app_at(tmp_path).test_client()
    body = client.post("/workflow", json={**REQUEST, "feature": feature}).get_json()
    assert body["contract_version"] == "1.0"
    assert body["feature"] == feature
    assert body["worker_result"]["model"] != "stub"
    assert client.get(f"/workflow/{body['workflow_id']}/export").get_json()[
        "coordination_audit.jsonl"
    ]


@pytest.mark.parametrize(
    "update",
    [
        {"task": 3},
        {"context": []},
        {"options": []},
        {"options": {"use_mcp": True}},
        {"evidence": REQUEST["evidence"] * 2},
        {"task": "x" * 4001},
        {"options": {"plan_only": True, "reuse_plan": "x"}},
        {"context": {"value": float("nan")}},
    ],
)
def test_strict_requests(tmp_path, update):
    assert (
        app_at(tmp_path)
        .test_client()
        .post("/workflow", json={**REQUEST, **update})
        .status_code
        == 400
    )


def test_resume_cannot_change_identity_or_original_evidence(tmp_path):
    client = app_at(tmp_path).test_client()
    first = client.post(
        "/workflow", json={**REQUEST, "options": {"plan_only": True}}
    ).get_json()
    for update in (
        {"feature": "budgets"},
        {"task": "Other task"},
        {"context": {"new": True}},
        {"evidence": []},
    ):
        assert (
            client.post(
                "/workflow",
                json={
                    **REQUEST,
                    "options": {"reuse_plan": first["workflow_id"]},
                    **update,
                },
            ).status_code
            == 409
        )


def test_repair_and_unknown_citations():
    class Repair(FakeModel):
        calls = 0

        def generate(self, *args):
            self.calls += 1
            return ("not json", "High") if self.calls == 1 else super().generate(*args)

    model = Repair()
    assert (
        Agents(Settings(), model).run("planner", REQUEST, time.monotonic() + 10)[
            "status"
        ]
        == "success"
    )
    assert model.calls == 2

    class Invented(FakeModel):
        def generate(self, *args):
            content, confidence = super().generate(*args)
            result = json.loads(content)
            result["required_evidence"] = ["invented"]
            return json.dumps(result), confidence

    with pytest.raises(AgentError, match="failed validation"):
        Agents(Settings(), Invented()).run("planner", REQUEST, time.monotonic() + 10)


@pytest.mark.parametrize("role", ["planner", "worker", "reviewer"])
def test_generation_schema_constrains_sources_without_mutating_shared_schema(role):
    field = "required_evidence" if role == "planner" else "evidence_used"

    class Constrained(FakeModel):
        def generate(self, role, messages, schema, deadline):
            sources = [
                item["source"]
                for item in json.loads(messages[1]["content"])["evidence"]
            ]
            refs = schema["properties"][field]
            assert refs["uniqueItems"]
            for name, property_schema in schema["properties"].items():
                if name != field and property_schema.get("type") == "array":
                    assert "enum" not in property_schema["items"]
                    assert property_schema.get("maxItems") != 0
            if sources:
                assert refs["items"]["enum"] == sorted(sources)
            else:
                assert refs["maxItems"] == 0
            return super().generate(role, messages, schema, deadline)

    agents = Agents(Settings(), Constrained())
    for evidence in ([], REQUEST["evidence"]):
        assert (
            agents.run(role, {**REQUEST, "evidence": evidence}, time.monotonic() + 10)[
                "status"
            ]
            == "success"
        )
    assert "enum" not in ROLE_SCHEMAS[role]["properties"][field]["items"]
    assert ROLE_SCHEMAS[role]["properties"][field].get("maxItems") != 0


@pytest.mark.parametrize("failed_role", ["planner", "worker", "reviewer"])
def test_failed_workflow_is_durable_and_only_rejectable(tmp_path, failed_role):
    class Failed(FakeModel):
        def generate(self, role, *args):
            if role == failed_role:
                raise AgentError("model_timeout", "Model deadline exceeded", "timeout")
            return super().generate(role, *args)

    settings = Settings()
    app = create_app(tmp_path, settings, Agents(settings, Failed()))
    client = app.test_client()
    response = client.post("/workflow", json=REQUEST)
    assert response.status_code == 503
    record = response.get_json()
    assert record["human_decision"]["allowed_values"] == ["Reject"]
    assert record[f"{failed_role}_result"]["status"] == "timeout"
    wid = record["workflow_id"]
    assert (
        client.post(
            f"/workflow/{wid}/decision", json={"decision": "Accept"}
        ).status_code
        == 409
    )
    assert (
        app_at(tmp_path).test_client().get(f"/workflow/{wid}").get_json()["status"]
        == "error"
    )
    assert (
        client.post(
            f"/workflow/{wid}/decision", json={"decision": "Reject"}
        ).status_code
        == 200
    )


def test_negative_review_requires_reason(tmp_path):
    client = app_at(tmp_path).test_client()
    record = client.post("/workflow", json={**REQUEST, "evidence": []}).get_json()
    path = f"/workflow/{record['workflow_id']}/decision"
    assert client.post(path, json={"decision": "Accept"}).status_code == 400
    assert (
        client.post(
            path, json={"decision": "Accept", "reason": "Manually verified"}
        ).status_code
        == 200
    )


def test_two_simultaneous_decisions_are_atomic(tmp_path):
    app = app_at(tmp_path)
    wid = app.test_client().post("/workflow", json=REQUEST).get_json()["workflow_id"]

    def decide(_):
        with app.test_client() as client:
            return client.post(
                f"/workflow/{wid}/decision", json={"decision": "Accept"}
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(decide, range(2))) == [200, 409]
    events = app.config["coordinator"].store.export(wid)["coordination_audit.jsonl"]
    assert sum(e["event"] == "human_decision" for e in events) == 1


def test_capacity_does_not_block_reads(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Blocked(FakeModel):
        def generate(self, role, *args):
            if role == "planner":
                entered.set()
                assert release.wait(10)
            return super().generate(role, *args)

    settings = Settings()
    app = create_app(tmp_path, settings, Agents(settings, Blocked()))
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            lambda: app.test_client().post("/workflow", json=REQUEST)
        )
        try:
            assert entered.wait(5)
            response = app.test_client().post("/workflow", json=REQUEST)
            assert (
                response.status_code == 503 and response.headers["Retry-After"] == "5"
            )
            assert app.test_client().get("/workflow/status").status_code == 200
        finally:
            release.set()
        assert future.result().status_code == 200


def test_interrupted_workflow_is_recovered_as_failed(tmp_path):
    record = app_at(tmp_path).test_client().post("/workflow", json=REQUEST).get_json()
    store = Store(tmp_path)
    _, revision = store.get(record["workflow_id"])
    record["workflow_state"] = "running"
    store.save(record, [], revision)
    recovered = (
        app_at(tmp_path)
        .test_client()
        .get(f"/workflow/{record['workflow_id']}")
        .get_json()
    )
    assert recovered["code"] == "workflow_interrupted"
    assert recovered["human_decision"]["allowed_values"] == ["Reject"]


def test_writes_do_not_export_history_and_storage_failure_is_explicit(tmp_path):
    app = app_at(tmp_path)
    store = app.config["coordinator"].store
    with patch.object(
        store, "export", side_effect=AssertionError("No export during writes")
    ):
        result = app.test_client().post("/workflow", json=REQUEST)
        assert result.status_code == 200
        assert app.test_client().get("/health").get_json()["export_mode"] == "on_demand"
        assert not (tmp_path / "workflow_history.jsonl").exists()
    with patch.object(store, "save", side_effect=OSError("disk full")):
        result = app.test_client().post("/workflow", json=REQUEST)
        assert (
            result.status_code == 503
            and result.get_json()["code"] == "storage_unavailable"
        )


def test_auth_and_binding(tmp_path):
    with pytest.raises(ValueError, match="Non-loopback"):
        Settings(host="0.0.0.0")
    settings = Settings(host="0.0.0.0", token="a" * 32)
    client = create_app(tmp_path, settings, Agents(settings, FakeModel())).test_client()
    assert client.get("/health").status_code == 401
    result = client.get(
        "/health", headers={"Authorization": f"Bearer {settings.token}"}
    )
    assert result.status_code == 200
    assert "Access-Control-Allow-Origin" not in result.headers
    assert result.headers["Cache-Control"] == "no-store"


def test_readiness_does_not_block_saved_decisions(tmp_path):
    app = app_at(tmp_path)
    client = app.test_client()
    wid = client.post("/workflow", json=REQUEST).get_json()["workflow_id"]
    with patch.object(
        app.config["coordinator"].agents.model,
        "health",
        return_value={"ready": False, "ollama": "down", "missing_models": []},
    ):
        assert client.get("/health").get_json()["ready"] is False
        assert client.get(f"/workflow/{wid}").status_code == 200
        assert (
            client.post(
                f"/workflow/{wid}/decision", json={"decision": "Accept"}
            ).status_code
            == 200
        )


def test_legacy_logs_require_explicit_migration(tmp_path):
    (tmp_path / "workflow_history.jsonl").write_text(
        '{"workflow_id":"old"}\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="Legacy"):
        Store(tmp_path)


def test_deadline_and_model_transport_errors():
    provider = Ollama(Settings())
    with pytest.raises(AgentError) as error:
        asyncio.run(provider._request("list", time.monotonic() - 1))
    assert error.value.status == "timeout"
    with (
        patch(
            "ollama.AsyncClient.list",
            side_effect=OSError("private diagnostics"),
        ),
        pytest.raises(AgentError, match="unavailable"),
    ):
        asyncio.run(provider._request("list", time.monotonic() + 1))


def test_sdk_uses_native_api_schema_options_and_bounds_responses(monkeypatch):
    real_client = ollama.AsyncClient
    requests = []

    def factory(**kwargs):
        def respond(request):
            requests.append(request)
            return httpx.Response(
                200, json={"message": {"role": "assistant", "content": "{}"}}
            )

        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(ollama, "AsyncClient", factory)
    provider = Ollama(Settings())
    assert (
        provider.generate("planner", [], ROLE_SCHEMAS["planner"], time.monotonic() + 2)
        == ("{}", None)
    )
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/api/chat"
    assert body["format"] == ROLE_SCHEMAS["planner"]
    assert body["logprobs"] is True
    assert body["options"]["num_ctx"] == 8192 and body["stream"] is False

    def oversized(**kwargs):
        return real_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * 262145)
            ),
        )

    monkeypatch.setattr(ollama, "AsyncClient", oversized)
    with pytest.raises(AgentError, match="exceeds limit"):
        provider.generate("planner", [], {}, time.monotonic() + 2)


def test_sdk_deadline_cancels_slow_response_and_closes_client(monkeypatch):
    real_client = ollama.AsyncClient
    clients = []

    def factory(**kwargs):
        async def respond(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={"models": []})

        client = real_client(**kwargs, transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr(ollama, "AsyncClient", factory)
    with pytest.raises(AgentError) as error:
        asyncio.run(Ollama(Settings())._request("list", time.monotonic() + 0.02))
    assert error.value.status == "timeout"
    assert clients[0]._client.is_closed


@pytest.mark.parametrize("failure", ["oversize", "slow"])
def test_sdk_bounds_streamed_body_and_closes_it(monkeypatch, failure):
    real_client = ollama.AsyncClient

    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b" " * 131072
            if failure == "slow":
                await asyncio.sleep(1)
            yield b" " * 131073

        async def aclose(self):
            self.closed = True

    body = Body()
    monkeypatch.setattr(
        ollama,
        "AsyncClient",
        lambda **kwargs: real_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=body)
            ),
        ),
    )
    with pytest.raises(AgentError) as error:
        asyncio.run(Ollama(Settings())._request("list", time.monotonic() + 0.05))
    assert error.value.code == (
        "model_timeout" if failure == "slow" else "invalid_model_output"
    )
    assert body.closed


def test_terminal_calls_http_and_includes_reason(monkeypatch, capsys):
    import io
    import urllib.request

    requests = []

    def open_request(request, timeout):
        requests.append(request)
        return io.BytesIO(b'{"status":"success","workflow_id":"example"}')

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    assert (
        main(
            [
                "--decide",
                "example",
                "--decision",
                "Partially Accept",
                "--corrected-output",
                "Revised",
                "--reason",
                "Correction",
            ]
        )
        == 0
    )
    body = json.loads(requests[0].data)
    assert body["reason"] == "Correction" and body["corrected_output"] == "Revised"
    assert "example" in capsys.readouterr().out


def test_structured_output_decision(tmp_path):
    class Structured(FakeModel):
        def generate(self, role, *args):
            content, confidence = super().generate(role, *args)
            result = json.loads(content)
            if role == "worker":
                result["structured"] = {"proposal": "example"}
            return json.dumps(result), confidence

    settings = Settings()
    client = create_app(
        tmp_path, settings, Agents(settings, Structured())
    ).test_client()
    wid = client.post("/workflow", json=REQUEST).get_json()["workflow_id"]
    result = client.post(
        f"/workflow/{wid}/decision", json={"decision": "Accept"}
    ).get_json()
    assert result["human_decision"]["final_output"] == {"proposal": "example"}


def test_instance_lock_prevents_competing_servers(tmp_path):
    with (
        InstanceLock(tmp_path),
        pytest.raises(RuntimeError, match="Another server"),
        InstanceLock(tmp_path),
    ):
        pytest.fail("A second server must not acquire the data directory")
    with InstanceLock(tmp_path):
        assert (tmp_path / "server.lock").exists()


def test_corrupt_database_prevents_startup(tmp_path):
    import sqlite3

    (tmp_path / "workflows.db").write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        Store(tmp_path)


def test_http_limits_and_invalid_json_are_explicit(tmp_path):
    client = app_at(tmp_path).test_client()
    assert (
        client.post("/workflow", data="{", content_type="application/json").status_code
        == 400
    )
    assert (
        client.post(
            "/workflow", data="x" * 65537, content_type="application/json"
        ).status_code
        == 413
    )
    assert (
        client.post("/workflow", data="text", content_type="text/plain").status_code
        == 415
    )
    nested = {}
    for _ in range(15):
        nested = {"nested": nested}
    response = client.post("/workflow", json={**REQUEST, "context": nested})
    assert response.status_code == 400


def test_oversize_stage_input_never_calls_provider():
    model = FakeModel()
    with patch.object(model, "generate") as generate:
        with pytest.raises(AgentError, match="Combined stage input"):
            Agents(Settings(), model).run(
                "planner",
                {**REQUEST, "context": {"data": "x" * 17000}},
                time.monotonic() + 10,
            )
        generate.assert_not_called()


def test_terminal_export_writes_named_jsonl_without_overwriting(monkeypatch, tmp_path):
    import io
    import urllib.request

    payload = {
        "workflow_history.jsonl": [{"workflow_id": "example"}],
        "coordination_audit.jsonl": [{"event": "human_decision"}],
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode()),
    )
    destination = tmp_path / "export"
    assert main(["--export", "example", "--output-dir", str(destination)]) == 0
    assert (
        json.loads((destination / "workflow_history.jsonl").read_text())["workflow_id"]
        == "example"
    )
    assert main(["--export", "example", "--output-dir", str(destination)]) == 1
