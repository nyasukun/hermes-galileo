#!/usr/bin/env python3
"""Install this project for tests while deliberately overriding Galileo's range."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 CI job
    import tomli as tomllib

_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")
_GALILEO_REQUIREMENT = re.compile(r"^\s*galileo(?:\[|[<=>!~;\s]|$)", re.IGNORECASE)
_CANONICAL_NAME_SEPARATOR = re.compile(r"[-_.]+")
_DISTRIBUTION_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _project_data(repository: Path) -> dict[str, Any]:
    with (repository / "pyproject.toml").open("rb") as project_file:
        return tomllib.load(project_file)


def _canonical_name(value: Any) -> str:
    return _CANONICAL_NAME_SEPARATOR.sub("-", str(value).strip()).lower()


def _is_resolved_distribution(name: str) -> bool:
    return bool(_DISTRIBUTION_NAME_PATTERN.fullmatch(name))


def parse_resolved_json(value: str, galileo_version: str) -> dict[str, str]:
    """Validate the exact Galileo dependency closure emitted by detection."""

    if not _VERSION_PATTERN.fullmatch(galileo_version):
        raise ValueError("Galileo version contains unsafe characters")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"resolved JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError("resolved JSON must be a non-empty object")

    resolved: dict[str, str] = {}
    for raw_name, raw_version in payload.items():
        if not isinstance(raw_name, str):
            raise ValueError("resolved distribution names must be strings")
        name = _canonical_name(raw_name)
        if raw_name != name:
            raise ValueError(
                f"resolved distribution name {raw_name!r} must be canonicalized as {name!r}"
            )
        if not _is_resolved_distribution(name):
            raise ValueError(f"resolved JSON contains unsafe distribution {name!r}")
        if not isinstance(raw_version, str) or not _VERSION_PATTERN.fullmatch(raw_version):
            raise ValueError(f"resolved version for {name} contains unsafe characters")
        resolved[name] = raw_version

    if resolved.get("galileo") != galileo_version:
        raise ValueError(
            f"resolved JSON must contain galileo={galileo_version}, got {resolved.get('galileo')!r}"
        )
    return dict(sorted(resolved.items()))


def _verify_installed_versions(resolved: dict[str, str]) -> None:
    for name, expected in resolved.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"resolved distribution {name} was not installed") from exc
        if actual != expected:
            raise RuntimeError(
                f"installed {name} version {actual} does not match resolved version {expected}"
            )


def install(
    repository: Path,
    galileo_version: str,
    resolved_json: str,
) -> None:
    """Install editable code, dev tools, and the exact Galileo dependency closure."""

    resolved = parse_resolved_json(resolved_json, galileo_version)
    project = _project_data(repository)
    runtime_dependencies = project["project"].get("dependencies", [])
    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    if not isinstance(runtime_dependencies, list) or not all(
        isinstance(item, str) for item in runtime_dependencies
    ):
        raise ValueError("project.dependencies must be a string list")
    if not isinstance(dev_dependencies, list) or not all(
        isinstance(item, str) for item in dev_dependencies
    ):
        raise ValueError("project.optional-dependencies.dev must be a string list")
    non_galileo_runtime = [
        dependency
        for dependency in runtime_dependencies
        if not _GALILEO_REQUIREMENT.match(dependency)
    ]
    exact_stack = [f"galileo[otel]=={galileo_version}"]
    exact_stack.extend(
        f"{name}=={version}" for name, version in resolved.items() if name != "galileo"
    )

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "-e",
            str(repository),
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            *non_galileo_runtime,
            *dev_dependencies,
            *exact_stack,
        ],
        check=True,
    )
    _verify_installed_versions(resolved)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--galileo-version", required=True)
    parser.add_argument("--resolved-json", required=True)
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args(argv)
    install(
        args.repository.resolve(),
        args.galileo_version,
        args.resolved_json,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
