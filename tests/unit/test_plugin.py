"""Tests for Hermes plugin registration and fail-open behavior."""

from __future__ import annotations

import importlib.util
import sys
from importlib.metadata import distribution
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import hermes_galileo
from hermes_galileo import hooks
from hermes_galileo.config import ConfigurationError, Settings
from hermes_galileo.runtime import RuntimeInitializationError


class _Context:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def register_hook(self, name: str, callback: Any) -> None:
        self.hooks[name] = callback


@pytest.fixture(autouse=True)
def _reset_plugin_runtime(monkeypatch: Any) -> None:
    hooks.set_runtime(None)
    monkeypatch.setattr(hermes_galileo, "_ATEXIT_REGISTERED", False)
    yield
    hooks.set_runtime(None)


def test_register_installs_supported_hooks_after_local_runtime_initialization(
    monkeypatch: Any,
) -> None:
    context = _Context()
    runtime = object()
    monkeypatch.setattr(hermes_galileo, "initialize", lambda: runtime)
    monkeypatch.setattr(
        hermes_galileo,
        "_valid_hermes_hooks",
        lambda: {name for name, _ in hermes_galileo._HOOKS},
    )

    hermes_galileo.register(context)

    assert set(context.hooks) == {name for name, _ in hermes_galileo._HOOKS}


def test_register_without_credentials_is_disabled(monkeypatch: Any) -> None:
    context = _Context()
    monkeypatch.setattr(hermes_galileo, "initialize", lambda: None)

    hermes_galileo.register(context)

    assert context.hooks == {}


def test_observer_callback_is_fail_open_when_runtime_raises() -> None:
    class BrokenRuntime:
        def on_pre_llm_call(self, payload: dict[str, Any]) -> None:
            raise RuntimeError("telemetry backend failed")

    hooks.set_runtime(BrokenRuntime())  # type: ignore[arg-type]
    try:
        hooks.on_pre_llm_call(session_id="session", user_message="must not be logged")
    finally:
        hooks.set_runtime(None)


def test_health_snapshot_reports_disabled_without_runtime() -> None:
    hooks.set_runtime(None)

    assert hermes_galileo.health_snapshot() == {"enabled": False}


def test_repository_root_loads_as_a_hermes_directory_plugin() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    module_name = "hermes_plugins.hermes_galileo_loader_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        repository_root / "__init__.py",
        submodule_search_locations=[str(repository_root)],
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        assert module.register is not None
        assert module.initialize is not None
        assert module.health_snapshot() == {"enabled": False}
    finally:
        for loaded_name in list(sys.modules):
            if loaded_name == module_name or loaded_name.startswith(f"{module_name}."):
                sys.modules.pop(loaded_name, None)


def test_installed_entry_point_loads_a_module_with_register() -> None:
    entry_point = next(
        item
        for item in distribution("hermes-galileo").entry_points
        if item.group == "hermes_agent.plugins" and item.name == "hermes_galileo"
    )

    module = entry_point.load()

    assert isinstance(module, ModuleType)
    assert callable(module.register)


def test_initialize_mirrors_validated_settings_and_registers_shutdown(
    monkeypatch: Any,
) -> None:
    settings = Settings.from_env(
        {
            "GALILEO_API_KEY": "programmatic-key",
            "GALILEO_PROJECT": "programmatic-project",
            "GALILEO_LOG_STREAM": "programmatic-stream",
            "GALILEO_CONSOLE_URL": "https://console.example.test",
            "GALILEO_API_URL": "https://api.example.test",
        }
    )
    created: list[Settings] = []
    shutdown_callbacks: list[Any] = []

    class FakeRuntime:
        def __init__(self, value: Settings) -> None:
            created.append(value)

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(hermes_galileo, "TelemetryRuntime", FakeRuntime)
    monkeypatch.setattr(hermes_galileo.atexit, "register", shutdown_callbacks.append)

    runtime = hermes_galileo.initialize(settings)

    assert isinstance(runtime, FakeRuntime)
    assert created == [settings]
    assert hooks.get_runtime() is runtime
    assert shutdown_callbacks == [hermes_galileo._shutdown_runtime]
    assert hermes_galileo.os.environ["GALILEO_API_KEY"] == "programmatic-key"
    assert hermes_galileo.os.environ["GALILEO_PROJECT"] == "programmatic-project"
    assert hermes_galileo.os.environ["GALILEO_LOG_STREAM"] == "programmatic-stream"
    assert hermes_galileo.os.environ["GALILEO_CONSOLE_URL"] == "https://console.example.test"
    assert hermes_galileo.os.environ["GALILEO_API_URL"] == "https://api.example.test"
    assert hermes_galileo.initialize(settings) is runtime


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeInitializationError("dependencies unavailable"),
        RuntimeError("unexpected SDK failure"),
    ],
)
def test_initialize_is_fail_open_for_runtime_failures(
    monkeypatch: Any,
    failure: Exception,
) -> None:
    settings = Settings.from_env(
        {
            "GALILEO_API_KEY": "key",
            "GALILEO_PROJECT": "project",
            "GALILEO_LOG_STREAM": "stream",
        }
    )

    def fail(_settings: Settings) -> None:
        raise failure

    monkeypatch.setattr(hermes_galileo, "TelemetryRuntime", fail)

    assert hermes_galileo.initialize(settings) is None
    assert hooks.get_runtime() is None


def test_initialize_is_fail_open_for_invalid_missing_and_disabled_settings(
    monkeypatch: Any,
) -> None:
    missing = Settings.from_env({})
    disabled = Settings.from_env({"HERMES_GALILEO_ENABLED": "false"})
    monkeypatch.setattr(
        hermes_galileo.Settings,
        "from_env",
        classmethod(lambda cls: (_ for _ in ()).throw(ConfigurationError("invalid"))),
    )
    assert hermes_galileo.initialize() is None

    assert hermes_galileo.initialize(missing) is None
    assert hermes_galileo.initialize(disabled) is None


def test_shutdown_flush_health_and_all_hook_adapters() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class RecordingRuntime:
        def __getattr__(self, name: str) -> Any:
            return lambda payload: calls.append((name, payload))

        def force_flush(self) -> bool:
            return False

        def health_snapshot(self) -> dict[str, Any]:
            return {"enabled": True, "inflight_turns": 2}

        def shutdown(self) -> None:
            calls.append(("shutdown", {}))

    runtime = RecordingRuntime()
    hooks.set_runtime(runtime)  # type: ignore[arg-type]

    for hook_name, callback in hermes_galileo._HOOKS:
        callback(probe=hook_name)

    assert [name for name, _ in calls] == [
        "on_session_start",
        "on_session_end",
        "on_session_finalize",
        "on_session_reset",
        "on_pre_llm_call",
        "on_post_llm_call",
        "on_pre_api_request",
        "on_post_api_request",
        "on_api_request_error",
        "on_pre_tool_call",
        "on_post_tool_call",
        "on_pre_approval_request",
        "on_post_approval_response",
        "on_subagent_start",
        "on_subagent_stop",
    ]
    assert all(payload["probe"] for _, payload in calls)
    assert hermes_galileo.force_flush() is False
    assert hermes_galileo.health_snapshot()["inflight_turns"] == 2
    hermes_galileo._shutdown_runtime()
    assert calls[-1] == ("shutdown", {})
    assert hooks.get_runtime() is None
