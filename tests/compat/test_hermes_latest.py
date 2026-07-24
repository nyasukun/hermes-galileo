"""Contract checks against a real Hermes Agent source checkout."""

from __future__ import annotations

import os
import sys
from importlib.metadata import distribution
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


class _RuntimeRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def record(payload: dict[str, Any]) -> None:
            self.events.append((name, payload))

        return record


@pytest.mark.compat
def test_latest_hermes_accepts_plugin_entry_point_and_observer_hooks(
    monkeypatch: Any,
) -> None:
    source_text = os.environ.get("HERMES_AGENT_SOURCE", "").strip()
    if not source_text:
        pytest.skip("HERMES_AGENT_SOURCE does not point to a Hermes checkout")

    source = Path(source_text).resolve()
    assert (source / "hermes_cli" / "plugins.py").is_file()
    monkeypatch.syspath_prepend(str(source))

    from hermes_cli.plugins import (
        VALID_HOOKS,
        PluginContext,
        PluginManager,
        PluginManifest,
    )

    entry_point = next(
        item
        for item in distribution("hermes-galileo").entry_points
        if item.group == "hermes_agent.plugins" and item.name == "hermes_galileo"
    )
    module = entry_point.load()
    assert isinstance(module, ModuleType)
    assert callable(module.register)

    plugin_hooks = {name for name, _ in module._HOOKS}
    assert plugin_hooks <= set(VALID_HOOKS)
    assert module._valid_hermes_hooks() == set(VALID_HOOKS)

    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name="hermes-galileo", source="entrypoint"),
        manager,
    )
    runtime = _RuntimeRecorder()
    monkeypatch.setattr(module, "initialize", lambda: runtime)
    module.hooks.set_runtime(runtime)
    try:
        module.register(context)

        assert set(manager._hooks) == plugin_hooks
        assert all(len(callbacks) == 1 for callbacks in manager._hooks.values())
        for hook_name in sorted(plugin_hooks):
            manager.invoke_hook(hook_name, compatibility_probe=hook_name)
        assert len(runtime.events) == len(plugin_hooks)
        assert {payload["compatibility_probe"] for _, payload in runtime.events} == plugin_hooks
        assert {payload["telemetry_schema_version"] for _, payload in runtime.events} == {
            "hermes.observer.v1"
        }
    finally:
        module.hooks.set_runtime(None)
        for imported in tuple(sys.modules):
            if imported == "hermes_cli" or imported.startswith("hermes_cli."):
                sys.modules.pop(imported, None)
