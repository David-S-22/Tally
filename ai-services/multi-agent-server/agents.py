import asyncio
import hashlib
import json
import logging
import time
from copy import deepcopy

import httpx
import ollama
from contracts import ROLE_SCHEMAS, WorkflowError, validate

logger = logging.getLogger(__name__)


class AgentError(Exception):
    def __init__(self, code, message, status="error"):
        super().__init__(message)
        self.code = code
        self.status = status


class BoundedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > 262144:
                raise AgentError("invalid_model_output", "Model response exceeds limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class Ollama:
    def __init__(self, settings):
        self.settings = settings

    async def _bound_response(self, response):
        if response.is_stream_consumed:
            if len(response.content) > 262144:
                raise AgentError("invalid_model_output", "Model response exceeds limit")
        else:
            response.stream = BoundedResponseStream(response.stream)

    async def _request(self, operation, deadline, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentError("model_timeout", "Model deadline exceeded", "timeout")
        client = ollama.AsyncClient(
            host=self.settings.ollama_url,
            timeout=remaining,
            follow_redirects=False,
            headers={"Accept-Encoding": "identity"},
            event_hooks={"response": [self._bound_response]},
        )
        try:
            async with asyncio.timeout(remaining):
                if operation == "chat":
                    return await client.chat(**kwargs)
                return await client.list()
        except (TimeoutError, httpx.TimeoutException):
            raise AgentError(
                "model_timeout", "Model deadline exceeded", "timeout"
            ) from None
        except (ollama.ResponseError, httpx.HTTPError, ConnectionError, OSError) as exc:
            logger.error("Ollama SDK failure kind=%s", type(exc).__name__)
            raise AgentError(
                "model_unavailable", "Model provider is unavailable"
            ) from None
        except ValueError:
            raise AgentError(
                "invalid_model_output", "Model provider returned invalid JSON"
            ) from None
        finally:
            await client.close()

    def generate(self, role, messages, schema, deadline):
        response = asyncio.run(
            self._request(
                "chat",
                deadline,
                model=self.settings.models[role],
                messages=messages,
                format=schema,
                stream=False,
                options={"temperature": 0, "num_predict": 2048, "num_ctx": 8192},
            )
        )
        if not response.message or not isinstance(response.message.content, str):
            raise AgentError(
                "invalid_model_output", "Model provider returned an invalid envelope"
            )
        return response.message.content

    def health(self):
        try:
            result = asyncio.run(self._request("list", time.monotonic() + 2))
            names = {model.model for model in result.models}
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
