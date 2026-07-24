"""Integration tests for Hermes event correlation and OTel span construction."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from hermes_galileo import runtime as runtime_module
from hermes_galileo.config import Settings
from hermes_galileo.native_sessions import NativeSessionManager
from hermes_galileo.privacy import CONTENT_DISABLED, pseudonymize_session_identifier
from hermes_galileo.runtime import RuntimeInitializationError, TelemetryRuntime


def _settings(**overrides: Any) -> Settings:
    base = Settings.from_env(
        {
            "GALILEO_API_KEY": "test-galileo-key",
            "GALILEO_PROJECT": "test-project",
            "GALILEO_LOG_STREAM": "test-stream",
            "HERMES_GALILEO_ASYNC_FLUSH_ON_TURN_END": "false",
            "HERMES_GALILEO_NATIVE_SESSIONS_ENABLED": "false",
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


def _sid(value: str) -> str:
    return pseudonymize_session_identifier(value, secret="test-galileo-key")


def _by_name(exporter: InMemorySpanExporter) -> dict[str, Any]:
    return {span.name: span for span in exporter.get_finished_spans()}


def _wait_until(predicate: Any, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


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
def test_native_session_rejects_conflicting_sdk_singleton_before_api_use(
    monkeypatch: Any,
) -> None:
    class ExistingSecret:
        def get_secret_value(self) -> str:
            return "test-galileo-key"

    class ExistingConfig:
        api_key = ExistingSecret()
        console_url = runtime_module.DEFAULT_CONSOLE_URL
        api_url = "https://wrong-api.example.test"

    class FakeGalileoConfig:
        _instance = ExistingConfig()

    client_created = False
    start_session_called = False

    class ClientThatMustNotBeCreated:
        def __init__(self) -> None:
            nonlocal client_created
            client_created = True

        def start_session(self, **kwargs: Any) -> str:
            nonlocal start_session_called
            start_session_called = True
            return "11111111-1111-4111-8111-111111111111"

    monkeypatch.setattr(runtime_module, "GalileoPythonConfig", FakeGalileoConfig)
    provider = TracerProvider()
    processor = SimpleSpanProcessor(InMemorySpanExporter())
    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=ClientThatMustNotBeCreated,
    )

    runtime.on_session_start({"session_id": "conflicting-native-session"})

    assert _wait_until(lambda: runtime.health_snapshot()["native_session_failed"] == 1)
    health = runtime.health_snapshot()
    assert client_created is False
    assert start_session_called is False
    assert health["native_session_attempts"] == 1
    assert health["native_session_failures"] == 1
    assert FakeGalileoConfig._instance is not None
    runtime.shutdown()


@pytest.mark.integration
def test_native_session_revalidates_sdk_singleton_after_client_construction(
    monkeypatch: Any,
) -> None:
    class ExistingSecret:
        def get_secret_value(self) -> str:
            return "test-galileo-key"

    class WrongRoute:
        api_key = ExistingSecret()
        console_url = runtime_module.DEFAULT_CONSOLE_URL
        api_url = "https://wrong-api.example.test"

    class FakeGalileoConfig:
        _instance: Any | None = None

    start_session_called = False

    class RacingClient:
        def __init__(self) -> None:
            FakeGalileoConfig._instance = WrongRoute()

        def start_session(self, **kwargs: Any) -> str:
            nonlocal start_session_called
            start_session_called = True
            return "11111111-1111-4111-8111-111111111111"

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    monkeypatch.setattr(runtime_module, "GalileoPythonConfig", FakeGalileoConfig)
    provider = TracerProvider()
    processor = SimpleSpanProcessor(InMemorySpanExporter())
    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=RacingClient,
    )

    runtime.on_session_start({"session_id": "racing-native-session"})

    assert _wait_until(lambda: runtime.health_snapshot()["native_session_failed"] == 1)
    assert start_session_called is False
    assert FakeGalileoConfig._instance is not None
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

    assert set(roots) == {_sid("session-a"), _sid("session-b")}
    assert len(api_spans) == 2
    assert roots[_sid("session-a")].context.trace_id != roots[_sid("session-b")].context.trace_id
    children_by_parent = {span.parent.span_id: span for span in api_spans}
    assert set(children_by_parent) == {
        roots[_sid("session-a")].context.span_id,
        roots[_sid("session-b")].context.span_id,
    }
    assert (
        children_by_parent[roots[_sid("session-b")].context.span_id].attributes[
            "hermes.span.synthesized"
        ]
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
    assert delegation.attributes["hermes.subagent.child_session_id"] == _sid("child-session")
    assert delegation.attributes["hermes.subagent.parent_session_id"] == _sid("parent-session")


@pytest.mark.integration
def test_parallel_subagents_keep_distinct_delegation_parents(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    parent = _base("parallel-parent", "parent-turn")
    children = (
        ("parallel-child-a", "researcher-a"),
        ("parallel-child-b", "researcher-b"),
    )
    runtime.on_pre_llm_call({**parent, "user_message": "delegate twice"})

    for child_session, role in children:
        runtime.on_subagent_start(
            {
                "parent_session_id": parent["session_id"],
                "parent_turn_id": parent["turn_id"],
                "child_session_id": child_session,
                "child_subagent_id": f"id-{role}",
                "child_role": role,
                "child_goal": f"goal-{role}",
            }
        )
        child = _base(child_session, f"turn-{role}")
        runtime.on_pre_llm_call(
            {
                **child,
                "agent_name": f"Hermes {role}",
                "user_message": f"input-{role}",
            }
        )

    for child_session, role in reversed(children):
        child = _base(child_session, f"turn-{role}")
        runtime.on_session_end({**child, "completed": True})
        runtime.on_subagent_stop(
            {
                "parent_session_id": parent["session_id"],
                "child_session_id": child_session,
                "child_role": role,
                "child_status": "completed",
            }
        )
    runtime.on_session_end({**parent, "completed": True})

    spans = exporter.get_finished_spans()
    parent_root = next(
        span for span in spans if span.name == "invoke_agent Hermes Agent" and span.parent is None
    )
    delegations = {
        span.name.removeprefix("invoke_agent "): span
        for span in spans
        if span.name in {"invoke_agent researcher-a", "invoke_agent researcher-b"}
    }
    child_roots = {
        span.name.removeprefix("invoke_agent Hermes "): span
        for span in spans
        if span.name
        in {
            "invoke_agent Hermes researcher-a",
            "invoke_agent Hermes researcher-b",
        }
    }
    assert set(delegations) == {"researcher-a", "researcher-b"}
    assert set(child_roots) == {"researcher-a", "researcher-b"}
    assert {span.parent.span_id for span in delegations.values()} == {parent_root.context.span_id}
    for role in ("researcher-a", "researcher-b"):
        assert child_roots[role].parent.span_id == delegations[role].context.span_id
        assert child_roots[role].context.trace_id == parent_root.context.trace_id


@pytest.mark.integration
def test_active_subagent_alias_is_not_evicted_at_capacity(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
    monkeypatch: Any,
) -> None:
    runtime, exporter = telemetry
    monkeypatch.setattr(runtime, "_MAX_SESSION_ALIASES", 1)
    first_parent = _base("alias-parent-one", "parent-one")
    second_parent = _base("alias-parent-two", "parent-two")

    runtime.on_pre_llm_call({**first_parent, "user_message": "first parent"})
    runtime.on_subagent_start(
        {
            "parent_session_id": first_parent["session_id"],
            "parent_turn_id": first_parent["turn_id"],
            "child_session_id": "protected-child",
            "child_role": "protected",
        }
    )
    runtime.on_pre_llm_call({**second_parent, "user_message": "second parent"})
    runtime.on_subagent_start(
        {
            "parent_session_id": second_parent["session_id"],
            "parent_turn_id": second_parent["turn_id"],
            "child_session_id": "rejected-child",
            "child_role": "rejected",
        }
    )

    protected_child = _base("protected-child", "protected-turn")
    runtime.on_pre_llm_call(
        {
            **protected_child,
            "agent_name": "Protected Child",
            "user_message": "keep alias",
        }
    )
    runtime.on_session_end({**protected_child, "completed": True})
    runtime.on_subagent_stop(
        {
            "child_session_id": "protected-child",
            "child_status": "completed",
        }
    )
    runtime.on_session_end({**first_parent, "completed": True})
    runtime.on_session_end({**second_parent, "completed": True})

    spans = exporter.get_finished_spans()
    child_root = next(span for span in spans if span.name == "invoke_agent Protected Child")
    assert child_root.attributes["gen_ai.conversation.id"] == _sid("alias-parent-one")
    assert child_root.attributes["hermes.session.id"] == _sid("alias-parent-one")
    assert len([span for span in spans if span.name == "invoke_agent protected"]) == 1
    assert not any(span.name == "invoke_agent rejected" for span in spans)
    assert runtime.health_snapshot()["session_alias_capacity_rejections"] == 1


@pytest.mark.integration
def test_completed_child_metadata_does_not_pin_inactive_alias_at_capacity(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
    monkeypatch: Any,
) -> None:
    runtime, exporter = telemetry
    monkeypatch.setattr(runtime, "_MAX_SESSION_ALIASES", 1)
    parent = _base("sequential-alias-parent", "parent-one")
    first_child = _base("sequential-child-one", "child-one")

    runtime.on_pre_llm_call({**parent, "user_message": "first delegation"})
    runtime.on_subagent_start(
        {
            "parent_session_id": parent["session_id"],
            "parent_turn_id": parent["turn_id"],
            "child_session_id": first_child["session_id"],
            "child_role": "first-child",
        }
    )
    runtime.on_session_start(first_child)
    runtime.on_pre_llm_call({**first_child, "agent_name": "First Child", "user_message": "first"})
    runtime.on_session_end({**first_child, "completed": True})
    runtime.on_subagent_stop(
        {
            "child_session_id": first_child["session_id"],
            "child_status": "completed",
        }
    )
    runtime.on_session_end({**parent, "completed": True})

    second_parent = _base(parent["session_id"], "parent-two")
    runtime.on_pre_llm_call({**second_parent, "user_message": "second delegation"})
    runtime.on_subagent_start(
        {
            "parent_session_id": second_parent["session_id"],
            "parent_turn_id": second_parent["turn_id"],
            "child_session_id": "sequential-child-two",
            "child_role": "second-child",
        }
    )

    snapshot = runtime.health_snapshot()
    assert snapshot["session_aliases"] == 1
    assert snapshot["session_alias_capacity_rejections"] == 0
    assert "sequential-child-two" in runtime._delegations
    assert "sequential-child-one" not in runtime._session_aliases

    runtime.on_subagent_stop(
        {
            "child_session_id": "sequential-child-two",
            "child_status": "completed",
        }
    )
    runtime.on_session_end({**second_parent, "completed": True})
    assert any(span.name == "invoke_agent second-child" for span in exporter.get_finished_spans())


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
    assert roots[_sid("session-finalized")].attributes["hermes.turn.final_status"] == "finalized"
    assert roots[_sid("session-reset")].attributes["hermes.turn.final_status"] == "reset"
    assert (
        roots[_sid("session-interrupted")].attributes["hermes.turn.final_status"] == "interrupted"
    )
    assert roots[_sid("session-expired")].attributes["hermes.turn.final_status"] == "timed_out"
    assert roots[_sid("session-expired")].status.status_code is StatusCode.ERROR
    assert roots[_sid("session-expired")].attributes["error.type"] == "timeout"


@pytest.mark.integration
def test_turn_end_closes_open_tool_but_keeps_delegation_until_stop(
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

    interim_spans = exporter.get_finished_spans()
    tool = next(span for span in interim_spans if span.name == "execute_tool terminal")
    assert len([span for span in interim_spans if span.name == "invoke_agent researcher"]) == 1
    assert runtime.health_snapshot()["inflight_subagents"] == 1

    assert tool.status.status_code is StatusCode.ERROR
    assert tool.attributes["error.type"] == "abandoned"
    runtime.on_subagent_stop(
        {
            "child_session_id": "duplicate-child",
            "child_status": "cancelled",
            "child_summary": "cancelled after parent turn ended",
        }
    )

    delegations = [
        span for span in exporter.get_finished_spans() if span.name == "invoke_agent researcher"
    ]
    error_types = {span.attributes["error.type"] for span in delegations}
    assert len(delegations) == 2
    assert error_types == {"duplicate_subagent_start", "cancelled"}
    assert runtime.health_snapshot()["orphaned_spans"] == 1


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


@pytest.mark.integration
def test_native_session_is_nonblocking_single_flight_and_shared_by_two_turns() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    entered = threading.Event()
    release = threading.Event()
    calls: list[dict[str, Any]] = []
    session_uuid = "11111111-1111-4111-8111-111111111111"

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            entered.set()
            release.wait(timeout=2)
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=1_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    raw_session_id = "raw-private-session"
    external_id = _sid(raw_session_id)
    runtime.on_session_start({"session_id": raw_session_id})
    assert entered.wait(timeout=1)

    started = time.monotonic()
    for turn_id in ("turn-one", "turn-two"):
        payload = _base(raw_session_id, turn_id)
        runtime.on_pre_llm_call({**payload, "user_message": f"message {turn_id}"})
        runtime.on_session_end({**payload, "completed": True})
    hook_duration = time.monotonic() - started

    assert hook_duration < 0.1
    assert exporter.get_finished_spans() == ()
    assert runtime.health_snapshot()["native_session_deferred_spans"] == 2
    assert len(calls) == 1
    assert calls[0] == {
        "name": "Hermes Agent session",
        "external_id": external_id,
        "metadata": {
            "service.name": "hermes-agent",
            "deployment.environment.name": "development",
        },
    }

    release.set()
    assert _wait_until(lambda: len(exporter.get_finished_spans()) == 2)
    roots = exporter.get_finished_spans()
    assert {span.attributes["galileo.session.id"] for span in roots} == {session_uuid}
    assert {span.attributes["gen_ai.conversation.id"] for span in roots} == {external_id}
    assert {span.attributes["hermes.session.id"] for span in roots} == {external_id}
    assert all(raw_session_id not in str(span.attributes) for span in roots)
    assert runtime.health_snapshot()["native_session_ready"] == 1
    assert runtime.health_snapshot()["native_session_deferred_spans"] == 0
    runtime.shutdown()


@pytest.mark.integration
def test_pending_finalize_waits_for_resolution_then_releases_local_mapping() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    entered = threading.Event()
    release = threading.Event()
    session_uuid = "22222222-2222-4222-8222-222222222222"

    class BlockingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=1_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=BlockingGalileoLogger,
    )
    payload = _base("finalize-while-pending", "short-turn")
    runtime.on_session_start(payload)
    assert entered.wait(timeout=1)
    runtime.on_pre_llm_call({**payload, "user_message": "short process"})

    started = time.monotonic()
    runtime.on_session_finalize({"session_id": payload["session_id"]})
    assert time.monotonic() - started < 0.1
    assert exporter.get_finished_spans() == ()
    assert runtime.health_snapshot()["native_session_release_pending"] == 1

    release.set()
    assert _wait_until(lambda: len(exporter.get_finished_spans()) == 1)
    root = exporter.get_finished_spans()[0]
    assert root.attributes["galileo.session.id"] == session_uuid
    assert root.attributes["hermes.turn.final_status"] == "finalized"
    assert _wait_until(lambda: runtime.health_snapshot()["native_session_mappings"] == 0)
    assert runtime.health_snapshot()["native_session_release_pending"] == 0
    runtime.shutdown()


@pytest.mark.integration
def test_reset_releases_old_mapping_and_new_session_gets_a_distinct_uuid() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    calls: list[str] = []
    ids = {
        _sid("before-reset"): "99999999-9999-4999-8999-999999999999",
        _sid("after-reset"): "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    }

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            external_id = kwargs["external_id"]
            calls.append(external_id)
            return ids[external_id]

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    before = _base("before-reset", "before-turn")
    runtime.on_pre_llm_call({**before, "user_message": "before"})
    runtime.on_session_end({**before, "completed": True})
    assert _wait_until(lambda: len(exporter.get_finished_spans()) == 1)

    runtime.on_session_reset(
        {
            "old_session_id": "before-reset",
            "new_session_id": "after-reset",
        }
    )
    assert runtime.health_snapshot()["native_session_mappings"] == 0

    after = _base("after-reset", "after-turn")
    runtime.on_pre_llm_call({**after, "user_message": "after"})
    runtime.on_session_end({**after, "completed": True})
    assert _wait_until(lambda: len(exporter.get_finished_spans()) == 2)

    roots = {span.attributes["hermes.session.id"]: span for span in exporter.get_finished_spans()}
    assert roots[_sid("before-reset")].attributes["galileo.session.id"] == ids[_sid("before-reset")]
    assert roots[_sid("after-reset")].attributes["galileo.session.id"] == ids[_sid("after-reset")]
    assert calls == [_sid("before-reset"), _sid("after-reset")]
    runtime.shutdown()


@pytest.mark.integration
def test_force_flush_waits_boundedly_for_native_session_before_export() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    entered = threading.Event()
    release = threading.Event()
    session_uuid = "33333333-3333-4333-8333-333333333333"

    class BlockingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=1_000,
            flush_timeout_millis=1_500,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=BlockingGalileoLogger,
    )
    payload = _base("explicit-flush", "turn")
    runtime.on_pre_llm_call({**payload, "user_message": "flush me"})
    assert entered.wait(timeout=1)
    runtime.on_session_end({**payload, "completed": True})

    result: list[bool] = []
    flush_thread = threading.Thread(target=lambda: result.append(runtime.force_flush()))
    flush_thread.start()
    assert flush_thread.is_alive()
    release.set()
    flush_thread.join(timeout=1)

    assert result == [True]
    roots = exporter.get_finished_spans()
    assert len(roots) == 1
    assert roots[0].attributes["galileo.session.id"] == session_uuid
    runtime.shutdown()


@pytest.mark.integration
def test_shutdown_drains_native_session_before_stopping_provider() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    entered = threading.Event()
    release = threading.Event()
    session_uuid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    class BlockingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=1_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=BlockingGalileoLogger,
    )
    payload = _base("shutdown-drain", "turn")
    runtime.on_pre_llm_call({**payload, "user_message": "drain"})
    runtime.on_session_end({**payload, "completed": True})
    assert entered.wait(timeout=1)

    shutdown_thread = threading.Thread(target=runtime.shutdown)
    shutdown_thread.start()
    assert shutdown_thread.is_alive()
    release.set()
    shutdown_thread.join(timeout=1)

    assert not shutdown_thread.is_alive()
    roots = exporter.get_finished_spans()
    assert len(roots) == 1
    assert roots[0].attributes["galileo.session.id"] == session_uuid


@pytest.mark.integration
def test_native_session_timeout_is_fail_open_and_shutdown_is_bounded() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    entered = threading.Event()
    release = threading.Event()

    class StuckGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return "44444444-4444-4444-8444-444444444444"

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=100,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=StuckGalileoLogger,
    )
    payload = _base("timeout-session", "turn")
    started = time.monotonic()
    runtime.on_pre_llm_call({**payload, "user_message": "do not block"})
    runtime.on_session_end({**payload, "completed": True})
    assert time.monotonic() - started < 0.1
    assert entered.wait(timeout=1)
    assert _wait_until(lambda: len(exporter.get_finished_spans()) == 1)

    root = exporter.get_finished_spans()[0]
    assert "galileo.session.id" not in root.attributes
    assert root.attributes["hermes.session.id"] == _sid("timeout-session")
    health = runtime.health_snapshot()
    assert health["native_session_timeouts"] == 1
    assert health["native_session_failed"] == 1

    started = time.monotonic()
    runtime.shutdown()
    assert time.monotonic() - started < 0.5
    release.set()


@pytest.mark.integration
def test_native_session_api_failure_is_not_retried_within_one_lifecycle() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    calls: list[str] = []

    class FailingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            calls.append(kwargs["external_id"])
            raise ConnectionError("synthetic Session API failure")

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FailingGalileoLogger,
    )
    session_id = "failed-native-session"
    for expected_count, turn_id in enumerate(("first", "second"), start=1):
        payload = _base(session_id, turn_id)
        runtime.on_pre_llm_call({**payload, "user_message": turn_id})
        runtime.on_session_end({**payload, "completed": True})
        assert _wait_until(
            lambda expected=expected_count: len(exporter.get_finished_spans()) >= expected
        )

    assert calls == [_sid(session_id)]
    assert all(
        "galileo.session.id" not in span.attributes for span in exporter.get_finished_spans()
    )
    assert runtime.health_snapshot()["native_session_failures"] == 1

    # Finalize releases only local state. A later lifecycle performs one new
    # idempotent SDK lookup using the same external ID.
    runtime.on_session_finalize({"session_id": session_id})
    third = _base(session_id, "third")
    runtime.on_pre_llm_call({**third, "user_message": "third"})
    runtime.on_session_end({**third, "completed": True})
    assert _wait_until(lambda: len(calls) == 2)
    assert calls == [_sid(session_id), _sid(session_id)]
    assert _wait_until(lambda: len(exporter.get_finished_spans()) == 3)
    runtime.shutdown()


@pytest.mark.integration
def test_subagent_alias_uses_parent_native_session_and_pseudonymous_ids() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    calls: list[str] = []
    session_uuid = "55555555-5555-4555-8555-555555555555"

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            calls.append(kwargs["external_id"])
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    parent = _base("native-parent", "parent-turn")
    child = _base("native-child", "child-turn")
    runtime.on_pre_llm_call({**parent, "user_message": "delegate"})
    assert _wait_until(lambda: runtime.health_snapshot()["native_session_ready"] == 1)
    runtime.on_subagent_start(
        {
            "parent_session_id": parent["session_id"],
            "parent_turn_id": parent["turn_id"],
            "child_session_id": child["session_id"],
            "child_role": "researcher",
            "child_goal": "inspect",
        }
    )
    runtime.on_session_start(child)
    runtime.on_pre_llm_call({**child, "agent_name": "Hermes Subagent", "user_message": "inspect"})
    runtime.on_session_end({**child, "completed": True})
    runtime.on_subagent_stop(
        {
            "child_session_id": child["session_id"],
            "child_status": "completed",
        }
    )
    runtime.on_session_finalize({"session_id": child["session_id"]})
    runtime.on_session_end({**parent, "completed": True})

    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    assert {span.attributes["galileo.session.id"] for span in spans} == {session_uuid}
    assert calls == [_sid("native-parent")]
    child_root = next(span for span in spans if span.name == "invoke_agent Hermes Subagent")
    delegation = next(span for span in spans if span.name == "invoke_agent researcher")
    assert child_root.attributes["hermes.session.id"] == _sid("native-parent")
    assert child_root.attributes["gen_ai.conversation.id"] == _sid("native-parent")
    assert delegation.attributes["hermes.subagent.parent_session_id"] == _sid("native-parent")
    assert delegation.attributes["hermes.subagent.child_session_id"] == _sid("native-child")
    assert "native-parent" not in str(delegation.attributes)
    assert "native-child" not in str(delegation.attributes)
    runtime.shutdown()


@pytest.mark.integration
def test_parent_finalize_defers_native_mapping_release_until_child_turn_ends() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    session_uuid = "56565656-5656-4656-8656-565656565656"

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    parent = _base("finalize-parent", "parent-turn")
    child = _base("finalize-child", "child-turn")
    try:
        runtime.on_pre_llm_call({**parent, "user_message": "delegate"})
        assert _wait_until(lambda: runtime.health_snapshot()["native_session_ready"] == 1)
        runtime.on_subagent_start(
            {
                "parent_session_id": parent["session_id"],
                "parent_turn_id": parent["turn_id"],
                "child_session_id": child["session_id"],
                "child_role": "finalize-child",
            }
        )
        runtime.on_pre_llm_call(
            {**child, "agent_name": "Finalize Child", "user_message": "still running"}
        )

        runtime.on_session_finalize({"session_id": parent["session_id"]})
        snapshot = runtime.health_snapshot()
        assert snapshot["pending_session_releases"] == 1
        assert snapshot["native_session_mappings"] == 1
        assert snapshot["session_aliases"] == 1

        runtime.on_session_end({**child, "completed": True})
        assert runtime.health_snapshot()["pending_session_releases"] == 1
        runtime.on_subagent_stop(
            {
                "child_session_id": child["session_id"],
                "child_status": "completed",
            }
        )
        assert runtime.health_snapshot()["pending_session_releases"] == 0
        assert runtime.health_snapshot()["native_session_mappings"] == 0
        assert runtime.health_snapshot()["session_aliases"] == 0

        spans = exporter.get_finished_spans()
        assert len(spans) == 3
        assert {span.attributes["galileo.session.id"] for span in spans} == {session_uuid}
    finally:
        runtime.shutdown()


@pytest.mark.integration
def test_parent_turn_and_finalize_before_queued_child_keep_one_native_session() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    session_uuid = "57575757-5757-4757-8757-575757575757"
    calls: list[str] = []

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            calls.append(kwargs["external_id"])
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    parent = _base("queued-parent", "parent-turn")
    child = _base("queued-child", "child-turn")
    try:
        runtime.on_pre_llm_call({**parent, "user_message": "delegate in background"})
        assert _wait_until(lambda: runtime.health_snapshot()["native_session_ready"] == 1)
        runtime.on_subagent_start(
            {
                "parent_session_id": parent["session_id"],
                "parent_turn_id": parent["turn_id"],
                "child_session_id": child["session_id"],
                "child_role": "queued-child",
            }
        )

        # Hermes may finish the parent turn and lifecycle while the child is
        # still waiting for a subagent execution slot.
        runtime.on_session_end({**parent, "completed": True})
        runtime.on_session_finalize({"session_id": parent["session_id"]})
        snapshot = runtime.health_snapshot()
        assert snapshot["inflight_subagents"] == 1
        assert snapshot["pending_session_releases"] == 1
        assert snapshot["native_session_mappings"] == 1
        assert snapshot["session_aliases"] == 1

        runtime.on_session_start(child)
        runtime.on_pre_llm_call(
            {**child, "agent_name": "Queued Child", "user_message": "run later"}
        )
        runtime.on_session_end({**child, "completed": True})
        assert runtime.health_snapshot()["native_session_mappings"] == 1
        runtime.on_subagent_stop(
            {
                "child_session_id": child["session_id"],
                "child_status": "completed",
            }
        )

        snapshot = runtime.health_snapshot()
        assert snapshot["pending_session_releases"] == 0
        assert snapshot["native_session_mappings"] == 0
        assert snapshot["session_aliases"] == 0
        spans = exporter.get_finished_spans()
        assert len(spans) == 3
        assert {span.attributes["galileo.session.id"] for span in spans} == {session_uuid}
        assert calls == [_sid(parent["session_id"])]
    finally:
        runtime.shutdown()


@pytest.mark.integration
def test_late_subagent_alias_updates_active_conversation_without_native_sessions(
    telemetry: tuple[TelemetryRuntime, InMemorySpanExporter],
) -> None:
    runtime, exporter = telemetry
    child = _base("late-disabled-child", "child-turn")
    parent = _base("late-disabled-parent", "parent-turn")

    runtime.on_pre_llm_call({**child, "agent_name": "Late Disabled Child", "user_message": "child"})
    runtime.on_pre_llm_call({**parent, "user_message": "parent"})
    runtime.on_subagent_start(
        {
            "parent_session_id": parent["session_id"],
            "parent_turn_id": parent["turn_id"],
            "child_session_id": child["session_id"],
            "child_role": "late-disabled-child",
        }
    )
    runtime.on_session_end({**child, "completed": True})
    runtime.on_subagent_stop(
        {
            "child_session_id": child["session_id"],
            "child_status": "completed",
        }
    )
    runtime.on_session_end({**parent, "completed": True})

    child_root = next(
        span
        for span in exporter.get_finished_spans()
        if span.name == "invoke_agent Late Disabled Child"
    )
    parent_conversation_id = _sid("late-disabled-parent")
    assert child_root.attributes["gen_ai.conversation.id"] == parent_conversation_id
    assert child_root.attributes["hermes.session.id"] == parent_conversation_id


@pytest.mark.integration
def test_late_subagent_alias_rebinds_pending_child_span_to_parent_session() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    child_entered = threading.Event()
    child_release = threading.Event()
    calls: list[str] = []
    parent_uuid = "77777777-7777-4777-8777-777777777777"
    obsolete_child_uuid = "88888888-8888-4888-8888-888888888888"
    child_external_id = _sid("out-of-order-child")

    class OrderingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            external_id = kwargs["external_id"]
            calls.append(external_id)
            if external_id == child_external_id:
                child_entered.set()
                child_release.wait(timeout=2)
                return obsolete_child_uuid
            return parent_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=1_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=OrderingGalileoLogger,
    )
    child = _base("out-of-order-child", "child-turn")
    parent = _base("out-of-order-parent", "parent-turn")

    # This intentionally violates Hermes' canonical ordering to prove that
    # still-pending local spans can be repaired when subagent_start arrives.
    runtime.on_pre_llm_call(
        {**child, "agent_name": "Early Child", "user_message": "started too soon"}
    )
    runtime.on_session_end({**child, "completed": True})
    assert child_entered.wait(timeout=1)
    assert exporter.get_finished_spans() == ()

    runtime.on_pre_llm_call({**parent, "user_message": "parent"})
    runtime.on_subagent_start(
        {
            "parent_session_id": parent["session_id"],
            "parent_turn_id": parent["turn_id"],
            "child_session_id": child["session_id"],
            "child_role": "late-child",
        }
    )

    assert _wait_until(
        lambda: any(
            span.name == "invoke_agent Early Child" for span in exporter.get_finished_spans()
        )
    )
    child_root = next(
        span for span in exporter.get_finished_spans() if span.name == "invoke_agent Early Child"
    )
    assert child_root.attributes["galileo.session.id"] == parent_uuid
    assert child_root.attributes["galileo.session.id"] != obsolete_child_uuid

    runtime.on_subagent_stop(
        {
            "child_session_id": child["session_id"],
            "child_status": "completed",
        }
    )
    runtime.on_session_end({**parent, "completed": True})
    child_release.set()
    assert _sid("out-of-order-parent") in calls
    assert child_external_id in calls
    assert all(
        span.attributes.get("galileo.session.id") != obsolete_child_uuid
        for span in exporter.get_finished_spans()
    )
    runtime.shutdown()


@pytest.mark.integration
def test_late_subagent_alias_stamps_deferred_child_when_parent_is_ready() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    child_entered = threading.Event()
    child_release = threading.Event()
    parent_uuid = "99999999-9999-4999-8999-999999999999"
    obsolete_child_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    child_external_id = _sid("deferred-ready-child")

    class OrderingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            if kwargs["external_id"] == child_external_id:
                child_entered.set()
                child_release.wait(timeout=2)
                return obsolete_child_uuid
            return parent_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=1_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=OrderingGalileoLogger,
    )
    parent = _base("deferred-ready-parent", "parent-turn")
    child = _base("deferred-ready-child", "child-turn")
    try:
        runtime.on_pre_llm_call({**parent, "user_message": "parent"})
        assert _wait_until(
            lambda: runtime._native_sessions.lookup(_sid("deferred-ready-parent")).status == "ready"
        )

        runtime.on_pre_llm_call({**child, "agent_name": "Deferred Child", "user_message": "child"})
        runtime.on_session_end({**child, "completed": True})
        assert child_entered.wait(timeout=1)
        assert exporter.get_finished_spans() == ()

        runtime.on_subagent_start(
            {
                "parent_session_id": parent["session_id"],
                "parent_turn_id": parent["turn_id"],
                "child_session_id": child["session_id"],
                "child_role": "deferred-child",
            }
        )

        assert _wait_until(
            lambda: any(
                span.name == "invoke_agent Deferred Child" for span in exporter.get_finished_spans()
            )
        )
        child_root = next(
            span
            for span in exporter.get_finished_spans()
            if span.name == "invoke_agent Deferred Child"
        )
        assert child_root.attributes["galileo.session.id"] == parent_uuid
        assert child_root.attributes["gen_ai.conversation.id"] == _sid("deferred-ready-parent")

        runtime.on_subagent_stop(
            {
                "child_session_id": child["session_id"],
                "child_status": "completed",
            }
        )
        runtime.on_session_end({**parent, "completed": True})
    finally:
        child_release.set()
        runtime.shutdown()


@pytest.mark.integration
def test_late_subagent_alias_overwrites_ready_child_uuid_before_span_end() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    child_uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    parent_uuid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    ids = {
        _sid("ready-child"): child_uuid,
        _sid("ready-parent"): parent_uuid,
    }

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            return ids[kwargs["external_id"]]

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    child = _base("ready-child", "child-turn")
    parent = _base("ready-parent", "parent-turn")
    runtime.on_session_start(child)
    assert _wait_until(
        lambda: runtime._native_sessions.lookup(_sid("ready-child")).status == "ready"
    )
    runtime.on_pre_llm_call(
        {**child, "agent_name": "Ready Child", "user_message": "already mapped"}
    )

    runtime.on_pre_llm_call({**parent, "user_message": "parent"})
    runtime.on_subagent_start(
        {
            "parent_session_id": parent["session_id"],
            "parent_turn_id": parent["turn_id"],
            "child_session_id": child["session_id"],
            "child_role": "ready-child",
        }
    )
    assert _wait_until(
        lambda: runtime._native_sessions.lookup(_sid("ready-parent")).status == "ready"
    )
    runtime.on_session_end({**child, "completed": True})

    child_root = next(
        span for span in exporter.get_finished_spans() if span.name == "invoke_agent Ready Child"
    )
    assert child_root.attributes["galileo.session.id"] == parent_uuid
    assert child_root.attributes["galileo.session.id"] != child_uuid

    runtime.on_subagent_stop(
        {
            "child_session_id": child["session_id"],
            "child_status": "completed",
        }
    )
    runtime.on_session_end({**parent, "completed": True})
    runtime.shutdown()


@pytest.mark.integration
def test_late_subagent_alias_drops_ready_child_uuid_when_parent_resolution_fails() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    child_uuid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    parent_external_id = _sid("failed-parent")

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            if kwargs["external_id"] == parent_external_id:
                raise RuntimeError("synthetic parent Session failure")
            return child_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    child = _base("failed-child", "child-turn")
    parent = _base("failed-parent", "parent-turn")
    runtime.on_session_start(child)
    assert _wait_until(
        lambda: runtime._native_sessions.lookup(_sid("failed-child")).status == "ready"
    )
    runtime.on_pre_llm_call({**child, "agent_name": "Rebound Child", "user_message": "child"})
    runtime.on_pre_llm_call({**parent, "user_message": "parent"})
    runtime.on_subagent_start(
        {
            "parent_session_id": parent["session_id"],
            "parent_turn_id": parent["turn_id"],
            "child_session_id": child["session_id"],
            "child_role": "rebound-child",
        }
    )
    assert _wait_until(
        lambda: runtime._native_sessions.lookup(parent_external_id).status == "failed"
    )
    runtime.on_session_end({**child, "completed": True})

    child_root = next(
        span for span in exporter.get_finished_spans() if span.name == "invoke_agent Rebound Child"
    )
    assert "galileo.session.id" not in child_root.attributes
    assert child_root.attributes["gen_ai.conversation.id"] == parent_external_id
    assert child_root.attributes["hermes.session.id"] == parent_external_id

    runtime.on_subagent_stop(
        {
            "child_session_id": child["session_id"],
            "child_status": "completed",
        }
    )
    runtime.on_session_end({**parent, "completed": False, "reason": "failed"})
    runtime.shutdown()


@pytest.mark.integration
def test_late_subagent_alias_retracks_active_child_after_child_session_failure() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    parent_uuid = "f0f0f0f0-f0f0-40f0-80f0-f0f0f0f0f0f0"
    child_external_id = _sid("failed-first-child")

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            if kwargs["external_id"] == child_external_id:
                raise ConnectionError("synthetic child Session failure")
            return parent_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    child = _base("failed-first-child", "child-turn")
    parent = _base("ready-later-parent", "parent-turn")
    try:
        runtime.on_pre_llm_call(
            {**child, "agent_name": "Failed First Child", "user_message": "child"}
        )
        assert _wait_until(
            lambda: (
                (state := runtime._resolve_turn_locked(child)) is not None
                and state.native_session_key == ""
            )
        )

        runtime.on_pre_llm_call({**parent, "user_message": "parent"})
        assert _wait_until(
            lambda: runtime._native_sessions.lookup(_sid("ready-later-parent")).status == "ready"
        )
        runtime.on_subagent_start(
            {
                "parent_session_id": parent["session_id"],
                "parent_turn_id": parent["turn_id"],
                "child_session_id": child["session_id"],
                "child_role": "failed-first-child",
            }
        )
        runtime.on_pre_tool_call(
            {
                **child,
                "tool_call_id": "after-rebind",
                "tool_name": "after_rebind",
                "args": {},
            }
        )
        runtime.on_post_tool_call(
            {
                **child,
                "tool_call_id": "after-rebind",
                "tool_name": "after_rebind",
                "status": "ok",
                "result": {},
            }
        )
        runtime.on_session_end({**child, "completed": True})
        runtime.on_subagent_stop(
            {
                "child_session_id": child["session_id"],
                "child_status": "completed",
            }
        )
        runtime.on_session_end({**parent, "completed": True})

        child_spans = [
            span
            for span in exporter.get_finished_spans()
            if span.name
            in {
                "invoke_agent Failed First Child",
                "execute_tool after_rebind",
            }
        ]
        assert len(child_spans) == 2
        assert {span.attributes["galileo.session.id"] for span in child_spans} == {parent_uuid}
        assert {
            span.attributes["gen_ai.conversation.id"]
            for span in child_spans
            if "gen_ai.conversation.id" in span.attributes
        } == {_sid("ready-later-parent")}
    finally:
        runtime.shutdown()


@pytest.mark.integration
def test_late_rebind_cannot_replace_an_expired_parent_generation() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    calls: list[str] = []
    uuids = iter(
        (
            "71717171-7171-4171-8171-717171717171",
            "72727272-7272-4272-8272-727272727272",
            "73737373-7373-4373-8373-737373737373",
        )
    )

    class SequencedGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            calls.append(kwargs["external_id"])
            return next(uuids)

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=SequencedGalileoLogger,
    )
    assert runtime._native_sessions is not None
    manager = runtime._native_sessions
    child = _base("expired-rebind-child", "child-turn")
    parent = _base("expired-rebind-parent", "parent-turn")
    parent_key = _sid(parent["session_id"])
    try:
        runtime.on_pre_llm_call({**child, "agent_name": "Expired Child", "user_message": "child"})
        runtime.on_pre_llm_call({**parent, "user_message": "parent"})
        assert manager.wait_until_idle(1)
        parent_state = runtime._resolve_turn_locked(parent)
        assert parent_state is not None
        old_parent_generation = parent_state.native_session_generation

        manager.finalize(parent_key)
        assert manager.ensure(parent_key).status == "pending"
        assert manager.wait_until_idle(1)
        assert manager.lookup(parent_key).generation != old_parent_generation

        runtime.on_subagent_start(
            {
                "parent_session_id": parent["session_id"],
                "parent_turn_id": parent["turn_id"],
                "child_session_id": child["session_id"],
                "child_role": "expired-child",
            }
        )
        child_state = runtime._resolve_turn_locked(child)
        assert child_state is not None
        assert child_state.native_session_key == ""
        assert child_state.native_session_generation == 0

        runtime.on_session_end({**child, "completed": True})
        runtime.on_subagent_stop(
            {
                "child_session_id": child["session_id"],
                "child_status": "completed",
            }
        )
        runtime.on_session_end({**parent, "completed": True})
        spans = exporter.get_finished_spans()
        assert len(spans) == 3
        assert all("galileo.session.id" not in span.attributes for span in spans)
        assert len(calls) == 3
        assert calls.count(_sid(child["session_id"])) == 1
        assert calls.count(parent_key) == 2
    finally:
        runtime.shutdown()


@pytest.mark.integration
def test_nested_late_rebind_uses_active_immediate_parent_generation() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    root_uuid = "74747474-7474-4474-8474-747474747474"
    grandchild_uuid = "75757575-7575-4575-8575-757575757575"
    calls: list[str] = []
    ids = {
        _sid("nested-root"): root_uuid,
        _sid("nested-grandchild"): grandchild_uuid,
    }

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            external_id = kwargs["external_id"]
            calls.append(external_id)
            return ids[external_id]

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    assert runtime._native_sessions is not None
    root = _base("nested-root", "root-turn")
    child = _base("nested-child", "child-turn")
    grandchild = _base("nested-grandchild", "grandchild-turn")
    try:
        runtime.on_pre_llm_call({**root, "user_message": "root"})
        assert runtime._native_sessions.wait_until_idle(1)
        runtime.on_subagent_start(
            {
                "parent_session_id": root["session_id"],
                "parent_turn_id": root["turn_id"],
                "child_session_id": child["session_id"],
                "child_role": "child",
            }
        )
        runtime.on_pre_llm_call({**child, "agent_name": "Nested Child", "user_message": "child"})
        runtime.on_session_end({**root, "completed": True})

        # The grandchild turn arrives before its nested subagent_start.
        runtime.on_pre_llm_call(
            {
                **grandchild,
                "agent_name": "Nested Grandchild",
                "user_message": "grandchild",
            }
        )
        assert runtime._native_sessions.wait_until_idle(1)
        runtime.on_subagent_start(
            {
                "parent_session_id": child["session_id"],
                "parent_turn_id": child["turn_id"],
                "child_session_id": grandchild["session_id"],
                "child_role": "grandchild",
            }
        )

        runtime.on_session_end({**grandchild, "completed": True})
        runtime.on_subagent_stop(
            {
                "child_session_id": grandchild["session_id"],
                "child_status": "completed",
            }
        )
        runtime.on_session_end({**child, "completed": True})
        runtime.on_subagent_stop(
            {
                "child_session_id": child["session_id"],
                "child_status": "completed",
            }
        )

        spans = exporter.get_finished_spans()
        assert len(spans) == 5
        assert {span.attributes["galileo.session.id"] for span in spans} == {root_uuid}
        assert calls == [_sid(root["session_id"]), _sid(grandchild["session_id"])]
    finally:
        runtime.shutdown()


@pytest.mark.integration
def test_cancel_pending_removes_a_finalized_release_mapping() -> None:
    entered = threading.Event()
    release = threading.Event()
    callbacks: list[tuple[str, str | None]] = []

    class BlockingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return "65656565-6565-4565-8565-656565656565"

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    manager = NativeSessionManager(
        _settings(native_sessions_enabled=True),
        client_factory=BlockingGalileoLogger,
        on_resolved=lambda key, value, _generation: callbacks.append((key, value)),
    )
    try:
        assert manager.ensure("hermes:cancel-finalized").status == "pending"
        assert entered.wait(timeout=1)
        manager.finalize("hermes:cancel-finalized")
        manager.cancel_pending()

        assert callbacks == [("hermes:cancel-finalized", None)]
        snapshot = manager.health_snapshot()
        assert snapshot["native_session_release_pending"] == 0
        assert snapshot["native_session_mappings"] == 0
        assert snapshot["native_session_failed"] == 0
    finally:
        release.set()
        manager.shutdown(1)


@pytest.mark.integration
def test_reopening_pending_finalized_mapping_cancels_local_release() -> None:
    entered = threading.Event()
    release = threading.Event()
    callbacks: list[tuple[str, str | None]] = []
    session_uuid = "67676767-6767-4767-8767-676767676767"

    class BlockingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    manager = NativeSessionManager(
        _settings(native_sessions_enabled=True),
        client_factory=BlockingGalileoLogger,
        on_resolved=lambda key, value, _generation: callbacks.append((key, value)),
    )
    try:
        external_id = "hermes:reopened"
        first = manager.ensure(external_id)
        assert first.status == "pending"
        assert entered.wait(timeout=1)
        manager.finalize(external_id)
        assert manager.health_snapshot()["native_session_release_pending"] == 1

        reopened = manager.ensure(external_id)
        assert reopened.status == "pending"
        assert reopened.generation == first.generation
        assert manager.health_snapshot()["native_session_release_pending"] == 0
        release.set()
        assert manager.wait_until_idle(1)

        resolved = manager.lookup(external_id)
        assert resolved.status == "ready"
        assert resolved.galileo_session_id == session_uuid
        assert callbacks == [(external_id, session_uuid)]
        assert manager.health_snapshot()["native_session_mappings"] == 1
        manager.finalize(external_id)
        assert manager.health_snapshot()["native_session_mappings"] == 0
    finally:
        release.set()
        manager.shutdown(1)


@pytest.mark.integration
def test_stale_generation_callback_drains_only_old_deferred_spans() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    first_call_entered = threading.Event()
    release_first_call = threading.Event()
    old_callback_entered = threading.Event()
    release_old_callback = threading.Event()
    call_lock = threading.Lock()
    calls = 0
    session_uuid = "68686868-6868-4868-8868-686868686868"

    class TwoGenerationGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            nonlocal calls
            with call_lock:
                calls += 1
                call_number = calls
            if call_number == 1:
                first_call_entered.set()
                release_first_call.wait(timeout=2)
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=2_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=TwoGenerationGalileoLogger,
    )
    assert runtime._native_sessions is not None
    manager = runtime._native_sessions
    cancel_thread: threading.Thread | None = None
    payload = _base("generation-session", "old-turn")
    external_id = _sid(payload["session_id"])
    try:
        runtime.on_pre_llm_call({**payload, "user_message": "old lifecycle"})
        assert first_call_entered.wait(timeout=1)
        old_generation = manager.lookup(external_id).generation
        runtime.on_session_end({**payload, "completed": True})
        assert runtime.health_snapshot()["native_session_deferred_spans"] == 1
        runtime.on_session_finalize({"session_id": payload["session_id"]})

        original_callback = manager._on_resolved

        def gated_callback(key: str, value: str | None, generation: int) -> None:
            if generation == old_generation:
                old_callback_entered.set()
                release_old_callback.wait(timeout=2)
            original_callback(key, value, generation)

        manager._on_resolved = gated_callback
        cancel_thread = threading.Thread(target=manager.cancel_pending)
        cancel_thread.start()
        assert old_callback_entered.wait(timeout=1)

        new_payload = _base(payload["session_id"], "new-turn")
        runtime.on_session_start(new_payload)
        runtime.on_pre_llm_call({**new_payload, "user_message": "new lifecycle"})
        assert _wait_until(
            lambda: (
                (resolution := manager.lookup(external_id)).status == "ready"
                and resolution.generation != old_generation
            )
        )

        release_old_callback.set()
        cancel_thread.join(timeout=1)
        assert not cancel_thread.is_alive()
        assert runtime.health_snapshot()["native_session_deferred_spans"] == 0
        new_state = runtime._resolve_turn_locked(new_payload)
        assert new_state is not None
        assert new_state.native_session_key == external_id
        assert new_state.native_session_generation != old_generation

        runtime.on_session_end({**new_payload, "completed": True})
        roots = [
            span
            for span in exporter.get_finished_spans()
            if span.name == "invoke_agent Hermes Agent"
        ]
        assert len(roots) == 2
        old_root = next(span for span in roots if span.attributes["hermes.turn.id"] == "old-turn")
        new_root = next(span for span in roots if span.attributes["hermes.turn.id"] == "new-turn")
        assert "galileo.session.id" not in old_root.attributes
        assert new_root.attributes["galileo.session.id"] == session_uuid
    finally:
        release_old_callback.set()
        release_first_call.set()
        if cancel_thread is not None:
            cancel_thread.join(timeout=1)
        runtime.shutdown()


@pytest.mark.integration
def test_child_span_cannot_reprovision_after_root_generation_is_evicted() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    release_target = threading.Event()
    target_call_entered = threading.Event()
    target_callback_entered = threading.Event()
    calls: list[str] = []
    target_session = "generation-consistency"
    target_key = _sid(target_session)
    other_key = _sid("generation-capacity-other")
    other_uuid = "69696969-6969-4969-8969-696969696969"

    class CapacityRaceGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            external_id = kwargs["external_id"]
            calls.append(external_id)
            if external_id == target_key and calls.count(target_key) == 1:
                target_call_entered.set()
                release_target.wait(timeout=2)
                raise RuntimeError("synthetic first-generation failure")
            if external_id == other_key:
                return other_uuid
            return "70707070-7070-4070-8070-707070707070"

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=2_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=CapacityRaceGalileoLogger,
    )
    assert runtime._native_sessions is not None
    manager = runtime._native_sessions
    manager._max_entries = 1
    payload = _base(target_session, "generation-turn")
    try:
        runtime.on_pre_llm_call({**payload, "user_message": "one generation per turn"})
        assert target_call_entered.wait(timeout=1)
        root = runtime._resolve_turn_locked(payload)
        assert root is not None
        root_generation = root.native_session_generation

        original_callback = manager._on_resolved

        def observed_callback(key: str, value: str | None, generation: int) -> None:
            if key == target_key and generation == root_generation:
                target_callback_entered.set()
            original_callback(key, value, generation)

        manager._on_resolved = observed_callback
        with runtime._lock:
            release_target.set()
            assert target_callback_entered.wait(timeout=1)
            assert manager.ensure(other_key).status == "pending"
            assert _wait_until(lambda: manager.lookup(other_key).status == "ready")
            manager.finalize(other_key)
            runtime.on_pre_tool_call(
                {
                    **payload,
                    "tool_call_id": "generation-tool",
                    "tool_name": "work",
                    "args": {},
                }
            )

        assert _wait_until(
            lambda: (
                (state := runtime._resolve_turn_locked(payload)) is not None
                and state.native_session_key == ""
            )
        )
        runtime.on_post_tool_call(
            {
                **payload,
                "tool_call_id": "generation-tool",
                "tool_name": "work",
                "status": "ok",
                "result": {},
            }
        )
        runtime.on_session_end({**payload, "completed": True})

        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert all("galileo.session.id" not in span.attributes for span in spans)
        assert calls == [target_key, other_key]
    finally:
        release_target.set()
        runtime.shutdown()


@pytest.mark.integration
def test_native_session_state_and_queue_are_bounded() -> None:
    entered = threading.Event()
    release = threading.Event()
    callbacks: list[tuple[str, str | None]] = []

    class BlockingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return "66666666-6666-4666-8666-666666666666"

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    manager = NativeSessionManager(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=1_000,
        ),
        client_factory=BlockingGalileoLogger,
        on_resolved=lambda key, value, _generation: callbacks.append((key, value)),
        max_entries=1,
        queue_capacity=1,
    )
    assert manager.ensure("hermes:first").status == "pending"
    assert entered.wait(timeout=1)
    assert manager.health_snapshot()["native_session_worker_calls_inflight"] == 1
    assert manager.ensure("hermes:first").status == "pending"
    assert manager.ensure("hermes:second").status == "failed"
    assert manager.health_snapshot()["native_session_capacity_rejections"] == 1

    manager.finalize("hermes:first")
    assert manager.health_snapshot()["native_session_mappings"] == 1
    assert callbacks == []
    release.set()
    assert manager.wait_until_idle(1)
    assert callbacks == [("hermes:first", "66666666-6666-4666-8666-666666666666")]
    assert manager.health_snapshot()["native_session_mappings"] == 0
    manager.shutdown(1)


@pytest.mark.integration
def test_native_session_ready_mapping_is_retained_until_lifecycle_release() -> None:
    callbacks: list[tuple[str, str | None]] = []

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            return "10101010-1010-4010-8010-101010101010"

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    manager = NativeSessionManager(
        _settings(native_sessions_enabled=True),
        client_factory=FakeGalileoLogger,
        on_resolved=lambda key, value, _generation: callbacks.append((key, value)),
        max_entries=1,
        queue_capacity=1,
    )
    assert manager.ensure("hermes:first").status == "pending"
    assert manager.wait_until_idle(1)
    assert manager.ensure("hermes:second").status == "failed"

    snapshot = manager.health_snapshot()
    assert snapshot["native_session_mapping_evictions"] == 0
    assert snapshot["native_session_capacity_rejections"] == 1
    assert snapshot["native_session_mappings"] == 1
    assert [key for key, _ in callbacks] == ["hermes:first"]
    manager.shutdown(1)


@pytest.mark.integration
def test_native_session_failed_mapping_eviction_is_observable() -> None:
    callbacks: list[tuple[str, str | None]] = []

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            if kwargs["external_id"] == "hermes:first":
                raise ConnectionError("synthetic first-session failure")
            return "20202020-2020-4020-8020-202020202020"

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    manager = NativeSessionManager(
        _settings(native_sessions_enabled=True),
        client_factory=FakeGalileoLogger,
        on_resolved=lambda key, value, _generation: callbacks.append((key, value)),
        max_entries=1,
        queue_capacity=1,
    )
    assert manager.ensure("hermes:first").status == "pending"
    assert manager.wait_until_idle(1)
    assert manager.ensure("hermes:second").status == "pending"
    assert manager.wait_until_idle(1)

    snapshot = manager.health_snapshot()
    assert snapshot["native_session_mapping_evictions"] == 1
    assert snapshot["native_session_capacity_rejections"] == 0
    assert snapshot["native_session_mappings"] == 1
    assert callbacks == [
        ("hermes:first", None),
        ("hermes:second", "20202020-2020-4020-8020-202020202020"),
    ]
    manager.shutdown(1)


@pytest.mark.integration
def test_native_session_capacity_rejection_stays_consistent_for_one_turn() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class BlockingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            calls.append(kwargs["external_id"])
            entered.set()
            release.wait(timeout=2)
            return "40404040-4040-4040-8040-404040404040"

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=2_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=BlockingGalileoLogger,
    )
    assert runtime._native_sessions is not None
    runtime._native_sessions._max_entries = 1
    try:
        occupier_key = _sid("capacity-occupier")
        assert runtime._native_sessions.ensure(occupier_key).status == "pending"
        assert entered.wait(timeout=1)

        turn = _base("capacity-rejected-session", "capacity-rejected-turn")
        runtime.on_pre_llm_call({**turn, "user_message": "stay fail-open"})
        root = runtime._resolve_turn_locked(turn)
        assert root is not None
        assert root.native_session_key == ""

        runtime._native_sessions.finalize(occupier_key)
        release.set()
        assert _wait_until(
            lambda: runtime._native_sessions.health_snapshot()["native_session_mappings"] == 0
        )

        runtime.on_pre_tool_call(
            {
                **turn,
                "tool_call_id": "capacity-tool",
                "tool_name": "search",
                "args": {"query": "consistency"},
            }
        )
        runtime.on_post_tool_call(
            {
                **turn,
                "tool_call_id": "capacity-tool",
                "tool_name": "search",
                "result": {"ok": True},
                "status": "ok",
            }
        )
        runtime.on_session_end({**turn, "completed": True})

        spans = exporter.get_finished_spans()
        assert len(spans) == 2
        assert {span.context.trace_id for span in spans} == {spans[0].context.trace_id}
        assert all("galileo.session.id" not in span.attributes for span in spans)
        assert calls == [occupier_key]
    finally:
        release.set()
        runtime.shutdown()


@pytest.mark.integration
def test_native_session_failure_stays_consistent_after_mapping_eviction() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    calls: list[str] = []
    target_session = "failed-turn-session"
    target_key = _sid(target_session)
    other_key = _sid("replacement-session")
    replacement_uuid = "50505050-5050-4050-8050-505050505050"

    class FakeGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            external_id = kwargs["external_id"]
            calls.append(external_id)
            if external_id == target_key:
                raise ConnectionError("synthetic target failure")
            return replacement_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    runtime = TelemetryRuntime(
        _settings(native_sessions_enabled=True),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=FakeGalileoLogger,
    )
    assert runtime._native_sessions is not None
    runtime._native_sessions._max_entries = 1
    try:
        turn = _base(target_session, "failed-turn")
        runtime.on_pre_llm_call({**turn, "user_message": "keep one decision"})
        assert _wait_until(
            lambda: (
                (state := runtime._resolve_turn_locked(turn)) is not None
                and state.native_session_key == ""
            )
        )

        runtime.on_pre_tool_call(
            {
                **turn,
                "tool_call_id": "before-eviction",
                "tool_name": "first",
                "args": {},
            }
        )
        runtime.on_post_tool_call(
            {
                **turn,
                "tool_call_id": "before-eviction",
                "tool_name": "first",
                "status": "ok",
                "result": {},
            }
        )

        assert runtime._native_sessions.ensure(other_key).status == "pending"
        assert runtime._native_sessions.wait_until_idle(1)
        assert runtime._native_sessions.lookup(other_key).status == "ready"
        runtime._native_sessions.finalize(other_key)

        runtime.on_pre_tool_call(
            {
                **turn,
                "tool_call_id": "after-eviction",
                "tool_name": "second",
                "args": {},
            }
        )
        runtime.on_post_tool_call(
            {
                **turn,
                "tool_call_id": "after-eviction",
                "tool_name": "second",
                "status": "ok",
                "result": {},
            }
        )
        runtime.on_session_end({**turn, "completed": True})

        spans = exporter.get_finished_spans()
        assert len(spans) == 3
        assert all("galileo.session.id" not in span.attributes for span in spans)
        assert calls == [target_key, other_key]
    finally:
        runtime.shutdown()


@pytest.mark.integration
def test_native_session_pending_span_buffer_overflow_fails_open(
    monkeypatch: Any,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    entered = threading.Event()
    release = threading.Event()
    session_uuid = "30303030-3030-4030-8030-303030303030"

    class BlockingGalileoLogger:
        def start_session(self, **kwargs: Any) -> str:
            entered.set()
            release.wait(timeout=2)
            return session_uuid

        def clear_session(self) -> None:
            return None

        def terminate(self) -> None:
            return None

    monkeypatch.setattr(TelemetryRuntime, "_MAX_DEFERRED_SPAN_ENDS", 1)
    runtime = TelemetryRuntime(
        _settings(
            native_sessions_enabled=True,
            native_session_timeout_millis=2_000,
        ),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
        native_session_client_factory=BlockingGalileoLogger,
    )
    try:
        first = _base("pending-overflow", "first")
        second = _base("pending-overflow", "second")
        runtime.on_pre_llm_call({**first, "user_message": "first"})
        runtime.on_session_end({**first, "completed": True})
        assert entered.wait(timeout=1)
        assert exporter.get_finished_spans() == ()

        runtime.on_pre_llm_call({**second, "user_message": "second"})
        runtime.on_session_end({**second, "completed": True})
        immediate = exporter.get_finished_spans()
        assert len(immediate) == 1
        assert "galileo.session.id" not in immediate[0].attributes
        snapshot = runtime.health_snapshot()
        assert snapshot["native_session_deferred_spans"] == 1
        assert snapshot["native_session_deferred_span_drops"] == 1

        release.set()
        assert _wait_until(lambda: len(exporter.get_finished_spans()) == 2)
        finished = exporter.get_finished_spans()
        assert (
            len(
                [
                    span
                    for span in finished
                    if span.attributes.get("galileo.session.id") == session_uuid
                ]
            )
            == 1
        )
    finally:
        release.set()
        runtime.shutdown()


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


@pytest.mark.integration
def test_runtime_drops_events_from_a_different_active_hermes_profile(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    processor = SimpleSpanProcessor(exporter)
    bound_home = (tmp_path / "profiles" / "bound").resolve()
    other_home = (tmp_path / "profiles" / "other").resolve()
    active_home = other_home
    monkeypatch.setattr(runtime_module, "active_hermes_home", lambda: active_home)
    runtime = TelemetryRuntime(
        _settings(hermes_home=str(bound_home)),
        tracer_provider=provider,
        span_processor=processor,
        start_async_flusher=False,
    )
    payload = _base("profile-session", "profile-turn")

    runtime.on_pre_llm_call({**payload, "user_message": "wrong profile"})
    runtime.on_session_end({**payload, "completed": True})
    assert exporter.get_finished_spans() == ()
    assert runtime.health_snapshot()["profile_scope_mismatches"] == 2
    assert runtime.health_snapshot()["profile_scope_enforced"] is True

    active_home = bound_home
    runtime.on_pre_llm_call({**payload, "user_message": "bound profile"})
    runtime.on_session_end({**payload, "completed": True})
    assert len(exporter.get_finished_spans()) == 1
    runtime.shutdown()
