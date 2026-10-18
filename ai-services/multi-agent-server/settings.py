import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 5010
    token: str = field(default="", repr=False)
    ollama_url: str = "http://localhost:11434"
    planner_model: str = "qwen2.5:3b"
    worker_model: str = "qwen2.5:3b"
    reviewer_model: str = "llama3.1:8b"
    step_timeout: float = 180
    workflow_timeout: float = 540
    data_dir: Path = Path(__file__).resolve().parent / "logs"
    prompt_dir: Path = Path(__file__).resolve().parents[2] / "prompts" / "multi_agent"

    def __post_init__(self):
        if self.host not in ("127.0.0.1", "::1", "localhost") and len(self.token) < 32:
            raise ValueError(
                "Non-loopback binding requires MULTI_AGENT_TOKEN of at least 32 characters"
            )
        if self.token and (
            len(self.token) < 32 or any(c.isspace() for c in self.token)
        ):
            raise ValueError(
                "MULTI_AGENT_TOKEN must contain at least 32 non-whitespace characters"
            )
        parsed = urlsplit(self.ollama_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError(
                "OLLAMA_URL must be an HTTP(S) origin without credentials or a path"
            )
        if not 1 <= self.port <= 65535:
            raise ValueError("MULTI_AGENT_PORT is invalid")
        if any(
            not math.isfinite(v) or v <= 0
            for v in (self.step_timeout, self.workflow_timeout)
        ):
            raise ValueError("Timeouts must be positive finite seconds")
        if not all(m.strip() for m in self.models.values()):
            raise ValueError("All role model names are required")

    @property
    def models(self):
        return {
            "planner": self.planner_model,
            "worker": self.worker_model,
            "reviewer": self.reviewer_model,
        }

    @classmethod
    def from_env(cls):
        return cls(
            host=os.getenv("MULTI_AGENT_HOST", "127.0.0.1"),
            port=int(os.getenv("MULTI_AGENT_PORT", "5010")),
            token=os.getenv("MULTI_AGENT_TOKEN", ""),
            ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            planner_model=os.getenv("MULTI_AGENT_PLANNER_MODEL", "qwen2.5:3b"),
            worker_model=os.getenv("MULTI_AGENT_WORKER_MODEL", "qwen2.5:3b"),
            reviewer_model=os.getenv("MULTI_AGENT_REVIEWER_MODEL", "llama3.1:8b"),
            step_timeout=float(os.getenv("AGENT_STEP_TIMEOUT_SECONDS", "180")),
            workflow_timeout=float(
                os.getenv("MULTI_AGENT_WORKFLOW_TIMEOUT_SECONDS", "540")
            ),
            data_dir=Path(os.getenv("MULTI_AGENT_LOG_DIR", str(cls.data_dir))),
        )
