"""Live Galileo E2E: two Hermes turns become one readable Galileo session."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from galileo.log_streams import get_log_stream
from galileo.projects import get_project
from galileo.resources.models import (
    LogRecordsIDFilter,
    LogRecordsIDFilterOperator,
    LogRecordsTextFilter,
    LogRecordsTextFilterOperator,
)
from galileo.resources.types import Unset
from galileo.search import get_sessions, get_spans, get_traces

import hermes_galileo
from hermes_galileo import hooks

pytestmark = [pytest.mark.e2e, pytest.mark.live]

_T = TypeVar("_T")


def _records(response: Any) -> list[Any]:
    records = getattr(response, "records", [])
    return [] if isinstance(records, Unset) else list(records)


def _record_text(record: Any) -> str:
    value = record.to_dict() if hasattr(record, "to_dict") else record
    return json.dumps(value, sort_keys=True, default=str)


def _poll(
    operation: Callable[[], _T | None],
    *,
    timeout: float,
    interval: float = 5,
) -> _T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = operation()
            if result is not None:
                return result
        except AssertionError:
            raise
        except Exception as exc:
            last_error = exc
        time.sleep(interval)
    if last_error is not None:
        raise AssertionError(f"Galileo read-back did not converge: {last_error}") from last_error
    raise AssertionError("Galileo read-back did not converge before the timeout")


def _normalized(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _completed_metric_value(value: Any) -> Any | None:
    if not isinstance(value, dict):
        return value if value not in (None, "", [], {}) else None
    normalized_items = {_normalized(key): item for key, item in value.items()}
    status = _normalized(normalized_items.get("statustype", ""))
    if status not in {"success", "rollup"}:
        return None
    for key in ("value", "score", "metricvalue"):
        candidate = normalized_items.get(key)
        if candidate not in (None, "", [], {}):
            return candidate
    return None


def _conversation_quality_value(value: Any) -> Any | None:
    if isinstance(value, dict):
        normalized_items = {_normalized(key): item for key, item in value.items()}
        for key, item in normalized_items.items():
            if "conversationquality" in key:
                found = _completed_metric_value(item)
                if found is not None:
                    return found
        metric_name = normalized_items.get("name") or normalized_items.get("metricname")
        if "conversationquality" in _normalized(metric_name):
            found = _completed_metric_value(value)
            if found is not None:
                return found
        for item in value.values():
            found = _conversation_quality_value(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _conversation_quality_value(item)
            if found is not None:
                return found
    return None


@pytest.mark.timeout(600)
def test_two_turns_share_live_galileo_session_and_are_readable(
    monkeypatch: Any,
) -> None:
    if os.environ.get("HERMES_GALILEO_RUN_LIVE_E2E", "").lower() != "true":
        pytest.skip("set HERMES_GALILEO_RUN_LIVE_E2E=true to use a real Galileo account")

    api_key = os.environ.get("GALILEO_API_KEY", "").strip()
    project_name = os.environ.get("GALILEO_PROJECT", "").strip()
    log_stream_name = os.environ.get("GALILEO_LOG_STREAM", "").strip()
    hermes_source = Path(os.environ.get("HERMES_AGENT_SOURCE", "")).expanduser()
    assert api_key, "GALILEO_API_KEY is required"
    assert project_name, "GALILEO_PROJECT is required"
    assert log_stream_name, "GALILEO_LOG_STREAM is required"
    assert (hermes_source / "hermes_cli" / "plugins.py").is_file(), (
        "HERMES_AGENT_SOURCE must point to a real Hermes Agent checkout"
    )

    monkeypatch.syspath_prepend(str(hermes_source.resolve()))
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

    run_marker = (
        "hermes-galileo-live:"
        f"{os.environ.get('GITHUB_RUN_ID', time.time_ns())}:"
        f"{os.environ.get('GITHUB_RUN_ATTEMPT', 'local')}"
    )
    raw_session_id = f"hermes-galileo-ci-session:{run_marker}"
    raw_sender_id = f"hermes-galileo-ci-user:{run_marker}"
    privacy_canary = f"hg-live-secret-{_normalized(run_marker)}"
    pseudonym_secret = os.environ.get(
        "HERMES_GALILEO_PSEUDONYM_SECRET",
        "synthetic-ci-pseudonym-v1",
    )
    pseudonym = hmac.new(
        pseudonym_secret.encode(),
        raw_session_id.encode(),
        hashlib.sha256,
    ).hexdigest()
    expected_external_id = f"hermes:{pseudonym}"
    expected_pairs = [
        (
            f"{run_marker}:turn-1:input",
            f"{run_marker}:turn-1:input Authorization: Bearer {privacy_canary}",
            f"{run_marker}:turn-1:output",
        ),
        (
            f"{run_marker}:turn-2:input",
            f"{run_marker}:turn-2:input Authorization: Bearer {privacy_canary}",
            f"{run_marker}:turn-2:output",
        ),
    ]

    monkeypatch.setenv("HERMES_GALILEO_CAPTURE_CONTENT", "true")
    monkeypatch.setenv("HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY", "false")
    monkeypatch.setenv("HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END", "false")
    monkeypatch.setenv("HERMES_GALILEO_NATIVE_SESSION_TIMEOUT_MILLIS", "60000")
    monkeypatch.setenv("HERMES_GALILEO_PSEUDONYM_SECRET", pseudonym_secret)
    hooks.set_runtime(None)
    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name="hermes-galileo", source="entrypoint"),
        manager,
    )

    try:
        hermes_galileo.register(context)
        runtime = hooks.get_runtime()
        assert runtime is not None, "Hermes Galileo runtime did not initialize"

        session_payload = {
            "session_id": raw_session_id,
            "model": "synthetic-live-e2e-model",
            "provider": "synthetic",
            "platform": "github-actions",
        }
        manager.invoke_hook(
            "on_session_start",
            **session_payload,
            sender_id=raw_sender_id,
        )

        for index, (_, input_text, output_text) in enumerate(expected_pairs, start=1):
            turn = {
                **session_payload,
                "turn_id": f"{run_marker}:turn-{index}",
                "task_id": f"{run_marker}:task-{index}",
            }
            manager.invoke_hook("pre_llm_call", **turn, user_message=input_text)
            manager.invoke_hook(
                "pre_api_request",
                **turn,
                api_request_id=f"{run_marker}:api-{index}",
                api_call_count=1,
                user_message=input_text,
                request={
                    "method": "POST",
                    "body": {"messages": [{"role": "user", "content": input_text}]},
                },
            )
            manager.invoke_hook(
                "post_api_request",
                **turn,
                api_request_id=f"{run_marker}:api-{index}",
                api_call_count=1,
                response={
                    "model": "synthetic-live-e2e-model",
                    "finish_reason": "stop",
                    "assistant_message": {
                        "role": "assistant",
                        "content": output_text,
                    },
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 5,
                        "total_tokens": 10,
                    },
                },
                response_model="synthetic-live-e2e-model",
                finish_reason="stop",
                usage={
                    "input_tokens": 5,
                    "output_tokens": 5,
                    "total_tokens": 10,
                },
            )
            tool_name = "synthetic_ok" if index == 1 else "synthetic_failure"
            tool_call_id = f"{run_marker}:tool-{index}"
            manager.invoke_hook(
                "pre_tool_call",
                **turn,
                api_request_id=f"{run_marker}:api-{index}",
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args={"query": f"{run_marker}:tool-input-{index}"},
            )
            tool_result: dict[str, Any] = {
                **turn,
                "api_request_id": f"{run_marker}:api-{index}",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "args": {"query": f"{run_marker}:tool-input-{index}"},
                "duration_ms": 10 + index,
            }
            if index == 1:
                tool_result.update(
                    status="ok",
                    result={"value": f"{run_marker}:tool-output-{index}"},
                )
            else:
                tool_result.update(
                    status="error",
                    error_type="synthetic_tool_error",
                    error_message=f"redact this Bearer {privacy_canary}",
                )
            manager.invoke_hook("post_tool_call", **tool_result)
            manager.invoke_hook("post_llm_call", **turn, assistant_response=output_text)
            manager.invoke_hook(
                "on_session_end",
                **turn,
                assistant_response=output_text,
                completed=True,
            )

        assert runtime._processor.wait_until_ready(30)

        def native_session_ready() -> dict[str, Any] | None:
            snapshot = runtime.health_snapshot()
            if snapshot.get("native_session_failed", 0):
                raise AssertionError(
                    "Galileo native Session provisioning failed: "
                    f"{snapshot.get('native_session_failures', 0)} failure(s)"
                )
            ready = snapshot.get("native_session_ready", 0) >= 1
            drained = snapshot.get("native_session_deferred_spans", 0) == 0
            return snapshot if ready and drained else None

        _poll(native_session_ready, timeout=75, interval=1)
        assert hermes_galileo.force_flush() is True

        def read_records() -> tuple[Any, list[Any], list[Any]] | None:
            project = get_project(name=project_name)
            if project is None:
                return None
            log_stream = get_log_stream(
                name=log_stream_name,
                project_id=project.id,
            )
            if log_stream is None:
                return None
            sessions = _records(
                get_sessions(
                    project.id,
                    log_stream_id=log_stream.id,
                    filters=[
                        LogRecordsTextFilter(
                            column_id="external_id",
                            operator=LogRecordsTextFilterOperator.EQ,
                            value=expected_external_id,
                        )
                    ],
                    limit=100,
                )
            )
            if not sessions:
                return None
            assert len(sessions) == 1, (
                "one Hermes session external ID resolved to multiple Galileo Sessions"
            )
            session = sessions[0]
            traces = [
                trace
                for trace in _records(
                    get_traces(
                        project.id,
                        log_stream_id=log_stream.id,
                        filters=[
                            LogRecordsIDFilter(
                                column_id="session_id",
                                operator=LogRecordsIDFilterOperator.EQ,
                                value=session.id,
                            )
                        ],
                        limit=100,
                    )
                )
                if run_marker in _record_text(trace)
            ]
            if len(traces) < 2:
                return None
            assert len(traces) == 2, "the run produced more than two Galileo traces"
            trace_ids = {trace.trace_id for trace in traces}
            span_records = _records(
                get_spans(
                    project.id,
                    log_stream_id=log_stream.id,
                    filters=[
                        LogRecordsIDFilter(
                            column_id="trace_id",
                            operator=LogRecordsIDFilterOperator.ONE_OF,
                            value=sorted(trace_ids),
                        )
                    ],
                    limit=100,
                )
            )
            root_spans = [span for span in span_records if span.type_ == "agent"]
            child_spans = [span for span in span_records if span.type_ in {"llm", "tool"}]
            if len(root_spans) < 2 or len(child_spans) < 4:
                return None
            assert len(span_records) == 6, "the run produced unexpected Galileo spans"
            assert len(root_spans) == 2, "the run produced unexpected Agent root spans"
            assert len(child_spans) == 4, "the run produced unexpected child spans"
            return session, traces, span_records

        session, traces, span_records = _poll(read_records, timeout=180)
        assert session.external_id == expected_external_id
        assert raw_session_id not in _record_text(session)
        assert len({trace.trace_id for trace in traces}) == 2
        assert len({trace.id for trace in traces}) == 2

        trace_texts = [_record_text(trace) for trace in traces]
        for expected_input, _, expected_output in expected_pairs:
            matching = [
                text for text in trace_texts if expected_input in text and expected_output in text
            ]
            assert len(matching) == 1
        all_record_text = "\n".join(
            [_record_text(session), *trace_texts, *map(_record_text, span_records)]
        )
        assert raw_session_id not in all_record_text
        assert raw_sender_id not in all_record_text
        assert privacy_canary not in all_record_text

        trace_record_ids = {trace.trace_id: trace.id for trace in traces}
        root_spans = [span for span in span_records if span.type_ == "agent"]
        child_spans = [span for span in span_records if span.type_ in {"llm", "tool"}]
        root_span_ids = {span.trace_id: span.id for span in root_spans}
        assert all(span.session_id == session.id for span in span_records)
        assert all(span.trace_id in trace_record_ids for span in span_records)
        assert set(root_span_ids) == set(trace_record_ids)
        assert all(span.parent_id == root_span_ids[span.trace_id] for span in child_spans)

        for trace_id in trace_record_ids:
            trace_spans = [span for span in child_spans if span.trace_id == trace_id]
            assert {span.type_ for span in trace_spans} == {"llm", "tool"}

        llm_spans = [span for span in child_spans if span.type_ == "llm"]
        assert len(llm_spans) == 2
        for span in llm_spans:
            assert span.model == "synthetic-live-e2e-model"
            assert _normalized(span.finish_reason) == "stop"
            metrics = span.metrics.to_dict()
            assert metrics["num_input_tokens"] == 5
            assert metrics["num_output_tokens"] == 5
            assert metrics["num_total_tokens"] == 10

        tool_spans = [span for span in child_spans if span.type_ == "tool"]
        assert len(tool_spans) == 2
        assert len([span for span in tool_spans if "synthetic_ok" in str(span.name)]) == 1
        failed_tools = [span for span in tool_spans if "synthetic_failure" in str(span.name)]
        assert len(failed_tools) == 1
        # Galileo Search's numeric status_code is not the OTLP Span Status.
        # The wire-level E2E pins ERROR status and error.type before ingestion.

        require_quality = (
            os.environ.get(
                "GALILEO_E2E_REQUIRE_CONVERSATION_QUALITY",
                "true",
            ).lower()
            == "true"
        )
        if require_quality:

            def read_conversation_quality() -> Any | None:
                project = get_project(name=project_name)
                if project is None:
                    return None
                log_stream = get_log_stream(
                    name=log_stream_name,
                    project_id=project.id,
                )
                if log_stream is None:
                    return None
                refreshed_sessions = _records(
                    get_sessions(
                        project.id,
                        log_stream_id=log_stream.id,
                        filters=[
                            LogRecordsTextFilter(
                                column_id="external_id",
                                operator=LogRecordsTextFilterOperator.EQ,
                                value=expected_external_id,
                            )
                        ],
                        limit=100,
                    )
                )
                if not refreshed_sessions:
                    return None
                assert len(refreshed_sessions) == 1
                refreshed_session = refreshed_sessions[0]
                assert refreshed_session.id == session.id
                return _conversation_quality_value(refreshed_session.to_dict())

            assert _poll(read_conversation_quality, timeout=300) is not None
    finally:
        manager.invoke_hook("on_session_finalize", session_id=raw_session_id)
        hermes_galileo._shutdown_runtime()
