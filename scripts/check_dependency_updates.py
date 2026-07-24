#!/usr/bin/env python3
"""Detect and record upstream Hermes Agent and Galileo SDK updates."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]*$")
_CANONICAL_NAME_SEPARATOR = re.compile(r"[-_.]+")
_DISTRIBUTION_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BaselineError(ValueError):
    """Raised when the dependency baseline or an upstream response is invalid."""


@dataclass(frozen=True, slots=True)
class Baseline:
    hermes_repository: str
    hermes_ref: str
    hermes_sha: str
    galileo_package: str
    galileo_version: str
    galileo_resolved: dict[str, str]


@dataclass(frozen=True, slots=True)
class Latest:
    hermes_sha: str
    galileo_version: str
    galileo_resolved: dict[str, str]


def _validated_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise BaselineError(f"{label} must be a string")
    text = value.strip().lower()
    if not _SHA_PATTERN.fullmatch(text):
        raise BaselineError(f"{label} must be a 40-character lowercase Git SHA")
    return text


def _validated_version(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise BaselineError(f"{label} must be a string")
    text = value.strip()
    if not _VERSION_PATTERN.fullmatch(text):
        raise BaselineError(f"{label} is not a safe package version")
    return text


def _canonical_name(value: Any) -> str:
    return _CANONICAL_NAME_SEPARATOR.sub("-", str(value).strip()).lower()


def _is_resolved_distribution(name: str) -> bool:
    return bool(_DISTRIBUTION_NAME_PATTERN.fullmatch(name))


def _validated_resolved(
    value: Any,
    *,
    galileo_version: str,
    label: str,
) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise BaselineError(f"{label} must be a non-empty JSON object")

    resolved: dict[str, str] = {}
    for raw_name, raw_version in value.items():
        if not isinstance(raw_name, str):
            raise BaselineError(f"{label} distribution names must be strings")
        canonical_name = _canonical_name(raw_name)
        if raw_name != canonical_name:
            raise BaselineError(
                f"{label} distribution name {raw_name!r} must be canonicalized "
                f"as {canonical_name!r}"
            )
        if not _is_resolved_distribution(canonical_name):
            raise BaselineError(f"{label} contains unsafe distribution {canonical_name!r}")
        resolved[canonical_name] = _validated_version(
            raw_version,
            label=f"{label} version for {canonical_name}",
        )

    if resolved.get("galileo") != galileo_version:
        raise BaselineError(
            f"{label} must contain galileo={galileo_version}, got {resolved.get('galileo')!r}"
        )
    return dict(sorted(resolved.items()))


def _compact_json(value: dict[str, str]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _resolved_from_pip_report(
    payload: Any,
    *,
    galileo_version: str,
) -> dict[str, str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("install"), list):
        raise BaselineError("pip resolution report must contain an install list")

    resolved: dict[str, str] = {}
    for item in payload["install"]:
        if not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            raise BaselineError("pip resolution report contains invalid package metadata")
        metadata = item["metadata"]
        try:
            raw_name = metadata["name"]
            if not isinstance(raw_name, str):
                raise BaselineError("pip resolved distribution name must be a string")
            name = _canonical_name(raw_name)
            version = _validated_version(
                metadata["version"],
                label=f"resolved version for {name}",
            )
        except KeyError as exc:
            raise BaselineError(
                f"pip resolution report metadata is missing {exc.args[0]!r}"
            ) from exc
        if not _is_resolved_distribution(name):
            raise BaselineError(f"pip resolved unsafe distribution name {name!r}")
        previous = resolved.get(name)
        if previous is not None and previous != version:
            raise BaselineError(
                f"pip resolved conflicting versions for {name}: {previous} and {version}"
            )
        resolved[name] = version

    return _validated_resolved(
        resolved,
        galileo_version=galileo_version,
        label="resolved Galileo dependency closure",
    )


def resolve_galileo_stack(
    package: str,
    version: str,
    *,
    python_executable: str = sys.executable,
) -> dict[str, str]:
    """Resolve Galileo's complete dependency closure without installing it."""

    safe_version = _validated_version(version, label="latest Galileo version")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", package):
        raise BaselineError("Galileo package contains unsafe characters")

    with tempfile.TemporaryDirectory(prefix="hermes-galileo-pip-report-") as temp_dir:
        report_path = Path(temp_dir) / "report.json"
        command = [
            python_executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--dry-run",
            "--ignore-installed",
            "--report",
            str(report_path),
            f"{package}[otel]=={safe_version}",
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or str(exc)).strip()
            raise BaselineError(f"pip could not resolve Galileo dependencies: {details}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BaselineError(f"cannot read pip resolution report: {exc}") from exc

    return _resolved_from_pip_report(payload, galileo_version=safe_version)


def load_baseline(path: Path) -> Baseline:
    """Load and validate the committed dependency baseline."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        hermes = data["hermes_agent"]
        galileo = data["galileo"]
        repository = str(hermes["repository"]).strip()
        ref = str(hermes["ref"]).strip()
        hermes_sha = hermes["sha"]
        package = str(galileo["package"]).strip()
        galileo_version = _validated_version(
            galileo["version"],
            label="Galileo baseline version",
        )
        galileo_resolved = galileo["resolved"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot load dependency baseline {path}: {exc}") from exc

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise BaselineError("Hermes repository must use the owner/repository form")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", ref):
        raise BaselineError("Hermes ref contains unsafe characters")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", package):
        raise BaselineError("Galileo package contains unsafe characters")

    return Baseline(
        hermes_repository=repository,
        hermes_ref=ref,
        hermes_sha=_validated_sha(hermes_sha, label="Hermes baseline SHA"),
        galileo_package=package,
        galileo_version=galileo_version,
        galileo_resolved=_validated_resolved(
            galileo_resolved,
            galileo_version=galileo_version,
            label="Galileo baseline dependency closure",
        ),
    )


def _get_json(url: str, *, token: str = "", attempts: int = 3) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "hermes-galileo-dependency-watch",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read()
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise BaselineError(f"{url} did not return a JSON object")
            return payload
        except (
            OSError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)

    raise BaselineError(f"failed to query {url}: {last_error}")


def fetch_latest(baseline: Baseline, *, github_token: str = "") -> Latest:
    """Fetch the current Hermes ref SHA and current PyPI Galileo release."""

    repository = baseline.hermes_repository
    ref = baseline.hermes_ref
    hermes_payload = _get_json(
        f"https://api.github.com/repos/{repository}/commits/{ref}",
        token=github_token,
    )
    galileo_payload = _get_json(f"https://pypi.org/pypi/{baseline.galileo_package}/json")
    try:
        hermes_sha = _validated_sha(
            hermes_payload["sha"],
            label="latest Hermes SHA",
        )
        galileo_version = _validated_version(
            galileo_payload["info"]["version"],
            label="latest Galileo version",
        )
    except (KeyError, TypeError) as exc:
        raise BaselineError(f"unexpected upstream response: {exc}") from exc
    galileo_resolved = resolve_galileo_stack(
        baseline.galileo_package,
        galileo_version,
    )
    return Latest(
        hermes_sha=hermes_sha,
        galileo_version=galileo_version,
        galileo_resolved=galileo_resolved,
    )


def update_flags(baseline: Baseline, latest: Latest) -> dict[str, str]:
    """Return GitHub Actions-compatible update outputs."""

    hermes_updated = baseline.hermes_sha != latest.hermes_sha
    galileo_updated = (
        baseline.galileo_version != latest.galileo_version
        or baseline.galileo_resolved != latest.galileo_resolved
    )
    return {
        "updated": str(hermes_updated or galileo_updated).lower(),
        "hermes_updated": str(hermes_updated).lower(),
        "galileo_updated": str(galileo_updated).lower(),
        "hermes_sha": latest.hermes_sha,
        "galileo_version": latest.galileo_version,
        "resolved_json": _compact_json(latest.galileo_resolved),
        "baseline_hermes_sha": baseline.hermes_sha,
        "baseline_galileo_version": baseline.galileo_version,
        "baseline_resolved_json": _compact_json(baseline.galileo_resolved),
    }


def write_baseline(path: Path, baseline: Baseline, latest: Latest) -> None:
    """Write a tested upstream version pair to the committed baseline."""

    data = {
        "galileo": {
            "package": baseline.galileo_package,
            "resolved": _validated_resolved(
                latest.galileo_resolved,
                galileo_version=latest.galileo_version,
                label="tested Galileo dependency closure",
            ),
            "version": _validated_version(
                latest.galileo_version,
                label="latest Galileo version",
            ),
        },
        "hermes_agent": {
            "ref": baseline.hermes_ref,
            "repository": baseline.hermes_repository,
            "sha": _validated_sha(latest.hermes_sha, label="latest Hermes SHA"),
        },
    }
    path.write_text(f"{json.dumps(data, indent=2, sort_keys=True)}\n", encoding="utf-8")


def _write_github_outputs(outputs: dict[str, str]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output_file:
        for name, value in outputs.items():
            output_file.write(f"{name}={value}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(".github/dependency-baseline.json"),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="record the explicitly supplied, successfully tested versions",
    )
    parser.add_argument("--hermes-sha")
    parser.add_argument("--galileo-version")
    parser.add_argument(
        "--resolved-json",
        help="tested Galileo dependency-closure version map",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        baseline = load_baseline(args.baseline)
        if args.write:
            if not args.hermes_sha or not args.galileo_version or not args.resolved_json:
                raise BaselineError(
                    "--write requires --hermes-sha, --galileo-version, and --resolved-json"
                )
            try:
                resolved_payload = json.loads(args.resolved_json)
            except json.JSONDecodeError as exc:
                raise BaselineError(f"--resolved-json is invalid JSON: {exc}") from exc
            latest = Latest(
                hermes_sha=_validated_sha(
                    args.hermes_sha,
                    label="tested Hermes SHA",
                ),
                galileo_version=_validated_version(
                    args.galileo_version,
                    label="tested Galileo version",
                ),
                galileo_resolved=_validated_resolved(
                    resolved_payload,
                    galileo_version=_validated_version(
                        args.galileo_version,
                        label="tested Galileo version",
                    ),
                    label="tested Galileo dependency closure",
                ),
            )
            write_baseline(args.baseline, baseline, latest)
            print(
                "Recorded tested dependencies: "
                f"Hermes {latest.hermes_sha}, Galileo {latest.galileo_version}, "
                f"resolved={_compact_json(latest.galileo_resolved)}"
            )
            return 0

        latest = fetch_latest(
            baseline,
            github_token=os.environ.get("GITHUB_TOKEN", ""),
        )
        outputs = update_flags(baseline, latest)
        _write_github_outputs(outputs)
        print(
            "Dependency check: "
            f"Hermes {baseline.hermes_sha} -> {latest.hermes_sha}; "
            f"Galileo {baseline.galileo_version} -> {latest.galileo_version}; "
            f"resolved {_compact_json(baseline.galileo_resolved)} -> "
            f"{_compact_json(latest.galileo_resolved)}; "
            f"updated={outputs['updated']}"
        )
        return 0
    except BaselineError as exc:
        print(f"dependency check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
