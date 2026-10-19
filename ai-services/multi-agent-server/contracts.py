import json

from jsonschema import Draft202012Validator

VERSION = "1.0"
FEATURES = ("transactions", "savings", "anomalies", "bills", "budgets")
DECISIONS = ("Accept", "Partially Accept", "Reject")
TEXT = {"type": "string", "minLength": 1, "maxLength": 16000, "pattern": "\\S"}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 64}
NONEMPTY_STRINGS = {**STRINGS, "minItems": 1}


class WorkflowError(Exception):
    def __init__(self, status, message, code="invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def object_schema(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


REQUEST_SCHEMA = object_schema(
    {
        "contract_version": {"const": VERSION},
        "feature": {"enum": list(FEATURES)},
        "task": {"type": "string", "minLength": 1, "maxLength": 4000},
        "evidence": {
            "type": "array",
            "maxItems": 32,
            "items": object_schema(
                {
                    "source": {"type": "string", "minLength": 1, "maxLength": 200},
                    "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "data": {},
                },
                ["source", "summary"],
            ),
        },
        "context": {"type": "object"},
        "options": object_schema(
            {
                "plan_only": {"type": "boolean"},
                "reuse_plan": {"type": "string", "minLength": 1, "maxLength": 36},
            },
            [],
        ),
    },
    ["feature", "task", "evidence"],
)

ROLE_SCHEMAS = {
    "planner": object_schema(
        {
            "plan": NONEMPTY_STRINGS,
            "required_evidence": STRINGS,
            "missing_information": STRINGS,
            "completion_criteria": NONEMPTY_STRINGS,
        }
    ),
    "worker": object_schema(
        {
            "output": TEXT,
            "structured": {"type": ["object", "null"]},
            "evidence_used": STRINGS,
            "limitations": STRINGS,
        }
    ),
    "reviewer": object_schema(
        {
            "verdict": {"enum": ["approve", "approve_with_changes", "reject"]},
            "risks": STRINGS,
            "recommendations": STRINGS,
            "review": TEXT,
            "evidence_used": STRINGS,
        }
    ),
}

DECISION_SCHEMA = object_schema(
    {
        "decision": {"enum": list(DECISIONS)},
        "corrected_output": {"type": ["string", "object", "null"], "maxLength": 16000},
        "reason": {"type": ["string", "null"], "maxLength": 4000},
        "decided_by": {"type": ["string", "null"], "maxLength": 200},
    },
    ["decision"],
)


def validate(value, schema):
    try:
        encoded = json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise WorkflowError(400, "A finite JSON value is required") from None
    if len(encoded.encode("utf-8")) > 65536:
        raise WorkflowError(413, "JSON payload exceeds 64 KiB", "payload_too_large")
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if depth > 12:
            raise WorkflowError(400, "JSON nesting exceeds 12 levels")
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    error = next(Draft202012Validator(schema).iter_errors(value), None)
    if error:
        location = ".".join(str(p) for p in error.absolute_path) or "body"
        raise WorkflowError(400, f"Invalid {location}: {error.validator} constraint")


def validate_request(body):
    validate(body, REQUEST_SCHEMA)
    if not body["task"].strip():
        raise WorkflowError(400, "task must not be blank")
    sources = [e["source"] for e in body["evidence"]]
    if any(
        not e["summary"].strip() or not e["source"].strip() for e in body["evidence"]
    ) or len(set(sources)) != len(sources):
        raise WorkflowError(
            400, "Evidence requires non-blank summaries and unique non-blank source IDs"
        )
    if len(json.dumps(body.get("context", {})).encode()) > 8192:
        raise WorkflowError(400, "context exceeds 8 KiB")
    options = body.get("options", {})
    if options.get("plan_only") and options.get("reuse_plan"):
        raise WorkflowError(400, "plan_only and reuse_plan are mutually exclusive")
