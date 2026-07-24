"""Configuration loading and validation for hermes-galileo."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(ValueError):
    """Raised when environment or YAML configuration cannot be validated."""


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

DEFAULT_CONFIG_PATH = Path.home() / ".hermes" / "plugins" / "hermes_galileo" / "config.yaml"

_YAML_TO_ENV = {
    "enabled": "HERMES_GALILEO_ENABLED",
    "capture_content": "HERMES_GALILEO_CAPTURE_CONTENT",
    "capture_conversation_history": "HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY",
    "hash_user_ids": "HERMES_GALILEO_HASH_USER_IDS",
    "max_content_chars": "HERMES_GALILEO_MAX_CONTENT_CHARS",
    "max_collection_items": "HERMES_GALILEO_MAX_COLLECTION_ITEMS",
    "sample_rate": "HERMES_GALILEO_SAMPLE_RATE",
    "turn_ttl_seconds": "HERMES_GALILEO_TURN_TTL_SECONDS",
    "async_flush_on_turn_end": "HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END",
    "flush_timeout_millis": "HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS",
    "debug": "HERMES_GALILEO_DEBUG",
    "environment": "HERMES_GALILEO_ENVIRONMENT",
    "service_name": "HERMES_GALILEO_SERVICE_NAME",
    "native_sessions_enabled": "HERMES_GALILEO_NATIVE_SESSIONS_ENABLED",
    "native_session_timeout_millis": "HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS",
}
_YAML_BOOLEAN_FIELDS = frozenset(
    {
        "enabled",
        "capture_content",
        "capture_conversation_history",
        "hash_user_ids",
        "async_flush_on_turn_end",
        "debug",
        "native_sessions_enabled",
    }
)
_YAML_INTEGER_FIELDS = frozenset(
    {
        "max_content_chars",
        "max_collection_items",
        "turn_ttl_seconds",
        "flush_timeout_millis",
        "native_session_timeout_millis",
    }
)
_YAML_FLOAT_FIELDS = frozenset({"sample_rate"})
_YAML_TEXT_FIELDS = frozenset({"environment", "service_name"})
_FORBIDDEN_YAML_FIELDS = frozenset(
    {
        "api_key",
        "pseudonym_secret",
        "project",
        "log_stream",
        "console_url",
        "api_url",
        "galileo_api_key",
        "galileo_project",
        "galileo_log_stream",
        "galileo_console_url",
        "galileo_api_url",
        "hermes_galileo_pseudonym_secret",
    }
)


def active_hermes_home() -> Path:
    """Resolve the active Hermes profile home at call time."""

    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        configured_home = os.environ.get("HERMES_HOME", "").strip()
        hermes_home = Path(configured_home) if configured_home else DEFAULT_CONFIG_PATH.parents[2]
    else:
        try:
            hermes_home = Path(get_hermes_home())
        except Exception:
            configured_home = os.environ.get("HERMES_HOME", "").strip()
            hermes_home = (
                Path(configured_home) if configured_home else DEFAULT_CONFIG_PATH.parents[2]
            )
    return hermes_home.expanduser().resolve(strict=False)


def _default_config_path() -> Path:
    return active_hermes_home() / "plugins" / "hermes_galileo" / "config.yaml"


def _yaml_value_as_env_text(name: str, value: Any) -> str:
    if name in _YAML_BOOLEAN_FIELDS:
        if type(value) is not bool:
            raise ConfigurationError(f"config.yaml field {name!r} must be a boolean")
        return "true" if value else "false"
    if name in _YAML_INTEGER_FIELDS:
        if type(value) is not int:
            raise ConfigurationError(f"config.yaml field {name!r} must be an integer")
        return str(value)
    if name in _YAML_FLOAT_FIELDS:
        if type(value) not in {int, float}:
            raise ConfigurationError(f"config.yaml field {name!r} must be a number")
        return str(value)
    if name in _YAML_TEXT_FIELDS:
        if not isinstance(value, str):
            raise ConfigurationError(f"config.yaml field {name!r} must be a string")
        return value
    raise ConfigurationError(f"unknown config.yaml field: {name!r}")


def _load_yaml_config(path: Path, *, required: bool) -> dict[str, str]:
    try:
        source = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        if required:
            raise ConfigurationError(f"config.yaml does not exist: {path}") from exc
        return {}
    except OSError as exc:
        raise ConfigurationError(
            f"config.yaml could not be read: {path} ({type(exc).__name__})"
        ) from exc

    try:
        document = yaml.safe_load(source)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"config.yaml is malformed: {path}") from exc

    if document is None:
        return {}
    if not isinstance(document, Mapping):
        raise ConfigurationError("config.yaml root must be a mapping")
    if any(not isinstance(name, str) for name in document):
        raise ConfigurationError("config.yaml field names must be strings")

    names = tuple(document)
    forbidden = sorted(name for name in names if name.casefold() in _FORBIDDEN_YAML_FIELDS)
    if forbidden:
        rendered = ", ".join(repr(name) for name in forbidden)
        raise ConfigurationError(
            f"config.yaml cannot contain secrets or Galileo routing fields: {rendered}; "
            "set them in the active Hermes .env"
        )

    unknown = sorted(set(names) - _YAML_TO_ENV.keys())
    if unknown:
        rendered = ", ".join(repr(name) for name in unknown)
        raise ConfigurationError(f"unknown config.yaml field(s): {rendered}")

    return {_YAML_TO_ENV[name]: _yaml_value_as_env_text(name, document[name]) for name in names}


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
    they can live directly in the active Hermes ``.env`` without a translation
    layer.
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
    native_sessions_enabled: bool = True
    native_session_timeout_millis: int = 5_000
    hermes_home: str = field(default="", repr=False)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        config_path: str | os.PathLike[str] | None = None,
    ) -> Settings:
        """Load settings with environment values taking precedence over YAML.

        Process environment loading reads the standard Hermes plugin path.
        Supplying an explicit environment mapping keeps tests and embedders
        isolated unless ``config_path`` is also supplied.
        """

        source = os.environ if env is None else env
        if env is None:
            hermes_home = str(active_hermes_home())
        else:
            configured_home = str(env.get("HERMES_HOME", "")).strip()
            hermes_home = (
                str(Path(configured_home).expanduser().resolve(strict=False))
                if configured_home
                else ""
            )
        yaml_values: dict[str, str] = {}
        if config_path is not None:
            yaml_values = _load_yaml_config(Path(config_path).expanduser(), required=True)
        elif env is None:
            yaml_values = _load_yaml_config(_default_config_path(), required=False)

        # A blank value in a shell or .env file means "not specified". It
        # must not erase a typed YAML value and silently restore a built-in
        # default.
        values = {
            **yaml_values,
            **{name: value for name, value in source.items() if str(value).strip()},
        }
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
            native_sessions_enabled=_boolean(
                values,
                "HERMES_GALILEO_NATIVE_SESSIONS_ENABLED",
                True,
            ),
            native_session_timeout_millis=_integer(
                values,
                "HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS",
                5_000,
                minimum=100,
                maximum=120_000,
            ),
            hermes_home=hermes_home,
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
