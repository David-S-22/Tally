import hashlib
import http.client
import json
import logging
import time
from copy import deepcopy
from urllib.parse import urlsplit

from contracts import ROLE_SCHEMAS, WorkflowError, validate

logger = logging.getLogger(__name__)


class AgentError(Exception):
    def __init__(self, code, message, status="error"):
        super().__init__(message)
        self.code = code
        self.status = status


class Ollama:
    def __init__(self, settings):
        self.settings = settings

    def request(self, method, path, body, deadline):
        origin = urlsplit(self.settings.ollama_url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError("model_timeout", "Model deadline exceeded", "timeout")
        connection_type = (
            http.client.HTTPSConnection
            if origin.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(origin.hostname, origin.port, timeout=remaining)
        try:
            connection.request(
                method,
                path,
                body=None if body is None else json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
            self._budget(connection, deadline)
            response = connection.getresponse()
            chunks = []
            size = 0
            while True:
                self._budget(connection, deadline)
                chunk = response.read1(8192)
                if not chunk:
                    break
                size += len(chunk)
                if size > 262144:
                    raise AgentError(
                        "invalid_model_output", "Model response exceeds limit"
                    )
                chunks.append(chunk)
            if response.status != 200:
                logger.error("Ollama request failed http=%s", response.status)
                raise AgentError(
                    "model_unavailable", "Model provider rejected the request"
                )
            try:
                return json.loads(b"".join(chunks))
            except (ValueError, UnicodeDecodeError):
                raise AgentError(
                    "invalid_model_output", "Model provider returned invalid JSON"
                ) from None
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            logger.error("Ollama transport failure kind=%s", type(exc).__name__)
            if isinstance(exc, TimeoutError):
                raise AgentError(
                    "model_timeout", "Model deadline exceeded", "timeout"
                ) from None
            raise AgentError(
                "model_unavailable", "Model provider is unavailable"
            ) from None
        finally:
            connection.close()

    @staticmethod
    def _budget(connection, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError("model_timeout", "Model deadline exceeded", "timeout")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)

    def generate(self, role, messages, schema, deadline):
        response = self.request(
            "POST",
            "/api/chat",
            {
                "model": self.settings.models[role],
                "messages": messages,
                "format": schema,
                "stream": False,
                "options": {"temperature": 0, "num_predict": 2048, "num_ctx": 8192},
            },
            deadline,
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("message"), dict)
            or not isinstance(response["message"].get("content"), str)
        ):
            raise AgentError(
                "invalid_model_output", "Model provider returned an invalid envelope"
            )
        return response["message"]["content"]

    def health(self):
        try:
            result = self.request("GET", "/api/tags", None, time.monotonic() + 2)
            names = {model["name"] for model in result["models"]}
            missing = sorted(set(self.settings.models.values()) - names)
            return {"ollama": "up", "ready": not missing, "missing_models": missing}
        except (AgentError, KeyError, TypeError):
            return {"ollama": "down", "ready": False, "missing_models": []}


class Agents:
    def __init__(self, settings, model=None):
        self.settings = settings
        self.model = model or Ollama(settings)
        self.prompts = {
            role: (settings.prompt_dir / f"{role}_prompt.txt").read_text(
                encoding="utf-8"
            )
            for role in ROLE_SCHEMAS
        }
        self.prompt_version = hashlib.sha256(
            "".join(self.prompts.values()).encode()
        ).hexdigest()[:16]

    def run(self, role, payload, deadline):
        if len(json.dumps(payload, allow_nan=False).encode("utf-8")) > 16384:
            raise AgentError(
                "context_limit",
                "Combined stage input exceeds 16 KiB; provide more concise evidence",
            )
        messages = [
            {"role": "system", "content": self.prompts[role]},
            {"role": "user", "content": json.dumps(payload, allow_nan=False)},
        ]
        sources = {item["source"] for item in payload["evidence"]}
        schema = deepcopy(ROLE_SCHEMAS[role])
        reference_field = "required_evidence" if role == "planner" else "evidence_used"
        references = deepcopy(schema["properties"][reference_field])
        schema["properties"][reference_field] = references
        references["uniqueItems"] = True
        if sources:
            references["items"]["enum"] = sorted(sources)
        else:
            references["maxItems"] = 0
        for attempt in range(2):
            content = self.model.generate(role, messages, schema, deadline)
            try:
                result = json.loads(content)
                validate(result, schema)
                refs = result.get("evidence_used", result.get("required_evidence", []))
                if len(set(refs)) != len(refs) or not set(refs) <= sources:
                    raise WorkflowError(
                        400, "Evidence references must be unique supplied source IDs"
                    )
                if (
                    role == "reviewer"
                    and result["verdict"] == "approve"
                    and result["risks"]
                ):
                    raise WorkflowError(400, "Approval cannot contain unresolved risks")
                return {
                    **result,
                    "status": "success",
                    "agent": f"{role}_agent",
                    "model": self.settings.models[role],
                    "human_approval_required": True,
                    **({"evidence_count": len(refs)} if role == "worker" else {}),
                }
            except (ValueError, WorkflowError):
                if attempt:
                    raise AgentError(
                        "invalid_model_output",
                        "Model output failed validation after one repair",
                    ) from None
                messages.append(
                    {
                        "role": "user",
                        "content": "Your response failed validation. Return only JSON matching the supplied schema. Use only unique evidence source IDs from the request. Do not approve unresolved risks.",
                    }
                )
        raise AssertionError("Unreachable")
