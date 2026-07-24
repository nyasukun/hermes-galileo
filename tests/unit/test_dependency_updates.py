"""Tests for the upstream dependency baseline helper."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import scripts.check_dependency_updates as dependency_updates
from scripts.check_dependency_updates import (
    Baseline,
    BaselineError,
    Latest,
    _resolved_from_pip_report,
    load_baseline,
    resolve_galileo_stack,
    update_flags,
    write_baseline,
)


@pytest.fixture
def baseline() -> Baseline:
    return Baseline(
        hermes_repository="NousResearch/hermes-agent",
        hermes_ref="main",
        hermes_sha="1" * 40,
        galileo_package="galileo",
        galileo_version="2.5.1",
        galileo_resolved={
            "galileo": "2.5.1",
            "opentelemetry-api": "1.36.0",
        },
    )


def test_update_flags_detect_each_upstream_independently(baseline: Baseline) -> None:
    unchanged = update_flags(
        baseline,
        Latest(
            hermes_sha="1" * 40,
            galileo_version="2.5.1",
            galileo_resolved=baseline.galileo_resolved,
        ),
    )
    hermes_update = update_flags(
        baseline,
        Latest(
            hermes_sha="2" * 40,
            galileo_version="2.5.1",
            galileo_resolved=baseline.galileo_resolved,
        ),
    )
    galileo_update = update_flags(
        baseline,
        Latest(
            hermes_sha="1" * 40,
            galileo_version="2.6.0",
            galileo_resolved={
                "galileo": "2.6.0",
                "opentelemetry-api": "1.37.0",
            },
        ),
    )
    transitive_update = update_flags(
        baseline,
        Latest(
            hermes_sha="1" * 40,
            galileo_version="2.5.1",
            galileo_resolved={
                "galileo": "2.5.1",
                "opentelemetry-api": "1.37.0",
            },
        ),
    )

    assert unchanged["updated"] == "false"
    assert unchanged["resolved_json"] == ('{"galileo":"2.5.1","opentelemetry-api":"1.36.0"}')
    assert unchanged["baseline_resolved_json"] == unchanged["resolved_json"]
    assert hermes_update["updated"] == "true"
    assert hermes_update["hermes_updated"] == "true"
    assert hermes_update["galileo_updated"] == "false"
    assert galileo_update["updated"] == "true"
    assert galileo_update["galileo_updated"] == "true"
    assert transitive_update["updated"] == "true"
    assert transitive_update["galileo_updated"] == "true"


def test_write_and_load_baseline_round_trip(
    tmp_path: Path,
    baseline: Baseline,
) -> None:
    path = tmp_path / "baseline.json"

    write_baseline(
        path,
        baseline,
        Latest(
            hermes_sha="a" * 40,
            galileo_version="3.0.0rc1",
            galileo_resolved={
                "galileo": "3.0.0rc1",
                "galileo-core": "1.2.3",
                "opentelemetry-api": "1.37.0",
            },
        ),
    )

    assert load_baseline(path) == Baseline(
        hermes_repository="NousResearch/hermes-agent",
        hermes_ref="main",
        hermes_sha="a" * 40,
        galileo_package="galileo",
        galileo_version="3.0.0rc1",
        galileo_resolved={
            "galileo": "3.0.0rc1",
            "galileo-core": "1.2.3",
            "opentelemetry-api": "1.37.0",
        },
    )
    assert json.loads(path.read_text())["hermes_agent"]["sha"] == "a" * 40
    assert json.loads(path.read_text())["galileo"]["resolved"] == {
        "galileo": "3.0.0rc1",
        "galileo-core": "1.2.3",
        "opentelemetry-api": "1.37.0",
    }


def test_load_baseline_rejects_unsafe_values(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "hermes_agent": {
                    "repository": "NousResearch/hermes-agent",
                    "ref": "main",
                    "sha": "$(unsafe)",
                },
                "galileo": {
                    "package": "galileo",
                    "resolved": {"galileo": "2.5.1"},
                    "version": "2.5.1",
                },
            }
        )
    )

    with pytest.raises(BaselineError, match="Git SHA"):
        load_baseline(path)


def test_resolved_report_tracks_the_full_dependency_closure() -> None:
    report = {
        "install": [
            {"metadata": {"name": "Galileo", "version": "2.5.1"}},
            {"metadata": {"name": "galileo_core", "version": "1.8.0"}},
            {"metadata": {"name": "OpenTelemetry-API", "version": "1.36.0"}},
            {
                "metadata": {
                    "name": "opentelemetry-instrumentation-openai",
                    "version": "0.47.3",
                }
            },
            {"metadata": {"name": "protobuf", "version": "5.29.5"}},
        ]
    }

    assert _resolved_from_pip_report(report, galileo_version="2.5.1") == {
        "galileo": "2.5.1",
        "galileo-core": "1.8.0",
        "opentelemetry-api": "1.36.0",
        "opentelemetry-instrumentation-openai": "0.47.3",
        "protobuf": "5.29.5",
    }


def test_resolver_uses_pip_dry_run_ignore_installed_report(
    monkeypatch: Any,
) -> None:
    observed: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        observed.extend(command)
        report_path = Path(command[command.index("--report") + 1])
        report_path.write_text(
            json.dumps(
                {
                    "install": [
                        {"metadata": {"name": "galileo", "version": "2.5.1"}},
                        {
                            "metadata": {
                                "name": "opentelemetry-sdk",
                                "version": "1.36.0",
                            }
                        },
                    ]
                }
            )
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(dependency_updates.subprocess, "run", fake_run)

    assert resolve_galileo_stack(
        "galileo",
        "2.5.1",
        python_executable="/safe/python",
    ) == {
        "galileo": "2.5.1",
        "opentelemetry-sdk": "1.36.0",
    }
    assert observed[:4] == ["/safe/python", "-m", "pip", "install"]
    assert "--dry-run" in observed
    assert "--ignore-installed" in observed
    assert "--report" in observed
    assert observed[-1] == "galileo[otel]==2.5.1"


@pytest.mark.parametrize(
    ("resolved", "match"),
    [
        ({"Galileo": "2.5.1"}, "canonicalized"),
        ({"galileo": "2.5.0"}, "must contain galileo=2.5.1"),
        (
            {"galileo": "2.5.1", "unsafe/name": "5.29.5"},
            "unsafe distribution",
        ),
        ({"galileo": "2.5.1", "opentelemetry-api": None}, "must be a string"),
    ],
)
def test_write_baseline_rejects_invalid_resolved_map(
    tmp_path: Path,
    baseline: Baseline,
    resolved: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(BaselineError, match=match):
        write_baseline(
            tmp_path / "baseline.json",
            baseline,
            Latest(
                hermes_sha="a" * 40,
                galileo_version="2.5.1",
                galileo_resolved=resolved,
            ),
        )
