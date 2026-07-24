from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.e2e import test_live_galileo as live_e2e
from tests.e2e.test_live_galileo import (
    _conversation_quality_diagnostic,
    _conversation_quality_is_enabled,
    _conversation_quality_metric_keys,
    _conversation_quality_value,
    _enable_conversation_quality,
    _ensure_galileo_resources,
    _poll,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"conversation_quality": {"status_type": "pending"}},
        {
            "metric_info": {
                "conversation_quality": {
                    "status_type": "computing",
                    "message": "Metric is computing.",
                }
            }
        },
        {
            "metrics": [
                {
                    "name": "Conversation Quality",
                    "status_type": "failed",
                    "message": "Metric failed to compute.",
                }
            ]
        },
        {
            "conversation_quality": {
                "status_type": "not_computed",
                "value": 0.9,
            }
        },
    ],
)
def test_conversation_quality_rejects_incomplete_or_failed_metrics(
    payload: dict[str, object],
) -> None:
    assert _conversation_quality_value(payload) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"conversation_quality": 0.75}, 0.75),
        (
            {
                "metric_info": {
                    "conversation_quality": {
                        "status_type": "success",
                        "value": 0.8,
                    }
                }
            },
            0.8,
        ),
        (
            {
                "metrics": [
                    {
                        "metric_name": "Conversation Quality",
                        "status_type": "roll_up",
                        "score": 0.85,
                    }
                ]
            },
            0.85,
        ),
        (
            {
                "metric_info": {
                    "0d41bb42-b42d-46d7-a0db-73ea1335f34e": {
                        "metric_key_alias": "conversation_quality",
                        "status_type": "success",
                        "value": 0.9,
                    }
                }
            },
            0.9,
        ),
    ],
)
def test_conversation_quality_accepts_completed_scores(
    payload: dict[str, object],
    expected: float,
) -> None:
    assert _conversation_quality_value(payload) == expected


def test_conversation_quality_uses_uuid_resolved_from_session_columns() -> None:
    scorer_id = "0d41bb42-b42d-46d7-a0db-73ea1335f34e"
    payload = {
        "metric_info": {
            scorer_id: {
                "status_type": "success",
                "value": 0.95,
            }
        }
    }

    assert (
        _conversation_quality_value(
            payload,
            metric_keys={scorer_id, f"metrics/{scorer_id}"},
        )
        == 0.95
    )


def test_conversation_quality_metric_keys_use_column_label_and_alias(
    monkeypatch: Any,
) -> None:
    scorer_id = "0d41bb42-b42d-46d7-a0db-73ea1335f34e"
    metric_stream = SimpleNamespace(
        session_columns={
            f"metrics/{scorer_id}": SimpleNamespace(
                id=f"metrics/{scorer_id}",
                label="Conversation Quality",
                metric_key_alias="conversation_quality",
            ),
            "created_at": SimpleNamespace(
                id="created_at",
                label="Created At",
                metric_key_alias=None,
            ),
        }
    )
    monkeypatch.setattr(
        live_e2e,
        "MetricLogStream",
        SimpleNamespace(get=lambda **kwargs: metric_stream),
    )

    assert _conversation_quality_metric_keys(
        project_name="hermes-galileo-ci",
        log_stream_name="github-actions-live-e2e",
    ) == {scorer_id, f"metrics/{scorer_id}"}


def test_conversation_quality_diagnostic_excludes_record_content() -> None:
    scorer_id = "0d41bb42-b42d-46d7-a0db-73ea1335f34e"
    diagnostic = _conversation_quality_diagnostic(
        {
            "input": "do-not-log-input",
            "output": "do-not-log-output",
            "metrics_batch_id": "batch-id",
            "session_batch_id": None,
            "metric_info": {
                scorer_id: {
                    "status_type": "failed",
                    "metric_key_alias": "conversation_quality",
                    "message": "do-not-log-backend-message",
                    "ems_error_code": 123,
                }
            },
            "error_message": "do-not-log-error",
        },
        metric_keys={scorer_id},
    )

    assert diagnostic == {
        "metrics_batch_id_present": True,
        "session_batch_id_present": False,
        "metric_info_entry_count": 1,
        "target_states": [{"status_type": "failed", "ems_error_code": 123}],
        "progress_message_present": False,
        "error_message_present": True,
    }
    assert "do-not-log" not in str(diagnostic)


def test_ensure_galileo_resources_uses_existing_dedicated_stream(
    monkeypatch: Any,
) -> None:
    project = SimpleNamespace(id="project-id")
    monkeypatch.setattr(live_e2e, "get_project", lambda *, name: project)
    monkeypatch.setattr(
        live_e2e,
        "get_log_stream",
        lambda *, name, project_id: SimpleNamespace(id="log-stream-id"),
    )
    monkeypatch.setattr(
        live_e2e,
        "create_log_stream",
        lambda **kwargs: pytest.fail("log stream must not be created"),
    )

    assert _ensure_galileo_resources(
        project_name="hermes-galileo-ci",
        log_stream_name="github-actions-live-e2e",
    )


def test_enable_conversation_quality_uses_server_metric(
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, object]] = []
    configured: list[str] = []
    metric_stream = SimpleNamespace(get_metrics=lambda: list(configured))
    monkeypatch.setattr(
        live_e2e,
        "MetricLogStream",
        SimpleNamespace(get=lambda **kwargs: metric_stream),
    )

    def enable_metrics(**kwargs: object) -> list[object]:
        calls.append(kwargs)
        return []

    monkeypatch.setattr(live_e2e, "enable_metrics", enable_metrics)

    assert _enable_conversation_quality(
        project_name="hermes-galileo-ci",
        log_stream_name="github-actions-live-e2e",
    )
    assert calls == [
        {
            "project_name": "hermes-galileo-ci",
            "log_stream_name": "github-actions-live-e2e",
            "metrics": [live_e2e.GalileoMetrics.conversation_quality],
        }
    ]
    assert configured == []

    configured.append("conversation_quality")
    assert _conversation_quality_is_enabled(
        project_name="hermes-galileo-ci",
        log_stream_name="github-actions-live-e2e",
    )
    assert len(calls) == 1


def test_enable_conversation_quality_preserves_existing_metric_configuration(
    monkeypatch: Any,
) -> None:
    metric_stream = SimpleNamespace(get_metrics=lambda: ["Conversation Quality", "Tool Error"])
    monkeypatch.setattr(
        live_e2e,
        "MetricLogStream",
        SimpleNamespace(get=lambda **kwargs: metric_stream),
    )
    monkeypatch.setattr(
        live_e2e,
        "enable_metrics",
        lambda **kwargs: pytest.fail("existing metrics must not be replaced"),
    )

    assert _enable_conversation_quality(
        project_name="hermes-galileo-ci",
        log_stream_name="github-actions-live-e2e",
    )


def test_enable_conversation_quality_rejects_shared_stream(
    monkeypatch: Any,
) -> None:
    metric_stream = SimpleNamespace(get_metrics=lambda: ["Tool Error"])
    monkeypatch.setattr(
        live_e2e,
        "MetricLogStream",
        SimpleNamespace(get=lambda **kwargs: metric_stream),
    )

    with pytest.raises(AssertionError, match="dedicated empty log stream"):
        _enable_conversation_quality(
            project_name="hermes-galileo-ci",
            log_stream_name="github-actions-live-e2e",
        )


def test_poll_fails_immediately_for_permanent_api_error() -> None:
    class UnauthorizedError(Exception):
        status_code = 401

    attempts = 0

    def unauthorized() -> None:
        nonlocal attempts
        attempts += 1
        raise UnauthorizedError("unauthorized")

    with pytest.raises(AssertionError, match="failed permanently"):
        _poll(unauthorized, timeout=60, interval=0)
    assert attempts == 1


def test_poll_retries_transient_api_error() -> None:
    class ServiceUnavailableError(Exception):
        status_code = 503

    attempts = 0

    def transient() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ServiceUnavailableError("unavailable")
        return True

    assert _poll(transient, timeout=1, interval=0)
    assert attempts == 2


def test_ensure_galileo_resources_creates_missing_log_stream(
    monkeypatch: Any,
) -> None:
    project = SimpleNamespace(id="project-id")
    created: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(live_e2e, "get_project", lambda *, name: project)
    monkeypatch.setattr(
        live_e2e,
        "get_log_stream",
        lambda *, name, project_id: None,
    )
    monkeypatch.setattr(
        live_e2e,
        "create_log_stream",
        lambda **kwargs: created.append(("log_stream", kwargs)),
    )

    assert _ensure_galileo_resources(
        project_name="hermes-galileo-ci",
        log_stream_name="github-actions-live-e2e",
    )
    assert created == [
        (
            "log_stream",
            {
                "name": "github-actions-live-e2e",
                "project_id": "project-id",
            },
        ),
    ]


def test_ensure_galileo_resources_requires_precreated_project(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(live_e2e, "get_project", lambda *, name: None)

    with pytest.raises(AssertionError, match="pre-create"):
        _ensure_galileo_resources(
            project_name="hermes-galileo-ci",
            log_stream_name="github-actions-live-e2e",
        )
