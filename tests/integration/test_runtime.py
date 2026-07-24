"""Integration tests for Hermes event correlation and OTel span construction."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from hermes_galileo import runtime as runtime_module
from hermes_galileo.config import Settings
from hermes_galileo.privacy import CONTENT_DISABLED
from hermes_galileo.runtime import RuntimeInitializationError, TelemetryRuntime


def _settings(**overrides: Any) -> Settings:
    base = Settings.from_env(
        {
            "GALILEO_API_KEY": "test-galileo-key",
            "GALILEO_PROJECT": "test-project",
            "GALILEO_LOG_STREAM": "test-stream",
            "HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END": "false",
        }
    )
    return replace(base, **overrides)


@pytest.fixture
def telemetry() -> tuple[TelemetryRuntime, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    runtime = TelemetryRuntime(
        _settings(),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
    )
    yield runtime, exporter
    runtime.shutdown()


def _base(session: str = "session-1", turn: str = "turn-1") -> dict[str, Any]:
    return {
        "session_id": session,
        "turn_id": turn,
        "task_id": f"task-{turn}",
        "model": "gpt-4.1-mini",
        "provider": "openai",
        "platform": "cli",
        "telemetry_schema_version": "hermes.observer.v1",
    }


def _by_name(exporter: InMemorySpanExporter) -> dict[str, Any]:
    return {span.name: span for span in exporter.get_finished_spans()}


@pytest.mark.integration
def test_official_sdk_connects_off_thread_and_replays_startup_buffer(
    monkeypatch: Any,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    received: list[Any] = []

    class FakeGalileoProcessor:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs == {
                "project": "test-project",
                "logstream": "test-stream",
                "timeout": 10.0,
            }
            entered.set()
            release.wait(timeout=2)

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            return None

        def on_end(self, span: Any) -> None:
            received.append(span)

        def force_flush(self, timeout_millis: int) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_module,
        "GalileoSpanProcessor",
        FakeGalileoProcessor,
    )

    started = time.monotonic()
    runtime = TelemetryRuntime(_settings())
    elapsed = time.monotonic() - started
    assert entered.wait(timeout=1)
    assert elapsed < 0.1

    base = _base()
    runtime.on_pre_llm_call({**base, "user_message": "captured during startup"})
    runtime.on_session_end({**base, "completed": True})
    assert runtime.health_snapshot()["buffered_spans"] == 1

    release.set()
    assert runtime._processor.wait_until_ready(1)
    assert len(received) == 1
    assert received[0].name == "invoke_agent Hermes Agent"
    assert received[0].attributes["galileo.project.name"] == "test-project"
    assert runtime.health_snapshot()["exporter_ready"] is True
    runtime.shutdown()


@pytest.mark.integration
def test_deferred_force_flush_rechecks_state_after_operation_lock(
    monkeypatch: Any,
) -> None:
    force_calls = 0

    class FakeGalileoProcessor:
        def __init__(self, **kwargs: Any) -> None:
            return None

        def force_flush(self, timeout_millis: int) -> bool:
            nonlocal force_calls
            force_calls += 1
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(runtime_module, "GalileoSpanProcessor", FakeGalileoProcessor)
    processor = runtime_module._DeferredGalileoSpanProcessor(_settings())
    assert processor.wait_until_ready(1)

    class ShutdownWinsLock:
        def acquire(self, *, timeout: float) -> bool:
            assert timeout > 0
            with processor._lock:
                processor._state = "stopping"
            return True

        def release(self) -> None:
            return None

    processor._operation_lock = ShutdownWinsLock()
    assert processor.force_flush(100) is False
    assert force_calls == 0

    # Restore a real operation lock and state so this isolated processor can
    # clean up normally after simulating the race.
    processor._operation_lock = threading.Lock()
    with processor._lock:
        processor._state = "ready"
    processor.shutdown()


@pytest.mark.integration
def test_shutdown_waits_bounded_time_for_inflight_sdk_connection(
    monkeypatch: Any,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    received: list[Any] = []

    class SlowGalileoProcessor:
        def __init__(self, **kwargs: Any) -> None:
            entered.set()
            release.wait(timeout=2)

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            return None

        def on_end(self, span: Any) -> None:
            received.append(span)

        def force_flush(self, timeout_millis: int) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_module,
        "GalileoSpanProcessor",
        SlowGalileoProcessor,
    )
    runtime = TelemetryRuntime(_settings(flush_timeout_millis=500))
    assert entered.wait(timeout=1)
    base = _base()
    runtime.on_pre_llm_call({**base, "user_message": "short-lived command"})
    runtime.on_session_end({**base, "completed": True})

    timer = threading.Timer(0.05, release.set)
    timer.start()
    runtime.shutdown()
    timer.join(timeout=1)

    assert [span.name for span in received] == ["invoke_agent Hermes Agent"]


@pytest.mark.integration
def test_shutdown_reports_connector_that_outlives_deadline(
    monkeypatch: Any,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingGalileoProcessor:
        def __init__(self, **kwargs: Any) -> None:
            entered.set()
            release.wait(timeout=2)

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(runtime_module, "GalileoSpanProcessor", BlockingGalileoProcessor)
    runtime = TelemetryRuntime(_settings(flush_timeout_millis=100))
    assert entered.wait(timeout=1)

    started = time.monotonic()
    runtime.shutdown()
    assert time.monotonic() - started < 0.5
    assert runtime.health_snapshot()["connector_cleanup_deferred"] is True

    release.set()
    deadline = time.monotonic() + 1
    while runtime.health_snapshot()["connector_cleanup_deferred"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)


@pytest.mark.integration
def test_sdk_initialization_failure_is_retried_without_losing_spans(
    monkeypatch: Any,
) -> None:
    attempts = 0
    received: list[Any] = []

    class EventuallyConnectedProcessor:
        def __init__(self, **kwargs: Any) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("temporary outage")

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            return None

        def on_end(self, span: Any) -> None:
            received.append(span)

        def force_flush(self, timeout_millis: int) -> bool:
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_module,
        "GalileoSpanProcessor",
        EventuallyConnectedProcessor,
    )
    monkeypatch.setattr(
        runtime_module._DeferredGalileoSpanProcessor,
        "_RETRY_DELAYS_SECONDS",
        (0.01,),
    )
    runtime = TelemetryRuntime(_settings())
    base = _base()
    runtime.on_pre_llm_call({**base, "user_message": "buffer across retry"})
    runtime.on_session_end({**base, "completed": True})

    assert runtime._processor.wait_until_ready(1)
    assert attempts == 2
    assert [span.name for span in received] == ["invoke_agent Hermes Agent"]
    assert runtime.health_snapshot()["connection_attempts"] == 2
    runtime.shutdown()


@pytest.mark.integration
def test_exporter_is_not_ready_until_startup_replay_finishes(
    monkeypatch: Any,
) -> None:
    constructor_release = threading.Event()
    replay_started = threading.Event()
    replay_release = threading.Event()
    received: list[Any] = []
    flush_calls = 0

    class ReplayBlockingProcessor:
        def __init__(self, **kwargs: Any) -> None:
            constructor_release.wait(timeout=2)

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            return None

        def on_end(self, span: Any) -> None:
            replay_started.set()
            replay_release.wait(timeout=2)
            received.append(span)

        def force_flush(self, timeout_millis: int) -> bool:
            nonlocal flush_calls
            flush_calls += 1
            return True

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(
        runtime_module,
        "GalileoSpanProcessor",
        ReplayBlockingProcessor,
    )
    runtime = TelemetryRuntime(_settings())
    base = _base()
    runtime.on_pre_llm_call({**base, "user_message": "buffer before connection"})
    runtime.on_session_end({**base, "completed": True})

    constructor_release.set()
    assert replay_started.wait(timeout=1)
    assert runtime._processor.wait_until_ready(0.02) is False
    assert runtime._processor.force_flush(20) is False
    assert flush_calls == 0

    replay_release.set()
    assert runtime._processor.wait_until_ready(1) is True
    assert [span.name for span in received] == ["invoke_agent Hermes Agent"]
    runtime.shutdown()


@pytest.mark.integration
def test_permanent_sdk_http_error_stops_connection_retries(
    monkeypatch: Any,
) -> None:
    attempts = 0

    class UnauthorizedError(Exception):
        status_code = 401

    class UnauthorizedProcessor:
        def __init__(self, **kwargs: Any) -> None:
            nonlocal attempts
            attempts += 1
            raise UnauthorizedError("invalid API key")

    monkeypatch.setattr(runtime_module, "GalileoSpanProcessor", UnauthorizedProcessor)
    monkeypatch.setattr(
        runtime_module._DeferredGalileoSpanProcessor,
        "_RETRY_DELAYS_SECONDS",
        (0.01,),
    )
    runtime = TelemetryRuntime(_settings())

    assert runtime._processor.wait_until_ready(0.2) is False
    time.sleep(0.03)
    health = runtime.health_snapshot()
    assert attempts == 1
    assert health["exporter_state"] == "failed"
    assert health["last_connection_error_type"] == "UnauthorizedError"
    assert health["last_connection_error_retryable"] is False
    assert health["retry_stopped_reason"] == "permanent_http_401"

    base = _base()
    runtime.on_pre_llm_call({**base, "user_message": "agent remains fail-open"})
    runtime.on_session_end({**base, "completed": True})
    assert runtime.health_snapshot()["dropped_spans"] == 1
    runtime.shutdown()


@pytest.mark.integration
def test_conflicting_galileo_sdk_singleton_fails_without_reset(
    monkeypatch: Any,
) -> None:
    class ExistingSecret:
        def get_secret_value(self) -> str:
            return "test-galileo-key"

    class ExistingConfig:
        api_key = ExistingSecret()
        console_url = "https://console.example.test"
        api_url = "https://api.example.test"

    class FakeGalileoConfig:
        _instance = ExistingConfig()

    processor_created = False

    class ProcessorThatMustNotBeCreated:
        def __init__(self, **kwargs: Any) -> None:
            nonlocal processor_created
            processor_created = True

    monkeypatch.setattr(runtime_module, "GalileoPythonConfig", FakeGalileoConfig)
    monkeypatch.setattr(
        runtime_module,
        "GalileoSpanProcessor",
        ProcessorThatMustNotBeCreated,
    )
    runtime = TelemetryRuntime(_settings())

    assert runtime._processor.wait_until_ready(0.2) is False
    health = runtime.health_snapshot()
    assert processor_created is False
    assert FakeGalileoConfig._instance is not None
    assert health["exporter_state"] == "failed"
    assert health["retry_stopped_reason"] == "sdk_configuration_conflict"
    runtime.shutdown()


@pytest.mark.integration
def test_post_construction_sdk_routing_validation_closes_processor(
    monkeypatch: Any,
) -> None:
    class ExistingSecret:
        def get_secret_value(self) -> str:
            return "test-galileo-key"

    class WrongRoute:
        api_key = ExistingSecret()
        console_url = "https://console.example.test"
        api_url = "https://wrong-api.example.test"

    class FakeGalileoConfig:
        _instance: Any | None = None

    shutdown_calls = 0

    class RacingProcessor:
        def __init__(self, **kwargs: Any) -> None:
            FakeGalileoConfig._instance = WrongRoute()

        def shutdown(self) -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1

    monkeypatch.setattr(runtime_module, "GalileoPythonConfig", FakeGalileoConfig)
    monkeypatch.setattr(runtime_module, "GalileoSpanProcessor", RacingProcessor)
    runtime = TelemetryRuntime(
        _settings(
            console_url="https://console.example.test",
            api_url="https://expected-api.example.test",
        )
    )

    assert runtime._processor.wait_until_ready(0.2) is False
    health = runtime.health_snapshot()
    assert shutdown_calls == 1
    assert health["exporter_state"] == "failed"
    assert health["retry_stopped_reason"] == "sdk_configuration_conflict"
    runtime.shutdown()


@pytest.mark.integration
def test_owned_provider_shutdown_uses_one_sdk_shutdown_path(
    monkeypatch: Any,
) -> None:
    shutdown_calls = 0
    flush_calls = 0
    constructor_kwargs: dict[str, Any] = {}

    class CountingProcessor:
        def __init__(self, **kwargs: Any) -> None:
            constructor_kwargs.update(kwargs)

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            return None

        def on_end(self, span: Any) -> None:
            return None

        def force_flush(self, timeout_millis: int) -> bool:
            nonlocal flush_calls
            flush_calls += 1
            return True

        def shutdown(self) -> None:
            nonlocal shutdown_calls
            shutdown_calls += 1

    monkeypatch.setattr(runtime_module, "GalileoSpanProcessor", CountingProcessor)
    runtime = TelemetryRuntime(_settings(flush_timeout_millis=100))
    assert runtime._processor.wait_until_ready(1)

    runtime.shutdown()
    runtime.shutdown()

    assert constructor_kwargs["timeout"] == 0.1
    assert flush_calls == 0
    assert shutdown_calls == 1


@pytest.mark.integration
def test_blocking_sdk_shutdown_continues_after_runtime_deadline(
    monkeypatch: Any,
) -> None:
    shutdown_started = threading.Event()
    shutdown_release = threading.Event()
    shutdown_finished = threading.Event()

    class BlockingShutdownProcessor:
        def __init__(self, **kwargs: Any) -> None:
            return None

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            return None

        def on_end(self, span: Any) -> None:
            return None

        def force_flush(self, timeout_millis: int) -> bool:
            return True

        def shutdown(self) -> None:
            shutdown_started.set()
            shutdown_release.wait(timeout=2)
            shutdown_finished.set()

    monkeypatch.setattr(
        runtime_module,
        "GalileoSpanProcessor",
        BlockingShutdownProcessor,
    )
    runtime = TelemetryRuntime(_settings(flush_timeout_millis=100))
    assert runtime._processor.wait_until_ready(1)

    started = time.monotonic()
    runtime.shutdown()
    elapsed = time.monotonic() - started

    assert shutdown_started.is_set()
    assert elapsed < 0.5
    assert runtime.health_snapshot()["delegate_cleanup_deferred"] is True

    shutdown_release.set()
    assert shutdown_finished.wait(timeout=1)
    deadline = time.monotonic() + 1
    while runtime.health_snapshot()["delegate_cleanup_deferred"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)


@pytest.mark.integration
def test_complete_turn_has_one_trace_and_correct_parentage(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    runtime.settings = replace(runtime.settings, capture_content=True)
    base = _base()

    runtime.on_session_start({**base, "sender_id": "customer@example.test"})
    runtime.on_pre_llm_call({**base, "user_message": "find the runbook"})
    runtime.on_pre_api_request(
        {
            **base,
            "api_request_id": "request-1",
            "api_call_count": 1,
            "user_message": "find the runbook",
            "base_url": "https://api.openai.com/v1/?token=secret",
            "request": {
                "method": "POST",
                "body": {
                    "messages": [{"role": "user", "content": "find the runbook"}],
                    "authorization": "Bearer provider-secret-value",
                },
            },
        }
    )
    runtime.on_post_api_request(
        {
            **base,
            "api_request_id": "request-1",
            "api_call_count": 1,
            "response": {"content": "Calling search"},
            "response_model": "gpt-4.1-mini-2026-01-01",
            "finish_reason": "tool_calls",
            "api_duration": 0.125,
            "usage": {
                "input_tokens": 21,
                "prompt_tokens": 30,
                "cache_read_tokens": 9,
                "output_tokens": 7,
                "total_tokens": 37,
                "cost": 0.004,
            },
        }
    )
    runtime.on_pre_tool_call(
        {
            **base,
            "api_request_id": "request-1",
            "tool_call_id": "call-1",
            "tool_name": "search",
            "args": {"query": "runbook"},
        }
    )
    runtime.on_post_tool_call(
        {
            **base,
            "api_request_id": "request-1",
            "tool_call_id": "call-1",
            "tool_name": "search",
            "args": {"query": "runbook"},
            "result": {"matches": 2},
            "duration_ms": 12,
            "status": "ok",
        }
    )
    runtime.on_post_llm_call({**base, "assistant_response": "Two matches found."})
    runtime.on_session_end({**base, "completed": True})

    spans = _by_name(exporter)
    assert set(spans) == {
        "invoke_agent Hermes Agent",
        "chat gpt-4.1-mini",
        "execute_tool search",
    }
    root = spans["invoke_agent Hermes Agent"]
    api = spans["chat gpt-4.1-mini"]
    tool = spans["execute_tool search"]

    assert root.parent is None
    assert api.parent.span_id == root.context.span_id
    assert tool.parent.span_id == root.context.span_id
    assert {span.context.trace_id for span in spans.values()} == {root.context.trace_id}
    assert root.kind is SpanKind.INTERNAL
    assert api.kind is SpanKind.CLIENT
    assert tool.kind is SpanKind.INTERNAL

    input_messages = json.loads(api.attributes["gen_ai.input.messages"])
    assert input_messages == [{"content": "find the runbook", "role": "user"}]
    assert "provider-secret-value" not in api.attributes["input.value"]
    assert api.attributes["server.address"] == "https://api.openai.com/v1/"
    assert api.attributes["gen_ai.usage.input_tokens"] == 30
    assert api.attributes["gen_ai.usage.output_tokens"] == 7
    assert api.attributes["gen_ai.usage.cache_read.input_tokens"] == 9
    assert api.attributes["hermes.usage.cost"] == 0.004
    assert api.attributes["hermes.api.attempt"] == 1
    assert "gen_ai.usage.input_tokens" not in root.attributes
    assert root.attributes["hermes.turn.api_call_count"] == 1
    assert root.attributes["hermes.turn.tool_count"] == 1
    assert root.attributes["hermes.turn.final_status"] == "completed"
    assert root.attributes["user.id"].startswith("hmac-sha256:")
    assert root.status.status_code is StatusCode.UNSET


@pytest.mark.integration
def test_content_opt_in_does_not_capture_conversation_history_without_separate_opt_in(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    runtime.settings = replace(
        runtime.settings,
        capture_content=True,
        capture_conversation_history=False,
    )
    base = _base()

    runtime.on_pre_llm_call({**base, "user_message": "current question"})
    runtime.on_pre_api_request(
        {
            **base,
            "api_request_id": "history-boundary",
            "user_message": "current question",
            "request": {
                "body": {
                    "messages": [
                        {"role": "user", "content": "past-turn-history-secret"},
                        {"role": "assistant", "content": "past answer"},
                        {"role": "user", "content": "current question"},
                    ]
                }
            },
        }
    )
    runtime.on_post_api_request(
        {
            **base,
            "api_request_id": "history-boundary",
            "response": {"content": "current answer"},
        }
    )
    runtime.on_session_end({**base, "completed": True})

    api = _by_name(exporter)["chat gpt-4.1-mini"]
    assert json.loads(api.attributes["gen_ai.input.messages"]) == [
        {"role": "user", "content": "current question"}
    ]
    assert api.attributes["input.value"] == "current question"
    assert "past-turn-history-secret" not in str(api.attributes)


@pytest.mark.integration
def test_retry_attempts_share_logical_request_id_and_have_distinct_ordinals(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    base = _base()
    request = {
        **base,
        "api_request_id": "turn-1:api:1",
        "api_call_count": 1,
        "request": {"body": {"messages": [{"role": "user", "content": "retry me"}]}},
    }

    runtime.on_pre_llm_call({**base, "user_message": "retry me"})
    runtime.on_pre_api_request(request)
    runtime.on_api_request_error(
        {
            **base,
            "api_request_id": "turn-1:api:1",
            "api_call_count": 1,
            "retry_count": 0,
            "max_retries": 3,
            "retryable": True,
            "status_code": 503,
            "error": {"type": "ServiceUnavailable", "message": "retrying"},
        }
    )
    runtime.on_pre_api_request(request)
    runtime.on_post_api_request(
        {
            **base,
            "api_request_id": "turn-1:api:1",
            "api_call_count": 1,
            "response": {"content": "success"},
            "usage": {"prompt_tokens": 4, "output_tokens": 2},
        }
    )
    runtime.on_session_end({**base, "completed": True})

    finished = exporter.get_finished_spans()
    attempts = [span for span in finished if span.name == "chat gpt-4.1-mini"]
    root = next(span for span in finished if span.name == "invoke_agent Hermes Agent")

    assert len(attempts) == 2
    assert [span.attributes["hermes.api.attempt"] for span in attempts] == [1, 2]
    assert {span.attributes["hermes.api.request_id"] for span in attempts} == {"turn-1:api:1"}
    assert attempts[0].attributes["hermes.retry.count"] == 0
    assert attempts[0].status.status_code is StatusCode.ERROR
    assert attempts[1].status.status_code is StatusCode.UNSET
    assert attempts[1].attributes["gen_ai.usage.total_tokens"] == 6
    assert root.attributes["hermes.turn.api_call_count"] == 1


@pytest.mark.integration
def test_content_is_private_by_default_and_errors_close_children(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    base = _base()
    runtime.on_pre_llm_call({**base, "user_message": "private prompt"})
    runtime.on_pre_api_request(
        {
            **base,
            "api_request_id": "request-error",
            "request": {"body": {"messages": [{"role": "user", "content": "secret"}]}},
        }
    )
    runtime.on_api_request_error(
        {
            **base,
            "api_request_id": "request-error",
            "status_code": 429,
            "retry_count": 1,
            "max_retries": 3,
            "retryable": True,
            "error": {
                "type": "RateLimitError",
                "message": "quota exceeded for Bearer private-token-12345",
            },
        }
    )
    runtime.on_pre_tool_call(
        {
            **base,
            "tool_call_id": "blocked-1",
            "tool_name": "terminal",
            "args": {"command": "unsafe command"},
        }
    )
    runtime.on_post_tool_call(
        {
            **base,
            "tool_call_id": "blocked-1",
            "tool_name": "terminal",
            "status": "blocked",
            "error_type": "policy_blocked",
            "error_message": "approval denied",
        }
    )
    runtime.on_session_end({**base, "completed": False, "reason": "failed"})

    spans = _by_name(exporter)
    api = spans["chat gpt-4.1-mini"]
    tool = spans["execute_tool terminal"]
    root = spans["invoke_agent Hermes Agent"]

    assert CONTENT_DISABLED in api.attributes["gen_ai.input.messages"]
    assert "private prompt" not in root.attributes["input.value"]
    assert api.status.status_code is StatusCode.ERROR
    assert "private-token-12345" not in (api.status.description or "")
    assert "private-token-12345" not in str(api.events)
    assert api.attributes["error.type"] == "RateLimitError"
    assert api.attributes["http.response.status_code"] == 429
    assert tool.status.status_code is StatusCode.ERROR
    assert tool.attributes["error.type"] == "policy_blocked"
    assert root.status.status_code is StatusCode.ERROR
    assert root.attributes["hermes.turn.error_count"] == 2
    assert runtime.health_snapshot()["inflight_turns"] == 0


@pytest.mark.integration
def test_opted_in_error_event_is_redacted_before_span_storage(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    runtime.settings = replace(runtime.settings, capture_content=True)
    base = _base()
    runtime.on_pre_llm_call({**base, "user_message": "trigger"})
    runtime.on_api_request_error(
        {
            **base,
            "api_request_id": "failed-without-start",
            "error": {
                "type": "AuthenticationError",
                "message": "Rejected Bearer secret-access-token-123",
            },
        }
    )
    runtime.on_session_end({**base, "completed": False})

    api = _by_name(exporter)["chat gpt-4.1-mini"]
    assert api.status.status_code is StatusCode.ERROR
    assert api.status.description == "AuthenticationError"
    assert len(api.events) == 1
    assert api.events[0].attributes["exception.message"] == "Rejected Bearer [REDACTED]"


@pytest.mark.integration
def test_missing_start_events_are_synthesized(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    base = _base()

    runtime.on_post_api_request(
        {
            **base,
            "api_request_id": "late-api",
            "response": {"content": "answer"},
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }
    )
    runtime.on_post_tool_call(
        {
            **base,
            "tool_call_id": "late-tool",
            "tool_name": "lookup",
            "args": {"query": "status"},
            "result": {"status": "ok"},
            "status": "ok",
        }
    )
    runtime.on_post_llm_call({**base, "assistant_response": "answer"})
    runtime.on_session_end({**base, "completed": True})

    spans = _by_name(exporter)
    root = spans["invoke_agent Hermes Agent"]
    api = spans["chat gpt-4.1-mini"]
    tool = spans["execute_tool lookup"]
    assert root.attributes["hermes.turn.synthesized"] is True
    assert api.attributes["hermes.span.synthesized"] is True
    assert api.attributes["input.value"] == CONTENT_DISABLED
    assert api.attributes["input.mime_type"] == "text/plain"
    assert api.attributes["openinference.span.kind"] == "LLM"
    assert api.attributes["gen_ai.usage.total_tokens"] == 5
    assert tool.attributes["hermes.span.synthesized"] is True
    assert tool.attributes["input.value"] == CONTENT_DISABLED
    assert tool.attributes["input.mime_type"] == "text/plain"
    assert tool.attributes["openinference.span.kind"] == "TOOL"


@pytest.mark.integration
def test_real_hermes_payload_enriches_agent_provider_from_api_event(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    # Canonical Hermes pre_llm_call has model/platform but no provider.
    pre_llm = _base()
    pre_llm.pop("provider")
    runtime.on_pre_llm_call({**pre_llm, "user_message": "hello"})
    runtime.on_pre_api_request(
        {
            **pre_llm,
            "provider": "openai",
            "api_request_id": "provider-discovery",
            "request": {"body": {"messages": [{"role": "user", "content": "hello"}]}},
        }
    )
    runtime.on_post_api_request(
        {
            **pre_llm,
            "provider": "openai",
            "api_request_id": "provider-discovery",
            "response": {"content": "hi"},
        }
    )
    runtime.on_session_end({**pre_llm, "completed": True})

    root = _by_name(exporter)["invoke_agent Hermes Agent"]
    assert root.attributes["gen_ai.provider.name"] == "openai"
    assert root.attributes["hermes.platform"] == "cli"


@pytest.mark.integration
def test_explicit_session_scope_prevents_cross_session_request_id_collision(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    first = _base("session-a", "turn-a")
    second = _base("session-b", "turn-b")

    runtime.on_pre_llm_call({**first, "user_message": "first"})
    runtime.on_pre_api_request(
        {
            **first,
            "api_request_id": "shared-request-id",
            "request": {"body": {"messages": [{"role": "user", "content": "first"}]}},
        }
    )

    # Session B has no start hooks and reuses A's request ID. Its post event
    # must synthesize a separate trace and child rather than closing A's child.
    runtime.on_post_api_request(
        {
            **second,
            "api_request_id": "shared-request-id",
            "response": {"content": "second response"},
        }
    )
    runtime.on_session_end({**second, "completed": True})
    runtime.on_post_api_request(
        {
            **first,
            "api_request_id": "shared-request-id",
            "response": {"content": "first response"},
        }
    )
    runtime.on_session_end({**first, "completed": True})

    spans = exporter.get_finished_spans()
    roots = {
        span.attributes["hermes.session.id"]: span
        for span in spans
        if span.name == "invoke_agent Hermes Agent"
    }
    api_spans = [span for span in spans if span.name == "chat gpt-4.1-mini"]

    assert set(roots) == {"session-a", "session-b"}
    assert len(api_spans) == 2
    assert roots["session-a"].context.trace_id != roots["session-b"].context.trace_id
    children_by_parent = {span.parent.span_id: span for span in api_spans}
    assert set(children_by_parent) == {
        roots["session-a"].context.span_id,
        roots["session-b"].context.span_id,
    }
    assert (
        children_by_parent[roots["session-b"].context.span_id].attributes["hermes.span.synthesized"]
        is True
    )


@pytest.mark.integration
def test_subagent_trace_is_nested_under_parent_delegation(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    parent = _base("parent-session", "parent-turn")
    child = _base("child-session", "child-turn")

    runtime.on_pre_llm_call({**parent, "user_message": "delegate research"})
    runtime.on_subagent_start(
        {
            "parent_session_id": "parent-session",
            "parent_turn_id": "parent-turn",
            "child_session_id": "child-session",
            "child_subagent_id": "child-1",
            "child_role": "researcher",
            "child_goal": "inspect SDK",
        }
    )
    runtime.on_pre_llm_call(
        {**child, "agent_name": "Hermes Subagent", "user_message": "inspect SDK"}
    )
    runtime.on_post_llm_call({**child, "assistant_response": "SDK inspected"})
    runtime.on_session_end({**child, "completed": True})
    runtime.on_subagent_stop(
        {
            "parent_session_id": "parent-session",
            "child_session_id": "child-session",
            "child_role": "researcher",
            "child_status": "completed",
            "child_summary": "SDK inspected",
        }
    )
    runtime.on_post_llm_call({**parent, "assistant_response": "done"})
    runtime.on_session_end({**parent, "completed": True})

    spans = _by_name(exporter)
    parent_root = spans["invoke_agent Hermes Agent"]
    delegation = spans["invoke_agent researcher"]
    child_root = spans["invoke_agent Hermes Subagent"]

    assert delegation.parent.span_id == parent_root.context.span_id
    assert child_root.parent.span_id == delegation.context.span_id
    assert child_root.context.trace_id == parent_root.context.trace_id
    assert delegation.attributes["hermes.subagent.child_session_id"] == "child-session"


@pytest.mark.integration
def test_approval_uses_turn_id_when_gateway_session_id_is_absent(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    first = _base("gateway-session-a", "turn-a")
    second = _base("gateway-session-b", "turn-b")
    runtime.on_pre_llm_call({**first, "user_message": "first"})
    runtime.on_pre_llm_call({**second, "user_message": "second"})
    runtime.on_pre_tool_call(
        {
            **first,
            "tool_call_id": "terminal-a",
            "tool_name": "terminal",
            "args": {"command": "dangerous"},
        }
    )

    approval = {
        "turn_id": "turn-a",
        "tool_call_id": "terminal-a",
        "session_key": "agent:main:telegram:dm:42",
        "pattern_key": "dangerous-command",
        "command": "dangerous",
        "surface": "gateway",
    }
    runtime.on_pre_approval_request(approval)
    runtime.on_post_approval_response({**approval, "choice": "deny"})
    runtime.on_post_tool_call(
        {
            **first,
            "tool_call_id": "terminal-a",
            "tool_name": "terminal",
            "status": "blocked",
        }
    )
    runtime.on_session_end({**first, "completed": True})
    runtime.on_session_end({**second, "completed": True})

    spans = exporter.get_finished_spans()
    approval_span = next(span for span in spans if span.name == "approval_request")
    tool_span = next(span for span in spans if span.name == "execute_tool terminal")
    roots = {
        span.attributes["hermes.turn.id"]: span
        for span in spans
        if span.name == "invoke_agent Hermes Agent"
    }

    assert approval_span.parent.span_id == tool_span.context.span_id
    assert approval_span.context.trace_id == roots["turn-a"].context.trace_id
    assert approval_span.context.trace_id != roots["turn-b"].context.trace_id


@pytest.mark.integration
def test_parallel_identical_approvals_correlate_by_tool_call_id(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    base = _base()
    runtime.on_pre_llm_call({**base, "user_message": "run two commands"})

    for tool_call_id in ("terminal-a", "terminal-b"):
        runtime.on_pre_tool_call(
            {
                **base,
                "tool_call_id": tool_call_id,
                "tool_name": "terminal",
                "args": {"command": "same-command"},
            }
        )

    common_approval = {
        **base,
        "session_key": "agent:main:cli",
        "pattern_key": "same-pattern",
        "command": "same-command",
        "surface": "cli",
    }
    runtime.on_pre_approval_request({**common_approval, "tool_call_id": "terminal-a"})
    runtime.on_pre_approval_request({**common_approval, "tool_call_id": "terminal-b"})
    runtime.on_post_approval_response(
        {**common_approval, "tool_call_id": "terminal-b", "choice": "allow"}
    )
    runtime.on_post_approval_response(
        {**common_approval, "tool_call_id": "terminal-a", "choice": "deny"}
    )

    for tool_call_id in ("terminal-a", "terminal-b"):
        runtime.on_post_tool_call(
            {
                **base,
                "tool_call_id": tool_call_id,
                "tool_name": "terminal",
                "status": "ok",
            }
        )
    runtime.on_session_end({**base, "completed": True})

    spans = exporter.get_finished_spans()
    tools = {
        span.attributes["gen_ai.tool.call.id"]: span
        for span in spans
        if span.name == "execute_tool terminal"
    }
    approvals = [span for span in spans if span.name == "approval_request"]
    choices_by_parent = {
        span.parent.span_id: span.attributes["hermes.approval.choice"] for span in approvals
    }

    assert len(approvals) == 2
    assert choices_by_parent == {
        tools["terminal-a"].context.span_id: "deny",
        tools["terminal-b"].context.span_id: "allow",
    }
    assert runtime.health_snapshot()["orphaned_spans"] == 0


@pytest.mark.integration
def test_concurrent_sessions_do_not_cross_correlate() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    runtime = TelemetryRuntime(
        _settings(),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
    )

    def execute(index: int) -> None:
        base = _base(f"session-{index}", f"turn-{index}")
        runtime.on_pre_llm_call({**base, "user_message": f"prompt {index}"})
        runtime.on_pre_tool_call(
            {
                **base,
                "tool_call_id": f"tool-{index}",
                "tool_name": "search",
                "args": {"index": index},
            }
        )
        runtime.on_post_tool_call(
            {
                **base,
                "tool_call_id": f"tool-{index}",
                "tool_name": "search",
                "result": index,
                "status": "ok",
            }
        )
        runtime.on_session_end({**base, "completed": True})

    threads = [threading.Thread(target=execute, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    spans = exporter.get_finished_spans()
    roots = [span for span in spans if span.name == "invoke_agent Hermes Agent"]
    tools = [span for span in spans if span.name == "execute_tool search"]
    roots_by_id = {span.context.span_id: span for span in roots}

    assert len(roots) == len(tools) == 12
    assert len({root.context.trace_id for root in roots}) == 12
    for tool in tools:
        assert tool.parent.span_id in roots_by_id
        assert tool.context.trace_id == roots_by_id[tool.parent.span_id].context.trace_id
    runtime.shutdown()


@pytest.mark.integration
def test_finalize_reset_interrupt_and_ttl_close_turns_with_distinct_outcomes(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    finalized = _base("session-finalized", "turn-finalized")
    reset = _base("session-reset", "turn-reset")
    interrupted = _base("session-interrupted", "turn-interrupted")
    expired = _base("session-expired", "turn-expired")

    runtime.on_session_start({**finalized, "sender_id": "user-1"})
    runtime.on_pre_llm_call({**finalized, "user_message": "finalize"})
    runtime.on_session_finalize({"session_id": "session-finalized"})

    runtime.on_pre_llm_call({**reset, "user_message": "reset"})
    runtime.on_session_reset({"old_session_id": "session-reset"})

    runtime.on_pre_llm_call({**interrupted, "user_message": "interrupt"})
    runtime.on_session_end({**interrupted, "interrupted": True})

    runtime.on_pre_llm_call({**expired, "user_message": "expire"})
    expired_state = runtime._resolve_turn_locked(expired)
    assert expired_state is not None
    expired_state.last_updated_monotonic -= runtime.settings.turn_ttl_seconds + 1
    assert runtime.sweep_expired() == 1
    assert runtime.sweep_expired() == 0

    runtime.on_session_end(
        {
            "session_id": "missing-session",
            "turn_id": "missing-turn",
            "completed": True,
        }
    )

    roots = {
        span.attributes["hermes.session.id"]: span
        for span in exporter.get_finished_spans()
        if span.name == "invoke_agent Hermes Agent"
    }
    assert roots["session-finalized"].attributes["hermes.turn.final_status"] == "finalized"
    assert roots["session-reset"].attributes["hermes.turn.final_status"] == "reset"
    assert roots["session-interrupted"].attributes["hermes.turn.final_status"] == "interrupted"
    assert roots["session-expired"].attributes["hermes.turn.final_status"] == "timed_out"
    assert roots["session-expired"].status.status_code is StatusCode.ERROR
    assert roots["session-expired"].attributes["error.type"] == "timeout"


@pytest.mark.integration
def test_turn_end_closes_open_tool_and_duplicate_subagent_delegations(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    parent = _base("orphan-parent", "orphan-turn")
    runtime.on_pre_llm_call({**parent, "user_message": "start children"})
    runtime.on_pre_tool_call(
        {
            **parent,
            "tool_call_id": "open-tool",
            "tool_name": "terminal",
            "args": {"command": "sleep"},
        }
    )
    delegation = {
        "parent_session_id": "orphan-parent",
        "parent_turn_id": "orphan-turn",
        "child_session_id": "duplicate-child",
        "child_subagent_id": "child-1",
        "child_role": "researcher",
        "child_goal": "inspect",
    }
    runtime.on_subagent_start({"child_session_id": ""})
    runtime.on_subagent_stop({"child_session_id": "never-started"})
    runtime.on_subagent_start(delegation)
    runtime.on_subagent_start(delegation)
    runtime.on_session_end({**parent, "completed": True})

    spans = exporter.get_finished_spans()
    tool = next(span for span in spans if span.name == "execute_tool terminal")
    delegations = [span for span in spans if span.name == "invoke_agent researcher"]
    error_types = {span.attributes["error.type"] for span in delegations}

    assert tool.status.status_code is StatusCode.ERROR
    assert tool.attributes["error.type"] == "abandoned"
    assert len(delegations) == 2
    assert error_types == {"duplicate_subagent_start", "abandoned_subagent"}
    assert runtime.health_snapshot()["orphaned_spans"] == 2


@pytest.mark.integration
def test_capacity_and_same_session_supersession_evict_old_turns(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
    monkeypatch: Any,
) -> None:
    runtime, exporter = telemetry
    monkeypatch.setattr(runtime, "_MAX_INFLIGHT_TURNS", 1)
    first = _base("capacity-a", "turn-a")
    second = _base("capacity-b", "turn-b")
    replacement = _base("capacity-b", "turn-c")

    runtime.on_pre_llm_call({**first, "user_message": "first"})
    runtime.on_pre_llm_call({**second, "user_message": "second"})
    runtime.on_pre_llm_call({**replacement, "user_message": "replacement"})
    runtime.on_session_end({**replacement, "completed": True})

    roots = {
        span.attributes["hermes.turn.id"]: span
        for span in exporter.get_finished_spans()
        if span.name == "invoke_agent Hermes Agent"
    }
    assert roots["turn-a"].attributes["hermes.turn.final_status"] == "evicted"
    assert roots["turn-a"].attributes["error.type"] == "state_capacity_exceeded"
    assert roots["turn-b"].attributes["hermes.turn.final_status"] == "superseded"
    assert roots["turn-b"].attributes["error.type"] == "superseded"
    assert roots["turn-c"].attributes["hermes.turn.final_status"] == "completed"


@pytest.mark.integration
def test_async_turn_end_flusher_runs_off_the_hook_path() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    flushed = threading.Event()
    original_force_flush = processor.force_flush

    def force_flush(timeout_millis: int = 30_000) -> bool:
        flushed.set()
        return original_force_flush(timeout_millis)

    processor.force_flush = force_flush  # type: ignore[method-assign]
    runtime = TelemetryRuntime(
        _settings(async_flush_on_turn_end=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=True,
    )
    base = _base("async-flush", "async-turn")

    runtime.on_pre_llm_call({**base, "user_message": "flush"})
    runtime.on_session_end({**base, "completed": True})

    assert flushed.wait(timeout=1)
    runtime.shutdown()


@pytest.mark.integration
def test_shutdown_defers_provider_cleanup_until_async_flush_finishes(
    monkeypatch: Any,
) -> None:
    flush_started = threading.Event()
    flush_release = threading.Event()
    sdk_shutdown = threading.Event()
    operation_guard = threading.Lock()
    active_operations = 0
    operations_overlapped = False

    class BlockingProcessor:
        def __init__(self, **kwargs: Any) -> None:
            return None

        def on_start(self, span: Any, parent_context: Any = None) -> None:
            return None

        def on_end(self, span: Any) -> None:
            return None

        def force_flush(self, timeout_millis: int) -> bool:
            nonlocal active_operations, operations_overlapped
            with operation_guard:
                active_operations += 1
                operations_overlapped = operations_overlapped or active_operations > 1
            flush_started.set()
            flush_release.wait(timeout=2)
            with operation_guard:
                active_operations -= 1
            return True

        def shutdown(self) -> None:
            nonlocal active_operations, operations_overlapped
            with operation_guard:
                active_operations += 1
                operations_overlapped = operations_overlapped or active_operations > 1
                active_operations -= 1
            sdk_shutdown.set()

    monkeypatch.setattr(runtime_module, "GalileoSpanProcessor", BlockingProcessor)
    runtime = TelemetryRuntime(
        _settings(
            async_flush_on_turn_end=True,
            flush_timeout_millis=100,
        )
    )
    assert runtime._processor.wait_until_ready(1)
    base = _base("blocking-flush", "blocking-turn")
    runtime.on_pre_llm_call({**base, "user_message": "flush slowly"})
    runtime.on_session_end({**base, "completed": True})
    assert flush_started.wait(timeout=1)

    before_shutdown = runtime.health_snapshot()["spans_started"]
    started = time.monotonic()
    runtime.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert runtime.health_snapshot()["provider_cleanup_deferred"] is True
    assert sdk_shutdown.is_set() is False

    # A callback racing after the runtime begins shutdown is rejected before
    # it can create state or touch the stopped provider.
    runtime.on_pre_llm_call(
        {
            **_base("late-session", "late-turn"),
            "user_message": "must be ignored",
        }
    )
    assert runtime.health_snapshot()["spans_started"] == before_shutdown
    assert runtime.health_snapshot()["inflight_turns"] == 0

    flush_release.set()
    assert sdk_shutdown.wait(timeout=1)
    assert operations_overlapped is False


def test_runtime_rejects_disabled_missing_and_unavailable_dependencies(
    monkeypatch: Any,
) -> None:
    with pytest.raises(RuntimeInitializationError, match="disabled"):
        TelemetryRuntime(_settings(enabled=False))

    with pytest.raises(RuntimeInitializationError, match="missing required"):
        TelemetryRuntime(Settings.from_env({}))

    monkeypatch.setattr(runtime_module, "_DEPENDENCIES_AVAILABLE", False)
    monkeypatch.setattr(runtime_module, "_DEPENDENCY_ERROR", ImportError("not installed"))
    with pytest.raises(RuntimeInitializationError, match="dependencies are unavailable"):
        TelemetryRuntime(_settings())
