"""Tests for the latest-Galileo test environment installer."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import scripts.install_latest_test_stack as installer
from scripts.install_latest_test_stack import install, parse_resolved_json

_RESOLVED_JSON = '{"galileo":"3.0.0rc1","opentelemetry-api":"1.37.0","opentelemetry-sdk":"1.37.0"}'


def test_installer_uses_no_deps_for_project_then_installs_dev_and_latest(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
version = "0"
dependencies = ["galileo[otel]>=2.5,<3", "PyYAML>=6,<7"]

[project.optional-dependencies]
dev = ["pytest>=8,<9", "ruff>=0.12,<1"]
""".strip()
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: calls.append(command),
    )
    installed = {
        "galileo": "3.0.0rc1",
        "opentelemetry-api": "1.37.0",
        "opentelemetry-sdk": "1.37.0",
    }
    monkeypatch.setattr(installer.metadata, "version", installed.__getitem__)

    install(tmp_path, "3.0.0rc1", _RESOLVED_JSON)

    assert calls[0][-3:] == ["--no-deps", "-e", str(tmp_path)]
    assert calls[1][-6:] == [
        "PyYAML>=6,<7",
        "pytest>=8,<9",
        "ruff>=0.12,<1",
        "galileo[otel]==3.0.0rc1",
        "opentelemetry-api==1.37.0",
        "opentelemetry-sdk==1.37.0",
    ]


def test_installer_rejects_unsafe_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        install(tmp_path, "$(command)", _RESOLVED_JSON)


@pytest.mark.parametrize(
    ("resolved_json", "match"),
    [
        ('{"Galileo":"3.0.0rc1"}', "canonicalized"),
        ('{"galileo":"2.5.1"}', "must contain galileo=3.0.0rc1"),
        (
            '{"galileo":"3.0.0rc1","unsafe/name":"5.29.5"}',
            "unsafe distribution",
        ),
        ("[]", "non-empty object"),
    ],
)
def test_resolved_json_validation(resolved_json: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_resolved_json(resolved_json, "3.0.0rc1")


def test_installer_fails_when_pip_did_not_honor_exact_stack(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "example"
version = "0"
dependencies = ["galileo[otel]>=2.5,<3"]

[project.optional-dependencies]
dev = []
""".strip()
    )
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: None)
    installed = {
        "galileo": "3.0.0rc1",
        "opentelemetry-api": "1.38.0",
        "opentelemetry-sdk": "1.37.0",
    }
    monkeypatch.setattr(installer.metadata, "version", installed.__getitem__)

    with pytest.raises(RuntimeError, match=r"opentelemetry-api version 1\.38\.0"):
        install(tmp_path, "3.0.0rc1", _RESOLVED_JSON)
