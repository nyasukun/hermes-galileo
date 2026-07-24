"""Configuration loading and validation for hermes-galileo."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigurationError(ValueError):
    """Raised when an explicitly configured setting is invalid."""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name, default)).strip()


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _text(env, name)
    if not raw:
        return default
    normalized = raw.lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"{name} must be one of {sorted(_TRUE_VALUES | _FALSE_VALUES)}, got {raw!r}"
    )


def _integer(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(
    env: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = _text(env, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime settings.

    Galileo credentials deliberately use the official SDK variable names so
    they can live directly in ``~/.hermes/.env`` without a translation layer.
    """

    enabled: bool
    api_key: str = field(repr=False)
    project: str
    log_stream: str
    console_url: str | None
    api_url: str | None
    capture_content: bool
    capture_conversation_history: bool
    hash_user_ids: bool
    max_content_chars: int
    max_collection_items: int
    sample_rate: float
    turn_ttl_seconds: int
    async_flush_on_turn_end: bool
    flush_timeout_millis: int
    debug: bool
    environment: str
    service_name: str
    pseudonym_secret: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        sdk_disabled = _boolean(values, "GALILEO_LOGGING_DISABLED", False)
        enabled = _boolean(values, "HERMES_GALILEO_ENABLED", True) and not sdk_disabled

        return cls(
            enabled=enabled,
            api_key=_text(values, "GALILEO_API_KEY"),
            project=_text(values, "GALILEO_PROJECT"),
            log_stream=_text(values, "GALILEO_LOG_STREAM"),
            console_url=_text(values, "GALILEO_CONSOLE_URL") or None,
            api_url=_text(values, "GALILEO_API_URL") or None,
            # Prompts and responses commonly contain personal or confidential
            # data. Capture is therefore an explicit operator decision.
            capture_content=_boolean(values, "HERMES_GALILEO_CAPTURE_CONTENT", False),
            capture_conversation_history=_boolean(
                values, "HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY", False
            ),
            hash_user_ids=_boolean(values, "HERMES_GALILEO_HASH_USER_IDS", True),
            max_content_chars=_integer(
                values,
                "HERMES_GALILEO_MAX_CONTENT_CHARS",
                12_000,
                minimum=256,
                maximum=1_000_000,
            ),
            max_collection_items=_integer(
                values,
                "HERMES_GALILEO_MAX_COLLECTION_ITEMS",
                100,
                minimum=1,
                maximum=10_000,
            ),
            sample_rate=_float(
                values,
                "HERMES_GALILEO_SAMPLE_RATE",
                1.0,
                minimum=0.0,
                maximum=1.0,
            ),
            turn_ttl_seconds=_integer(
                values,
                "HERMES_GALILEO_TURN_TTL_SECONDS",
                900,
                minimum=30,
                maximum=86_400,
            ),
            async_flush_on_turn_end=_boolean(
                values, "HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END", True
            ),
            flush_timeout_millis=_integer(
                values,
                "HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS",
                10_000,
                minimum=100,
                maximum=120_000,
            ),
            debug=_boolean(values, "HERMES_GALILEO_DEBUG", False),
            environment=_text(values, "HERMES_GALILEO_ENVIRONMENT", "development"),
            service_name=_text(values, "HERMES_GALILEO_SERVICE_NAME", "hermes-agent"),
            pseudonym_secret=_text(values, "HERMES_GALILEO_PSEUDONYM_SECRET"),
        )

    def missing_required(self) -> tuple[str, ...]:
        """Return missing SDK routing/authentication variables."""

        if not self.enabled:
            return ()
        missing: list[str] = []
        if not self.api_key:
            missing.append("GALILEO_API_KEY")
        if not self.project:
            missing.append("GALILEO_PROJECT")
        if not self.log_stream:
            missing.append("GALILEO_LOG_STREAM")
        if self.console_url and not self.api_url:
            # A custom console can derive or inherit an API host inside the
            # process-global Galileo singleton. Require an explicit trusted
            # route so another SDK user cannot win that initialization race.
            missing.append("GALILEO_API_URL")
        return tuple(missing)
