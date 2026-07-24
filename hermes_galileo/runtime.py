"""Thread-safe Hermes observer state mapped to Galileo-compatible OTel spans."""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .config import Settings
from .privacy import (
    anonymize_identifier,
    captured_value,
    messages_json,
    request_messages_json,
    response_messages_json,
)

logger = logging.getLogger("hermes_galileo")

try:
    from galileo.config import GalileoPythonConfig
    from galileo.constants import DEFAULT_API_URL, DEFAULT_CONSOLE_URL
    from galileo.otel import GalileoSpanProcessor, add_galileo_span_processor
    from opentelemetry import trace
    from opentelemetry.context import Context
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.trace import SpanKind, Status, StatusCode

    _DEPENDENCIES_AVAILABLE = True
    _DEPENDENCY_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised without optional dependencies
    DEFAULT_API_URL = ""  # type: ignore[assignment]
    DEFAULT_CONSOLE_URL = ""  # type: ignore[assignment]
    GalileoPythonConfig = None  # type: ignore[assignment,misc]
    GalileoSpanProcessor = None  # type: ignore[assignment,misc]
    Context = None  # type: ignore[assignment,misc]
    Resource = None  # type: ignore[assignment,misc]
    TracerProvider = None  # type: ignore[assignment,misc]
    ParentBased = None  # type: ignore[assignment,misc]
    TraceIdRatioBased = None  # type: ignore[assignment,misc]
    SpanKind = None  # type: ignore[assignment,misc]
    Status = None  # type: ignore[assignment,misc]
    StatusCode = None  # type: ignore[assignment,misc]
    trace = None  # type: ignore[assignment]
    add_galileo_span_processor = None  # type: ignore[assignment]
    _DEPENDENCIES_AVAILABLE = False
    _DEPENDENCY_ERROR = exc


class RuntimeInitializationError(RuntimeError):
    """Raised when the telemetry runtime cannot be initialized."""


class _PermanentGalileoConfigurationError(RuntimeError):
    """A process-global Galileo SDK configuration cannot be retried safely."""


class _DeferredGalileoSpanProcessor:
    """Buffer completed spans while the official Galileo SDK connects.

    Galileo's SDK performs health, login, and current-user requests in its
    processor constructor. This wrapper keeps those requests off Hermes'
    observer path, retains a bounded startup buffer, and retries transient
    initialization failures without blocking agent work.
    """

    _MAX_BUFFERED_SPANS = 2_048
    _RETRY_DELAYS_SECONDS = (1.0, 5.0, 30.0, 60.0)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._stopping = threading.Event()
        self._state_changed = threading.Event()
        self._state = "connecting"
        self._delegate: Any | None = None
        self._buffer: deque[Any] = deque()
        self._dropped_spans = 0
        self._attempts = 0
        self._last_error_type = ""
        self._last_error_retryable: bool | None = None
        self._retry_stopped_reason = ""
        self._delegate_cleanup_deferred = False
        self._delegate_cleanup_thread: threading.Thread | None = None
        self._thread = threading.Thread(
            target=self._connect_loop,
            name="hermes-galileo-connect",
            daemon=True,
        )
        self._thread.start()

    def _stamp_routing(self, span: Any) -> None:
        try:
            span.set_attribute("galileo.project.name", self._settings.project)
            span.set_attribute("galileo.logstream.name", self._settings.log_stream)
        except Exception:
            logger.debug("Could not stamp Galileo routing attributes", exc_info=True)

    def on_start(self, span: Any, parent_context: Any | None = None) -> None:
        self._stamp_routing(span)
        with self._lock:
            delegate = self._delegate if self._state == "ready" else None
        if delegate is not None:
            delegate.on_start(span, parent_context)

    def _on_ending(self, span: Any) -> None:
        """OpenTelemetry 1.44 pre-end hook; no mutation is needed here."""

    def on_end(self, span: Any) -> None:
        with self._lock:
            delegate = self._delegate if self._state == "ready" else None
            if delegate is None:
                if self._state in {"failed", "stopping", "stopped"}:
                    self._dropped_spans += 1
                    return
                if len(self._buffer) >= self._MAX_BUFFERED_SPANS:
                    self._buffer.popleft()
                    self._dropped_spans += 1
                self._buffer.append(span)
                return
        try:
            delegate.on_end(span)
        except Exception:
            logger.warning("Galileo span enqueue failed", exc_info=True)

    @staticmethod
    def _normalized_endpoint(value: Any) -> str:
        return str(value or "").strip().rstrip("/")

    def _validate_existing_sdk_configuration(
        self,
        *,
        reject_unpinned_custom_api: bool,
    ) -> None:
        """Reject a conflicting process-global SDK singleton without resetting it."""

        existing = getattr(GalileoPythonConfig, "_instance", None)
        if existing is None:
            return
        existing_key = getattr(existing, "api_key", None)
        if hasattr(existing_key, "get_secret_value"):
            existing_key = existing_key.get_secret_value()
        if not existing_key or str(existing_key) != self._settings.api_key:
            raise _PermanentGalileoConfigurationError(
                "the existing Galileo SDK singleton cannot use the configured API key"
            )
        existing_console = self._normalized_endpoint(getattr(existing, "console_url", ""))
        expected_console = self._normalized_endpoint(
            self._settings.console_url or DEFAULT_CONSOLE_URL
        )
        if not existing_console or existing_console != expected_console:
            raise _PermanentGalileoConfigurationError(
                "the existing Galileo SDK singleton uses a different console URL"
            )

        existing_api = self._normalized_endpoint(getattr(existing, "api_url", ""))
        expected_api = self._normalized_endpoint(
            self._settings.api_url
            or (DEFAULT_API_URL if self._settings.console_url is None else "")
        )
        if expected_api:
            if not existing_api or existing_api != expected_api:
                raise _PermanentGalileoConfigurationError(
                    "the existing Galileo SDK singleton uses a different API URL"
                )
        elif reject_unpinned_custom_api:
            # A pre-existing custom deployment can use a separate API host.
            # Require an explicit pin so this plugin cannot silently inherit a
            # route selected by unrelated Galileo instrumentation.
            raise _PermanentGalileoConfigurationError(
                "GALILEO_API_URL is required to validate a pre-existing custom "
                "Galileo SDK singleton"
            )

    @staticmethod
    def _connection_status_code(exc: Exception) -> int | None:
        status_code = _integer(getattr(exc, "status_code", None))
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = _integer(getattr(response, "status_code", None))
        return status_code

    @classmethod
    def _is_retryable_connection_error(cls, exc: Exception) -> bool:
        if isinstance(exc, _PermanentGalileoConfigurationError):
            return False
        status_code = cls._connection_status_code(exc)
        if status_code is None:
            # Connection, timeout, DNS, and SDK bootstrap failures do not
            # consistently share one exception hierarchy.
            return not isinstance(exc, (ImportError, TypeError, ValueError))
        return status_code in {408, 429} or status_code >= 500

    @staticmethod
    def _terminal_reason(exc: Exception) -> str:
        if isinstance(exc, _PermanentGalileoConfigurationError):
            return "sdk_configuration_conflict"
        status_code = _DeferredGalileoSpanProcessor._connection_status_code(exc)
        return f"permanent_http_{status_code}" if status_code is not None else "permanent_error"

    def _connect_loop(self) -> None:
        retry_index = 0
        while not self._stopping.is_set():
            with self._lock:
                self._attempts += 1
            processor: Any | None = None
            try:
                had_existing_config = getattr(GalileoPythonConfig, "_instance", None) is not None
                self._validate_existing_sdk_configuration(
                    reject_unpinned_custom_api=had_existing_config,
                )
                processor = GalileoSpanProcessor(
                    project=self._settings.project,
                    logstream=self._settings.log_stream,
                    timeout=self._settings.flush_timeout_millis / 1_000,
                )
                # Close the validation-to-construction race. The exporter has
                # now captured this singleton's API endpoint.
                self._validate_existing_sdk_configuration(
                    reject_unpinned_custom_api=False,
                )
            except Exception as exc:
                if processor is not None:
                    try:
                        processor.shutdown()
                    except Exception:
                        logger.warning(
                            "Could not close Galileo processor after routing validation failed",
                            exc_info=True,
                        )
                retryable = self._is_retryable_connection_error(exc)
                with self._lock:
                    if self._stopping.is_set():
                        return
                    self._last_error_type = type(exc).__name__
                    self._last_error_retryable = retryable
                    if not retryable:
                        self._retry_stopped_reason = self._terminal_reason(exc)
                        self._state = "failed"
                        buffered_count = len(self._buffer)
                        self._dropped_spans += buffered_count
                        self._buffer.clear()
                        self._state_changed.set()
                if not retryable:
                    logger.warning(
                        "Galileo SDK connection failed permanently; telemetry "
                        "export is disabled for this process: %s",
                        type(exc).__name__,
                    )
                    return
                if retry_index == 0:
                    logger.warning(
                        "Galileo SDK connection failed; telemetry is buffered and "
                        "connection will be retried: %s",
                        type(exc).__name__,
                    )
                else:
                    logger.debug("Galileo SDK reconnect failed", exc_info=True)
                delay = self._RETRY_DELAYS_SECONDS[
                    min(retry_index, len(self._RETRY_DELAYS_SECONDS) - 1)
                ]
                retry_index += 1
                self._stopping.wait(delay * random.uniform(0.8, 1.2))
                continue

            with self._lock:
                if self._stopping.is_set():
                    should_shutdown = True
                else:
                    self._delegate = processor
                    self._state = "replaying"
                    self._last_error_type = ""
                    self._last_error_retryable = None
                    should_shutdown = False
            if should_shutdown:
                processor.shutdown()
                return

            replayed = 0
            while True:
                with self._lock:
                    if self._stopping.is_set():
                        remaining = len(self._buffer)
                        self._dropped_spans += remaining
                        self._buffer.clear()
                        should_shutdown = True
                        batch: list[Any] = []
                    elif self._buffer:
                        batch = list(self._buffer)
                        self._buffer.clear()
                        should_shutdown = False
                    else:
                        self._state = "ready"
                        self._state_changed.set()
                        logger.info(
                            "Galileo SDK connected; replayed %d buffered spans",
                            replayed,
                        )
                        return
                if should_shutdown:
                    processor.shutdown()
                    return
                for buffered_span in batch:
                    try:
                        processor.on_end(buffered_span)
                        replayed += 1
                    except Exception:
                        with self._lock:
                            self._dropped_spans += 1
                        logger.warning("Could not replay buffered Galileo span", exc_info=True)

    def force_flush(self, timeout_millis: int = 40_000) -> bool:
        deadline = time.monotonic() + max(timeout_millis, 0) / 1_000
        with self._lock:
            state = self._state
        if state in {"connecting", "replaying"}:
            self._state_changed.wait(max(deadline - time.monotonic(), 0))
        with self._lock:
            delegate = self._delegate if self._state == "ready" else None
        if delegate is None or time.monotonic() >= deadline:
            return False
        try:
            remaining_seconds = max(deadline - time.monotonic(), 0)
            if not self._operation_lock.acquire(timeout=remaining_seconds):
                return False
            try:
                # Shutdown may have changed state after the optimistic read
                # above while this call waited for another SDK operation.
                with self._lock:
                    if self._state != "ready" or self._delegate is not delegate:
                        return False
                remaining_millis = max(int((deadline - time.monotonic()) * 1_000), 1)
                result = delegate.force_flush(remaining_millis)
            finally:
                self._operation_lock.release()
            return True if result is None else bool(result)
        except Exception:
            logger.warning("Galileo force_flush failed", exc_info=True)
            return False

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "exporter_ready": self._state == "ready",
                "exporter_state": self._state,
                "buffered_spans": len(self._buffer),
                "dropped_spans": self._dropped_spans,
                "connection_attempts": self._attempts,
                "last_connection_error_type": self._last_error_type,
                "last_connection_error_retryable": self._last_error_retryable,
                "retry_stopped_reason": self._retry_stopped_reason,
                "connector_cleanup_deferred": (
                    self._state == "stopped" and self._thread.is_alive()
                ),
                "delegate_cleanup_deferred": self._delegate_cleanup_deferred,
            }

    def wait_until_ready(self, timeout_seconds: float) -> bool:
        with self._lock:
            if self._state == "ready":
                return True
            if self._state in {"failed", "stopping", "stopped"}:
                return False
        self._state_changed.wait(max(timeout_seconds, 0))
        with self._lock:
            return self._state == "ready"

    def _shutdown_delegate_after_operations(self, delegate: Any) -> None:
        try:
            with self._operation_lock:
                delegate.shutdown()
        except Exception:
            logger.warning("Galileo span processor shutdown failed", exc_info=True)
        finally:
            with self._lock:
                self._delegate_cleanup_deferred = False

    def shutdown(self) -> None:
        # Give an in-progress SDK login a bounded chance to finish so
        # short-lived CLI/cron turns can replay their startup buffer.
        timeout_seconds = self._settings.flush_timeout_millis / 1_000
        deadline = time.monotonic() + timeout_seconds
        self.wait_until_ready(timeout_seconds)
        with self._lock:
            prior_state = self._state
            self._stopping.set()
            self._state = "stopping"
            self._state_changed.set()
            delegate = self._delegate if prior_state == "ready" else None
            buffered_count = len(self._buffer)
            self._dropped_spans += buffered_count
            self._buffer.clear()
        if self._thread.is_alive():
            self._thread.join(timeout=max(deadline - time.monotonic(), 0))
        if delegate is not None:
            with self._lock:
                self._delegate_cleanup_deferred = True
            self._delegate_cleanup_thread = threading.Thread(
                target=self._shutdown_delegate_after_operations,
                args=(delegate,),
                name="hermes-galileo-sdk-cleanup",
                daemon=True,
            )
            self._delegate_cleanup_thread.start()
            self._delegate_cleanup_thread.join(timeout=max(deadline - time.monotonic(), 0))
            if self._delegate_cleanup_thread.is_alive():
                logger.warning(
                    "Galileo SDK shutdown exceeded the deadline and will finish asynchronously"
                )
        elif buffered_count:
            logger.warning(
                "Dropped %d buffered Galileo spans because the SDK did not "
                "connect before the shutdown deadline",
                buffered_count,
            )
        with self._lock:
            self._state = "stopped"


@dataclass(slots=True)
class _TurnState:
    key: str
    session_id: str
    turn_id: str
    task_id: str
    span: Any
    started_monotonic: float
    last_updated_monotonic: float
    model: str = ""
    provider: str = ""
    output: Any = None
    api_count: int = 0
    tool_count: int = 0
    error_count: int = 0
    tool_names: set[str] = field(default_factory=set)
    api_attempts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _ChildState:
    key: str
    category: str
    identity: str
    root_key: str
    span: Any
    started_monotonic: float


@dataclass(slots=True)
class _DelegationState:
    child_session_id: str
    parent_root_key: str
    span: Any
    role: str
    started_monotonic: float


class _AsyncFlusher:
    """Coalesce turn-end flush requests on a background thread."""

    def __init__(self, flush_callback: Any) -> None:
        self._flush_callback = flush_callback
        self._requested = threading.Event()
        self._stopping = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="hermes-galileo-flush",
            daemon=True,
        )
        self._thread.start()

    def request(self) -> None:
        self._requested.set()

    def _run(self) -> None:
        while True:
            self._requested.wait(timeout=1.0)
            if self._stopping.is_set():
                return
            if not self._requested.is_set():
                continue
            self._requested.clear()
            try:
                self._flush_callback()
            except Exception:
                logger.warning("Galileo background flush failed", exc_info=True)

    def shutdown(self, timeout_seconds: float = 5.0) -> bool:
        self._stopping.set()
        self._requested.set()
        self._thread.join(timeout=max(timeout_seconds, 0))
        return not self._thread.is_alive()

    def wait_until_stopped(self) -> None:
        self._thread.join()


def _package_version() -> str:
    try:
        return version("hermes-galileo")
    except PackageNotFoundError:
        return "0.1.0"


def _string(value: Any, limit: int = 500) -> str:
    try:
        text = str(value or "").strip()
    except Exception:
        return ""
    return text[:limit]


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_url(value: Any) -> str:
    """Keep endpoint identity while dropping query strings and credentials."""

    text = _string(value, 2_000)
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        hostname = parts.hostname or ""
        if parts.port:
            hostname = f"{hostname}:{parts.port}"
        return urlunsplit((parts.scheme, hostname, parts.path, "", ""))
    except Exception:
        return text.split("?", 1)[0][:500]


def _attribute_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        cleaned = [
            item if isinstance(item, (str, bool, int, float)) else str(item)
            for item in value
            if item is not None
        ]
        return cleaned
    return str(value)


def _attributes(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: normalized
        for key, value in values.items()
        if (normalized := _attribute_value(value)) is not None and normalized != ""
    }


def _usage_attributes(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    # Hermes' prompt_tokens includes uncached input plus cache reads/writes;
    # input_tokens is only the uncached portion.
    input_tokens = _integer(usage.get("prompt_tokens"))
    if input_tokens is None:
        input_tokens = _integer(usage.get("input_tokens"))
    output_tokens = _integer(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _integer(usage.get("completion_tokens"))
    total_tokens = _integer(usage.get("total_tokens"))
    if total_tokens is None and (input_tokens is not None or output_tokens is not None):
        total_tokens = (input_tokens or 0) + (output_tokens or 0)

    values: dict[str, Any] = {
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.total_tokens": total_tokens,
        # OpenInference compatibility remains useful for Galileo and other
        # processors attached to the same private provider.
        "llm.token_count.prompt": input_tokens,
        "llm.token_count.completion": output_tokens,
        "llm.token_count.total": total_tokens,
    }
    cache_read = _integer(usage.get("cache_read_tokens"))
    cache_write = _integer(usage.get("cache_write_tokens"))
    reasoning = _integer(usage.get("reasoning_tokens"))
    if cache_read is not None:
        values["gen_ai.usage.cache_read.input_tokens"] = cache_read
    if cache_write is not None:
        values["gen_ai.usage.cache_creation.input_tokens"] = cache_write
    if reasoning is not None:
        values["gen_ai.usage.reasoning.output_tokens"] = reasoning
    cost = _float(usage.get("cost"))
    if cost is not None:
        values["hermes.usage.cost"] = cost
    return _attributes(values)


def _error_type(value: Any, fallback: str) -> str:
    """Bound ``error.type`` to a low-cardinality identifier."""

    text = _string(value, 200)
    if not text:
        return fallback
    normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", text).strip("_")
    if not normalized or len(normalized) > 100:
        return fallback
    return normalized


def _while_accepting_events(method: Any) -> Any:
    """Serialize hook admission with shutdown and reject late callbacks."""

    @wraps(method)
    def guarded(self: TelemetryRuntime, payload: dict[str, Any]) -> None:
        with self._lock:
            if self._shutdown:
                return
            method(self, payload)

    return guarded


class TelemetryRuntime:
    """Owns an isolated OTel provider and reconstructs concurrent Hermes turns."""

    _MAX_INFLIGHT_TURNS = 512

    def __init__(
        self,
        settings: Settings,
        *,
        tracer_provider: Any | None = None,
        span_processor: Any | None = None,
        start_async_flusher: bool = True,
    ) -> None:
        if not settings.enabled:
            raise RuntimeInitializationError("hermes-galileo is disabled")
        missing = settings.missing_required()
        if missing:
            raise RuntimeInitializationError(
                "missing required configuration: " + ", ".join(missing)
            )
        if not _DEPENDENCIES_AVAILABLE:
            raise RuntimeInitializationError(
                "Galileo/OpenTelemetry dependencies are unavailable; "
                'install with `pip install -e ".[dev]"` or `pip install "galileo[otel]"`: '
                f"{_DEPENDENCY_ERROR}"
            )

        self.settings = settings
        self._lock = threading.RLock()
        self._turns: dict[str, _TurnState] = {}
        self._active_by_session: dict[str, str] = {}
        self._children: dict[str, _ChildState] = {}
        self._delegations: dict[str, _DelegationState] = {}
        self._session_metadata: dict[str, dict[str, Any]] = {}
        self._shutdown = False
        self._provider_cleanup_deferred = False
        self._provider_cleanup_thread: threading.Thread | None = None
        self._stats = {
            "turns_started": 0,
            "turns_finished": 0,
            "spans_started": 0,
            "spans_finished": 0,
            "orphaned_spans": 0,
            "initialization_errors": 0,
        }

        self._owns_provider = tracer_provider is None
        if tracer_provider is None:
            resource = Resource.create(
                {
                    "service.name": settings.service_name,
                    "service.version": _package_version(),
                    "deployment.environment.name": settings.environment,
                    "telemetry.sdk.language": "python",
                    "hermes.plugin.name": "hermes-galileo",
                }
            )
            sampler = ParentBased(TraceIdRatioBased(settings.sample_rate))
            tracer_provider = TracerProvider(resource=resource, sampler=sampler)
            span_processor = _DeferredGalileoSpanProcessor(settings)
            # The official SDK helper performs the supported provider
            # registration and keeps Galileo's OTel context consistent while
            # the wrapper connects the official processor off-thread.
            add_galileo_span_processor(tracer_provider, span_processor)
        elif span_processor is not None:
            # Injection is used by embedders and tests. Supplying a processor
            # means the runtime owns attaching it to the supplied provider.
            tracer_provider.add_span_processor(span_processor)

        self._provider = tracer_provider
        self._processor = span_processor
        self._tracer = tracer_provider.get_tracer(
            "hermes-galileo",
            _package_version(),
        )
        self._flusher = (
            _AsyncFlusher(self.force_flush)
            if start_async_flusher and settings.async_flush_on_turn_end
            else None
        )

    # ------------------------------------------------------------------
    # Span primitives and state lookup
    # ------------------------------------------------------------------

    def _start_span(
        self,
        name: str,
        *,
        parent: Any | None,
        kind: Any,
        attributes: dict[str, Any],
    ) -> Any:
        base_context = Context()
        context = (
            trace.set_span_in_context(parent, base_context) if parent is not None else base_context
        )
        span = self._tracer.start_span(
            name,
            context=context,
            kind=kind,
            attributes=_attributes(attributes),
        )
        self._stats["spans_started"] += 1
        return span

    def _set_attributes(self, span: Any, values: dict[str, Any]) -> None:
        for key, value in _attributes(values).items():
            try:
                span.set_attribute(key, value)
            except Exception:
                logger.debug("Could not set span attribute %s", key, exc_info=True)

    def _end_span(
        self,
        span: Any,
        *,
        values: dict[str, Any] | None = None,
        error_type: str = "",
        error_message: str = "",
    ) -> None:
        if values:
            self._set_attributes(span, values)
        try:
            if error_type:
                normalized_error_type = _error_type(error_type, "telemetry_error")
                self._set_attributes(span, {"error.type": normalized_error_type})
                # Keep the status description useful to Galileo without
                # copying an exception message that may contain credentials
                # or user content.
                span.set_status(Status(StatusCode.ERROR, normalized_error_type))
                if self.settings.capture_content and error_message:
                    span.add_event(
                        "exception",
                        _attributes(
                            {
                                "exception.type": normalized_error_type,
                                "exception.message": captured_value(
                                    error_message,
                                    self.settings,
                                ),
                                "exception.escaped": True,
                            }
                        ),
                    )
            span.end()
            self._stats["spans_finished"] += 1
        except Exception:
            logger.warning("Could not end Galileo span", exc_info=True)

    @staticmethod
    def _scope(payload: dict[str, Any]) -> tuple[str, str, str]:
        session_id = _string(payload.get("session_id"), 500)
        task_id = _string(payload.get("task_id"), 500)
        turn_id = _string(payload.get("turn_id"), 500)
        scope = session_id or task_id or f"thread-{threading.get_ident()}"
        return scope, session_id, turn_id

    def _explicit_turn_key(self, payload: dict[str, Any]) -> str:
        scope, _, turn_id = self._scope(payload)
        if turn_id:
            return f"{scope}:turn:{turn_id}"
        return ""

    def _resolve_turn_locked(self, payload: dict[str, Any]) -> _TurnState | None:
        _, session_id, turn_id = self._scope(payload)
        task_id = _string(payload.get("task_id"), 500)

        # A supplied session is a hard correlation boundary. Never attach its
        # events to another session merely because only one turn is active.
        if session_id:
            if turn_id:
                return self._turns.get(f"{session_id}:turn:{turn_id}")
            active_key = self._active_by_session.get(session_id)
            active = self._turns.get(active_key) if active_key else None
            if task_id:
                if active is not None and active.task_id == task_id:
                    return active
                return None
            return active

        # Sessionless approval and compatibility hooks can still correlate by
        # globally unique turn/task IDs, but only when the match is unambiguous.
        if turn_id:
            candidates = [state for state in self._turns.values() if state.turn_id == turn_id]
            return candidates[0] if len(candidates) == 1 else None
        if task_id:
            candidates = [state for state in self._turns.values() if state.task_id == task_id]
            return candidates[0] if len(candidates) == 1 else None

        # The sole-turn heuristic is reserved for legacy payloads that carry
        # no correlation fields at all.
        if len(self._turns) == 1:
            return next(iter(self._turns.values()))
        return None

    def _turn_input(self, payload: dict[str, Any]) -> Any:
        if self.settings.capture_conversation_history and payload.get("conversation_history"):
            return payload["conversation_history"]
        return payload.get("user_message") or payload.get("input") or ""

    def _begin_turn_locked(self, payload: dict[str, Any], *, synthesized: bool) -> _TurnState:
        scope, session_id, turn_id = self._scope(payload)
        task_id = _string(payload.get("task_id"), 500)
        key = f"{scope}:turn:{turn_id}" if turn_id else f"{scope}:active"

        existing = self._turns.get(key)
        if existing is not None:
            existing.last_updated_monotonic = time.monotonic()
            return existing

        if session_id:
            old_key = self._active_by_session.get(session_id)
            if old_key and old_key in self._turns:
                self._finish_turn_locked(
                    old_key,
                    output=None,
                    final_status="superseded",
                    error_type="superseded",
                    error_message="A new Hermes turn started before the previous turn ended",
                    request_flush=False,
                )

        if len(self._turns) >= self._MAX_INFLIGHT_TURNS:
            oldest = min(self._turns.values(), key=lambda state: state.last_updated_monotonic)
            self._finish_turn_locked(
                oldest.key,
                output=None,
                final_status="evicted",
                error_type="state_capacity_exceeded",
                error_message="In-flight turn state capacity exceeded",
                request_flush=False,
            )

        session_metadata = self._session_metadata.get(session_id, {})
        model = _string(payload.get("model") or session_metadata.get("model"), 300)
        provider = _string(
            payload.get("provider") or session_metadata.get("provider") or "hermes",
            200,
        )
        agent_name = _string(payload.get("agent_name") or "Hermes Agent", 200)
        input_value = self._turn_input(payload)
        captured_input = captured_value(input_value, self.settings)
        sender_id = anonymize_identifier(
            payload.get("sender_id") or session_metadata.get("sender_id"),
            enabled=self.settings.hash_user_ids,
            secret=self.settings.pseudonym_secret or self.settings.api_key,
        )
        parent = self._delegations.get(session_id).span if session_id in self._delegations else None

        attributes = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.provider.name": provider,
            "gen_ai.agent.name": agent_name,
            "gen_ai.request.model": model,
            "gen_ai.conversation.id": session_id,
            "gen_ai.input.messages": messages_json("user", input_value, self.settings),
            "input.value": captured_input,
            "input.mime_type": "text/plain",
            "openinference.span.kind": "AGENT",
            "hermes.session.id": session_id,
            "hermes.turn.id": turn_id,
            "hermes.task.id": task_id,
            "hermes.platform": payload.get("platform") or session_metadata.get("platform"),
            "hermes.telemetry.schema_version": payload.get("telemetry_schema_version"),
            "hermes.turn.synthesized": synthesized,
            "user.id": sender_id,
        }
        span = self._start_span(
            f"invoke_agent {agent_name}",
            parent=parent,
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        )
        now = time.monotonic()
        state = _TurnState(
            key=key,
            session_id=session_id,
            turn_id=turn_id,
            task_id=task_id,
            span=span,
            started_monotonic=now,
            last_updated_monotonic=now,
            model=model,
            provider=provider,
        )
        self._turns[key] = state
        if session_id:
            self._active_by_session[session_id] = key
        self._stats["turns_started"] += 1
        return state

    def _update_root_model_provider_locked(
        self,
        root: _TurnState,
        *,
        model: str,
        provider: str,
    ) -> None:
        """Enrich an Agent root with provider data learned at the API boundary.

        Hermes' canonical ``pre_llm_call`` currently carries ``platform``
        (for example ``cli`` or ``gateway``), but the actual model provider is
        first available on ``pre_api_request``. Keep those concepts separate
        and make the first concrete API provider authoritative for the turn.
        """

        values: dict[str, Any] = {}
        if model and not root.model:
            root.model = model
            values["gen_ai.request.model"] = model
        if provider and provider != "unknown" and root.provider in {"", "hermes", "unknown"}:
            root.provider = provider
            values["gen_ai.provider.name"] = provider
        if values:
            self._set_attributes(root.span, values)

    def _ensure_turn_locked(
        self, payload: dict[str, Any], *, synthesized: bool = True
    ) -> _TurnState:
        state = self._resolve_turn_locked(payload)
        if state is not None:
            state.last_updated_monotonic = time.monotonic()
            return state
        return self._begin_turn_locked(payload, synthesized=synthesized)

    @staticmethod
    def _api_identity(payload: dict[str, Any]) -> str:
        explicit = _string(payload.get("api_request_id"), 500)
        if explicit:
            return explicit
        task_id = _string(payload.get("task_id"), 300)
        call_count = _string(payload.get("api_call_count"), 40)
        return f"{task_id}:{call_count}" if task_id or call_count else f"api-{time.monotonic_ns()}"

    @staticmethod
    def _tool_identity(payload: dict[str, Any]) -> str:
        explicit = _string(payload.get("tool_call_id"), 500)
        if explicit:
            return explicit
        task_id = _string(payload.get("task_id"), 300)
        tool_name = _string(payload.get("tool_name"), 200)
        return f"{task_id}:{tool_name}" if task_id or tool_name else f"tool-{time.monotonic_ns()}"

    @staticmethod
    def _approval_identity(payload: dict[str, Any]) -> str:
        tool_call_id = _string(payload.get("tool_call_id"), 500)
        if tool_call_id:
            return tool_call_id
        fallback = ":".join(
            filter(
                None,
                (
                    _string(payload.get("session_key"), 200),
                    _string(payload.get("pattern_key"), 200),
                    _string(payload.get("command"), 200),
                ),
            )
        )
        return fallback or f"approval-{time.monotonic_ns()}"

    def _child_key(self, root_key: str, category: str, identity: str) -> str:
        return f"{root_key}|{category}|{identity}"

    @staticmethod
    def _record_api_attempt_locked(
        root: _TurnState,
        identity: str,
        *,
        starting: bool,
        reported_retry_count: int | None = None,
    ) -> int:
        """Return a stable one-based attempt ordinal for one logical request."""

        current = root.api_attempts.get(identity, 0)
        if identity not in root.api_attempts:
            root.api_count += 1
        if starting:
            attempt = current + 1
        elif reported_retry_count is not None and reported_retry_count >= 0:
            attempt = max(current, reported_retry_count + 1)
        else:
            attempt = max(current, 1)
        root.api_attempts[identity] = attempt
        return attempt

    def _start_child_locked(
        self,
        payload: dict[str, Any],
        *,
        category: str,
        identity: str,
        name: str,
        kind: Any,
        values: dict[str, Any],
        parent_span: Any | None = None,
    ) -> _ChildState:
        root = self._ensure_turn_locked(payload)
        key = self._child_key(root.key, category, identity)
        existing = self._children.pop(key, None)
        if existing is not None:
            self._end_span(
                existing.span,
                error_type="duplicate_start",
                error_message=f"Duplicate {category} start event",
            )
            self._stats["orphaned_spans"] += 1
        span = self._start_span(
            name,
            parent=parent_span if parent_span is not None else root.span,
            kind=kind,
            attributes=values,
        )
        child = _ChildState(
            key=key,
            category=category,
            identity=identity,
            root_key=root.key,
            span=span,
            started_monotonic=time.monotonic(),
        )
        self._children[key] = child
        root.last_updated_monotonic = time.monotonic()
        return child

    def _find_child_locked(
        self,
        payload: dict[str, Any],
        *,
        category: str,
        identity: str,
    ) -> _ChildState | None:
        root = self._resolve_turn_locked(payload)
        if root is not None:
            child = self._children.get(self._child_key(root.key, category, identity))
            if child is not None:
                return child
        _, session_id, turn_id = self._scope(payload)
        task_id = _string(payload.get("task_id"), 500)
        if session_id or turn_id or task_id:
            return None
        matches = [
            child
            for child in self._children.values()
            if child.category == category and child.identity == identity
        ]
        return matches[0] if len(matches) == 1 else None

    def _finish_child_locked(
        self,
        payload: dict[str, Any],
        *,
        category: str,
        identity: str,
        fallback_name: str,
        fallback_kind: Any,
        fallback_values: dict[str, Any],
        final_values: dict[str, Any],
        error_type: str = "",
        error_message: str = "",
    ) -> _ChildState:
        child = self._find_child_locked(payload, category=category, identity=identity)
        if child is None:
            child = self._start_child_locked(
                payload,
                category=category,
                identity=identity,
                name=fallback_name,
                kind=fallback_kind,
                values={**fallback_values, "hermes.span.synthesized": True},
            )
        self._children.pop(child.key, None)
        self._end_span(
            child.span,
            values=final_values,
            error_type=error_type,
            error_message=error_message,
        )
        root = self._turns.get(child.root_key)
        if root is not None:
            root.last_updated_monotonic = time.monotonic()
            if error_type:
                root.error_count += 1
        return child

    # ------------------------------------------------------------------
    # Hermes hook handlers
    # ------------------------------------------------------------------

    @_while_accepting_events
    def on_session_start(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            session_id = _string(payload.get("session_id"), 500)
            if session_id:
                self._session_metadata[session_id] = {
                    "model": payload.get("model"),
                    "platform": payload.get("platform"),
                    "sender_id": payload.get("sender_id"),
                }

    @_while_accepting_events
    def on_pre_llm_call(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            self._begin_turn_locked(payload, synthesized=False)

    @_while_accepting_events
    def on_post_llm_call(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            state = self._ensure_turn_locked(payload)
            output = payload.get("assistant_response")
            if output is None:
                output = payload.get("output")
            state.output = output
            self._set_attributes(
                state.span,
                {
                    "gen_ai.output.messages": messages_json("assistant", output, self.settings),
                    "output.value": captured_value(output, self.settings),
                    "output.mime_type": "text/plain",
                    "gen_ai.response.model": payload.get("model") or state.model,
                },
            )
            state.last_updated_monotonic = time.monotonic()

    @_while_accepting_events
    def on_pre_api_request(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            identity = self._api_identity(payload)
            root = self._ensure_turn_locked(payload)
            attempt = self._record_api_attempt_locked(root, identity, starting=True)
            model = _string(payload.get("model"), 300) or "unknown"
            provider = _string(payload.get("provider") or payload.get("platform") or "unknown", 200)
            request = payload.get("request")
            if request is None:
                request = payload.get("request_messages") or payload.get("conversation_history")
            if self.settings.capture_conversation_history:
                captured_request = captured_value(request, self.settings)
                input_messages = request_messages_json(request, self.settings)
            else:
                # Canonical Hermes provider requests contain the accumulated
                # conversation. Content opt-in alone must not broaden into
                # history capture; actual hooks also provide the current user
                # message separately.
                current_input = payload.get("user_message")
                if current_input is None:
                    current_input = payload.get("input") or ""
                captured_request = captured_value(current_input, self.settings)
                input_messages = messages_json("user", current_input, self.settings)
            values: dict[str, Any] = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model,
                "gen_ai.conversation.id": payload.get("session_id"),
                "gen_ai.input.messages": input_messages,
                "input.value": captured_request,
                "input.mime_type": "text/plain",
                "openinference.span.kind": "LLM",
                "hermes.api.request_id": identity,
                "hermes.api.attempt": attempt,
                "hermes.api.call_count": _integer(payload.get("api_call_count")),
                "hermes.api.mode": payload.get("api_mode"),
                "hermes.api.message_count": _integer(payload.get("message_count")),
                "hermes.api.tool_count": _integer(payload.get("tool_count")),
                "hermes.api.request_char_count": _integer(payload.get("request_char_count")),
                "server.address": _safe_url(payload.get("base_url")),
            }
            for source, target in (
                ("max_tokens", "gen_ai.request.max_tokens"),
                ("temperature", "gen_ai.request.temperature"),
                ("top_p", "gen_ai.request.top_p"),
            ):
                if payload.get(source) is not None:
                    values[target] = payload[source]
            child = self._start_child_locked(
                payload,
                category="api",
                identity=identity,
                name=f"chat {model}",
                kind=SpanKind.CLIENT,
                values=values,
            )
            root = self._turns.get(child.root_key)
            if root is not None:
                self._update_root_model_provider_locked(
                    root,
                    model=model,
                    provider=provider,
                )

    @_while_accepting_events
    def on_post_api_request(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            identity = self._api_identity(payload)
            root = self._ensure_turn_locked(payload)
            attempt = self._record_api_attempt_locked(root, identity, starting=False)
            model = _string(payload.get("model"), 300) or "unknown"
            provider = _string(payload.get("provider") or payload.get("platform") or "unknown", 200)
            response = payload.get("response")
            if response is None:
                response = payload.get("assistant_message") or payload.get("response_content")
            duration = _float(payload.get("api_duration"))
            finish_reason = _string(payload.get("finish_reason"), 200)
            missing_request = "[request start event missing]"
            values = {
                "gen_ai.provider.name": provider,
                "gen_ai.response.model": payload.get("response_model") or model,
                "gen_ai.response.finish_reasons": [finish_reason] if finish_reason else None,
                "gen_ai.output.messages": response_messages_json(response, self.settings),
                "output.value": captured_value(response, self.settings),
                "output.mime_type": "text/plain",
                "hermes.api.attempt": attempt,
                "hermes.api.duration_ms": duration * 1000 if duration is not None else None,
                "hermes.api.assistant_content_chars": _integer(
                    payload.get("assistant_content_chars")
                ),
                "hermes.api.assistant_tool_call_count": _integer(
                    payload.get("assistant_tool_call_count")
                ),
                **_usage_attributes(payload.get("usage")),
            }
            child = self._finish_child_locked(
                payload,
                category="api",
                identity=identity,
                fallback_name=f"chat {model}",
                fallback_kind=SpanKind.CLIENT,
                fallback_values={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": provider,
                    "gen_ai.request.model": model,
                    "gen_ai.input.messages": messages_json(
                        "user",
                        missing_request,
                        self.settings,
                    ),
                    "input.value": captured_value(missing_request, self.settings),
                    "input.mime_type": "text/plain",
                    "openinference.span.kind": "LLM",
                    "hermes.api.request_id": identity,
                    "hermes.api.attempt": attempt,
                },
                final_values=values,
            )
            root = self._turns.get(child.root_key)
            if root is not None:
                self._update_root_model_provider_locked(
                    root,
                    model=model,
                    provider=provider,
                )

    @_while_accepting_events
    def on_api_request_error(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            identity = self._api_identity(payload)
            root = self._ensure_turn_locked(payload)
            retry_count = _integer(payload.get("retry_count"))
            attempt = self._record_api_attempt_locked(
                root,
                identity,
                starting=False,
                reported_retry_count=retry_count,
            )
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            error_type = _error_type(
                error.get("type") or payload.get("error_type") or "api_error",
                "api_error",
            )
            error_message = _string(
                error.get("message") or payload.get("error_message") or payload.get("reason"),
                1_000,
            )
            model = _string(payload.get("model"), 300) or "unknown"
            provider = _string(payload.get("provider") or payload.get("platform") or "unknown", 200)
            duration = _float(payload.get("api_duration"))
            status_code = _integer(payload.get("status_code"))
            missing_request = "[request start event missing]"
            values = {
                "http.response.status_code": status_code,
                "gen_ai.provider.name": provider,
                "hermes.api.duration_ms": duration * 1000 if duration is not None else None,
                "hermes.api.attempt": attempt,
                "hermes.retry.count": retry_count,
                "hermes.retry.max": _integer(payload.get("max_retries")),
                "hermes.retryable": payload.get("retryable"),
            }
            child = self._finish_child_locked(
                payload,
                category="api",
                identity=identity,
                fallback_name=f"chat {model}",
                fallback_kind=SpanKind.CLIENT,
                fallback_values={
                    "gen_ai.operation.name": "chat",
                    "gen_ai.provider.name": provider,
                    "gen_ai.request.model": model,
                    "gen_ai.input.messages": messages_json(
                        "user",
                        missing_request,
                        self.settings,
                    ),
                    "input.value": captured_value(missing_request, self.settings),
                    "input.mime_type": "text/plain",
                    "openinference.span.kind": "LLM",
                    "hermes.api.request_id": identity,
                    "hermes.api.attempt": attempt,
                },
                final_values=values,
                error_type=error_type,
                error_message=error_message,
            )
            root = self._turns.get(child.root_key)
            if root is not None:
                self._update_root_model_provider_locked(
                    root,
                    model=model,
                    provider=provider,
                )

    @_while_accepting_events
    def on_pre_tool_call(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            identity = self._tool_identity(payload)
            tool_name = _string(payload.get("tool_name"), 300) or "unknown"
            arguments = payload.get("args") or {}
            captured_arguments = captured_value(arguments, self.settings)
            child = self._start_child_locked(
                payload,
                category="tool",
                identity=identity,
                name=f"execute_tool {tool_name}",
                kind=SpanKind.INTERNAL,
                values={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool_name,
                    "gen_ai.tool.call.id": identity,
                    "gen_ai.tool.call.arguments": captured_arguments,
                    "gen_ai.input.messages": messages_json("tool", arguments, self.settings),
                    "input.value": captured_arguments,
                    "input.mime_type": "text/plain",
                    "openinference.span.kind": "TOOL",
                    "hermes.turn.id": payload.get("turn_id"),
                    "hermes.api.request_id": payload.get("api_request_id"),
                },
            )
            root = self._turns.get(child.root_key)
            if root is not None:
                root.tool_count += 1
                root.tool_names.add(tool_name)

    @_while_accepting_events
    def on_post_tool_call(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            identity = self._tool_identity(payload)
            tool_name = _string(payload.get("tool_name"), 300) or "unknown"
            arguments = payload.get("args") or {}
            result = payload.get("result")
            outcome = _string(payload.get("status") or "ok", 100).lower()
            error_type = ""
            error_message = ""
            if outcome not in {"ok", "success", "completed"}:
                error_type = _error_type(
                    payload.get("error_type") or outcome or "tool_error",
                    "tool_error",
                )
                error_message = _string(
                    payload.get("error_message") or result or error_type,
                    1_000,
                )
            captured_result = captured_value(result, self.settings)
            child = self._finish_child_locked(
                payload,
                category="tool",
                identity=identity,
                fallback_name=f"execute_tool {tool_name}",
                fallback_kind=SpanKind.INTERNAL,
                fallback_values={
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool_name,
                    "gen_ai.tool.call.id": identity,
                    "gen_ai.tool.call.arguments": captured_value(arguments, self.settings),
                    "gen_ai.input.messages": messages_json("tool", arguments, self.settings),
                    "input.value": captured_value(arguments, self.settings),
                    "input.mime_type": "text/plain",
                    "openinference.span.kind": "TOOL",
                },
                final_values={
                    "gen_ai.tool.call.result": captured_result,
                    "gen_ai.output.messages": messages_json("tool", result, self.settings),
                    "output.value": captured_result,
                    "output.mime_type": "text/plain",
                    "hermes.tool.duration_ms": _integer(payload.get("duration_ms")),
                    "hermes.tool.status": outcome,
                },
                error_type=error_type,
                error_message=error_message,
            )
            root = self._turns.get(child.root_key)
            if root is not None and root.tool_count == 0:
                root.tool_count = 1
                root.tool_names.add(tool_name)

    @_while_accepting_events
    def on_pre_approval_request(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            identity = self._approval_identity(payload)
            command = payload.get("command")
            root = self._ensure_turn_locked(payload)
            parent_span = root.span
            tool_call_id = _string(payload.get("tool_call_id"), 500)
            if tool_call_id:
                tool = self._find_child_locked(
                    payload,
                    category="tool",
                    identity=tool_call_id,
                )
                if tool is not None:
                    parent_span = tool.span
            self._start_child_locked(
                payload,
                category="approval",
                identity=identity,
                name="approval_request",
                kind=SpanKind.INTERNAL,
                values={
                    "hermes.approval.pattern": payload.get("pattern_key"),
                    "hermes.approval.surface": payload.get("surface"),
                    "hermes.approval.command": captured_value(command, self.settings),
                    "input.value": captured_value(command, self.settings),
                },
                parent_span=parent_span,
            )

    @_while_accepting_events
    def on_post_approval_response(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            identity = self._approval_identity(payload)
            choice = _string(payload.get("choice") or "unknown", 100)
            self._finish_child_locked(
                payload,
                category="approval",
                identity=identity,
                fallback_name="approval_request",
                fallback_kind=SpanKind.INTERNAL,
                fallback_values={
                    "input.value": captured_value(payload.get("command"), self.settings)
                },
                final_values={
                    "hermes.approval.choice": choice,
                    "hermes.approval.decided_by": payload.get("decided_by"),
                    "output.value": choice,
                },
            )

    @_while_accepting_events
    def on_subagent_start(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            child_session_id = _string(payload.get("child_session_id"), 500)
            if not child_session_id:
                return
            parent_payload = dict(payload)
            parent_payload["session_id"] = payload.get("parent_session_id")
            parent = self._ensure_turn_locked(parent_payload)
            existing = self._delegations.pop(child_session_id, None)
            if existing:
                self._end_span(
                    existing.span,
                    error_type="duplicate_subagent_start",
                    error_message="Duplicate subagent start",
                )
            role = _string(payload.get("child_role") or "subagent", 200)
            goal = payload.get("child_goal")
            span = self._start_span(
                f"invoke_agent {role}",
                parent=parent.span,
                kind=SpanKind.INTERNAL,
                attributes={
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.provider.name": "hermes",
                    "gen_ai.agent.name": role,
                    "gen_ai.input.messages": messages_json("user", goal, self.settings),
                    "input.value": captured_value(goal, self.settings),
                    "openinference.span.kind": "AGENT",
                    "hermes.subagent.parent_session_id": payload.get("parent_session_id"),
                    "hermes.subagent.child_session_id": child_session_id,
                    "hermes.subagent.parent_turn_id": payload.get("parent_turn_id"),
                    "hermes.subagent.parent_id": payload.get("parent_subagent_id"),
                    "hermes.subagent.child_id": payload.get("child_subagent_id"),
                    "hermes.subagent.role": role,
                },
            )
            self._delegations[child_session_id] = _DelegationState(
                child_session_id=child_session_id,
                parent_root_key=parent.key,
                span=span,
                role=role,
                started_monotonic=time.monotonic(),
            )

    @_while_accepting_events
    def on_subagent_stop(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            child_session_id = _string(payload.get("child_session_id"), 500)
            delegation = self._delegations.pop(child_session_id, None)
            if delegation is None:
                return
            status = _string(payload.get("child_status") or "completed", 100).lower()
            failed = status in {"error", "failed", "failure", "cancelled", "timeout"}
            summary = payload.get("child_summary")
            self._end_span(
                delegation.span,
                values={
                    "gen_ai.output.messages": messages_json("assistant", summary, self.settings),
                    "output.value": captured_value(summary, self.settings),
                    "hermes.subagent.status": status,
                    "hermes.subagent.duration_ms": _float(payload.get("duration_ms")),
                },
                error_type=status if failed else "",
                error_message=_string(summary, 1_000) if failed else "",
            )

    @_while_accepting_events
    def on_session_end(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            state = self._resolve_turn_locked(payload)
            if state is None:
                return
            completed = bool(payload.get("completed"))
            interrupted = bool(payload.get("interrupted"))
            if completed:
                final_status = "completed"
                error_type = ""
            elif interrupted:
                final_status = "interrupted"
                error_type = ""
            else:
                final_status = "incomplete"
                error_type = "incomplete"
            self._finish_turn_locked(
                state.key,
                output=state.output,
                final_status=final_status,
                error_type=error_type,
                error_message=_string(payload.get("reason") or final_status, 500),
                request_flush=True,
            )

    @_while_accepting_events
    def on_session_finalize(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.sweep_expired_locked()
            session_id = _string(payload.get("session_id"), 500)
            key = self._active_by_session.get(session_id)
            if key:
                self._finish_turn_locked(
                    key,
                    output=None,
                    final_status="finalized",
                    error_type="",
                    error_message="",
                    request_flush=True,
                )
            self._session_metadata.pop(session_id, None)

    @_while_accepting_events
    def on_session_reset(self, payload: dict[str, Any]) -> None:
        old_session_id = _string(payload.get("old_session_id") or payload.get("session_id"), 500)
        with self._lock:
            self.sweep_expired_locked()
            key = self._active_by_session.get(old_session_id)
            if key:
                self._finish_turn_locked(
                    key,
                    output=None,
                    final_status="reset",
                    error_type="",
                    error_message="",
                    request_flush=True,
                )
            self._session_metadata.pop(old_session_id, None)

    # ------------------------------------------------------------------
    # Cleanup, flushing, and diagnostics
    # ------------------------------------------------------------------

    def _finish_turn_locked(
        self,
        key: str,
        *,
        output: Any,
        final_status: str,
        error_type: str,
        error_message: str,
        request_flush: bool,
    ) -> None:
        state = self._turns.pop(key, None)
        if state is None:
            return

        abandoned = [child for child in self._children.values() if child.root_key == key]
        for child in abandoned:
            self._children.pop(child.key, None)
            self._end_span(
                child.span,
                error_type="abandoned",
                error_message=f"{child.category} span was still open when the turn ended",
            )
            self._stats["orphaned_spans"] += 1

        dangling_delegations = [
            delegation
            for delegation in self._delegations.values()
            if delegation.parent_root_key == key
        ]
        for delegation in dangling_delegations:
            self._delegations.pop(delegation.child_session_id, None)
            self._end_span(
                delegation.span,
                error_type="abandoned_subagent",
                error_message="Parent turn ended before the subagent stop event",
            )
            self._stats["orphaned_spans"] += 1

        final_output = output if output is not None else state.output
        self._end_span(
            state.span,
            values={
                "gen_ai.output.messages": messages_json("assistant", final_output, self.settings),
                "output.value": captured_value(final_output, self.settings),
                "output.mime_type": "text/plain",
                "hermes.turn.final_status": final_status,
                "hermes.turn.api_call_count": state.api_count,
                "hermes.turn.tool_count": state.tool_count,
                "hermes.turn.tool_names": sorted(state.tool_names),
                "hermes.turn.error_count": state.error_count,
                "hermes.turn.duration_ms": (time.monotonic() - state.started_monotonic) * 1000,
            },
            error_type=error_type,
            error_message=error_message,
        )
        if state.session_id and self._active_by_session.get(state.session_id) == key:
            self._active_by_session.pop(state.session_id, None)
        self._stats["turns_finished"] += 1

        if request_flush and self.settings.async_flush_on_turn_end:
            if self._flusher is not None:
                self._flusher.request()
            else:
                # Tests may deliberately disable the thread but still exercise
                # the configured turn-end behavior.
                logger.debug("Async turn-end flush requested without a flush worker")

    def sweep_expired_locked(self) -> int:
        cutoff = time.monotonic() - self.settings.turn_ttl_seconds
        expired = [
            state.key for state in self._turns.values() if state.last_updated_monotonic < cutoff
        ]
        for key in expired:
            self._finish_turn_locked(
                key,
                output=None,
                final_status="timed_out",
                error_type="timeout",
                error_message="Hermes turn exceeded telemetry state TTL",
                request_flush=True,
            )
        return len(expired)

    def sweep_expired(self) -> int:
        with self._lock:
            return self.sweep_expired_locked()

    def force_flush(self) -> bool:
        timeout = self.settings.flush_timeout_millis
        try:
            if self._processor is not None and hasattr(self._processor, "force_flush"):
                result = self._processor.force_flush(timeout)
            elif hasattr(self._provider, "force_flush"):
                result = self._provider.force_flush(timeout)
            else:
                return True
            return True if result is None else bool(result)
        except Exception:
            logger.warning("Galileo force_flush failed", exc_info=True)
            return False

    def health_snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = {
                "enabled": not self._shutdown,
                "project": self.settings.project,
                "log_stream": self.settings.log_stream,
                "capture_content": self.settings.capture_content,
                "sample_rate": self.settings.sample_rate,
                "inflight_turns": len(self._turns),
                "inflight_child_spans": len(self._children),
                "inflight_subagents": len(self._delegations),
                "provider_cleanup_deferred": self._provider_cleanup_deferred,
                **self._stats,
            }
            if hasattr(self._processor, "health_snapshot"):
                snapshot.update(self._processor.health_snapshot())
            return snapshot

    def _shutdown_owned_provider(self) -> None:
        if not hasattr(self._provider, "shutdown"):
            return
        try:
            self._provider.shutdown()
        except Exception:
            logger.warning("Galileo tracer provider shutdown failed", exc_info=True)

    def _shutdown_provider_after_flusher(self) -> None:
        assert self._flusher is not None
        self._flusher.wait_until_stopped()
        self._shutdown_owned_provider()
        with self._lock:
            self._provider_cleanup_deferred = False

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            for key in list(self._turns):
                self._finish_turn_locked(
                    key,
                    output=None,
                    final_status="shutdown",
                    error_type="shutdown",
                    error_message="Hermes process stopped before the turn completed",
                    request_flush=False,
                )
        flusher_stopped = True
        if self._flusher is not None:
            flusher_stopped = self._flusher.shutdown(self.settings.flush_timeout_millis / 1_000)
        if self._owns_provider:
            if flusher_stopped:
                self._shutdown_owned_provider()
            else:
                # A Galileo force_flush is already in progress. Never invoke
                # processor shutdown concurrently; finish provider cleanup on
                # a daemon after the bounded runtime shutdown call returns.
                logger.warning(
                    "Galileo background flush exceeded the shutdown deadline; "
                    "provider cleanup will finish asynchronously"
                )
                with self._lock:
                    self._provider_cleanup_deferred = True
                self._provider_cleanup_thread = threading.Thread(
                    target=self._shutdown_provider_after_flusher,
                    name="hermes-galileo-provider-cleanup",
                    daemon=True,
                )
                self._provider_cleanup_thread.start()
        else:
            # An injected provider belongs to the caller, so only request a
            # best-effort queue drain and leave its lifecycle untouched.
            if flusher_stopped:
                self.force_flush()
