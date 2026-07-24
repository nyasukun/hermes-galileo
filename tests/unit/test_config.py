"""Unit tests for environment-backed plugin configuration."""

from __future__ import annotations

import pytest

from hermes_galileo.config import ConfigurationError, Settings


def test_defaults_are_safe_and_predictable() -> None:
    settings = Settings.from_env({})

    assert settings.enabled is True
    assert settings.api_key == ""
    assert settings.project == ""
    assert settings.log_stream == ""
    assert settings.console_url is None
    assert settings.api_url is None
    assert settings.capture_content is False
    assert settings.capture_conversation_history is False
    assert settings.hash_user_ids is True
    assert settings.max_content_chars == 12_000
    assert settings.max_collection_items == 100
    assert settings.sample_rate == 1.0
    assert settings.turn_ttl_seconds == 900
    assert settings.async_flush_on_turn_end is True
    assert settings.flush_timeout_millis == 10_000
    assert settings.debug is False
    assert settings.environment == "development"
    assert settings.service_name == "hermes-agent"
    assert settings.pseudonym_secret == ""


def test_secret_settings_are_excluded_from_repr() -> None:
    settings = Settings.from_env(
        {
            "GALILEO_API_KEY": "repr-api-key-secret",
            "HERMES_GALILEO_PSEUDONYM_SECRET": "repr-pseudonym-secret",
        }
    )

    rendered = repr(settings)

    assert "repr-api-key-secret" not in rendered
    assert "repr-pseudonym-secret" not in rendered


def test_explicit_values_are_trimmed_and_capture_requires_opt_in() -> None:
    settings = Settings.from_env(
        {
            "GALILEO_API_KEY": "  galileo-key  ",
            "GALILEO_PROJECT": "  my-project ",
            "GALILEO_LOG_STREAM": " production ",
            "GALILEO_CONSOLE_URL": " https://console.example.test/ ",
            "GALILEO_API_URL": " https://api.example.test/ ",
            "HERMES_GALILEO_CAPTURE_CONTENT": "TrUe",
            "HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY": "yes",
            "HERMES_GALILEO_HASH_USER_IDS": "off",
            "HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END": "0",
            "HERMES_GALILEO_DEBUG": "on",
            "HERMES_GALILEO_ENVIRONMENT": " staging ",
            "HERMES_GALILEO_SERVICE_NAME": " agent-service ",
            "HERMES_GALILEO_PSEUDONYM_SECRET": " dedicated-hash-key ",
        }
    )

    assert settings.api_key == "galileo-key"
    assert settings.project == "my-project"
    assert settings.log_stream == "production"
    assert settings.console_url == "https://console.example.test/"
    assert settings.api_url == "https://api.example.test/"
    assert settings.capture_content is True
    assert settings.capture_conversation_history is True
    assert settings.hash_user_ids is False
    assert settings.async_flush_on_turn_end is False
    assert settings.debug is True
    assert settings.environment == "staging"
    assert settings.service_name == "agent-service"
    assert settings.pseudonym_secret == "dedicated-hash-key"


def test_custom_console_requires_an_explicit_api_route() -> None:
    settings = Settings.from_env(
        {
            "GALILEO_API_KEY": "key",
            "GALILEO_PROJECT": "project",
            "GALILEO_LOG_STREAM": "stream",
            "GALILEO_CONSOLE_URL": "https://console.example.test",
        }
    )

    assert settings.missing_required() == ("GALILEO_API_URL",)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HERMES_GALILEO_ENABLED", "maybe"),
        ("HERMES_GALILEO_CAPTURE_CONTENT", "enabled"),
        ("HERMES_GALILEO_MAX_CONTENT_CHARS", "many"),
        ("HERMES_GALILEO_MAX_CONTENT_CHARS", "255"),
        ("HERMES_GALILEO_MAX_CONTENT_CHARS", "1000001"),
        ("HERMES_GALILEO_MAX_COLLECTION_ITEMS", "0"),
        ("HERMES_GALILEO_MAX_COLLECTION_ITEMS", "10001"),
        ("HERMES_GALILEO_SAMPLE_RATE", "not-a-number"),
        ("HERMES_GALILEO_SAMPLE_RATE", "-0.01"),
        ("HERMES_GALILEO_SAMPLE_RATE", "1.01"),
        ("HERMES_GALILEO_TURN_TTL_SECONDS", "29"),
        ("HERMES_GALILEO_TURN_TTL_SECONDS", "86401"),
        ("HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS", "99"),
        ("HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS", "120001"),
    ],
)
def test_invalid_explicit_values_raise_configuration_error(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env({name: value})


@pytest.mark.parametrize(
    ("name", "minimum", "maximum", "attribute"),
    [
        (
            "HERMES_GALILEO_MAX_CONTENT_CHARS",
            "256",
            "1000000",
            "max_content_chars",
        ),
        (
            "HERMES_GALILEO_MAX_COLLECTION_ITEMS",
            "1",
            "10000",
            "max_collection_items",
        ),
        ("HERMES_GALILEO_SAMPLE_RATE", "0", "1", "sample_rate"),
        ("HERMES_GALILEO_TURN_TTL_SECONDS", "30", "86400", "turn_ttl_seconds"),
        (
            "HERMES_GALILEO_FLUSH_TIMEOUT_MILLIS",
            "100",
            "120000",
            "flush_timeout_millis",
        ),
    ],
)
def test_numeric_boundaries_are_accepted(
    name: str,
    minimum: str,
    maximum: str,
    attribute: str,
) -> None:
    low = Settings.from_env({name: minimum})
    high = Settings.from_env({name: maximum})

    assert float(getattr(low, attribute)) == float(minimum)
    assert float(getattr(high, attribute)) == float(maximum)


def test_missing_required_reports_only_absent_or_blank_values() -> None:
    settings = Settings.from_env(
        {
            "GALILEO_API_KEY": "configured",
            "GALILEO_PROJECT": "   ",
        }
    )

    assert settings.missing_required() == (
        "GALILEO_PROJECT",
        "GALILEO_LOG_STREAM",
    )


@pytest.mark.parametrize(
    "env",
    [
        {"HERMES_GALILEO_ENABLED": "false"},
        {
            "HERMES_GALILEO_ENABLED": "true",
            "GALILEO_LOGGING_DISABLED": "true",
        },
    ],
)
def test_disabled_plugin_does_not_require_galileo_credentials(
    env: dict[str, str],
) -> None:
    settings = Settings.from_env(env)

    assert settings.enabled is False
    assert settings.missing_required() == ()


def test_all_required_values_satisfy_missing_check() -> None:
    settings = Settings.from_env(
        {
            "GALILEO_API_KEY": "key",
            "GALILEO_PROJECT": "project",
            "GALILEO_LOG_STREAM": "stream",
        }
    )

    assert settings.missing_required() == ()
