"""Environment configuration for the anomalies backend.

Values are resolved lazily from the environment: reading an attribute of
``config`` raises a ``RuntimeError`` if the corresponding variable is not set
(or cannot be parsed). Call :meth:`config.check_all` to validate every variable
up front.
"""
import os
from typing import Any, Callable


def _url(value: str) -> str:
    return value.rstrip("/")


def _bool(value: str) -> bool:
    normalised = value.strip().casefold()
    if normalised in {"1", "true", "yes", "on"}:
        return True
    if normalised in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected a boolean but got {value!r}")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise ValueError(f"expected a positive integer but got {value!r}")
    return parsed


def _resolve(name: str, parser: Callable[[str], Any]) -> Any:
    try:
        raw = os.environ[name]
    except KeyError:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set"
        ) from None

    try:
        return parser(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Environment variable {name!r} has an invalid value {raw!r}: {exc}"
        ) from exc


class _Config:
    """Lazily-resolved anomalies backend configuration.

    Each setting is a property so IDEs and type checkers see its concrete
    return type. When adding a property, also add its name to the list in
    :meth:`check_all` so it is validated.
    """

    @property
    def PORT(self) -> int:
        return _resolve("PORT", int)

    @property
    def ANOMALIES_DB_URL(self) -> str:
        return _resolve("ANOMALIES_DB_URL", _url)

    @property
    def TRANSACTIONS_DB_URL(self) -> str:
        return _resolve("TRANSACTIONS_DB_URL", _url)

    @property
    def AI_MODE_ENABLED(self) -> bool:
        """AI mode (direct Ollama calls).

        Unlike the other settings this one is optional and absent means off,
        so a deployment that configures nothing refuses model calls rather
        than reaching for whatever ``OLLAMA_URL`` happens to point at. It is
        therefore not listed in :meth:`check_all`.
        """
        raw = os.environ.get("AI_MODE_ENABLED")
        return False if raw is None else _bool(raw)

    @property
    def OLLAMA_URL(self) -> str:
        return _resolve("OLLAMA_URL", _url)

    @property
    def OLLAMA_MODEL(self) -> str:
        return _resolve("OLLAMA_MODEL", str)

    @property
    def MCP_SERVER_URL(self) -> str:
        return _resolve("MCP_SERVER_URL", _url)

    @property
    def MCP_TIMEOUT_SECONDS(self) -> float:
        return float(os.environ.get("MCP_TIMEOUT_SECONDS", "15"))

    @property
    def RAG_ENABLED(self) -> bool:
        return _resolve("RAG_ENABLED", _bool)

    @property
    def RAG_SERVER_URL(self) -> str:
        return _resolve("RAG_SERVER_URL", _url)

    @property
    def RAG_FEATURE(self) -> str:
        return _resolve("RAG_FEATURE", str)

    @property
    def RAG_TOP_K(self) -> int:
        return _resolve("RAG_TOP_K", _positive_int)

    @property
    def RAG_TIMEOUT_SECONDS(self) -> float:
        return _resolve("RAG_TIMEOUT_SECONDS", float)

    def check_all(self) -> None:
        """Resolve every configured variable, raising if any is missing or invalid.

        Errors are aggregated so a single call reports every problem at once.
        """
        errors = []
        for name in (
            "PORT",
            "ANOMALIES_DB_URL",
            "TRANSACTIONS_DB_URL",
            "OLLAMA_URL",
            "OLLAMA_MODEL",
            "MCP_SERVER_URL",
            "RAG_ENABLED",
            "RAG_SERVER_URL",
            "RAG_FEATURE",
            "RAG_TOP_K",
            "RAG_TIMEOUT_SECONDS",
        ):
            try:
                getattr(self, name)
            except RuntimeError as exc:
                errors.append(str(exc))

        if errors:
            raise RuntimeError(
                "Invalid anomalies backend configuration:\n  - "
                + "\n  - ".join(errors)
            )


config = _Config()
