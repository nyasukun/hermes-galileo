"""Unit tests for environment-backed plugin configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

import hermes_galileo.config as config_module
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
    assert settings.native_sessions_enabled is True
    assert settings.native_session_timeout_millis == 5_000


def test_explicit_mapping_is_isolated_from_default_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_path = tmp_path / "config.yaml"
    default_path.write_text("enabled: false\nsample_rate: 0.25\n", encoding="utf-8")
    monkeypatch.setattr(config_module, "_default_config_path", lambda: default_path)

    settings = Settings.from_env({})

    assert settings.enabled is True
    assert settings.sample_rate == 1.0


def test_process_environment_loads_default_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_path = tmp_path / "config.yaml"
    default_path.write_text(
        "capture_content: true\nsample_rate: 0.25\nenvironment: staging\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "_default_config_path", lambda: default_path)
    for env_name in (
        "HERMES_GALILEO_CAPTURE_CONTENT",
        "HERMES_GALILEO_SAMPLE_RATE",
        "HERMES_GALILEO_ENVIRONMENT",
        "GALILEO_LOGGING_DISABLED",
    ):
        monkeypatch.delenv(env_name, raising=False)

    settings = Settings.from_env()

    assert settings.capture_content is True
    assert settings.sample_rate == 0.25
    assert settings.environment == "staging"


def test_process_environment_resolves_config_from_active_hermes_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_home = tmp_path / "profiles" / "first"
    second_home = tmp_path / "profiles" / "second"
    for hermes_home, environment in (
        (first_home, "first-profile"),
        (second_home, "second-profile"),
    ):
        config_path = hermes_home / "plugins" / "hermes_galileo" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(f"environment: {environment}\n", encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(first_home))
    first_settings = Settings.from_env()
    assert first_settings.environment == "first-profile"
    assert first_settings.hermes_home == str(first_home.resolve())

    monkeypatch.setenv("HERMES_HOME", str(second_home))
    second_settings = Settings.from_env()
    assert second_settings.environment == "second-profile"
    assert second_settings.hermes_home == str(second_home.resolve())


def test_explicit_config_path_loads_all_supported_behavior_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            (
                "enabled: false",
                "capture_content: true",
                "capture_conversation_history: true",
                "hash_user_ids: false",
                "max_content_chars: 4096",
                "max_collection_items: 50",
                "sample_rate: 0.5",
                "turn_ttl_seconds: 300",
                "async_flush_on_turn_end: false",
                "flush_timeout_millis: 2500",
                "debug: true",
                "environment: production",
                "service_name: hermes-custom",
                "native_sessions_enabled: false",
                "native_session_timeout_millis: 1500",
            )
        ),
        encoding="utf-8",
    )

    settings = Settings.from_env({}, config_path=config_path)

    assert settings.enabled is False
    assert settings.capture_content is True
    assert settings.capture_conversation_history is True
    assert settings.hash_user_ids is False
    assert settings.max_content_chars == 4_096
    assert settings.max_collection_items == 50
    assert settings.sample_rate == 0.5
    assert settings.turn_ttl_seconds == 300
    assert settings.async_flush_on_turn_end is False
    assert settings.flush_timeout_millis == 2_500
    assert settings.debug is True
    assert settings.environment == "production"
    assert settings.service_name == "hermes-custom"
    assert settings.native_sessions_enabled is False
    assert settings.native_session_timeout_millis == 1_500


def test_tracked_yaml_example_matches_the_supported_schema() -> None:
    example_path = Path(__file__).resolve().parents[2] / "config.yaml.example"

    settings = Settings.from_env({}, config_path=example_path)

    assert settings.enabled is True
    assert settings.capture_content is False
    assert settings.native_sessions_enabled is True


def test_environment_values_override_yaml_per_field(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "capture_content: false\nsample_rate: 0.25\nenvironment: yaml\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(
        {
            "HERMES_GALILEO_CAPTURE_CONTENT": "true",
            "HERMES_GALILEO_SAMPLE_RATE": "0.75",
        },
        config_path=config_path,
    )

    assert settings.capture_content is True
    assert settings.sample_rate == 0.75
    assert settings.environment == "yaml"


def test_blank_environment_values_do_not_erase_yaml_fields(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "enabled: false\nsample_rate: 0.25\nenvironment: yaml\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(
        {
            "HERMES_GALILEO_ENABLED": " ",
            "HERMES_GALILEO_SAMPLE_RATE": "",
            "HERMES_GALILEO_ENVIRONMENT": "\t",
        },
        config_path=config_path,
    )

    assert settings.enabled is False
    assert settings.sample_rate == 0.25
    assert settings.environment == "yaml"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("[enabled, true]\n", "root must be a mapping"),
        ("enabled: [\n", "is malformed"),
        ('enabled: "yes"\n', "'enabled' must be a boolean"),
        ("max_content_chars: many\n", "'max_content_chars' must be an integer"),
        ("sample_rate: sometimes\n", "'sample_rate' must be a number"),
        ("environment: 123\n", "'environment' must be a string"),
        ("unknown_setting: true\n", "unknown config.yaml field"),
        ("1: true\n", "field names must be strings"),
    ],
)
def test_invalid_yaml_schema_raises_configuration_error(
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(source, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        Settings.from_env({}, config_path=config_path)


@pytest.mark.parametrize(
    "field_name",
    [
        "api_key",
        "pseudonym_secret",
        "project",
        "log_stream",
        "console_url",
        "api_url",
        "GALILEO_API_KEY",
        "HERMES_GALILEO_PSEUDONYM_SECRET",
    ],
)
def test_yaml_rejects_secret_and_routing_fields(
    tmp_path: Path,
    field_name: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"{field_name}: must-not-leak\n", encoding="utf-8")

    with pytest.raises(
        ConfigurationError,
        match="cannot contain secrets or Galileo routing fields",
    ) as error:
        Settings.from_env({}, config_path=config_path)

    assert "must-not-leak" not in str(error.value)


def test_missing_explicit_config_path_raises_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        Settings.from_env({}, config_path=tmp_path / "missing.yaml")


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
            "HERMES_GALILEO_NATIVE_SESSIONS_ENABLED": "false",
            "HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS": "1234",
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
    assert settings.native_sessions_enabled is False
    assert settings.native_session_timeout_millis == 1_234


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
        ("HERMES_GALILEO_NATIVE_SESSIONS_ENABLED", "sometimes"),
        ("HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS", "99"),
        ("HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS", "120001"),
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
        (
            "HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS",
            "100",
            "120000",
            "native_session_timeout_millis",
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
